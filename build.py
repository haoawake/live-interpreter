r"""打包（开发用，改完代码后运行 build.bat / python3 build.py 即可）。

Windows：产物直接放在本文件夹：同声传译.exe + _internal\（运行库）。models\、bin\、config.json
这些保持原位，exe 和源码运行共用同一套模型与设置。

    python build.py --stage dist   发布用（CI）：在 dist\ 里组装 live-interpreter\ 文件夹并打成
                                   live-interpreter-v<版本>-windows-x64.zip + .sha256，
                                   不动本文件夹的 exe、模型和快捷方式

macOS（只能在 Mac 上打，需要 Xcode 命令行工具里的 swiftc）：

    python3 build.py --stage dist  生成 dist/同声传译.app（含 Core Audio 助手 lt-audio，ad-hoc 签名）
                                   和 live-interpreter-v<版本>-macos-arm64.zip + .sha256
    python3 build.py --helper      只编译 macos/build/lt-audio（从源码运行 app.py 时要用）

模型、llama.cpp 都不打进包里：首次运行时下载到 ~/Library/Application Support/同声传译/。
"""
import argparse
import hashlib
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import zipfile

import config

ROOT = config.SOURCE_DIR
WORK = os.path.join(ROOT, "build")
NAME = "LiveTranslate"  # ASCII while PyInstaller works; renamed to 同声传译.exe at the end
MAC_NAME = "LiveInterpreter"  # CFBundleExecutable; the bundle itself is 同声传译.app
ICON = os.path.join(ROOT, "assets", "icon.ico")
ASSET = f"live-interpreter-v{config.VERSION}"
DOCS = ["glossary.txt", "README.md", "README.zh-CN.md", "LICENSE", "THIRD_PARTY_NOTICES.md"]
_V = tuple(int(x) for x in config.VERSION.split("."))
VERSION = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({_V[0]}, {_V[1]}, {_V[2]}, 0), prodvers=({_V[0]}, {_V[1]}, {_V[2]}, 0), mask=0x3f,
                    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
      StringStruct('CompanyName', 'haoawake'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 haoawake. MIT License.'),
      StringStruct('FileDescription', '同声传译 Live Interpreter'),
      StringStruct('FileVersion', '{config.VERSION}'),
      StringStruct('InternalName', 'LiveTranslate'),
      StringStruct('OriginalFilename', '同声传译.exe'),
      StringStruct('ProductName', '同声传译'),
      StringStruct('ProductVersion', '{config.VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    with open(path + ".sha256", "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{h.hexdigest()}  {os.path.basename(path)}\n")
    return h.hexdigest()


# ---- Windows ----------------------------------------------------------------------

def build_windows(stage=None):
    import sys_win

    if stage is None and sys_win.app_running():
        sys.exit("同声传译正在运行，请先退出它再打包（exe 被占用时无法替换）。")
    if not os.path.exists(ICON):
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "make_icon.py")], check=True)
    os.makedirs(WORK, exist_ok=True)
    version_file = os.path.join(WORK, "version.txt")
    with open(version_file, "w", encoding="utf-8") as f:
        f.write(VERSION)

    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--noupx",
        "--name", NAME, "--icon", ICON, "--version-file", version_file,
        "--add-data", f"{ICON};assets",
        "--collect-all", "sherpa_onnx",  # its onnxruntime/sherpa DLLs sit next to the .pyd
        "--collect-data", "soundcard",  # the cffi header soundcard parses at import
        "--exclude-module", "PIL", "--exclude-module", "pyflakes",
        "--exclude-module", "sys_mac", "--exclude-module", "audio_mac",
        "--distpath", os.path.join(WORK, "dist"), "--workpath", os.path.join(WORK, "work"),
        "--specpath", WORK, os.path.join(ROOT, "app.py"),
    ], check=True)

    out = os.path.join(WORK, "dist", NAME)
    if stage is not None:
        _stage_windows(out, stage)
        shutil.rmtree(WORK)
        return
    internal = os.path.join(ROOT, "_internal")
    if os.path.exists(internal):
        shutil.rmtree(internal)
    shutil.move(os.path.join(out, "_internal"), internal)
    os.replace(os.path.join(out, NAME + ".exe"), os.path.join(ROOT, config.EXE_NAME))
    shutil.rmtree(WORK)

    import setup_models
    made = setup_models.create_shortcuts()
    print(f"\n完成：{os.path.join(ROOT, config.EXE_NAME)}")
    for p in made:
        print("快捷方式：", p)


