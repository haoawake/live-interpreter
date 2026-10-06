"""下载识别模型、翻译模型和 llama.cpp，创建快捷方式（仅 Windows）。可重复运行，已有的会跳过。

程序第一次运行时会自动调用这里，在窗口里显示进度。命令行用法（开发用）：
    python setup_models.py           默认组件
    python setup_models.py --all     另外下载「精准」识别模型和 Q6_K 翻译模型
    python setup_models.py --mirror  HuggingFace 文件改走 hf-mirror.com（国内网络）
"""
import argparse
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile

import config

os.chdir(config.APP_DIR)
DOWNLOADS = "downloads"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # the packaged app has no console to borrow
_UA = {"User-Agent": "live-translate-setup"}
_HF, _HF_MIRROR = "https://huggingface.co/", "https://hf-mirror.com/"


def _ssl_context():
    """python.org's macOS Python has no CA store of its own (the frozen app even less so):
    use the bundle macOS itself ships for LibreSSL. Windows Python reads the system store."""
    if not config.MAC or not os.path.exists("/etc/ssl/cert.pem"):
        return None
    try:
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    except Exception:
        return None


_SSL = _ssl_context()
_GH = f"https://github.com/ggml-org/llama.cpp/releases/download/{config.LLAMA_BUILD}/"
LLAMA_BUILDS = {  # flavor -> (label, MB, archives)
    "cuda": ("NVIDIA 显卡版", 550, [_GH + f"llama-{config.LLAMA_BUILD}-bin-win-cuda-13.4-x64.zip",
                                   _GH + "cudart-llama-bin-win-cuda-13.4-x64.zip"]),
    "vulkan": ("通用显卡版", 32, [_GH + f"llama-{config.LLAMA_BUILD}-bin-win-vulkan-x64.zip"]),
    "metal": ("Apple 芯片版", 12, [_GH + f"llama-{config.LLAMA_BUILD}-bin-macos-arm64.tar.gz"]),
}


