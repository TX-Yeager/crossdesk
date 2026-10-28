"""Local desktop configuration UI; layout uses the very same Screen model as routing."""
from dataclasses import replace
from collections import deque
from datetime import datetime, timezone
import json
import ipaddress
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

from crossdesk import __version__
from crossdesk import diagnostics
from crossdesk.discovery import Discovery, lan_addresses
from crossdesk.engine import Engine
from crossdesk.geometry import Desktop, Screen, snap_screen, screen_connections
from crossdesk.transport import PeerTransport, _parse_pairing_code
from crossdesk.theme import BG, CARD, INK, MUTED, BLUE, GREEN, apply_theme, rounded_rectangle


def default_state_dir():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home()/"AppData/Local"))/"CrossDesk"
    return Path.home()/"Library/Application Support/CrossDesk"


class DemoBackend:
    """Preview/test backend. It never reads, captures, or injects OS input."""
    running = False
    def screens(self):
        return [{"name":"Windows 显示器","x":0,"y":0,"width":2560,"height":1440}]
    def position(self):
        return (1280,720)
    def supported_keys(self):
        return set()
    def start(self, handler):
        self.running=True
    def stop(self):
        self.running=False
    def release_all(self):
        pass
    def inject(self, event):
        pass
    def warp(self, x, y):
        pass


