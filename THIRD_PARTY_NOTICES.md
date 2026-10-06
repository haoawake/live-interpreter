# Third-party notices

Live Interpreter's own source code is licensed under the MIT License (see `LICENSE`), including the macOS audio helper `lt-audio` built from `macos/lt-audio.swift`. This file lists the third-party software that the release packages contain, and the components they download on first launch. Full license texts are in the `licenses/` folder (on macOS: `同声传译.app/Contents/Resources/licenses/`).

## Bundled in the Windows release package

These components are included in `_internal\` by PyInstaller.

| Component | Version | License | Text | Project |
|---|---|---|---|---|
| CPython runtime | 3.12.10 | Python Software Foundation License 2.0 | `licenses/Python.txt` | https://www.python.org |
| Tcl/Tk | 8.6.15 | Tcl/Tk License (BSD-style) | `licenses/Tcl-Tk.txt` | https://www.tcl.tk |
| OpenSSL (used by CPython) | 3.0.16 | Apache License 2.0 | `licenses/Apache-2.0.txt` | https://www.openssl.org |
| NumPy | 2.5.3 | BSD-3-Clause, plus the licenses of its bundled components | `licenses/NumPy.txt` | https://numpy.org |
| sherpa-onnx | 1.13.8 | Apache License 2.0 | `licenses/sherpa-onnx.txt` | https://github.com/k2-fsa/sherpa-onnx |
| ONNX Runtime (shipped with sherpa-onnx) | 1.28.2 | MIT License | `licenses/ONNX-Runtime.txt` | https://github.com/microsoft/onnxruntime |
| PyAudioWPatch | 0.2.12.8 | Apache License 2.0 | `licenses/PyAudioWPatch.txt` | https://github.com/s0d3s/PyAudioWPatch |
| PortAudio (linked into PyAudioWPatch) | v19 | PortAudio License (MIT-style) | `licenses/PortAudio.txt` | https://www.portaudio.com |
| SoundCard | 0.4.6 | BSD-3-Clause | `licenses/SoundCard.txt` | https://github.com/bastibe/SoundCard |
| cffi | 2.1.1 | MIT No Attribution | `licenses/cffi.txt` | https://github.com/python-cffi/cffi |
| pycparser | 3.0 | BSD-3-Clause | `licenses/pycparser.txt` | https://github.com/eliben/pycparser |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later with the PyInstaller bootloader exception, which permits distributing the generated executable under any license | `licenses/PyInstaller.txt` | https://pyinstaller.org |
| Microsoft Visual C++ runtime and Universal CRT DLLs | — | Microsoft redistributable terms | — | https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist |

## Bundled in the macOS release package

These components are included in `同声传译.app/Contents/Frameworks` and `Resources` by PyInstaller. PyAudioWPatch, PortAudio, SoundCard, cffi, pycparser and the Microsoft runtimes are Windows-only and not part of the Mac app.

| Component | Version | License | Text | Project |
|---|---|---|---|---|
| CPython runtime (python.org macOS build) | 3.12.10 | Python Software Foundation License 2.0 | `licenses/Python.txt` | https://www.python.org |
| Tcl/Tk (Aqua) | 8.6.16 | Tcl/Tk License (BSD-style) | `licenses/Tcl-Tk.txt` | https://www.tcl.tk |
| OpenSSL (used by CPython) | 3.0 | Apache License 2.0 | `licenses/Apache-2.0.txt` | https://www.openssl.org |
| NumPy (built against Apple Accelerate) | 2.5.3 | BSD-3-Clause, plus the licenses of its bundled components | `licenses/NumPy.txt` | https://numpy.org |
| sherpa-onnx | 1.13.8 | Apache License 2.0 | `licenses/sherpa-onnx.txt` | https://github.com/k2-fsa/sherpa-onnx |
| ONNX Runtime (shipped with sherpa-onnx) | 1.28.2 | MIT License | `licenses/ONNX-Runtime.txt` | https://github.com/microsoft/onnxruntime |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later with the PyInstaller bootloader exception | `licenses/PyInstaller.txt` | https://pyinstaller.org |

## Downloaded on first launch (not redistributed)

These are fetched from their publishers by the first-run setup and are not part of this repository or the release package. Using them means accepting their licenses.

| Component | Source | License |
|---|---|---|
| llama.cpp `b11269` (llama-server and ggml libraries; CUDA or Vulkan build on Windows, macOS arm64 Metal build on a Mac) | https://github.com/ggml-org/llama.cpp/releases | MIT License |
| NVIDIA CUDA runtime libraries (in llama.cpp's CUDA package, Windows only) | https://github.com/ggml-org/llama.cpp/releases | NVIDIA CUDA Toolkit EULA (redistributable components) |
| Nemotron Speech Streaming en 0.6B, sherpa-onnx int8 export | https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models (original model: https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b) | NVIDIA Open Model License |
| HY-MT1.5-1.8B, GGUF | https://huggingface.co/tencent/HY-MT1.5-1.8B-GGUF | Tencent HY Community License Agreement; does not apply in the European Union, the United Kingdom or South Korea |
