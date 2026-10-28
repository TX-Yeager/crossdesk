"""Routing/safety tests using fake backends and transport, never real input."""
from dataclasses import replace
import queue
import threading
import time
import unittest
from unittest.mock import patch

from crossdesk.engine import Engine
import uuid


class FakeBackend:
    def __init__(self):
        self.pointer = (50.0, 50.0)
        self.injected = []
        self.warps = []
        self.keys, self.buttons = set(), set()
        self.releases = self.stops = 0
        self.starts = 0
        self.start_error = None
        self.running = False
        self.handler = None
        self.native_screens=[{"name":"main","x":0,"y":0,"width":100,"height":100}]

    def screens(self):
        return list(self.native_screens)

    def position(self):
        return self.pointer

    def start(self, handler):
        self.starts += 1
        if self.start_error:
            raise self.start_error
        if self.running:
            raise RuntimeError("capture already running")
        self.handler, self.running = handler, True

    def stop(self):
        self.running = False
        self.stops += 1

    def warp(self, x, y):
        self.pointer = (x, y)
        self.warps.append(self.pointer)

    def inject(self, event):
        self.injected.append(dict(event))
        kind = event["type"]
        if kind == "position":
            self.pointer = (event["x"], event["y"])
        elif kind == "key":
            (self.keys.add if event["down"] else self.keys.discard)(event["key"])
        elif kind == "button":
            (self.buttons.add if event["down"] else self.buttons.discard)(event["button"])

    def release_all(self):
        self.releases += 1
        self.keys.clear()
        self.buttons.clear()


