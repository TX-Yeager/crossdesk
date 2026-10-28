"""Private, bounded lifecycle/error logs with an explicit data allowlist.

Never pass input events or clipboard snapshots to this API. Unknown events are
dropped; unknown fields are ignored; reason/code values outside the published
enums become ``other``. Exceptions include only a known class name and code
locations, never their message, arguments, locals or source lines.
"""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import threading


SAFE_REASONS = frozenset({
    "other", "normal", "normal_exit", "unknown", "user", "user_start", "user_pause",
    "ui_start", "ui_pause", "local_pause", "peer_pause", "peer_control", "start", "pause",
    "stop", "enabled", "disabled", "f12", "F12", "emergency", "emergency_stop",
    "queue_overflow", "queue_full", "control_timeout", "control_rollback", "session_rollback",
    "control_failed", "capture_failed", "capture_stopped", "backend_error", "routing_error",
    "cursor_failed", "native_handoff_failed", "position_mismatch", "hook_not_observed",
    "permission_required", "permission_denied", "protocol_error", "transport_error",
    "disconnect", "network_disconnect", "connection_lost", "connection_closed", "peer_disconnected",
    "window_close", "wm_delete_window", "mac_quit", "app_close", "app_exit", "close",
    "sigterm", "SIGTERM", "keyboard_interrupt", "KeyboardInterrupt", "mainloop_return",
    "startup_failed", "startup_exception", "callback_exception", "unexpected_exception",
    "cleanup_failed", "close_error", "update_restart", "update", "cancelled", "timeout",
    "manual", "requested", "rollback", "control_change", "confirmed", "routing_exception",
    "gui_callback", "open_logs", "input_queue_full", "cursor_exception", "cursor_state",
    "cursor_management", "position_timeout", "position_cancelled", "native_position_failed",
    "cursor_clipped", "windows_hook", "macos_capture", "cursor_hide_failed",
    "start_timeout", "pause_timeout", "position_failed", "input_restore_failed", "layout_changed",
    "physical_move", "physical_button", "peer_reclaim", "stale_focus", "pending_takeover",
})
SAFE_CODES = frozenset({
    "other", "ok", "unknown", "cancelled", "timeout", "permission_required", "permission_denied",
    "capture_failed", "capture_stopped", "control_failed", "control_timeout", "queue_full",
    "protocol_error", "transport_error", "native_error", "io_error", "startup_failed",
    "ui_callback_failed", "cleanup_failed", "clipboard_failed", "update_failed", "cursor_failed",
    "depart", "arrive",
})
_ERROR_TYPES = frozenset({
    "BaseException", "Exception", "RuntimeError", "ValueError", "TypeError", "AttributeError",
    "KeyError", "IndexError", "LookupError", "AssertionError", "NotImplementedError",
    "OSError", "IOError", "PermissionError", "FileNotFoundError", "FileExistsError",
    "TimeoutError", "ConnectionError", "ConnectionResetError", "ConnectionRefusedError",
    "ConnectionAbortedError", "BrokenPipeError", "BlockingIOError", "InterruptedError",
    "ImportError", "ModuleNotFoundError", "MemoryError", "OverflowError", "RecursionError",
    "UnicodeError", "UnicodeEncodeError", "UnicodeDecodeError", "SyntaxError", "EOFError",
    "KeyboardInterrupt", "SystemExit", "TclError", "ClipboardError", "UpdateError",
    "ProtocolError", "SSLError", "SSLEOFError", "SSLCertVerificationError", "InvalidSignature",
})
_COMMON = frozenset({"code", "source"})
_SESSION = frozenset({"enabled", "peer_active", "peer_ready", "connected", "backend_running", "control_pending"})
EVENT_FIELDS = {
    "app_started": {"version", "platform", "host_api", "host_active", "updated"},
    "app_exit_requested": {"reason"},
    "app_closed": {"reason", "success", "exit_code"} | _SESSION,
    "control_requested": {"enabled", "reason", "propagate"},
    "control_changed": {"enabled", "reason", "peer_active"},
    "control_exit": {"reason"} | _SESSION,
    "control_rollback": {"reason"} | _SESSION,
    "session_stopped": {"reason", "source"},
    "cursor_degraded": {"reason"},
    "pointer_handoff": {"code"},
    "pointer_claim": {"reason", "source"},
    "pointer_focus_rejected": {"reason"},
    "peer_control": {"enabled"},
    "connection_changed": {"connected", "reason"},
    "capture_failed": {"reason"},
    "routing_exception": {"reason"},
    "cursor_failed": {"reason"},
    "native_handoff_failed": {"reason", "hook_seen", "position_reached", "error_x", "error_y", "native_code"},
    "gesture_sent": {"action"},
    "gesture_send_failed": {"action", "reason"},
    "gesture_executed": {"action"},
    "unexpected_exception": {"reason"},
    "cleanup_failed": {"reason"},
}
_BOOL_FIELDS = _SESSION | {"success", "propagate", "hook_seen", "position_reached", "host_active", "updated"}
_SOURCE = frozenset({"local", "peer", "system", "gui", "router", "transport", "backend",
                     "windows", "macos", "startup", "shutdown", "update", "clipboard"})
