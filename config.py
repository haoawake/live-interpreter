"""Paths, model registry and persisted user settings."""
import json
import os
import sys

# Packaged (PyInstaller) the app folder is the one holding 同声传译.exe; from
# source it is this file's folder. Either way models/, bin/ and config.json live there.
FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", APP_DIR)  # read-only files shipped inside the exe
ICON_PATH = os.path.join(BUNDLE_DIR, "assets", "icon.ico")
APP_NAME = "同声传译"
EXE_NAME = APP_NAME + ".exe"
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

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
LLAMA_SERVER = LLAMA_DIR + "/llama-server.exe"
GLOSSARY_PATH = "glossary.txt"
TRANSCRIPT_DIR = "transcripts"
LOG_DIR = "logs"

DEFAULTS = {
    "source": "loopback:default",  # see audio.list_sources()
    "asr": "160",
    "asr_threads": 6,
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


def asr_installed():
    return [k for k, m in ASR_MODELS.items()
            if os.path.exists(os.path.join(APP_DIR, m["dir"], "encoder.int8.onnx"))]


def mt_installed():
    return [k for k, m in MT_MODELS.items() if os.path.exists(os.path.join(APP_DIR, m["file"]))]
