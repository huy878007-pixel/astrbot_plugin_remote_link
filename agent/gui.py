#!/usr/bin/env python3
"""云信互联 本地代理 —— Windows 桌面版（GUI v2）

自绘标题栏（无边框，可拖动，品牌猫娘 Logo + 连接状态点 + 窗口按钮）
五区导航：总览（自定义卡片页）/ 配置 / 历史 / 日志 / 设置
新增功能：多服务状态总览 · 系统资源监控 · 生成历史 · 完成桌面通知 · 日志搜索导出 · 可配置自定义页

打包：
    pyinstaller --onefile --windowed --name YunxinAgent --icon assets/yunxin.ico \
        --add-data "assets/yunxin.ico;." --add-data "agent/yunxin.png;." \
        --add-data "agent/default_workflow_api.json;." agent/gui.py
"""

import asyncio
import base64
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox

import ttkbootstrap as tb

from local_agent import (
    DEFAULT_CONFIG,
    LocalAgent,
    app_dir,
    bundled_file,
    load_config,
)

APP_NAME = "云信互联 本地代理 v0.2.0"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "YunxinAgent"
FONT = "Microsoft YaHei UI"

DEFAULT_CONFIG_DICT = {
    "server_url": "ws://你的云服务器IP:8468/ws",
    "token": "",
    "reconnect_seconds": 5,
    "request_timeout": 600,
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8899,
    "comfyui": {
        "base_url": "http://127.0.0.1:8188",
        "timeout": 600,
        "userdata_dir": "",
        "default_checkpoint": "",
        "default_workflow_file": "",
    },
    "openai": {"base_url": "http://127.0.0.1:11434/v1", "api_key": "", "timeout": 300},
    # 多 LLM 服务：额外服务列表（GUI 多服务状态总览）
    "openai_services": [],
    "llm_profiles": [
        {
            "id": "default", "name": "默认 Agent 模型",
            "provider_type": "openai_compatible",
            "base_url": "", "api_key": "", "model": "", "timeout": 60,
        }
    ],
    "brain": {"enabled": False, "default_profile": "default"},
    "shell": {"enabled": False, "timeout": 60, "allowed_patterns": []},
    # GUI 偏好（存同一配置文件）
    "gui_overview_cards": ["services", "resources", "task", "recent"],
    "gui_notify_on_done": True,
}

FORM_FIELDS = [
    ("服务器", "云端地址 server_url", ("server_url",), "str"),
    ("服务器", "共享密钥 token", ("token",), "password"),
    ("服务器", "重连间隔（秒）", ("reconnect_seconds",), "int"),
    ("服务器", "请求超时（秒）", ("request_timeout",), "int"),
    ("ComfyUI", "API 地址", ("comfyui", "base_url"), "str"),
    ("ComfyUI", "user 目录（读取工作流）", ("comfyui", "userdata_dir"), "dir"),
    ("ComfyUI", "默认 checkpoint", ("comfyui", "default_checkpoint"), "str"),
    ("ComfyUI", "执行超时（秒）", ("comfyui", "timeout"), "int"),
    ("本地 LLM", "主服务地址", ("openai", "base_url"), "str"),
    ("本地 LLM", "主服务 API Key", ("openai", "api_key"), "password"),
    ("本地 LLM", "主服务超时（秒）", ("openai", "timeout"), "int"),
    ("其他", "本地看板端口（0=关闭）", ("dashboard_port",), "int"),
    ("其他", "允许云端执行 Shell 命令", ("shell", "enabled"), "bool"),
    ("其他", "Shell 超时（秒）", ("shell", "timeout"), "int"),
    ("其他", "Shell 命令白名单（正则，JSON 数组）", ("shell", "allowed_patterns"), "str"),
]

LOG_LEVELS = ("全部", "INFO", "WARNING", "ERROR")


def _enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


def _get_nested(d: dict, keys: tuple):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(k)
    return cur if cur is not None else ""


def _set_nested(d: dict, keys: tuple, value):
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


def _resource_snapshot() -> dict:
    """CPU / 内存（ctypes，零依赖）+ GPU（nvidia-smi，失败降级）。"""
    out = {"cpu": None, "mem": None, "gpu": None}
    try:
        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]
        idle, kern, user = FILETIME(), FILETIME(), FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        total = (kern.dwHighDateTime << 32 | kern.dwLowDateTime) + (user.dwHighDateTime << 32 | user.dwLowDateTime)
        idlev = idle.dwHighDateTime << 32 | idle.dwLowDateTime
        now = time.time()
        if "_cpu_prev" not in _resource_snapshot.__dict__:
            _resource_snapshot._cpu_prev = (total, idlev, now)
        pt, pi, pn = _resource_snapshot._cpu_prev
        dt = max(now - pn, 0.001)
        out["cpu"] = round(100 * (1 - (idlev - pi) / (total - pt)) / 100 * 100) if (total - pt) > 0 else 0
        out["cpu"] = max(0, min(100, round(100 * (1 - (idlev - pi) / max(total - pt, 1)))))
        _resource_snapshot._cpu_prev = (total, idlev, now)
    except Exception:  # noqa: BLE001
        pass
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            out["mem"] = {
                "pct": int(m.dwMemoryLoad),
                "total_gb": round(m.ullTotalPhys / 1024**3, 1),
                "avail_gb": round(m.ullAvailPhys / 1024**3, 1),
            }
    except Exception:  # noqa: BLE001
        pass
    try:
        if "_gpu_ts" not in _resource_snapshot.__dict__ or time.time() - _resource_snapshot._gpu_ts > 5:
            p = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if p.returncode == 0 and p.stdout.strip():
                parts = [x.strip() for x in p.stdout.strip().split(",")]
                out["gpu"] = {"name": parts[0], "util": int(parts[1]), "used_gb": round(int(parts[2]) / 1024, 1),
                              "total_gb": round(int(parts[3]) / 1024, 1)}
                _resource_snapshot._gpu_ts = time.time()
                _resource_snapshot._gpu_data = out["gpu"]
            else:
                out["gpu"] = None
        else:
            out["gpu"] = _resource_snapshot._gpu_data
    except Exception:  # noqa: BLE001
        out["gpu"] = None
    return out


