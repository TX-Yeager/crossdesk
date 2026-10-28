"""LAN presence only. Discovery packets contain no credentials or input events."""
import json
import socket
import threading
import time

PORT = 48194
MAGIC = "crossdesk-lan-v1"


def lan_addresses():
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip != "0.0.0.0":
                found.add(ip)
    except OSError:
        pass
    return sorted(found) or ["127.0.0.1"]


class Discovery:
    def __init__(self, describe, on_peer, on_status=lambda text: None):
        self.describe, self.on_peer, self.on_status = describe, on_peer, on_status
        self.stop_event = threading.Event()
        self.sock = None

    def start(self):
        threading.Thread(target=self._run, name="CrossDesk-discovery", daemon=True).start()

    def _run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock = sock
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("", PORT))
            sock.settimeout(0.3)
            next_send, recent = 0.0, {}
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now >= next_send:
                    description = self.describe()
                    packet = json.dumps({"magic": MAGIC, **description}, ensure_ascii=False).encode("utf-8")
                    if len(packet) <= 12000:
                        try:
                            sock.sendto(packet, ("255.255.255.255", PORT))
                        except OSError:
                            pass
                    next_send = now+3
                try:
                    packet, addr = sock.recvfrom(12001)
                except socket.timeout:
                    continue
                if len(packet) > 12000 or now-recent.get(addr[0], -1) < 1:
                    continue
                recent[addr[0]] = now
                if len(recent) > 128:
                    recent = {addr[0]: now}
                try:
                    data = json.loads(packet)
                    if (not isinstance(data, dict) or data.get("magic") != MAGIC
                            or not isinstance(data.get("machine"), dict)
                            or data["machine"].get("id") == self.describe()["machine"]["id"]):
                        continue
                    machine = data["machine"]
                    if not all(isinstance(machine.get(k), str) and 0 < len(machine[k]) <= 100 for k in ("id", "name", "platform")):
                        continue
                    if not isinstance(data.get("screens"), list) or not 1 <= len(data["screens"]) <= 16:
                        continue
                    data["address"], data["seen"] = addr[0], time.monotonic()
                    self.on_peer(data)
                except (ValueError, TypeError, KeyError, RecursionError):
                    continue
        except OSError:
            self.on_status("局域网自动发现暂不可用，仍可粘贴配对码连接。")
        finally:
            if self.sock:
                self.sock.close()

    def stop(self):
        self.stop_event.set()
