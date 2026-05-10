"""
REAL_T02_SELF_TEST.py

End-to-end smoke test on **actual Trimble .T02 bytes** (not synthetic placeholders):
  copy one file → runpkr00 + teqc → SQLite row → export_manifests → non-empty CSV.

Requires optional sample tree on disk (not shipped with every bundle):
  - geonet_2026_060-119_all/2026/060/*.T02  (repo checkout), or
  - geonet_sample/*.t02

Exit 0 = pass or SKIP (no sample data). Exit 1 = tools missing or conversion/manifest failed.

Run:
  python REAL_T02_SELF_TEST.py
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


def _find_first_t02(root: Path) -> Path | None:
    """Prefer shallow known layouts to avoid scanning huge trees."""
    candidates = [
        root / "geonet_2026_060-119_all" / "2026" / "060",
        root / "geonet_sample",
    ]
    for d in candidates:
        if not d.is_dir():
            continue
        for pat in ("*.T02", "*.t02", "*.TO2", "*.to2"):
            hits = sorted(d.glob(pat))
            if hits:
                return hits[0]
    return None


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))

    src = _find_first_t02(root)
    if src is None:
        print("[SKIP] No real .T02/.t02 under geonet_2026_060-119_all or geonet_sample")
        return 0

    from to2_pipeline import PipelineConfig, export_manifests, run_pipeline

    runpkr = root / "tools" / "runpkr00" / "runpkr00.exe"
    teqc = root / "tools" / "teqc" / "teqc.exe"
    if not runpkr.exists() or not teqc.exists():
        print("[FAIL] Bundled tools/runpkr00/runpkr00.exe or tools/teqc/teqc.exe missing")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="gnss_real_t02_"))
    try:
        data_root = tmp / "data"
        data_root.mkdir()
        dst = data_root / src.name
        shutil.copy2(src, dst)

        cache = tmp / "cache"
        cfg = PipelineConfig(
            data_root=data_root,
            cache_dir=cache,
            runpkr00_path=runpkr,
            teqc_path=teqc,
            max_files_per_station=1,
            stop_after_success_per_station=True,
        )
        db_path = run_pipeline(cfg)

        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT convert_status, convert_detail FROM files WHERE path=?",
            (str(dst),),
        ).fetchone()
        con.close()
        if not row:
            print("[FAIL] No SQLite row for copied T02")
            return 1
        status, detail = row[0], row[1]
        if status != "ok":
            print(f"[FAIL] Expected convert_status='ok' for real T02, got {status!r} detail={detail!r}")
            return 1

        manifests_dir = export_manifests(db_path, out_dir=cache / "exported")
        mf_csv = manifests_dir / "files_manifest.csv"
        if not mf_csv.exists():
            print("[FAIL] export_manifests did not write files_manifest.csv")
            return 1

        import pandas as pd

        df = pd.read_csv(mf_csv)
        if df.empty:
            print("[FAIL] Manifest CSV is empty")
            return 1

        print(
            f"[OK] Real T02 pipeline: source={src.name} rows={len(df)} "
            f"manifests={manifests_dir}"
        )
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
