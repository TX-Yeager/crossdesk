"""Windows input routing with fake User32; no hooks or input injection."""
import ctypes
import contextlib
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from crossdesk.backends.windows import (
    WindowsBackend, _INPUT, _KBDLLHOOKSTRUCT, _MSLLHOOKSTRUCT,
)


class FakeUser32:
    def __init__(self):
        self.cursor_queries = 0
        self.next_calls = 0
        self.quit_calls = 0

    def GetCursorPos(self, _point):
        self.cursor_queries += 1
        raise AssertionError("The hook must not query GetCursorPos")

    def CallNextHookEx(self, *_args):
        self.next_calls += 1
        return 37

    def PostQuitMessage(self, _exit_code):
        self.quit_calls += 1


class WindowsDeltaTests(unittest.TestCase):
    def backend(self, blocked=True):
        backend = WindowsBackend.__new__(WindowsBackend)
        backend._user32 = FakeUser32()
        backend._tag = 0x435244531234
        backend._reference = (100, 100)
        backend._stop_requested = threading.Event()
        backend._last_error = None
        backend._reset_metrics()
        backend.delivered = []
        backend._handler = lambda event: backend.delivered.append(event) or blocked
        return backend

    def move(self, backend, x, y, flags=0, tag=0):
        data = _MSLLHOOKSTRUCT()
        data.pt.x, data.pt.y = x, y
        data.flags, data.dwExtraInfo = flags, tag
        return backend._mouse_callback(0, 0x0200, ctypes.addressof(data))

    def test_repeated_suppressed_motion_uses_last_committed_reference(self):
        backend = self.backend()
        for point in ((105, 103), (105, 103), (95, 96)):
            self.assertEqual(self.move(backend, *point), 1)
        self.assertEqual([(event["dx"], event["dy"]) for event in backend.delivered],
                         [(5, 3), (5, 3), (-5, -4)])
        self.assertEqual(backend._reference, (100, 100))
        self.assertEqual(backend._user32.cursor_queries, 0)

    def test_passed_physical_motion_commits_each_point(self):
        backend = self.backend(blocked=False)
        self.assertEqual(self.move(backend, 105, 103), 37)
        self.assertEqual(self.move(backend, 110, 100), 37)
        self.assertEqual([(event["dx"], event["dy"]) for event in backend.delivered],
                         [(5, 3), (5, -3)])
        self.assertEqual(backend._reference, (110, 100))
        self.assertEqual(backend._user32.cursor_queries, 0)

    def test_own_tagged_anchor_updates_reference_before_next_physical_move(self):
        backend = self.backend()
        self.assertEqual(self.move(backend, 500, 500, flags=1, tag=backend._tag), 37)
        self.assertEqual(backend.delivered, [])
        self.assertEqual(backend._reference, (500, 500))
        self.assertEqual(self.move(backend, 505, 497), 1)
        self.assertEqual((backend.delivered[0]["dx"], backend.delivered[0]["dy"]), (5, -3))
        self.assertEqual(backend._reference, (500, 500))
        self.assertEqual(backend.metrics()["own_tag_skipped"], 1)
        self.assertEqual(backend.metrics()["move_seen"], 1)

    def test_zero_motion_does_not_call_handler_or_reclaim_remote_input(self):
        backend = self.backend()
        handler = mock.Mock(side_effect=AssertionError("Zero motion must not take ownership"))
        backend._handler = handler
        self.assertEqual(self.move(backend, 100, 100), 37)
        handler.assert_not_called()
        self.assertEqual(backend.metrics()["move_zero_delta"], 1)
        self.assertEqual(backend._user32.quit_calls, 0)
        self.assertEqual(backend._user32.cursor_queries, 0)

    def test_foreign_injected_motion_passes_locally_without_claiming_ownership(self):
        for injected_flags in (1, 2, 3):
            with self.subTest(flags=injected_flags):
                backend = self.backend()
                self.assertEqual(self.move(backend, 500, 500, flags=injected_flags, tag=99), 37)
                self.assertEqual(backend.delivered, [])
                self.assertEqual(backend._reference, (500, 500))
                self.assertEqual(backend.metrics()["move_seen"], 0)
                self.assertEqual(backend.metrics()["own_tag_skipped"], 0)
                self.assertEqual(self.move(backend, 503, 500), 1)
                self.assertEqual(backend.delivered[0]["dx"], 3)

    def test_foreign_injected_button_passes_without_changing_motion_reference(self):
        backend = self.backend()
        data = _MSLLHOOKSTRUCT()
        data.pt.x, data.pt.y = 500, 500
        data.flags = 1
        self.assertEqual(backend._mouse_callback(0, 0x0201, ctypes.addressof(data)), 37)
        self.assertEqual(backend.delivered, [])
        self.assertEqual(backend._reference, (100, 100))

    def test_missing_reference_fails_open_without_a_cursor_query(self):
        backend = self.backend()
        del backend._reference
        self.assertEqual(self.move(backend, 500, 500), 37)
        self.assertEqual(backend.delivered, [])
        self.assertEqual(backend._reference, (500, 500))
        self.assertEqual(backend._user32.cursor_queries, 0)

    def test_start_reads_cursor_once_and_resets_aggregate_counters(self):
        backend = self.backend()
        backend._thread = None
        backend._ready = threading.Event()
        backend.position = mock.Mock(return_value=(200, 300))
        backend._move_seen = backend._callback_failures = 9
        fake_thread = SimpleNamespace(start=backend._ready.set, is_alive=lambda: True)
        with mock.patch("crossdesk.backends.windows.threading.Thread", return_value=fake_thread):
            backend.start(lambda _event: False)
        backend.position.assert_called_once_with()
        self.assertEqual(backend._reference, (200, 300))
        self.assertTrue(all(value == 0 for value in backend.metrics().values()))

    def test_f12_and_own_keyboard_tag_keep_existing_key_routing(self):
        backend = self.backend()
        data = _KBDLLHOOKSTRUCT()
        data.vkCode = 0x7B
        self.assertEqual(backend._keyboard_callback(0, 0x0100, ctypes.addressof(data)), 1)
        self.assertEqual(backend.delivered, [{"type": "key", "key": "f12", "down": True}])
        data.dwExtraInfo = backend._tag
        self.assertEqual(backend._keyboard_callback(0, 0x0100, ctypes.addressof(data)), 37)
        self.assertEqual(len(backend.delivered), 1)
        self.assertEqual(backend.metrics()["own_tag_skipped"], 1)

    def test_handler_failure_is_counted_and_fails_open_without_payload_logging(self):
        backend = self.backend()
        backend._handler = mock.Mock(side_effect=RuntimeError("private-input-debug-marker"))
        with self.assertLogs("crossdesk.backends.windows", level="ERROR") as captured:
            self.assertEqual(self.move(backend, 103, 100), 37)
        self.assertEqual(backend.metrics()["callback_failures"], 1)
        self.assertTrue(backend._stop_requested.is_set())
        self.assertIsNone(backend._handler)
        self.assertNotIn("private-input-debug-marker", "\n".join(captured.output))

    def test_metrics_are_only_content_free_aggregate_counts(self):
        backend = self.backend()
        self.move(backend, 100, 100)
        self.move(backend, 102, 100)
        self.assertEqual(backend.metrics(), {
            "move_seen": 2, "move_zero_delta": 1, "move_nonzero_delta": 1,
            "own_tag_skipped": 0, "callback_failures": 0,
        })
        self.assertTrue(backend.needs_remote_anchor)
        self.assertTrue(all(type(value) is int for value in backend.metrics().values()))
        self.assertTrue(all(value == 0 for value in WindowsBackend.__new__(WindowsBackend).metrics().values()))

    @unittest.skipUnless(sys.platform == "win32", "Native ABI sizes apply to Windows")
    def test_windows_input_and_hook_pointer_abi_sizes_are_unchanged(self):
        sizes = (ctypes.sizeof(_INPUT), ctypes.sizeof(_MSLLHOOKSTRUCT), ctypes.sizeof(_KBDLLHOOKSTRUCT))
        self.assertEqual(sizes, (40, 32, 24) if ctypes.sizeof(ctypes.c_void_p) == 8 else (28, 24, 20))


