"""Bounded, time-based wheel smoothing; one wheel notch is three text lines."""
import time

class ScrollCurve:
    duration=.11
    def __init__(self,clock=time.monotonic):
        self.clock=clock
        self.impulses=[]
    def clear(self):self.impulses.clear()
    @property
    def pending(self):return bool(self.impulses)
    def push(self,dx,dy):
        if not (dx or dy):return
        now=self.clock()
        # A reversal stops the previous inertia on that axis immediately.
        for item in self.impulses:
            if dx*item[1]<0:item[1]=0
            if dy*item[2]<0:item[2]=0
        if len(self.impulses)>=32:
            dx+=sum(item[1]*(1-item[3])/3 for item in self.impulses)
            dy+=sum(item[2]*(1-item[3])/3 for item in self.impulses)
            self.clear()
        self.impulses.append([now,dx*3,dy*3,0.])
    def sample(self):
        now=self.clock();dx=dy=0.;remaining=[]
        for item in self.impulses:
            progress=min(1.,max(0.,(now-item[0])/self.duration))
            fraction=1-(1-progress)**3
            step=fraction-item[3]
            dx+=item[1]*step;dy+=item[2]*step
            item[3]=fraction
            if fraction<1:remaining.append(item)
        self.impulses=remaining
        return dx,dy
