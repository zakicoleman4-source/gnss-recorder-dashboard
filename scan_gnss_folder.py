from __future__ import annotations

import argparse
import csv
import json
import os
import re
import gzip
import io
import zipfile
import bz2
import struct
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Tuple, Set, Dict, List


# Station naming rule (robust + consistent):
# - Station is derived ONLY from the start of the filename.
# - We trust only the leading letters.
# - Default is first 3–4 letters (common in GNSS station naming).
#   Examples:
#     AUCK1234.T02 -> AUCK
#     abc9.T02     -> ABC
_DEFAULT_PREFIX_REGEX = r"^(?P<prefix>[A-Za-z]{3,4})"
_DATE_TOKEN_REGEX = re.compile(
    r"(?P<date>(?:19|20)\d{2}[-_/]?(?:0[1-9]|1[0-2])[-_/]?(?:0[1-9]|[12]\d|3[01]))"
)

_RINEX_END_HEADER = "END OF HEADER"
_RINEX3_SYS_OBS = "SYS / # / OBS TYPES"
_RINEX2_OBS_TYPES = "# / TYPES OF OBSERV"
_RINEX2_VERSION = "RINEX VERSION / TYPE"

# Hide Windows console pop-ups + close stdin so converters can't hang on input.
_SUBPROC_KW: dict = {"stdin": subprocess.DEVNULL}
if os.name == "nt":
    _SUBPROC_KW["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_BZIP_MAGIC = b"BZh"


@dataclass(frozen=True)
class FileRecord:
    station: str
    station_source: str
    prefix: str
    file_name: str
    ext: str
    size_bytes: int
    modified_utc: str
    discovered_from: str
    inferred_date: Optional[str]
    rinex_version: Optional[str]
    rinex_file_type: Optional[str]
    constellations: Optional[str]
    signals: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    height_m: Optional[float]
    ecef_x: Optional[float]
    ecef_y: Optional[float]
    ecef_z: Optional[float]

@dataclass(frozen=True)
class StationCapability:
    station: str
    sample_file: str
    derived_from: str
    constellations: str
    signals: str


def _iso_utc_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _infer_date_token(path: Path) -> Optional[str]:
    m = _DATE_TOKEN_REGEX.search(str(path))
    if not m:
        return None
    raw = m.group("date")
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) != 8:
        return None
    return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"


_RE_GEONET_4CHAR_YYYYMMDDHHMM = re.compile(r"^(?P<st>[A-Za-z0-9]{4})(?:19|20)\d{10}", re.IGNORECASE)
_RE_RINEX3_MARKER = re.compile(r"^(?P<marker>[A-Za-z0-9]{4,12})_R_20\d{2}\d{3}\d{4}_", re.IGNORECASE)
_RE_RINEX2_SHORT = re.compile(r"^(?P<st>[A-Za-z0-9]{4})\d{3}\d", re.IGNORECASE)  # basc0010
_RE_ANY_LEADING = re.compile(r"^(?P<lead>[A-Za-z]{3,4})")
_RE_DIR_STATION = re.compile(r"^[A-Za-z0-9]{3,12}$")


def _infer_station(file_name: str, discovered_from: str, prefix_re: re.Pattern[str]) -> Tuple[str, str]:
    """
    Robust station inference for messy client data.
    Returns: (station, source)

    Heuristics (first match wins):
    - GeoNet style: STATION(4) + YYYYMMDDHHMM...
    - RINEX3 style: MARKER_R_YYYYDOYHHMM...
    - RINEX2 short: ssssDDDh (e.g., basc0010)
    - Folder hint: use a path segment that looks like a station (3-12 alnum)
    - Generic prefix regex: first 3-12 alnum
    - Fallback: unknown
    """
    fn = file_name.strip()
    rel = (discovered_from or "").replace("\\", "/")

    # Primary rule for this project: station is the leading letters of the filename.
    # We do NOT trust folder names and we ignore trailing digits.
    m = prefix_re.match(fn)
    if m:
        p = (m.groupdict().get("prefix") or m.group(0)).lower()
        return p, "filename_prefix_letters"

    m = _RE_GEONET_4CHAR_YYYYMMDDHHMM.match(fn)
    if m:
        return m.group("st").lower(), "filename_geonet_4char+datetime"

    m = _RE_RINEX3_MARKER.match(fn)
    if m:
        return m.group("marker").lower(), "filename_rinex3_marker"

    stem = Path(fn).stem
    m = _RE_RINEX2_SHORT.match(stem)
    if m:
        return m.group("st").lower(), "filename_rinex2_short"

    m = _RE_ANY_LEADING.match(fn)
    if m:
        return m.group("lead").lower(), "filename_leading_letters"

    return "unknown", "unknown"


