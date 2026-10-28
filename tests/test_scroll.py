import unittest
from crossdesk.scroll import ScrollCurve

class ScrollCurveTests(unittest.TestCase):
    def setUp(self):
        self.now=0.
        self.curve=ScrollCurve(clock=lambda:self.now)
    def test_one_notch_is_three_lines_spread_across_time(self):
        self.curve.push(0,1)
        self.assertEqual(self.curve.sample(),(0,0))
        total=0;steps=[]
        for i in range(1,15):
            self.now=i/120
            _,step=self.curve.sample();total+=step;steps.append(step)
        self.assertAlmostEqual(total,3)
        self.assertGreater(sum(step>0 for step in steps),8)
        self.assertFalse(self.curve.pending)
    def test_overlapping_wheel_ticks_accumulate_without_losing_distance(self):
        self.curve.push(1,1);self.now=.02
        x,y=self.curve.sample();self.curve.push(2,3)
        self.now=1
        dx,dy=self.curve.sample()
        self.assertAlmostEqual(x+dx,9);self.assertAlmostEqual(y+dy,12)
    def test_reversal_cancels_prior_inertia_immediately(self):
        self.curve.push(0,1);self.now=.01;self.curve.sample()
        self.curve.push(0,-1);self.now=.02
        self.assertLess(self.curve.sample()[1],0)
    def test_clear_stops_old_screen_scroll(self):
        self.curve.push(0,1);self.curve.clear();self.now=1
        self.assertEqual(self.curve.sample(),(0,0))
    def test_flood_is_bounded_and_retains_total_distance(self):
        for _ in range(1000):self.curve.push(0,1)
        self.assertLessEqual(len(self.curve.impulses),32)
        self.now=1
        self.assertAlmostEqual(self.curve.sample()[1],3000)
