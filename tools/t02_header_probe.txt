"""
t02_header_probe.py  —  Tier 1 T02/T04 header scraper (no conversion needed)

Usage:
    python t02_header_probe.py  <file_or_folder>  [--limit N]

What it does:
    1. Reads raw bytes of each .T02/.T04 file
    2. Scans the first 64 KB for any embedded bzip2 stream (BZh magic)
    3. Decompresses and searches for known Trimble text patterns
    4. Also parses the filename for datetime
    5. Prints a per-file summary + writes results.csv

No external dependencies — stdlib only.
"""
from __future__ import annotations

import argparse
import bz2
import csv
import re
import struct
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# ── Extensions ──────────────────────────────────────────────────────────────
TO_EXTS = {".t02", ".to2", ".t04", ".to4"}

# ── Filename datetime patterns ───────────────────────────────────────────────
# SSSS YYYY MM DD HH MM [SS] [a]  e.g. AHTI20260301000a.T02
_FN_MMDD = re.compile(
    r"[A-Za-z]{0,4}((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])"
    r"([01]\d|2[0-3])(\d{2})(?:\d{2})?[a-zA-Z]?(?=\.)", re.IGNORECASE
)
# SSSS YYYY DOY HH MM [SS] [a]  e.g. AHTI2026060000a.T02
_FN_DOY = re.compile(
    r"[A-Za-z]{0,4}((?:19|20)\d{2})(00[1-9]|0[1-9]\d|[12]\d{2}|3[0-5]\d|36[0-6])"
    r"([01]\d|2[0-3])(\d{2})(?:\d{2})?[a-zA-Z]?(?=\.)", re.IGNORECASE
)


def _parse_filename(name: str) -> dict:
    m = _FN_MMDD.search(name)
    if m:
        try:
            y, mo, d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
            return {"fn_date": date(y, mo, d).isoformat(), "fn_hour": h, "fn_minute": mi, "fn_layout": "MMDD"}
        except (ValueError, OverflowError):
            pass
    m = _FN_DOY.search(name)
    if m:
        try:
            y, doy, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            d = (date(y, 1, 1) + timedelta(days=doy - 1)).isoformat()
            return {"fn_date": d, "fn_hour": h, "fn_minute": mi, "fn_layout": "DOY"}
        except (ValueError, OverflowError):
            pass
    return {"fn_date": None, "fn_hour": None, "fn_minute": None, "fn_layout": None}


# ── Known Trimble text patterns inside bzip2 blobs ──────────────────────────
# These were observed in real T02 dumps. Patterns are intentionally loose
# so we catch vendor variations. We dump all raw hits too.

_PATTERNS = [
    # Receiver identity
    ("receiver_model",   re.compile(r"ReceiverId\s*:\s*\d+\s*,\s*([^,\r\n]+?)\s*(?:,|$|\r|\n)", re.IGNORECASE)),
    ("receiver_serial",  re.compile(r"ReceiverId\s*:\s*\d+\s*,[^,\r\n]+,\s*([A-Za-z0-9]+)", re.IGNORECASE)),
    ("firmware",         re.compile(r"\bfw\s*:\s*([0-9][^\s;,\r\n]{0,20})", re.IGNORECASE)),
    # Timestamps  (ISO-like or compact forms)
    ("session_start",    re.compile(r"(?:SessionStart|StartTime|session_start)\s*[=:]\s*([0-9T:.\-Z+]{10,30})", re.IGNORECASE)),
    ("session_end",      re.compile(r"(?:SessionEnd|EndTime|session_end)\s*[=:]\s*([0-9T:.\-Z+]{10,30})", re.IGNORECASE)),
    # Sample interval
    ("interval_s",       re.compile(r"(?:Interval|SampleRate|interval|sample_rate|Rate)\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)\s*(?:s|sec|seconds)?", re.IGNORECASE)),
    # Constellations (text list or individual flags)
    ("constellations",   re.compile(r"(?:Constellations?|GNSS|Systems?)\s*[=:]\s*([A-Za-z,; ]+?)(?:\r|\n|;|$)", re.IGNORECASE)),
    # Site / marker name
    ("marker_name",      re.compile(r"(?:MarkerName|SiteName|Station|marker)\s*[=:]\s*([A-Za-z0-9_\-]{2,20})", re.IGNORECASE)),
    # Antenna / receiver type fallbacks
    ("antenna",          re.compile(r"(?:AntennaType|Antenna)\s*[=:]\s*([^\r\n,;]{3,40})", re.IGNORECASE)),
]

# Fallback: try to find any ISO-ish datetime in the blob
_DT_ANYWHERE = re.compile(r"((?:19|20)\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)")


