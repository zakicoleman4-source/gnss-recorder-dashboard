from __future__ import annotations

import bz2
import gc
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

# Serialises pipeline runs within a process — prevents Streamlit rerun races
# from opening two write connections to the same DB simultaneously.
_PIPELINE_LOCK = threading.Lock()


TO_EXTS = {".to2", ".t02", ".to4", ".t04", ".t00", ".t01"}
# Match station prefix before an embedded year (19xx/20xx) or a 3-digit DOY+hour-letter.
# Handles alpha stations (AHTI), numeric VRS stations (2406), names with _ (AB_C),
# and RINEX2-style names (INVK119a, AB_C119a_1).
STATION_RE = re.compile(r"^([A-Za-z0-9_]{3,9})(?=(?:19|20)\d{2}|\d{3}[a-xA-X])", re.IGNORECASE)
# Fallback: client files may have reliable 3-4 char prefix but unreliable date suffix.
# Allows _ and . inside code (e.g. AH_I, A.HTI); strips trailing _ . after match.
_STATION_PREFIX_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.]{2,3})", re.IGNORECASE)

_THIS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class PipelineConfig:
    data_root: Path
    cache_dir: Path
    convbin_path: Optional[Path] = None
    runpkr00_path: Optional[Path] = None
    rinex_ver: str = "3.04"
    max_files_per_station: Optional[int] = None
    stop_after_success_per_station: bool = False
    probe_max_total_files: int = 200_000
    convert_cmd_template: Optional[str] = None
    ctr_path: Optional[Path] = None             # convertToRinex_cli.exe for RT27/Alloy
    ctr_first: bool = False                     # skip runpkr00+convbin, go straight to CTR
    station_coords_path: Optional[Path] = None  # CSV: station,lat,lon,height_m
    rnx2rtkp_path: Optional[Path] = None        # rnx2rtkp.exe for SPP coord solve


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_to_files(root: Path, exclude_dirs: Optional[Iterable[Path]] = None) -> Iterable[Path]:
    """
    Walk `root` and yield TO2/T02/TO4/T04 files (case-insensitive).
    Tolerates bad symlinks / permission errors per file.
    Skips excluded subtrees in-place.
    """
    excl_resolved: list[Path] = []
    for d in exclude_dirs or []:
        try:
            excl_resolved.append(Path(d).resolve())
        except Exception:
            continue

    def _is_excluded(dirpath_resolved: Path) -> bool:
        for ed in excl_resolved:
            try:
                dirpath_resolved.relative_to(ed)
                return True
            except Exception:
                continue
        return False

    try:
        walker = os.walk(root, onerror=lambda _e: None, followlinks=False)
    except Exception:
        return

    for dirpath, dirnames, filenames in walker:
        try:
            dp_resolved = Path(dirpath).resolve()
        except Exception:
            dp_resolved = Path(dirpath)
        if excl_resolved:
            keep = []
            for d in list(dirnames):
                try:
                    if _is_excluded((dp_resolved / d).resolve()):
                        continue
                except Exception:
                    pass
                keep.append(d)
            dirnames[:] = keep
        if _is_excluded(dp_resolved):
            continue
        for fn in filenames:
            try:
                p = Path(dirpath) / fn
                if p.suffix.lower() in TO_EXTS:
                    yield p
            except Exception:
                continue


def _pick_probe_files(
    root: Path,
    max_files_per_station: int,
    max_total_files: int,
    exclude_dirs: Optional[Iterable[Path]] = None,
) -> list[Path]:
    picked: dict[str, list[Path]] = {}
    examined = 0
    for p in _iter_to_files(root, exclude_dirs=exclude_dirs):
        examined += 1
        st = _station_from_filename(p.name)
        lst = picked.get(st)
        if lst is None:
            picked[st] = [p]
        elif len(lst) < max_files_per_station:
            lst.append(p)
        if examined >= max_total_files:
            break

    out: list[Path] = []
    for _st, lst in picked.items():
        out.extend(lst[:max_files_per_station])
    return out


def _station_from_filename(name: str) -> str:
    m = STATION_RE.match(name)
    if m:
        # Strip trailing _ . (separator chars accidentally pulled in by greedy match).
        code = m.group(1).upper().rstrip("_.")
        if len(code) >= 3:
            return code
    # Fallback: first 3-4 chars; strip trailing _ . (separators, not part of code)
    m = _STATION_PREFIX_RE.match(name)
    if m:
        code = m.group(1).upper().rstrip("_.")
        if len(code) >= 3:
            return code
    return "UNKNOWN"


# ── Filename timestamp parsing ────────────────────────────────────────────────
# Format 1: {STATION}{YYYY}{MM}{DD}{HH}{MM}[SS][a].T02  e.g. AHTI202603010100a.T02
_FN_DT_MMDD = re.compile(
    r"[A-Za-z0-9]{0,9}"
    r"((?:19|20)\d{2})"
    r"(0[1-9]|1[0-2])"
    r"(0[1-9]|[12]\d|3[01])"
    r"([01]\d|2[0-3])"
    r"\d{2}"
    r"(?:\d{2})?"
    r"[a-zA-Z]?"
    r"(?=\.)",
    re.IGNORECASE,
)
# Format 2: {STATION}{YYYY}{DOY}{HH}{MM}[SS][a].T02  e.g. AHTI2026060010a.T02
_FN_DT_DOY = re.compile(
    r"[A-Za-z0-9]{0,9}"
    r"((?:19|20)\d{2})"
    r"(00[1-9]|0[1-9]\d|[12]\d{2}|3[0-5]\d|36[0-6])"
    r"([01]\d|2[0-3])"
    r"\d{2}"
    r"(?:\d{2})?"
    r"[a-zA-Z]?"
    r"(?=\.)",
    re.IGNORECASE,
)
# Format 3: {STATION}{DOY}{h}[_N|<other>].T02  RINEX2-style, hour = letter a-x (a=0..x=23)
# Station may include _ (AB_C). After the hour letter the name may carry a
# duplicate suffix (_1, _2) or any other trailing token before the extension.
# No year in filename — year must come from directory path.
# e.g. INVK119a.T02, AB_C119a_1.T02, AHTI060x_dup.T02
_FN_DT_RINEX2 = re.compile(
    r"^[A-Za-z0-9_]{3,9}"
    r"(00[1-9]|0[1-9]\d|[12]\d{2}|3[0-5]\d|36[0-6])"
    r"([a-x])"
    r"(?=[._]|$)",
    re.IGNORECASE,
)


def _year_from_path(path: Path) -> Optional[int]:
    """Scan parent directory names for a 4-digit year (19xx/20xx)."""
    for part in reversed(path.parts[:-1]):
        if re.fullmatch(r"(?:19|20)\d{2}", part):
            return int(part)
    return None


def _parse_filename_dt(name: str, path: Optional[Path] = None) -> tuple[Optional[str], Optional[int]]:
    """Return (iso_date, hour) from Trimble filename, or (None, None).
    Pass path to enable RINEX2 letter-hour format (year extracted from parent dirs).
    """
    import datetime as _dt

    m = _FN_DT_MMDD.search(name)
    if m:
        try:
            y, mo, d, h = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            return _dt.date(y, mo, d).isoformat(), h
        except (ValueError, OverflowError):
            pass

    m = _FN_DT_DOY.search(name)
    if m:
        try:
            y, doy, h = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return (_dt.date(y, 1, 1) + _dt.timedelta(days=doy - 1)).isoformat(), h
        except (ValueError, OverflowError):
            pass

    # RINEX2 letter-hour: DOY + letter (a=0..x=23), year from directory path
    m = _FN_DT_RINEX2.match(name)
    if m:
        try:
            doy = int(m.group(1))
            hour = ord(m.group(2).lower()) - ord('a')
            year = _year_from_path(path) if path else None
            if year and 0 <= hour <= 23 and 1 <= doy <= 366:
                return (_dt.date(year, 1, 1) + _dt.timedelta(days=doy - 1)).isoformat(), hour
        except (ValueError, OverflowError):
            pass

    return None, None


# ── T02 binary header probe ──────────────────────────────────────────────────
# T02/T04 files are TBC archive containers wrapping bzip2-compressed metadata
# blocks + binary measurement records. Metadata block normally near the start
# (~byte 21) but can shift between receiver firmware versions, so scan the
# whole file (capped). Also scans raw bytes outside bzip2 streams to catch
# formats that embed plain-text headers.

# Hard upper bound on bytes scanned per file. Most T02s are 200-400 KB; 8 MB
# covers 24-h files and pathological cases without blowing memory.
_T02_PROBE_MAX_BYTES = 8 * 1024 * 1024

# Field-value terminator: stop on any control byte (0x00-0x1F — which Trimble
# uses as length/separator bytes between adjacent fields), comma, semicolon,
# or the start of the next CamelCase key. Used in every key regex so adjacent
# fields don't run together.
_FIELD_END = r"(?=[\x00-\x1f]|,|;|[A-Z][a-z]+[A-Z]|$)"

