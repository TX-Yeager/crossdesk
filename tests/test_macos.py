"""Mac screen discovery with simulated Quartz calls; runnable on Windows."""
import ctypes as C
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from crossdesk.backends.macos import MacOSBackend, CGRect, CGPoint, CGSize, TAG, USER_DATA


class FakeQuartz:
    def __init__(self, active, online):
        self.active, self.online = active, online
        self.calls = []
        self.bounds = {1: CGRect(CGPoint(0, 0), CGSize(1728, 1117)),
                       3: CGRect(CGPoint(0, -1440), CGSize(2560, 1440))}
        self.error = None

    def query(self, name, ids, maximum, output, count):
        self.calls.append(name)
        if self.error == name:
            return 1000
        value = len(ids) if output is None else min(maximum, len(ids))
        C.cast(count, C.POINTER(C.c_uint32)).contents.value = value
        if output is not None:
            for index, display in enumerate(ids[:value]):
                output[index] = display
        return 0

    def CGGetActiveDisplayList(self, maximum, output, count):
        return self.query("active", self.active, maximum, output, count)

    def CGGetOnlineDisplayList(self, maximum, output, count):
        return self.query("online", self.online, maximum, output, count)

    def CGDisplayBounds(self, display):
        return self.bounds[display]

    def CGDisplayIsBuiltin(self, display):
        return int(display == 1)

    def CGMainDisplayID(self):
        return 1


class MacScreenTests(unittest.TestCase):
    def backend(self, active, online):
        backend = MacOSBackend.__new__(MacOSBackend)
        backend.cg = FakeQuartz(active, online)
        return backend

    def test_active_displays_keep_stable_ids_and_readable_names(self):
        backend = self.backend([1, 3], [1, 3])
        screens = backend.screens()
        self.assertEqual([s["id"] for s in screens], ["1", "3"])
        self.assertEqual([s["name"] for s in screens], ["内建显示器", "外接显示器 1"])
        self.assertTrue(screens[0]["primary"])
        self.assertEqual(backend.cg.calls, ["active", "active"])

    def test_sleeping_displays_fall_back_to_online_with_original_geometry(self):
        backend = self.backend([], [1, 3])
        screens = backend.screens()
        self.assertEqual(backend.cg.calls, ["active", "online", "online"])
        self.assertEqual([(s["width"], s["height"]) for s in screens],
                         [(1728, 1117), (2560, 1440)])
        self.assertEqual(screens[1]["y"], -1440)
        backend.cg.active = [1, 3]
        self.assertEqual(backend.screens(), screens)

    def test_online_query_errors_remain_explicit(self):
        backend = self.backend([], [1, 3])
        backend.cg.error = "online"
        with self.assertRaisesRegex(RuntimeError, "CGGetOnlineDisplayList"):
            backend.screens()

    def test_disconnected_headless_host_does_not_invent_a_display(self):
        self.assertEqual(self.backend([], []).screens(), [])


class FakeInputQuartz:
    def __init__(self):
        self.fields = {USER_DATA: 0, 4: 0, 5: 0}

    def CGEventGetIntegerValueField(self, _event, field):
        return self.fields.get(field, 0)

    def CGEventGetLocation(self, _event):
        return CGPoint(7, 9)


