"""macOS specifics, the counterpart of sys_win.py.

Cocoa is reached through ctypes and the Objective-C runtime rather than PyObjC,
for the handful of things Tk does not expose: window level and Spaces behaviour,
App Nap, the application menu's (English) item titles, the About panel.
"""
import ctypes
import fcntl
import logging
import os
import subprocess

import config

log = logging.getLogger(__name__)

MAC = True
UI_FONT = "PingFang SC"
EN_FONT = "Helvetica Neue"
SYMBOL_FONT = "Helvetica Neue"
# Tk on Aqua treats a point as one screen point (72 dpi) where Windows uses 96 dpi:
# without this every font would come out a quarter smaller than on Windows.
FONT_SCALE = 4 / 3
NO_WINDOW = 0
GPU_LABEL = "使用 GPU 加速（Metal）"
GPU_NAME = "Metal"
MOVE_CURSOR = "arrow"
BUTTON_CURSOR = "pointinghand"
RESIZE_CURSORS = {"n": "resizeupdown", "s": "resizeupdown", "e": "resizeleftright", "w": "resizeleftright",
                  "nw": "resizetopleft", "se": "resizebottomright", "ne": "resizetopright",
                  "sw": "resizebottomleft"}
# Tk 8.6 on Aqua reports the secondary (right / two-finger) click as button 2, Tk 9 as 3;
# Control-click is the one-button way to the same menu.
MENU_EVENTS = ("<Button-2>", "<Button-3>", "<Control-Button-1>")
ZOOM_EVENTS = ("<Command-MouseWheel>", "<Control-MouseWheel>")

c_void_p, c_char_p, c_bool, c_ulong, c_long, c_double = (
    ctypes.c_void_p, ctypes.c_char_p, ctypes.c_bool, ctypes.c_ulong, ctypes.c_long, ctypes.c_double)


class CGRect(ctypes.Structure):
    _fields_ = [("x", c_double), ("y", c_double), ("w", c_double), ("h", c_double)]


_objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
for _name, _res, _args in (("objc_getClass", c_void_p, [c_char_p]), ("sel_registerName", c_void_p, [c_char_p]),
                           ("sel_getName", c_char_p, [c_void_p]), ("object_getClassName", c_char_p, [c_void_p])):
    getattr(_objc, _name).restype = _res
    getattr(_objc, _name).argtypes = _args
_MSG_SEND = ctypes.cast(_objc.objc_msgSend, c_void_p).value
_prototypes = {}


def send(obj, selector, *args, restype=c_void_p, argtypes=None):
    """objc_msgSend with an explicit signature (arm64 requires the exact one)."""
    if not obj:
        return None
    argtypes = tuple(argtypes) if argtypes is not None else (c_void_p,) * len(args)
    fn = _prototypes.get((restype, argtypes))
    if fn is None:
        fn = _prototypes[restype, argtypes] = ctypes.CFUNCTYPE(restype, c_void_p, c_void_p, *argtypes)(_MSG_SEND)
    return fn(obj, _objc.sel_registerName(selector.encode()), *args)


def cls(name):
    return _objc.objc_getClass(name.encode())


def nsstring(text):
    return send(cls("NSString"), "stringWithUTF8String:", text.encode(), argtypes=[c_char_p])


def pystring(ns):
    raw = send(ns, "UTF8String", restype=c_char_p)
    return raw.decode("utf-8", "replace") if raw else ""


def nsapp():
    return send(cls("NSApplication"), "sharedApplication")


def ui_scale(root):
    return 1.0  # Tk on Aqua already works in points


def font(size):
    return round(size * FONT_SCALE)


# ---- process ----------------------------------------------------------------

_LOCK = None
_ACTIVITY = None


