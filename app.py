"""同声传译：英文语音 -> 中文字幕的置顶悬浮窗。"""
import argparse
import ctypes
import logging
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont
from tkinter import ttk

import config

_LAUNCH_DIR = os.getcwd()
os.chdir(config.APP_DIR)  # models are opened by relative path, see config.py

import audio  # noqa: E402
import setup_models  # noqa: E402
from engine import Engine  # noqa: E402

log = logging.getLogger("app")

BG = "#15161b"
BAR_FG, BAR_DIM, BAR_HOVER = "#a3a8b6", "#474b57", "#ffffff"
ZH, ZH_PENDING, ZH_LIVE = "#ffffff", "#c3c7d1", "#bfe0ff"
EN, EN_LIVE = "#8d92a0", "#6f7481"
GREEN, YELLOW, RED, GRAY = "#4cd07d", "#f0b429", "#ff5c5c", "#6b6f7a"
UI_FONT = "Microsoft YaHei UI"
RESIZE_CURSORS = {"n": "size_ns", "s": "size_ns", "e": "size_we", "w": "size_we",
                  "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw"}


def _norm(s):
    return re.sub(r"[\W_]+", "", s.lower())


def _set_window_icon(widget):
    """Tk gives Windows the .ico's first frame for every size, which the taskbar then
    stretches. Load the frames that match the current DPI instead."""
    try:
        u32 = ctypes.windll.user32
        u32.LoadImageW.restype = wintypes.HANDLE
        u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        hwnd = u32.GetParent(widget.winfo_id())
        for which, metric in ((1, 11), (0, 49)):  # ICON_BIG <- SM_CXICON, ICON_SMALL <- SM_CXSMICON
            size = u32.GetSystemMetrics(metric)
            icon = u32.LoadImageW(None, config.ICON_PATH, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            if icon:
                u32.SendMessageW(hwnd, 0x80, which, icon)  # WM_SETICON
    except Exception:
        log.exception("setting window icon failed")


def _dark_title_bar(widget):
    """Match the dark windows with a dark Windows title bar (Windows 10 20H1+)."""
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x27)  # redraw frame, keep pos/size/z
    except Exception:
        log.exception("dark title bar failed")


class Entry:
    __slots__ = ("id", "en", "zh", "done", "placeholder")

    def __init__(self, sid, en, placeholder):
        self.id, self.en, self.zh, self.done, self.placeholder = sid, en, "", False, placeholder

    def display(self):
        """(text, tag) of the Chinese line; the live translation stands in until the final one catches up."""
        if self.zh and (self.done or len(self.zh) >= len(self.placeholder)):
            return self.zh, "zh"
        if self.placeholder:
            return self.placeholder, "zh_pending"
        return ("…", "zh_pending") if not self.done else ("", "zh")


class History(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("同传记录")
        self.geometry("760x560")
        self.configure(bg=BG)
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=10, pady=(8, 0))
        for label, cmd in (("复制全部", self.copy_all), ("打开记录文件夹", app.open_transcripts)):
            tk.Button(bar, text=label, command=cmd, font=(UI_FONT, 9), relief="flat", bg="#2a2c34",
                      fg="#e6e6ea", activebackground="#3a3d48", activeforeground="#ffffff",
                      padx=10).pack(side="left", padx=(0, 8))
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.Vertical.TScrollbar", troughcolor="#1c1d23", background="#3a3d48",
                        bordercolor="#1c1d23", lightcolor="#3a3d48", darkcolor="#3a3d48", arrowcolor=EN)
        style.map("Dark.Vertical.TScrollbar",  # clam repaints these states in its light defaults
                  background=[("pressed", "#5a5e6a"), ("active", "#4a4e5a"), ("disabled", "#2a2c34")],
                  troughcolor=[("disabled", "#1c1d23")], arrowcolor=[("disabled", "#474b57")])
        sb = ttk.Scrollbar(body, style="Dark.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        self.text = tk.Text(body, wrap="word", bg="#1c1d23", fg=ZH, bd=0, padx=12, pady=10,
                            insertbackground=ZH, yscrollcommand=sb.set, font=(UI_FONT, 12))
        self.text.pack(fill="both", expand=True)
        sb.config(command=self.text.yview)
        self.text.tag_configure("en", foreground=EN, font=("Segoe UI", 10), spacing1=8)
        self.text.tag_configure("zh", foreground=ZH, font=(UI_FONT, 12))
        for e in app.entries:
            if e.done:
                self.add(e)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(50, lambda: (_set_window_icon(self), _dark_title_bar(self)))

    def add(self, e):
        at_bottom = self.text.yview()[1] > 0.98
        self.text.insert("end", e.en + "\n", "en")
        if e.zh:
            self.text.insert("end", e.zh + "\n", "zh")
        if at_bottom:
            self.text.see("end")

    def copy_all(self):
        self.clipboard_clear()
        self.clipboard_append(self.text.get("1.0", "end").strip())

    def close(self):
        self.app.history = None
        self.destroy()


