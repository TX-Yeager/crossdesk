"""Real engine threads over loopback TLS, with wholly simulated native input.

These tests bind only ephemeral 127.0.0.1 ports. They never install an OS hook,
inject hardware input, open LAN ports, or change a service/firewall setting.
"""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest

from crossdesk.engine import Engine
from crossdesk.transport import PeerTransport


class FakeBackend:
    def __init__(self):
        self.lock = threading.RLock()
        self.pointer = (50.0, 50.0)
        self.keys = set()
        self.buttons = set()
        self.injected = []
        self.warps = []
        self.releases = 0
        self.running = False
        self.last_error = None
        self.handler = None
        self.starts = 0
        self.start_error = None

    def screens(self):
        return [{"name": "main", "x": 0, "y": 0, "width": 100, "height": 100}]

    def supported_keys(self):
        return set("abcdefghijklmnopqrstuvwxyz") | {"f12", "ctrl_l", "meta_l", "shift_l", "caps_lock"}

    def position(self):
        with self.lock:
            return self.pointer

    def start(self, handler):
        with self.lock:
            self.starts += 1
            if self.start_error:
                raise self.start_error
            if self.running:
                raise RuntimeError("fake capture already active")
            self.handler, self.running = handler, True

    def stop(self):
        with self.lock:
            self.running = False
            self.handler = None

    def warp(self, x, y):
        with self.lock:
            self.pointer = (x, y)
            self.warps.append(self.pointer)

    def inject(self, event):
        with self.lock:
            self.injected.append(dict(event))
            if event["type"] == "position":
                self.pointer = (event["x"], event["y"])
            elif event["type"] == "key":
                (self.keys.add if event["down"] else self.keys.discard)(event["key"])
            elif event["type"] == "button":
                (self.buttons.add if event["down"] else self.buttons.discard)(event["button"])

    def release_all(self):
        with self.lock:
            self.keys.clear()
            self.buttons.clear()
            self.releases += 1

    def emit(self, event):
        # The OS commits a physical move only if the hook lets it through.
        # The engine hook never calls the backend or waits on its worker.
        with self.lock:
            handler = self.handler if self.running else None
            event=dict(event)
            if event.get("type")=="move":
                event.setdefault("x",max(0,min(99,self.pointer[0]+event.get("dx",0))))
                event.setdefault("y",max(0,min(99,self.pointer[1]+event.get("dy",0))))
            blocked=bool(handler and handler(event))
            if event.get("type")=="move" and not blocked:
                self.pointer=(event["x"],event["y"])
            return blocked

    def has_key(self, key):
        with self.lock:
            return key in self.keys

    def has_button(self, button):
        with self.lock:
            return button in self.buttons

    def contains_event(self, event):
        with self.lock:
            return event in self.injected

    def all_released(self):
        with self.lock:
            return not self.keys and not self.buttons


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("loopback routing did not reach the expected state before timeout")


def layout(engine):
    with engine.lock:
        return sorted((s.id, s.x, s.y, s.w, s.h) for s in engine.desktop.screens)


