from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path


STATION_RE = re.compile(r"^([A-Za-z]{3,4})")


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python OFFLINE_SCAN_COMPARE.py <data_root> <baseline_summary_json>")
        return 2

    data_root = Path(sys.argv[1]).expanduser().resolve()
    baseline = Path(sys.argv[2]).expanduser().resolve()

    if not data_root.exists():
        print(f"Data root not found: {data_root}")
        return 1
    if not baseline.exists():
        print(f"Baseline summary.json not found: {baseline}")
        return 1

    base = json.loads(baseline.read_text(encoding="utf-8"))

    exts = {".t02", ".to2", ".t04", ".to4"}
    counts = Counter()
    file_count = 0
    bytes_total = 0

    for dirpath, _dirnames, filenames in os.walk(data_root):
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in exts:
                continue
            file_count += 1
            try:
                bytes_total += p.stat().st_size
            except Exception:
                pass
            m = STATION_RE.match(fn)
            if m:
                counts[m.group(1).lower()] += 1
            else:
                counts["unknown"] += 1

    print("Offline scan summary (fast):")
    print(f"  files: {file_count}")
    print(f"  total_bytes: {bytes_total}")
    print(f"  unique_stations: {len(counts)}")

    # Compare with baseline where possible
    base_files = int(base.get("files_in_manifest", -1))
    base_total = int(base.get("total_bytes", -1))
    base_unique = int(base.get("unique_prefixes", -1))

    print("\nBaseline summary.json:")
    print(f"  files_in_manifest: {base_files}")
    print(f"  total_bytes: {base_total}")
    print(f"  unique_prefixes: {base_unique}")

    ok = True
    if base_files != -1 and file_count != base_files:
        ok = False
        print(f"\n[DIFF] file_count mismatch: offline={file_count} baseline={base_files}")
    if base_total != -1 and bytes_total != base_total:
        # allow minor mismatch if stat failed on some files
        delta = abs(bytes_total - base_total)
        if delta > 0:
            print(f"[WARN] total_bytes differs by {delta} bytes (can happen if some file stats failed)")
    if base_unique != -1 and len(counts) != base_unique:
        print(f"[WARN] unique station count differs: offline={len(counts)} baseline={base_unique}")
        print("       (this can differ if baseline used a different station regex)")

    # High-signal preview: top 15 stations
    print("\nTop stations (offline):")
    for st, n in counts.most_common(15):
        print(f"  {st}: {n}")

    if ok:
        print("\nRESULT: PASS (offline fast scan matches baseline file count)")
        return 0
    print("\nRESULT: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