def _safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _iter_files(root: Path, exclude_names: Optional[Set[str]] = None) -> Iterable[Path]:
    """
    Walk `root` and yield every file. Skips well-known noise directories so
    repeated runs (which write `_manifests/`, `_cache_*/`, `.venv*/`) don't end
    up scanning their own outputs.

    Path.rglob is very slow on Windows for deep trees -- os.walk is typically
    much faster and gives us filenames directly.
    """
    skip = {
        "_manifests",
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        ".venv_offline",
        "node_modules",
    }
    if exclude_names:
        skip |= set(exclude_names)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None, followlinks=False):
        # Prune in-place; os.walk respects mutation of dirnames.
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith("_cache_")]
        for fn in filenames:
            try:
                yield Path(dirpath) / fn
            except Exception:
                continue


def _looks_like_rinex_header(first_line: str) -> bool:
    # Keep for backwards-compat; we now prefer checking any line.
    return _RINEX2_VERSION in first_line


def _has_rinex_version_line(lines: List[str]) -> bool:
    return any(_RINEX2_VERSION in ln for ln in lines[:50])


def _parse_rinex_header_lines(lines: List[str]) -> Tuple[Optional[str], Optional[str], Set[str], Set[str]]:
    """
    Returns: (version, file_type, constellations, signals)
    - RINEX 3: parse SYS / # / OBS TYPES (signals are the observation codes, e.g. C1C)
    - RINEX 2: parse # / TYPES OF OBSERV; constellation is not encoded -> assume GPS ('G')
    """
    version = None
    file_type = None
    constellations: Set[str] = set()
    signals: Set[str] = set()

    # Version/type line
    for ln in lines[:10]:
        if _RINEX2_VERSION in ln:
            # RINEX2/3: version is cols 0-9; type at col 20 (O/N/M/...)
            version = ln[0:9].strip() or None
            if len(ln) > 20:
                ft = ln[20:21].strip()
                file_type = ft or None
            break

    rinex3_seen = False
    for ln in lines:
        if _RINEX3_SYS_OBS in ln:
            rinex3_seen = True
            sys = ln[0:1].strip()
            if sys:
                constellations.add(sys)
            # obs types start at col 7; fixed width 4 each
            payload = ln[7:60]
            for i in range(0, len(payload), 4):
                code = payload[i:i + 4].strip()
                if code:
                    signals.add(code)
        elif _RINEX2_OBS_TYPES in ln:
            # RINEX2: no constellation info in header
            constellations.add("G")
            payload = ln[6:60]
            for i in range(0, len(payload), 6):
                code = payload[i:i + 6].strip()
                if code:
                    signals.add(code)

        if _RINEX_END_HEADER in ln:
            break

    # If it was RINEX3 but no SYS lines (rare), we still keep version/type.
    if rinex3_seen and not constellations:
        constellations = set()

    return version, file_type, constellations, signals


