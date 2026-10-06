"""同声传译：英文语音 -> 中文字幕的置顶悬浮窗。"""
import argparse
import json
import logging
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import font as tkfont
from tkinter import ttk

import config

_LAUNCH_DIR = os.getcwd()
os.chdir(config.APP_DIR)  # models are opened by relative path, see config.py

import audio  # noqa: E402
import setup_models  # noqa: E402
from engine import Engine  # noqa: E402

if sys.platform == "darwin":
    import audio_mac  # noqa: E402
    import sys_mac as plat  # noqa: E402
else:
    import sys_win as plat  # noqa: E402

log = logging.getLogger("app")

BG = "#15161b"
BAR_FG, BAR_DIM, BAR_HOVER = "#a3a8b6", "#474b57", "#ffffff"
ZH, ZH_PENDING, ZH_LIVE = "#ffffff", "#c3c7d1", "#bfe0ff"
EN, EN_LIVE = "#8d92a0", "#6f7481"
GREEN, YELLOW, RED, GRAY = "#4cd07d", "#f0b429", "#ff5c5c", "#6b6f7a"
UI_FONT, EN_FONT = plat.UI_FONT, plat.EN_FONT
F = plat.font  # point sizes are tuned on Windows; macOS draws its points smaller
MAC = plat.MAC
FOLDER_LABEL = "在访达中打开记录文件夹" if MAC else "打开记录文件夹"


def _norm(s):
    return re.sub(r"[\W_]+", "", s.lower())


def _usable_cursor(widget, *names):
    """The first cursor name this Tk accepts (they differ between Windows and Aqua)."""
    for name in names:
        try:
            widget.configure(cursor=name)
            return name
        except tk.TclError:
            continue
        finally:
            widget.configure(cursor="")
    return ""