def nvidia_driver_major():
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        out = subprocess.run([exe, "--query-gpu=driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW).stdout
        return int(out.strip().split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def llama_flavor():
    if config.MAC:
        return "metal"  # Apple silicon only; the build runs on the GPU (Metal) or the CPU
    # The CUDA 13 build needs an R580+ driver; everything else (AMD, Intel, older
    # NVIDIA drivers) gets the Vulkan build.
    major = nvidia_driver_major()
    return "cuda" if major and major >= 580 else "vulkan"


def needed(all_models=False, flavor=None):
    """[(key, label, MB)] of what still has to be downloaded to run."""
    out = []
    if not os.path.exists(config.LLAMA_SERVER):
        flavor = flavor or llama_flavor()
        label, mb, _ = LLAMA_BUILDS[flavor]
        out.append((f"llama:{flavor}", f"翻译推理引擎 llama.cpp（{label}）", mb))
    have = config.asr_installed()
    for key in (["160", "560"] if all_models else [] if have else ["160"]):
        if key not in have:
            m = config.ASR_MODELS[key]
            out.append((f"asr:{key}", f"语音识别模型 Nemotron {m['label']}", m["mb"]))
    have = config.mt_installed()
    for key in (["q4", "q6"] if all_models else [] if have else ["q4"]):
        if key not in have:
            m = config.MT_MODELS[key]
            out.append((f"mt:{key}", f"翻译模型 {m['label']}", m["mb"]))
    return out


def install(key, progress=None, mirror=False):
    """Download and unpack one component. progress(done_bytes, total_bytes, phase)."""
    kind, name = key.split(":")
    if kind == "llama":
        for url in LLAMA_BUILDS[name][2]:
            archive = _fetch_archive(url, progress, mirror)
            if progress:
                progress(0, 0, "unpack")
            if archive.endswith(".tar.gz"):
                _extract_flat(archive, config.LLAMA_DIR)
            else:
                with zipfile.ZipFile(archive) as z:
                    z.extractall(config.LLAMA_DIR)
            os.remove(archive)
    elif kind == "asr":
        m = config.ASR_MODELS[name]
        archive = _fetch_archive(m["url"], progress, mirror)
        if progress:
            progress(0, 0, "unpack")
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(os.path.dirname(m["dir"]), filter="data")
        os.remove(archive)
    elif kind == "mt":
        m = config.MT_MODELS[name]
        download(m["url"], m["file"], progress, mirror)
    if os.path.isdir(DOWNLOADS) and not os.listdir(DOWNLOADS):
        os.rmdir(DOWNLOADS)


def _extract_flat(archive, dest):
    """The macOS build is llama-bNNNN/{llama-server, lib*.dylib, ...} with the dylib
    version symlinks, which zipfile could not recreate; tarfile keeps them and the
    executable bits. The top folder is dropped so the server lands in bin/llama/."""
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = []
        for m in tar.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            m.name = parts[1]
            members.append(m)
        tar.extractall(dest, members=members, filter="data")
    if config.MAC:  # never let Gatekeeper second-guess binaries we fetched ourselves
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", dest], capture_output=True)


def _fetch_archive(url, progress, mirror):
    path = os.path.join(DOWNLOADS, url.rsplit("/", 1)[1])
    if not os.path.exists(path):
        download(url, path, progress, mirror)
    return path


def _remote_size(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers=_UA)
        with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
            return int(r.headers.get("Content-Length") or 0) or None
    except Exception:
        return None


def download(url, dest, progress=None, mirror=False):
    """Resumable download into dest. HuggingFace files fall back to hf-mirror.com."""
    urls = [url]
    if url.startswith(_HF):
        mirrored = _HF_MIRROR + url[len(_HF):]
        urls = [mirrored, url] if mirror else [url, mirrored]
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    errors = []
    for u in urls:
        try:
            _download_one(u, dest, progress)
            return
        except Exception as e:
            errors.append(f"{u.split('/')[2]}: {e}")
    raise RuntimeError("下载失败 " + os.path.basename(dest) + "；" + "；".join(errors))


def _download_one(url, dest, progress, attempts=5):
    part = dest + ".part"
    curl = shutil.which("curl")  # Windows' bundled curl.exe is several times faster than urllib
    if curl:
        cmd = [curl, "-L", "--fail", "--retry", str(attempts), "--retry-delay", "2", "-C", "-", "-o", part, url]
        if progress is None:
            code = subprocess.run(cmd).returncode
        else:
            total = _remote_size(url)
            p = subprocess.Popen([cmd[0], "-sS", *cmd[1:]], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, creationflags=NO_WINDOW)
            while p.poll() is None:
                progress(os.path.getsize(part) if os.path.exists(part) else 0, total, "download")
                time.sleep(0.25)
            code = p.returncode
            if code:
                log_err = p.stderr.read().decode("utf-8", "replace").strip()
                raise RuntimeError(log_err or f"curl 退出代码 {code}")
        if code == 0:
            os.replace(part, dest)
            return
        print(f"  curl 失败（代码 {code}），改用 Python 下载…")

    name = os.path.basename(dest)
    for attempt in range(1, attempts + 1):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        headers = dict(_UA, Range=f"bytes={have}-") if have else _UA
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60,
                                        context=_SSL) as r:
                if have and r.status != 206:  # server ignored the range: start over
                    have = 0
                total = have + int(r.headers.get("Content-Length") or 0)
                done, t0, last = have, time.time(), 0.0
                with open(part, "ab" if have else "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if time.time() - last > 0.25:
                            last = time.time()
                            if progress:
                                progress(done, total or None, "download")
                            else:
                                speed = (done - have) / max(1e-3, last - t0) / 2 ** 20
                                print(f"\r  {name}  {done / 2 ** 20:,.0f}/{total / 2 ** 20:,.0f} MB"
                                      f"  {speed:.1f} MB/s   ", end="", flush=True)
            if not progress:
                print()
            os.replace(part, dest)
            return
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:  # the .part already holds the whole file
                os.replace(part, dest)
                return
            if attempt == attempts:
                raise
        except Exception:
            if attempt == attempts:
                raise
        time.sleep(2 * attempt)


def create_shortcuts(where=("Desktop", "Programs")):
    """同声传译.lnk on the desktop / in the Start menu. Returns the paths created.

    Windows only: on macOS the app lives in 应用程序 and Launchpad/Spotlight find it."""
    if config.MAC:
        return []
    exe = sys.executable if config.FROZEN else os.path.join(config.APP_DIR, config.EXE_NAME)
    if os.path.exists(exe):
        target, args, icon = exe, "", exe + ",0"
    else:  # running from source without a build
        target = os.path.join(config.APP_DIR, ".venv", "Scripts", "pythonw.exe")
        args, icon = '"' + os.path.join(config.APP_DIR, "app.py") + '"', config.ICON_PATH
    # Paths travel as environment variables so the Chinese names never have to
    # survive PowerShell's command-line quoting; GetFolderPath follows OneDrive
    # desktop redirection.
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ws=New-Object -ComObject WScript.Shell;"
          "foreach($f in $env:LT_WHERE.Split(',')){$d=[Environment]::GetFolderPath($f);"
          "$l=Join-Path $d ($env:LT_NAME+'.lnk');$s=$ws.CreateShortcut($l);$s.TargetPath=$env:LT_TARGET;"
          "$s.Arguments=$env:LT_ARGS;$s.WorkingDirectory=$env:LT_WD;$s.IconLocation=$env:LT_ICON;"
          "$s.Description='English to Chinese live interpreter';$s.Save();$l}")
    env = dict(os.environ, LT_WHERE=",".join(where), LT_NAME=config.APP_NAME, LT_TARGET=target,
               LT_ARGS=args, LT_WD=config.APP_DIR, LT_ICON=icon)
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       env=env, capture_output=True, timeout=60, creationflags=NO_WINDOW)
    return [p for p in r.stdout.decode("utf-8", "replace").splitlines() if p.strip() and os.path.exists(p)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="也下载「精准」识别模型和 Q6_K 翻译模型")
    ap.add_argument("--mirror", action="store_true", help="HuggingFace 走 hf-mirror.com")
    ap.add_argument("--llama", choices=tuple(LLAMA_BUILDS), help="默认自动检测显卡")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    todo = needed(args.all, args.llama)
    if not todo:
        print("[已有] 所有组件都已就绪")
    for key, label, mb in todo:
        print(f"[下载] {label}（约 {mb} MB）")
        install(key, mirror=args.mirror)
    print("\n全部就绪。")


if __name__ == "__main__":
    main()
