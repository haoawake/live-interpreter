"""Windows specifics: Win32 window tweaks, single instance, Job Object, shell helpers.

sys_mac.py exports the same names for macOS; app.py and mt.py import whichever
matches the platform as `plat`.
"""
import ctypes
import logging
import os
import subprocess
from ctypes import wintypes

import config

log = logging.getLogger(__name__)

MAC = False
UI_FONT = "Microsoft YaHei UI"
EN_FONT = "Segoe UI"
SYMBOL_FONT = "Segoe UI"
FONT_SCALE = 1.0
NO_WINDOW = subprocess.CREATE_NO_WINDOW  # the packaged app has no console to borrow
GPU_LABEL = "使用显卡（GPU）"
GPU_NAME = "GPU"
MOVE_CURSOR = "fleur"
BUTTON_CURSOR = "hand2"
RESIZE_CURSORS = {"n": "size_ns", "s": "size_ns", "e": "size_we", "w": "size_we",
                  "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw"}
MENU_EVENTS = ("<Button-3>",)
ZOOM_EVENTS = ("<Control-MouseWheel>",)
MUTEX_NAME = "Local\\HaoziLiveTranslate"


def ui_scale(root):
    return root.winfo_fpixels("1i") / 96


def font(size):
    return round(size * FONT_SCALE)


# ---- process ----------------------------------------------------------------

_MUTEX = None


def claim_single_instance():
    """False if a copy is already running; its window is brought to the front instead."""
    global _MUTEX
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    _MUTEX = k32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return True
    u32 = ctypes.windll.user32
    hwnd = u32.FindWindowW(None, config.APP_NAME)
    if hwnd:
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
    return False


def app_running():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenMutexW(0x00100000, False, MUTEX_NAME)  # SYNCHRONIZE
    if h:
        k32.CloseHandle(h)
    return bool(h)


def process_setup():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    if not config.FROZEN:  # own taskbar identity instead of grouping under python
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Haozi.LiveTranslate")


def open_path(path):
    os.startfile(os.path.abspath(path))


def edit_text(path):
    os.startfile(os.path.abspath(path))


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in
                ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def kill_with_parent(proc):
    """Put `proc` in a job that Windows kills when this process exits, even on a crash,
    so a llama-server never lingers holding VRAM."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle)))
    return job  # the handle must stay open for as long as we live


# ---- windows ------------------------------------------------------------------

def set_window_icon(widget):
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


def dark_title_bar(widget):
    """Match the dark windows with a dark Windows title bar (Windows 10 20H1+)."""
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x27)  # redraw frame, keep pos/size/z
    except Exception:
        log.exception("dark title bar failed")


def style_window(widget):
    """Icon and dark title bar for an ordinary (framed) window."""
    widget.after(50, lambda: (set_window_icon(widget), dark_title_bar(widget)))


def new_root(tk):
    root = tk.Tk()
    try:
        root.iconbitmap(default=config.ICON_PATH)
    except tk.TclError:
        log.warning("icon not found: %s", config.ICON_PATH)
    return root


def make_app_window(root):
    """Give the frameless window a taskbar button so it can be minimized and found again.
    Returns the HWND, which the other window functions take."""
    try:
        u32 = ctypes.windll.user32
        hwnd = u32.GetParent(root.winfo_id())
        style = u32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        u32.SetWindowLongW(hwnd, -20, (style & ~0x80) | 0x40000)  # -TOOLWINDOW +APPWINDOW
        root.withdraw()
        root.after(10, root.deiconify)
        root.after(60, lambda: set_window_icon(root))
        return hwnd
    except Exception:
        log.exception("taskbar setup failed")
        return None


def minimize(root, hwnd):
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE


def is_minimized(hwnd):
    return bool(hwnd and ctypes.windll.user32.IsIconic(hwnd))


def window_rect(root, hwnd):
    """(x, y, w, h) as Windows has it right now. Tk's winfo_*/geometry() lag behind a
    resize until its Configure event is processed, which would make drags jump."""
    r = wintypes.RECT()
    if hwnd and ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    return root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height()


def on_screen(x, y, w, h):
    """Whether a window there could still be grabbed (e.g. its monitor was not unplugged)."""
    metric = ctypes.windll.user32.GetSystemMetrics
    vx, vy, vw, vh = metric(76), metric(77), metric(78), metric(79)  # virtual screen
    return not (x + w - 80 < vx or x + 80 > vx + vw or y < vy or y + 40 > vy + vh)


def work_area(root):
    """(x, y, w, h) of the area where the overlay goes by default, and the gap kept above the taskbar."""
    scale = ui_scale(root)
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight(), 70 * scale