class App:
    def __init__(self, root, cfg, test_wav=None):
        self.root, self.cfg = root, cfg
        self.events = queue.Queue()
        self.engine = Engine(cfg, self.events)
        self.entries = []
        self.live_epoch = 0
        self.live_en = self.live_zh = self.live_zh_src = ""
        self.live_zh_complete = False
        self.status = {"asr": "正在加载识别模型…", "mt": "正在启动翻译引擎…" if cfg["translate"] else "翻译已关闭"}
        self.stats = {}
        self.error, self.error_until = None, 0.0
        self.paused = False
        self.history = None
        self.dirty = True
        self.test_wav = test_wav
        self._vars = {}
        self._build()
        self.engine.start(source=False if test_wav else None)
        root.after(30, self._poll)
        root.after(1000, self._status_tick)

    # ---- window ------------------------------------------------------------

    def _build(self):
        r = self.root
        r.title("同声传译")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", self.cfg["opacity"])
        r.configure(bg=BG)
        r.geometry(self._saved_geometry() or self._default_geometry())
        r.report_callback_exception = lambda *a: log.error("UI error", exc_info=a)
        scale = r.winfo_fpixels("1i") / 96
        self._edge = max(6, round(7 * scale))  # how close to the border a press starts a resize
        self._min_size = (round(260 * scale), round(70 * scale))
        self._mode = None  # "move" / "resize" while the left button is held

        bar = tk.Frame(r, bg=BG)
        bar.pack(fill="x", side="top", padx=(10, 4), pady=(4, 0))
        self.bar = bar
        self.dot = tk.Canvas(bar, width=10, height=10, bg=BG, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 6))
        self.dot_id = self.dot.create_oval(1, 1, 9, 9, fill=YELLOW, outline="")
        self.meter = tk.Canvas(bar, width=26, height=10, bg=BG, highlightthickness=0)
        self.meter.pack(side="left", padx=(0, 8))
        self.meter_bars = [self.meter.create_rectangle(i * 5, 10 - 2 * (i + 1), i * 5 + 3, 10, fill=BAR_DIM,
                                                       outline="") for i in range(5)]
        self.status_lbl = tk.Label(bar, text="", bg=BG, fg=BAR_DIM, font=(UI_FONT, 9), anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)

        self.buttons = []
        for text, cmd in (("✕", self.quit), ("—", self.minimize), ("设置", self._show_menu_at_button),
                          ("记录", self.open_history), ("英", self.toggle_en), ("A+", lambda: self.zoom(2)),
                          ("A−", lambda: self.zoom(-2)), ("暂停", self.toggle_pause)):
            b = tk.Label(bar, text=text, bg=BG, fg=BAR_DIM, font=(UI_FONT, 9), padx=5, cursor="hand2")
            b.pack(side="right")
            b.bind("<ButtonRelease-1>", lambda e, c=cmd: self._click(e, c))
            b.bind("<Enter>", lambda e, w=b: w.configure(fg=BAR_HOVER))
            b.bind("<Leave>", lambda e, w=b: w.configure(fg=BAR_FG if self._hover else BAR_DIM))
            self.buttons.append(b)
            if text == "暂停":
                self.pause_btn = b
            if text == "设置":
                self.menu_btn = b

        # The text is pinned to the bottom of `body` and grows upwards, so the newest
        # line stays where the eye expects it, like live captions.
        self.body = tk.Frame(r, bg=BG)
        self.body.pack(fill="both", expand=True, pady=(0, 6))
        self.text = tk.Text(self.body, bg=BG, fg=ZH, bd=0, highlightthickness=0, wrap="word", padx=16, pady=2,
                            cursor="fleur", state="disabled", takefocus=0, selectbackground=BG,
                            inactiveselectbackground=BG, exportselection=False)
        self.text.place(x=0, rely=1.0, anchor="sw", relwidth=1.0, height=1)
        self.body.bind("<Configure>", lambda e: setattr(self, "dirty", True))
        self.grip = tk.Label(r, text="◢", bg=BG, fg=BAR_DIM, cursor="size_nw_se", font=("Segoe UI", 8))
        self.grip.place(relx=1.0, rely=1.0, anchor="se")

        # Bound once on the toplevel, which is in every child's bindtags; binding the
        # children as well would run each handler twice.
        self._cursors = {}  # widget -> its own cursor, restored away from the border
        r.bind("<Motion>", self._on_motion)
        r.bind("<ButtonPress-1>", self._on_press)
        r.bind("<B1-Motion>", self._on_drag)
        r.bind("<ButtonRelease-1>", self._on_release)
        r.bind("<Button-3>", self._show_menu)
        self.text.bind("<Control-MouseWheel>", lambda e: self.zoom(2 if e.delta > 0 else -2))

        self._hover = False
        self._apply_fonts()
        r.after(50, self._make_app_window)
        r.after(300, self._hover_tick)

    def _saved_geometry(self):
        """The last geometry, unless it is now off-screen (e.g. a monitor was unplugged):
        a frameless window placed there could never be dragged back."""
        m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", self.cfg["geometry"] or "")
        if not m:
            return None
        w, h, x, y = map(int, m.groups())
        metric = ctypes.windll.user32.GetSystemMetrics
        vx, vy, vw, vh = metric(76), metric(77), metric(78), metric(79)  # virtual screen
        if x + w - 80 < vx or x + 80 > vx + vw or y < vy or y + 40 > vy + vh:
            return None
        return self.cfg["geometry"]

    def _default_geometry(self):
        """Bottom-centre, tall enough for one finished sentence plus the live one."""
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        scale = self.root.winfo_fpixels("1i") / 96
        size = self.cfg["font_size"]
        zh = tkfont.Font(family=UI_FONT, size=size).metrics("linespace")
        en = tkfont.Font(family="Segoe UI", size=max(9, round(size * 0.55))).metrics("linespace")
        w = int(min(sw * 0.7, 1000 * scale))
        h = int(28 * scale + 4 * zh + 2 * en + 3 * size * 0.45 * scale)
        return f"{w}x{h}+{(sw - w) // 2}+{int(sh - h - 70 * scale)}"

    def _make_app_window(self):
        """Give the frameless window a taskbar button so it can be minimized and found again."""
        try:
            u32 = ctypes.windll.user32
            hwnd = u32.GetParent(self.root.winfo_id())
            style = u32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            u32.SetWindowLongW(hwnd, -20, (style & ~0x80) | 0x40000)  # -TOOLWINDOW +APPWINDOW
            self.root.withdraw()
            self.root.after(10, self.root.deiconify)
            self.root.after(60, lambda: _set_window_icon(self.root))
            self.hwnd = hwnd
        except Exception:
            log.exception("taskbar setup failed")
            self.hwnd = None

    def minimize(self):
        if getattr(self, "hwnd", None):
            ctypes.windll.user32.ShowWindow(self.hwnd, 6)  # SW_MINIMIZE

    def _hover_tick(self):
        x, y = self.root.winfo_pointerxy()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        inside = rx <= x < rx + self.root.winfo_width() and ry <= y < ry + self.root.winfo_height()
        if inside != self._hover:
            self._hover = inside
            color = BAR_FG if inside else BAR_DIM
            for b in self.buttons:
                b.configure(fg=color)
            self.grip.configure(fg=color)
            self._update_status()
        self.root.after(250, self._hover_tick)

    # ---- move & resize: drag an edge or corner to resize, anywhere else to move ----

    def _rect(self):
        """(x, y, w, h) as Windows has it right now. Tk's winfo_*/geometry() lag behind a
        resize until its Configure event is processed, which would make drags jump."""
        hwnd = getattr(self, "hwnd", None)
        r = wintypes.RECT()
        if hwnd and ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return r.left, r.top, r.right - r.left, r.bottom - r.top
        root = self.root
        return root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height()

    def _zone(self, e):
        """"", or which border(s) the pointer is on: "n", "se", ..."""
        if e.widget is self.grip:
            return "se"
        rx, ry, w, h = self._rect()
        x, y, m = e.x_root - rx, e.y_root - ry, self._edge
        ns = "n" if y < m else "s" if y >= h - m else ""
        we = "w" if x < m else "e" if x >= w - m else ""
        if ns and not we:  # corners get a longer grab area along each edge
            we = "w" if x < 3 * m else "e" if x >= w - 3 * m else ""
        elif we and not ns:
            ns = "n" if y < 3 * m else "s" if y >= h - 3 * m else ""
        return ns + we

    def _on_motion(self, e):
        w = e.widget
        if self._mode or not isinstance(w, tk.Misc):
            return
        own = self._cursors.setdefault(w, w.cget("cursor"))
        zone = self._zone(e)
        want = RESIZE_CURSORS[zone] if zone else own
        if w.cget("cursor") != want:
            w.configure(cursor=want)

    def _on_press(self, e):
        zone = self._zone(e)
        if not zone and e.widget in self.buttons:
            self._mode = None  # a click; the button handles it on release
            return
        self._mode = "resize" if zone else "move"
        self._moved = False
        self._start = (e.x_root, e.y_root, *self._rect(), zone)

    def _on_drag(self, e):
        if not self._mode:
            return
        x0, y0, x, y, w, h, zone = self._start
        dx, dy = e.x_root - x0, e.y_root - y0
        self._moved = True
        if self._mode == "move":
            self.root.geometry(f"+{x + dx}+{y + dy}")
            return
        min_w, min_h = self._min_size
        if "e" in zone:
            w = max(min_w, w + dx)
        elif "w" in zone:
            new_w = max(min_w, w - dx)
            x, w = x + w - new_w, new_w  # the right edge stays put
        if "s" in zone:
            h = max(min_h, h + dy)
        elif "n" in zone:
            new_h = max(min_h, h - dy)
            y, h = y + h - new_h, new_h
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _on_release(self, e):
        if self._mode and self._moved:
            self._save_geometry()
        self._mode = None

    def _click(self, e, cmd):
        # Runs before _on_release: skip if this press started a resize, or the
        # pointer slid off the button before letting go.
        if self._mode is None and e.widget.winfo_containing(e.x_root, e.y_root) is e.widget:
            self.root.after_idle(cmd)

    def _save_geometry(self):
        hwnd = getattr(self, "hwnd", None)
        if hwnd and ctypes.windll.user32.IsIconic(hwnd):
            return  # minimized: Windows reports a placeholder rect; the last real one is already saved
        x, y, w, h = self._rect()
        self.cfg["geometry"] = f"{w}x{h}+{x}+{y}"
        config.save(self.cfg)

    # ---- rendering ------------------------------------------------------------

    def _apply_fonts(self):
        size = self.cfg["font_size"]
        zh = (UI_FONT, size)
        en = ("Segoe UI", max(9, round(size * 0.55)))
        gap = round(size * 0.45)
        t = self.text
        big_en = not self.cfg["translate"]  # English only: show it at full size
        t.tag_configure("en", font=zh if big_en else en, foreground=ZH if big_en else EN, spacing1=gap)
        t.tag_configure("en_live", font=zh if big_en else en, foreground=ZH_LIVE if big_en else EN_LIVE, spacing1=gap)
        zh_gap = 1 if self.cfg["show_en"] else gap
        t.tag_configure("zh", font=zh, foreground=ZH, spacing1=zh_gap)
        t.tag_configure("zh_pending", font=zh, foreground=ZH_PENDING, spacing1=zh_gap)
        t.tag_configure("zh_live", font=zh, foreground=ZH_LIVE, spacing1=zh_gap)
        t.tag_configure("hint", font=(UI_FONT, max(10, round(size * 0.6))), foreground=EN, spacing1=gap)
        self.dirty = True

    def _render(self):
        self.dirty = False
        translate = self.cfg["translate"]
        show_en = self.cfg["show_en"] or not translate
        groups = []  # one list of (text, tag) lines per sentence, oldest first
        n = self.cfg["lines"]  # -1: as many as fit
        for e in self.entries[-n:] if n > 0 else [] if n == 0 else self.entries[-60:]:
            g = [(e.en, "en")] if show_en else []
            zh, tag = e.display()
            if translate and zh:
                g.append((zh, tag))
            groups.append(g)
        if self.live_en:
            g = [(self.live_en, "en_live")] if show_en else []
            if translate:
                g.append((self.live_zh or "…", "zh_live"))
            groups.append(g)
        if not groups:
            groups.append([(self._hint(), "hint")])
        # Newest sentence first, then older ones on top for as long as they fit
        # whole; never one cut in half at the top edge.
        t = self.text
        pady = int(t.cget("pady"))
        room = max(1, self.body.winfo_height() - 2 * pady)
        t.configure(state="normal")
        t.delete("1.0", "end")
        height = 0
        for i, g in enumerate(reversed(groups)):
            chunk = []
            for j, (line, tag) in enumerate(g):
                chunk += [line if i == 0 and j == len(g) - 1 else line + "\n", tag]
            t.insert("1.0", *chunk)
            new_height = t.count("1.0", "end", "update", "ypixels") or 0
            if i and new_height > room:
                t.delete("1.0", f"1.0 + {sum(len(c) for c in chunk[::2])} chars")
                break
            height = new_height
        t.configure(state="disabled")
        t.place_configure(height=min(room, height) + 2 * pady)
        t.see("end")

    def _hint(self):
        if self.engine.asr is None:
            return self.status["asr"]
        if self.paused:
            return "已暂停，点「继续」恢复"
        if self.cfg["source"].startswith("mic:"):
            return "正在聆听麦克风…  对着麦克风说英文即可看到同传字幕（右键打开设置）"
        return "正在聆听电脑播放的声音…  播放英文即可看到同传字幕（右键打开设置）"

    # ---- engine events ----------------------------------------------------------

    def _poll(self):
        try:
            for _ in range(1000):
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.test_wav and self.engine.asr and (self.engine.translator or not self.cfg["translate"]):
            self.engine.set_source("file:" + self.test_wav)
            self.test_wav = None
        if self.dirty:
            self._render()
        self.root.after(30, self._poll)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "live":
            _, epoch, en = ev
            if epoch != self.live_epoch:
                self.live_epoch, self.live_zh, self.live_zh_src, self.live_zh_complete = epoch, "", "", False
            self.live_en = en
            self.dirty = True
        elif kind == "live_zh":
            _, epoch, src, zh, done = ev
            # Once a complete live translation is on screen, only swap in other complete
            # ones; streaming every token of each re-translation would make it flicker.
            if epoch == self.live_epoch and (done or not self.live_zh_complete):
                self.live_zh, self.live_zh_src = zh, src
                self.live_zh_complete |= done
                self.dirty = True
        elif kind == "final":
            _, sid, epoch, en = ev
            same = self.live_zh_complete and _norm(self.live_zh_src) == _norm(en)
            self.entries.append(Entry(sid, en, self.live_zh if same else ""))
            del self.entries[:-3000]
            self.live_epoch, self.live_en, self.live_zh, self.live_zh_src = epoch, "", "", ""
            self.live_zh_complete = False
            self.dirty = True
        elif kind == "final_zh":
            _, sid, zh, done, _ms = ev
            for e in reversed(self.entries):
                if e.id == sid:
                    e.zh, e.done = zh, done
                    if done and self.history:
                        self.history.add(e)
                    break
            self.dirty = True
        elif kind == "level":
            self._draw_level(ev[1])
        elif kind == "stats":
            self.stats = ev[1]
            self._update_status()
        elif kind == "status":
            self.status[ev[1]] = ev[2]
            self._apply_fonts()
            self._update_status()
        elif kind == "error":
            self.error, self.error_until = ev[1], time.time() + 10
            self._update_status()

    def _draw_level(self, peak):
        db = 20 * math.log10(max(peak, 1e-5))
        lit = max(0, min(5, round((db + 50) / 9)))
        for i, bar in enumerate(self.meter_bars):
            self.meter.itemconfigure(bar, fill=GREEN if i < lit else BAR_DIM)

    def _status_tick(self):
        self._update_status()
        self.root.after(1000, self._status_tick)

    def _update_status(self):
        s = self.stats
        err = self.error if time.time() < self.error_until else s.get("audio_error")
        if err:
            text, color, dot = err, RED, RED
        elif self.engine.asr is None or (self.cfg["translate"] and self.engine.translator is None
                                         and "失败" not in self.status["mt"]):
            text, color, dot = f"{self.status['asr']}  {self.status['mt']}", BAR_FG, YELLOW
        else:
            parts = [f"识别 {s['asr_ms']:.0f}ms" if s.get("asr_ms") else "识别就绪"]
            if self.cfg["translate"]:
                parts.append(f"翻译 {s['mt_ms']:.0f}ms" if s.get("mt_ms") else self.status["mt"])
            if s.get("device"):
                parts.append(s["device"])
            text = "  ·  ".join(parts)
            color = BAR_FG if self._hover else BAR_DIM
            dot = GRAY if self.paused else (GREEN if "失败" not in self.status["mt"] else YELLOW)
        self.status_lbl.configure(text=text, fg=color)
        self.dot.itemconfigure(self.dot_id, fill=dot)

    # ---- actions --------------------------------------------------------------

    def toggle_pause(self):
        self.paused = not self.paused
        self.engine.set_paused(self.paused)
        self.pause_btn.configure(text="继续" if self.paused else "暂停")
        self._update_status()
        self.dirty = True

    def toggle_en(self):
        self._set("show_en", not self.cfg["show_en"])

    def zoom(self, step):
        self._set("font_size", max(10, min(48, self.cfg["font_size"] + step)))

    def _set(self, key, value):
        self.cfg[key] = value
        config.save(self.cfg)
        if key == "opacity":
            self.root.attributes("-alpha", value)
        elif key in ("source",):
            self.engine.set_source(value)
        elif key == "asr":
            self.engine.load_asr()
        elif key in ("translate", "mt", "mt_gpu"):
            self.engine.restart_mt()
        self._apply_fonts()
        self._update_status()

    def open_history(self):
        if self.history is None:
            self.history = History(self)
        else:
            self.history.deiconify()
            self.history.lift()

    def open_transcripts(self):
        os.makedirs(config.TRANSCRIPT_DIR, exist_ok=True)
        os.startfile(os.path.abspath(config.TRANSCRIPT_DIR))

    def edit_glossary(self):
        if not os.path.exists(config.GLOSSARY_PATH):
            with open(config.GLOSSARY_PATH, "w", encoding="utf-8") as f:
                f.write("# 每行一条「英文 = 中文」\n")
        os.startfile(os.path.abspath(config.GLOSSARY_PATH))

    def make_shortcut(self):
        def run():
            made = setup_models.create_shortcuts(("Desktop",))
            self.events.put(("error", "已在桌面创建快捷方式" if made else "创建快捷方式失败"))
        threading.Thread(target=run, daemon=True).start()

    def quit(self):
        self._save_geometry()
        self.engine.shutdown()
        self.root.destroy()

    # ---- settings menu ------------------------------------------------------------

    def _var(self, key, cls):
        v = self._vars.get(key)
        if v is None:
            v = self._vars[key] = cls(self.root)
        v.set(self.cfg[key])
        return v

    def _show_menu_at_button(self):
        b = self.menu_btn
        self._popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def _show_menu(self, e):
        self._popup(e.x_root, e.y_root)

    def _popup(self, x, y):
        m = self._build_menu()
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0, font=(UI_FONT, 10))

        src = tk.Menu(m, tearoff=0, font=(UI_FONT, 10))
        v = self._var("source", tk.StringVar)
        try:
            sources = audio.list_sources()
        except Exception as e:
            log.exception("device listing failed")
            sources = [(audio.SYSTEM_DEFAULT, f"系统声音（默认）  [设备列表读取失败：{e}]")]
        for i, (sid, label) in enumerate(sources):
            if i and sid == audio.MIC_DEFAULT:
                src.add_separator()
            src.add_radiobutton(label=label, value=sid, variable=v, command=lambda s=sid: self._set("source", s))
        m.add_cascade(label="音频来源", menu=src)

        asr = tk.Menu(m, tearoff=0, font=(UI_FONT, 10))
        v = self._var("asr", tk.StringVar)
        for key in config.asr_installed():
            asr.add_radiobutton(label=config.ASR_MODELS[key]["label"], value=key, variable=v,
                                command=lambda k=key: self._set("asr", k))
        m.add_cascade(label="识别模式", menu=asr)

        mt = tk.Menu(m, tearoff=0, font=(UI_FONT, 10))
        mt.add_checkbutton(label="开启翻译", variable=self._var("translate", tk.BooleanVar),
                           command=lambda: self._set("translate", not self.cfg["translate"]))
        mt.add_checkbutton(label="使用显卡（GPU）", variable=self._var("mt_gpu", tk.BooleanVar),
                           command=lambda: self._set("mt_gpu", not self.cfg["mt_gpu"]))
        mt.add_separator()
        v = self._var("mt", tk.StringVar)
        for key in config.mt_installed():
            mt.add_radiobutton(label=config.MT_MODELS[key]["label"], value=key, variable=v,
                               command=lambda k=key: self._set("mt", k))
        mt.add_separator()
        mt.add_command(label="编辑术语表…", command=self.edit_glossary)
        m.add_cascade(label="翻译", menu=mt)

        disp = tk.Menu(m, tearoff=0, font=(UI_FONT, 10))
        disp.add_checkbutton(label="显示英文原文", variable=self._var("show_en", tk.BooleanVar),
                             command=self.toggle_en)
        for label, key, values, fmt in (("显示句数", "lines", (-1, 0, 1, 2, 3, 4, 6), "{}"),
                                        ("字号", "font_size", (12, 14, 16, 18, 20, 24, 28, 32, 40), "{}"),
                                        ("不透明度", "opacity", (1.0, 0.9, 0.8, 0.7, 0.6, 0.5), "{:.0%}")):
            sub = tk.Menu(disp, tearoff=0, font=(UI_FONT, 10))
            v = self._var(key, tk.DoubleVar if key == "opacity" else tk.IntVar)
            for val in values:
                name = {("lines", -1): "自动（填满窗口）", ("lines", 0): "只显示正在说的"}.get((key, val))
                sub.add_radiobutton(label=name or fmt.format(val), value=val, variable=v,
                                    command=lambda k=key, x=val: self._set(k, x))
            disp.add_cascade(label=label, menu=sub)
        m.add_cascade(label="显示", menu=disp)

        m.add_separator()
        m.add_checkbutton(label="自动保存同传记录", variable=self._var("save_transcript", tk.BooleanVar),
                          command=lambda: self._set("save_transcript", not self.cfg["save_transcript"]))
        m.add_command(label="查看记录", command=self.open_history)
        m.add_command(label="打开记录文件夹", command=self.open_transcripts)
        m.add_command(label="创建桌面快捷方式", command=self.make_shortcut)
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        return m


