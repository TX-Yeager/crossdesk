"""Hide the inactive desktop cursor inside an owned, nonactivating window.

No system cursor theme, foreign visibility counter or foreground focus changes.
The inactive desktop stays covered while the pointer is parked or repositioned.
Restore it only after a returning pointer has reached its destination.
"""
import ctypes as C
from ctypes import wintypes as W
import os
import sys
import threading
import time

PROC=getattr(C,'WINFUNCTYPE',C.CFUNCTYPE)(C.c_ssize_t,W.HWND,W.UINT,W.WPARAM,W.LPARAM)
HIDE_MESSAGE=0x8000+211
SHOW_MESSAGE=HIDE_MESSAGE+1
CLOSE_MESSAGE=SHOW_MESSAGE+1

class WindowClass(C.Structure):
    _fields_=[('cbSize',W.UINT),('style',W.UINT),('lpfnWndProc',PROC),('cbClsExtra',C.c_int),
              ('cbWndExtra',C.c_int),('hInstance',W.HINSTANCE),('hIcon',W.HANDLE),
              ('hCursor',W.HANDLE),('hbrBackground',W.HANDLE),('lpszMenuName',W.LPCWSTR),
              ('lpszClassName',W.LPCWSTR),('hIconSm',W.HANDLE)]

class CursorInfo(C.Structure):
    _fields_=[('cbSize',W.DWORD),('flags',W.DWORD),('hCursor',W.HANDLE),('ptScreenPos',W.POINT)]