def _stage_windows(out, stage):
    """The release zip, laid out like v1.0.0: live-interpreter/{同声传译.exe, _internal/,
    glossary.txt, README*, LICENSE, THIRD_PARTY_NOTICES.md, licenses/}."""
    folder = os.path.join(stage, "live-interpreter")
    if os.path.exists(folder):
        shutil.rmtree(folder)
    os.makedirs(folder)
    os.replace(os.path.join(out, NAME + ".exe"), os.path.join(folder, config.EXE_NAME))
    shutil.move(os.path.join(out, "_internal"), os.path.join(folder, "_internal"))
    for doc in DOCS:
        shutil.copy2(os.path.join(ROOT, doc), folder)
    shutil.copytree(os.path.join(ROOT, "licenses"), os.path.join(folder, "licenses"))

    order = [config.EXE_NAME]
    for base, dirs, files in os.walk(os.path.join(folder, "_internal")):
        dirs.sort()
        order += sorted(os.path.relpath(os.path.join(base, f), folder) for f in files)
    order += DOCS + sorted(os.path.join("licenses", f) for f in os.listdir(os.path.join(folder, "licenses")))
    zip_path = os.path.join(stage, f"{ASSET}-windows-x64.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in order:
            z.write(os.path.join(folder, rel), "live-interpreter/" + rel.replace(os.sep, "/"))
    print(f"\n完成：{zip_path}  sha256 {sha256_file(zip_path)}")


# ---- macOS --------------------------------------------------------------------------

def build_helper(out_dir, target="arm64-apple-macos14.0"):
    """lt-audio: Core Audio process tap / microphone capture and the llama-server watchdog."""
    os.makedirs(out_dir, exist_ok=True)
    exe = os.path.join(out_dir, "lt-audio")
    subprocess.run(["xcrun", "swiftc", "-O", "-swift-version", "5", "-target", target,
                    "-o", exe, os.path.join(ROOT, "macos", "lt-audio.swift")], check=True)
    return exe


def _icns(work):
    src = os.path.join(ROOT, "assets", "icon-mac.png")
    iconset = os.path.join(work, "icon.iconset")
    os.makedirs(iconset, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            px = size * scale
            subprocess.run(["sips", "-z", str(px), str(px), src, "--out", os.path.join(iconset, name)],
                           check=True, capture_output=True)
    icns = os.path.join(work, "icon.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    return icns


def _macho_minos(path):
    """Minimum macOS of a thin arm64 (or fat) Mach-O, from LC_BUILD_VERSION / LC_VERSION_MIN_MACOSX."""
    with open(path, "rb") as f:
        data = f.read(1 << 16)
        magic = struct.unpack(">I", data[:4])[0]
        if magic in (0xCAFEBABE, 0xCAFEBABF):  # fat: find the arm64 slice
            n = struct.unpack(">I", data[4:8])[0]
            wide = magic == 0xCAFEBABF
            for i in range(n):
                if wide:
                    cpu, _, off = struct.unpack(">iiQ", data[8 + i * 32:8 + i * 32 + 16])
                else:
                    cpu, _, off = struct.unpack(">iiI", data[8 + i * 20:8 + i * 20 + 12])
                if cpu == 0x0100000C:
                    f.seek(off)
                    data = f.read(1 << 16)
                    break
            else:
                return None
    if struct.unpack("<I", data[:4])[0] != 0xFEEDFACF:
        return None
    ncmds = struct.unpack("<I", data[16:20])[0]
    off = 32
    for _ in range(ncmds):
        if off + 8 > len(data):
            break
        cmd, size = struct.unpack("<II", data[off:off + 8])
        if cmd == 0x32:  # LC_BUILD_VERSION
            v = struct.unpack("<I", data[off + 12:off + 16])[0]
            return v >> 16, (v >> 8) & 0xFF
        if cmd == 0x24:  # LC_VERSION_MIN_MACOSX
            v = struct.unpack("<I", data[off + 8:off + 12])[0]
            return v >> 16, (v >> 8) & 0xFF
        off += size
    return None


def _min_macos(app):
    found = {}
    for base, _, files in os.walk(app):
        for f in files:
            p = os.path.join(base, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            with open(p, "rb") as fh:
                head = fh.read(4)
            if len(head) == 4 and struct.unpack(">I", head)[0] in (0xCAFEBABE, 0xCAFEBABF, 0xCFFAEDFE):
                v = _macho_minos(p)
                if v:
                    found[os.path.relpath(p, app)] = v
    worst = max(found.values(), default=(11, 0))
    culprits = sorted(k for k, v in found.items() if v == worst)
    return worst, culprits


def build_mac(stage):
    work = os.path.join(WORK, "mac")
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    helper = build_helper(work)
    os.makedirs(os.path.join(ROOT, "macos", "build"), exist_ok=True)
    shutil.copy2(helper, os.path.join(ROOT, "macos", "build", "lt-audio"))  # for source runs, too
    icns = _icns(work)

    sep = os.pathsep
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--noupx",
        "--name", MAC_NAME, "--icon", icns, "--osx-bundle-identifier", config.BUNDLE_ID,
        "--target-arch", "arm64",
        "--add-data", f"{os.path.join(ROOT, 'assets', 'icon.png')}{sep}assets",
        "--add-data", f"{os.path.join(ROOT, 'glossary.txt')}{sep}.",
        "--collect-all", "sherpa_onnx",
        "--collect-data", "certifi",
        "--exclude-module", "PIL", "--exclude-module", "pyflakes",
        "--exclude-module", "sys_win", "--exclude-module", "audio_win",
        "--exclude-module", "pyaudiowpatch", "--exclude-module", "soundcard",
        "--distpath", os.path.join(work, "dist"), "--workpath", os.path.join(work, "pyi"),
        "--specpath", work, os.path.join(ROOT, "app.py"),
    ], check=True)

    os.makedirs(stage, exist_ok=True)
    app = os.path.join(stage, config.APP_NAME + ".app")
    if os.path.exists(app):
        shutil.rmtree(app)
    shutil.move(os.path.join(work, "dist", MAC_NAME + ".app"), app)
    contents = os.path.join(app, "Contents")
    # Every Mach-O file has to sit under Contents/MacOS or Frameworks for codesign
    shutil.copy2(helper, os.path.join(contents, "MacOS", "lt-audio"))
    resources = os.path.join(contents, "Resources")
    for doc in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(os.path.join(ROOT, doc), resources)
    shutil.copytree(os.path.join(ROOT, "licenses"), os.path.join(resources, "licenses"))
    lproj = os.path.join(resources, "zh-Hans.lproj")
    os.makedirs(lproj, exist_ok=True)
    with open(os.path.join(lproj, "InfoPlist.strings"), "w", encoding="utf-8") as f:
        f.write(f'CFBundleName = "{config.APP_NAME}";\nCFBundleDisplayName = "{config.APP_NAME}";\n')

    (major, minor), culprits = _min_macos(app)
    minimum = f"{major}.{minor}"
    print(f"minimum macOS {minimum} (set by {', '.join(culprits[:5])})")
    plist_path = os.path.join(contents, "Info.plist")
    with open(plist_path, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleName": config.APP_NAME,
        "CFBundleDisplayName": config.APP_NAME,
        "CFBundleIdentifier": config.BUNDLE_ID,
        "CFBundleShortVersionString": config.VERSION,
        "CFBundleVersion": config.VERSION,
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleLocalizations": ["zh-Hans"],
        "LSMinimumSystemVersion": minimum,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Copyright © 2026 haoawake. MIT License.",
        "NSAudioCaptureUsageDescription": "同声传译需要录制电脑正在播放的声音（网课、会议、视频），在这台 Mac 上实时"
                                          "识别并翻译成中文字幕。声音不会离开这台 Mac。",
        "NSMicrophoneUsageDescription": "选择麦克风作为音频来源时，同声传译用它来识别英文语音并翻译成中文字幕。"
                                        "声音不会离开这台 Mac。",
    })
    with open(plist_path, "wb") as f:
        plistlib.dump(info, f)

    # Ad-hoc signature (arm64 refuses unsigned code); nested code first, then the bundle.
    subprocess.run(["codesign", "--force", "--sign", "-", os.path.join(contents, "MacOS", "lt-audio")], check=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app], check=True)

    zip_path = os.path.join(stage, f"{ASSET}-macos-arm64.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    # ditto, not zip: it keeps the bundle's symlinks and extended attributes
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, zip_path], check=True)
    size = subprocess.run(["du", "-sh", app], capture_output=True, text=True).stdout.split()[0]
    print(f"\n完成：{app}（{size}）\n      {zip_path}  sha256 {sha256_file(zip_path)}")
    print(f"LSMinimumSystemVersion={minimum}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", help="build the release zip into this folder (CI)")
    ap.add_argument("--helper", action="store_true", help="macOS: only compile macos/build/lt-audio")
    args = ap.parse_args()
    if config.MAC:
        if args.helper:
            print(build_helper(os.path.join(ROOT, "macos", "build")))
            return
        build_mac(os.path.abspath(args.stage or os.path.join(ROOT, "dist")))
    else:
        build_windows(os.path.abspath(args.stage) if args.stage else None)


if __name__ == "__main__":
    main()
