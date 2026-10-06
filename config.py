"""Paths, model registry and persisted user settings."""
import json
import os
import shutil
import subprocess
import sys

VERSION = "1.1.0"
MAC = sys.platform == "darwin"
APP_NAME = "同声传译"
BUNDLE_ID = "com.haoawake.live-interpreter"
FROZEN = getattr(sys, "frozen", False)
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

if MAC:
    # Never inside the .app: it is signed (writing breaks the seal) and, launched from
    # 下载, App Translocation runs it from a read-only copy. LT_DATA_DIR is for tests.
    APP_DIR = os.environ.get("LT_DATA_DIR") or os.path.join(
        os.path.expanduser("~/Library/Application Support"), APP_NAME)
    os.makedirs(APP_DIR, exist_ok=True)
else:
    # Packaged (PyInstaller) the app folder is the one holding 同声传译.exe; from
    # source it is this file's folder. Either way models/, bin/ and config.json live there.
    APP_DIR = os.path.dirname(sys.executable) if FROZEN else SOURCE_DIR
BUNDLE_DIR = getattr(sys, "_MEIPASS", SOURCE_DIR)  # read-only files shipped inside the exe / .app
ICON_PATH = os.path.join(BUNDLE_DIR, "assets", "icon.png" if MAC else "icon.ico")
EXE_NAME = APP_NAME + ".exe"
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
# macOS: the Core Audio helper sits next to the executable in Contents/MacOS (all
# Mach-O code must live there to pass codesign); from source it is built by build.py.
MAC_HELPER = (os.path.join(os.path.dirname(sys.executable), "lt-audio") if FROZEN
              else os.path.join(SOURCE_DIR, "macos", "build", "lt-audio"))

# Every path below is relative to APP_DIR, and the app chdirs there on startup.
# sherpa-onnx opens files through the ANSI code page on Windows, so an absolute
# path containing Chinese characters (like this folder's) would fail to load.
_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
_HYMT = "https://huggingface.co/tencent/HY-MT1.5-1.8B-GGUF/resolve/main/"

ASR_MODELS = {
    "160": {
        "label": "极速（160ms 分块）",
        "chunk": 0.16,
        "mb": 442,
        "dir": "models/asr/sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25",
        "url": _SHERPA + "sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25.tar.bz2",
    },
    "560": {
        "label": "精准（560ms 分块）",
        "chunk": 0.56,
        "mb": 442,
        "dir": "models/asr/sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25",
        "url": _SHERPA + "sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25.tar.bz2",
    },
}

MT_MODELS = {
    "q4": {
        "label": "HY-MT1.5-1.8B Q4_K_M（更快）",
        "mb": 1081,
        "file": "models/mt/HY-MT1.5-1.8B-Q4_K_M.gguf",
        "url": _HYMT + "HY-MT1.5-1.8B-Q4_K_M.gguf",
    },
    "q6": {
        "label": "HY-MT1.5-1.8B Q6_K（略准）",
        "mb": 1406,
        "file": "models/mt/HY-MT1.5-1.8B-Q6_K.gguf",
        "url": _HYMT + "HY-MT1.5-1.8B-Q6_K.gguf",
    },
}

LLAMA_BUILD = "b11269"
LLAMA_DIR = "bin/llama"
LLAMA_SERVER = LLAMA_DIR + ("/llama-server" if MAC else "/llama-server.exe")
GPU_NAME = "Metal" if MAC else "GPU"  # shown as 翻译就绪 · GPU
GLOSSARY_PATH = "glossary.txt"
TRANSCRIPT_DIR = "transcripts"
LOG_DIR = "logs"


def _mac_cores():
    """Performance cores on Apple silicon (the efficiency cores only slow a decode down)."""
    try:
        out = subprocess.run(["sysctl", "-n", "hw.perflevel0.physicalcpu"], capture_output=True,
                             text=True, timeout=5).stdout
        return max(1, int(out.strip()))
    except (OSError, ValueError, subprocess.SubprocessError):
        return max(1, (os.cpu_count() or 4) // 2)


CPU_CORES = _mac_cores() if MAC else None

DEFAULTS = {
    "source": "loopback:default",  # see audio.list_sources()
    "asr": "160",
    "asr_threads": min(4, CPU_CORES) if MAC else 6,
    "mt": "q4",
    "mt_gpu": True,
    "translate": True,
    "show_en": True,
    "font_size": 16,
    "opacity": 0.9,
    "lines": -1,  # finished sentences shown above the live one; -1 = as many as fit
    "save_transcript": True,
    "geometry": "",
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def ensure_glossary():
    """macOS keeps its data outside the app, so the default glossary is copied out once."""
    path = os.path.join(APP_DIR, GLOSSARY_PATH)
    src = os.path.join(BUNDLE_DIR, GLOSSARY_PATH)
    if MAC and not os.path.exists(path) and os.path.exists(src):
        try:
            shutil.copyfile(src, path)
        except OSError:
            pass


def asr_installed():
    return [k for k, m in ASR_MODELS.items()
            if os.path.exists(os.path.join(APP_DIR, m["dir"], "encoder.int8.onnx"))]


def mt_installed():
    return [k for k, m in MT_MODELS.items() if os.path.exists(os.path.join(APP_DIR, m["file"]))]
