from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
import urllib.request
import concurrent.futures
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


_LINE_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<date>\d{2}-[A-Za-z]{3}-\d{4})\s+(?P<time>\d{2}:\d{2})\s+(?P<size>\d+)\s*$"
)
_ANCHOR_LINE_RE = re.compile(
    r"^<a\s+href=\"(?P<href>[^\"]+)\">(?P<name>[^<]+)</a>\s+(?P<date>\d{2}-[A-Za-z]{3}-\d{4})\s+(?P<time>\d{2}:\d{2})\s+(?P<size>\d+)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int


def _fetch_text(url: str, timeout_s: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "gnss-recorder-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _parse_index_listing(text: str) -> list[RemoteFile]:
    """
    The GeoNet endpoint returns an "Index of ..." plaintext-ish listing.
    We parse lines like:
      ABCD202604290000a.T02  29-Apr-2026 01:22  58548
    """
    out: list[RemoteFile] = []
    for line in text.splitlines():
        line = line.strip().strip("`")
        m = _ANCHOR_LINE_RE.match(line) or _LINE_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        # skip parent links etc
        if name.endswith("/"):
            continue
        try:
            size = int(m.group("size"))
        except ValueError:
            continue
        out.append(RemoteFile(name=name, size=size))
    return out


def _iter_doys(start_doy: int, end_doy: int) -> Iterable[int]:
    if end_doy < start_doy:
        start_doy, end_doy = end_doy, start_doy
    for d in range(start_doy, end_doy + 1):
        yield d


def _match_station(name: str, stations: Optional[set[str]]) -> bool:
    if not stations:
        return True
    upper = name.upper()
    for s in stations:
        if upper.startswith(s):
            return True
    return False


def _download(url: str, dst: Path, timeout_s: int = 120) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    req = urllib.request.Request(url, headers={"User-Agent": "gnss-recorder-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp, dst.open("wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


def _download_with_retries(url: str, dst: Path, timeout_s: int, retries: int) -> None:
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            _download(url, dst, timeout_s=timeout_s)
            return
        except Exception as e:
            last_err = e
            # Backoff; be extra gentle on HTTP 429.
            msg = str(e)
            if "HTTP Error 429" in msg or "Too Many Requests" in msg:
                # Strong backoff for rate limiting
                time.sleep(min(300.0, 20.0 * (attempt + 1)))
            else:
                time.sleep(min(20.0, 0.5 * (2**attempt)))
    if last_err:
        raise last_err


def main() -> int:
    ap = argparse.ArgumentParser(description="Download GeoNet GNSS raw (.T02/.T04) by year/DOY and station prefix.")
    ap.add_argument("--base_url", type=str, default="https://data.geonet.org.nz/v1/data/gnss/raw", help="GeoNet base URL")
    ap.add_argument("--year", type=int, default=2026, help="Year (e.g., 2026)")
    ap.add_argument("--start_doy", type=int, required=True, help="Start day-of-year (1-366)")
    ap.add_argument("--end_doy", type=int, required=True, help="End day-of-year (1-366)")
    ap.add_argument(
        "--stations",
        type=str,
        default=None,
        help="Comma-separated station prefixes to download (e.g. AHTI,AKTO). If omitted, downloads all (not recommended).",
    )
    ap.add_argument(
        "--out_dir",
        type=str,
        default=None,
        help="Output directory (default: ./geonet_raw_<year>_<start>-<end>/)",
    )
    ap.add_argument("--limit_per_day", type=int, default=None, help="Optional max files per day (debug/sampling).")
    ap.add_argument("--timeout_s", type=int, default=60, help="HTTP timeout seconds")
    ap.add_argument("--max_workers", type=int, default=6, help="Parallel download workers (default 6; reduce 429s)")
    ap.add_argument("--retries", type=int, default=2, help="Retries per file download (default 2)")
    args = ap.parse_args()

    stations = None
    if args.stations:
        stations = {s.strip().upper() for s in args.stations.split(",") if s.strip()}

    out_dir = Path(args.out_dir) if args.out_dir else Path(f"geonet_raw_{args.year}_{args.start_doy:03d}-{args.end_doy:03d}")
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    errors = 0
    for doy in _iter_doys(args.start_doy, args.end_doy):
        doy_dir = f"{doy:03d}"
        index_url = f"{args.base_url.rstrip('/')}/{args.year}/{doy_dir}/"
        print(f"[INFO] DOY {doy_dir}: fetching index...")
        try:
            text = _fetch_text(index_url, timeout_s=args.timeout_s)
        except Exception as e:
            print(f"[WARN] Failed to fetch {index_url}: {e}", file=sys.stderr)
            continue

        files = _parse_index_listing(text)
        files = [f for f in files if _match_station(f.name, stations)]
        if args.limit_per_day is not None:
            files = files[: args.limit_per_day]

        if not files:
            print(f"[INFO] DOY {doy_dir}: no matching files")
            continue

        print(f"[INFO] DOY {doy_dir}: {len(files)} files to download (pre-resume)")
        day_out = out_dir / str(args.year) / doy_dir
        day_out.mkdir(parents=True, exist_ok=True)

        jobs = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(args.max_workers))) as ex:
            for rf in files:
                file_url = urllib.parse.urljoin(index_url, rf.name)
                dst = day_out / rf.name
                jobs.append(ex.submit(_download_with_retries, file_url, dst, max(120, args.timeout_s), int(args.retries)))
            for fut in concurrent.futures.as_completed(jobs):
                try:
                    fut.result()
                    total += 1
                    if total % 200 == 0:
                        print(f"Downloaded {total} files...")
                except Exception as e:
                    errors += 1
                    if errors <= 20:
                        print(f"[WARN] Download failed: {e}", file=sys.stderr)

    print(f"Done. Downloaded {total} files to {out_dir} (errors={errors})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

