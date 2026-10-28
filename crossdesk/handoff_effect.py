"""A brief, mouse-through light bridge at a cross-computer screen seam.

Only the GUI thread touches presentation. No cursor, activation, input, network
or global appearance APIs are used. Both native surfaces use real pixel alpha;
transparent pixels never become a black rectangle over the user's desktop.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes as C
import math
import sys
import threading
import time

from PIL import Image, ImageChops, ImageFilter


class _Draw:
    """The two RGBA primitives needed here, using the already bundled core.

    The permanent 0.2.x host contains Pillow's image core, Chops and Filter,
    but does not contain the optional ImageDraw Python module. Keep signed
    feature updates compatible with that host instead of adding a dependency.
    All colors here are explicit RGBA tuples; no fonts or color parser needed.
    """
    def __init__(self, image):
        image.load()
        self._image = image
        self._draw = Image.core.draw(image.im, 0)

    def ellipse(self, coordinates, fill):
        self._draw.draw_ellipse(coordinates, self._draw.draw_ink(fill), 1)

    def line(self, coordinates, fill, width=1):
        self._draw.draw_lines(coordinates, self._draw.draw_ink(fill), width)


@dataclass(frozen=True)
class EffectBounds:
    x: int
    y: int
    width: int
    height: int
    edge: str
    anchor: float

    @property
    def canonical_size(self):
        return ((self.height, self.width) if self.edge in ('top', 'bottom')
                else (self.width, self.height))


def effect_bounds(event):
    """Clip a small seam patch to its own monitor, including negative origins."""
    edge = event['edge']
    if edge not in ('left', 'right', 'top', 'bottom'):
        raise ValueError('Unknown seam edge')
    screen = event['screen']
    sx, sy, sw, sh = (float(screen[key]) for key in ('x', 'y', 'width', 'height'))
    px, py = float(event['x']), float(event['y'])
    if (not all(math.isfinite(v) for v in (sx, sy, sw, sh, px, py))
            or not 1 <= sw <= 100000 or not 1 <= sh <= 100000):
        raise ValueError('Invalid effect screen')
    sx, sy, sw, sh = round(sx), round(sy), max(1, round(sw)), max(1, round(sh))
    px, py = min(sx+sw-1, max(sx, px)), min(sy+sh-1, max(sy, py))
    if edge in ('left', 'right'):
        width, height = min(220, sw), min(560, sh)
        x = sx if edge == 'left' else sx+sw-width
        y = round(min(sy+sh-height, max(sy, py-height/2)))
        anchor = py-y
    else:
        width, height = min(560, sw), min(220, sh)
        x = round(min(sx+sw-width, max(sx, px-width/2)))
        y = sy if edge == 'top' else sy+sh-height
        anchor = px-x
    return EffectBounds(x, y, width, height, edge, anchor)


def render_frame(bounds, phase, progress):
    """Draw in half resolution, then smooth up to a bounded native seam patch."""
    progress = max(0., min(1., progress))
    width, height = bounds.canonical_size
    scale = .5
    w, h = max(1, round(width*scale)), max(1, round(height*scale))
    center = bounds.anchor*scale
    empty = Image.new('RGBA', (w, h))
    if progress >= 1.:
        result = empty.resize((width, height))
    else:
        arriving = phase == 'arrive'
        travel = 1-(1-progress)**3 if arriving else (1-progress)**2
        fade = min(1., progress/.07+.18) * (1-progress)**1.15
        aura = Image.new('RGBA', (w, h))
        draw = _Draw(aura)
        # An elongated chromatic lens hugs the seam rather than flashing a
        # full monitor. The white core and trailing rings read as liquid light.
        for offset, color in ((-32, (180, 126, 255)), (30, (66, 218, 255)),
                              (0, (121, 173, 255))):
            ry = (76+57*travel)*scale
            rx = (32+38*travel)*scale
            cy = center+offset*scale
            draw.ellipse((-rx, cy-ry, rx, cy+ry), fill=(*color, round(100*fade)))
        aura = aura.filter(ImageFilter.GaussianBlur(14*scale))
        rings = Image.new('RGBA', (w, h))
        ring = _Draw(rings)
        for index in range(3):
            advance = max(0., travel-index*.17)
            rx = (10+154*advance)*scale
            ry = (25+173*advance)*scale
            alpha = round(fade*(1-index*.23)*155)
            points = []
            for step in range(65):
                angle = -math.pi/2+math.pi*step/64
                ripple = 1+.025*math.sin(angle*6+progress*9+index)
                points.append((-7*scale+rx*math.cos(angle)*ripple,
                               center+ry*math.sin(angle)))
            ring.line(points, fill=(126+index*28, 183-index*8, 255, alpha),
                      width=max(1, round((2.6-index*.5)*scale)))
        haze = rings.filter(ImageFilter.GaussianBlur(4*scale))
        result = Image.alpha_composite(aura, haze)
        result = Image.alpha_composite(result, rings)
        detail = Image.new('RGBA', (w, h))
        draw = _Draw(detail)
        length = (30+126*(math.sin(math.pi*progress)**.7))*scale
        # Fine spectral seam, tapering continuously at both ends.
        for i in range(60):
            fraction = (i+.5)/60
            y = center-length+2*length*fraction
            color = (round(182-91*fraction), round(153+85*fraction), 255)
            alpha = round(235*fade*math.sin(math.pi*fraction)**1.1)
            x = (2+3*math.sin(fraction*math.pi))*scale
            draw.line((x, y, x, y+2*length/60+1), fill=(*color, alpha), width=2)
            draw.line((max(0, x-1), y, max(0, x-1), y+2*length/60+1),
                      fill=(235, 248, 255, round(alpha*.85)), width=1)
        # Stable trajectories: each transfer is one coherent burst, not noise.
        for i in range(30):
            seed = (i*.61803398875) % 1
            extent = 38+147*((i*.41421356) % 1)
            spread = (seed-.5)*330
            flight = travel**(.65+seed*.5)
            x = (4+extent*flight)*scale
            y = center+spread*flight*scale
            radius = (1.0+((i*.732) % 1)*1.4)*scale
            alpha = round(215*fade*(.4+.6*(1-flight)))
            color = (191, 160, 255) if i % 3 == 0 else (147, 233, 255)
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=(*color, alpha))
            tail = (4+6*seed)*scale*(1-progress)
            draw.line((max(0,x-tail),y,x,y), fill=(*color,round(alpha*.32)),width=1)
        result = Image.alpha_composite(result, detail)
        result = result.resize((width, height), Image.Resampling.BILINEAR)
    if bounds.edge == 'right':
        result = result.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    elif bounds.edge == 'top':
        result = result.transpose(Image.Transpose.TRANSPOSE)
    elif bounds.edge == 'bottom':
        result = result.transpose(Image.Transpose.ROTATE_90)
    return result


def _premultiplied(frame):
    red, green, blue, alpha = frame.split()
    return Image.merge('RGBA', (ImageChops.multiply(red, alpha),
                               ImageChops.multiply(green, alpha),
                               ImageChops.multiply(blue, alpha), alpha))


def _premultiplied_bgra(frame):
    return _premultiplied(frame).tobytes('raw', 'BGRA')


class _WindowsSurface:
    """Owned layered window: transparent, nonactivating, absent from Alt-Tab."""
    def __init__(self):
        from ctypes import wintypes as W
        from crossdesk.backends.windows_cursor import PROC, WindowClass
        self.u = C.WinDLL('user32', use_last_error=True)
        self.g = C.WinDLL('gdi32', use_last_error=True)
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.callback = PROC(self._message)
        self.window = self.dc = self.bitmap = self.old_bitmap = None
        self.size = None
        class BitmapHeader(C.Structure):
            _fields_ = [('size', W.DWORD), ('width', C.c_long), ('height', C.c_long),
                        ('planes', W.WORD), ('bits', W.WORD), ('compression', W.DWORD),
                        ('image_size', W.DWORD), ('xppm', C.c_long), ('yppm', C.c_long),
                        ('used', W.DWORD), ('important', W.DWORD)]
        class Blend(C.Structure):
            _fields_ = [('operation', W.BYTE), ('flags', W.BYTE),
                        ('alpha', W.BYTE), ('format', W.BYTE)]
        self.W, self.Header, self.Blend = W, BitmapHeader, Blend
        for dll, name, args, result in (
            (self.k,'GetModuleHandleW',[W.LPCWSTR],W.HMODULE),
            (self.u,'RegisterClassExW',[C.POINTER(WindowClass)],W.WORD),
            (self.u,'UnregisterClassW',[W.LPCWSTR,W.HINSTANCE],W.BOOL),
            (self.u,'CreateWindowExW',[W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,C.c_int,C.c_int,C.c_int,C.c_int,W.HWND,W.HMENU,W.HINSTANCE,C.c_void_p],W.HWND),
            (self.u,'DefWindowProcW',[W.HWND,W.UINT,W.WPARAM,W.LPARAM],C.c_ssize_t),
            (self.u,'DestroyWindow',[W.HWND],W.BOOL),
            (self.u,'ShowWindow',[W.HWND,C.c_int],W.BOOL),
            (self.u,'SetWindowPos',[W.HWND,W.HWND,C.c_int,C.c_int,C.c_int,C.c_int,W.UINT],W.BOOL),
            (self.u,'SetThreadDpiAwarenessContext',[C.c_void_p],C.c_void_p),
            (self.u,'UpdateLayeredWindow',[W.HWND,W.HDC,C.POINTER(W.POINT),C.POINTER(W.SIZE),W.HDC,C.POINTER(W.POINT),W.DWORD,C.POINTER(Blend),W.DWORD],W.BOOL),
            (self.g,'CreateCompatibleDC',[W.HDC],W.HDC),
            (self.g,'CreateDIBSection',[W.HDC,C.POINTER(BitmapHeader),W.UINT,C.POINTER(C.c_void_p),W.HANDLE,W.DWORD],W.HBITMAP),
            (self.g,'SelectObject',[W.HDC,W.HANDLE],W.HANDLE),
            (self.g,'DeleteObject',[W.HANDLE],W.BOOL),
            (self.g,'DeleteDC',[W.HDC],W.BOOL)):
            fn = getattr(dll,name); fn.argtypes = args; fn.restype = result
        self.name = f'CrossDesk.LightBridge.{id(self)}'
        self.instance = self.k.GetModuleHandleW(None)
        cls = WindowClass(cbSize=C.sizeof(WindowClass), lpfnWndProc=self.callback,
                          hInstance=self.instance, lpszClassName=self.name)
        if not self.u.RegisterClassExW(C.byref(cls)):
            raise C.WinError(C.get_last_error())
        try:
            self.window = self.u.CreateWindowExW(0x080800A0,self.name,'',0x80000000,
                                                0,0,1,1,None,None,self.instance,None)
            if not self.window: raise C.WinError(C.get_last_error())
            self.dc = self.g.CreateCompatibleDC(None)
            if not self.dc: raise C.WinError(C.get_last_error())
        except Exception:
            self.close()
            raise

    def _message(self, window, message, wparam, lparam):
        if message == 0x84: return -1  # HTTRANSPARENT
        if message == 0x21: return 3   # MA_NOACTIVATE
        return self.u.DefWindowProcW(window,message,wparam,lparam)

    def show(self, frame, bounds):
        w, h = frame.size
        if self.size != frame.size:
            if self.bitmap:
                self.g.SelectObject(self.dc,self.old_bitmap)
                self.g.DeleteObject(self.bitmap)
                self.bitmap = None
            header = self.Header(size=C.sizeof(self.Header),width=w,height=-h,
                                 planes=1,bits=32,compression=0)
            self.pixels = C.c_void_p()
            self.bitmap = self.g.CreateDIBSection(self.dc,C.byref(header),0,C.byref(self.pixels),None,0)
            if not self.bitmap: raise C.WinError(C.get_last_error())
            self.old_bitmap = self.g.SelectObject(self.dc,self.bitmap)
            self.size = frame.size
        data = _premultiplied_bgra(frame)
        C.memmove(self.pixels,data,len(data))
        destination, source = self.W.POINT(bounds.x,bounds.y), self.W.POINT(0,0)
        size, blend = self.W.SIZE(w,h), self.Blend(0,0,255,1)
        previous = self.u.SetThreadDpiAwarenessContext(C.c_void_p(-4))
        try:
            if not self.u.UpdateLayeredWindow(self.window,None,C.byref(destination),C.byref(size),
                                             self.dc,C.byref(source),0,C.byref(blend),2):
                raise C.WinError(C.get_last_error())
            if not self.u.SetWindowPos(self.window,C.c_void_p(-1),0,0,0,0,0x1|0x2|0x10|0x40):
                raise C.WinError(C.get_last_error())
        finally:
            if previous: self.u.SetThreadDpiAwarenessContext(previous)

    def hide(self):
        if self.window: self.u.ShowWindow(self.window,0)

    def close(self):
        self.hide()
        if self.window: self.u.DestroyWindow(self.window); self.window = None
        if self.bitmap:
            self.g.SelectObject(self.dc,self.old_bitmap)
            self.g.DeleteObject(self.bitmap); self.bitmap = None
        if self.dc: self.g.DeleteDC(self.dc); self.dc = None
        self.u.UnregisterClassW(self.name,self.instance)


class _MacSurface:
    """Nonactivating NSPanel with a transparent bitmap, outside Tk's frames."""
    def __init__(self):
        from crossdesk.backends.mac_cursor import _PublicMacCursorAPI, _Rect, _Point, _Size
        self.api = _PublicMacCursorAPI()
        self.Rect, self.Point, self.Size = _Rect, _Point, _Size
        self.window = self.view = self.image = self.rep = None
        self.size = None
        send, cls = self.api._send, self.api._class
        allocated = send(cls('NSPanel'),'alloc',C.c_void_p)
        self.window = send(allocated,'initWithContentRect:styleMask:backing:defer:',C.c_void_p,
                           (_Rect,C.c_ulong,C.c_ulong,C.c_bool),_Rect(_Point(0,0),_Size(1,1)),128,2,False)
        if not self.window: raise RuntimeError('Light bridge panel unavailable')
        send(self.window,'setReleasedWhenClosed:',None,(C.c_bool,),False)
        send(self.window,'setOpaque:',None,(C.c_bool,),False)
        clear = send(cls('NSColor'),'clearColor',C.c_void_p)
        send(self.window,'setBackgroundColor:',None,(C.c_void_p,),clear)
        send(self.window,'setHasShadow:',None,(C.c_bool,),False)
        send(self.window,'setIgnoresMouseEvents:',None,(C.c_bool,),True)
        send(self.window,'setHidesOnDeactivate:',None,(C.c_bool,),False)
        send(self.window,'setLevel:',None,(C.c_long,),25)
        send(self.window,'setCollectionBehavior:',None,(C.c_ulong,),1|256)

    def _clear_bitmap(self):
        send = self.api._send
        if self.view: send(self.view,'setImage:',None,(C.c_void_p,),None)
        for name in ('image','rep','view'):
            value = getattr(self,name)
            if value: send(value,'release'); setattr(self,name,None)

    def show(self, frame, bounds):
        send, cls = self.api._send, self.api._class
        w, h = frame.size
        if self.size != frame.size:
            self._clear_bitmap()
            rect = self.Rect(self.Point(0,0),self.Size(w,h))
            self.view = send(send(cls('NSImageView'),'alloc',C.c_void_p),'initWithFrame:',
                             C.c_void_p,(self.Rect,),rect)
            color_name = send(cls('NSString'),'stringWithUTF8String:',C.c_void_p,
                              (C.c_char_p,),b'NSDeviceRGBColorSpace')
            self.rep = send(send(cls('NSBitmapImageRep'),'alloc',C.c_void_p),
                            'initWithBitmapDataPlanes:pixelsWide:pixelsHigh:bitsPerSample:samplesPerPixel:hasAlpha:isPlanar:colorSpaceName:bytesPerRow:bitsPerPixel:',
                            C.c_void_p,(C.c_void_p,C.c_long,C.c_long,C.c_long,C.c_long,C.c_bool,C.c_bool,C.c_void_p,C.c_long,C.c_long),
                            None,w,h,8,4,True,False,color_name,w*4,32)
            if not self.rep: raise RuntimeError('Light bridge bitmap unavailable')
            self.image = send(send(cls('NSImage'),'alloc',C.c_void_p),'initWithSize:',
                              C.c_void_p,(self.Size,),self.Size(w,h))
            send(self.image,'addRepresentation:',None,(C.c_void_p,),self.rep)
            send(self.view,'setImage:',None,(C.c_void_p,),self.image)
            send(self.window,'setContentView:',None,(C.c_void_p,),self.view)
            self.size = frame.size
        # NSBitmapImageRep's default RGBA format uses associated alpha, just
        # like a Windows layered DIB. Keep glow edges free of dark fringes.
        data = _premultiplied(frame).tobytes()
        pixels = send(self.rep,'bitmapData',C.c_void_p)
        if not pixels: raise RuntimeError('Light bridge bitmap data unavailable')
        C.memmove(pixels,data,len(data))
        send(self.image,'recache')
        screens = send(cls('NSScreen'),'screens',C.c_void_p)
        primary = send(screens,'objectAtIndex:',C.c_void_p,(C.c_ulong,),0)
        # Quartz's origin is the primary display's top-left. Cocoa's is bottom-left.
        rect = send(primary,'frame',self.Rect)
        top = rect.origin.y+rect.size.height
        target = self.Rect(self.Point(bounds.x,top-bounds.y-h),self.Size(w,h))
        send(self.window,'setFrame:display:',None,(self.Rect,C.c_bool),target,False)
        send(self.view,'setNeedsDisplay:',None,(C.c_bool,),True)
        send(self.window,'orderFrontRegardless')

    def hide(self):
        if self.window: self.api._send(self.window,'orderOut:',None,(C.c_void_p,),None)

    def close(self):
        self.hide()
        self._clear_bitmap()
        if self.window:
            self.api._send(self.window,'close')
            self.api._send(self.window,'release')
            self.window = None


