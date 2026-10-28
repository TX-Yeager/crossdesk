"""Screen geometry, deliberately independent of hooks, networking and the GUI."""
from dataclasses import dataclass, replace
import math


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class Screen:
    id: str
    machine: str
    name: str
    native_x: float
    native_y: float
    native_w: float
    native_h: float
    x: float
    y: float
    w: float
    h: float

    @classmethod
    def from_dict(cls, data):
        fields = {key: data[key] for key in cls.__dataclass_fields__}
        for key in ("id", "machine", "name"):
            if not isinstance(fields[key], str) or not 0 < len(fields[key]) <= 200:
                raise ValueError("无效显示器标识")
        for key, value in fields.items():
            if key not in ("id", "machine", "name") and (not number(value) or abs(value) > 200000):
                raise ValueError("无效显示器坐标")
        for key in ("native_w", "native_h", "w", "h"):
            if not 32 <= fields[key] <= 32768:
                raise ValueError("显示器尺寸超出范围")
        return cls(**fields)

    def as_dict(self):
        return dict(self.__dict__)

    @property
    def right(self):
        return self.x + self.w

    @property
    def bottom(self):
        return self.y + self.h

    def contains(self, x, y):
        return self.x <= x < self.right and self.y <= y < self.bottom

    def clamp(self, x, y):
        return (max(self.x, min(self.right - 1, x)), max(self.y, min(self.bottom - 1, y)))

    def to_native(self, x, y):
        x, y = self.clamp(x, y)
        return (self.native_x + (x-self.x) * (self.native_w-1) / (self.w-1),
                self.native_y + (y-self.y) * (self.native_h-1) / (self.h-1))

    def from_native(self, x, y):
        return self.clamp(self.x + (x-self.native_x) * (self.w-1) / (self.native_w-1),
                          self.y + (y-self.native_y) * (self.h-1) / (self.native_h-1))


class Desktop:
    def __init__(self, screens):
        self.screens = list(screens)
        if len(self.screens) > 32 or len({s.id for s in self.screens}) != len(self.screens):
            raise ValueError("显示器重复或数量超出范围")
        self.by_id = {s.id: s for s in self.screens}

    def validate_layout(self):
        for i, a in enumerate(self.screens):
            for b in self.screens[i+1:]:
                if min(a.right, b.right)-max(a.x,b.x) > 0.1 and min(a.bottom,b.bottom)-max(a.y,b.y) > 0.1:
                    raise ValueError("显示器不能重叠，请将相邻边缘贴合。")

    def native_screen(self, machine, x, y):
        local = [s for s in self.screens if s.machine == machine]
        if not local:
            raise ValueError("没有本机显示器")
        return min(local, key=lambda s: max(s.native_x-x, 0, x-(s.native_x+s.native_w-1))**2
                   + max(s.native_y-y, 0, y-(s.native_y+s.native_h-1))**2)

    def advance(self, screen_id, x, y, dx, dy, allow_machine_cross=True):
        """Trace a movement across touching edges; gaps stop the pointer.

        Visible last pixels are the crossing threshold: a physical OS clips mouse
        coordinates there, so waiting for an out-of-screen pixel would get stuck.
        """
        screen = self.by_id[screen_id]
        x, y = screen.clamp(x, y)
        for _ in range(16):
            if abs(dx)+abs(dy) < 1e-7:
                break
            hits = []
            if dx > 0:
                hits.append(((screen.right-1-x)/dx, "right"))
            elif dx < 0:
                hits.append(((screen.x-x)/dx, "left"))
            if dy > 0:
                hits.append(((screen.bottom-1-y)/dy, "bottom"))
            elif dy < 0:
                hits.append(((screen.y-y)/dy, "top"))
            t, side = min(hits)
            t = max(0.0, t)
            if t > 1:
                x, y = screen.clamp(x+dx, y+dy)
                break
            qx, qy = x+dx*t, y+dy*t
            candidates = []
            for other in self.screens:
                if other.id == screen.id or (not allow_machine_cross and other.machine != screen.machine):
                    continue
                if side == "right" and abs(other.x-screen.right) <= 2 and other.y <= qy < other.bottom:
                    candidates.append(other)
                elif side == "left" and abs(other.right-screen.x) <= 2 and other.y <= qy < other.bottom:
                    candidates.append(other)
                elif side == "bottom" and abs(other.y-screen.bottom) <= 2 and other.x <= qx < other.right:
                    candidates.append(other)
                elif side == "top" and abs(other.bottom-screen.y) <= 2 and other.x <= qx < other.right:
                    candidates.append(other)
            if not candidates:
                # Preserve tangential movement while the normal component stops.
                if side in ("left", "right"):
                    x, y = screen.clamp(qx, qy)
                    dx, dy = 0, dy*(1-t)
                else:
                    x, y = screen.clamp(qx, qy)
                    dx, dy = dx*(1-t), 0
                continue
            other = min(candidates, key=lambda s: s.id)
            dx, dy = dx*(1-t), dy*(1-t)
            if side == "right":
                qx = other.x + 0.01
            elif side == "left":
                qx = other.right - 1.01
            elif side == "bottom":
                qy = other.y + 0.01
            else:
                qy = other.bottom - 1.01
            screen = other
            x, y = screen.clamp(qx, qy)
        return screen, x, y


def snap_screen(screen, others, distance=60):
    """Snap one edge and, separately, align a nearby top/left edge."""
    x_options, y_options = [], []
    for other in others:
        for x in (other.x-screen.w, other.right, other.x, other.right-screen.w):
            if abs(x-screen.x) <= distance:
                x_options.append(x)
        for y in (other.y-screen.h, other.bottom, other.y, other.bottom-screen.h):
            if abs(y-screen.y) <= distance:
                y_options.append(y)
    return replace(screen,
                   x=min(x_options, key=lambda x: abs(x-screen.x)) if x_options else screen.x,
                   y=min(y_options, key=lambda y: abs(y-screen.y)) if y_options else screen.y)


def screen_connections(screens, tolerance=2):
    """Actual shared edge segments, using the same tolerance as routing.

    Returns (first, second, x1, y1, x2, y2); corner contact and separated
    monitors never advertise a crossing channel.
    """
    result=[]
    for i,a in enumerate(screens):
        for b in screens[i+1:]:
            top,bottom=max(a.y,b.y),min(a.bottom,b.bottom)
            left,right=max(a.x,b.x),min(a.right,b.right)
            if bottom-top>1:
                if abs(a.right-b.x)<=tolerance:
                    x=(a.right+b.x)/2;result.append((a,b,x,top,x,bottom))
                elif abs(b.right-a.x)<=tolerance:
                    x=(b.right+a.x)/2;result.append((a,b,x,top,x,bottom))
            if right-left>1:
                if abs(a.bottom-b.y)<=tolerance:
                    y=(a.bottom+b.y)/2;result.append((a,b,left,y,right,y))
                elif abs(b.bottom-a.y)<=tolerance:
                    y=(b.bottom+a.y)/2;result.append((a,b,left,y,right,y))
    return result
