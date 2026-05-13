"""Unit tests for filename parsing hardening (station + DOY + leap-year guard)."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from to2_pipeline import (
    _station_from_filename,
    _parse_filename_dt,
    _doy_to_date,
    STATION_RE,
    _FN_DT_RINEX2,
)


# ── DOY leap-year guard ─────────────────────────────────────────────────────
def test_doy_to_date_leap_year_366_valid():
    assert _doy_to_date(2024, 366).isoformat() == "2024-12-31"
    assert _doy_to_date(2020, 366).isoformat() == "2020-12-31"


def test_doy_to_date_366_non_leap_rejected():
    assert _doy_to_date(2025, 366) is None
    assert _doy_to_date(2026, 366) is None
    assert _doy_to_date(2023, 366) is None


def test_doy_to_date_365_always_valid():
    assert _doy_to_date(2024, 365).isoformat() == "2024-12-30"
    assert _doy_to_date(2025, 365).isoformat() == "2025-12-31"


def test_doy_to_date_invalid_doy_rejected():
    assert _doy_to_date(2026, 0) is None
    assert _doy_to_date(2026, -1) is None
    assert _doy_to_date(2026, 367) is None
    assert _doy_to_date(2026, 1000) is None


def test_doy_to_date_day_1_valid():
    assert _doy_to_date(2026, 1).isoformat() == "2026-01-01"


# ── Station prefix ───────────────────────────────────────────────────────────
def test_station_simple_alpha():
    assert _station_from_filename("AHTI2026060010a.T02") == "AHTI"


def test_station_alpha_short():
    assert _station_from_filename("ABC119a.T02") == "ABC"


def test_station_numeric_vrs():
    assert _station_from_filename("2406202603010000a.T02") == "2406"


def test_station_with_underscore():
    """Spec: 3-4 chars + _ allowed (e.g. AB_C)"""
    assert _station_from_filename("AB_C119a.T02") == "AB_C"


def test_station_with_underscore_long():
    assert _station_from_filename("A_BC119a.T02") == "A_BC"


def test_station_rinex2_letter_hour():
    assert _station_from_filename("INVK119a.T02") == "INVK"


def test_station_garbage_unknown():
    assert _station_from_filename("zzz.T02") == "ZZZ"  # prefix fallback


def test_station_empty_unknown():
    assert _station_from_filename("") == "UNKNOWN"


# ── Filename date parsing ───────────────────────────────────────────────────
def test_parse_mmdd_format():
    assert _parse_filename_dt("AHTI202603010100a.T02") == ("2026-03-01", 1)


def test_parse_doy_format_with_year():
    # STATION + YYYY + DOY + HH + MM + [SS] + [letter]
    # Regex is greedy: 2026 + 060 + 10 (HH) + 00 (MM) + a
    assert _parse_filename_dt("AHTI20260601000a.T02") == ("2026-03-01", 10)


def test_parse_rinex2_letter_hour_with_year_dir():
    p = Path("C:/data/2026/AHTI060a.T02")
    assert _parse_filename_dt("AHTI060a.T02", p) == ("2026-03-01", 0)


def test_parse_rinex2_hour_letters():
    p = Path("C:/data/2026/AHTI060.T02")
    # 'a' = hour 0
    assert _parse_filename_dt("AHTI060a.T02", p)[1] == 0
    # 'x' = hour 23
    assert _parse_filename_dt("AHTI060x.T02", p)[1] == 23


def test_parse_rinex2_with_duplicate_suffix_1():
    p = Path("C:/data/2026/AHTI060a_1.T02")
    assert _parse_filename_dt("AHTI060a_1.T02", p) == ("2026-03-01", 0)


def test_parse_rinex2_with_duplicate_suffix_2():
    p = Path("C:/data/2026/AHTI060a_2.T02")
    assert _parse_filename_dt("AHTI060a_2.T02", p) == ("2026-03-01", 0)


def test_parse_rinex2_with_misc_suffix():
    p = Path("C:/data/2026/AHTI060a_dup.T02")
    assert _parse_filename_dt("AHTI060a_dup.T02", p) == ("2026-03-01", 0)


def test_parse_doy_366_leap_year_dec_31():
    p = Path("C:/data/2024/AHTI366a.T02")
    assert _parse_filename_dt("AHTI366a.T02", p) == ("2024-12-31", 0)


def test_parse_doy_366_non_leap_rejected():
    p = Path("C:/data/2025/AHTI366a.T02")
    assert _parse_filename_dt("AHTI366a.T02", p) == (None, None)
    p = Path("C:/data/2026/AHTI366a.T02")
    assert _parse_filename_dt("AHTI366a.T02", p) == (None, None)


def test_parse_underscore_station_with_suffix():
    p = Path("C:/data/2026/AB_C119a_1.T02")
    assert _parse_filename_dt("AB_C119a_1.T02", p) == ("2026-04-29", 0)


def test_parse_no_match_returns_nones():
    assert _parse_filename_dt("garbage.T02") == (None, None)


def test_parse_missing_year_returns_nones():
    """RINEX2 letter-hour needs year from parent dir; without path it should fail."""
    # Without path, RINEX2 cannot fire
    result = _parse_filename_dt("AHTI060a.T02")
    # If MMDD/DOY-with-year don't match either, result is (None, None)
    assert result == (None, None)


if __name__ == "__main__":
    # Run inline
    import inspect as _i
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