def claim_single_instance():
    """False if a copy is already running (then it is brought to the front instead).

    Finder and the Dock never start a second copy of an app anyway; this covers
    starting the binary directly or a second copy of the .app elsewhere.
    """
    global _LOCK
    _LOCK = open(os.path.join(config.APP_DIR, ".lock"), "w")
    try:
        fcntl.flock(_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        subprocess.run(["open", "-b", config.BUNDLE_ID], capture_output=True, timeout=10)
        return False


def app_running():
    try:
        with open(os.path.join(config.APP_DIR, ".lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True


def process_setup():
    """Keep macOS from throttling us: the overlay usually sits over another, active app,
    and App Nap would otherwise coalesce our timers into visible caption lag."""
    global _ACTIVITY
    try:
        info = send(cls("NSProcessInfo"), "processInfo")
        options = 0x00FFFFFF & ~(1 << 20) | 0xFF00000000  # UserInitiatedAllowingIdleSystemSleep | LatencyCritical
        activity = send(info, "beginActivityWithOptions:reason:", options, nsstring("实时字幕"),
                        argtypes=[ctypes.c_uint64, c_void_p])
        _ACTIVITY = send(activity, "retain")
    except Exception:
        log.exception("App Nap opt-out failed")


def open_path(path):
    subprocess.Popen(["open", os.path.abspath(path)])


def edit_text(path):
    subprocess.Popen(["open", "-t", os.path.abspath(path)])  # the default text editor (文本编辑)


def kill_with_parent(proc):
    """macOS has no Job Objects or PDEATHSIG: a tiny watchdog (kqueue on our pid, see
    lt-audio.swift) SIGKILLs llama-server the moment we exit, crash included."""
    args = [config.MAC_HELPER, "watchdog", str(os.getpid()), str(proc.pid)]
    if not os.path.exists(config.MAC_HELPER):  # from source without a built helper
        args = ["/bin/sh", "-c", 'while kill -0 "$0" 2>/dev/null && kill -0 "$1" 2>/dev/null; do sleep 1; done; '
                                 'kill -0 "$0" 2>/dev/null || kill -9 "$1" 2>/dev/null', str(os.getpid()), str(proc.pid)]
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


# ---- windows ------------------------------------------------------------------

def ns_window(widget):
    """The NSWindow behind a mapped Tk toplevel.

    Tk's window id on Aqua is a MacDrawable whose second field is the NSView it draws
    into; for a toplevel that is the content view of its NSWindow.
    """
    try:
        widget.update_idletasks()
        windows = send(nsapp(), "windows")
        all_windows = [send(windows, "objectAtIndex:", i, argtypes=[c_ulong])
                       for i in range(send(windows, "count", restype=c_ulong) or 0)]
        # Tk titles the NSWindow when it is first mapped, borderless ones included, and
        # our titles are unique.
        title = widget.wm_title()
        named = [w for w in all_windows if pystring(send(w, "title")) == title]
        if len(named) == 1:
            return named[0]
        drawable = int(widget.winfo_id())
        view = c_void_p.from_address(drawable + ctypes.sizeof(c_void_p)).value
        for w in all_windows:
            if view and send(w, "contentView") == view:
                return w
    except Exception:
        log.exception("NSWindow lookup failed")
    return None


def dark_appearance():
    """Every window of ours is dark (the captions, the transcript, the setup window), so
    the whole app is pinned to Dark Aqua: title bars, native buttons, scrollers and menus
    then match in light and dark system mode alike."""
    try:
        dark = send(cls("NSAppearance"), "appearanceNamed:", nsstring("NSAppearanceNameDarkAqua"))
        send(nsapp(), "setAppearance:", dark, restype=None)
    except Exception:
        log.exception("dark appearance failed")


def set_window_icon(widget):
    pass  # the Dock shows the bundle's .icns


def style_window(widget):
    pass  # see dark_appearance()


def new_root(tk):
    root = tk.Tk()
    root.withdraw()  # shown once it is styled; an Aqua toplevel flashes otherwise
    dark_appearance()
    if not config.FROZEN:  # the Dock would show Python's rocket
        try:
            root.iconphoto(True, tk.PhotoImage(file=config.ICON_PATH))
        except tk.TclError:
            pass
    return root


def borderless(root):
    """A borderless *panel* that does not activate the app when clicked: unlike an ordinary
    (overrideredirect) window, macOS lets such a panel float over other apps' full-screen
    Spaces, so the captions stay on top of a full-screen video. Must run before mapping."""
    try:
        root.tk.call("::tk::unsupported::MacWindowStyle", "style", root._w, "utility",
                     "noTitleBar nonActivating canJoinAllSpaces doesNotHide doesNotCycle")
    except Exception:
        log.exception("utility panel style failed, falling back to overrideredirect")
        root.overrideredirect(True)


def make_app_window(root):
    """Float the caption window over every Space and over full-screen apps, and keep it
    out of ⌘` cycling. Tk already made it borderless (overrideredirect) and topmost."""
    root.attributes("-topmost", True)  # mapping a borderless window clears Tk's topmost flag
    win = ns_window(root)
    if not win:
        return None
    try:
        # CanJoinAllSpaces | Stationary | IgnoresCycle | FullScreenAuxiliary
        send(win, "setCollectionBehavior:", 1 | 16 | 64 | 256, argtypes=[c_ulong], restype=None)
        send(win, "setLevel:", 25, argtypes=[c_long], restype=None)  # NSStatusWindowLevel
        send(win, "setHasShadow:", True, argtypes=[c_bool], restype=None)
        if send(win, "respondsToSelector:", _objc.sel_registerName(b"setHidesOnDeactivate:"), restype=c_bool):
            send(win, "setHidesOnDeactivate:", False, argtypes=[c_bool], restype=None)  # panels hide by default
        if os.environ.get("LT_ACCESSORY") == "1":  # experiment: no Dock icon / menu bar
            send(nsapp(), "setActivationPolicy:", 1, argtypes=[c_long], restype=c_bool)
    except Exception:
        log.exception("window level setup failed")
    return win


def window_info(win):
    """Diagnostics for the CI log."""
    if not win:
        return {}
    return {
        "class": (_objc.object_getClassName(win) or b"").decode(),
        "hidesOnDeactivate": send(win, "hidesOnDeactivate", restype=c_bool),
        "activationPolicy": send(nsapp(), "activationPolicy", restype=c_long),
        "level": send(win, "level", restype=c_long),
        "collectionBehavior": send(win, "collectionBehavior", restype=c_ulong),
        "styleMask": send(win, "styleMask", restype=c_ulong),
        "canBecomeKey": send(win, "canBecomeKeyWindow", restype=c_bool),
        "visible": send(win, "isVisible", restype=c_bool),
        "backingScale": send(win, "backingScaleFactor", restype=c_double),
    }


def minimize(root, win):
    """Borderless windows cannot be minimized into the Dock: hide the app instead (like ⌘H);
    clicking its Dock icon shows it again."""
    hide_app()


def is_minimized(win):
    return False


def _screens():
    """[(frame, visible_frame)] as Tk-style (x, y, w, h): origin at the top left of the
    main screen, y growing downwards (Cocoa's origin is bottom left)."""
    screens = send(cls("NSScreen"), "screens")
    out = []
    n = send(screens, "count", restype=c_ulong) or 0
    for i in range(n):
        s = send(screens, "objectAtIndex:", i, argtypes=[c_ulong])
        out.append((send(s, "frame", restype=CGRect), send(s, "visibleFrame", restype=CGRect)))
    if not out:
        return []
    top = out[0][0].h  # screens[0] is the one with the menu bar, at the origin

    def tk(r):
        return round(r.x), round(top - r.y - r.h), round(r.w), round(r.h)
    return [(tk(f), tk(v)) for f, v in out]


def window_rect(root, win):
    """(x, y, w, h) from Cocoa: Tk's own numbers lag behind a drag until it catches up."""
    if win:
        try:
            screens = send(cls("NSScreen"), "screens")
            main = send(screens, "objectAtIndex:", 0, argtypes=[c_ulong])
            top = send(main, "frame", restype=CGRect).h
            r = send(win, "frame", restype=CGRect)
            return round(r.x), round(top - r.y - r.h), round(r.w), round(r.h)
        except Exception:
            log.exception("frame lookup failed")
    return root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height()


def on_screen(x, y, w, h):
    try:
        screens = _screens()
    except Exception:
        return True
    for (sx, sy, sw, sh), _ in screens:
        if x + w - 80 >= sx and x + 80 <= sx + sw and y >= sy - 1 and y + 40 <= sy + sh:
            return True
    return False


def work_area(root):
    """The main screen minus menu bar and Dock, and the gap kept above the Dock."""
    try:
        (_, (x, y, w, h)) = _screens()[0]
        return x, y, w, y + h, 24
    except Exception:
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight(), 90


def activate():
    send(nsapp(), "activateIgnoringOtherApps:", True, argtypes=[c_bool], restype=None)


def hide_app():
    send(nsapp(), "hide:", None, restype=None)


def about_panel():
    """The standard Cocoa About panel: name, version and icon come from Info.plist."""
    activate()
    send(nsapp(), "orderFrontStandardAboutPanelWithOptions:", send(cls("NSDictionary"), "dictionary"),
         restype=None)


# Tk fills the application menu with fixed English items; give them their Chinese
# titles. They are matched by action so a future Tk with other wording still works.
_APP_MENU_TITLES = {
    "preferences:": "设置…",
    "hide:": f"隐藏{config.APP_NAME}",
    "hideOtherApplications:": "隐藏其他",
    "unhideAllApplications:": "全部显示",
    "terminate:": f"退出{config.APP_NAME}",
}


def localize_app_menu():
    try:
        main = send(nsapp(), "mainMenu")
        item = send(main, "itemAtIndex:", 0, argtypes=[c_long])
        menu = send(item, "submenu")
        for i in range(send(menu, "numberOfItems", restype=c_long) or 0):
            it = send(menu, "itemAtIndex:", i, argtypes=[c_long])
            action = send(it, "action")
            name = _objc.sel_getName(action).decode() if action else ""
            title = _APP_MENU_TITLES.get(name)
            if title is None and send(it, "hasSubmenu", restype=c_bool) and pystring(send(it, "title")) == "Services":
                title = "服务"
                send(send(it, "submenu"), "setTitle:", nsstring(title), restype=None)
            if title:
                send(it, "setTitle:", nsstring(title), restype=None)
    except Exception:
        log.exception("menu localisation failed")


def menu_titles():
    """{menu: [item titles]} of the menu bar as Cocoa shows it (CI checks the translations)."""
    out = {}
    main = send(nsapp(), "mainMenu")
    for i in range(send(main, "numberOfItems", restype=c_long) or 0):
        item = send(main, "itemAtIndex:", i, argtypes=[c_long])
        sub = send(item, "submenu")
        titles = []
        for j in range(send(sub, "numberOfItems", restype=c_long) or 0):
            it = send(sub, "itemAtIndex:", j, argtypes=[c_long])
            if not send(it, "isSeparatorItem", restype=c_bool):
                titles.append(pystring(send(it, "title")) + ("" if not send(it, "isHidden", restype=c_bool) else " (hidden)"))
        out[pystring(send(item, "title")) or pystring(send(sub, "title"))] = titles
    return out


def snapshot(widget, path):
    """Render a Tk toplevel into a PNG without screen-recording permission (used by CI)."""
    win = ns_window(widget)
    view = send(win, "contentView")
    bounds = send(view, "bounds", restype=CGRect)
    rep = send(view, "bitmapImageRepForCachingDisplayInRect:", bounds, argtypes=[CGRect])
    send(view, "cacheDisplayInRect:toBitmapImageRep:", bounds, rep, argtypes=[CGRect, c_void_p], restype=None)
    data = send(rep, "representationUsingType:properties:", 4, send(cls("NSDictionary"), "dictionary"),
                argtypes=[c_ulong, c_void_p])  # NSBitmapImageFileTypePNG
    return bool(send(data, "writeToFile:atomically:", nsstring(os.path.abspath(path)), True,
                     argtypes=[c_void_p, c_bool], restype=c_bool))


def window_number(widget):
    return send(ns_window(widget), "windowNumber", restype=c_long)
