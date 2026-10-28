"""Handoff presentation follows real routing, using fake native input only."""
from dataclasses import replace
import unittest

from tests import test_engine as routing


class HandoffRoutingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = routing.EngineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    @staticmethod
    def effects(engine):
        return [value for kind, value in engine.test_notifications if kind == "handoff"]

    def clear_effects(self, *engines):
        for engine in engines:
            engine.test_notifications.clear()

    def assert_effect(self, engine, phase, edge):
        events = self.effects(engine)
        self.assertEqual(len(events), 1, events)
        event = events[0]
        self.assertEqual((event["phase"], event["edge"]), (phase, edge))
        bounds = event["screen"]
        self.assertLessEqual(bounds["x"], event["x"])
        self.assertLessEqual(event["x"], bounds["x"] + bounds["width"] - 1)
        self.assertLessEqual(bounds["y"], event["y"])
        self.assertLessEqual(event["y"], bounds["y"] + bounds["height"] - 1)
        self.assertGreater(event["created_at"], 0)
        return event

    def test_windows_mouse_round_trip_animates_departure_and_arrival_once(self):
        win, mac = self.fixture.pair()
        self.clear_effects(win, mac)
        self.fixture.move(win, 80)
        self.fixture.pump(win, mac)
        self.assert_effect(win, "depart", "right")
        self.assert_effect(mac, "arrive", "left")

        self.clear_effects(win, mac)
        self.fixture.move(win, -80)
        self.fixture.pump(win, mac)
        self.assert_effect(win, "arrive", "right")
        self.assert_effect(mac, "depart", "left")
        self.assertTrue(win.enabled and mac.enabled)

    def test_mac_trackpad_round_trip_uses_the_opposite_edges(self):
        win, mac = self.fixture.pair()
        self.clear_effects(win, mac)
        self.fixture.move(mac, -80)
        self.fixture.pump(win, mac)
        self.assert_effect(mac, "depart", "left")
        self.assert_effect(win, "arrive", "right")

        self.clear_effects(win, mac)
        self.fixture.move(mac, 80)
        self.fixture.pump(win, mac)
        self.assert_effect(mac, "arrive", "left")
        self.assert_effect(win, "depart", "right")

    def test_vertical_monitor_arrangement_uses_top_and_bottom_edges(self):
        win, mac = self.fixture.pair(enable=False)
        win.update_layout([replace(screen, x=0, y=0 if screen.machine == "win" else -100)
                           for screen in win.desktop.screens])
        self.fixture.pump(win, mac)
        win.set_enabled(True)
        self.fixture.pump(win, mac)
        self.clear_effects(win, mac)
        self.fixture.move(win, 0, -80)
        self.fixture.pump(win, mac)
        self.assert_effect(win, "depart", "top")
        self.assert_effect(mac, "arrive", "bottom")
        self.clear_effects(win, mac)
        self.fixture.move(win, 0, 80)
        self.fixture.pump(win, mac)
        self.assert_effect(win, "arrive", "top")
        self.assert_effect(mac, "depart", "bottom")

    def test_same_machine_monitor_change_and_ordinary_motion_do_not_animate(self):
        backend = routing.FakeBackend()
        backend.native_screens.append({"name": "second", "x": 100, "y": 0,
                                       "width": 100, "height": 100})
        win = self.fixture.engine("win", True, backend=backend)
        mac = self.fixture.engine("mac")
        win._hello()
        mac._hello()
        self.fixture.pump(win, mac)
        win.set_enabled(True)
        self.fixture.pump(win, mac)
        self.clear_effects(win, mac)
        self.fixture.move(win, 5)
        self.fixture.move(win, 95, point=(150, 50))
        self.fixture.pump(win, mac)
        self.assertEqual(win.current, "win:second")
        self.assertEqual(self.effects(win), [])
        self.assertEqual(self.effects(mac), [])

    def test_keyboard_focus_and_rejected_stale_or_paused_focus_do_not_animate(self):
        win, mac = self.fixture.pair()
        self.clear_effects(win, mac)
        mac._network({"type": "keyboard_focus", "screen": "win:main",
                      "focus_revision": list(mac._keyboard_revision)})
        stale = [max(0, mac._keyboard_revision[0] - 1), "win"]
        mac._network({"type": "focus", "screen": "mac:main", "u": 0, "v": .5,
                      "focus_revision": stale})
        with self.assertRaises(ValueError):
            mac._network({"type": "focus", "screen": "mac:main", "u": 1.5, "v": .5})
        mac.set_enabled(False, propagate=False)
        mac._network({"type": "focus", "screen": "mac:main", "u": 0, "v": .5})
        self.assertEqual(self.effects(win), [])
        self.assertEqual(self.effects(mac), [])

    def test_duplicate_focus_and_continuous_remote_motion_do_not_replay_arrival(self):
        win, mac = self.fixture.pair()
        self.fixture.move(win, 80)
        focus = next(message.copy() for message in win.transport.messages
                     if message["type"] == "focus")
        self.fixture.pump(win, mac)
        self.clear_effects(win, mac)
        mac._network(focus)
        self.fixture.move(win, 10)
        self.fixture.pump(win, mac)
        self.assertEqual(self.effects(mac), [])
        self.assertEqual(self.effects(win), [])

    def test_malformed_optional_visual_metadata_cannot_interrupt_pointer_routing(self):
        for malformed in ([], {}):
            with self.subTest(metadata=malformed):
                win, mac = self.fixture.pair()
                mac._network({"type": "focus", "screen": "mac:main", "u": 0,
                              "v": .5, "from_screen": malformed})
                self.assertTrue(mac.incoming)
                self.assertEqual(mac.backend.pointer, (0, 49.5))
                mac._network({"type": "release", "handoff": {
                    "from": "mac:main", "to": malformed, "u": 0, "v": .5}})
                self.assertFalse(mac.incoming)
                self.assertTrue(mac.enabled)
                self.assertFalse(mac.emergency.is_set())

    def test_negative_native_origins_and_scaled_layout_use_native_screen_coordinates(self):
        win_backend, mac_backend = routing.FakeBackend(), routing.FakeBackend()
        win_backend.native_screens = [{"name": "main", "x": -200, "y": -100,
                                       "width": 200, "height": 100}]
        mac_backend.native_screens = [{"name": "main", "x": -1440, "y": -900,
                                       "width": 1440, "height": 900}]
        win_backend.pointer, mac_backend.pointer = (-100, -50), (-720, -450)
        win = self.fixture.engine("win", True, backend=win_backend)
        mac = self.fixture.engine("mac", backend=mac_backend)
        win._hello()
        mac._hello()
        self.fixture.pump(win, mac)
        win.update_layout([replace(screen, x=0 if screen.machine == "win" else 100,
                                   y=0, w=100, h=100) for screen in win.desktop.screens])
        self.fixture.pump(win, mac)
        win.set_enabled(True)
        self.fixture.pump(win, mac)
        self.clear_effects(win, mac)
        self.fixture.move(win, 99, point=(-1, -50))
        self.fixture.pump(win, mac)
        departed = self.assert_effect(win, "depart", "right")
        arrived = self.assert_effect(mac, "arrive", "left")
        self.assertEqual((departed["x"], departed["y"]), (-1, -50))
        self.assertEqual(arrived["x"], -1440)
        self.assertAlmostEqual(arrived["y"], -900 + 50 / 99 * 899)

    def test_first_mac_input_crosses_from_the_touching_external_edge_only(self):
        # Match the real three-screen arrangement plus a remembered retired ID.
        saved = [{"id": "win:display", "x": 0, "y": 0, "w": 2560, "h": 1440},
                 {"id": "mac:book", "x": -2155, "y": 1440, "w": 1728, "h": 1117},
                 {"id": "mac:external", "x": -2560, "y": 0, "w": 2560, "h": 1440},
                 {"id": "mac:retired-external", "x": -2560, "y": 0,
                  "w": 2560, "h": 1440}]
        for start_on_book in (False, True):
            with self.subTest(start_on_book=start_on_book):
                win_backend, mac_backend = routing.FakeBackend(), routing.FakeBackend()
                win_backend.native_screens = [{"name": "display", "x": 0, "y": 0,
                                               "width": 3840, "height": 2160}]
                mac_backend.native_screens = [
                    {"name": "book", "x": 0, "y": 0, "width": 1728, "height": 1117},
                    {"name": "external", "x": -405, "y": -1440,
                     "width": 2560, "height": 1440}]
                win_backend.pointer = (1920, 1080)
                mac_backend.pointer = (1000, 500) if start_on_book else (2100, -720)
                win = self.fixture.engine("win", True, backend=win_backend, saved_layout=saved)
                mac = self.fixture.engine("mac", backend=mac_backend, saved_layout=saved)
                win._hello()
                mac._hello()
                self.fixture.pump(win, mac)
                win.set_enabled(True)
                self.fixture.pump(win, mac)
                self.assertEqual((win.pointer_owner, mac.pointer_owner), ("win", "win"))
                self.assertNotIn("mac:retired-external", mac.desktop.by_id)
                self.clear_effects(win, mac)
                if start_on_book:
                    # The book's right edge ends at x=-427 and never touches Windows.
                    self.assertFalse(self.fixture.move(mac, 727, point=(1727, 500)))
                    self.fixture.pump(win, mac)
                    self.assertEqual(mac.current, "mac:book")
                    self.assertEqual(self.effects(mac), [])
                    self.assertFalse(win.incoming)
                    # Travel natively to the external monitor before crossing.
                    self.fixture.move(mac, 373, -1220, point=(2100, -720))
                    self.fixture.pump(win, mac)
                self.assertFalse(self.fixture.move(mac, 54, point=(2154, -720)))
                self.fixture.pump(win, mac)
                self.assertTrue(win.incoming)
                self.assertEqual(mac.desktop.by_id[mac.current].machine, "win")
                self.assert_effect(mac, "depart", "right")
                self.assert_effect(win, "arrive", "left")
                self.assertTrue(win.enabled and mac.enabled)


if __name__ == "__main__":
    unittest.main()