def _button(parent, text, cmd, primary=False):
    """A flat dark button on Windows; a native one on macOS (Aqua ignores button colours)."""
    if MAC:
        return ttk.Button(parent, text=text, command=cmd, default="active" if primary else "normal")
    colors = (dict(bg="#6366F1", fg="#ffffff", activebackground="#7c7ff5") if primary else
              dict(bg="#2a2c34", fg="#e6e6ea", activebackground="#3a3d48"))
    return tk.Button(parent, text=text, command=cmd, relief="flat", activeforeground="#ffffff", **colors)


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
        if MAC:
            plat.style_window(self)
            self.bind("<Command-w>", lambda e: self.close())
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=10, pady=(8, 0))
        for label, cmd in (("拷贝全部" if MAC else "复制全部", self.copy_all), (FOLDER_LABEL, app.open_transcripts)):
            b = _button(bar, label, cmd)
            if not MAC:
                b.configure(font=(UI_FONT, 9), padx=10)
            b.pack(side="left", padx=(0, 8))
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        if MAC:
            sb = ttk.Scrollbar(body)  # native overlay-style scroller, dark with the window
        else:
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
                            insertbackground=ZH, yscrollcommand=sb.set, font=(UI_FONT, F(12)),
                            **({"highlightthickness": 0} if MAC else {}))  # no Aqua focus ring
        self.text.pack(fill="both", expand=True)
        sb.config(command=self.text.yview)
        self.text.tag_configure("en", foreground=EN, font=(EN_FONT, F(10)), spacing1=8)
        self.text.tag_configure("zh", foreground=ZH, font=(UI_FONT, F(12)))
        for e in app.entries:
            if e.done:
                self.add(e)
        self.protocol("WM_DELETE_WINDOW", self.close)
        if not MAC:
            plat.style_window(self)

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
    def __init__(self, root, cfg, test_wav=None, shot_dir=None, menubar=None):
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
        self.shot_dir = shot_dir
        self.hwnd = None
        self.banner = None
        self._banner_for = None
        self._dismissed = set()
        self._vars = {}
        self._build()
        if menubar is not None:
            self._extend_menubar(menubar)
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
        scale = plat.ui_scale(r)
        self._edge = max(6, round(7 * scale))  # how close to the border a press starts a resize
        self._min_size = (round(260 * scale), round(70 * scale))
        self._mode = None  # "move" / "resize" while the left button is held
        self._resize_cursors = {z: _usable_cursor(r, c) for z, c in plat.RESIZE_CURSORS.items()}
        button_cursor = _usable_cursor(r, plat.BUTTON_CURSOR, "hand2")
        move_cursor = _usable_cursor(r, plat.MOVE_CURSOR, "fleur")

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
        self.status_lbl = tk.Label(bar, text="", bg=BG, fg=BAR_DIM, font=(UI_FONT, F(9)), anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)

        self.buttons = []
        for text, cmd in (("✕", self.quit), ("—", self.minimize), ("设置", self._show_menu_at_button),
                          ("记录", self.open_history), ("英", self.toggle_en), ("A+", lambda: self.zoom(2)),
                          ("A−", lambda: self.zoom(-2)), ("暂停", self.toggle_pause)):
            b = tk.Label(bar, text=text, bg=BG, fg=BAR_DIM, font=(UI_FONT, F(9)), padx=5, cursor=button_cursor)
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
                            cursor=move_cursor, state="disabled", takefocus=0, selectbackground=BG,
                            inactiveselectbackground=BG, exportselection=False)
        self.text.place(x=0, rely=1.0, anchor="sw", relwidth=1.0, height=1)
        self.body.bind("<Configure>", lambda e: setattr(self, "dirty", True))
        self.grip = tk.Label(r, text="◢", bg=BG, fg=BAR_DIM, cursor=self._resize_cursors["se"],
                             font=(plat.SYMBOL_FONT, F(8)))
        self.grip.place(relx=1.0, rely=1.0, anchor="se")

        # Bound once on the toplevel, which is in every child's bindtags; binding the
        # children as well would run each handler twice.
        self._cursors = {}  # widget -> its own cursor, restored away from the border
        r.bind("<Motion>", self._on_motion)
        r.bind("<ButtonPress-1>", self._on_press)
        r.bind("<B1-Motion>", self._on_drag)
        r.bind("<ButtonRelease-1>", self._on_release)
        for ev in plat.MENU_EVENTS:
            r.bind(ev, self._show_menu)
        for ev in plat.ZOOM_EVENTS:
            self.text.bind(ev, lambda e: self.zoom(2 if e.delta > 0 else -2))

        self._hover = False
        self._apply_fonts()
        if MAC:
            r.deiconify()  # new_root() kept it hidden until it was borderless
        r.after(50, self._make_app_window)
        r.after(300, self._hover_tick)

    def _saved_geometry(self):
        """The last geometry, unless it is now off-screen (e.g. a monitor was unplugged):
        a frameless window placed there could never be dragged back."""
        m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", self.cfg["geometry"] or "")
        if not m:
            return None
        w, h, x, y = map(int, m.groups())
        if not plat.on_screen(x, y, w, h):
            return None
        return self.cfg["geometry"]

    def _default_geometry(self):
        """Bottom-centre, tall enough for one finished sentence plus the live one."""
        x0, y0, sw, bottom, gap = plat.work_area(self.root)
        scale = plat.ui_scale(self.root)
        size = self.cfg["font_size"]
        zh = tkfont.Font(family=UI_FONT, size=F(size)).metrics("linespace")
        en = tkfont.Font(family=EN_FONT, size=F(max(9, round(size * 0.55)))).metrics("linespace")
        w = int(min(sw * 0.7, 1000 * scale))
        h = int(28 * scale + 4 * zh + 2 * en + 3 * size * 0.45 * scale)
        return f"{w}x{h}+{x0 + (sw - w) // 2}+{int(bottom - h - gap)}"

    def _make_app_window(self):
        """Windows: a taskbar button for the frameless window. macOS: float it over every
        Space and full-screen app."""
        self.hwnd = plat.make_app_window(self.root)

    def minimize(self):
        plat.minimize(self.root, self.hwnd)

    def restore(self):
        """The Dock icon was clicked (macOS): bring the overlay back if it was hidden."""
        self.root.deiconify()
        self.root.lift()
        if MAC:
            self.root.after(50, self._make_app_window)

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
        """(x, y, w, h) as the window system has it right now. Tk's winfo_*/geometry() lag
        behind a resize until its Configure event is processed, which would make drags jump."""
        return plat.window_rect(self.root, self.hwnd)

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
        want = self._resize_cursors[zone] if zone else own
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
        if MAC and e.state & 0x4:  # Control-click opens the menu on a Mac, it is not a click
            return
        if self._mode is None and e.widget.winfo_containing(e.x_root, e.y_root) is e.widget:
            self.root.after_idle(cmd)

    def _save_geometry(self):
        if plat.is_minimized(self.hwnd) or (MAC and self.root.state() == "withdrawn"):
            return  # minimized: Windows reports a placeholder rect; the last real one is already saved
        x, y, w, h = self._rect()
        self.cfg["geometry"] = f"{w}x{h}+{x}+{y}"
        config.save(self.cfg)

    # ---- rendering ------------------------------------------------------------

    def _apply_fonts(self):
        size = self.cfg["font_size"]
        zh = (UI_FONT, F(size))
        en = (EN_FONT, F(max(9, round(size * 0.55))))
        gap = round(size * 0.45)
        t = self.text
        big_en = not self.cfg["translate"]  # English only: show it at full size
        t.tag_configure("en", font=zh if big_en else en, foreground=ZH if big_en else EN, spacing1=gap)
        t.tag_configure("en_live", font=zh if big_en else en, foreground=ZH_LIVE if big_en else EN_LIVE, spacing1=gap)
        zh_gap = 1 if self.cfg["show_en"] else gap
        t.tag_configure("zh", font=zh, foreground=ZH, spacing1=zh_gap)
        t.tag_configure("zh_pending", font=zh, foreground=ZH_PENDING, spacing1=zh_gap)
        t.tag_configure("zh_live", font=zh, foreground=ZH_LIVE, spacing1=zh_gap)
        t.tag_configure("hint", font=(UI_FONT, F(max(10, round(size * 0.6)))), foreground=EN, spacing1=gap)
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
        menu = "（右键或双指轻点打开设置）" if MAC else "（右键打开设置）"
        if self.cfg["source"].startswith("mic:"):
            return "正在聆听麦克风…  对着麦克风说英文即可看到同传字幕" + menu
        return "正在聆听电脑播放的声音…  播放英文即可看到同传字幕" + menu

    # ---- macOS permission banner ----------------------------------------------

    def _show_problem(self, problem):
        """A strip over the captions explaining a missing permission, with a button to
        the right pane of 系统设置 (macOS only; Windows has no such permissions)."""
        if problem in self._dismissed:
            problem = None
        if problem == self._banner_for:
            return
        self._banner_for = problem
        if self.banner is not None:
            self.banner.destroy()
            self.banner = None
        if not problem:
            return
        kind, message = audio_mac.PROBLEMS[problem]
        b = self.banner = tk.Frame(self.body, bg="#3a2a12", padx=12, pady=8)
        msg = tk.Label(b, text=message, bg="#3a2a12", fg="#ffd27a", font=(UI_FONT, F(10)), justify="left",
                       anchor="w")
        msg.pack(fill="x")
        b.bind("<Configure>", lambda e: msg.configure(wraplength=max(200, e.width - 24)))
        row = tk.Frame(b, bg="#3a2a12")
        row.pack(fill="x", pady=(6, 0))
        actions = [("打开系统设置", lambda: audio_mac.open_settings(kind))]
        if kind == "tap":
            actions.append(("改用麦克风", lambda: self._set("source", audio.MIC_DEFAULT)))
        actions.append(("关闭提示", lambda: (self._dismissed.add(problem), self._show_problem(None))))
        for text, cmd in actions:
            lbl = tk.Label(row, text=text, bg="#5a4318", fg="#ffffff", font=(UI_FONT, F(9)), padx=10, pady=3,
                           cursor=_usable_cursor(row, plat.BUTTON_CURSOR, "hand2"))
            lbl.pack(side="left", padx=(0, 8))
            lbl.bind("<ButtonRelease-1>", lambda e, c=cmd: self._click(e, c))
        b.place(x=8, y=4, relwidth=1.0, width=-16)

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
            if self.shot_dir:
                self._shots_started = time.time()
                self.root.after(500, self._shot_tick)
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
            if s.get("audio_notice"):
                parts.append(s["audio_notice"])
            elif s.get("device"):
                parts.append(s["device"])
            text = "  ·  ".join(parts)
            color = BAR_FG if self._hover else BAR_DIM
            dot = GRAY if self.paused else (GREEN if "失败" not in self.status["mt"] else YELLOW)
        self.status_lbl.configure(text=text, fg=color)
        self.dot.itemconfigure(self.dot_id, fill=dot)
        if MAC:
            self._show_problem(s.get("audio_problem"))

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
        plat.open_path(config.TRANSCRIPT_DIR)

    def edit_glossary(self):
        if not os.path.exists(config.GLOSSARY_PATH):
            with open(config.GLOSSARY_PATH, "w", encoding="utf-8") as f:
                f.write("# 每行一条「英文 = 中文」\n")
        plat.edit_text(config.GLOSSARY_PATH)

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

    def _menu(self, parent, **kw):
        if MAC:  # native menus: the system font and size, not ours
            return tk.Menu(parent, tearoff=0, **kw)
        return tk.Menu(parent, tearoff=0, font=(UI_FONT, 10), **kw)

    def _build_menu(self):
        m = self._menu(self.root)
        self._fill_menu(m)
        return m

    def _fill_menu(self, m, menubar=False):
        for child in m.winfo_children():
            child.destroy()
        m.delete(0, "end")

        src = self._menu(m)
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
        if MAC and not audio_mac.tap_supported():
            src.add_separator()
            src.add_command(label="系统声音：需要 macOS 14.2 或更高版本", state="disabled")
        m.add_cascade(label="音频来源", menu=src)

        asr = self._menu(m)
        v = self._var("asr", tk.StringVar)
        for key in config.asr_installed():
            asr.add_radiobutton(label=config.ASR_MODELS[key]["label"], value=key, variable=v,
                                command=lambda k=key: self._set("asr", k))
        m.add_cascade(label="识别模式", menu=asr)

        mt = self._menu(m)
        mt.add_checkbutton(label="开启翻译", variable=self._var("translate", tk.BooleanVar),
                           command=lambda: self._set("translate", not self.cfg["translate"]))
        mt.add_checkbutton(label=plat.GPU_LABEL, variable=self._var("mt_gpu", tk.BooleanVar),
                           command=lambda: self._set("mt_gpu", not self.cfg["mt_gpu"]))
        mt.add_separator()
        v = self._var("mt", tk.StringVar)
        for key in config.mt_installed():
            mt.add_radiobutton(label=config.MT_MODELS[key]["label"], value=key, variable=v,
                               command=lambda k=key: self._set("mt", k))
        mt.add_separator()
        mt.add_command(label="编辑术语表…", command=self.edit_glossary)
        m.add_cascade(label="翻译", menu=mt)

        disp = self._menu(m)
        disp.add_checkbutton(label="显示英文原文", variable=self._var("show_en", tk.BooleanVar),
                             command=self.toggle_en)
        for label, key, values, fmt in (("显示句数", "lines", (-1, 0, 1, 2, 3, 4, 6), "{}"),
                                        ("字号", "font_size", (12, 14, 16, 18, 20, 24, 28, 32, 40), "{}"),
                                        ("不透明度", "opacity", (1.0, 0.9, 0.8, 0.7, 0.6, 0.5), "{:.0%}")):
            sub = self._menu(disp)
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
        m.add_command(label=FOLDER_LABEL, command=self.open_transcripts)
        if not MAC:
            m.add_command(label="创建桌面快捷方式", command=self.make_shortcut)
        if not menubar:
            m.add_separator()
            m.add_command(label="退出", command=self.quit)

    def _extend_menubar(self, bar):
        """macOS menu bar: the overlay's settings menu, plus a 窗口 menu."""
        settings = self._menu(bar)
        settings.configure(postcommand=lambda: self._fill_menu(settings, menubar=True))
        self._fill_menu(settings, menubar=True)
        bar.add_cascade(label="设置", menu=settings)
        win = self._menu(bar)
        win.add_command(label="显示字幕窗口", command=self.restore)
        win.add_command(label="同传记录", command=self.open_history, accelerator="Command-L")
        win.add_separator()
        win.add_command(label="关闭窗口", command=self._close_front, accelerator="Command-W")
        bar.add_cascade(label="窗口", menu=win)

    def _close_front(self):
        if self.history is not None and self.root.focus_get() is not None \
                and self.root.focus_get().winfo_toplevel() is self.history:
            self.history.close()

    # ---- CI screenshots (macOS: the window renders itself, no screen-recording permission) ----

    def _shot(self, name, widget=None):
        if not self.shot_dir or not hasattr(plat, "snapshot"):
            return
        os.makedirs(self.shot_dir, exist_ok=True)
        try:
            plat.snapshot(widget or self.root, os.path.join(self.shot_dir, name + ".png"))
            subprocess.run(["screencapture", "-x", os.path.join(self.shot_dir, name + "-screen.png")],
                           timeout=20, capture_output=True)
            info = {"window": plat.window_info(plat.ns_window(widget or self.root)), "rect": self._rect(),
                    "entries": [(e.en, e.zh) for e in self.entries], "live": [self.live_en, self.live_zh]}
            with open(os.path.join(self.shot_dir, name + ".json"), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=1)
        except Exception:
            log.exception("screenshot failed")

    def _shot_tick(self):
        elapsed = time.time() - self._shots_started
        done = sum(1 for e in self.entries if e.done)
        cap = self.engine.capture
        finished = getattr(cap, "finished", None)
        if not getattr(self, "_shot1", False) and (done >= 2 and self.live_en or elapsed > 25):
            self._shot1 = True
            self._shot("overlay-live")
        if finished is not None and finished.is_set() and not getattr(self, "_shot2", False):
            self._shot2 = time.time()
        if getattr(self, "_shot2", False) and time.time() - self._shot2 > 4:
            self._shot("overlay-done")
            self.open_history()
            self.root.after(1500, lambda: (self._shot("history", self.history), self.quit()))
            return
        if elapsed > 240:
            self._shot("overlay-timeout")
            self.quit()
            return
        self.root.after(500, self._shot_tick)


