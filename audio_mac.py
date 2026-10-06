"""macOS audio capture through the bundled lt-audio helper (macos/lt-audio.swift).

System audio comes from a Core Audio process tap (macOS 14.2+), which needs the
「录屏与系统录音」 permission; microphones need the 麦克风 permission. The helper
streams framed mono float32 to stdout and reports status as JSON lines on stderr.
"""
import json
import logging
import os
import platform
import struct
import subprocess
import threading

import numpy as np

import config

log = logging.getLogger(__name__)

SYSTEM_DEFAULT = "loopback:default"
MIC_DEFAULT = "mic:default"
_HEADER = struct.Struct("<4sII")

def _macos():
    try:
        return tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2])
    except ValueError:
        return (0, 0)


# Where the user grants each permission (系统设置 → 隐私与安全性 → ...). macOS 15 has its
# own anchor for 「仅系统录音」; on 14.x that list sits in the screen-recording pane.
_PANE = "x-apple.systempreferences:com.apple.preference.security?"
SETTINGS_URL = {
    "tap": _PANE + ("Privacy_AudioCapture" if _macos() >= (15, 0) else "Privacy_ScreenCapture"),
    "mic": _PANE + "Privacy_Microphone",
}
PROBLEMS = {  # helper error code -> (permission kind, message shown in the overlay)
    "tap-denied": ("tap", "没有录制系统声音的权限：请在「系统设置 › 隐私与安全性 › 录屏与系统录音」"
                          "中允许「同声传译」，然后重新打开本程序。"),
    "tap-silent": ("tap", "其他 App 正在播放声音，但同声传译什么也没听到：可能没有允许录制系统声音，"
                          "请到「系统设置 › 隐私与安全性 › 录屏与系统录音」中检查「同声传译」。"),
    "mic-denied": ("mic", "没有使用麦克风的权限：请在「系统设置 › 隐私与安全性 › 麦克风」中允许「同声传译」。"),
}
_ERRORS = {
    "unsupported": "macOS 14.2 以下无法采集系统声音，已改用麦克风",
    "no-output": "找不到声音输出设备",
    "no-device": "找不到麦克风",
}


def tap_supported():
    return _macos() >= (14, 2)


def helper():
    if not os.path.exists(config.MAC_HELPER):
        raise RuntimeError(f"缺少音频组件 {config.MAC_HELPER}")
    return config.MAC_HELPER


def devices():
    """The helper's view of the audio hardware and of our permissions (never prompts)."""
    out = subprocess.run([helper(), "devices"], capture_output=True, timeout=10, check=True).stdout
    return json.loads(out.decode("utf-8"))


def list_sources():
    """[(source_id, label)] for the settings menu, system audio first."""
    mics = []
    try:
        for d in devices().get("inputs", []):
            mics.append((f"mic:{d['uid']}", "麦克风：" + d["name"]))
    except Exception:
        log.exception("device listing failed")
    system = [(SYSTEM_DEFAULT, "系统声音（电脑正在播放的声音）")] if tap_supported() else []
    return [*system, (MIC_DEFAULT, "麦克风（系统默认）"), *mics]


def open_settings(kind):
    subprocess.Popen(["open", SETTINGS_URL[kind]])


class Capture:
    """Pushes (sample_rate, mono float32 ndarray) blocks onto `out`, like audio_win.Capture.

    The helper follows the default devices itself; this class restarts the helper
    when it exits, and exposes `problem` (a PROBLEMS key) for the permission banner.
    """

    def __init__(self, source, out):
        self.source = source
        self.out = out
        self.device_name = ""
        self.error = None
        self.problem = None
        self.notice = None
        self._stop = threading.Event()
        self._proc = None
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            p.terminate()
        self._thread.join(timeout=3)

    def _args(self):
        kind, name = self.source.split(":", 1)
        if kind == "loopback":
            if tap_supported():
                return [helper(), "tap"]
            self.notice = _ERRORS["unsupported"]
            return [helper(), "mic", "default"]
        return [helper(), "mic", name]

    def _run(self):
        while not self._stop.is_set():
            code = None
            try:
                args = self._args()
                log.info("starting %s", " ".join(args))
                self._proc = p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE)
                threading.Thread(target=self._read_status, args=(p,), name="capture-status", daemon=True).start()
                self._read_audio(p)
                code = p.wait()
                log.info("audio helper exited with %s", code)
                if code not in (0, 3) and not self._stop.is_set() and not self.error:
                    self.error = f"音频组件意外退出（代码 {code}）"
            except Exception as e:
                self.error = str(e)
                log.exception("capture failed")
            # A denied permission only changes when the user flips it in 系统设置, so poll gently.
            self._stop.wait(5.0 if code == 3 else 2.0)

    def _read_audio(self, p):
        read = p.stdout.read
        while True:
            head = read(_HEADER.size)
            if len(head) < _HEADER.size:
                return
            magic, rate, frames = _HEADER.unpack(head)
            if magic != b"LTAU":
                raise RuntimeError("音频组件输出格式错误")
            body = read(frames * 4)
            if len(body) < frames * 4:
                return
            if not self._stop.is_set():
                self.out.put((rate, np.frombuffer(body, dtype="<f4")))

    def _read_status(self, p):
        for line in p.stderr:
            try:
                ev = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                log.info("helper: %s", line.decode("utf-8", "replace").rstrip())
                continue
            log.info("helper: %s", ev)
            kind = ev.get("event")
            if kind == "started":
                self.device_name = ev.get("device", "")
                if ev.get("source") == "mic" and self.source.startswith("loopback:"):
                    self.device_name += "（麦克风）"
                self.error = self.problem = None
            elif kind == "permission":
                which = "系统声音录制" if ev.get("kind") == "tap" else "麦克风"
                self.error = f"请在弹出的对话框中允许「同声传译」使用{which}"
            elif kind == "silent":
                self.problem = "tap-silent"
            elif kind == "signal" and self.problem == "tap-silent":
                self.problem = None
            elif kind == "error":
                code = ev.get("code", "")
                if code in PROBLEMS:
                    self.problem = code  # the overlay shows the long explanation and a button
                self.error = _ERRORS.get(code) or ev.get("message") or "音频采集失败"
