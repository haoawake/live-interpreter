"""Audio capture over WASAPI: system audio (loopback) or a microphone."""
import logging
import threading

import numpy as np
import pyaudiowpatch as pyaudio

log = logging.getLogger(__name__)

SYSTEM_DEFAULT = "loopback:default"
MIC_DEFAULT = "mic:default"
_LOOPBACK = " [Loopback]"


def list_sources():
    """[(source_id, label)] for the settings menu, system audio first."""
    p = pyaudio.PyAudio()
    try:
        host = p.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        loops, mics = [], []
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != host or d["maxInputChannels"] <= 0:
                continue
            if d.get("isLoopbackDevice"):
                loops.append((f"loopback:{d['name']}", "系统声音：" + d["name"].removesuffix(_LOOPBACK)))
            else:
                mics.append((f"mic:{d['name']}", "麦克风：" + d["name"]))
    finally:
        p.terminate()
    return [(SYSTEM_DEFAULT, "系统声音（跟随默认播放设备）"), *loops,
            (MIC_DEFAULT, "麦克风（系统默认）"), *mics]


def _default_name(kind):
    """Current Windows default device, named the way PortAudio lists it.

    PortAudio only enumerates devices once per PyAudio() instance, so ask
    Windows directly (soundcard queries the MMDevice API on every call).
    """
    import soundcard

    if kind == "loopback":
        return soundcard.default_speaker().name + _LOOPBACK
    return soundcard.default_microphone().name


def _find_device(p, source):
    kind, name = source.split(":", 1)
    host = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    if name == "default":
        try:
            name = _default_name(kind)
        except Exception:
            log.exception("default device lookup failed")
            if kind == "loopback":
                return p.get_default_wasapi_loopback()
            return p.get_device_info_by_index(host["defaultInputDevice"])
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if (d["hostApi"] == host["index"] and d["maxInputChannels"] > 0 and d["name"] == name
                and bool(d.get("isLoopbackDevice")) == (kind == "loopback")):
            return d
    raise RuntimeError(f"找不到音频设备：{name}")


class Capture:
    """Pushes (sample_rate, mono float32 ndarray) blocks of ~10ms onto `out`.

    Reopens the stream when the device disappears or, for the "default"
    sources, when Windows switches default device (e.g. headphones plugged in).
    """

    def __init__(self, source, out):
        self.source = source
        self.out = out
        self.device_name = ""
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self):
        kind, name = self.source.split(":", 1)
        while not self._stop.is_set():
            p = pyaudio.PyAudio()
            stream = None
            try:
                dev = _find_device(p, self.source)
                channels = dev["maxInputChannels"]
                rate = int(dev["defaultSampleRate"])

                def callback(data, frames, time_info, status):
                    a = np.frombuffer(data, dtype=np.float32)
                    if channels > 1:
                        a = a.reshape(-1, channels).mean(axis=1)
                    self.out.put((rate, a))
                    return None, pyaudio.paContinue

                stream = p.open(format=pyaudio.paFloat32, channels=channels, rate=rate, input=True,
                                input_device_index=dev["index"], frames_per_buffer=rate // 100,
                                stream_callback=callback)
                self.device_name = dev["name"].removesuffix(_LOOPBACK)
                self.error = None
                log.info("capturing %r (%d Hz, %d ch)", dev["name"], rate, channels)
                while not self._stop.wait(1.5):
                    if not stream.is_active():
                        log.warning("stream stopped, reopening")
                        break
                    if name == "default" and _default_name(kind) != dev["name"]:
                        log.info("default device changed, reopening")
                        break
            except Exception as e:
                self.error = str(e)
                log.exception("capture failed")
                self._stop.wait(2.0)
            finally:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                p.terminate()