_T02_RE_START    = re.compile(r"(?:SessionStart(?:Utc)?|StartTime|FirstObs|session_start)\s*[=:]\s*([0-9T:.\-Z+ ]{10,30}?)" + _FIELD_END, re.IGNORECASE)
_T02_RE_END      = re.compile(r"(?:SessionEnd(?:Utc)?|EndTime|LastObs|session_end)\s*[=:]\s*([0-9T:.\-Z+ ]{10,30}?)" + _FIELD_END, re.IGNORECASE)
# SessionMeasIntervalMsecs (GeoNet T02 format) stores interval in milliseconds.
# Plain Interval/SampleRate/Rate stores it in seconds.
_T02_RE_INTERVAL_MS = re.compile(r"(?:SessionMeasIntervalMsecs|MeasIntervalMsecs|IntervalMsecs)\s*[=:]\s*([0-9]+)", re.IGNORECASE)
_T02_RE_INTERVAL = re.compile(r"(?:MeasInterval|SampleInterval|SampleRate|Interval|Rate)\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
# Marker/station name. Char class excludes uppercase to avoid running into
# next CamelCase field; values like "2406", "HIKB", "site_001" still match.
_T02_RE_MARKER   = re.compile(r"(?:RefStationName|RefStationCode|MarkerName|SiteName|StationName|StationId|station_id|marker)\s*[=:]\s*([A-Za-z0-9][A-Za-z0-9_\-]{1,18}?)" + _FIELD_END, re.IGNORECASE)
_T02_RE_DT_ANY   = re.compile(r"((?:19|20)\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)")
# Compact datetime: YYYYMMDDHHMMSS (some Trimble firmware writes this form)
_T02_RE_DT_COMPACT = re.compile(r"(?<![0-9])((?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:[01]\d|2[0-3])[0-5]\d[0-5]\d)(?![0-9])")
# Trimble Alloy VRS: RefStationLLH:lat,lon,height  (decimal degrees, metres)
_T02_RE_LLH      = re.compile(r"(?:RefStationLLH|StationLLH|MarkerLLH)\s*[=:]\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", re.IGNORECASE)
# Trimble Alloy filePath:/Internal/YYYYMM/DD/<hour-letter>/...
# Universal in GeoNet T02s and survives even when filename is garbled.
_T02_RE_FILEPATH = re.compile(r"filePath\s*[=:]\s*/[^/]*?/(\d{4})(\d{2})/(\d{2})/([a-x])/", re.IGNORECASE)
# Bare ECEF XYZ block (some Trimble firmware records position pre-RINEX)
_T02_RE_ECEF     = re.compile(r"(?:RefStationXYZ|StationXYZ|ApproxXYZ)\s*[=:]\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", re.IGNORECASE)
# Receiver model — used to identify Alloy/RT27 receivers up-front so we can
# skip the (slow, futile) runpkr00→convbin attempt for them.
# Sample observed field: `ReceiverId:162,Trimble Alloy,6016R40025`
_T02_RE_RX_MODEL = re.compile(r"ReceiverId\s*[:=]\s*\d+\s*,\s*([^,\x00\r\n]{2,40})", re.IGNORECASE)

# Receivers known to record in RT27 / CMRx (records 75/99/114/etc.) — no open
# source decoder for these. Match is substring + case-insensitive.
_RT27_RECEIVER_MARKERS = ("alloy", "netr9 ti-m", "r12i", "r12 receiver")


def _validate_marker(s: Optional[str]) -> Optional[str]:
    """Reject obvious field-bleed captures (e.g. '2406RefStationCode')."""
    if not s:
        return None
    s = s.strip()
    # Reject if it looks like it includes an adjacent CamelCase field name
    if re.search(r"(?:Ref|Marker|Station|Site|Antenna|Receiver|Session)[A-Z]", s):
        return None
    # Reject if it ends with a known field-key suffix
    if re.search(r"(?:Name|Code|Id|Type|Number)$", s) and len(s) > 4:
        return None
    if len(s) < 2 or len(s) > 20:
        return None
    return s


def _absorb_text(text: str, result: dict) -> None:
    """Run all probe regexes against `text` and fill missing fields in `result`."""
    if not text:
        return
    if result["session_start"] is None:
        m = _T02_RE_START.search(text)
        if m:
            result["session_start"] = m.group(1).strip()
    if result["session_end"] is None:
        m = _T02_RE_END.search(text)
        if m:
            result["session_end"] = m.group(1).strip()
    if result["interval_s"] is None:
        m = _T02_RE_INTERVAL_MS.search(text)
        if m:
            try:
                v = int(m.group(1)) / 1000.0
                if 0.0 < v <= 3600.0:
                    result["interval_s"] = str(v)
            except (ValueError, ZeroDivisionError):
                pass
    if result["interval_s"] is None:
        m = _T02_RE_INTERVAL.search(text)
        if m:
            try:
                v = float(m.group(1))
                if 0.0 < v <= 3600.0:
                    result["interval_s"] = m.group(1).strip()
            except ValueError:
                pass
    if result["marker_name"] is None:
        m = _T02_RE_MARKER.search(text)
        if m:
            vm = _validate_marker(m.group(1))
            if vm:
                result["marker_name"] = vm
    if result["lat"] is None:
        m = _T02_RE_LLH.search(text)
        if m:
            try:
                lat, lon, h = float(m.group(1)), float(m.group(2)), float(m.group(3))
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    result["lat"], result["lon"], result["height_m"] = lat, lon, h
            except ValueError:
                pass
    if result["receiver_model"] is None:
        m = _T02_RE_RX_MODEL.search(text)
        if m:
            v = m.group(1).strip()
            # Reject obvious bleed into next field
            if v and not re.search(r"[A-Z][a-z]+[A-Z][a-z]", v):
                result["receiver_model"] = v
    # filePath gives definitive date+hour even when filename is unparseable
    if result["filepath_date"] is None:
        m = _T02_RE_FILEPATH.search(text)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                hour = ord(m.group(4).lower()) - ord('a')
                if 1 <= mo <= 12 and 1 <= d <= 31 and 0 <= hour <= 23:
                    import datetime as _dt
                    result["filepath_date"] = _dt.date(y, mo, d).isoformat()
                    result["filepath_hour"] = hour
            except (ValueError, OverflowError):
                pass
    # Fallback: any ISO-ish datetime
    if result["session_start"] is None:
        m = _T02_RE_DT_ANY.search(text)
        if m:
            result["session_start"] = m.group(1)
    # Fallback: compact YYYYMMDDHHMMSS
    if result["session_start"] is None:
        m = _T02_RE_DT_COMPACT.search(text)
        if m:
            s = m.group(1)
            result["session_start"] = (
                f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:{s[12:14]}"
            )


def _probe_t02_header(path: Path) -> dict:
    """
    Scan a T02/T04 binary for embedded metadata. Multi-strategy:
      1. Read up to _T02_PROBE_MAX_BYTES of the file
      2. Find every BZh bzip2 stream, decompress each, regex-scan its text
      3. Also regex-scan the raw bytes for plain-text headers (some legacy
         Trimble formats embed unencoded ASCII metadata)
      4. Accumulate hits across all blocks; never break early

    Never raises. Works regardless of filename naming convention.
    """
    result: dict = {
        "session_start": None, "session_end": None,
        "interval_s": None,     "marker_name": None,
        "lat": None, "lon": None, "height_m": None,
        "filepath_date": None,  "filepath_hour": None,
        "receiver_model": None,
    }
    try:
        size = path.stat().st_size
    except OSError:
        return result
    if size <= 0:
        return result

    try:
        with path.open("rb") as fh:
            blob = fh.read(min(size, _T02_PROBE_MAX_BYTES))
    except OSError:
        return result

    # Strategy 1: scan raw bytes (first 256 KB only — plain-text legacy headers
    # are always near the start, and decoding multi-MB blobs is wasted CPU).
    try:
        raw_text = blob[:262144].decode("latin-1", errors="ignore")
    except Exception:
        raw_text = ""
    _absorb_text(raw_text, result)

    def _have_enough() -> bool:
        """Stop scanning once we have date+hour+coords+interval+marker+model."""
        date_ok = (result["filepath_date"] is not None) or (
            result["session_start"] is not None
        )
        return (
            date_ok
            and result["interval_s"]     is not None
            and result["marker_name"]    is not None
            and result["lat"]            is not None
            and result["receiver_model"] is not None
        )

    if _have_enough():
        return result

    # Strategy 2: decompress BZh streams (cap at 8 to bound worst case;
    # metadata is in block 0 for every Trimble format we've seen — extra
    # blocks are pure measurement records).
    MAGIC = b"BZh"
    blocks_scanned = 0
    offset = 0
    while blocks_scanned < 8:
        pos = blob.find(MAGIC, offset)
        if pos < 0:
            break
        offset = pos + 1  # advance now so any `continue` doesn't loop forever
        blocks_scanned += 1
        try:
            d = bz2.BZ2Decompressor()
            dec = d.decompress(blob[pos:])
        except Exception:
            continue
        if not dec:
            continue
        try:
            text = dec.decode("ascii", errors="ignore")
        except Exception:
            continue
        _absorb_text(text, result)
        if _have_enough():
            break

    return result


def _iso_from_t02_ts(s: Optional[str]) -> Optional[str]:
    """Parse a T02 header timestamp string to UTC ISO-format string. Never raises."""
    if not s:
        return None
    try:
        ts = pd.Timestamp(s)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts.isoformat()
    except Exception:
        return None


def _duration_s(t_first: Optional[str], t_last: Optional[str]) -> Optional[float]:
    """Seconds between two ISO-format UTC timestamps; None if either is missing."""
    if not t_first or not t_last:
        return None
    try:
        a = pd.Timestamp(t_first, tz="UTC")
        b = pd.Timestamp(t_last, tz="UTC")
        diff = (b - a).total_seconds()
        return diff if diff >= 0 else None
    except Exception:
        return None


# ── RINEX epoch-level statistics ─────────────────────────────────────────────
# Matches RINEX 3 epoch header line: "> YYYY MM DD HH MM SS.sss  flag  nsv"
_EPOCH_R3 = re.compile(
    r"^>\s+(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+([\d.]+)"
)

_STANDARD_INTERVALS = [
    0.05, 0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0,
    15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0, 1800.0, 3600.0,
]


def _snap_interval(raw: float) -> float:
    """Round a detected epoch interval to the nearest standard GNSS recording rate."""
    return min(_STANDARD_INTERVALS, key=lambda s: abs(s - raw))


def _parse_rinex_epochs(obs_path: Path, max_epochs: int = 500_000) -> dict:
    """
    Stream-parse a RINEX obs file for epoch statistics. Never raises.

    Returns:
      interval_s           : detected sample interval in seconds
      total_epochs         : count of valid epoch records read
      intra_file_gap_count : number of data gaps detected within the file
      intra_file_gaps      : list of {gap_start_utc, gap_end_utc, gap_epochs, gap_seconds}
    """
    _empty: dict = {
        "interval_s": None,
        "total_epochs": 0,
        "intra_file_gap_count": 0,
        "intra_file_gaps": [],
    }
    try:
        if not obs_path.exists() or obs_path.stat().st_size == 0:
            return _empty
    except OSError:
        return _empty

    from datetime import datetime as _dtime

    timestamps: list[_dtime] = []
    rinex3 = True   # convbin default output is RINEX 3; update from header
    in_header = True

    try:
        with obs_path.open("rb") as fh:
            for raw in fh:
                try:
                    line = raw.decode("ascii", errors="ignore").rstrip("\r\n")
                except Exception:
                    continue

                if in_header:
                    if "RINEX VERSION / TYPE" in line:
                        try:
                            rinex3 = float(line[:9].strip()) >= 3.0
                        except Exception:
                            pass
                    if "END OF HEADER" in line:
                        in_header = False
                    continue

                ts: Optional[_dtime] = None

                if rinex3:
                    m = _EPOCH_R3.match(line)
                    if m:
                        try:
                            y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
                            sf = float(m.group(6))
                            s = int(sf)
                            us = min(int(round((sf - s) * 1_000_000)), 999_999)
                            ts = _dtime(y, mo, d, h, mi, s, us)
                        except Exception:
                            pass
                else:
                    # RINEX 2 epoch line: " YY MM DD HH MI SS.s  flag  nsv ..."
                    # Distinguish from observation lines by requiring ≥8 fields and
                    # valid date/flag/nsv ranges.
                    if line and line[0] == " ":
                        parts = line.split()
                        if len(parts) >= 8:
                            try:
                                yy = int(parts[0])
                                mo = int(parts[1])
                                d  = int(parts[2])
                                h  = int(parts[3])
                                mi = int(parts[4])
                                sf = float(parts[5])
                                flag = int(parts[6])
                                nsv  = int(parts[7])
                                if (0 <= yy <= 99 and 1 <= mo <= 12 and 1 <= d <= 31
                                        and 0 <= h <= 23 and 0 <= mi <= 59
                                        and 0 <= flag <= 6 and 1 <= nsv <= 99):
                                    y = 2000 + yy if yy < 80 else 1900 + yy
                                    s = int(sf)
                                    us = min(int(round((sf - s) * 1_000_000)), 999_999)
                                    ts = _dtime(y, mo, d, h, mi, s, us)
                            except Exception:
                                pass

                if ts is not None:
                    timestamps.append(ts)
                    if len(timestamps) >= max_epochs:
                        break

    except Exception:
        return _empty

    n = len(timestamps)
    result = dict(_empty)
    result["total_epochs"] = n

    if n < 2:
        return result

    # Compute consecutive time diffs
    diffs: list[float] = []
    for i in range(1, n):
        try:
            d = (timestamps[i] - timestamps[i - 1]).total_seconds()
            if 0.0 < d < 86_400.0:
                diffs.append(d)
            else:
                diffs.append(0.0)   # placeholder keeps index alignment with timestamps
        except Exception:
            diffs.append(0.0)

    pos_diffs = sorted(d for d in diffs if d > 0)
    if not pos_diffs:
        return result

    # Use lower quartile of positive diffs — robust against gaps inflating the median
    q25 = pos_diffs[max(0, len(pos_diffs) // 4)]
    if q25 <= 0:
        return result
    interval_s = _snap_interval(q25)
    result["interval_s"] = interval_s

    # Detect gaps: diff > 1.5× interval
    threshold = interval_s * 1.5
    gaps: list[dict] = []
    for i, diff in enumerate(diffs):
        if diff > threshold:
            gap_start = timestamps[i]
            gap_end   = timestamps[i + 1]
            missed = max(0, round(diff / interval_s) - 1)
            gaps.append({
                "gap_start_utc": gap_start.isoformat(),
                "gap_end_utc":   gap_end.isoformat(),
                "gap_epochs":    int(missed),
                "gap_seconds":   round(diff, 3),
            })

    result["intra_file_gap_count"] = len(gaps)
    result["intra_file_gaps"] = gaps
    return result


def _file_sig(p: Path) -> tuple[int, int]:
    try:
        st = p.stat()
        return int(st.st_size), int(st.st_mtime)
    except OSError:
        return 0, 0


def _db_connect(db_path: Path) -> sqlite3.Connection:
    # isolation_level=None: pure autocommit — eliminates Python's implicit
    # transaction management which intermittently causes "database is locked"
    # when PRAGMA / DDL / DML are interleaved. Explicit BEGIN/COMMIT used in
    # the scan loop for batching.
    # No journal_mode PRAGMA: DELETE mode (SQLite default) avoids the WAL
    # conversion lock that kills writes on Windows when a stale -wal file exists.
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _db_init(conn: sqlite3.Connection) -> None:
    # Use individual execute() calls — executescript() bypasses busy_timeout
    # and raises immediately on a locked DB instead of retrying.
    ddl = [
        """CREATE TABLE IF NOT EXISTS files (
          path               TEXT PRIMARY KEY,
          station            TEXT NOT NULL,
          size_bytes         INTEGER NOT NULL,
          mtime              INTEGER NOT NULL,
          rinex_obs_path     TEXT,
          time_first_obs     TEXT,
          time_last_obs      TEXT,
          lat                REAL,
          lon                REAL,
          height_m           REAL,
          constellations     TEXT,
          signals            TEXT,
          convert_status     TEXT,
          convert_detail     TEXT,
          filename_date      TEXT,
          filename_hour      INTEGER,
          duration_s         REAL,
          interval_s         REAL,
          total_epochs       INTEGER,
          expected_epochs    INTEGER,
          completeness_pct   REAL,
          intra_file_gap_count INTEGER,
          updated_at         TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS intra_file_gaps (
          id             INTEGER PRIMARY KEY AUTOINCREMENT,
          path           TEXT NOT NULL,
          station        TEXT NOT NULL,
          gap_start_utc  TEXT NOT NULL,
          gap_end_utc    TEXT NOT NULL,
          gap_epochs     INTEGER NOT NULL,
          gap_seconds    REAL NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_files_station_first ON files(station, time_first_obs)",
        "CREATE INDEX IF NOT EXISTS idx_files_station_date  ON files(station, filename_date, filename_hour)",
        "CREATE INDEX IF NOT EXISTS idx_ifg_path            ON intra_file_gaps(path)",
        "CREATE INDEX IF NOT EXISTS idx_ifg_station         ON intra_file_gaps(station)",
    ]
    for stmt in ddl:
        conn.execute(stmt)

    # Migrate existing DBs — ALTER TABLE is a no-op if column already exists
    # (SQLite ≥ 3.37 raises OperationalError; we catch and ignore).
    for col, typedef in [
        ("filename_date",         "TEXT"),
        ("filename_hour",         "INTEGER"),
        ("duration_s",            "REAL"),
        ("interval_s",            "REAL"),
        ("total_epochs",          "INTEGER"),
        ("expected_epochs",       "INTEGER"),
        ("completeness_pct",      "REAL"),
        ("intra_file_gap_count",  "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE files ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass


def _read_rinex_header_lines(path: Path, max_bytes: int = 256 * 1024) -> list[str]:
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes)
    except OSError:
        return []
    try:
        text = data.decode("ascii", errors="ignore")
    except Exception:
        text = ""
    lines: list[str] = []
    for ln in text.splitlines():
        lines.append(ln.rstrip("\r\n"))
        if "END OF HEADER" in ln:
            break
    return lines


def _parse_rinex_time(line: str) -> Optional[pd.Timestamp]:
    """
    RINEX "TIME OF FIRST/LAST OBS" header line.
    Tries fixed columns first (spec), falls back to tolerant numeric scan.
    """
    if not line:
        return None
    head = line[:60]
    try:
        y = int(head[0:6].strip())
        mo = int(head[6:12].strip())
        d = int(head[12:18].strip())
        h = int(head[18:24].strip())
        mi = int(head[24:30].strip())
        sec = float(head[30:43].strip())
        s = max(0, min(59, int(sec)))  # clamp -- leap second rounds down
        us = max(0, min(int(round((sec - s) * 1_000_000)), 999_999))
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and 0 <= h < 24 and 0 <= mi < 60:
            return pd.Timestamp(year=y, month=mo, day=d, hour=h, minute=mi, second=s, microsecond=us, tz="UTC")
    except Exception:
        pass
    nums = re.findall(r"[-+]?\d+\.\d+|[-+]?\d+", head)
    if len(nums) < 6:
        return None
    try:
        y, mo, d, h, mi = [int(float(x)) for x in nums[:5]]
        sec = float(nums[5])
        s = max(0, min(59, int(sec)))
        us = max(0, min(int(round((sec - s) * 1_000_000)), 999_999))
        if not (1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
            return None
        return pd.Timestamp(year=y, month=mo, day=d, hour=h, minute=mi, second=s, microsecond=us, tz="UTC")
    except Exception:
        return None


def _parse_rinex_position_xyz(line: str) -> Optional[tuple[float, float, float]]:
    """
    "APPROX POSITION XYZ" header line. Tolerates scientific notation.
    Rejects all-zero, NaN/Inf, and off-Earth radii.
    """
    if not line:
        return None
    head = line[:60]
    try:
        x = float(head[0:14].strip())
        y = float(head[14:28].strip())
        z = float(head[28:42].strip())
    except Exception:
        nums = re.findall(r"[-+]?\d+\.\d+(?:[eE][-+]?\d+)?|[-+]?\d+", head)
        if len(nums) < 3:
            return None
        try:
            x, y, z = float(nums[0]), float(nums[1]), float(nums[2])
        except Exception:
            return None

    import math as _m
    if not all(_m.isfinite(v) for v in (x, y, z)):
        return None
    if x == 0.0 and y == 0.0 and z == 0.0:
        return None
    r = _m.sqrt(x * x + y * y + z * z)
    if r < 5_000_000.0 or r > 8_000_000.0:
        return None
    return x, y, z


def _ecef_to_llh_wgs84(x: float, y: float, z: float) -> tuple[float, float, float]:
    import math

    if not all(map(math.isfinite, [x, y, z])):
        return 0.0, 0.0, 0.0
    if x == 0.0 and y == 0.0 and z == 0.0:
        return 0.0, 0.0, 0.0

    a = 6_378_137.0
    f = 1.0 / 298.257_223_563
    e2 = f * (2.0 - f)
    b = a * (1.0 - f)

    lon = math.atan2(y, x)
    p = math.hypot(x, y)

    if p < 1.0:
        sign = 1.0 if z >= 0.0 else -1.0
        return sign * 90.0, math.degrees(lon), abs(z) - b

    lat = math.atan2(z, p * (1.0 - e2))
    for _ in range(10):
        sin_lat = math.sin(lat)
        n = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
        lat_new = math.atan2(z + e2 * n * sin_lat, p)
        if abs(lat_new - lat) < 1e-12:
            lat = lat_new
            break
        lat = lat_new

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    n = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    if abs(cos_lat) >= abs(sin_lat):
        h = p / cos_lat - n
    else:
        h = z / sin_lat - n * (1.0 - e2)
    return math.degrees(lat), math.degrees(lon), h


def _llh_to_ecef(lat_deg: float, lon_deg: float, h_m: float) -> tuple[float, float, float]:
    import math
    a  = 6_378_137.0
    f  = 1.0 / 298.257_223_563
    e2 = f * (2.0 - f)
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    N = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    x = (N + h_m) * cos_lat * math.cos(lon)
    y = (N + h_m) * cos_lat * math.sin(lon)
    z = (N * (1.0 - e2) + h_m) * sin_lat
    return x, y, z


def _patch_rinex_approx_pos(obs_path: Path, lat: float, lon: float, h: float) -> None:
    """
    If APPROX POSITION XYZ is absent from the RINEX obs header, inject it
    before END OF HEADER. Silently no-ops on any error or if already present.
    """
    try:
        raw = obs_path.read_bytes()
        text = raw.decode("ascii", errors="ignore")
    except Exception:
        return
    if "APPROX POSITION XYZ" in text:
        return
    try:
        x, y, z = _llh_to_ecef(lat, lon, h)
    except Exception:
        return
    # RINEX header record: 60-char data field + 20-char label field = 80 chars
    # XYZ: three 14.4f values (42 chars) + 18 spaces filler = 60 chars data
    pos_line = f"{x:14.4f}{y:14.4f}{z:14.4f}                  APPROX POSITION XYZ \n"
    lines = text.splitlines(keepends=True)
    new_lines: list[str] = []
    injected = False
    for ln in lines:
        if not injected and "END OF HEADER" in ln:
            new_lines.append(pos_line)
            injected = True
        new_lines.append(ln)
    if not injected:
        return
    try:
        with obs_path.open("w", encoding="ascii", errors="ignore") as _f:
            _f.write("".join(new_lines))
    except Exception:
        pass


_VALID_SYS = set("GREJCIS")  # GPS, GLONASS, Galileo, QZSS, BeiDou, IRNSS, SBAS


def _parse_rinex_signals(lines: list[str]) -> tuple[str | None, str | None]:
    consts: set[str] = set()
    sigs: set[str] = set()
    last_sys: Optional[str] = None
    for ln in lines:
        if "SYS / # / OBS TYPES" in ln:
            sys = ln[:1].upper()
            if sys in _VALID_SYS:
                consts.add(sys)
                last_sys = sys
            elif ln[:1] == " " and last_sys:
                pass
            for t in re.findall(r"\b[A-Z][0-9][A-Z]\b", ln):
                sigs.add(t)
        elif "# / TYPES OF OBSERV" in ln:
            consts.add("G")
            for t in re.findall(r"\b[A-Z][0-9][A-Z]\b", ln):
                sigs.add(t)
    c = ",".join(sorted(consts)) if consts else None
    s = ",".join(sorted(sigs)) if sigs else None
    return c, s


def _safe_stem(p: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", p.stem)
    stem = stem.strip("._-") or "file"
    h = hashlib.sha1(str(p).encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"{stem}_{h}"


class ConverterError(RuntimeError):
    """Raised so run_pipeline can record a useful detail message."""


def _short_err(p: "subprocess.CompletedProcess[str]", limit: int = 240) -> str:
    err = (p.stderr or "").strip() or (p.stdout or "").strip()
    if not err:
        return f"exit={p.returncode}"
    return f"exit={p.returncode}: {err[:limit]}"


_SUBPROC_KW: dict = {"stdin": subprocess.DEVNULL}
if os.name == "nt":
    _SUBPROC_KW["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _runpkr00_gd_first_dat_or_tgd(runpkr00_path: Path, inp: Path, work_dir: Path) -> Optional[Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    local = work_dir / inp.name
    try:
        shutil.copy2(inp, local)
    except OSError:
        return None
    try:
        subprocess.run(
            [str(runpkr00_path), "-g", "-d", str(local)],
            cwd=str(work_dir),
            capture_output=True, text=True, errors="replace", timeout=120, check=False,
            **_SUBPROC_KW,
        )
    except (subprocess.TimeoutExpired, Exception):
        return None
    hits = sorted(work_dir.glob("*.dat")) + sorted(work_dir.glob("*.tgd"))
    return hits[0] if hits else None


def _runpkr00_make_dat(
    runpkr00_path: Path,
    inp: Path,
    dat: Path,
    eph: Path,
    out_dir: Path,
) -> None:
    """
    Run runpkr00 to produce .dat (and optionally .eph) from a T02/T04 file.
    Tries -devg first; falls back to -g -d if no .dat produced.
    Raises ConverterError on failure.
    """
    base = dat.with_suffix("")
    try:
        p1 = subprocess.run(
            [str(runpkr00_path), "-devg", str(inp), str(base)],
            cwd=str(out_dir),
            capture_output=True, text=True, errors="replace", timeout=120, check=False,
            **_SUBPROC_KW,
        )
    except subprocess.TimeoutExpired:
        raise ConverterError("runpkr00 timed out (120s)")
    except Exception as e:
        raise ConverterError(f"runpkr00 failed to launch: {e}")

    if not dat.exists():
        try:
            tw = Path(tempfile.mkdtemp(prefix="runpkr_gd_", dir=str(out_dir)))
        except OSError as e:
            raise ConverterError(f"runpkr00 tempdir create failed: {e}")
        try:
            alt = _runpkr00_gd_first_dat_or_tgd(runpkr00_path, inp, tw)
            if alt is None:
                raise ConverterError(
                    f"runpkr00 produced no .dat/.tgd (-devg nor -g -d) ({_short_err(p1)})"
                )
            try:
                shutil.copy2(alt, dat)
            except OSError as e:
                raise ConverterError(f"copy Trimble intermediate: {e}")
        finally:
            shutil.rmtree(tw, ignore_errors=True)


def _is_rt17_dat(dat: Path) -> bool:
    """
    Sniff runpkr00 output to detect whether records are RT17 (decodable by
    convbin) or the newer RT27/CMRx format (Alloy-era receivers).

    RT17 records start with STX (0x02). RT27 / modern Trimble DAT records
    start with `0x74` ('t') or other non-STX bytes. Convbin only handles RT17,
    so attempting conversion on non-RT17 .dat just produces an empty .obs.
    Sniffing the first byte lets us fail fast with a clear status.
    """
    try:
        with dat.open("rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    return len(head) > 0 and head[0] == 0x02


def _convbin_on_dat(convbin_path: Path, dat: Path, obs_path: Path) -> None:
    """
    Convert runpkr00 .dat (RT17 format) → RINEX 3 obs + nav using convbin.
    Nav file is written alongside obs with .nav extension (used by SPP solver).
    Raises ConverterError on failure (incl. detected RT27 / unsupported format).
    """
    # Fail fast on non-RT17 records — convbin would silently produce empty obs
    if not _is_rt17_dat(dat):
        raise ConverterError(
            "unsupported_rt27: runpkr00 produced non-RT17 records "
            "(modern Trimble Alloy / RT27 format — no open-source decoder available; "
            "metadata extracted from T02 header instead)"
        )

    nav_path = obs_path.with_suffix(".nav")
    for fp in (obs_path, nav_path):
        try:
            fp.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        p = subprocess.run(
            [
                str(convbin_path), "-r", "rt17",
                str(dat),
                "-o", str(obs_path),
                "-n", str(nav_path),  # nav (mixed) alongside obs
                "-v", "3.04",
                "-os",                # include SNR
            ],
            cwd=str(obs_path.parent),
            capture_output=True, text=True, errors="replace", timeout=180, check=False,
            **_SUBPROC_KW,
        )
    except subprocess.TimeoutExpired:
        raise ConverterError("convbin -r rt17 timed out (180s)")
    except Exception as e:
        raise ConverterError(f"convbin failed to launch: {e}")
    try:
        ok = obs_path.stat().st_size > 0
    except OSError:
        ok = False
    if not ok:
        raise ConverterError(f"convbin produced no non-empty obs ({_short_err(p)})")


def _rnx2rtkp_spp(
    rnx2rtkp_path: Path,
    obs_path: Path,
    nav_path: Path,
) -> Optional[tuple[float, float, float]]:
    """
    Single-point position solve on one obs+nav RINEX pair.
    Returns median (lat_deg, lon_deg, height_m) over all valid epochs, or None.
    Never raises. Cleans up temp .pos file.
    """
    pos_path = obs_path.with_suffix(".spp.pos")
    try:
        pos_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        subprocess.run(
            [
                str(rnx2rtkp_path),
                "-p", "0",    # single-point
                "-m", "10",   # 10° elevation mask
                "-o", str(pos_path),
                str(obs_path),
                str(nav_path),
            ],
            capture_output=True, text=True, errors="replace", timeout=120, check=False,
            **_SUBPROC_KW,
        )
    except Exception:
        return None

    lats: list[float] = []
    lons: list[float] = []
    hgts: list[float] = []
    try:
        for line in pos_path.read_text(encoding="ascii", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("%"):
                continue
            parts = line.split()
            if len(parts) < 7:
                continue
            try:
                lat = float(parts[2])
                lon = float(parts[3])
                hgt = float(parts[4])
                q   = int(parts[5])
                if q > 0 and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    lats.append(lat)
                    lons.append(lon)
                    hgts.append(hgt)
            except Exception:
                continue
    except Exception:
        return None
    finally:
        try:
            pos_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not lats:
        return None
    triplets = sorted(zip(lats, lons, hgts))
    mid = len(triplets) // 2
    return triplets[mid]



def _nonempty_obs(out_dir: Path) -> Optional[Path]:
    candidates = list(out_dir.glob("*.obs")) + list(out_dir.glob("*.??o"))
    live = []
    for c in candidates:
        try:
            if c.stat().st_size > 0:
                live.append(c)
        except OSError:
            pass
    if not live:
        return None
    try:
        return max(live, key=lambda x: x.stat().st_mtime)
    except OSError:
        return live[0]


def _convert_t02_ctr(ctr_exe: Path, inp: Path, out_dir: Path) -> Optional[Path]:
    """
    Convert T02 → RINEX 3 using convertToRinex_cli.exe (Trimble CLI build).
    Handles RT27/Alloy files that runpkr00+convbin cannot decode.
    Returns obs Path on success. Raises ConverterError on failure.

    Robust output detection: snapshots existing .??o files BEFORE invocation so
    stale RINEX from a previous T02 in the same out_dir cannot be mis-attributed.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    # Snapshot pre-existing obs files (multiple T02s share station out_dir)
    try:
        pre_existing: set = {p.resolve() for p in out_dir.glob("*.??o")}
    except OSError:
        pre_existing = set()
    start_time = time.time() - 1.0  # 1s slack for filesystem clock skew

    try:
        r = subprocess.run(
            [str(ctr_exe), str(inp), "-p", str(out_dir), "-v", "3.04"],
            capture_output=True, text=True, errors="replace", timeout=120, check=False,
            **_SUBPROC_KW,
        )
    except subprocess.TimeoutExpired:
        raise ConverterError("convertToRinex timed out (120s)")
    except Exception as e:
        raise ConverterError(f"convertToRinex failed to launch: {e}")
    combined = (r.stdout or "") + (r.stderr or "")
    if "aborted" in combined.lower():
        raise ConverterError(f"convertToRinex aborted: {combined[:200].strip()}")

    import glob as _glob
    stem_pat = _glob.escape(inp.stem)

    def _fresh_and_nonempty(p: Path) -> bool:
        try:
            st = p.stat()
            return st.st_size > 0 and st.st_mtime >= start_time
        except OSError:
            return False

    def _nonempty(p: Path) -> bool:
        try:
            return p.stat().st_size > 0
        except OSError:
            return False

    # 1. Stem-matched file, freshly written by this invocation (most specific)
    matches = [p for p in out_dir.glob(f"{stem_pat}.??o") if _fresh_and_nonempty(p)]
    if matches:
        return matches[0]

    # 2. Stem-matched file from any time (CTR may not bump mtime on re-conversion)
    matches = [p for p in out_dir.glob(f"{stem_pat}.??o") if _nonempty(p)]
    if matches:
        return matches[0]

    # 3. Any NEW obs file written during this invocation that didn't exist before
    new_files = [
        p for p in out_dir.glob("*.??o")
        if _fresh_and_nonempty(p) and p.resolve() not in pre_existing
    ]
    if new_files:
        try:
            return max(new_files, key=lambda x: x.stat().st_mtime)
        except OSError:
            return new_files[0]

    raise ConverterError(
        f"convertToRinex produced no obs "
        f"({combined[:120].strip() or f'exit={r.returncode}'})"
    )


def convert_to_rinex(cfg: PipelineConfig, inp: Path, out_dir: Path) -> Optional[Path]:
    """
    Convert a vendor binary (T02/T04) → RINEX 3 obs. Returns the obs path on success.

    Priority:
      1. Custom convert_cmd_template (user-supplied)
      2. runpkr00 → .dat (RT17) → convbin -r rt17 → RINEX 3

    Raises ConverterError on failure. Returns None when no converter is configured.
    """
    # 0. Custom template
    if cfg.convert_cmd_template:
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = cfg.convert_cmd_template.format(input=str(inp), out_dir=str(out_dir))
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True, errors="replace", timeout=180, **_SUBPROC_KW)
        except subprocess.TimeoutExpired:
            raise ConverterError("custom convert_cmd timed out (180s)")
        except Exception as e:
            raise ConverterError(f"custom convert_cmd failed to launch: {e}")
        if p.returncode != 0:
            raise ConverterError(f"custom convert_cmd {_short_err(p)}")
        obs = _nonempty_obs(out_dir)
        if obs is None:
            raise ConverterError("custom convert_cmd produced no non-empty .obs")
        return obs

    # 1. runpkr00 → .dat → convbin -r rt17
    has_runpkr00 = bool(cfg.runpkr00_path and cfg.runpkr00_path.exists())
    has_convbin  = bool(cfg.convbin_path  and cfg.convbin_path.exists())

    if has_runpkr00 and has_convbin:
        out_dir.mkdir(parents=True, exist_ok=True)
        base   = out_dir / _safe_stem(inp)
        dat    = base.with_suffix(".dat")
        eph    = base.with_suffix(".eph")
        obs_cb = base.with_suffix(".obs")

        for fp in (dat, eph, obs_cb):
            try:
                fp.unlink(missing_ok=True)
            except Exception:
                pass

        _runpkr00_make_dat(cfg.runpkr00_path, inp, dat, eph, out_dir)
        _convbin_on_dat(cfg.convbin_path, dat, obs_cb)
        return obs_cb

    return None


def run_pipeline(cfg: PipelineConfig, progress_cb=None) -> Path:
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = cfg.cache_dir / "scan_cache.sqlite"
    rinex_dir = cfg.cache_dir / "rinex"
    rinex_dir.mkdir(parents=True, exist_ok=True)

    exclude = [cfg.cache_dir]

    _PIPELINE_LOCK.acquire()
    conn = None
    try:
        # Pre-flight: any stale WAL/SHM file from a killed process blocks DELETE-mode
        # writes with "database is locked" because SQLite needs exclusive access to
        # resolve the orphaned WAL before it can write. Delete sidecars after
        # acquiring the lock so we never clobber another process's in-flight write.
        for _suf in ("-wal", "-shm", "-journal"):
            try:
                Path(str(db_path) + _suf).unlink()
            except OSError:
                pass

        conn = _db_connect(db_path)
        try:
            _db_init(conn)
        except sqlite3.OperationalError as _e:
            if "locked" not in str(_e).lower():
                raise
            # Stale WAL-mode or crashed DB — nuke and recreate.
            conn.close()
            conn = None
            # gc.collect() forces Python to release the underlying file
            # handle on Windows (which keeps handles alive until GC).
            gc.collect()
            time.sleep(0.15)
            for _suf in ("", "-wal", "-shm", "-journal"):
                try:
                    Path(str(db_path) + _suf).unlink()
                except OSError:
                    pass
            conn = _db_connect(db_path)
            _db_init(conn)
        if cfg.max_files_per_station is not None:
            files = _pick_probe_files(
                cfg.data_root,
                max_files_per_station=cfg.max_files_per_station,
                max_total_files=cfg.probe_max_total_files,
                exclude_dirs=exclude,
            )
        else:
            # Cap unbounded discovery: a 10M-file tree would otherwise blow memory
            # before the scan even starts.
            from itertools import islice as _islice
            _cap = max(1, int(cfg.probe_max_total_files or 200_000))
            files = list(_islice(
                _iter_to_files(cfg.data_root, exclude_dirs=exclude),
                _cap,
            ))
        total = max(1, len(files))

        # Load user-supplied station coordinates override (station → lat, lon, height_m).
        # Used to inject APPROX POSITION XYZ into RINEX headers when receiver didn't embed one.
        station_coords: dict[str, tuple[float, float, float]] = {}
        if cfg.station_coords_path and cfg.station_coords_path.exists():
            try:
                sc_df = pd.read_csv(cfg.station_coords_path, dtype=str)
                for _, row in sc_df.iterrows():
                    try:
                        st   = str(row["station"]).strip().upper()
                        lat_c = float(row["lat"])
                        lon_c = float(row["lon"])
                        h_c   = float(row.get("height_m", 0) or 0)
                        if st:
                            station_coords[st] = (lat_c, lon_c, h_c)
                    except Exception:
                        pass
            except Exception:
                pass

        # Tracks stations where SPP was already attempted this run (success or fail).
        # One attempt per station — use the result for every subsequent file.
        station_spp_done: set[str] = set()

        attempted_by_station: dict[str, int] = {}
        success_by_station: set[str] = set()
        cache_hits = 0
        processed = 0
        failed = 0
        skipped_empty = 0

        # Transaction batching. If BEGIN fails (e.g. immediately after a crashed
        # prior connection), fall back to autocommit (isolation_level=None already
        # enables that). Tracked via in_tx so subsequent COMMITs are skipped.
        try:
            conn.execute("BEGIN")
            in_tx = True
        except Exception:
            in_tx = False
        for i, p in enumerate(files, start=1):
            if progress_cb:
                # Never let a buggy callback (Streamlit threading, etc) abort scan.
                try:
                    progress_cb(i, total, str(p))
                except Exception:
                    pass

            station  = _station_from_filename(p.name)
            fn_date, fn_hour = _parse_filename_dt(p.name, p)

            if cfg.stop_after_success_per_station and station in success_by_station:
                continue
            if cfg.max_files_per_station is not None:
                n = attempted_by_station.get(station, 0)
                if n >= cfg.max_files_per_station:
                    continue

            # Per-file variables — initialised here so INSERT always has values
            # regardless of which code path (empty/cached/converted/failed) is taken.
            _hdr:                 dict            = {}  # bzip2 probe result
            interval_s:           Optional[float] = None
            total_epochs:         int             = 0
            expected_epochs:      Optional[int]   = None
            completeness_pct:     Optional[float] = None
            intra_file_gap_count: int             = 0
            intra_file_gaps_data: list            = []

            try:
                size_b, mtime = _file_sig(p)
                if size_b <= 0:
                    rinex_obs      = None
                    convert_status = "skipped"
                    convert_detail = "empty or unreadable file (0 bytes or stat failed)"
                    t_first = t_last = None
                    lat = lon = h_m = None
                    consts = sigs   = None
                    dur_s           = None
                    skipped_empty  += 1
                else:
                    row = conn.execute(
                        "SELECT size_bytes, mtime FROM files WHERE path=?", (str(p),)
                    ).fetchone()
                    if row and int(row["size_bytes"]) == size_b and int(row["mtime"]) == mtime:
                        cache_hits += 1
                        continue

                    # Tier-0: read T02 binary bzip2 header — gives session timestamps
                    # and marker name without any external tools, regardless of filename
                    # naming convention.
                    _hdr = _probe_t02_header(p)
                    if station == "UNKNOWN" and _hdr.get("marker_name"):
                        station = re.sub(r"[^A-Z0-9]", "", _hdr["marker_name"].upper())[:8] or "UNKNOWN"
                    # Fallback chain for date+hour: filename → SessionStart → filePath
                    if fn_date is None and _hdr.get("session_start"):
                        _t0 = _iso_from_t02_ts(_hdr["session_start"])
                        if _t0:
                            try:
                                _dt0 = pd.Timestamp(_t0)
                                fn_date = _dt0.date().isoformat()
                                fn_hour = _dt0.hour
                            except Exception:
                                pass
                    if fn_date is None and _hdr.get("filepath_date"):
                        fn_date = _hdr["filepath_date"]
                        fh = _hdr.get("filepath_hour")
                        if fn_hour is None and isinstance(fh, int):
                            fn_hour = fh

                    out_dir = rinex_dir / station
                    rinex_obs      = None
                    convert_status = "skipped"
                    convert_detail = None
                    has_ctr        = bool(cfg.ctr_path and cfg.ctr_path.exists())
                    has_converter  = bool(
                        cfg.convert_cmd_template
                        or has_ctr
                        or (cfg.runpkr00_path and cfg.runpkr00_path.exists()
                            and has_convbin_cfg(cfg))
                    )

                    rx_model = (_hdr.get("receiver_model") or "").lower()
                    is_rt27_receiver = any(
                        m in rx_model for m in _RT27_RECEIVER_MARKERS
                    )

                    if (is_rt27_receiver or cfg.ctr_first) and has_ctr:
                        # RT27/Alloy + CTR converter available
                        # ctr_first=True also routes here: skips runpkr00+convbin entirely
                        try:
                            rinex_obs = _convert_t02_ctr(cfg.ctr_path, p, out_dir)
                            convert_status = "ok" if rinex_obs else "failed"
                            if not rinex_obs:
                                convert_detail = "convertToRinex produced no output file"
                        except ConverterError as ce:
                            rinex_obs      = None
                            convert_status = "failed"
                            convert_detail = str(ce)
                            # Fallback to runpkr00+convbin when ctr_first picked CTR for an
                            # untagged file (might be RT17, not RT27). Skip fallback when
                            # the file was tagged RT27 -- runpkr00 cannot decode RT27.
                            if (
                                cfg.ctr_first and not is_rt27_receiver
                                and cfg.runpkr00_path and cfg.runpkr00_path.exists()
                                and has_convbin_cfg(cfg)
                            ):
                                try:
                                    rinex_obs = convert_to_rinex(cfg, p, out_dir)
                                except ConverterError as ce2:
                                    convert_detail = f"CTR: {ce}; runpkr00+convbin: {ce2}"
                                else:
                                    if rinex_obs:
                                        convert_status = "ok"
                                        convert_detail = f"CTR failed ({ce}); runpkr00+convbin succeeded"
                                    else:
                                        convert_detail = f"CTR: {ce}; runpkr00+convbin: no output"
                    elif has_converter and is_rt27_receiver:
                        # Early-skip: RT27 with no CTR — runpkr00+convbin can't decode
                        convert_status = "unsupported_rt27"
                        convert_detail = (
                            f"unsupported_rt27: {rx_model!r} records in RT27 / CMRx "
                            "format (no open-source decoder); metadata extracted from "
                            "T02 header instead"
                        )
                    elif has_converter:
                        try:
                            rinex_obs = convert_to_rinex(cfg, p, out_dir)
                        except ConverterError as ce:
                            rinex_obs      = None
                            msg            = str(ce)
                            if msg.startswith("unsupported_rt27") and has_ctr:
                                # RT27 detected mid-stream — fall through to CTR
                                try:
                                    rinex_obs = _convert_t02_ctr(cfg.ctr_path, p, out_dir)
                                    convert_status = "ok" if rinex_obs else "failed"
                                    convert_detail = None if rinex_obs else "convertToRinex produced no output file"
                                except ConverterError as ce2:
                                    convert_status = "failed"
                                    convert_detail = str(ce2)
                            elif msg.startswith("unsupported_rt27"):
                                convert_status = "unsupported_rt27"
                                convert_detail = msg
                            else:
                                convert_status = "failed"
                                convert_detail = msg
                        else:
                            if rinex_obs is None and has_ctr:
                                # runpkr00+convbin produced nothing — may be untagged RT27
                                try:
                                    rinex_obs = _convert_t02_ctr(cfg.ctr_path, p, out_dir)
                                    convert_status = "ok" if rinex_obs else "failed"
                                    convert_detail = None if rinex_obs else "convertToRinex produced no output file"
                                except ConverterError as ce:
                                    convert_status = "failed"
                                    convert_detail = str(ce)
                            else:
                                convert_status = "ok" if rinex_obs else "failed"
                                if not rinex_obs:
                                    convert_detail = "converter ran but produced no output file"

                    attempted_by_station[station] = attempted_by_station.get(station, 0) + 1
                    # Treat unsupported_rt27 as "done" for stop_after_success_per_station —
                    # every file from the same Alloy receiver hits the same wall,
                    # so retrying wastes runpkr00 time. Probe metadata is already saved.
                    if convert_status in ("ok", "unsupported_rt27"):
                        success_by_station.add(station)

                    # Seed from bzip2 probe; RINEX conversion overrides below if available
                    t_first = _iso_from_t02_ts(_hdr.get("session_start"))
                    t_last  = _iso_from_t02_ts(_hdr.get("session_end"))
                    # Probe coords (RefStationLLH in VRS files) — RINEX overrides if present
                    lat    = _hdr.get("lat")
                    lon    = _hdr.get("lon")
                    h_m    = _hdr.get("height_m")
                    consts = sigs = None
                    dur_s = _duration_s(t_first, t_last)
                    if interval_s is None and _hdr.get("interval_s"):
                        try:
                            interval_s = _snap_interval(float(_hdr["interval_s"]))
                        except Exception:
                            pass

                    if rinex_obs and rinex_obs.exists():
                        hdr_lines = _read_rinex_header_lines(rinex_obs)
                        # Per-line guard: one malformed record cannot discard metadata
                        # already collected from preceding lines.
                        for ln in hdr_lines:
                            try:
                                if "TIME OF FIRST OBS" in ln:
                                    ts = _parse_rinex_time(ln)
                                    if ts is not None:
                                        t_first = ts.isoformat()
                                if "TIME OF LAST OBS" in ln:
                                    ts = _parse_rinex_time(ln)
                                    if ts is not None:
                                        t_last = ts.isoformat()
                                if "APPROX POSITION XYZ" in ln:
                                    xyz = _parse_rinex_position_xyz(ln)
                                    if xyz:
                                        try:
                                            lat, lon, h_m = _ecef_to_llh_wgs84(*xyz)
                                        except ZeroDivisionError:
                                            lat = lon = h_m = None
                                # Use column-60+ label region to avoid matching comment text
                                label = ln[60:].strip() if len(ln) > 60 else ""
                                if label == "INTERVAL" and interval_s is None:
                                    try:
                                        interval_s = _snap_interval(float(ln[:60].strip()))
                                    except (ValueError, TypeError):
                                        pass
                                if label == "MARKER NAME":
                                    raw = ln[:60].strip()
                                    if raw:
                                        clean = re.sub(r"[^A-Z0-9_.]", "", raw.upper())[:8]
                                        if len(clean) >= 3 and clean != station:
                                            station = clean
                            except Exception:
                                continue
                        try:
                            consts, sigs = _parse_rinex_signals(hdr_lines)
                        except Exception:
                            consts, sigs = None, None
                        dur_s = _duration_s(t_first, t_last)

                        # ── Backfill fn_date/fn_hour from RINEX when filename gave nothing ──
                        if fn_date is None and t_first:
                            try:
                                _dt = pd.Timestamp(t_first)
                                fn_date = _dt.date().isoformat()
                                fn_hour = _dt.hour
                            except Exception:
                                pass

                        # ── Inject coords if header had none ─────────────────
                        if lat is None and station in station_coords:
                            lat, lon, h_m = station_coords[station]
                            _patch_rinex_approx_pos(rinex_obs, lat, lon, h_m)

                        # ── SPP solve (one attempt per station per run) ────────
                        if (
                            lat is None
                            and station not in station_spp_done
                            and cfg.rnx2rtkp_path and cfg.rnx2rtkp_path.exists()
                        ):
                            station_spp_done.add(station)
                            nav_path = rinex_obs.with_suffix(".nav")
                            if nav_path.exists() and nav_path.stat().st_size > 0:
                                spp = _rnx2rtkp_spp(cfg.rnx2rtkp_path, rinex_obs, nav_path)
                                if spp is not None:
                                    lat, lon, h_m = spp
                                    station_coords[station] = (lat, lon, h_m)
                                    _patch_rinex_approx_pos(rinex_obs, lat, lon, h_m)

                        # ── Epoch-level statistics ─────────────────────────
                        ep = _parse_rinex_epochs(rinex_obs)
                        interval_s           = ep.get("interval_s")
                        total_epochs         = ep.get("total_epochs") or 0
                        intra_file_gap_count = ep.get("intra_file_gap_count", 0)
                        intra_file_gaps_data = ep.get("intra_file_gaps", [])
                        if interval_s and interval_s > 0 and dur_s and dur_s > 0:
                            expected_epochs  = max(1, round(dur_s / interval_s))
                            completeness_pct = (
                                min(100.0, round(total_epochs / expected_epochs * 100, 2))
                                if expected_epochs > 0 else None
                            )

                    # Fallback: for files with a known hour (standard 1-hour GeoNet
                    # convention) infer 3600 s duration when probe + RINEX both failed.
                    if dur_s is None and fn_hour is not None and fn_date is not None:
                        dur_s = 3600.0

                    # Completeness from probe interval when RINEX epoch stats unavailable.
                    if (expected_epochs is None and dur_s and dur_s > 0
                            and interval_s and interval_s > 0):
                        expected_epochs = max(1, round(dur_s / interval_s))
                        if completeness_pct is None:
                            completeness_pct = min(
                                100.0, round(total_epochs / expected_epochs * 100, 2)
                            )

            except Exception as e:
                size_b, mtime  = _file_sig(p)
                rinex_obs      = None
                convert_status = "failed"
                convert_detail = f"{type(e).__name__}: {e}"
                t_first = _iso_from_t02_ts(_hdr.get("session_start"))
                t_last  = _iso_from_t02_ts(_hdr.get("session_end"))
                lat = lon = h_m = None
                consts = sigs   = None
                dur_s = _duration_s(t_first, t_last)
                failed         += 1

            processed += 1

            # ── Persist file row ───────────────────────────────────────────
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                      path, station, size_bytes, mtime,
                      rinex_obs_path, time_first_obs, time_last_obs,
                      lat, lon, height_m,
                      constellations, signals,
                      convert_status, convert_detail,
                      filename_date, filename_hour, duration_s,
                      interval_s, total_epochs, expected_epochs,
                      completeness_pct, intra_file_gap_count,
                      updated_at
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                      station=excluded.station,
                      size_bytes=excluded.size_bytes,
                      mtime=excluded.mtime,
                      rinex_obs_path=excluded.rinex_obs_path,
                      time_first_obs=excluded.time_first_obs,
                      time_last_obs=excluded.time_last_obs,
                      lat=excluded.lat,
                      lon=excluded.lon,
                      height_m=excluded.height_m,
                      constellations=excluded.constellations,
                      signals=excluded.signals,
                      convert_status=excluded.convert_status,
                      convert_detail=excluded.convert_detail,
                      filename_date=excluded.filename_date,
                      filename_hour=excluded.filename_hour,
                      duration_s=excluded.duration_s,
                      interval_s=excluded.interval_s,
                      total_epochs=excluded.total_epochs,
                      expected_epochs=excluded.expected_epochs,
                      completeness_pct=excluded.completeness_pct,
                      intra_file_gap_count=excluded.intra_file_gap_count,
                      updated_at=excluded.updated_at
                    """,
                    (
                        str(p), station, size_b, mtime,
                        str(rinex_obs) if rinex_obs else None,
                        t_first, t_last,
                        lat, lon, h_m,
                        consts, sigs,
                        convert_status, convert_detail,
                        fn_date, fn_hour, dur_s,
                        interval_s, total_epochs, expected_epochs,
                        completeness_pct, intra_file_gap_count,
                        _utc_now_iso(),
                    ),
                )
                if processed % 250 == 0 and in_tx:
                    # Two-phase COMMIT/BEGIN: track each independently so a failure
                    # in one doesn't leave the conn in an inconsistent transaction state.
                    try:
                        conn.execute("COMMIT")
                    except Exception:
                        # COMMIT failed — try to reset state with ROLLBACK so a fresh
                        # BEGIN can start cleanly. If ROLLBACK also fails, fall back
                        # to autocommit for the rest of the scan.
                        try:
                            conn.execute("ROLLBACK")
                        except Exception:
                            pass
                    try:
                        conn.execute("BEGIN")
                    except Exception:
                        in_tx = False  # remaining INSERTs autocommit individually
            except Exception:
                failed += 1

            # ── Persist intra-file gaps (best-effort — never aborts scan) ──
            if intra_file_gaps_data:
                try:
                    conn.execute("DELETE FROM intra_file_gaps WHERE path=?", (str(p),))
                    conn.executemany(
                        """INSERT INTO intra_file_gaps
                           (path, station, gap_start_utc, gap_end_utc, gap_epochs, gap_seconds)
                           VALUES(?,?,?,?,?,?)""",
                        [
                            (
                                str(p), station,
                                g["gap_start_utc"], g["gap_end_utc"],
                                g["gap_epochs"],    g["gap_seconds"],
                            )
                            for g in intra_file_gaps_data
                        ],
                    )
                except Exception:
                    pass

        if in_tx:
            try:
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        _PIPELINE_LOCK.release()

    return db_path


def has_convbin_cfg(cfg: PipelineConfig) -> bool:
    return bool(cfg.convbin_path and cfg.convbin_path.exists())


def _generate_coverage_gaps(df: pd.DataFrame, out_path: Path) -> None:
    """
    Write coverage_gaps.csv: per-station contiguous blocks of missing hourly slots.
    Uses filename_date + filename_hour. Columns: station, gap_start_utc, gap_end_utc, gap_hours.
    """
    gap_cols = ["station", "gap_start_utc", "gap_end_utc", "gap_hours"]
    empty = pd.DataFrame(columns=gap_cols)

    needed = {"station", "filename_date", "filename_hour"}
    if df.empty or not needed.issubset(df.columns):
        empty.to_csv(out_path, index=False)
        return

    tdf = df[df["filename_date"].notna() & df["filename_hour"].notna()].copy()
    if tdf.empty:
        empty.to_csv(out_path, index=False)
        return

    tdf["filename_date"] = tdf["filename_date"].astype(str)
    tdf["filename_hour"] = tdf["filename_hour"].astype(int)

    covered: set[tuple[str, str, int]] = set(
        zip(tdf["station"], tdf["filename_date"], tdf["filename_hour"])
    )

    gap_rows: list[dict] = []
    for station, sdf in tdf.groupby("station"):
        try:
            min_ts = pd.Timestamp(sdf["filename_date"].min())
            max_ts = pd.Timestamp(sdf["filename_date"].max())
        except Exception:
            continue

        cur = min_ts
        end = max_ts + pd.Timedelta(hours=23)
        gap_start: Optional[pd.Timestamp] = None

        while cur <= end:
            date_str = cur.date().isoformat()
            hour     = cur.hour
            has_file = (station, date_str, hour) in covered
            if not has_file:
                if gap_start is None:
                    gap_start = cur
            else:
                if gap_start is not None:
                    gap_end   = cur - pd.Timedelta(hours=1)
                    gap_hours = int((cur - gap_start).total_seconds() / 3600)
                    gap_rows.append({
                        "station":       station,
                        "gap_start_utc": gap_start.isoformat(),
                        "gap_end_utc":   gap_end.isoformat(),
                        "gap_hours":     gap_hours,
                    })
                    gap_start = None
            cur += pd.Timedelta(hours=1)

        if gap_start is not None:
            gap_end   = end
            gap_hours = int((end - gap_start).total_seconds() / 3600) + 1
            gap_rows.append({
                "station":       station,
                "gap_start_utc": gap_start.isoformat(),
                "gap_end_utc":   gap_end.isoformat(),
                "gap_hours":     gap_hours,
            })

    result = pd.DataFrame(gap_rows, columns=gap_cols) if gap_rows else empty
    result.to_csv(out_path, index=False)


def _export_intra_file_gaps(db_path: Path, out_path: Path) -> None:
    """Export intra_file_gaps table → CSV. Always writes the file (empty if no gaps)."""
    cols = ["file_name", "station", "gap_start_utc", "gap_end_utc", "gap_epochs", "gap_seconds"]
    conn = _db_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT path, station, gap_start_utc, gap_end_utc, gap_epochs, gap_seconds
               FROM intra_file_gaps
               ORDER BY station, gap_start_utc"""
        ).fetchall()
        df = pd.DataFrame([dict(r) for r in rows])
    finally:
        conn.close()

    if df.empty:
        pd.DataFrame(columns=cols).to_csv(out_path, index=False)
        return

    df["file_name"] = df["path"].astype(str).map(lambda s: Path(s).name)
    df.drop(columns=["path"], inplace=True)
    df = df[cols]
    df.to_csv(out_path, index=False)


def export_manifests(db_path: Path, out_dir: Path) -> Path:
    """
    Export scan cache SQLite into manifests expected by dashboard.py:
      _manifests/files_manifest.csv
      _manifests/coverage_gaps.csv
      _manifests/intra_file_gaps.csv
      _manifests/summary.json
    """
    out_dir  = out_dir.resolve()
    manifests = out_dir / "_manifests"
    manifests.mkdir(parents=True, exist_ok=True)

    conn = _db_connect(db_path)
    _db_init(conn)
    try:
        rows = conn.execute(
            """
            SELECT
              station,
              station                              AS prefix,
              path                                 AS file_name,
              size_bytes,
              datetime(mtime, 'unixepoch')         AS modified_utc,
              path                                 AS discovered_from,
              filename_date                        AS inferred_date,
              filename_hour,
              duration_s,
              interval_s,
              total_epochs,
              expected_epochs,
              completeness_pct,
              intra_file_gap_count,
              NULL                                 AS rinex_version,
              NULL                                 AS rinex_file_type,
              constellations,
              signals,
              lat,
              lon,
              height_m,
              NULL                                 AS ecef_x,
              NULL                                 AS ecef_y,
              NULL                                 AS ecef_z
            FROM files
            """
        ).fetchall()
        df = pd.DataFrame([dict(r) for r in rows])
    finally:
        conn.close()

    expected_cols = [
        "station", "prefix", "file_name", "ext", "size_bytes", "modified_utc",
        "discovered_from", "inferred_date", "filename_hour", "duration_s",
        "interval_s", "total_epochs", "expected_epochs", "completeness_pct",
        "intra_file_gap_count",
        "rinex_version", "rinex_file_type",
        "constellations", "signals", "lat", "lon", "height_m",
        "ecef_x", "ecef_y", "ecef_z",
    ]
    if df.empty:
        df = pd.DataFrame({c: pd.Series(dtype="object") for c in expected_cols})
    else:
        df["file_name"]       = df["file_name"].astype(str).map(lambda s: Path(s).name)
        df["discovered_from"] = df["discovered_from"].astype(str).map(lambda s: str(Path(s).parent))
        df["ext"]             = df["file_name"].map(lambda s: Path(s).suffix.lower())

    csv_path = manifests / "files_manifest.csv"
    df.to_csv(csv_path, index=False)

    gaps_csv        = manifests / "coverage_gaps.csv"
    intra_gaps_csv  = manifests / "intra_file_gaps.csv"
    _generate_coverage_gaps(df, gaps_csv)
    _export_intra_file_gaps(db_path, intra_gaps_csv)

    if df.empty:
        by_station = {}
        by_ext     = {}
        total_bytes        = 0
        unique_prefixes    = 0
        unique_exts        = 0
    else:
        by_station   = df["station"].value_counts().to_dict()
        by_ext       = df["ext"].value_counts().to_dict() if "ext" in df.columns else {}
        total_bytes  = int(df["size_bytes"].fillna(0).sum()) if "size_bytes" in df.columns else 0
        unique_prefixes = int(df["station"].nunique())
        unique_exts  = int(df["ext"].nunique()) if "ext" in df.columns else 0

    summary = {
        "paths_checked":       int(len(df)),
        "files_in_manifest":   int(len(df)),
        "total_bytes":         total_bytes,
        "unique_prefixes":     unique_prefixes,
        "unique_exts":         unique_exts,
        "by_constellation_counts": {},
        "by_ext_counts":       by_ext,
        "by_prefix_counts":    {str(k).lower(): int(v) for k, v in by_station.items()},
        "generated_utc":       _utc_now_iso(),
        "root":                str(out_dir),
        "out_dir":             str(out_dir),
        "manifests_dir":       str(manifests),
        "include_ext":         sorted(list(TO_EXTS)),
        "exclude_ext":         None,
        "prefix_regex":        STATION_RE.pattern,
        "parse_rinex_headers": True,
        "source":              str(db_path),
        "coverage_gaps_csv":   str(gaps_csv),
        "intra_file_gaps_csv": str(intra_gaps_csv),
    }
    (manifests / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return manifests
