#!/usr/bin/env python3
"""
Offline helper: load scan **manifests** (zip or folder with ``files_manifest.csv`` +
``summary.json``) and analyze **one station**.

Includes a **weekly** breakdown (UTC, ISO weeks): estimated hours **recording**
vs **gap** (no session overlap) from manifest file timestamps / optional RINEX
observation windows.

**Gap detail:** lists every contiguous **no-recording** stretch inside the scan span,
in **local time** via ``--tz`` (so you can see e.g. Sunday 15:00-16:00). Optional
``--slot-local 'YYYY-MM-DD HH:MM-HH:MM'`` checks one window (same calendar day).

CLI examples::

    python analyze_station_manifest.py path\\\\to\\\\manifests.zip ABCD
    python analyze_station_manifest.py --folder \"D:\\\\scan\\\\exported\\\\_manifests\" 2406 --session-hours 1

``--session-hours`` applies when the CSV has no ``time_first_obs`` / ``time_last_obs``
columns (typical raw scan): each file counts as that many hours starting at the
time inferred from the filename (hourly T02 names -> use 1).

GUI (double-click or no arguments on Windows)::

    python analyze_station_manifest.py --gui

Requirements: pandas (same venv as the dashboard).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


# --- Event time inference (aligned with dashboard.py for consistent filenames) ---
_TS_REGEXES_LINE = [
    re.compile(r"(20\d{2})([01]\d)([0-3]\d)([0-2]\d)([0-5]\d)([0-5]\d)"),
    re.compile(r"(20\d{2})([01]\d)([0-3]\d)([0-2]\d)([0-5]\d)"),
    re.compile(r"(20\d{2})([01]\d)([0-3]\d)([0-2]\d)"),
]
_RINEX2_REGEX = re.compile(r"^[A-Za-z0-9]{4}(\d{3})(\d)$")
_RINEX3_NAME_DOY_HHMM = re.compile(r"_R_(?P<year>20\d{2})(?P<doy>\d{3})(?P<hhmm>\d{4})_")


def _infer_ts_from_row(row: "pd.Series") -> "pd.Timestamp":
    import pandas as pd

    file_name = str(row.get("file_name", ""))
    discovered_from = str(row.get("discovered_from", ""))
    joined = f"{discovered_from} {file_name}"

    for rgx in _TS_REGEXES_LINE:
        m = rgx.search(joined)
        if not m:
            continue
        parts = [int(x) for x in m.groups()]
        if len(parts) == 6:
            y, mo, d, h, mi, s = parts
        elif len(parts) == 5:
            y, mo, d, h, mi = parts
            s = 0
        else:
            y, mo, d, h = parts
            mi = 0
            s = 0
        try:
            return pd.Timestamp(year=y, month=mo, day=d, hour=h, minute=mi, second=s, tz="UTC")
        except ValueError:
            pass

    m3 = _RINEX3_NAME_DOY_HHMM.search(joined)
    if m3:
        try:
            year = int(m3.group("year"))
            doy = int(m3.group("doy"))
            hhmm = m3.group("hhmm")
            hour = int(hhmm[0:2])
            minute = int(hhmm[2:4])
            jan1 = pd.Timestamp(year=year, month=1, day=1, tz="UTC")
            return jan1 + pd.Timedelta(days=doy - 1, hours=hour, minutes=minute)
        except Exception:
            pass

    stem = Path(file_name).stem
    m_rinex = _RINEX2_REGEX.match(stem)
    if m_rinex:
        doy = int(m_rinex.group(1))
        hour = int(m_rinex.group(2))
        year_m = re.search(r"(20\d{2})", joined)
        if year_m:
            year = int(year_m.group(1))
        else:
            year = pd.to_datetime(row.get("modified_utc"), utc=True, errors="coerce").year
        if pd.notna(year):
            jan1 = pd.Timestamp(year=int(year), month=1, day=1, tz="UTC")
            return jan1 + pd.Timedelta(days=doy - 1, hours=hour)

    fallback = pd.to_datetime(row.get("modified_utc"), utc=True, errors="coerce")
    if pd.isna(fallback):
        return pd.Timestamp.now(tz="UTC")
    return fallback


def _build_event_ts(df_in: "pd.DataFrame") -> "pd.Series":
    import pandas as pd

    fn = df_in.get("file_name", pd.Series([""] * len(df_in))).astype(str)
    df_path = df_in.get("discovered_from", pd.Series([""] * len(df_in))).astype(str)
    joined = df_path.str.cat(fn, sep=" ")
    out = pd.Series(pd.NaT, index=df_in.index, dtype="datetime64[ns, UTC]")

    # Priority 0: time_first_obs from RINEX (most authoritative -- exported by
    # to2_pipeline.export_manifests since the 2026-05-13 release).
    if "time_first_obs" in df_in.columns:
        tfo = pd.to_datetime(df_in["time_first_obs"], errors="coerce", utc=True)
        mask = tfo.notna()
        if mask.any():
            out.loc[mask] = tfo.loc[mask]

    # Priority 1: inferred_date + filename_hour (pipeline's filename DOY parse).
    if "inferred_date" in df_in.columns:
        dates = pd.to_datetime(df_in["inferred_date"], errors="coerce", utc=True)
        if "filename_hour" in df_in.columns:
            hours = pd.to_numeric(df_in["filename_hour"], errors="coerce").fillna(0).clip(0, 23).astype("Int64")
            mask = dates.notna() & out.isna()
            out.loc[mask] = dates.loc[mask] + pd.to_timedelta(hours.loc[mask].astype(float), unit="h")
        else:
            mask = dates.notna() & out.isna()
            out.loc[mask] = dates.loc[mask]

    patterns = [
        (r"(20\d{2})([01]\d)([0-3]\d)([0-2]\d)([0-5]\d)([0-5]\d)", "%Y%m%d%H%M%S"),
        (r"(20\d{2})([01]\d)([0-3]\d)([0-2]\d)([0-5]\d)", "%Y%m%d%H%M"),
        (r"(20\d{2})([01]\d)([0-3]\d)([0-2]\d)", "%Y%m%d%H"),
    ]
    remaining = joined.copy()
    for pat, fmt in patterns:
        mask = out.isna()
        if not mask.any():
            break
        ext = remaining[mask].str.extract(pat)
        if ext.empty:
            continue
        merged = ext.fillna("").agg("".join, axis=1)
        ts = pd.to_datetime(merged.where(merged.str.len() > 0), format=fmt, errors="coerce", utc=True)
        out.loc[mask] = out.loc[mask].combine_first(ts)

    miss_mask = out.isna()
    if miss_mask.any():
        sub = df_in.loc[miss_mask]
        if len(sub) <= 50_000:
            slow = sub.apply(_infer_ts_from_row, axis=1)
            out.loc[miss_mask] = pd.to_datetime(slow, utc=True, errors="coerce")
        elif "modified_utc" in df_in.columns:
            out.loc[miss_mask] = pd.to_datetime(sub["modified_utc"], utc=True, errors="coerce")

    if out.isna().any():
        fb = pd.to_datetime(df_in.get("modified_utc", pd.NaT), utc=True, errors="coerce")
        out = out.fillna(fb)
    if out.isna().any():
        out = out.fillna(pd.Timestamp.now(tz="UTC"))
    return out


def _optional_rinex_interval(row: "pd.Series") -> tuple["pd.Timestamp | None", "pd.Timestamp | None"]:
    """Use observation window columns when present (pipeline / extended manifests)."""
    import pandas as pd

    pairs = [
        ("time_first_obs", "time_last_obs"),
        ("time_first", "time_last"),
    ]
    for a, b in pairs:
        if a not in row.index or b not in row.index:
            continue
        ta = pd.to_datetime(row.get(a), utc=True, errors="coerce")
        tb = pd.to_datetime(row.get(b), utc=True, errors="coerce")
        if pd.notna(ta) and pd.notna(tb) and tb >= ta:
            return ta, tb
    return None, None


def recording_intervals_utc(sub: "pd.DataFrame", session_hours: float) -> list[tuple["pd.Timestamp", "pd.Timestamp"]]:
    """One [start,end) interval per manifest row."""
    import pandas as pd

    if sub.empty:
        return []
    ev = _build_event_ts(sub)
    td_sess = pd.Timedelta(hours=float(session_hours))
    intervals: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for idx in sub.index:
        row = sub.loc[idx]
        t0, t1 = _optional_rinex_interval(row)
        if t0 is not None and t1 is not None:
            start, end = t0, t1
        else:
            ev_val = ev.loc[idx]
            if pd.isna(ev_val):
                # Skip rows with no inferable timestamp -- can't place them on a timeline
                continue
            try:
                start = pd.Timestamp(ev_val).tz_convert("UTC") if ev_val.tzinfo else ev_val.tz_localize("UTC")
            except (AttributeError, TypeError):
                continue
            end = start + td_sess
        if end <= start:
            end = start + td_sess
        intervals.append((start, end))
    return intervals


def merge_intervals(intervals: list[tuple["pd.Timestamp", "pd.Timestamp"]]) -> list[tuple["pd.Timestamp", "pd.Timestamp"]]:
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    merged: list = [intervals[0]]
    for s, e in intervals[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def overlap_seconds(
    merged: list[tuple["pd.Timestamp", "pd.Timestamp"]],
    win_start: "pd.Timestamp",
    win_end: "pd.Timestamp",
) -> float:
    """Total length of intersection of merged intervals with [win_start, win_end)."""
    sec = 0.0
    for s, e in merged:
        a = max(s, win_start)
        b = min(e, win_end)
        if b > a:
            sec += (b - a).total_seconds()
    return sec


def gaps_in_window(
    merged: list[tuple["pd.Timestamp", "pd.Timestamp"]],
    win_start: "pd.Timestamp",
    win_end: "pd.Timestamp",
) -> list[tuple["pd.Timestamp", "pd.Timestamp"]]:
    """
    Return contiguous gaps (no recording) inside [win_start, win_end), in UTC.
    ``merged`` must be non-overlapping sorted intervals (e.g. output of merge_intervals).
    """
    import pandas as pd

    def _to_utc(x):
        x = pd.Timestamp(x)
        return x.tz_localize("UTC") if x.tzinfo is None else x.tz_convert("UTC")

    win_start = _to_utc(win_start)
    win_end = _to_utc(win_end)
    if win_end <= win_start:
        return []
    if not merged:
        return [(win_start, win_end)]

    gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    cur = win_start
    for s, e in sorted(merged, key=lambda x: x[0]):
        s = _to_utc(s)
        e = _to_utc(e)
        s = max(s, win_start)
        e = min(e, win_end)
        if e <= win_start or s >= win_end:
            continue
        if s > cur:
            gaps.append((cur, s))
        cur = max(cur, e)
        if cur >= win_end:
            break
    if cur < win_end:
        gaps.append((cur, win_end))
    return gaps


def analysis_window_utc(merged: list[tuple["pd.Timestamp", "pd.Timestamp"]]) -> tuple["pd.Timestamp", "pd.Timestamp"]:
    """UTC window: Monday 00:00 of first week through Monday 00:00 after last week."""
    import pandas as pd

    if not merged:
        raise ValueError("empty merged")
    t0 = min(s for s, _ in merged)
    t1 = max(e for _, e in merged)
    w0 = utc_monday_start(t0)
    w1 = utc_monday_start(t1)
    return w0, w1 + pd.Timedelta(days=7)


def _get_zoneinfo(name: str):
    from zoneinfo import ZoneInfo

    n = (name or "UTC").strip()
    if n.upper() == "UTC":
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(n)
    except Exception:
        return None


def format_gap_table(
    gaps: list[tuple["pd.Timestamp", "pd.Timestamp"]],
    tz_name: str,
    min_gap_minutes: float,
    limit: int,
) -> str:
    """Human-readable gap list in local wall-clock time (IANA tz, e.g. Pacific/Auckland)."""
    import pandas as pd

    z = _get_zoneinfo(tz_name)
    if z is None:
        tz_disp = "UTC"
        z = _get_zoneinfo("UTC")
        warn = f"(Timezone {tz_name!r} unavailable; install 'tzdata' or use UTC.) "
    else:
        tz_disp = tz_name
        warn = ""
    assert z is not None

    min_sec = max(0.0, float(min_gap_minutes) * 60.0)
    filt: list = [(s, e) for s, e in gaps if (e - s).total_seconds() >= min_sec]

    lines = [
        "",
        f"Gap detail (no recording), local timezone: {tz_disp}",
        "-" * 72,
        warn
        + "Each line is a continuous period with zero session overlap (inferred from manifest).",
        f"Gaps shorter than {min_gap_minutes:g} minutes are omitted.",
        "-" * 72,
    ]
    if not filt:
        lines.append("(none in analysis window, or all shorter than min-gap)")
        lines.append("")
        return "\n".join(lines)

    shown = 0
    for s, e in filt:
        dur = (e - s).total_seconds()
        s_l = pd.Timestamp(s).tz_convert(z)
        e_l = pd.Timestamp(e).tz_convert(z)
        dh = dur / 3600.0
        tzabbr = s_l.tzname() or ""
        if s_l.normalize() == e_l.normalize():
            span = f"{s_l.strftime('%Y-%m-%d %a %H:%M')}-{e_l.strftime('%H:%M')} ({dh:.2f} h) {tzabbr}"
        else:
            span = (
                f"{s_l.strftime('%Y-%m-%d %a %H:%M')} -> {e_l.strftime('%Y-%m-%d %a %H:%M')} ({dh:.2f} h) {tzabbr}"
            )
        lines.append(span)
        shown += 1
        if shown >= limit:
            rest = len(filt) - shown
            if rest > 0:
                lines.append(f"... ({rest} more gaps not shown; use --gap-limit)")
            break
    lines.append("")
    return "\n".join(lines)


def gaps_json(gaps: list[tuple["pd.Timestamp", "pd.Timestamp"]], tz_name: str) -> list[dict]:
    import pandas as pd

    z = _get_zoneinfo(tz_name) or _get_zoneinfo("UTC")
    assert z is not None
    out = []
    for s, e in gaps:
        s_l = pd.Timestamp(s).tz_convert(z)
        e_l = pd.Timestamp(e).tz_convert(z)
        out.append(
            {
                "start_utc": pd.Timestamp(s).isoformat(),
                "end_utc": pd.Timestamp(e).isoformat(),
                "duration_h": round((e - s).total_seconds() / 3600.0, 4),
                "start_local": s_l.strftime("%Y-%m-%d %H:%M:%S"),
                "end_local": e_l.strftime("%Y-%m-%d %H:%M:%S"),
                "tz": tz_name,
            }
        )
    return out


def parse_local_slot(
    text: str,
    tz_name: str,
) -> tuple["pd.Timestamp", "pd.Timestamp"] | None:
    """
    Parse 'YYYY-MM-DD HH:MM-HH:MM' or 'YYYY-MM-DD HH:MM - HH:MM' in **local** tz (same calendar day).
    Returns UTC boundaries [start, end).
    """
    import pandas as pd
    import re

    t = (text or "").strip()
    if not t:
        return None
    m = re.match(
        r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$",
        t,
    )
    if not m:
        return None
    day, t0s, t1s = m.group(1), m.group(2), m.group(3)

    z = _get_zoneinfo(tz_name)
    if z is None:
        return None

    def _parse_hm(x: str) -> tuple[int, int]:
        a, b = x.split(":", 1)
        return int(a), int(b)

    h0, m0 = _parse_hm(t0s)
    h1, m1 = _parse_hm(t1s)
    try:
        s_naive = pd.Timestamp(f"{day} {h0:02d}:{m0:02d}:00")
        e_naive = pd.Timestamp(f"{day} {h1:02d}:{m1:02d}:00")
        s_loc = s_naive.tz_localize(z, nonexistent="shift_forward")
        e_loc = e_naive.tz_localize(z, nonexistent="shift_forward")
    except Exception:
        return None
    if pd.isna(s_loc) or pd.isna(e_loc):
        return None
    if e_loc <= s_loc:
        return None
    return s_loc.tz_convert("UTC"), e_loc.tz_convert("UTC")


def slot_status_line(
    merged: list[tuple["pd.Timestamp", "pd.Timestamp"]],
    slot_utc: tuple["pd.Timestamp", "pd.Timestamp"],
) -> str:
    """One-line RECORDING vs GAP for a UTC half-open interval."""
    import pandas as pd

    ss, se = slot_utc
    def _utc(x):
        x = pd.Timestamp(x)
        return x.tz_localize("UTC") if x.tzinfo is None else x.tz_convert("UTC")
    se = _utc(se)
    ss = _utc(ss)
    cov = overlap_seconds(merged, ss, se)
    total = max(0.0, (se - ss).total_seconds())
    if total <= 0:
        return "Slot invalid (end <= start)."
    if cov <= 0:
        return f"Slot {ss.isoformat()} -> {se.isoformat()} UTC: GAP (no recording in this model)."
    if cov >= total - 1e-6:
        return f"Slot {ss.isoformat()} -> {se.isoformat()} UTC: RECORDING for full slot (~{cov/3600:.2f} h overlap)."
    return (
        f"Slot {ss.isoformat()} -> {se.isoformat()} UTC: PARTIAL (~{cov/3600:.2f} h overlap "
        f"of {total/3600:.2f} h slot)."
    )


def utc_monday_start(ts: "pd.Timestamp") -> "pd.Timestamp":
    import pandas as pd

    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    day = t.normalize()
    dow = int(day.dayofweek)  # Monday=0
    monday = day - pd.Timedelta(days=dow)
    return monday


def weekly_recording_breakdown(
    merged: list[tuple["pd.Timestamp", "pd.Timestamp"]],
) -> "pd.DataFrame":
    """
    One row per ISO week (UTC, week starts Monday 00:00 UTC) overlapping the
    recording span. ``recording_h`` = merged interval length inside the week;
    ``gap_h`` = rest of the 168h week (no overlap with any file/session).
    """
    import pandas as pd

    if not merged:
        return pd.DataFrame(
            columns=[
                "iso_week",
                "week_start_utc",
                "recording_h",
                "gap_h",
                "pct_recording",
                "status",
            ]
        )

    t0 = min(s for s, _ in merged)
    t1 = max(e for _, e in merged)
    w0 = utc_monday_start(t0)
    w1 = utc_monday_start(t1)
    week_sec = 7 * 24 * 3600.0

    rows = []
    cur = w0
    while cur <= w1:
        wend = cur + pd.Timedelta(days=7)
        rec_sec = overlap_seconds(merged, cur, wend)
        gap_sec = max(0.0, week_sec - rec_sec)
        rec_h = rec_sec / 3600.0
        gap_h = gap_sec / 3600.0
        y, w, _ = cur.isocalendar()
        iso_week = f"{y}-W{w:02d}"
        pct = (rec_sec / week_sec * 100.0) if week_sec else 0.0
        st = "recording" if rec_sec > 0 else "no_recording"
        rows.append(
            {
                "iso_week": iso_week,
                "week_start_utc": cur,
                "recording_h": round(rec_h, 2),
                "gap_h": round(gap_h, 2),
                "pct_recording": round(pct, 1),
                "status": st,
            }
        )
        cur = wend

    return pd.DataFrame(rows)


def format_weekly_table(wdf: "pd.DataFrame") -> str:
    """Plain-text weekly recording summary (ASCII-safe)."""
    import pandas as pd

    if wdf.empty:
        return ""
    lines = [
        "",
        "Weekly recording (UTC, ISO weeks Mon-Sun)",
        "-" * 72,
        "Assumes each file = one session unless time_first_obs/time_last_obs exist.",
        "When those columns are missing, session length defaults to --session-hours (see help).",
        "-" * 72,
        f"{'ISO week':<12} {'Week start (Mon) UTC':<22} {'Rec (h)':>8} {'Gap (h)':>8} {'% rec':>7} Status",
    ]
    for _, r in wdf.iterrows():
        ws = r["week_start_utc"]
        if hasattr(ws, "strftime"):
            wss = ws.strftime("%Y-%m-%d")
        else:
            wss = str(ws)[:10]
        lines.append(
            f"{r['iso_week']:<12} {wss:<22} {r['recording_h']:>8.2f} {r['gap_h']:>8.2f} "
            f"{r['pct_recording']:>6.1f}% {r['status']}"
        )
    lines.append("")
    return "\n".join(lines)


def weekly_payload(wdf: "pd.DataFrame") -> list[dict]:
    import pandas as pd

    out = []
    for _, r in wdf.iterrows():
        d = r.to_dict()
        ws = d.get("week_start_utc")
        if hasattr(ws, "isoformat"):
            d["week_start_utc"] = ws.isoformat()
        out.append(d)
    return out


def hour_coverage_grid_utc(
    merged: list[tuple["pd.Timestamp", "pd.Timestamp"]],
    week_start_utc: "pd.Timestamp",
) -> list[list[int]]:
    """
    7 x 24 matrix (Mon..Sun row, hours 0..23 UTC): 1 if any recording overlaps that hour.
    ``week_start_utc`` must be Monday 00:00 UTC for that ISO week.
    """
    import pandas as pd

    ws = pd.Timestamp(week_start_utc)
    if ws.tzinfo is None:
        ws = ws.tz_localize("UTC")
    else:
        ws = ws.tz_convert("UTC")
    grid: list[list[int]] = []
    for d in range(7):
        row = []
        for h in range(24):
            a = ws + pd.Timedelta(days=d, hours=h)
            b = a + pd.Timedelta(hours=1)
            row.append(1 if overlap_seconds(merged, a, b) > 1e-9 else 0)
        grid.append(row)
    return grid


def format_week_hour_grid_ascii(grid: list[list[int]]) -> str:
    """7 lines, 24 chars each: # = hour with coverage, . = gap (UTC)."""
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    lines = []
    for i, name in enumerate(days):
        chars = "".join("#" if grid[i][j] else "." for j in range(24))
        lines.append(f"{name} UTC  {chars}")
    return "\n".join(lines)


