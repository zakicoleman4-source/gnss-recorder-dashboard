from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert .T02/.TO2 files to RINEX using RTKLIB convbin.exe.")
    ap.add_argument("--input", required=True, help="Input .T02/.TO2 file path")
    ap.add_argument("--out-dir", required=True, help="Output directory for RINEX")
    ap.add_argument("--convbin", default="tools/rtklib/convbin.exe", help="Path to convbin.exe")
    ap.add_argument("--rinex-ver", default="3.04", help="RINEX version (default 3.04)")
    args = ap.parse_args()

    inp = Path(args.input).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    convbin = Path(args.convbin).resolve()
    if not convbin.exists():
        raise SystemExit(f"convbin.exe not found: {convbin}")
    if not inp.exists():
        raise SystemExit(f"Input not found: {inp}")

    # convbin usage varies across builds; these flags are supported by most:
    # -r <format> can auto-detect by extension; we set output dir only.
    # -v for rinex version
    cmd = [
        str(convbin),
        str(inp),
        "-od",
        str(out_dir),
        "-v",
        str(args.rinex_ver),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"convbin failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

