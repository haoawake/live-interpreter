"""Pipeline: capture -> streaming ASR -> segmenter -> translation, all off the UI thread."""
import logging
import os
import queue
import threading
import time
import wave
from datetime import datetime

import numpy as np

import config
from asr import Segmenter, StreamingASR, ends_sentence
from audio import Capture
from mt import LlamaServer, Translator

log = logging.getLogger(__name__)


class FileSource:
    """Plays a 16-bit WAV into the pipeline in real time (testing without a sound card)."""

    def __init__(self, path, out):
        self.path, self.out = path, out
        self.device_name = os.path.basename(path)
        self.error = None
        self.finished = threading.Event()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, name="file-source", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        with wave.open(self.path) as w:
            sr, ch = w.getframerate(), w.getnchannels()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        step, t0 = sr // 100, time.perf_counter()
        for i in range(0, len(a), step):
            if self._stop.is_set():
                break
            self.out.put((sr, a[i:i + step]))
            delay = t0 + (i + step) / sr - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        self.finished.set()


class Engine:
    """Owns the pipeline threads and reports to the UI through `events`:

    ("status", key, text)              key is "asr" or "mt"
    ("stats", dict)                    once a second: asr_ms, mt_ms, device, audio_error
    ("level", peak)                    ~10x a second, input loudness 0..1
    ("live", epoch, en)                the unfinished sentence being spoken
    ("live_zh", epoch, src, zh, done)  its tentative translation
    ("final", seg_id, epoch, en)       a finished sentence; `epoch` numbers the live text after it
    ("final_zh", seg_id, zh, done, ms)
    ("error", text)
    """

    def __init__(self, cfg, events):
        self.cfg = cfg
        self.events = events
        self.audio_q = queue.Queue()
        self.capture = None
        self.asr = None
        self._asr_next = None
        self.server = None
        self.translator = None
        self.paused = False
        self.mt_ms = None
        self._stop = threading.Event()
        self._mt_lock = threading.Lock()
        self._src_lock = threading.Lock()
        self._final_q = queue.Queue()
        self._live_req = (0, "")
        self._live_evt = threading.Event()
        self._epoch = 0
        self._seg_id = 0
        self._pending_en = {}
        self._transcript = None
        self._transcript_lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------

    def start(self, source=None):
        self.load_asr()
        self.restart_mt()
        for target in (self._asr_loop, self._final_worker, self._live_worker):
            threading.Thread(target=target, name=target.__name__, daemon=True).start()
        if source is not False:
            self.set_source(source or self.cfg["source"])

    def shutdown(self):
        self._stop.set()
        if self.capture:
            self.capture.stop()
        if self.server:
            self.server.stop()
        with self._transcript_lock:
            if self._transcript:
                self._transcript.close()
                self._transcript = None

    def set_source(self, source):
        def run():
            with self._src_lock:
                if self.capture:
                    self.capture.stop()
                if source.startswith("file:"):
                    self.capture = FileSource(source[5:], self.audio_q).start()
                else:
                    self.cfg["source"] = source
                    self.capture = Capture(source, self.audio_q).start()
        threading.Thread(target=run, daemon=True).start()

    def set_paused(self, paused):
        self.paused = paused

    def load_asr(self):
        def run():
            key = self.cfg["asr"]
            m = config.ASR_MODELS.get(key) or config.ASR_MODELS["160"]
            self.events.put(("status", "asr", "正在加载识别模型…"))
            try:
                asr = StreamingASR(m["dir"], m["chunk"], self.cfg["asr_threads"])
            except Exception as e:
                log.exception("ASR load failed")
                self.events.put(("status", "asr", "识别模型加载失败"))
                self.events.put(("error", f"识别模型加载失败：{e}"))
                return
            if self.asr is None:
                self.asr = asr
            else:
                self._asr_next = asr
            self.events.put(("status", "asr", "识别就绪"))
        threading.Thread(target=run, daemon=True).start()

    def restart_mt(self):
        def run():
            with self._mt_lock:
                self.translator = None
                if self.server:
                    self.server.stop()
                    self.server = None
                if not self.cfg["translate"]:
                    self.events.put(("status", "mt", "翻译已关闭"))
                    return
                m = config.MT_MODELS.get(self.cfg["mt"]) or config.MT_MODELS["q4"]
                self.events.put(("status", "mt", "正在启动翻译引擎…"))
                try:
                    try:
                        server = LlamaServer(m["file"], gpu=self.cfg["mt_gpu"]).start()
                    except Exception:
                        if not self.cfg["mt_gpu"]:
                            raise
                        log.exception("GPU start failed, falling back to CPU")
                        server = LlamaServer(m["file"], gpu=False).start()
                    translator = Translator(server.port)
                    for _ in translator.stream("Hello, everyone."):  # warm-up (CUDA graphs etc.)
                        pass
                except Exception as e:
                    log.exception("MT start failed")
                    self.events.put(("status", "mt", "翻译引擎启动失败"))
                    self.events.put(("error", f"翻译引擎启动失败：{e}"))
                    return
                self.server, self.translator = server, translator
                self.events.put(("status", "mt", f"翻译就绪 · {config.GPU_NAME if server.gpu else 'CPU'}"))
        threading.Thread(target=run, daemon=True).start()

    # ---- ASR loop --------------------------------------------------------

    # Seconds without new text before committing, on top of one ASR chunk (text
    # only ever arrives once per chunk, so a bigger chunk looks "quieter").
    PAUSE_COMMIT = 0.75  # the speaker stopped: finish the sentence
    PERIOD_COMMIT = 0.2  # the model already put a full stop (wait in case it was "3." of "3.5")

    def _next_audio(self, asr):
        try:
            block = [self.audio_q.get(timeout=0.25)]
        except queue.Empty:
            # WASAPI loopback delivers nothing while the PC is silent. Feed silence
            # so the model still emits the words it was holding back.
            if asr is None or asr.sample_rate is None:
                return []
            return [(asr.sample_rate, np.zeros(asr.sample_rate // 4, dtype=np.float32))]
        while True:  # drain everything queued so we never fall behind
            try:
                block.append(self.audio_q.get_nowait())
            except queue.Empty:
                return block

    def _asr_loop(self):
        seg = Segmenter()
        last_text, last_change, live = "", time.perf_counter(), ""
        peak, last_loud, last_level, last_stats = 0.0, 0.0, 0.0, 0.0
        was_paused = False
        while not self._stop.is_set():
            if self._asr_next is not None:
                if self.asr is not None:
                    self._flush(seg, self.asr.text())
                self.asr, self._asr_next = self._asr_next, None
                seg.reset()
                last_text, live = "", ""
            asr = self.asr
            block = self._next_audio(asr)
            now = time.perf_counter()

            if self.paused or asr is None:
                if self.paused and not was_paused and asr is not None:
                    self._flush(seg, asr.text())
                    asr.reset()
                    seg.reset()
                    last_text, live = "", ""
                was_paused = self.paused
                continue
            was_paused = False

            block_peak = max((float(np.abs(c).max()) for _, c in block if c.size), default=0.0)
            peak = max(peak, block_peak)
            if block_peak > 0.01:
                last_loud = now
            # Digital silence well after the last word (video paused, nothing playing):
            # skip the model instead of decoding zeros all day.
            idle = block_peak < 1e-4 and now - last_change > 3 and not seg.pending(last_text)
            if not idle:
                for sr, chunk in block:
                    if asr.sample_rate not in (None, sr):  # device switched: finish what we have
                        self._flush(seg, asr.text())
                        seg.reset()
                        last_text, live = "", ""
                    asr.accept(sr, chunk)
                asr.decode()

            text = asr.text()
            if text != last_text:
                last_change = now
            quiet = now - last_change
            pend = seg.pending(text)
            flush = bool(pend) and (quiet > asr.chunk + self.PAUSE_COMMIT
                                    or (ends_sentence(pend[-1]) and quiet > asr.chunk + self.PERIOD_COMMIT))
            if text != last_text or flush:
                segs, tail = seg.update(text, flush)
                for s in segs:
                    self._commit(s)
                if tail != live or segs:
                    self._set_live(tail)
                    live = tail
            last_text = text

            # The transcript grows for as long as the stream lives. Reset it only
            # in real silence with everything committed, so no word gets cut.
            if text and not seg.pending(text) and (
                    (quiet > 3 and now - last_loud > 2) or (len(text) > 4000 and quiet > 1.5)):
                asr.reset()
                seg.reset()
                last_text = ""

            if now - last_level > 0.1:
                self.events.put(("level", min(1.0, peak)))
                peak, last_level = 0.0, now
            if now - last_stats > 1.0:
                cap = self.capture
                self.events.put(("stats", {
                    "asr_ms": asr.decode_ms, "mt_ms": self.mt_ms,
                    "device": getattr(cap, "device_name", ""), "audio_error": getattr(cap, "error", None),
                    "audio_problem": getattr(cap, "problem", None), "audio_notice": getattr(cap, "notice", None)}))
                last_stats = now

    def _flush(self, seg, text):
        segs, _ = seg.update(text, flush=True)
        for s in segs:
            self._commit(s)
        self._set_live("")

    def _commit(self, text):
        self._seg_id += 1
        self._epoch += 1
        sid = self._seg_id
        self._pending_en[sid] = (datetime.now(), text)
        self.events.put(("final", sid, self._epoch, text))
        if self.translator is not None:
            self._final_q.put((sid, text, time.perf_counter()))
        else:
            self.events.put(("final_zh", sid, "", True, None))
            self._write_transcript(sid, "")

    def _set_live(self, tail):
        self.events.put(("live", self._epoch, tail))
        self._live_req = (self._epoch, tail)
        self._live_evt.set()

    # ---- translation workers ----------------------------------------------

    def _final_worker(self):
        while not self._stop.is_set():
            try:
                sid, text, t0 = self._final_q.get(timeout=0.5)
            except queue.Empty:
                continue
            zh = ""
            tr = self.translator
            if tr is not None:
                try:
                    for zh in tr.stream(text, cancelled=self._stop.is_set, final=True):
                        self.events.put(("final_zh", sid, zh, False, None))
                except Exception as e:
                    log.warning("translation failed: %s", e)
                    self.events.put(("error", f"翻译失败：{e}"))
            self.mt_ms = (time.perf_counter() - t0) * 1000
            self.events.put(("final_zh", sid, zh, True, self.mt_ms))
            self._write_transcript(sid, zh)

    def _live_worker(self):
        """Re-translates the unfinished sentence, always jumping to the newest text.

        A request is only abandoned when its sentence got committed (new epoch);
        otherwise it runs to completion so the screen isn't restarted mid-sentence.
        """
        done = None
        while not self._stop.is_set():
            if not self._live_evt.wait(0.5):
                continue
            self._live_evt.clear()
            req = self._live_req
            epoch, src = req
            tr = self.translator
            if req == done or tr is None:
                continue
            zh = ""
            try:
                if src:
                    stale = lambda: self._live_req[0] != epoch or self._stop.is_set()
                    for zh in tr.stream(src, cancelled=stale):
                        self.events.put(("live_zh", epoch, src, zh, False))
                if self._live_req[0] == epoch:
                    self.events.put(("live_zh", epoch, src, zh, True))
            except Exception as e:
                log.warning("live translation failed: %s", e)
            done = req

    def _write_transcript(self, sid, zh):
        ts, en = self._pending_en.pop(sid, (datetime.now(), ""))
        if not self.cfg["save_transcript"] or not en:
            return
        with self._transcript_lock:
            if self._transcript is None:
                os.makedirs(config.TRANSCRIPT_DIR, exist_ok=True)
                name = datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".txt"
                self._transcript = open(os.path.join(config.TRANSCRIPT_DIR, name), "a",
                                        encoding="utf-8", buffering=1)
            self._transcript.write(f"[{ts:%H:%M:%S}] {en}\n")
            if zh:
                self._transcript.write(f"           {zh}\n")


def main(argv=None):
    """Headless run for testing: python engine.py --wav lecture.wav

    The packaged app accepts the same arguments (同声传译.exe / Contents/MacOS/LiveInterpreter --wav ...).
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--wav", help="16-bit WAV played into the pipeline in real time")
    src.add_argument("--listen", type=float, metavar="SECONDS",
                     help="capture the saved audio source (system audio by default) for this long")
    ap.add_argument("--asr", default="160")
    ap.add_argument("--mt", default="q4")
    ap.add_argument("--cpu", action="store_true", help="translate on CPU")
    ap.add_argument("--no-mt", action="store_true")
    ap.add_argument("--all-events", action="store_true", help="also print live/streaming updates")
    ap.add_argument("--threads", type=int, help="ASR threads (default: the saved setting)")
    args = ap.parse_args(argv)
    wav = os.path.abspath(args.wav) if args.wav else None
    os.chdir(config.APP_DIR)
    config.ensure_glossary()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = config.load()
    cfg.update(asr=args.asr, mt=args.mt, mt_gpu=not args.cpu, translate=not args.no_mt, save_transcript=False)
    if args.threads:
        cfg["asr_threads"] = args.threads
    events = queue.Queue()
    eng = Engine(cfg, events)
    eng.start(source=False)
    t_ready = time.perf_counter()
    while eng.asr is None or (cfg["translate"] and eng.translator is None):
        try:
            e = events.get(timeout=0.1)
            if e[0] in ("status", "error"):
                print("  ", e)
        except queue.Empty:
            pass
        if time.perf_counter() - t_ready > 120:
            raise SystemExit("engine did not become ready")
    print(f"ready in {time.perf_counter() - t_ready:.1f}s")

    eng.set_source("file:" + wav if wav else cfg["source"])
    t0 = time.perf_counter()
    finals, translated = {}, set()
    finished_at = None
    last_stats = None
    while True:
        try:
            e = events.get(timeout=0.05)
        except queue.Empty:
            e = None
        t = time.perf_counter() - t0
        cap = eng.capture
        if finished_at is None and (isinstance(cap, FileSource) and cap.finished.is_set()
                                    or args.listen and t > args.listen):
            finished_at = t
            print(f"{t:6.2f}s  [audio ended]")
        # done once every committed sentence has its translation (the runner's VM is slow)
        if finished_at is not None and t - finished_at > 4 and (
                len(translated) >= len(finals) or not cfg["translate"] or t - finished_at > 180):
            break
        if e is None:
            continue
        kind = e[0]
        if kind == "final":
            finals[e[1]] = (t, e[3])
            print(f"{t:6.2f}s  FINAL#{e[1]}  {e[3]}")
        elif kind == "final_zh" and e[3]:
            translated.add(e[1])
            print(f"{t:6.2f}s  ZH#{e[1]} (+{e[4]:.0f}ms)  {e[2]}")
        elif kind == "live" and args.all_events:
            print(f"{t:6.2f}s  live  {e[2]}")
        elif kind == "live_zh" and e[4]:
            if args.all_events:
                print(f"{t:6.2f}s  live_zh  {e[3]}")
        elif kind in ("error", "status"):
            print(f"{t:6.2f}s  {e}")
        elif kind == "stats":
            key = tuple(e[1].get(k) for k in ("device", "audio_error", "audio_problem"))
            if args.all_events or (args.listen and key != last_stats):
                print(f"{t:6.2f}s  stats {e[1]}")
            last_stats = key
    eng.shutdown()


if __name__ == "__main__":
    main()
