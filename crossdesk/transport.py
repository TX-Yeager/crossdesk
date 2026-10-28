"""A single-peer, authenticated LAN link for input events.

Both endpoints may send events over the same TLS connection. The network role
(listen/connect) has no relationship to which computer owns the pointer. No
input event or pairing code is written to a log or sent to a third party.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import secrets
import select
import socket
import ssl
import struct
import threading
import time
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


MAX_FRAME = 64 * 1024
MAX_DEPTH = 12
MAX_NODES = 4096
MAX_PACKETS_PER_SECOND = 12000  # Accommodate 8 kHz input plus control/ACK traffic.
QUEUE_SIZE = 2048
HANDSHAKE_TIMEOUT = 3.0
HEARTBEAT_INTERVAL = 1.0
IDLE_TIMEOUT = 6.0
PAIRING_PREFIX = "crossdesk://v1/"
_PROTOCOL = "crossdesk"
_VERSION = 1


class ProtocolError(ValueError):
    """The peer failed an identity, authentication, or framing check."""


class _QueuedFrame(tuple):
    def __new__(cls, epoch, frame, pointer_key=None, bulk=False):
        item=super().__new__(cls,(epoch,frame))
        item.pointer_key,item.bulk=pointer_key,bulk
        return item


class _SendQueue(queue.Queue):
    """Coalesce only adjacent absolute pointers; never reorder input barriers."""
    def put(self,item,block=True,timeout=None):
        with self.not_full:
            key=getattr(item,'pointer_key',None)
            if key is not None and self.queue and getattr(self.queue[-1],'pointer_key',None)==key:
                self.queue[-1]=item
                self.not_empty.notify()
                return
            if self.maxsize>0 and self._qsize()>=self.maxsize:raise queue.Full
            self._put(item)
            self.unfinished_tasks+=1
            self.not_empty.notify()

    def _get(self):
        # Chunks are independently ACKed. Input/control may bypass queued bulk,
        # but their relative ordering (focus, pointer, click, key) stays intact.
        for index,item in enumerate(self.queue):
            if not getattr(item,'bulk',False):
                del self.queue[index]
                return item
        return self.queue.popleft()


def _validate_json(value: object) -> None:
    """Bound work even for syntactically valid but hostile JSON."""
    remaining = MAX_NODES

    def visit(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if depth > MAX_DEPTH or remaining < 0:
            raise ProtocolError("消息结构超过限制")
        if item is None or isinstance(item, (bool, str)):
            return
        if isinstance(item, (int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise ProtocolError("消息含无效数字")
            return
        if isinstance(item, dict):
            if len(item) > 128 or any(not isinstance(k, str) for k in item):
                raise ProtocolError("消息字段无效")
            for child in item.values():
                visit(child, depth + 1)
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        raise ProtocolError("消息含不支持的数据类型")

    visit(value, 0)


def _pack(message: dict) -> bytes:
    _validate_json(message)
    try:
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("消息无法编码") from exc
    if not 0 < len(payload) <= MAX_FRAME:
        raise ProtocolError("消息超过 64 KiB 限制")
    return struct.pack("!I", len(payload)) + payload


def _decode(payload: bytes) -> dict:
    def unique_object(pairs: list) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError("消息含重复字段")
            result[key] = value
        return result

    try:
        message = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(
                                 ProtocolError("消息含无效数字")))
        _validate_json(message)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("收到无效消息") from exc
    if (not isinstance(message, dict) or message.get("protocol") != _PROTOCOL
            or type(message.get("version")) is not int
            or message["version"] != _VERSION):
        raise ProtocolError("对端协议身份不符")
    return message


def _envelope(kind: str, **fields: object) -> dict:
    return {"protocol": _PROTOCOL, "version": _VERSION, "kind": kind, **fields}


def _recv_frame(sock: ssl.SSLSocket, deadline: float | None = None) -> dict:
    """Used only by the bounded authentication exchange, never input hooks."""
    deadline = deadline or time.monotonic() + HANDSHAKE_TIMEOUT

    def exact(size: int) -> bytes:
        buffer = bytearray()
        while len(buffer) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("配对握手超时")
            sock.settimeout(remaining)
            chunk = sock.recv(size - len(buffer))
            if not chunk:
                raise ConnectionError("对端关闭连接")
            buffer.extend(chunk)
        return bytes(buffer)

    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ProtocolError("消息长度无效")
    return _decode(exact(size))


def _proof(secret: bytes, role: str, server_nonce: bytes, client_nonce: bytes) -> str:
    material = b"crossdesk-v1/" + role.encode("ascii") + b"\0" + server_nonce + client_nonce
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def _hex_bytes(value: object, size: int) -> bytes:
    if not isinstance(value, str) or len(value) != size * 2:
        raise ProtocolError("认证字段无效")
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise ProtocolError("认证字段无效") from exc
    if len(result) != size:
        raise ProtocolError("认证字段无效")
    return result


def _parse_pairing_code(code: str) -> dict:
    if not isinstance(code, str) or not code.startswith(PAIRING_PREFIX) or len(code) > 2048:
        raise ValueError("配对码格式无效")
    try:
        text = code[len(PAIRING_PREFIX):]
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"host", "port", "pin", "secret"}:
            raise ValueError
        ipaddress.ip_address(value["host"])
        if type(value["port"]) is not int or not 1 <= value["port"] <= 65535:
            raise ValueError
        _hex_bytes(value["pin"], 32)
        _hex_bytes(value["secret"], 32)
        return value
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("配对码格式无效") from exc


def _write_private(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _installation_identity(state_dir: Path) -> tuple[Path, Path, bytes, str]:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    certificate_path = state_dir / "transport-cert.pem"
    key_path = state_dir / "transport-key.pem"
    secret_path = state_dir / "transport-secret.bin"
    if not certificate_path.exists() and not key_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CrossDesk local peer")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(days=1))
                       .not_valid_after(now + timedelta(days=3650))
                       .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                       .sign(key, hashes.SHA256()))
        _write_private(key_path, key.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        _write_private(certificate_path, certificate.public_bytes(serialization.Encoding.PEM))
    # Never silently rotate an existing or damaged identity.
    certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    if not secret_path.exists():
        _write_private(secret_path, secrets.token_bytes(32))
    secret = secret_path.read_bytes()
    if len(secret) != 32:
        raise ValueError("本机配对凭据损坏，请在应用数据目录重置配对")
    for path in (certificate_path, key_path, secret_path):
        os.chmod(path, 0o600)
    return certificate_path, key_path, secret, certificate.fingerprint(hashes.SHA256()).hex()


def _advertised_ip(bind_host: str) -> str:
    if bind_host not in ("0.0.0.0", "::", ""):
        return str(ipaddress.ip_address(socket.gethostbyname(bind_host)))
    # Resolve local interfaces only; never contact a public service for discovery.
    try:
        addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return "127.0.0.1"
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_loopback and not address.is_link_local:
            return str(address)
    return "127.0.0.1"


class PeerTransport:
    """TLS + certificate pinning + mutual challenge authentication, one peer.

    ``send`` only validates and enqueues; a full queue requests disconnection so
    a dropped key-up cannot leave the remote computer with a held key. Callbacks
    execute in a network thread and should enqueue work, never block.
    """

    def __init__(self, state_dir: Path, on_message: Callable[[dict], None],
                 on_status: Callable[[str], None]):
        self._cert, self._key, self._secret, self._fingerprint = _installation_identity(Path(state_dir))
        self._on_message = on_message
        self._on_status = on_status
        self._queue: queue.Queue[tuple[int, bytes]] = _SendQueue(maxsize=QUEUE_SIZE)
        self._session_epoch = 0
        self._connected = threading.Event()
        self._stop_event = threading.Event()
        self._abort = threading.Event()
        self._abort_reason = "连接已取消"
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | None = None
        self._active_socket: socket.socket | None = None
        self._send_wakeup: socket.socket | None = None
        self._lifecycle_lock = threading.RLock()
        self._is_listener = False
        self.bound_port = 0

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def is_listener(self) -> bool:
        return self._is_listener

    def _status(self, text: str) -> None:
        try:
            self._on_status(text)
        except Exception:
            pass  # A UI callback must never crash connection cleanup.

    def _message(self, message: dict) -> None:
        try:
            self._on_message(message)
        except Exception as exc:
            raise ProtocolError("应用无法处理消息") from exc

    def _prepare(self) -> None:
        self.stop()
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("上一连接仍在退出，请稍后重试")
        self._stop_event = threading.Event()
        self._abort = threading.Event()
        self._queue = _SendQueue(maxsize=QUEUE_SIZE)
        self.bound_port = 0

    def listen(self, host: str = "0.0.0.0", port: int = 48193,
               advertise_host: str = "") -> str:
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("端口应在 0 到 65535 之间")
        if advertise_host:
            ipaddress.ip_address(advertise_host)
        with self._lifecycle_lock:
            self._prepare()
            self._is_listener = True
            ready = threading.Event()
            result: dict = {}
            self._thread = threading.Thread(target=self._listen_loop,
                args=(host, port, advertise_host, ready, result), name="crossdesk-listen", daemon=True)
            self._thread.start()
            if not ready.wait(HANDSHAKE_TIMEOUT + 2):
                self.stop()
                raise TimeoutError("监听启动超时")
            if "error" in result:
                self.stop()
                raise OSError(result["error"])
            return result["code"]

    def connect(self, code: str) -> None:
        peer = _parse_pairing_code(code.strip())
        with self._lifecycle_lock:
            self._prepare()
            self._is_listener = False
            self._thread = threading.Thread(target=self._connect_loop, args=(peer,),
                                            name="crossdesk-connect", daemon=True)
            self._thread.start()

    def send(self, message: dict) -> bool:
        if not self._connected.is_set():
            return False
        epoch = self._session_epoch
        outbox = self._queue
        abort = self._abort
        if (not isinstance(message, dict) or not isinstance(message.get("type"), str)
                or not 0 < len(message["type"]) <= 64 or message["type"] == "disconnect"):
            return False
        try:
            frame = _pack(_envelope("event", body=message))
            if not self._connected.is_set() or epoch != self._session_epoch:
                return False
            key=None
            if message['type']=='pointer' and message.get('coalesce') is True:
                key=(epoch,message.get('screen'),repr(message.get('focus_revision')))
            outbox.put_nowait(_QueuedFrame(epoch,frame,key,message['type']=='clipboard_chunk'))
            # Wake the selector immediately; small pointer/focus packets must
            # not wait for the network thread's periodic idle timeout.
            wakeup = self._send_wakeup
            if wakeup is not None:
                try:
                    wakeup.send(b"\0")
                except (OSError, BlockingIOError):
                    pass  # A full wake buffer is already readable.
            return True
        except queue.Full:
            if epoch == self._session_epoch:
                self._abort_reason = "发送队列已满，已断开并释放输入"
                self._connected.clear()
            abort.set()
            return False
        except (ProtocolError, ValueError, TypeError):
            return False

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            self._abort.set()
            self._connected.clear()
            for sock in (self._active_socket, self._listener):
                if sock is not None:
                    self._close(sock)
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(HANDSHAKE_TIMEOUT + 0.5)
            self._drain_queue()

    @staticmethod
    def _close(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if hasattr(ssl, "OP_NO_RENEGOTIATION"):
            context.options |= ssl.OP_NO_RENEGOTIATION
        context.load_cert_chain(str(self._cert), str(self._key))
        return context

    @staticmethod
    def _client_context() -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if hasattr(ssl, "OP_NO_RENEGOTIATION"):
            context.options |= ssl.OP_NO_RENEGOTIATION
        context.check_hostname = False
        # The exact SHA-256 certificate pin is verified before any credentials
        # or input are sent; a system CA is deliberately not trusted here.
        context.verify_mode = ssl.CERT_NONE
        return context

    def _authenticate_server(self, sock: ssl.SSLSocket) -> None:
        server_nonce = secrets.token_bytes(32)
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        sock.settimeout(HANDSHAKE_TIMEOUT)
        sock.sendall(_pack(_envelope("challenge", nonce=server_nonce.hex())))
        response = _recv_frame(sock, deadline)
        if set(response) != {"protocol", "version", "kind", "nonce", "proof"} or response["kind"] != "authenticate":
            raise ProtocolError("对端未通过配对认证")
        client_nonce = _hex_bytes(response["nonce"], 32)
        proof = _hex_bytes(response["proof"], 32)
        expected = bytes.fromhex(_proof(self._secret, "client", server_nonce, client_nonce))
        if not hmac.compare_digest(proof, expected):
            raise ProtocolError("配对凭据不匹配")
        sock.sendall(_pack(_envelope("authenticated", proof=_proof(
            self._secret, "server", server_nonce, client_nonce))))

    def _authenticate_client(self, sock: ssl.SSLSocket, peer: dict) -> None:
        certificate = sock.getpeercert(binary_form=True)
        actual_pin = hashlib.sha256(certificate).digest()
        if not hmac.compare_digest(actual_pin, _hex_bytes(peer["pin"], 32)):
            raise ProtocolError("证书指纹不匹配，连接已拒绝")
        secret = _hex_bytes(peer["secret"], 32)
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        challenge = _recv_frame(sock, deadline)
        if set(challenge) != {"protocol", "version", "kind", "nonce"} or challenge["kind"] != "challenge":
            raise ProtocolError("对端协议身份不符")
        server_nonce = _hex_bytes(challenge["nonce"], 32)
        client_nonce = secrets.token_bytes(32)
        sock.sendall(_pack(_envelope("authenticate", nonce=client_nonce.hex(),
                                     proof=_proof(secret, "client", server_nonce, client_nonce))))
        response = _recv_frame(sock, deadline)
        if set(response) != {"protocol", "version", "kind", "proof"} or response["kind"] != "authenticated":
            raise ProtocolError("对端未通过配对认证")
        expected = bytes.fromhex(_proof(secret, "server", server_nonce, client_nonce))
        if not hmac.compare_digest(_hex_bytes(response["proof"], 32), expected):
            raise ProtocolError("对端未通过配对认证")

    def _listen_loop(self, host: str, port: int, advertise_host: str,
                     ready: threading.Event, result: dict) -> None:
        listener = None
        try:
            context = self._server_context()
            listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
            self._listener = listener
            # Do not use SO_REUSEADDR on Windows: it permits a second process
            # to bind an existing listening port. A restart can choose a free port.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((host, port))
            listener.listen(1)
            listener.settimeout(0.2)
            self.bound_port = listener.getsockname()[1]
            address = advertise_host or _advertised_ip(host)
            value = {"host": address, "port": self.bound_port,
                     "pin": self._fingerprint, "secret": self._secret.hex()}
            encoded = base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
            result["code"] = PAIRING_PREFIX + encoded
            ready.set()
            self._status(f"等待配对 · {address}:{self.bound_port}")
            while not self._stop_event.is_set():
                try:
                    raw, _ = listener.accept()
                except socket.timeout:
                    continue
                self._active_socket = raw
                tls = None
                try:
                    raw.settimeout(HANDSHAKE_TIMEOUT)
                    tls = context.wrap_socket(raw, server_side=True)
                    self._active_socket = tls
                    self._authenticate_server(tls)
                    if not self._stop_event.is_set():
                        self._session(tls)
                except (OSError, ValueError, TimeoutError) as exc:
                    if not self._stop_event.is_set():
                        self._status(self._error_status(exc))
                finally:
                    self._close(tls if tls is not None else raw)
                    self._active_socket = None
                    self._abort.clear()
        except (OSError, ValueError) as exc:
            if not ready.is_set():
                result["error"] = "无法监听：" + self._error_status(exc)
                ready.set()
            elif not self._stop_event.is_set():
                self._status(self._error_status(exc))
        finally:
            if listener is not None:
                self._close(listener)
            self._listener = None
            ready.set()

    def _connect_loop(self, peer: dict) -> None:
        delay = 0.5
        while not self._stop_event.is_set():
            tls = None
            raw = None
            try:
                self._status("正在建立加密连接")
                raw = socket.create_connection((peer["host"], peer["port"]), HANDSHAKE_TIMEOUT)
                self._active_socket = raw
                tls = self._client_context().wrap_socket(raw, server_hostname=None)
                self._active_socket = tls
                self._authenticate_client(tls, peer)
                if not self._stop_event.is_set():
                    delay = 0.5
                    self._session(tls)
            except ProtocolError as exc:
                # Wrong identity or credentials require deliberate re-pairing.
                self._status(str(exc))
                return
            except (OSError, ValueError, TimeoutError) as exc:
                if not self._stop_event.is_set():
                    self._status(self._error_status(exc))
            finally:
                if tls is not None or raw is not None:
                    self._close(tls if tls is not None else raw)
                self._active_socket = None
                self._abort.clear()
            if self._stop_event.wait(delay):
                break
            self._status("连接断开，正在重试")
            delay = min(delay * 2, 8.0)

    @staticmethod
    def _error_status(exc: BaseException) -> str:
        if isinstance(exc, ProtocolError):
            return str(exc)
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "连接超时，请检查对端和局域网"
        # Never format arbitrary peer data or pairing material in diagnostics.
        return "连接中断或被拒绝，请检查对端、端口和局域网"

    def _session(self, sock: ssl.SSLSocket) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setblocking(False)
        wake_reader, wake_writer = socket.socketpair()
        wake_reader.setblocking(False)
        wake_writer.setblocking(False)
        self._send_wakeup = wake_writer
        self._session_epoch += 1
        epoch = self._session_epoch
        self._queue = _SendQueue(maxsize=QUEUE_SIZE)
        self._abort = threading.Event()
        self._connected.set()
        self._status("已加密连接")
        received = bytearray()
        outgoing = b""
        offset = 0
        need_pong = False
        last_received = time.monotonic()
        next_ping = last_received + HEARTBEAT_INTERVAL
        window_start = last_received
        packet_count = 0
        reason = "连接已断开"
        try:
            while not self._stop_event.is_set() and not self._abort.is_set():
                now = time.monotonic()
                if now - last_received > IDLE_TIMEOUT:
                    raise TimeoutError("心跳超时")
                if not outgoing:
                    if need_pong:
                        outgoing = _pack(_envelope("pong"))
                        need_pong = False
                    elif now >= next_ping:
                        outgoing = _pack(_envelope("ping"))
                        next_ping = now + HEARTBEAT_INTERVAL
                    else:
                        try:
                            queued_epoch, frame = self._queue.get_nowait()
                            if queued_epoch == epoch:
                                outgoing = frame
                        except queue.Empty:
                            pass
                    offset = 0
                readable, writable, _ = select.select(
                    [sock, wake_reader], [sock] if outgoing else [], [], 0 if sock.pending() else .05)
                if wake_reader in readable:
                    try:
                        while wake_reader.recv(4096):
                            pass
                    except (BlockingIOError, OSError):
                        pass
                if outgoing and writable:
                    try:
                        count = sock.send(outgoing[offset:])
                        if count == 0:
                            raise ConnectionError("对端关闭连接")
                        offset += count
                        if offset == len(outgoing):
                            outgoing = b""
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        pass
                if sock in readable or sock.pending():
                    try:
                        chunk = sock.recv(16384)
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        continue
                    if not chunk:
                        raise ConnectionError("对端关闭连接")
                    received.extend(chunk)
                    while len(received) >= 4:
                        size = struct.unpack("!I", received[:4])[0]
                        if not 0 < size <= MAX_FRAME:
                            raise ProtocolError("消息长度超过限制，连接已关闭")
                        if len(received) < size + 4:
                            break
                        message = _decode(bytes(received[4:size + 4]))
                        del received[:size + 4]
                        now = time.monotonic()
                        if now - window_start >= 1.0:
                            window_start, packet_count = now, 0
                        packet_count += 1
                        if packet_count > MAX_PACKETS_PER_SECOND:
                            raise ProtocolError("对端消息过于频繁，连接已关闭")
                        last_received = now
                        kind = message.get("kind")
                        if kind in ("ping", "pong"):
                            if set(message) != {"protocol", "version", "kind"}:
                                raise ProtocolError("心跳消息无效")
                            if kind == "ping":
                                need_pong = True
                        elif kind == "event":
                            body = message.get("body")
                            if (set(message) != {"protocol", "version", "kind", "body"}
                                    or not isinstance(body, dict)
                                    or not isinstance(body.get("type"), str)
                                    or not 0 < len(body["type"]) <= 64
                                    or body["type"] == "disconnect"):
                                raise ProtocolError("输入消息无效")
                            self._message(body)
                        else:
                            raise ProtocolError("对端消息类型无效")
            if self._abort.is_set() and not self._stop_event.is_set():
                reason = self._abort_reason
        except (OSError, ValueError, TimeoutError) as exc:
            reason = self._error_status(exc)
        finally:
            if self._send_wakeup is wake_writer:
                self._send_wakeup = None
            wake_reader.close()
            wake_writer.close()
            self._connected.clear()
            self._drain_queue()
            self._status(reason)
            try:
                self._on_message({"type": "disconnect"})
            except Exception:
                pass
