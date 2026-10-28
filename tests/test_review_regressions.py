"""Focused 0.2.7 regressions; fake input and isolated clipboards only."""
import queue
import threading
import unittest
from unittest.mock import Mock
from crossdesk.backends.windows_cursor import WindowsCursor
from crossdesk.transport import _QueuedFrame, _SendQueue
from tests.test_engine import EngineTests
from tests.test_clipboard_sync import ClipboardSyncTests


class ReviewRegressions(unittest.TestCase):
    def test_cursor_recovers_transient_loss_with_a_fresh_deadline(self):
        now=[0.]
        native=Mock()
        native.hidden.return_value=True
        cursor=WindowsCursor(None,native=native,clock=lambda:now[0])
        self.assertIsNone(cursor.update(True))
        for _ in range(100):cursor.update(True)
        self.assertEqual(native.show.call_count,1)
        now[0]=20.;native.hidden.return_value=False
        self.assertIsNone(cursor.update(True))
        now[0]=20.2;native.hidden.return_value=True
        self.assertIsNone(cursor.update(True))
        now[0]=40.;native.hidden.return_value=False
        self.assertIsNone(cursor.update(True))
        now[0]=40.6
        self.assertIsNone(cursor.update(True))
        self.assertIsNotNone(cursor.warning)
        self.assertFalse(cursor.hidden)

    def test_gui_cursor_tick_does_not_wait_for_router(self):
        cursor=WindowsCursor(None,native=Mock())
        held=threading.Event();release=threading.Event()
        def work():
            with cursor._lock:held.set();release.wait(1)
        thread=threading.Thread(target=work);thread.start();held.wait(1)
        try:self.assertIsNone(cursor.update(True))
        finally:release.set();thread.join(1)
        cursor._native.show.assert_not_called()

    def test_unsupported_or_unreadable_clipboard_cannot_paste_stale_content(self):
        fixture=ClipboardSyncTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        broker,native,_=fixture.single()
        for fail in (False,True):
            with self.subTest(read_error=fail):
                result=[]
                native.read=Mock(side_effect=OSError() if fail else None,return_value=None)
                broker.before_paste('other',result.append)
                broker.poll(active=True)
                self.assertEqual(result,[False])

    def test_pointer_modifiers_map_both_ways_and_release_cleanly(self):
        fixture=EngineTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        win,mac=fixture.pair()
        for source,target,original,mapped in ((mac,win,'meta_l','ctrl_l'),(win,mac,'ctrl_r','meta_r')):
            destination=next(s for s in source.desktop.screens if s.machine==target.machine['id'])
            source.current=destination.id
            source._set_keyboard_focus(destination,force=True)
            source._local({'type':'key','key':original,'down':True})
            source._local({'type':'button','button':'left','down':True})
            self.assertEqual(source._key_modifiers,{mapped})
            source._local({'type':'button','button':'left','down':False})
            source._local({'type':'key','key':original,'down':False})
            self.assertFalse(source._key_modifiers)
            source.swap_modifiers=False;source.held_keys={original}
            self.assertEqual(source._pointer_modifiers(destination),{original})
            source.held_keys.clear()

    def test_queue_coalesces_only_adjacent_pointer_samples_and_prioritizes_input(self):
        outbox=_SendQueue(8)
        for item in (_QueuedFrame(1,b'chunk',bulk=True),_QueuedFrame(1,b'p1',('screen',1)),
                     _QueuedFrame(1,b'p2',('screen',1)),_QueuedFrame(1,b'click'),
                     _QueuedFrame(1,b'p3',('screen',1)),_QueuedFrame(1,b'focus'),
                     _QueuedFrame(1,b'p4',('screen',2))):outbox.put_nowait(item)
        self.assertEqual([outbox.get_nowait()[1] for _ in range(6)],
                         [b'p2',b'click',b'p3',b'focus',b'p4',b'chunk'])
        with self.assertRaises(queue.Empty):outbox.get_nowait()

    def test_dragged_pointer_preserves_every_sample(self):
        fixture=EngineTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        win,mac=fixture.pair()
        screen=win.desktop.by_id[win.current]
        self.assertTrue(win._pointer_message('pointer',screen)['coalesce'])
        win.held_buttons.add('left')
        self.assertFalse(win._pointer_message('pointer',screen)['coalesce'])

if __name__=='__main__':unittest.main()