class CursorBeforeInjectionTests(unittest.TestCase):
    def test_restores_cursor_after_position_but_before_click_and_propagates_errors(self):
        backend=WindowsBackend.__new__(WindowsBackend)
        backend._input_lock=threading.RLock()
        order=[]
        backend.before_pointer_inject=lambda:order.append('restore')
        backend.after_pointer_position=lambda x,y:order.append('restore-position')
        backend.warp=lambda x,y:order.append('inject')
        backend.inject({'type':'position','x':1,'y':2})
        self.assertEqual(order,['inject','restore-position'])
        backend.before_pointer_inject=mock.Mock(side_effect=OSError('cursor unavailable'))
        with self.assertRaises(OSError):backend.inject({'type':'button','button':'left','down':True})
        self.assertEqual(order,['inject','restore-position'])

    def test_remote_parking_hides_before_moving_to_centre(self):
        backend=WindowsBackend.__new__(WindowsBackend)
        order=[]
        backend.before_remote_anchor=lambda:order.append('hide')
        def warp(x,y,before=None):
            if before:before()
            order.append(('park',x,y))
        backend._warp_to=warp
        backend.park_remote(1920,1080)
        self.assertEqual(order,['hide',('park',1920,1080)])

class WindowsWarpBarrierTests(unittest.TestCase):
    def test_handoff_uses_physical_setter_and_allows_its_untagged_target_notification(self):
        backend=self.backend()
        def setter(x,y):
            self.assertIsNotNone(backend._warp_pending)
            self.assertEqual(self.move(backend,x,y),37)
            backend.actual=(x,y)
            return True
        backend._user32.SetPhysicalCursorPos=setter
        backend._mouse_input=mock.Mock(side_effect=AssertionError('Handoff must not enqueue SendInput'))
        backend.warp(0.01,600.2)
        self.assertEqual(backend._reference,(0,600))
        self.assertIsNone(backend._warp_pending)
        self.assertEqual(backend.delivered,[])

    def test_steady_remote_pointer_packets_keep_original_nonblocking_path(self):
        backend=WindowsBackend.__new__(WindowsBackend)
        backend._input_lock=threading.RLock()
        backend._warp_to=mock.Mock()
        backend._wait_warp=mock.Mock(side_effect=AssertionError('Steady motion must not wait'))
        backend.inject({'type':'position','x':40,'y':70,'handoff':False})
        backend._warp_to.assert_called_once_with(40,70,wait=False)
        backend._wait_warp.assert_not_called()

    """Only fake User32 and mocked positions; never install or inject input."""

    move = WindowsDeltaTests.move

    def backend(self):
        backend = WindowsDeltaTests().backend()
        backend._input_lock = threading.RLock()
        backend._physical_coordinates = contextlib.nullcontext
        backend._user32.GetSystemMetrics = lambda index: {
            76: 0, 77: 0, 78: 3840, 79: 2160,
        }[index]
        backend.actual = (100, 100)
        backend.position = lambda: backend.actual
        backend._warp_pending = None
        return backend

    def test_parking_blocks_edge_and_centre_samples_until_tag_and_position_commit(self):
        backend = self.backend()
        backend.actual = backend._reference = (0, 600)
        order = []

        def hide():
            order.append("hide")
            self.assertIsNotNone(backend._warp_pending)
            self.assertEqual(self.move(backend, 0, 600), 1)
            self.assertEqual(self.move(backend, 1, 601), 1)

        def send(*_args, **_kwargs):
            order.append("send")
            # A native setter's target notification must pass to Windows, but
            # must never reach the routing queue or become a physical delta.
            self.assertEqual(self.move(backend, 1920, 1080), 37)
            self.assertEqual(self.move(backend, 1920, 1080, 1, backend._tag), 37)
            self.assertTrue(backend._warp_pending.seen.is_set())
            self.assertEqual(self.move(backend, 0, 600), 1)  # Old pt/new base.

        reads = []
        def position():
            reads.append(1)
            if len(reads) == 1:
                self.assertIsNotNone(backend._warp_pending)
                self.assertEqual(self.move(backend, 0, 600), 1)
                return backend.actual  # Hook ACK precedes the actual OS commit.
            backend.actual = (1920, 1080)
            return backend.actual

        backend.before_remote_anchor = hide
        backend._mouse_input = send
        backend.position = position
        with mock.patch("crossdesk.backends.windows.time.sleep"):
            backend.park_remote(1920, 1080)
        self.assertEqual(order, ["hide", "send"])
        self.assertEqual(len(reads), 2)
        self.assertEqual(backend.delivered, [])
        self.assertIsNone(backend._warp_pending)
        self.assertEqual(backend._reference, (1920, 1080))
        self.assertEqual(self.move(backend, 1917, 1082), 1)
        self.assertEqual((backend.delivered[0]["dx"], backend.delivered[0]["dy"]), (-3, 2))
        self.assertEqual(backend._user32.cursor_queries, 0)

    def test_return_position_commits_before_visibility_restore_and_keeps_f12(self):
        backend = self.backend()
        order = []

        def send(*_args, **_kwargs):
            self.assertEqual(self.move(backend, 1900, 1000), 1)
            key = _KBDLLHOOKSTRUCT(vkCode=0x7B)
            self.assertEqual(backend._keyboard_callback(0, 0x0100, ctypes.addressof(key)), 1)
            self.assertEqual(self.move(backend, 0, 600, 1, backend._tag), 37)
            backend.actual = (0, 600)
            order.append("posted")

        def restore(x, y):
            self.assertEqual((x, y), backend.actual)
            self.assertEqual(backend._reference, backend.actual)
            self.assertIsNone(backend._warp_pending)
            order.append("restore")

        backend._mouse_input = send
        backend.after_pointer_position = restore
        backend.inject({"type": "position", "x": 0, "y": 600})
        self.assertEqual(order, ["posted", "restore"])
        self.assertEqual(backend.delivered, [{"type": "key", "key": "f12", "down": True}])

    def test_actual_position_without_extra_own_callback_is_a_valid_commit(self):
        backend = self.backend()
        def send(*_args, **_kwargs):
            # A foreign injector arriving at the target is not our ACK.
            self.assertEqual(self.move(backend, 500, 500, flags=1, tag=99), 37)
            self.assertFalse(backend._warp_pending.seen.is_set())
            backend.actual = (500, 500)
        backend._mouse_input = send
        backend.warp(500, 500)
        self.assertIsNone(backend._warp_pending)
        self.assertIsNotNone(backend._handler)
        self.assertFalse(backend._stop_requested.is_set())
        self.assertEqual(backend._reference,(500,500))

    def test_unrelated_own_move_does_not_ack_and_cancel_wakes_without_stuck_capture(self):
        backend = self.backend()
        def send(*_args, **_kwargs):
            self.move(backend, 400, 400, flags=1, tag=backend._tag)
            self.assertFalse(backend._warp_pending.seen.is_set())
            pending = backend._warp_pending
            backend._cancel_warp()
            self.assertTrue(pending.seen.is_set())
        backend._mouse_input = send
        with self.assertRaisesRegex(OSError, "cancelled"):
            backend.warp(500, 500)
        self.assertIsNone(backend._warp_pending)
        self.assertIsNone(backend._handler)
        self.assertTrue(backend._stop_requested.is_set())


if __name__ == "__main__":
    unittest.main()
