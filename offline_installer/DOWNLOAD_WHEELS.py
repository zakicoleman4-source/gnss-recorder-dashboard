from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


SUPPORTED_PYTHONS: tuple[tuple[int, int], ...] = ((3, 10), (3, 11), (3, 12), (3, 13))


def _pip_download(
    *,
    req: Path,
    wheelhouse: Path,
    python_version: tuple[int, int] | None = None,
) -> int:
    """
    Download wheels into wheelhouse.

    If python_version is set, we ask pip to resolve for that target interpreter
    on Windows amd64 (works even if you don't have that Python installed).
    """
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--dest",
        str(wheelhouse),
        "--requirement",
        str(req),
        "--only-binary",
        ":all:",
    ]

    if python_version is not None:
        maj, minor = python_version
        cp_tag = f"cp{maj}{minor}"
        cmd += [
            "--platform",
            "win_amd64",
            "--implementation",
            "cp",
            "--python-version",
            f"{maj}.{minor}",
            "--abi",
            cp_tag,
        ]

    print("Running:", " ".join(cmd))
    p = subprocess.run(cmd)
    return int(p.returncode)


def main() -> int:
    ap = argparse.ArgumentParser(description="Download offline wheelhouse for this project.")
    ap.add_argument(
        "--all-supported",
        action="store_true",
        help="Download wheelhouses for Python 3.10–3.13 (win_amd64).",
    )
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    req = here / "requirements_offline.txt"
    if not req.exists():
        print(f"Missing: {req}", file=sys.stderr)
        return 1

    # This bundle targets Python 3.10+.
    if (sys.version_info.major, sys.version_info.minor) < (3, 10):
        print("ERROR: Python 3.10+ is required to build wheelhouses.", file=sys.stderr)
        return 1

    if args.all_supported:
        for ver in SUPPORTED_PYTHONS:
            py_tag = f"cp{ver[0]}{ver[1]}"
            wheelhouse = here / f"wheelhouse_{py_tag}"
            wheelhouse.mkdir(parents=True, exist_ok=True)
            rc = _pip_download(req=req, wheelhouse=wheelhouse, python_version=ver)
            if rc != 0:
                print(f"ERROR: pip download failed for Python {ver[0]}.{ver[1]}", file=sys.stderr)
                return rc
        return 0

    # Default: build only for the Python running this script
    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    wheelhouse = here / f"wheelhouse_{py_tag}"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    return _pip_download(req=req, wheelhouse=wheelhouse, python_version=None)


if __name__ == "__main__":
    raise SystemExit(main())

