"""English -> Chinese translation with Tencent HY-MT1.5 served by llama.cpp."""
import http.client
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time

import config

if sys.platform == "darwin":
    import sys_mac as plat
else:
    import sys_win as plat

log = logging.getLogger(__name__)

_PLAIN = "将以下文本翻译为中文，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}"
_TERMS = "参考下面的翻译：\n{terms}\n\n将以下文本翻译为中文，注意只需要输出翻译后的结果，不要额外解释：\n{text}"
_COMPLETE = re.compile(r"[.!?…][\"')\]]*$")
_ELLIPSIS = re.compile(r"\s*(?:…|\.{3}|。{3})+$")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    def __init__(self, model_file, gpu=True):
        self.model_file = model_file
        self.gpu = gpu
        self.port = None
        self.proc = None
        self._job = None
        self._log = None

    def start(self, timeout=90):
        os.makedirs(config.LOG_DIR, exist_ok=True)
        self._log = open(os.path.join(config.LOG_DIR, "llama-server.log"), "wb")
        self.port = _free_port()
        exe = os.path.normpath(os.path.join(config.APP_DIR, config.LLAMA_SERVER))
        if config.MAC:  # Apple silicon: Metal on the GPU, or the performance cores
            threads = str(min(4, config.CPU_CORES) if self.gpu else config.CPU_CORES)
        else:
            threads = "4" if self.gpu else "8"
        args = [exe, "-m", self.model_file, "--host", "127.0.0.1", "--port", str(self.port),
                "-c", "4096", "-np", "2", "-fa", "auto", "--no-ui",
                "-ngl", "99" if self.gpu else "0", "-t", threads]
        log.info("starting %s", " ".join(args))
        self.proc = subprocess.Popen(args, cwd=config.APP_DIR, stdout=self._log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=plat.NO_WINDOW)
        try:
            self._job = plat.kill_with_parent(self.proc)
        except Exception:
            log.exception("kill-with-parent setup failed")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server 退出（代码 {self.proc.returncode}），详见 logs/llama-server.log")
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
                c.request("GET", "/health")
                if c.getresponse().status == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.2)
        self.stop()
        raise RuntimeError("llama-server 启动超时")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if isinstance(self._job, subprocess.Popen):  # the macOS watchdog quits with llama-server
            try:
                self._job.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._job.kill()
        if self._log:
            self._log.close()


def load_glossary(path=config.GLOSSARY_PATH):
    """Lines of `english = 中文`; '#' starts a comment."""
    terms = []
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    src, dst = (s.strip() for s in line.split("=", 1))
                    if src and dst:
                        # "self-attention" should also match the ASR's "self attention"
                        body = re.sub(r"\\[- ]", r"[-\\s]?", re.escape(src))
                        pattern = re.compile(rf"(?<!\w){body}(?:s|es)?(?!\w)", re.I)
                        terms.append((pattern, src, dst))
    except OSError:
        pass
    return terms


class Translator:
    def __init__(self, port, glossary_path=config.GLOSSARY_PATH):
        self.port = port
        self._glossary_path = glossary_path
        self._glossary_mtime = None
        self._glossary_checked = 0.0
        self.glossary = []

    def _refresh_glossary(self):
        """Pick up edits to glossary.txt without a restart."""
        now = time.monotonic()
        if now - self._glossary_checked < 1.0:
            return
        self._glossary_checked = now
        try:
            mtime = os.stat(self._glossary_path).st_mtime
        except OSError:
            mtime = None
        if mtime != self._glossary_mtime:
            self._glossary_mtime = mtime
            self.glossary = load_glossary(self._glossary_path)

    def prompt(self, text):
        self._refresh_glossary()
        hits = [f"{src} 翻译成 {dst}" for pat, src, dst in self.glossary if pat.search(text)]
        if hits:
            return _TERMS.format(terms="\n".join(hits), text=text)
        return _PLAIN.format(text=text)

    def stream(self, text, cancelled=lambda: False, final=False):
        """Yield the growing translation; stop early (dropping the request) once cancelled().

        Text without a closing mark gets a "..." so the model doesn't invent an
        ending for an unfinished sentence. For a finished (`final`) segment the
        "……" that this makes the model echo is trimmed off again.
        """
        open_ended = not _COMPLETE.search(text)
        if open_ended:
            text += "..."
        trim = final and open_ended
        body = {
            "messages": [{"role": "user", "content": self.prompt(text)}],
            "stream": True,
            "temperature": 0,  # greedy keeps re-translations of a growing sentence stable
            "top_k": 1,
            "repeat_penalty": 1.05,
            "max_tokens": min(512, 24 + 4 * len(text.split())),
            "cache_prompt": True,
        }
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("POST", "/v1/chat/completions", json.dumps(body).encode(),
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                raise RuntimeError(f"翻译请求失败 HTTP {resp.status}: {resp.read()[:200]!r}")
            acc = ""
            while True:
                line = resp.readline()
                if not line or cancelled():
                    return
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    return
                piece = json.loads(data)["choices"][0].get("delta", {}).get("content")
                if piece:
                    acc += piece
                    yield _ELLIPSIS.sub("", acc).rstrip() if trim else acc.strip()
        finally:
            conn.close()
