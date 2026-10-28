"""Seam geometry, true alpha and bounded animation; no native input APIs."""
import unittest

from crossdesk.handoff_effect import HandoffEffect, effect_bounds, render_frame, _premultiplied_bgra


def event(edge='left', phase='arrive'):
    return {'phase':phase, 'edge':edge, 'x':-500, 'y':90,
            'screen':{'x':-500, 'y':-100, 'width':800, 'height':600}}


class Root:
    def __init__(self): self.pending = {}; self.sequence = 0
    def after(self, delay, callback):
        self.sequence += 1
        self.pending[self.sequence] = callback
        return self.sequence
    def after_cancel(self, token): self.pending.pop(token,None)
    def step(self):
        _, callback = self.pending.popitem()
        callback()


class Surface:
    def __init__(self): self.frames = 0; self.hidden = 0; self.closed = 0
    def show(self, frame, bounds): self.frames += 1
    def hide(self): self.hidden += 1
    def close(self): self.closed += 1


class HandoffEffectTests(unittest.TestCase):
    def test_core_primitives_preserve_frame_without_optional_imagedraw_dependency(self):
        # ImageDraw is present in the development environment, but absent from
        # the original permanent host. Compare pixels before shipping an update.
        from unittest.mock import patch
        from PIL import ImageDraw
        bounds = effect_bounds(event())
        for phase in ('depart', 'arrive'):
            expected = render_frame(bounds,phase,.34).tobytes()
            with patch('crossdesk.handoff_effect._Draw',ImageDraw.Draw):
                reference = render_frame(bounds,phase,.34).tobytes()
            self.assertEqual(expected,reference)

    def test_bounds_clip_each_edge_at_negative_origin_and_corner(self):
        for edge in ('left','right','top','bottom'):
            data = event(edge)
            data['x'], data['y'] = -900, -800
            bounds = effect_bounds(data)
            self.assertGreaterEqual(bounds.x,-500)
            self.assertGreaterEqual(bounds.y,-100)
            self.assertLessEqual(bounds.x+bounds.width,300)
            self.assertLessEqual(bounds.y+bounds.height,500)
            self.assertEqual(bounds.anchor,0)
            frame = render_frame(bounds,'arrive',.3)
            self.assertEqual(frame.size,(bounds.width,bounds.height))

    def test_spectral_pixels_are_translucent_and_last_frame_clears(self):
        bounds = effect_bounds(event())
        image = render_frame(bounds,'arrive',.35)
        self.assertEqual(image.mode,'RGBA')
        self.assertGreater(image.getchannel('A').getextrema()[1],80)
        self.assertEqual(image.getpixel((219,559)),(0,0,0,0))
        self.assertIsNone(render_frame(bounds,'arrive',1).getbbox())
        self.assertNotEqual(image.tobytes(),render_frame(bounds,'depart',.35).tobytes())

    def test_premultiplication_does_not_brighten_transparent_pixels(self):
        from PIL import Image
        self.assertEqual(_premultiplied_bgra(Image.new('RGBA',(1,1),(200,100,50,128))),
                         bytes((25,50,100,128)))

    def test_rapid_crossing_replaces_timer_and_auto_hides(self):
        root, surface, clock = Root(), Surface(), [10.]
        effect = HandoffEffect(root,surface_factory=lambda:surface,clock=lambda:clock[0])
        effect.play(event())
        self.assertEqual(len(root.pending),1)
        first = set(root.pending)
        effect.play(event('right','depart'))
        self.assertEqual(len(root.pending),1)
        self.assertFalse(first & set(root.pending))
        self.assertEqual(surface.frames,2)
        clock[0] += .8
        root.step()
        self.assertFalse(root.pending)
        self.assertIsNone(effect._bounds)
        self.assertGreater(surface.hidden,0)
        effect.close(); effect.close()
        self.assertEqual(surface.closed,1)

    def test_surface_failure_is_visual_only_and_stops_scheduling(self):
        root, surface = Root(), Surface()
        def fail(*args): raise OSError('No compositor')
        surface.show = fail
        effect = HandoffEffect(root,surface_factory=lambda:surface)
        effect.play(event())
        self.assertTrue(effect._failed)
        self.assertFalse(root.pending)
        self.assertEqual(surface.closed,1)
        effect.play(event())
        self.assertFalse(root.pending)

    def test_disabled_or_closed_effect_allocates_nothing(self):
        root = Root()
        def unexpected(): self.fail('Disabled effect allocated a native surface')
        effect = HandoffEffect(root,False,surface_factory=unexpected)
        effect.play(event())
        effect.enabled = True
        effect.close()
        effect.play(event())
        self.assertFalse(root.pending)

    def test_cancel_hides_and_discards_the_only_frame_timer(self):
        root, surface = Root(), Surface()
        effect = HandoffEffect(root,surface_factory=lambda:surface)
        effect.play(event())
        effect.cancel()
        self.assertFalse(root.pending)
        self.assertIsNone(effect._bounds)
        self.assertGreater(surface.hidden,0)


if __name__ == '__main__': unittest.main()
