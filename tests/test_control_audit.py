"""Regressions for intermittent crossings and ambiguous stops, without OS input."""
import time
import unittest
from unittest.mock import Mock

from crossdesk.backends.windows_cursor import WindowsCursor
from tests.test_engine import EngineTests


class ControlAuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = EngineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_ineffective_cursor_hide_does_not_block_first_or_repeated_crossings(self):
        win, mac = self.fixture.pair()
        now = [0.]
        native = Mock()
        native.hidden.return_value = False
        cursor = WindowsCursor(None, native=native, clock=lambda: now[0])
        def park(x, y):
            cursor.ensure_hidden()
            win.backend.warp(x, y)
        win.backend.park_remote = park
        for cycle in range(20):
            cursor.update(False)
            self.fixture.move(win, 80)
            self.fixture.pump(win, mac)
            self.assertEqual((win.pointer_owner, mac.pointer_owner), ('mac', 'mac'))
            now[0] += 1
            self.assertIsNone(cursor.update(True))
            self.assertIsNotNone(cursor.warning)
            self.fixture.move(win, -80)
            self.fixture.pump(win, mac)
            self.assertEqual((win.pointer_owner, mac.pointer_owner), ('win', 'win'))
            self.assertTrue(win.enabled and mac.enabled)
            self.assertIsNone(win.last_stop)

    def test_native_hide_error_degrades_but_failed_overlay_removal_is_fatal(self):
        native = Mock()
        native.show.side_effect = OSError('show failed')
        cursor = WindowsCursor(None, native=native)
        cursor.ensure_hidden()
        self.assertIsNotNone(cursor.warning)
        native.hide.assert_called_once()
        cursor.update(False)
        native.hide.side_effect = OSError('cannot remove input window')
        with self.assertRaises(OSError):
            cursor.ensure_hidden()

    def test_specific_stop_survives_ready_control_echo_and_restart_clears_it(self):
        win, mac = self.fixture.pair()
        win.set_enabled(False, reason='input_restore_failed')
        self.fixture.pump(win, mac)
        self.assertEqual(win.last_stop['code'], 'input_restore_failed')
        self.assertEqual(win.last_stop['source'], 'local')
        self.assertEqual(mac.last_stop['code'], 'input_restore_failed')
        self.assertEqual(mac.last_stop['source'], 'peer')
        win._network({'type': 'ready', 'enabled': False})
        self.assertEqual(win.last_stop['code'], 'input_restore_failed')
        mac.set_enabled(True)
        self.fixture.pump(win, mac)
        self.assertEqual((win.session_state, mac.session_state), ('active', 'active'))
        self.assertIsNone(win.last_stop)
        self.assertIsNone(mac.last_stop)

    def test_same_layout_new_revision_does_not_pause_or_reset_pointer(self):
        win, mac = self.fixture.pair()
        self.fixture.move(win, 80)
        self.fixture.pump(win, mac)
        before = (win.current, win.cursor, win.backend.stops)
        win._network({'type': 'layout', 'revision': [win.revision[0]+1, 'mac'],
                      'screens': [s.as_dict() for s in reversed(win.desktop.screens)]})
        self.assertEqual((win.current, win.cursor, win.backend.stops), before)
        self.assertTrue(win.enabled)
        self.assertFalse(win.control_pending)

    def test_changed_layout_pauses_both_with_reason_and_invalid_layout_is_atomic(self):
        win, mac = self.fixture.pair()
        before = win.desktop
        raw = [s.as_dict() for s in win.desktop.screens]
        raw[1]['x'] = raw[0]['x']
        with self.assertRaises(ValueError):
            win._network({'type': 'layout', 'revision': [win.revision[0]+1, 'mac'], 'screens': raw})
        self.assertIs(win.desktop, before)
        self.assertTrue(win.enabled)
        raw[1]['x'] = 110
        win._network({'type': 'layout', 'revision': [win.revision[0]+2, 'mac'], 'screens': raw})
        self.fixture.pump(win, mac)
        self.assertFalse(win.enabled or mac.enabled)
        self.assertEqual(win.last_stop['code'], 'layout_changed')

    def test_timeout_is_named_and_malformed_peer_reason_never_reaches_ui(self):
        win, mac = self.fixture.pair(enable=False)
        win.set_enabled(True)
        self.assertEqual(win.session_state, 'starting')
        win._pending_control['deadline'] = time.monotonic()-1
        win._check_control_timeout()
        self.assertEqual(win.last_stop['code'], 'start_timeout')
        self.fixture.pump(win, mac)
        win.set_enabled(True)
        self.fixture.pump(win, mac)
        win._network({'type': 'ready', 'enabled': False, 'reason': {'private': 'details'}})
        self.assertEqual(win.last_stop['code'], 'peer_pause')
        self.assertNotIn('private', win.last_stop['message'])


if __name__ == '__main__':
    unittest.main()