class SetupWindow:
    """First run (or the folder was copied without its models): download what is missing."""

    def __init__(self, root, todo, shot_dir=None):
        self.root, self.todo, self.ok = root, todo, False
        # macOS keeps one Tk root for the whole run (a second Tk() after destroying the
        # first is not safe on Aqua): the setup window is a Toplevel of the hidden root.
        win = self.win = tk.Toplevel(root) if MAC else root
        self.events = queue.Queue()
        self._sample = (0.0, 0)  # (time, bytes) for the speed readout
        self._rate = 0.0
        bg = BG
        if MAC:
            plat.style_window(win)
            try:
                win.winfo_rgb("systemWindowBackgroundColor")
                bg = "systemWindowBackgroundColor"  # so the native controls blend in
            except tk.TclError:
                bg = "#2b2b2e"
        win.title(f"{config.APP_NAME} · 首次运行")
        win.configure(bg=bg)
        win.resizable(False, False)
        pad = int(18 * (1.0 if MAC else root.winfo_fpixels("1i") / 96))
        text = dict(bg=bg, anchor="w", justify="left")
        tk.Label(win, text="第一次使用，需要先下载模型", font=(UI_FONT, F(14), "bold"), fg=ZH, **text).pack(
            fill="x", padx=pad, pady=(pad, 2))
        self.summary = tk.Label(win, font=(UI_FONT, F(10)), fg=EN, **text)
        self.summary.pack(fill="x", padx=pad)
        self.bg = bg
        self.items = tk.Frame(win, bg=bg)
        self.items.pack(fill="x", padx=pad, pady=(10, 6))
        self.mirror = tk.BooleanVar(value=False)
        self.shortcut = tk.BooleanVar(value=config.FROZEN and not MAC)
        options = [("国内网络：翻译模型改从 hf-mirror.com 镜像下载", self.mirror)]
        if config.FROZEN and not MAC:  # a Mac app lives in 应用程序; Launchpad and Spotlight find it
            options.append(("在桌面和开始菜单创建快捷方式", self.shortcut))
        for label, var in options:
            if MAC:
                ttk.Checkbutton(win, text=label, variable=var).pack(fill="x", padx=pad, pady=(2, 0))
            else:
                tk.Checkbutton(win, text=label, variable=var, bg=BG, fg=ZH_PENDING, activebackground=BG,
                               activeforeground=ZH, selectcolor="#2a2c34", font=(UI_FONT, 10), anchor="w",
                               highlightthickness=0, bd=0).pack(fill="x", padx=pad)
        if MAC:
            self.bar = ttk.Progressbar(win, maximum=1000, length=int(30 * pad))
        else:
            style = ttk.Style(root)
            style.theme_use("clam")
            style.configure("Setup.Horizontal.TProgressbar", troughcolor="#2a2c34", background="#6366F1",
                            bordercolor="#2a2c34", lightcolor="#6366F1", darkcolor="#6366F1")
            self.bar = ttk.Progressbar(win, style="Setup.Horizontal.TProgressbar", maximum=1000,
                                       length=int(30 * pad))
        self.bar.pack(fill="x", padx=pad, pady=(14, 4))
        self.status = tk.Label(win, text="准备就绪，点「开始下载」", font=(UI_FONT, F(9)), fg=EN, **text)
        self.status.pack(fill="x", padx=pad)
        buttons = tk.Frame(win, bg=bg)
        buttons.pack(fill="x", padx=pad, pady=(12, pad))
        self.go = _button(buttons, "开始下载", self.start, primary=True)
        quit_btn = _button(buttons, "退出", root.destroy)
        if not MAC:
            for b in (self.go, quit_btn):
                b.configure(font=(UI_FONT, 10), bd=0, padx=16, pady=4, cursor="hand2")
        self.go.pack(side="right")
        quit_btn.pack(side="right", padx=(0, 10))
        self._show_items()
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 3}")
        win.bind("<Return>", lambda e: str(self.go["state"]) in ("normal", "!disabled") and self.start())
        win.protocol("WM_DELETE_WINDOW", root.destroy)
        if MAC:
            win.bind("<Command-w>", lambda e: root.destroy())
            plat.activate()
        else:
            plat.style_window(root)
            # Come to the front once, even if Windows' focus-stealing guard opened us behind
            # whatever the user switched to while the exe was starting.
            root.attributes("-topmost", True)
            root.after(400, lambda: root.attributes("-topmost", False))
        win.focus_force()
        self.go.focus_set()
        if os.environ.get("LT_AUTOSTART") == "1":  # unattended setup
            root.after(500, self.start)
        if shot_dir:
            root.after(1500, lambda: self._shot(shot_dir))

    def _shot(self, shot_dir):
        os.makedirs(shot_dir, exist_ok=True)
        try:
            plat.snapshot(self.win, os.path.join(shot_dir, "first-run.png"))
            subprocess.run(["screencapture", "-x", os.path.join(shot_dir, "first-run-screen.png")],
                           timeout=20, capture_output=True)
        except Exception:
            log.exception("screenshot failed")
        if os.environ.get("LT_AUTOSTART") != "1":
            self.root.destroy()

    def _show_items(self):
        for w in self.items.winfo_children():
            w.destroy()
        for _, label, mb in self.todo:
            row = tk.Frame(self.items, bg=self.bg)
            row.pack(fill="x")
            tk.Label(row, text="·  " + label, font=(UI_FONT, F(10)), fg=ZH_PENDING, bg=self.bg).pack(side="left")
            tk.Label(row, text=f"{mb:,} MB", font=(UI_FONT, F(10)), fg=EN, bg=self.bg).pack(side="right")
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
                    if MAC:
                        self.win.destroy()
                        self.root.quit()
                    else:
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


