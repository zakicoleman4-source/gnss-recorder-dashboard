from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import json


TO_EXTS = {".to2", ".t02", ".to4", ".t04"}
STATION_RE = re.compile(r"^([A-Za-z]{3,4})")

_THIS_DIR = Path(__file__).resolve().parent


def _debug_log_dir() -> Path:
    """Where to put the debug log files. See dashboard.py for the same logic."""
    override = os.environ.get("GNSS_DEBUG_DIR", "").strip()
    if override:
        try:
            p = Path(override).expanduser().resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass
    try:
        _THIS_DIR.mkdir(parents=True, exist_ok=True)
        return _THIS_DIR
    except Exception:
        return Path.cwd()


# #region agent log
def _dbg(hypothesis_id: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": "c48812",
            "runId": "pre-fix",
            "hypothesisId": hypothesis_id,
            "location": "to2_pipeline.py",
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        log_dir = _debug_log_dir()
        (log_dir / "debug-c48812.log").open("a", encoding="utf-8").write(json.dumps(payload) + "\n")
        kv = " ".join([f"{k}={repr(v)[:200]}" for k, v in (data or {}).items()])
        line = f"{payload['timestamp']} | {hypothesis_id} | {message} | {kv}\n"
        (log_dir / "debug-c48812_readable.txt").open("a", encoding="utf-8").write(line)
    except Exception:
        pass
# #endregion agent log


@dataclass(frozen=True)
class PipelineConfig:
    data_root: Path
    cache_dir: Path
    convbin_path: Optional[Path] = None
    runpkr00_path: Optional[Path] = None
    teqc_path: Optional[Path] = None
    rinex_ver: str = "3.04"
    # If set, limits how many files we attempt per station during run_pipeline.
    # Useful for "probe" mode to quickly extract station coords/signals.
    max_files_per_station: Optional[int] = None
    # If True, once a station has a successful conversion, skip remaining files for that station.
    # Default is False so that "FULL scan" really processes every file. Probe mode in the
    # dashboard explicitly sets this to True.
    stop_after_success_per_station: bool = False
    # In probe mode, stop scanning after examining at most this many TO files.
    # Generous default: high enough that very large archives still discover every station,
    # but bounded so a corrupt symlink loop can't spin forever.
    probe_max_total_files: int = 200_000
    # Optional: override conversion with a custom command template.
    # Use {input} and {out_dir} placeholders.
    convert_cmd_template: Optional[str] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_to_files(root: Path, exclude_dirs: Optional[Iterable[Path]] = None) -> Iterable[Path]:
    """
    Walk `root` and yield TO2/T02/TO4/T04 files (case-insensitive).

    `exclude_dirs` is an optional iterable of subtrees to skip (e.g. our own
    cache_dir if it lives inside data_root). We compare resolved paths so a
    subfolder of an excluded dir is also skipped.

    Robustness:
    - Tolerates bad symlinks / permission errors per file (continue, do not crash).
    - Skips known excluded subtrees in-place by mutating `dirnames`.
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
        # Prune excluded subtrees in-place so os.walk doesn't descend into them.
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
    """
    Fast "probe" selector: pick up to N files per station prefix without
    scanning the entire archive.

    `max_total_files` is a safety cap (default huge) so a corrupted/symlink-loop
    folder can't spin forever. We do NOT stop early on station count: we keep
    examining until we hit the cap or finish the walk, so we never silently
    miss a station that only appears late in the walk order.
    """
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
# Trimble T02/TO2 files embed the recording start time in the filename.
# Two common layouts:
#   MMDD layout: SSSSYYYYMMDDHH[MM[SS]][a].T02  e.g. AHTI202603010000a.T02
#   DOY  layout: SSSSYYYYDDDHHMMSS[a].T02       e.g. AHTI20260600000a.T02

_FN_DT_MMDD = re.compile(
    r"[A-Za-z]{0,4}"              # station prefix (0–4 letters, may be absent)
    r"((?:19|20)\d{2})"           # year (4 digits)
    r"(0[1-9]|1[0-2])"            # month 01–12
    r"(0[1-9]|[12]\d|3[01])"      # day 01–31
    r"([01]\d|2[0-3])"            # hour 00–23
    r"\d{2}"                      # minute (ignored)
    r"(?:\d{2})?"                 # optional second
    r"[a-zA-Z]?"                  # optional session letter (Trimble convention)
    r"(?=\.)",                    # lookahead: must be followed by extension dot
    re.IGNORECASE,
)

_FN_DT_DOY = re.compile(
    r"[A-Za-z]{0,4}"
    r"((?:19|20)\d{2})"           # year
    r"(00[1-9]|0[1-9]\d|[12]\d{2}|3[0-5]\d|36[0-6])"  # day-of-year 001–366
    r"([01]\d|2[0-3])"            # hour
    r"\d{2}"                      # minute
    r"(?:\d{2})?"                 # optional second
    r"[a-zA-Z]?"
    r"(?=\.)",
    re.IGNORECASE,
)


def _parse_filename_dt(name: str) -> tuple[Optional[str], Optional[int]]:
    """
    Return ``(iso_date, hour)`` parsed from a Trimble T02 filename, or
    ``(None, None)`` if the filename does not match a known layout.

    Tries MMDD layout first (most common), then DOY layout.
    """
    import datetime as _dt

    m = _FN_DT_MMDD.search(name)
    if m:
        try:
            y, mo, d, h = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            date_str = _dt.date(y, mo, d).isoformat()
            return date_str, h
        except (ValueError, OverflowError):
            pass

    m = _FN_DT_DOY.search(name)
    if m:
        try:
            y, doy, h = int(m.group(1)), int(m.group(2)), int(m.group(3))
            date_str = (_dt.date(y, 1, 1) + _dt.timedelta(days=doy - 1)).isoformat()
            return date_str, h
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


def _file_sig(p: Path) -> tuple[int, int]:
    try:
        st = p.stat()
        return int(st.st_size), int(st.st_mtime)
    except OSError:
        return 0, 0


def _db_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def _db_init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
          path TEXT PRIMARY KEY,
          station TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          mtime INTEGER NOT NULL,
          rinex_obs_path TEXT,
          time_first_obs TEXT,
          time_last_obs TEXT,
          lat REAL,
          lon REAL,
          height_m REAL,
          constellations TEXT,
          signals TEXT,
          convert_status TEXT,
          convert_detail TEXT,
          filename_date TEXT,
          filename_hour INTEGER,
          duration_s REAL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_files_station_first ON files(station, time_first_obs);
        CREATE INDEX IF NOT EXISTS idx_files_station_date  ON files(station, filename_date, filename_hour);
        """
    )
    conn.commit()
    # Migrate existing DBs that pre-date these columns (ALTER TABLE is a no-op if
    # the column already exists in SQLite >= 3.37; for older SQLite we catch and ignore).
    for col, typedef in [
        ("filename_date", "TEXT"),
        ("filename_hour", "INTEGER"),
        ("duration_s",    "REAL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE files ADD COLUMN {col} {typedef}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists


def _read_rinex_header_lines(path: Path, max_bytes: int = 256 * 1024) -> list[str]:
    lines: list[str] = []
    with path.open("rb") as f:
        data = f.read(max_bytes)
    try:
        text = data.decode("ascii", errors="ignore")
    except Exception:
        text = ""
    for ln in text.splitlines():
        lines.append(ln.rstrip("\r\n"))
        if "END OF HEADER" in ln:
            break
    return lines


def _parse_rinex_time(line: str) -> Optional[pd.Timestamp]:
    """
    RINEX "TIME OF FIRST/LAST OBS" header line.

    Spec layout: 6I6, F13.7, 5X, A3 (year, month, day, hour, min, sec, ..., sys).
    Reality: many vendors leave varying whitespace. We try fixed columns first,
    fall back to a tolerant numeric scan that explicitly clips off the
    "TIME OF ..." trailing label so we never mis-parse it as a number.
    """
    if not line:
        return None
    # Strip the trailing label so regex doesn't pick up digits inside it.
    head = line[:60]
    # First try strict spec columns
    try:
        y = int(head[0:6].strip())
        mo = int(head[6:12].strip())
        d = int(head[12:18].strip())
        h = int(head[18:24].strip())
        mi = int(head[24:30].strip())
        sec = float(head[30:43].strip())
        s = int(sec)
        us = int(round((sec - s) * 1_000_000))
        # Sanity check on year so we don't accept "0001" from junk data.
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and 0 <= h < 24 and 0 <= mi < 60:
            return pd.Timestamp(year=y, month=mo, day=d, hour=h, minute=mi, second=s, microsecond=us, tz="UTC")
    except Exception:
        pass
    # Fallback: tolerant numeric scan (still bounded to the columns area).
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
    "APPROX POSITION XYZ" header line. Spec: 3F14.4 in cols 1..42.
    Real-world files sometimes use scientific notation; tolerate that.
    Reject obviously bad triples (all-zero, NaN, or radius outside Earth bounds).
    """
    if not line:
        return None
    head = line[:60]
    # Try fixed columns first.
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
    # Reject points clearly not on/near Earth (radius far outside ~6.4M m).
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

    a = 6_378_137.0           # WGS84 semi-major axis (m)
    f = 1.0 / 298.257_223_563  # flattening
    e2 = f * (2.0 - f)        # first eccentricity squared
    b = a * (1.0 - f)         # semi-minor axis

    lon = math.atan2(y, x)
    p = math.hypot(x, y)

    # Polar singularity: point is on or very near the z-axis.
    if p < 1.0:
        sign = 1.0 if z >= 0.0 else -1.0
        return sign * 90.0, math.degrees(lon), abs(z) - b

    # Bowring iterative method — converges in 3–4 iterations for any lat/height.
    # Starting approximation: geocentric latitude reduced by e².
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
    # Dual formula: equatorial branch avoids /0 near poles; polar branch near equator.
    if abs(cos_lat) >= abs(sin_lat):
        h = p / cos_lat - n
    else:
        h = z / sin_lat - n * (1.0 - e2)
    return math.degrees(lat), math.degrees(lon), h


_VALID_SYS = set("GREJCISG")  # GPS, GLO, GAL, QZSS, BDS, IRNSS, SBAS


def _parse_rinex_signals(lines: list[str]) -> tuple[str | None, str | None]:
    """
    Collect constellation letters and observation codes from RINEX header.

    RINEX3 spec: "SYS / # / OBS TYPES" rows have the constellation letter in
    column 1 (G/R/E/J/C/I/S). Continuation lines start with a blank in col 1.

    We only accept a single uppercase letter from `_VALID_SYS` so that
    comments / weird whitespace don't pollute the constellation list.
    """
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
                # Continuation line for previous system.
                pass
            for t in re.findall(r"\b[A-Z][0-9][A-Z]\b", ln):
                sigs.add(t)
        elif "# / TYPES OF OBSERV" in ln:
            # RINEX2: no constellation; assume GPS by convention.
            consts.add("G")
            for t in re.findall(r"\b[A-Z][0-9][A-Z]\b", ln):
                sigs.add(t)
    c = ",".join(sorted(consts)) if consts else None
    s = ",".join(sorted(sigs)) if sigs else None
    return c, s


def _safe_stem(p: Path) -> str:
    # Keep filenames filesystem-safe and short-ish (Windows path length matters).
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", p.stem)
    stem = stem.strip("._-") or "file"
    # add a short stable hash to avoid collisions between same stems
    h = hashlib.sha1(str(p).encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"{stem}_{h}"


class ConverterError(RuntimeError):
    """Raised internally so run_pipeline can record a useful detail message."""


def _short_err(p: "subprocess.CompletedProcess[str]", limit: int = 240) -> str:
    err = (p.stderr or "").strip() or (p.stdout or "").strip()
    if not err:
        return f"exit={p.returncode}"
    return f"exit={p.returncode}: {err[:limit]}"


# Subprocess defaults shared by every external converter call:
#  - stdin=DEVNULL: prevents a tool that reads stdin from hanging forever
#    waiting for input that will never come (we noticed runpkr00 doing this on
#    a few client machines).
#  - On Windows, also hide the console window so the dashboard UI doesn't
#    flicker every time we spawn a converter (CREATE_NO_WINDOW).
_SUBPROC_KW: dict = {"stdin": subprocess.DEVNULL}
if os.name == "nt":
    _SUBPROC_KW["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _runpkr00_gd_first_dat_or_tgd(runpkr00_path: Path, inp: Path, work_dir: Path) -> Optional[Path]:
    """
    Alternate Trimble decompress: ``runpkr00 -g -d`` (same spirit as scan_gnss_folder).

    Returns the first ``.dat`` or ``.tgd`` in ``work_dir``, or None when nothing is produced.
    """
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
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            **_SUBPROC_KW,
        )
    except (subprocess.TimeoutExpired, Exception):
        return None
    hits = sorted(work_dir.glob("*.dat")) + sorted(work_dir.glob("*.tgd"))
    return hits[0] if hits else None


def _teqc_trimble_to_obs(
    teqc_path: Path,
    rinex_o: Path,
    rinex_n: Path,
    dat: Path,
    eph: Optional[Path],
) -> None:
    """
    Build a non-empty RINEX ``.o``. Tries several teqc argv styles seen in the wild.

    - Prefer ``+obs`` + ``+nav`` when a ``.eph`` exists (-devg path).
    - Fall back to ``teqc +obs out.o raw.dat`` (matches station-capability scrape).
    - teqc frequently returns non-zero yet still writes a valid ``.o`` (warnings): we
      treat non-empty ``rinex_o`` as success regardless of exit code (same pragmatic
      rule already used for runpkr00 + ``.dat/.eph`` existence).
    """
    for fp in (rinex_o, rinex_n):
        try:
            fp.unlink(missing_ok=True)
        except OSError:
            pass

    recipes: list[list[str]] = []
    if eph is not None and eph.exists():
        recipes.append(
            [
                str(teqc_path),
                "+obs",
                str(rinex_o),
                "+nav",
                str(rinex_n),
                str(dat),
                str(eph),
            ]
        )
    recipes.append([str(teqc_path), "+obs", str(rinex_o), str(dat)])
    if eph is not None and eph.exists():
        recipes.append([str(teqc_path), "+obs", str(rinex_o), str(dat), str(eph)])

    cwd = str(rinex_o.parent)
    last_err = "no subprocess result"
    for cmd in recipes:
        try:
            last_cp = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=180,
                **_SUBPROC_KW,
            )
            last_err = _short_err(last_cp)
        except subprocess.TimeoutExpired:
            raise ConverterError("teqc timed out (180s)")
        except Exception as e:
            raise ConverterError(f"teqc failed to launch: {e}")
        if rinex_o.exists() and rinex_o.stat().st_size > 0:
            return
    raise ConverterError(f"teqc produced no non-empty .o ({last_err})")


def _nonempty_obs(out_dir: Path) -> Optional[Path]:
    """Return the newest non-empty RINEX .obs / .??o file, or None."""
    candidates = list(out_dir.glob("*.obs")) + list(out_dir.glob("*.??o"))
    candidates = [c for c in candidates if c.exists() and c.stat().st_size > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x.stat().st_mtime)


def convert_to_rinex(cfg: PipelineConfig, inp: Path, out_dir: Path) -> Optional[Path]:
    """
    Convert a vendor binary (T02/T04) to RINEX. Returns the .obs path on success.

    Raises ConverterError(str) on a "real" failure so callers can record a
    detail message. Returns None only when no converter is configured.
    """
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

    # Preferred path for Trimble .T02/.T04: runpkr00 -> teqc
    if cfg.runpkr00_path and cfg.runpkr00_path.exists() and cfg.teqc_path and cfg.teqc_path.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        base = out_dir / _safe_stem(inp)
        dat = Path(str(base) + ".dat")
        eph = Path(str(base) + ".eph")
        rinex_o = Path(str(base) + ".o")
        rinex_n = Path(str(base) + ".n")

        # Clean any leftovers from previous runs (do NOT remove rinex_o yet --
        # we rely on its post-teqc size to detect garbage output).
        for fp in (dat, eph, rinex_o, rinex_n):
            try:
                fp.unlink(missing_ok=True)
            except Exception:
                pass

        # runpkr00 sometimes returns a crash code even when it produced outputs;
        # treat "dat+eph exist" as success.
        run_cmd = [str(cfg.runpkr00_path), "-devg", str(inp), str(base)]
        try:
            p1 = subprocess.run(
                run_cmd,
                cwd=str(out_dir),
                capture_output=True,
                text=True,
                timeout=120,
                **_SUBPROC_KW,
            )
        except subprocess.TimeoutExpired:
            raise ConverterError("runpkr00 timed out (120s)")
        except Exception as e:
            raise ConverterError(f"runpkr00 failed to launch: {e}")

        # If -devg did not emit .dat (common with some receivers), retry with -g -d
        # and copy intermediate into the paths teqc expects.
        if not dat.exists():
            tw = Path(tempfile.mkdtemp(prefix="runpkr_gd_", dir=str(out_dir)))
            try:
                alt = _runpkr00_gd_first_dat_or_tgd(cfg.runpkr00_path, inp, tw)
                if alt is None:
                    raise ConverterError(f"runpkr00 produced no .dat/.tgd (-devg nor -g -d) ({_short_err(p1)})")
                try:
                    shutil.copy2(alt, dat)
                except OSError as e:
                    raise ConverterError(f"copy Trimble intermediate: {e}")
            finally:
                shutil.rmtree(tw, ignore_errors=True)

        elif not eph.exists():
            # Some firmware leaves .dat but no matching .eph; teqc falls back recipes handle obs-only.
            pass

        eph_arg: Optional[Path] = eph if eph.exists() else None
        try:
            _teqc_trimble_to_obs(cfg.teqc_path, rinex_o, rinex_n, dat, eph_arg)
        except ConverterError:
            raise
        return rinex_o

    if not cfg.convbin_path or not cfg.convbin_path.exists():
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(cfg.convbin_path), str(inp), "-od", str(out_dir), "-v", cfg.rinex_ver]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, **_SUBPROC_KW)
    except subprocess.TimeoutExpired:
        raise ConverterError("convbin timed out (180s)")
    except Exception as e:
        raise ConverterError(f"convbin failed to launch: {e}")
    if p.returncode != 0:
        raise ConverterError(f"convbin {_short_err(p)}")
    obs = _nonempty_obs(out_dir)
    if obs is None:
        raise ConverterError("convbin produced no non-empty .obs")
    return obs


def run_pipeline(cfg: PipelineConfig, progress_cb=None) -> Path:
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = cfg.cache_dir / "scan_cache.sqlite"
    rinex_dir = cfg.cache_dir / "rinex"
    rinex_dir.mkdir(parents=True, exist_ok=True)

    # Skip our own cache directory while scanning so we don't accidentally
    # ingest converted/intermediate files if cache_dir lives under data_root.
    exclude = [cfg.cache_dir]

    conn = _db_connect(db_path)
    try:
        _db_init(conn)
        mode = "probe" if cfg.max_files_per_station is not None else "full"
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
        _dbg(
            "A",
            "pipeline_file_list_built",
            {
                "mode": mode,
                "data_root": str(cfg.data_root),
                "files_len": int(len(files)),
                "probe_max_total_files": int(getattr(cfg, "probe_max_total_files", -1)),
                "max_files_per_station": cfg.max_files_per_station,
                "stop_after_success_per_station": bool(cfg.stop_after_success_per_station),
            },
        )

        attempted_by_station: dict[str, int] = {}
        success_by_station: set[str] = set()
        cache_hits = 0
        processed = 0
        failed = 0
        skipped_empty = 0

        for i, p in enumerate(files, start=1):
            if progress_cb:
                progress_cb(i, total, str(p))

            station = _station_from_filename(p.name)
            fn_date, fn_hour = _parse_filename_dt(p.name)

            # Optional quick-probe mode: only try a small number of files per station,
            # and (by default) stop after first successful conversion per station.
            if cfg.stop_after_success_per_station and station in success_by_station:
                continue
            if cfg.max_files_per_station is not None:
                n = attempted_by_station.get(station, 0)
                if n >= cfg.max_files_per_station:
                    continue

            # Robustness: do not let a single bad file crash the scan.
            try:
                size_b, mtime = _file_sig(p)
                if size_b <= 0:
                    # Keep a row so the user can see "what happened" in the cache/manifests.
                    rinex_obs = None
                    convert_status = "skipped"
                    convert_detail = "empty or unreadable file (0 bytes or stat failed)"
                    t_first = t_last = None
                    lat = lon = h_m = None
                    consts = sigs = None
                    dur_s = None
                    skipped_empty += 1
                else:
                    row = conn.execute("SELECT size_bytes, mtime FROM files WHERE path=?", (str(p),)).fetchone()
                    if row and int(row["size_bytes"]) == size_b and int(row["mtime"]) == mtime:
                        cache_hits += 1
                        continue

                    # Convert file into a deterministic subfolder by station
                    out_dir = rinex_dir / station
                    rinex_obs = None
                    convert_status = "skipped"
                    convert_detail = None
                    has_converter = bool(
                        cfg.convert_cmd_template
                        or (cfg.convbin_path and cfg.convbin_path.exists())
                        or (cfg.runpkr00_path and cfg.runpkr00_path.exists() and cfg.teqc_path and cfg.teqc_path.exists())
                    )
                    if has_converter:
                        try:
                            rinex_obs = convert_to_rinex(cfg, p, out_dir)
                        except ConverterError as ce:
                            rinex_obs = None
                            convert_status = "failed"
                            convert_detail = str(ce)
                        else:
                            if rinex_obs:
                                convert_status = "ok"
                            else:
                                convert_status = "failed"
                                convert_detail = "no converter configured for this file"

                    attempted_by_station[station] = attempted_by_station.get(station, 0) + 1
                    if convert_status == "ok":
                        success_by_station.add(station)

                    t_first = t_last = None
                    lat = lon = h_m = None
                    consts = sigs = None
                    dur_s = None
                    if rinex_obs and rinex_obs.exists():
                        lines = _read_rinex_header_lines(rinex_obs)
                        for ln in lines:
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
                        consts, sigs = _parse_rinex_signals(lines)
                        dur_s = _duration_s(t_first, t_last)
            except Exception as e:
                # Record failure for this file and continue.
                size_b, mtime = _file_sig(p)
                out_dir = rinex_dir / station
                rinex_obs = None
                convert_status = "failed"
                convert_detail = f"{type(e).__name__}: {e}"
                t_first = t_last = None
                lat = lon = h_m = None
                consts = sigs = None
                dur_s = None
                failed += 1
            processed += 1

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
                      updated_at
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                      updated_at=excluded.updated_at
                    """,
                    (
                        str(p),
                        station,
                        size_b,
                        mtime,
                        str(rinex_obs) if rinex_obs else None,
                        t_first,
                        t_last,
                        lat,
                        lon,
                        h_m,
                        consts,
                        sigs,
                        convert_status,
                        convert_detail,
                        fn_date,
                        fn_hour,
                        dur_s,
                        _utc_now_iso(),
                    ),
                )
                # Periodic commit so an unexpected crash mid-scan still leaves
                # most rows persisted (instead of rolling back the whole batch).
                if processed % 250 == 0:
                    try:
                        conn.commit()
                    except Exception:
                        pass
            except Exception as ie:
                # A single bad row (locked DB, encoding issue, etc.) must NOT
                # abort the entire scan. Log and keep going.
                _dbg("A", "pipeline_row_insert_failed", {"path": str(p), "err": str(ie)})
                failed += 1
        try:
            conn.commit()
        except Exception:
            pass
        _dbg(
            "A",
            "pipeline_done",
            {
                "mode": mode,
                "files_len": int(len(files)),
                "processed": int(processed),
                "cache_hits": int(cache_hits),
                "failed": int(failed),
                "skipped_empty": int(skipped_empty),
                "success_stations": int(len(success_by_station)),
            },
        )
    finally:
        conn.close()

    return db_path


def _generate_coverage_gaps(df: pd.DataFrame, out_path: Path) -> None:
    """
    Write coverage_gaps.csv: per-station contiguous blocks of missing hourly slots.

    Uses ``filename_date`` + ``filename_hour`` columns (parsed from filenames).
    Only considers the date range actually observed per station so the output
    doesn't balloon on archives with sparse coverage.

    Columns: station, gap_start_utc, gap_end_utc, gap_hours
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

    # Build set of covered (station, date, hour) triples.
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

        # Iterate every hour from first to last day.
        cur = min_ts
        end = max_ts + pd.Timedelta(hours=23)
        gap_start: Optional[pd.Timestamp] = None

        while cur <= end:
            date_str = cur.date().isoformat()
            hour = cur.hour
            has_file = (station, date_str, hour) in covered
            if not has_file:
                if gap_start is None:
                    gap_start = cur
            else:
                if gap_start is not None:
                    gap_end = cur - pd.Timedelta(hours=1)
                    gap_hours = int((cur - gap_start).total_seconds() / 3600)
                    gap_rows.append({
                        "station": station,
                        "gap_start_utc": gap_start.isoformat(),
                        "gap_end_utc": gap_end.isoformat(),
                        "gap_hours": gap_hours,
                    })
                    gap_start = None
            cur += pd.Timedelta(hours=1)

        # Close any gap still open at the end of the range.
        if gap_start is not None:
            gap_end = end
            gap_hours = int((end - gap_start).total_seconds() / 3600) + 1
            gap_rows.append({
                "station": station,
                "gap_start_utc": gap_start.isoformat(),
                "gap_end_utc": gap_end.isoformat(),
                "gap_hours": gap_hours,
            })

    result = pd.DataFrame(gap_rows, columns=gap_cols) if gap_rows else empty
    result.to_csv(out_path, index=False)


def export_manifests(db_path: Path, out_dir: Path) -> Path:
    """
    Export scan cache SQLite into the existing manifests format expected by dashboard.py:
      _manifests/files_manifest.csv
      _manifests/summary.json
    """
    out_dir = out_dir.resolve()
    manifests = out_dir / "_manifests"
    manifests.mkdir(parents=True, exist_ok=True)

    conn = _db_connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
              station,
              station as prefix,
              path as file_name,
              size_bytes,
              datetime(mtime, 'unixepoch') as modified_utc,
              path as discovered_from,
              filename_date as inferred_date,
              filename_hour,
              duration_s,
              NULL as rinex_version,
              NULL as rinex_file_type,
              constellations,
              signals,
              lat,
              lon,
              height_m,
              NULL as ecef_x,
              NULL as ecef_y,
              NULL as ecef_z
            FROM files
            """
        ).fetchall()
        df = pd.DataFrame([dict(r) for r in rows])
    finally:
        conn.close()

    # Normalize to the schema expected by dashboard.py.
    # NOTE: previous implementation used SQL `substr(path, instr(path, '.'))` to
    # derive `ext`, which returns the substring from the FIRST dot. On Windows
    # paths like  C:\my.folder\STATION1234.T02  that produces ".folder\station1234.t02"
    # instead of ".t02", which then poisons the dashboard's extension filter.
    # Compute ext in Python from the file name so we always get the true suffix.
    if df.empty:
        # Make sure all expected columns exist even on empty DB so downstream
        # code (and the dashboard) doesn't KeyError on the first column access.
        expected_cols = [
            "station", "prefix", "file_name", "ext", "size_bytes", "modified_utc",
            "discovered_from", "inferred_date", "filename_hour", "duration_s",
            "rinex_version", "rinex_file_type",
            "constellations", "signals", "lat", "lon", "height_m",
            "ecef_x", "ecef_y", "ecef_z",
        ]
        df = pd.DataFrame({c: pd.Series(dtype="object") for c in expected_cols})
    else:
        df["file_name"] = df["file_name"].astype(str).map(lambda s: Path(s).name)
        df["discovered_from"] = df["discovered_from"].astype(str).map(lambda s: str(Path(s).parent))
        df["ext"] = df["file_name"].map(lambda s: Path(s).suffix.lower())

    csv_path = manifests / "files_manifest.csv"
    df.to_csv(csv_path, index=False)

    _generate_coverage_gaps(df, manifests / "coverage_gaps.csv")

    if df.empty:
        by_station: dict = {}
        by_ext: dict = {}
        total_bytes = 0
        unique_prefixes = 0
        unique_exts = 0
    else:
        by_station = df["station"].value_counts().to_dict()
        by_ext = df["ext"].value_counts().to_dict() if "ext" in df.columns else {}
        total_bytes = int(df["size_bytes"].fillna(0).sum()) if "size_bytes" in df.columns else 0
        unique_prefixes = int(df["station"].nunique())
        unique_exts = int(df["ext"].nunique()) if "ext" in df.columns else 0

    summary = {
        "paths_checked": int(len(df)),
        "files_in_manifest": int(len(df)),
        "total_bytes": total_bytes,
        "unique_prefixes": unique_prefixes,
        "unique_exts": unique_exts,
        "by_constellation_counts": {},
        "by_ext_counts": by_ext,
        "by_prefix_counts": {str(k).lower(): int(v) for k, v in by_station.items()},
        "generated_utc": _utc_now_iso(),
        "root": str(out_dir),
        "out_dir": str(out_dir),
        "manifests_dir": str(manifests),
        "include_ext": sorted(list(TO_EXTS)),
        "exclude_ext": None,
        "prefix_regex": STATION_RE.pattern,
        "parse_rinex_headers": True,
        "source": str(db_path),
        "coverage_gaps_csv": str(manifests / "coverage_gaps.csv"),
    }
    (manifests / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")  # type: ignore[name-defined]
    return manifests