def _parse_approx_position_xyz(lines: List[str]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Extract APPROX POSITION XYZ from a RINEX header.

    Skips degenerate values (all-zero, NaN/Inf, or not-on-Earth radius). These
    show up in real-world headers and would otherwise:
      - crash _ecef_to_llh_wgs84 (ZeroDivisionError) on the 0,0,0 path; and
      - place a phantom point at (0,0) -- in the Gulf of Guinea -- on the map.
    """
    import math as _m
    for ln in lines:
        if "APPROX POSITION XYZ" in ln:
            fields = ln[:60].split()
            if len(fields) < 3:
                return None, None, None
            try:
                x, y, z = float(fields[0]), float(fields[1]), float(fields[2])
            except ValueError:
                return None, None, None
            if not all(_m.isfinite(v) for v in (x, y, z)):
                return None, None, None
            if x == 0.0 and y == 0.0 and z == 0.0:
                return None, None, None
            r = _m.sqrt(x * x + y * y + z * z)
            if r < 5_000_000.0 or r > 8_000_000.0:
                return None, None, None
            return x, y, z
    return None, None, None


def _ecef_to_llh_wgs84(x: float, y: float, z: float) -> Tuple[float, float, float]:
    """Convert ECEF (m) to geodetic lat/lon (deg) and ellipsoidal height (m) on WGS84."""
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

    # Polar singularity: on or very near the z-axis.
    if p < 1.0:
        sign = 1.0 if z >= 0.0 else -1.0
        return sign * 90.0, math.degrees(lon), abs(z) - b

    # Bowring iterative method.
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
    # Dual formula: avoids division-by-zero at both equator and poles.
    if abs(cos_lat) >= abs(sin_lat):
        h = p / cos_lat - n
    else:
        h = z / sin_lat - n * (1.0 - e2)
    return math.degrees(lat), math.degrees(lon), h


def _read_text_header_from_fileobj(f: io.BufferedReader, max_bytes: int) -> List[str]:
    raw = f.read(max_bytes)
    try:
        text = raw.decode("ascii", errors="ignore")
    except Exception:
        text = ""
    lines = text.splitlines()
    # Keep until END OF HEADER if present
    out: List[str] = []
    for ln in lines:
        out.append(ln)
        if _RINEX_END_HEADER in ln:
            break
    return out


def _parse_rinex_from_path(path: Path, max_header_bytes: int = 128 * 1024) -> Tuple[Optional[str], Optional[str], Set[str], Set[str]]:
    """
    Try to parse a RINEX header from:
    - plain text files
    - .gz compressed files
    - .zip archives (tries to find a likely obs file inside)
    """
    ext = path.suffix.lower()
    try:
        if ext == ".gz":
            with gzip.open(path, "rb") as f:
                lines = _read_text_header_from_fileobj(f, max_header_bytes)
                if not lines:
                    return None, None, set(), set()
                # CRINEX (.crx.gz) starts with "COMPACT RINEX FORMAT" and only later contains
                # the "RINEX VERSION / TYPE" line. So check a window of lines, not just line 1.
                if not _has_rinex_version_line(lines):
                    return None, None, set(), set()
                return _parse_rinex_header_lines(lines)
        if ext == ".zip":
            with zipfile.ZipFile(path) as z:
                # Pick a likely observation member first.
                members = z.namelist()
                preferred = []
                for n in members:
                    ln = n.lower()
                    # common obs indicators: .o, .rnx, .obs, .yy o (e.g. .25o)
                    if ln.endswith(".obs") or ln.endswith(".rnx") or re.search(r"\.\d{2}o$", ln) or ln.endswith(".o"):
                        preferred.append(n)
                # fallback: anything that's not huge directory
                candidates = preferred or [n for n in members if not n.endswith("/")]
                for n in candidates[:10]:
                    with z.open(n, "r") as f:
                        lines = _read_text_header_from_fileobj(f, max_header_bytes)
                    if lines and _has_rinex_version_line(lines):
                        return _parse_rinex_header_lines(lines)
            return None, None, set(), set()

        # Plain file (might still be RINEX)
        with path.open("rb") as f:
            lines = _read_text_header_from_fileobj(f, max_header_bytes)
        if not lines or not _has_rinex_version_line(lines):
            return None, None, set(), set()
        return _parse_rinex_header_lines(lines)
    except Exception:
        return None, None, set(), set()


def _rtcm_msg_type(payload: bytes) -> Optional[int]:
    if len(payload) < 2:
        return None
    return (payload[0] << 4) | (payload[1] >> 4)


def _rtcm_type_to_constellation(msg_type: int) -> Optional[str]:
    # Common RTCM3 MSM message type mapping
    if 1071 <= msg_type <= 1077:
        return "G"  # GPS
    if 1081 <= msg_type <= 1087:
        return "R"  # GLONASS
    if 1091 <= msg_type <= 1097:
        return "E"  # Galileo
    if 1101 <= msg_type <= 1107:
        return "S"  # SBAS
    if 1111 <= msg_type <= 1117:
        return "J"  # QZSS
    if 1121 <= msg_type <= 1127:
        return "C"  # BeiDou
    if 1131 <= msg_type <= 1137:
        return "I"  # IRNSS
    return None


def _scan_rtcm3_messages(data: bytes, max_messages: int = 5000) -> Tuple[Set[str], Set[str]]:
    # CRC24Q (RTCM3) polynomial 0x1864CFB
    def crc24q(buf: bytes) -> int:
        crc = 0
        for b in buf:
            crc ^= (b << 16)
            for _ in range(8):
                crc <<= 1
                if crc & 0x1000000:
                    crc ^= 0x1864CFB
        return crc & 0xFFFFFF

    constellations: Set[str] = set()
    signals: Set[str] = set()
    i = 0
    found = 0
    n = len(data)
    while i + 6 < n and found < max_messages:
        if data[i] != 0xD3:
            i += 1
            continue
        # 0xD3, then 2 bytes with 10-bit length
        b1 = data[i + 1]
        b2 = data[i + 2]
        length = ((b1 & 0x03) << 8) | b2
        frame_len = 3 + length + 3  # header + payload + CRC24Q
        if length <= 0 or i + frame_len > n:
            i += 1
            continue
        frame_wo_crc = data[i : i + 3 + length]
        crc_expected = (data[i + 3 + length] << 16) | (data[i + 3 + length + 1] << 8) | data[i + 3 + length + 2]
        if crc24q(frame_wo_crc) != crc_expected:
            i += 1
            continue
        payload = data[i + 3 : i + 3 + length]
        mt = _rtcm_msg_type(payload)
        if mt is not None:
            signals.add(f"RTCM{mt}")
            c = _rtcm_type_to_constellation(mt)
            if c:
                constellations.add(c)
        found += 1
        i += frame_len
    return constellations, signals


def _scan_ubx_nav_sat(data: bytes, max_msgs: int = 2000) -> Tuple[Set[str], Set[str]]:
    """
    Parse UBX messages and extract GNSS IDs from NAV-SAT (class 0x01, id 0x35).
    Maps UBX gnssId -> constellation letter.
    """
    constellations: Set[str] = set()
    signals: Set[str] = set()

    gnss_map = {
        0: "G",  # GPS
        1: "S",  # SBAS
        2: "E",  # Galileo
        3: "C",  # BeiDou
        4: "I",  # IMES (rare)
        5: "J",  # QZSS
        6: "R",  # GLONASS
    }

    i = 0
    n = len(data)
    seen = 0
    while i + 8 < n and seen < max_msgs:
        if data[i] != 0xB5 or data[i + 1] != 0x62:
            i += 1
            continue
        cls = data[i + 2]
        mid = data[i + 3]
        length = data[i + 4] | (data[i + 5] << 8)
        end = i + 6 + length + 2
        if length < 0 or end > n:
            i += 1
            continue
        payload = data[i + 6 : i + 6 + length]
        if cls == 0x01 and mid == 0x35:
            signals.add("UBX-NAV-SAT")
            # UBX-NAV-SAT: header 8 bytes, then numSvs * 12 byte blocks
            if len(payload) >= 8:
                num_svs = payload[5]
                off = 8
                for _ in range(num_svs):
                    if off + 12 > len(payload):
                        break
                    gnss_id = payload[off]
                    c = gnss_map.get(int(gnss_id))
                    if c:
                        constellations.add(c)
                    off += 12
        seen += 1
        i = end
    return constellations, signals


def _decompress_embedded_bzip2(blob: bytes, max_out: int = 50_000_000) -> Optional[bytes]:
    """
    Some .T02/.T04 files contain a small proprietary header then an embedded bzip2 stream.
    We locate the first 'BZh' and decompress from there.
    """
    off = blob.find(_BZIP_MAGIC)
    if off < 0:
        return None
    try:
        d = bz2.BZ2Decompressor()
        out = d.decompress(blob[off:])
        if len(out) > max_out:
            return out[:max_out]
        return out
    except Exception:
        return None


def _parse_to2_to4(path: Path) -> Tuple[Optional[str], Optional[str], Set[str], Set[str]]:
    """
    Best-effort parser for Trimble-style .T02/.T04 logs.

    1) If runpkr00 is available, use `runpkr00 -q` to extract receiver metadata (serial/firmware).
       This is reliable on Windows and avoids guessing proprietary packet formats.
    2) Additionally, try embedded bzip2 receiver text extraction (receiver model) for extra context.

    Returns: (format, file_type, constellations, signals)
    Note: constellations/signals from T02/T04 require a full conversion pipeline (runpkr00 -> tgd/dat -> RINEX),
    which is not implemented here yet. We still expose receiver metadata in `signals`.
    """
    try:
        blob = path.read_bytes()
    except Exception:
        return None, None, set(), set()

    decomp = _decompress_embedded_bzip2(blob)
    data = decomp if decomp is not None else blob

    constellations: Set[str] = set()
    signals: Set[str] = set()

    # Try runpkr00 quick summary if available.
    runpkr00_path = os.environ.get("RUNPKR00_PATH")
    # Allow fast bulk scans without spawning runpkr00 per file.
    if os.environ.get("GNSS_SKIP_RUNPKR00", "").strip() in {"1", "true", "yes"}:
        runpkr00_path = None
    if not runpkr00_path:
        # Auto-detect if the tool was installed under this repo.
        candidate = Path(__file__).resolve().parent / "tools" / "runpkr00" / "runpkr00.exe"
        if candidate.exists():
            runpkr00_path = str(candidate)
    if runpkr00_path and Path(runpkr00_path).exists():
        try:
            cp = subprocess.run(
                [runpkr00_path, "-q", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
                check=False,
                **_SUBPROC_KW,
            )
            out = (cp.stdout or "").strip()
            if out:
                # Typical output: "sn:6016R40025;fw:6.23;rx:162"
                signals.add(f"RUNPKR00:{out}")
                for token in out.split(";"):
                    token = token.strip()
                    if token:
                        signals.add(token.upper())
        except Exception:
            pass

    # Always try to extract receiver model info from the decompressed text prefix.
    try:
        text_head = data[:8192].decode("ascii", errors="ignore")
        # Example observed: "ReceiverId:162,Trimble Alloy,6016R40025"
        m = re.search(r"ReceiverId:\s*\d+\s*,\s*([^,\r\n]+)\s*,\s*([A-Za-z0-9]+)", text_head)
        if m:
            model = m.group(1).strip()
            fw = m.group(2).strip()
            signals.add(f"RECEIVER:{model}")
            signals.add(f"FW:{fw}")
        elif "Trimble" in text_head:
            signals.add("RECEIVER:Trimble")
    except Exception:
        pass

    fmt = "T02/T04(bzip2)" if decomp is not None else "T02/T04(raw)"
    ftype = "GNSS_LOG"
    return fmt, ftype, constellations, signals


def _tool_paths() -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (runpkr00_path, teqc_path) if found.
    """
    runpkr00_path = os.environ.get("RUNPKR00_PATH")
    teqc_path = os.environ.get("TEQC_PATH")
    base = Path(__file__).resolve().parent / "tools"
    if not runpkr00_path:
        c = base / "runpkr00" / "runpkr00.exe"
        if c.exists():
            runpkr00_path = str(c)
    if not teqc_path:
        c = base / "teqc" / "teqc.exe"
        if c.exists():
            teqc_path = str(c)
    return runpkr00_path, teqc_path