def _mac_menubar(root):
    """A Mac menu bar from the first window on; without one Tk shows its own developer
    menus (Source…, Run Widget Demo). The app menu's standard items are translated."""
    bar = tk.Menu(root)
    apple = tk.Menu(bar, name="apple", tearoff=0)
    apple.add_command(label=f"关于{config.APP_NAME}", command=plat.about_panel)
    bar.add_cascade(menu=apple)

    def virtual(event):
        w = root.focus_get()
        if w is not None:
            w.event_generate(event)
    edit = tk.Menu(bar, tearoff=0)
    edit.add_command(label="拷贝", accelerator="Command-C", command=lambda: virtual("<<Copy>>"))
    edit.add_command(label="全选", accelerator="Command-A", command=lambda: virtual("<<SelectAll>>"))
    bar.add_cascade(label="编辑", menu=edit)
    root.config(menu=bar)
    root.update_idletasks()
    plat.localize_app_menu()
    return bar


def _selftest(path):
    """--selftest OUT: import and touch everything the app needs, write a JSON report, exit."""
    report = {"version": config.VERSION, "platform": sys.platform, "frozen": config.FROZEN,
              "executable": sys.executable, "app_dir": config.APP_DIR}
    try:
        import numpy
        import sherpa_onnx
        report["numpy"] = numpy.__version__
        report["sherpa_onnx"] = getattr(sherpa_onnx, "__version__", "?")
        report["sherpa_onnx_api"] = hasattr(sherpa_onnx, "OnlineRecognizer")
        report["sources"] = audio.list_sources()
        r = tk.Tk()
        report["tk"] = r.tk.call("info", "patchlevel")
        report["windowingsystem"] = r.tk.call("tk", "windowingsystem")
        report["fonts_have_ui_font"] = UI_FONT in tkfont.families(r)
        r.destroy()
        report["needed"] = setup_models.needed()
        if MAC:
            report["helper"] = os.path.exists(config.MAC_HELPER)
            report["devices"] = audio_mac.devices()
        report["ok"] = True
    except Exception:
        report["ok"] = False
        report["error"] = traceback.format_exc()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return 0 if report["ok"] else 1


