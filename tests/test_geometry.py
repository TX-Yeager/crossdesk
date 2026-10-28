"""Desktop topology tests: no native input, networking, or display changes."""
from dataclasses import replace
import unittest

from crossdesk.geometry import Desktop, Screen, snap_screen, screen_connections


def screen(name, machine, x, y, w=100, h=100, **native):
    return Screen(name, machine, name, native.get("native_x", x),
                  native.get("native_y", y), native.get("native_w", w),
                  native.get("native_h", h), x, y, w, h)


class GeometryTests(unittest.TestCase):
    def test_traverses_each_adjacent_edge_and_returns(self):
        cases = [
            ((100, 0), (80, 0), (-100, 0)),
            ((-100, 0), (-80, 0), (100, 0)),
            ((0, 100), (0, 80), (0, -100)),
            ((0, -100), (0, -80), (0, 100)),
        ]
        for destination, outward, inward in cases:
            with self.subTest(destination=destination):
                a = screen("a", "win", 0, 0)
                b = screen("b", "mac", *destination)
                desktop = Desktop([a, b])
                desktop.validate_layout()
                target, x, y = desktop.advance("a", 50, 50, *outward)
                self.assertEqual(target.id, "b")
                self.assertTrue(b.contains(x, y))
                target, x, y = desktop.advance(target.id, x, y, *inward)
                self.assertEqual(target.id, "a")
                self.assertTrue(a.contains(x, y))

    def test_pointer_moves_within_screen_without_crossing(self):
        a = screen("a", "win", 0, 0)
        target, x, y = Desktop([a]).advance("a", 50, 50, -20, 30)
        self.assertEqual((target, x, y), (a, 30, 80))

    def test_zero_motion_at_edge_never_switches_computer(self):
        a, b = screen("a", "win", 0, 0), screen("b", "mac", 100, 0)
        target, x, y = Desktop([a, b]).advance("a", 99, 40, 0, 0)
        self.assertEqual((target.id, x, y), ("a", 99, 40))

    def test_native_clipped_last_pixel_can_cross(self):
        a, b = screen("a", "win", 0, 0), screen("b", "mac", 100, 0)
        target, x, _ = Desktop([a, b]).advance("a", 99, 40, 2, 0)
        self.assertEqual(target.id, "b")
        self.assertGreater(x, 100)

    def test_gaps_stop_motion_but_preserve_tangential_component(self):
        a, b = screen("a", "win", 0, 0), screen("b", "mac", 110, 0)
        target, x, y = Desktop([a, b]).advance("a", 90, 20, 40, 30)
        self.assertEqual((target.id, x, y), ("a", 99, 50))

    def test_partial_edge_does_not_teleport_to_a_nonadjacent_row(self):
        a, b = screen("a", "win", 0, 0), screen("b", "mac", 100, 60)
        desktop = Desktop([a, b])
        target, x, y = desktop.advance("a", 90, 20, 30, 0)
        self.assertEqual((target.id, x, y), ("a", 99, 20))
        target, x, y = desktop.advance("a", 90, 80, 30, 0)
        self.assertEqual(target.id, "b")

    def test_sliding_along_outer_wall_can_reach_an_adjacent_monitor(self):
        a, b = screen("a", "win", 0, 0), screen("b", "mac", 100, 0)
        target, x, y = Desktop([a, b]).advance("a", 50, 10, 100, -50)
        self.assertEqual(target.id, "b")
        self.assertTrue(b.contains(x, y))
        self.assertEqual(y, 0)

    def test_large_motion_crosses_multiple_screens_in_order(self):
        screens = [screen(str(i), "mac" if i < 2 else "win", i * 100, 0)
                   for i in range(4)]
        target, x, y = Desktop(screens).advance("0", 20, 40, 300, 0)
        self.assertEqual(target.id, "3")
        self.assertTrue(target.contains(x, y))

    def test_drag_can_cross_same_mac_monitors_but_stops_at_windows(self):
        built_in = screen("mac:built-in", "mac", -100, 100)
        external = screen("mac:external", "mac", -100, 0)
        windows = screen("win:external", "win", 0, 0)
        desktop = Desktop([built_in, external, windows])
        target, x, y = desktop.advance(built_in.id, -50, 150, 0, -100,
                                       allow_machine_cross=False)
        self.assertEqual(target.id, external.id)
        target, x, y = desktop.advance(target.id, x, y, 100, 0,
                                       allow_machine_cross=False)
        self.assertEqual(target.id, external.id)
        self.assertEqual(x, -1)

    def test_negative_native_coordinates_and_retina_canvas_round_trip(self):
        monitor = screen("mac:retina", "mac", -640, -360, 640, 360,
                         native_x=-1280, native_y=-720, native_w=1280, native_h=720)
        for x, y in ((-1280, -720), (-640, -360), (-1, -1)):
            logical = monitor.from_native(x, y)
            native = monitor.to_native(*logical)
            self.assertAlmostEqual(native[0], x)
            self.assertAlmostEqual(native[1], y)
        self.assertEqual(monitor.to_native(10000, 10000), (-1, -1))

    def test_native_monitor_lookup_uses_os_coordinates_after_canvas_drag(self):
        first = screen("one", "mac", 900, 900, native_x=-100, native_y=0)
        second = screen("two", "mac", 0, 0, native_x=0, native_y=0)
        desktop = Desktop([first, second])
        self.assertEqual(desktop.native_screen("mac", -50, 20).id, "one")
        self.assertEqual(desktop.native_screen("mac", 50, 20).id, "two")
        with self.assertRaises(ValueError):
            desktop.native_screen("missing", 0, 0)

    def test_overlap_and_duplicate_ids_are_rejected(self):
        a = screen("a", "win", 0, 0)
        with self.assertRaises(ValueError):
            Desktop([a, screen("b", "mac", 50, 20)]).validate_layout()
        with self.assertRaises(ValueError):
            Desktop([a, replace(a, machine="mac")])

    def test_wire_geometry_rejects_nonfinite_boolean_and_tiny_dimensions(self):
        row = screen("a", "win", 0, 0).as_dict()
        for changes in ({"x": float("nan")}, {"y": float("inf")},
                        {"w": True}, {"h": 1}, {"machine": ""}, {"id": 12}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Screen.from_dict({**row, **changes})
        self.assertEqual(Screen.from_dict(row).as_dict(), row)

    def test_snap_creates_a_touching_boundary(self):
        a = screen("a", "win", 0, 0)
        b = screen("b", "mac", 112, 8)
        snapped = snap_screen(b, [a], distance=20)
        self.assertEqual((snapped.x, snapped.y), (100, 0))
        Desktop([a, snapped]).validate_layout()

    def test_connection_segments_match_crossable_partial_edges(self):
        a=screen("a","win",0,0)
        b=screen("b","mac",100,40)
        connections=screen_connections([a,b])
        self.assertEqual([(row[2:]) for row in connections],[(100,40,100,100)])
        desktop=Desktop([a,b])
        target,*_=desktop.advance(a.id,99,70,1,0)
        self.assertEqual(target.id,b.id)

    def test_gap_and_corner_touch_do_not_advertise_a_channel(self):
        a=screen("a","win",0,0)
        self.assertEqual(screen_connections([a,screen("b","mac",103,0)]),[])
        self.assertEqual(screen_connections([a,screen("b","mac",100,100)]),[])

    def test_horizontal_and_reversed_connections(self):
        a=screen("a","win",0,0)
        b=screen("b","mac",20,100)
        self.assertEqual(screen_connections([b,a])[0][2:],(20,100,100,100))


if __name__ == "__main__":
    unittest.main()
