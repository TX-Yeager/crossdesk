"""macOS Quartz input backend, using only Python's standard library.

Coordinates use Core Graphics global display points (origin at the main
display's top left). Mouse deltas are native Quartz deltas, never differences
between cursor locations: a captured pointer can therefore travel beyond a
display edge. Trackpad movement, clicks and two-finger scrolling are mouse
events. Recognized Dock vertical/pinch gestures can be mapped to Windows
commands while the global pointer belongs there; Mac-local gestures pass through.

The launching Terminal/Python app needs Accessibility and Input Monitoring
permissions. No root process, driver, or display configuration change is used.

Apple API references:
https://developer.apple.com/documentation/coregraphics/quartz-event-services
https://developer.apple.com/documentation/coregraphics/cgevent
https://developer.apple.com/documentation/coregraphics/cgeventsource/pixelsperline
https://developer.apple.com/documentation/coregraphics/cgwarpmousecursorposition(_:)
"""

from __future__ import annotations

import ctypes as C
import logging
import math
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any
from ..gestures import DockGesture

log = logging.getLogger(__name__)


class CGPoint(C.Structure):
    _fields_ = [("x", C.c_double), ("y", C.c_double)]


class CGSize(C.Structure):
    _fields_ = [("width", C.c_double), ("height", C.c_double)]


class CGRect(C.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


# Carbon virtual key codes describe physical US-layout positions. Text entry
# uses the receiving computer's active input source, including its IME.
KEY_CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6,
    "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "enter": 36, "l": 37,
    "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43,
    "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49,
    "`": 50, "backspace": 51, "escape": 53, "meta_r": 54,
    "meta_l": 55, "shift_l": 56, "caps_lock": 57, "alt_l": 58,
    "ctrl_l": 59, "shift_r": 60, "alt_r": 61, "ctrl_r": 62,
    "fn": 63, "f17": 64, "num_decimal": 65, "num_multiply": 67,
    "num_add": 69, "num_lock": 71, "num_divide": 75, "num_enter": 76,
    "num_subtract": 78, "f18": 79, "f19": 80, "num_equal": 81,
    "num_0": 82, "num_1": 83, "num_2": 84, "num_3": 85,
    "num_4": 86, "num_5": 87, "num_6": 88, "num_7": 89,
    "f20": 90, "num_8": 91, "num_9": 92, "f5": 96, "f6": 97,
    "f7": 98, "f3": 99, "f8": 100, "f9": 101, "f11": 103,
    "f13": 105, "f16": 106, "f14": 107, "f10": 109, "f12": 111,
    "f15": 113, "insert": 114, "home": 115, "page_up": 116,
    "delete": 117, "f4": 118, "end": 119, "f2": 120,
    "page_down": 121, "f1": 122, "left": 123, "right": 124,
    "down": 125, "up": 126,
}
# Use the same wire names as the Windows backend.
_CANONICAL_NAMES = {
    "-": "minus", "=": "equal", "[": "bracket_left", "]": "bracket_right",
    "\\": "backslash", ";": "semicolon", "'": "apostrophe", "`": "grave",
    ",": "comma", ".": "period", "/": "slash",
}
_CANONICAL_NAMES.update({key: key.replace("num_", "keypad_", 1)
                         for key in KEY_CODES if key.startswith("num_") and key != "num_lock"})
KEY_CODES = {_CANONICAL_NAMES.get(key, key): code for key, code in KEY_CODES.items()}
CODE_KEYS = {value: key for key, value in KEY_CODES.items()}
KEY_ALIASES = {
    "return": "enter", "esc": "escape", "command": "meta_l",
    "command_l": "meta_l", "command_r": "meta_r", "win_l": "meta_l",
    "win_r": "meta_r", "option_l": "alt_l", "option_r": "alt_r",
    "control_l": "ctrl_l", "control_r": "ctrl_r", "shift": "shift_l",
    "ctrl": "ctrl_l", "alt": "alt_l", "meta": "meta_l",
    "pageup": "page_up", "pagedown": "page_down",
}
KEY_ALIASES.update(_CANONICAL_NAMES)
MODIFIER_FLAGS = {
    "shift_l": 1 << 17, "shift_r": 1 << 17,
    "ctrl_l": 1 << 18, "ctrl_r": 1 << 18,
    "alt_l": 1 << 19, "alt_r": 1 << 19,
    "meta_l": 1 << 20, "meta_r": 1 << 20, "fn": 1 << 23,
}
BUTTON_IDS = {"left": 0, "right": 1, "middle": 2, "back": 3, "forward": 4}
ID_BUTTONS = {value: key for key, value in BUTTON_IDS.items()}
MOVE_TYPES = {5, 6, 7, 27}
DOWN_TYPES = {1, 3, 25}
UP_TYPES = {2, 4, 26}
EVENT_TYPES = MOVE_TYPES | DOWN_TYPES | UP_TYPES | {10, 11, 12, 22, 30}
USER_DATA = 42
TAG = 0x43524F5353440000 | (os.getpid() & 0xFFFF)
TAP_CALLBACK = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p, C.c_void_p)


