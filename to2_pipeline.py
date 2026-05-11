from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

# Serialises pipeline runs within a process — prevents Streamlit rerun races
# from opening two write connections to the same DB simultaneously.
_PIPELINE_LOCK = threading.Lock()


TO_EXTS = {".to2", ".t02", ".to4", ".t04"}
STATION_RE = re.compile(r"^([A-Za-z]{3,4})")

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
    if not m:
        return "UNKNOWN"
    return m.group(1).upper()


# ── Filename timestamp parsing ────────────────────────────────────────────────
_FN_DT_MMDD = re.compile(
    r"[A-Za-z]{0,4}"
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
_FN_DT_DOY = re.compile(
    r"[A-Za-z]{0,4}"
    r"((?:19|20)\d{2})"
    r"(00[1-9]|0[1-9]\d|[12]\d{2}|3[0-5]\d|36[0-6])"
    r"([01]\d|2[0-3])"
    r"\d{2}"
    r"(?:\d{2})?"
    r"[a-zA-Z]?"
    r"(?=\.)",
    re.IGNORECASE,
)


def _parse_filename_dt(name: str) -> tuple[Optional[str], Optional[int]]:
    """Return (iso_date, hour) from Trimble filename, or (None, None)."""
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

    return None, None


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
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # busy_timeout must come before any write — including journal_mode change.
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
    conn.commit()

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
            conn.commit()
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
        s = int(sec)
        us = int(round((sec - s) * 1_000_000))
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
        s = int(sec)
        us = int(round((sec - s) * 1_000_000))
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


_VALID_SYS = set("GREJCISG")


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
            capture_output=True, text=True, timeout=120, check=False,
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
            capture_output=True, text=True, timeout=120, check=False,
            **_SUBPROC_KW,
        )
    except subprocess.TimeoutExpired:
        raise ConverterError("runpkr00 timed out (120s)")
    except Exception as e:
        raise ConverterError(f"runpkr00 failed to launch: {e}")

    if not dat.exists():
        tw = Path(tempfile.mkdtemp(prefix="runpkr_gd_", dir=str(out_dir)))
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


def _convbin_on_dat(convbin_path: Path, dat: Path, obs_path: Path) -> None:
    """
    Convert runpkr00 .dat (RT17 format) → RINEX 3 obs + nav using convbin.
    Nav file is written alongside obs with .nav extension (used by SPP solver).
    Raises ConverterError on failure.
    """
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
            capture_output=True, text=True, timeout=180, check=False,
            **_SUBPROC_KW,
        )
    except subprocess.TimeoutExpired:
        raise ConverterError("convbin -r rt17 timed out (180s)")
    except Exception as e:
        raise ConverterError(f"convbin failed to launch: {e}")
    if not (obs_path.exists() and obs_path.stat().st_size > 0):
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
            capture_output=True, text=True, timeout=120, check=False,
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
    mid = len(lats) // 2
    return sorted(lats)[mid], sorted(lons)[mid], sorted(hgts)[mid]



def _nonempty_obs(out_dir: Path) -> Optional[Path]:
    candidates = list(out_dir.glob("*.obs")) + list(out_dir.glob("*.??o"))
    candidates = [c for c in candidates if c.exists() and c.stat().st_size > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x.stat().st_mtime)


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
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180, **_SUBPROC_KW)
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
    conn = _db_connect(db_path)
    try:
        _db_init(conn)
        if cfg.max_files_per_station is not None:
            files = _pick_probe_files(
                cfg.data_root,
                max_files_per_station=cfg.max_files_per_station,
                max_total_files=cfg.probe_max_total_files,
                exclude_dirs=exclude,
            )
        else:
            files = list(_iter_to_files(cfg.data_root, exclude_dirs=exclude))
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

        for i, p in enumerate(files, start=1):
            if progress_cb:
                progress_cb(i, total, str(p))

            station  = _station_from_filename(p.name)
            fn_date, fn_hour = _parse_filename_dt(p.name)

            if cfg.stop_after_success_per_station and station in success_by_station:
                continue
            if cfg.max_files_per_station is not None:
                n = attempted_by_station.get(station, 0)
                if n >= cfg.max_files_per_station:
                    continue

            # Per-file variables — initialised here so INSERT always has values
            # regardless of which code path (empty/cached/converted/failed) is taken.
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

                    out_dir = rinex_dir / station
                    rinex_obs      = None
                    convert_status = "skipped"
                    convert_detail = None
                    has_converter  = bool(
                        cfg.convert_cmd_template
                        or (cfg.runpkr00_path and cfg.runpkr00_path.exists()
                            and has_convbin_cfg(cfg))
                    )
                    if has_converter:
                        try:
                            rinex_obs = convert_to_rinex(cfg, p, out_dir)
                        except ConverterError as ce:
                            rinex_obs      = None
                            convert_status = "failed"
                            convert_detail = str(ce)
                        else:
                            convert_status = "ok" if rinex_obs else "failed"
                            if not rinex_obs:
                                convert_detail = "no converter configured for this file"

                    attempted_by_station[station] = attempted_by_station.get(station, 0) + 1
                    if convert_status == "ok":
                        success_by_station.add(station)

                    t_first = t_last = None
                    lat = lon = h_m = None
                    consts = sigs = None
                    dur_s = None

                    if rinex_obs and rinex_obs.exists():
                        hdr_lines = _read_rinex_header_lines(rinex_obs)
                        for ln in hdr_lines:
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
                        consts, sigs = _parse_rinex_signals(hdr_lines)
                        dur_s = _duration_s(t_first, t_last)

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

            except Exception as e:
                size_b, mtime  = _file_sig(p)
                rinex_obs      = None
                convert_status = "failed"
                convert_detail = f"{type(e).__name__}: {e}"
                t_first = t_last = None
                lat = lon = h_m = None
                consts = sigs   = None
                dur_s           = None
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
                if processed % 250 == 0:
                    try:
                        conn.commit()
                    except Exception:
                        pass
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

        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
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
