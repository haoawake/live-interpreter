"""Streaming English ASR (sherpa-onnx + NVIDIA Nemotron) and sentence segmentation."""
import re
import time

import sherpa_onnx

_SENT_END = re.compile(r"[.!?…][\"')\]]*$")
_ABBREV = {"mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.", "vs.", "e.g.", "i.e.",
           "etc.", "fig.", "approx.", "u.s.", "u.k."}


def ends_sentence(word):
    return (bool(_SENT_END.search(word)) and word.lower() not in _ABBREV
            and not re.fullmatch(r"[A-Z]\.", word))


class StreamingASR:
    """Cache-aware streaming transducer: feed audio, read the growing transcript.

    sherpa's endpoint detection is left off on purpose: resetting the stream at
    a pause drops the first word after it (its frames were already consumed
    while the model delayed emitting it) along with the sentence-final period.
    The caller segments the continuous text instead and only resets in silence.
    """

    def __init__(self, model_dir, chunk_seconds, num_threads=4):
        self.chunk = chunk_seconds  # text arrives in bursts of this size
        self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=f"{model_dir}/tokens.txt",
            encoder=f"{model_dir}/encoder.int8.onnx",
            decoder=f"{model_dir}/decoder.int8.onnx",
            joiner=f"{model_dir}/joiner.int8.onnx",
            num_threads=num_threads,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",  # greedy output is append-only, which the segmenter relies on
            provider="cpu",
        )
        self.sample_rate = None
        self.stream = self.rec.create_stream()
        self.decode_ms = 0.0  # moving average cost of one chunk

    def accept(self, sample_rate, samples):
        # sherpa-onnx resamples internally but aborts if the rate changes on a
        # stream, so a device switch (48k -> 44.1k) gets a fresh stream.
        if sample_rate != self.sample_rate:
            if self.sample_rate is not None:
                self.stream = self.rec.create_stream()
            self.sample_rate = sample_rate
        self.stream.accept_waveform(sample_rate, samples)

    def decode(self):
        n = 0
        while self.rec.is_ready(self.stream):
            t = time.perf_counter()
            self.rec.decode_stream(self.stream)
            ms = (time.perf_counter() - t) * 1000
            self.decode_ms = 0.9 * self.decode_ms + 0.1 * ms if self.decode_ms else ms
            n += 1
        return n

    def text(self):
        return self.rec.get_result(self.stream).strip()

    def reset(self):
        self.rec.reset(self.stream)


class Segmenter:
    """Cuts the append-only text of one utterance into sentences plus a live tail.

    A sentence is committed as soon as the next one starts, so translation
    never waits for the speaker to pause. Long run-on clauses are cut at a
    comma (or, failing that, forcibly) to keep latency bounded.
    """

    def __init__(self, soft_words=24, hard_words=40):
        self.soft, self.hard = soft_words, hard_words
        self.done = 0  # words of the current utterance already committed

    def reset(self):
        self.done = 0

    def pending(self, text):
        return text.split()[self.done:]

    def update(self, text, flush=False):
        """Return (committed_segments, live_tail) for the utterance's full text."""
        words = text.split()
        self.done = min(self.done, len(words))
        pend = words[self.done:]
        segs = []
        if flush:
            if pend:
                segs.append(" ".join(pend))
            self.done = len(words)
            return segs, ""
        start = 0
        for i in range(len(pend) - 1):  # the last word may still be growing
            if ends_sentence(pend[i]):
                segs.append(" ".join(pend[start:i + 1]))
                start = i + 1
        rest = pend[start:]
        if len(rest) > self.soft:
            cut = next((j for j in range(len(rest) - 4, 5, -1) if rest[j][-1] in ",;:"), None)
            if cut is None and len(rest) > self.hard:
                cut = len(rest) - 5
            if cut is not None:
                segs.append(" ".join(rest[:cut + 1]))
                start += cut + 1
        self.done += start
        return segs, " ".join(pend[start:])
