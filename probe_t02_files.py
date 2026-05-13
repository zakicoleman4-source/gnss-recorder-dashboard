"""
probe_t02_files.py — standalone T02/T04 file inventory tool

Scans a folder of Trimble binary files, extracts metadata from each file's
embedded bzip2 header (no external tools needed), and prints a summary report
plus a CSV of all results.

Usage:
    python probe_t02_files.py <data_folder> [--out results.csv] [--workers 16]

Requires: Python 3.8+, pandas (pip install pandas)
"""
from __future__ import annotations

import argparse
import bz2
import datetime
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Regex battery — same patterns as to2_pipeline.py probe
# ---------------------------------------------------------------------------
# Field terminator: REQUIRED at least one control/punct char so non-greedy
# captures must extend up to a real delimiter (was `*` -- allowed zero, which
# let non-greedy stop at minimum and truncate names like "AHTI" to "AHT").
_FIELD_END = r"[\x00-\x1f,;]+"

_RE_START    = re.compile(
    r"(?:SessionStart(?:Utc)?|StartTime|FirstObs|session_start)\s*[=:]\s*"
    r"([0-9T:.\-Z+ ]{10,30}?)" + _FIELD_END, re.IGNORECASE)
_RE_END      = re.compile(
    r"(?:SessionEnd(?:Utc)?|EndTime|LastObs|session_end)\s*[=:]\s*"
    r"([0-9T:.\-Z+ ]{10,30}?)" + _FIELD_END, re.IGNORECASE)
_RE_INTERVAL_MS = re.compile(
    r"(?:SessionMeasIntervalMsecs|MeasIntervalMsecs|IntervalMsecs)\s*[=:]\s*([0-9]+)",
    re.IGNORECASE)
_RE_INTERVAL = re.compile(
    r"(?:MeasInterval|SampleInterval|SampleRate|Interval|Rate)\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE)
_RE_MARKER   = re.compile(
    r"(?:RefStationName|RefStationCode|MarkerName|SiteName|StationName|StationId|station_id|marker)"
    r"\s*[=:]\s*([A-Za-z0-9][A-Za-z0-9_\-]{2,18}?)" + _FIELD_END, re.IGNORECASE)
_RE_LLH      = re.compile(
    r"RefStationLLH\s*[=:]\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)",
    re.IGNORECASE)
_RE_ECEF     = re.compile(
    r"(?:RefStationXYZ|StationXYZ|ApproxXYZ)\s*[=:]\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)",
    re.IGNORECASE)
_RE_RX_MODEL = re.compile(
    r"ReceiverId\s*[:=]\s*\d+\s*,\s*([^,\x00\r\n]{2,40})", re.IGNORECASE)
_RE_RX_SERIAL = re.compile(
    r"ReceiverId\s*[:=]\s*\d+\s*,[^,]+,\s*([A-Z0-9]{4,20})", re.IGNORECASE)
_RE_RINEX_XYZ = re.compile(
    r"APPROX POSITION XYZ\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)")

# Known RT27 / CMRx receivers (Alloy-era — no open-source decoder)
_RT27_MARKERS = ("alloy", "netr9 ti-m", "r12i", "r12 receiver")

_PROBE_MAX_BYTES = 1024 * 1024  # 1 MB — covers all metadata blocks


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------
def _ecef_to_llh(x: float, y: float, z: float):
    """Approximate WGS-84 ECEF -> lat/lon/h. Good to ~1 m.
    Returns None for degenerate inputs (NaN/inf, origin, off-Earth radius).
    """
    import math
    if not all(map(math.isfinite, (x, y, z))):
        return None
    if x == 0.0 and y == 0.0 and z == 0.0:
        return None
    r = math.sqrt(x * x + y * y + z * z)
    if r < 5_000_000.0 or r > 8_000_000.0:
        return None  # not a plausible point on/near Earth surface
    a = 6378137.0
    e2 = 6.69437999014e-3
    lon = math.atan2(y, x)
    p = math.sqrt(x * x + y * y)
    if p < 1.0:
        return (90.0 if z >= 0 else -90.0), math.degrees(lon), abs(z) - a * math.sqrt(1.0 - e2)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(5):
        N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * N * math.sin(lat), p)
    N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    cos_lat = math.cos(lat)
    sin_lat = math.sin(lat)
    if abs(cos_lat) >= abs(sin_lat):
        h = p / cos_lat - N
    else:
        h = z / sin_lat - N * (1 - e2)
    return math.degrees(lat), math.degrees(lon), h