def _scrape_station_capability_from_t02(
    t02_path: Path,
    station: str,
    runpkr00_path: str,
    teqc_path: str,
) -> Optional[StationCapability]:
    """
    Convert one T02 -> (TGD/DAT) -> RINEX OBS, then parse header for constellations + signals.
    Keeps output small by writing OBS to a temp file and only reading its header.

    Uses a fresh tempfile.mkdtemp() per call and ALWAYS cleans up afterwards --
    the previous implementation left growing piles of `gnss_cap_*` folders in
    %TEMP% on every scan, eventually filling the disk on long-running clients.
    """
    import tempfile as _tempfile
    import shutil as _shutil

    tmp = Path(_tempfile.mkdtemp(prefix=f"gnss_cap_{station}_"))
    try:
        # Copy the T02 into temp so runpkr00 writes outputs locally and we don't
        # pollute the source tree.
        local_t02 = tmp / t02_path.name
        try:
            local_t02.write_bytes(t02_path.read_bytes())
        except Exception:
            return None

        try:
            subprocess.run(
                [runpkr00_path, "-g", "-d", str(local_t02)],
                cwd=str(tmp),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
                **_SUBPROC_KW,
            )
        except Exception:
            return None

        produced = list(tmp.glob("*.tgd")) + list(tmp.glob("*.dat"))
        if not produced:
            return None
        raw = produced[0]

        obs_path = tmp / f"{station}.obs"
        try:
            subprocess.run(
                [teqc_path, "+obs", str(obs_path), str(raw)],
                cwd=str(tmp),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=False,
                **_SUBPROC_KW,
            )
        except Exception:
            return None

        if not (obs_path.exists() and obs_path.stat().st_size > 0):
            return None

        try:
            with obs_path.open("rb") as f:
                lines = _read_text_header_from_fileobj(f, 256 * 1024)
            if not lines or not _has_rinex_version_line(lines):
                return None
            _, _, cs, ss = _parse_rinex_header_lines(lines)
            if not cs and not ss:
                return None
            return StationCapability(
                station=station,
                sample_file=str(t02_path.name),
                derived_from=str(raw.name),
                constellations=",".join(sorted(cs)),
                signals=",".join(sorted(ss)),
            )
        except Exception:
            return None
    finally:
        # Always release the temp dir, even if subprocess timed out.
        _shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Scan a GNSS data folder (messy hierarchy ok) and write a manifest of what files exist. "
            "Designed to support both .to2 and (example) RINEX/.zip style datasets."
        )
    )
    ap.add_argument("root", type=str, help="Folder to scan recursively (e.g. ...\\2025\\2025 or ...\\2026).")
    ap.add_argument(
        "--out_dir",
        type=str,
        default=None,
        help="Output directory for manifests (default: <root>_scanned/_manifests).",
    )
    ap.add_argument(
        "--prefix_regex",
        type=str,
        default=_DEFAULT_PREFIX_REGEX,
        help="Regex used to extract a trusted prefix from the start of the filename.",
    )
    ap.add_argument(
        "--include_ext",
        type=str,
        default=None,
        help="Comma-separated list of extensions to include (case-insensitive), e.g. '.to2,.zip,.25o'.",
    )
    ap.add_argument(
        "--exclude_ext",
        type=str,
        default=None,
        help="Comma-separated list of extensions to exclude (case-insensitive).",
    )
    ap.add_argument(
        "--parse_rinex_headers",
        action="store_true",
        help="Attempt to parse RINEX observation headers to extract constellations + signal/obs types. "
             "Works for plain files and for files inside .gz/.zip (best-effort).",
    )
    ap.add_argument(
        "--max_header_kb",
        type=int,
        default=128,
        help="Max KB to read when parsing headers (default 128KB).",
    )
    ap.add_argument(
        "--skip_runpkr00",
        action="store_true",
        help="Skip running runpkr00 when scanning .T02/.T04 (much faster for large datasets).",
    )
    ap.add_argument(
        "--t02_capability_samples_per_station",
        type=int,
        default=0,
        help="For .T02/.T04: for each station, convert up to N sample files to RINEX using runpkr00+teqc and "
             "extract constellations/signals into station_capabilities.csv. 0 disables.",
    )
    ap.add_argument("--limit", type=int, default=None, help="Optional limit for number of files (debugging).")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"root does not exist or is not a directory: {root}")

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else root.with_name(root.name + "_scanned")
    manifests_dir = out_dir / "_manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    prefix_re = re.compile(args.prefix_regex)

    include = None
    if args.include_ext:
        include = {e.strip().lower() for e in args.include_ext.split(",") if e.strip()}
    exclude = set()
    if args.exclude_ext:
        exclude = {e.strip().lower() for e in args.exclude_ext.split(",") if e.strip()}

    records: list[FileRecord] = []
    scanned = 0
    if args.skip_runpkr00:
        os.environ["GNSS_SKIP_RUNPKR00"] = "1"

    # Station capability scrape setup
    runpkr00_path, teqc_path = _tool_paths()
    cap_enabled = int(args.t02_capability_samples_per_station) > 0 and runpkr00_path and teqc_path
    cap_seen: Dict[str, int] = {}
    cap_rows: List[StationCapability] = []

    for p in _iter_files(root):
        scanned += 1
        if args.limit is not None and len(records) >= args.limit:
            break

        ext = p.suffix.lower()
        if include is not None and ext not in include:
            continue
        if ext in exclude:
            continue

        try:
            st = p.stat()
            if int(st.st_size) <= 0:
                rel = _safe_relpath(p, root)
                station, station_source = _infer_station(p.name, rel, prefix_re)
                prefix = station
                records.append(
                    FileRecord(
                        station=station,
                        station_source=station_source,
                        prefix=prefix,
                        file_name=p.name,
                        ext=ext,
                        size_bytes=int(st.st_size),
                        modified_utc=_iso_utc_from_mtime(st.st_mtime),
                        discovered_from=rel,
                        inferred_date=_infer_date_token(p),
                        rinex_version=None,
                        rinex_file_type=None,
                        constellations=None,
                        signals=None,
                        lat=None,
                        lon=None,
                        height_m=None,
                        ecef_x=None,
                        ecef_y=None,
                        ecef_z=None,
                    )
                )
                continue

            rinex_version = None
            rinex_file_type = None
            constellations: Set[str] = set()
            signals: Set[str] = set()
            ecef_x = ecef_y = ecef_z = None
            lat = lon = h_m = None
            # RINEX/CRINEX header parsing
            if args.parse_rinex_headers:
                rv, rft, cs, ss = _parse_rinex_from_path(p, max_header_bytes=int(args.max_header_kb) * 1024)
                rinex_version = rv
                rinex_file_type = rft
                constellations = cs
                signals = ss
                # If this is a RINEX-like file, try extracting approximate position.
                try:
                    ext2 = p.suffix.lower()
                    lines = None
                    if ext2 == ".gz":
                        with gzip.open(p, "rb") as f:
                            lines = _read_text_header_from_fileobj(f, int(args.max_header_kb) * 1024)
                    elif ext2 == ".zip":
                        # Skip zip coordinate parsing here to avoid costly opening of large archives.
                        lines = None
                    else:
                        with p.open("rb") as f:
                            lines = _read_text_header_from_fileobj(f, int(args.max_header_kb) * 1024)
                    if lines and _has_rinex_version_line(lines):
                        ecef_x, ecef_y, ecef_z = _parse_approx_position_xyz(lines)
                        if ecef_x is not None and ecef_y is not None and ecef_z is not None:
                            lat, lon, h_m = _ecef_to_llh_wgs84(ecef_x, ecef_y, ecef_z)
                except Exception:
                    pass
            # TO2/TO4 parsing (constellations/signals via RTCM/UBX inside)
            if ext in {".t02", ".t04"}:
                fmt, ftype, cs, ss = _parse_to2_to4(p)
                rinex_version = fmt
                rinex_file_type = ftype
                constellations |= cs
                signals |= ss

            # Optional: station capability scrape from T02/T04 via runpkr00+teqc (sampled)
            if cap_enabled and ext in {".t02", ".t04"}:
                station, _src = _infer_station(p.name, _safe_relpath(p, root), prefix_re)
                n = cap_seen.get(station, 0)
                if n < int(args.t02_capability_samples_per_station):
                    cap = _scrape_station_capability_from_t02(p, station, runpkr00_path, teqc_path)
                    if cap:
                        cap_rows.append(cap)
                        cap_seen[station] = n + 1

            rel = _safe_relpath(p, root)
            station, station_source = _infer_station(p.name, rel, prefix_re)
            # Backwards compatible field used by existing dashboard logic.
            prefix = station
            records.append(
                FileRecord(
                    station=station,
                    station_source=station_source,
                    prefix=prefix,
                    file_name=p.name,
                    ext=ext,
                    size_bytes=int(st.st_size),
                    modified_utc=_iso_utc_from_mtime(st.st_mtime),
                    discovered_from=rel,
                    inferred_date=_infer_date_token(p),
                    rinex_version=rinex_version,
                    rinex_file_type=rinex_file_type,
                    constellations=",".join(sorted(constellations)) if constellations else None,
                    signals=",".join(sorted(signals)) if signals else None,
                    lat=lat,
                    lon=lon,
                    height_m=h_m,
                    ecef_x=ecef_x,
                    ecef_y=ecef_y,
                    ecef_z=ecef_z,
                )
            )
        except Exception:
            # Don't crash the whole scan on a single bad path/file.
            continue

    jsonl_path = manifests_dir / "files_manifest.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    csv_path = manifests_dir / "files_manifest.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = list(asdict(records[0]).keys()) if records else list(asdict(FileRecord("", "", "", 0, "", "", None)).keys())
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in records:
            w.writerow(asdict(r))

    by_ext: dict[str, int] = {}
    by_prefix: dict[str, int] = {}
    by_constellation: Dict[str, int] = {}
    total_bytes = 0
    for r in records:
        by_ext[r.ext] = by_ext.get(r.ext, 0) + 1
        by_prefix[r.prefix] = by_prefix.get(r.prefix, 0) + 1
        total_bytes += r.size_bytes
        if r.constellations:
            for c in r.constellations.split(","):
                if c:
                    by_constellation[c] = by_constellation.get(c, 0) + 1

    summary = {
        "paths_checked": scanned,
        "files_in_manifest": len(records),
        "total_bytes": total_bytes,
        "unique_prefixes": len(by_prefix),
        "unique_exts": len(by_ext),
        "by_constellation_counts": dict(sorted(by_constellation.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_ext_counts": dict(sorted(by_ext.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_prefix_counts": dict(sorted(by_prefix.items(), key=lambda kv: (-kv[1], kv[0]))),
        "root": str(root),
        "out_dir": str(out_dir),
        "manifests_dir": str(manifests_dir),
        "include_ext": sorted(include) if include is not None else None,
        "exclude_ext": sorted(exclude) if exclude else None,
        "prefix_regex": args.prefix_regex,
        "parse_rinex_headers": bool(args.parse_rinex_headers),
        "max_header_kb": int(args.max_header_kb),
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(),
    }
    (manifests_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Station capabilities output (optional)
    if cap_rows:
        cap_path = manifests_dir / "station_capabilities.csv"
        with cap_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["station", "sample_file", "derived_from", "constellations", "signals"])
            w.writeheader()
            for r in cap_rows:
                w.writerow(asdict(r))
        print(f"Wrote station capabilities: {cap_path}")

    print(f"Scanned: {root}")
    print(f"Manifest files: {len(records)}")
    print(f"Manifests: {manifests_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

