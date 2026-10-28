"""Complete gesture routing, no native input or real trackpad access."""
import unittest
import threading
from crossdesk.gestures import DockGesture
from crossdesk.backends.windows import WindowsBackend
from tests import test_engine

class GestureTests(unittest.TestCase):
    def test_one_command_per_completed_sequence_and_cancel_is_silent(self):
        for axis,action in ((2,'task_view'),(3,'start_menu')):
            with self.subTest(axis=axis):
                state=DockGesture();actions=[]
                def handler(e):
                    if e['action']:actions.append(e['action'])
                    return True
                self.assertTrue(state.consume(axis,1,0,handler))
                self.assertTrue(state.consume(axis,2,-.4,handler))
                self.assertTrue(state.consume(axis,4,0,handler))
                self.assertTrue(state.consume(axis,4,-.4,handler))
                self.assertEqual(actions,[action])
                state.consume(axis,1,0,handler)
                state.consume(axis,2,.6,handler)
                state.consume(axis,8,.6,handler)
                self.assertEqual(actions,[action])

    def test_local_gesture_and_unknown_horizontal_gesture_pass_through(self):
        state=DockGesture()
        self.assertFalse(state.consume(2,1,0,lambda e:False))
        self.assertFalse(state.consume(2,4,.5,lambda e:False))
        self.assertFalse(state.consume(1,1,.5,lambda e:True))

class GestureRoutingTests(unittest.TestCase):
    def test_windows_actions_release_their_own_shortcut_keys(self):
        backend=WindowsBackend.__new__(WindowsBackend)
        backend._input_lock=threading.RLock()
        backend._pressed_keys={'ctrl_r'};backend._tag=123
        sent=[]
        backend._send=lambda item:sent.append((item.ki.wVk,item.ki.dwFlags))
        backend.inject({'type':'gesture','action':'task_view'})
        self.assertEqual([vk for vk,flags in sent],[0x5B,9,9,0x5B])
        self.assertEqual(backend._pressed_keys,{'ctrl_r'})
        sent.clear();backend.inject({'type':'gesture','action':'start_menu'})
        self.assertEqual([vk for vk,flags in sent],[0x5B,0x5B])
        self.assertEqual(backend._pressed_keys,{'ctrl_r'})

    def test_gesture_reaches_windows_without_moving_or_reclaiming_pointer(self):
        fixture=test_engine.EngineTests()
        fixture.setUp();self.addCleanup(fixture.tearDown)
        win,mac=fixture.pair()
        self.assertEqual(mac.pointer_owner,'win')
        before=(win.backend.pointer,mac.backend.pointer)
        self.assertTrue(mac.capture({'type':'gesture','action':None}))
        self.assertTrue(mac.capture({'type':'gesture','action':'task_view'}))
        kind,event=mac.events.get_nowait();self.assertEqual(kind,'local')
        mac._local(event);fixture.pump(win,mac)
        self.assertIn({'type':'gesture','action':'task_view'},win.backend.injected)
        self.assertEqual(before,(win.backend.pointer,mac.backend.pointer))
        self.assertTrue(win.enabled and mac.enabled)
        mac.peer_gestures.clear()
        self.assertFalse(mac.capture({'type':'gesture','action':None}))
