"""LAN presence only. Discovery packets contain no credentials or input events."""
import json
import ipaddress
import socket
import threading
import time

PORT = 48194
MAGIC = "crossdesk-lan-v1"


def route_address(peer="192.0.2.1"):
    """Ask the local routing table only; UDP connect sends no network packet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((str(ipaddress.IPv4Address(peer)), 9))
            return probe.getsockname()[0]
    except (OSError, ValueError):
        return None


def lan_addresses():
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            address=ipaddress.IPv4Address(ip)
            if not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast):
                found.add(ip)
    except OSError:
        pass
    preferred=route_address()
    if preferred and preferred != "127.0.0.1":
        found.add(preferred)
    return sorted(found,key=lambda value:(value!=preferred,int(ipaddress.IPv4Address(value)))) or ["127.0.0.1"]


def pairing_offer(peer):
    """Treat broadcasts as untrusted hints, never as pairing credentials."""
    offer=peer.get("pairing")
    if not isinstance(offer,dict) or type(offer.get("version")) is not int or offer["version"]!=1:
        return None
    port,pin,busy=offer.get("port"),offer.get("pin"),offer.get("busy")
    if type(port) is not int or not 0<=port<=65535 or type(busy) is not bool:
        return None
    if not isinstance(pin,str) or len(pin)!=64 or any(c not in "0123456789abcdef" for c in pin):
        return None
    return {"version":1,"port":port,"pin":pin,"busy":busy}


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
                        # Bind the sending address explicitly so a virtual adapter
                        # cannot take all broadcasts away from the physical LAN.
                        for address in lan_addresses():
                            try:
                                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
                                    sender.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
                                    sender.bind((address,0))
                                    sender.sendto(packet,("255.255.255.255",PORT))
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
