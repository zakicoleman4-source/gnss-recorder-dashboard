"""Tests for export_manifests + schema consistency."""
from __future__ import annotations
import sys
import shutil
import tempfile
import sqlite3
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from to2_pipeline import PipelineConfig, run_pipeline, export_manifests


def _seed_data(n_files: int = 3) -> Path:
    d = Path(tempfile.mkdtemp(prefix="export_seed_"))
    for i in range(n_files):
        (d / f"TEST{i:03d}a.T02").write_bytes(b"FAKE_T02\x00" * 30)
    return d


def test_export_empty_db_writes_columns():
    """Empty scan should still produce a CSV with all expected columns."""
    data = _seed_data(0)
    cache = Path(tempfile.mkdtemp(prefix="export_empty_"))
    try:
        cfg = PipelineConfig(data_root=data, cache_dir=cache)
        db = run_pipeline(cfg)
        out = export_manifests(db, cache.resolve() / "exported")
        csv = out / "files_manifest.csv"
        assert csv.exists()
        import pandas as pd
        df = pd.read_csv(csv)
        assert "station" in df.columns
        assert "convert_status" in df.columns
        assert "completeness_pct" in df.columns
        # Summary JSON also produced
        summary = out / "summary.json"
        if summary.exists():
            data_json = json.loads(summary.read_text())
            assert isinstance(data_json, dict)
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


def test_export_with_files_produces_manifest():
    data = _seed_data(3)
    cache = Path(tempfile.mkdtemp(prefix="export_files_"))
    try:
        cfg = PipelineConfig(data_root=data, cache_dir=cache)
        db = run_pipeline(cfg)
        out = export_manifests(db, cache.resolve() / "exported")
        csv = out / "files_manifest.csv"
        assert csv.exists()
        import pandas as pd
        df = pd.read_csv(csv)
        assert len(df) == 3, f"expected 3 rows, got {len(df)}"
        # file_name should be just basename (not full path)
        for fn in df["file_name"]:
            assert "/" not in str(fn) and "\\" not in str(fn)
        # ext column present + correctly populated
        assert all(df["ext"].astype(str).str.lower() == ".t02")
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


def test_export_creates_summary_json():
    data = _seed_data(2)
    cache = Path(tempfile.mkdtemp(prefix="export_summary_"))
    try:
        cfg = PipelineConfig(data_root=data, cache_dir=cache)
        db = run_pipeline(cfg)
        out = export_manifests(db, cache.resolve() / "exported")
        summary = out / "summary.json"
        assert summary.exists()
        data_json = json.loads(summary.read_text())
        assert "total_files" in data_json or "files_total" in data_json or len(data_json) > 0
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


def test_export_db_schema_complete():
    """run_pipeline should create all required columns in files table."""
    data = _seed_data(1)
    cache = Path(tempfile.mkdtemp(prefix="schema_"))
    try:
        cfg = PipelineConfig(data_root=data, cache_dir=cache)
        db = run_pipeline(cfg)
        conn = sqlite3.connect(str(db))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
        conn.close()
        # Critical columns referenced by export_manifests
        required = {
            "path", "station", "size_bytes", "mtime",
            "time_first_obs", "time_last_obs", "duration_s",
            "interval_s", "total_epochs", "expected_epochs",
            "completeness_pct", "intra_file_gap_count",
            "lat", "lon", "height_m",
            "constellations", "signals",
            "convert_status", "convert_detail",
            "filename_date", "filename_hour",
        }
        missing = required - set(cols)
        assert not missing, f"schema missing columns: {missing}"
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


if __name__ == "__main__":
    n_pass = n_fail = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                n_pass += 1
                print(f"PASS  {name}")
            except AssertionError as e:
                n_fail += 1
                print(f"FAIL  {name}: {e}")
            except Exception as e:
                n_fail += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{n_pass} pass, {n_fail} fail")
    sys.exit(1 if n_fail else 0)