class FakeTransport:
    def __init__(self, is_listener=False):
        self.connected = True
        self.is_listener = is_listener
        self.messages = []
        self.sent = []

    def send(self, message):
        if not self.connected:
            return False
        self.messages.append(message)
        self.sent.append(message)
        return True

    def stop(self):
        self.connected = False


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("router did not reach expected state")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            engine.close()
        for engine in self.engines:
            engine.worker.join(timeout=1)

    def engine(self, machine_id="win", listener=False, worker=False, backend=None, saved_layout=None):
        backend, notifications = backend or FakeBackend(), []
        backend.needs_remote_anchor=machine_id!="mac"
        machine = {"id": machine_id, "name": machine_id,
                   "platform": "darwin" if machine_id == "mac" else "win32"}
        if worker:
            engine = Engine(backend, machine, lambda k, v: notifications.append((k, v)),saved_layout)
        else:
            # Let the real thread exit immediately for deterministic state tests.
            with patch.object(Engine, "_run", return_value=None):
                engine = Engine(backend, machine, lambda k, v: notifications.append((k, v)),saved_layout)
            engine.worker.join(timeout=1)
        engine.transport = FakeTransport(listener)
        engine.test_notifications = notifications
        self.engines.append(engine)
        return engine

    @staticmethod
    def pump(left, right):
        for _ in range(30):
            changed = False
            for source, destination in ((left, right), (right, left)):
                while source.transport.messages:
                    changed = True
                    destination._network(source.transport.messages.pop(0))
            if not changed:
                return
        raise AssertionError("message exchange did not settle")

    def pair(self, listener="win", enable=True):
        win = self.engine("win", listener == "win")
        mac = self.engine("mac", listener == "mac")
        win._hello()
        mac._hello()
        self.pump(win, mac)
        if enable:
            win.set_enabled(True)
            self.pump(win, mac)
            self.assertTrue(win.enabled and mac.enabled)
            self.assertFalse(win.control_pending or mac.control_pending)
        return win, mac

    def test_start_initiator_is_single_global_pointer_owner_before_first_move(self):
        win,mac=self.pair(enable=False)
        mac.set_enabled(True);self.pump(win,mac)
        self.assertEqual(win.keyboard_target,'mac')
        self.assertEqual(mac.keyboard_target,'mac')
        self.assertTrue(win.enabled and mac.enabled)

    def test_wheel_smoothing_is_cancelled_on_owner_change(self):
        win,mac=self.pair()
        mac.backend.smooth_wheel=True
        mac._scroll_curve.clock=lambda:0
        mac._inject_input({'type':'scroll','dx':0,'dy':1},'peer')
        self.assertTrue(mac._scroll_curve.pending)
        local=mac.desktop.native_screen('mac',50,50)
        mac._set_keyboard_focus(local,force=True)
        other=next(screen for screen in mac.desktop.screens if screen.machine=='win')
        mac._set_keyboard_focus(other,force=True)
        self.assertFalse(mac._scroll_curve.pending)

    def test_both_scroll_sources_follow_global_pointer_even_without_incoming_focus(self):
        win,mac=self.pair()
        event={'type':'scroll','dx':0,'dy':.5}
        self.assertFalse(win.incoming)
        mac._local(event);self.pump(win,mac)
        self.assertIn(event,win.backend.injected)
        self.assertNotIn(event,mac.backend.injected)
        local=mac.desktop.native_screen('mac',50,50)
        mac._set_keyboard_focus(local,broadcast=True,force=True);self.pump(win,mac)
        self.assertFalse(mac.incoming)
        win._local(event);self.pump(win,mac)
        self.assertIn(event,mac.backend.injected)

    def move(self,engine,dx,dy=0,point=None):
        """A hardware sample, including native OS movement when passed through."""
        if point is None:
            point=(max(0,min(99,engine.backend.pointer[0]+dx)),max(0,min(99,engine.backend.pointer[1]+dy)))
        event={"type":"move","x":point[0],"y":point[1],"dx":dx,"dy":dy}
        blocked=engine.capture(event)
        if not blocked:
            engine.backend.pointer=point
        try:
            kind,queued=engine.events.get_nowait()
        except queue.Empty:
            return blocked
        self.assertEqual(kind,"local")
        engine._local(queued)
        return blocked

    def test_each_computer_can_drive_the_other_regardless_of_network_role(self):
        for listener in ("win", "mac"):
            with self.subTest(listener=listener):
                win, mac = self.pair(listener)
                # Determine direction from the shared layout, not listen/connect.
                local = win.desktop.by_id[win.current]
                remote = next(s for s in win.desktop.screens if s.machine == "mac")
                direction = 1 if remote.x >= local.right else -1
                self.assertFalse(self.move(win,direction*80))
                self.pump(win, mac)
                self.assertEqual(win.desktop.by_id[win.current].machine, "mac")
                self.assertTrue(mac.incoming)
                win._local({"type": "button", "button": "left", "down": True})
                self.pump(win, mac)
                self.assertIn("left", mac.backend.buttons)
                win._local({"type": "button", "button": "left", "down": False})
                self.pump(win, mac)
                # Mac's physical trackpad reclaims control and crosses back.
                self.assertFalse(self.move(mac,-direction*100))
                self.pump(win, mac)
                self.assertEqual(mac.desktop.by_id[mac.current].machine, "win")
                self.assertTrue(win.incoming)
                self.assertFalse(mac.incoming)
                mac._local({"type": "key", "key": "a", "down": True})
                self.pump(win, mac)
                self.assertIn("a", win.backend.keys)

    def test_local_keyboard_is_reinjected_but_mouse_remains_native(self):
        win, mac = self.pair()
        win._local({"type": "key", "key": "ctrl_l", "down": True})
        self.assertFalse(self.move(win,5,7))
        self.assertIn("ctrl_l", win.backend.keys)
        self.assertEqual(win.backend.pointer, (55, 57))
        self.assertFalse(any(row["type"]=="position" for row in win.backend.injected))
        self.assertFalse(any(row["type"] == "input" for row in win.transport.messages))

    def test_local_absolute_move_works_when_os_position_already_updated_and_delta_zero(self):
        win,mac=self.pair()
        win.backend.pointer=(67,64)  # Hook-time GetCursorPos already sees this point.
        self.assertFalse(self.move(win,0,0,point=(67,64)))
        self.assertEqual(win.cursor,(67,64))
        self.assertFalse(any(row["type"]=="position" for row in win.backend.injected))

    def test_zero_move_and_zero_scroll_never_reclaim_an_incoming_pointer(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        point=mac.backend.pointer
        before=mac.backend.releases
        for event in ({"type":"move","x":point[0],"y":point[1],"dx":0,"dy":0},
                      {"type":"scroll","dx":0,"dy":0}):
            self.assertFalse(mac.capture(event))
            mac._local(event)
        self.assertTrue(mac.incoming)
        self.assertEqual(mac.backend.releases,before)
        self.assertFalse(any(row["type"]=="reclaim" for row in mac.transport.sent))
        self.assertFalse(self.move(mac,0,0,point=(point[0]+1,point[1])))
        self.assertFalse(mac.incoming)
        self.assertTrue(any(row["type"]=="reclaim" for row in mac.transport.sent))

    def test_repeated_remote_deltas_advance_without_receiver_zero_notifications_stealing_focus(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        initial=win.cursor[0]
        for _ in range(10):
            self.assertTrue(self.move(win,2))
            self.pump(win,mac)
            point=mac.backend.pointer
            self.assertFalse(mac.capture({"type":"move","x":point[0],"y":point[1],"dx":0,"dy":0}))
            self.assertTrue(mac.incoming)
        self.assertAlmostEqual(win.cursor[0]-initial,20)
        self.assertFalse(any(row["type"]=="reclaim" for row in mac.transport.sent))

    def test_native_move_queued_before_handoff_cannot_jump_back_after_focus(self):
        win,mac=self.pair()
        self.assertFalse(win.capture({"type":"move","x":99,"y":50,"dx":49,"dy":0}))
        self.assertFalse(win.capture({"type":"move","x":99,"y":55,"dx":0,"dy":5}))
        first=win.events.get_nowait()[1]
        stale=win.events.get_nowait()[1]
        win._local(first)
        cursor=win.cursor
        self.assertEqual(win.desktop.by_id[win.current].machine,"mac")
        win._local(stale)
        self.assertEqual(win.cursor,cursor)
        self.assertEqual(sum(row["type"]=="focus" for row in win.transport.sent),1)

    def test_return_to_local_injects_once_then_passes_native_moves(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        before=sum(row["type"]=="position" for row in win.backend.injected)
        self.assertTrue(win.capture({"type":"move","x":49,"y":50,"dx":-2,"dy":0}))
        self.assertTrue(win.capture({"type":"move","x":49,"y":50,"dx":-2,"dy":0}))
        win._local(win.events.get_nowait()[1])
        win._local(win.events.get_nowait()[1])
        self.assertEqual(win.desktop.by_id[win.current].machine,"win")
        self.assertEqual(sum(row["type"]=="position" for row in win.backend.injected),before+1)
        self.assertFalse(self.move(win,-5))
        self.assertEqual(sum(row["type"]=="position" for row in win.backend.injected),before+1)

    def test_windows_handoff_presents_remote_owner_until_return_edge_is_positioned(self):
        win,mac=self.pair()
        owners=[]
        warp=win.backend.warp
        def park(x,y):
            owners.append(('park',win.pointer_owner))
            warp(x,y)
        win.backend.park_remote=park
        inject=win.backend.inject
        def position(event):
            if event['type']=='position':owners.append(('return',win.pointer_owner,event['x']))
            inject(event)
        win.backend.inject=position
        self.move(win,80);self.pump(win,mac)
        self.move(win,-2);self.pump(win,mac)
        self.assertEqual(owners[0],('park','mac'))
        self.assertEqual(owners[1][0:2],('return','mac'))
        self.assertGreater(owners[1][2],95) # Enter Windows at its right edge.
        self.assertEqual(win.pointer_owner,'win')

    def test_windows_parking_interval_samples_cannot_shift_the_mac_entry_point(self):
        win,mac=self.pair()
        queued=[]
        def park(x,y):
            # A hook sample captured after the new target was selected but
            # before the parking coordinate was committed must be discarded.
            self.assertTrue(win.capture({'type':'move','x':0,'y':0,'dx':-2000,'dy':-1000}))
            queued.append(win.events.get_nowait()[1])
            win.backend.warp(x,y)
        win.backend.park_remote=park
        self.move(win,80);self.pump(win,mac)
        entry=win.cursor
        win._local(queued[0]);self.pump(win,mac)
        self.assertEqual(win.cursor,entry)
        self.assertEqual(win.pointer_owner,'mac')
        self.assertAlmostEqual(mac.backend.pointer[0],.011,places=4)

    def test_incoming_takeover_coalesces_physical_points_and_beats_queued_remote_pointer(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        screen=next(s for s in mac.desktop.screens if s.machine=="mac")
        old={"type":"pointer","screen":screen.id,"u":.8,"v":.8}
        self.assertFalse(mac.capture({"type":"move","x":20,"y":20,"dx":5,"dy":0}))
        mac.backend.pointer=(20,20)
        self.assertFalse(mac.capture({"type":"move","x":25,"y":22,"dx":5,"dy":2}))
        mac.backend.pointer=(25,22)
        first=mac.events.get_nowait()[1]
        second=mac.events.get_nowait()[1]
        before=sum(row["type"]=="position" for row in mac.backend.injected)
        mac._network(old)  # Queued remote packets cannot overwrite takeover.
        self.assertEqual(mac.backend.pointer,(25,22))
        # Also model an older OS post that was already submitted before capture.
        mac.backend.pointer=(79.2,79.2)
        mac._local(first)
        mac._local(second)
        self.assertFalse(mac.incoming)
        self.assertEqual(mac.backend.pointer,(25,22))
        self.assertEqual(mac.cursor,screen.from_native(25,22))
        self.assertEqual(sum(row["type"]=="position" for row in mac.backend.injected),before+1)
        self.assertFalse(self.move(mac,1,point=(26,22)))
        self.assertEqual(mac.backend.pointer,(26,22))
        self.assertEqual(sum(row["type"]=="position" for row in mac.backend.injected),before+1)

    def test_clipped_absolute_edge_uses_nonzero_direction_only_for_crossing_probe(self):
        win,mac=self.pair()
        win.backend.pointer=(99,50)
        win._reset_cursor()
        self.assertFalse(self.move(win,3,point=(99,50)))
        self.pump(win,mac)
        self.assertTrue(mac.incoming)
        self.assertAlmostEqual(win.cursor[0],100.011,places=5)

    def test_real_monitor_sizes_native_mac_two_screens_and_scaled_windows_crossing(self):
        saved=[{"id":"win:display","x":0,"y":0,"w":2560,"h":1440},
               {"id":"mac:external","x":-2560,"y":0,"w":2560,"h":1440},
               {"id":"mac:book","x":-2155,"y":1440,"w":1728,"h":1117}]
        wb,mb=FakeBackend(),FakeBackend()
        wb.native_screens=[{"name":"display","x":0,"y":0,"width":3840,"height":2160}]
        wb.pointer=(1920,1080)
        mb.native_screens=[{"name":"external","x":-405,"y":-1440,"width":2560,"height":1440},
                           {"name":"book","x":0,"y":0,"width":1728,"height":1117}]
        mb.pointer=(2100,-720)
        win=self.engine("win",True,backend=wb,saved_layout=saved)
        mac=self.engine("mac",backend=mb,saved_layout=saved)
        win._hello();mac._hello();self.pump(win,mac)
        win.set_enabled(True);self.pump(win,mac)
        self.assertFalse(self.move(mac,54,point=(2154,-720)))
        self.pump(win,mac)
        self.assertEqual(mac.desktop.by_id[mac.current].machine,"win")
        self.assertTrue(win.incoming)
        self.assertFalse(mb.warps)
        self.assertTrue(0<=wb.pointer[0]<1)
        self.assertTrue(1079<wb.pointer[1]<1082)
        self.assertFalse(self.move(win,-1,point=(0,wb.pointer[1])))
        self.pump(win,mac)
        self.assertEqual(win.desktop.by_id[win.current].machine,"mac")
        self.assertTrue(mac.incoming)
        self.assertTrue(2153<mb.pointer[0]<=2154)
        self.assertEqual(len(wb.warps),1)
        # Mac physical takeover then native travel onto its built-in screen.
        self.assertFalse(self.move(mac,-10,point=(2140,-700)))
        self.pump(win,mac)
        injected=len(mb.injected)
        self.assertFalse(self.move(mac,-2140,710,point=(0,10)))
        self.assertEqual(mac.current,"mac:book")
        self.assertEqual(mac.cursor,(-2155,1450))
        self.assertEqual(len(mb.injected),injected)
        self.assertFalse(mb.warps)

    def test_shortcut_translation_is_chord_specific_and_optional(self):
        win, mac = self.pair()
        win._local({"type": "key", "key": "ctrl_l", "down": True})
        self.move(win,80)
        self.pump(win, mac)
        self.assertNotIn("ctrl_l", win.backend.keys)
        self.assertNotIn("meta_l", mac.backend.keys)  # No bare modifier swap.
        win._local({"type":"key","key":"c","down":True})
        self.pump(win,mac)
        self.assertEqual(mac.backend.keys,{"meta_l","c"})
        win._local({"type":"key","key":"c","down":False})
        win._local({"type": "key", "key": "ctrl_l", "down": False})
        self.pump(win, mac)
        self.assertFalse(mac.backend.keys)
        win.swap_modifiers = False
        win._local({"type": "key", "key": "ctrl_r", "down": True})
        self.pump(win, mac)
        self.assertIn("ctrl_r", mac.backend.keys)

    def test_drag_stays_on_source_computer_until_mouse_up(self):
        win, mac = self.pair()
        win._local({"type": "button", "button": "left", "down": True})
        self.move(win,100)
        self.pump(win, mac)
        self.assertEqual(win.desktop.by_id[win.current].machine, "win")
        self.assertEqual(win.backend.pointer[0], 99)
        self.assertFalse(mac.incoming)
        win._local({"type": "button", "button": "left", "down": False})
        self.move(win,-1)
        self.move(win,3)
        self.pump(win, mac)
        self.assertEqual(win.desktop.by_id[win.current].machine, "mac")

    @staticmethod
    def key(engine,key,down=True):
        engine._local({"type":"key","key":key,"down":down})

    def test_both_keyboards_follow_incoming_pointer_without_reclaim(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        self.assertEqual(mac.current,"mac:main")
        self.key(mac,"b")
        self.key(win,"a")
        self.pump(win,mac)
        self.assertTrue(mac.incoming)
        self.assertEqual(mac.backend.keys,{"a","b"})
        self.assertEqual((win.keyboard_target,mac.keyboard_target),("mac","mac"))
        self.assertFalse(any(row["type"]=="reclaim" for row in mac.transport.sent))
        self.key(mac,"b",False)
        self.key(win,"a",False)
        self.move(mac,1)
        self.pump(win,mac)
        self.move(mac,-200)
        self.pump(win,mac)
        self.assertTrue(win.incoming)
        self.key(win,"a")
        self.key(mac,"b")
        self.pump(win,mac)
        self.assertTrue(win.incoming)
        self.assertEqual(win.backend.keys,{"a","b"})
        self.assertEqual((win.keyboard_target,mac.keyboard_target),("win","win"))

    def test_same_logical_key_from_both_keyboards_is_reference_counted(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        self.key(win,"a")
        self.pump(win,mac)
        self.key(mac,"a")
        self.key(win,"a",False)
        self.pump(win,mac)
        self.assertIn("a",mac.backend.keys)
        self.key(mac,"a",False)
        self.assertNotIn("a",mac.backend.keys)

    def test_modifiers_transfer_from_both_keyboards_and_up_never_sticks(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        self.key(win,"ctrl_l")
        self.key(win,"c")
        self.key(mac,"shift_l")
        self.pump(win,mac)
        self.assertEqual(mac.backend.keys,{"meta_l","c","shift_l"})
        self.move(win,-200)
        self.pump(win,mac)
        self.assertEqual((win.keyboard_target,mac.keyboard_target),("win","win"))
        self.assertEqual(win.backend.keys,{"ctrl_l","shift_l"})
        self.assertFalse(mac.backend.keys)
        self.key(win,"c",False)
        self.key(win,"ctrl_l",False)
        self.key(mac,"shift_l",False)
        self.pump(win,mac)
        self.assertFalse(win.backend.keys or mac.backend.keys)

    def test_click_can_reclaim_but_keyboard_and_scroll_do_not(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        self.key(mac,"a")
        mac._local({"type":"scroll","dx":0,"dy":1})
        self.assertTrue(mac.incoming)
        mac._local({"type":"button","button":"left","down":True})
        self.pump(win,mac)
        self.assertFalse(mac.incoming)
        self.assertEqual((win.keyboard_target,mac.keyboard_target),("mac","mac"))
        self.assertIn("a",mac.backend.keys)

    def test_first_local_pointer_activity_synchronizes_keyboard_destination(self):
        win,mac=self.pair()
        self.move(win,1)
        self.pump(win,mac)
        self.assertEqual((win.keyboard_target,mac.keyboard_target),("win","win"))
        self.key(mac,"a")
        self.pump(win,mac)
        self.assertIn("a",win.backend.keys)
        self.assertFalse(win.incoming)

    def test_simultaneous_local_activity_converges_and_stale_focus_is_ignored(self):
        win,mac=self.pair()
        self.move(win,1)
        self.move(mac,1)
        self.pump(win,mac)
        self.assertEqual(win.keyboard_target,mac.keyboard_target)
        revision=win._keyboard_revision
        target=win.keyboard_target
        win._network({"type":"keyboard_focus","screen":"mac:main","focus_revision":[0,"mac"]})
        self.assertEqual((win.keyboard_target,win._keyboard_revision),(target,revision))

    def test_cross_platform_shortcut_profiles_are_explicit(self):
        win,mac=self.pair()
        cases=[
            (win,"ctrl_l","c","c",{"meta_l"}),
            (win,"ctrl_l shift_l","z","z",{"meta_l","shift_l"}),
            (win,"ctrl_l","y","z",{"meta_l","shift_l"}),
            (win,"ctrl_l","left","left",{"alt_l"}),
            (win,"ctrl_l","backspace","backspace",{"alt_l"}),
            (win,"ctrl_l","home","up",{"meta_l"}),
            (win,"","end","right",{"meta_l"}),
            (win,"alt_l","tab","tab",{"meta_l"}),
            (win,"alt_l","f4","w",{"meta_l"}),
            (win,"alt_l","left","bracket_left",{"meta_l"}),
            (win,"","f5","r",{"meta_l"}),
            (win,"shift_l","insert","v",{"meta_l"}),
            (win,"ctrl_l alt_l","delete","delete",{"ctrl_l","alt_l"}),
            (win,"ctrl_l","e","e",{"ctrl_l"}),
            (mac,"meta_l","v","v",{"ctrl_l"}),
            (mac,"meta_l shift_l","z","y",{"ctrl_l"}),
            (mac,"meta_l","tab","tab",{"alt_l"}),
            (mac,"meta_l","q","f4",{"alt_l"}),
            (mac,"meta_l","left","home",set()),
            (mac,"meta_l","up","home",{"ctrl_l"}),
            (mac,"alt_l","delete","delete",{"ctrl_l"}),
            (mac,"meta_l shift_l","bracket_left","tab",{"ctrl_l","shift_l"}),
            (mac,"meta_l shift_l","bracket_right","tab",{"ctrl_l"}),
        ]
        for engine,held,key,mapped,modifiers in cases:
            with self.subTest(source=engine.machine["id"],held=held,key=key):
                engine.held_keys=set(held.split())
                target=next(s for s in engine.desktop.screens if s.machine!=engine.machine["id"])
                self.assertEqual(engine._shortcut_spec(key,target),(mapped,modifiers))

    def test_switch_app_modifier_stays_held_between_tab_presses(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        self.key(win,"alt_l")
        self.key(win,"tab")
        self.key(win,"tab",False)
        self.pump(win,mac)
        self.assertEqual(mac.backend.keys,{"meta_l"})
        self.key(win,"alt_l",False)
        self.pump(win,mac)
        self.assertFalse(mac.backend.keys)

    def test_browser_navigation_uses_real_backend_wire_names(self):
        from crossdesk.backends.macos import KEY_CODES
        from crossdesk.backends.windows import _KEY_TO_VK
        win,mac=self.pair()
        win.peer_supported=set(KEY_CODES)-{"fn"}
        mac.peer_supported=set(_KEY_TO_VK)
        self.move(win,80)
        self.pump(win,mac)
        self.key(win,"alt_l")
        self.key(win,"left")
        self.pump(win,mac)
        self.assertEqual(mac.backend.keys,{"meta_l","bracket_left"})
        self.key(win,"left",False)
        self.key(win,"alt_l",False)
        self.move(mac,1)
        self.pump(win,mac)
        self.move(mac,-200)
        self.pump(win,mac)
        self.key(mac,"meta_l")
        self.key(mac,"bracket_right")
        self.pump(win,mac)
        self.assertEqual(win.backend.keys,{"alt_l","right"})

    def test_all_transformed_shortcuts_use_supported_receiver_key_names(self):
        from itertools import combinations
        from crossdesk.backends.macos import KEY_CODES
        from crossdesk.backends.windows import _KEY_TO_VK
        win,mac=self.pair()
        for source,source_names,destination_names in (
                (win,set(_KEY_TO_VK),set(KEY_CODES)-{"fn"}),
                (mac,set(KEY_CODES)-{"fn"},set(_KEY_TO_VK))):
            target=next(s for s in source.desktop.screens if s.machine!=source.machine["id"])
            for count in range(5):
                for selected in combinations(("ctrl_l","shift_l","alt_l","meta_l"),count):
                    source.held_keys=set(selected)
                    for key in source_names:
                        mapped,modifiers=source._shortcut_spec(key,target)
                        if mapped!=key or modifiers!=set(selected):
                            with self.subTest(platform=source.machine["platform"],key=key,held=selected):
                                self.assertIn(mapped,destination_names)
                                self.assertTrue(modifiers<=destination_names)

    def test_browser_tab_zoom_and_format_commands_translate_both_directions(self):
        win,mac=self.pair()
        for source,prefix,target_prefix in ((win,"ctrl_","meta_"),(mac,"meta_","ctrl_")):
            target=next(s for s in source.desktop.screens if s.machine!=source.machine["id"])
            source.held_keys={prefix+"r"}
            for key in "0123456789biud":
                with self.subTest(source=source.machine["id"],key=key):
                    self.assertEqual(source._shortcut_spec(key,target),(key,{target_prefix+"r"}))
            for key in ("minus","equal"):
                self.assertEqual(source._shortcut_spec(key,target),(key,{target_prefix+"r"}))

    def test_key_up_uses_cached_mapped_key_even_if_profile_changes(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        self.key(win,"ctrl_l")
        self.key(win,"y")
        self.pump(win,mac)
        self.assertEqual(mac.backend.keys,{"meta_l","shift_l","z"})
        win.swap_modifiers=False
        self.key(win,"y",False)
        self.key(win,"ctrl_l",False)
        self.pump(win,mac)
        self.assertFalse(mac.backend.keys)

    def test_async_paste_waits_then_sends_complete_chord_after_modifier_up(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        pending=[]
        win.before_paste=lambda target,done:pending.append((target,done))
        self.key(win,"ctrl_l")
        self.key(win,"v")
        self.key(win,"v",False)
        self.key(win,"ctrl_l",False)
        self.key(win,"b")
        self.key(win,"b",False)
        self.pump(win,mac)
        self.assertFalse(mac.backend.keys)
        self.assertEqual(pending[0][0],"mac")
        self.assertFalse(any(row.get("key")=="v" for row in mac.backend.injected))
        pending[0][1](True)
        kind,event=win.events.get_nowait()
        self.assertEqual(kind,"paste_ready")
        win._finish_paste(event["token"],event["ok"])
        self.pump(win,mac)
        sequence=[(row["key"],row["down"]) for row in mac.backend.injected if row["type"]=="key"]
        self.assertEqual(sequence[-4:],[("meta_l",True),("v",True),("v",False),("meta_l",False)])

    def test_local_incoming_paste_is_deferred_without_pointer_reclaim(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        pending=[]
        mac.before_paste=lambda target,done:pending.append((target,done))
        self.key(mac,"meta_l")
        self.key(mac,"v")
        self.key(mac,"meta_l",False)
        pending[0][1](True)
        _,event=mac.events.get_nowait()
        mac._finish_paste(event["token"],True)
        self.assertEqual(pending[0][0],"mac")
        self.assertTrue(mac.incoming)
        self.assertFalse(mac.backend.keys)

    def test_pending_paste_cancels_on_handoff_timeout_and_late_completion(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        pending,cancelled=[],[]
        def hook(target,done):
            pending.append(done)
            return lambda:cancelled.append(True)
        win.before_paste=hook
        self.key(win,"ctrl_l")
        self.key(win,"v")
        self.move(win,-200)
        self.pump(win,mac)
        self.assertEqual(cancelled,[True])
        pending[0](True)
        _,event=win.events.get_nowait()
        win._finish_paste(event["token"],True)
        self.assertFalse(any(row.get("key")=="v" for row in mac.backend.injected))
        self.key(win,"v",False)
        self.key(win,"v")
        for item in win._pending_pastes.values():item["deadline"]=-1
        win._check_paste_timeouts()
        self.assertEqual(cancelled,[True,True])
        self.assertFalse(win._pending_pastes)

    def test_local_focus_gate_preserves_physical_and_peer_key_order(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        ready=[False]
        mac.local_keyboard_ready=lambda:ready[0]
        before=len(mac.backend.injected)
        self.key(mac,"shift_l")
        self.key(win,"c")
        self.pump(win,mac)
        self.key(mac,"shift_l",False)
        self.key(win,"c",False)
        self.pump(win,mac)
        self.assertEqual(len(mac.backend.injected),before)
        self.assertEqual(len(mac._local_key_queue),4)
        self.assertTrue(mac.incoming)
        ready[0]=True
        mac._flush_local_keys()
        self.assertEqual([(row["key"],row["down"]) for row in mac.backend.injected[before:]],
                         [("shift_l",True),("c",True),("shift_l",False),("c",False)])
        self.assertFalse(mac.backend.keys or mac._local_key_queue)

    def test_local_focus_gate_cancels_queued_keys_on_route_change(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        mac.local_keyboard_ready=lambda:False
        self.key(mac,"a")
        self.key(mac,"a",False)
        before=len(mac.backend.injected)
        mac._new_route()
        mac.local_keyboard_ready=lambda:True
        mac._flush_local_keys()
        self.assertEqual(len(mac.backend.injected),before)
        self.assertFalse(mac._local_key_queue or mac.backend.keys)

    def test_local_focus_gate_timeout_releases_already_posted_modifier(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        self.key(mac,"meta_l")
        self.assertIn("meta_l",mac.backend.keys)
        mac.local_keyboard_ready=lambda:False
        self.key(mac,"a")
        keyboard,route,_,event=mac._local_key_queue[0]
        mac._local_key_queue[0]=(keyboard,route,-1,event)
        with self.assertRaises(RuntimeError):mac._flush_local_keys()
        self.assertFalse(mac.backend.keys or mac._local_key_queue)

    def test_local_focus_gate_is_bounded_and_pause_discards_waiting_keys(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        mac.local_keyboard_ready=lambda:False
        for _ in range(512):self.key(mac,"a")
        with self.assertRaises(RuntimeError):self.key(mac,"a")
        mac.set_enabled(False)
        self.assertFalse(mac._local_key_queue or mac.backend.keys)

    def test_keyboard_disabled_native_capture_passes_without_queueing(self):
        win,mac=self.pair()
        self.move(win,80)
        self.pump(win,mac)
        mac.keyboard=False
        self.assertFalse(mac.capture({"type":"key","key":"a","down":True}))
        self.assertTrue(mac.events.empty())
        self.assertTrue(mac.incoming)

    def test_remote_input_requires_focus_and_enable_on_both_computers(self):
        win, mac = self.pair()
        message = {"type": "input", "event": {"type": "key", "key": "a", "down": True}}
        mac._network(message)
        self.assertFalse(mac.backend.keys)
        mac._network({"type": "pointer", "screen": "mac:main", "u": .2, "v": .3})
        self.assertEqual(mac.backend.pointer, (50, 50))
        mac.peer_active = False
        mac._network({"type": "focus", "screen": "mac:main", "u": .2, "v": .3})
        self.assertFalse(mac.incoming)
        self.assertFalse(mac.backend.keys)

    def test_fractional_scroll_and_named_punctuation_survive_routing(self):
        win, mac = self.pair()
        self.move(win,80)
        self.pump(win, mac)
        scroll = {"type": "scroll", "dx": -.25, "dy": .125}
        win._local(scroll)
        win._local({"type": "key", "key": "semicolon", "down": True})
        self.pump(win, mac)
        self.assertIn(scroll, mac.backend.injected)
        self.assertIn("semicolon", mac.backend.keys)

    def test_pointer_takeover_on_same_computer_preserves_keyboard_destination(self):
        win, mac = self.pair()
        self.move(win,80)
        win._local({"type": "key", "key": "a", "down": True})
        self.pump(win, mac)
        self.assertIn("a", mac.backend.keys)
        self.move(mac,1)
        self.pump(win, mac)
        self.assertFalse(mac.incoming)
        self.assertIn("a",mac.backend.keys)
        self.assertTrue(any(message["type"] == "reclaim" for message in mac.transport.sent))
        self.assertIn("a",win.held_keys)
        self.assertEqual((win.keyboard_target,mac.keyboard_target),("mac","mac"))
        win._local({"type":"key","key":"a","down":False})
        self.pump(win,mac)
        self.assertFalse(mac.backend.keys)

    def test_disconnect_releases_keys_and_stops_capture(self):
        win, mac = self.pair()
        self.move(win,80)
        win._local({"type": "key", "key": "a", "down": True})
        self.pump(win, mac)
        mac._network({"type": "disconnect"})
        self.assertFalse(mac.enabled)
        self.assertFalse(mac.incoming)
        self.assertFalse(mac.peer_ready)
        self.assertFalse(mac.backend.running)
        self.assertFalse(mac.backend.keys)
        self.assertFalse(mac.capture({"type": "move", "dx": 4, "dy": 0}))

    def test_peer_pause_releases_remote_injected_keys(self):
        win, mac = self.pair()
        self.move(win,80)
        win._local({"type": "key", "key": "a", "down": True})
        self.pump(win, mac)
        win.set_enabled(False)
        self.pump(win, mac)
        self.assertFalse(mac.backend.keys)
        self.assertFalse(mac.incoming)
        self.assertFalse(mac.peer_active)
        self.assertFalse(win.enabled or mac.enabled)
        self.assertFalse(mac.capture({"type": "move", "dx": 1, "dy": 0}))

    def test_f12_worker_panic_restores_local_input(self):
        engine = self.engine(worker=True)
        remote = {"machine": {"id": "mac", "name": "Mac", "platform": "darwin"},
                  "screens": [{"id": "mac:main", "machine": "mac", "name": "main",
                               "native_x": 0, "native_y": 0, "native_w": 100, "native_h": 100,
                               "x": 100, "y": 0, "w": 100, "h": 100}],
                  "type": "hello", "enabled": True}
        with engine.lock:
            engine._network(remote)
            engine.set_enabled(True)
        self.assertTrue(engine.capture({"type": "key", "key": "f12", "down": True}))
        wait_for(lambda: not engine.enabled and not engine.backend.running)
        self.assertGreater(engine.backend.releases, 0)
        self.assertFalse(engine.capture({"type": "move", "dx": 1, "dy": 0}))

    def test_capture_queue_overflow_fails_open_immediately(self):
        win, mac = self.pair()
        win.events = queue.Queue(maxsize=1)
        win.events.put(("local", {"type": "move"}))
        self.assertFalse(win.capture({"type": "move", "x":51,"y":50,"dx": 1, "dy": 0}))
        self.assertFalse(win.enabled)
        self.assertTrue(win.emergency.is_set())

    def test_close_serializes_with_inflight_key_injection(self):
        engine = self.engine(worker=True)
        with engine.lock:
            engine.peer_ready = engine.peer_active = True
            engine.set_enabled(True, propagate=False)
        injecting, proceed, closed = threading.Event(), threading.Event(), threading.Event()
        original_inject = engine.backend.inject

        def delayed_inject(event):
            injecting.set()
            if not proceed.wait(2):
                raise RuntimeError("test injection barrier timed out")
            original_inject(event)

        engine.backend.inject = delayed_inject
        engine.events.put(("local", {"type": "key", "key": "a", "down": True}))
        self.assertTrue(injecting.wait(1))

        def close_engine():
            engine.close()
            closed.set()

        closer = threading.Thread(target=close_engine)
        closer.start()
        try:
            # close must await the in-flight route before its final release_all.
            self.assertFalse(closed.wait(.05))
        finally:
            proceed.set()
            closer.join(timeout=2)
        self.assertTrue(closed.is_set())
        self.assertFalse(engine.backend.keys)

    def test_layout_changes_sync_both_directions_and_old_revisions_are_ignored(self):
        win, mac = self.pair(enable=False)
        moved = [replace(s, y=100 if s.machine == "mac" else 0,
                         x=0) for s in win.desktop.screens]
        win.update_layout(moved)
        self.pump(win, mac)
        self.assertEqual(win.revision, mac.revision)
        self.assertEqual({s.id: (s.x, s.y) for s in win.desktop.screens},
                         {s.id: (s.x, s.y) for s in mac.desktop.screens})
        old_revision = list(win.revision)
        moved_again = [replace(s, y=-100 if s.machine == "mac" else 0)
                       for s in mac.desktop.screens]
        mac.update_layout(moved_again)
        self.pump(win, mac)
        self.assertEqual(win.revision, mac.revision)
        self.assertEqual(win.desktop.by_id["mac:main"].y, -100)
        win._network({"type": "layout", "revision": old_revision, "screens": []})
        self.assertEqual(win.desktop.by_id["mac:main"].y, -100)

    def test_layout_cannot_change_native_coordinates_and_cannot_overlap(self):
        win, mac = self.pair(enable=False)
        screens = win.desktop.screens
        with self.assertRaises(ValueError):
            win.update_layout([replace(s, native_x=1000) if s.machine == "mac" else s
                               for s in screens])
        with self.assertRaises(ValueError):
            win.update_layout([replace(s, x=0, y=0) for s in screens])
        win.set_enabled(True)
        with self.assertRaises(RuntimeError):
            win.update_layout(screens)

    def test_invalid_remote_coordinates_and_input_are_rejected(self):
        win, mac = self.pair()
        for focus in ({"screen": "win:main", "u": .5, "v": .5},
                      {"screen": "mac:main", "u": float("nan"), "v": .5},
                      {"screen": "mac:main", "u": 1.1, "v": .5}):
            with self.subTest(focus=focus), self.assertRaises(ValueError):
                mac._network({"type": "focus", **focus})
        mac.incoming = True
        for event in ({"type": "position", "x": 10, "y": 10},
                      {"type": "key", "key": "a", "down": "yes"},
                      {"type": "button", "button": "unknown", "down": True},
                      {"type": "scroll", "dx": 0, "dy": 101}):
            with self.subTest(event=event), self.assertRaises(ValueError):
                mac._network({"type": "input", "event": event})

    def test_preview_does_not_authorize_capture_and_keyboard_sharing_can_be_disabled(self):
        win, mac = self.pair(enable=False)
        fresh = self.engine("fresh")
        fresh.add_discovered(win.describe())
        self.assertFalse(fresh.peer_ready)
        with self.assertRaises(RuntimeError):
            fresh.set_enabled(True)
        win.set_enabled(True)
        self.pump(win, mac)
        win.keyboard = False
        self.assertFalse(win.capture({"type": "key", "key": "a", "down": True}))
        self.assertFalse(win.capture({"type": "move", "x":51,"y":50,"dx": 1, "dy": 0}))

    def test_one_start_activates_both_and_either_pause_stops_both(self):
        for source in ("win", "mac"):
            with self.subTest(source=source):
                win, mac = self.pair(enable=False)
                starter = win if source == "win" else mac
                starter.set_enabled(True)
                self.assertTrue(starter.control_pending)
                self.assertFalse(starter.capture({"type": "move", "dx": 1, "dy": 0}))
                self.pump(win, mac)
                self.assertTrue(win.enabled and mac.enabled)
                self.assertEqual((win.backend.starts, mac.backend.starts), (1, 1))
                other = mac if source == "win" else win
                other.set_enabled(False)
                self.pump(win, mac)
                self.assertFalse(win.enabled or mac.enabled)
                self.assertFalse(win.control_pending or mac.control_pending)

    def test_remote_permissions_failure_rolls_back_with_named_mac_help(self):
        win, mac = self.pair(enable=False)
        mac.backend.start_error = PermissionError("sensitive internal detail")
        win.set_enabled(True)
        self.pump(win, mac)
        self.assertFalse(win.enabled or mac.enabled)
        self.assertFalse(win.backend.running or mac.backend.running)
        self.assertFalse(win.control_pending)
        status = [value for kind, value in win.test_notifications if kind == "status"]
        self.assertTrue(any("mac" in value and "辅助功能" in value and "输入监控" in value
                            for value in status))
        self.assertFalse(any("sensitive internal detail" in value for value in status))

    def test_late_start_ack_and_old_control_cannot_undo_new_pause(self):
        win, mac = self.pair(enable=False)
        win.set_enabled(True)
        start = next(m for m in win.transport.messages if m["type"] == "control")
        win.set_enabled(False)
        pause = win._pending_control["request"]
        win._network({"type": "control_result", "request": start["request"],
                      "enabled": True, "ok": True, "error": ""})
        self.assertFalse(win.enabled)
        self.assertEqual(win._pending_control["request"], pause)
        self.pump(win, mac)
        self.assertFalse(win.enabled or mac.enabled)
        mac._network({**start, "request": uuid.uuid4().hex})
        self.assertFalse(mac.enabled)

    def test_duplicate_and_simultaneous_start_do_not_restart_backend(self):
        win, mac = self.pair(enable=False)
        win.set_enabled(True)
        first = next(m for m in win.transport.messages if m["type"] == "control")
        win.set_enabled(True)
        mac.set_enabled(True)
        self.pump(win, mac)
        mac._network(first)
        self.pump(win, mac)
        self.assertTrue(win.enabled and mac.enabled)
        self.assertEqual((win.backend.starts, mac.backend.starts), (1, 1))

    def test_start_timeout_rolls_back_and_newer_pause_cancels_delayed_start(self):
        win, mac = self.pair(enable=False)
        with patch("crossdesk.engine.CONTROL_TIMEOUT", .01):
            win.set_enabled(True)
        win._pending_control["deadline"] = time.monotonic()-1
        win._check_control_timeout()
        self.assertFalse(win.enabled or win.control_pending)
        self.assertTrue(any(kind == "status" and "没有及时确认" in value
                            for kind, value in win.test_notifications))
        self.pump(win, mac)
        self.assertFalse(win.enabled or mac.enabled)

    def test_reconnection_does_not_enable_a_paused_session(self):
        win, mac = self.pair()
        win._network({"type": "disconnect"})
        mac._network({"type": "disconnect"})
        win.transport.messages.clear()
        mac.transport.messages.clear()
        win._hello()
        mac._hello()
        self.pump(win, mac)
        self.assertTrue(win.peer_ready and mac.peer_ready)
        self.assertFalse(win.enabled or mac.enabled)
        self.assertFalse(win.backend.running or mac.backend.running)

    def test_peer_ready_without_control_ack_keeps_input_local(self):
        win, mac = self.pair(enable=False)
        win.set_enabled(True)
        while win.transport.messages:
            mac._network(win.transport.messages.pop(0))
        response = next(row for row in mac.transport.messages if row["type"] == "control_result")
        for message in mac.transport.messages:
            if message["type"] != "control_result":
                win._network(message)
        self.assertTrue(win.peer_active)
        self.assertTrue(win.control_pending)
        self.assertFalse(win.capture({"type": "move", "dx": 1, "dy": 0}))
        win.backend.pointer = (20, 30)
        win._network(response)
        self.assertFalse(win.control_pending)
        self.assertEqual(win.cursor, (20, 30))
        self.assertFalse(win.capture({"type": "move", "x":21,"y":30,"dx": 1, "dy": 0}))

    def test_concurrent_pause_takes_precedence_over_start(self):
        win, mac = self.pair(enable=False)
        win.set_enabled(True)
        mac.set_enabled(False)
        self.pump(win, mac)
        self.assertFalse(win.enabled or mac.enabled)
        self.assertFalse(win.control_pending or mac.control_pending)
        self.assertFalse(win.backend.running or mac.backend.running)

    def test_remote_capture_api_failure_reports_only_safe_device_error(self):
        win, mac = self.pair(enable=False)
        mac.backend.start_error = OSError("private API internals")
        win.set_enabled(True)
        self.pump(win, mac)
        statuses = [value for kind, value in win.test_notifications if kind == "status"]
        self.assertFalse(win.enabled or mac.enabled)
        self.assertTrue(any("mac" in value and "系统输入接口不可用" in value for value in statuses))
        self.assertFalse(any("private API internals" in value for value in statuses))


if __name__ == "__main__":
    unittest.main()
