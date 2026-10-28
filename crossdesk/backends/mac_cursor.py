"""Public AppKit/Quartz cursor visibility lease, driven by the GUI main thread.

Construct only after Tk has created its NSApplication; creating a plain NSApp
first can break Tk initialization. Call ``update(remote)`` from the GUI poll.
Activation is asynchronous: the manager requires both ``isActive`` and the
workspace's frontmost application to agree across GUI polls before hiding.
No mouse association, cursor positioning, input capture or input injection is
performed here. A separate transparent mouse-through key window keeps the
public cursor lease effective. The real application windows stay untouched.

Apple references:
https://developer.apple.com/documentation/coregraphics/cgdisplayhidecursor(_:)
https://developer.apple.com/documentation/coregraphics/cgdisplayshowcursor(_:)
https://developer.apple.com/documentation/appkit/nswindow/orderout(_:)
https://developer.apple.com/documentation/appkit/nswindow/orderfront(_:)
https://developer.apple.com/documentation/appkit/nsrunningapplication/isactive
https://developer.apple.com/documentation/appkit/nsapplication/activate()
"""

from __future__ import annotations

import ctypes as C
import sys
import threading
import time
from typing import Any


ACTIVATION_FAILED = "Mac 光标呈现暂不可用，跨屏继续工作。"
HIDE_FAILED = "Mac 源光标隐藏暂不可用，跨屏继续工作。"
RESTORE_FAILED = "Mac 光标恢复未完成，正在重试。请保持 CrossDesk 打开。"
RETURN_FAILED = "Mac 未能恢复原应用的键盘焦点，互控已暂停。请在 Mac 选择要操作的应用后重试。"


class _Point(C.Structure):
    _fields_ = [("x", C.c_double), ("y", C.c_double)]


class _Size(C.Structure):
    _fields_ = [("width", C.c_double), ("height", C.c_double)]


class _Rect(C.Structure):
    _fields_ = [("origin", _Point), ("size", _Size)]