class App:
    def __init__(self, root, backend, state_dir, demo=False):
        self.root, self.backend, self.state_dir, self.demo = root, backend, Path(state_dir), demo
        self._closed = False
        self._close_reason='app_exit'
        self._poll_after = None
        self._permissions_after = None
        self._network_after = None
        self._network_generation = 0
        self._pending_connect_profile = None
        self._profile_save_scheduled = False
        self._toggle_busy = False
        self._last_diagnostic_payload = None
        self._last_diagnostic_at = 0.0
        self._last_permission_check = 0.0
        self._diagnostic_permissions = {}
        self._diagnostic_metrics = {}
        self._diagnostic_probe_busy = False
        self._mac_quit_command = False
        self._cursor_controller = None
        self._cursor_controller_error = None
        self._cursor_warning = None
        self._cursor_after = None
        self._handoff_events = deque(maxlen=2)
        self._handoff_preview_after = None
        self._handoff_preview_until = 0.0
        self._handoff_effect = None
        self.clipboard = None
        self._peer_clipboard_enabled = False
        self._peer_clipboard_epoch = None
        self._sent_clipboard_settings = None
        self._clipboard_settings_at = 0.0
        self._update_busy = False
        self._action_lock = threading.RLock()
        self.close_error = None
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_path=diagnostics.configure(self.state_dir)
        diagnostics.event('app_started',version=__version__,platform=sys.platform)
        self.root.report_callback_exception=self._report_callback_exception
        self.settings_path=self.state_dir/"settings.json"
        self.network_path=self.state_dir/"network.json"
        try:
            settings=json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            settings={}
        if not isinstance(settings, dict):
            settings = {}
        machine_id = settings.get("machine_id")
        if not isinstance(machine_id, str) or not 0 < len(machine_id) <= 100:
            machine_id = uuid.uuid4().hex
        saved_layout = settings.get("layout")
        if not isinstance(saved_layout, list):
            saved_layout = None
        machine={"id":machine_id,"name":socket.gethostname(),"platform":sys.platform}
        self.messages=queue.Queue()
        self.engine=Engine(backend,machine,self.notify,saved_layout)
        self.engine.local_keyboard_ready = lambda: getattr(self._cursor_controller, "local_ready", True)
        preferences = settings.get("preferences", {})
        if not isinstance(preferences, dict):
            preferences = {}
        self.engine.keyboard = preferences.get("keyboard", True) is not False
        self.engine.swap_modifiers = preferences.get("shortcuts", True) is not False
        self._clipboard_enabled = preferences.get("clipboard", True) is not False
        self._handoff_enabled = preferences.get("handoff_effect", True) is not False
        from crossdesk.handoff_effect import HandoffEffect
        self._handoff_effect = HandoffEffect(self.root, enabled=self._handoff_enabled)
        self.transport=PeerTransport(self.state_dir/"identity",self._receive_peer_message,lambda text:self.notify("status",text))
        self.engine.transport=self.transport
        self.peers={}
        self.machine_names={machine["id"]:machine["name"]+" · 本机"}
        self.layout=list(self.engine.desktop.screens)
        self.selected=None
        self.drag=None
        self.canvas_scale=.2
        self.canvas_offset=(50,50)
        self.zoom=1.0
        self.pair_code=""
        self.root.title(f"CrossDesk 跨屏 · {__version__}"+(" · 布局演示" if demo else ""))
        self.ui_scale=max(1.0,min(1.5,float(self.root.tk.call("tk","scaling"))/(4/3)))
        width=min(round(1220*self.ui_scale),self.root.winfo_screenwidth()-60)
        height=min(round(800*self.ui_scale),self.root.winfo_screenheight()-100)
        self.root.geometry(f"{max(950,width)}x{max(660,height)}")
        self.root.minsize(950,660)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW",self.close)
        if sys.platform=="darwin":
            self.root.createcommand("tk::mac::Quit",self.close)
            self._mac_quit_command=True
        self._build()
        if not demo and backend.__class__.__module__ in ("crossdesk.backends.windows", "crossdesk.backends.macos"):
            from crossdesk.clipboard_native import create_clipboard
            from crossdesk.clipboard_sync import ClipboardSync
            self.clipboard = ClipboardSync(create_clipboard(window_handle=self.root.winfo_id()),
                                           self.state_dir, machine["id"], self._send_clipboard, self.notify)
            self.engine.before_paste = self.clipboard.before_paste
        if sys.platform == "darwin" and not demo:
            from crossdesk.backends.macos import MacOSBackend
            if isinstance(backend, MacOSBackend):
                try:
                    from crossdesk.backends.mac_cursor import MacForegroundCursor
                    self._cursor_controller = MacForegroundCursor(self.root)
                    self._cursor_after = self.root.after(8, self._cursor_tick)
                except Exception:
                    self._cursor_warning = "Mac 源光标隐藏不可用，跨屏可继续工作。"
        if sys.platform == "win32" and not demo:
            from crossdesk.backends.windows import WindowsBackend
            if isinstance(backend,WindowsBackend):
                try:
                    from crossdesk.backends.windows_cursor import WindowsCursor
                    self._cursor_controller=WindowsCursor(self.root)
                    backend.before_pointer_inject=self._cursor_controller.ensure_visible
                    backend.before_remote_anchor=self._cursor_controller.ensure_hidden
                    backend.after_pointer_position=self._cursor_controller.restore_after_position
                    self._cursor_after=self.root.after(8,self._cursor_tick)
                except Exception:
                    self._cursor_warning="Windows 源光标隐藏不可用，跨屏可继续工作。"
        self._save()
        if demo:
            remote={"id":"demo-mac","name":"MacBook Pro","platform":"darwin"}
            self.machine_names[remote["id"]]="MacBook Pro · 演示"
            raw=[]
            for name,nx,ny,w,h,x,y in [("Mac 外接显示器",0,-1440,2560,1440,-2560,0),("MacBook 内建显示器",560,0,1440,900,-2000,1440)]:
                raw.append(Screen("demo-mac:"+name,"demo-mac",name,nx,ny,w,h,x,y,w,h).as_dict())
            self.engine.add_discovered({"machine":remote,"screens":raw})
            with self.engine.lock:
                arranged=[]
                for s in self.engine.desktop.screens:
                    template=next((r for r in raw if r["id"]==s.id),None)
                    arranged.append(Screen.from_dict(template) if template else s)
                self.engine.update_layout(arranged,False)
            self.notify("status","布局演示：可拖动、对齐和缩放显示器；不会捕获或注入任何输入。")
            self.discovery=None
        else:
            self.discovery=Discovery(self.engine.describe,lambda peer:self.notify("discovered",peer),lambda text:self.notify("status",text))
            self.discovery.start()
        self._poll_after = self.root.after(80,self._poll)
        if not self.demo:
            self._network_after=self.root.after(500,self._restore_network_profile)
        if (sys.platform == "darwin" and not self.demo
                and (getattr(sys, "frozen", False) or not os.environ.get("SSH_CONNECTION"))):
            self._permissions_after = self.root.after(500, self._check_mac_permissions)
        if not self.demo:
            from crossdesk.updates import confirm_started
            confirm_started(self.state_dir)

    def notify(self,kind,value):
        if not self._closed:
            if kind=="handoff":
                self._handoff_events.append(value)
            else:
                self.messages.put((kind,value))

    def _receive_peer_message(self, message):
        if message.get("type") == "clipboard_settings":
            if self.transport.connected and isinstance(message.get("enabled"), bool):
                self._peer_clipboard_enabled = message["enabled"]
                self._peer_clipboard_epoch = getattr(self.transport, "_session_epoch", None)
            return
        if str(message.get("type", "")).startswith("clipboard_"):
            if (self.clipboard is not None and self._clipboard_enabled and self.engine.enabled
                    and self._peer_clipboard_enabled and self.engine.peer_ready
                    and self.engine.peer_active and self.transport.connected and not self.engine.control_pending):
                self.clipboard.receive(message)
            return
        self.engine.receive(message)

    def _send_clipboard(self, message):
        # Workers check live session consent for every chunk, including the
        # interval before the next GUI poll observes a pause or peer opt-out.
        if not (self._clipboard_enabled and self._peer_clipboard_enabled and self.engine.enabled
                and self.engine.peer_ready and self.engine.peer_active and self.transport.connected
                and not self.engine.control_pending):
            return False
        return self.transport.send(message)

    def _import_update(self):
        if self.demo:
            self.status_var.set("布局演示不安装更新。")
            return
        if self._update_busy:
            return
        from crossdesk.updates import UpdateManager, UpdateError
        path=filedialog.askopenfilename(parent=self.root,title="选择 CrossDesk 更新包",
                                       filetypes=[("CrossDesk 更新包","*.crossdesk-update")])
        if not path:
            return
        manager=UpdateManager(self.state_dir)
        try:
            metadata=manager.inspect(path)
        except (UpdateError,OSError):
            messagebox.showerror("无法更新","更新包无效、签名不正确或不适用于当前程序。",parent=self.root)
            return
        if not messagebox.askyesno("安装更新",f"更新到 {metadata['version']} 并重新打开 CrossDesk？\n互控会先暂停，配对和显示器布局会保留。",parent=self.root):
            return
        self._queue_upgrade(lambda:manager.stage(path),manager)

    def _rollback_update(self):
        if self.demo or self._update_busy:
            return
        from crossdesk.updates import UpdateManager
        manager=UpdateManager(self.state_dir)
        status=manager.status()
        if not status.get("current"):
            self.status_var.set("当前是安装时的版本，还没有可恢复的更新。")
            return
        previous=status.get("previous")
        version=previous.get("version") if previous else "安装时的版本"
        if messagebox.askyesno("恢复版本",f"恢复到 {version} 并重新打开 CrossDesk？\n互控会先暂停，配对和显示器布局会保留。",parent=self.root):
            self._queue_upgrade(manager.rollback,manager)

    def _queue_upgrade(self, install, manager):
        self._update_busy=True
        self.upgrade_button.configure(state="disabled")
        self.rollback_button.configure(state="disabled")
        self.status_var.set("正在验证并准备更新……")
        def action():
            try:
                install()
                self.engine.set_enabled(False,reason='update')
                self.notify("update_restart",manager)
            finally:
                self.notify("update_idle",None)
        self._async(action)

    def _restart_update(self, manager):
        self._close_reason='update'
        if self.close():
            manager.restart()

    def _report_callback_exception(self,exception_type,error,traceback):
        diagnostics.exception('unexpected_exception',error,reason='gui_callback')

    def _open_logs(self):
        try:
            directory=self.state_dir/'logs'
            if sys.platform=='win32':os.startfile(str(directory))
            else:subprocess.Popen(['/usr/bin/open',str(directory)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except Exception as error:
            diagnostics.exception('unexpected_exception',error,reason='open_logs')
            self.status_var.set('无法打开日志文件夹，请检查配置目录。')

    def _build(self):
        font="Microsoft YaHei UI" if sys.platform=="win32" else "PingFang SC"
        self.ui_font=font
        self.root.option_add("*Font",(font,10))
        self._theme_images=apply_theme(self.root,font)
        header=tk.Frame(self.root,bg=CARD,height=round(92*self.ui_scale))
        header.pack(fill="x")
        header.pack_propagate(False)
        brand=tk.Frame(header,bg=CARD)
        brand.pack(side="left",padx=28,pady=12)
        tk.Label(brand,text="CrossDesk",font=(font,23,"bold"),fg=INK,bg=CARD).pack(anchor="w")
        tk.Label(brand,text="跨屏，让桌面自然相连",fg=MUTED,bg=CARD,font=(font,9)).pack(anchor="w",pady=(1,0))
        tk.Label(header,text="Windows  /  macOS",fg=MUTED,bg=CARD,font=(font,10)).pack(side="right",padx=30)
        body=ttk.Frame(self.root,padding=(24,22))
        body.pack(fill="both",expand=True)
        left_shell=ttk.Frame(body,width=round(300*self.ui_scale),style="Card.TFrame",padding=16)
        left_shell.pack(side="left",fill="y",padx=(0,22))
        left_shell.pack_propagate(False)
        self.sidebar_tabs=ttk.Notebook(left_shell)
        self.sidebar_tabs.pack(fill="both",expand=True)
        devices=ttk.Frame(self.sidebar_tabs,style="Card.TFrame")
        preferences=ttk.Frame(self.sidebar_tabs,style="Card.TFrame",padding=(3,5))
        maintenance=ttk.Frame(self.sidebar_tabs,style="Card.TFrame",padding=(3,5))
        for page,title in ((devices,"设备"),(preferences,"偏好"),(maintenance,"更新")):
            self.sidebar_tabs.add(page,text=title)
        self.sidebar_scrollbar=ttk.Scrollbar(devices,orient="vertical")
        self.sidebar_scrollbar.pack(side="right",fill="y")
        self.sidebar=tk.Canvas(devices,bg=CARD,highlightthickness=0,width=255,yscrollincrement=20,
                               yscrollcommand=self.sidebar_scrollbar.set)
        self.sidebar.pack(side="left",fill="both",expand=True)
        self.sidebar_scrollbar.configure(command=self.sidebar.yview)
        left=ttk.Frame(self.sidebar,padding=(0,4,8,10),style="Card.TFrame")
        sidebar_window=self.sidebar.create_window((0,0),window=left,anchor="nw")
        left.bind("<Configure>",lambda event:self.sidebar.configure(scrollregion=self.sidebar.bbox("all")))
        self.sidebar.bind("<Configure>",lambda event:self.sidebar.itemconfigure(sidebar_window,width=event.width))
        self.root.bind("<MouseWheel>",self._sidebar_wheel,add="+")
        def label(parent,text,**options):
            return ttk.Label(parent,text=text,style="Card.TLabel",**options)
        label(left,"附近的电脑",font=(font,12,"bold")).pack(anchor="w",pady=(0,5))
        label(left,"同一局域网，自动发现",foreground=MUTED).pack(anchor="w",pady=(0,13))
        self.peer_tree=ttk.Treeview(left,columns=("address","screens"),show="tree headings",height=3,selectmode="browse")
        self.peer_tree.heading("#0",text="电脑")
        self.peer_tree.heading("address",text="IP 地址")
        self.peer_tree.heading("screens",text="屏")
        self.peer_tree.column("#0",width=93,minwidth=60)
        self.peer_tree.column("address",width=106,minwidth=85)
        self.peer_tree.column("screens",width=26,minwidth=22,anchor="center")
        self.peer_tree.pack(fill="x")
        self.peer_tree.bind("<Double-1>",lambda event:self._add_selected_peer())
        ttk.Button(left,text="添加显示器",command=self._add_selected_peer).pack(fill="x",pady=(9,22))
        label(left,"安全连接",font=(font,12,"bold")).pack(anchor="w",pady=(0,5))
        label(left,"在一端生成配对码，在另一端粘贴。",foreground=MUTED,wraplength=240).pack(anchor="w",pady=(0,13))
        label(left,"本机地址",foreground=MUTED).pack(anchor="w")
        addresses=lan_addresses()
        self.address_var=tk.StringVar(value=addresses[0])
        self.address_combo=ttk.Combobox(left,textvariable=self.address_var,values=addresses,state="readonly")
        self.address_combo.pack(fill="x",pady=(4,8))
        portrow=ttk.Frame(left,style="Card.TFrame")
        portrow.pack(fill="x",pady=(0,9))
        label(portrow,"连接端口",foreground=MUTED).pack(side="left")
        self.port_var=tk.StringVar(value=str(settings_port(self.settings_path)))
        ttk.Spinbox(portrow,textvariable=self.port_var,from_=1024,to=65535,width=9).pack(side="right")
        ttk.Button(left,text="生成配对码",command=self._listen).pack(fill="x",pady=4)
        ttk.Button(left,text="粘贴配对码连接",command=self._connect).pack(fill="x",pady=4)
        ttk.Button(left,text="断开连接",style="Quiet.TButton",command=self._disconnect).pack(fill="x",pady=(6,10))
        label(preferences,"输入与共享",font=(font,12,"bold")).pack(anchor="w",pady=(4,12))
        self.permissions_button=None
        if sys.platform == "darwin" and not self.demo:
            self.permissions_button=ttk.Button(preferences,text="Mac 输入权限",command=self._request_mac_permissions)
            self.permissions_button.pack(fill="x",pady=(0,12))
        self.keyboard_var=tk.BooleanVar(value=self.engine.keyboard)
        self.swap_var=tk.BooleanVar(value=self.engine.swap_modifiers)
        self.clipboard_var=tk.BooleanVar(value=self._clipboard_enabled)
        self.keyboard_check=ttk.Checkbutton(preferences,text="键盘跟随光标",variable=self.keyboard_var,command=self._preferences)
        self.keyboard_check.pack(anchor="w",pady=4)
        self.swap_check=ttk.Checkbutton(preferences,text="转换 Windows / Mac 快捷键",variable=self.swap_var,command=self._preferences)
        self.swap_check.pack(anchor="w",pady=4)
        self.clipboard_check=ttk.Checkbutton(preferences,text="共享文本、图片和文件",variable=self.clipboard_var,command=self._preferences)
        self.clipboard_check.pack(anchor="w",pady=4)
        ttk.Separator(preferences).pack(fill="x",pady=(18,12))
        self.handoff_var=tk.BooleanVar(value=self._handoff_enabled)
        ttk.Checkbutton(preferences,text="跨屏流光动效",variable=self.handoff_var,
                        command=self._handoff_preferences).pack(anchor="w",pady=4)
        label(preferences,"光粒收束离开，柔光波纹迎接。",foreground=MUTED,wraplength=235).pack(anchor="w",pady=(2,8))
        ttk.Button(preferences,text="预览跨屏动效",style="Quiet.TButton",
                   command=self._preview_handoff).pack(fill="x")
        self.keyboard_hint_var=tk.StringVar(value="互控开始后，键盘跟随活动光标。")
        ttk.Label(preferences,textvariable=self.keyboard_hint_var,style="Card.TLabel",foreground=MUTED,wraplength=242).pack(anchor="w",pady=(16,0))
        label(preferences,"修改偏好前，请先暂停互控。",foreground=MUTED,wraplength=235).pack(anchor="w",pady=(12,0))
        label(maintenance,"保持更新",font=(font,12,"bold")).pack(anchor="w",pady=(4,8))
        label(maintenance,f"CrossDesk {__version__}",foreground=MUTED).pack(anchor="w",pady=(0,12))
        label(maintenance,"安装签名更新包，保留配对、布局和程序身份。",foreground=MUTED,wraplength=235).pack(anchor="w",pady=(0,10))
        self.upgrade_button=ttk.Button(maintenance,text="从更新包升级…",command=self._import_update)
        self.upgrade_button.pack(fill="x",pady=(12,4))
        self.rollback_button=ttk.Button(maintenance,text="恢复上一个版本",style="Quiet.TButton",command=self._rollback_update)
        self.rollback_button.pack(fill="x",pady=(0,4))
        ttk.Separator(maintenance).pack(fill='x',pady=(20,12))
        label(maintenance,'诊断日志',font=(font,12,'bold')).pack(anchor='w',pady=(0,8))
        label(maintenance,'自动记录异常及互控退出原因，最多保留约 4 MiB。',foreground=MUTED,wraplength=235).pack(anchor='w',pady=(0,8))
        ttk.Button(maintenance,text='打开日志文件夹',style='Quiet.TButton',command=self._open_logs).pack(fill='x')
        label(preferences,"紧急返回  F12\nMac 可使用 Fn + F12\n断开连接时自动释放输入",foreground=MUTED,wraplength=240).pack(anchor="w",pady=(26,0))
        main=ttk.Frame(body)
        main.pack(side="left",fill="both",expand=True)
        top=ttk.Frame(main)
        top.pack(fill="x",pady=(0,10))
        ttk.Label(top,text="你的工作台",font=(font,18,"bold")).pack(side="left")
        ttk.Button(top,text="适合画布",command=self._fit).pack(side="right")
        ttk.Button(top,text="＋",width=3,command=lambda:self._zoom(1.2)).pack(side="right",padx=3)
        ttk.Button(top,text="－",width=3,command=lambda:self._zoom(1/1.2)).pack(side="right",padx=3)
        ttk.Label(main,text="拖动屏幕排列 · 绿色连接条标出可跨屏的边界",foreground=MUTED).pack(anchor="w",pady=(0,15))
        self.canvas=tk.Canvas(main,bg="#fafbfd",highlightthickness=1,highlightbackground="#e4eaf2",cursor="hand2")
        self.canvas.pack(fill="both",expand=True)
        self.canvas.bind("<Configure>",lambda event:self._draw())
        self.canvas.bind("<ButtonPress-1>",self._press)
        self.canvas.bind("<B1-Motion>",self._motion)
        self.canvas.bind("<ButtonRelease-1>",self._release)
        details=ttk.Frame(main)
        details.pack(fill="x",pady=(12,6))
        self.selection_var=tk.StringVar(value="点击显示器，查看分辨率或调整布局大小。")
        ttk.Label(details,textvariable=self.selection_var,foreground=MUTED).pack(side="left")
        self.size_var=tk.StringVar(value="100%")
        sizecombo=ttk.Combobox(details,textvariable=self.size_var,values=["50%","67%","75%","100%","125%","150%","200%"],width=6,state="readonly")
        sizecombo.pack(side="right")
        sizecombo.bind("<<ComboboxSelected>>",self._resize_selected)
        ttk.Label(details,text="布局大小 ").pack(side="right")
        controls=ttk.Frame(main)
        controls.pack(fill="x",pady=(14,0))
        self.connection_var=tk.StringVar(value="尚未配对 · 当前仅显示本机屏幕")
        ttk.Label(controls,textvariable=self.connection_var,foreground=MUTED,wraplength=450).pack(side="left")
        self.enable_button=ttk.Button(controls,text="开始互控",style="Accent.TButton",command=self._toggle)
        self.enable_button.pack(side="right")
        footer=tk.Frame(self.root,bg=CARD)
        footer.pack(fill="x")
        self.status_var=tk.StringVar(value="先选择局域网电脑排列屏幕，再完成安全配对。")
        tk.Label(footer,textvariable=self.status_var,bg=CARD,fg=MUTED,anchor="w",padx=26,pady=10,wraplength=1120,font=(font,9)).pack(fill="x")

    def _sidebar_wheel(self,event):
        if event.widget is self.peer_tree or not str(event.widget).startswith(str(self.sidebar)):
            return
        if event.delta:
            units=-int(event.delta/120) if abs(event.delta)>=120 else (-1 if event.delta>0 else 1)
            self.sidebar.yview_scroll(units,"units")
            return "break"

    def _check_mac_permissions(self):
        self._permissions_after=None
        if self._closed or self.demo or sys.platform!="darwin":
            return
        try:
            permissions=self.backend.permissions(request=False)
            if not all(permissions.get(key) is True for key in ("accessibility","input_monitoring")):
                self._request_mac_permissions(automatic=True)
        except Exception:
            self.status_var.set("无法检查 Mac 输入权限；请点击“Mac 输入权限”，在系统设置中允许辅助功能和输入监控。")

    def _request_mac_permissions(self,automatic=False):
        if self._closed or self.demo or sys.platform!="darwin":
            return
        try:
            permissions=self.backend.permissions(request=True)
        except Exception:
            self.status_var.set("无法请求 Mac 输入权限；请在系统设置中允许 CrossDesk 的辅助功能和输入监控。")
            return
        if all(permissions.get(key) is True for key in ("accessibility","input_monitoring")):
            self.status_var.set("Mac 输入权限已就绪。完成配对后，在任意一台点击开始互控，两台一起启动。")
            return
        instruction="在系统设置允许 CrossDesk 辅助功能和输入监控，然后重新打开 CrossDesk。源码运行时，请允许对应的 Python 或终端。"
        self.status_var.set(instruction)
        if not automatic:
            try:
                subprocess.Popen(["/usr/bin/open","x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            except OSError:
                pass
            messagebox.showinfo("Mac 输入权限",instruction,parent=self.root)

    def _save(self):
        # Offline displays belong in the remembered layout, not the active
        # Desktop. Startup sees only local displays, so replacing this cache
        # with that snapshot would erase the peer's placement on every launch.
        with self.engine.lock:
            remembered={s.id:s.as_dict() for s in self.engine.desktop.screens}
            for row in self.engine.saved.values():
                if len(remembered)>=32:
                    break
                try:
                    screen=Screen.from_dict(row)
                except (ValueError,KeyError,TypeError):
                    continue
                if screen.id.startswith(screen.machine+":") and screen.id not in remembered:
                    remembered[screen.id]=screen.as_dict()
            self.engine.saved=remembered
            data={"machine_id":self.engine.machine["id"],"layout":list(remembered.values()),"port":getattr(self,"port_var",None).get() if hasattr(self,"port_var") else "48193",
                  "preferences":{"keyboard":self.engine.keyboard,"shortcuts":self.engine.swap_modifiers,
                                 "clipboard":self._clipboard_enabled,"handoff_effect":self._handoff_enabled}}
        temp=self.settings_path.with_suffix(".tmp")
        temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
        os.replace(temp,self.settings_path)

    @staticmethod
    def _validated_network_profile(profile):
        if not isinstance(profile,dict):
            raise ValueError("Invalid network profile")
        if profile.get("mode")=="listen" and set(profile)=={"mode","host","port"}:
            if not isinstance(profile["host"],str) or type(profile["port"]) is not int or not 1024<=profile["port"]<=65535:
                raise ValueError("Invalid listener profile")
            address=ipaddress.ip_address(profile["host"])
            if address.is_multicast:
                raise ValueError("Invalid listener address")
            return {"mode":"listen","host":str(address),"port":profile["port"]}
        if profile.get("mode")=="connect" and set(profile)=={"mode","code"}:
            _parse_pairing_code(profile["code"])
            return {"mode":"connect","code":profile["code"]}
        raise ValueError("Invalid network mode")

    def _load_network_profile(self):
        if self.demo:
            return None
        try:
            if self.network_path.stat().st_size>8192:
                raise ValueError("Network profile exceeds limit")
            os.chmod(self.network_path,0o600)
            return self._validated_network_profile(json.loads(self.network_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError,ValueError,TypeError,UnicodeError,RecursionError):
            self.notify("status","保存的连接配置无效或无法读取，请重新生成或粘贴配对码。")
            return None

    def _save_network_profile(self,profile):
        if self.demo:
            return False
        temporary=self.network_path.with_name(".network-"+uuid.uuid4().hex+".tmp")
        try:
            profile=self._validated_network_profile(profile)
            descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,"w",encoding="utf-8") as stream:
                json.dump(profile,stream,ensure_ascii=True,separators=(",",":"))
            os.replace(temporary,self.network_path)
            os.chmod(self.network_path,0o600)
            return True
        except (OSError,ValueError,TypeError):
            self.notify("status","连接已建立，但无法保存恢复配置；下次启动需要重新配对。")
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _restore_network_profile(self):
        self._network_after=None
        if self._closed or self.demo or self._network_generation:
            return
        generation=self._network_generation
        def action():
            profile=self._load_network_profile()
            if not profile or generation!=self._network_generation:
                return
            self.notify("status","正在恢复上次的加密连接……")
            if profile["mode"]=="listen":
                host=profile["host"]
                advertise="" if ipaddress.ip_address(host).is_unspecified else host
                self.transport.listen(host=host,port=profile["port"],advertise_host=advertise)
            else:
                self.transport.connect(profile["code"])
        self._async(action)

    def _remember_connected_profile(self,generation,profile):
        try:
            if generation==self._network_generation and self.transport.connected:
                self._save_network_profile(profile)
                if self._pending_connect_profile==(generation,profile):
                    self._pending_connect_profile=None
        finally:
            self._profile_save_scheduled=False

    def _async(self,fn):
        def run():
            with self._action_lock:
                if self._closed:
                    return
                try:
                    fn()
                except Exception as exc:
                    self.notify("error",str(exc))
        threading.Thread(target=run,name="CrossDesk-UI-action",daemon=True).start()

    def _listen(self):
        if self.demo:
            self.status_var.set("当前是布局演示；正常启动后可生成配对码。")
            return
        try:
            port=int(self.port_var.get())
            if not 1024<=port<=65535:
                raise ValueError()
        except ValueError:
            messagebox.showerror("端口","请输入 1024–65535 之间的独立端口。",parent=self.root)
            return
        address=self.address_var.get()
        self._network_generation+=1
        generation=self._network_generation
        self._pending_connect_profile=None
        def action():
            if generation!=self._network_generation:
                return
            if self.engine.enabled:
                self.engine.set_enabled(False)
            code=self.transport.listen(host=address,port=port,advertise_host=address)
            if generation==self._network_generation:
                self._save_network_profile({"mode":"listen","host":address,"port":port})
                self.notify("pair_code",code)
        self._async(action)

    def _show_code(self,code):
        self.pair_code=code
        dialog=tk.Toplevel(self.root)
        dialog.title("CrossDesk 安全配对")
        dialog.geometry("690x280")
        dialog.transient(self.root)
        ttk.Label(dialog,text="在另一台电脑点击“粘贴另一台的配对码”。",font=("",12,"bold"),wraplength=640).pack(anchor="w",padx=22,pady=(20,10))
        ttk.Label(dialog,text="配对码含连接凭据，只交给你要控制的那台电脑。",foreground=MUTED).pack(anchor="w",padx=22)
        field=tk.Text(dialog,height=5,wrap="char",font=("Consolas",10))
        field.pack(fill="x",padx=22,pady=12)
        field.insert("1.0",code)
        field.configure(state="disabled")
        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.status_var.set("配对码已复制。请在另一台粘贴；连接后在任意一台点击开始互控，两台一起启动。")
        ttk.Button(dialog,text="复制配对码",command=copy).pack(side="right",padx=22)

    def _connect(self):
        if self.demo:
            self.status_var.set("演示模式不会连接其他电脑。")
            return
        code=simpledialog.askstring("连接另一台电脑","粘贴另一台 CrossDesk 生成的完整配对码：",parent=self.root)
        if not code:
            return
        self._network_generation+=1
        generation=self._network_generation
        self._pending_connect_profile=None
        def action():
            if generation!=self._network_generation:
                return
            if self.engine.enabled:
                self.engine.set_enabled(False)
            self.transport.connect(code.strip())
            if generation==self._network_generation:
                self._pending_connect_profile=(generation,{"mode":"connect","code":code.strip()})
        self._async(action)

    def _disconnect(self):
        self._network_generation+=1
        generation=self._network_generation
        self._pending_connect_profile=None
        def action():
            if generation!=self._network_generation:
                return
            forgotten=True
            if not self.demo:
                try:
                    self.network_path.unlink(missing_ok=True)
                except OSError:
                    forgotten=False
            if self.engine.enabled:
                self.engine.set_enabled(False)
            self.transport.stop()
            self.notify("status","连接已断开。" if forgotten else "连接已断开，但无法移除恢复配置；请检查配置目录权限，下次启动可能恢复连接。")
        self._async(action)

    def _toggle(self):
        if self.demo:
            self.status_var.set("演示模式只测试显示器画布，不会捕获输入。")
            return
        if self._toggle_busy:
            return
        pending=self.engine.control_pending
        enabled=not (self.engine.enabled or pending)
        self._toggle_busy=True
        self.status_var.set("正在启动双端互控……" if enabled else "正在暂停双端互控……")
        self._refresh_session_ui()
        def action():
            try:
                self.engine.set_enabled(enabled)
            finally:
                self.notify("toggle_done",None)
        self._async(action)

    def _preferences(self):
        if self.engine.enabled:
            self.keyboard_var.set(self.engine.keyboard)
            self.swap_var.set(self.engine.swap_modifiers)
            self.clipboard_var.set(self._clipboard_enabled)
            self.status_var.set("请先暂停互控，再更改键盘设置。")
            return
        self.engine.keyboard=self.keyboard_var.get()
        self.engine.swap_modifiers=self.swap_var.get()
        self._clipboard_enabled=self.clipboard_var.get()
        self._save()

    def _handoff_preferences(self):
        self._handoff_enabled=bool(self.handoff_var.get())
        self._handoff_effect.enabled=self._handoff_enabled
        if not self._handoff_enabled:
            self._cancel_handoff()
        self._save()

    def _cancel_handoff(self):
        self._handoff_events.clear()
        self._handoff_preview_until=0.0
        if self._handoff_preview_after is not None:
            self.root.after_cancel(self._handoff_preview_after)
            self._handoff_preview_after=None
        if self._handoff_effect is not None:
            self._handoff_effect.cancel()

    def _preview_handoff(self):
        if not self._handoff_enabled:
            self.status_var.set("请先打开“跨屏流光动效”，再预览。")
            return
        if self.engine.enabled:
            self.status_var.set("请先暂停互控，再预览；实际跨电脑时会自动播放。")
            return
        self._cancel_handoff()
        screen=next(s for s in self.engine.desktop.screens if s.machine==self.engine.machine["id"])
        edge="right"
        preview_point=None
        for a,b,x1,y1,x2,y2 in screen_connections(self.engine.desktop.screens):
            if a.machine==b.machine:continue
            local=a if a.machine==self.engine.machine["id"] else b if b.machine==self.engine.machine["id"] else None
            if local is not None:
                screen=local
                if x1==x2:edge="left" if abs(x1-screen.x)<abs(x1-screen.right) else "right"
                else:edge="top" if abs(y1-screen.y)<abs(y1-screen.bottom) else "bottom"
                preview_point=screen.to_native((x1+x2)/2,(y1+y2)/2)
                break
        x=screen.native_x+(0 if edge=="left" else screen.native_w-1 if edge=="right" else screen.native_w/2)
        y=screen.native_y+(0 if edge=="top" else screen.native_h-1 if edge=="bottom" else screen.native_h/2)
        if preview_point is not None:x,y=preview_point
        event={"phase":"depart","x":x,"y":y,"edge":edge,
               "screen":{"x":screen.native_x,"y":screen.native_y,
                         "width":screen.native_w,"height":screen.native_h}}
        self._handoff_preview_until=time.monotonic()+1.8
        self._handoff_effect.play(event)
        def arrive():
            self._handoff_preview_after=None
            if not self._closed and self._handoff_enabled:
                self._handoff_effect.play({**event,"phase":"arrive"})
        self._handoff_preview_after=self.root.after(780,arrive)
        self.status_var.set(self._handoff_effect.last_error or
                           "动效预览：本机屏幕边缘依次播放离开与到达；实际切换时两端分别播放。")

    def _update_handoff(self):
        if time.monotonic()<self._handoff_preview_until:
            return
        if not (self._handoff_enabled and self.engine.enabled and self.engine.peer_active
                and self.engine.peer_ready and self.transport.connected):
            self._handoff_events.clear()
            if self._handoff_effect is not None:self._handoff_effect.cancel()
            return
        if self._handoff_events:
            event=self._handoff_events.pop()
            self._handoff_events.clear()
            if time.monotonic()-event.get("created_at",0)<0.65:
                self._handoff_effect.play(event)

    def _add_selected_peer(self):
        selected=self.peer_tree.selection()
        if not selected:
            self.status_var.set("先在左侧选择一台局域网电脑。")
            return
        peer=self.peers.get(selected[0])
        if peer:
            try:
                self.engine.add_discovered(peer)
                self.status_var.set("对方显示器已加入画布。可以拖动排列；控制前仍需完成配对。")
            except (ValueError,KeyError,TypeError,RuntimeError) as exc:
                self.status_var.set(str(exc))

    def _fit(self):
        self.zoom=1.0
        self._draw()

    def _zoom(self,factor):
        self.zoom=max(.4,min(3,self.zoom*factor))
        self._draw()

    def _draw(self,keep_transform=False):
        canvas=self.canvas
        width,height=max(200,canvas.winfo_width()),max(200,canvas.winfo_height())
        screens=self.layout
        if not screens:
            return
        if not keep_transform:
            left,top=min(s.x for s in screens),min(s.y for s in screens)
            right,bottom=max(s.right for s in screens),max(s.bottom for s in screens)
            scale=min((width-100)/max(1,right-left),(height-110)/max(1,bottom-top))*self.zoom
            self.canvas_scale=max(.015,scale)
            self.canvas_offset=((width-(right-left)*self.canvas_scale)/2-left*self.canvas_scale,
                                (height-(bottom-top)*self.canvas_scale)/2-top*self.canvas_scale)
        scale=self.canvas_scale
        ox,oy=self.canvas_offset
        canvas.delete("all")
        font=self.ui_font
        for x in range(24,width,40):
            for y in range(24,height,40):
                canvas.create_oval(x,y,x+1,y+1,fill="#dce4ef",outline="")
        for i,screen in enumerate(screens):
            x,y=screen.x*scale+ox,screen.y*scale+oy
            w,h=screen.w*scale,screen.h*scale
            local=screen.machine==self.engine.machine["id"]
            selected=screen.id==self.selected
            color=BLUE if local else "#158c9c"
            fill="#edf3ff" if local else "#edf8f7"
            tags=("screen",screen.id)
            rounded_rectangle(canvas,x+2,y+4,x+w+2,y+h+4,fill="#e8edf4",outline="")
            rounded_rectangle(canvas,x,y,x+w,y+h,fill=fill,outline=color if selected else "#c9d8e9",
                              width=2.5 if selected else 1.5,tags=tags)
            if w>100 and h>65:
                canvas.create_oval(x+13,y+13,x+36,y+36,fill=color,outline="",tags=tags)
                canvas.create_text(x+24.5,y+24.5,text=str(i+1),fill="white",font=(font,9,"bold"),tags=tags)
                canvas.create_text(x+w-15,y+24,text="本机" if local else "另一台电脑",anchor="e",fill=color,font=(font,9),tags=tags)
            font_size=max(9,min(13,int(w/20)))
            title="Windows 显示器" if "DISPLAY" in screen.name else screen.name.replace("MacBook 内建显示器","内建显示器")
            compact=h<140
            canvas.create_text(x+w/2,y+h/2+(3 if compact else -9),text=title,fill=INK,font=(font,font_size,"bold"),width=max(60,w-24),tags=tags)
            name=self.machine_names.get(screen.machine,"局域网电脑")
            if not compact:
                canvas.create_text(x+w/2,y+h/2+19,text=name,fill=MUTED,font=(font,9),width=max(60,w-24),tags=tags)
            if h>95:
                canvas.create_text(x+w/2,y+h-18,text=f"{int(screen.native_w)} × {int(screen.native_h)}",fill=MUTED,font=(font,8),tags=tags)
        connections=screen_connections(screens)
        try:
            Desktop(screens).validate_layout()
        except ValueError:
            connections=[]
        cross_count=0
        for first,second,x1,y1,x2,y2 in connections:
            if first.machine==second.machine:
                continue
            cross_count+=1
            ax,ay,bx,by=x1*scale+ox,y1*scale+oy,x2*scale+ox,y2*scale+oy
            tag=("connection",first.id+"::"+second.id)
            canvas.create_line(ax,ay,bx,by,fill="#d0f1e6",width=13,capstyle="round",tags=tag)
            canvas.create_line(ax,ay,bx,by,fill=GREEN,width=5,capstyle="round",tags=tag)
            cx,cy=(ax+bx)/2,(ay+by)/2
            rounded_rectangle(canvas,cx-28,cy-13,cx+28,cy+13,radius=8,fill=GREEN,outline="",tags=tag)
            canvas.create_text(cx,cy,text="可跨屏",fill="white",font=(font,8,"bold"),tags=tag)
        legend=f"本机 · 蓝色     另一台 · 青色     {cross_count} 处跨屏连接" if cross_count else "将不同电脑的屏幕边缘贴合，即可建立跨屏通道"
        canvas.create_text(18,height-18,text=legend,anchor="w",fill=MUTED,font=(font,8))

    def _press(self,event):
        if self.engine.enabled:
            self.status_var.set("请先点击暂停互控，再拖动显示器。")
            return
        x=(event.x-self.canvas_offset[0])/self.canvas_scale
        y=(event.y-self.canvas_offset[1])/self.canvas_scale
        screen=next((s for s in reversed(self.layout) if s.contains(x,y)),None)
        if not screen:
            self.selected=None
            self._draw(True)
            return
        self.selected=screen.id
        self.drag=(event.x,event.y,screen,list(self.layout))
        self.selection_var.set(f"{screen.name} · {int(screen.native_w)} × {int(screen.native_h)}")
        self.size_var.set(f"{round(100*screen.w/screen.native_w)}%")
        self._draw(True)

    def _motion(self,event):
        if not self.drag:
            return
        px,py,screen,_=self.drag
        moved=replace(screen,x=screen.x+(event.x-px)/self.canvas_scale,y=screen.y+(event.y-py)/self.canvas_scale)
        moved=snap_screen(moved,[s for s in self.layout if s.id!=moved.id],distance=12/self.canvas_scale)
        self.layout=[moved if s.id==screen.id else s for s in self.layout]
        self._draw(True)

    def _release(self,event):
        if not self.drag:
            return
        _,_,original,before=self.drag
        self.drag=None
        moved=next((s for s in self.layout if s.id==original.id),None)
        if moved is None:
            self.layout=list(self.engine.desktop.screens)
            self._draw()
            return
        moved=snap_screen(moved,[s for s in self.layout if s.id!=moved.id],distance=18/self.canvas_scale)
        proposed=[moved if s.id==moved.id else s for s in self.layout]
        try:
            self.engine.update_layout(proposed)
            self.layout=proposed
            self._save()
            count=sum(a.machine!=b.machine for a,b,*_ in screen_connections(proposed))
            self.status_var.set(f"布局已保存并同步 · {count} 处跨屏连接 · 绿色连接条处可穿越（互控启用后生效）")
        except (ValueError,RuntimeError) as exc:
            self.layout=list(self.engine.desktop.screens)
            self.status_var.set(str(exc))
        except OSError:
            self.layout=list(self.engine.desktop.screens)
            self.status_var.set("布局已更新，但保存失败；请检查配置目录的写入权限。")
        self._draw()

    def _resize_selected(self,event=None):
        if self.engine.enabled or not self.selected:
            self.status_var.set("请先暂停，并选择一块显示器。")
            return
        try:
            ratio=float(self.size_var.get().rstrip("%"))/100
            screen=next(s for s in self.layout if s.id==self.selected)
            resized=replace(screen,w=screen.native_w*ratio,h=screen.native_h*ratio)
            resized=snap_screen(resized,[s for s in self.layout if s.id!=resized.id],distance=18/self.canvas_scale)
            proposed=[resized if s.id==resized.id else s for s in self.layout]
            self.engine.update_layout(proposed)
            self.layout=proposed
            self._save()
            self._draw()
        except (ValueError,StopIteration,RuntimeError) as exc:
            self.status_var.set(str(exc))
        except OSError:
            self.status_var.set("布局已更新，但保存失败；请检查配置目录的写入权限。")
            self._draw()

    def _poll(self):
        self._poll_after = None
        if self._closed:
            return
        if self.transport.connected and self._pending_connect_profile and not self._profile_save_scheduled:
            generation,profile=self._pending_connect_profile
            self._profile_save_scheduled=True
            self._async(lambda:self._remember_connected_profile(generation,profile))
        try:
            for _ in range(100):
                kind,value=self.messages.get_nowait()
                if kind=="status":
                    self.status_var.set(value)
                elif kind=="error":
                    self.status_var.set(value)
                    messagebox.showerror("CrossDesk",value,parent=self.root)
                elif kind=="toggle_done":
                    self._toggle_busy=False
                elif kind=="clipboard_handoff" and self.clipboard is not None:
                    self.clipboard.force_snapshot()
                elif kind=="update_restart":
                    self.root.after_idle(lambda manager=value:self._restart_update(manager))
                elif kind=="update_idle":
                    self._update_busy=False
                    self.upgrade_button.configure(state="normal")
                    self.rollback_button.configure(state="normal")
                elif kind=="layout":
                    self.drag=None
                    self.layout=list(value)
                    try:
                        self._save()
                    except OSError:
                        self.status_var.set("布局已更新，但保存失败；请检查配置目录的写入权限。")
                    self._draw()
                elif kind=="machines":
                    self.machine_names[value["id"]]=value["name"]
                elif kind=="pair_code":
                    self._show_code(value)
                elif kind=="discovered":
                    mid=value["machine"]["id"]
                    if len(self.peers)<32 or mid in self.peers:
                        self.peers[mid]=value
                        self.machine_names[mid]=value["machine"]["name"]
                        row=(value["address"],len(value["screens"]))
                        if self.peer_tree.exists(mid):
                            self.peer_tree.item(mid,text=value["machine"]["name"],values=row)
                        else:
                            self.peer_tree.insert("","end",iid=mid,text=value["machine"]["name"],values=row)
        except queue.Empty:
            pass
        self._update_source_cursor()
        self._update_handoff()
        self._sync_clipboard_preferences()
        if self.clipboard is not None:
            active=bool(self._clipboard_enabled and self._peer_clipboard_enabled and self.engine.enabled and self.engine.peer_ready
                        and self.engine.peer_active and self.transport.connected and not self.engine.control_pending)
            self.clipboard.poll(active=active)
        self._refresh_session_ui()
        self._write_diagnostics()
        now=time.monotonic()
        for mid,peer in list(self.peers.items()):
            if now-peer["seen"]>15:
                self.peers.pop(mid,None)
                if self.peer_tree.exists(mid):
                    self.peer_tree.delete(mid)
        self._poll_after=self.root.after(80,self._poll)

    def _sync_clipboard_preferences(self):
        if not self.transport.connected:
            self._peer_clipboard_enabled=False
            self._peer_clipboard_epoch=None
            self._sent_clipboard_settings=None
            return
        epoch=getattr(self.transport,"_session_epoch",None)
        if epoch != self._peer_clipboard_epoch:
            self._peer_clipboard_enabled=False
        setting=(epoch,self._clipboard_enabled)
        now=time.monotonic()
        if self.engine.peer_ready and (setting != self._sent_clipboard_settings or now-self._clipboard_settings_at>2):
            if self.transport.send({"type":"clipboard_settings","enabled":self._clipboard_enabled}):
                self._sent_clipboard_settings=setting
                self._clipboard_settings_at=now

    def _update_source_cursor(self):
        # Cursor presentation is reconciled on the GUI thread. The native input
        # hook and router never wait for AppKit, Tk, or an activation request.
        controller = self._cursor_controller
        if controller is None and self._cursor_controller_error is None:
            return
        engine = self.engine
        owner = engine.pointer_owner
        remote = bool(engine.enabled and engine.peer_ready and engine.peer_active
                      and self.transport.connected
                      and getattr(self.backend, "running", False)
                      and owner != engine.machine["id"])
        error = None
        if controller is not None:
            try:
                if self._cursor_controller_error:
                    # An unexpected controller failure disables this instance,
                    # but retain it until its owned hide has been paid back.
                    restore_error = controller.close()
                    if not restore_error:
                        self._cursor_controller = None
                        self._cursor_controller_error = None
                    error = restore_error
                else:
                    error = controller.update(remote)
                    warning = getattr(controller, 'warning', None)
                    if warning and warning != self._cursor_warning:
                        diagnostics.event('cursor_degraded', reason='cursor_state')
                    self._cursor_warning = warning
            except Exception as caught:
                diagnostics.exception('cursor_failed',caught,reason='cursor_exception')
                # Keep cleanup independent from router shutdown and preserve a
                # useful error without logging foreground apps or input data.
                restored=False
                try:
                    if not controller.close():
                        self._cursor_controller = None
                        restored=True
                except Exception:
                    pass
                self._cursor_warning = "源光标呈现已关闭，跨屏可继续工作。"
                if not restored:
                    error = "无法恢复本机光标显示，互控已暂停。请重新打开 CrossDesk。"
                    self._cursor_controller_error = error
        if error:
            if engine.enabled:
                diagnostics.event('cursor_failed',reason='cursor_state')
                engine._emergency_reason='input_restore_failed'
                engine.emergency.set()
            self.status_var.set(error)

    def _cursor_tick(self):
        self._cursor_after = None
        if self._closed:
            return
        before = getattr(self._cursor_controller, "local_ready", True)
        self._update_source_cursor()
        self._update_handoff()
        after = getattr(self._cursor_controller, "local_ready", True)
        if not before and after:
            try:
                self.engine.events.put_nowait(("local_ready", {}))
            except queue.Full:
                self.engine.emergency.set()
        busy = self.engine.enabled or getattr(self._cursor_controller, "pending", False)
        interval=(20 if sys.platform=='win32' else 8) if busy else 80
        self._cursor_after = self.root.after(interval, self._cursor_tick)

    def _refresh_session_ui(self):
        pending=self.engine.control_pending
        enabled=bool(self.engine.enabled)
        target=getattr(self.engine,"keyboard_target",self.engine.machine["id"])
        name=self.machine_names.get(target,"另一台电脑")
        self.keyboard_hint_var.set(("键盘 → "+name) if enabled and self.engine.keyboard else
                                   ("键盘留在各自电脑。" if not self.engine.keyboard else "互控开始后，键盘跟随活动光标。"))
        self.enable_button.configure(text="正在处理…" if self._toggle_busy else ("暂停互控" if enabled or pending else "开始互控"),
                                     state="disabled" if self._toggle_busy else "normal")
        if self.transport.connected and self.engine.peer_ready:
            peer=self.engine.peer_machine or {}
            if pending:
                suffix="本端已启动，等待对端确认" if enabled else "本端已暂停，等待对端确认"
            elif enabled and self.engine.peer_active:
                suffix="双端互控已启用 · 任意一端可暂停"
            elif self.engine.last_stop:
                suffix="互控已暂停 · "+self.engine.last_stop['message']
            elif enabled:
                suffix="本端已启动，等待对端响应"
            else:
                suffix="互控未启用 · 任意一端点击开始，两台一起启动"
            if enabled and self._cursor_warning:
                suffix+=' · '+self._cursor_warning
            self.connection_var.set(f"已加密连接 · {peer.get('name','另一台电脑')} · {suffix}")
        elif not self.demo:
            self.connection_var.set(self.engine.last_stop['message'] if self.engine.last_stop else
                                    "尚未配对 · 连接后在任意一端开始双端互控")

    @staticmethod
    def _safe_status_text(value):
        text=str(value)[:1000]
        text=re.sub(r"crossdesk://[^\s]+","[配对凭据已隐藏]",text)
        return re.sub(r"(?i)\b[0-9a-f]{64}\b","[私有凭据已隐藏]",text)

    def _write_diagnostics(self,force=False):
        if self.demo:
            return
        now=time.monotonic()
        if now-self._last_permission_check>=2 and not self._diagnostic_probe_busy:
            self._last_permission_check=now
            self._diagnostic_probe_busy=True
            mac=sys.platform=="darwin"
            def probe():
                try:
                    if mac:
                        try:
                            permissions=self.backend.permissions(request=False)
                            self._diagnostic_permissions={key:permissions.get(key) is True for key in ("accessibility","input_monitoring")}
                        except Exception:
                            self._diagnostic_permissions={"accessibility":None,"input_monitoring":None}
                    try:
                        raw=getattr(self.backend,"metrics",lambda:{})()
                        allowed=("move_seen","move_zero_delta","move_nonzero_delta","own_tag_skipped","callback_failures","tap_enabled",
                                 "gesture_sequences","gesture_forwarded","gesture_task_view","gesture_start_menu",
                                 "warp_suppressed_moves","warp_timeouts")
                        self._diagnostic_metrics={key:raw[key] for key in allowed if key in raw and
                                                  (isinstance(raw[key],bool) or type(raw[key]) is int and raw[key]>=0)}
                    except Exception:
                        self._diagnostic_metrics={}
                finally:
                    self._diagnostic_probe_busy=False
            threading.Thread(target=probe,name="CrossDesk-diagnostics",daemon=True).start()
        try:
            running=bool(getattr(self.backend,"running",False))
        except Exception:
            running=False
        current=getattr(self.engine,"current",None)
        payload={"version":__version__,"connected":bool(self.transport.connected),"enabled":bool(self.engine.enabled),
                 "peer_active":bool(self.engine.peer_active),"peer_ready":bool(self.engine.peer_ready),
                 "control_pending":bool(getattr(self.engine,"control_pending",False)),
                 "backend_running":running,"incoming":bool(getattr(self.engine,"incoming",False)),
                 "current_screen_id":current if isinstance(current,str) else None,
                 "held_buttons_count":len(getattr(self.engine,"held_buttons",())),
                 "last_status":self._safe_status_text(self.status_var.get()),
                 "permissions":dict(self._diagnostic_permissions),"backend_metrics":dict(self._diagnostic_metrics)}
        from crossdesk.updates import runtime_info
        payload.update(runtime_info())
        payload["pointer_owner"]=self.engine.pointer_owner
        payload['cursor_phase']=getattr(self._cursor_controller,'_phase',None)
        payload['session_state']=self.engine.session_state
        target=self.engine.desktop.by_id.get(self.engine.current)
        payload['routing_mode']=('receiving' if self.engine.incoming else
                                 'sending' if target and target.machine!=self.engine.machine['id'] else 'local')
        payload['last_stop']=self.engine.last_stop
        payload['cursor_warning']=self._cursor_warning
        payload["keyboard_target"] = getattr(self.engine,"keyboard_target",self.engine.machine["id"])
        payload["clipboard_enabled"] = bool(self._clipboard_enabled and self._peer_clipboard_enabled)
        if self._cursor_controller is not None:
            payload["source_cursor_hidden"] = bool(self._cursor_controller.hidden)
        if not force and payload==self._last_diagnostic_payload and now-self._last_diagnostic_at<1:
            return
        path=self.state_dir/"status.json"
        temporary=path.with_name(".status-"+uuid.uuid4().hex+".tmp")
        try:
            descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,"w",encoding="utf-8") as stream:
                json.dump({**payload,"updated_at":datetime.now(timezone.utc).isoformat()},stream,ensure_ascii=False,separators=(",",":"))
            os.replace(temporary,path)
            os.chmod(path,0o600)
            self._last_diagnostic_payload=payload
            self._last_diagnostic_at=now
        except OSError:
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def close(self):
        if self._closed:
            return self.close_error is None
        self._closed=True
        self._cancel_handoff()
        if self._handoff_effect is not None:
            self._handoff_effect.close()
        diagnostics.event('app_exit_requested',reason=getattr(self,'_close_reason','app_exit'))
        if self._cursor_after is not None:
            try:
                self.root.after_cancel(self._cursor_after)
            except tk.TclError:
                pass
            self._cursor_after=None
        if self._network_after is not None:
            try:
                self.root.after_cancel(self._network_after)
            except tk.TclError:
                pass
            self._network_after=None
        if self._permissions_after is not None:
            try:
                self.root.after_cancel(self._permissions_after)
            except tk.TclError:
                pass
            self._permissions_after=None
        if self._poll_after is not None:
            try:
                self.root.after_cancel(self._poll_after)
            except tk.TclError:
                pass
            self._poll_after=None
        errors=[]
        if self.clipboard is not None:
            try:
                self.clipboard.close()
            except Exception as exc:
                errors.append(exc)
        # Polling has stopped; restore the owned hide and foreground lease here,
        # even if engine.close or another lifecycle operation subsequently fails.
        if self._cursor_controller is not None:
            try:
                error = self._cursor_controller.close()
                if error:
                    errors.append(RuntimeError(error))
            except Exception as exc:
                errors.append(exc)
        try:
            self._save()
        except Exception as exc:
            errors.append(exc)
        # Serialize closing with connect/listen so an action cannot start a
        # fresh listener after the window and engine have already closed.
        with self._action_lock:
            if self._pending_connect_profile and self.transport.connected:
                generation,profile=self._pending_connect_profile
                self._remember_connected_profile(generation,profile)
            if self.discovery:
                try:
                    self.discovery.stop()
                except Exception as exc:
                    errors.append(exc)
            try:
                self.engine.close()
            except Exception as exc:
                errors.append(exc)
            finally:
                try:
                    self.transport.stop()
                except Exception as exc:
                    errors.append(exc)
        self.close_error=errors[0] if errors else None
        for error in errors:diagnostics.exception('unexpected_exception',error,reason='app_exit')
        diagnostics.event('app_closed',reason=getattr(self,'_close_reason','app_exit'))
        self.status_var.set("应用已关闭，输入已释放。" if not errors else "应用关闭时发生错误，请检查输入释放状态。")
        self._write_diagnostics(force=True)
        if self._mac_quit_command:
            try:
                self.root.tk.deletecommand("tk::mac::Quit")
            except tk.TclError:
                pass
            self._mac_quit_command=False
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        return not errors


def settings_port(path):
    try:
        value=int(json.loads(path.read_text(encoding="utf-8")).get("port",48193))
        return value if 1024<=value<=65535 else 48193
    except (OSError,ValueError,TypeError,AttributeError):
        return 48193
