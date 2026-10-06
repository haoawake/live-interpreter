<p align="center"><img src="assets/icon.png" width="88" alt="同声传译图标"></p>

# 同声传译（Live Interpreter）

面向 Windows 和 macOS（Apple 芯片）的离线、低延迟英译中同声传译工具。程序实时采集电脑播放的声音（网课、会议、视频）或麦克风输入，先用缓存感知流式语音识别模型转写英文，再由本地翻译模型译成中文，最后以置顶悬浮窗显示中英双语字幕。音频与文本全程不离开本机。

[English](README.md) · [下载](https://github.com/haoawake/live-interpreter/releases/latest)

**技术栈：** Python 3.12 · sherpa-onnx（ONNX Runtime）· NVIDIA Nemotron Speech Streaming 0.6B · llama.cpp（CUDA / Vulkan / Metal）· 腾讯混元 HY-MT1.5-1.8B · WASAPI · Core Audio（Swift）· Tkinter · PyInstaller

![悬浮字幕窗：白色为已定稿译文，蓝色为正在进行的句子](docs/overlay.png)

---

## 项目背景

云端字幕服务的每一句话都要经过一次网络往返，通常需要注册账号，而且会把课堂或会议的音频上传给第三方。本项目在普通笔记本电脑上本地完成识别与翻译的全部环节，目标只有一个：在说话人还没说完的时候，就给出可读的中文译文。

## 主要特性

| | |
|---|---|
| **真正的流式识别** | 采用缓存感知（cache-aware）FastConformer-RNNT 模型，以 160 ms 为单位增量解码并复用编码器状态，不对滑动窗口重复转写。词语随说随出，自带标点与大小写。 |
| **与语音同步的翻译** | 尚未结束的句子每出现一个新词就重译一次，作为临时译文显示；句子结束后再译一次并定稿。定稿翻译在笔记本 GPU 上耗时 60–240 ms。 |
| **完全离线** | 识别运行于 CPU，翻译运行于本地 GPU（Windows 上 CUDA 或 Vulkan，Mac 上 Metal）或 CPU。无需 API 密钥，首次下载完成后不再产生网络流量。 |
| **任意音源** | 采集电脑播放的任何声音（Windows 用 WASAPI Loopback，macOS 14.2 起用 Core Audio 进程录音，无需安装虚拟声卡），也支持麦克风。默认音源在插拔耳机时自动跟随系统默认设备。 |
| **术语表** | 以「英文 = 中文」形式定义术语，通过模型的术语干预提示注入，使课程或企业专有名词译法一致；修改后立即生效，无需重启。 |
| **免安装** | Windows 为单文件夹可执行程序（约 90 MB），Mac 为拖进「应用程序」即可使用的 `同声传译.app`。首次运行从各模型发布方下载约 2 GB 模型，支持断点续传和国内镜像。 |

## 系统架构

```
系统音频（WASAPI Loopback）/ 麦克风
  │  以 10 ms 为块、按设备原生采样率送入，由 sherpa-onnx 内部重采样
  ▼
流式识别 ── Nemotron Speech Streaming en 0.6B（int8，sherpa-onnx，CPU）
  │  只增不改的转写文本，每 160 ms 更新一次
  ▼
断句器 ── 下一句开始、句号出现后片刻或出现停顿时提交句子；过长的从句在逗号处切分
  ├─ 未完成的文本 ──► 实时通道 ──┐
  └─ 已提交的句子 ──► 定稿通道 ──┴─► llama-server（2 个并行槽位）── HY-MT1.5-1.8B Q4_K_M
                                     │
                                     ▼
                  悬浮窗：已定稿句子（白色）+ 正在进行的句子（蓝色）
```

采集、识别和两条翻译通道均运行在后台线程，通过同一个事件队列向界面汇报；Tk 主循环只负责绘制。

### 设计要点

- **说话过程中从不重置识别器。** 流式转写模型的输出存在延迟。若像常规端点检测那样在一秒停顿处重置识别流，下一个词的音频帧会在该词输出之前就被消耗掉。在测试讲座中，"Today"、"First" 等句首词因此丢失，句末句号也全部缺失。现在识别流持续运行，由断句器自行记录已提交的词，只有在连续三秒静音且没有待提交内容时才重置。
- **断句策略。** 该模型的句号通常在句子最后一个词之后约一秒、随下一句的首词一同输出。因此句子的提交时机有三种：下一句开始时立即提交；出现句号且其后没有新内容时，等待一个识别块再加 0.2 s 后提交（这段等待也能避免把 "3.5" 中的 "3." 误判为句末）；无新文本超过一个识别块加 0.75 s 时提交。超过 24 个词的从句在最后一个逗号处切分，超过 40 个词则强制切分，从而限制连续长句带来的延迟。
- **避免模型臆测补全。** 对于 "I'm not sure that" 这类未说完的片段，翻译模型会自行补全语义（"我不太确定这一点。"）。在未完成文本末尾追加 "..." 后，模型只翻译已说出的部分（"我不太确定……"）。句子定稿时再去掉译文末尾的省略号。
- **稳定的重译。** 实时请求采用贪心解码，同一句子在逐词增长时，前后两次译文的开头保持一致。实时通道只在句子已提交时才放弃进行中的请求；界面只替换完整的译文，而不是逐 token 刷新，否则每出现一个新词整行都会重新开始。
- **使用术语提示而非上下文提示。** 曾尝试 HY-MT 的上下文翻译模板，但模型会把作为上下文的句子一并译出，因此未采用。现在每个句子独立翻译；当句中出现术语表词条（包括复数形式及连字符/空格的不同写法）时，改用术语干预模板。以默认术语表为例，"transformer" 会译为 "Transformer"，而不是 "变压器"。
- **静音时的系统声音采集。** 没有声音播放时，WASAPI Loopback 不会送来任何数据包。引擎会主动补入静音，让识别器输出尚未吐出的词；持续三秒纯静音后停止解码，空闲时不占用 CPU。
- **Windows 平台细节。** sherpa-onnx 按系统 ANSI 代码页打开模型文件，因此程序先切换到自身目录，再以相对路径加载模型，安装目录可以包含中文。llama-server 被加入 Windows 作业对象（Job Object），即使主程序崩溃也会随之退出，不会残留占用显存。悬浮窗为无边框窗口，边缘缩放由程序自行判定；由于 Tk 报告的窗口尺寸在缩放过程中存在滞后，程序直接从 Win32 读取窗口矩形。
- **macOS 平台细节。** 系统声音通过 Core Audio 进程 tap（macOS 14.2 起）采集：一个排除本程序自身的全局单声道 tap，挂在以默认输出设备为时钟的私有聚合设备上。采集由随程序附带的 Swift 小助手（`macos/lt-audio.swift`，位于 `Contents/MacOS`）完成并把音频流传给主程序；默认输出设备变化、设备断开或采样率变化时会自动重建。没有授权时 macOS 只会送来静音而不报错，助手会检查授权状态，并在其他 App 正在播放却什么也收不到时发出提示，悬浮窗随即显示说明和「打开系统设置」按钮。llama.cpp 使用与 Windows 同一版本的官方 macOS arm64（Metal）构建；macOS 没有作业对象，改由助手里的看门狗用 kqueue 等待主程序退出，主程序崩溃或被强制退出时立即结束 llama-server。悬浮窗浮在所有桌面空间和全屏 App 之上，运行期间关闭 App Nap，避免在后台时字幕延迟。

## 性能

测试平台：AMD Ryzen 9 8940HX、NVIDIA GeForce RTX 5060 Laptop GPU（8 GB）、Windows 11。翻译相关指标测量期间，另有程序使 GPU 占用率保持在约 90%。

| 指标 | 结果 |
|---|---|
| 识别：每 160 ms 音频块的解码耗时（CPU） | 8 线程 52 ms · 4 线程 61 ms · 2 线程 102 ms |
| 识别：实时率 | 0.36（160 ms 分块，8 线程）· 0.13（560 ms 分块，4 线程） |
| 翻译：首字延迟（Q4_K_M） | 35–85 ms |
| 翻译：句子提交到定稿译文完成 | 60–240 ms |
| 模型加载 | 2–3 s |
| 内存占用 | 识别 0.8 GB 内存 · 翻译 1.5 GB 内存与 1.5 GB 显存 |

识别准确率引自模型说明（8 个 Open ASR Leaderboard 测试集的平均词错误率）：160 ms 分块 7.67%，560 ms 分块 7.07%，1120 ms 分块 6.93%。

## 系统要求

**Windows**

- Windows 10 / 11，64 位
- 内存 8 GB 以上，推荐 16 GB
- 推荐配备显卡：NVIDIA 显卡需驱动版本 R580 及以上（使用 llama.cpp 的 CUDA 版本）；AMD 与 Intel 显卡使用 Vulkan 版本。没有可用显卡时，翻译回退到 CPU，每句约 0.5–1 s。
- 约 2.5 GB 磁盘空间；仅首次运行需要联网

**macOS**

- Apple 芯片（M1 及更新）的 Mac；不支持 Intel 处理器的 Mac
- macOS 14 Sonoma 或更新版本。采集「电脑播放的声音」需要 macOS 14.2 或更新版本；14.0 / 14.1 上只能使用麦克风
- 内存 8 GB 以上，推荐 16 GB；翻译在 GPU（Metal）上运行
- 约 2.5 GB 磁盘空间；仅首次运行需要联网

## 安装

### Windows

1. 从 [Releases](https://github.com/haoawake/live-interpreter/releases/latest) 下载 `live-interpreter-v<版本>-windows-x64.zip`，解压到任意目录，路径可以包含中文。
2. 运行 `同声传译.exe`。首次运行会列出需要下载的组件，支持断点续传，并可在桌面和开始菜单创建快捷方式。
3. 程序未进行代码签名。若 Windows SmartScreen 提示「Windows 已保护你的电脑」，请选择「更多信息 → 仍要运行」。

### macOS

1. 从 [Releases](https://github.com/haoawake/live-interpreter/releases/latest) 下载 `live-interpreter-v<版本>-macos-arm64.zip`，双击解压，把 `同声传译.app` 拖进「应用程序」文件夹。
2. 第一次打开：程序没有经过 Apple 公证，macOS 会提示无法验证开发者。先在「应用程序」里双击一次，然后打开「系统设置 → 隐私与安全性」，在页面下方点「仍要打开」，再确认一次即可，以后直接打开。（也可以在「终端」里运行 `xattr -cr /Applications/同声传译.app` 后再打开。）
3. 首次运行会列出需要下载的组件（约 2 GB），点「开始下载」，支持断点续传；国内网络可勾选镜像。
4. 开始采集时 macOS 会询问是否允许「同声传译」录制系统声音（选麦克风作音源时则询问麦克风），请选择允许。

### 从源码构建

```bat
git clone https://github.com/haoawake/live-interpreter.git
cd live-interpreter
build.bat
```

`build.bat` 会创建 Python 3.12 虚拟环境、安装依赖、下载模型并生成 `同声传译.exe`。如不打包，可直接运行 `.venv\Scripts\pythonw.exe app.py`。

在 Mac 上（需要 python.org 的 Python 3.12 和 Xcode 命令行工具）：

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt pyinstaller
.venv/bin/python build.py --helper     # 只编译 Core Audio 助手，从源码运行时要用
.venv/bin/python app.py                # 从源码运行
.venv/bin/python build.py              # 打包成 dist/同声传译.app 和发布用的 zip
```

## 使用说明

| 操作 | 作用 |
|---|---|
| 拖动窗口边缘或四角 | 调整窗口大小；窗口越高，显示的历史句子越多 |
| 拖动窗口其他位置 | 移动窗口 |
| `A−` / `A+`，或 Ctrl + 鼠标滚轮（Mac 上 ⌘ + 滚动） | 调整字号 |
| `英` | 显示或隐藏英文原文 |
| `暂停` | 暂停识别 |
| `记录` | 打开本次会话的中英对照记录（可复制） |
| 右键（Mac 上双指轻点或按住 Control 点按），或点击 `设置` | 音频来源、识别模式、翻译模型与 GPU、显示选项、自动保存记录、术语表；Mac 菜单栏的「设置」里也有同样的选项 |
| `—` | Windows 上最小化到任务栏；Mac 上隐藏程序，点程序坞图标恢复 |

白色文字为已定稿的译文；蓝色文字为正在进行的句子，在句子结束前可能随语音变化。会话记录默认保存至 `transcripts\`（可关闭），设置保存在 `config.json`。Mac 上这些文件以及模型、日志、术语表都在 `~/Library/Application Support/同声传译/`，可从菜单「在访达中打开记录文件夹」直接打开。

**识别模式。** 「极速」使用 160 ms 分块；「精准」使用 560 ms 分块，错误率更低，但延迟增加约 0.4 s。翻译模型默认为 Q4_K_M，另可选用略为准确的 Q6_K。

**术语表。** `glossary.txt` 每行一条：

```
transformer = Transformer
self-attention = 自注意力
```

## 常见问题

- **没有字幕。** 观察左上角的音量条是否跳动，并在右键菜单的「音频来源」中确认设备。默认选项「跟随默认播放设备」会在插拔耳机时自动切换。
- **翻译变慢。** 显卡被其他程序（如 Blender、游戏）占满时翻译会变慢，可在右键菜单中关闭「使用显卡（GPU）」改用 CPU。
- **首次下载缓慢。** 勾选「国内网络」可让翻译模型改从 hf-mirror.com 下载；推理引擎与识别模型来自 GitHub Releases。
- **排查问题。** 运行日志位于 `logs\app.log` 与 `logs\llama-server.log`（Mac 上在 `~/Library/Application Support/同声传译/logs/`）。

**Mac 常见问题**

- **提示「无法打开」或「无法验证开发者」。** 见上面「安装 → macOS」第 2 步：「系统设置 → 隐私与安全性 → 仍要打开」。
- **字幕窗口提示没有录制系统声音的权限。** 点提示里的「打开系统设置」，在「隐私与安全性 → 录屏与系统录音」中打开「同声传译」，然后退出并重新打开本程序。使用麦克风时对应的是「隐私与安全性 → 麦克风」。
- **更新到新版本后又要授权。** 程序没有 Apple 开发者签名，macOS 按程序内容识别它，每次更新后可能需要重新允许一次录音权限；如果开关已经是打开的，先关掉再打开。
- **没有字幕，但视频在播放。** 确认音量条在跳动；在「设置 → 音频来源」中选「系统声音」。macOS 14.2 以下的系统不支持采集系统声音，只能用麦克风。
- **全屏看视频时也想看到字幕。** 字幕窗口浮在所有桌面空间和全屏 App 之上；如果看不到，点一下程序坞里的「同声传译」图标。
- **用的是 Intel 处理器的 Mac。** 目前只提供 Apple 芯片版本。

## 项目结构

| 路径 | 说明 |
|---|---|
| `app.py` | 悬浮窗、设置菜单、记录窗口、首次运行下载窗口 |
| `engine.py` | 处理流水线（采集 → 识别 → 断句 → 翻译）及命令行测试工具 |
| `asr.py` | sherpa-onnx 流式识别与断句器 |
| `mt.py` | llama-server 进程管理、提示模板、术语匹配、流式客户端 |
| `audio.py` | 音频采集入口；`audio_win.py` 为跟随默认设备的 WASAPI 采集，`audio_mac.py` 调用 Core Audio 助手 |
| `macos/lt-audio.swift` | macOS 的 Core Audio 助手：进程 tap 采集系统声音、麦克风采集、llama-server 看门狗 |
| `sys_win.py`、`sys_mac.py` | 平台相关代码：Win32 窗口与作业对象；Cocoa 窗口层级、菜单、单实例 |
| `config.py` | 模型清单、路径与默认设置 |
| `setup_models.py` | 可续传的组件下载（含镜像回退）与快捷方式创建 |
| `build.py`、`build.bat` | PyInstaller 打包（Windows 的 exe、macOS 的 .app） |
| `tests/` | 单元测试：`python -m unittest discover -s tests` |
| `.github/workflows/release.yml` | 推送 `v*` 标签后在 GitHub Actions 上构建两个平台的发行包并发布 |

## 开发与测试

按实时速度将 16 bit WAV 文件送入流水线（不启动界面），并打印每个句子提交与翻译完成的时间线：

```bat
.venv\Scripts\python.exe engine.py --wav lecture.wav
.venv\Scripts\python.exe engine.py --wav lecture.wav --all-events
```

`--all-events` 会同时打印实时更新。打包后的程序接受同样的参数（Mac 上为 `同声传译.app/Contents/MacOS/LiveInterpreter --wav lecture.wav`），加 `--cpu` 则翻译走 CPU。修改代码后，先退出程序，再运行 `build.bat` 重新打包。

## 已知限制

- 仅支持英译中，识别模型为英文模型。
- 进行中的句子会随语音不断重译，定稿前措辞可能变化。
- 镜像选项仅适用于 HuggingFace 上的翻译模型；推理引擎与识别模型从 GitHub Releases 下载。
- 其他程序占满 GPU 时翻译速度下降，可在菜单中将翻译切换到 CPU。
- Mac 版仅支持 Apple 芯片；系统声音采集需要 macOS 14.2 或更新版本。程序未经 Apple 公证，首次打开需要在「隐私与安全性」中确认。

## 许可

本项目源代码以 [MIT License](LICENSE) 发布。发行包内附带的第三方组件遵循各自的许可协议，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

模型文件不随本仓库或发行版分发，而是在首次运行时从发布方下载，分别受 NVIDIA Open Model License（Nemotron Speech Streaming）与 Tencent HY Community License Agreement（HY-MT1.5）约束；后者不适用于欧盟、英国和韩国。

## 致谢

[NVIDIA Nemotron Speech Streaming](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b) · [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) · [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) · [Tencent HY-MT](https://github.com/Tencent-Hunyuan/HY-MT) · [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) · [SoundCard](https://github.com/bastibe/SoundCard)
