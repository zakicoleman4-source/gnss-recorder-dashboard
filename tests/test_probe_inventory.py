"""Tests for probe_t02_files.py standalone inventory tool."""
from __future__ import annotations
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from probe_t02_files import (
    _station_from_filename,
    _date_from_filename,
    _doy_to_date,
    probe_file,
)


def test_doy_to_date_leap_year_366():
    assert _doy_to_date(2024, 366).isoformat() == "2024-12-31"


def test_doy_to_date_non_leap_366_rejected():
    assert _doy_to_date(2025, 366) is None
    assert _doy_to_date(2026, 366) is None


def test_doy_to_date_invalid_rejected():
    assert _doy_to_date(2026, 0) is None
    assert _doy_to_date(2026, 367) is None


def test_station_simple():
    assert _station_from_filename("AHTI2026060010a.T02") == "AHTI"


def test_station_with_underscore():
    assert _station_from_filename("AB_C119a.T02") == "AB_C"


def test_station_numeric_vrs():
    assert _station_from_filename("2406202603010000a.T02") == "2406"


def test_date_mmdd_format():
    assert _date_from_filename("AHTI20260301000a") == "2026-03-01T00:00:00"


def test_date_doy_format():
    assert _date_from_filename("AHTI202606001") == "2026-03-01T01:00:00"


def test_date_rinex2_letter_hour_with_path():
    p = Path("C:/data/2026/AHTI060a.T02")
    assert _date_from_filename("AHTI060a", p) == "2026-03-01T00:00:00"


def test_date_rinex2_366_leap():
    p = Path("C:/data/2024/AHTI366a.T02")
    assert _date_from_filename("AHTI366a", p) == "2024-12-31T00:00:00"


def test_date_rinex2_366_non_leap_rejected():
    p = Path("C:/data/2025/AHTI366a.T02")
    assert _date_from_filename("AHTI366a", p) is None


def test_date_rinex2_with_suffix():
    p = Path("C:/data/2026/AHTI060a_1.T02")
    assert _date_from_filename("AHTI060a_1", p) == "2026-03-01T00:00:00"


def test_probe_file_empty():
    """Empty .T02 file should be recorded as error, no crash."""
    tmp = Path(tempfile.mkdtemp(prefix="probe_empty_"))
    try:
        f = tmp / "AHTI001a.T02"
        f.write_bytes(b"")
        r = probe_file(f)
        assert r["error"] is not None or r["size_kb"] == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_probe_file_garbage():
    """Garbage bytes should not crash."""
    tmp = Path(tempfile.mkdtemp(prefix="probe_junk_"))
    try:
        f = tmp / "AHTI001a.T02"
        f.write_bytes(b"NOT A REAL T02 FILE" * 50)
        r = probe_file(f)
        # Should not raise; format_guess should be "unknown"
        assert r["filename"] == "AHTI001a.T02"
        assert r["station_from_fn"] == "AHTI"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