class MacOSBackend:
    """Capture physical input; return True from handler to suppress it.

    ``start`` raises PermissionError/RuntimeError synchronously on setup errors.
    ``last_error`` records subsequent callback/run-loop errors. The callback must
    do bounded, quick work (for example queue an event); do not perform network
    I/O there. Any callback exception fails open, restoring local input.
    """

    # Outgoing movement uses Quartz deltas rather than differences between
    # cursor positions. Do not recenter an outgoing cursor: a native warp can
    # affect a later hardware event's delta and corrupt remote motion.
    needs_remote_anchor = False
    smooth_wheel = True

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise OSError("MacOSBackend is available only on macOS")
        self.cg = C.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.ax = C.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self._bind()
        self._source = self.cg.CGEventSourceCreate(-1)  # Private, isolated state.
        if not self._source:
            raise RuntimeError("CGEventSourceCreate failed")
        self.cg.CGEventSourceSetUserData(self._source, TAG)
        # Posting must not suppress real hardware for Quartz's default interval.
        self.cg.CGEventSourceSetLocalEventsSuppressionInterval(self._source, 0.0)
        self._pixels_per_line = self.cg.CGEventSourceGetPixelsPerLine(self._source) or 10.0
        self._scroll_point_residual = [0.,0.]
        self._handler: Callable[[dict[str, Any]], bool] | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._lifecycle = threading.RLock()
        self._inject_lock = threading.RLock()
        self._tap = self._runloop = None
        self._callback = None
        self._startup_error: BaseException | None = None
        self.last_error: str | None = None
        self._keys_down: set[str] = set()
        self._buttons_down: set[str] = set()
        self._caps_active = bool(self.cg.CGEventSourceFlagsState(0) & (1 << 16))
        self._captured_caps_active = self._caps_active
        self._clicks: dict[str, tuple[float, float, float, int]] = {}
        self._reset_metrics()

    def _bind(self) -> None:
        def bind(lib: Any, name: str, restype: Any, *argtypes: Any) -> None:
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, list(argtypes)

        ptr, u32, i32, u64 = C.c_void_p, C.c_uint32, C.c_int32, C.c_uint64
        bind(self.cg, "CGGetActiveDisplayList", i32, u32, C.POINTER(u32), C.POINTER(u32))
        bind(self.cg, "CGGetOnlineDisplayList", i32, u32, C.POINTER(u32), C.POINTER(u32))
        bind(self.cg, "CGDisplayBounds", CGRect, u32)
        bind(self.cg, "CGDisplayIsBuiltin", u32, u32)  # boolean_t is uint32_t.
        bind(self.cg, "CGMainDisplayID", u32)
        bind(self.cg, "CGEventCreate", ptr, ptr)
        bind(self.cg, "CGEventGetLocation", CGPoint, ptr)
        bind(self.cg, "CGEventGetIntegerValueField", C.c_int64, ptr, u32)
        bind(self.cg, "CGEventGetDoubleValueField", C.c_double, ptr, u32)
        bind(self.cg, "CGEventGetFlags", u64, ptr)
        bind(self.cg, "CGEventSetIntegerValueField", None, ptr, u32, C.c_int64)
        bind(self.cg, "CGEventSetDoubleValueField", None, ptr, u32, C.c_double)
        bind(self.cg, "CGEventSetFlags", None, ptr, u64)
        bind(self.cg, "CGEventSetType", None, ptr, u32)
        bind(self.cg, "CGEventSourceCreate", ptr, i32)
        bind(self.cg, "CGEventCreateSourceFromEvent", ptr, ptr)
        bind(self.cg, "CGEventSourceGetPixelsPerLine", C.c_double, ptr)
        bind(self.cg, "CGEventSourceFlagsState", u64, i32)
        bind(self.cg, "CGEventSourceKeyState", C.c_bool, i32, C.c_uint16)
        bind(self.cg, "CGEventSourceSetUserData", None, ptr, C.c_int64)
        bind(self.cg, "CGEventSourceSetLocalEventsSuppressionInterval", None, ptr, C.c_double)
        bind(self.cg, "CGEventCreateMouseEvent", ptr, ptr, u32, CGPoint, u32)
        bind(self.cg, "CGEventCreateKeyboardEvent", ptr, ptr, C.c_uint16, C.c_bool)
        # The fixed-arity variant avoids Darwin arm64 variadic ABI pitfalls.
        bind(self.cg, "CGEventCreateScrollWheelEvent2", ptr, ptr, u32, u32, i32, i32, i32)
        bind(self.cg, "CGEventPost", None, u32, ptr)
        bind(self.cg, "CGWarpMouseCursorPosition", i32, CGPoint)
        bind(self.cg, "CGEventTapCreate", ptr, u32, u32, u32, u64, TAP_CALLBACK, ptr)
        bind(self.cg, "CGEventTapEnable", None, ptr, C.c_bool)
        bind(self.cg, "CGEventTapIsEnabled", C.c_bool, ptr)
        bind(self.cf, "CFMachPortCreateRunLoopSource", ptr, ptr, ptr, C.c_long)
        bind(self.cf, "CFMachPortInvalidate", None, ptr)
        bind(self.cf, "CFRunLoopGetCurrent", ptr)
        bind(self.cf, "CFRunLoopAddSource", None, ptr, ptr, ptr)
        bind(self.cf, "CFRunLoopRemoveSource", None, ptr, ptr, ptr)
        bind(self.cf, "CFRunLoopRunInMode", i32, ptr, C.c_double, C.c_bool)
        bind(self.cf, "CFRunLoopStop", None, ptr)
        bind(self.cf, "CFRunLoopWakeUp", None, ptr)
        bind(self.cf, "CFRelease", None, ptr)
        bind(self.ax, "AXIsProcessTrusted", C.c_bool)
        self._mode = C.c_void_p.in_dll(self.cf, "kCFRunLoopDefaultMode").value
        for name in ("CGPreflightListenEventAccess", "CGPreflightPostEventAccess",
                     "CGRequestListenEventAccess", "CGRequestPostEventAccess"):
            if hasattr(self.cg, name):
                bind(self.cg, name, C.c_bool)

    def permissions(self, request: bool = False) -> dict[str, bool]:
        """Report TCC access; optionally ask macOS to present its access prompts."""
        if request:
            for name in ("CGRequestListenEventAccess", "CGRequestPostEventAccess"):
                if hasattr(self.cg, name):
                    getattr(self.cg, name)()
        trusted = bool(self.ax.AXIsProcessTrusted())
        listen = getattr(self.cg, "CGPreflightListenEventAccess", lambda: trusted)()
        post = getattr(self.cg, "CGPreflightPostEventAccess", lambda: trusted)()
        return {"accessibility": bool(trusted or post), "input_monitoring": bool(listen)}

    def supported_keys(self) -> set[str]:
        """Wire keys that Quartz can inject; Fn remains a local hardware key."""
        return set(KEY_CODES) - {"fn"}

    def _reset_metrics(self) -> None:
        # Aggregate counters only: no event payloads, keys, coordinates or times.
        self._move_seen = 0
        self._move_zero_delta = 0
        self._move_nonzero_delta = 0
        self._own_tag_skipped = 0
        self._callback_failures = 0
        self._dock_gesture = DockGesture()

    def metrics(self) -> dict[str, int | bool]:
        """Return content-free counts since start and the native tap state.

        Counter snapshots may race a single integer increment, which is fine
        for diagnostics. The tap pointer is read and queried while holding the
        lifecycle lock so run-loop cleanup cannot release it during the query.
        Lightweight ``__new__`` test backends without lifecycle state report an
        inactive tap and zero for counters they have not used.
        """
        counts: dict[str, int | bool] = {
            name: getattr(self, "_" + name, 0)
            for name in ("move_seen", "move_zero_delta", "move_nonzero_delta",
                         "own_tag_skipped", "callback_failures")
        }
        tap_enabled = False
        lifecycle = getattr(self, "_lifecycle", None)
        if lifecycle is not None:
            with lifecycle:
                tap = getattr(self, "_tap", None)
                if tap:
                    query = getattr(self.cg, "CGEventTapIsEnabled", None)
                    if query is not None:
                        tap_enabled = bool(query(tap))
        counts["tap_enabled"] = tap_enabled
        gesture=getattr(self,"_dock_gesture",None)
        if gesture is not None:
            counts.update(gesture_sequences=gesture.sequences,
                          gesture_forwarded=gesture.forwarded,
                          gesture_task_view=gesture.task_view,
                          gesture_start_menu=gesture.start_menu)
        return counts

    def _display_ids(self, function_name: str) -> list[int]:
        query = getattr(self.cg, function_name)
        count = C.c_uint32()
        error = query(0, None, C.byref(count))
        if error:
            raise RuntimeError(f"{function_name} failed: {error}")
        if not count.value:
            return []
        displays = (C.c_uint32 * count.value)()
        error = query(len(displays), displays, C.byref(count))
        if error:
            raise RuntimeError(f"{function_name} failed: {error}")
        return list(displays)[:count.value]

    def screens(self) -> list[dict[str, Any]]:
        displays = self._display_ids("CGGetActiveDisplayList")
        if not displays:
            # Sleeping displays remain in desktop space but are not drawable,
            # so Active can be empty while Online still has the actual layout.
            displays = self._display_ids("CGGetOnlineDisplayList")
        result = []
        main = self.cg.CGMainDisplayID()
        external_number = 0
        for display in displays:
            bounds = self.cg.CGDisplayBounds(display)
            builtin = bool(self.cg.CGDisplayIsBuiltin(display))
            if not builtin:
                external_number += 1
            name = "内建显示器" if builtin else f"外接显示器 {external_number}"
            result.append({"id": str(display), "x": bounds.origin.x, "y": bounds.origin.y,
                           "width": bounds.size.width, "height": bounds.size.height,
                           "name": name, "primary": display == main})
        return result

    def position(self) -> tuple[float, float]:
        event = self.cg.CGEventCreate(None)
        if not event:
            raise RuntimeError("CGEventCreate failed while reading pointer position")
        try:
            point = self.cg.CGEventGetLocation(event)
            return point.x, point.y
        finally:
            self.cf.CFRelease(event)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stopping.is_set())

    def start(self, handler: Callable[[dict[str, Any]], bool]) -> None:
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lifecycle:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Mac input capture is already running")
            missing = [name for name, allowed in self.permissions().items() if not allowed]
            if missing:
                raise PermissionError(
                    "macOS permission required: " + ", ".join(missing) +
                    ". Open System Settings > Privacy & Security, allow the launching "
                    "Terminal/Python application under Accessibility and Input Monitoring, "
                    "then quit and reopen that application.")
            self._handler = handler
            self._startup_error = None
            self.last_error = None
            self._reset_metrics()
            # Native Caps Lock may change while sharing is paused. Resynchronize
            # before listening so its first new toggle still produces a pulse.
            self._caps_active = bool(self.cg.CGEventSourceFlagsState(0) & (1 << 16))
            self._captured_caps_active = self._caps_active
            self._stopping.clear()
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="crossdesk-quartz", daemon=True)
            self._thread.start()
        if not self._ready.wait(5):
            self.stop()
            raise RuntimeError("Timed out creating macOS input event tap")
        if self._startup_error:
            self.stop()
            raise RuntimeError(str(self._startup_error)) from self._startup_error

    def _run(self) -> None:
        tap = source = runloop = None
        try:
            callback = TAP_CALLBACK(self._event_callback)
            self._callback = callback  # Keep ctypes callback alive until tap is gone.
            mask = sum(1 << kind for kind in EVENT_TYPES)
            # Session tap (1), head insertion (0), active filtering (0).
            # A HID tap would require root; a session tap does not.
            tap = self.cg.CGEventTapCreate(1, 0, 0, mask, callback, None)
            if not tap:
                raise RuntimeError("CGEventTapCreate failed. Check Accessibility and Input "
                                   "Monitoring permissions, then restart the launching app.")
            source = self.cf.CFMachPortCreateRunLoopSource(None, tap, 0)
            if not source:
                raise RuntimeError("CFMachPortCreateRunLoopSource failed")
            runloop = self.cf.CFRunLoopGetCurrent()
            self.cf.CFRunLoopAddSource(runloop, source, self._mode)
            with self._lifecycle:
                self._tap, self._runloop = tap, runloop
            self.cg.CGEventTapEnable(tap, True)
            self._ready.set()
            while not self._stopping.is_set():
                # A bounded run also permits a stop even if the wake-up races setup.
                result = self.cf.CFRunLoopRunInMode(self._mode, 0.5, False)
                if result in (1, 2) and not self._stopping.is_set():
                    raise RuntimeError("macOS input run loop stopped unexpectedly; local input restored")
        except BaseException as exc:
            self._startup_error = exc
            self.last_error = str(exc)
            log.error("macOS input capture failed; local input restored")
        finally:
            self._stopping.set()
            with self._lifecycle:
                self._tap = self._runloop = None
            if tap:
                self.cg.CGEventTapEnable(tap, False)
            if source and runloop:
                self.cf.CFRunLoopRemoveSource(runloop, source, self._mode)
            if tap:
                self.cf.CFMachPortInvalidate(tap)
            if source:
                self.cf.CFRelease(source)
            if tap:
                self.cf.CFRelease(tap)
            self._callback = None
            self._ready.set()
            self.release_all()

    def stop(self) -> None:
        self._stopping.set()
        with self._lifecycle:
            runloop, thread = self._runloop, self._thread
            if runloop:
                self.cf.CFRunLoopStop(runloop)
                self.cf.CFRunLoopWakeUp(runloop)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        self.release_all()

    def _event_callback(self, _proxy: int, kind: int, event: int, _ref: int) -> int | None:
        try:
            if kind in (0xFFFFFFFE, 0xFFFFFFFF):
                self.last_error = "macOS disabled the input tap; capture was re-enabled"
                if self._tap and not self._stopping.is_set():
                    self.cg.CGEventTapEnable(self._tap, True)
                return event
            if not event or self._stopping.is_set() or not self._handler:
                return event
            if self.cg.CGEventGetIntegerValueField(event, USER_DATA) == TAG:
                self._own_tag_skipped = getattr(self, "_own_tag_skipped", 0) + 1
                return event
            if kind==30 and self.cg.CGEventGetIntegerValueField(event,110)==23:
                axis=self.cg.CGEventGetDoubleValueField(event,123)
                phase=self.cg.CGEventGetIntegerValueField(event,132)
                progress=self.cg.CGEventGetDoubleValueField(event,124)
                return None if self._dock_gesture.consume(axis,phase,progress,self._handler) else event
            payload = self._decode(kind, event)
            if payload is not None:
                if payload.pop("pulse", False):
                    # Caps Lock exposes a toggle-state transition on macOS,
                    # unlike the physical down/up pair expected on the wire.
                    blocked = self._handler(payload)
                    blocked = self._handler({**payload, "down": False}) or blocked
                else:
                    blocked = self._handler(payload)
                if blocked:
                    return None
        except BaseException as exc:
            # Exceptions must never escape a ctypes callback and leave input locked.
            self._callback_failures = getattr(self, "_callback_failures", 0) + 1
            self.last_error = "Input callback failed; local input restored"
            self._stopping.set()
            log.error("macOS event handler failed open")
        return event

    def _decode(self, kind: int, event: int) -> dict[str, Any] | None:
        integer = self.cg.CGEventGetIntegerValueField
        if kind in MOVE_TYPES:
            self._move_seen = getattr(self, "_move_seen", 0) + 1
            dx, dy = integer(event, 4), integer(event, 5)
            if not dx and not dy:
                self._move_zero_delta = getattr(self, "_move_zero_delta", 0) + 1
                return None  # A warp/synthetic location notification is not input.
            self._move_nonzero_delta = getattr(self, "_move_nonzero_delta", 0) + 1
            point = self.cg.CGEventGetLocation(event)
            return {"type": "move", "x": point.x, "y": point.y, "dx": dx, "dy": dy}
        if kind in DOWN_TYPES | UP_TYPES:
            button = ID_BUTTONS.get(integer(event, 3))
            if button is not None:
                return {"type": "button", "button": button, "down": kind in DOWN_TYPES}
        elif kind == 22:
            continuous = bool(integer(event, 88))
            if continuous:
                # Point deltas preserve sub-line trackpad scrolling. Use its actual
                # source scale rather than assuming a fixed ten pixels per line.
                source = self.cg.CGEventCreateSourceFromEvent(event)
                try:
                    scale = (self.cg.CGEventSourceGetPixelsPerLine(source)
                             if source else self._pixels_per_line) or 10.0
                finally:
                    if source:
                        self.cf.CFRelease(source)
                dx = self.cg.CGEventGetDoubleValueField(event, 97) / scale
                dy = self.cg.CGEventGetDoubleValueField(event, 96) / scale
            else:
                dx = self.cg.CGEventGetDoubleValueField(event, 94)
                dy = self.cg.CGEventGetDoubleValueField(event, 93)
                if not dx:
                    dx = float(integer(event, 12))
                if not dy:
                    dy = float(integer(event, 11))
            if dx or dy:
                # Quartz positive horizontal scroll is left; wire/Windows is right.
                return {"type": "scroll", "dx": -dx, "dy": dy,
                        "continuous":bool(integer(event,88))}
        elif kind in (10, 11, 12):
            code = integer(event, 9)
            key = CODE_KEYS.get(code)
            if key is not None and key != "fn":
                if key == "caps_lock" and kind == 12:
                    active = bool(self.cg.CGEventGetFlags(event) & (1 << 16))
                    if active == self._captured_caps_active:
                        return None
                    self._captured_caps_active = active
                    return {"type": "key", "key": key, "down": True, "pulse": True}
                # HID state distinguishes releasing one Shift while the opposite
                # Shift is still held; aggregate event flags cannot do that.
                down = bool(self.cg.CGEventSourceKeyState(1, code)) if kind == 12 else kind == 10
                return {"type": "key", "key": key, "down": down,
                        "repeat": bool(integer(event, 8)) if kind == 10 else False}
        return None

    @staticmethod
    def _coordinate(value: Any) -> float:
        result = float(value)
        if not math.isfinite(result) or abs(result) > 1_000_000:
            raise ValueError("Invalid mouse coordinate")
        return result

    def warp(self, x: float, y: float) -> None:
        point = CGPoint(self._coordinate(x), self._coordinate(y))
        error = self.cg.CGWarpMouseCursorPosition(point)
        if error:
            raise RuntimeError(f"CGWarpMouseCursorPosition failed: {error}")

    def _flags(self) -> int:
        flags = (1 << 16) if self._caps_active else 0
        for key in self._keys_down:
            flags |= MODIFIER_FLAGS.get(key, 0)
        return flags

    def _post(self, event: int) -> None:
        if not event:
            raise RuntimeError("Could not construct a Quartz input event")
        try:
            self.cg.CGEventSetIntegerValueField(event, USER_DATA, TAG)
            self.cg.CGEventSetFlags(event, self._flags())
            self.cg.CGEventPost(1, event)
        finally:
            self.cf.CFRelease(event)

    def inject(self, event: dict[str, Any]) -> None:
        with self._inject_lock:
            kind = event.get("type")
            if kind == "position":
                x, y = self._coordinate(event["x"]), self._coordinate(event["y"])
                old_x, old_y = self.position()
                button = next((b for b in ("left", "right", "middle", "back", "forward")
                               if b in self._buttons_down), None)
                mouse_type = {"left": 6, "right": 7}.get(button, 27 if button else 5)
                value = self.cg.CGEventCreateMouseEvent(self._source, mouse_type,
                                                       CGPoint(x, y), BUTTON_IDS.get(button, 0))
                if value:
                    self.cg.CGEventSetIntegerValueField(value, 4, round(x - old_x))
                    self.cg.CGEventSetIntegerValueField(value, 5, round(y - old_y))
                    self.cg.CGEventSetIntegerValueField(value, 1,
                        self._clicks.get(button, (0, 0, 0, 1))[3] if button else 0)
                self._post(value)
            elif kind == "button":
                button = str(event["button"])
                if button not in BUTTON_IDS:
                    raise ValueError(f"Unsupported mouse button: {button}")
                down = bool(event["down"])
                if down:
                    self._buttons_down.add(button)
                else:
                    self._buttons_down.discard(button)
                x, y = self.position()
                now = time.monotonic()
                if down:
                    last_time, last_x, last_y, count = self._clicks.get(button, (0, 0, 0, 0))
                    count = count % 3 + 1 if now - last_time < 0.5 and math.hypot(x - last_x, y - last_y) < 4 else 1
                    self._clicks[button] = now, x, y, count
                count = self._clicks.get(button, (0, 0, 0, 1))[3]
                mouse_type = {"left": (1, 2), "right": (3, 4)}.get(button, (25, 26))[not down]
                value = self.cg.CGEventCreateMouseEvent(self._source, mouse_type,
                                                       CGPoint(x, y), BUTTON_IDS[button])
                if value:
                    self.cg.CGEventSetIntegerValueField(value, 1, count)
                self._post(value)
            elif kind == "scroll":
                dx = -self._coordinate(event.get("dx", 0))
                dy = self._coordinate(event.get("dy", 0))
                if not dx and not dy:
                    return
                scale = self._pixels_per_line
                residual=getattr(self,"_scroll_point_residual",[0.,0.])
                raw_x,raw_y=dx*scale+residual[0],dy*scale+residual[1]
                point_x,point_y=math.trunc(raw_x),math.trunc(raw_y)
                self._scroll_point_residual=[raw_x-point_x,raw_y-point_y]
                value = self.cg.CGEventCreateScrollWheelEvent2(self._source, 0, 2,
                                                              point_y,point_x,0)
                if value:
                    self.cg.CGEventSetIntegerValueField(value, 88, 1)
                    for field, delta in ((93, dy), (94, dx)):
                        # FixedPtDelta is measured in lines, PointDelta in
                        # pixels. Multiplying both made apps disagree by 10x.
                        self.cg.CGEventSetDoubleValueField(value, field, delta)
                    # PointDelta fields are integer fields. FixedPtDelta above
                    # retains any fraction even when a point delta rounds to 0.
                    self.cg.CGEventSetIntegerValueField(value, 96, point_y)
                    self.cg.CGEventSetIntegerValueField(value, 97, point_x)
                    self.cg.CGEventSetIntegerValueField(value, 11, round(dy))
                    self.cg.CGEventSetIntegerValueField(value, 12, round(dx))
                self._post(value)
            elif kind == "key":
                key = str(event["key"]).lower()
                key = KEY_ALIASES.get(key, key)
                code = KEY_CODES.get(key)
                if code is None:
                    log.debug("Ignoring unsupported keyboard input")
                    return
                down = bool(event["down"])
                repeat = down and key in self._keys_down
                if down:
                    if key == "caps_lock" and not repeat:
                        self._caps_active = not self._caps_active
                    self._keys_down.add(key)
                else:
                    self._keys_down.discard(key)
                value = self.cg.CGEventCreateKeyboardEvent(self._source, code, down)
                if value:
                    if key in MODIFIER_FLAGS or key == "caps_lock":
                        self.cg.CGEventSetType(value, 12)
                    elif repeat:
                        self.cg.CGEventSetIntegerValueField(value, 8, 1)
                self._post(value)
            else:
                raise ValueError(f"Unsupported input event type: {kind}")

    def release_all(self) -> None:
        """Release every button and key posted by this backend, even after errors."""
        with self._inject_lock:
            # Release normal keys before modifier keys so shortcuts cannot stick.
            keys = sorted(self._keys_down, key=lambda key: key in MODIFIER_FLAGS)
            for key in keys:
                try:
                    self.inject({"type": "key", "key": key, "down": False})
                except Exception:
                    log.error("Could not release an injected keyboard input")
            for button in list(self._buttons_down):
                try:
                    self.inject({"type": "button", "button": button, "down": False})
                except Exception:
                    log.error("Could not release an injected mouse input")
            self._keys_down.clear()
            self._buttons_down.clear()
            self._scroll_point_residual=[0.,0.]