class SetupWindow:
    """First run (or the folder was copied without its models): download what is missing."""

    def __init__(self, root, todo):
        self.root, self.todo, self.ok = root, todo, False
        self.events = queue.Queue()
        self._sample = (0.0, 0)  # (time, bytes) for the speed readout
        self._rate = 0.0
        root.title(f"{config.APP_NAME} · 首次运行")
        root.configure(bg=BG)
        root.resizable(False, False)
        pad = int(18 * root.winfo_fpixels("1i") / 96)
        text = dict(bg=BG, anchor="w", justify="left")
        tk.Label(root, text="第一次使用，需要先下载模型", font=(UI_FONT, 14, "bold"), fg=ZH, **text).pack(
            fill="x", padx=pad, pady=(pad, 2))
        self.summary = tk.Label(root, font=(UI_FONT, 10), fg=EN, **text)
        self.summary.pack(fill="x", padx=pad)
        self.items = tk.Frame(root, bg=BG)
        self.items.pack(fill="x", padx=pad, pady=(10, 6))
        check = dict(bg=BG, fg=ZH_PENDING, activebackground=BG, activeforeground=ZH, selectcolor="#2a2c34",
                     font=(UI_FONT, 10), anchor="w", highlightthickness=0, bd=0)
        self.mirror = tk.BooleanVar(value=False)
        tk.Checkbutton(root, text="国内网络：翻译模型改从 hf-mirror.com 镜像下载", variable=self.mirror,
                       **check).pack(fill="x", padx=pad)
        self.shortcut = tk.BooleanVar(value=config.FROZEN)
        if config.FROZEN:
            tk.Checkbutton(root, text="在桌面和开始菜单创建快捷方式", variable=self.shortcut, **check).pack(
                fill="x", padx=pad)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Setup.Horizontal.TProgressbar", troughcolor="#2a2c34", background="#6366F1",
                        bordercolor="#2a2c34", lightcolor="#6366F1", darkcolor="#6366F1")
        self.bar = ttk.Progressbar(root, style="Setup.Horizontal.TProgressbar", maximum=1000,
                                   length=int(30 * pad))
        self.bar.pack(fill="x", padx=pad, pady=(14, 4))
        self.status = tk.Label(root, text="准备就绪，点「开始下载」", font=(UI_FONT, 9), fg=EN, **text)
        self.status.pack(fill="x", padx=pad)
        buttons = tk.Frame(root, bg=BG)
        buttons.pack(fill="x", padx=pad, pady=(12, pad))
        btn = dict(font=(UI_FONT, 10), relief="flat", bd=0, padx=16, pady=4, cursor="hand2")
        self.go = tk.Button(buttons, text="开始下载", command=self.start, bg="#6366F1", fg="#ffffff",
                            activebackground="#7c7ff5", activeforeground="#ffffff", **btn)
        self.go.pack(side="right")
        tk.Button(buttons, text="退出", command=root.destroy, bg="#2a2c34", fg="#e6e6ea",
                  activebackground="#3a3d48", activeforeground="#ffffff", **btn).pack(side="right", padx=(0, 10))
        self._show_items()
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
        root.bind("<Return>", lambda e: self.go["state"] == "normal" and self.start())
        root.after(50, lambda: (_set_window_icon(root), _dark_title_bar(root)))
        # Come to the front once, even if Windows' focus-stealing guard opened us behind
        # whatever the user switched to while the exe was starting.
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))
        root.focus_force()
        self.go.focus_set()
        if os.environ.get("LT_AUTOSTART") == "1":  # unattended setup
            root.after(500, self.start)

    def _show_items(self):
        for w in self.items.winfo_children():
            w.destroy()
        for _, label, mb in self.todo:
            row = tk.Frame(self.items, bg=BG)
            row.pack(fill="x")
            tk.Label(row, text="·  " + label, font=(UI_FONT, 10), fg=ZH_PENDING, bg=BG).pack(side="left")
            tk.Label(row, text=f"{mb:,} MB", font=(UI_FONT, 10), fg=EN, bg=BG).pack(side="right")
        total = sum(mb for _, _, mb in self.todo)
        self.summary.configure(text=f"共约 {total / 1024:.1f} GB，只需下载一次，之后完全离线使用。")

    def run(self):
        self.root.mainloop()
        return self.ok

    def start(self):
        self.go.configure(state="disabled", text="下载中…")
        self.status.configure(fg=EN)
        args = (list(self.todo), self.mirror.get(), self.shortcut.get())
        threading.Thread(target=self._work, args=args, daemon=True).start()
        self.root.after(150, self._poll)

    def _work(self, todo, mirror, shortcut):
        total = sum(mb for _, _, mb in todo) * 2 ** 20
        base = 0
        try:
            for key, label, mb in todo:
                def progress(done, _size, phase, label=label, base=base):
                    self.events.put(("progress", label, phase, base + done, total))
                setup_models.install(key, progress, mirror)
                base += mb * 2 ** 20
            if shortcut:
                setup_models.create_shortcuts()
            self.events.put(("done",))
        except Exception as e:
            log.exception("setup failed")
            self.events.put(("error", str(e)))

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "progress":
                    self._progress(*ev[1:])
                elif ev[0] == "done":
                    self.ok = True
                    self.root.destroy()
                    return
                elif ev[0] == "error":
                    self.todo = setup_models.needed()  # finished parts are skipped on retry
                    self._show_items()
                    self.status.configure(text=f"下载失败：{ev[1][:160]}\n检查网络后点「重试」，会从断点继续。",
                                          fg=RED)
                    self.go.configure(state="normal", text="重试")
                    return
        except queue.Empty:
            pass
        self.root.after(150, self._poll)

    def _progress(self, label, phase, done, total):
        self.bar["value"] = min(1000, 1000 * done / max(1, total))
        if phase == "unpack":
            self.status.configure(text=f"正在解压：{label}…")
            return
        now = time.time()
        t0, b0 = self._sample
        if now - t0 >= 1.0:
            if t0:
                self._rate = max(0.0, (done - b0) / (now - t0) / 2 ** 20)
            self._sample = (now, done)
        speed = f"  ·  {self._rate:.1f} MB/s" if self._rate else ""
        self.status.configure(text=f"正在下载：{label}    {done / 2 ** 20:,.0f} / {total / 2 ** 20:,.0f} MB{speed}")


