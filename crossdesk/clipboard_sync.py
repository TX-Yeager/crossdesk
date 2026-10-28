"""Paired-session clipboard replication with bounded, acknowledged file chunks.

Native clipboard access stays on the GUI thread. Disk IO and transfer waits run
on two private workers; at most eight 24 KiB chunks are outstanding per direction. Input bypasses
queued chunks without reordering input events; every chunk is acknowledged. Received
files stay in a new private directory, never overwrite an existing user file,
and are advertised to the OS clipboard only after their hashes match.
"""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import io
import os
from pathlib import Path
import queue
import re
import shutil
import tempfile
import threading
import time
import uuid

CHUNK = 24 * 1024
WINDOW = 8
MAX_FILES = 64
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_IMAGE = 16 * 1024 * 1024
MAX_TEXT = 1024 * 1024
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}


class ClipboardSync:
    def __init__(self, native, state_dir, machine_id, send, notify=lambda *_: None):
        self.native, self.machine_id, self.send, self.notify = native, machine_id, send, notify
        self.received = Path(state_dir) / "received-files"
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.active = False
        self.epoch = 0
        self.counter = 0
        self.revision = (0, "")
        self._seen_token = None
        self._applied_token = None
        self._fingerprint = None
        self._force = False
        self._next_poll = 0.0
        self._outgoing = None
        self._failed_revision = None
        self._incoming = None
        self._acks = {}
        self._snapshot_failed = False
        self._progress_at = 0.
        self._paste_waiters = []
        self._peer_requests = []
        self._ready_requests = {}
        self._main = queue.Queue(32)
        self._inbox = queue.Queue(16)
        self._jobs = queue.Queue(2)
        self._threads = [threading.Thread(target=self._receive_loop, name="CrossDesk-clipboard-receive", daemon=True),
                         threading.Thread(target=self._send_loop, name="CrossDesk-clipboard-send", daemon=True)]
        for thread in self._threads:
            thread.start()

    def force_snapshot(self):
        self._force = True

    def before_paste(self, target_machine, done):
        """Router hook: enqueue a GUI request and return a cancellation handle."""
        cancelled = threading.Event()
        if not self.active or self.closed.is_set():
            done(True)
            return cancelled.set
        try:
            self._main.put_nowait(("paste", (target_machine, done, cancelled)))
        except queue.Full:
            done(False)
        return cancelled.set

    @staticmethod
    def _valid_name(name):
        return (isinstance(name, str) and 0 < len(name) <= 180 and name not in (".", "..")
                and not any(ord(c) < 32 or c in '/\\:*?"<>|' for c in name)
                and not name.endswith((" ", ".")) and name.split(".", 1)[0].upper() not in _RESERVED)

    def _status(self, text):
        self.notify("status", text)

    def _reset(self):
        with self.lock:
            self.epoch += 1
            self._outgoing = None
            self._failed_revision = None
            for waiting in self._acks.values():
                waiting["ok"] = False
                waiting["event"].set()
            self._acks.clear()
            self._snapshot_failed=False
            for waiter in self._paste_waiters:
                waiter["done"](False)
            self._paste_waiters.clear()
            self._peer_requests.clear()
            self._ready_requests.clear()
            self._force = False
            self._seen_token = None
            self._applied_token = None
            self._fingerprint = None

    def poll(self, *, active):
        """GUI-only native clipboard reads/writes, with no disk/network waits."""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("剪贴板轮询必须由 GUI 主线程调用。")
        if self.closed.is_set():
            return
        if active != self.active:
            self.active = active
            self._reset()
            if active:
                try:
                    self._seen_token = self.native.get_change_token()
                except Exception:
                    self._seen_token = None
        for _ in range(32):
            try:
                kind, value = self._main.get_nowait()
            except queue.Empty:
                break
            if kind == "apply":
                self._apply(value)
            elif kind == "paste":
                target, done, cancelled = value
                if not active or cancelled.is_set():
                    done(not cancelled.is_set())
                    continue
                request = uuid.uuid4().hex if target == self.machine_id else None
                self._paste_waiters.append({"done": done, "cancelled": cancelled,
                                           "request": request, "revision": None,
                                           "deadline": time.monotonic() + 28,
                                           "maximum": time.monotonic() + 290,
                                           "progress": self._progress_at})
                self._force = True
                if request:
                    self.send({"type": "clipboard_request", "id": request})
            elif kind == "request" and active:
                self._peer_requests.append(value)
                self._force = True
        if not active:
            return
        now = time.monotonic()
        if self._force or now >= self._next_poll:
            force, self._force = self._force, False
            self._next_poll = now + .15
            try:
                token = self.native.get_change_token()
                if force or token != self._seen_token:
                    token_changed = token != self._seen_token
                    snapshot = getattr(self.native, "read_raw", self.native.read)()
                    self._seen_token = token
                    self._snapshot_failed=snapshot is None
                    if snapshot is None:
                        self._status("当前剪贴板没有可同步的文本、图片或普通文件，本次跨端粘贴已取消。")
                    if snapshot is not None:
                        fingerprint = self._snapshot_hash(snapshot)
                        if token != self._applied_token and (fingerprint != self._fingerprint
                                or snapshot.get("kind") == "files" and token_changed):
                            self._fingerprint = fingerprint
                            with self.lock:
                                self.counter += 1
                                self.revision = (self.counter, self.machine_id)
                                job = (self.epoch, self.revision, snapshot)
                                self._outgoing = job
                                self._failed_revision = None
                            # Supersede a queued copy, without buffering file data.
                            try:
                                self._jobs.put_nowait(job)
                            except queue.Full:
                                try:
                                    self._jobs.get_nowait()
                                except queue.Empty:
                                    pass
                                self._jobs.put_nowait(job)
            except Exception:
                self._snapshot_failed=True
                self._status("剪贴板暂时被占用或内容超出支持范围，请稍后重试复制。")
        with self.lock:
            busy = self._outgoing is not None or self._incoming is not None
        if not busy:
            for request in self._peer_requests:
                self.send({"type": "clipboard_ready", "id": request,
                           "ok": not self._snapshot_failed and self._failed_revision != self.revision})
            self._peer_requests.clear()
        remaining = []
        for waiter in self._paste_waiters:
            request = waiter["request"]
            if busy and self._progress_at>waiter['progress']:
                waiter['deadline']=min(waiter['maximum'],now+28)
                waiter['progress']=self._progress_at
            if waiter["revision"] is None:
                waiter["revision"] = self.revision
            if waiter["cancelled"].is_set():
                continue
            if request is None and waiter["revision"] != self.revision:
                waiter["done"](False)  # A newer copy supersedes this pending paste.
            elif now >= waiter["deadline"]:
                waiter["done"](False)
                self._status("剪贴板传输未完成，本次粘贴已取消。")
            elif not busy and (request is None or request in self._ready_requests):
                ready = self._ready_requests.pop(request, True)
                waiter["done"](ready and (request is not None or not self._snapshot_failed)
                               and self._failed_revision != waiter["revision"])
            else:
                remaining.append(waiter)
        self._paste_waiters = remaining

    @staticmethod
    def _snapshot_hash(snapshot):
        kind = snapshot.get("kind")
        if kind == "text":
            data = snapshot["text"].encode("utf-8")
        elif kind == "image":
            data = snapshot["data"]
        elif kind == "files":
            data = "\0".join(str(path) for path in snapshot["paths"]).encode("utf-8")
        else:
            raise ValueError("Unsupported clipboard format")
        return kind, hashlib.sha256(data).hexdigest()

    def receive(self, message):
        """TLS callback: bound queueing and ACK wakeups; never touch native UI."""
        if not self.active or self.closed.is_set():
            return
        kind, ident = message.get("type"), message.get("id")
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            return
        if kind == "clipboard_ack":
            with self.lock:
                step=message.get('step')
                waiting = self._acks.get((ident,step)) if type(step) in (int,str) else None
                if waiting:
                    waiting["ok"] = message.get("ok") is True
                    if waiting['ok']:self._progress_at=time.monotonic()
                    waiting["event"].set()
            return
        if kind == "clipboard_ready":
            if len(self._ready_requests) < 64 and isinstance(message.get("ok", True), bool):
                self._ready_requests[ident] = message.get("ok", True)
            return
        if kind == "clipboard_request":
            try:
                self._main.put_nowait(("request", ident))
            except queue.Full:
                pass
            return
        if kind not in ("clipboard_offer", "clipboard_chunk", "clipboard_end", "clipboard_cancel"):
            return
        try:
            self._inbox.put_nowait((self.epoch, message))
        except queue.Full:
            self._ack(ident, message.get("step"), False)

    def _ack(self, ident, step, ok=True):
        return self.send({"type": "clipboard_ack", "id": ident, "step": step, "ok": ok})

    def _begin_wait(self,message,epoch,revision):
        waiting = {"id": message["id"], "step": message["step"], "event": threading.Event(), "ok": False,
                   'deadline':time.monotonic()+10}
        with self.lock:
            if not self.active or epoch != self.epoch or revision != self.revision:
                return None
            self._acks[(message['id'],message['step'])]=waiting
        if not self.send(message):
            with self.lock:self._acks.pop((message['id'],message['step']),None)
            return None
        return waiting

    def _wait_ack(self,waiting,epoch,revision):
        if waiting is None:return False
        try:
            while not waiting['event'].wait(.05):
                if (self.closed.is_set() or not self.active or epoch!=self.epoch
                        or revision!=self.revision or time.monotonic()>waiting['deadline']):return False
            return waiting['ok'] and self.active and epoch==self.epoch and revision==self.revision
        finally:
            with self.lock:self._acks.pop((waiting['id'],waiting['step']),None)

    def _send_wait(self,message,epoch,revision):
        return self._wait_ack(self._begin_wait(message,epoch,revision),epoch,revision)

    def _sources(self, snapshot):
        kind = snapshot["kind"]
        if kind in ("text", "image"):
            data = snapshot["text"].encode("utf-8") if kind == "text" else snapshot["data"]
            limit = MAX_TEXT if kind == "text" else MAX_IMAGE
            if len(data) > limit:
                raise ValueError("Clipboard content too large")
            return [{"name": "content", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}], [data]
        paths = snapshot["paths"]
        if kind != "files" or not 1 <= len(paths) <= MAX_FILES:
            raise ValueError("Invalid file list")
        entries, sources, names, total = [], [], set(), 0
        for raw in paths:
            path = Path(raw)
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise ValueError("Only regular files can be copied")
            size = path.stat().st_size
            if size > MAX_FILE:
                raise ValueError("File too large")
            name = path.name
            if not self._valid_name(name):
                raise ValueError("Unsupported file name")
            candidate, index = name, 2
            while candidate.casefold() in names:
                candidate = f"{path.stem} ({index}){path.suffix}"
                index += 1
            if not self._valid_name(candidate):
                raise ValueError("Unsupported file name")
            names.add(candidate.casefold())
            total += size
            if total > MAX_TOTAL:
                raise ValueError("Clipboard files too large")
            digest = hashlib.sha256()
            actual = 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    actual += len(chunk)
                    if actual > size:
                        raise ValueError("File changed while copying")
                    digest.update(chunk)
                    if self.closed.is_set() or not self.active:
                        raise ValueError("Transfer cancelled")
            if actual != size:
                raise ValueError("File changed while copying")
            entries.append({"name": candidate, "size": size, "sha256": digest.hexdigest()})
            sources.append(path)
        return entries, sources

    def _send_loop(self):
        while not self.closed.is_set():
            try:
                job = self._jobs.get(timeout=.1)
            except queue.Empty:
                continue
            epoch, revision, snapshot = job
            ident = uuid.uuid4().hex
            try:
                snapshot = getattr(self.native, "prepare_snapshot", lambda value: value)(snapshot)
                entries, sources = self._sources(snapshot)
                self._status("正在同步剪贴板文件……" if snapshot["kind"] == "files" else "正在同步剪贴板……")
                offer = {"type": "clipboard_offer", "id": ident, "step": "offer", "revision": list(revision),
                         "format": snapshot["kind"], "entries": entries}
                if not self._send_wait(offer, epoch, revision):
                    raise ValueError("Transfer cancelled")
                step = 0
                pending=deque()
                for index, source in enumerate(sources):
                    stream = io.BytesIO(source) if isinstance(source, bytes) else source.open("rb")
                    with stream:
                        offset = 0
                        for data in iter(lambda: stream.read(CHUNK), b""):
                            message = {"type": "clipboard_chunk", "id": ident, "step": step,
                                       "entry": index, "offset": offset, "data": base64.b64encode(data).decode("ascii")}
                            waiting=self._begin_wait(message,epoch,revision)
                            if waiting is None:
                                raise ValueError("Transfer cancelled")
                            pending.append(waiting)
                            if len(pending)>=WINDOW and not self._wait_ack(pending.popleft(),epoch,revision):
                                raise ValueError('Transfer cancelled')
                            offset += len(data)
                            step += 1
                while pending:
                    if not self._wait_ack(pending.popleft(),epoch,revision):raise ValueError('Transfer cancelled')
                if not self._send_wait({"type": "clipboard_end", "id": ident, "step": "end"}, epoch, revision):
                    raise ValueError("Transfer cancelled")
            except Exception:
                self.send({"type": "clipboard_cancel", "id": ident})
                if self.active and epoch == self.epoch:
                    with self.lock:
                        if revision == self.revision:
                            self._failed_revision = revision
                            self._fingerprint = None  # A later forced paste may retry.
                    self._status("剪贴板未同步：仅支持文本、图片和普通文件（最多64个、单个256 MiB、合计1 GiB）。")
            finally:
                with self.lock:
                    if self._outgoing is job:
                        self._outgoing = None
                    for key in list(self._acks):
                        if key[0]==ident:self._acks.pop(key,None)

    def _discard_incoming(self):
        with self.lock:
            item, self._incoming = self._incoming, None
        if item:
            for stream in item["streams"]:
                stream.close()
            # Only these newly created private paths are eligible for cleanup.
            if item.get("folder") and not item.get("committed"):
                for path in item["paths"]:
                    path.unlink(missing_ok=True)
                item["folder"].rmdir()

    def _offer(self, epoch, message):
        kind, rows, revision = message.get("format"), message.get("entries"), message.get("revision")
        if (kind not in ("text", "image", "files") or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES
                or not isinstance(revision, list) or len(revision) != 2 or type(revision[0]) is not int
                or not 0 < revision[0] < 2**63 or not isinstance(revision[1], str) or not 0 < len(revision[1]) <= 100
                or (kind != "files" and len(rows) != 1)):
            raise ValueError("Invalid clipboard offer")
        total, names = 0, set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"name", "size", "sha256"}:
                raise ValueError("Invalid file metadata")
            name, size, digest = row["name"], row["size"], row["sha256"]
            if (not self._valid_name(name) or name.casefold() in names or type(size) is not int or not 0 <= size <= MAX_FILE
                    or not isinstance(digest, str) or not _HASH.fullmatch(digest)):
                raise ValueError("Invalid file metadata")
            names.add(name.casefold())
            total += size
        if total > MAX_TOTAL or kind == "image" and total > MAX_IMAGE or kind == "text" and total > MAX_TEXT:
            raise ValueError("Clipboard too large")
        with self.lock:
            incoming_revision = tuple(revision)
            self.counter = max(self.counter, revision[0])
            if incoming_revision <= self.revision:
                raise ValueError("Superseded clipboard")
            self.revision = incoming_revision
        self._discard_incoming()
        streams, paths, folder = [], [], None
        try:
            if kind == "files":
                if self.received.is_symlink():
                    raise ValueError("Invalid receive directory")
                self.received.mkdir(parents=True, exist_ok=True)
                os.chmod(self.received, 0o700)
                if shutil.disk_usage(self.received).free < total + 512 * 1024 * 1024:
                    raise ValueError("Insufficient receive space")
                folder = Path(tempfile.mkdtemp(prefix="copy-", dir=self.received))
                for row in rows:
                    path = folder / row["name"]
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    paths.append(path)
                    streams.append(os.fdopen(descriptor, "wb"))
            else:
                streams = [io.BytesIO()]
            item = {"id": message["id"], "epoch": epoch, "revision": incoming_revision,
                    "kind": kind, "rows": rows, "streams": streams, "paths": paths, "folder": folder,
                    "counts": [0] * len(rows), "hashes": [hashlib.sha256() for _ in rows], "step": 0,
                    "touched": time.monotonic(), "committed": False}
            with self.lock:
                if not self.active or epoch != self.epoch or incoming_revision != self.revision:
                    raise ValueError("Clipboard superseded")
                self._incoming = item
        except Exception:
            for stream in streams:
                stream.close()
            for path in paths:
                path.unlink(missing_ok=True)
            if folder:
                folder.rmdir()
            raise
        self._ack(message["id"], "offer")

    def _without_state_lock(self, action):
        # The single receive worker holds exactly one outer lock here. File IO
        # and codecs cannot hold the lock that the GUI uses for short snapshots.
        self.lock.release()
        try:
            return action()
        finally:
            self.lock.acquire()

    def _prepare_received(self, item):
        if item["kind"] == "files":
            for stream in item["streams"]:
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
            snapshot = {"kind": "files", "paths": list(item["paths"])}
        else:
            data = item["streams"][0].getvalue()
            snapshot = ({"kind": "text", "text": data.decode("utf-8")} if item["kind"] == "text"
                        else {"kind": "image", "data": data})
        prepare = getattr(self.native, "prepare_write", None)
        return snapshot, prepare(snapshot) if prepare else None

    def _receive_loop(self):
        while not self.closed.is_set():
            try:
                epoch, message = self._inbox.get(timeout=.1)
            except queue.Empty:
                item = self._incoming
                if item and (not self.active or item["epoch"] != self.epoch or time.monotonic() - item["touched"] > 15):
                    self._discard_incoming()
                continue
            if not self.active or epoch != self.epoch:
                continue
            ident, kind = message["id"], message["type"]
            self.lock.acquire()
            try:
                if kind == "clipboard_offer":
                    self._without_state_lock(lambda: self._offer(epoch, message))
                    continue
                item = self._incoming
                if not item or item["id"] != ident:
                    continue
                if kind == "clipboard_cancel":
                    self._without_state_lock(self._discard_incoming)
                    continue
                if item["epoch"] != epoch or item["revision"] != self.revision:
                    raise ValueError("Superseded clipboard")
                item["touched"] = time.monotonic()
                if kind == "clipboard_chunk":
                    index, offset, data, step = (message.get(k) for k in ("entry", "offset", "data", "step"))
                    if (type(index) is not int or not 0 <= index < len(item["rows"]) or type(offset) is not int
                            or offset != item["counts"][index] or type(step) is not int or step != item["step"]
                            or not isinstance(data, str) or len(data) > ((CHUNK + 2) // 3) * 4):
                        raise ValueError("Invalid clipboard chunk")
                    raw = base64.b64decode(data, validate=True)
                    if not 0 < len(raw) <= CHUNK or offset + len(raw) > item["rows"][index]["size"]:
                        raise ValueError("Invalid clipboard length")
                    self._without_state_lock(lambda: item["streams"][index].write(raw))
                    if not self.active or epoch != self.epoch or item is not self._incoming or item["revision"] != self.revision:
                        raise ValueError("Clipboard superseded")
                    item["counts"][index] += len(raw)
                    item["hashes"][index].update(raw)
                    item["step"] += 1
                    self._progress_at=time.monotonic()
                    self._ack(ident, step)
                elif kind == "clipboard_end":
                    if any(count != row["size"] or digest.hexdigest() != row["sha256"]
                           for count, row, digest in zip(item["counts"], item["rows"], item["hashes"])):
                        raise ValueError("Clipboard checksum mismatch")
                    # Pillow decoding/encoding is pure computation. Keep it off
                    # the GUI thread and do not hold the GUI-facing state lock.
                    snapshot, prepared = self._without_state_lock(lambda: self._prepare_received(item))
                    if item is not self._incoming or item["epoch"] != self.epoch or item["revision"] != self.revision:
                        raise ValueError("Clipboard superseded")
                    self._main.put_nowait(("apply", (item, snapshot, prepared)))
            except Exception:
                self._ack(ident, message.get("step"), False)
                self._without_state_lock(self._discard_incoming)
            finally:
                self.lock.release()
        self._discard_incoming()

    def _apply(self, payload):
        with self.lock:
            self._apply_locked(payload)

    def _apply_locked(self, payload):
        item, snapshot, prepared = payload
        try:
            if not self.active or item["epoch"] != self.epoch or item is not self._incoming or item["revision"] != self.revision:
                raise ValueError("Clipboard superseded")
            if prepared is not None:
                self.native.write_prepared(prepared)
                snapshot = prepared.snapshot
            else:
                self.native.write(snapshot)
            item["committed"] = True
            self._snapshot_failed=False
            self._seen_token = self.native.get_change_token()
            self._applied_token = self._seen_token
            self._fingerprint = self._snapshot_hash(snapshot)
            self._ack(item["id"], "end")
            self._status("文件已同步，可以粘贴。" if item["kind"] == "files" else "剪贴板已同步。")
        except Exception:
            self._ack(item["id"], "end", False)
            self._status("剪贴板写入失败，请重新复制后再试。")
        finally:
            if self._incoming is item:
                self._discard_incoming()

    def close(self):
        self.active = False
        self._reset()
        self.closed.set()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(1)