def week_local_span_label(week_start_utc: "pd.Timestamp", tz_name: str) -> str:
    """One line: local start -> local end of this UTC ISO week (for context)."""
    import pandas as pd

    z = _get_zoneinfo(tz_name) or _get_zoneinfo("UTC")
    assert z is not None
    ws = pd.Timestamp(week_start_utc)
    if ws.tzinfo is None:
        ws = ws.tz_localize("UTC")
    else:
        ws = ws.tz_convert("UTC")
    we = ws + pd.Timedelta(days=7) - pd.Timedelta(seconds=1)
    a = ws.tz_convert(z)
    b = we.tz_convert(z)
    return f"Same week in {tz_name}: {a.strftime('%Y-%m-%d %a %H:%M')} -> {b.strftime('%Y-%m-%d %a %H:%M')} ({a.tzname()})"


def _norm_station_series(s: "pd.Series") -> "pd.Series":
    import pandas as pd

    txt = s.astype(str).str.strip().str.lower()
    txt = txt.replace({"nan": "unknown", "<na>": "unknown", "": "unknown", "none": "unknown"})
    txt = txt.str.replace(r"^(\d+)\.0+$", r"\1", regex=True)
    return txt


def _zip_extract_safe(zip_path: Path, dest: Path) -> None:
    """Extract zip members under ``dest`` only (Zip Slip safe)."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename:
                continue
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            target = (dest / name).resolve()
            try:
                target.relative_to(dest)
            except ValueError:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _find_manifests_dir(root: Path) -> Path:
    candidates = [root / "_manifests", root / "manifests" / "_manifests", root]
    for c in candidates:
        if (c / "files_manifest.csv").exists() and (c / "summary.json").exists():
            return c
    for p in root.rglob("files_manifest.csv"):
        if (p.parent / "summary.json").exists():
            return p.parent
    raise FileNotFoundError(
        "Could not find files_manifest.csv + summary.json under "
        f"{root} (expected _manifests/ or flat layout)."
    )


def resolve_manifests_source(path: Path) -> tuple[Path, Path | None]:
    """
    Returns (manifests_dir, temp_dir_to_delete_or_None).

    Accepts a ``.zip`` (extracted to a temp dir) or a directory tree that contains manifests.
    """
    path = path.expanduser().resolve()
    if path.is_file() and path.suffix.lower() == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="gnss_manifests_analyze_"))
        try:
            _zip_extract_safe(path, tmp)
            return _find_manifests_dir(tmp), tmp
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    if path.is_dir():
        return _find_manifests_dir(path), None
    raise FileNotFoundError(f"Not a .zip file or directory: {path}")


def load_manifest_df(manifests_dir: Path):
    import pandas as pd

    csv_path = manifests_dir / "files_manifest.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing {csv_path}")
    return pd.read_csv(csv_path, dtype={"file_name": "string", "ext": "string"}, low_memory=False)


def station_column(df) -> str:
    if "station" in df.columns:
        return "station"
    if "prefix" in df.columns:
        return "prefix"
    raise ValueError("Manifest has no station/prefix column.")


def normalize_station_query(q: str) -> str:
    q = (q or "").strip().lower()
    q = re.sub(r"^(\d+)\.0+$", r"\1", q)
    return q


def filter_one_station(df, station_query: str):
    import pandas as pd

    col = station_column(df)
    q = normalize_station_query(station_query)
    keys = _norm_station_series(df[col])
    mask = keys == q
    # Accept prefix match if column looks like 4-char prefix but user typed shorter
    if not mask.any() and len(q) >= 2:
        mask = keys.str.startswith(q)
    sub = df.loc[mask].copy()
    return sub


def summarize_station(sub) -> dict:
    import pandas as pd

    out: dict = {"rows": int(len(sub)), "total_bytes": 0}
    if sub.empty:
        return out
    if "size_bytes" in sub.columns:
        out["total_bytes"] = int(pd.to_numeric(sub["size_bytes"], errors="coerce").fillna(0).sum())
    # Time span from modified_utc
    if "modified_utc" in sub.columns:
        ts = pd.to_datetime(sub["modified_utc"], utc=True, errors="coerce")
        out["time_first_utc"] = ts.min()
        out["time_last_utc"] = ts.max()
    # Position sample
    for c in ("lat", "lon", "height_m"):
        if c in sub.columns:
            v = pd.to_numeric(sub[c], errors="coerce").dropna()
            if len(v):
                out[f"{c}_mean"] = float(v.mean())
    # Signals / constellations sample (non-null first)
    for c in ("constellations", "signals"):
        if c in sub.columns:
            nz = sub[c].dropna().astype(str)
            nz = nz[nz.str.len() > 0]
            if len(nz):
                out[f"{c}_sample"] = nz.iloc[0][:500]
    return out


def format_report(
    station_query: str,
    manifests_dir: Path,
    sub,
    summary: dict,
    summary_json: dict | None,
    *,
    session_hours: float = 1.0,
    merged_recording_h: float | None = None,
) -> str:
    lines = [
        "GNSS manifest - single station analysis",
        "=" * 60,
        f"Manifests dir : {manifests_dir}",
        f"Station filter: {station_query!r} (normalized match)",
        f"Matching rows : {summary['rows']:,}",
        f"Total size    : {summary['total_bytes'] / (1024 ** 2):.2f} MB",
    ]
    lines.append(
        f"Session model : {session_hours} h per file when manifest has no time_first_obs/time_last_obs"
    )
    if merged_recording_h is not None:
        lines.append(f"Merged record : ~{merged_recording_h:.2f} h UTC (union of sessions, overlaps counted once)")
    if summary.get("time_first_utc") is not None and str(summary["time_first_utc"]) != "NaT":
        lines.append(f"File mtime span : {summary['time_first_utc']} -> {summary['time_last_utc']} (from modified_utc)")
    if summary_json:
        lines.append(f"Dataset root   : {summary_json.get('root', summary_json.get('manifests_dir', ''))}")
    for k in ("lat_mean", "lon_mean", "height_m_mean"):
        if k in summary:
            lines.append(f"{k:14}: {summary[k]:.6f}")
    for k in ("constellations_sample", "signals_sample"):
        if k in summary:
            lines.append(f"{k.replace('_sample','')}: {summary[k]}")
    if not sub.empty and "file_name" in sub.columns:
        lines.append("")
        lines.append("Sample files (up to 12):")
        for fn in sub["file_name"].astype(str).head(12):
            lines.append(f"  - {fn}")
        if len(sub) > 12:
            lines.append(f"  ... ({len(sub) - 12} more)")
    lines.append("")
    return "\n".join(lines)


def run_cli(
    manifests_path: Path,
    station: str,
    out_csv: Path | None,
    out_weekly: Path | None,
    as_json: bool,
    session_hours: float,
    *,
    tz_name: str = "UTC",
    show_gaps: bool = True,
    min_gap_mins: float = 1.0,
    gap_limit: int = 300,
    slot_local: str | None = None,
) -> int:
    import pandas as pd

    md, tmp = resolve_manifests_source(manifests_path)
    try:
        df = load_manifest_df(md)
        summ_path = md / "summary.json"
        summary_json = json.loads(summ_path.read_text(encoding="utf-8")) if summ_path.exists() else None
        sub = filter_one_station(df, station)
        summary = summarize_station(sub)

        merged: list = []
        merged_h: float | None = None
        wdf = weekly_recording_breakdown([])
        if not sub.empty:
            merged = merge_intervals(recording_intervals_utc(sub, session_hours))
            merged_h = sum((e - s).total_seconds() for s, e in merged) / 3600.0
            wdf = weekly_recording_breakdown(merged)

        raw_gaps: list = []
        if merged:
            ws, we = analysis_window_utc(merged)
            raw_gaps = gaps_in_window(merged, ws, we)

        slot_line = ""
        if slot_local and merged:
            slot = parse_local_slot(slot_local, tz_name)
            if slot is None:
                slot_line = (
                    f"\n--slot-local: could not parse {slot_local!r} "
                    f"(use e.g. '2026-04-27 15:00-16:00' with --tz {tz_name})\n"
                )
            else:
                slot_line = "\n" + slot_status_line(merged, slot) + "\n"
        elif slot_local and not merged:
            slot_line = "\n--slot-local ignored (no merged sessions for this station).\n"

        if as_json:
            payload = {
                "manifests_dir": str(md),
                "station_query": station,
                "session_hours_model": float(session_hours),
                "timezone": tz_name,
                "rows": summary["rows"],
                "total_bytes": summary["total_bytes"],
                "merged_recording_hours_approx": merged_h,
                "weekly": weekly_payload(wdf),
                "gaps": gaps_json(raw_gaps, tz_name),
            }
            for k, v in summary.items():
                if k not in payload and k not in ("time_first_utc", "time_last_utc"):
                    try:
                        payload[k] = float(v) if isinstance(v, float) else v
                    except Exception:
                        payload[k] = str(v)
            if summary.get("time_first_utc") is not None:
                payload["time_first_utc"] = str(summary["time_first_utc"])
                payload["time_last_utc"] = str(summary["time_last_utc"])
            if slot_local and merged:
                ps = parse_local_slot(slot_local, tz_name)
                payload["slot_check"] = (
                    None
                    if ps is None
                    else {
                        "query_local": slot_local,
                        "overlap_seconds": overlap_seconds(merged, ps[0], ps[1]),
                        "slot_seconds": (ps[1] - ps[0]).total_seconds(),
                    }
                )
            print(json.dumps(payload, indent=2))
        else:
            gap_block = ""
            if show_gaps and merged:
                gap_block = format_gap_table(raw_gaps, tz_name, min_gap_mins, gap_limit)
            print(
                format_report(
                    station,
                    md,
                    sub,
                    summary,
                    summary_json,
                    session_hours=session_hours,
                    merged_recording_h=merged_h,
                )
                + format_weekly_table(wdf)
                + gap_block
                + slot_line
            )
            if sub.empty:
                print(
                    f"No rows for station {station!r}. Unique IDs (first 40): "
                    f"{sorted(_norm_station_series(df[station_column(df)]).unique())[:40]}",
                    file=sys.stderr,
                )
                return 2
        if out_csv and not sub.empty:
            sub.to_csv(out_csv, index=False, encoding="utf-8")
            if not as_json:
                print(f"Wrote filtered CSV: {out_csv}")
        if out_weekly and not wdf.empty:
            wdf.to_csv(out_weekly, index=False, encoding="utf-8")
            if not as_json:
                print(f"Wrote weekly CSV: {out_weekly}")
        return 0 if not sub.empty else 2
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def run_gui() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except Exception as e:
        print(f"[FAIL] GUI requires tkinter: {e}", file=sys.stderr)
        print("Use: python analyze_station_manifest.py <manifests.zip|folder> <STATION>", file=sys.stderr)
        return 1

    root = tk.Tk()
    root.title("Analyze one station (offline manifests)")
    root.geometry("900x760")

    frm = ttk.Frame(root, padding=8)
    frm.pack(fill=tk.BOTH, expand=True)

    src_var = tk.StringVar(value="")
    station_var = tk.StringVar(value="")

    ttk.Label(frm, text="1) Choose manifests zip or folder (must contain files_manifest.csv + summary.json).").pack(
        anchor="w"
    )
    bf = ttk.Frame(frm)
    bf.pack(fill=tk.X, pady=4)

    manifest_root: dict[str, Path | None] = {"md": None}
    df_holder: dict = {"df": None}
    tmp_holder: dict[str, Path | None] = {"tmp": None}

    def cleanup_tmp() -> None:
        t = tmp_holder["tmp"]
        if t:
            shutil.rmtree(t, ignore_errors=True)
            tmp_holder["tmp"] = None

    def load_zip() -> None:
        cleanup_tmp()
        p = filedialog.askopenfilename(
            title="Select manifests zip",
            filetypes=[("Zip archive", "*.zip"), ("All files", "*.*")],
        )
        if not p:
            return
        try:
            md, tmp = resolve_manifests_source(Path(p))
            tmp_holder["tmp"] = tmp
            manifest_root["md"] = md
            df_holder["df"] = load_manifest_df(md)
            src_var.set(p)
            fill_stations()
            messagebox.showinfo("Loaded", f"Loaded {len(df_holder['df']):,} manifest rows.")
        except Exception as e:
            cleanup_tmp()
            messagebox.showerror("Error", str(e))

    def load_folder() -> None:
        cleanup_tmp()
        p = filedialog.askdirectory(title="Select folder containing manifests (_manifests or csv)")
        if not p:
            return
        try:
            md, tmp = resolve_manifests_source(Path(p))
            tmp_holder["tmp"] = tmp
            manifest_root["md"] = md
            df_holder["df"] = load_manifest_df(md)
            src_var.set(p)
            fill_stations()
            messagebox.showinfo("Loaded", f"Loaded {len(df_holder['df']):,} manifest rows.")
        except Exception as e:
            cleanup_tmp()
            messagebox.showerror("Error", str(e))

    def fill_stations() -> None:
        combo["values"] = ()
        df = df_holder["df"]
        if df is None or df.empty:
            return
        col = station_column(df)
        opts = sorted(_norm_station_series(df[col]).unique().tolist())
        combo["values"] = tuple(opts[:2000])
        if opts:
            station_var.set(opts[0])

    ttk.Button(bf, text="Open zip…", command=load_zip).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(bf, text="Open folder…", command=load_folder).pack(side=tk.LEFT)

    ttk.Label(frm, textvariable=src_var, wraplength=680).pack(anchor="w", pady=4)

    ttk.Label(frm, text="2) Pick station ID:").pack(anchor="w", pady=(8, 0))
    combo = ttk.Combobox(frm, textvariable=station_var, width=48)
    combo.pack(anchor="w", pady=4)

    session_var = tk.StringVar(value="1.0")
    sf = ttk.Frame(frm)
    sf.pack(anchor="w", pady=2)
    ttk.Label(sf, text="Hours per file (when manifest has no obs time window):").pack(side=tk.LEFT, padx=(0, 8))
    ttk.Entry(sf, textvariable=session_var, width=8).pack(side=tk.LEFT)
    ttk.Label(sf, text="Typical hourly T02: 1. See --session-hours in CLI help.").pack(side=tk.LEFT, padx=(8, 0))

    tz_var = tk.StringVar(value="UTC")
    tf = ttk.Frame(frm)
    tf.pack(anchor="w", pady=2)
    ttk.Label(tf, text="Timezone for gap list (IANA, e.g. Pacific/Auckland):").pack(side=tk.LEFT, padx=(0, 8))
    ttk.Entry(tf, textvariable=tz_var, width=28).pack(side=tk.LEFT)

    slot_var = tk.StringVar(value="")
    pf = ttk.Frame(frm)
    pf.pack(anchor="w", pady=2)
    ttk.Label(pf, text="Optional slot check (local): ").pack(side=tk.LEFT)
    ttk.Label(pf, text="YYYY-MM-DD HH:MM-HH:MM", foreground="gray").pack(side=tk.LEFT, padx=(0, 8))
    ttk.Entry(pf, textvariable=slot_var, width=36).pack(side=tk.LEFT)

    viz: dict = {"merged": [], "wdf": None, "tz": "UTC"}
    week_idx_var = tk.IntVar(value=0)

    row_btn = ttk.Frame(frm)
    row_btn.pack(fill=tk.X, pady=(10, 4))

    week_lf = ttk.LabelFrame(frm, text="Week-by-week coverage — use buttons to flip weeks")
    week_lf.pack(fill=tk.X, pady=4)

    nav = ttk.Frame(week_lf)
    nav.pack(fill=tk.X)
    btn_prev = ttk.Button(nav, text="<< Previous week", width=20)
    btn_prev.pack(side=tk.LEFT, padx=(0, 8))
    week_head_var = tk.StringVar(value="Run Analyze to build weekly coverage")
    ttk.Label(nav, textvariable=week_head_var, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=4)
    btn_next = ttk.Button(nav, text="Next week >>", width=20)
    btn_next.pack(side=tk.RIGHT, padx=(8, 0))

    week_stats_var = tk.StringVar(value="")
    ttk.Label(week_lf, textvariable=week_stats_var, wraplength=780, justify=tk.LEFT).pack(anchor="w", pady=2)

    week_tz_note = tk.StringVar(value="")
    ttk.Label(week_lf, textvariable=week_tz_note, foreground="gray", wraplength=780, justify=tk.LEFT).pack(anchor="w")

    week_grid = tk.Text(week_lf, height=9, width=92, font=("Consolas", 9), undo=False, relief=tk.FLAT, bg="#f5f5f5")
    week_grid.pack(fill=tk.X, pady=4)

    ttk.Label(
        week_lf,
        text="Legend: rows = Mon-Sun (UTC). Columns = hour 0-23 UTC.  # = coverage, . = gap. "
        "Uses same session model as the report below.",
        wraplength=780,
    ).pack(anchor="w")

    txt = scrolledtext.ScrolledText(frm, height=14, wrap=tk.WORD)
    txt.pack(fill=tk.BOTH, expand=True, pady=8)

    def refresh_week_view() -> None:
        wdf = viz.get("wdf")
        merged = viz.get("merged") or []
        tz_u = str(viz.get("tz") or "UTC")
        if wdf is None or getattr(wdf, "empty", True):
            week_head_var.set("No weekly data")
            week_stats_var.set("Analyze a station with at least one manifest row.")
            week_tz_note.set("")
            week_grid.delete("1.0", tk.END)
            btn_prev.state(["disabled"])
            btn_next.state(["disabled"])
            return
        n = int(len(wdf))
        i = int(week_idx_var.get())
        i = max(0, min(i, n - 1))
        week_idx_var.set(i)
        row = wdf.iloc[i]
        ws = row["week_start_utc"]
        iso = row["iso_week"]
        rec = float(row["recording_h"])
        gap = float(row["gap_h"])
        pct = float(row["pct_recording"])
        st = str(row["status"])
        week_head_var.set(f"Week {i + 1} of {n}  ·  {iso}  ·  UTC week (Mon 00:00)")
        week_stats_var.set(
            f"Recording (approx): {rec:.2f} h  |  Gap: {gap:.2f} h  |  Of 168 h week: {pct:.1f}%  |  {st}"
        )
        try:
            week_tz_note.set(week_local_span_label(ws, tz_u))
        except Exception:
            week_tz_note.set(f"(Could not format local span for {tz_u})")

        g = hour_coverage_grid_utc(merged, ws) if merged else [[0] * 24 for _ in range(7)]
        week_grid.delete("1.0", tk.END)
        week_grid.insert(tk.END, format_week_hour_grid_ascii(g) + "\n")

        if i <= 0:
            btn_prev.state(["disabled"])
        else:
            btn_prev.state(["!disabled"])
        if i >= n - 1:
            btn_next.state(["disabled"])
        else:
            btn_next.state(["!disabled"])

    def week_prev() -> None:
        week_idx_var.set(max(0, int(week_idx_var.get()) - 1))
        refresh_week_view()

    def week_next() -> None:
        wdf = viz.get("wdf")
        if wdf is None or wdf.empty:
            return
        week_idx_var.set(min(len(wdf) - 1, int(week_idx_var.get()) + 1))
        refresh_week_view()

    btn_prev.config(command=week_prev)
    btn_next.config(command=week_next)
    btn_prev.state(["disabled"])
    btn_next.state(["disabled"])

    def analyze() -> None:
        txt.delete("1.0", tk.END)
        df = df_holder["df"]
        if df is None:
            messagebox.showwarning("Nothing loaded", "Open a zip or folder first.")
            return
        st = station_var.get().strip()
        if not st:
            messagebox.showwarning("Station", "Choose or type a station ID.")
            return
        try:
            session_hours = float((session_var.get() or "1").strip())
        except ValueError:
            messagebox.showerror("Session hours", "Enter a number (e.g. 1 or 24).")
            return
        if session_hours <= 0:
            messagebox.showerror("Session hours", "Must be positive.")
            return
        tz_use = (tz_var.get() or "UTC").strip() or "UTC"
        md = manifest_root["md"]
        assert md is not None
        summ_path = md / "summary.json"
        summary_json = json.loads(summ_path.read_text(encoding="utf-8")) if summ_path.exists() else None
        sub = filter_one_station(df, st)
        summary = summarize_station(sub)
        merged_h = None
        gap_txt = ""
        slot_txt = ""
        merged: list = []
        wdf = weekly_recording_breakdown([])
        if not sub.empty:
            merged = merge_intervals(recording_intervals_utc(sub, session_hours))
            merged_h = sum((e - s).total_seconds() for s, e in merged) / 3600.0
            wdf = weekly_recording_breakdown(merged)
            if merged:
                ws, we = analysis_window_utc(merged)
                raw_gaps = gaps_in_window(merged, ws, we)
                gap_txt = format_gap_table(raw_gaps, tz_use, 1.0, 300)
        sl = (slot_var.get() or "").strip()
        if sl and merged:
            ps = parse_local_slot(sl, tz_use)
            if ps is None:
                slot_txt = (
                    f"\nSlot: could not parse {sl!r} (use e.g. 2026-04-27 15:00-16:00)\n"
                )
            else:
                slot_txt = "\n" + slot_status_line(merged, ps) + "\n"
        elif sl and not merged:
            slot_txt = "\nSlot check skipped (no sessions).\n"
        weekly_txt = format_weekly_table(wdf)
        report = (
            format_report(
                st,
                md,
                sub,
                summary,
                summary_json,
                session_hours=session_hours,
                merged_recording_h=merged_h,
            )
            + weekly_txt
            + gap_txt
            + slot_txt
        )
        txt.insert(tk.END, report)
        viz["merged"] = merged
        viz["wdf"] = wdf
        viz["tz"] = tz_use
        week_idx_var.set(0)
        refresh_week_view()
        if sub.empty:
            messagebox.showinfo("No rows", f"No manifest rows matched {st!r}.")

    ttk.Button(row_btn, text="Analyze station", command=analyze).pack(side=tk.LEFT)

    def on_close() -> None:
        cleanup_tmp()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    cleanup_tmp()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(description="Analyze one station from offline scan manifests.")
    ap.add_argument("manifests", nargs="?", help="Path to manifests .zip or folder")
    ap.add_argument("station", nargs="?", help="Station ID / prefix (case-insensitive)")
    ap.add_argument("--folder", dest="manifests_alt", help="Explicit manifests folder or zip (same as positional)")
    ap.add_argument("--out", type=Path, help="Write filtered rows to this CSV path")
    ap.add_argument(
        "--out-weekly",
        type=Path,
        help="Write weekly recording vs gap table (CSV)",
    )
    ap.add_argument(
        "--session-hours",
        type=float,
        default=1.0,
        help=(
            "When manifest rows have no time_first_obs/time_last_obs, treat each file as "
            "this many hours of recording starting at inferred event time (default: 1). "
            "Use 24 for one file per day."
        ),
    )
    ap.add_argument("--json", action="store_true", help="Print machine-readable JSON to stdout")
    ap.add_argument("--gui", action="store_true", help="Open file picker + station list (Windows-friendly)")
    ap.add_argument(
        "--tz",
        default="UTC",
        help="IANA timezone for gap listing and --slot-local (e.g. Pacific/Auckland). Default: UTC",
    )
    ap.add_argument(
        "--no-gaps",
        action="store_true",
        help="Do not print the detailed list of gap time ranges.",
    )
    ap.add_argument(
        "--min-gap-mins",
        type=float,
        default=1.0,
        help="Omit gaps shorter than this many minutes from the gap list (default: 1).",
    )
    ap.add_argument(
        "--gap-limit",
        type=int,
        default=300,
        help="Maximum number of gap lines to print (default: 300).",
    )
    ap.add_argument(
        "--slot-local",
        metavar="TEXT",
        default=None,
        help=(
            "Check one local-time window in --tz, same calendar day, format: "
            "'YYYY-MM-DD HH:MM-HH:MM' (example: Sunday 3-4pm in Auckland: "
            "'2026-04-27 15:00-16:00' with --tz Pacific/Auckland)."
        ),
    )
    args = ap.parse_args(argv)

    if args.gui or (not args.manifests and not args.manifests_alt):
        return run_gui()

    src = args.manifests_alt or args.manifests
    if not src or not args.station:
        ap.print_help()
        print("\nProvide both MANIFESTS and STATION, or use --gui.", file=sys.stderr)
        return 2

    return run_cli(
        Path(src),
        args.station,
        args.out,
        args.out_weekly,
        args.json,
        float(args.session_hours),
        tz_name=str(args.tz or "UTC").strip() or "UTC",
        show_gaps=not bool(args.no_gaps),
        min_gap_mins=float(args.min_gap_mins),
        gap_limit=int(args.gap_limit),
        slot_local=args.slot_local,
    )


if __name__ == "__main__":
    raise SystemExit(main())