# Same pattern as to2_pipeline.py STATION_RE — kept in sync deliberately
# Allows _ inside station code (AB_C) and tolerates _1/_2 duplicate suffixes
# downstream via the date parser.
_STATION_RE = re.compile(
    r"^([A-Za-z0-9_]{3,9})(?=(?:19|20)\d{2}|\d{3}[a-xA-X])", re.IGNORECASE
)

_STATION_PREFIX_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.]{2,3})", re.IGNORECASE)

def _station_from_filename(name: str) -> Optional[str]:
    m = _STATION_RE.match(name)
    if m:
        code = m.group(1).upper().rstrip("_.")
        if len(code) >= 3:
            return code
    # Fallback: first 3-4 chars; strip trailing _ . (separators, not part of code)
    m = _STATION_PREFIX_RE.match(name)
    if m:
        code = m.group(1).upper().rstrip("_.")
        if len(code) >= 3:
            return code
    return None


# ---------------------------------------------------------------------------
# Filename date extraction (GeoNet: SSSS_YYYYDDDHHMM_*  or  SSSSYYYYMMDDHHMMSS_*)
# Also handles RINEX2-style {STATION}{DOY}{hour-letter}[_N].T02 — year derived
# from parent directory.
# ---------------------------------------------------------------------------
_FN_DT_RINEX2_PROBE = re.compile(
    r"^[A-Za-z0-9_]{3,9}"
    r"(00[1-9]|0[1-9]\d|[12]\d{2}|3[0-5]\d|36[0-6])"
    r"([a-x])"
    r"(?=[._]|$)",
    re.IGNORECASE,
)


def _year_from_path(path: Optional[Path]) -> Optional[int]:
    if path is None:
        return None
    for part in reversed(path.parts[:-1]):
        if re.fullmatch(r"(?:19|20)\d{2}", part):
            return int(part)
    return None


def _doy_to_date(year: int, doy: int):
    """Convert (year, DOY 1..365/366) to date. Rejects DOY 366 on non-leap year
    (prevents silent year-rollover via timedelta)."""
    if doy < 1 or doy > 366:
        return None
    try:
        d = datetime.date(year, 1, 1) + datetime.timedelta(days=doy - 1)
    except (ValueError, OverflowError):
        return None
    if d.year != year:
        return None
    return d