class YunxinGui:
    def __init__(self, root: tb.Window):
        self.root = root
        root.title(APP_NAME)
        root.geometry("1060x700")
        root.minsize(880, 580)
        self._apply_theme()
        self._set_window_icon()

        self.cfg_path = DEFAULT_CONFIG
        self.log_queue: deque = deque(maxlen=3000)
        self.agent: LocalAgent | None = None
        self.agent_thread: threading.Thread | None = None
        self.vars: dict = {}
        self._drag = None
        self._prev_prog = None
        self._gpu_cache = None

        # 自绘标题栏（无边框）
        root.overrideredirect(True)
        self._build_titlebar()
        self._build_body()
        self._build_toast()
        # 无边框窗口补任务栏图标（overrideredirect 默认隐藏任务栏条目）
        root.after(200, self._show_in_taskbar)

        self._load_gui_config()
        self._setup_file_log()
        self._load_config_into_form()
        self._start_agent()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Map>", self._on_map)
        root.after(300, self._drain_logs)
        root.after(1000, self._refresh_loop)
        self._tray = None
        self._tray_thread = None
        root.after(800, self._start_tray)

    # ---------------- 外观 ----------------

    def _show_in_taskbar(self):
        """让无边框（overrideredirect）窗口也出现在任务栏。

        overrideredirect 窗口默认带 WS_EX_TOOLWINDOW（不显示在任务栏），
        去掉它并加上 WS_EX_APPWINDOW，前台运行时任务栏就有图标了。
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            client = self.root.winfo_id()
            GA_ROOT = 2
            hwnd = user32.GetAncestor(client, GA_ROOT) or user32.GetParent(client) or client
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            # 触发任务栏刷新
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0020)
        except Exception:  # noqa: BLE001
            pass

    def _apply_theme(self):
        try:
            style = self.root.style
            style.configure("TLabel", font=(FONT, 10))
            style.configure("TEntry", font=(FONT, 10))
            style.configure("TButton", font=(FONT, 10))
            style.configure("TCheckbutton", font=(FONT, 10))
            style.configure("TLabelframe.Label", font=(FONT, 10, "bold"))
            style.configure("Status.TLabel", font=(FONT, 14, "bold"))
            style.configure("Dim.TLabel", font=(FONT, 9))
        except Exception:  # noqa: BLE001
            pass
        try:
            colors = tb.style.colors
            self._log_bg = getattr(colors, "inputbg", "#12121c")
            self._log_fg = getattr(colors, "fg", "#e8e8f5")
        except Exception:  # noqa: BLE001
            self._log_bg, self._log_fg = "#12121c", "#e8e8f5"

    def _set_window_icon(self):
        try:
            p = bundled_file("yunxin.ico")
            if not p.exists():
                alt = Path(__file__).resolve().parent.parent / "assets" / "yunxin.ico"
                if alt.exists():
                    p = alt
            if p.exists():
                self.root.iconbitmap(str(p))
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 自绘标题栏 ----------------

    def _build_titlebar(self):
        bar = tk.Frame(self.root, bg="#121a30")
        bar.pack(fill="x")
        bar._drag = None  # 类型提示用
        self._logo_img = None
        try:
            p = bundled_file("yunxin.png")
            if not p.exists():
                alt = Path(__file__).resolve().parent / "yunxin.png"
                if alt.exists():
                    p = alt
            self._logo_img = tk.PhotoImage(file=str(p)).subsample(4, 4)
        except Exception:  # noqa: BLE001
            pass
        if self._logo_img:
            tk.Label(bar, image=self._logo_img, bg="#121a30").pack(side="left", padx=(14, 0), pady=6)
        tk.Label(bar, text="云信互联 本地代理", font=(FONT, 12, "bold"), bg="#121a30", fg="#e8f0ff").pack(side="left", padx=(10, 2))
        tk.Label(bar, text="v0.1.0", font=(FONT, 9), bg="#121a30", fg="#6b7ba8").pack(side="left")
        self.title_dot = tk.Label(bar, text="●", font=(FONT, 12), bg="#121a30", fg="#ff4d5e")
        self.title_dot.pack(side="left", padx=(16, 4))
        self.title_status = tk.Label(bar, text="未连接", font=(FONT, 9), bg="#121a30", fg="#9aa8cf")
        self.title_status.pack(side="left")
        btns = tk.Frame(bar, bg="#121a30")
        btns.pack(side="right")
        for txt, cmd in (("─", self._minimize), ("✕", self._on_close)):
            b = tk.Button(btns, text=txt, font=(FONT, 11), width=3, bd=0, bg="#121a30", fg="#9aa8cf",
                          activebackground="#2a3b66", activeforeground="#fff", command=cmd)
            b.pack(side="left", padx=1)
        bar.bind("<Button-1>", self._drag_start)
        bar.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _minimize(self):
        # 最小化按钮：进托盘（与点 ✕ 一致），不在任务栏留下图标
        if getattr(self, "_tray", None) is not None:
            self._hide_to_tray()
            return
        try:
            self.root.iconify()
        except Exception:  # noqa: BLE001
            pass

    def _on_map(self, _e):
        try:
            if self.root.state() == "normal":
                self.root.after(50, lambda: self.root.overrideredirect(True))
                # overrideredirect 会重置窗口扩展样式，重新补任务栏图标
                self.root.after(150, self._show_in_taskbar)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 系统托盘（点 ✕ 最小化到托盘，托盘菜单恢复/退出） ----------------

    def _tray_icon(self):
        """从捆绑的猫娘 logo 生成托盘图标（PIL Image）。"""
        try:
            from PIL import Image, ImageDraw
        except Exception:  # noqa: BLE001
            Image = ImageDraw = None
        try:
            p = bundled_file("yunxin.png")
            if not p.exists():
                alt = Path(__file__).resolve().parent / "yunxin.png"
                if alt.exists():
                    p = alt
            if p.exists():
                return Image.open(str(p)).resize((64, 64))
        except Exception:  # noqa: BLE001
            pass
        # 兜底：纯色圆点图标
        img = Image.new("RGB", (64, 64), "#161f3a")
        d = ImageDraw.Draw(img)
        d.ellipse((16, 16, 48, 48), fill="#00e5ff")
        return img

    def _start_tray(self):
        """启动托盘图标线程：点 ✕ 隐藏到托盘；菜单「显示主窗口」恢复、「退出」真正退出。"""
        try:
            import pystray
        except Exception as e:  # noqa: BLE001
            self._log_line(f"托盘不可用（{e}），关闭按钮将直接退出")
            return

        def on_show(_icon=None, _item=None):
            self.root.after(0, self._show_from_tray)

        def on_quit(_icon=None, _item=None):
            self._tray_quit = True
            self.root.after(0, self._real_close)

        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", on_show, default=True),
                pystray.MenuItem("退出", on_quit),
            )
            self._tray = pystray.Icon("yunxin_agent", self._tray_icon(), "云信互联 本地代理", menu)
            self._tray_thread = threading.Thread(target=self._tray.run, daemon=True)
            self._tray_thread.start()
        except Exception as e:  # noqa: BLE001
            self._log_line(f"托盘启动失败（{e}），关闭按钮将直接退出")

    def _show_from_tray(self):
        try:
            self.root.deiconify()
            self.root.overrideredirect(True)
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(100, lambda: self.root.attributes("-topmost", False))
            self.root.after(200, self._show_in_taskbar)
        except Exception:  # noqa: BLE001
            pass

    def _hide_to_tray(self):
        """点 ✕：隐藏窗口到托盘（代理继续后台运行）。"""
        try:
            self.root.withdraw()
        except Exception:  # noqa: BLE001
            pass

    def _real_close(self):
        """托盘菜单「退出」：真正关闭并退出托盘。"""
        try:
            if getattr(self, "_tray", None) is not None:
                self._tray.stop()
        except Exception:  # noqa: BLE001
            pass
        self._tray = None
        self._on_close()

    def _on_close(self):
        """点 ✕ / WM_DELETE_WINDOW：有托盘则隐藏到托盘，否则直接退出。"""
        if getattr(self, "_tray", None) is not None:
            self._hide_to_tray()
            return
        try:
            self._stop_agent()
        except Exception:  # noqa: BLE001
            pass
        if self._file_log:
            try:
                self._file_log.close()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()

    # ---------------- 骨架：侧边导航 + 五页 ----------------

    def _build_body(self):
        body = tk.Frame(self.root, bg="#0a0e1a")
        body.pack(fill="both", expand=True)
        nav = tk.Frame(body, bg="#0e1526", width=118)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        self._nav_btns = {}
        for key, ico, label in (("overview", "🏠", "总览"), ("config", "⚙️", "配置"), ("history", "📜", "历史"),
                                ("log", "📋", "日志"), ("settings", "🔧", "设置"), ("env", "🧪", "环境/能力")):
            b = tk.Button(nav, text=f"{ico} {label}", font=(FONT, 10), bd=0, relief="flat", anchor="w",
                          padx=14, pady=9, bg="#0e1526", fg="#9aa8cf", activebackground="#1b2450",
                          activeforeground="#fff", command=lambda k=key: self._show_page(k))
            b.pack(fill="x")
            self._nav_btns[key] = b
        self.pages: dict = {}
        for key in ("overview", "config", "history", "log", "settings", "env"):
            p = tk.Frame(body, bg="#0a0e1a")
            self.pages[key] = p
        self._build_overview()
        self._build_config()
        self._build_history()
        self._build_log()
        self._build_settings()
        self._build_env()
        self._show_page("overview")

    def _show_page(self, key):
        for k, p in self.pages.items():
            p.pack_forget()
        self.pages[key].pack(side="left", fill="both", expand=True)
        for k, b in self._nav_btns.items():
            b.configure(bg="#0e1526", fg="#fff" if k == key else "#9aa8cf",
                        activebackground="#1b2450")

    def _build_env(self):
        """环境 / 能力页面：本机、服务、能力、AI 大脑、问题建议。"""
        page = self.pages["env"]
        inner = self._page_scroll(page)
        bar = tk.Frame(inner, bg="#0a0e1a")
        bar.pack(fill="x", padx=12, pady=(8, 4))
        tb.Label(bar, text="🧪 环境 / 能力", font=(FONT, 13), bootstyle="inverse-dark").pack(side="left")
        tb.Button(bar, text="🔃 重新扫描", bootstyle="info-outline",
                  command=self._request_env_rescan).pack(side="right")

        # AI 大脑快速配置（独立于 AstrBot）
        brain = tk.LabelFrame(inner, text=" AI 大脑（OpenAI Compatible） ", bg="#0a0e1a",
                              fg="#00e5ff", font=(FONT, 10, "bold"), padx=10, pady=6)
        brain.pack(fill="x", padx=12, pady=(4, 4))
        self.brain_url_var = tk.StringVar(value="")
        self.brain_model_var = tk.StringVar(value="")
        self.brain_key_var = tk.StringVar(value="")
        tk.Label(brain, text="Base URL", bg="#0a0e1a", fg="#9aa8cf").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        tk.Entry(brain, textvariable=self.brain_url_var, width=50, bg="#0e1526", fg="#fff",
                 insertbackground="#fff", relief="flat").grid(row=0, column=1, padx=4, pady=3)
        tk.Label(brain, text="Model", bg="#0a0e1a", fg="#9aa8cf").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        tk.Entry(brain, textvariable=self.brain_model_var, width=50, bg="#0e1526", fg="#fff",
                 insertbackground="#fff", relief="flat").grid(row=1, column=1, padx=4, pady=3)
        tk.Label(brain, text="API Key", bg="#0a0e1a", fg="#9aa8cf").grid(row=2, column=0, sticky="w", padx=4, pady=3)
        tk.Entry(brain, textvariable=self.brain_key_var, width=50, show="•", bg="#0e1526", fg="#fff",
                 insertbackground="#fff", relief="flat").grid(row=2, column=1, padx=4, pady=3)
        btns = tk.Frame(brain, bg="#0a0e1a")
        btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        tb.Button(btns, text="💾 保存配置", bootstyle="primary", command=self._save_brain_config).pack(side="left")
        tb.Button(btns, text="🔌 测试连接", bootstyle="info-outline", command=self._run_brain_test).pack(side="left", padx=(8, 0))
        tb.Button(btns, text="📥 填入本地模型", bootstyle="secondary-outline", command=self._adopt_local_model).pack(side="left", padx=(8, 0))

        self._load_brain_form()

        self.env_text = tk.Text(inner, bg="#0a0e1a", fg="#c9d4f0", font=("Consolas", 10),
                                relief="flat", height=22, wrap="word")
        self.env_text.pack(fill="both", expand=True, padx=12, pady=8)

    def _load_brain_form(self):
        """从当前默认 Brain Profile 回填 GUI 表单（API Key 仍掩码显示）。"""
        try:
            cfg = load_config()
            from brain.profiles import get_default_profile, normalize_brain_config
            normalize_brain_config(cfg)
            p = get_default_profile(cfg)
            if p:
                self.brain_url_var.set(p.base_url or "")
                self.brain_model_var.set(p.model or "")
                self.brain_key_var.set(p.api_key or "")
        except Exception as e:  # noqa: BLE001
            self._log_line(f"Brain 配置回填失败: {e}")

    def _adopt_local_model(self):
        """从当前 Environment Snapshot 中选取第一个在线 LLM 模型填入 Brain 表单，不自动保存。"""
        if not self.agent:
            self._log_line("代理尚未启动，无法读取本地模型")
            return
        from brain.profiles import first_local_model
        picked = first_local_model(self.agent.snapshot().get("environment") or {})
        if picked:
            base_url, model, provider_type = picked
            self.brain_url_var.set(base_url)
            self.brain_model_var.set(model)
            self.brain_key_var.set(self.brain_key_var.get() or "")
            self._log_line(f"已填入本地模型：{model} @ {base_url}（{provider_type}）")
        else:
            self._log_line("未发现可用的本地 LLM 模型")

    def _save_brain_config(self):
        cfg = load_config()
        from brain.profiles import normalize_brain_config
        normalize_brain_config(cfg)
        profiles = cfg["llm_profiles"]
        if not profiles:
            profiles.append({})
        p0 = profiles[0]
        p0["provider_type"] = "openai_compatible"
        p0["base_url"] = (self.brain_url_var.get() or "").strip()
        p0["model"] = (self.brain_model_var.get() or "").strip()
        p0["api_key"] = (self.brain_key_var.get() or "").strip()
        p0["timeout"] = 60
        cfg["brain"]["enabled"] = bool(p0["base_url"])
        cfg["brain"]["default_profile"] = "default"
        cfg["brain"]["last_test_ok"] = False
        try:
            cfg_path = Path(app_dir()) / "agent_config.json"
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            if self.agent:
                self.agent.cfg = cfg
            self._log_line("AI 大脑配置已保存（不包含日志输出 API Key）")
            self._toast("已保存", "AI 大脑配置已保存")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"保存失败：{e}")

    def _run_brain_test(self):
        from brain.client import BrainClient
        from brain.profiles import BrainProfile
        profile = BrainProfile(
            provider_type="openai_compatible",
            base_url=(self.brain_url_var.get() or "").strip(),
            model=(self.brain_model_var.get() or "").strip(),
            api_key=(self.brain_key_var.get() or "").strip(),
            timeout=60,
        )
        if not profile.base_url:
            messagebox.showwarning(APP_NAME, "请先填写 Base URL")
            return

        def worker():
            try:
                import asyncio
                result = asyncio.run(BrainClient(profile).test_connection())
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "category": "unknown", "message": str(e)}
            self.root.after(0, lambda: self._show_brain_test_result(result))

        threading.Thread(target=worker, daemon=True).start()

    def _show_brain_test_result(self, result):
        self._set_brain_test_ok(bool(result.get("ok")))
        if result.get("ok"):
            messagebox.showinfo(APP_NAME, "✓ API 可访问" + chr(10) + "✓ 模型可用" + chr(10) + "响应正常")
        else:
            messagebox.showerror(
                APP_NAME,
                "连接失败：" + str(result.get('category', 'unknown')) + chr(10) + str(result.get('message', '')),
            )

    def _set_brain_test_ok(self, ok: bool):
        try:
            cfg = load_config()
            from brain.profiles import normalize_brain_config
            normalize_brain_config(cfg)
            cfg["brain"]["last_test_ok"] = bool(ok)
            cfg_path = Path(app_dir()) / "agent_config.json"
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            if self.agent:
                self.agent.cfg = cfg
        except Exception:  # noqa: BLE001
            pass

    def _request_env_rescan(self):
        if self.agent:
            self._log_line("请求重新扫描本机环境…")
            self.agent.request_rescan()
        else:
            self._log_line("代理尚未启动，无法扫描")

    def _refresh_env(self):
        if not hasattr(self, "env_text"):
            return
        try:
            snap = {}
            if self.agent:
                snap = self.agent.snapshot().get("environment") or {}
            if not snap:
                self.env_text.delete("1.0", "end")
                self.env_text.insert("end", "正在检查本机环境……\n")
                return
            lines = []
            machine = snap.get("machine") or {}
            lines.append("【本机】")
            lines.append(f"设备名: {machine.get('name', '?')}")
            lines.append(f"系统: {machine.get('os', '?')}")
            lines.append(f"CPU: {machine.get('cpu_count', '?')} 核 | RAM: {machine.get('ram_total_gb', '?')} GB")
            gpus = machine.get("gpus") or []
            if gpus:
                for g in gpus:
                    lines.append(f"GPU: {g.get('name', '?')} | VRAM: {g.get('vram_total_gb', '?')} GB")
            else:
                lines.append("GPU: unknown / 未检测到 NVIDIA")
            lines.append(f"磁盘: 总 {machine.get('disk_total_gb', '?')} GB / 空闲 {machine.get('disk_free_gb', '?')} GB")
            lines.append("")
            lines.append("【服务】")
            for svc in snap.get("services") or []:
                status = "Ready" if svc.get("ok") else "Offline"
                base = svc.get("base_url", "")
                extra = ""
                if svc.get("type") == "comfyui":
                    extra = f" | workflows: {svc.get('workflows', 0)}"
                if svc.get("type") == "llm":
                    extra = f" | models: {len(svc.get('models') or [])}"
                if svc.get("type") == "ffmpeg":
                    base = svc.get("path", base)
                lines.append(f"{svc.get('provider_type') or svc.get('type')} [{status}] {base}{extra}")
            lines.append("")
            lines.append("【能力】")
            caps = snap.get("capabilities") or {}
            if caps:
                for cid, items in caps.items():
                    for item in items:
                        ev = item.get("evidence") or {}
                        wf = ev.get("workflow") or item.get("metadata", {}).get("source", "")
                        lines.append(f"{'✓' if item.get('status') == 'ready' else '?'} {cid} ({item.get('provider')}) {wf}")
            else:
                lines.append("（尚未扫描到确定能力）")
            lines.append("")
            lines.append("【AI 大脑】")
            brain = snap.get("brain") or {}
            prof = brain.get("profile") or {}
            lines.append(f"状态: {brain.get('status', 'not_configured')} | Provider: {prof.get('provider_type', '')}")
            lines.append(f"Endpoint: {prof.get('base_url', '')} | Model: {prof.get('model', '')}")
            lines.append("")
            lines.append("【问题与建议】")
            issues = snap.get("issues") or []
            if issues:
                for it in issues:
                    lines.append(f"⚠ {it.get('message', '')}")
            else:
                lines.append("✓ 未发现确定性问题")
            text = "\n".join(lines)
            self.env_text.delete("1.0", "end")
            self.env_text.insert("end", text)
        except Exception as e:  # noqa: BLE001
            self._log_line(f"环境页刷新失败: {e}")

    def _page_scroll(self, parent):
        """给内容页一个可滚动的容器（配置/日志等长内容用），深色风格滚动条，内容铺满宽度。"""
        style = self.root.style
        try:
            style.configure("Yunxin.Vertical.TScrollbar",
                            background="#2a3b66", troughcolor="#12141d", bordercolor="#12141d",
                            arrowcolor="#9aa8cf", lightcolor="#2a3b66", darkcolor="#2a3b66")
        except Exception:  # noqa: BLE001
            pass
        canvas = tk.Canvas(parent, bg="#0a0e1a", highlightthickness=0)
        sb = tb.Scrollbar(parent, orient="vertical", command=canvas.yview, style="Yunxin.Vertical.TScrollbar")
        inner = tk.Frame(canvas, bg="#0a0e1a")
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return inner

    # ---------------- 总览（可配置自定义卡片页） ----------------

    def _build_overview(self):
        self.ov_inner = self._page_scroll(self.pages["overview"])
        self.task_intent_var = tk.StringVar()
        self.task_kind_var = tk.StringVar(value="")  # 生成类型：空=智能 / 图片 / 视频 / 音频
        self.ov_services = tk.Label(self.ov_inner, text="—", font=(FONT, 10), bg="#0a0e1a", fg="#9aa8cf",
                                    justify="left", anchor="nw")
        self.ov_resources = tk.Label(self.ov_inner, text="—", font=(FONT, 10), bg="#0a0e1a", fg="#9aa8cf",
                                     justify="left", anchor="nw")
        self.ov_task = tk.Label(self.ov_inner, text="任务：空闲", font=(FONT, 10), bg="#0a0e1a", fg="#00e5ff",
                                justify="left", anchor="nw")
        self.ov_recent = tk.Label(self.ov_inner, text="暂无生成记录", font=(FONT, 9), bg="#0a0e1a",
                                  fg="#9aa8cf", justify="left", anchor="nw")

    def _card_box(self, title):
        frm = tk.Frame(self.ov_inner, bg="#121a30", highlightthickness=1,
                       highlightbackground="#2a3b66")
        tk.Label(frm, text=title, font=(FONT, 10, "bold"), bg="#121a30", fg="#00e5ff", anchor="w").pack(
            fill="x", padx=10, pady=(8, 2))
        return frm

    def _render_overview_cards(self, snap):
        for w in self.ov_inner.winfo_children():
            w.destroy()
        cards = self._gui_cards
        info = snap.get("info") or {}
        # 无条件渲染「提交生成任务」卡片（与 QQ 群/Web 控制台同一套内生调度；类型按钮=明确指令）
        box = self._card_box("⚡ 提交生成任务（云端调度 · 产物在 Web 控制台/QQ 可取）")
        kind_row = tk.Frame(box, bg="#121a30")
        kind_row.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(kind_row, text="类型：", font=(FONT, 9), bg="#121a30", fg="#9aa8cf").pack(side="left")
        for val, label in (("", "🧠 智能"), ("图片", "🖼️ 图片"), ("视频", "🎬 视频"), ("音频", "🎵 音频")):
            tb.Radiobutton(kind_row, text=label, value=val, variable=self.task_kind_var,
                           bootstyle="info-toolbutton", command=None).pack(side="left", padx=(4, 0))
        row = tk.Frame(box, bg="#121a30")
        row.pack(fill="x", padx=10, pady=(0, 8))
        entry = tk.Entry(row, textvariable=self.task_intent_var, font=(FONT, 10),
                         bg="#0a0e1a", fg="#e8f0ff", insertbackground="#e8f0ff")
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self._submit_task())
        tb.Button(row, text="🚀 生成", bootstyle="primary", command=self._submit_task).pack(side="left", padx=(8, 0))
        img_row = tk.Frame(box, bg="#121a30")
        img_row.pack(fill="x", padx=10, pady=(0, 8))
        tb.Button(img_row, text="🖼️ 选择参考图（图生图/图生视频）", bootstyle="secondary-outline",
                  command=self._pick_task_images).pack(side="left")
        self.task_images_label = tk.Label(img_row, text="未选择", font=(FONT, 9), bg="#121a30",
                                          fg="#9aa8cf", anchor="w")
        self.task_images_label.pack(side="left", padx=(10, 0))
        self.task_images = []  # 参考图 base64 列表
        box.pack(fill="x", pady=(0, 8))
        if "services" in cards:
            box = self._card_box("🛰️ 服务状态")
            lines = []
            connected = bool(snap.get("connected"))
            lines.append(("云端隧道", "🟢 已连接" if connected else "🔴 未连接"))
            comfy = info.get("comfyui") or {}
            if comfy.get("ok"):
                devs = comfy.get("devices") or []
                gpu_line = "; ".join(f"{d.get('name')} {d.get('vram_free_gb')}/{d.get('vram_total_gb')}GB" for d in devs) or "在线"
                lines.append(("ComfyUI", f"🟢 {gpu_line}"))
            else:
                lines.append(("ComfyUI", f"🔴 {comfy.get('error') or '未检测'}"))
            oai = info.get("openai") or {}
            if oai.get("ok"):
                lines.append(("本地 LLM 主", f"🟢 {len(oai.get('models') or [])} 个模型"))
            else:
                lines.append(("本地 LLM 主", f"🔴 {oai.get('error') or '未检测'}"))
            for svc in info.get("openai_services") or []:
                mark = "🟢" if svc.get("ok") else "🔴"
                detail = f"{len(svc.get('models') or [])} 个模型" if svc.get("ok") else (svc.get("error") or "离线")
                lines.append((f"LLM · {svc.get('name')}", f"{mark} {detail}"))
            for name, val in lines:
                row = tk.Frame(box, bg="#121a30")
                row.pack(fill="x", padx=10, pady=1)
                tk.Label(row, text=name, font=(FONT, 9), width=16, anchor="w", bg="#121a30", fg="#9aa8cf").pack(side="left")
                tk.Label(row, text=val, font=(FONT, 9), anchor="w", bg="#121a30", fg="#e8f0ff").pack(side="left")
            box.pack(fill="x", pady=(0, 8))
        if "resources" in cards:
            box = self._card_box("🖥️ 系统资源")
            lbl = tk.Label(box, text=self._resources_text(), font=(FONT, 9), bg="#121a30", fg="#e8f0ff",
                           justify="left", anchor="nw")
            lbl.pack(fill="x", padx=10, pady=(0, 8))
            self.ov_resources = lbl
            box.pack(fill="x", pady=(0, 8))
        if "task" in cards:
            box = self._card_box("🎨 当前任务")
            self.ov_task = tk.Label(box, text=self._task_text(snap), font=(FONT, 10), bg="#121a30",
                                    fg="#00e5ff", justify="left", anchor="nw", wraplength=760)
            self.ov_task.pack(fill="x", padx=10, pady=(0, 8))
            box.pack(fill="x", pady=(0, 8))
        if "recent" in cards:
            box = self._card_box("📜 最近生成")
            self.ov_recent = tk.Label(box, text=self._history_text(snap), font=(FONT, 9), bg="#121a30",
                                      fg="#9aa8cf", justify="left", anchor="nw")
            self.ov_recent.pack(fill="x", padx=10, pady=(0, 8))
            box.pack(fill="x", pady=(0, 8))

    def _resources_text(self) -> str:
        res = _resource_snapshot()
        parts = []
        if res.get("cpu") is not None:
            parts.append(f"CPU {res['cpu']}%")
        if res.get("mem"):
            parts.append(f"内存 {res['mem']['pct']}%（{res['mem']['avail_gb']}/{res['mem']['total_gb']}GB 可用）")
        if res.get("gpu"):
            g = res["gpu"]
            parts.append(f"GPU {g['util']}% · {g['used_gb']}/{g['total_gb']}GB · {g['name']}")
        return " · ".join(parts) if parts else "资源信息不可用"

    def _task_text(self, snap) -> str:
        prog = snap.get("progress")
        if not prog:
            return "空闲"
        pct = prog.get("percent")
        return f"{pct}% · {prog.get('text')}" if pct is not None else prog.get("text") or "执行中…"

    def _history_text(self, snap) -> str:
        hist = snap.get("history") or []
        if not hist:
            return "暂无生成记录"
        lines = []
        for h in hist[:8]:
            t = time.strftime("%m-%d %H:%M", time.localtime(h.get("t", 0)))
            files = ",".join(h.get("files") or [])[:60]
            lines.append(f"{t} · {h.get('workflow')} · {h.get('duration')}s · {h.get('prompt', '')[:40]} → {files}")
        return "\n".join(lines)

    # ---------------- 提交生成任务（走云端插件内嵌 HTTP /submit） ----------------

    def _submit_url(self) -> str:
        """从 server_url（ws://host:port/ws）推导云端插件 HTTP 端点。"""
        url = str(load_config().get("server_url") or "")
        url = url.replace("ws://", "http://").replace("wss://", "https://")
        return url.rstrip("/") + "/submit"

    def _pick_task_images(self):
        """选择本地图片作为参考图（图生图/图生视频），转 base64 随任务提交。"""
        files = filedialog.askopenfilenames(
            parent=self.root, title="选择参考图",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.gif"), ("所有文件", "*.*")],
        )
        if not files:
            return
        self.task_images = []
        names = []
        for p in files[:6]:
            try:
                data = Path(p).read_bytes()
                if not data or len(data) > 20 * 1024 * 1024:
                    continue
                self.task_images.append(base64.b64encode(data).decode("ascii"))
                names.append(Path(p).name)
            except Exception:  # noqa: BLE001
                continue
        if self.task_images:
            self.task_images_label.config(text="🖼️ " + ", ".join(names))
        else:
            self.task_images_label.config(text="读取失败")

    def _submit_task(self):
        intent = (self.task_intent_var.get() or "").strip()
        if not intent:
            self._toast("提示", "先输入要生成的内容描述")
            return
        # 选类型 → 加明确前缀（路由关键词直接命中，不靠 LLM 猜分类）
        kind = (self.task_kind_var.get() or "").strip()
        if kind == "图片" and not any(w in intent for w in ("图", "照片", "插画", "头像", "壁纸", "海报")):
            intent = "生成一张图片：" + intent
        elif kind == "视频" and not any(w in intent for w in ("视频", "动画", "短片")):
            intent = "生成一段视频：" + intent
        elif kind == "音频" and not any(w in intent for w in ("音乐", "音频", "歌曲")):
            intent = "生成一段音频：" + intent
        cfg = load_config()
        token = str(cfg.get("token") or "")
        url = self._submit_url()

        def worker():
            try:
                import urllib.request

                body = {"intent": intent}
                if getattr(self, "task_images", None):
                    body["images"] = self.task_images
                req = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                if data.get("ok"):
                    self.after(0, lambda: self._toast(
                        "已提交",
                        f"任务 {data.get('task_id')} 已提交，正在云端调度本地生成…\n"
                        f"完成后可在云端控制台查看/下载，或回复 QQ 群取产物",
                    ))
                    self.after(0, lambda: self.task_intent_var.set(""))
                else:
                    self.after(0, lambda: self._toast("提交失败", str(data.get("message") or "未知错误")))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: self._toast("提交失败", f"{e}\n请确认云端插件在线（隧道已连接）"))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------- 配置页（表单 + 多服务） ----------------

    def _build_config(self):
        page = self.pages["config"]
        # 先 pack 底部按钮栏，再 pack 滚动区：pack 按序分配空间，先占位者会被后续挤占，
        # 滚动区（fill=both, expand=True）必须最后 pack 才能铺满剩余宽度。
        btns = tk.Frame(page, bg="#0a0e1a")
        btns.pack(side="bottom", fill="x", pady=(8, 0))
        tb.Button(btns, text="💾 保存并重启代理", bootstyle="primary", command=self._save_and_restart).pack(side="left")
        tb.Button(btns, text="🔄 强制重连", bootstyle="info-outline", command=self._force_reconnect).pack(side="left", padx=(8, 0))
        tb.Button(btns, text="🌐 打开本地看板", bootstyle="secondary-outline", command=self._open_dashboard).pack(side="left", padx=(8, 0))
        tb.Button(btns, text="📁 打开程序目录", bootstyle="secondary-outline", command=self._open_app_dir).pack(side="left", padx=(8, 0))
        inner = self._page_scroll(page)
        self._section_frames = {}
        nb = tb.Notebook(inner)
        nb.pack(fill="both", expand=True)
        tab_frames: dict = {}
        for section in ("服务器", "ComfyUI", "本地 LLM", "其他"):
            frm = tb.Frame(nb, padding=10)
            nb.add(frm, text=section)
            tab_frames[section] = frm
        for section, label, keypath, kind in FORM_FIELDS:
            frm = tab_frames[section]
            row = frm.grid_size()[1]
            tb.Label(frm, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            key = ".".join(keypath)
            if kind == "bool":
                var = tk.BooleanVar(value=False)
                tb.Checkbutton(frm, variable=var, bootstyle="square-toggle").grid(row=row, column=1, sticky="w", pady=5)
            elif kind == "dir":
                var = tk.StringVar(value="")
                holder = tb.Frame(frm)
                holder.grid(row=row, column=1, sticky="ew", pady=5)
                holder.grid_columnconfigure(0, weight=1)
                tb.Entry(holder, textvariable=var, width=40).grid(row=0, column=0, sticky="ew")
                tb.Button(holder, text="📁 浏览…", bootstyle="info-outline",
                          command=lambda v=var: self._pick_dir(v)).grid(row=0, column=1, padx=(8, 0))
            else:
                var = tk.StringVar(value="")
                tb.Entry(frm, textvariable=var, width=44 if kind in ("str", "password") else 12,
                         show="●" if kind == "password" else "").grid(row=row, column=1, sticky="ew", pady=5)
            self.vars[key] = var
        # 多 LLM 服务说明
        o = tab_frames["本地 LLM"]
        row = o.grid_size()[1]
        tb.Label(o, text="额外 LLM 服务", style="Dim.TLabel", bootstyle="secondary").grid(row=row, column=0, sticky="w", pady=(10, 0))
        self.ov_services_extra = tb.Label(
            o, text="在 agent_config.json 的 openai_services 里配置：\n[{\"name\":\"Ollama\",\"base_url\":\"http://127.0.0.1:11434/v1\",\"api_key\":\"\"}]",
            style="Dim.TLabel", bootstyle="secondary", justify="left")
        self.ov_services_extra.grid(row=row + 1, column=1, sticky="w", pady=(10, 0))
        # 其他页：开机自启
        other = tab_frames["其他"]
        row = other.grid_size()[1]
        tb.Label(other, text="开机自启").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        self.autostart_var = tk.BooleanVar(value=self._autostart_enabled())
        tb.Checkbutton(other, variable=self.autostart_var, bootstyle="square-toggle",
                       command=self._toggle_autostart).grid(row=row, column=1, sticky="w", pady=5)
        tb.Label(other, text="Windows 登录后自动启动（注册表 HKCU，免管理员）",
                 style="Dim.TLabel", bootstyle="secondary").grid(row=row + 1, column=1, sticky="w", pady=(0, 5))

    # ---------------- 历史页 ----------------

    def _build_history(self):
        page = self.pages["history"]
        tb.Button(page, text="📂 打开输出目录", bootstyle="secondary-outline",
                  command=self._open_output_dir).pack(side="bottom", pady=(6, 0))
        bar = tk.Frame(page, bg="#0a0e1a")
        bar.pack(fill="x", pady=(0, 6))
        tk.Label(bar, text="📤 云端任务 · 产物发送到QQ", font=(FONT, 11, "bold"),
                 bg="#0a0e1a", fg="#00e5ff").pack(side="left")
        tb.Button(bar, text="🔄 刷新", bootstyle="info-outline", command=self._render_cloud_queue).pack(side="right")
        canvas = tk.Canvas(page, bg="#0a0e1a", highlightthickness=0)
        sb = tb.Scrollbar(page, orient="vertical", command=canvas.yview, style="Yunxin.Vertical.TScrollbar")
        inner = tk.Frame(canvas, bg="#0a0e1a")
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.hist_inner = inner
        self._queue_ts = 0.0

    # ---------------- 云端任务 / 产物发送（本地 GUI 产物库） ----------------

    def _api_http(self, path: str, method: str = "GET", body: dict | None = None):
        """调云端插件内嵌 HTTP（auth_token 鉴权）：/queue /task_send 等。"""
        import urllib.request

        cfg = load_config()
        token = str(cfg.get("token") or "")
        base = self._submit_url().rsplit("/submit", 1)[0]
        data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
        headers = {"Authorization": f"Bearer {token}"}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _render_cloud_queue(self):
        def worker():
            try:
                data = self._api_http("/queue")
                self.after(0, lambda: self._render_queue_ui(data.get("queue") or []))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: self._toast("拉取失败", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _render_queue_ui(self, queue):
        for w in self.hist_inner.winfo_children():
            w.destroy()
        if not queue:
            tk.Label(self.hist_inner, text="暂无任务（先在云端/QQ 发起生成）", bg="#0a0e1a",
                     fg="#9aa8cf").pack(pady=14)
            return
        for t in queue[:20]:
            card = tk.Frame(self.hist_inner, bg="#121a30", highlightthickness=1, highlightbackground="#2a3b66")
            card.pack(fill="x", pady=3, padx=1)
            st = t.get("status")
            mark = {"success": "✅", "running": "⏳", "failed": "❌",
                    "queued": "⏸", "waiting_confirm": "🛡️"}.get(st, "❔")
            tk.Label(card, text=f"{mark} {t.get('workflow') or t.get('subtype') or '智能调度'}  {t.get('task_id','')}",
                     font=(FONT, 10, "bold"), bg="#121a30", fg="#e8f0ff", anchor="w").pack(anchor="w", padx=8, pady=(5, 0))
            files = t.get("files") or []
            ftext = ", ".join(f.get("filename", "?") for f in files) or ("运行中…" if st in ("running", "queued") else "无产物")
            tk.Label(card, text="   " + ftext, font=(FONT, 9), bg="#121a30", fg="#9aa8cf", anchor="w").pack(anchor="w", padx=8)
            if files and t.get("origin") and not str(t.get("origin", "")).startswith("web:"):
                btns = tk.Frame(card, bg="#121a30")
                btns.pack(anchor="w", padx=6, pady=(0, 5))
                tid = t.get("task_id")
                for f in files:
                    tb.Button(btns, text=f"📤 发送 {str(f.get('filename','?'))[:12]}", bootstyle="info-outline",
                              command=lambda i=tid: self._send_task_to_qq(i)).pack(side="left", padx=(2, 0))

    def _send_task_to_qq(self, task_id):
        def worker():
            try:
                d = self._api_http("/task_send", "POST", {"task_id": task_id})
                if d.get("ok"):
                    self.after(0, lambda: self._toast("发送", "已发送到会话" if d.get("sent") else "发送失败（见云端日志）"))
                else:
                    self.after(0, lambda: self._toast("发送失败", d.get("message") or "未知错误"))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: self._toast("发送失败", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _open_output_dir(self):
        # 输出目录：优先按配置的 comfyui.userdata_dir 推导（ComfyUI/output），
        # 未配置时尝试常见默认路径，避免硬编码用户机器路径。
        try:
            ud = str((self.cfg.get("comfyui") or {}).get("userdata_dir") or "").strip()
            if ud:
                p = Path(ud)
                # 兼容三种指向：.../ComfyUI/user/default、.../ComfyUI/user、.../ComfyUI
                root = p
                if p.name.lower() in ("default", "user"):
                    root = p.parent
                if root.name.lower() == "user":
                    root = root.parent
                out = root / "output"
            else:
                out = Path(os.environ.get("USERPROFILE", "")) / "ComfyUI" / "output"
            if out.is_dir():
                os.startfile(str(out))
                return
            # 兜底：找不到就不弹
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 日志页（搜索 / 过滤 / 导出） ----------------

    def _build_log(self):
        bar = tk.Frame(self.pages["log"], bg="#0a0e1a")
        bar.pack(fill="x")
        tk.Label(bar, text="🔍", bg="#0a0e1a", fg="#9aa8cf").pack(side="left")
        self.log_search = tk.Entry(bar, font=(FONT, 10), bg="#121a30", fg="#e8f0ff",
                                   insertbackground="#fff", relief="flat")
        self.log_search.pack(side="left", fill="x", expand=True, padx=6)
        self.log_search.bind("<KeyRelease>", lambda e: self._redraw_log())
        self.log_level = tk.StringVar(value="全部")
        levels = tk.OptionMenu(bar, self.log_level, *LOG_LEVELS, command=lambda _: self._redraw_log())
        levels.configure(font=(FONT, 9), bg="#121a30", fg="#e8f0ff", relief="flat")
        levels.pack(side="left")
        tb.Button(bar, text="💾 导出", bootstyle="secondary-outline", command=self._export_log).pack(side="left", padx=(6, 0))
        self.log_text = tk.Text(self.pages["log"], bg=self._log_bg, fg=self._log_fg, font=("NSimSun", 9),
                                relief="flat", state="disabled", wrap="none")
        log_sb = tb.Scrollbar(self.pages["log"], orient="vertical", command=self.log_text.yview,
                              style="Yunxin.Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=log_sb.set)
        self.log_text.pack(side="left", fill="both", expand=True, pady=(6, 0))
        log_sb.pack(side="right", fill="y", pady=(6, 0))

    def _redraw_log(self):
        needle = self.log_search.get().lower()
        level = self.log_level.get()
        buf = deque(maxlen=1500)
        raw = list(self.log_queue)
        for line in raw:
            if level != "全部" and f"[{level}]" not in line:
                continue
            if needle and needle not in line.lower():
                continue
            buf.append(line)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "\n".join(buf))
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _export_log(self):
        try:
            p = filedialog.asksaveasfilename(parent=self.root, defaultextension=".log",
                                             initialfile="yunxin_agent.log", title="导出日志")
            if p:
                Path(p).write_text("\n".join(self.log_queue), encoding="utf-8")
                self._toast("已导出", f"日志已保存到 {p}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"导出失败：{e}")

    # ---------------- 设置页（通知 / 自定义卡片 / 自启） ----------------

    def _build_settings(self):
        inner = self._page_scroll(self.pages["settings"])
        # 完成通知
        frm = tk.Frame(inner, bg="#121a30", highlightthickness=1, highlightbackground="#2a3b66")
        tk.Label(frm, text="🔔 完成通知", font=(FONT, 10, "bold"), bg="#121a30", fg="#00e5ff").pack(anchor="w", padx=10, pady=(8, 2))
        self.notify_var = tk.BooleanVar(value=True)
        tb.Checkbutton(frm, text="任务完成时弹出通知", variable=self.notify_var, bootstyle="square-toggle").pack(anchor="w", padx=10, pady=(0, 8))
        frm.pack(fill="x", pady=(0, 10))
        # 自定义总览页卡片
        frm2 = tk.Frame(inner, bg="#121a30", highlightthickness=1, highlightbackground="#2a3b66")
        tk.Label(frm2, text="🏠 总览页卡片", font=(FONT, 10, "bold"), bg="#121a30", fg="#00e5ff").pack(anchor="w", padx=10, pady=(8, 2))
        tk.Label(frm2, text="勾选要在总览页显示的卡片：", font=(FONT, 9), bg="#121a30", fg="#9aa8cf").pack(anchor="w", padx=10)
        self._card_vars = {}
        for key, label in (("services", "🛰️ 服务状态"), ("resources", "🖥️ 系统资源"), ("task", "🎨 当前任务"), ("recent", "📜 最近生成")):
            var = tk.BooleanVar(value=True)
            tb.Checkbutton(frm2, text=label, variable=var, bootstyle="square-toggle").pack(anchor="w", padx=14, pady=1)
            self._card_vars[key] = var
        tb.Button(frm2, text="💾 保存显示配置", bootstyle="primary", command=self._save_gui_config).pack(anchor="w", padx=10, pady=(6, 10))
        frm2.pack(fill="x", pady=(0, 10))
        # 关于
        frm3 = tk.Frame(inner, bg="#121a30", highlightthickness=1, highlightbackground="#2a3b66")
        tk.Label(frm3, text="ℹ️ 关于", font=(FONT, 10, "bold"), bg="#121a30", fg="#00e5ff").pack(anchor="w", padx=10, pady=(8, 2))
        tk.Label(frm3, text=f"{APP_NAME}\n云端 AstrBot ↔ 本地模型隧道。多 LLM 服务在配置页或 agent_config.json 的 openai_services 添加。",
                 font=(FONT, 9), bg="#121a30", fg="#9aa8cf", justify="left").pack(anchor="w", padx=10, pady=(0, 10))
        frm3.pack(fill="x")

    # ---------------- GUI 偏好存取 ----------------

    def _load_gui_config(self):
        cfg = load_config()
        self._gui_cards = cfg.get("gui_overview_cards") or ["services", "resources", "task", "recent"]
        self.notify_var = tk.BooleanVar(value=bool(cfg.get("gui_notify_on_done", True)))
        for key, var in getattr(self, "_card_vars", {}).items():
            var.set(key in self._gui_cards)

    def _save_gui_config(self):
        try:
            cfg = load_config()
            cards = [k for k, var in self._card_vars.items() if var.get()]
            cfg["gui_overview_cards"] = cards
            cfg["gui_notify_on_done"] = bool(self.notify_var.get())
            cfg["openai_services"] = cfg.get("openai_services") or []
            self._gui_cards = cards
            cfg_path = Path(app_dir()) / "agent_config.json"
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self._toast("已保存", "总览显示配置已保存（重启代理后生效）")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"保存失败：{e}")

    # ---------------- 完成通知（自绘 toast） ----------------

    def _build_toast(self):
        self._toast_win = None

    def _toast(self, title: str, text: str):
        try:
            if self._toast_win is not None:
                try:
                    self._toast_win.destroy()
                except Exception:  # noqa: BLE001
                    pass
            w = tk.Toplevel(self.root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            w.configure(bg="#161f3a")
            tk.Label(w, text=title, font=(FONT, 10, "bold"), bg="#161f3a", fg="#00e5ff").pack(anchor="w", padx=12, pady=(8, 0))
            tk.Label(w, text=text, font=(FONT, 9), bg="#161f3a", fg="#e8f0ff", justify="left").pack(anchor="w", padx=12, pady=(2, 10))
            w.update_idletasks()
            x = self.root.winfo_x() + self.root.winfo_width() - w.winfo_reqwidth() - 20
            y = self.root.winfo_y() + 70
            w.geometry(f"+{x}+{y}")
            self._toast_win = w
            w.after(3500, lambda: self._destroy_toast(w))
        except Exception:  # noqa: BLE001
            pass

    def _destroy_toast(self, w):
        try:
            w.destroy()
            if self._toast_win is w:
                self._toast_win = None
        except Exception:  # noqa: BLE001
            pass

    def _show_done_notify(self):
        if self.notify_var.get():
            try:
                self.root.bell()
            except Exception:  # noqa: BLE001
                pass
            self._toast("✅ 任务完成", "本地生成已完成！")

    # ---------------- 刷新循环 ----------------

    def _refresh_loop(self):
        snap = self.agent.snapshot() if self.agent else {}
        # 标题栏状态
        connected = bool(snap.get("connected"))
        self.title_dot.configure(fg="#34d399" if connected else "#ff4d5e")
        self.title_status.configure(text="已连接" if connected else ("重连中…" if snap.get("last_error") else "未连接"))
        # 完成通知（progress 从有到无 = 任务结束）
        prog = snap.get("progress")
        if prog is None:
            if self._prev_prog is not None:
                self._show_done_notify()
            self._prev_prog = None
        else:
            self._prev_prog = prog
        # 总览页重绘（内容变化时才重绘，避免闪烁）
        key = (json.dumps(snap.get("history") or [], ensure_ascii=False)[:200],
               str(prog), str(snap.get("info", {}).get("openai_services")), connected)
        if getattr(self, "_ov_cache", None) != key:
            self._ov_cache = key
            self._render_overview_cards(snap)
        else:
            # 资源数字会变：只更新文本
            try:
                self.ov_resources.configure(text=self._resources_text())
            except Exception:  # noqa: BLE001
                pass
        # 历史页（产物库）：仅当可见时刷新云端任务（限频 5s）
        if self._nav_btns["history"].cget("fg") == "#fff":
            if time.time() - getattr(self, "_queue_ts", 0.0) > 5:
                self._queue_ts = time.time()
                self._render_cloud_queue()
        self.root.after(1000, self._refresh_loop)

    # ---------------- 日志与代理生命周期（沿用原实现） ----------------

    def _setup_file_log(self):
        try:
            self._file_log = open(app_dir() / "yunxin_agent.log", "a", encoding="utf-8")
        except Exception:  # noqa: BLE001
            self._file_log = None

    def _on_agent_log(self, text: str):
        self.log_queue.append(text)
        if self._file_log:
            try:
                self._file_log.write(text + "\n")
                self._file_log.flush()
            except Exception:  # noqa: BLE001
                pass

    def _log_line(self, text: str):
        self.log_queue.append(f"{time.strftime('%H:%M:%S')} [GUI] {text}")
        if self._file_log:
            try:
                self._file_log.write(f"{time.strftime('%H:%M:%S')} [GUI] {text}\n")
                self._file_log.flush()
            except Exception:  # noqa: BLE001
                pass

    def _drain_logs(self):
        if self.log_queue:
            self._redraw_log()
        self._refresh_env()
        self.root.after(400, self._drain_logs)

    def _start_agent(self):
        try:
            cfg = load_config()
        except Exception:  # noqa: BLE001
            cfg = dict(DEFAULT_CONFIG_DICT)
        self.agent = LocalAgent(cfg)
        self.agent.add_log_sink(self._on_agent_log)
        self.agent_thread = threading.Thread(target=self._run_agent_loop, daemon=True)
        self.agent_thread.start()

    def _run_agent_loop(self):
        asyncio.run(self.agent.run())

    def _save_and_restart(self):
        try:
            cfg = load_config()
        except Exception:  # noqa: BLE001
            cfg = dict(DEFAULT_CONFIG_DICT)
        for key, var in self.vars.items():
            if isinstance(var, tk.BooleanVar):
                _set_nested(cfg, tuple(key.split(".")), var.get())
            elif isinstance(var, tk.StringVar):
                raw = var.get()
                keys = tuple(key.split("."))
                if keys[-1] in ("port", "timeout", "reconnect_seconds", "dashboard_port"):
                    try:
                        _set_nested(cfg, keys, int(raw))
                    except ValueError:
                        messagebox.showerror(APP_NAME, f"「{key}」需要整数")
                        return
                elif keys[-1] == "allowed_patterns":
                    try:
                        val = json.loads(raw) if raw.strip() else []
                        if not isinstance(val, list):
                            raise ValueError("需要 JSON 数组")
                        _set_nested(cfg, keys, val)
                    except Exception:
                        messagebox.showerror(APP_NAME, f"「{key}」需要 JSON 数组，例如 [\"^nvidia-smi\"]")
                        return
                else:
                    _set_nested(cfg, keys, raw)
        cfg.setdefault("openai_services", [])
        cfg.setdefault("gui_overview_cards", self._gui_cards)
        cfg.setdefault("gui_notify_on_done", self.notify_var.get())
        cfg_path = Path(app_dir()) / "agent_config.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log_line("配置已保存，重启代理…")
        self._stop_agent()
        self.root.after(800, self._start_agent)
        self._toast("已重启", "配置已保存，代理重启中…")

    def _stop_agent(self):
        if self.agent:
            try:
                self.agent.request_stop()
            except Exception:  # noqa: BLE001
                pass
            self.agent = None
        if self.agent_thread:
            self.agent_thread = None

    def _force_reconnect(self):
        if self.agent:
            try:
                self.agent.request_reconnect()
                self._log_line("已请求强制重连")
            except Exception as e:  # noqa: BLE001
                self._log_line(f"强制重连失败：{e}")

    def _open_dashboard(self):
        try:
            port = int(load_config().get("dashboard_port", 8899))
            if port > 0:
                webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:  # noqa: BLE001
            pass

    def _open_app_dir(self):
        try:
            os.startfile(str(app_dir()))
        except Exception:  # noqa: BLE001
            pass

    def _pick_dir(self, var: tk.StringVar):
        initial = str(var.get()).strip()
        if not initial or not Path(initial).is_dir():
            initial = str(Path.home())
        chosen = filedialog.askdirectory(parent=self.root, initialdir=initial, title="选择目录")
        if chosen:
            var.set(str(Path(chosen)))

    def _autostart_enabled(self) -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, RUN_NAME)
                return True
        except Exception:  # noqa: BLE001
            return False

    def _toggle_autostart(self):
        import winreg
        try:
            exe = sys.executable if getattr(sys, "frozen", False) else ""
            if not exe:
                self.autostart_var.set(False)
                messagebox.showinfo(APP_NAME, "仅打包版 exe 支持开机自启（源码运行请自行配置）")
                return
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                if self.autostart_var.get():
                    winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{exe}" --autostart')
                else:
                    try:
                        winreg.DeleteValue(k, RUN_NAME)
                    except FileNotFoundError:
                        pass
            self._log_line("开机自启：" + ("已开启" if self.autostart_var.get() else "已关闭"))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"设置开机自启失败：{e}")

    def _load_config_into_form(self):
        try:
            cfg = load_config()
        except Exception:  # noqa: BLE001
            cfg = dict(DEFAULT_CONFIG_DICT)
        for key, var in self.vars.items():
            val = _get_nested(cfg, tuple(key.split(".")))
            if isinstance(var, tk.BooleanVar):
                var.set(bool(val))
            elif isinstance(val, (list, dict)):
                var.set(json.dumps(val, ensure_ascii=False))
            else:
                var.set(str(val) if val is not None else "")

    def _on_close(self):
        try:
            self._stop_agent()
        except Exception:  # noqa: BLE001
            pass
        if self._file_log:
            try:
                self._file_log.close()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()


def main():
    _enable_dpi_awareness()
    root = tb.Window(title=APP_NAME, themename="cyborg")
    if "--autostart" in sys.argv:
        # 开机自启：直接进托盘，不打扰（托盘启动后由 _start_tray 完成隐藏）
        root.after(600, lambda: root.overrideredirect(False))
        root.after(600, root.iconify)
        root.after(1200, lambda: root.overrideredirect(True))
    try:
        gui = YunxinGui(root)
        if "--autostart" in sys.argv:
            # 开机自启：等托盘就绪后再隐藏（避免托盘未启动就 withdraw 导致无入口）
            def _autostart_hide():
                if gui._tray is not None:
                    gui._hide_to_tray()
                else:
                    root.after(1000, _autostart_hide)
            root.after(2000, _autostart_hide)
    except Exception as e:  # noqa: BLE001
        messagebox.showerror(APP_NAME, f"启动失败：{e}")
    root.mainloop()


if __name__ == "__main__":
    main()
