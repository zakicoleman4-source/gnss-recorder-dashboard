"""Build a fully-portable distribution: embeddable Python + dashboard + deps.

Run on a dev machine with internet to produce a folder that runs end-to-end
on a target Windows machine with no Python install required.

Output:
    dist/GNSS_Recorder_Dashboard_portable/
        python311/                  (embeddable Python)
        gnss-recorder-dashboard/    (source)
        LAUNCH.bat                  (sets PYTHONPATH and starts dashboard)
        README.txt

User flow on target machine:
    1. Unzip the portable folder anywhere
    2. Double-click LAUNCH.bat
    3. Browser opens at http://localhost:8501

Requires:
    - Python 3.11+ on builder machine
    - Internet for: python embeddable download, pip wheel fetch
"""
from __future__ import annotations
import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PY_EMBED_VERSION = "3.11.9"
PY_EMBED_URL = f"https://www.python.org/ftp/python/{PY_EMBED_VERSION}/python-{PY_EMBED_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def _log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def _download(url: str, dest: Path) -> None:
    _log(f"download {url}")
    with urllib.request.urlopen(url, timeout=60) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="dist/GNSS_Recorder_Dashboard_portable",
                    help="Output dir (default: dist/GNSS_Recorder_Dashboard_portable)")
    ap.add_argument("--keep-cache", action="store_true",
                    help="Keep downloaded zip + wheel cache for next run")
    ap.add_argument("--zip", action="store_true",
                    help="Also produce a .zip archive next to the output folder for distribution")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    out = Path(args.out).resolve()
    cache = root / "offline_installer" / "_build_cache"
    cache.mkdir(parents=True, exist_ok=True)

    _log(f"output: {out}")
    if out.exists():
        _log(f"clearing existing {out}")
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    py_zip = cache / f"python-{PY_EMBED_VERSION}-embed-amd64.zip"
    if not py_zip.exists():
        _download(PY_EMBED_URL, py_zip)
    else:
        _log(f"cached embed zip: {py_zip}")

    py_dir = out / "python311"
    py_dir.mkdir()
    _log(f"unzip embed -> {py_dir}")
    with zipfile.ZipFile(py_zip) as z:
        z.extractall(py_dir)

    # Enable site-packages (embeddable disables it by default)
    pth = next(py_dir.glob("python*._pth"))
    txt = pth.read_text()
    if "#import site" in txt:
        txt = txt.replace("#import site", "import site")
        pth.write_text(txt)
        _log(f"enabled site-packages in {pth.name}")

    # Bootstrap pip via get-pip.py
    get_pip = cache / "get-pip.py"
    if not get_pip.exists():
        _download(GET_PIP_URL, get_pip)
    py_exe = py_dir / "python.exe"
    _log("installing pip into embed runtime")
    subprocess.check_call([str(py_exe), str(get_pip), "--no-warn-script-location"])

    _log("installing dashboard deps")
    req = root / "requirements.txt"
    subprocess.check_call([
        str(py_exe), "-m", "pip", "install",
        "--no-warn-script-location",
        "-r", str(req),
    ])

    # Copy dashboard source via git ls-files (only tracked files = shipping content).
    # Falls back to a curated list if git not available.
    app_dir = out / "gnss-recorder-dashboard"
    app_dir.mkdir()
    _log(f"copy source -> {app_dir}")

    tracked: list[str] = []
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        tracked = [ln for ln in r.stdout.splitlines() if ln.strip()]
    except Exception as e:
        _log(f"git ls-files failed ({e}); skipping source copy")
        tracked = []

    if not tracked:
        _log("ERROR: no tracked files found; aborting source copy")
        return 1

    copied = 0
    for rel in tracked:
        src_f = root / rel
        if not src_f.exists() or not src_f.is_file():
            continue
        dst_f = app_dir / rel
        dst_f.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src_f, dst_f)
            copied += 1
        except OSError:
            continue
    _log(f"copied {copied} tracked files into source tree")

    # Write LAUNCH.bat
    launch = out / "LAUNCH.bat"
    launch.write_bytes(_LAUNCH_BAT.replace("\n", "\r\n").encode("ascii"))
    readme = out / "README.txt"
    readme.write_text(_PORTABLE_README, encoding="ascii")
    _log(f"wrote {launch.name} + {readme.name}")

    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    _log(f"build complete. total size: {total/1024/1024:.1f} MB")

    if args.zip:
        archive_base = out.parent / out.name
        _log(f"zipping -> {archive_base}.zip (this can take a minute)")
        archive = shutil.make_archive(str(archive_base), "zip", out.parent, out.name)
        zsize = Path(archive).stat().st_size
        _log(f"ZIP ready: {archive} ({zsize/1024/1024:.1f} MB)")
        _log(f"distribute: send {archive} to client")
    else:
        _log(f"distribute: zip {out} -> ship to client  (or rerun with --zip)")

    if not args.keep_cache:
        shutil.rmtree(cache, ignore_errors=True)
    return 0


_LAUNCH_BAT = """@echo off
setlocal enabledelayedexpansion
set HERE=%~dp0
set PY=%HERE%python311\\python.exe
set APP=%HERE%gnss-recorder-dashboard

if not exist "%PY%" (
  echo [gnss] ERROR: portable Python missing at %PY%
  echo [gnss] This bundle is corrupt -- re-download.
  pause
  exit /b 1
)

set STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
set "PYTHONPATH=%APP%;%PYTHONPATH%"
if exist "%APP%\\tools\\runpkr00\\runpkr00.exe"                     set "GNSS_RUNPKR00=%APP%\\tools\\runpkr00\\runpkr00.exe"
if exist "%APP%\\tools\\rtklib\\convbin.exe"                        set "GNSS_CONVBIN=%APP%\\tools\\rtklib\\convbin.exe"
if exist "%APP%\\tools\\rtklib\\rnx2rtkp.exe"                       set "GNSS_RNX2RTKP=%APP%\\tools\\rtklib\\rnx2rtkp.exe"
if exist "%APP%\\tools\\convert_to_rinex\\convertToRinex_cli.exe"   set "GNSS_CTR=%APP%\\tools\\convert_to_rinex\\convertToRinex_cli.exe"

set "PORT="
for /L %%P in (8501,1,8520) do (
  if not defined PORT (
    "%PY%" -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',%%P)); s.close()" >nul 2>&1
    if not errorlevel 1 set "PORT=%%P"
  )
)
if not defined PORT set "PORT=8501"

echo [gnss] Starting portable dashboard at http://localhost:!PORT!
echo [gnss] Press Ctrl+C to stop.

"%PY%" -m streamlit run "%APP%\\dashboard.py" --server.headless=true --browser.gatherUsageStats=false --server.port=!PORT!

echo.
echo [gnss] Dashboard stopped.
pause
endlocal
"""


_PORTABLE_README = """GNSS Recorder Dashboard -- PORTABLE BUNDLE

No Python install required.

1. Unzip this folder anywhere (e.g. C:\\gnss-dashboard\\).
2. Double-click LAUNCH.bat.
3. The browser opens automatically at http://localhost:8501 (or next free port).

To stop the dashboard: press Ctrl+C in the cmd window.

For diagnostics if something breaks:
    Inside gnss-recorder-dashboard\\, double-click CLIENT_DOCTOR.bat.
"""


if __name__ == "__main__":
    sys.exit(main())