def _date_from_filename(stem: str, path: Optional[Path] = None) -> Optional[str]:
    # YYYYMMDD + 2-digit hour anywhere in stem
    m = re.search(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(\d{2})", stem)
    if m:
        try:
            dt = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
            return dt.strftime("%Y-%m-%dT%H:00:00")
        except ValueError:
            pass
    # YYYY + 3-digit DOY + 2-digit hour
    m = re.search(r"(20\d{2})(\d{3})(\d{2})", stem)
    if m:
        try:
            year, doy, hour = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 0 <= hour <= 23:
                d = _doy_to_date(year, doy)
                if d is not None:
                    return f"{d.strftime('%Y-%m-%d')}T{hour:02d}:00:00"
        except (ValueError, OverflowError):
            pass
    # RINEX2-style: {STATION}{DOY}{a-x}[_N].T02 — needs year from parent dir
    m = _FN_DT_RINEX2_PROBE.match(stem)
    if m:
        try:
            doy = int(m.group(1))
            hour = ord(m.group(2).lower()) - ord('a')
            year = _year_from_path(path)
            if year and 0 <= hour <= 23:
                d = _doy_to_date(year, doy)
                if d is not None:
                    return f"{d.strftime('%Y-%m-%d')}T{hour:02d}:00:00"
        except (ValueError, OverflowError):
            pass
    return None


# ---------------------------------------------------------------------------
# Core probe
# ---------------------------------------------------------------------------
def probe_file(path: Path) -> dict:
    result = {
        "path": str(path),
        "filename": path.name,
        "station_from_fn": _station_from_filename(path.name),
        "size_kb": 0,
        "session_start": None,
        "session_end": None,
        "interval_s": None,
        "marker_name": None,
        "lat": None,
        "lon": None,
        "height_m": None,
        "receiver_model": None,
        "receiver_serial": None,
        "format_guess": "unknown",  # rt17 | rt27 | unknown
        "bzip2_blocks_found": 0,
        "error": None,
    }

    try:
        size = path.stat().st_size
        result["size_kb"] = round(size / 1024, 1)
    except OSError as e:
        result["error"] = str(e)
        return result

    if size <= 0:
        result["error"] = "empty file"
        return result

    try:
        with path.open("rb") as fh:
            blob = fh.read(min(size, _PROBE_MAX_BYTES))
    except OSError as e:
        result["error"] = str(e)
        return result

    def absorb(text: str) -> None:
        if not text:
            return
        if result["session_start"] is None:
            m = _RE_START.search(text)
            if m:
                result["session_start"] = m.group(1).strip()
        if result["session_end"] is None:
            m = _RE_END.search(text)
            if m:
                result["session_end"] = m.group(1).strip()
        if result["interval_s"] is None:
            m = _RE_INTERVAL_MS.search(text)
            if m:
                try:
                    v = int(m.group(1)) / 1000.0
                    if 0 < v < 3600:  # reject garbage like 99999s "intervals"
                        result["interval_s"] = v
                except ValueError:
                    pass
        if result["interval_s"] is None:
            m = _RE_INTERVAL.search(text)
            if m:
                try:
                    v = float(m.group(1))
                    if 0 < v < 3600:
                        result["interval_s"] = v
                except ValueError:
                    pass
        if result["marker_name"] is None:
            m = _RE_MARKER.search(text)
            if m:
                v = m.group(1).strip()
                if 2 <= len(v) <= 9:
                    result["marker_name"] = v
        if result["lat"] is None:
            m = _RE_LLH.search(text)
            if m:
                try:
                    result["lat"]      = float(m.group(1))
                    result["lon"]      = float(m.group(2))
                    result["height_m"] = float(m.group(3))
                except ValueError:
                    pass
        if result["lat"] is None:
            m = _RE_ECEF.search(text)
            if m:
                try:
                    out = _ecef_to_llh(float(m.group(1)), float(m.group(2)), float(m.group(3)))
                    if out is not None:
                        result["lat"], result["lon"], result["height_m"] = out
                except Exception:
                    pass
        if result["lat"] is None:
            m = _RE_RINEX_XYZ.search(text)
            if m:
                try:
                    out = _ecef_to_llh(float(m.group(1)), float(m.group(2)), float(m.group(3)))
                    if out is not None:
                        result["lat"], result["lon"], result["height_m"] = out
                except Exception:
                    pass
        if result["receiver_model"] is None:
            m = _RE_RX_MODEL.search(text)
            if m:
                result["receiver_model"] = m.group(1).strip()
        if result["receiver_serial"] is None:
            m = _RE_RX_SERIAL.search(text)
            if m:
                result["receiver_serial"] = m.group(1).strip()

    # Pass 1: raw bytes as latin-1 (plain-text headers near file start)
    try:
        absorb(blob.decode("latin-1", errors="ignore"))
    except Exception:
        pass

    # Pass 2: decompress BZh streams — stop after first successful block
    # (T02 session metadata is always in block 0; later blocks are measurements)
    MAGIC = b"BZh"
    offset = 0
    blocks = 0
    while blocks < 8:
        pos = blob.find(MAGIC, offset)
        if pos < 0:
            break
        offset = pos + 1
        blocks += 1
        try:
            dec = bz2.BZ2Decompressor().decompress(blob[pos:])
            if dec:
                absorb(dec.decode("ascii", errors="ignore"))
        except Exception:
            continue

    result["bzip2_blocks_found"] = blocks

    # Filename fallback for session_start when header doesn't embed it.
    # Pass full path so RINEX2 letter-hour format can derive year from parent dir.
    if result["session_start"] is None:
        result["session_start"] = _date_from_filename(path.stem, path)

    # Classify format based on receiver model
    rx = (result["receiver_model"] or "").lower()
    if any(m in rx for m in _RT27_MARKERS):
        result["format_guess"] = "rt27"
    elif result["receiver_model"] is not None:
        result["format_guess"] = "rt17"  # known receiver, assume RT17
    elif blocks > 0:
        result["format_guess"] = "unknown_with_bzip2"
    else:
        result["format_guess"] = "no_bzip2_header"

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Probe Trimble T02/T04 files and report what you have.")
    ap.add_argument("folder", help="Root folder to scan recursively")
    ap.add_argument("--out", default="t02_probe_results.csv", help="Output CSV path (default: t02_probe_results.csv)")
    ap.add_argument("--workers", type=int, default=min(16, os.cpu_count() or 4), help="Parallel workers (default: min(16, cpu_count))")
    ap.add_argument("--limit", type=int, default=0, help="Max files to probe (0 = all)")
    args = ap.parse_args()

    root = Path(args.folder)
    if not root.exists():
        sys.exit(f"ERROR: folder not found: {root}")

    print(f"Scanning {root} ...")
    extensions = {".t02", ".t04", ".to2", ".to4"}
    files: list = []
    limit = args.limit if args.limit > 0 else 0
    # Tolerant walk: skips dirs with permission errors instead of aborting
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _e: None, followlinks=False):
        for fn in filenames:
            if Path(fn).suffix.lower() in extensions:
                files.append(Path(dirpath) / fn)
                if limit and len(files) >= limit:
                    break
        if limit and len(files) >= limit:
            break
    print(f"Found {len(files):,} files")

    if limit:
        print(f"Limited to first {limit}")

    if not files:
        sys.exit("No T02/T04 files found.")

    results = []
    t0 = time.time()
    done = 0

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(probe_file, f): f for f in files}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                # Don't let one bad file abort the whole probe
                f = futures[fut]
                results.append({"path": str(f), "filename": f.name, "error": f"worker_exception: {e}"})
            done += 1
            if done % 500 == 0 or done == len(files):
                pct = done / len(files) * 100
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(files) - done) / rate if rate > 0 else 0
                print(f"  {done:,}/{len(files):,}  ({pct:.0f}%)  {rate:.0f} files/s  ETA {eta:.0f}s",
                      end="\r", flush=True)

    print(f"\nProbed {len(results):,} files in {time.time()-t0:.1f}s")

    # --- Write CSV ---
    try:
        import pandas as pd
        df = pd.DataFrame(results)
        df.to_csv(args.out, index=False, encoding="utf-8")
        print(f"Full results -> {args.out}")
    except ImportError:
        import csv
        if results:
            with open(args.out, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=results[0].keys())
                w.writeheader()
                w.writerows(results)
        print(f"Full results -> {args.out}")

    # --- Console summary ---
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    # Format breakdown
    fmt_counts: dict = {}
    for r in results:
        k = r["format_guess"]
        fmt_counts[k] = fmt_counts.get(k, 0) + 1

    print("\nFormat detection (from receiver model in header):")
    label_map = {
        "rt27":               "RT27/Alloy (convertToRinex_cli.exe needed)",
        "rt17":               "RT17 (runpkr00 + convbin can convert)",
        "unknown_with_bzip2": "Unknown receiver model (bzip2 header found -- try CTR)",
        "no_bzip2_header":    "No bzip2 header found (non-standard / corrupted?)",
        "unknown":            "Could not determine format",
    }
    for fmt, count in sorted(fmt_counts.items(), key=lambda x: -x[1]):
        print(f"  {label_map.get(fmt, fmt):<55} {count:>6,}")

    # Receiver model breakdown
    rx_counts: dict = {}
    for r in results:
        k = r["receiver_model"] or "(not found in header)"
        rx_counts[k] = rx_counts.get(k, 0) + 1

    print(f"\nReceiver models ({len(rx_counts)} unique):")
    for rx, count in sorted(rx_counts.items(), key=lambda x: -x[1])[:20]:
        print(f"  {rx:<45} {count:>6,}")
    if len(rx_counts) > 20:
        print(f"  ... and {len(rx_counts)-20} more (see CSV)")

    # Station breakdown — header-based
    stn_counts: dict = {}
    for r in results:
        k = r["marker_name"] or "(not found in header)"
        stn_counts[k] = stn_counts.get(k, 0) + 1

    print(f"\nStations from header ({len(stn_counts)} unique):")
    for stn, count in sorted(stn_counts.items(), key=lambda x: -x[1])[:20]:
        print(f"  {stn:<20} {count:>6,}")
    if len(stn_counts) > 20:
        print(f"  ... and {len(stn_counts)-20} more (see CSV)")

    # Station breakdown — filename-based (what pipeline will use)
    fn_stn_counts: dict = {}
    for r in results:
        k = r["station_from_fn"] or "(UNKNOWN -- filename pattern not recognised)"
        fn_stn_counts[k] = fn_stn_counts.get(k, 0) + 1

    unknown_fn = fn_stn_counts.get("(UNKNOWN -- filename pattern not recognised)", 0)
    print(f"\nStations from FILENAME ({len(fn_stn_counts)} unique) [pipeline will use these]:")
    for stn, count in sorted(fn_stn_counts.items(), key=lambda x: -x[1])[:20]:
        print(f"  {stn:<40} {count:>6,}")
    if len(fn_stn_counts) > 20:
        print(f"  ... and {len(fn_stn_counts)-20} more (see CSV)")
    if unknown_fn > 0:
        print(f"  WARNING: {unknown_fn:,} files could not extract station from filename.")
        print("  These will all group as UNKNOWN in dashboard. Send a sample filename.")
    else:
        print(f"  OK: all files have recognisable filename station prefix.")

    # Interval breakdown
    iv_counts: dict = {}
    for r in results:
        k = r["interval_s"]
        label = f"{k}s" if k is not None else "(not found)"
        iv_counts[label] = iv_counts.get(label, 0) + 1

    print("\nSample intervals:")
    for iv, count in sorted(iv_counts.items(), key=lambda x: -x[1]):
        print(f"  {iv:<15} {count:>6,}")

    # Date range
    starts = [r["session_start"] for r in results if r["session_start"]]
    if starts:
        print(f"\nDate range (session_start): {min(starts)[:10]}  ->  {max(starts)[:10]}")

    # Metadata completeness
    fields = ["session_start", "station_from_fn", "marker_name", "lat", "receiver_model", "interval_s"]
    print("\nMetadata completeness (% of files with field populated):")
    for f in fields:
        found = sum(1 for r in results if r[f] is not None)
        print(f"  {f:<20} {found:>6,} / {len(results):,}  ({found/len(results)*100:.0f}%)")

    # Errors
    errors = [r for r in results if r["error"]]
    if errors:
        print(f"\nFiles with read errors: {len(errors)}")
        for r in errors[:5]:
            print(f"  {r['filename']}: {r['error']}")
        if len(errors) > 5:
            print(f"  ... and {len(errors)-5} more (see CSV)")

    print()
    print("Converter recommendation:")
    rt27 = fmt_counts.get("rt27", 0) + fmt_counts.get("unknown_with_bzip2", 0)
    rt17 = fmt_counts.get("rt17", 0)
    if rt27 > 0 and rt17 > 0:
        print(f"  Mixed dataset: {rt27:,} RT27/Alloy files need convertToRinex_cli.exe,")
        print(f"  {rt17:,} RT17 files need runpkr00 + convbin.")
    elif rt27 > 0:
        print(f"  All {rt27:,} files appear to be RT27/Alloy -> need convertToRinex_cli.exe.")
        print("  runpkr00 + convbin will NOT work on these.")
    elif rt17 > 0:
        print(f"  All {rt17:,} files appear to be RT17 -> runpkr00 + convbin should work.")
    else:
        print("  Could not determine format from headers — check CSV for bzip2_blocks_found.")
    print()


if __name__ == "__main__":
    main()
