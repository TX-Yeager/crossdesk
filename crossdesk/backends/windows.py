"""Windows desktop input backend using only the Python standard library.

``handler(event)`` runs on the dedicated low-level-hook thread. It must return
immediately (put events into a queue; never do network I/O here). Return True to
suppress the original physical event. Injected events carrying this backend's
tag are always passed through and never sent to the handler.

Coordinates are physical pixels, including negative monitor origins. Windows
secure desktops, UAC prompts, Ctrl+Alt+Delete and higher-integrity applications
are outside a normal desktop process's input privileges.

Mouse movement uses the last committed cursor position. Passed physical moves
and injected moves advance this reference; suppressed physical moves do not.
All injected mouse events pass locally without taking remote input ownership.
"""

from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from .. import diagnostics


_LOG = logging.getLogger(__name__)
_WARP_TIMEOUT = 0.3
_ULONG_PTR = ctypes.c_size_t
_LRESULT = ctypes.c_ssize_t
_HOOKPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    _LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)
_MONITORPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
    ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
)


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT),
                ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", _INPUTUNION)]


class _MONITORINFOEX(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


# Names describe physical keys; the receiving computer supplies its own layout
# and IME. No text, password or keystroke log is retained by this backend.
_VK_TO_KEY = {
    0x08: "backspace", 0x09: "tab", 0x0C: "clear", 0x0D: "enter",
    0x13: "pause", 0x14: "caps_lock", 0x1B: "escape", 0x20: "space",
    0x21: "page_up", 0x22: "page_down", 0x23: "end", 0x24: "home",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x2C: "print_screen", 0x2D: "insert", 0x2E: "delete",
    0x5B: "meta_l", 0x5C: "meta_r", 0x5D: "menu",
    0x6A: "keypad_multiply", 0x6B: "keypad_add", 0x6C: "keypad_separator",
    0x6D: "keypad_subtract", 0x6E: "keypad_decimal", 0x6F: "keypad_divide",
    0x90: "num_lock", 0x91: "scroll_lock",
    0xA0: "shift_l", 0xA1: "shift_r", 0xA2: "ctrl_l", 0xA3: "ctrl_r",
    0xA4: "alt_l", 0xA5: "alt_r",
    0xAD: "volume_mute", 0xAE: "volume_down", 0xAF: "volume_up",
    0xB0: "media_next", 0xB1: "media_previous", 0xB2: "media_stop",
    0xB3: "media_play_pause",
    0xBA: "semicolon", 0xBB: "equal", 0xBC: "comma", 0xBD: "minus",
    0xBE: "period", 0xBF: "slash", 0xC0: "grave", 0xDB: "bracket_left",
    0xDC: "backslash", 0xDD: "bracket_right", 0xDE: "apostrophe",
    0xE2: "intl_backslash",
}
_VK_TO_KEY.update({0x41 + n: chr(0x61 + n) for n in range(26)})
_VK_TO_KEY.update({0x30 + n: str(n) for n in range(10)})
_VK_TO_KEY.update({0x70 + n: f"f{n + 1}" for n in range(24)})
_VK_TO_KEY.update({0x60 + n: f"keypad_{n}" for n in range(10)})
_KEY_TO_VK = {key: vk for vk, key in _VK_TO_KEY.items()}
_KEY_TO_VK["keypad_enter"] = 0x0D
# Windows has no general VK for the Mac numeric keypad equals key. The ordinary
# equals key is the closest layout-aware equivalent for incoming Mac events.
_KEY_TO_VK["keypad_equal"] = 0xBB
_EXTENDED_KEYS = {"ctrl_r", "alt_r", "meta_l", "meta_r", "menu", "insert",
                  "delete", "home", "end", "page_up", "page_down", "left",
                  "right", "up", "down", "keypad_divide", "keypad_enter",
                  "num_lock", "print_screen"}
_BUTTON_MESSAGES = {
    0x0201: ("left", True), 0x0202: ("left", False),
    0x0204: ("right", True), 0x0205: ("right", False),
    0x0207: ("middle", True), 0x0208: ("middle", False),
}
_BUTTON_FLAGS = {
    "left": (0x0002, 0x0004, 0), "right": (0x0008, 0x0010, 0),
    "middle": (0x0020, 0x0040, 0), "back": (0x0080, 0x0100, 1),
    "forward": (0x0080, 0x0100, 2),
}


@dataclass
class _WarpPending:
    target: tuple[float, float]
    captured: bool
    seen: threading.Event = field(default_factory=threading.Event)


class WindowsBackend:
    """Capture and synthesize normal desktop mouse and keyboard events.

    ``start`` raises if either hook cannot be installed. Callback exceptions
    disable capture immediately, pass the triggering event locally, and remove
    both hooks. ``last_error`` exposes runtime failure; ``running`` indicates
    whether the hook thread is alive. No hook is installed at construction.
    """

    needs_remote_anchor = True

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("WindowsBackend requires Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()
        self._tag = (0x43524453 << 16 | (os.getpid() & 0xFFFF)) & (
            (1 << (ctypes.sizeof(_ULONG_PTR) * 8)) - 1
        )
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._handler: Callable[[dict], bool] | None = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._last_error: BaseException | None = None
        self._mouse_hook = None
        self._keyboard_hook = None
        self._mouse_proc = _HOOKPROC(self._mouse_callback)
        self._keyboard_proc = _HOOKPROC(self._keyboard_callback)
        self._input_lock = threading.RLock()
        self._pressed_keys: set[str] = set()
        self._pressed_buttons: set[str] = set()
        self._wheel_residual = [0.0, 0.0]
        self._reference: tuple[int, int] | None = None
        self._warp_pending: _WarpPending | None = None
        self._reset_metrics()
        self._set_process_dpi_awareness()

    def _configure_signatures(self) -> None:
        u, k = self._user32, self._kernel32
        specs = {
            "SetWindowsHookExW": ([ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE,
                                     wintypes.DWORD], wintypes.HANDLE),
            "UnhookWindowsHookEx": ([wintypes.HANDLE], wintypes.BOOL),
            "CallNextHookEx": ([wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM,
                                 wintypes.LPARAM], _LRESULT),
            "GetMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                              wintypes.UINT, wintypes.UINT], ctypes.c_int),
            "PeekMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            "TranslateMessage": ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
            "DispatchMessageW": ([ctypes.POINTER(wintypes.MSG)], _LRESULT),
            "PostThreadMessageW": ([wintypes.DWORD, wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM], wintypes.BOOL),
            "PostQuitMessage": ([ctypes.c_int], None),
            "GetCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "GetPhysicalCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "SetPhysicalCursorPos": ([ctypes.c_int,ctypes.c_int],wintypes.BOOL),
            "GetClipCursor": ([ctypes.POINTER(wintypes.RECT)],wintypes.BOOL),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
            "EnumDisplayMonitors": ([wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                      _MONITORPROC, wintypes.LPARAM], wintypes.BOOL),
            "GetMonitorInfoW": ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
            "SendInput": ([wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int], wintypes.UINT),
        }
        for name, (args, result) in specs.items():
            function = getattr(u, name)
            function.argtypes, function.restype = args, result
        k.GetCurrentThreadId.argtypes, k.GetCurrentThreadId.restype = [], wintypes.DWORD
        k.GetModuleHandleW.argtypes, k.GetModuleHandleW.restype = [wintypes.LPCWSTR], wintypes.HMODULE
        if hasattr(u, "SetThreadDpiAwarenessContext"):
            u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        if hasattr(u, "SetProcessDpiAwarenessContext"):
            u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        u.SetProcessDPIAware.argtypes, u.SetProcessDPIAware.restype = [], wintypes.BOOL

    def _set_process_dpi_awareness(self) -> None:
        # ERROR_ACCESS_DENIED means a manifest or GUI framework already chose a
        # process awareness. Per-thread contexts below still keep our APIs physical.
        if hasattr(self._user32, "SetProcessDpiAwarenessContext"):
            self._user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        else:
            self._user32.SetProcessDPIAware()

    @contextlib.contextmanager
    def _physical_coordinates(self):
        setter = getattr(self._user32, "SetThreadDpiAwarenessContext", None)
        previous = setter(ctypes.c_void_p(-4)) if setter else None
        try:
            yield
        finally:
            if previous:
                setter(previous)

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._handler is not None)

    def supported_keys(self) -> set[str]:
        """Wire key names this Windows receiver can inject."""
        return set(_KEY_TO_VK)

    def _reset_metrics(self) -> None:
        self._move_seen = 0
        self._move_zero_delta = 0
        self._move_nonzero_delta = 0
        self._own_tag_skipped = 0
        self._callback_failures = 0

    def metrics(self) -> dict[str, int]:
        """Aggregate counts only; never expose positions, keys or payloads."""
        return {name: getattr(self, "_" + name, 0)
                for name in ("move_seen", "move_zero_delta", "move_nonzero_delta",
                             "own_tag_skipped", "callback_failures")}

    def screens(self) -> list[dict]:
        result: list[dict] = []
        errors: list[BaseException] = []
        try:
            shcore = ctypes.WinDLL("shcore", use_last_error=True)
            get_dpi = shcore.GetDpiForMonitor
            get_dpi.argtypes = [wintypes.HANDLE, ctypes.c_int,
                               ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
            get_dpi.restype = ctypes.c_long
        except (OSError, AttributeError):
            get_dpi = None

        @_MONITORPROC
        def collect(monitor, _dc, _rect, _data):
            try:
                info = _MONITORINFOEX()
                info.cbSize = ctypes.sizeof(info)
                if not self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                rect = info.rcMonitor
                scale = 1.0
                if get_dpi:
                    dx, dy = wintypes.UINT(), wintypes.UINT()
                    if get_dpi(monitor, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
                        scale = dx.value / 96.0
                result.append({"x": rect.left, "y": rect.top,
                               "width": rect.right - rect.left,
                               "height": rect.bottom - rect.top,
                               "name": info.szDevice, "scale": scale,
                               "primary": bool(info.dwFlags & 1)})
                return True
            except Exception as exc:
                errors.append(exc)
                return False

        with self._physical_coordinates():
            ok = self._user32.EnumDisplayMonitors(None, None, collect, 0)
        if errors:
            raise errors[0]
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def position(self) -> tuple[int, int]:
        point = wintypes.POINT()
        with self._physical_coordinates():
            getter=getattr(self._user32,'GetPhysicalCursorPos',self._user32.GetCursorPos)
            if not getter(ctypes.byref(point)):
                raise ctypes.WinError(ctypes.get_last_error())
        return point.x, point.y

    def start(self, handler: Callable[[dict], bool]) -> None:
        if not callable(handler):
            raise TypeError("Input handler must be callable")
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Windows input capture is already running")
        self._cancel_warp()
        # Read once before capture starts. Reading GetCursorPos for the same
        # event inside the hook is not a stable relative-motion baseline.
        self._reference = self.position()
        self._reset_metrics()
        self._last_error = None
        self.failure_reason = None
        self._ready.clear()
        self._stop_requested.clear()
        self._handler = handler
        self._thread = threading.Thread(target=self._hook_loop, name="crossdesk-win-hooks", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            self.stop()
            raise RuntimeError("Windows input hook startup timed out")
        if self._last_error:
            self._handler = None
            raise OSError(f"Cannot install Windows input hooks: {self._last_error}") from self._last_error

    def _hook_loop(self) -> None:
        try:
            with self._physical_coordinates():
                self._thread_id = self._kernel32.GetCurrentThreadId()
                message = wintypes.MSG()
                # Force creation of a thread message queue before start() returns.
                self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
                module = self._kernel32.GetModuleHandleW(None)
                self._mouse_hook = self._user32.SetWindowsHookExW(14, self._mouse_proc, module, 0)
                if not self._mouse_hook:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._keyboard_hook = self._user32.SetWindowsHookExW(13, self._keyboard_proc, module, 0)
                if not self._keyboard_hook:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._ready.set()
                while not self._stop_requested.is_set():
                    status = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                    if status == 0:
                        break
                    if status == -1:
                        raise ctypes.WinError(ctypes.get_last_error())
                    self._user32.TranslateMessage(ctypes.byref(message))
                    self._user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._last_error = exc
            _LOG.exception("Windows input capture stopped; local input is available")
        finally:
            self._handler = None
            self._cancel_warp()
            for name in ("_mouse_hook", "_keyboard_hook"):
                handle = getattr(self, name)
                if handle:
                    self._user32.UnhookWindowsHookEx(handle)
                    setattr(self, name, None)
            self._thread_id = 0
            self._ready.set()
            self.release_all()

    def _fail_open(self, exc: BaseException) -> None:
        self._last_error = exc
        self._handler = None
        self._cancel_warp()
        self._stop_requested.set()
        self._user32.PostQuitMessage(1)
        _LOG.error("Input handler failed; hooks are being removed")

    def _mouse_callback(self, code, message, address):
        if code < 0 or self._handler is None:
            return self._user32.CallNextHookEx(None, code, message, address)
        try:
            data = ctypes.cast(address, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            if data.dwExtraInfo == self._tag:
                self._own_tag_skipped = getattr(self, "_own_tag_skipped", 0) + 1
                if message == 0x0200:
                    self._reference = data.pt.x, data.pt.y
                    pending = getattr(self, "_warp_pending", None)
                    if pending and self._near_point(self._reference, pending.target):
                        pending.seen.set()
                return self._user32.CallNextHookEx(None, code, message, address)
            if data.flags & 3:
                # Foreign injected mouse events are local input, not physical
                # ownership requests. Follow their location without forwarding.
                if message == 0x0200:
                    self._reference = data.pt.x, data.pt.y
                return self._user32.CallNextHookEx(None, code, message, address)
            event = None
            point = None
            if message == 0x0200:
                self._move_seen = getattr(self, "_move_seen", 0) + 1
                point = data.pt.x, data.pt.y
                pending=getattr(self,'_warp_pending',None)
                if pending is not None and self._near_point(point,pending.target):
                    return self._user32.CallNextHookEx(None,code,message,address)
                reference = getattr(self, "_reference", None)
                if reference is None:
                    # Fail open if a lightweight test backend or unexpected
                    # callback lacks the startup reference; never guess a delta.
                    self._reference = reference = point
                dx, dy = point[0] - reference[0], point[1] - reference[1]
                if not dx and not dy:
                    self._move_zero_delta = getattr(self, "_move_zero_delta", 0) + 1
                    if getattr(self, "_warp_pending", None) is not None:
                        return 1
                    return self._user32.CallNextHookEx(None, code, message, address)
                self._move_nonzero_delta = getattr(self, "_move_nonzero_delta", 0) + 1
                # The old edge and new parking/return point use different
                # absolute baselines. Never forward a sample between them.
                # Own tagged moves above still pass and acknowledge the warp.
                if getattr(self, "_warp_pending", None) is not None:
                    return 1
                event = {"type": "move", "x": data.pt.x, "y": data.pt.y,
                         "dx": dx, "dy": dy}
            elif message in _BUTTON_MESSAGES:
                button, down = _BUTTON_MESSAGES[message]
                event = {"type": "button", "button": button, "down": down}
            elif message in (0x020B, 0x020C):
                button = {1: "back", 2: "forward"}.get((data.mouseData >> 16) & 0xFFFF)
                if button:
                    event = {"type": "button", "button": button, "down": message == 0x020B}
            elif message in (0x020A, 0x020E):
                delta = ctypes.c_short(data.mouseData >> 16).value / 120.0
                event = {"type": "scroll", "dx": delta if message == 0x020E else 0,
                         "dy": delta if message == 0x020A else 0}
            handler = self._handler
            if event is not None and handler is not None and handler(event):
                return 1
            if point is not None:
                self._reference = point
        except BaseException as exc:
            self._callback_failures = getattr(self, "_callback_failures", 0) + 1
            self._fail_open(exc)
        return self._user32.CallNextHookEx(None, code, message, address)

    def _keyboard_callback(self, code, message, address):
        if code < 0 or self._handler is None:
            return self._user32.CallNextHookEx(None, code, message, address)
        try:
            data = ctypes.cast(address, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            if data.dwExtraInfo == self._tag:
                self._own_tag_skipped = getattr(self, "_own_tag_skipped", 0) + 1
                return self._user32.CallNextHookEx(None, code, message, address)
            key = _VK_TO_KEY.get(data.vkCode)
            if data.vkCode == 0x10:
                key = "shift_r" if data.scanCode == 0x36 else "shift_l"
            elif data.vkCode == 0x11:
                key = "ctrl_r" if data.flags & 1 else "ctrl_l"
            elif data.vkCode == 0x12:
                key = "alt_r" if data.flags & 1 else "alt_l"
            elif data.vkCode == 0x0D and data.flags & 1:
                key = "keypad_enter"
            # Unknown or IME packet keys must remain available locally.
            handler = self._handler
            if key and handler and message in (0x0100, 0x0101, 0x0104, 0x0105):
                if handler({"type": "key", "key": key, "down": message in (0x0100, 0x0104)}):
                    return 1
        except BaseException as exc:
            self._callback_failures = getattr(self, "_callback_failures", 0) + 1
            self._fail_open(exc)
        return self._user32.CallNextHookEx(None, code, message, address)

    def stop(self) -> None:
        self._handler = None  # Stop suppressing input before asking the loop to exit.
        self._cancel_warp()
        self._stop_requested.set()
        thread = self._thread
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        if thread and thread is not threading.current_thread():
            thread.join(3.0)
            if thread.is_alive():
                self._last_error = RuntimeError("Input hook thread did not stop promptly")
                _LOG.error("Input hook thread did not stop promptly; capture has been disabled")
        self.release_all()

    @staticmethod
    def _finite(value) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Input coordinates and deltas must be finite")
        return number

    def _send(self, item: _INPUT) -> None:
        if self._user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT)) != 1:
            error = ctypes.get_last_error()
            if error:
                raise ctypes.WinError(error)
            raise OSError("SendInput was blocked (the target may have higher integrity or use a secure desktop)")

    def _mouse_input(self, flags: int, data: int = 0, x: int = 0, y: int = 0) -> None:
        item = _INPUT(type=0)
        item.mi = _MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, self._tag)
        self._send(item)

    @staticmethod
    def _near_point(actual, target) -> bool:
        # Absolute SendInput coordinates are quantized to physical pixels.
        return abs(actual[0] - target[0]) <= 1 and abs(actual[1] - target[1]) <= 1

    def _cancel_warp(self) -> None:
        pending = getattr(self, "_warp_pending", None)
        self._warp_pending = None
        if pending is not None:
            pending.seen.set()  # Wake a worker; identity check rejects cancellation.

    def _wait_warp(self, pending: _WarpPending) -> None:
        deadline = time.monotonic() + _WARP_TIMEOUT
        while True:
            if (getattr(self, "_warp_pending", None) is not pending
                    or pending.captured and self._handler is None):
                raise OSError("Cursor transition was cancelled")
            # Windows can commit an absolute move without delivering another
            # low-level mouse callback (notably at the same pixel). The actual
            # physical position is the confirmation; the tag is only advisory.
            point = self.position()
            if self._near_point(point, pending.target):
                if self._warp_pending is not pending:
                    raise OSError("Cursor transition was cancelled")
                self._reference = point
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError("Cursor transition did not complete in time")
            # Only the worker waits; the native callbacks remain nonblocking.
            time.sleep(min(.002, remaining))

    def _warp_failed(self, error: BaseException, stage='position') -> None:
        pending=getattr(self,'_warp_pending',None)
        detail=str(error)
        reason=('position_timeout' if 'did not complete' in detail else
                'position_cancelled' if 'cancelled' in detail else 'native_position_failed')
        if stage=='presentation_cleanup':reason='input_restore_failed'
        fields={'reason':reason,'hook_seen':bool(pending and pending.seen.is_set()),
                'native_code':getattr(error,'winerror',0) or 0}
        if pending is not None and stage=='position':
            try:
                actual=self.position()
                fields.update(position_reached=self._near_point(actual,pending.target),
                              error_x=abs(actual[0]-pending.target[0]),
                              error_y=abs(actual[1]-pending.target[1]))
                rectangle=wintypes.RECT()
                if hasattr(self._user32,'GetClipCursor') and self._user32.GetClipCursor(ctypes.byref(rectangle)):
                    if not (rectangle.left<=pending.target[0]<rectangle.right and rectangle.top<=pending.target[1]<rectangle.bottom):
                        fields['reason']='cursor_clipped'
            except Exception:pass
        diagnostics.exception('native_handoff_failed',error,**fields)
        # This path runs on the worker, so PostQuitMessage would target the
        # wrong thread. Stop suppression before posting to the hook thread.
        self._last_error = error
        self.failure_reason = 'input_restore_failed' if stage=='presentation_cleanup' else 'position_failed'
        self._handler = None
        self._cancel_warp()
        stopping = getattr(self, "_stop_requested", None)
        if stopping is not None:
            stopping.set()
        thread_id = getattr(self, "_thread_id", 0)
        if thread_id:
            self._user32.PostThreadMessageW(thread_id, 0x0012, 0, 0)

    def _warp_to(self, x, y, before=None, *, wait=True) -> None:
        x, y = self._finite(x), self._finite(y)
        # Metrics are cheap enough for every pointer packet, and follow display
        # hot-plug changes without enumerating monitors on every movement.
        with self._physical_coordinates():
            left, top, width, height = (
                self._user32.GetSystemMetrics(index) for index in (76, 77, 78, 79)
            )
        if width <= 0 or height <= 0:
            raise OSError("No Windows display is available")
        # Target the centre of the selected physical pixel in the virtual desktop.
        nx = min(65535, max(0, int((x - left + 0.5) * 65536 / width)))
        ny = min(65535, max(0, int((y - top + 0.5) * 65536 / height)))
        with self._input_lock:
            if not wait:
                self._mouse_input(0x0001 | 0x8000 | 0x4000, x=nx, y=ny)
                return
            pending = _WarpPending((x, y), self._handler is not None)
            self._warp_pending = pending
            stage='presentation_cleanup' if before is not None else 'position'
            try:
                if before is not None:
                    before()
                stage='position'
                setter=getattr(self._user32,'SetPhysicalCursorPos',None)
                if setter is not None:
                    if not setter(round(x),round(y)):
                        raise ctypes.WinError(ctypes.get_last_error())
                else:
                    self._mouse_input(0x0001 | 0x8000 | 0x4000, x=nx, y=ny)
                self._wait_warp(pending)
            except BaseException as exc:
                self._warp_failed(exc,stage=stage)
                raise
            finally:
                if self._warp_pending is pending:
                    self._warp_pending = None

    def warp(self, x, y) -> None:
        self._warp_to(x, y)

    def park_remote(self,x,y) -> None:
        # Hiding is best effort; only failure to remove an input-receiving
        # presentation surface may abort the native transition.
        hide=getattr(self,"before_remote_anchor",None)
        self._warp_to(x,y,before=hide)

    def inject(self, event: dict) -> None:
        kind = event.get("type")
        if kind in ("button","scroll"):
            restore=getattr(self,"before_pointer_inject",None)
            if restore is not None:restore()
        with self._input_lock:
            if kind == "position":
                if event.get('handoff',True):self.warp(event["x"], event["y"])
                else:self._warp_to(event['x'],event['y'],wait=False)
                restore=getattr(self,"after_pointer_position",None)
                if restore is not None:restore(event["x"],event["y"])
            elif kind == "button":
                button = event["button"]
                if button not in _BUTTON_FLAGS:
                    raise ValueError(f"Unsupported mouse button: {button}")
                down_flag, up_flag, data = _BUTTON_FLAGS[button]
                down = bool(event["down"])
                self._mouse_input(down_flag if down else up_flag, data)
                if down:
                    self._pressed_buttons.add(button)
                else:
                    self._pressed_buttons.discard(button)
            elif kind == "scroll":
                # Retain sub-tick precision across small trackpad scroll events.
                for axis, flags, value in ((0, 0x1000, event.get("dx", 0)),
                                            (1, 0x0800, event.get("dy", 0))):
                    raw = self._finite(value) * 120 + self._wheel_residual[axis]
                    ticks = math.trunc(raw)
                    self._wheel_residual[axis] = raw - ticks
                    if ticks:
                        self._mouse_input(flags, ticks)
            elif kind == "key":
                key = str(event["key"])
                if key not in _KEY_TO_VK:
                    raise ValueError(f"Unsupported keyboard key: {key}")
                down = bool(event["down"])
                flags = (1 if key in _EXTENDED_KEYS else 0) | (0 if down else 2)
                item = _INPUT(type=1)
                item.ki = _KEYBDINPUT(_KEY_TO_VK[key], 0, flags, 0, self._tag)
                self._send(item)
                if down:
                    self._pressed_keys.add(key)
                else:
                    self._pressed_keys.discard(key)
            elif kind=='gesture':
                actions={'task_view':('meta_l','tab'),'start_menu':('meta_l',)}
                chord=actions.get(event.get('action'))
                if chord is None:raise ValueError('Unsupported Windows gesture')
                added=[]
                try:
                    for key in chord:
                        if key not in self._pressed_keys:
                            self.inject({'type':'key','key':key,'down':True})
                            added.append(key)
                finally:
                    for key in reversed(added):self.inject({'type':'key','key':key,'down':False})
            else:
                raise ValueError(f"Unsupported input event type: {kind}")

    def release_all(self) -> None:
        """Release only keys/buttons this instance injected, retaining failures."""
        with self._input_lock:
            for key in list(self._pressed_keys):
                try:
                    self.inject({"type": "key", "key": key, "down": False})
                except OSError:
                    _LOG.warning("Could not release an injected keyboard input")
            for button in list(self._pressed_buttons):
                try:
                    self.inject({"type": "button", "button": button, "down": False})
                except OSError:
                    _LOG.warning("Could not release an injected pointer input")
            self._wheel_residual[:] = [0.0, 0.0]
