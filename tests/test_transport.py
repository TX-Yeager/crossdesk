"""Real loopback TLS tests. No LAN listener, hooks, firewall or service changes."""
import base64
import json
from pathlib import Path
import queue
import socket
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from crossdesk.transport import (
    MAX_FRAME, PAIRING_PREFIX, PeerTransport, ProtocolError,
    _envelope, _pack, _parse_pairing_code, _recv_frame,
)


def wait_for(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


def changed_code(code, **fields):
    value = _parse_pairing_code(code)
    value.update(fields)
    return PAIRING_PREFIX + base64.urlsafe_b64encode(
        json.dumps(value).encode()).decode().rstrip("=")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crossdesk-test-")
        self.peers = []
        self.sockets = []

    def tearDown(self):
        for sock in self.sockets:
            try:
                sock.close()
            except OSError:
                pass
        for peer in reversed(self.peers):
            peer.stop()
        self.temporary.cleanup()

    def peer(self):
        messages, statuses = [], []
        state = Path(self.temporary.name) / str(len(self.peers))
        peer = PeerTransport(state, messages.append, statuses.append)
        self.peers.append(peer)
        return peer, messages, statuses

    def pair(self):
        listener, left, left_status = self.peer()
        connector, right, right_status = self.peer()
        code = listener.listen("127.0.0.1", 0)
        connector.connect(code)
        wait_for(lambda: listener.connected and connector.connected)
        return listener, connector, left, right, left_status, right_status

    def raw_peer(self, listener, code):
        """An authenticated peer whose wire frames the test controls."""
        helper, _, _ = self.peer()
        raw = socket.create_connection(("127.0.0.1", listener.bound_port), 2)
        secure = helper._client_context().wrap_socket(raw, server_hostname=None)
        self.sockets.append(secure)
        helper._authenticate_client(secure, _parse_pairing_code(code))
        wait_for(lambda: listener.connected)
        return secure

    def test_real_tls_full_duplex_preserves_order(self):
        listener, connector, left, right, _, _ = self.pair()
        self.assertTrue(listener.is_listener)
        self.assertFalse(connector.is_listener)
        self.assertNotEqual(listener.fingerprint, connector.fingerprint)
        self.assertGreater(listener.bound_port, 0)
        for number in range(30):
            self.assertTrue(listener.send({"type": "test", "side": "left", "n": number}))
            self.assertTrue(connector.send({"type": "test", "side": "right", "n": number}))
        wait_for(lambda: len(left) == len(right) == 30)
        self.assertEqual([item["n"] for item in left], list(range(30)))
        self.assertEqual([item["n"] for item in right], list(range(30)))
        self.assertEqual({item["side"] for item in left}, {"right"})
        self.assertEqual({item["side"] for item in right}, {"left"})

    def test_both_tls_directions_send_small_input_packets_without_nagle(self):
        listener,connector,*_=self.pair()
        # Darwin returns the enabled flag bit (4), Windows returns 1.
        self.assertNotEqual(listener._active_socket.getsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY),0)
        self.assertNotEqual(connector._active_socket.getsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY),0)
        self.assertIsNotNone(listener._send_wakeup)
        self.assertIsNotNone(connector._send_wakeup)

    def test_wrong_pin_rejected_before_authentication(self):
        listener, left, _ = self.peer()
        connector, right, status = self.peer()
        code = listener.listen("127.0.0.1", 0)
        connector.connect(changed_code(code, pin="00" * 32))
        wait_for(lambda: any("证书指纹" in item for item in status))
        self.assertFalse(listener.connected)
        self.assertFalse(connector.connected)
        self.assertFalse(connector.send({"type": "key", "key": "a"}))
        self.assertEqual(left + right, [])

    def test_wrong_secret_never_establishes_input_session(self):
        listener, left, status = self.peer()
        connector, right, _ = self.peer()
        code = listener.listen("127.0.0.1", 0)
        connector.connect(changed_code(code, secret="00" * 32))
        wait_for(lambda: any("配对凭据" in item for item in status))
        self.assertFalse(listener.connected)
        self.assertFalse(connector.connected)
        self.assertEqual(left + right, [])

    def test_unauthenticated_input_is_not_dispatched(self):
        listener, messages, statuses = self.peer()
        listener.listen("127.0.0.1", 0)
        raw = socket.create_connection(("127.0.0.1", listener.bound_port), 2)
        sock = PeerTransport._client_context().wrap_socket(raw, server_hostname=None)
        self.sockets.append(sock)
        self.assertEqual(_recv_frame(sock)["kind"], "challenge")
        sock.sendall(_pack(_envelope("event", body={"type": "key", "key": "a"})))
        wait_for(lambda: any("配对认证" in item for item in statuses))
        self.assertFalse(listener.connected)
        self.assertEqual(messages, [])

    def test_oversized_header_closes_authenticated_session(self):
        listener, messages, _ = self.peer()
        code = listener.listen("127.0.0.1", 0)
        sock = self.raw_peer(listener, code)
        sock.sendall(struct.pack("!I", MAX_FRAME + 1))
        wait_for(lambda: messages == [{"type": "disconnect"}])
        self.assertFalse(listener.connected)

    def test_wrong_protocol_identity_closes_session(self):
        listener, messages, _ = self.peer()
        code = listener.listen("127.0.0.1", 0)
        sock = self.raw_peer(listener, code)
        sock.sendall(_pack({"protocol": "some-other-app", "version": 1,
                            "kind": "event", "body": {"type": "key"}}))
        wait_for(lambda: messages == [{"type": "disconnect"}])
        self.assertFalse(listener.connected)

    def test_peer_cannot_forge_local_disconnect_notification(self):
        listener, messages, _ = self.peer()
        code = listener.listen("127.0.0.1", 0)
        sock = self.raw_peer(listener, code)
        sock.sendall(_pack(_envelope("event", body={"type": "disconnect", "injected": True})))
        wait_for(lambda: messages == [{"type": "disconnect"}])

    def test_second_connection_cannot_take_over_active_peer(self):
        listener, connector, left, right, _, _ = self.pair()
        intruder = socket.create_connection(("127.0.0.1", listener.bound_port), 1)
        self.sockets.append(intruder)
        self.assertTrue(listener.connected)
        self.assertTrue(connector.send({"type": "test", "n": 7}))
        wait_for(lambda: left == [{"type": "test", "n": 7}])
        self.assertTrue(listener.send({"type": "test", "n": 8}))
        wait_for(lambda: right == [{"type": "test", "n": 8}])

    def test_stop_notifies_disconnect_and_no_stale_events_survive(self):
        listener, connector, left, right, _, _ = self.pair()
        connector.stop()
        wait_for(lambda: left == right == [{"type": "disconnect"}])
        self.assertFalse(listener.connected)
        self.assertFalse(connector.connected)
        self.assertFalse(connector.send({"type": "key"}))

    def test_queue_full_disconnect_request_is_nonblocking(self):
        peer, _, _ = self.peer()
        # No network consumer, so a one-slot queue has a deterministic overflow.
        peer._queue = queue.Queue(maxsize=1)
        peer._connected.set()
        self.assertTrue(peer.send({"type": "key", "down": True}))
        started = time.monotonic()
        self.assertFalse(peer.send({"type": "key", "down": False}))
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertFalse(peer.connected)
        self.assertTrue(peer._abort.is_set())

    def test_bounded_json_and_invalid_local_messages(self):
        peer, _, _ = self.peer()
        peer._connected.set()
        self.assertFalse(peer.send({"type": "test", "value": "x" * MAX_FRAME}))
        self.assertFalse(peer.send({"type": "test", "value": float("nan")}))
        self.assertFalse(peer.send({"value": "missing type"}))
        nested = {}
        for _ in range(20):
            nested = {"nested": nested}
        self.assertFalse(peer.send({"type": "test", "value": nested}))
        with self.assertRaises(ProtocolError):
            _pack(nested)

    def test_idle_authenticated_peer_times_out_and_releases_input(self):
        with patch("crossdesk.transport.IDLE_TIMEOUT", 0.3), patch("crossdesk.transport.HEARTBEAT_INTERVAL", 0.05):
            listener, messages, status = self.peer()
            code = listener.listen("127.0.0.1", 0)
            self.raw_peer(listener, code)  # Authenticates, but never sends pong.
            wait_for(lambda: messages == [{"type": "disconnect"}], timeout=2)
            self.assertFalse(listener.connected)
            self.assertTrue(any("超时" in item for item in status))

    def test_healthy_heartbeat_keeps_both_directions_connected(self):
        with patch("crossdesk.transport.IDLE_TIMEOUT", 0.35), patch("crossdesk.transport.HEARTBEAT_INTERVAL", 0.05):
            listener, connector, left, right, _, _ = self.pair()
            time.sleep(0.65)
            self.assertTrue(listener.connected)
            self.assertTrue(connector.connected)
            self.assertEqual(left + right, [])

    def test_connector_reconnects_after_session_loss(self):
        listener, connector, left, right, _, _ = self.pair()
        listener._abort.set()
        wait_for(lambda: {"type": "disconnect"} in left and {"type": "disconnect"} in right)
        wait_for(lambda: listener.connected and connector.connected)
        self.assertTrue(connector.send({"type": "test", "after": "reconnect"}))
        wait_for(lambda: {"type": "test", "after": "reconnect"} in left)

    def test_authenticated_packet_rate_is_bounded(self):
        with patch("crossdesk.transport.MAX_PACKETS_PER_SECOND", 4):
            listener, messages, _ = self.peer()
            code = listener.listen("127.0.0.1", 0)
            sock = self.raw_peer(listener, code)
            sock.sendall(_pack(_envelope("pong")) * 5)
            wait_for(lambda: messages == [{"type": "disconnect"}])
            self.assertFalse(listener.connected)

    def test_identity_persists_and_pairing_code_validates(self):
        peer, _, _ = self.peer()
        state = Path(self.temporary.name) / "0"
        previous = peer.fingerprint
        copy = PeerTransport(state, lambda _: None, lambda _: None)
        self.peers.append(copy)
        self.assertEqual(previous, copy.fingerprint)
        self.assertEqual(len((state / "transport-secret.bin").read_bytes()), 32)
        with self.assertRaises(ValueError):
            peer.connect("crossdesk://v1/garbage")
        with self.assertRaises(ValueError):
            peer.connect(changed_code(peer.listen("127.0.0.1", 0), host="example.com"))


if __name__ == "__main__":
    unittest.main()
