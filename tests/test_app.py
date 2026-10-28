"""Tk integration tests with a non-injecting backend and loopback TLS only."""
from dataclasses import replace
import base64
import gc
import json
import logging
import os
import signal
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import Mock, patch
import tkinter as tk

from crossdesk.app import App, DemoBackend, settings_port
from crossdesk.__main__ import main
from crossdesk.geometry import Screen


class PermissionBackend(DemoBackend):
    def __init__(self):
        self.requests=[]

    def permissions(self,request=False):
        self.requests.append(request)
        return {"accessibility":False,"input_monitoring":False}


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            root = tk.Tk()
            root.withdraw()
            root.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk/display unavailable: {exc}")

    def setUp(self):
        # Tk interpreters and Variable finalizers must be released on their
        # creating thread. Tests create many roots, unlike the one-root app;
        # automatic cyclic GC could otherwise run on a later router thread.
        self.gc_was_enabled = gc.isenabled()
        gc.disable()
        self.addCleanup(self._restore_gc)
        self.temporary = tempfile.TemporaryDirectory(prefix="crossdesk-gui-test-")
        self.apps = []
        self.callback_errors = []

    def tearDown(self):
        try:
            self._dispose_apps()
            self.assertEqual(self.callback_errors, [])
        finally:
            logging.shutdown()  # Release the test's log file before Windows temp cleanup.
            self.temporary.cleanup()

    def _restore_gc(self):
        # unittest calls cleanups even when a test or tearDown fails.
        gc.collect()
        if self.gc_was_enabled:
            gc.enable()

    def _dispose_apps(self):
        while self.apps:
            app = self.apps.pop()
            app.close()
            del app
        # A completed UI action or diagnostics probe can still hold its App
        # closure briefly. Let those test-owned threads drop it before GC.
        for thread in threading.enumerate():
            if thread.name in ("CrossDesk-UI-action", "CrossDesk-diagnostics"):
                thread.join(2)
                self.assertFalse(thread.is_alive(), f"GUI test thread leaked: {thread.name}")
        gc.collect()

    def app(self, settings=None, demo=True, backend=None, state_dir=None):
        state = Path(state_dir) if state_dir is not None else Path(self.temporary.name) / str(len(self.apps))
        state.mkdir(exist_ok=True)
        if settings is not None:
            (state / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        root = tk.Tk()
        root.withdraw()
        root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        app = App(root, backend or DemoBackend(), state, demo=demo)
        self.apps.append(app)
        self.wait_for(lambda: len(app.layout) == (3 if demo else 1))
        return app

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for app in self.apps:
                if not app._closed:
                    app.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("GUI condition timed out")

    @staticmethod
    def point(app, screen):
        return SimpleNamespace(x=(screen.x + screen.w / 2) * app.canvas_scale + app.canvas_offset[0],
                               y=(screen.y + screen.h / 2) * app.canvas_scale + app.canvas_offset[1])

    def drag_to(self, app, screen, x, y):
        point = self.point(app, screen)
        app._press(point)
        target = SimpleNamespace(x=point.x + (x - screen.x) * app.canvas_scale,
                                 y=point.y + (y - screen.y) * app.canvas_scale)
        app._motion(target)
        app._release(target)

    def test_demo_draws_three_screens_without_hooks_or_discovery(self):
        app = self.app()
        self.assertIsNone(app.discovery)
        self.assertFalse(app.backend.running)
        self.assertFalse(app.transport.connected)
        self.assertFalse(app.engine.enabled)
        self.assertTrue(app.canvas.find_withtag("screen"))

    def test_destroyed_tk_cycles_are_collected_on_the_gui_thread(self):
        app = self.app()
        reference = weakref.ref(app)
        finalized = []
        weakref.finalize(app.status_var, lambda: finalized.append(threading.get_ident()))
        del app
        self._dispose_apps()
        self.assertIsNone(reference())
        self.assertEqual(finalized, [threading.main_thread().ident])

    @staticmethod
    def pairing_code():
        value={"host":"127.0.0.1","port":48193,"pin":"00"*32,"secret":"11"*32}
        return "crossdesk://v1/"+base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    def test_normal_start_restores_listener_without_showing_code(self):
        with patch("crossdesk.app.Discovery"):
            app=self.app(demo=False)
            profile={"mode":"listen","host":"127.0.0.1","port":48193}
            self.assertTrue(app._save_network_profile(profile))
            with patch.object(app.transport,"listen",return_value=self.pairing_code()) as listen, patch.object(app,"_show_code") as show:
                self.wait_for(lambda: listen.called)
                listen.assert_called_once_with(host="127.0.0.1",port=48193,advertise_host="127.0.0.1")
                show.assert_not_called()
            self.assertEqual(app._load_network_profile(),profile)
            if os.name!="nt":
                self.assertEqual(app.network_path.stat().st_mode&0o077,0)

    def test_normal_start_restores_connector_and_close_keeps_profile(self):
        with patch("crossdesk.app.Discovery"):
            app=self.app(demo=False)
            profile={"mode":"connect","code":self.pairing_code()}
            self.assertTrue(app._save_network_profile(profile))
            with patch.object(app.transport,"connect") as connect:
                self.wait_for(lambda: connect.called)
                connect.assert_called_once_with(profile["code"])
            app.close()
            self.assertEqual(json.loads(app.network_path.read_text()),profile)

    def test_manual_connect_is_remembered_after_authentication_only(self):
        with patch("crossdesk.app.Discovery"):
            app=self.app(demo=False)
            code=self.pairing_code()
            with patch("crossdesk.app.simpledialog.askstring",return_value=code), patch.object(app.transport,"connect") as connect:
                app._connect()
                self.wait_for(lambda: connect.called)
            self.assertFalse(app.network_path.exists())
            app.transport._connected.set()  # Simulates completed TLS, no socket or hooks.
            self.wait_for(app.network_path.exists)
            self.assertEqual(app._load_network_profile(),{"mode":"connect","code":code})
            self.assertNotIn(code,app.status_var.get())

    def test_manual_listen_remembers_and_disconnect_forgets(self):
        with patch("crossdesk.app.Discovery"):
            app=self.app(demo=False)
            app.address_var.set("127.0.0.1")
            with patch.object(app.transport,"listen",return_value=self.pairing_code()), patch.object(app,"_show_code"):
                app._listen()
                self.wait_for(app.network_path.exists)
                self.assertEqual(app._load_network_profile(),{"mode":"listen","host":"127.0.0.1","port":48193})
                app._disconnect()
                self.wait_for(lambda: not app.network_path.exists())
            app.close()
            self.assertFalse(app.network_path.exists())

    def test_demo_never_loads_changes_or_removes_network_profile(self):
        app=self.app()
        original=json.dumps({"mode":"connect","code":"private test marker"})
        app.network_path.write_text(original)
        self.assertIsNone(app._load_network_profile())
        self.assertFalse(app._save_network_profile({"mode":"listen","host":"127.0.0.1","port":48193}))
        self.assertIsNone(app._network_after)
        app._restore_network_profile()
        app._disconnect()
        self.wait_for(lambda: "断开" in app.status_var.get())
        app.close()
        self.assertEqual(app.network_path.read_text(),original)

    def test_invalid_profile_fails_closed_without_exposing_credentials(self):
        with patch("crossdesk.app.Discovery"):
            app=self.app(demo=False)
            secret="private-marker-that-must-not-appear"
            app.network_path.write_text(json.dumps({"mode":"connect","code":secret}))
            with patch.object(app.transport,"connect") as connect, patch.object(app.transport,"listen") as listen:
                # This test calls the timer callback early. Cancel its original
                # Tcl token before the callback clears the token reference.
                app.root.after_cancel(app._network_after)
                app._network_after=None
                app._restore_network_profile()
                self.wait_for(lambda: "配置无效" in app.status_var.get())
                connect.assert_not_called()
                listen.assert_not_called()
            self.assertNotIn(secret,app.status_var.get())

    def test_profile_write_failure_is_contained_and_cleans_temporary_file(self):
        with patch("crossdesk.app.Discovery"):
            app=self.app(demo=False)
            with patch("crossdesk.app.os.replace",side_effect=OSError("isolated write failure")):
                self.assertFalse(app._save_network_profile({"mode":"connect","code":self.pairing_code()}))
            self.assertFalse(app.network_path.exists())
            self.assertEqual(list(app.state_dir.glob(".network-*.tmp")),[])

    def test_mac_permission_button_uses_system_request_and_settings(self):
        backend=PermissionBackend()
        with patch("crossdesk.app.sys.platform","darwin"), patch("crossdesk.app.Discovery"), patch("crossdesk.app.subprocess.Popen") as opened, patch("crossdesk.app.messagebox.showinfo") as explained:
            app=self.app(demo=False,backend=backend)
            self.assertIsNotNone(app.permissions_button)
            app.permissions_button.invoke()
            self.assertEqual(backend.requests.count(True),1)
            self.assertIn("辅助功能和输入监控",app.status_var.get())
            self.assertIn("Privacy_Accessibility",opened.call_args.args[0][1])
            explained.assert_called_once()

    def test_normal_mac_start_requests_missing_permissions_once(self):
        backend=PermissionBackend()
        with patch("crossdesk.app.sys.platform","darwin"), patch("crossdesk.app.Discovery"), patch.dict(os.environ,{"SSH_CONNECTION":""}), patch("crossdesk.app.subprocess.Popen") as opened:
            app=self.app(demo=False,backend=backend)
            self.wait_for(lambda: True in backend.requests)
            self.assertEqual(backend.requests.count(True),1)
            self.assertGreaterEqual(backend.requests.count(False),1)
            self.assertIsNone(app._permissions_after)
            opened.assert_not_called()

    def test_demo_and_source_ssh_do_not_request_mac_permissions(self):
        backend=PermissionBackend()
        with patch("crossdesk.app.sys.platform","darwin"), patch("crossdesk.app.Discovery"), patch.dict(os.environ,{"SSH_CONNECTION":"isolated test"}):
            app=self.app(demo=False,backend=backend)
            self.assertIsNone(app._permissions_after)
            demo=self.app(backend=backend)
            self.assertIsNone(demo._permissions_after)
            self.assertIsNone(demo.permissions_button)
            demo._check_mac_permissions()
            self.assertNotIn(True,backend.requests)

    def test_preferences_tab_remains_reachable_at_minimum_height(self):
        with patch("crossdesk.app.sys.platform","darwin"), patch("crossdesk.app.Discovery"), patch.dict(os.environ,{"SSH_CONNECTION":"isolated test"}):
            app=self.app(demo=False,backend=PermissionBackend())
            app.root.geometry("1000x660+30+30")
            app.root.deiconify()
            app.root.update()
            self.assertGreaterEqual(app.root.winfo_height(),660)
            self.assertTrue(app.sidebar.winfo_ismapped())
            self.assertTrue(app.sidebar_scrollbar.winfo_ismapped())
            self.assertLess(app.sidebar.yview()[1],1)
            app.sidebar_tabs.select(1)
            app.root.update()
            top=app.sidebar_tabs.winfo_rooty()
            bottom=top+app.sidebar_tabs.winfo_height()
            for widget in (app.permissions_button,app.keyboard_check,app.swap_check):
                self.assertTrue(widget.winfo_ismapped())
                self.assertGreaterEqual(widget.winfo_rooty(),top)
                self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),bottom)

    def test_drag_snaps_edge_and_persists_layout(self):
        app = self.app()
        local = next(s for s in app.layout if s.machine == app.engine.machine["id"])
        remote = next(s for s in app.layout if s.name == "Mac 外接显示器")
        self.drag_to(app, remote, local.right + 5, local.y + 3)
        moved = app.engine.desktop.by_id[remote.id]
        self.assertEqual((moved.x, moved.y), (local.right, local.y))
        self.assertEqual((moved.native_x, moved.native_y), (remote.native_x, remote.native_y))
        saved = json.loads(app.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(next(row for row in saved["layout"] if row["id"] == moved.id)["x"], local.right)

    def test_drag_previews_a_real_crossing_seam_and_removing_it_clears_marker(self):
        app=self.app()
        local=next(s for s in app.layout if s.machine==app.engine.machine["id"])
        remote=next(s for s in app.layout if s.name=="Mac 外接显示器")
        self.assertTrue(app.canvas.find_withtag("connection"))
        point=self.point(app,remote)
        app._press(point)
        target=SimpleNamespace(x=point.x-1000*app.canvas_scale,y=point.y)
        app._motion(target)
        self.assertFalse(app.canvas.find_withtag("connection"))
        target=SimpleNamespace(x=point.x+(local.right+3-remote.x)*app.canvas_scale,
                               y=point.y+(local.y-remote.y)*app.canvas_scale)
        app._motion(target)
        self.assertTrue(app.canvas.find_withtag("connection"))
        moved=next(s for s in app.layout if s.id==remote.id)
        self.assertEqual(moved.x,local.right)

    def test_cursor_readiness_transition_wakes_router_and_timer_closes(self):
        app=self.app()
        controller=SimpleNamespace(local_ready=False,pending=False,hidden=False,close=lambda:None)
        controller.update=lambda remote:setattr(controller,"local_ready",True)
        app._cursor_controller=controller
        with patch.object(app.engine.events,"put_nowait") as wake:
            app._cursor_tick()
            wake.assert_called_once_with(("local_ready",{}))
        timer=app._cursor_after
        self.assertIsNotNone(timer)
        app.close()
        self.assertIsNone(app._cursor_after)

    def test_two_offline_launches_preserve_remote_layout_until_rediscovery(self):
        local=Screen("remember-local:Windows 显示器","remember-local","Windows 显示器",
                     0,0,2560,1440,0,0,2560,1440)
        remote=Screen("remember-mac:external","remember-mac","Mac 外接显示器",
                      -405,-1440,2560,1440,-1920,120,1920,1080)
        settings={"machine_id":local.machine,"layout":[local.as_dict(),remote.as_dict()]}
        description={"machine":{"id":remote.machine,"name":"Remembered Mac","platform":"darwin"},
                     "screens":[remote.as_dict()]}
        with patch("crossdesk.app.Discovery"):
            first=self.app(settings=settings,demo=False)
            state=first.state_dir
            self.assertEqual(set(first.engine.desktop.by_id),{local.id})
            first.close()
            second=self.app(demo=False,state_dir=state)
            self.assertEqual(set(second.engine.desktop.by_id),{local.id})
            self.assertEqual(second.engine.saved[remote.id],remote.as_dict())
            second.close()
            restored=self.app(demo=False,state_dir=state)
            restored.engine.add_discovered(description)
            self.assertEqual(restored.engine.desktop.by_id[remote.id],remote)
            self.assertFalse(restored.engine.peer_ready or restored.engine.enabled)

    def test_layout_edits_refresh_cache_before_peer_is_offline(self):
        remote=Screen("cache-mac:external","cache-mac","Mac 外接显示器",
                      0,0,1920,1080,-1920,0,1920,1080)
        description={"machine":{"id":remote.machine,"name":"Cached Mac","platform":"darwin"},
                     "screens":[remote.as_dict()]}
        with patch("crossdesk.app.Discovery"):
            app=self.app(demo=False)
            app.engine.add_discovered(description)
            edited=replace(remote,x=-1600,y=180,w=1600,h=900)
            app.engine.update_layout([edited if s.id==remote.id else s for s in app.engine.desktop.screens],False)
            app._save()
            self.assertEqual(app.engine.saved[remote.id],edited.as_dict())
            other=replace(remote,id="cache-other:external",machine="cache-other")
            app.engine.add_discovered({"machine":{"id":other.machine,"name":"Other Mac","platform":"darwin"},
                                       "screens":[other.as_dict()]})
            app._save()
            self.assertNotIn(remote.id,app.engine.desktop.by_id)
            saved=json.loads(app.settings_path.read_text(encoding="utf-8"))["layout"]
            self.assertEqual(next(row for row in saved if row["id"]==remote.id),edited.as_dict())
            app.engine.add_discovered(description)
            self.assertEqual(app.engine.desktop.by_id[remote.id],edited)

    def test_saved_layout_is_valid_bounded_and_current_displays_take_priority(self):
        app=self.app()
        current=list(app.engine.desktop.screens)
        history={"invalid":{"id":"invalid"},
                 "wrong-owner":replace(current[0],id="unrelated-id").as_dict()}
        history.update({f"history-{i}:display":replace(current[0],id=f"history-{i}:display",
                                                      machine=f"history-{i}").as_dict() for i in range(40)})
        history[current[0].id]=replace(current[0],x=12345).as_dict()
        app.engine.saved=history
        app._save()
        saved=json.loads(app.settings_path.read_text(encoding="utf-8"))["layout"]
        self.assertEqual(len(saved),32)
        self.assertEqual({row["id"]:row for row in saved},app.engine.saved)
        self.assertNotIn("invalid",app.engine.saved)
        self.assertNotIn("unrelated-id",app.engine.saved)
        for screen in current:
            self.assertEqual(app.engine.saved[screen.id],screen.as_dict())
        self.assertEqual(app.engine.desktop.screens,current)

    def test_drag_overlap_reverts_to_engine_layout(self):
        app = self.app()
        remote = next(s for s in app.layout if s.name == "Mac 外接显示器")
        before = list(app.engine.desktop.screens)
        self.drag_to(app, remote, 0, 0)
        self.assertEqual(app.layout, before)
        self.assertEqual(app.engine.desktop.screens, before)
        self.assertIn("重叠", app.status_var.get())

    def test_drag_save_failure_keeps_applied_layout_and_reports_error(self):
        app = self.app()
        remote = next(s for s in app.layout if s.name == "Mac 外接显示器")
        with patch.object(app, "_save", side_effect=OSError("read-only test state")):
            self.drag_to(app, remote, 2560, 0)
        self.assertEqual(app.engine.desktop.by_id[remote.id].x, 2560)
        self.assertEqual(app.layout, app.engine.desktop.screens)
        self.assertIn("保存失败", app.status_var.get())

    def test_layout_notification_cancels_inflight_drag(self):
        app = self.app()
        screen = app.layout[0]
        app._press(self.point(app, screen))
        self.assertIsNotNone(app.drag)
        thread = threading.Thread(target=lambda: app.notify("layout", list(app.engine.desktop.screens)))
        thread.start()
        thread.join()
        self.wait_for(lambda: app.drag is None)

    def test_discovery_selection_adds_preview_without_permission(self):
        app = self.app()
        remote = {"id": "test-other", "name": "另一台 Mac", "platform": "darwin"}
        screen = Screen("test-other:screen", remote["id"], "外接屏", 0, 0, 1920, 1080, 0, 0, 1920, 1080)
        description = {"machine": remote, "screens": [screen.as_dict()],
                       "address": "127.0.0.1", "seen": time.monotonic()}
        producer = threading.Thread(target=lambda: app.notify("discovered", description))
        producer.start()
        producer.join()
        self.wait_for(lambda: app.peer_tree.exists(remote["id"]))
        app.peer_tree.selection_set(remote["id"])
        app._add_selected_peer()
        self.wait_for(lambda: screen.id in {s.id for s in app.layout})
        self.assertFalse(app.engine.peer_ready)
        self.assertFalse(app.engine.enabled)
        self.assertFalse(app.transport.connected)

    def test_two_apps_exchange_and_save_layout_over_loopback_tls(self):
        left, right = self.app(), self.app()
        code = left.transport.listen("127.0.0.1", 0)
        right.transport.connect(code)
        self.wait_for(lambda: left.engine.peer_ready and right.engine.peer_ready)
        self.wait_for(lambda: len(left.layout) == len(right.layout) == 2)
        remote = next(s for s in left.layout if s.machine != left.engine.machine["id"])
        adjusted = [replace(s, y=100) if s.id == remote.id else s for s in left.layout]
        left.engine.update_layout(adjusted)
        self.wait_for(lambda: right.engine.desktop.by_id[remote.id].y == 100)
        self.wait_for(lambda: any(row["id"] == remote.id and row["y"] == 100 for row in
                      json.loads(right.settings_path.read_text(encoding="utf-8"))["layout"]))
        self.assertIn("已加密连接", left.connection_var.get())
        self.assertFalse(left.backend.running or right.backend.running)

    def test_one_app_start_and_other_app_pause_control_both_backends(self):
        with patch("crossdesk.app.Discovery"):
            left,right=self.app(demo=False),self.app(demo=False)
            code=left.transport.listen("127.0.0.1",0)
            right.transport.connect(code)
            self.wait_for(lambda:left.engine.peer_ready and right.engine.peer_ready)
            self.assertFalse(left.engine.enabled or right.engine.enabled)
            left._toggle()
            self.wait_for(lambda:left.engine.enabled and right.engine.enabled and left.engine.peer_active and right.engine.peer_active)
            self.wait_for(lambda:"双端互控已启用" in left.connection_var.get() and "双端互控已启用" in right.connection_var.get())
            self.assertEqual(str(left.enable_button.cget("text")),"暂停互控")
            self.assertEqual(str(right.enable_button.cget("text")),"暂停互控")
            right._toggle()
            self.wait_for(lambda:not left.engine.enabled and not right.engine.enabled)
            self.assertFalse(left.backend.running or right.backend.running)

    def test_local_toggle_action_ignores_repeated_click_until_complete(self):
        with patch("crossdesk.app.Discovery"):
            app=self.app(demo=False)
            entered,release=threading.Event(),threading.Event()
            def wait_enabled(_):
                entered.set()
                release.wait(1)
            with patch.object(app.engine,"set_enabled",side_effect=wait_enabled) as enabled:
                app._toggle()
                self.assertTrue(entered.wait(1))
                app._toggle()
                self.assertEqual(enabled.call_count,1)
                release.set()
                self.wait_for(lambda:not app._toggle_busy)

    def test_status_diagnostics_are_private_safe_and_reflect_state_changes(self):
        with patch("crossdesk.app.Discovery"):
            app=self.app(demo=False)
            app.status_var.set("测试 crossdesk://v1/private-secret-code "+"ab"*32)
            app.backend.metrics=lambda:{"move_seen":7,"tap_enabled":False,"key":"must not appear"}
            app._last_permission_check=0
            app._write_diagnostics(force=True)
            self.wait_for(lambda:app._diagnostic_metrics.get("move_seen")==7)
            app._write_diagnostics(force=True)
            path=app.state_dir/"status.json"
            text=path.read_text(encoding="utf-8")
            snapshot=json.loads(text)
            self.assertEqual(snapshot["backend_metrics"],{"move_seen":7,"tap_enabled":False})
            self.assertNotIn("private-secret-code",text)
            self.assertNotIn("ab"*32,text)
            self.assertNotIn("must not appear",text)
            self.assertIn("updated_at",snapshot)
            self.assertIn("version",snapshot)
            self.assertNotIn("held_keys",snapshot)
            with patch("crossdesk.app.os.replace") as replaced:
                app._write_diagnostics()
                replaced.assert_not_called()
            app.engine.held_buttons.add("left")
            app._write_diagnostics()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["held_buttons_count"],1)
            app.close()
            final=json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(final["connected"] or final["enabled"] or final["backend_running"])

    def test_demo_does_not_write_diagnostic_status(self):
        app=self.app()
        app._write_diagnostics(force=True)
        app.close()
        self.assertFalse((app.state_dir/"status.json").exists())

    @staticmethod
    def freeze_router(app):
        app.engine.closed.set()
        app.engine.worker.join(1)

    @staticmethod
    def poll_once(app):
        if app._poll_after is not None:
            app.root.after_cancel(app._poll_after)
            app._poll_after=None
        app._poll()

    def test_keyboard_shortcut_and_clipboard_preferences_persist(self):
        app=self.app()
        app.keyboard_var.set(False)
        app.swap_var.set(False)
        app.clipboard_var.set(False)
        app._preferences()
        state=app.state_dir
        self.assertEqual(json.loads(app.settings_path.read_text(encoding="utf-8"))["preferences"],
                         {"keyboard":False,"shortcuts":False,"clipboard":False,"handoff_effect":True})
        machine=app.engine.machine["id"]
        app.close()
        reopened=self.app(state_dir=state)
        self.assertFalse(reopened.engine.keyboard or reopened.engine.swap_modifiers or reopened._clipboard_enabled)
        self.assertFalse(reopened.keyboard_var.get() or reopened.swap_var.get() or reopened.clipboard_var.get())
        self.assertEqual(reopened.engine.machine["id"],machine)

    def test_active_session_preferences_are_rejected_without_changing_saved_values(self):
        app=self.app()
        self.freeze_router(app)
        app.engine.enabled=True
        original=app.settings_path.read_bytes()
        for variable in (app.keyboard_var,app.swap_var,app.clipboard_var):variable.set(False)
        app._preferences()
        self.assertTrue(app.engine.keyboard and app.engine.swap_modifiers and app._clipboard_enabled)
        self.assertTrue(app.keyboard_var.get() and app.swap_var.get() and app.clipboard_var.get())
        self.assertEqual(app.settings_path.read_bytes(),original)

    def test_handoff_preference_persists_and_can_disable_during_control(self):
        app=self.app()
        self.freeze_router(app)
        app.engine.enabled=True
        app.handoff_var.set(False)
        app._handoff_preferences()
        self.assertFalse(app._handoff_enabled or app._handoff_effect.enabled)
        self.assertTrue(app.engine.enabled)
        self.assertFalse(json.loads(app.settings_path.read_text(encoding="utf-8"))["preferences"]["handoff_effect"])

    def test_handoff_queue_keeps_latest_and_discards_stale_or_paused_effects(self):
        app=self.app()
        self.freeze_router(app)
        effect=Mock()
        app._handoff_effect=effect
        app.engine.enabled=app.engine.peer_ready=app.engine.peer_active=True
        app.transport._connected.set()
        for i in range(6):app.notify("handoff",{"phase":"arrive","created_at":time.monotonic(),"id":i})
        self.assertEqual(len(app._handoff_events),2)
        app._update_handoff()
        self.assertEqual(effect.play.call_count,1)
        self.assertEqual(effect.play.call_args.args[0]["id"],5)
        app.notify("handoff",{"created_at":time.monotonic()-2})
        app._update_handoff()
        self.assertEqual(effect.play.call_count,1)
        app.engine.enabled=False
        app.notify("handoff",{"created_at":time.monotonic()})
        app._update_handoff()
        self.assertFalse(app._handoff_events)
        effect.cancel.assert_called()

    def test_preview_uses_local_shared_edge_and_close_cancels_preview(self):
        app=self.app()
        effect=Mock(last_error=None)
        app._handoff_effect=effect
        app._preview_handoff()
        self.assertEqual(effect.play.call_count,1)
        event=effect.play.call_args.args[0]
        self.assertEqual(event["phase"],"depart")
        self.assertEqual(event["edge"],"left")
        self.assertIsNotNone(app._handoff_preview_after)
        self.assertTrue(app.close())
        self.assertIsNone(app._handoff_preview_after)
        effect.close.assert_called_once()

    def test_clipboard_poll_and_receive_require_both_peer_and_local_opt_in(self):
        app=self.app()
        self.freeze_router(app)
        states,native_reads,received=[],[],[]
        def poll(*,active):
            states.append(active)
            if active:native_reads.append(True)
        app.clipboard=SimpleNamespace(poll=poll,receive=lambda message:received.append(message),close=lambda:None)
        app.engine.enabled=app.engine.peer_ready=app.engine.peer_active=True
        app.transport._connected.set()
        app._clipboard_enabled=True
        app._peer_clipboard_enabled=True
        app._peer_clipboard_epoch=app.transport._session_epoch
        message={"type":"clipboard_offer","id":"mock-transfer"}
        self.poll_once(app)
        self.assertTrue(states[-1])
        app._receive_peer_message(message)
        self.assertEqual(received,[message])
        for field in ("local","peer","enabled","ready","active","connected","pending"):
            with self.subTest(gate=field):
                app._clipboard_enabled=app._peer_clipboard_enabled=True
                app.engine.enabled=app.engine.peer_ready=app.engine.peer_active=True
                app.engine._pending_control=None
                app.transport._connected.set()
                app._peer_clipboard_epoch=app.transport._session_epoch
                if field=="local":app._clipboard_enabled=False
                elif field=="peer":app._peer_clipboard_enabled=False
                elif field=="enabled":app.engine.enabled=False
                elif field=="ready":app.engine.peer_ready=False
                elif field=="active":app.engine.peer_active=False
                elif field=="connected":app.transport._connected.clear()
                else:app.engine._pending_control={"enabled":True}
                reads=len(native_reads)
                accepted=len(received)
                self.poll_once(app)
                self.assertFalse(states[-1])
                self.assertEqual(len(native_reads),reads)
                app._receive_peer_message(message)
                self.assertEqual(len(received),accepted)
        app.engine._pending_control=None

    def test_peer_clipboard_disabled_setting_arrives_when_local_off_or_before_hello(self):
        app=self.app()
        self.freeze_router(app)
        app.transport._connected.set()
        app._clipboard_enabled=False
        app.engine.enabled=app.engine.peer_ready=app.engine.peer_active=False
        app._peer_clipboard_enabled=True
        broker=Mock()
        app.clipboard=broker
        app._receive_peer_message({"type":"clipboard_settings","enabled":False})
        self.assertFalse(app._peer_clipboard_enabled)
        self.assertEqual(app._peer_clipboard_epoch,app.transport._session_epoch)
        app._receive_peer_message({"type":"clipboard_offer","id":"mock-transfer"})
        broker.receive.assert_not_called()
        app._receive_peer_message({"type":"clipboard_settings","enabled":"true"})
        self.assertFalse(app._peer_clipboard_enabled)
        app._receive_peer_message({"type":"clipboard_settings","enabled":True})
        self.assertTrue(app._peer_clipboard_enabled)
        app.transport._connected.clear()
        app._sync_clipboard_preferences()
        self.assertFalse(app._peer_clipboard_enabled)

    def test_clipboard_outbound_chunks_check_live_consent_before_next_gui_poll(self):
        app=self.app()
        self.freeze_router(app)
        message={"type":"clipboard_chunk","id":"mock-transfer","index":0,"data":"mock"}
        with patch.object(app.transport,"send",return_value=True) as send:
            for gate in (None,"pending","paused","peer_opt_out","local_opt_out","not_ready","peer_paused","disconnected"):
                with self.subTest(gate=gate):
                    app._clipboard_enabled=app._peer_clipboard_enabled=True
                    app.engine.enabled=app.engine.peer_ready=app.engine.peer_active=True
                    app.engine._pending_control=None
                    app.transport._connected.set()
                    if gate=="pending":app.engine._pending_control={"enabled":True}
                    elif gate=="paused":app.engine.enabled=False
                    elif gate=="peer_opt_out":app._peer_clipboard_enabled=False
                    elif gate=="local_opt_out":app._clipboard_enabled=False
                    elif gate=="not_ready":app.engine.peer_ready=False
                    elif gate=="peer_paused":app.engine.peer_active=False
                    elif gate=="disconnected":app.transport._connected.clear()
                    send.reset_mock()
                    # Deliberately no GUI poll between the state change and send.
                    self.assertEqual(app._send_clipboard(message),gate is None)
                    if gate is None:send.assert_called_once_with(message)
                    else:send.assert_not_called()
        app.engine._pending_control=None

    def test_clipboard_peer_opt_in_is_not_reused_after_transport_epoch_changes(self):
        app=self.app()
        self.freeze_router(app)
        app.transport._connected.set()
        app.engine.peer_ready=True
        app._peer_clipboard_enabled=True
        app._peer_clipboard_epoch=app.transport._session_epoch
        app.transport._session_epoch+=1
        with patch.object(app.transport,"send",return_value=True) as send:
            app._sync_clipboard_preferences()
        self.assertFalse(app._peer_clipboard_enabled)
        send.assert_called_once_with({"type":"clipboard_settings","enabled":True})

    def test_app_close_stops_clipboard_before_cursor_and_router(self):
        app=self.app()
        order=[]
        app.clipboard=SimpleNamespace(close=lambda:order.append("clipboard"))
        app._cursor_controller=SimpleNamespace(close=lambda:order.append("cursor"),local_ready=True)
        original_close=app.engine.close
        def close_engine():
            order.append("engine")
            original_close()
        with patch.object(app.engine,"close",side_effect=close_engine):
            self.assertTrue(app.close())
            self.assertTrue(app.close())
        self.assertEqual(order,["clipboard","cursor","engine"])

    def test_update_inspection_failure_never_pauses_or_closes_app(self):
        from crossdesk.updates import UpdateError
        app=self.app()
        app.demo=False
        manager=Mock()
        manager.inspect.side_effect=UpdateError("mock invalid signature")
        with patch("crossdesk.updates.UpdateManager",return_value=manager), \
             patch("crossdesk.app.filedialog.askopenfilename",return_value=str(app.state_dir/"invalid.crossdesk-update")), \
             patch("crossdesk.app.messagebox.showerror") as show, \
             patch.object(app.engine,"set_enabled") as pause:
            app._import_update()
        show.assert_called_once()
        pause.assert_not_called()
        manager.stage.assert_not_called()
        manager.restart.assert_not_called()
        self.assertFalse(app._closed or app._update_busy)

    def test_update_staging_failure_keeps_current_session_and_does_not_restart(self):
        app=self.app()
        self.freeze_router(app)
        app.engine.enabled=True
        manager=Mock()
        def fail_stage():raise OSError("mock stage failure")
        with patch.object(app.engine,"set_enabled") as pause, \
             patch("crossdesk.app.messagebox.showerror"):
            app._queue_upgrade(fail_stage,manager)
            self.wait_for(lambda:not app._update_busy)
        pause.assert_not_called()
        manager.restart.assert_not_called()
        self.assertTrue(app.engine.enabled)
        self.assertFalse(app._closed)
        self.assertEqual(str(app.upgrade_button.cget("state")),"normal")

    def test_successful_update_closes_resources_before_mock_restart(self):
        app=self.app()
        order=[]
        manager=Mock()
        def restart():
            self.assertTrue(app._closed)
            self.assertTrue(app.engine.closed.is_set())
            self.assertFalse(app.engine.worker.is_alive())
            order.append("restart")
        manager.restart.side_effect=restart
        original_close=app.close
        def close_app():
            order.append("close")
            return original_close()
        with patch.object(app.engine,"set_enabled",side_effect=lambda enabled:order.append(("enabled",enabled))), \
             patch.object(app,"close",side_effect=close_app):
            app._queue_upgrade(lambda:order.append("stage"),manager)
            self.wait_for(lambda:manager.restart.called)
        self.assertEqual(order,["stage",("enabled",False),"close","restart"])

    def test_update_does_not_restart_if_resource_cleanup_fails(self):
        app=self.app()
        manager=Mock()
        with patch.object(app,"close",return_value=False):
            app._restart_update(manager)
        manager.restart.assert_not_called()

    def test_keyboard_readiness_callback_reads_cached_manager_flag_only(self):
        app=self.app()
        controller=SimpleNamespace(local_ready=False)
        app._cursor_controller=controller
        self.assertFalse(app.engine.local_keyboard_ready())
        controller.local_ready=True
        self.assertTrue(app.engine.local_keyboard_ready())
        app._cursor_controller=None
        self.assertTrue(app.engine.local_keyboard_ready())

    def test_source_cursor_is_hidden_only_for_an_active_outgoing_route(self):
        app = self.app()
        app.engine.closed.set()
        app.engine.worker.join(1)
        requests = []
        app._cursor_controller = SimpleNamespace(update=lambda remote: requests.append(remote), close=lambda: None, hidden=False)
        engine = app.engine
        engine.enabled = engine.peer_ready = engine.peer_active = True
        app.backend.running = True
        app.transport._connected.set()  # Simulated connection, no native input.
        remote = next(s for s in engine.desktop.screens if s.machine != engine.machine["id"])
        local = next(s for s in engine.desktop.screens if s.machine == engine.machine["id"])
        engine.current = remote.id
        engine.keyboard_target = remote.machine
        app._update_source_cursor()
        self.assertEqual(requests, [True])
        # Pointer owner is global; current/incoming describe only local routing.
        engine.current=local.id
        engine.incoming=True
        app._update_source_cursor()
        self.assertTrue(requests[-1])
        for field, value in (("enabled", False), ("peer_active", False)):
            original = getattr(engine, field)
            setattr(engine, field, value)
            app._update_source_cursor()
            self.assertFalse(requests[-1])
            setattr(engine, field, original)
        engine.current = local.id
        engine.keyboard_target=local.machine
        app._update_source_cursor()
        self.assertFalse(requests[-1])
        engine.current = remote.id
        engine.keyboard_target=remote.machine
        app.backend.running = False
        app._update_source_cursor()
        self.assertFalse(requests[-1])
        app.backend.running = True
        app.transport._connected.clear()
        app._update_source_cursor()
        self.assertFalse(requests[-1])

    def test_source_cursor_exception_with_successful_cleanup_does_not_stop_routing(self):
        app = self.app()
        closed = []
        def broken_update(_remote):
            raise RuntimeError("private foreground app details")
        app._cursor_controller = SimpleNamespace(update=broken_update, close=lambda: closed.append(True), hidden=True)
        with patch.object(app.engine.emergency, "set") as emergency:
            app._update_source_cursor()
            emergency.assert_not_called()
        self.assertEqual(closed, [True])
        self.assertIsNone(app._cursor_controller)
        self.assertNotIn("private foreground", app.status_var.get())

    def test_source_cursor_restore_failure_still_stops_input(self):
        app = self.app()
        app.engine.closed.set()
        app.engine.worker.join(1)
        app.engine.enabled=True
        app._cursor_controller=SimpleNamespace(update=lambda _: 'restore failed', close=lambda: None)
        with patch.object(app.engine.emergency, 'set') as emergency:
            app._update_source_cursor()
            emergency.assert_called_once_with()
        self.assertEqual(app.engine._emergency_reason, 'input_restore_failed')

    def test_stop_reason_is_not_overwritten_by_generic_pause_or_clipboard_status(self):
        app = self.app()
        app.engine._record_stop('position_failed')
        for text in ('对方已暂停互控', '剪贴板传输已完成'):
            app.notify('status', text)
        app._poll()
        self.assertEqual(app.engine.last_stop['code'], 'position_failed')
        app.demo=False
        app._refresh_session_ui()
        self.assertIn('定位失败', app.connection_var.get())

    def test_gui_poll_reconciles_source_cursor_on_main_thread(self):
        app = self.app()
        requests = []
        app._cursor_controller = SimpleNamespace(update=lambda remote: requests.append((remote, threading.get_ident())), close=lambda: None, hidden=False)
        self.wait_for(lambda: bool(requests))
        self.assertEqual(requests[-1], (False, threading.main_thread().ident))

    def test_close_restores_source_cursor_before_router_cleanup_can_fail(self):
        app = self.app()
        order = []
        app._cursor_controller = SimpleNamespace(update=lambda _remote: None, close=lambda: order.append("cursor"), hidden=False)
        original_close = app.engine.close
        def fail_after_cleanup():
            order.append("engine")
            original_close()
            raise RuntimeError("simulated router cleanup failure")
        with patch.object(app.engine, "close", side_effect=fail_after_cleanup):
            self.assertFalse(app.close())
        self.assertEqual(order, ["cursor", "engine"])
        app.close()
        self.assertEqual(order, ["cursor", "engine"])

    def test_close_reports_cursor_restore_error_and_continues_input_cleanup(self):
        app = self.app()
        app._cursor_controller = SimpleNamespace(update=lambda _remote: None, close=lambda: "Mac 光标恢复未完成", hidden=True)
        self.assertFalse(app.close())
        self.assertIsInstance(app.close_error, RuntimeError)
        self.assertFalse(app.engine.worker.is_alive())
        self.assertFalse(app.backend.running)
        self.assertIn("错误", app.status_var.get())

    def test_mac_standard_quit_releases_resources(self):
        with patch("crossdesk.app.sys.platform","darwin"):
            app=self.app()
            self.assertTrue(app.root.tk.call("info","commands","tk::mac::Quit"))
            app.root.tk.call("tk::mac::Quit")
            self.assertTrue(app._closed)
            self.assertFalse(app.engine.worker.is_alive())
            self.assertFalse(app._mac_quit_command)

    def test_mainloop_return_or_keyboard_interrupt_always_closes_app(self):
        for interrupted in (False,True):
            with self.subTest(interrupted=interrupted):
                created=[]
                def remember(*args,**kwargs):
                    app=App(*args,**kwargs)
                    app.root.update_idletasks()
                    created.append(app)
                    self.apps.append(app)
                    return app
                state=str(Path(self.temporary.name)/("main-interrupted" if interrupted else "main-returned"))
                loop=patch("tkinter.Tk.mainloop",side_effect=KeyboardInterrupt) if interrupted else patch("tkinter.Tk.mainloop",return_value=None)
                with patch("sys.argv",["crossdesk","--demo","--state-dir",state]), patch("crossdesk.app.App",side_effect=remember), loop, patch("crossdesk.__main__.signal.signal") as registered:
                    self.assertEqual(main(),130 if interrupted else 0)
                    registered.assert_not_called()  # Demo never alters host signals.
                self.assertEqual(len(created),1)
                self.assertTrue(created[0]._closed)
                self.assertFalse(created[0].engine.worker.is_alive())

    def test_normal_main_sigterm_schedules_close_and_restores_handler(self):
        created=[]
        installed=[]
        previous=signal.SIG_DFL
        def remember(*args,**kwargs):
            app=App(*args,**kwargs)
            app.root.update_idletasks()
            created.append(app)
            self.apps.append(app)
            return app
        def register(signum,handler):
            installed.append((signum,handler))
            return previous
        def loop():
            installed[0][1](signal.SIGTERM,None)
            created[0].root.update()
        state=str(Path(self.temporary.name)/"main-sigterm")
        with patch("sys.argv",["crossdesk","--state-dir",state]), patch("crossdesk.app.App",side_effect=remember), patch("crossdesk.app.Discovery"), patch("crossdesk.backends.native_backend",return_value=DemoBackend()), patch("tkinter.Tk.mainloop",side_effect=loop), patch("crossdesk.__main__.signal.getsignal",return_value=previous), patch("crossdesk.__main__.signal.signal",side_effect=register):
            self.assertEqual(main(),0)
        self.assertEqual(len(installed),2)
        self.assertEqual(installed[-1],(signal.SIGTERM,previous))
        self.assertTrue(created[0]._closed)
        self.assertFalse(created[0].engine.worker.is_alive())

    def test_invalid_settings_container_does_not_prevent_startup(self):
        app = self.app(settings=[])
        self.assertEqual(len(app.layout), 3)
        self.assertEqual(settings_port(app.settings_path), 48193)

    def test_close_releases_resources_even_if_settings_write_fails(self):
        app = self.app()
        with patch.object(app, "_save", side_effect=OSError("read-only test state")):
            self.assertFalse(app.close())
        self.assertTrue(app.engine.closed.is_set())
        self.assertFalse(app.engine.worker.is_alive())
        self.assertFalse(app.transport.connected)
        self.assertIsNone(app._poll_after)
        self.assertFalse(app.close())  # Idempotent, retains the shutdown result.
        ran = threading.Event()
        app._async(ran.set)
        self.assertFalse(ran.wait(0.05))

    def test_smoke_assertion_failure_exits_nonzero_and_cleans_up(self):
        created = []

        def broken_app(*args, **kwargs):
            app = App(*args, **kwargs)
            app.canvas.find_all = lambda: ()
            created.append(app)
            self.apps.append(app)
            return app

        state = str(Path(self.temporary.name) / "smoke-failure")
        with patch("sys.argv", ["crossdesk", "--smoke-test", "--state-dir", state]), patch("crossdesk.app.App", side_effect=broken_app):
            self.assertEqual(main(), 1)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].engine.closed.is_set())
        self.assertFalse(created[0].engine.worker.is_alive())


if __name__ == "__main__":
    unittest.main()
