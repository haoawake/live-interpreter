<p align="center"><img src="assets/icon.png" width="88" alt="Live Interpreter icon"></p>

# Live Interpreter (同声传译)

An offline, low-latency English-to-Chinese simultaneous interpreter for Windows. It listens to whatever the PC is playing (lectures, meetings, videos) or to a microphone, transcribes the speech with a cache-aware streaming recognizer, translates it with an on-device translation model, and shows bilingual live captions in an always-on-top overlay. Audio and text never leave the machine.

[中文说明](README.zh-CN.md) · [Download](https://github.com/haoawake/live-interpreter/releases/latest)

**Stack:** Python 3.12 · sherpa-onnx (ONNX Runtime) · NVIDIA Nemotron Speech Streaming 0.6B · llama.cpp (CUDA / Vulkan) · Tencent HY-MT1.5-1.8B · WASAPI · Tkinter · PyInstaller

![Overlay: committed sentences in white, the sentence in progress in blue](docs/overlay.png)

---

## Motivation

Cloud captioning adds a network round trip to every sentence, needs an account, and sends the audio of a lecture or a meeting to a third party. Live Interpreter runs the whole chain on a consumer laptop and is tuned for one job: showing a usable Chinese rendering of what is being said while it is still being said.

## Highlights

| | |
|---|---|
| **True streaming recognition** | A cache-aware FastConformer-RNNT model decodes 160 ms chunks incrementally and reuses encoder state instead of re-transcribing a sliding window. Words appear as they are spoken, with punctuation and casing. |
| **Translation that keeps up** | The unfinished sentence is re-translated after every new word and shown as a tentative line; when the sentence ends it is translated once more and committed. A committed sentence takes 60–240 ms on a laptop GPU. |
| **Fully offline** | Recognition runs on the CPU, translation on the local GPU (CUDA or Vulkan) or the CPU. No API keys and no network traffic after the first-run download. |
| **Any audio source** | WASAPI loopback captures anything the PC plays; microphones work as well. The default source follows Windows' default device when headphones are plugged in or removed. |
| **Glossary** | `English = 中文` pairs are injected through the model's terminology prompt, so course- or company-specific terms are translated consistently. Edits take effect without a restart. |
| **Self-contained Windows app** | A single-folder executable of about 90 MB. The first launch downloads the models (about 2 GB) from their publishers, with resume and a mirror option, and creates shortcuts. |

## Architecture

```
System audio (WASAPI loopback) / microphone
  │  10 ms blocks at the device rate, resampled inside sherpa-onnx
  ▼
Streaming ASR ── Nemotron Speech Streaming en 0.6B, int8, sherpa-onnx on the CPU
  │  append-only transcript, extended once per 160 ms chunk
  ▼
Segmenter ── commits a sentence when the next one starts, shortly after a full stop,
  │          or after a pause; over-long clauses are cut at a comma
  ├─ unfinished text ──► live lane  ──┐
  └─ committed sentence ► final lane ─┴─► llama-server, 2 slots ── HY-MT1.5-1.8B Q4_K_M
                                          │
                                          ▼
                 Overlay: committed sentences (white) + sentence in progress (blue)
```

Capture, recognition and both translation lanes run on worker threads and report through one event queue; the Tk main loop only renders.

### Design notes

- **The recognizer is never reset while someone is speaking.** Streaming transducers emit tokens with a delay. Resetting the stream at a one-second pause, which is what endpoint detection normally does, consumed the frames of the next word before that word was emitted. In a test lecture the words "Today" and "First" disappeared and every sentence-final period was lost. The stream now runs continuously, the segmenter tracks committed words itself, and the stream is reset only after three seconds of silence with nothing pending.
- **Segmentation.** This model emits a sentence's full stop about a second after its last word, usually together with the first word of the next sentence. A sentence is therefore committed when the next one begins; 0.2 s plus one chunk after a full stop when nothing follows (the wait also keeps "3." in "3.5" from ending a sentence); or after 0.75 s plus one chunk with no new text. Clauses longer than 24 words are cut at their last comma, and text beyond 40 words is cut regardless, which bounds the latency of run-on speech.
- **No invented endings.** Given a fragment such as "I'm not sure that", the translation model completes the thought ("我不太确定这一点。"). Appending "..." to unfinished text makes it translate only what was said ("我不太确定……"). The ellipsis is stripped again when the sentence is committed.
- **Stable re-translation.** Live requests use greedy decoding, so consecutive translations of a growing sentence share their prefix. The live lane abandons a request only when its sentence is committed, and the overlay swaps in complete translations instead of streaming each one token by token, which would restart the line on every word.
- **Terminology instead of context.** HY-MT's contextual-translation template was tried and rejected: the model translated the context sentence into the output as well. Sentences are translated independently, and the terminology template is used whenever a glossary entry matches, including plural and hyphen/space variants. With the default glossary, "transformer" comes out as "Transformer" rather than "变压器" (electrical transformer).
- **Silent loopback.** WASAPI loopback delivers no packets while nothing is playing. The engine injects silence so the recognizer can flush the words it is holding back, then stops decoding after three seconds of digital silence, so an idle session costs no CPU.
- **Windows specifics.** sherpa-onnx opens model files through the ANSI code page, so models are loaded by relative path after changing into the application folder, which may contain non-ASCII characters. llama-server is assigned to a Job Object and exits with the app, even on a crash. The overlay is a frameless window with its own hit-testing for resizing, and it reads the window rectangle from Win32 because Tk's reported geometry lags behind a resize.

## Performance

Measured on an AMD Ryzen 9 8940HX with an NVIDIA GeForce RTX 5060 Laptop GPU (8 GB) under Windows 11. During the translation measurements another application kept the GPU at about 90 % utilisation.

| Metric | Result |
|---|---|
| Recognition, decode time per 160 ms chunk (CPU) | 52 ms at 8 threads · 61 ms at 4 threads · 102 ms at 2 threads |
| Recognition, real-time factor | 0.36 (160 ms chunks, 8 threads) · 0.13 (560 ms chunks, 4 threads) |
| Translation, time to first token (Q4_K_M) | 35–85 ms |
| Translation, committed sentence end to end | 60–240 ms |
| Model load | 2–3 s |
| Memory | recognizer 0.8 GB RAM · translation 1.5 GB RAM and 1.5 GB VRAM |

Recognition accuracy from the model card, as average WER over eight Open ASR Leaderboard test sets: 7.67 % with 160 ms chunks, 7.07 % with 560 ms chunks, 6.93 % with 1120 ms chunks.

## Requirements

- Windows 10 or 11, 64-bit
- 8 GB of RAM; 16 GB recommended
- A GPU is recommended. NVIDIA cards with driver R580 or newer use the CUDA build of llama.cpp, and AMD and Intel GPUs use the Vulkan build. Without a usable GPU, translation falls back to the CPU at roughly 0.5–1 s per sentence.
- About 2.5 GB of disk space, and an internet connection for the first launch

## Installation

### Release build (recommended)

1. Download `live-interpreter-v1.0.0-windows-x64.zip` from [Releases](https://github.com/haoawake/live-interpreter/releases/latest) and extract it anywhere; paths with non-ASCII characters are fine.
2. Run `同声传译.exe`. The first launch lists the components it needs, downloads them with resume support, and can create desktop and Start-menu shortcuts.
3. The executable is not code-signed. If Windows SmartScreen reports "Windows protected your PC", choose **More info → Run anyway**.

### From source

```bat
git clone https://github.com/haoawake/live-interpreter.git
cd live-interpreter
build.bat
```

`build.bat` creates a Python 3.12 virtual environment, installs the dependencies, downloads the models and produces `同声传译.exe`. To run without packaging, use `.venv\Scripts\pythonw.exe app.py`.

## Usage

| Action | Effect |
|---|---|
| Drag an edge or a corner | Resize the window; a taller window shows more history |
| Drag anywhere else | Move the window |
| `A−` / `A+`, or Ctrl + mouse wheel | Change the font size |
| `英` | Show or hide the English source |
| `暂停` | Pause recognition |
| `记录` | Open the session's bilingual transcript (copyable) |
| Right-click, or `设置` | Audio source, recognition mode, translation model and GPU use, display options, transcript saving, glossary |

White lines are committed translations. The blue line is the sentence in progress and may change until the sentence ends. Transcripts are saved to `transcripts\` (this can be turned off), and settings persist in `config.json`.

**Recognition modes.** *Fast* uses 160 ms chunks. *Accurate* uses 560 ms chunks, which lowers the error rate at the cost of about 0.4 s of extra delay. The Q6_K translation model is available as a slightly more accurate alternative to the default Q4_K_M.

**Glossary.** `glossary.txt` holds one entry per line:

```
transformer = Transformer
self-attention = 自注意力
```

## Project layout

| Path | Purpose |
|---|---|
| `app.py` | Overlay window, settings menu, transcript window, first-run downloader |
| `engine.py` | Pipeline threads (capture → recognition → segmentation → translation) and a headless test harness |
| `asr.py` | sherpa-onnx streaming recognizer and the sentence segmenter |
| `mt.py` | llama-server process management, prompts, glossary matching, streaming client |
| `audio.py` | WASAPI capture that follows the default device |
| `config.py` | Model registry, paths, default settings |
| `setup_models.py` | Resumable component downloads with mirror fallback, and shortcut creation |
| `build.py`, `build.bat` | PyInstaller packaging |

## Development

Run the pipeline headless on a 16-bit WAV file at real-time speed and print a timeline of every committed sentence and translation:

```bat
.venv\Scripts\python.exe engine.py --wav lecture.wav
.venv\Scripts\python.exe engine.py --wav lecture.wav --all-events
```

`--all-events` also prints the live updates. After changing the code, close the app and run `build.bat` to rebuild the executable.

## Known limitations

- English to Chinese only; the recognizer is an English model.
- The live line is re-translated as the sentence grows, so its wording can change until the sentence is committed.
- The mirror option covers the HuggingFace download only; the llama.cpp and recognizer archives are fetched from GitHub Releases.
- Translation slows down while another application saturates the GPU. Translation can be moved to the CPU from the menu.

## License

The source code is released under the [MIT License](LICENSE). The release package bundles third-party components under their own licenses, listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The models are not distributed with this repository or its releases. They are downloaded from their publishers on first launch and are governed by the NVIDIA Open Model License (Nemotron Speech Streaming) and the Tencent HY Community License Agreement (HY-MT1.5). The latter does not apply in the European Union, the United Kingdom or South Korea.

## Acknowledgements

[NVIDIA Nemotron Speech Streaming](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b) · [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) · [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) · [Tencent HY-MT](https://github.com/Tencent-Hunyuan/HY-MT) · [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) · [SoundCard](https://github.com/bastibe/SoundCard)