def current_machine(engine):
    with engine.lock:
        return engine.desktop.by_id[engine.current].machine


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crossdesk-integration-")
        self.engines = []
        self.transports = []
        self.notifications = []

    def tearDown(self):
        for engine in reversed(self.engines):
            engine.close()
        for transport in reversed(self.transports):
            transport.stop()
        for engine in self.engines:
            engine.worker.join(timeout=2)
        self.temporary.cleanup()

    def engine(self, name):
        native = FakeBackend()
        native.needs_remote_anchor=name!="mac"
        machine = {"id": name, "name": name,
                   "platform": "darwin" if name == "mac" else "win32"}
        events, statuses = [], []
        engine = Engine(native, machine, lambda kind, value: events.append((kind, value)))
        transport = PeerTransport(Path(self.temporary.name) / name,
                                  engine.receive, statuses.append)
        engine.transport = transport
        self.engines.append(engine)
        self.transports.append(transport)
        self.notifications.append(events)
        return engine

    def pair(self, listener="win", enable=True):
        win, mac = self.engine("win"), self.engine("mac")
        host, client = (win, mac) if listener == "win" else (mac, win)
        code = host.transport.listen("127.0.0.1", 0)
        client.transport.connect(code)
        wait_for(lambda: win.transport.connected and mac.transport.connected)
        try:
            wait_for(lambda: win.peer_ready and mac.peer_ready
                     and win.revision[0] > 0 and win.revision == mac.revision
                     and layout(win) == layout(mac))
        except AssertionError as exc:
            raise AssertionError(
                f"initial hello/layout race: listener={listener}, "
                f"ready={(win.peer_ready, mac.peer_ready)}, "
                f"revisions={(win.revision, mac.revision)}, "
                f"layouts={(layout(win), layout(mac))}, "
                f"notifications={self.notifications}"
            ) from exc
        self.assertEqual(win.peer_supported, mac.backend.supported_keys())
        self.assertEqual(mac.peer_supported, win.backend.supported_keys())
        if enable:
            win.set_enabled(True)
            wait_for(lambda: win.enabled and mac.enabled and win.peer_active and mac.peer_active
                     and not win.control_pending and not mac.control_pending)
        return win, mac

    def emit(self, engine, **event):
        with engine.lock:
            native=event.get("type")=="move" and (engine.incoming or current_machine(engine)==engine.machine["id"])
            self.assertEqual(engine.backend.emit(event),not native)

    def toward_other(self, engine):
        with engine.lock:
            source = engine.desktop.by_id[engine.current]
            remote = next(s for s in engine.desktop.screens
                          if s.machine != engine.machine["id"])
            return 1 if remote.x >= source.right else -1

    def exercise_bidirectional_session(self, listener):
        win, mac = self.pair(listener)
        direction = self.toward_other(win)
        # Windows mouse crosses to Mac over the authenticated asynchronous link.
        self.emit(win, type="move", dx=80 * direction, dy=0)
        wait_for(lambda: current_machine(win) == "mac" and mac.incoming)
        self.assertTrue(0 <= mac.backend.position()[0] <= 99)
        self.assertTrue(win.backend.warps)
        self.emit(win, type="button", button="left", down=True)
        wait_for(lambda: mac.backend.has_button("left"))
        self.emit(win, type="button", button="left", down=False)
        wait_for(lambda: not mac.backend.has_button("left"))
        self.emit(win, type="key", key="ctrl_l", down=True)
        self.emit(win, type="key", key="c", down=True)
        wait_for(lambda: mac.backend.has_key("meta_l") and mac.backend.has_key("c"))
        # Mac's physical touchpad takes over at its currently displayed pointer.
        self.emit(mac, type="move", dx=direction, dy=0)
        wait_for(lambda: not mac.incoming and win.keyboard_target=="mac")
        self.assertTrue(mac.backend.has_key("c"))
        self.emit(win,type="key",key="c",down=False)
        self.emit(win,type="key",key="ctrl_l",down=False)
        wait_for(lambda:mac.backend.all_released() and not win.held_keys)
        self.emit(mac, type="move", dx=-200 * direction, dy=0)
        wait_for(lambda: current_machine(mac) == "win" and win.incoming)
        self.emit(mac, type="key", key="b", down=True)
        wait_for(lambda: win.backend.has_key("b"))
        scroll = {"type": "scroll", "dx": -.25, "dy": .125}
        self.assertTrue(mac.backend.emit(scroll))
        wait_for(lambda: win.backend.contains_event(scroll))
        # Pausing the controlling side releases every held target key/button.
        mac.set_enabled(False)
        wait_for(lambda: not win.peer_active and not win.incoming
                 and not win.enabled and not mac.enabled
                 and not win.control_pending and not mac.control_pending
                 and win.backend.all_released() and mac.backend.all_released())
        self.assertFalse(mac.backend.running)
        self.assertFalse(win.backend.emit({"type": "move", "dx": 1, "dy": 0}))
        # A later session starts without carrying pressed-state from the old one.
        mac.set_enabled(True)
        wait_for(lambda: win.peer_active and mac.peer_active and win.enabled and mac.enabled
                 and not win.control_pending and not mac.control_pending)
        self.emit(mac, type="move", dx=-200 * direction, dy=0)
        wait_for(lambda: current_machine(mac) == "win" and win.incoming)
        self.emit(mac, type="key", key="a", down=True)
        self.emit(mac, type="button", button="right", down=True)
        wait_for(lambda: win.backend.has_key("a") and win.backend.has_button("right"))
        mac.transport.stop()
        wait_for(lambda: not win.transport.connected and not mac.transport.connected
                 and not win.enabled and not mac.enabled
                 and not win.backend.running and not mac.backend.running
                 and win.backend.all_released() and mac.backend.all_released())
        self.assertFalse(any(kind == "status" and str(value).startswith("已安全暂停")
                             for events in self.notifications for kind, value in events))

    def test_real_tls_bidirectional_takeover_with_windows_listening(self):
        self.exercise_bidirectional_session("win")

    def test_real_tls_bidirectional_takeover_with_mac_listening(self):
        self.exercise_bidirectional_session("mac")

    def test_real_tls_native_absolute_motion_is_passed_without_self_injection(self):
        win,mac=self.pair()
        before=sum(row["type"]=="position" for row in win.backend.injected)
        # A native sample can carry a changed point with zero reported delta.
        self.emit(win,type="move",x=70,y=60,dx=0,dy=0)
        wait_for(lambda:win.cursor==(70,60))
        self.assertEqual(win.backend.position(),(70,60))
        self.assertEqual(sum(row["type"]=="position" for row in win.backend.injected),before)
        self.assertFalse(mac.incoming)
        self.emit(win,type="move",x=99,y=60,dx=0,dy=0)
        wait_for(lambda:mac.incoming and current_machine(win)=="mac")

    def test_real_tls_repeated_remote_motion_and_zero_receiver_events_do_not_reclaim(self):
        win,mac=self.pair()
        self.emit(win,type="move",dx=80,dy=0)
        wait_for(lambda:mac.incoming)
        initial=mac.backend.position()[0]
        for number in range(1,16):
            # The same proposed native point can repeat while an anchored source
            # is suppressed; every relative delta still has to be delivered.
            self.emit(win,type="move",x=51,y=50,dx=1,dy=0)
            wait_for(lambda:mac.backend.position()[0]>=initial+number-.001)
            with mac.lock:
                point=mac.backend.position()
                before=mac.backend.releases
                self.assertFalse(mac.backend.emit({"type":"move","x":point[0],"y":point[1],"dx":0,"dy":0}))
                self.assertFalse(mac.backend.emit({"type":"scroll","dx":0,"dy":0}))
                self.assertTrue(mac.incoming)
                self.assertEqual(mac.backend.releases,before)
        self.assertTrue(mac.incoming)
        self.assertEqual(current_machine(win),"mac")
        self.assertFalse(mac.backend.warps)

    def test_real_tls_layout_edits_sync_from_either_computer(self):
        win, mac = self.pair(enable=False)
        with win.lock:
            first = [replace(s, x=0, y=-100 if s.machine == "mac" else 0)
                     for s in win.desktop.screens]
            win.update_layout(first)
            revision = win.revision
        wait_for(lambda: mac.revision == revision and layout(win) == layout(mac))
        with mac.lock:
            second = [replace(s, x=-100 if s.machine == "mac" else 0, y=0)
                      for s in mac.desktop.screens]
            mac.update_layout(second)
            revision = mac.revision
        wait_for(lambda: win.revision == revision and layout(win) == layout(mac))
        self.assertEqual(win.desktop.by_id["mac:main"].x, -100)
        self.assertEqual(mac.desktop.by_id["win:main"].native_x, 0)
        win.set_enabled(True)
        wait_for(lambda: win.peer_active and mac.peer_active
                 and not win.control_pending and not mac.control_pending)
        self.emit(win, type="move", dx=-80, dy=0)
        wait_for(lambda: mac.incoming and current_machine(win) == "mac")

    def test_real_tls_f12_panic_releases_target_and_preserves_connection(self):
        win, mac = self.pair()
        self.emit(win, type="move", dx=80, dy=0)
        wait_for(lambda: mac.incoming)
        self.emit(win, type="key", key="a", down=True)
        wait_for(lambda: mac.backend.has_key("a"))
        self.emit(win, type="key", key="f12", down=True)
        wait_for(lambda: not win.enabled and not win.backend.running
                 and not mac.enabled and not mac.backend.running
                 and not mac.peer_active and not mac.incoming
                 and mac.backend.all_released())
        self.assertTrue(win.transport.connected and mac.transport.connected)

    def test_real_tls_receiver_keyboard_follows_pointer_without_reclaim(self):
        win,mac=self.pair()
        self.emit(win,type="move",dx=80,dy=0)
        wait_for(lambda:mac.incoming and win.keyboard_target==mac.keyboard_target=="mac")
        self.emit(mac,type="key",key="a",down=True)
        self.emit(win,type="key",key="b",down=True)
        wait_for(lambda:mac.backend.has_key("a") and mac.backend.has_key("b"))
        self.assertTrue(mac.incoming)
        self.assertEqual(current_machine(mac),"mac")
        self.assertFalse(win.backend.has_key("a") or win.backend.has_key("b"))
        self.emit(mac,type="key",key="a",down=False)
        self.emit(win,type="key",key="b",down=False)
        self.emit(mac,type="move",dx=2,dy=0)
        wait_for(lambda:not mac.incoming)
        self.emit(mac,type="move",dx=-200,dy=0)
        wait_for(lambda:win.incoming and win.keyboard_target==mac.keyboard_target=="win")
        self.emit(win,type="key",key="a",down=True)
        self.emit(mac,type="key",key="b",down=True)
        wait_for(lambda:win.backend.has_key("a") and win.backend.has_key("b"))
        self.assertTrue(win.incoming)

    def test_real_tls_async_clipboard_barrier_preserves_whole_paste_chord(self):
        win,mac=self.pair()
        self.emit(win,type="move",dx=80,dy=0)
        wait_for(lambda:mac.incoming)
        pending=[]
        win.before_paste=lambda machine,done:pending.append((machine,done))
        self.emit(win,type="key",key="ctrl_l",down=True)
        self.emit(win,type="key",key="v",down=True)
        self.emit(win,type="key",key="v",down=False)
        self.emit(win,type="key",key="ctrl_l",down=False)
        wait_for(lambda:len(pending)==1 and not win.held_keys)
        self.assertFalse(mac.backend.contains_event({"type":"key","key":"v","down":True}))
        pending[0][1](True)
        wait_for(lambda:mac.backend.contains_event({"type":"key","key":"v","down":False}))
        wait_for(mac.backend.all_released)
        self.assertTrue(mac.incoming)
        self.assertEqual(pending[0][0],"mac")

    def test_real_tls_duplicate_hello_does_not_pause_an_enabled_session(self):
        win, mac = self.pair()
        revision = win.revision
        before = [sum(kind == "peer_active" for kind, _ in events)
                  for events in self.notifications]
        self.assertTrue(win._hello())
        self.assertTrue(mac._hello())
        wait_for(lambda: all(sum(kind == "peer_active" for kind, _ in events) > count
                             for events, count in zip(self.notifications, before)))
        self.assertEqual(win.revision, revision)
        self.assertEqual(mac.revision, revision)
        self.assertTrue(win.enabled and mac.enabled)
        self.assertTrue(win.backend.running and mac.backend.running)
        self.emit(win, type="move", dx=80, dy=0)
        wait_for(lambda: mac.incoming)

    def test_real_tls_remote_permission_failure_rolls_back_both(self):
        win, mac = self.pair(enable=False)
        mac.backend.start_error = PermissionError("private diagnostic payload")
        win.set_enabled(True)
        wait_for(lambda: not win.control_pending and not win.enabled
                 and not mac.enabled and not win.backend.running and not mac.backend.running)
        statuses = [value for kind, value in self.notifications[0] if kind == "status"]
        self.assertTrue(any("mac" in value and "辅助功能" in value and "输入监控" in value
                            for value in statuses))
        self.assertFalse(any("private diagnostic payload" in value for value in statuses))

    def test_real_tls_starting_from_mac_enables_windows_without_second_click(self):
        win, mac = self.pair(enable=False)
        mac.set_enabled(True)
        wait_for(lambda: win.enabled and mac.enabled and win.peer_active and mac.peer_active
                 and not mac.control_pending)
        self.assertEqual((win.backend.starts, mac.backend.starts), (1, 1))
        win.set_enabled(False)
        wait_for(lambda: not win.enabled and not mac.enabled
                 and not win.control_pending and not mac.control_pending)


if __name__ == "__main__":
    unittest.main()
