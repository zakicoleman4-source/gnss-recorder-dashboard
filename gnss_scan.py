from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd


# Common GNSS binary logs are seen as .to2/.to4 and also .t02/.t04 (often uppercase on Windows).
TO_EXTS = {".to2", ".to4", ".t02", ".t04"}


@dataclass(frozen=True)
class ScanConfig:
    root: Path
    exts: frozenset[str] = frozenset(TO_EXTS)


_HOUR_2DIGIT_RE = re.compile(r"(?<!\d)([01]\d|2[0-3])(?!\d)")
_PREFIX_RE = re.compile(r"^([A-Za-z]+)")


def _safe_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except Exception:
        return None


def _parse_date(year_hint: Optional[int], day_folder: str) -> Optional[date]:
    s = day_folder.strip()

    # YYYYMMDD
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except Exception:
            return None

    # YYYY-MM-DD
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        pass

    # DOY (001..366) with year_hint
    if year_hint is not None and len(s) in (3, 4) and s.isdigit():
        doy = _safe_int(s)
        if doy is None:
            return None
        if 1 <= doy <= 366:
            return date(year_hint, 1, 1) + timedelta(days=doy - 1)

    return None


def _infer_receiver_prefix(filename: str) -> str:
    # "ABCD..." from the start of the filename.
    m = _PREFIX_RE.match(filename)
    if m:
        return m.group(1).upper()
    return "UNKNOWN"


def _infer_hour(filename: str) -> Optional[int]:
    name = Path(filename).stem

    # Common case: contains HH anywhere (00-23)
    m = _HOUR_2DIGIT_RE.search(name)
    if m:
        return int(m.group(1))

    # Some GNSS file schemes use A=00 ... X=23 (24 letters)
    # We'll look for a single trailing hour-letter token.
    # e.g. PREFIX..._K.to2  -> K=10
    hour_letter = None
    for token in re.split(r"[^A-Za-z]+", name):
        if len(token) == 1 and token.isalpha():
            hour_letter = token.upper()
    if hour_letter is not None:
        idx = ord(hour_letter) - ord("A")
        if 0 <= idx <= 23:
            return idx

    return None


def iter_gnss_files(root: Path, exts: Iterable[str]) -> Iterable[Path]:
    exts_l = {e.lower() for e in exts}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in exts_l:
                yield p


def scan_gnss_tree(cfg: ScanConfig) -> pd.DataFrame:
    """
    Expected layout (flexible):
      <root>/<year>/<day>/<receiver_folder>/<files...>

    If we can't parse date/hour from folders/filename, we still keep the row and
    mark date/hour as NA.
    """
    root = cfg.root
    year_hint = _safe_int(root.name) if root.name.isdigit() else None

    rows: list[dict] = []
    for p in iter_gnss_files(root, cfg.exts):
        rel = p.relative_to(root)
        parts = rel.parts

        # Heuristic: year/day/receiver/...file
        year_part = parts[0] if len(parts) >= 3 else None
        day_part = parts[1] if len(parts) >= 3 else None
        receiver_folder = parts[2] if len(parts) >= 3 else (parts[1] if len(parts) >= 2 else None)

        year_from_path = _safe_int(year_part) if year_part and year_part.isdigit() else None
        d = _parse_date(year_from_path or year_hint, day_part or "")

        hour = _infer_hour(p.name)
        prefix = _infer_receiver_prefix(p.name)

        rows.append(
            {
                "path": str(p),
                "rel_path": str(rel),
                "ext": p.suffix.lower(),
                "year_folder": year_part,
                "day_folder": day_part,
                "receiver_folder": receiver_folder,
                "receiver_prefix": prefix,
                "date": d,
                "hour": hour,
                "size_bytes": p.stat().st_size if p.exists() else None,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime) if p.exists() else None,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Normalize types
    df["date"] = pd.to_datetime(df["date"])
    df["hour"] = pd.to_numeric(df["hour"], errors="coerce").astype("Int64")
    return df


def hourly_coverage(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns rows: receiver_prefix, date, hour, recorded(bool), n_files
    """
    if df.empty:
        return pd.DataFrame(columns=["receiver_prefix", "date", "hour", "recorded", "n_files"])

    x = df.dropna(subset=["date", "hour"]).copy()
    if x.empty:
        return pd.DataFrame(columns=["receiver_prefix", "date", "hour", "recorded", "n_files"])

    g = (
        x.groupby(["receiver_prefix", "date", "hour"], dropna=False)
        .size()
        .reset_index(name="n_files")
    )
    g["recorded"] = True
    return g


def build_week_grid(coverage: pd.DataFrame, receiver_prefix: str, week_start: date) -> pd.DataFrame:
    """
    Returns a 7x24 grid with columns: dow(0..6), hour(0..23), recorded(bool), n_files(int)
    """
    week_start_dt = pd.to_datetime(week_start)
    days = [week_start_dt + pd.Timedelta(days=i) for i in range(7)]
    grid = pd.MultiIndex.from_product([days, range(24)], names=["date", "hour"]).to_frame(index=False)

    cov = coverage[coverage["receiver_prefix"] == receiver_prefix].copy()
    cov = cov[(cov["date"] >= days[0]) & (cov["date"] <= days[-1])]

    merged = grid.merge(cov[["date", "hour", "n_files"]], on=["date", "hour"], how="left")
    merged["n_files"] = merged["n_files"].fillna(0).astype(int)
    merged["recorded"] = merged["n_files"] > 0
    merged["dow"] = (merged["date"].dt.dayofweek).astype(int)  # Monday=0
    return merged

