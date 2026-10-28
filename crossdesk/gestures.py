"""Translate a complete Dock gesture, without forwarding raw touch data.

macOS 15 Dock-control CGEvent fields are undocumented. Kept here as a small
optional adapter: only vertical/pinch sequences begun on a capable remote
Windows desktop are consumed. Normal Mac gestures pass through unchanged.
Field reference: https://github.com/oomol-lab/dockswipe/blob/main/dockswipe.m
"""
import math
import time

ACTIONS=frozenset(('task_view','start_menu'))

class DockGesture:
    def __init__(self,clock=time.monotonic):
        self.clock=clock
        self.axis=None
        self.captured=False
        self.peak=0.
        self.last_axis=None
        self.last_end=-10.
        self.sequences=self.forwarded=self.task_view=self.start_menu=0

    def consume(self,axis,phase,progress,handler):
        if axis not in (2,3) or phase not in (1,2,4,8):return False
        if not math.isfinite(progress):return False
        if phase==1:
            self.axis=axis
            self.captured=bool(handler({'type':'gesture','action':None}))
            self.peak=0.
            self.sequences+=1
        elif self.axis!=axis:
            # WindowServer may repeat an ending. It must not toggle twice or
            # reach the Dock as a dangling half of a consumed gesture.
            return bool(phase in (4,8) and self.last_axis==axis
                        and self.clock()-self.last_end<1.)
        self.peak=max(self.peak,abs(progress))
        captured=self.captured
        if phase in (4,8):
            self.axis=None
            if captured:
                self.last_axis,self.last_end=axis,self.clock()
            if captured and phase==4 and self.peak>.02:
                action='task_view' if axis==2 else 'start_menu'
                if handler({'type':'gesture','action':action}):
                    self.forwarded+=1
                    setattr(self,action,getattr(self,action)+1)
            self.captured=False
        return captured
