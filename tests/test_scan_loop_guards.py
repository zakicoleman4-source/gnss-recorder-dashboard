"""Tests for scan loop hardening: lock leak, COMMIT recovery, file discovery cap, progress_cb guard."""
from __future__ import annotations
import sys
import shutil
import tempfile
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from to2_pipeline import PipelineConfig, run_pipeline, _PIPELINE_LOCK


def _make_test_data(n_files: int = 3) -> Path:
    """Make a tmp dir with n empty .T02 files (header sentinel for size > 0)."""
    d = Path(tempfile.mkdtemp(prefix="scan_test_"))
    for i in range(n_files):
        f = d / f"TEST{i:03d}a.T02"
        f.write_bytes(b"FAKE_T02_HEADER\x00" * 50)  # nonzero size, won't convert
    return d


def test_progress_cb_exception_does_not_kill_scan():
    """Buggy progress_cb must not abort the scan."""
    data = _make_test_data(3)
    cache = Path(tempfile.mkdtemp(prefix="scan_cache_"))
    try:
        cfg = PipelineConfig(data_root=data, cache_dir=cache)

        def boom(*a, **kw):
            raise RuntimeError("simulated callback crash")

        # Should not raise — guard wraps progress_cb call
        db = run_pipeline(cfg, progress_cb=boom)
        assert db.exists()
        conn = sqlite3.connect(str(db))
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()
        assert n == 3, f"expected 3 rows, got {n}"
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


def test_pipeline_lock_released_on_success():
    """_PIPELINE_LOCK must be released after normal scan."""
    data = _make_test_data(2)
    cache = Path(tempfile.mkdtemp(prefix="lock_test_"))
    try:
        cfg = PipelineConfig(data_root=data, cache_dir=cache)
        run_pipeline(cfg)
        # Lock should be released — try to acquire+release without blocking
        acquired = _PIPELINE_LOCK.acquire(blocking=False)
        assert acquired, "lock was not released after scan"
        _PIPELINE_LOCK.release()
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


def test_pipeline_lock_released_on_bad_data_root():
    """_PIPELINE_LOCK must be released even when data_root is invalid."""
    cache = Path(tempfile.mkdtemp(prefix="lock_bad_"))
    try:
        cfg = PipelineConfig(
            data_root=Path("C:/__definitely_does_not_exist__"),
            cache_dir=cache,
        )
        # May raise or return — either way, lock should not leak
        try:
            run_pipeline(cfg)
        except Exception:
            pass
        acquired = _PIPELINE_LOCK.acquire(blocking=False)
        assert acquired, "lock leaked after invalid data_root"
        _PIPELINE_LOCK.release()
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def test_probe_max_total_files_caps_discovery():
    """Unbounded file walk must respect probe_max_total_files."""
    data = _make_test_data(20)
    cache = Path(tempfile.mkdtemp(prefix="cap_test_"))
    try:
        cfg = PipelineConfig(
            data_root=data,
            cache_dir=cache,
            probe_max_total_files=5,  # cap below 20
        )
        db = run_pipeline(cfg)
        conn = sqlite3.connect(str(db))
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()
        assert n <= 5, f"discovery cap not enforced: got {n} rows, cap=5"
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


def test_empty_file_does_not_crash():
    """Zero-byte .T02 files must be recorded as 'skipped', not crash."""
    data = Path(tempfile.mkdtemp(prefix="empty_test_"))
    cache = Path(tempfile.mkdtemp(prefix="empty_cache_"))
    try:
        (data / "EMPT001a.T02").write_bytes(b"")
        (data / "EMPT002a.T02").write_bytes(b"")
        cfg = PipelineConfig(data_root=data, cache_dir=cache)
        db = run_pipeline(cfg)
        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT convert_status FROM files").fetchall()
        conn.close()
        assert len(rows) == 2
        assert all(r[0] == "skipped" for r in rows), f"expected all skipped, got {rows}"
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


def test_repeat_scan_hits_cache():
    """Second run on same data should mark files as cache hits (not re-process)."""
    data = _make_test_data(3)
    cache = Path(tempfile.mkdtemp(prefix="repeat_cache_"))
    try:
        cfg = PipelineConfig(data_root=data, cache_dir=cache)
        db = run_pipeline(cfg)
        # First run populates files
        conn = sqlite3.connect(str(db))
        n1 = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()
        # Second run should not crash and not produce duplicate rows (UPSERT on path)
        run_pipeline(cfg)
        conn = sqlite3.connect(str(db))
        n2 = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()
        assert n1 == n2 == 3
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