class _PublicMacCursorAPI:
    """Small Objective-C runtime bridge; selectors are public AppKit APIs."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise OSError("Mac foreground cursor management requires macOS")
        self.appkit = C.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
        self.objc = C.CDLL("/usr/lib/libobjc.A.dylib")
        self.cg = C.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self.objc.objc_getClass.restype = C.c_void_p
        self.objc.objc_getClass.argtypes = [C.c_char_p]
        self.objc.sel_registerName.restype = C.c_void_p
        self.objc.sel_registerName.argtypes = [C.c_char_p]
        self._message_address = C.cast(self.objc.objc_msgSend, C.c_void_p).value
        self._messages: dict[tuple[Any, tuple[Any, ...]], Any] = {}
        self._selectors: dict[str, int] = {}
        self._control_keys: dict[int, int | None] = {}
        for name in ("CGDisplayHideCursor", "CGDisplayShowCursor"):
            function = getattr(self.cg, name)
            function.restype = C.c_int32
            function.argtypes = [C.c_uint32]
        self.cg.CGCursorIsVisible.restype = C.c_uint32  # boolean_t, not C bool.
        self.cg.CGCursorIsVisible.argtypes = []
        self._app = self._send(self._class("NSApplication"), "sharedApplication", C.c_void_p)
        if not self._app or not self._current_application():
            raise RuntimeError("AppKit application is unavailable")

    def _class(self, name: str) -> int:
        result = self.objc.objc_getClass(name.encode("ascii"))
        if not result:
            raise RuntimeError("Required AppKit class is unavailable")
        return result

    def _selector(self, name: str) -> int:
        if name not in self._selectors:
            self._selectors[name] = self.objc.sel_registerName(name.encode("ascii"))
        return self._selectors[name]

    def _send(self, receiver: int, name: str, result: Any = None,
              arguments: tuple[Any, ...] = (), *values: Any) -> Any:
        key = result, arguments
        if key not in self._messages:
            signature = C.CFUNCTYPE(result, C.c_void_p, C.c_void_p, *arguments)
            self._messages[key] = signature(self._message_address)
        return self._messages[key](receiver, self._selector(name), *values)

    def retain_frontmost(self) -> int | None:
        workspace = self._send(self._class("NSWorkspace"), "sharedWorkspace", C.c_void_p)
        application = self._send(workspace, "frontmostApplication", C.c_void_p)
        if application:
            self._send(application, "retain", C.c_void_p)
        return application

    def release_application(self, application: int) -> None:
        self._send(application, "release")

    def _current_application(self) -> int:
        # Fetch this borrowed object inside the current GUI iteration instead of
        # retaining an autoreleased pointer across main-loop autorelease pools.
        return self._send(self._class("NSRunningApplication"), "currentApplication", C.c_void_p)

    def is_current_application(self, application: int) -> bool:
        return self._send(application, "processIdentifier", C.c_int) == self._send(
            self._current_application(), "processIdentifier", C.c_int)

    def frontmost_matches(self, application: int) -> bool:
        workspace = self._send(self._class("NSWorkspace"), "sharedWorkspace", C.c_void_p)
        frontmost = self._send(workspace, "frontmostApplication", C.c_void_p)
        return bool(frontmost) and self._send(frontmost, "processIdentifier", C.c_int) == self._send(
            application, "processIdentifier", C.c_int)

    def current_active(self) -> bool:
        return bool(self._send(self._current_application(), "isActive", C.c_bool))

    def current_frontmost(self) -> bool:
        return self.frontmost_matches(self._current_application())

    def activate_current(self) -> bool:
        # The public NSApplication request also activates a transparent helper
        # when all original Tk windows are withdrawn. NSRunningApplication's
        # request can be rejected in that state on macOS 15. This legacy API is
        # deprecated but remains public; its void return is never confirmation.
        selector = self._selector("activateIgnoringOtherApps:")
        if self._send(self._app, "respondsToSelector:", C.c_bool,
                      (C.c_void_p,), selector):
            self._send(self._app, "activateIgnoringOtherApps:", None, (C.c_bool,), True)
            return True
        return bool(self._send(self._current_application(), "activateWithOptions:", C.c_bool,
                               (C.c_ulong,), 2))

    def activate_application(self, application: int) -> bool:
        if self._send(application, "isTerminated", C.c_bool):
            return False
        selector = self._selector("yieldActivationToApplication:")
        if self._send(self._app, "respondsToSelector:", C.c_bool,
                      (C.c_void_p,), selector):
            self._send(self._app, "yieldActivationToApplication:", None,
                       (C.c_void_p,), application)
        # No ignoring-other-apps flag when returning control to the previous app.
        return bool(self._send(application, "activateWithOptions:", C.c_bool,
                               (C.c_ulong,), 0))

    def create_control_window(self) -> int:
        """Own a transparent key-capable window without altering real frames."""
        previous_key = self._send(self._app, "keyWindow", C.c_void_p)
        if previous_key:
            self._send(previous_key, "retain", C.c_void_p)
        window = None
        try:
            allocated = self._send(self._class("NSWindow"), "alloc", C.c_void_p)
            window = self._send(allocated, "initWithContentRect:styleMask:backing:defer:",
                                C.c_void_p, (_Rect, C.c_ulong, C.c_ulong, C.c_bool),
                                _Rect(_Point(0, 0), _Size(1, 1)), 1, 2, False)
            if not window:
                raise RuntimeError("Mac cursor control window is unavailable")
            self._send(window, "setReleasedWhenClosed:", None, (C.c_bool,), False)
            self._send(window, "setAlphaValue:", None, (C.c_double,), 0.0)
            self._send(window, "setOpaque:", None, (C.c_bool,), False)
            self._send(window, "setIgnoresMouseEvents:", None, (C.c_bool,), True)
            self._control_keys[window] = previous_key
            return window
        except Exception:
            if window:
                self._send(window, "release")
            if previous_key:
                self._send(previous_key, "release")
            raise

    def show_control_window(self, window: int) -> None:
        self._send(window, "makeKeyAndOrderFront:", None, (C.c_void_p,), None)

    def control_window_ready(self, window: int) -> bool:
        return (bool(self._send(window, "isVisible", C.c_bool))
                and bool(self._send(window, "isKeyWindow", C.c_bool)))

    def destroy_control_window(self, window: int) -> None:
        previous_key = self._control_keys.pop(window, None)
        try:
            try:
                self._send(window, "orderOut:", None, (C.c_void_p,), None)
                self._send(window, "close")
            finally:
                self._send(window, "release")
        finally:
            if previous_key:
                try:
                    if (self.current_active()
                            and not self._send(self._app, "keyWindow", C.c_void_p)
                            and self._send(previous_key, "isVisible", C.c_bool)
                            and not self._send(previous_key, "isMiniaturized", C.c_bool)):
                        self._send(previous_key, "makeKeyWindow")
                finally:
                    self._send(previous_key, "release")

    def hide_cursor(self) -> int:
        return self.cg.CGDisplayHideCursor(0)  # Display argument has no effect.

    def show_cursor(self) -> int:
        return self.cg.CGDisplayShowCursor(0)

    def cursor_visible(self) -> bool:
        return bool(self.cg.CGCursorIsVisible())


class MacForegroundCursor:
    """A single balanced hide lease, with no blocking wait for app activation.

    ``update`` returns a fixed, input-free error once per failed remote interval;
    the caller should trigger the engine's emergency pause when it is non-None.
    A false desired state restores our hide count and waits for the previous
    application's confirmed focus before ``local_ready`` permits keyboard input.
    ``close`` must run before destroying the GUI and is safe to call repeatedly.
    """

    def __init__(self, root: Any, *, native: Any = None,
                 clock: Any = time.monotonic, activation_timeout: float = 1.5) -> None:
        self._assert_main_thread()
        self.root = root
        self._native = native if native is not None else _PublicMacCursorAPI()
        self._clock = clock
        self._activation_timeout = activation_timeout
        self._phase = "idle"
        self._deadline = 0.0
        self._previous_app = None
        self._control_window = None
        self._hide_owned = False
        self._reported = False
        self._closed = False
        self._local_ready = True
        self._returning = False
        self._return_requested = False
        self._return_deadline = 0.0
        self._activation_pending = False
        self._foreground_seen = False
        self._recovery_attempts = 0
        self._recovery_after = 0.0
        self._recovery_deadline = 0.0
        self.last_error: str | None = None
        self.warning: str | None = None

    @staticmethod
    def _assert_main_thread() -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Mac cursor management must run on the GUI main thread")

    @property
    def pending(self) -> bool:
        return (self._returning or self._activation_pending
                or self._phase in ("activating", "cancelled", "suspended", "recovering"))

    @property
    def local_ready(self) -> bool:
        """Cached GUI result; workers may read it without invoking AppKit."""
        return self._local_ready

    @property
    def remote_hidden(self) -> bool:
        return self._hide_owned and self._phase == "hidden"

    @property
    def hidden(self) -> bool:
        """Whether the manager has verified its current remote hide lease."""
        return self.remote_hidden

    def _error_once(self, message: str) -> str | None:
        self.last_error = message
        if self._reported:
            return None
        self._reported = True
        return message

    def _finish_return(self) -> bool:
        application, self._previous_app = self._previous_app, None
        self._returning = False
        self._return_requested = False
        success = self._dispose_control_window()
        if application is not None:
            try:
                self._native.release_application(application)
            except Exception:
                success = False
        self._local_ready = success
        return success

    def _dispose_control_window(self) -> bool:
        window, self._control_window = self._control_window, None
        if window is not None:
            try:
                self._native.destroy_control_window(window)
            except Exception:
                return False
        return True

    def _foreground_confirmed(self) -> bool:
        # isActive can become true synchronously with the activation request,
        # before WindowServer/NSWorkspace have actually changed foreground.
        return self._native.current_active() and self._native.current_frontmost()

    def _capture_ready(self) -> bool:
        return (self._foreground_confirmed() and self._control_window is not None
                and self._native.control_window_ready(self._control_window))

    def _return_application(self) -> bool:
        application = self._previous_app
        if application is None:
            return self._finish_return()
        active = self._native.current_active()
        front_self = self._native.current_frontmost()
        if self._activation_pending:
            if active and front_self:
                self._activation_pending = False
            else:
                self._local_ready = False
                # A submitted activation cannot be cancelled. Keep the saved
                # application while it may still arrive; isActive alone is
                # specifically not proof that the request has settled.
                if active or front_self:
                    return self._clock() < self._deadline
                if self._native.frontmost_matches(application):
                    if self._clock() < self._deadline:
                        return True
                self._activation_pending = False
        # Confirm both sides of the return transition. The workspace can name
        # the previous app before this process's isActive has turned false.
        if self._native.is_current_application(application):
            return self._finish_return()
        if not active and not front_self:
            # A third application has become active, or CrossDesk was the
            # original destination. Respect that context instead of stealing it.
            return self._finish_return()
        self._local_ready = False
        if not self._returning:
            self._returning = True
            self._return_deadline = self._clock() + self._activation_timeout
        if active and front_self and not self._return_requested:
            self._return_requested = True
            self._native.activate_application(application)
        if not self._native.current_active() and not self._native.current_frontmost():
            return self._finish_return()
        return self._clock() < self._return_deadline

    def _restore(self, *, wait_for_return: bool = False) -> bool:
        """Balance our hide lease and restore the previous application."""
        success = True
        if self._hide_owned:
            try:
                if self._native.show_cursor() == 0:
                    self._hide_owned = False
                else:
                    success = False
            except Exception:
                success = False
        if self._hide_owned:
            self._local_ready = False
            return False  # Remain active so the next update(False) can retry Show.
        if wait_for_return:
            try:
                return self._return_application() and success
            except Exception:
                self._local_ready = False
                return False
        application, self._previous_app = self._previous_app, None
        if application is not None:
            try:
                if (self._foreground_confirmed()
                        and not self._native.is_current_application(application)):
                    self._native.activate_application(application)
            except Exception:
                success = False
            finally:
                try:
                    self._native.release_application(application)
                except Exception:
                    success = False
        self._returning = False
        self._return_requested = False
        self._local_ready = success
        return success

    def _fail(self, message: str) -> str | None:
        self._phase = "failed"
        if not self._restore(wait_for_return=True):
            message = RETURN_FAILED if self._returning else RESTORE_FAILED
        if message in (ACTIVATION_FAILED, HIDE_FAILED):
            self.warning = message
            self._phase = 'degraded'
            return None
        return self._error_once(message)

    def _suspend_visual_lease(self) -> str | None:
        """Pay our hide count without changing the router's remote ownership.

        Mission Control can show the cursor before changing activation or key
        window state. Neither observation proves that input capture has failed.
        Restoration failures remain fatal because an unpaid hide needs cleanup.
        """
        if self._hide_owned:
            if self._native.show_cursor() != 0:
                return self._fail(RESTORE_FAILED)
            self._hide_owned = False
        self._phase = "suspended"
        self._local_ready = False
        self._foreground_seen = False
        self._recovery_after = self._clock() + 0.25
        return None

    def _recover_visual_lease(self) -> str | None:
        if self._phase == "suspended":
            if (self._clock() < self._recovery_after or self._recovery_attempts >= 3
                    or not self._foreground_confirmed()):
                return None
            # Only restore a key window inside our already-active app. Never
            # activate CrossDesk over Mission Control, Launchpad or another app.
            self._recovery_attempts += 1
            self._native.show_control_window(self._control_window)
            self._phase = "recovering"
            self._recovery_deadline = self._clock() + 0.5
            self._foreground_seen = False
        if not self._capture_ready():
            self._foreground_seen = False
            if self._clock() >= self._recovery_deadline:
                return self._suspend_visual_lease()
            return None
        if not self._foreground_seen:
            self._foreground_seen = True
            return None
        if self._native.hide_cursor() != 0:
            return self._suspend_visual_lease()
        self._hide_owned = True
        if self._native.cursor_visible():
            return self._suspend_visual_lease()
        self._phase = "hidden"
        self._recovery_attempts = 0
        return None

    def _update_local(self) -> str | None:
        if (self._phase == "idle" and not self._hide_owned
                and self._previous_app is None and not self._activation_pending
                and self._control_window is None):
            return None
        if not self._restore(wait_for_return=True):
            self._phase = "failed"
            return self._error_once(RETURN_FAILED if self._returning else RESTORE_FAILED)
        if self._activation_pending:
            self._phase = "cancelled"
            return None
        self._phase = "idle"
        self._reported = False
        self.last_error = None
        self.warning = None
        return None

    def update(self, remote: bool) -> str | None:
        self._assert_main_thread()
        if self._closed:
            return None
        if not remote:
            try:
                return self._update_local()
            except Exception:
                return self._fail(RESTORE_FAILED)
        if self._phase in ("failed", "degraded"):
            # Finish any restoration, but never repeatedly steal activation.
            if self._hide_owned or self._returning or self._activation_pending:
                if not self._restore(wait_for_return=True):
                    return self._error_once(RETURN_FAILED if self._returning else RESTORE_FAILED)
            return None
        try:
            if self._returning:
                if not self._return_application():
                    return self._error_once(RETURN_FAILED)
                if self._returning:
                    return None  # Do not race a fresh Hide against pending return activation.
            if self._phase == "cancelled":
                # A rapid fresh crossing can reuse a still-pending activation
                # and its saved app. If the user changed apps, save the new one.
                if (self._previous_app is not None and not self._native.current_active()
                        and not self._native.frontmost_matches(self._previous_app)):
                    self._native.release_application(self._previous_app)
                    self._previous_app = None
                    self._activation_pending = False
                self._phase = "idle"
            if self._phase == "idle":
                self._local_ready = False
                if self._previous_app is None:
                    self._previous_app = self._native.retain_frontmost()
                if self._control_window is None:
                    # Keep every real window visible and preserve its state.
                    self._control_window = self._native.create_control_window()
                self._native.show_control_window(self._control_window)
                self._phase = "activating"
                self._foreground_seen = False
                self._recovery_attempts = 0
                if not self._activation_pending:
                    self._deadline = self._clock() + self._activation_timeout
                if not self._foreground_confirmed() and not self._activation_pending:
                    self._activation_pending = True
                    self._native.activate_current()
            if self._phase == "activating":
                if not self._capture_ready():
                    self._foreground_seen = False
                    if self._clock() >= self._deadline:
                        return self._fail(ACTIVATION_FAILED)
                    return None
                if not self._foreground_seen:
                    # Give the GUI event loop one full iteration after the two
                    # public foreground indicators first agree. Hiding in the
                    # same activation callback can return success with no effect.
                    self._foreground_seen = True
                    return None
                self._activation_pending = False
                if self._native.hide_cursor() != 0:
                    return self._fail(HIDE_FAILED)
                self._hide_owned = True
                if self._native.cursor_visible():
                    # Hide/visibility confirmation can also lag WindowServer.
                    # Balance every successful Hide before retrying on a later
                    # poll; never accumulate hide counts or spin in the GUI.
                    if self._native.show_cursor() != 0:
                        return self._fail(RESTORE_FAILED)
                    self._hide_owned = False
                    if self._clock() >= self._deadline:
                        return self._fail(HIDE_FAILED)
                    self._foreground_seen = False
                    return None
                self._phase = "hidden"
                return None
            if self._phase == "hidden":
                if not self._capture_ready() or self._native.cursor_visible():
                    return self._suspend_visual_lease()
            if self._phase in ("suspended", "recovering"):
                return self._recover_visual_lease()
        except Exception:
            return self._fail(HIDE_FAILED)
        return None

    def close(self) -> str | None:
        self._assert_main_thread()
        self._closed = True
        success = self._restore()
        if self._hide_owned:
            # A failed Show does not pay our count. A bounded second attempt is
            # safe; never call Show after a successful balance or until visible.
            success = self._restore() and success
        application, self._previous_app = self._previous_app, None
        if application is not None:
            try:
                self._native.release_application(application)
            except Exception:
                success = False
        success = self._dispose_control_window() and success
        self._phase = "closed"
        self._activation_pending = False
        if not success:
            self.last_error = RESTORE_FAILED
            return RESTORE_FAILED
        return None
