"""Platform-independent checks: python -m unittest discover -s tests -v"""
import io
import os
import queue
import struct
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from asr import Segmenter, ends_sentence  # noqa: E402


class SegmenterTest(unittest.TestCase):
    def test_commits_when_next_sentence_starts(self):
        seg = Segmenter()
        segs, tail = seg.update("Hello there. How are")
        self.assertEqual(segs, ["Hello there."])
        self.assertEqual(tail, "How are")
        segs, tail = seg.update("Hello there. How are you?", flush=True)
        self.assertEqual(segs, ["How are you?"])
        self.assertEqual(tail, "")

    def test_abbreviations_do_not_end_sentences(self):
        self.assertFalse(ends_sentence("Dr."))
        self.assertFalse(ends_sentence("U."))
        self.assertTrue(ends_sentence("done."))

    def test_long_clause_is_cut_at_a_comma(self):
        words = " ".join(f"w{i}," if i == 12 else f"w{i}" for i in range(30))
        segs, tail = Segmenter().update(words)
        self.assertEqual(len(segs), 1)
        self.assertTrue(segs[0].endswith("w12,"))
        self.assertTrue(tail.startswith("w13"))


class GlossaryTest(unittest.TestCase):
    def test_terms_match_plural_and_hyphen_variants(self):
        from mt import Translator, load_glossary
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "g.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# comment\nself-attention = 自注意力\ntransformer = Transformer\n")
            terms = load_glossary(path)
            self.assertEqual([t[1] for t in terms], ["self-attention", "transformer"])
            tr = Translator(0, glossary_path=path)
            prompt = tr.prompt("Self attention lets transformers look everywhere.")
            self.assertIn("self-attention 翻译成 自注意力", prompt)
            self.assertIn("transformer 翻译成 Transformer", prompt)
            self.assertNotIn("参考", tr.prompt("Nothing to see here."))


class MacAudioProtocolTest(unittest.TestCase):
    """The framing lt-audio writes (see macos/lt-audio.swift), parsed by audio_mac."""

    def test_packets_are_decoded(self):
        import numpy as np
        import audio_mac

        samples = np.array([0.0, 0.5, -0.25], dtype="<f4")
        data = (b"LTAU" + struct.pack("<II", 48000, 3) + samples.tobytes()) * 2 + b"LTAU\x00"
        out = queue.Queue()
        cap = audio_mac.Capture("mic:default", out)

        class P:
            stdout = io.BytesIO(data)
        cap._read_audio(P())
        got = [out.get_nowait(), out.get_nowait()]
        self.assertTrue(out.empty())
        for rate, a in got:
            self.assertEqual(rate, 48000)
            self.assertEqual(a.tolist(), samples.tolist())

    def test_status_lines_set_problem_and_device(self):
        import audio_mac

        cap = audio_mac.Capture("loopback:default", queue.Queue())

        class P:
            stderr = io.BytesIO(
                b'{"event":"error","code":"tap-denied","message":"\xe6\xb2\xa1\xe6\x9c\x89"}\n'
                b'not json\n')
        cap._read_status(P())
        self.assertEqual(cap.problem, "tap-denied")
        self.assertTrue(cap.error)
        P.stderr = io.BytesIO('{"event":"started","rate":48000,"device":"MacBook Pro 扬声器","source":"tap"}\n'
                              .encode())
        cap._read_status(P())
        self.assertIsNone(cap.problem)
        self.assertIsNone(cap.error)
        self.assertEqual(cap.device_name, "MacBook Pro 扬声器")


class LlamaArchiveTest(unittest.TestCase):
    def test_macos_archive_is_flattened(self):
        import setup_models

        with tempfile.TemporaryDirectory() as d:
            archive = os.path.join(d, "llama-b1-bin-macos-arm64.tar.gz")
            with tarfile.open(archive, "w:gz") as tar:
                for name, body in (("llama-b1/llama-server", b"#!/bin/sh\n"), ("llama-b1/libggml.0.1.dylib", b"x")):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(body), 0o755
                    tar.addfile(info, io.BytesIO(body))
                if os.name != "nt":
                    link = tarfile.TarInfo("llama-b1/libggml.dylib")
                    link.type, link.linkname = tarfile.SYMTYPE, "libggml.0.1.dylib"
                    tar.addfile(link)
            dest = os.path.join(d, "bin")
            setup_models._extract_flat(archive, dest)
            server = os.path.join(dest, "llama-server")
            self.assertTrue(os.path.isfile(server))
            if os.name != "nt":
                self.assertTrue(os.access(server, os.X_OK))
                self.assertEqual(os.readlink(os.path.join(dest, "libggml.dylib")), "libggml.0.1.dylib")


class BuildHelpersTest(unittest.TestCase):
    def test_macho_minimum_version(self):
        import build

        # mach_header_64 + one LC_BUILD_VERSION (platform macOS, minos 13.3, sdk 15.0)
        cmd = struct.pack("<IIIIII", 0x32, 24, 1, (13 << 16) | (3 << 8), 15 << 16, 0)
        head = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, len(cmd), 0, 0)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(head + cmd)
        try:
            self.assertEqual(build._macho_minos(f.name), (13, 3))
        finally:
            os.remove(f.name)

    def test_version_is_semver(self):
        self.assertRegex(config.VERSION, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