class _WindowsCursorAPI:
    def __init__(self):
        if sys.platform!='win32':raise OSError('Windows cursor requires Windows')
        self.u=C.WinDLL('user32',use_last_error=True)
        self.k=C.WinDLL('kernel32',use_last_error=True)
        for dll,name,args,result in (
            (self.k,'GetModuleHandleW',[W.LPCWSTR],W.HMODULE),
            (self.k,'GetCurrentThreadId',[],W.DWORD),
            (self.u,'RegisterClassExW',[C.POINTER(WindowClass)],W.WORD),
            (self.u,'UnregisterClassW',[W.LPCWSTR,W.HINSTANCE],W.BOOL),
            (self.u,'CreateWindowExW',[W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,C.c_int,C.c_int,C.c_int,C.c_int,W.HWND,W.HMENU,W.HINSTANCE,C.c_void_p],W.HWND),
            (self.u,'DefWindowProcW',[W.HWND,W.UINT,W.WPARAM,W.LPARAM],C.c_ssize_t),
            (self.u,'DestroyWindow',[W.HWND],W.BOOL),
            (self.u,'GetMessageW',[C.POINTER(W.MSG),W.HWND,W.UINT,W.UINT],C.c_int),
            (self.u,'TranslateMessage',[C.POINTER(W.MSG)],W.BOOL),
            (self.u,'DispatchMessageW',[C.POINTER(W.MSG)],C.c_ssize_t),
            (self.u,'PostQuitMessage',[C.c_int],None),
            (self.u,'SetLayeredWindowAttributes',[W.HWND,W.DWORD,W.BYTE,W.DWORD],W.BOOL),
            (self.u,'SetWindowPos',[W.HWND,W.HWND,C.c_int,C.c_int,C.c_int,C.c_int,W.UINT],W.BOOL),
            (self.u,'ShowWindow',[W.HWND,C.c_int],W.BOOL),
            (self.u,'GetPhysicalCursorPos',[C.POINTER(W.POINT)],W.BOOL),
            (self.u,'GetSystemMetrics',[C.c_int],C.c_int),
            (self.u,'SetThreadDpiAwarenessContext',[C.c_void_p],C.c_void_p),
            (self.u,'GetCursorInfo',[C.POINTER(CursorInfo)],W.BOOL),
            (self.u,'SetCursor',[W.HANDLE],W.HANDLE),
            (self.u,'LoadCursorW',[W.HINSTANCE,C.c_void_p],W.HANDLE),
            (self.u,'SendMessageTimeoutW',[W.HWND,W.UINT,W.WPARAM,W.LPARAM,W.UINT,W.UINT,C.POINTER(C.c_size_t)],C.c_ssize_t)):
            fn=getattr(dll,name);fn.argtypes=args;fn.restype=result
        self.owner=self.k.GetCurrentThreadId()
        self.instance=self.k.GetModuleHandleW(None)
        self.name=f'CrossDesk.Cursor.{os.getpid()}.{id(self)}'
        self.callback=PROC(self._window_message)
        cls=WindowClass(cbSize=C.sizeof(WindowClass),lpfnWndProc=self.callback,
                        hInstance=self.instance,lpszClassName=self.name)
        if not self.u.RegisterClassExW(C.byref(cls)):raise C.WinError(C.get_last_error())
        self.window=self.u.CreateWindowExW(0x08080080,self.name,'',0x80000000,0,0,24,24,
                                           None,None,self.instance,None)
        if not self.window:
            self.u.UnregisterClassW(self.name,self.instance)
            raise C.WinError(C.get_last_error())
        self.u.SetLayeredWindowAttributes(self.window,0,1,2)

    def _window_message(self,window,message,wparam,lparam):
        if message==0x20: # WM_SETCURSOR, only our owned window.
            self.u.SetCursor(None)
            return 1
        if message==0x21:return 3 # WM_MOUSEACTIVATE: MA_NOACTIVATE
        if message==HIDE_MESSAGE:
            self._hide_now()
            return 1
        if message==SHOW_MESSAGE:
            try:self._show_now()
            except OSError:return 0
            return 1
        if message==CLOSE_MESSAGE:
            self._hide_now()
            self.u.DestroyWindow(self.window)
            self.u.PostQuitMessage(0)
            return 1
        return self.u.DefWindowProcW(window,message,wparam,lparam)

    def show(self):
        if self.k.GetCurrentThreadId()==self.owner:
            self._show_now()
        else:
            result=C.c_size_t()
            if (not self.u.SendMessageTimeoutW(self.window,SHOW_MESSAGE,0,0,3,150,C.byref(result))
                    or not result.value):
                raise OSError('Cursor owner did not hide the outgoing pointer in time')

    def _show_now(self):
        previous=self.u.SetThreadDpiAwarenessContext(C.c_void_p(-4))
        try:
            # Cover both the old point and the parking/return point, so moving
            # outside a tiny cursor window cannot reveal the parked cursor.
            x,y,w,h=(self.u.GetSystemMetrics(i) for i in (76,77,78,79))
            if not self.u.SetWindowPos(self.window,C.c_void_p(-1),x,y,w,h,0x10|0x40):
                raise C.WinError(C.get_last_error())
            self.u.SetCursor(None)
        finally:
            if previous:self.u.SetThreadDpiAwarenessContext(previous)

    def at_position(self,x,y):
        point=W.POINT()
        if not self.u.GetPhysicalCursorPos(C.byref(point)):raise C.WinError(C.get_last_error())
        return abs(point.x-x)<=1 and abs(point.y-y)<=1

    def hidden(self):
        info=CursorInfo(cbSize=C.sizeof(CursorInfo))
        if not self.u.GetCursorInfo(C.byref(info)):raise C.WinError(C.get_last_error())
        return not bool(info.flags&1)

    def _hide_now(self):
        self.u.ShowWindow(self.window,0)
        self.u.SetCursor(self.u.LoadCursorW(None,C.c_void_p(32512)))

    def hide(self):
        if self.k.GetCurrentThreadId()==self.owner:
            self._hide_now()
        else:
            result=C.c_size_t()
            if not self.u.SendMessageTimeoutW(self.window,HIDE_MESSAGE,0,0,3,150,C.byref(result)):
                raise OSError('Cursor owner did not restore local input in time')

    def close(self):
        if self.k.GetCurrentThreadId()!=self.owner:
            result=C.c_size_t()
            if not self.u.SendMessageTimeoutW(self.window,CLOSE_MESSAGE,0,0,3,500,C.byref(result)):
                raise OSError('Cursor owner did not close in time')
            return
        self.hide()
        if not self.u.DestroyWindow(self.window):raise C.WinError(C.get_last_error())
        self.u.UnregisterClassW(self.name,self.instance)


