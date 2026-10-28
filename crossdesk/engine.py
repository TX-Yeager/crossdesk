"""Symmetric input routing. Network roles never decide who can control whom."""
from dataclasses import replace
from collections import OrderedDict, deque
import queue
import threading
import time
import uuid
from .geometry import Desktop, Screen, number
from .scroll import ScrollCurve
from .gestures import ACTIONS as GESTURE_ACTIONS
from .session import stop_reason
from . import diagnostics

MODIFIERS = {p+s for p in ("ctrl_", "shift_", "alt_", "meta_") for s in ("l", "r")}
BUTTONS = {"left", "right", "middle", "back", "forward"}
CONTROL_TIMEOUT = 6.0
LOCAL_KEY_TIMEOUT = 2.0
LOCAL_KEY_QUEUE_LIMIT = 512
COMMON_COMMAND_KEYS = frozenset("acxvzsfntwroplbiud0123456789") | {"minus", "equal"}


class Engine:
    def __init__(self, backend, machine, notify=lambda kind, value: None, saved_layout=None):
        self.backend, self.machine, self.notify = backend, machine, notify
        self.transport = None
        self.lock = threading.RLock()
        self.events = queue.Queue(4096)
        self.closed = threading.Event()
        self.emergency = threading.Event()
        self._emergency_reason='emergency'
        self.enabled = self.peer_active = self.peer_ready = self.incoming = False
        self.keyboard = True
        self.swap_modifiers = True
        self.peer_machine = None
        self.peer_supported = set()
        self.peer_gestures = set()
        self.hello_sent = False
        self._pending_control = None
        self.last_stop = None
        self._control_generation = 0
        self._peer_control_generation = -1
        self._control_results = OrderedDict()
        self.held_keys, self.held_buttons = set(), set()
        self.keyboard_target = machine["id"]
        self._keyboard_screen = None
        self._keyboard_shared = False
        self._keyboard_generation = 0
        self._keyboard_revision = (0, "")
        self._key_routes = {}
        self._key_modifiers = set()
        self._modifier_target = None
        self._last_key_chord = None
        self._key_sources = {"local": set(), "peer": set()}
        self._button_sources = {"local": set(), "peer": set()}
        self.before_paste = None
        self._pending_pastes = {}
        self._paste_keys = set()
        self.local_keyboard_ready = lambda: True
        self._local_key_queue = deque()
        self._native_keys_down = set()
        self._scroll_curve = ScrollCurve()
        self.current = None
        self.cursor = (0.0, 0.0)
        self._routing_generation = 0
        self._native_expected = (0, 0, None)
        self._native_observed = None
        self._physical_takeover = None
        self.origin_scale = (1.0, 1.0)
        self.revision = (0, "")
        saved = {s.get("id"): s for s in saved_layout or [] if isinstance(s, dict)}
        screens = []
        for i, raw in enumerate(backend.screens()):
            sid = f"{machine['id']}:{raw.get('id', raw.get('name', i))}"
            nx, ny, nw, nh = (float(raw[k]) for k in ("x", "y", "width", "height"))
            item = Screen(sid, machine["id"], str(raw.get("name", f"显示器 {i+1}")), nx, ny, nw, nh, nx, ny, nw, nh)
            previous = saved.get(sid)
            if previous:
                try:
                    item = Screen.from_dict({**item.as_dict(), **{k: previous[k] for k in ("x", "y", "w", "h")}})
                except (ValueError, KeyError, TypeError):
                    pass
            screens.append(item)
        self.desktop = Desktop(screens)
        try:
            self.desktop.validate_layout()
        except ValueError:
            self.desktop = Desktop([replace(s, x=s.native_x, y=s.native_y, w=s.native_w, h=s.native_h) for s in screens])
        self.saved = saved
        try:
            self._reset_cursor()
        except OSError:
            # A sandbox or non-interactive desktop can enumerate monitors while
            # denying GetCursorPos. Keep the layout preview usable; enabling
            # capture still calls the strict _reset_cursor and reports failure.
            screen = screens[0]
            self.current = screen.id
            self.cursor = (screen.x + (screen.w-1)/2, screen.y + (screen.h-1)/2)
            self.origin_scale = (screen.w/screen.native_w, screen.h/screen.native_h)
        self.worker = threading.Thread(target=self._run, name="CrossDesk-router", daemon=True)
        self.worker.start()

    def _keyboard_screen_target(self):
        target = self.desktop.by_id.get(self._keyboard_screen)
        return target or next(s for s in self.desktop.screens if s.machine == self.machine["id"])

    @property
    def pointer_owner(self):
        return self.keyboard_target

    def _set_keyboard_focus(self, screen, broadcast=False, sync=True, revision=None, force=False):
        """Synchronize pointer ownership and its following keyboard destination."""
        announce = broadcast and (not self._keyboard_shared or self.keyboard_target != screen.machine)
        if revision is not None:
            if revision < self._keyboard_revision:
                return False
            self._keyboard_revision = revision
        elif force or announce or self.keyboard_target != screen.machine:
            self._keyboard_revision = (self._keyboard_revision[0]+1,self.machine["id"])
        changed = self.keyboard_target != screen.machine
        if changed:
            self._scroll_curve.clear()
            self._cancel_local_keys()
            self._cancel_pastes()
            self._keyboard_generation += 1
            self._release_key_routes()
            # A peer's old key-up may already be queued. Clear only its old
            # contribution; the second keyboard may hold the same logical key.
            self._release_injected_source("peer", keys=True, buttons=False)
        self.keyboard_target, self._keyboard_screen = screen.machine, screen.id
        self._keyboard_shared = True
        if changed and sync:
            self._sync_key_modifiers(screen, self._base_key_modifiers(screen))
        if announce:
            self._send({"type": "keyboard_focus", "screen": screen.id,
                        "focus_revision":list(self._keyboard_revision)})
        return True

    def _peer_focus_revision(self,message):
        revision = message.get("focus_revision")
        if revision is None:
            return (self._keyboard_revision[0]+1,(self.peer_machine or {}).get("id",""))
        if (not isinstance(revision,list) or len(revision)!=2 or type(revision[0]) is not int
                or not 0<=revision[0]<2**63 or not isinstance(revision[1],str)
                or revision[1] not in (self.machine["id"],(self.peer_machine or {}).get("id"))):
            raise ValueError("无效输入焦点版本")
        return tuple(revision)

    def _inject_input(self, event, source):
        kind = event["type"]
        if kind not in ("key", "button"):
            if (kind=="scroll" and getattr(self.backend,"smooth_wheel",False)
                    and not event.get("continuous",False)):
                self._scroll_curve.push(event.get("dx",0),event.get("dy",0))
                return
            if kind=="scroll" and event.get("continuous",False):
                self._scroll_curve.clear()
            self.backend.inject(event)
            return
        field = "key" if kind == "key" else "button"
        groups = self._key_sources if kind == "key" else self._button_sources
        own, other = groups[source], groups["peer" if source == "local" else "local"]
        value, down = event[field], event["down"]
        repeated = value in own
        if down:
            own.add(value)
            if value not in other or repeated:
                self._post_local_key(event) if kind == "key" else self.backend.inject(event)
        else:
            was_down = value in own
            own.discard(value)
            if was_down and value not in other:
                self._post_local_key(event) if kind == "key" else self.backend.inject(event)

    def _native_key(self,event):
        self.backend.inject(event)
        (self._native_keys_down.add if event["down"] else self._native_keys_down.discard)(event["key"])

    def _post_local_key(self,event):
        if self._local_key_queue or not self.local_keyboard_ready():
            if len(self._local_key_queue) >= LOCAL_KEY_QUEUE_LIMIT:
                raise RuntimeError("Local keyboard readiness queue exceeded its limit")
            self._local_key_queue.append((self._keyboard_generation,self._routing_generation,
                                          time.monotonic()+LOCAL_KEY_TIMEOUT,dict(event)))
        else:
            self._native_key(event)

    def _cancel_local_keys(self):
        if not self._local_key_queue:
            return
        self._local_key_queue.clear()
        for key in sorted(self._native_keys_down, key=lambda value:value in MODIFIERS):
            self._native_key({"type":"key","key":key,"down":False})
        for values in self._key_sources.values():
            values.clear()
        self._key_routes = {key:route for key,route in self._key_routes.items()
                            if self.desktop.by_id[route[0]].machine != self.machine["id"]}
        self._key_modifiers.clear()
        self._modifier_target = None
        self._last_key_chord = None

    def _flush_local_keys(self):
        if not self._local_key_queue:
            return
        keyboard,route,deadline,_ = self._local_key_queue[0]
        if (self.keyboard_target != self.machine["id"] or not self.enabled
                or keyboard!=self._keyboard_generation or route!=self._routing_generation):
            self._cancel_local_keys()
            return
        if time.monotonic() >= deadline:
            self._cancel_local_keys()
            raise RuntimeError("Local application focus was not restored in time")
        if not self.local_keyboard_ready():
            return
        while self._local_key_queue:
            keyboard,route,_,event = self._local_key_queue.popleft()
            if keyboard==self._keyboard_generation and route==self._routing_generation:
                self._native_key(event)

    def _release_injected_source(self, source, keys=True, buttons=True):
        for kind, groups, allowed in (("key", self._key_sources, keys),
                                      ("button", self._button_sources, buttons)):
            if allowed:
                for value in list(groups[source]):
                    self._inject_input({"type": kind, kind: value, "down": False}, source)

    def _release_pointer_inputs(self):
        self._release_injected_source("local", keys=False)
        self._release_injected_source("peer", keys=False)
        self.held_buttons.clear()

    def _clear_input_state(self):
        self._scroll_curve.clear()
        self._cancel_local_keys()
        self._cancel_pastes()
        self.held_keys.clear()
        self.held_buttons.clear()
        self._key_routes.clear()
        self._key_modifiers.clear()
        self._modifier_target = None
        self._last_key_chord = None
        self._paste_keys.clear()
        self._keyboard_shared = False
        self._native_keys_down.clear()
        for groups in (self._key_sources, self._button_sources):
            for values in groups.values():
                values.clear()

    def _emit_key(self, key, down, target):
        event = {"type": "key", "key": key, "down": down}
        if target.machine == self.machine["id"]:
            self._inject_input(event, "local")
        else:
            self._send({"type": "input", "event": event})

    def _release_key_routes(self):
        for target_id, key in list(self._key_routes.values()):
            target = self.desktop.by_id.get(target_id)
            if target:
                self._emit_key(key, False, target)
        self._key_routes.clear()
        old = self.desktop.by_id.get(self._modifier_target)
        if old:
            for key in sorted(self._key_modifiers):
                self._emit_key(key, False, old)
        self._key_modifiers.clear()
        self._modifier_target = None
        self._last_key_chord = None

    def _shortcut_profile(self, target):
        peer = self.peer_machine or {}
        return bool(self.swap_modifiers and target.machine != self.machine["id"]
                    and {self.machine.get("platform"), peer.get("platform")} == {"win32", "darwin"})

    def _base_key_modifiers(self, target):
        modifiers = self.held_keys & MODIFIERS
        if self._shortcut_profile(target):
            # Do not briefly press the Windows key before discovering that a
            # physical Command key belongs to a copy/paste chord. Emit complete
            # chords on the following key/button/scroll instead.
            if modifiers and self._last_key_chord and self._last_key_chord[1] == target.machine:
                return self._shortcut_spec(self._last_key_chord[0], target)[1]
            return {key for key in modifiers if key.startswith("shift_")}
        return set(modifiers)

    def _pointer_modifiers(self,target):
        modifiers=set(self.held_keys & MODIFIERS)
        if self._shortcut_profile(target):
            source,destination=('meta_','ctrl_') if self.machine['platform']=='darwin' else ('ctrl_','meta_')
            modifiers={destination+key[-1] if key.startswith(source) else key for key in modifiers}
        return modifiers

    def _shortcut_spec(self, key, target):
        modifiers = set(self.held_keys & MODIFIERS)
        if not self._shortcut_profile(target):
            return key, modifiers
        source_mac = self.machine["platform"] == "darwin"
        primary = "meta_" if source_mac else "ctrl_"
        alternate = "ctrl_" if source_mac else "meta_"
        has = lambda prefix: any(value.startswith(prefix) for value in modifiers)
        shift = has("shift_")

        def replace_mod(old, new):
            for value in list(modifiers):
                if value.startswith(old):
                    modifiers.remove(value)
                    modifiers.add(new + value[-1])

        # Standard application/editing commands, with explicit exceptions below.
        if has(primary) and not has(alternate) and not has("alt_"):
            if key in COMMON_COMMAND_KEYS:
                replace_mod(primary, "ctrl_" if source_mac else "meta_")
                if source_mac and key == "z" and shift:
                    key = "y"
                    modifiers = {value for value in modifiers if not value.startswith("shift_")}
            elif not source_mac and key == "y":
                replace_mod("ctrl_", "meta_")
                modifiers.add("shift_l")
                key = "z"
            elif source_mac and key == "q":
                replace_mod("meta_", "alt_")
                key = "f4"
            elif source_mac and key == "tab":
                replace_mod("meta_", "alt_")
            elif source_mac and key in ("left", "right", "up", "down"):
                document = key in ("up", "down")
                modifiers = {value for value in modifiers if not value.startswith("meta_")}
                key = {"left": "home", "right": "end", "up": "home", "down": "end"}[key]
                if document:
                    modifiers.add("ctrl_l")
            elif source_mac and key in ("bracket_left", "bracket_right"):
                if shift:
                    replace_mod("meta_", "ctrl_")
                    if key == "bracket_right":
                        modifiers = {value for value in modifiers if not value.startswith("shift_")}
                    key = "tab"
                else:
                    replace_mod("meta_", "alt_")
                    key = "left" if key == "bracket_left" else "right"
            elif not source_mac and key in ("left", "right", "backspace", "delete"):
                replace_mod("ctrl_", "alt_")
            elif not source_mac and key in ("home", "end"):
                replace_mod("ctrl_", "meta_")
                key = "up" if key == "home" else "down"
        elif has("alt_") and not has("ctrl_") and not has("meta_"):
            if source_mac and key in ("left", "right", "backspace", "delete"):
                replace_mod("alt_", "ctrl_")
            elif not source_mac and key == "tab":
                replace_mod("alt_", "meta_")
            elif not source_mac and key == "f4":
                replace_mod("alt_", "meta_")
                key = "w"
            elif not source_mac and key in ("left", "right"):
                replace_mod("alt_", "meta_")
                key = "bracket_left" if key == "left" else "bracket_right"
        elif not has("ctrl_") and not has("meta_") and not has("alt_"):
            if not source_mac and key == "insert" and shift:
                modifiers = {value for value in modifiers if not value.startswith("shift_")}
                modifiers.add("meta_l")
                key = "v"
            elif not source_mac and key in ("home", "end"):
                modifiers.add("meta_l")
                key = "left" if key == "home" else "right"
            elif not source_mac and key == "f5":
                modifiers.add("meta_l")
                key = "r"
        return key, modifiers

    def _sync_key_modifiers(self, target, wanted):
        previous = self.desktop.by_id.get(self._modifier_target)
        if previous and previous.machine != target.machine:
            for key in sorted(self._key_modifiers):
                self._emit_key(key, False, previous)
            self._key_modifiers.clear()
        for key in sorted(self._key_modifiers - wanted):
            self._emit_key(key, False, target)
        for key in sorted(wanted - self._key_modifiers):
            self._emit_key(key, True, target)
        self._key_modifiers = set(wanted)
        self._modifier_target = target.id

    def _sync_held_key_modifiers(self, target):
        routes = [key for key, (screen, _) in self._key_routes.items()
                  if self.desktop.by_id[screen].machine == target.machine]
        wanted = set().union(*(self._shortcut_spec(key, target)[1] for key in routes)) if routes else self._base_key_modifiers(target)
        self._sync_key_modifiers(target, wanted)

    def _local_key(self, event):
        key, down = event["key"], event["down"]
        target = self._keyboard_screen_target()
        if down:
            self.held_keys.add(key)
        else:
            self.held_keys.discard(key)
        if key in MODIFIERS:
            self._sync_held_key_modifiers(target)
            return
        if down:
            if key in self._paste_keys:
                return
            mapped, modifiers = self._shortcut_spec(key, target)
            command = "meta_" if self.machine.get("platform") == "darwin" else "ctrl_"
            paste = ((key == "v" and any(value.startswith(command) for value in self.held_keys))
                     or (self.machine.get("platform") == "win32" and key == "insert"
                         and any(value.startswith("shift_") for value in self.held_keys)))
            if paste and callable(self.before_paste):
                self._paste_keys.add(key)
                self._defer_paste(target, mapped, modifiers)
                return
            route = self._key_routes.get(key)
            if route:
                target, mapped = self.desktop.by_id[route[0]], route[1]
            else:
                self._key_routes[key] = (target.id, mapped)
            self._last_key_chord = (key, target.machine)
            self._sync_key_modifiers(target, modifiers)
            self._emit_key(mapped, True, target)
        else:
            self._paste_keys.discard(key)
            route = self._key_routes.pop(key, None)
            if route:
                self._emit_key(route[1], False, self.desktop.by_id[route[0]])
            self._sync_held_key_modifiers(target)

    def _defer_paste(self, target, key, modifiers):
        """The hook queues GUI clipboard work and returns immediately.

        before_paste(machine_id, done) may return an optional nonblocking cancel
        callback. done(bool) may run on any thread; it only queues a token. The
        engine emits the whole captured chord after success, or discards it on
        focus/session changes or failure. The clipboard broker cancels stalled
        transfers after 28 seconds; active transfers have a five-minute ceiling.
        """
        token = uuid.uuid4().hex
        self._pending_pastes[token] = {"route": self._routing_generation,
            "keyboard": self._keyboard_generation, "target": target.id,
            "key": key, "modifiers": set(modifiers), "deadline": time.monotonic()+300,
            "cancel": None}

        def done(ok):
            try:
                self.events.put_nowait(("paste_ready", {"token": token, "ok": ok is True}))
            except queue.Full:
                self.emergency.set()

        try:
            cancel = self.before_paste(target.machine, done)
            if token in self._pending_pastes and callable(cancel):
                self._pending_pastes[token]["cancel"] = cancel
        except Exception:
            self._finish_paste(token, False)

    def _cancel_pastes(self):
        pending, self._pending_pastes = self._pending_pastes, {}
        for item in pending.values():
            cancel = item.get("cancel")
            if callable(cancel):
                try:
                    cancel()
                except Exception:
                    pass

    def _check_paste_timeouts(self):
        for token,item in list(self._pending_pastes.items()):
            if time.monotonic() >= item["deadline"]:
                self._finish_paste(token,False)

    def _finish_paste(self, token, ok):
        item = self._pending_pastes.pop(token, None)
        if not item:
            return
        if (not ok or not self.enabled or not self.peer_ready or not self.peer_active
                or not self.transport or not self.transport.connected
                or item["route"] != self._routing_generation
                or item["keyboard"] != self._keyboard_generation):
            cancel = item.get("cancel")
            if callable(cancel):
                try:
                    cancel()
                except Exception:
                    pass
            if not ok:
                self.notify("status", "剪贴板同步未完成，本次粘贴已取消。")
            return
        target = self.desktop.by_id.get(item["target"])
        if not target or target.machine != self.keyboard_target:
            return
        self._sync_key_modifiers(target, item["modifiers"])
        self._emit_key(item["key"], True, target)
        self._emit_key(item["key"], False, target)
        self._last_key_chord = None
        self._sync_held_key_modifiers(target)

    def describe(self):
        with self.lock:
            return {"machine": self.machine, "screens": [s.as_dict() for s in self.desktop.screens if s.machine == self.machine["id"]]}

    def _send(self, message):
        return bool(self.transport and self.transport.connected and self.transport.send(message))

    def _hello(self):
        keys = sorted(getattr(self.backend, "supported_keys", lambda: set())())
        sent = self._send({"type": "hello", **self.describe(), "enabled": self.enabled,
                           "supported_keys": keys,
                           "gestures":sorted(GESTURE_ACTIONS) if self.machine['platform']=='win32' else []})
        if sent:
            self.hello_sent = True
        return sent

    def receive(self, message):
        try:
            self.events.put_nowait(("network", message))
        except queue.Full:
            self.emergency.set()

    def capture(self, event):
        # Native hook: no network, disk access, locks, or input injection here.
        if event.get("type") == "key" and event.get("key") == "f12":
            if event.get("down"):
                self._emergency_reason='f12'
                self.emergency.set()
            return bool(self.enabled)
        if self._pending_control and self._pending_control["enabled"]:
            return False  # Keep input local until the peer confirms startup.
        if not (self.enabled and self.peer_ready and self.peer_active and self.transport and self.transport.connected):
            return False
        if event.get('type')=='gesture':
            target=self._keyboard_screen_target()
            action=event.get('action')
            if (not target or target.machine==self.machine['id'] or not self.peer_machine
                    or self.peer_machine.get('platform')!='win32' or not self.peer_gestures
                    or action is not None and action not in self.peer_gestures):
                return False
            if action is None:return True # Synchronous gate, no network in tap.
        native_move = False
        if event.get("type") == "move":
            if not all(number(event.get(key)) for key in ("x","y","dx","dy")):
                return False
            generation = self._routing_generation
            expected = self._native_expected
            previous = self._native_reference(generation)
            point = (event["x"],event["y"])
            absolute_changed = previous is not None and point != previous
            if not (event["dx"] or event["dy"] or absolute_changed):
                return False
            target = self.desktop.by_id.get(self.current)
            native_move = self.incoming or not target or target.machine == self.machine["id"]
            if not native_move and not (event["dx"] or event["dy"]):
                return False
            event = {**event,"_native_passed":native_move,"_route_generation":generation,
                     "_native_previous":previous,"_motion_effective":True}
            if native_move:
                # A separate observation record cannot overwrite a newer
                # expected injection/warp written concurrently by the worker.
                self._native_observed = (expected[0],expected[1],point)
                if self.incoming:
                    self._physical_takeover = (generation,point,previous,event["dx"],event["dy"])
        elif event.get("type") == "scroll" and not (event.get("dx") or event.get("dy")):
            return False
        if event.get("type") == "key" and not self.keyboard:
            return False
        if event.get("type") == "key" and self.peer_supported:
            target = self._keyboard_screen_target()
            if target and target.machine != self.machine["id"]:
                if self._mapped_key(event.get("key", ""), target) not in self.peer_supported:
                    return False  # Preserve unsupported physical keys locally.
        try:
            self.events.put_nowait(("local", event))
            return not native_move
        except queue.Full:
            self.enabled = False  # fail open immediately, cleanup on the worker
            self._emergency_reason='input_queue_full'
            self.emergency.set()
            return False

    def _reset_cursor(self):
        nx, ny = self.backend.position()
        screen = self.desktop.native_screen(self.machine["id"], nx, ny)
        self.current, self.cursor = screen.id, screen.from_native(nx, ny)
        if not self.enabled:
            self.keyboard_target, self._keyboard_screen = screen.machine, screen.id
        self.origin_scale = (screen.w/screen.native_w, screen.h/screen.native_h)
        self._new_route((nx,ny))

    def _native_reference(self,generation):
        expected = self._native_expected
        observed = self._native_observed
        if observed and observed[:2] == expected[:2] and observed[0] == generation:
            return observed[2]
        return expected[2] if expected[0] == generation else None

    def _expect_native(self,point):
        self._native_expected = (self._routing_generation,self._native_expected[1]+1,point)

    def _new_route(self,point=None):
        self._scroll_curve.clear()
        self._cancel_local_keys()
        self._cancel_pastes()
        if point is None:
            point = self._native_reference(self._routing_generation)
        self._routing_generation += 1
        self._expect_native(point)
        self._physical_takeover = None

    @property
    def control_pending(self):
        return self._pending_control is not None

    @property
    def session_state(self):
        if not self.transport or not self.transport.connected or not self.peer_ready:
            return 'disconnected'
        if self._pending_control:
            return 'starting' if self._pending_control['enabled'] else 'pausing'
        if self.enabled and self.peer_active:
            return 'active'
        return 'paused'

    def _record_stop(self, code, source='local'):
        # Preserve the originating reason through ready/control echoes and
        # unrelated UI messages. A new explicit start clears it.
        if self.last_stop is None:
            self.last_stop = stop_reason(code, source)
            diagnostics.event('session_stopped', reason=self.last_stop['code'], source=source)

    def _stop_fields(self):
        return {'reason': self.last_stop['code']} if self.last_stop else {}

    def _pending(self, value):
        previous = self.control_pending
        self._pending_control = value
        if previous != self.control_pending:
            self.notify("session_pending", self.control_pending)

    def _control_message(self, enabled):
        self._control_generation += 1
        return {"type": "control", "enabled": enabled, "request": uuid.uuid4().hex,
                "generation": self._control_generation, **({} if enabled else self._stop_fields())}

    def _local_enabled(self, enabled, reason='requested'):
        was_enabled=self.enabled
        if enabled:
            if not (self.enabled and getattr(self.backend, "running", True)):
                self.desktop.validate_layout()
                self._reset_cursor()
                self.backend.start(self.capture)
            self.enabled = True
        else:
            self.enabled = False
            self.incoming = False
            self._send({"type": "release"})
            self._clear_input_state()
            try:
                self.backend.release_all()
            finally:
                self.backend.stop()
            self._reset_cursor()
        self._send({"type": "ready", "enabled": self.enabled,
                    **({} if self.enabled else self._stop_fields())})
        diagnostics.event('control_changed',enabled=self.enabled,reason=reason,peer_active=self.peer_active)
        if not enabled and was_enabled:
            diagnostics.event('control_exit',reason=reason,enabled=False,peer_active=self.peer_active,
                              connected=bool(self.transport and self.transport.connected))
        self.notify("enabled", self.enabled)

    def _start_error(self, error, remote=False):
        machine = (self.peer_machine or {}) if remote else self.machine
        name = "".join(c for c in str(machine.get("name", "对方电脑" if remote else "这台电脑"))
                       if c.isprintable())[:80]
        if error == "permission_required":
            if machine.get("platform") == "darwin":
                return f"无法启动 {name}：请在这台 Mac 的系统设置中允许 CrossDesk 的辅助功能和输入监控，再重试开始互控。"
            return f"无法启动 {name}：请检查系统输入权限，再重试开始互控。"
        return f"无法启动 {name}：系统输入接口不可用，请检查权限和桌面会话，再重试。"

    @staticmethod
    def _error_code(exc):
        if isinstance(exc, PermissionError) or any(word in str(exc).lower() for word in
                ("permission", "accessibility", "input monitoring")):
            return "permission_required"
        return "capture_failed"

    def _rollback_control(self, status, send_cancel=True, reason='routing_error', source='local'):
        self._record_stop(reason, source)
        diagnostics.event('control_rollback',reason=self.last_stop['code'])
        self._pending(None)
        try:
            self._local_enabled(False,reason=self.last_stop['code'])
        except Exception:
            self.enabled = self.incoming = False
            self.notify("enabled", False)
        if send_cancel and self.peer_ready and self.transport and self.transport.connected:
            # A newer pause supersedes a start that might still be queued remotely.
            self._send(self._control_message(False))
        self.notify("status", status)

    def _complete_control(self):
        pending = self._pending_control
        if not pending or not pending.get("acked"):
            return
        if self.enabled != pending["enabled"] or self.peer_active != pending["enabled"]:
            return
        enabled = pending["enabled"]
        if enabled:
            # Input remained local while awaiting confirmation; start from the
            # cursor's current position, not where the user clicked Start.
            self._reset_cursor()
        self._pending(None)
        if enabled:
            self._set_keyboard_focus(self.desktop.by_id[self.current],broadcast=True,force=True)
        diagnostics.event('control_changed',enabled=enabled,reason='confirmed',peer_active=self.peer_active)
        self.notify("status", "两台电脑的互控已启动，可直接跨屏；F12 可同时暂停两端。" if enabled
                    else "两台电脑的互控已暂停，鼠标和触摸板由各自电脑正常接管。")

    def _check_control_timeout(self):
        pending = self._pending_control
        if pending and time.monotonic() >= pending["deadline"]:
            self._rollback_control("对方电脑没有及时确认，互控已暂停。请检查对方的 CrossDesk 和系统权限后重试。",
                                   reason='start_timeout' if pending['enabled'] else 'pause_timeout')

    def set_enabled(self, enabled, propagate=True, reason='manual'):
        with self.lock:
            enabled = bool(enabled)
            diagnostics.event('control_requested',enabled=enabled,reason=reason)
            if enabled:
                if not self.transport or not self.transport.connected or not self.peer_ready:
                    raise RuntimeError("请先完成两台电脑的配对连接。")
                if propagate and self.enabled and self._pending_control and self._pending_control["enabled"]:
                    return  # Repeated clicks share the pending request.
                if propagate and self.enabled and self.peer_active and not self.control_pending:
                    return
                if not self.enabled:
                    self.last_stop = None
            else:
                if reason in ('peer_control', 'peer_pause'):
                    self._record_stop('peer_pause', 'peer')
                else:
                    self._record_stop(reason)
                if propagate and self._pending_control and not self._pending_control['enabled']:
                    return
            try:
                self._local_enabled(enabled,reason=reason)
            except Exception as exc:
                diagnostics.exception('capture_failed',exc,reason='control_change')
                if not propagate:
                    raise
                status = self._start_error(self._error_code(exc))
                self._rollback_control(status, reason=self._error_code(exc))
                raise RuntimeError(status) from None
            if not propagate:
                return
            if not (self.transport and self.transport.connected and self.peer_ready):
                self._pending(None)
                self.notify("status", "互控已暂停；重新连接后仍需点击开始。")
                return
            message = self._control_message(enabled)
            self._pending({**message, "deadline": time.monotonic()+CONTROL_TIMEOUT, "acked": False})
            if not self._send(message):
                self._rollback_control("无法发送会话请求，互控已暂停。请检查连接后重试。", send_cancel=False, reason='disconnect')
                return
            self.notify("status", "正在启动两台电脑的互控，等待对方确认；F12 可取消。" if enabled
                        else "本机已暂停，正在确认对方也已暂停。")

    def add_discovered(self, description):
        """Display preview only; this never makes an unauthenticated peer ready."""
        with self.lock:
            if self.enabled:
                raise RuntimeError("请先暂停互控，再编辑显示器。")
            machine = description["machine"]
            if machine["id"] == self.machine["id"]:
                return
            if self.peer_ready and self.peer_machine["id"] != machine["id"]:
                raise ValueError("当前版本同时连接两台电脑，请先断开当前电脑。")
            local = [s for s in self.desktop.screens if s.machine == self.machine["id"]]
            prior = self.desktop.by_id
            remote, next_y = [], min(s.y for s in local)
            next_x = max(s.right for s in local)
            for raw in description["screens"]:
                screen = Screen.from_dict(raw)
                if screen.machine != machine["id"] or not screen.id.startswith(machine["id"]+":"):
                    raise ValueError("显示器不属于所选电脑")
                old = prior.get(screen.id)
                if old:
                    screen = replace(screen, x=old.x, y=old.y, w=old.w, h=old.h)
                else:
                    saved = self.saved.get(screen.id)
                    if saved:
                        try:
                            screen = Screen.from_dict({**screen.as_dict(), **{k:saved[k] for k in ("x","y","w","h")}})
                        except (ValueError, KeyError, TypeError):
                            screen = replace(screen, x=next_x, y=next_y)
                    else:
                        screen = replace(screen, x=next_x, y=next_y)
                remote.append(screen)
                next_y = screen.bottom
            if not remote:
                raise ValueError("对方没有可用显示器")
            self.desktop = Desktop(local+remote)
            self.notify("machines", machine)
            self.notify("layout", self.desktop.screens)

    def update_layout(self, screens, broadcast=True):
        with self.lock:
            if self.enabled:
                raise RuntimeError("请先暂停互控，再调整布局。")
            proposed = Desktop(screens)
            if set(proposed.by_id) != set(self.desktop.by_id):
                raise ValueError("显示器列表已变化，请刷新布局")
            proposed.validate_layout()
            # Do not allow a layout editor or peer to alter OS display coordinates.
            for screen in screens:
                old = self.desktop.by_id[screen.id]
                for field in ("machine", "native_x", "native_y", "native_w", "native_h"):
                    if getattr(screen, field) != getattr(old, field):
                        raise ValueError("布局不能修改系统显示器信息")
            self.desktop = proposed
            self.revision = (max(time.time_ns(), self.revision[0]+1), self.machine["id"])
            self._reset_cursor()
            self.notify("layout", screens)
            if broadcast:
                self._send_layout()

    def _send_layout(self):
        self._send({"type":"layout", "revision":list(self.revision), "screens":[{k:s.as_dict()[k] for k in ("id","x","y","w","h")} for s in self.desktop.screens]})

    def _receive_control(self, message):
        enabled, request = message.get("enabled"), message.get("request")
        generation = message.get("generation")
        if (not isinstance(enabled, bool) or not isinstance(request, str)
                or len(request) > 64 or type(generation) is not int
                or not 0 < generation < 2**63):
            raise ValueError("无效会话控制请求")
        try:
            uuid.UUID(request)
        except ValueError:
            raise ValueError("无效会话请求标识") from None
        cached = self._control_results.get(request)
        if cached:
            if cached[:2] != (enabled, generation):
                raise ValueError("重复会话请求不一致")
            self._send(cached[2])
            return
        error = ""
        if generation <= self._peer_control_generation:
            error = "superseded"
        elif enabled and self._pending_control and not self._pending_control["enabled"]:
            # Simultaneous start/pause resolves in favour of the explicit pause.
            self._peer_control_generation = generation
            error = "superseded"
        else:
            self._peer_control_generation = generation
            if not enabled:
                self._pending(None)
                self._record_stop(message.get('reason', 'peer_pause'), 'peer')
            try:
                diagnostics.event('peer_control',enabled=enabled)
                self.set_enabled(enabled, propagate=False,reason='peer_control')
                self.notify("status", "两台电脑的互控已启动，可直接跨屏；F12 可同时暂停两端。" if enabled
                            else "对方已暂停互控，两台电脑均已恢复本地操作。")
            except Exception as exc:
                error = self._error_code(exc)
                self._rollback_control(self._start_error(error), send_cancel=False, reason=error)
        result = {"type": "control_result", "request": request,
                  "enabled": self.enabled, "ok": not bool(error), "error": error}
        self._control_results[request] = (enabled, generation, result)
        while len(self._control_results) > 128:
            self._control_results.popitem(last=False)
        self._send(result)

    def _receive_control_result(self, message):
        pending = self._pending_control
        if not pending or message.get("request") != pending["request"]:
            return  # A late start acknowledgement cannot undo a newer pause.
        enabled, ok, error = message.get("enabled"), message.get("ok"), message.get("error", "")
        if (not isinstance(enabled, bool) or not isinstance(ok, bool)
                or error not in ("", "permission_required", "capture_failed", "superseded")):
            raise ValueError("无效会话确认")
        if not ok or enabled != pending["enabled"]:
            status = ("对方已暂停，本次开启已取消。" if error == "superseded" else
                      self._start_error(error, remote=True))
            self._rollback_control(status, reason='peer_pause' if error=='superseded' else error, source='peer')
            return
        pending["acked"] = True
        self._complete_control()

    def _network(self, message):
        kind = message.get("type")
        if kind == "disconnect":
            diagnostics.event('connection_changed',connected=False)
            self.peer_ready = self.peer_active = False
            self.peer_gestures=set()
            self.peer_supported.clear()
            self.hello_sent = False
            self._pending(None)
            self._peer_control_generation = -1
            self._control_results.clear()
            if self.enabled:
                self.set_enabled(False, propagate=False,reason='disconnect')
            else:
                self._clear_input_state()
                self.backend.release_all()
            self.notify("connection", False)
            self.notify("status", "连接已断开，互控已暂停；重新连接后仍需点击开始。")
            return
        if kind == "hello":
            machine = message.get("machine", {})
            if (not isinstance(machine, dict) or not all(isinstance(machine.get(k),str) and 0<len(machine[k])<=100 for k in ("id","name","platform"))
                    or machine["id"] == self.machine["id"] or not isinstance(message.get("screens"),list) or not 1<=len(message["screens"])<=16):
                raise ValueError("对方电脑信息无效")
            keys = message.get("supported_keys", [])
            gestures=message.get('gestures',[])
            if not isinstance(gestures,list) or len(gestures)>4 or any(not isinstance(g,str) or g not in GESTURE_ACTIONS for g in gestures):
                raise ValueError('对方手势能力信息无效')
            if (not isinstance(keys, list) or len(keys) > 512
                    or any(not isinstance(key, str) or not 0 < len(key) <= 40
                           or not all(c.isalnum() or c == "_" for c in key) for key in keys)):
                raise ValueError("对方键盘能力信息无效")
            # A network callback may wake this worker before its next
            # connected-state check. Queue our own identity before any layout,
            # otherwise the peer would discard that layout before its hello.
            if not self.hello_sent and not self._hello():
                return
            if self.peer_ready and self.peer_machine == machine:
                canonical = ("id", "machine", "name", "native_x", "native_y",
                             "native_w", "native_h")
                old = sorted(tuple(getattr(screen, field) for field in canonical)
                             for screen in self.desktop.screens
                             if screen.machine == machine["id"])
                descriptions = [Screen.from_dict(row) for row in message["screens"]]
                proposed = sorted(tuple(getattr(screen, field) for field in canonical)
                                  for screen in descriptions)
                if old == proposed:
                    # Hello retries are idempotent. They must not rebuild an
                    # enabled desktop or create a new layout revision.
                    self.peer_supported = set(keys)
                    self.peer_gestures=set(gestures)
                    self._network({"type": "ready", "enabled": message.get("enabled") is True})
                    return
            self.add_discovered(message)
            self.peer_machine = machine
            self.peer_supported = set(keys)
            self.peer_gestures=set(gestures)
            self.peer_ready, self.peer_active = True, message.get("enabled") is True
            self.notify("connection", True)
            diagnostics.event('connection_changed',connected=True)
            # The listening side proposes the initial shared canvas. Afterwards
            # timestamped edits can originate at either computer.
            if getattr(self.transport, "is_listener", False):
                self.revision = (time.time_ns(), self.machine["id"])
                self._send_layout()
            return
        if not self.peer_ready:
            return
        if kind == "control":
            self._receive_control(message)
        elif kind == "control_result":
            self._receive_control_result(message)
        elif kind == "ready":
            self.peer_active = message.get("enabled") is True
            if not self.peer_active:
                self._clear_input_state()
                self.backend.release_all()
                self.incoming = False
                # Key/button releases while a peer is paused bypass capture.
                # Do not carry their old held state into the next ready session.
                self._reset_cursor()
                pending_start = self._pending_control and self._pending_control["enabled"]
                if self.enabled and not pending_start:
                    self._record_stop(message.get('reason', 'peer_pause'), 'peer')
                    self.set_enabled(False, propagate=False,reason='peer_pause')
                    self.notify("status", "对方已暂停互控，两台电脑均已恢复本地操作。")
            self.notify("peer_active", self.peer_active)
            self._complete_control()
        elif kind == "layout":
            raw, revision = message.get("screens"), message.get("revision")
            if (not isinstance(raw,list) or not isinstance(revision,list) or len(revision)!=2
                    or not isinstance(revision[0],int) or not isinstance(revision[1],str)):
                raise ValueError("无效布局消息")
            if tuple(revision) <= self.revision:
                return
            if len(raw) != len(self.desktop.screens) or {r.get("id") for r in raw if isinstance(r,dict)} != set(self.desktop.by_id):
                raise ValueError("对方布局与已连接显示器不一致")
            screens=[]
            for row in raw:
                old=self.desktop.by_id[row["id"]]
                screens.append(Screen.from_dict({**old.as_dict(), **{k:row[k] for k in ("x","y","w","h")}}))
            proposed=Desktop(screens)
            proposed.validate_layout()
            self.revision=tuple(revision)
            if proposed.by_id == self.desktop.by_id:
                return
            if self.enabled:
                self.set_enabled(False, reason='layout_changed')
            self.desktop=proposed
            self._reset_cursor()
            self.notify("layout", screens)
        elif kind == "keyboard_focus" and self.enabled and self.peer_active:
            screen = self.desktop.by_id.get(message.get("screen"))
            if not screen or screen.machine != self.peer_machine["id"]:
                raise ValueError("无效键盘焦点")
            self._set_keyboard_focus(screen,revision=self._peer_focus_revision(message))
        elif kind in ("release", "reclaim"):
            screen = self.desktop.by_id.get(message.get("screen"))
            revision=self._peer_focus_revision(message) if screen else None
            if revision is not None and revision < self._keyboard_revision:
                return
            if kind=='reclaim':
                diagnostics.event('pointer_claim',reason='peer_reclaim',source='peer')
            if kind=="release" and self.incoming and self.enabled and self.peer_active:
                outgoing=self.desktop.by_id.get(self.current)
                crossing=message.get("handoff")
                if outgoing and outgoing.machine==self.machine["id"]:
                    point=self.cursor
                    other=None
                    if isinstance(crossing,dict):
                        other=self.desktop.by_id.get(crossing.get("to")) if isinstance(crossing.get("to"),str) else None
                        u,v=crossing.get("u"),crossing.get("v")
                        if (crossing.get("from")==outgoing.id and other
                                and other.machine==self.peer_machine["id"]
                                and number(u) and number(v) and 0<=u<=1 and 0<=v<=1):
                            point=(outgoing.x+u*(outgoing.w-1),outgoing.y+v*(outgoing.h-1))
                        else:
                            other=None
                    self._handoff(outgoing,"depart",point,other)
            self._release_pointer_inputs()
            self.incoming=False
            self._reset_cursor()
            if screen:
                if screen.machine != self.peer_machine["id"]:
                    raise ValueError("无效接管焦点")
                self._set_keyboard_focus(screen,revision=revision)
        elif kind in ("focus", "pointer") and self.enabled and self.peer_active:
            takeover=self._physical_takeover
            if takeover and takeover[0]==self._routing_generation:
                if kind=='focus':diagnostics.event('pointer_focus_rejected',reason='pending_takeover')
                return  # A physical move already took precedence over queued packets.
            if kind == "pointer" and not self.incoming:
                return
            screen=self.desktop.by_id.get(message.get("screen"))
            u,v=message.get("u"),message.get("v")
            if not screen or screen.machine!=self.machine["id"] or not number(u) or not number(v) or not (0<=u<=1 and 0<=v<=1):
                raise ValueError("无效远程光标位置")
            revision=self._peer_focus_revision(message)
            if revision < self._keyboard_revision:
                if kind=='focus':diagnostics.event('pointer_focus_rejected',reason='stale_focus')
                return
            arriving=kind=="focus" and not self.incoming
            if kind=="focus":
                self._release_pointer_inputs()
                self.incoming=True
                self._new_route()
            x,y=screen.x+u*(screen.w-1),screen.y+v*(screen.h-1)
            self.current,self.cursor=screen.id,(x,y)
            self.origin_scale=(screen.w/screen.native_w,screen.h/screen.native_h)
            nx,ny=screen.to_native(x,y)
            self._expect_native((nx,ny))
            self.backend.inject({"type":"position","x":nx,"y":ny,"handoff":kind=='focus'})
            if kind=='focus' and getattr(self.backend,'needs_remote_anchor',False):
                self._new_route((nx,ny))
            self._set_keyboard_focus(screen,revision=revision)
            if arriving:
                previous=self.desktop.by_id.get(message.get("from_screen")) if isinstance(message.get("from_screen"),str) else None
                if previous and previous.machine!=self.peer_machine["id"]:previous=None
                self._handoff(screen,"arrive",(x,y),previous)
        elif kind=="input" and self.enabled and self.peer_active:
            event=message.get("event")
            if not self._valid_input(event):
                raise ValueError("无效远程输入")
            if event["type"]=="key":
                if self.keyboard and self._keyboard_shared and self.keyboard_target==self.machine["id"]:
                    self._inject_input(event,"peer")
            elif event["type"]=="scroll":
                if self.keyboard_target==self.machine["id"]:
                    self._inject_input(event,"peer")
            elif event['type']=='gesture':
                if self.pointer_owner==self.machine['id'] and self.machine['platform']=='win32':
                    self._inject_input(event,'peer')
                    diagnostics.event('gesture_executed',action=event['action'])
            elif self.incoming:
                self._inject_input(event,"peer")

    @staticmethod
    def _valid_input(event):
        if not isinstance(event,dict):
            return False
        kind=event.get("type")
        if kind=='gesture':return isinstance(event.get('action'),str) and event['action'] in GESTURE_ACTIONS
        if kind=="button":
            return event.get("button") in BUTTONS and isinstance(event.get("down"),bool)
        if kind=="scroll":
            return (all(number(event.get(k)) and abs(event[k])<=100 for k in ("dx","dy"))
                    and ("continuous" not in event or isinstance(event["continuous"],bool)))
        if kind=="key":
            key=event.get("key")
            return isinstance(key,str) and 0<len(key)<=40 and all(c.isalnum() or c=="_" for c in key) and isinstance(event.get("down"),bool)
        return False

    def _mapped_key(self, key, target):
        return self._shortcut_spec(key,target)[0]

    def _pointer_message(self, kind, screen):
        return {"type":kind,"coalesce":not self.held_buttons,"screen":screen.id,"u":max(0,min(1,(self.cursor[0]-screen.x)/(screen.w-1))),"v":max(0,min(1,(self.cursor[1]-screen.y)/(screen.h-1))),"focus_revision":list(self._keyboard_revision)}

    def _handoff(self, screen, phase, point, other=None):
        diagnostics.event('pointer_handoff',code=phase)
        """Queue presentation only after a real cross-machine pointer transition.

        Coordinates stay in memory; the GUI renders outside the input thread.
        Snap the effect to the crossed edge even when a move overshoots it.
        """
        x,y=screen.clamp(*point)
        edge=None
        if other is not None:
            if abs(screen.right-other.x)<=2:edge="right"
            elif abs(screen.x-other.right)<=2:edge="left"
            elif abs(screen.bottom-other.y)<=2:edge="bottom"
            elif abs(screen.y-other.bottom)<=2:edge="top"
        if edge is None:
            edge=min(((abs(x-screen.x),"left"),(abs(x-screen.right+1),"right"),
                      (abs(y-screen.y),"top"),(abs(y-screen.bottom+1),"bottom")))[1]
        if edge=="left":x=screen.x
        elif edge=="right":x=screen.right-1
        elif edge=="top":y=screen.y
        else:y=screen.bottom-1
        nx,ny=screen.to_native(x,y)
        self.notify("handoff",{"phase":phase,"x":nx,"y":ny,"edge":edge,
                    "screen":{"x":screen.native_x,"y":screen.native_y,
                              "width":screen.native_w,"height":screen.native_h},
                    "created_at":time.monotonic()})

    def _local(self, event):
        if not (self.enabled and self.peer_ready and self.peer_active):
            return
        if self._pending_control and self._pending_control["enabled"]:
            return
        kind=event.get("type")
        if kind=='gesture':
            action=event.get('action')
            if (action in self.peer_gestures and self.pointer_owner!=self.machine['id']
                    and self.peer_machine and self.peer_machine.get('platform')=='win32'):
                if self._send({'type':'input','event':{'type':'gesture','action':action}}):
                    diagnostics.event('gesture_sent',action=action)
                else:
                    diagnostics.event('gesture_send_failed',action=action,reason='transport_error')
            return
        if kind=="key":
            if self.keyboard and self._valid_input(event):
                self._local_key(event)
            return
        if kind=="move":
            if not all(number(event.get(key)) for key in ("x","y","dx","dy")):
                return
            if event.get("_route_generation",self._routing_generation)!=self._routing_generation:
                return  # The capture happened before a focus/handoff boundary.
            reference=event.get("_native_previous",self._native_reference(self._routing_generation))
            point=(event["x"],event["y"])
            changed=reference is not None and point!=reference
            if not (event["dx"] or event["dy"] or changed or event.get("_motion_effective")):
                return
        elif kind=="scroll" and not (event.get("dx") or event.get("dy")):
            return
        if self.incoming and (kind=="move" or (kind=="button" and event.get("down"))):
            diagnostics.event('pointer_claim',reason='physical_move' if kind=='move' else 'physical_button',source='local')
            if kind=="move":
                takeover=self._physical_takeover
                if takeover and takeover[0]==self._routing_generation:
                    point,reference=takeover[1],takeover[2]
                    event={**event,"x":point[0],"y":point[1],"dx":takeover[3],"dy":takeover[4],
                           "_native_passed":True}
            self._release_pointer_inputs()
            self.incoming=False
            if kind=="move":
                self._new_route(point)
                # A prior remote CGEventPost/SendInput may already be queued in
                # the OS. Restore the newest physical point once at takeover;
                # ordinary subsequent local moves remain wholly native.
                self.backend.inject({"type":"position","x":point[0],"y":point[1]})
            else:
                self._reset_cursor()
            claimed=self.desktop.native_screen(self.machine["id"],*point) if kind=="move" else self.desktop.by_id[self.current]
            self._set_keyboard_focus(claimed,force=True)
            self._send({"type":"reclaim","screen":claimed.id,"focus_revision":list(self._keyboard_revision)})
        target=self._keyboard_screen_target() if kind=="scroll" else self.desktop.by_id[self.current]
        if kind=="move":
            native_passed=event.get("_native_passed",target.machine==self.machine["id"])
            if native_passed:
                native=self.desktop.native_screen(self.machine["id"],*point)
                x,y=native.from_native(*point)
                self.current,self.cursor=native.id,(x,y)
                self.origin_scale=(native.w/native.native_w,native.h/native.native_h)
                self._set_keyboard_focus(native,broadcast=True)
                # Absolute motion has already happened in the OS. Probe only
                # the touching edge; do not add that physical delta a second time.
                dx=(point[0]-reference[0]) if reference else 0
                dy=(point[1]-reference[1]) if reference else 0
                # At a clipped OS edge, the absolute point can stay fixed even
                # while the hardware continues outward. Its relative direction
                # is used only for this tiny probe, never added to local motion.
                if not dx:
                    dx=event["dx"]
                if not dy:
                    dy=event["dy"]
                probe_x=(.001 if dx>0 else -.001) if dx else 0
                probe_y=(.001 if dy>0 else -.001) if dy else 0
                candidate,cx,cy=self.desktop.advance(native.id,x,y,probe_x,probe_y,
                                                     allow_machine_cross=not self.held_buttons)
                if candidate.machine==self.machine["id"]:
                    return  # Preserve the real local cursor; never reinject it.
                target=native
                self.current,self.cursor=candidate.id,(cx,cy)
                self._new_route(point)
                self._release_pointer_inputs()
                self._set_keyboard_focus(candidate,sync=False)
                if getattr(self.backend,"needs_remote_anchor",True):
                    anchor=(native.native_x+native.native_w/2,native.native_y+native.native_h/2)
                    self._expect_native(anchor)
                    getattr(self.backend,"park_remote",self.backend.warp)(*anchor)
                    # Drop every sample captured while the old edge position
                    # was being replaced by the native parking point.
                    self._new_route(anchor)
                self._send({**self._pointer_message("focus",candidate),"from_screen":native.id})
                self._sync_key_modifiers(candidate,self._base_key_modifiers(candidate))
                self._handoff(native,"depart",(x,y),candidate)
                self.notify("clipboard_handoff",True)
                self.notify("focus",candidate.id)
                return
            dx,dy=event["dx"],event["dy"]
            if not (dx or dy):
                return
            if target.machine==self.machine["id"]:
                return  # A stale suppressed sample must not move a local cursor.
            old=target
            target,x,y=self.desktop.advance(self.current,*self.cursor,max(-4096,min(4096,dx))*self.origin_scale[0],max(-4096,min(4096,dy))*self.origin_scale[1],allow_machine_cross=not self.held_buttons)
            self.current,self.cursor=target.id,(x,y)
            if old.machine!=target.machine:
                self._new_route()
            if target.machine==self.machine["id"]:
                nx,ny=target.to_native(x,y)
                self._expect_native((nx,ny))
                self.backend.inject({"type":"position","x":nx,"y":ny})
                if getattr(self.backend,'needs_remote_anchor',False):
                    self._new_route((nx,ny))
                self.origin_scale=(target.w/target.native_w,target.h/target.native_h)
            else:
                self._send(self._pointer_message("pointer",target))
            if old.machine!=target.machine:
                self._set_keyboard_focus(target,broadcast=target.machine==self.machine["id"])
                ox,oy=old.clamp(x,y)
                self._send({"type":"release","handoff":{"from":old.id,"to":target.id,
                            "u":(ox-old.x)/(old.w-1),"v":(oy-old.y)/(old.h-1)}})
                if target.machine==self.machine["id"]:
                    self._handoff(target,"arrive",(x,y),old)
            if old.id!=target.id:
                self.notify("focus",target.id)
        elif self._valid_input(event):
            if kind=="button":
                if event["down"] and target.machine==self.machine["id"]:
                    self._set_keyboard_focus(target,broadcast=True)
                (self.held_buttons.add if event["down"] else self.held_buttons.discard)(event["button"])
            self._sync_key_modifiers(self._keyboard_screen_target(),self._pointer_modifiers(self._keyboard_screen_target()))
            self._deliver(event,target)

    def _deliver(self,event,target):
        if target.machine==self.machine["id"]:
            self._inject_input(event,"local")
        else:
            self._send({"type":"input","event":event})

    def _run(self):
        was_connected=False
        while not self.closed.is_set():
            try:
                if self.enabled and getattr(self.backend, "running", True) is False:
                    error = getattr(self.backend, "last_error", None)
                    raise RuntimeError("系统输入捕获已停止，互控已暂停。") from (
                        error if isinstance(error, BaseException) else None
                    )
                if self.emergency.is_set():
                    self.emergency.clear()
                    reason=self._emergency_reason
                    self._emergency_reason='emergency'
                    self.set_enabled(False,reason=reason)
                with self.lock:
                    self._check_control_timeout()
                    self._check_paste_timeouts()
                    self._flush_local_keys()
                    if self._scroll_curve.pending:
                        dx,dy=self._scroll_curve.sample()
                        if dx or dy:
                            self.backend.inject({"type":"scroll","dx":dx,"dy":dy,"continuous":True})
                connected=bool(self.transport and self.transport.connected)
                if connected and not self.hello_sent:
                    self._hello()
                elif was_connected and not connected:
                    self._network({"type":"disconnect"})
                was_connected=connected
                try:
                    kind,event=self.events.get(timeout=.008 if self._scroll_curve.pending else .025)
                except queue.Empty:
                    continue
                with self.lock:
                    if kind=="network":
                        self._network(event)
                    elif kind=="paste_ready":
                        self._finish_paste(event.get("token"),event.get("ok") is True)
                    elif kind=="local_ready":
                        self._flush_local_keys()
                    else:
                        self._local(event)
            except Exception as exc:
                diagnostics.exception('routing_exception',exc,reason='routing_exception')
                diagnostics.event('control_exit',reason='routing_exception',enabled=self.enabled,
                                  peer_active=self.peer_active,connected=bool(self.transport and self.transport.connected))
                with self.lock:
                    native_error=getattr(self.backend,'last_error',None)
                    detail=str(native_error if isinstance(native_error,BaseException) else exc)
                    reason=(getattr(self.backend,'failure_reason',None) or
                            ('input_restore_failed' if 'focus was not restored' in str(exc) else
                             'capture_failed' if not getattr(self.backend,'running',True) else 'routing_error'))
                    known=('Cursor transition','Cursor owner','Returning pointer','Outgoing pointer','SendInput was blocked')
                    if detail.startswith(known):
                        self._rollback_control(f"已安全暂停：{detail[:180]}",reason=reason)
                    else:
                        self._rollback_control(f"已安全暂停：{type(exc).__name__}。请检查连接、系统权限和布局。",reason=reason)

    def close(self):
        diagnostics.event('control_exit',reason='app_exit',enabled=self.enabled,
                          peer_active=self.peer_active,connected=bool(self.transport and self.transport.connected))
        # Drain any in-flight routing decision before releasing its injected
        # inputs. Otherwise the worker could inject key-down after release_all.
        with self.lock:
            self.enabled=False
            self.incoming=False
            self._pending(None)
            self.closed.set()
            self._send({"type":"release"})
            self._send({"type":"ready","enabled":False,"reason":"app_exit"})
            self._clear_input_state()
            self.backend.release_all()
            self.backend.stop()
        if self.worker is not threading.current_thread():
            self.worker.join(2.0)
        if self.transport:
            self.transport.stop()