def _scrape_bzip2_blob(blob: bytes) -> dict:
    """Find and decompress every BZh block in blob; extract known patterns."""
    results: dict = {k: None for k, _ in _PATTERNS}
    results["raw_datetimes"] = []
    results["bzip2_blocks_found"] = 0
    results["decompressed_bytes"] = 0
    results["raw_text_sample"] = None

    MAGIC = b"BZh"
    search_in = blob[:65536]  # first 64 KB is enough for headers
    offset = 0

    while True:
        pos = search_in.find(MAGIC, offset)
        if pos < 0:
            break
        results["bzip2_blocks_found"] += 1
        try:
            dec = bz2.decompress(search_in[pos:])
            results["decompressed_bytes"] += len(dec)
        except Exception:
            # Try streaming decompressor which tolerates trailing garbage
            try:
                d = bz2.BZ2Decompressor()
                dec = d.decompress(search_in[pos:])
                results["decompressed_bytes"] += len(dec)
            except Exception:
                offset = pos + 1
                continue

        try:
            text = dec.decode("ascii", errors="ignore")
        except Exception:
            text = ""

        # Save a short sample of the raw text for debugging
        if results["raw_text_sample"] is None and text.strip():
            results["raw_text_sample"] = text[:500].replace("\r", " ").replace("\n", " ")

        for key, pat in _PATTERNS:
            if results[key] is None:
                m = pat.search(text)
                if m:
                    results[key] = m.group(1).strip()

        # Grab any datetime-looking strings for inspection
        for m in _DT_ANYWHERE.finditer(text):
            dt = m.group(1)
            if dt not in results["raw_datetimes"]:
                results["raw_datetimes"].append(dt)

        offset = pos + 1

    results["raw_datetimes"] = "; ".join(results["raw_datetimes"][:10]) or None
    return results


def _probe_file(path: Path) -> dict:
    result: dict = {
        "file": path.name,
        "path": str(path),
        "size_bytes": 0,
    }
    result.update(_parse_filename(path.name))

    try:
        stat = path.stat()
        result["size_bytes"] = stat.st_size
    except OSError as e:
        result["error"] = str(e)
        return result

    if stat.st_size == 0:
        result["error"] = "empty file"
        return result

    try:
        blob = path.read_bytes()
    except OSError as e:
        result["error"] = str(e)
        return result

    result.update(_scrape_bzip2_blob(blob))
    return result


def _iter_files(root: Path, limit: Optional[int]) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in TO_EXTS else []
    found = []
    for dirpath, dirnames, filenames in __import__("os").walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in {"__pycache__", ".venv", "node_modules"}]
        for fn in filenames:
            if Path(fn).suffix.lower() in TO_EXTS:
                found.append(Path(dirpath) / fn)
                if limit and len(found) >= limit:
                    return found
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="Tier 1 T02/T04 header probe — no conversion needed")
    ap.add_argument("path", help="T02/T04 file or folder to scan")
    ap.add_argument("--limit", type=int, default=20, help="Max files to probe (default 20)")
    ap.add_argument("--out", default="t02_probe_results.csv", help="Output CSV path")
    args = ap.parse_args()

    root = Path(args.path).expanduser().resolve()
    files = _iter_files(root, args.limit)
    if not files:
        print(f"No T02/T04 files found under: {root}", file=sys.stderr)
        return 1

    print(f"Probing {len(files)} file(s)...\n")

    rows = []
    for p in files:
        r = _probe_file(p)
        rows.append(r)

        # Console summary
        print(f"{'='*60}")
        print(f"FILE    : {r['file']}")
        print(f"SIZE    : {r['size_bytes']:,} bytes")
        if r.get("fn_date"):
            print(f"FN DATE : {r['fn_date']}  hour={r['fn_hour']}  min={r['fn_minute']}  layout={r['fn_layout']}")
        else:
            print(f"FN DATE : (not parsed)")
        print(f"BZ2 BLKS: {r.get('bzip2_blocks_found', 0)}  decomp={r.get('decompressed_bytes', 0):,} bytes")
        for key, _ in _PATTERNS:
            val = r.get(key)
            if val:
                print(f"  {key:<20}: {val}")
        if r.get("raw_datetimes"):
            print(f"  {'raw_datetimes':<20}: {r['raw_datetimes']}")
        if r.get("raw_text_sample"):
            print(f"  {'raw_text_sample':<20}: {r['raw_text_sample'][:200]}")
        if r.get("error"):
            print(f"  ERROR: {r['error']}")
        print()

    # Write CSV
    all_keys = list(dict.fromkeys(k for r in rows for k in r))
    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"Results written to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