class HandoffEffect:
    """One replaceable animation; idle presentation has no timer or redraws."""
    def __init__(self, root, enabled=True, *, surface_factory=None, clock=time.monotonic):
        self.root, self.enabled, self._clock = root, enabled, clock
        self._surface_factory = surface_factory or (_WindowsSurface if sys.platform == 'win32' else _MacSurface)
        self._surface = None
        self._after = None
        self._closed = self._failed = False
        self._bounds = None
        self.last_error = None

    def play(self, event):
        if self._closed or self._failed or not self.enabled:
            return
        if threading.current_thread() is not threading.main_thread():
            return  # Input workers never call native GUI APIs.
        try:
            if event.get('phase') not in ('depart','arrive'):
                return
            bounds = effect_bounds(event)
            self.cancel()
            self._bounds, self._phase = bounds, event['phase']
            self._duration = .72 if self._phase == 'arrive' else .58
            if self._surface is None:
                self._surface = self._surface_factory()
            self._started = self._clock()
            self._tick()
        except Exception as error:
            self._disable(error)

    def _tick(self):
        self._after = None
        if self._closed or self._failed or self._bounds is None:
            return
        try:
            started = self._clock()
            progress = (started-self._started)/self._duration
            if progress >= 1.:
                self.cancel()
                return
            self._surface.show(render_frame(self._bounds,self._phase,progress),self._bounds)
            elapsed = self._clock()-started
            self._after = self.root.after(max(1,round(1000/60-elapsed*1000)),self._tick)
        except Exception as error:
            self._disable(error)

    def cancel(self):
        if self._after is not None:
            try: self.root.after_cancel(self._after)
            except Exception: pass
            self._after = None
        self._bounds = None
        if self._surface is not None:
            try: self._surface.hide()
            except Exception as error: self._disable(error)

    def _disable(self, error=None):
        self._failed = True
        # Keep a local fixed diagnostic, never native messages that might
        # contain foreground titles, coordinates or other user's input data.
        self.last_error = 'CrossDesk 跨屏动效暂不可用；键鼠互控不受影响。'
        if self._after is not None:
            try: self.root.after_cancel(self._after)
            except Exception: pass
            self._after = None
        surface, self._surface = self._surface, None
        self._bounds = None
        if surface is not None:
            try: surface.close()
            except Exception: pass

    def close(self):
        self._closed = True
        self.cancel()
        surface, self._surface = self._surface, None
        if surface is not None:
            try: surface.close()
            except Exception: pass
