"""Integration test for T02 -> RINEX conversion via convertToRinex_cli.exe.

Skipped if test dataset not present.
"""
from __future__ import annotations
import sys
import shutil
import tempfile
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from to2_pipeline import (
    PipelineConfig, run_pipeline, _convert_t02_ctr, ConverterError,
)

# Test data: GeoNet 2026 samples (path from project memory)
_TEST_SRC = Path("C:/Aj/LINGBOT/geonet_2026_060-119_all/2026/060")
_CTR_EXE = Path(__file__).resolve().parent.parent / "tools/convert_to_rinex/convertToRinex_cli.exe"


def _have_test_data() -> bool:
    return _TEST_SRC.exists() and _CTR_EXE.exists()


def test_ctr_convert_single_t02():
    if not _have_test_data():
        print("    SKIP: test data not present")
        return
    samples = sorted(_TEST_SRC.glob("AHTI2026*.T02"))[:1]
    assert samples, "no AHTI sample files"
    out = Path(tempfile.mkdtemp(prefix="ctr_one_"))
    try:
        obs = _convert_t02_ctr(_CTR_EXE, samples[0], out)
        assert obs is not None and obs.exists()
        assert obs.stat().st_size > 1000, "obs file suspiciously small"
        # Header sanity
        head = obs.read_text(encoding="ascii", errors="ignore")[:2000]
        assert "RINEX VERSION / TYPE" in head
        assert "TIME OF FIRST OBS" in head
        assert "MARKER NAME" in head
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_ctr_empty_file_does_not_crash():
    if not _have_test_data():
        print("    SKIP: test data not present")
        return
    tmp = Path(tempfile.mkdtemp(prefix="ctr_empty_"))
    out = tmp / "out"
    out.mkdir()
    try:
        empty = tmp / "empty.T02"
        empty.write_bytes(b"")
        # CTR produces header-only RINEX from empty input; should not raise here
        try:
            obs = _convert_t02_ctr(_CTR_EXE, empty, out)
            # CTR succeeded, obs exists with header-only content
            if obs:
                assert obs.exists()
        except ConverterError:
            # Acceptable: CTR rejected the empty input
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ctr_stale_obs_not_returned():
    """If a stale .NNo from a prior T02 sits in out_dir, current conversion must
    not return the stale file."""
    if not _have_test_data():
        print("    SKIP: test data not present")
        return
    sample = next(_TEST_SRC.glob("AHTI2026*.T02"), None)
    if sample is None:
        print("    SKIP: AHTI sample missing")
        return
    out = Path(tempfile.mkdtemp(prefix="ctr_stale_"))
    try:
        # Pre-create a stale obs file
        stale = out / "STALE2025001000a.26o"
        stale.write_text("STALE BYTES FROM PRIOR RUN")
        import os, time
        old = time.time() - 3600
        os.utime(stale, (old, old))

        obs = _convert_t02_ctr(_CTR_EXE, sample, out)
        assert obs is not None and obs.exists()
        # Must not have returned the stale file
        assert obs.name != stale.name
        assert obs.name.startswith("AHTI")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_pipeline_end_to_end_two_stations():
    if not _have_test_data():
        print("    SKIP: test data not present")
        return
    data = Path(tempfile.mkdtemp(prefix="pipe_e2e_data_"))
    cache = Path(tempfile.mkdtemp(prefix="pipe_e2e_cache_"))
    try:
        # 2 files / 2 stations
        for st in ("AHTI", "AKTO"):
            for p in sorted(_TEST_SRC.glob(f"{st}2026*.T02"))[:2]:
                shutil.copy2(p, data / p.name)
        cfg = PipelineConfig(
            data_root=data,
            cache_dir=cache,
            ctr_path=_CTR_EXE,
            ctr_first=True,
        )
        db = run_pipeline(cfg)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT station, convert_status, total_epochs, lat, lon "
            "FROM files ORDER BY path"
        ).fetchall()
        conn.close()
        assert len(rows) == 4
        ok = [r for r in rows if r["convert_status"] == "ok"]
        assert len(ok) == 4, f"expected 4 ok rows, got {[dict(r) for r in rows]}"
        # All should have valid positions (NZ stations)
        for r in ok:
            assert r["lat"] is not None and -90 <= r["lat"] <= 90
            assert r["lon"] is not None and -180 <= r["lon"] <= 180
            assert r["total_epochs"] == 120, f"60-min file at 30s should have 120 epochs"
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


if __name__ == "__main__":
    n_pass = n_fail = n_skip = 0
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