class MacMetricsTests(unittest.TestCase):
    metric_names = {"move_seen", "move_zero_delta", "move_nonzero_delta",
                    "own_tag_skipped", "callback_failures", "tap_enabled"}

    def backend(self):
        backend = MacOSBackend.__new__(MacOSBackend)
        backend.cg = FakeInputQuartz()
        backend._stopping = threading.Event()
        backend._handler = lambda _event: False
        return backend

    def test_outgoing_motion_does_not_require_native_cursor_recentering(self):
        self.assertFalse(MacOSBackend.needs_remote_anchor)
        self.assertFalse(self.backend().needs_remote_anchor)

    def test_motion_keeps_native_delta_independent_of_absolute_cursor_location(self):
        backend = self.backend()
        backend.cg.fields.update({4: 12, 5: -3})
        backend.cg.CGEventGetLocation = lambda _event: CGPoint(-405, -1440)
        delivered = []
        backend._handler = lambda event: delivered.append(event) or True
        self.assertIsNone(backend._event_callback(0, 5, 123, 0))
        self.assertEqual(delivered, [{"type": "move", "x": -405, "y": -1440,
                                      "dx": 12, "dy": -3}])

    def test_uninitialized_test_backend_reports_only_empty_aggregate_metrics(self):
        metrics = MacOSBackend.__new__(MacOSBackend).metrics()
        self.assertEqual(set(metrics), self.metric_names)
        self.assertFalse(metrics["tap_enabled"])
        self.assertTrue(all(value == 0 for value in metrics.values()))

    def test_native_zero_and_nonzero_moves_are_counted_without_changing_routing(self):
        backend = self.backend()
        delivered = []
        backend._handler = lambda event: delivered.append(event) or False
        self.assertEqual(backend._event_callback(0, 5, 123, 0), 123)
        self.assertEqual(delivered, [])  # Zero-delta handling remains unchanged.
        backend.cg.fields[4] = 3
        self.assertEqual(backend._event_callback(0, 6, 123, 0), 123)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(backend.metrics(), {
            "move_seen": 2, "move_zero_delta": 1, "move_nonzero_delta": 1,
            "own_tag_skipped": 0, "callback_failures": 0, "tap_enabled": False,
        })

    def test_own_tag_is_counted_without_decoding_or_forwarding(self):
        backend = self.backend()
        backend.cg.fields[USER_DATA] = TAG
        backend._decode = mock.Mock(side_effect=AssertionError("Must not decode own input"))
        backend._handler = mock.Mock()
        self.assertEqual(backend._event_callback(0, 5, 123, 0), 123)
        backend._decode.assert_not_called()
        backend._handler.assert_not_called()
        self.assertEqual(backend.metrics()["own_tag_skipped"], 1)
        self.assertEqual(backend.metrics()["move_seen"], 0)

    def test_callback_failure_is_counted_and_logs_no_exception_payload(self):
        backend = self.backend()
        backend.cg.fields[4] = 1
        backend._handler = mock.Mock(side_effect=RuntimeError("private-input-debug-marker"))
        with self.assertLogs("crossdesk.backends.macos", level="ERROR") as captured:
            self.assertEqual(backend._event_callback(0, 5, 123, 0), 123)
        self.assertTrue(backend._stopping.is_set())
        self.assertEqual(backend.metrics()["callback_failures"], 1)
        self.assertNotIn("private-input-debug-marker", "\n".join(captured.output))
        self.assertTrue(all(isinstance(value, (int, bool)) for value in backend.metrics().values()))

    def test_native_tap_pointer_read_and_query_hold_lifecycle_lock(self):
        class TrackingLock:
            entered = False
            def __enter__(lock):
                lock.entered = True
            def __exit__(lock, *_args):
                lock.entered = False

        class GuardedBackend(MacOSBackend):
            @property
            def _tap(instance):
                if not instance._lifecycle.entered:
                    raise AssertionError("Tap pointer read without lifecycle lock")
                return instance.test_tap

        backend = GuardedBackend.__new__(GuardedBackend)
        backend._lifecycle = TrackingLock()
        backend.test_tap = 55
        queries = []
        def query(tap):
            self.assertTrue(backend._lifecycle.entered)
            queries.append(tap)
            return True
        backend.cg = SimpleNamespace(CGEventTapIsEnabled=query)
        self.assertTrue(backend.metrics()["tap_enabled"])
        self.assertEqual(queries, [55])
        self.assertFalse(backend._lifecycle.entered)
        backend.test_tap = None
        self.assertFalse(backend.metrics()["tap_enabled"])
        self.assertEqual(queries, [55])  # Never query a null/released tap.

    def test_start_resets_previous_session_counters_without_native_input(self):
        backend = self.backend()
        backend._lifecycle = threading.RLock()
        backend._thread = None
        backend._ready = threading.Event()
        backend.permissions = lambda: {"accessibility": True, "input_monitoring": True}
        backend.cg.CGEventSourceFlagsState = lambda _state: 0
        backend._move_seen = backend._callback_failures = 9
        fake_thread = SimpleNamespace(start=backend._ready.set, is_alive=lambda: True)
        with mock.patch("crossdesk.backends.macos.threading.Thread", return_value=fake_thread):
            backend.start(lambda _event: False)
        self.assertTrue(all(value == 0 for value in backend.metrics().values()))

    def test_tap_enabled_binding_uses_bool_return_and_pointer_argument(self):
        backend = MacOSBackend.__new__(MacOSBackend)
        backend.cg, backend.cf, backend.ax = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(C.c_void_p, "in_dll", return_value=C.c_void_p(7)):
            backend._bind()
        self.assertIs(backend.cg.CGEventTapIsEnabled.restype, C.c_bool)
        self.assertEqual(backend.cg.CGEventTapIsEnabled.argtypes, [C.c_void_p])


class MacScrollInjectionTests(unittest.TestCase):
    def backend(self):
        backend=MacOSBackend.__new__(MacOSBackend)
        backend._inject_lock=threading.RLock()
        backend._source=42;backend._pixels_per_line=10
        backend.events=[]
        def create(*args):
            event={'create':args};backend.events.append(event);return event
        def field(event,key,value):event[key]=value
        backend.cg=SimpleNamespace(CGEventCreateScrollWheelEvent2=create,
            CGEventSetIntegerValueField=field,CGEventSetDoubleValueField=field)
        backend._post=lambda event:None
        return backend
    def test_lines_and_pixels_are_consistent_in_vertical_and_horizontal_fields(self):
        backend=self.backend();backend.inject({'type':'scroll','dx':2,'dy':3})
        event=backend.events[-1]
        self.assertEqual((event[93],event[94]),(3,-2))
        self.assertEqual((event[96],event[97]),(30,-20))
        self.assertEqual((event[11],event[12]),(3,-2))
    def test_fractional_smooth_events_retain_subpixel_distance(self):
        backend=self.backend()
        for _ in range(4):backend.inject({'type':'scroll','dx':0,'dy':.025})
        self.assertEqual(sum(event[96] for event in backend.events),1)
        self.assertAlmostEqual(sum(event[93] for event in backend.events),.1)

if __name__ == "__main__":
    unittest.main()
