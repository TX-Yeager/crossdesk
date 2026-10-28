"""Local log privacy, bounded rotation and fail-open IO; no native input."""
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from crossdesk import diagnostics


class DiagnosticsTests(unittest.TestCase):
    def test_real_router_pause_records_both_local_and_peer_exit_reasons(self):
        from tests import test_engine
        path=diagnostics.configure(self.root)
        fixture=test_engine.EngineTests();fixture.setUp()
        try:
            win,mac=fixture.pair()
            win.set_enabled(False,reason='manual');fixture.pump(win,mac)
            rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            reasons={row.get('reason') for row in rows if row['event']=='control_exit'}
            self.assertIn('manual',reasons)
            self.assertIn('peer_pause',reasons)
        finally:fixture.tearDown()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crossdesk-log-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        for handler in diagnostics._LOGGER.handlers:
            handler.close()
        diagnostics._LOGGER.handlers[:] = [logging.NullHandler()]
        self.temporary.cleanup()

    def test_allowlist_and_exception_stack_never_include_sensitive_values(self):
        path = diagnostics.configure(self.root)
        secret = "private-clipboard-password-pairing-marker"
        diagnostics.event("control_exit", reason="user_pause", enabled=False, peer_active=True,
                          connected=True, key="ctrl_l", clipboard=secret, secret=secret, x=32, y=55)
        try:
            exec(compile("raise RuntimeError(secret)", "/private/user-name/crossdesk/engine.py", "exec"),
                 {"RuntimeError": RuntimeError, "secret": secret})
        except RuntimeError as error:
            diagnostics.exception("unexpected_exception", error, reason="RuntimeError", message=secret,
                                  pairing_code=secret, local_variables={"key": "ctrl_l"})
        diagnostics.event(secret, reason=secret)
        diagnostics.event("native_handoff_failed", reason=secret, hook_seen=True, position_reached=False,
                          error_x=2.25, error_y=float("nan"), x=1000, y=800)
        text = path.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["reason"], "user_pause")
        self.assertEqual(rows[1]["exception_type"], "RuntimeError")
        self.assertEqual(rows[1]["stack"][-1], {"file": "engine.py", "function": "<module>", "line": 1})
        self.assertEqual(rows[2]["reason"], "other")
        self.assertEqual(rows[2]["error_x"], 2.25)
        self.assertNotIn("error_y", rows[2])
        for prohibited in (secret, "ctrl_l", "user-name", "pairing_code", "local_variables", '"x":', '"y":'):
            self.assertNotIn(prohibited, text)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertEqual(path.parent.stat().st_mode & 0o077, 0)

    def test_rotating_handler_keeps_bounded_valid_json_under_concurrent_writers(self):
        path = diagnostics.configure(self.root)
        handler = diagnostics._LOGGER.handlers[0]
        self.assertEqual(handler.maxBytes, 1024 * 1024)
        self.assertEqual(handler.backupCount, 3)
        handler.maxBytes = 600
        def write_events():
            for _ in range(35):
                diagnostics.event("control_changed", enabled=False, reason="peer_pause", peer_active=False)
        threads = [threading.Thread(target=write_events) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        files = list(path.parent.glob("crossdesk.jsonl*"))
        self.assertEqual(len(files), 4)
        for file in files:
            self.assertLessEqual(file.stat().st_size, 600)
            for line in file.read_text(encoding="utf-8").splitlines():
                self.assertEqual(json.loads(line)["event"], "control_changed")

    def test_configuration_and_emit_errors_never_escape_or_print_exception_details(self):
        blocker = self.root / "not-a-directory"
        blocker.write_bytes(b"existing file")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(diagnostics.configure(blocker), blocker / "logs/crossdesk.jsonl")
            diagnostics.event("app_exit_requested", reason="window_close")
            diagnostics.configure(self.root)
            handler = diagnostics._LOGGER.handlers[0]
            with patch.object(handler, "shouldRollover", side_effect=OSError("private-error-marker")):
                diagnostics.event("app_closed", reason="normal", success=True)
                diagnostics.exception("routing_exception", RuntimeError("private-error-marker"), reason="routing_error")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(blocker.read_bytes(), b"existing file")


if __name__ == "__main__":
    unittest.main()
