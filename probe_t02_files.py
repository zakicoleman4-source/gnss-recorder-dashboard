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
_FIELD_END = r"[\x00-\x1f,;\x00]*"

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
    """Approximate WGS-84 ECEF -> lat/lon/h. Good to ~1 m."""
    import math
    a = 6378137.0
    e2 = 6.69437999014e-3
    lon = math.atan2(y, x)
    p = math.sqrt(x * x + y * y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(5):
        N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * N * math.sin(lat), p)
    N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    h = p / math.cos(lat) - N if abs(math.cos(lat)) > 1e-10 else abs(z) / math.sin(lat) - N * (1 - e2)
    return math.degrees(lat), math.degrees(lon), h


# Same pattern as to2_pipeline.py STATION_RE — kept in sync deliberately
_STATION_RE = re.compile(
    r"^([A-Za-z0-9]{3,9})(?=(?:19|20)\d{2}|\d{3}[a-xA-X])", re.IGNORECASE
)

_STATION_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]{2,3})", re.IGNORECASE)

def _station_from_filename(name: str) -> Optional[str]:
    m = _STATION_RE.match(name)
    if m:
        return m.group(1).upper()
    # Fallback: first 3-4 chars — client filenames have reliable station prefix
    # but unreliable date suffix that prevents STATION_RE lookahead from matching
    m = _STATION_PREFIX_RE.match(name)
    return m.group(1).upper() if m else None


# ---------------------------------------------------------------------------
# Filename date extraction (GeoNet: SSSS_YYYYDDDHHMM_*  or  SSSSYYYYMMDDHHMMSS_*)
# ---------------------------------------------------------------------------
def _date_from_filename(stem: str) -> Optional[str]:
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
            if 1 <= doy <= 366 and 0 <= hour <= 23:
                dt = datetime.datetime(year, 1, 1) + datetime.timedelta(days=doy - 1)
                return f"{dt.strftime('%Y-%m-%d')}T{hour:02d}:00:00"
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
                    result["interval_s"] = int(m.group(1)) / 1000.0
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
                    la, lo, h = _ecef_to_llh(float(m.group(1)), float(m.group(2)), float(m.group(3)))
                    result["lat"], result["lon"], result["height_m"] = la, lo, h
                except Exception:
                    pass
        if result["lat"] is None:
            m = _RE_RINEX_XYZ.search(text)
            if m:
                try:
                    la, lo, h = _ecef_to_llh(float(m.group(1)), float(m.group(2)), float(m.group(3)))
                    result["lat"], result["lon"], result["height_m"] = la, lo, h
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

    # Filename fallback for session_start when header doesn't embed it
    if result["session_start"] is None:
        result["session_start"] = _date_from_filename(path.stem)

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
    files = [p for p in root.rglob("*") if p.suffix.lower() in extensions]
    print(f"Found {len(files):,} files")

    if args.limit > 0:
        files = files[: args.limit]
        print(f"Limiting to first {args.limit}")

    if not files:
        sys.exit("No T02/T04 files found.")

    results = []
    t0 = time.time()
    done = 0

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(probe_file, f): f for f in files}
        for fut in as_completed(futures):
            results.append(fut.result())
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
        "rt27":               "RT27/Alloy (convertToRinex_patched.exe needed)",
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
        print(f"  Mixed dataset: {rt27:,} RT27/Alloy files need convertToRinex_patched.exe,")
        print(f"  {rt17:,} RT17 files need runpkr00 + convbin.")
    elif rt27 > 0:
        print(f"  All {rt27:,} files appear to be RT27/Alloy -> need convertToRinex_patched.exe.")
        print("  runpkr00 + convbin will NOT work on these.")
    elif rt17 > 0:
        print(f"  All {rt17:,} files appear to be RT17 -> runpkr00 + convbin should work.")
    else:
        print("  Could not determine format from headers — check CSV for bzip2_blocks_found.")
    print()


if __name__ == "__main__":
    main()
