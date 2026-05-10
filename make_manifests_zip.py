from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="Zip a _manifests folder for sharing/deployment.")
    ap.add_argument(
        "manifests_dir",
        type=str,
        help="Path to a folder containing files_manifest.csv and summary.json (usually .../_manifests).",
    )
    ap.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output zip path (default: <manifests_dir>/../manifests.zip).",
    )
    args = ap.parse_args()

    mdir = Path(args.manifests_dir).expanduser().resolve()
    if not mdir.exists() or not mdir.is_dir():
        raise SystemExit(f"Not a directory: {mdir}")
    if not (mdir / "files_manifest.csv").exists():
        raise SystemExit(f"Missing {mdir / 'files_manifest.csv'}")
    if not (mdir / "summary.json").exists():
        raise SystemExit(f"Missing {mdir / 'summary.json'}")

    out = Path(args.out).expanduser().resolve() if args.out else (mdir.parent / "manifests.zip")
    out.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # Keep a stable layout for the hosted dashboard.
        for name in ["files_manifest.csv", "summary.json", "files_manifest.jsonl", "to2_manifest.csv", "to2_manifest.jsonl"]:
            p = mdir / name
            if p.exists():
                z.write(p, arcname=f"_manifests/{name}")

    print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