_MUTEX = None


def _claim_single_instance():
    """False if a copy is already running; its window is brought to the front instead."""
    global _MUTEX
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    _MUTEX = k32.CreateMutexW(None, False, "Local\\HaoziLiveTranslate")
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return True
    u32 = ctypes.windll.user32
    hwnd = u32.FindWindowW(None, config.APP_NAME)
    if hwnd:
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
    return False


def _new_root():
    root = tk.Tk()
    try:
        root.iconbitmap(default=config.ICON_PATH)
    except tk.TclError:
        log.warning("icon not found: %s", config.ICON_PATH)
    return root


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-wav", help="feed a WAV file instead of a sound device (testing)")
    args = ap.parse_args()

    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(filename=os.path.join(config.LOG_DIR, "app.log"), filemode="w", encoding="utf-8",
                        level=logging.INFO, format="%(asctime)s %(threadName)s %(name)s %(levelname)s %(message)s")
    sys.excepthook = lambda *a: log.critical("uncaught", exc_info=a)
    if not args.test_wav and not _claim_single_instance():
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    if not config.FROZEN:  # own taskbar identity instead of grouping under python
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Haozi.LiveTranslate")

    root = _new_root()
    todo = setup_models.needed()
    if todo:
        if not SetupWindow(root, todo).run():
            return
        root = _new_root()

    cfg = config.load()
    if cfg["asr"] not in config.asr_installed():
        cfg["asr"] = config.asr_installed()[0]
    if cfg["mt"] not in config.mt_installed():
        cfg["mt"] = config.mt_installed()[0]
    app = App(root, cfg, test_wav=os.path.join(_LAUNCH_DIR, args.test_wav) if args.test_wav else None)
    root.protocol("WM_DELETE_WINDOW", app.quit)
    root.mainloop()


if __name__ == "__main__":
    main()
