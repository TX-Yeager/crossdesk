import ctypes as C,sys,unittest
from unittest.mock import Mock
from crossdesk.backends.windows_cursor import WindowsCursor,WindowClass,CursorInfo,_WindowsCursorAPI,_INPUT

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
    def test_null_cursor_image_is_hidden_even_when_visibility_counter_is_showing(self):
        api=_WindowsCursorAPI.__new__(_WindowsCursorAPI)
        api.u=Mock()
        for flags,handle,expected in ((1,0,True),(1,123,False),(0,123,True),(2,123,True)):
            def state(pointer):
                info=C.cast(pointer,C.POINTER(CursorInfo)).contents
                info.flags,info.hCursor=flags,handle
                return True
            api.u.GetCursorInfo.side_effect=state
            self.assertEqual(api.hidden(),expected)
    def test_fast_recross_hides_even_during_previous_return_guard(self):
        self.cursor.update(True);self.cursor.ensure_visible()
        self.cursor.ensure_hidden()
        self.assertTrue(self.cursor.hidden)
    def test_transient_hide_failure_recovers_without_returning_to_local(self):
        self.native.show=lambda:self.native.events.append('failed_hide')
        self.cursor.update(True)
        self.now=.6;self.cursor.update(True)
        self.assertTrue(self.cursor.warning)
        count=len(self.native.events)
        self.now=1.5;self.cursor.update(True)
        self.assertEqual(len(self.native.events),count)
        self.native.show=lambda:setattr(self.native,'visible',False)
        self.now=1.7;self.cursor.update(True)
        self.assertTrue(self.cursor.hidden)
        self.assertIsNone(self.cursor.warning)
    def test_new_departure_retries_immediately_after_degradation(self):
        self.native.show=Mock(side_effect=OSError('temporary failure'))
        self.cursor.ensure_hidden()
        self.assertTrue(self.cursor.warning)
        self.native.show=lambda:setattr(self.native,'visible',False)
        self.cursor.ensure_hidden()
        self.assertTrue(self.cursor.hidden)
        self.assertIsNone(self.cursor.warning)
    def test_native_window_owns_only_one_balanced_visibility_decrement(self):
        api=_WindowsCursorAPI.__new__(_WindowsCursorAPI)
        api.u=Mock();api.window=1;api._visibility_owned=False
        api.u.SetThreadDpiAwarenessContext.return_value=None
        api.u.GetSystemMetrics.return_value=100
        api.u.SendInput.return_value=1
        api._show_now();api._show_now()
        api.u.ShowCursor.assert_called_once_with(False)
        api._hide_now();api._hide_now()
        self.assertEqual([call.args for call in api.u.ShowCursor.call_args_list],[(False,),(True,)])
    def test_refresh_is_only_zero_relative_mouse_motion(self):
        api=_WindowsCursorAPI.__new__(_WindowsCursorAPI)
        api.u=Mock();api.window=1;api._visibility_owned=False
        api.u.SetThreadDpiAwarenessContext.return_value=None
        api.u.GetSystemMetrics.return_value=100
        values=[]
        def send(count,address,size):
            event=C.cast(address,C.POINTER(_INPUT)).contents
            values.append((count,size,event.type,event.mi.dx,event.mi.dy,event.mi.mouseData,event.mi.dwFlags))
            return 1
        api.u.SendInput.side_effect=send
        api._show_now()
        self.assertEqual(values,[(1,C.sizeof(_INPUT),0,0,0,0,1)])
        api.u.SetCursorPos.assert_not_called()
        api.u.SetPhysicalCursorPos.assert_not_called()
        api.u.SendInput.reset_mock()
        self.assertEqual(api._window_message(1,0x20,0,0),1)
        api.u.SendInput.assert_not_called() # No WM_SETCURSOR feedback loop.
    @unittest.skipUnless(sys.platform=='win32','Native Windows ABI')
    def test_64bit_cursor_window_abi(self):
        if C.sizeof(C.c_void_p)==8:
            self.assertEqual(C.sizeof(WindowClass),80)
            self.assertEqual(C.sizeof(CursorInfo),24)