def main():
    argv = sys.argv[1:]
    if "--wav" in argv:  # headless pipeline test, see engine.main()
        import engine
        i = argv.index("--wav") + 1
        if i < len(argv):
            argv[i] = os.path.join(_LAUNCH_DIR, argv[i])
        return engine.main(argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-wav", help="feed a WAV file instead of a sound device (testing)")
    ap.add_argument("--selftest", help="write a JSON report of the runtime to this file and exit")
    ap.add_argument("--shot", help="save screenshots into this folder and quit (testing, macOS)")
    args, _ = ap.parse_known_args()  # Finder may add arguments of its own
    if args.selftest:
        sys.exit(_selftest(os.path.join(_LAUNCH_DIR, args.selftest)))
    shot_dir = os.path.join(_LAUNCH_DIR, args.shot) if args.shot else None

    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(filename=os.path.join(config.LOG_DIR, "app.log"), filemode="w", encoding="utf-8",
                        level=logging.INFO, format="%(asctime)s %(threadName)s %(name)s %(levelname)s %(message)s")
    sys.excepthook = lambda *a: log.critical("uncaught", exc_info=a)
    log.info("同声传译 %s on %s, data in %s", config.VERSION, sys.platform, config.APP_DIR)
    if not args.test_wav and not shot_dir and not plat.claim_single_instance():
        return
    plat.process_setup()
    config.ensure_glossary()

    root = plat.new_root(tk)
    menubar = None
    holder = {}
    if MAC:
        menubar = _mac_menubar(root)
        root.createcommand("::tk::mac::Quit", lambda: holder["app"].quit() if "app" in holder else root.destroy())
        root.createcommand("::tk::mac::ReopenApplication", lambda: "app" in holder and holder["app"].restore())
        root.createcommand("::tk::mac::ShowPreferences",
                           lambda: "app" in holder and holder["app"]._show_menu_at_button())
    todo = setup_models.needed()
    if todo or (shot_dir and os.environ.get("LT_SHOT_SETUP") == "1"):
        if not SetupWindow(root, todo, shot_dir if os.environ.get("LT_SHOT_SETUP") == "1" else None).run():
            return
        if shot_dir and not args.test_wav:  # CI: only the setup window was being tested
            return
        if not MAC:
            root = plat.new_root(tk)

    cfg = config.load()
    if cfg["asr"] not in config.asr_installed():
        cfg["asr"] = config.asr_installed()[0]
    if cfg["mt"] not in config.mt_installed():
        cfg["mt"] = config.mt_installed()[0]
    app = holder["app"] = App(root, cfg, test_wav=os.path.join(_LAUNCH_DIR, args.test_wav) if args.test_wav else None,
                              shot_dir=shot_dir, menubar=menubar)
    root.protocol("WM_DELETE_WINDOW", app.quit)
    root.mainloop()


if __name__ == "__main__":
    main()