class _ThreadedCursorAPI:
    """Own the native window on a message thread independent of Tk/clipboard IO."""
    def __init__(self):
        self._ready=threading.Event()
        self._stopping=threading.Event()
        self._error=None
        self._api=None
        self._thread=threading.Thread(target=self._run,name='CrossDesk-cursor',daemon=True)
        self._thread.start()
        if not self._ready.wait(2):
            self._stopping.set()
            raise OSError('Cursor owner startup timed out')
        self._check()

    def _run(self):
        try:
            self._api=_WindowsCursorAPI()
            self._ready.set()
            if self._stopping.is_set():return
            message=W.MSG()
            while True:
                result=self._api.u.GetMessageW(C.byref(message),None,0,0)
                if result==0:break
                if result<0:raise C.WinError(C.get_last_error())
                self._api.u.TranslateMessage(C.byref(message))
                self._api.u.DispatchMessageW(C.byref(message))
        except BaseException as error:
            self._error=error
        finally:
            self._ready.set()
            if self._api is not None:
                self._api._hide_now()
                self._api.u.DestroyWindow(self._api.window)
                self._api.u.UnregisterClassW(self._api.name,self._api.instance)

    def _check(self):
        if self._error is not None:raise OSError('Cursor owner failed') from self._error
        if self._api is None or not self._thread.is_alive():raise OSError('Cursor owner stopped')

    def show(self):self._check();self._api.show()
    def hide(self):self._check();self._api.hide()
    def hidden(self):self._check();return self._api.hidden()
    def at_position(self,x,y):self._check();return self._api.at_position(x,y)
    def close(self):
        if not self._thread.is_alive():return
        self._api.close()
        self._thread.join(.7)
        if self._thread.is_alive():raise OSError('Cursor owner did not stop')


class WindowsCursor:
    local_ready=True
    def __init__(self,root,*,native=None,clock=time.monotonic):
        self._native=native or _ThreadedCursorAPI()
        self._lock=threading.RLock()
        self._clock=clock
        self._active=False
        self._hidden=False
        self._closed=False
        self._deadline=0
        self._local_until=0
        self._retry_at=0
        self.warning=None
        self._degraded=False

    @property
    def hidden(self):return self._hidden
    @property
    def pending(self):return self._active and not self._hidden and not self._degraded

    def ensure_visible(self):
        """Worker-safe; restore before SendInput so the first click is not eaten."""
        with self._lock:self._ensure_visible()

    def _ensure_visible(self):
        self._degraded=False
        self.warning=None
        if self._closed or not self._active:return
        self._local_until=self._clock()+.025
        self._native.hide()
        self._active=self._hidden=False

    def ensure_hidden(self):
        """Request presentation once; visibility must never gate input routing."""
        with self._lock:
            self._local_until=0
            self._update(True)

    def restore_after_position(self,x,y):
        if self._closed or not self._active:return
        # SendInput queues a tagged move. It may return before Windows commits
        # that move; never reveal the old centre position during this interval.
        deadline=time.monotonic()+.15
        while not self._native.at_position(x,y):
            if time.monotonic()>=deadline:
                raise OSError('Returning pointer did not reach its screen edge in time')
            time.sleep(.001)
        self.ensure_visible()

    def update(self,remote):
        # GUI reconciliation must never wait behind a native handoff.
        if not self._lock.acquire(blocking=False):return None
        try:return self._update(remote)
        finally:self._lock.release()

    def _update(self,remote):
        if self._closed:return None
        if not remote:
            self.ensure_visible()
            self._degraded=False
            self.warning=None
            return None
        if self._degraded:return None
        if self._clock()<self._local_until:return None
        try:
            self._update_remote()
        except Exception:
            # Show/visibility can fail without affecting native capture. Remove
            # our input-receiving window before allowing a visible fallback.
            self._degrade()
        return None

    def _degrade(self):
        self.ensure_visible()  # A failed removal still propagates: clicks need it.
        self._degraded=True
        self.warning='源光标隐藏暂不可用，跨屏继续工作；本机可能显示第二个光标。'

    def _update_remote(self):
        if not self._active:
            self._active=True
            self._deadline=self._clock()+.5
            self._retry_at=0
        elif self._native.hidden():
            self._hidden=True
            self._deadline=0
            return None
        elif self._hidden:
            # A fresh visibility loss gets its own recovery budget.
            self._hidden=False
            self._deadline=self._clock()+.5
            self._retry_at=0
        if self._clock()>=self._retry_at:
            self._native.show()
            self._retry_at=self._clock()+.05
        self._hidden=self._native.hidden()
        if self._hidden:self._deadline=0
        if not self._hidden and self._clock()>self._deadline:
            self._degrade()

    def close(self):
        with self._lock:return self._close()

    def _close(self):
        if not self._closed:
            self._native.close()
            self._closed=True
            self._active=self._hidden=False
        return None
