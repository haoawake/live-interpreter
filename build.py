"""打包成 同声传译.exe（开发用，改完代码后运行 build.bat 即可）。

产物直接放在本文件夹：同声传译.exe + _internal\（运行库）。models\、bin\、config.json
这些保持原位，exe 和源码运行共用同一套模型与设置。
"""
import ctypes
import os
import shutil
import subprocess
import sys

import config
import setup_models

ROOT = config.APP_DIR
WORK = os.path.join(ROOT, "build")
NAME = "LiveTranslate"  # ASCII while PyInstaller works; renamed to 同声传译.exe at the end
ICON = os.path.join(ROOT, "assets", "icon.ico")
VERSION = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=(1, 0, 0, 0), prodvers=(1, 0, 0, 0), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
      StringStruct('CompanyName', 'haoawake'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 haoawake. MIT License.'),
      StringStruct('FileDescription', '同声传译 Live Interpreter'),
      StringStruct('FileVersion', '1.0.0'),
      StringStruct('InternalName', 'LiveTranslate'),
      StringStruct('OriginalFilename', '同声传译.exe'),
      StringStruct('ProductName', '同声传译'),
      StringStruct('ProductVersion', '1.0.0')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def app_running():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenMutexW(0x00100000, False, "Local\\HaoziLiveTranslate")  # SYNCHRONIZE
    if h:
        k32.CloseHandle(h)
    return bool(h)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if app_running():
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
        "--distpath", os.path.join(WORK, "dist"), "--workpath", os.path.join(WORK, "work"),
        "--specpath", WORK, os.path.join(ROOT, "app.py"),
    ], check=True)

    out = os.path.join(WORK, "dist", NAME)
    internal = os.path.join(ROOT, "_internal")
    if os.path.exists(internal):
        shutil.rmtree(internal)
    shutil.move(os.path.join(out, "_internal"), internal)
    os.replace(os.path.join(out, NAME + ".exe"), os.path.join(ROOT, config.EXE_NAME))
    shutil.rmtree(WORK)

    made = setup_models.create_shortcuts()
    print(f"\n完成：{os.path.join(ROOT, config.EXE_NAME)}")
    for p in made:
        print("快捷方式：", p)


if __name__ == "__main__":
    main()