_VERSION = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}(?:[-+][A-Za-z0-9.-]{1,32})?\Z")
_FUNCTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}\Z")
_CODE_FILES = frozenset({
    "__init__.py", "__main__.py", "app.py", "engine.py", "geometry.py", "transport.py",
    "discovery.py", "diagnostics.py", "updates.py", "clipboard_sync.py", "clipboard_native.py",
    "gestures.py", "mac_cursor.py", "windows_cursor.py", "scroll.py", "macos.py", "windows.py", "trustedhost.py", "runtime_host.py",
})
_CONFIG_LOCK = threading.RLock()
_LOGGER = logging.Logger("crossdesk.private.lifecycle", level=logging.INFO)
_LOGGER.propagate = False
_LOGGER.addHandler(logging.NullHandler())


class _PrivateRotatingHandler(logging.FileHandler):
    """Bounded handler using modules already present in the permanent host."""
    def __init__(self,path,*,maxBytes,backupCount,encoding):
        self.maxBytes,self.backupCount=maxBytes,backupCount
        super().__init__(path,mode='a',encoding=encoding)

    def emit(self,record):
        # logging.Handler holds its thread lock around emit and rotation.
        try:
            if self.stream is None:self.stream=self._open()
            size=len((self.format(record)+'\n').encode('utf-8'))
            if os.fstat(self.stream.fileno()).st_size+size>self.maxBytes:
                self.stream.close();self.stream=None
                for number in range(self.backupCount-1,0,-1):
                    source=f'{self.baseFilename}.{number}'
                    if os.path.exists(source):os.replace(source,f'{self.baseFilename}.{number+1}')
                if self.backupCount and os.path.exists(self.baseFilename):
                    os.replace(self.baseFilename,self.baseFilename+'.1')
                self.stream=self._open()
            super().emit(record)
        except Exception:self.handleError(record)

    def _open(self):
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(self.baseFilename, 0o600)
            return io.open(descriptor, "a", encoding="utf-8", errors="strict", closefd=True)
        except Exception:
            os.close(descriptor)
            raise

    def handleError(self, record):
        # The default logging handler can print errors, including arbitrary OS
        # paths, to stderr. Diagnostics must never affect the input session.
        pass


def configure(state_dir):
    """Return the local log path; inaccessible storage silently disables logging."""
    path = None
    handler = None
    try:
        path = Path(state_dir) / "logs" / "crossdesk.jsonl"
        if path.parent.is_symlink() or path.is_symlink():
            raise OSError("Unsafe diagnostic destination")
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        handler = _PrivateRotatingHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
    except Exception:
        if handler is not None:
            try:
                handler.close()
            except Exception:
                pass
        handler = logging.NullHandler()
    try:
        with _CONFIG_LOCK:
            previous = list(_LOGGER.handlers)
            _LOGGER.handlers[:] = [handler]
        for old in previous:
            try:
                old.close()
            except Exception:
                pass
    except Exception:
        pass
    return path


def _fields(name, supplied):
    result = {}
    for field in EVENT_FIELDS[name] | _COMMON:
        if field not in supplied:
            continue
        value = supplied[field]
        if field in _BOOL_FIELDS:
            if type(value) is bool:
                result[field] = value
        elif field in ("reason", "code"):
            allowed = SAFE_REASONS | _ERROR_TYPES if field == "reason" else SAFE_CODES
            result[field] = value if type(value) is str and value in allowed else "other"
        elif field == "source":
            if type(value) is str and value in _SOURCE:
                result[field] = value
        elif field == "action":
            if type(value) is str and value in ("task_view", "start_menu"):
                result[field] = value
        elif field == "platform":
            if type(value) is str and value in ("win32", "darwin", "linux", "unknown"):
                result[field] = value
        elif field == "version":
            if type(value) is str and _VERSION.fullmatch(value):
                result[field] = value
        elif field in ("exit_code", "host_api"):
            if type(value) is int and (-65535 <= value <= 65535 if field == "exit_code" else 0 <= value <= 65535):
                result[field] = value
        elif field=='native_code':
            if type(value) is int and 0<=value<=65535:result[field]=value
        elif field in ("error_x", "error_y"):
            if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 200000:
                result[field] = round(value, 3)
    return result


def _trace(error):
    frames = []
    current = error.__traceback__
    while current is not None:
        code = current.tb_frame.f_code
        basename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        known = basename in _CODE_FILES
        function = code.co_name
        frames.append({"file": basename if known else "<external>",
                       "function": function if known and (_FUNCTION.fullmatch(function) or function in ("<module>", "<lambda>")) else "<external>",
                       "line": max(0, min(10_000_000, current.tb_lineno))})
        if len(frames) > 32:
            del frames[0]
        current = current.tb_next
    return frames


def _write(name, error, supplied):
    try:
        if type(name) is not str or name not in EVENT_FIELDS:
            return
        record = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "event": name, **_fields(name, supplied)}
        if isinstance(error, BaseException):
            kind = type(error).__name__
            record["exception_type"] = kind if kind in _ERROR_TYPES else "Exception"
            record["stack"] = _trace(error)
        message = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        _LOGGER.log(logging.ERROR if error is not None else logging.INFO, message)
    except Exception:
        pass


def event(name, **safe_fields):
    """Record an allowed lifecycle event. Unsupported fields are never formatted."""
    _write(name, None, safe_fields)


def exception(name, error, **safe_fields):
    """Record exception class/code locations without formatting its message."""
    _write(name, error, safe_fields)
