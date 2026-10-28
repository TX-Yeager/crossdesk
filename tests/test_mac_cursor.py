"""Foreground cursor leases with fake AppKit/Quartz only; no native input."""

import ctypes as C
import threading
import unittest

from crossdesk.backends.mac_cursor import (
    ACTIVATION_FAILED, HIDE_FAILED, RESTORE_FAILED, RETURN_FAILED,
    MacForegroundCursor,
    _Point, _Size, _Rect, _PublicMacCursorAPI,
)


class FakeMacCursorAPI:
    def __init__(self):
        self.active = False
        self.visible = True
        self.frontmost = "editor"
        self.windows = ["front", "back"]
        self.frames = {"front": (10, 20, 900, 600), "back": (50, 60, 400, 300)}
        self.calls = []
        self.hide_error = 0
        self.show_errors = []
        self.hide_effective = True
        self.return_immediate = True
        self.workspace_self = None
        self.control_ready = True

    def retain_frontmost(self):
        self.calls.append(("retain_app", self.frontmost))
        return self.frontmost

    def release_application(self, app):
        self.calls.append(("release_app", app))

    def is_current_application(self, app):
        return app == "crossdesk"

    def frontmost_matches(self, app):
        front_self = self.current_frontmost()
        return front_self if app == "crossdesk" else not front_self and self.frontmost == app

    def current_active(self):
        return self.active

    def current_frontmost(self):
        return self.active if self.workspace_self is None else self.workspace_self

    def activate_current(self):
        self.calls.append(("activate_self",))
        return True  # Deliberately not proof of active status.

    def activate_application(self, app):
        self.calls.append(("activate_app", app))
        if self.return_immediate:
            self.active = False
        return True

    def create_control_window(self):
        self.calls.append(("create_control",))
        return "control"

    def show_control_window(self, window):
        self.calls.append(("show_control", window))

    def control_window_ready(self, window):
        return self.control_ready

    def destroy_control_window(self, window):
        self.calls.append(("destroy_control", window))

    def hide_cursor(self):
        self.calls.append(("hide",))
        if not self.hide_error and self.hide_effective:
            self.visible = False
        return self.hide_error

    def show_cursor(self):
        self.calls.append(("show",))
        error = self.show_errors.pop(0) if self.show_errors else 0
        if not error:
            self.visible = True
        return error

    def cursor_visible(self):
        self.calls.append(("visible",))
        return self.visible


class MacForegroundCursorTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeMacCursorAPI()
        self.now = 0.0
        self.manager = MacForegroundCursor(object(), native=self.api,
                                           clock=lambda: self.now, activation_timeout=1)

    def enter(self):
        self.assertIsNone(self.manager.update(True))
        self.api.active = True
        self.assertIsNone(self.manager.update(True))
        self.assertIsNone(self.manager.update(True))
        self.assertTrue(self.manager.remote_hidden)

    def count(self, name):
        return sum(call[0] == name for call in self.api.calls)

    def test_waits_for_actual_activation_and_hides_once(self):
        self.assertIsNone(self.manager.update(True))
        self.assertTrue(self.manager.pending)
        self.assertEqual(self.count("activate_self"), 1)
        self.assertEqual(self.count("hide"), 0)
        self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.count("activate_self"), 1)
        self.api.active = True
        self.manager.update(True)
        self.assertEqual(self.count("hide"), 0)
        self.manager.update(True)
        self.assertTrue(self.manager.remote_hidden)
        self.assertEqual(self.count("hide"), 1)

    def test_real_windows_stay_visible_while_cursor_is_hidden_and_returned(self):
        original_frames = dict(self.api.frames)
        self.enter()
        self.assertIsNone(self.manager.update(False))
        self.manager.update(False)
        self.assertEqual(self.count("hide"), 1)
        self.assertEqual(self.count("show"), 1)
        calls = self.api.calls
        self.assertEqual(self.count("hide_window"), 0)
        self.assertEqual(self.count("restore_window"), 0)
        self.assertEqual(self.count("retain_windows"), 0)
        self.assertLess(calls.index(("show",)), calls.index(("activate_app", "editor")))
        self.assertEqual(self.api.frames, original_frames)
        self.assertEqual(self.count("release_app"), 1)
        self.assertEqual(self.count("release_window"), 0)

    def test_cancelled_late_activation_restores_previous_app_without_hiding(self):
        self.manager.update(True)
        self.manager.update(False)
        self.assertTrue(self.manager.pending)
        self.assertEqual(self.count("release_app"), 0)
        self.assertEqual(self.count("destroy_control"), 0)
        self.api.active = True  # A late activation response cannot acquire a lease.
        self.manager.update(False)
        self.assertEqual(self.count("hide"), 0)
        self.assertEqual(self.count("show"), 0)
        self.assertEqual(self.count("activate_app"), 1)
        self.assertEqual(self.count("release_app"), 1)
        self.assertEqual(self.count("destroy_control"), 1)
        self.assertFalse(self.manager.pending)

    def test_cancel_guard_expires_and_releases_saved_app(self):
        self.manager.update(True)
        self.manager.update(False)
        self.now = 1
        self.manager.update(False)
        self.assertEqual(self.count("activate_app"), 0)
        self.assertEqual(self.count("release_app"), 1)
        self.assertFalse(self.manager.pending)

    def test_cancel_query_failure_restores_windows_and_returns_safe_error(self):
        self.manager.update(True)
        def fail_query():
            raise RuntimeError("private-native-debug-marker")
        self.api.current_active = fail_query
        self.api.frontmost_matches = lambda _application: fail_query()
        self.assertEqual(self.manager.update(False), RESTORE_FAILED)
        self.assertEqual(self.count("restore_window"), 0)
        self.assertEqual(self.count("release_app"), 0)
        self.assertFalse(self.manager.local_ready)
        self.assertNotIn("private-native-debug-marker", self.manager.last_error)
        self.manager.close()
        self.assertEqual(self.count("release_app"), 1)

    def test_local_keyboard_waits_for_confirmed_return_not_activation_request(self):
        self.assertTrue(self.manager.local_ready)
        self.enter()
        self.assertFalse(self.manager.local_ready)
        self.api.return_immediate = False
        self.assertIsNone(self.manager.update(False))
        self.assertFalse(self.manager.hidden)
        self.assertFalse(self.manager.local_ready)
        self.assertTrue(self.manager.pending)
        self.assertEqual(self.count("release_app"), 0)
        self.manager.update(False)
        self.assertEqual(self.count("activate_app"), 1)
        self.api.active = False
        self.api.frontmost = "editor"
        self.manager.update(False)
        self.assertTrue(self.manager.local_ready)
        self.assertFalse(self.manager.pending)
        self.assertEqual(self.count("release_app"), 1)

    def test_control_window_survives_until_original_focus_return_is_confirmed(self):
        self.enter()
        self.api.return_immediate = False
        self.manager.update(False)
        self.assertEqual(self.count("show"), 1)
        self.assertEqual(self.count("destroy_control"), 0)
        self.assertFalse(self.manager.local_ready)
        self.api.active = False
        self.manager.update(False)
        self.assertEqual(self.count("destroy_control"), 1)
        self.assertTrue(self.manager.local_ready)
        self.manager.close()
        self.assertEqual(self.count("destroy_control"), 1)

    def test_control_window_creation_failure_restores_original_windows_and_app(self):
        def fail_create():
            raise RuntimeError("private-native-debug-marker")
        self.api.create_control_window = fail_create
        self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.manager.warning, HIDE_FAILED)
        self.assertEqual(self.count("hide"), 0)
        self.assertEqual(self.count("restore_window"), 0)
        self.assertEqual(self.count("release_app"), 1)
        self.assertEqual(self.count("destroy_control"), 0)
        self.assertNotIn("private-native-debug-marker", self.manager.warning)

    def test_no_visible_original_windows_still_acquires_own_key_window(self):
        self.api.windows = []
        self.enter()
        self.assertEqual(self.count("create_control"), 1)
        self.assertEqual(self.count("show_control"), 1)
        self.manager.update(False)
        self.assertEqual(self.count("destroy_control"), 1)

    def test_nonkey_control_window_waits_without_hiding(self):
        self.api.active = True
        self.api.control_ready = False
        self.manager.update(True)
        self.manager.update(True)
        self.assertEqual(self.count("hide"), 0)
        self.api.control_ready = True
        self.manager.update(True)
        self.assertEqual(self.count("hide"), 0)
        self.manager.update(True)
        self.assertTrue(self.manager.hidden)

    def test_return_timeout_keeps_keyboard_gated_and_reports_once(self):
        self.enter()
        self.api.return_immediate = False
        self.manager.update(False)
        self.now = 1
        self.assertEqual(self.manager.update(False), RETURN_FAILED)
        self.assertFalse(self.manager.local_ready)
        self.assertIsNone(self.manager.update(False))
        self.assertEqual(self.count("activate_app"), 1)
        self.api.active = False
        self.api.frontmost = "browser"
        self.manager.update(False)
        self.assertTrue(self.manager.local_ready)

    def test_user_switch_during_return_is_ready_without_forcing_original_app(self):
        self.enter()
        self.api.return_immediate = False
        self.manager.update(False)
        self.api.active = False
        self.api.frontmost = "browser"
        self.manager.update(False)
        self.assertTrue(self.manager.local_ready)
        self.assertEqual(self.count("activate_app"), 1)
        self.assertEqual(self.count("release_app"), 1)

    def test_cancel_guard_respects_user_switch_to_another_app(self):
        self.manager.update(True)
        self.manager.update(False)
        self.api.frontmost = "browser"
        self.manager.update(False)
        self.assertEqual(self.count("release_app"), 1)
        self.api.active = True
        self.manager.update(False)
        self.assertEqual(self.count("activate_app"), 0)

    def test_new_crossing_reuses_cancelled_activation_original_app(self):
        self.manager.update(True)
        self.manager.update(False)
        self.api.active = True
        self.manager.update(True)
        self.manager.update(True)
        self.assertTrue(self.manager.hidden)
        self.assertEqual(self.count("retain_app"), 1)
        self.manager.update(False)
        self.assertIn(("activate_app", "editor"), self.api.calls)
        self.assertEqual(self.count("release_app"), 1)

    def test_activation_timeout_restores_and_reports_only_once(self):
        self.manager.update(True)
        self.now = 1
        self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.manager.warning, ACTIVATION_FAILED)
        self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.count("activate_self"), 1)
        self.assertEqual(self.count("hide"), 0)
        self.assertEqual(self.count("release_app"), 1)
        self.manager.update(False)
        self.manager.update(True)
        self.assertEqual(self.count("activate_self"), 2)

    def test_hide_failure_does_not_acquire_or_overbalance_a_count(self):
        self.api.active = True
        self.api.hide_error = 1000
        self.assertIsNone(self.manager.update(True))
        self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.manager.warning, HIDE_FAILED)
        self.assertEqual(self.count("hide"), 1)
        self.assertEqual(self.count("show"), 0)
        self.assertFalse(self.manager.remote_hidden)
        self.manager.close()
        self.assertEqual(self.count("show"), 0)

    def test_success_code_with_cursor_still_visible_retries_then_fails_balanced(self):
        self.api.active = True
        self.api.hide_effective = False
        self.assertIsNone(self.manager.update(True))
        self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.count("hide"), 1)
        self.assertEqual(self.count("show"), 1)
        self.now = 1
        self.assertIsNone(self.manager.update(True))
        self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.manager.warning, HIDE_FAILED)
        self.assertEqual(self.count("hide"), 2)
        self.assertEqual(self.count("show"), 2)
        self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.count("hide"), 2)

    def test_activation_true_before_workspace_frontmost_waits_across_polls(self):
        self.manager.update(True)
        self.api.active = True
        self.api.workspace_self = False
        self.manager.update(True)
        self.manager.update(True)
        self.assertEqual(self.count("hide"), 0)
        self.assertFalse(self.manager.local_ready)
        self.api.workspace_self = True
        self.manager.update(True)
        self.assertEqual(self.count("hide"), 0)
        self.manager.update(True)
        self.assertTrue(self.manager.hidden)
        self.assertEqual(self.count("hide"), 1)

    def test_cancel_during_inconsistent_activation_retains_app_until_return_confirmed(self):
        self.manager.update(True)
        self.api.active = True
        self.api.workspace_self = False  # Original still reported frontmost.
        self.assertIsNone(self.manager.update(False))
        self.assertFalse(self.manager.local_ready)
        self.assertTrue(self.manager.pending)
        self.assertEqual(self.count("release_app"), 0)
        self.assertEqual(self.count("activate_app"), 0)
        self.api.workspace_self = True  # The pending activation arrives late.
        self.api.return_immediate = False
        self.manager.update(False)
        self.assertFalse(self.manager.local_ready)
        self.assertEqual(self.count("activate_app"), 1)
        self.api.workspace_self = False  # Workspace leads isActive on return too.
        self.manager.update(False)
        self.assertFalse(self.manager.local_ready)
        self.assertEqual(self.count("release_app"), 0)
        self.api.active = False
        self.manager.update(False)
        self.assertTrue(self.manager.local_ready)
        self.assertEqual(self.count("release_app"), 1)

    def test_transient_ineffective_hide_balances_before_successful_retry(self):
        self.api.active = True
        self.api.hide_effective = False
        self.manager.update(True)
        self.manager.update(True)
        self.assertFalse(self.manager.hidden)
        self.assertEqual(self.count("show"), 1)
        self.api.hide_effective = True
        self.manager.update(True)
        self.manager.update(True)
        self.assertTrue(self.manager.hidden)
        self.assertEqual(self.count("hide"), 2)
        self.manager.update(False)
        self.assertEqual(self.count("show"), 2)

    def test_return_inactive_before_workspace_moves_still_gates_local_keyboard(self):
        self.enter()
        self.api.return_immediate = False
        self.manager.update(False)
        self.api.active = False
        self.api.workspace_self = True
        self.manager.update(False)
        self.assertFalse(self.manager.local_ready)
        self.assertEqual(self.count("release_app"), 0)
        self.api.workspace_self = False
        self.manager.update(False)
        self.assertTrue(self.manager.local_ready)
        self.assertEqual(self.count("release_app"), 1)

    def test_workspace_third_app_before_inactive_does_not_reactivate_original(self):
        self.enter()
        self.api.workspace_self = False
        self.api.frontmost = "browser"
        self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.manager._phase, "suspended")
        self.assertEqual(self.count("activate_app"), 0)
        self.assertFalse(self.manager.local_ready)
        self.api.active = False
        self.manager.update(False)
        self.assertTrue(self.manager.local_ready)
        self.assertEqual(self.count("activate_app"), 0)

    def test_focus_loss_still_balances_but_does_not_steal_focus_back(self):
        self.enter()
        self.api.active = False
        self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.manager._phase, "suspended")
        self.assertEqual(self.count("show"), 1)
        self.assertEqual(self.count("activate_app"), 0)
        self.assertIsNone(self.manager.update(False))

    def test_system_cursor_reveal_recovers_without_error_or_local_takeover(self):
        self.enter()
        self.api.visible = True  # System UI can reveal it while all focus flags remain true.
        self.assertIsNone(self.manager.update(True))
        self.assertFalse(self.manager.hidden)
        self.assertFalse(self.manager.local_ready)
        self.assertEqual(self.count("show"), 1)
        self.assertIsNone(self.manager.last_error)
        self.manager.update(True)
        self.assertEqual(self.count("hide"), 1)  # Respect the cooling interval.
        self.now = 0.25
        self.manager.update(True)
        self.assertEqual(self.count("hide"), 1)  # Confirm a fresh GUI poll first.
        self.manager.update(True)
        self.assertTrue(self.manager.hidden)
        self.assertEqual(self.count("hide"), 2)
        self.assertEqual(self.count("show"), 1)
        self.assertEqual(self.count("activate_self"), 1)

    def test_system_foreground_loss_waits_without_reactivation_and_gates_return(self):
        self.enter()
        self.api.active = False
        self.api.frontmost = "system_ui"
        self.manager.update(True)
        for index in range(6):
            self.now = (index + 1) * 0.5
            self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.count("activate_self"), 1)
        self.assertEqual(self.count("activate_app"), 0)
        self.assertEqual(self.count("hide"), 1)
        self.assertEqual(self.count("show"), 1)
        self.api.active = True  # OS naturally gives our key window back.
        self.manager.update(True)
        self.manager.update(True)
        self.assertTrue(self.manager.hidden)
        self.api.return_immediate = False
        self.manager.update(False)
        self.assertFalse(self.manager.local_ready)
        self.assertEqual(self.count("destroy_control"), 0)
        self.api.active = False
        self.api.frontmost = "editor"
        self.manager.update(False)
        self.assertTrue(self.manager.local_ready)
        self.assertEqual(self.count("destroy_control"), 1)
        self.assertEqual(self.count("hide"), self.count("show"))

    def test_failed_visual_retries_are_bounded_and_close_does_not_overbalance(self):
        self.enter()
        self.api.visible = True
        self.api.hide_effective = False
        self.manager.update(True)
        for index in range(5):
            self.now += 0.3
            self.assertIsNone(self.manager.update(True))
            self.assertIsNone(self.manager.update(True))
        self.assertEqual(self.count("hide"), 4)  # Original lease plus at most three retries.
        self.assertEqual(self.count("show"), 4)
        self.assertIsNone(self.manager.last_error)
        self.assertIsNone(self.manager.close())
        self.assertEqual(self.count("show"), 4)
        self.assertEqual(self.count("destroy_control"), 1)

    def test_show_failure_can_retry_without_repeating_hide_or_window_restore(self):
        self.enter()
        self.api.show_errors = [1000, 0]
        self.assertEqual(self.manager.update(False), RESTORE_FAILED)
        self.assertEqual(self.count("activate_app"), 0)
        self.assertEqual(self.count("release_app"), 0)
        self.assertIsNone(self.manager.update(False))
        self.assertEqual(self.count("hide"), 1)
        self.assertEqual(self.count("show"), 2)
        self.assertEqual(self.count("restore_window"), 0)
        self.assertEqual(self.count("activate_app"), 1)

    def test_close_cancels_and_balances_idempotently(self):
        self.enter()
        self.assertIsNone(self.manager.close())
        self.assertIsNone(self.manager.close())
        self.manager.update(True)
        self.assertEqual(self.count("show"), 1)
        self.assertEqual(self.count("hide"), 1)
        self.assertEqual(self.count("release_app"), 1)

    def test_close_retries_only_failed_show(self):
        self.enter()
        self.api.show_errors = [1000, 0]
        self.manager.close()
        self.assertEqual(self.count("show"), 2)
        self.manager.close()
        self.assertEqual(self.count("show"), 2)

    def test_original_crossdesk_app_is_not_reactivated(self):
        self.api.frontmost = "crossdesk"
        self.api.active = True
        self.manager.update(True)
        self.manager.update(True)
        self.manager.update(False)
        self.assertEqual(self.count("activate_self"), 0)
        self.assertEqual(self.count("activate_app"), 0)
        self.assertEqual(self.count("release_app"), 1)

    def test_windows_already_hidden_or_minimized_are_not_added_or_shown(self):
        self.api.windows = []  # Native adapter only returns visible unminimized windows.
        self.enter()
        self.manager.close()
        self.assertEqual(self.count("hide_window"), 0)
        self.assertEqual(self.count("restore_window"), 0)

    def test_all_public_mutations_require_main_thread(self):
        errors = []
        def background():
            for action in (lambda: self.manager.update(True), self.manager.close,
                           lambda: MacForegroundCursor(object(), native=self.api)):
                try:
                    action()
                except RuntimeError as exc:
                    errors.append(str(exc))
        thread = threading.Thread(target=background)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 3)
        self.assertEqual(self.api.calls, [])

    def test_control_window_rect_abi_and_public_configuration(self):
        self.assertEqual(C.sizeof(_Point), 16)
        self.assertEqual(C.sizeof(_Size), 16)
        self.assertEqual(C.sizeof(_Rect), 32)
        native = _PublicMacCursorAPI.__new__(_PublicMacCursorAPI)
        native._app = 1
        native._control_keys = {}
        native._class = lambda _name: 2
        calls = []
        def send(receiver, selector, result=None, arguments=(), *values):
            calls.append((receiver, selector, arguments, values))
            if selector == "keyWindow":return 3
            if selector == "alloc":return 4
            if selector == "initWithContentRect:styleMask:backing:defer:":return 5
        native._send = send
        self.assertEqual(native.create_control_window(), 5)
        init = next(call for call in calls if call[1].startswith("initWithContentRect:"))
        self.assertEqual(init[2], (_Rect, C.c_ulong, C.c_ulong, C.c_bool))
        rect, style, backing, deferred = init[3]
        self.assertEqual((rect.size.width, rect.size.height, style, backing, deferred),
                         (1, 1, 1, 2, False))
        values = {call[1]:call[3] for call in calls}
        self.assertEqual(values["setAlphaValue:"], (0.0,))
        self.assertEqual(values["setOpaque:"], (False,))
        self.assertEqual(values["setIgnoresMouseEvents:"], (True,))
        self.assertEqual(values["setReleasedWhenClosed:"], (False,))
        self.assertEqual(native._control_keys, {5:3})

    def test_self_activation_uses_public_nsapp_void_bool_request(self):
        native = _PublicMacCursorAPI.__new__(_PublicMacCursorAPI)
        native._app = 1
        native._selector = lambda _name: 2
        native._current_application = lambda: 3
        calls = []
        def send(receiver, selector, result=None, arguments=(), *values):
            calls.append((receiver, selector, result, arguments, values))
            return selector == "respondsToSelector:"
        native._send = send
        self.assertTrue(native.activate_current())
        self.assertEqual(calls[-1], (1, "activateIgnoringOtherApps:", None, (C.c_bool,), (True,)))
        self.assertFalse(any(call[1] == "activateWithOptions:" for call in calls))

    def test_self_activation_falls_back_only_if_nsapp_selector_unavailable(self):
        native = _PublicMacCursorAPI.__new__(_PublicMacCursorAPI)
        native._app = 1
        native._selector = lambda _name: 2
        native._current_application = lambda: 3
        calls = []
        def send(receiver, selector, result=None, arguments=(), *values):
            calls.append((receiver, selector, result, arguments, values))
            return selector == "activateWithOptions:"
        native._send = send
        self.assertTrue(native.activate_current())
        self.assertEqual(calls[-1], (3, "activateWithOptions:", C.c_bool, (C.c_ulong,), (2,)))


if __name__ == "__main__":
    unittest.main()
