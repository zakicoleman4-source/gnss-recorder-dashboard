from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


_DEFAULT_PREFIX_REGEX = r"^(?P<prefix>[A-Za-z0-9]{3,12})"
_DATE_TOKEN_REGEX = re.compile(
    r"(?P<date>(?:19|20)\d{2}[-_/]?(?:0[1-9]|1[0-2])[-_/]?(?:0[1-9]|[12]\d|3[01]))"
)


@dataclass(frozen=True)
class To2Record:
    prefix: str
    file_name: str
    size_bytes: int
    sha1: str
    modified_utc: str
    discovered_from: str
    inferred_date: Optional[str]
    first_bytes_hex: str


def _sha1_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _read_first_bytes_hex(path: Path, n: int = 32) -> str:
    try:
        with path.open("rb") as f:
            return f.read(n).hex()
    except OSError:
        return ""


def _iso_utc_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _infer_date_token(path: Path) -> Optional[str]:
    """
    Heuristic: try to find YYYYMMDD (or YYYY-MM-DD / YYYY_MM_DD) anywhere in path.
    Returns YYYY-MM-DD if found.
    """
    m = _DATE_TOKEN_REGEX.search(str(path))
    if not m:
        return None
    raw = m.group("date")
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) != 8:
        return None
    return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"


def _extract_prefix(file_name: str, prefix_re: re.Pattern[str]) -> str:
    """
    Trust the prefix at the start of the filename.
    Falls back to 'unknown' if it can't be extracted.
    """
    m = prefix_re.match(file_name)
    if not m:
        return "unknown"
    p = m.groupdict().get("prefix") or m.group(0)
    return p.lower()


def _iter_to2_files(root: Path) -> Iterable[Path]:
    # Be robust to inconsistent casing.
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".to2":
            yield p


def _safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _copy_or_link(src: Path, dst: Path, mode: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    if mode == "copy":
        shutil.copy2(src, dst)
        return
    if mode == "hardlink":
        os.link(src, dst)
        return
    if mode == "symlink":
        os.symlink(src, dst)
        return
    raise ValueError(f"Unknown mode: {mode}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Robust .to2 ingester: recursively discover .to2 files under a messy year folder "
            "(e.g. 2026/), group by trusted filename prefix, materialize into a clean layout, "
            "and write a manifest summarizing what was found."
        )
    )
    ap.add_argument(
        "year_folder",
        type=str,
        help="Path to the folder named like '2026' (will be scanned recursively).",
    )
    ap.add_argument(
        "--out_dir",
        type=str,
        default=None,
        help="Output directory for normalized data + manifests (default: <year_folder>_ingested).",
    )
    ap.add_argument(
        "--mode",
        type=str,
        default="copy",
        choices=["copy", "hardlink", "symlink"],
        help="How to materialize files into the normalized layout.",
    )
    ap.add_argument(
        "--prefix_regex",
        type=str,
        default=_DEFAULT_PREFIX_REGEX,
        help="Regex used to extract a trusted prefix from the start of the filename.",
    )
    ap.add_argument(
        "--dry_run",
        action="store_true",
        help="Scan and write manifests, but do not copy/link files.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for how many .to2 files to process (debugging).",
    )
    args = ap.parse_args()

    root = Path(args.year_folder).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"year_folder does not exist or is not a directory: {root}")

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else root.with_name(root.name + "_ingested")
    manifests_dir = out_dir / "_manifests"
    normalized_dir = out_dir / "to2_by_prefix"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    normalized_dir.mkdir(parents=True, exist_ok=True)

    prefix_re = re.compile(args.prefix_regex)

    records: list[To2Record] = []
    scanned = 0

    for src in _iter_to2_files(root):
        scanned += 1
        if args.limit is not None and len(records) >= args.limit:
            break

        prefix = _extract_prefix(src.name, prefix_re)
        inferred_date = _infer_date_token(src)
        st = src.stat()
        rec = To2Record(
            prefix=prefix,
            file_name=src.name,
            size_bytes=int(st.st_size),
            sha1=_sha1_file(src),
            modified_utc=_iso_utc_from_mtime(st.st_mtime),
            discovered_from=_safe_relpath(src, root),
            inferred_date=inferred_date,
            first_bytes_hex=_read_first_bytes_hex(src, 32),
        )
        records.append(rec)

        # Normalize layout:
        #   to2_by_prefix/<prefix>/[optional-date]/<sha1>__<original_name>.to2
        # Using sha1 in filename makes collisions / duplicates explicit.
        date_part = inferred_date if inferred_date else "unknown-date"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", src.name)
        dst = normalized_dir / prefix / date_part / f"{rec.sha1}__{safe_name}"
        if not args.dry_run:
            _copy_or_link(src, dst, args.mode)

    # Manifest: jsonl (stream-friendly) + csv (human-friendly) + summary json
    jsonl_path = manifests_dir / "to2_manifest.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    csv_path = manifests_dir / "to2_manifest.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys()) if records else list(asdict(To2Record("", "", 0, "", "", "", None, "")).keys()))
        w.writeheader()
        for r in records:
            w.writerow(asdict(r))

    by_prefix: dict[str, int] = {}
    total_bytes = 0
    for r in records:
        by_prefix[r.prefix] = by_prefix.get(r.prefix, 0) + 1
        total_bytes += r.size_bytes

    summary = {
        "scanned_paths_checked": scanned,
        "to2_files_found": len(records),
        "total_bytes": total_bytes,
        "unique_prefixes": len(by_prefix),
        "by_prefix_counts": dict(sorted(by_prefix.items(), key=lambda kv: (-kv[1], kv[0]))),
        "root": str(root),
        "out_dir": str(out_dir),
        "normalized_dir": str(normalized_dir),
        "manifests_dir": str(manifests_dir),
        "dry_run": bool(args.dry_run),
        "mode": args.mode,
        "prefix_regex": args.prefix_regex,
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(),
    }
    (manifests_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Found {len(records)} .to2 files under {root}")
    print(f"Normalized data: {normalized_dir}")
    print(f"Manifests: {manifests_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
