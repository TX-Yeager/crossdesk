import ctypes as C,sys,unittest
from unittest.mock import Mock
from crossdesk.backends.windows_cursor import WindowsCursor,WindowClass,CursorInfo

class FakeCursor:
    def __init__(self):self.visible=True;self.events=[]
    def show(self):self.events.append('hide');self.visible=False
    def hidden(self):return not self.visible
    def hide(self):self.events.append('restore');self.visible=True
    def close(self):self.events.append('close');self.visible=True

class WindowsCursorTests(unittest.TestCase):
    def setUp(self):
        self.native=FakeCursor();self.now=0
        self.cursor=WindowsCursor(object(),native=self.native,clock=lambda:self.now)
    def test_global_inactive_owner_hides_and_local_owner_restores(self):
        self.cursor.update(True);self.assertTrue(self.cursor.hidden)
        self.cursor.update(False);self.assertFalse(self.cursor.hidden)
        self.assertEqual(self.native.events,['hide','restore'])
    def test_first_local_injection_restores_before_any_native_click(self):
        self.cursor.update(True);self.cursor.ensure_visible()
        self.assertFalse(self.cursor.hidden)
        self.assertTrue(self.native.visible)
        self.cursor.update(True)
        self.assertTrue(self.native.visible) # Brief guard for an in-flight route change.
        self.now=.03;self.cursor.update(True);self.assertFalse(self.native.visible)
    def test_close_is_balanced_and_idempotent(self):
        self.cursor.update(True);self.cursor.close();self.cursor.close()
        self.cursor.update(True);self.cursor.ensure_visible()
        self.assertEqual(self.native.events,['hide','close'])
    def test_restore_failure_is_not_silently_ignored_before_click(self):
        self.cursor.update(True);self.native.hide=Mock(side_effect=OSError('test'))
        with self.assertRaises(OSError):self.cursor.ensure_visible()
    def test_return_stays_hidden_until_edge_position_is_committed(self):
        self.cursor.ensure_hidden()
        def committed(x,y):
            self.assertFalse(self.native.visible)
            self.assertEqual((x,y),(0,830))
            return len(self.native.at_position.mock_calls)>1
        self.native.at_position=Mock(side_effect=committed)
        self.cursor.restore_after_position(0,830)
        self.assertEqual(self.native.at_position.call_count,2)
        self.assertTrue(self.native.visible)
    def test_fast_recross_hides_even_during_previous_return_guard(self):
        self.cursor.update(True);self.cursor.ensure_visible()
        self.cursor.ensure_hidden()
        self.assertTrue(self.cursor.hidden)
    @unittest.skipUnless(sys.platform=='win32','Native Windows ABI')
    def test_64bit_cursor_window_abi(self):
        if C.sizeof(C.c_void_p)==8:
            self.assertEqual(C.sizeof(WindowClass),80)
            self.assertEqual(C.sizeof(CursorInfo),24)
