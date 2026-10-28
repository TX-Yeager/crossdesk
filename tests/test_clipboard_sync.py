"""Clipboard protocol tests with fake native clipboards and an in-memory wire."""
from __future__ import annotations

import base64
import copy
import hashlib
from io import BytesIO
import os
from pathlib import Path
import queue
import threading
import time
import tempfile
import unittest
from unittest.mock import patch
import uuid
from types import SimpleNamespace

from crossdesk.clipboard_sync import CHUNK, WINDOW, ClipboardSync


class FakeNativeClipboard:
    def __init__(self):
        self.token = 0
        self.snapshot = None
        self.writes = []
        self.threads = []
        self.reencode = False

    def _main(self):
        self.threads.append(threading.get_ident())
        if threading.current_thread() is not threading.main_thread():
            raise AssertionError("Native clipboard accessed outside GUI thread")

    def get_change_token(self):
        self._main()
        return self.token

    def read(self):
        self._main()
        return copy.deepcopy(self.snapshot)

    def write(self, snapshot):
        self._main()
        self.snapshot = copy.deepcopy(snapshot)
        self.writes.append(copy.deepcopy(snapshot))
        if self.reencode and snapshot["kind"] == "image":
            self.snapshot["data"] += b"different-native-PNG-encoding"
        self.token += 1

    def user_copy(self, snapshot):
        self._main()
        self.snapshot = copy.deepcopy(snapshot)
        self.token += 1


class MemoryWire:
    def __init__(self):
        self.peers = []
        self.messages = []
        self.held = []
        self.hold_ack = False
        self.hold_chunk_ack = False
        self.fail = False
        self.errors = []
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="Test-clipboard-wire", daemon=True)
        self.thread.start()

    def send(self, source, message):
        self.messages.append((source, copy.deepcopy(message)))
        if self.fail:
            return False
        item = (1-source, copy.deepcopy(message))
        if message["type"] == "clipboard_ack" and (self.hold_ack or self.hold_chunk_ack and type(message.get("step")) is int):
            self.held.append(item)
        else:
            self.queue.put(item)
        return True

    def release(self):
        self.hold_ack = False
        self.hold_chunk_ack = False
        held, self.held = self.held, []
        for item in held:
            self.queue.put(item)

    def run(self):
        while not self.stop.is_set():
            try:
                target, message = self.queue.get(timeout=.03)
            except queue.Empty:
                continue
            try:
                self.peers[target].receive(message)
            except Exception as exc:
                self.errors.append(exc)

    def close(self):
        self.stop.set()
        self.thread.join(1)


class ClipboardSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crossdesk-clipboard-test-")
        self.root = Path(self.temporary.name)
        self.brokers, self.wires = [], []

    def tearDown(self):
        for broker in self.brokers:
            broker.close()
            self.assertTrue(all(not thread.is_alive() for thread in broker._threads))
        for wire in self.wires:
            wire.close()
            self.assertEqual(wire.errors, [])
        self.temporary.cleanup()

    def wait(self, predicate, *, poll=True, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if poll:
                for broker in self.brokers:
                    broker._next_poll = 0
                    broker.poll(active=broker.active)
            if predicate():
                return
            time.sleep(.005)
        self.fail("Clipboard protocol condition timed out")

    def single(self):
        native, sent = FakeNativeClipboard(), []
        broker = ClipboardSync(native, self.root / str(len(self.brokers)), "receiver", sent.append)
        self.brokers.append(broker)
        broker.poll(active=True)
        return broker, native, sent

    def pair(self, factory=FakeNativeClipboard):
        wire = MemoryWire()
        self.wires.append(wire)
        natives = [factory(), factory()]
        brokers = [ClipboardSync(natives[index], self.root / str(len(self.brokers)+index), f"machine-{index}",
                                 lambda message, index=index: wire.send(index, message)) for index in range(2)]
        wire.peers = brokers
        self.brokers.extend(brokers)
        for broker in brokers:
            broker.poll(active=True)
        return brokers, natives, wire

    @staticmethod
    def offer(data, *, kind="text", revision=1, ident=None, name="content", digest=None):
        return {"type": "clipboard_offer", "id": ident or uuid.uuid4().hex, "step": "offer",
                "revision": [revision, "sender"], "format": kind,
                "entries": [{"name": name, "size": len(data), "sha256": digest or hashlib.sha256(data).hexdigest()}]}

    def accepted_offer(self, broker, sent, data, **kwargs):
        offer = self.offer(data, **kwargs)
        broker.receive(offer)
        self.wait(lambda: any(message.get("id") == offer["id"] and message.get("step") == "offer" for message in sent), poll=False)
        self.assertTrue(next(message["ok"] for message in sent if message.get("id") == offer["id"] and message.get("step") == "offer"))
        return offer

    def chunks(self, broker, sent, offer, data):
        for step, offset in enumerate(range(0, len(data), CHUNK)):
            broker.receive({"type": "clipboard_chunk", "id": offer["id"], "step": step, "entry": 0,
                            "offset": offset, "data": base64.b64encode(data[offset:offset+CHUNK]).decode("ascii")})
            self.wait(lambda: any(message.get("id") == offer["id"] and message.get("step") == step for message in sent), poll=False)
        broker.receive({"type": "clipboard_end", "id": offer["id"], "step": "end"})

    def test_text_end_to_end_unicode_and_no_clipboard_echo(self):
        brokers, natives, wire = self.pair()
        snapshot = {"kind": "text", "text": "中文与 emoji 🖱️\nCrossDesk"}
        natives[0].user_copy(snapshot)
        self.wait(lambda: natives[1].writes)
        self.assertEqual(natives[1].writes, [snapshot])
        self.wait(lambda: brokers[0]._outgoing is None)
        brokers[1].force_snapshot()
        brokers[1].poll(active=True)
        time.sleep(.03)
        self.assertFalse(any(source == 1 and message["type"] == "clipboard_offer" for source, message in wire.messages))
        self.assertTrue(all(ident == threading.main_thread().ident for native in natives for ident in native.threads))

    def test_image_end_to_end_and_native_reencoding_does_not_echo_on_force(self):
        from PIL import Image
        with Image.new("RGBA", (3, 2), (30, 90, 200, 127)) as image:
            stream = BytesIO()
            image.save(stream, format="PNG")
        brokers, natives, wire = self.pair()
        natives[1].reencode = True
        snapshot = {"kind": "image", "data": stream.getvalue()}
        natives[0].user_copy(snapshot)
        self.wait(lambda: natives[1].writes)
        self.assertEqual(natives[1].writes, [snapshot])
        self.wait(lambda: brokers[0]._outgoing is None)
        brokers[1].force_snapshot()
        brokers[1].poll(active=True)
        time.sleep(.03)
        self.assertFalse(any(source == 1 and message["type"] == "clipboard_offer" for source, message in wire.messages))

    def test_files_end_to_end_duplicate_names_private_destination_and_empty_file(self):
        brokers, natives, _ = self.pair()
        first, second = self.root / "a", self.root / "b"
        first.mkdir(); second.mkdir()
        (first / "same.txt").write_bytes(b"first content")
        (second / "same.txt").write_bytes(b"second content")
        (first / "empty.bin").write_bytes(b"")
        sources = [first / "same.txt", second / "same.txt", first / "empty.bin"]
        natives[0].user_copy({"kind": "files", "paths": sources})
        self.wait(lambda: natives[1].writes)
        paths = natives[1].writes[0]["paths"]
        self.assertEqual([path.name for path in paths], ["same.txt", "same (2).txt", "empty.bin"])
        self.assertEqual([path.read_bytes() for path in paths], [b"first content", b"second content", b""])
        self.assertTrue(all(path.is_relative_to(brokers[1].received) for path in paths))
        if os.name != "nt":
            self.assertTrue(all(path.stat().st_mode & 0o077 == 0 for path in paths))

    def test_same_path_recopy_after_file_edit_sends_new_contents(self):
        brokers, natives, _ = self.pair()
        path = self.root / "edited.txt"
        path.write_bytes(b"before")
        snapshot = {"kind": "files", "paths": [path]}
        natives[0].user_copy(snapshot)
        self.wait(lambda: natives[1].writes)
        self.wait(lambda: brokers[0]._outgoing is None)
        path.write_bytes(b"after, same original file path")
        natives[0].user_copy(snapshot)
        self.wait(lambda: len(natives[1].writes) == 2)
        self.assertEqual(natives[1].writes[-1]["paths"][0].read_bytes(), path.read_bytes())

    def test_waiting_for_ack_never_floods_chunks_or_transport_queue(self):
        brokers, natives, wire = self.pair()
        wire.hold_ack = True
        path = self.root / "several-chunks.bin"
        path.write_bytes(b"x" * (CHUNK * 5 + 13))
        natives[0].user_copy({"kind": "files", "paths": [path]})
        self.wait(lambda: wire.held)
        time.sleep(.05)
        self.assertEqual(sum(message["type"] == "clipboard_offer" for _, message in wire.messages), 1)
        self.assertEqual(sum(message["type"] == "clipboard_chunk" for _, message in wire.messages), 0)
        wire.release()
        self.wait(lambda: natives[1].writes)
        self.assertEqual(natives[1].writes[0]["paths"][0].stat().st_size, path.stat().st_size)

    def test_data_window_is_bounded_and_completes_after_ack(self):
        _, natives, wire = self.pair()
        wire.hold_chunk_ack = True
        natives[0].user_copy({"kind": "text", "text": "x" * (CHUNK * (WINDOW+3) + 1)})
        self.wait(lambda: wire.held)
        time.sleep(.04)
        chunks = [message for _, message in wire.messages if message["type"] == "clipboard_chunk"]
        self.assertEqual(len(chunks), WINDOW)
        wire.release()
        self.wait(lambda: natives[1].writes)

    def test_one_mebibyte_text_is_chunked_and_delivered(self):
        _, natives, wire = self.pair()
        text = "大" * ((1024 * 1024) // 3)
        natives[0].user_copy({"kind": "text", "text": text})
        self.wait(lambda: natives[1].writes)
        self.assertEqual(natives[1].writes[0]["text"], text)
        self.assertGreater(sum(message["type"] == "clipboard_chunk" for _, message in wire.messages), 40)

    def test_codec_preparation_runs_in_workers_and_only_native_calls_use_gui(self):
        class PreparedNative(FakeNativeClipboard):
            def __init__(self):
                super().__init__()
                self.preparations = []
            def read_raw(self):
                return self.read()
            def prepare_snapshot(self, raw):
                self.preparations.append(threading.get_ident())
                self.assert_worker()
                return raw
            def prepare_write(self, snapshot):
                self.preparations.append(threading.get_ident())
                self.assert_worker()
                return SimpleNamespace(snapshot=snapshot)
            def write_prepared(self, prepared):
                self.write(prepared.snapshot)
            @staticmethod
            def assert_worker():
                if threading.current_thread() is threading.main_thread():
                    raise AssertionError("Codec ran on GUI thread")
        _, natives, _ = self.pair(PreparedNative)
        natives[0].user_copy({"kind": "image", "data": b"fake normalized image bytes"})
        self.wait(lambda: natives[1].writes)
        self.assertTrue(natives[0].preparations and natives[1].preparations)
        self.assertTrue(all(ident != threading.main_thread().ident for native in natives for ident in native.preparations))
        self.assertTrue(all(ident == threading.main_thread().ident for native in natives for ident in native.threads))

    def test_slow_codec_does_not_hold_gui_poll_state_lock(self):
        broker, native, sent = self.single()
        entered, release = threading.Event(), threading.Event()
        def prepare(snapshot):
            entered.set()
            release.wait(1)
            return SimpleNamespace(snapshot=snapshot)
        native.prepare_write = prepare
        native.write_prepared = lambda prepared: native.write(prepared.snapshot)
        offer = self.accepted_offer(broker, sent, b"image", kind="image")
        self.chunks(broker, sent, offer, b"image")
        self.assertTrue(entered.wait(1))
        watchdog = threading.Timer(.5, release.set)
        watchdog.start()
        try:
            started = time.monotonic()
            broker.poll(active=True)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, .2)
        finally:
            release.set()
            watchdog.cancel()
        self.wait(lambda: native.writes)

    def test_real_pure_codecs_connect_windows_formats_and_mac_png_without_native_access(self):
        from PIL import Image
        from crossdesk.clipboard_native import prepare_snapshot, prepare_write
        platforms = iter(("win32", "darwin"))
        class CodecNative(FakeNativeClipboard):
            def __init__(self):
                super().__init__()
                self.platform = next(platforms)
            def read_raw(self):
                return self.read()
            def prepare_snapshot(self, raw):
                return prepare_snapshot(raw, platform=self.platform)
            def prepare_write(self, snapshot):
                return prepare_write(snapshot, platform=self.platform)
            def write_prepared(self, prepared):
                if prepared.platform != self.platform:
                    raise AssertionError("Wrong native clipboard platform")
                self.write(prepared.snapshot)
        _, natives, _ = self.pair(CodecNative)
        with Image.new("RGBA", (3, 2), (10, 70, 180, 120)) as image:
            stream = BytesIO()
            image.save(stream, format="PNG")
        natives[0].user_copy({"kind": "image", "data": stream.getvalue(), "encoding": "png"})
        self.wait(lambda: natives[1].writes)
        self.assertEqual(natives[1].writes[0]["kind"], "image")
        with Image.open(BytesIO(natives[1].writes[0]["data"])) as decoded:
            self.assertEqual(decoded.getpixel((0, 0)), (10, 70, 180, 120))

    def test_slow_received_file_write_does_not_hold_gui_poll_state_lock(self):
        broker, native, sent = self.single()
        entered, release = threading.Event(), threading.Event()
        offer = self.accepted_offer(broker, sent, b"file", kind="files", name="safe.bin")
        original = broker._incoming["streams"][0]
        class SlowStream:
            def write(self, value):
                entered.set()
                release.wait(1)
                return original.write(value)
            def __getattr__(self, name):
                return getattr(original, name)
        broker._incoming["streams"][0] = SlowStream()
        broker.receive({"type": "clipboard_chunk", "id": offer["id"], "step": 0, "entry": 0,
                        "offset": 0, "data": base64.b64encode(b"file").decode()})
        self.assertTrue(entered.wait(1))
        watchdog = threading.Timer(.5, release.set)
        watchdog.start()
        try:
            started = time.monotonic()
            broker.poll(active=True)
            self.assertLess(time.monotonic() - started, .2)
        finally:
            release.set()
            watchdog.cancel()
        self.wait(lambda: any(message.get("step") == 0 for message in sent), poll=False)
        broker.receive({"type": "clipboard_end", "id": offer["id"], "step": "end"})
        self.wait(lambda: native.writes)

    def test_file_commit_is_preserved_if_native_token_query_fails_after_write(self):
        broker, native, sent = self.single()
        offer = self.accepted_offer(broker, sent, b"file", kind="files", name="safe.bin")
        get_token = native.get_change_token
        def fail_after_write():
            if native.writes:
                raise RuntimeError("isolated token failure")
            return get_token()
        native.get_change_token = fail_after_write
        self.chunks(broker, sent, offer, b"file")
        self.wait(lambda: native.writes)
        self.assertEqual(native.writes[0]["paths"][0].read_bytes(), b"file")
        self.assertTrue(any(message.get("step") == "end" and message.get("ok") is False for message in sent))

    def test_remote_paste_waits_for_receiver_native_commit_and_ack(self):
        brokers, natives, wire = self.pair()
        wire.hold_ack = True
        natives[0].user_copy({"kind": "text", "text": "pending paste"})
        done = []
        brokers[0].before_paste("machine-1", done.append)
        self.wait(lambda: wire.held)
        self.assertEqual(done, [])
        self.assertEqual(natives[1].writes, [])
        wire.release()
        self.wait(lambda: done)
        self.assertEqual(done, [True])
        self.assertEqual(natives[1].writes[-1]["text"], "pending paste")

    def test_local_paste_requests_latest_peer_clipboard_before_ready(self):
        brokers, natives, _ = self.pair()
        natives[1].user_copy({"kind": "text", "text": "peer's new clipboard"})
        done = []
        brokers[0].before_paste("machine-0", done.append)
        self.wait(lambda: done)
        self.assertEqual(done, [True])
        self.assertEqual(natives[0].writes[-1]["text"], "peer's new clipboard")

    def test_failed_send_rejects_paste_and_next_forced_paste_retries(self):
        brokers, natives, wire = self.pair()
        wire.fail = True
        natives[0].user_copy({"kind": "text", "text": "must reach receiver"})
        failed = []
        brokers[0].before_paste("machine-1", failed.append)
        self.wait(lambda: failed)
        self.assertEqual(failed, [False])
        self.assertEqual(natives[1].writes, [])
        wire.fail = False
        retried = []
        brokers[0].before_paste("machine-1", retried.append)
        self.wait(lambda: retried)
        self.assertEqual(retried, [True])
        self.assertEqual(natives[1].writes[-1]["text"], "must reach receiver")

    def test_pause_and_explicit_cancel_never_release_pending_paste(self):
        brokers, natives, wire = self.pair()
        wire.hold_ack = True
        natives[0].user_copy({"kind": "text", "text": "cancelled"})
        done = []
        cancel = brokers[0].before_paste("machine-1", done.append)
        self.wait(lambda: wire.held)
        cancel()
        brokers[0].poll(active=True)
        self.assertEqual(done, [])
        paused = []
        brokers[0].before_paste("machine-1", paused.append)
        brokers[0].poll(active=True)
        brokers[0].poll(active=False)
        self.assertEqual(paused, [False])
        wire.release()
        self.wait(lambda: brokers[0]._outgoing is None)
        self.assertFalse(any(done))

    def test_new_copy_cancels_old_paste_while_superseding_old_transfer(self):
        brokers, natives, wire = self.pair()
        wire.hold_ack = True
        natives[0].user_copy({"kind": "text", "text": "old copy"})
        done = []
        brokers[0].before_paste("machine-1", done.append)
        self.wait(lambda: wire.held)
        natives[0].user_copy({"kind": "text", "text": "new copy"})
        brokers[0]._next_poll = 0
        brokers[0].poll(active=True)
        self.assertEqual(done, [False])
        wire.release()
        self.wait(lambda: natives[1].writes and natives[1].writes[-1]["text"] == "new copy")

    def test_poll_rejects_background_thread_before_native_or_state_changes(self):
        broker, native, _ = self.single()
        calls = len(native.threads)
        failures = []
        def background():
            try:
                broker.poll(active=False)
            except RuntimeError as exc:
                failures.append(str(exc))
        thread = threading.Thread(target=background)
        thread.start(); thread.join()
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(native.threads), calls)
        self.assertTrue(broker.active)

    def test_malicious_file_names_and_metadata_fail_before_creating_files(self):
        broker, native, sent = self.single()
        for name in ("..", "../escape", "a/b", "a\\b", "CON.txt", "NUL", "bad.", "bad ", "bad:stream", "control\0name"):
            offer = self.offer(b"payload", kind="files", name=name)
            broker.receive(offer)
            self.wait(lambda: any(message.get("id") == offer["id"] for message in sent), poll=False)
            self.assertFalse(next(message["ok"] for message in sent if message.get("id") == offer["id"]))
        for value in (True, -1, 2**40):
            offer = self.offer(b"payload", kind="files")
            offer["entries"][0]["size"] = value
            broker.receive(offer)
            self.wait(lambda: any(message.get("id") == offer["id"] for message in sent), poll=False)
            self.assertFalse(next(message["ok"] for message in sent if message.get("id") == offer["id"]))
        self.assertFalse(broker.received.exists())
        self.assertEqual(native.writes, [])

    def test_hash_failure_never_writes_native_and_removes_partial_files(self):
        broker, native, sent = self.single()
        data = b"this data does not match the promised hash"
        offer = self.accepted_offer(broker, sent, data, kind="files", name="safe.txt", digest="0" * 64)
        self.chunks(broker, sent, offer, data)
        self.wait(lambda: any(message.get("step") == "end" for message in sent))
        self.assertFalse(next(message["ok"] for message in sent if message.get("step") == "end"))
        self.assertEqual(native.writes, [])
        self.assertEqual(list(broker.received.iterdir()), [])

    def test_stale_apply_cannot_destroy_newer_incoming_transfer(self):
        broker, native, sent = self.single()
        first = self.accepted_offer(broker, sent, b"older", revision=1)
        self.chunks(broker, sent, first, b"older")
        self.wait(lambda: broker._main.qsize() == 1, poll=False)
        newer = self.accepted_offer(broker, sent, b"newest", revision=2)
        broker.poll(active=True)  # Dequeues only the obsolete apply.
        self.assertIsNotNone(broker._incoming)
        self.assertEqual(broker._incoming["id"], newer["id"])
        self.assertEqual(native.writes, [])
        self.chunks(broker, sent, newer, b"newest")
        self.wait(lambda: native.writes)
        self.assertEqual(native.writes, [{"kind": "text", "text": "newest"}])

    def test_bad_offsets_steps_and_base64_are_rejected_without_native_write(self):
        broker, native, sent = self.single()
        for revision, changes in enumerate(({"offset": 1}, {"step": 1}, {"entry": True}, {"data": "!!bad!!"}), 1):
            offer = self.accepted_offer(broker, sent, b"valid", revision=revision)
            message = {"type": "clipboard_chunk", "id": offer["id"], "step": 0, "entry": 0,
                       "offset": 0, "data": base64.b64encode(b"valid").decode()}
            message.update(changes)
            broker.receive(message)
            self.wait(lambda: any(ack.get("id") == offer["id"] and ack.get("ok") is False for ack in sent), poll=False)
        self.assertEqual(native.writes, [])

    def test_foreign_ack_and_nack_do_not_complete_other_transfer(self):
        broker, native, sent = self.single()
        native.user_copy({"kind": "text", "text": "waiting"})
        broker._next_poll = 0
        broker.poll(active=True)
        self.wait(lambda: any(message["type"] == "clipboard_offer" for message in sent), poll=False)
        offer = next(message for message in sent if message["type"] == "clipboard_offer")
        broker.receive({"type": "clipboard_ack", "id": uuid.uuid4().hex, "step": "offer", "ok": True})
        time.sleep(.03)
        self.assertFalse(any(message["type"] == "clipboard_chunk" for message in sent))
        broker.receive({"type": "clipboard_ack", "id": offer["id"], "step": "offer", "ok": False})
        self.wait(lambda: broker._outgoing is None, poll=False)
        self.assertEqual(broker._failed_revision, broker.revision)


if __name__ == "__main__":
    unittest.main()
