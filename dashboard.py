from __future__ import annotations

import io
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st


# Ensure local modules (e.g. to2_pipeline.py) are importable even when Streamlit
# is launched from a different working directory.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))


st.set_page_config(page_title="GNSS Recorder Dashboard", layout="wide")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_manifest_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)


@st.cache_data(show_spinner=False)
def _load_manifest_csv_cached(path_str: str, mtime: float, size: int) -> pd.DataFrame:
    """
    Streamlit-cached CSV loader.

    The cache key intentionally includes mtime + size so an updated manifest
    busts the cache automatically. dtype=str for ext/file_name to avoid
    pandas guessing odd types on tiny manifests.
    """
    df = pd.read_csv(
        path_str,
        dtype={"file_name": "string", "ext": "string", "discovered_from": "string"},
        low_memory=False,
    )
    return df


_TS_REGEXES = [
    re.compile(r"(20\d{2})([01]\d)([0-3]\d)([0-2]\d)([0-5]\d)([0-5]\d)"),  # YYYYMMDDHHMMSS
    re.compile(r"(20\d{2})([01]\d)([0-3]\d)([0-2]\d)([0-5]\d)"),            # YYYYMMDDHHMM
    re.compile(r"(20\d{2})([01]\d)([0-3]\d)([0-2]\d)"),                      # YYYYMMDDHH
]
_RINEX2_REGEX = re.compile(r"^[A-Za-z0-9]{4}(\d{3})(\d)$")  # e.g. basc0010
_RINEX3_NAME_DOY_HHMM = re.compile(r"_R_(?P<year>20\d{2})(?P<doy>\d{3})(?P<hhmm>\d{4})_")


def _infer_ts_from_row(row: pd.Series) -> pd.Timestamp:
    """
    Best-effort timestamp extraction:
    1) Full timestamp tokens in file/path (YYYYMMDDHH[MM[SS]])
    2) RINEX2 naming (station + day-of-year + hour), year from path or modified_utc
    3) fallback to modified_utc
    """
    file_name = str(row.get("file_name", ""))
    discovered_from = str(row.get("discovered_from", ""))
    joined = f"{discovered_from} {file_name}"

    for rgx in _TS_REGEXES:
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

    # RINEX3 common filename token:
    #   <MARKER>_R_YYYYDOYHHMM_... e.g. ROUL00LUX_R_20253650000_01D_30S_MO.crx.gz
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


def _to_tz(ts: pd.Series, tz_name: str) -> pd.Series:
    """
    Convert a datetime Series to the requested timezone.

    Handles both tz-aware and tz-naive inputs gracefully so the Station tab
    never raises "Cannot convert tz-naive timestamps" mid-render.
    """
    target = "UTC" if (tz_name or "").upper() == "UTC" else tz_name
    s = pd.to_datetime(ts, errors="coerce", utc=False)
    try:
        is_aware = s.dt.tz is not None
    except (AttributeError, TypeError):
        is_aware = False
    if not is_aware:
        s = s.dt.tz_localize("UTC", nonexistent="NaT", ambiguous="NaT")
    return s.dt.tz_convert(target)


def _auto_center_zoom(lat: pd.Series, lon: pd.Series) -> tuple[dict, int]:
    """
    Compute a safe (center, zoom) for Plotly maps from lat/lon Series.

    Robust to: empty input, single point, all-NaN, all-equal coords.
    Falls back to a world view (0,0 / zoom=1) when nothing usable is provided.
    """
    lat_clean = pd.to_numeric(lat, errors="coerce").dropna()
    lon_clean = pd.to_numeric(lon, errors="coerce").dropna()
    if lat_clean.empty or lon_clean.empty:
        return {"lat": 0.0, "lon": 0.0}, 1
    lat_min, lat_max = float(lat_clean.min()), float(lat_clean.max())
    lon_min, lon_max = float(lon_clean.min()), float(lon_clean.max())
    center = {"lat": (lat_min + lat_max) / 2.0, "lon": (lon_min + lon_max) / 2.0}
    span = max(abs(lat_max - lat_min), abs(lon_max - lon_min))
    if span <= 0.0:
        zoom = 11  # single point: zoom in
    elif span < 0.05:
        zoom = 11
    elif span < 0.2:
        zoom = 9
    elif span < 1.0:
        zoom = 7
    elif span < 5.0:
        zoom = 5
    else:
        zoom = 3
    return center, zoom


def _scatter_map_compat(*, data: pd.DataFrame, lat: str, lon: str, height: int, **kwargs):
    """
    Plotly compatibility wrapper:
    - Prefer `px.scatter_map` (MapLibre, Plotly>=6)
    - Fall back to `px.scatter_mapbox` (Mapbox, older Plotly)
    Returns (fig, kind) where kind is "map" or "mapbox".
    """
    if hasattr(px, "scatter_map"):
        fig = px.scatter_map(data, lat=lat, lon=lon, height=height, **kwargs)
        return fig, "map"
    fig = px.scatter_mapbox(data, lat=lat, lon=lon, height=height, **kwargs)
    return fig, "mapbox"


def _day_labels() -> list[str]:
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _coverage_matrix(timeline: pd.DataFrame, value_col: str) -> pd.DataFrame:
    """
    timeline columns required: dow (0..6), hour (0..23), plus value_col (0/1 or ratio)
    Returns 7x24 matrix with day labels.
    """
    mat = (
        timeline.pivot(index="dow", columns="hour", values=value_col)
        .reindex(index=range(7), columns=range(24), fill_value=0)
    )
    mat.index = _day_labels()
    return mat


def _parse_csv_list(s: object) -> list[str]:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return []
    txt = str(s).strip()
    if not txt:
        return []
    return [x for x in txt.split(",") if x]


def _normalize_station_id_series(series: pd.Series) -> pd.Series:
    """Lowercase trimmed station IDs with a stable string form.

    `read_csv` frequently infers all-digit GNSS prefixes as int/float columns. A
    subsequent plain `.astype(str)` then yields a mix of `'3563'` and `'3563.0'`,
    so the Station tab selectbox (fed from `groupby` uniques) stops matching
    `df[station_col] == station` and the user sees "No records for station" despite
    thousands of manifest rows — a real offline client failure mode.
    """
    txt = series.astype(str).str.strip().str.lower()
    txt = txt.replace({"nan": "unknown", "<na>": "unknown", "": "unknown", "none": "unknown"})
    # Collapse float stringifications of whole numbers: 3563.0, 3563.00, ...
    txt = txt.str.replace(r"^(\d+)\.0+$", r"\1", regex=True)
    return txt


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _extract_runpkr00_inventory(signals_series: pd.Series) -> pd.DataFrame:
    """
    Extract receiver inventory fields from the `signals` column produced by the scanner for T02/T04:
    - RECEIVER:<model>
    - FW:<firmware>
    - SN:<serial>
    - RX:<id>
    """
    rows = []
    for raw in signals_series.dropna().astype(str).tolist():
        items = [x.strip() for x in raw.split(",") if x.strip()]
        rec = {"receiver": None, "fw": None, "sn": None, "rx": None}
        for it in items:
            u = it.upper()
            if u.startswith("RECEIVER:"):
                rec["receiver"] = it.split(":", 1)[1].strip()
            elif u.startswith("FW:"):
                rec["fw"] = it.split(":", 1)[1].strip()
            elif u.startswith("SN:"):
                rec["sn"] = it.split(":", 1)[1].strip()
            elif u.startswith("RX:"):
                rec["rx"] = it.split(":", 1)[1].strip()
        if any(rec.values()):
            rows.append(rec)
    if not rows:
        return pd.DataFrame(columns=["receiver", "fw", "sn", "rx"])
    return pd.DataFrame(rows)


class _SkipTab(Exception):
    """Raised inside a tab body to safely stop rendering without halting Streamlit.

    Replaces st.stop() inside per-tab code paths -- st.stop() halts the WHOLE app
    (which is why touching one widget could blank the entire dashboard).
    """


from contextlib import contextmanager


@contextmanager
def _safe_tab(name: str, tab):
    """Render a Streamlit tab safely.

    - Catches _SkipTab so an early exit only stops THIS tab, not the app.
    - Catches any other Exception, shows a readable error in-place, and
      keeps the rest of the app interactive instead of going blank.
    """
    with tab:
        try:
            yield
        except _SkipTab:
            return
        except Exception as e:
            st.error(f"{name} tab failed: {type(e).__name__}: {e}")


def _longest_false_run(mask: pd.Series) -> int:
    """
    mask: boolean series where True=covered, False=missing.
    Returns longest consecutive missing length.
    """
    longest = 0
    cur = 0
    for v in mask.fillna(False).astype(bool).tolist():
        if v:
            longest = max(longest, cur)
            cur = 0
        else:
            cur += 1
    return max(longest, cur)


st.title("GNSS Recorder Dashboard")
st.caption("Station-first GNSS coverage analysis (prefix-trusted), regardless of folder hierarchy.")


# Hard ceilings for untrusted zip downloads. Generous enough for real manifests
# (a file_manifest.csv for ~250k files is well under 50MB), small enough that a
# malicious or runaway URL can't blow out memory or freeze the UI.
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MB cap on downloaded zip
_MAX_EXTRACT_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB cap on uncompressed contents (zip-bomb defense)
_MAX_EXTRACT_ENTRIES = 200_000
_DOWNLOAD_CONNECT_TIMEOUT = 8       # seconds
_DOWNLOAD_READ_CHUNK_TIMEOUT = 30   # seconds (per chunk read)


def _stream_download(url: str, dest: Path, max_bytes: int = _MAX_DOWNLOAD_BYTES) -> None:
    """
    Stream a URL to `dest`, enforcing a hard size cap so a malicious or runaway
    URL can't OOM the dashboard or hang on a 10GB body.

    Previous code used `requests.get(url, timeout=120).content` which both
    freezes the UI for up to 2 minutes and loads the entire response into
    memory before doing anything with it.
    """
    with requests.get(
        url,
        stream=True,
        timeout=(_DOWNLOAD_CONNECT_TIMEOUT, _DOWNLOAD_READ_CHUNK_TIMEOUT),
        allow_redirects=True,
    ) as r:
        r.raise_for_status()
        # Honour Content-Length up-front when present. Parse defensively so a
        # bogus header (non-numeric) doesn't accidentally swallow the size cap.
        cl = r.headers.get("Content-Length")
        if cl is not None:
            try:
                cl_int = int(cl)
            except (TypeError, ValueError):
                cl_int = None  # unparseable -> rely on streaming cap below
            if cl_int is not None and cl_int > max_bytes:
                raise ValueError(
                    f"Refusing to download {cl_int:,} bytes (cap is {max_bytes:,}). "
                    "Increase the cap if this is legit."
                )
        written = 0
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(
                        f"Download exceeded size cap of {max_bytes:,} bytes (still streaming). "
                        f"Aborting to protect memory."
                    )
                f.write(chunk)


def _safe_extract_zip(zpath: Path, dest: Path) -> None:
    """
    Safely extract `zpath` into `dest`, refusing any member that would write
    outside `dest` (Zip Slip / `..` traversal) or absolute paths, and capping
    total uncompressed size to defuse zip bombs.

    Defaults to "skip the bad member" rather than "abort everything", so a
    dataset with one weird filename still loads, but a malicious zip can never
    silently overwrite arbitrary files on disk.
    """
    dest_resolved = dest.resolve()
    extracted_bytes = 0
    extracted_entries = 0
    with zipfile.ZipFile(zpath) as z:
        infos = z.infolist()
        if len(infos) > _MAX_EXTRACT_ENTRIES:
            raise ValueError(f"Refusing to extract zip with {len(infos):,} entries (cap is {_MAX_EXTRACT_ENTRIES:,}).")
        for info in infos:
            name = info.filename
            if not name or name.endswith("/"):
                # directory entries are recreated implicitly when files extract
                continue
            # Disallow absolute paths and traversal up-front.
            if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                continue
            target = (dest_resolved / name).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError:
                # Member tried to escape the destination directory -- skip it.
                continue
            # Cap uncompressed size before extracting.
            extracted_bytes += int(info.file_size or 0)
            if extracted_bytes > _MAX_EXTRACT_BYTES:
                raise ValueError(
                    f"Zip uncompressed size exceeded cap of {_MAX_EXTRACT_BYTES:,} bytes (zip bomb?)."
                )
            extracted_entries += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info, "r") as src, target.open("wb") as dst:
                # Stream the member in 1MB chunks so we don't materialise huge
                # files in memory.
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    dst.write(buf)


def _find_manifests_dir(root: Path) -> Path:
    """
    Locate the {files_manifest.csv, summary.json} pair inside `root`, supporting
    common layouts (root/_manifests, root/manifests/_manifests, root itself, or
    anywhere via rglob fallback).
    """
    candidates = [root / "_manifests", root / "manifests" / "_manifests", root]
    for c in candidates:
        if (c / "files_manifest.csv").exists() and (c / "summary.json").exists():
            return c
    for p in root.rglob("files_manifest.csv"):
        if (p.parent / "summary.json").exists():
            return p.parent
    raise FileNotFoundError("Could not find files_manifest.csv + summary.json in zip.")


def _download_and_extract_manifests_zip(url: str) -> Path:
    """
    Download `url` (must be a zip) and extract it to a fresh temp dir, returning
    the path to the manifests folder.

    Note: NOT cached via @st.cache_data — the old cache could return a tempdir
    path that was deleted later (FileNotFound in the field). The sidebar instead
    keeps the last successful extract path + URL in Streamlit session state so
    ordinary reruns do not re-hit the network.
    """
    tmp = Path(tempfile.mkdtemp(prefix="gnss_manifests_"))
    zpath = tmp / "manifests.zip"
    _stream_download(url, zpath)
    _safe_extract_zip(zpath, tmp)
    return _find_manifests_dir(tmp)


@st.cache_data(show_spinner=False)
def _geonet_station_coords(stations: tuple[str, ...], network: str = "NZ") -> pd.DataFrame:
    """
    Best-effort fetch of station coords from GeoNet FDSN station service (text format).
    Returns columns: station, lat, lon, site_name

    Batched in groups of 50 stations so we don't build a giant URL that some
    proxies/servers reject with 414 URI Too Long. Each batch has its own short
    timeout so a single slow batch doesn't freeze the whole UI.
    """
    if not stations:
        return pd.DataFrame(columns=["station", "lat", "lon", "site_name"])

    url = "https://service.geonet.org.nz/fdsnws/station/1/query"
    uniq = sorted(set(s for s in stations if s))
    batch_size = 50
    all_rows: list[dict] = []
    for i in range(0, len(uniq), batch_size):
        batch = uniq[i : i + batch_size]
        params = {
            "network": network,
            "station": ",".join(batch),
            "level": "station",
            "format": "text",
        }
        try:
            r = requests.get(url, params=params, timeout=4)
            r.raise_for_status()
        except Exception:
            # One batch failing should not throw away the others.
            continue
        for line in r.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 6:
                continue
            _net, sta, lat, lon, _elev, site = parts[:6]
            try:
                all_rows.append(
                    {
                        "station": str(sta).strip().lower(),
                        "lat": float(lat),
                        "lon": float(lon),
                        "site_name": str(site).strip(),
                    }
                )
            except Exception:
                continue
    return pd.DataFrame(all_rows)


def _manifest_to_sqlite(df: pd.DataFrame, out_path: Path) -> None:
    """
    Write a small SQLite DB derived from the manifest (portable/shareable).
    """
    cols = [
        c
        for c in [
            "station",
            "prefix",
            "file_name",
            "ext",
            "size_bytes",
            "modified_utc",
            "discovered_from",
            "inferred_date",
            "rinex_version",
            "rinex_file_type",
            "constellations",
            "signals",
            "lat",
            "lon",
            "height_m",
            "ecef_x",
            "ecef_y",
            "ecef_z",
            "event_ts_utc",
            "event_hour_utc",
        ]
        if c in df.columns
    ]
    out = df[cols].copy()

    conn = sqlite3.connect(str(out_path), timeout=30)
    try:
        out.to_sql("files", conn, if_exists="replace", index=False)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_station_hour ON files(station, event_hour_utc)")
        conn.commit()
    finally:
        conn.close()


def _try_load_station_capabilities(manifests_dir: Path) -> pd.DataFrame:
    p = manifests_dir / "station_capabilities.csv"
    if not p.exists():
        return pd.DataFrame()
    try:
        dfc = pd.read_csv(p)
        if "station" in dfc.columns:
            dfc["station"] = dfc["station"].astype(str).str.strip().str.lower()
        return dfc
    except Exception:
        return pd.DataFrame()


def _scan_folder_session_key(data_root: Path, cache_dir: Path) -> str:
    """Stable Data|Cache key so post-scan resume survives Streamlit reruns.

    On Windows, paths that differ only by case or minor spelling (``foo`` vs
    ``./foo`` after resolve) used to break ``last_key == scan_cache_key``, which
    triggered ``st.stop()`` and hid Overview/Station tabs even though manifests
    were already on disk.
    """
    try:
        dr = data_root.expanduser().resolve()
        cr = cache_dir.expanduser().resolve()
    except Exception:
        dr = data_root.expanduser()
        cr = cache_dir.expanduser()
    ds = os.path.normpath(str(dr))
    cs = os.path.normpath(str(cr))
    if os.name == "nt":
        ds = os.path.normcase(ds)
        cs = os.path.normcase(cs)
    return f"{ds}|{cs}"


def _normalize_saved_scan_cache_key(raw: str) -> str:
    """Parse a previously stored ``data|cache`` string using the same rules."""
    s = (raw or "").strip()
    if not s or "|" not in s:
        return ""
    left, _, right = s.partition("|")
    return _scan_folder_session_key(Path(left), Path(right))


with st.sidebar:
    st.header("Input")
    # Default manifests folder: use env var if set, else the bundled cache under the
    # app folder (works on any machine, including the offline client). We deliberately
    # avoid hardcoded developer-machine paths like "C:\Aj\..." which broke client setups.
    _bundle_default = (_THIS_DIR / "_cache_default" / "exported" / "_manifests")
    default_root = os.environ.get("GNSS_MANIFESTS_DIR", str(_bundle_default))

    # On a fresh install nothing has been scanned yet -> the default manifest dir
    # is empty. Default the sidebar to "Scan folder" mode in that case so the user
    # isn't greeted by a "missing manifest" error before they've done anything.
    _source_options = ["Local folder", "Scan folder (TO2/T02)", "URL (zip of manifests)", "Upload (zip of manifests)"]
    _default_index = 0
    try:
        if not (Path(default_root).expanduser() / "files_manifest.csv").exists():
            _default_index = 1
    except Exception:
        _default_index = 1
    source = st.radio("Data source", options=_source_options, index=_default_index)


    def _clean_path_input(raw: str) -> str:
        """Strip whitespace and surrounding quotes that Windows users routinely
        paste in (File Explorer 'Copy as path' wraps paths in double quotes)."""
        if raw is None:
            return ""
        s = str(raw).strip()
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            s = s[1:-1].strip()
        return s

    manifests_dir: Path
    if source == "Local folder":
        manifests_dir = Path(_clean_path_input(st.text_input("Manifests folder", value=default_root))).expanduser()
    elif source == "Scan folder (TO2/T02)":
        st.caption("Point to a folder of .TO2/.T02/.TO4/.T04 files. Results are cached so you don't rescan every time.")
        st.caption("Tip: in File Explorer use Shift+Right-Click on the folder -> 'Copy as path' and paste here. Surrounding quotes are stripped automatically.")
        data_root = Path(_clean_path_input(st.text_input("Data folder to scan", value=os.environ.get("GNSS_DATA_ROOT", "")))).expanduser()
        cache_dir = Path(_clean_path_input(st.text_input("Cache folder", value=os.environ.get("GNSS_CACHE_DIR", str(Path.home() / ".gnss_dash_cache"))))).expanduser()

        runpkr_default = str((Path(__file__).resolve().parent / "tools" / "runpkr00" / "runpkr00.exe"))
        runpkr_path = Path(_clean_path_input(st.text_input("runpkr00.exe (recommended for .T02/.T04)", value=os.environ.get("GNSS_RUNPKR00", runpkr_default)))).expanduser()

        teqc_bundled = str((Path(__file__).resolve().parent / "tools" / "teqc" / "teqc.exe"))
        teqc_default = os.environ.get("GNSS_TEQC", teqc_bundled)
        teqc_path = Path(_clean_path_input(st.text_input("teqc.exe (recommended for .T02/.T04)", value=teqc_default))).expanduser()

        convbin_default = str((Path(__file__).resolve().parent / "tools" / "rtklib" / "convbin.exe"))
        convbin_path = Path(_clean_path_input(st.text_input("convbin.exe (optional)", value=os.environ.get("GNSS_CONVBIN", convbin_default)))).expanduser()
        use_trimble = st.checkbox(
            "Convert TO2/T02 to RINEX (recommended: runpkr00 + teqc)",
            value=runpkr_path.exists() and teqc_path.exists(),
        )
        use_convbin = st.checkbox(
            "Convert TO2/T02 to RINEX (fallback: RTKLIB convbin.exe)",
            value=(not use_trimble) and convbin_path.exists(),
        )

        quick_probe = st.checkbox(
            "Quick probe (1 file per station) — fastest for coords/signals",
            value=False,
            help="ON = fast sample (good for station coords/signals). OFF = full file scan (good for coverage stats).",
        )
        mode_label = "PROBE" if quick_probe else "FULL"
        st.markdown(f"**Scan mode**: `{mode_label}`")
        if quick_probe:
            st.warning(
                "Probe mode exports ~1 file per station. This is **not** suitable for full coverage stats.",
            )
        else:
            st.success(
                "Full scan will include **all matching files** under the chosen folder (best for coverage stats).",
            )

        force_rescan = st.checkbox(
            "Force full rescan (ignore cache)",
            value=False,
            help="If ON, the scanner will reprocess all files even if they were cached previously.",
        )

        # Streamlit reruns after every interaction. Only the single frame right
        # when "Scan now" is pressed has scan_clicked=True. If we blindly
        # st.stop() when the button is false, EVERY subsequent rerun while the
        # user still sits on "Scan folder" mode **never renders the tabs** –
        # the manifest IS on disk but the main body never runs. Clients see
        # "scan completed" (one flash) then a blank / stalled app forever.
        scan_cache_key = _scan_folder_session_key(data_root, cache_dir)
        scan_clicked = st.button("Scan now", type="primary", use_container_width=True)
        if not scan_clicked:
            last_manifests_str = (
                str(st.session_state.get("_gnss_last_scan_manifests_dir") or "").strip()
            )
            last_key_raw = str(st.session_state.get("_gnss_last_scan_cache_key") or "").strip()
            last_key_norm = _normalize_saved_scan_cache_key(last_key_raw)
            candidate = Path(last_manifests_str).expanduser() if last_manifests_str else None
            keys_match = bool(last_key_norm) and last_key_norm == scan_cache_key
            manifests_ok = bool(
                candidate
                and (candidate / "files_manifest.csv").exists()
                and (candidate / "summary.json").exists()
            )
            if candidate and manifests_ok and keys_match:
                manifests_dir = candidate
                st.success(
                    f"Using manifests from **last scan** ({manifests_dir.name}). "
                    "Click **Scan now** again to rescan."
                )
            elif candidate and manifests_ok and not keys_match:
                st.warning(
                    "Manifests from an earlier scan are still on disk, but **Data folder** or "
                    "**Cache folder** does not match that scan. Put the same paths back (check "
                    "spelling and drive letter), or click **Scan now** to rebuild."
                )
                st.info("Click **Scan now** to scan the folder and build cached manifests.")
                st.stop()
            else:
                st.info("Click **Scan now** to scan the folder and build cached manifests.")
                st.stop()

        # Defensive: bail before any expensive walk if the user gave us nothing
        # (or a bogus path). Streamlit's st.stop is a silent no-op when the
        # script isn't running under streamlit-run, so the previous order of
        # checks could lead to a multi-minute walk of the wrong folder.
        if (not str(data_root).strip()) or (not data_root.exists()) or (not data_root.is_dir()):
            st.error(f"Data folder not found or empty: {data_root!r}. Pick a real folder of .TO2/.T02 files.")
            st.stop()

        from to2_pipeline import PipelineConfig, export_manifests, run_pipeline

        # Pre-flight: count matching files so it's obvious when the wrong folder
        # level was chosen. Cap the walk so on a multi-million-file archive the
        # pre-flight doesn't take longer than the actual scan.
        PREFLIGHT_CAP = 100_000
        match_count = None
        match_capped = False
        try:
            from to2_pipeline import _iter_to_files  # type: ignore

            n = 0
            cache_resolved = cache_dir.resolve()
            for _ in _iter_to_files(data_root.resolve(), exclude_dirs=[cache_resolved]):
                n += 1
                if n >= PREFLIGHT_CAP:
                    match_capped = True
                    break
            match_count = n
        except Exception:
            match_count = None

        if match_count is not None:
            count_label = f"{match_count:,}{'+' if match_capped else ''}"
            st.write(f"**Matching files found**: `{count_label}` (extensions: .TO2/.T02/.TO4/.T04)")
            if match_capped:
                st.info(
                    f"Pre-flight stopped counting after {PREFLIGHT_CAP:,} files. The full scan will still process every match."
                )
            if not quick_probe and match_count < 200 and not match_capped:
                st.warning(
                    "This looks like a *subset* (very few matching files). You probably selected the wrong folder level. "
                    "Pick the parent folder that contains all station/day subfolders.",
                )
            if quick_probe and match_count < 4:
                st.warning("Very few matching files found. Probe mode may return almost nothing.")

        prog = st.progress(0, text="Starting scan...")

        def _cb(i: int, total: int, path: str) -> None:
            pct = int((i / max(1, total)) * 100)
            prog.progress(min(100, pct), text=f"Scanning {i}/{total}: {Path(path).name}")

        cfg = PipelineConfig(
            data_root=data_root.resolve(),
            cache_dir=cache_dir.resolve(),
            convbin_path=convbin_path.resolve() if use_convbin and convbin_path.exists() else None,
            runpkr00_path=runpkr_path.resolve() if use_trimble and runpkr_path.exists() else None,
            teqc_path=teqc_path.resolve() if use_trimble and teqc_path.exists() else None,
            max_files_per_station=1 if quick_probe else None,
            # IMPORTANT:
            # - Probe mode: stop after first successful conversion per station (fast station metadata).
            # - Full scan: do NOT stop early, otherwise it silently "skips" most files per station.
            stop_after_success_per_station=bool(quick_probe),
            convert_cmd_template=os.environ.get("GNSS_CONVERT_CMD") or None,
        )
        with st.spinner("Scanning and caching results..."):
            if force_rescan:
                # Delete the cache DB AND the converted RINEX dir so a forced rescan is
                # truly deterministic. Errors here are not fatal -- run_pipeline will
                # recreate everything it needs.
                # NOTE: we use WAL mode, which leaves -wal and -shm sidecars next to
                # the .sqlite file. If we delete the main DB but leave those, sqlite
                # can revive a stale snapshot on the next open. Sweep all 3.
                import shutil as _shutil
                cache_resolved = cache_dir.resolve()
                for sidecar in (
                    "scan_cache.sqlite",
                    "scan_cache.sqlite-wal",
                    "scan_cache.sqlite-shm",
                    "scan_cache.sqlite-journal",
                ):
                    try:
                        p = cache_resolved / sidecar
                        if p.exists():
                            p.unlink()
                    except Exception:
                        pass
                try:
                    rinex_old = cache_resolved / "rinex"
                    if rinex_old.exists():
                        _shutil.rmtree(rinex_old, ignore_errors=True)
                except Exception:
                    pass
                # Also bust any Streamlit caches that reference the old manifest
                # (download buttons, manifest CSV cache, etc.).
                try:
                    st.cache_data.clear()
                except Exception:
                    pass
            db_path = run_pipeline(cfg, progress_cb=_cb)
            manifests_dir = export_manifests(db_path, out_dir=cache_dir.resolve() / "exported")
            st.session_state["_gnss_last_scan_manifests_dir"] = str(manifests_dir.resolve())
            st.session_state["_gnss_last_scan_cache_key"] = scan_cache_key
        # Post-flight summary: make it obvious what happened.
        sum_path = manifests_dir / "summary.json"
        if sum_path.exists():
            try:
                s = json.loads(sum_path.read_text(encoding="utf-8", errors="ignore"))
                st.success(
                    f"Scan complete ({mode_label}). "
                    f"files={int(s.get('files_in_manifest', 0)):,}, "
                    f"stations={int(s.get('unique_prefixes', 0)):,}, "
                    f"total={(float(s.get('total_bytes', 0)) / (1024**3)):.2f} GB. "
                    f"Manifests: {manifests_dir}"
                )
                if mode_label == "FULL" and int(s.get("files_in_manifest", 0)) < 200:
                    st.warning(
                        "Full scan produced very few files. This almost always means you selected the wrong folder level "
                        "or your files don't actually end with .TO2/.T02/.TO4/.T04.",
                    )
            except Exception:
                st.success(f"Scan complete. Manifests: {manifests_dir}")
        else:
            st.success(f"Scan complete. Manifests: {manifests_dir}")
    elif source == "URL (zip of manifests)":
        url = st.text_input("Manifests zip URL", value=os.environ.get("GNSS_MANIFESTS_ZIP_URL", ""))
        url_norm = (url or "").strip()
        last_zip_url = str(st.session_state.get("_gnss_last_zip_url") or "").strip()
        last_zip_dir_s = str(st.session_state.get("_gnss_last_zip_manifests_dir") or "").strip()
        cand_zip = Path(last_zip_dir_s).expanduser() if last_zip_dir_s else None
        zip_cache_ok = (
            cand_zip is not None
            and (cand_zip / "files_manifest.csv").exists()
            and (cand_zip / "summary.json").exists()
        )

        # Same pattern as "Scan folder": empty widget + st.stop() on every rerun
        # blanked the whole app. Allow continuing from the last successful download.
        if not url_norm:
            if zip_cache_ok and cand_zip is not None:
                manifests_dir = cand_zip
                st.success(
                    "Using manifests from **last URL download** in this session. "
                    "Paste a URL to fetch a different zip."
                )
            else:
                st.info("Paste a URL to a zip containing `files_manifest.csv` and `summary.json`.")
                st.stop()
        elif url_norm == last_zip_url and zip_cache_ok and cand_zip is not None:
            manifests_dir = cand_zip
            st.caption(f"Using extracted manifests from this URL (no re-download): `{manifests_dir}`")
        else:
            with st.spinner("Downloading manifests..."):
                manifests_dir = _download_and_extract_manifests_zip(url_norm)
            st.session_state["_gnss_last_zip_url"] = url_norm
            st.session_state["_gnss_last_zip_manifests_dir"] = str(manifests_dir.resolve())
            st.caption(f"Loaded manifests from URL into `{manifests_dir}`")
    else:
        up = st.file_uploader("Upload manifests zip", type=["zip"])
        last_up_sig = str(st.session_state.get("_gnss_upload_sig") or "").strip()
        last_up_dir_s = str(st.session_state.get("_gnss_last_upload_manifests_dir") or "").strip()
        cand_up = Path(last_up_dir_s).expanduser() if last_up_dir_s else None
        upload_cache_ok = (
            cand_up is not None
            and (cand_up / "files_manifest.csv").exists()
            and (cand_up / "summary.json").exists()
        )

        if up is None:
            if upload_cache_ok and cand_up is not None:
                manifests_dir = cand_up
                st.success(
                    "Using manifests from **last upload** in this session. "
                    "Choose a file above to replace."
                )
            else:
                st.info("Upload a zip containing `files_manifest.csv` and `summary.json`.")
                st.stop()

        if up is not None:
            sig = f"{getattr(up, 'name', '')}|{int(getattr(up, 'size', 0) or 0)}"
            if sig == last_up_sig and upload_cache_ok and cand_up is not None:
                manifests_dir = cand_up
                st.caption(f"Using extracted manifests from this upload (no re-extract): `{manifests_dir}`")
            else:
                tmp = Path(tempfile.mkdtemp(prefix="gnss_manifests_upload_"))
                zpath = tmp / "upload.zip"
                # Defend against an oversize upload (Streamlit allows up to 200MB by
                # default, but a malicious user could still try a zip-bomb).
                upload_bytes = up.getvalue()
                if len(upload_bytes) > _MAX_DOWNLOAD_BYTES:
                    st.error(f"Uploaded zip is {len(upload_bytes):,} bytes (cap is {_MAX_DOWNLOAD_BYTES:,}).")
                    st.stop()
                zpath.write_bytes(upload_bytes)
                try:
                    _safe_extract_zip(zpath, tmp)
                    manifests_dir = _find_manifests_dir(tmp)
                except FileNotFoundError:
                    st.error("Could not find `files_manifest.csv` + `summary.json` in the uploaded zip.")
                    st.stop()
                except ValueError as ve:
                    st.error(f"Upload rejected: {ve}")
                    st.stop()
                st.session_state["_gnss_upload_sig"] = sig
                st.session_state["_gnss_last_upload_manifests_dir"] = str(manifests_dir.resolve())
                st.caption(f"Loaded uploaded manifests into `{manifests_dir}`")

    manifest_csv = manifests_dir / "files_manifest.csv"
    summary_json = manifests_dir / "summary.json"

    if not manifest_csv.exists():
        st.error(f"Missing manifest: {manifest_csv}")
        st.stop()
    if not summary_json.exists():
        st.error(f"Missing summary: {summary_json}")
        st.stop()

    st.divider()
    st.header("Utilities")
    st.caption("Extras added from the newer dashboard: downloads + coordinate autofill.")
    offline_mode = os.environ.get("GNSS_OFFLINE", "").strip() in ("1", "true", "TRUE", "yes", "YES")
    # Default OFF. On offline machines (most clients) hitting GeoNet on every
    # rerun freezes the UI for ~3s. Users who *want* it on the office network
    # can flip this on and it's still cached + retry-suppressed.
    auto_geonet = st.checkbox(
        "Auto-fill station coords from GeoNet (NZ)",
        value=False,
        help="Online only. After one failed call we stop retrying for this session.",
        disabled=offline_mode,
    )

summary = _load_json(summary_json)
try:
    _mf_stat = manifest_csv.stat()
    df = _load_manifest_csv_cached(str(manifest_csv), _mf_stat.st_mtime, _mf_stat.st_size)
except Exception:
    df = _load_manifest_csv(manifest_csv)
cap_df = _try_load_station_capabilities(manifests_dir)

if df.empty:
    st.warning("Manifest is empty. (No files matched your scan filters.)")
    st.stop()


@st.cache_data(show_spinner=False)
def _build_event_ts(df_in: pd.DataFrame) -> pd.Series:
    """
    Vectorized fast path with per-row fallback for tricky filenames.

    1) Try a vectorized YYYYMMDDHHMMSS / HHMM / HH match across the joined
       (discovered_from + file_name) string. This handles the vast majority
       of GNSS station filenames in a few ms even for 100k rows.
    2) For rows that didn't match, fall back to the row-wise _infer_ts_from_row
       (RINEX2/3 naming etc.).
    """
    fn = df_in.get("file_name", pd.Series([""] * len(df_in))).astype(str)
    df_path = df_in.get("discovered_from", pd.Series([""] * len(df_in))).astype(str)
    joined = df_path.str.cat(fn, sep=" ")
    out = pd.Series(pd.NaT, index=df_in.index, dtype="datetime64[ns, UTC]")

    # YYYYMMDDHHMMSS first, then YYYYMMDDHHMM, then YYYYMMDDHH.
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

    # Fill remaining gaps using the slow per-row inference.
    miss_mask = out.isna()
    if miss_mask.any():
        sub = df_in.loc[miss_mask]
        # Worst-case: hundreds of thousands of TO2 filenames with no numeric
        # timestamp token -> row-wise inference becomes O(n^2 perceived) UI freeze.
        if len(sub) <= 50_000:
            slow = sub.apply(_infer_ts_from_row, axis=1)
            out.loc[miss_mask] = pd.to_datetime(slow, utc=True, errors="coerce")
        elif "modified_utc" in df_in.columns:
            out.loc[miss_mask] = pd.to_datetime(sub["modified_utc"], utc=True, errors="coerce")

    # Final safety net: anything still NaT -> use modified_utc, then now.
    if out.isna().any():
        fb = pd.to_datetime(df_in.get("modified_utc", pd.NaT), utc=True, errors="coerce")
        out = out.fillna(fb)
    if out.isna().any():
        out = out.fillna(pd.Timestamp.now(tz="UTC"))
    return out


df["size_mb"] = df["size_bytes"] / (1024 * 1024)
with st.spinner("Inferring event timestamps..."):
    df["event_ts_utc"] = _build_event_ts(df)
# pandas>=3 prefers lowercase freq aliases
df["event_hour_utc"] = df["event_ts_utc"].dt.floor("h")

# Prefer robust station inference if present in manifest.
station_col = "station" if "station" in df.columns else "prefix"
df[station_col] = _normalize_station_id_series(df[station_col])

# If lat/lon are missing (common for T02/T04-only datasets), best-effort enrich from GeoNet.
# Suppress retries for the rest of the session if we already failed once -- otherwise
# every interaction freezes the UI for ~3s waiting for a network timeout.
if (
    auto_geonet
    and {"lat", "lon"}.issubset(df.columns)
    and not st.session_state.get("_geonet_failed", False)
):
    lat_ok = pd.to_numeric(df["lat"], errors="coerce").notna().any()
    lon_ok = pd.to_numeric(df["lon"], errors="coerce").notna().any()
    if not (lat_ok and lon_ok):
        stations = tuple(sorted(df[station_col].dropna().astype(str).str.lower().unique().tolist()))
        try:
            coords = _geonet_station_coords(stations)
        except Exception:
            coords = pd.DataFrame(columns=["station", "lat", "lon", "site_name"])
        # Treat "no coords returned" the same as a failure -- otherwise we'd
        # retry the (slow) HTTP call on every interaction and freeze the UI.
        if coords.empty:
            st.session_state["_geonet_failed"] = True
            st.caption("GeoNet lookup returned no coordinates (offline or unknown stations). Disabled for this session.")
        if not coords.empty:
            df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
            df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
            df = df.merge(
                coords.rename(columns={"station": station_col}),
                on=station_col,
                how="left",
                suffixes=("", "_geonet"),
            )
            df["lat"] = df["lat"].fillna(df.get("lat_geonet"))
            df["lon"] = df["lon"].fillna(df.get("lon_geonet"))
            # Keep the UI clean (these may not exist if merge didn't create them)
            for c in ["lat_geonet", "lon_geonet", "site_name"]:
                if c in df.columns:
                    pass

# Utility downloads (manifest zip + sqlite db).
# IMPORTANT: build the bytes lazily AND cache them. Previously these ran on
# EVERY rerun -- on a 100k-row manifest that meant a fresh SQLite write +
# read-back on every keystroke, which made the sidebar feel frozen.

@st.cache_data(show_spinner=False)
def _build_manifests_zip_bytes(manifest_csv_str: str, summary_json_str: str, station_caps_str: str | None, mtime_csv: float, mtime_json: float) -> bytes:
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(manifest_csv_str, arcname="files_manifest.csv")
        z.write(summary_json_str, arcname="summary.json")
        if station_caps_str and Path(station_caps_str).exists():
            z.write(station_caps_str, arcname="station_capabilities.csv")
    return zbuf.getvalue()


@st.cache_data(show_spinner=False)
def _build_manifest_sqlite_bytes(df_in: pd.DataFrame) -> bytes:
    """
    Serialize the manifest DataFrame into a one-off SQLite DB and return its
    bytes. Cached on the dataframe hash so reruns don't re-do the work.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="gnss_dash_db_"))
    db_path = tmp_dir / "gnss_manifest.db"
    try:
        _manifest_to_sqlite(df_in, db_path)
        return db_path.read_bytes()
    finally:
        # Cleanup the temp dir; we have the bytes in memory now.
        try:
            import shutil as _shutil
            _shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


with st.sidebar:
    try:
        _csv_stat = manifest_csv.stat()
        _json_stat = summary_json.stat()
        _caps_path = manifests_dir / "station_capabilities.csv"
        zip_bytes = _build_manifests_zip_bytes(
            str(manifest_csv),
            str(summary_json),
            str(_caps_path) if _caps_path.exists() else None,
            _csv_stat.st_mtime,
            _json_stat.st_mtime,
        )
        st.download_button(
            "Download manifests zip",
            data=zip_bytes,
            file_name="manifests.zip",
            mime="application/zip",
            use_container_width=True,
        )
    except Exception:
        pass

    try:
        db_bytes = _build_manifest_sqlite_bytes(df)
        st.download_button(
            "Download SQLite DB (from manifest)",
            data=db_bytes,
            file_name="gnss_manifest.db",
            mime="application/octet-stream",
            use_container_width=True,
        )
    except Exception:
        pass

tab_overview, tab_station, tab_map, tab_vrs, tab_raw = st.tabs(["Overview", "Station coverage", "Map", "VRS", "Raw data"])

with _safe_tab("Overview", tab_overview):
    st.subheader("Dataset summary")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Files", int(summary.get("files_in_manifest", len(df))))
    col2.metric("Stations (prefixes)", int(summary.get("unique_prefixes", df["prefix"].nunique())))
    col3.metric("Extensions", int(summary.get("unique_exts", df["ext"].nunique())))
    col4.metric("Total size (GB)", f"{(df['size_bytes'].sum() / (1024**3)):.2f}")

    with st.expander("Scan details"):
        st.json(summary)

    st.markdown(f"### Stations by volume ({station_col})")
    by_station = df.groupby(station_col, as_index=False).agg(files=("file_name", "count"), size_bytes=("size_bytes", "sum"))
    by_station["size_gb"] = by_station["size_bytes"] / (1024**3)
    by_station = by_station.sort_values(["files", "size_bytes"], ascending=False).head(100)
    st.dataframe(by_station[[station_col, "files", "size_gb"]], use_container_width=True, height=420)

    st.markdown("### Data health insights")
    # Pick a lightweight default time window for fast, high-signal analysis.
    min_day = df["event_ts_utc"].min().date()
    max_day = df["event_ts_utc"].max().date()
    o1, o2, o3 = st.columns([2, 2, 2])
    health_start = o1.date_input("Health window start", value=min_day, min_value=min_day, max_value=max_day, key="health_start")
    health_end = o2.date_input("Health window end", value=max_day, min_value=min_day, max_value=max_day, key="health_end")
    min_files_per_hour = o3.number_input("Active if ≥ files/hour", min_value=1, max_value=1000, value=1, step=1, key="health_min_files_per_hour")

    win_start = pd.Timestamp(health_start, tz="UTC")
    win_end = pd.Timestamp(health_end, tz="UTC") + pd.Timedelta(hours=23)
    hours = pd.date_range(win_start, win_end, freq="h", tz="UTC")
    if len(hours) == 0:
        st.warning("Empty health window. Pick a wider range.")
        raise _SkipTab()

    # Coverage per station in window (24/7 expected, for overview health).
    wdf = df[(df["event_hour_utc"] >= win_start) & (df["event_hour_utc"] <= win_end)].copy()
    counts = wdf.groupby([station_col, "event_hour_utc"], as_index=False).agg(file_count=("file_name", "count"))
    counts["covered"] = counts["file_count"] >= int(min_files_per_hour)

    # Defensive cap. The full station x hour grid can explode for huge datasets
    # (e.g. 10k stations x 8760 hours = ~88M rows -> several GB). Cap to the top
    # N stations by file count and warn the user instead of OOM-ing.
    GRID_MAX_CELLS = 5_000_000
    all_station_counts = (
        df[station_col].dropna().value_counts().rename_axis(station_col).reset_index(name="files")
    )
    n_hours = len(hours)
    max_stations = max(1, GRID_MAX_CELLS // max(1, n_hours))
    if len(all_station_counts) > max_stations:
        st.warning(
            f"Showing top **{max_stations:,}** stations by file count (out of {len(all_station_counts):,}) "
            "to keep the dashboard responsive. Narrow the date range to include more stations."
        )
        stations_all = all_station_counts[station_col].head(max_stations).tolist()
    else:
        stations_all = all_station_counts[station_col].tolist()
    grid = pd.MultiIndex.from_product([stations_all, hours], names=[station_col, "event_hour_utc"]).to_frame(index=False)
    merged = grid.merge(counts[[station_col, "event_hour_utc", "covered"]], on=[station_col, "event_hour_utc"], how="left")
    merged["covered"] = merged["covered"].fillna(False)
    cov = merged.groupby(station_col, as_index=False).agg(coverage_pct=("covered", "mean"), missing_hours=("covered", lambda s: int((~s).sum())))
    cov["coverage_pct"] = cov["coverage_pct"] * 100.0

    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Stations", f"{len(stations_all):,}")
    h2.metric("Window hours", f"{len(hours):,}")
    h3.metric("Median station coverage", f"{cov['coverage_pct'].median():.1f}%")
    h4.metric("Stations <50% coverage", f"{int((cov['coverage_pct'] < 50).sum()):,}")

    # Distribution + top issues
    left, right = st.columns([2, 2])
    with left:
        fig = px.histogram(cov, x="coverage_pct", nbins=20, title="Station coverage distribution (%)")
        st.plotly_chart(fig, use_container_width=True)
    with right:
        worst = cov.sort_values(["coverage_pct", "missing_hours"], ascending=[True, False]).head(20)
        best = cov.sort_values(["coverage_pct", "missing_hours"], ascending=[False, True]).head(10)
        st.markdown("**Worst stations (by coverage)**")
        st.dataframe(worst, use_container_width=True, height=220)
        st.markdown("**Best stations**")
        st.dataframe(best, use_container_width=True, height=220)

    if "station_source" in df.columns:
        st.markdown("### Station inference audit")
        src = df["station_source"].fillna("unknown").astype(str)
        src_counts = src.value_counts().reset_index()
        src_counts.columns = ["station_source", "files"]
        fig = px.pie(src_counts, names="station_source", values="files", title="How station IDs were inferred")
        st.plotly_chart(fig, use_container_width=True)

    if "signals" in df.columns:
        st.markdown("### Receiver inventory (from runpkr00 / embedded headers)")
        inv = _extract_runpkr00_inventory(df["signals"])
        if inv.empty:
            st.info("No receiver inventory found in this manifest (likely not T02/T04 or not scanned with runpkr00).")
        else:
            inv2 = inv.groupby(["receiver", "fw"], dropna=False, as_index=False).size().rename(columns={"size": "files"})
            inv2 = inv2.sort_values("files", ascending=False).head(50)
            st.dataframe(inv2, use_container_width=True, height=380)

    if not cap_df.empty:
        st.markdown("### TO2/T04 capabilities (from runpkr00 + teqc)")
        st.caption("Derived by converting a sample of T02/T04 files to RINEX and reading the observation header.")
        show = cap_df.copy()
        # Keep it readable for PMs
        show["signals"] = show["signals"].astype(str).str.slice(0, 180)
        st.dataframe(show.sort_values("station").head(500), use_container_width=True, height=420)


with _safe_tab("Station", tab_station):
    st.subheader("Station coverage analysis")
    station_counts = df.groupby(station_col, as_index=False).size().rename(columns={"size": "files"})
    station_counts = station_counts.sort_values("files", ascending=False)
    all_prefixes = station_counts[station_col].tolist()
    if not all_prefixes:
        st.warning("No stations in the manifest. Run a scan first.")
        raise _SkipTab()
    # Pick a real default from the actual data (do not hardcode "for"; that's a
    # debug leftover that produced an empty selection on every other dataset).
    default_focus = all_prefixes[0]

    c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
    # Avoid blank/stop behavior while typing by using an explicit submit form.
    if "station_query" not in st.session_state:
        st.session_state.station_query = default_focus
    if "station_select_value" not in st.session_state:
        st.session_state.station_select_value = None

    with c1.form("station_pick_form", clear_on_submit=False):
        q = st.text_input("Find station", value=st.session_state.station_query)
        submitted = st.form_submit_button("Apply", use_container_width=True)
    if submitted:
        st.session_state.station_query = q

    query = (st.session_state.station_query or "").strip().lower()
    filtered = [p for p in all_prefixes if query in str(p).lower()] if query else all_prefixes
    if not filtered:
        st.info("No stations match your search; showing all stations.")
        filtered = all_prefixes

    opts = filtered[:500]
    prior = st.session_state.get("station_select_value")
    # Streamlit ignores `index=` on reruns — the widget's session_state wins. So
    # after a rescan/new manifest, or after the search box narrows options, the
    # old `station_select_value` can point at a station ID that no longer exists
    # in `df`, producing an empty filter and the confusing "No records" message
    # even though `len(df)` is large. Force-coerce to a valid option BEFORE the
    # selectbox is instantiated.
    if opts and (prior not in opts):
        st.session_state["station_select_value"] = opts[0]

    station = c1.selectbox(
        f"Station ({station_col})",
        options=opts,
        key="station_select_value",
    )
    # Defensive: same normalization as df[station_col] (numeric-ish widget return values).
    station = _normalize_station_id_series(pd.Series([station])).iloc[0]

    exts = sorted(df["ext"].dropna().unique().tolist())
    default_exts = [e for e in exts if e in {".to2", ".t02", ".to4", ".t04", ".zip", ".gz", ".rnx", ".obs", ".o"}] or exts
    sel_ext = c2.multiselect("Include extensions", exts, default=default_exts)
    # Empty selection means "all extensions" -- otherwise the next filter would
    # always return zero rows and blank the tab.
    if not sel_ext:
        sel_ext = exts

    tz_name = c3.selectbox("Timezone", options=["UTC"], index=0)
    week_start = c4.selectbox("Week starts on", options=["Mon"], index=0, disabled=True)

    sdf = df[(df[station_col] == station) & (df["ext"].isin(sel_ext))].copy()
    if sdf.empty:
        sdf = df[df[station_col] == station].copy()
    if sdf.empty:
        st.warning(f"No records for station '{station}'. Pick a different station.")
        raise _SkipTab()

    sdf["event_ts"] = _to_tz(sdf["event_ts_utc"], tz_name)
    sdf["event_hour"] = sdf["event_ts"].dt.floor("h")

    # Coverage range controls
    min_ts = sdf["event_hour"].min()
    max_ts = sdf["event_hour"].max()
    r1, r2, r3 = st.columns([2, 2, 2])
    start_date = r1.date_input("Start date", value=min_ts.date())
    end_date = r2.date_input("End date", value=max_ts.date())
    expected_mode = r3.selectbox("Expected schedule", options=["24/7", "Business hours", "Custom"], index=0)

    # Build expected-hours mask (dow, hour)
    if expected_mode == "24/7":
        expected_dows = set(range(7))
        expected_hours = set(range(24))
    elif expected_mode == "Business hours":
        expected_dows = set(range(5))  # Mon-Fri
        expected_hours = set(range(9, 18))  # 09..17
    else:
        cd1, cd2 = st.columns([2, 2])
        expected_dows = set(cd1.multiselect("Days", options=_day_labels(), default=_day_labels()))
        expected_dows = { _day_labels().index(d) for d in expected_dows } if expected_dows else set()
        hr = cd2.slider("Hours", 0, 23, (0, 23))
        expected_hours = set(range(int(hr[0]), int(hr[1]) + 1))

    # Construct full hourly timeline in selected range
    start_ts = pd.Timestamp(start_date, tz=tz_name)
    end_ts = pd.Timestamp(end_date, tz=tz_name) + pd.Timedelta(hours=23)
    full_hours = pd.date_range(start=start_ts, end=end_ts, freq="h", tz=tz_name)
    timeline = pd.DataFrame({"event_hour": full_hours})
    hour_counts = sdf.groupby("event_hour", as_index=False).agg(file_count=("file_name", "count"))
    timeline = timeline.merge(hour_counts, on="event_hour", how="left")
    timeline["file_count"] = timeline["file_count"].fillna(0).astype(int)
    timeline["has_data"] = timeline["file_count"] > 0
    timeline["dow"] = timeline["event_hour"].dt.dayofweek
    timeline["hour"] = timeline["event_hour"].dt.hour
    timeline["is_expected"] = timeline["dow"].isin(list(expected_dows)) & timeline["hour"].isin(list(expected_hours))

    expected = timeline[timeline["is_expected"]].copy()
    if expected.empty:
        st.warning("No expected hours in the selected date range + schedule. Adjust schedule/range.")
        raise _SkipTab()

    cov = expected["has_data"].mean()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Coverage (expected hours)", f"{cov * 100:.1f}%")
    m2.metric("Expected hours", f"{len(expected):,}")
    m3.metric("Hours with data", f"{int(expected['has_data'].sum()):,}")
    m4.metric("Hours missing", f"{int((~expected['has_data']).sum()):,}")

    st.markdown("### Gap diagnostics (expected hours)")
    # Longest missing streak and most-missed hours of day.
    expected_sorted = expected.sort_values("event_hour")
    longest_gap = _longest_false_run(expected_sorted["has_data"])
    g1, g2, g3 = st.columns(3)
    g1.metric("Longest missing streak (hours)", f"{int(longest_gap):,}")
    # Most missed hours-of-day (systematic issues)
    miss = expected_sorted[~expected_sorted["has_data"]].copy()
    if miss.empty:
        g2.metric("Most-missed hour", "—")
        g3.metric("Most-missed day", "—")
    else:
        by_hour = miss.groupby("hour", as_index=False).size().rename(columns={"size": "missing"})
        by_hour = by_hour.sort_values("missing", ascending=False)
        by_day = miss.groupby("dow", as_index=False).size().rename(columns={"size": "missing"})
        by_day = by_day.sort_values("missing", ascending=False)
        top_hour = int(by_hour.iloc[0]["hour"])
        top_day = int(by_day.iloc[0]["dow"])
        g2.metric("Most-missed hour", f"{top_hour:02d}:00 (missing {int(by_hour.iloc[0]['missing']):,})")
        g3.metric("Most-missed day", f"{_day_labels()[top_day]} (missing {int(by_day.iloc[0]['missing']):,})")

        st.markdown("**Top missing hours (hour-of-day)**")
        by_hour["hour_label"] = by_hour["hour"].apply(lambda h: f"{int(h):02d}:00")
        st.dataframe(by_hour[["hour_label", "missing"]].head(12), use_container_width=True, height=240)

    # Capabilities (constellations/signals) if present in manifest
    if "constellations" in sdf.columns or "signals" in sdf.columns:
        cs = sorted({c for v in sdf.get("constellations", pd.Series(dtype=str)).dropna().tolist() for c in _parse_csv_list(v)})
        ss = sorted({s for v in sdf.get("signals", pd.Series(dtype=str)).dropna().tolist() for s in _parse_csv_list(v)})
        cap1, cap2 = st.columns(2)
        with cap1:
            st.markdown("### Constellations")
            st.write(", ".join(cs) if cs else "— (not parsed / not present)")
        with cap2:
            st.markdown("### Signals / Obs types")
            st.write(", ".join(ss[:200]) + (" …" if len(ss) > 200 else "") if ss else "— (not parsed / not present)")

    st.markdown("### Weekly hour-by-hour coverage (expected hours)")
    expected["iso_year"] = expected["event_hour"].dt.isocalendar().year.astype(int)
    expected["iso_week"] = expected["event_hour"].dt.isocalendar().week.astype(int)
    expected["week_label"] = expected["iso_year"].astype(str) + "-W" + expected["iso_week"].astype(str).str.zfill(2)
    expected["has_data_int"] = expected["has_data"].astype(int)

    # "Average: Sundays 15-16 active 83%" style control
    st.markdown("### Answer: “On <day> between <h1>–<h2> active X%”")
    a1, a2, a3 = st.columns([2, 2, 2])
    day = a1.selectbox("Day", options=_day_labels(), index=_day_labels().index("Sun"))
    h_from, h_to = a2.slider("Hours window", 0, 23, (15, 16))
    scope = a3.selectbox("Scope", options=["Across selected range", "Per-week table"], index=0)

    target = expected[(expected["dow"] == _day_labels().index(day)) & (expected["hour"].between(int(h_from), int(h_to)))].copy()
    if target.empty:
        st.info("No expected hours match that day/hour window in the selected range.")
    else:
        ratio = target["has_data"].mean()
        st.success(f"{day} {h_from:02d}:00–{h_to:02d}:59 active **{ratio * 100:.1f}%** (expected hours only)")
        if scope == "Per-week table":
            by_week = target.groupby("week_label", as_index=False).agg(
                expected_hours=("has_data", "count"),
                active_hours=("has_data", "sum"),
            )
            by_week["active_pct"] = (by_week["active_hours"] / by_week["expected_hours"]) * 100.0
            st.dataframe(by_week.sort_values("week_label"), use_container_width=True, height=360)

    # Heatmaps: per selected week and aggregate across all weeks
    week_labels = sorted(expected["week_label"].dropna().unique().tolist())
    if not week_labels:
        st.warning("No weeks available in this date range. Expand the date range or check your station selection.")
        raise _SkipTab()

    selected_week = st.selectbox("Week to view", week_labels, index=len(week_labels) - 1)
    week_df = expected[expected["week_label"] == selected_week].copy()

    week_mat = _coverage_matrix(
        week_df.groupby(["dow", "hour"], as_index=False)["has_data_int"].max(),
        value_col="has_data_int",
    )
    fig_week = px.imshow(
        week_mat,
        labels=dict(x="Hour", y="Day", color="Has data"),
        color_continuous_scale=["#d62728", "#2ca02c"],
        zmin=0,
        zmax=1,
        aspect="auto",
        title=f"{station} — Week {selected_week} (expected hours): 1=has data, 0=missing",
    )
    st.plotly_chart(fig_week, use_container_width=True)

    agg = (
        expected.groupby(["week_label", "dow", "hour"], as_index=False)["has_data_int"].max()
        .groupby(["dow", "hour"], as_index=False)["has_data_int"].mean()
        .rename(columns={"has_data_int": "coverage_ratio"})
    )
    agg_mat = _coverage_matrix(agg, value_col="coverage_ratio")
    fig_agg = px.imshow(
        agg_mat,
        labels=dict(x="Hour", y="Day", color="Coverage"),
        color_continuous_scale="Viridis",
        zmin=0.0,
        zmax=1.0,
        aspect="auto",
        title=f"{station} — Across all weeks: fraction of weeks with data (expected hours)",
    )
    st.plotly_chart(fig_agg, use_container_width=True)

    st.markdown("### Whole-week table: % covered (week-by-week)")
    st.caption("X = day of week, Y = hour (0–23). Values are % covered for that hour.")

    # Week navigation with arrows
    # Reset week index when station changes, and always clamp to valid range.
    if st.session_state.get("week_nav_station") != station:
        st.session_state.week_nav_station = station
        st.session_state.week_nav_idx = len(week_labels) - 1
    if "week_nav_idx" not in st.session_state:
        st.session_state.week_nav_idx = len(week_labels) - 1
    st.session_state.week_nav_idx = int(max(0, min(len(week_labels) - 1, int(st.session_state.week_nav_idx))))

    nav1, nav2, nav3 = st.columns([1, 2, 1])
    with nav1:
        if st.button("◀ Prev week", use_container_width=True, disabled=len(week_labels) <= 1) and st.session_state.week_nav_idx > 0:
            st.session_state.week_nav_idx -= 1
    with nav3:
        if (
            st.button("Next week ▶", use_container_width=True, disabled=len(week_labels) <= 1)
            and st.session_state.week_nav_idx < len(week_labels) - 1
        ):
            st.session_state.week_nav_idx += 1
    with nav2:
        if len(week_labels) > 1:
            st.session_state.week_nav_idx = st.slider(
                "Week index",
                0,
                len(week_labels) - 1,
                int(st.session_state.week_nav_idx),
                label_visibility="collapsed",
            )
        else:
            # Avoid StreamlitAPIException: slider min must be < max.
            st.caption("Only one week in this date range.")

    nav_week = week_labels[int(st.session_state.week_nav_idx)] if week_labels else selected_week
    st.write(f"Showing: **{nav_week}**")

    nav_week_df = expected[expected["week_label"] == nav_week].copy()
    nav_week_df["covered_pct"] = nav_week_df["has_data_int"] * 100.0
    week_pct = (
        nav_week_df.groupby(["dow", "hour"], as_index=False)["covered_pct"].max()
        .pivot(index="hour", columns="dow", values="covered_pct")
        .reindex(index=range(24), columns=range(7), fill_value=0.0)
    )
    week_pct.columns = _day_labels()
    hour_labels = [f"{h:02d}:00" for h in range(24)]
    fig_week_pct = px.imshow(
        week_pct,
        x=_day_labels(),
        y=hour_labels,
        labels=dict(x="Day of week", y="Hour of day", color="% covered"),
        color_continuous_scale="Viridis",
        zmin=0.0,
        zmax=100.0,
        aspect="auto",
        title=f"{station} — {nav_week}: % covered per day/hour (expected hours)",
    )
    fig_week_pct.update_yaxes(autorange="reversed")
    st.plotly_chart(fig_week_pct, use_container_width=True)

    st.markdown("### Whole-week table: % covered (selected date range)")
    st.caption("Same layout, but % is computed across the full selected date range (so values like 83% are meaningful).")

    range_pct = (
        expected.groupby(["dow", "hour"], as_index=False)["has_data_int"].mean()
        .rename(columns={"has_data_int": "coverage_ratio"})
    )
    range_pct["coverage_pct"] = range_pct["coverage_ratio"] * 100.0
    range_mat_pct = (
        range_pct.pivot(index="hour", columns="dow", values="coverage_pct")
        .reindex(index=range(24), columns=range(7), fill_value=0.0)
    )
    range_mat_pct.columns = _day_labels()
    fig_range_pct = px.imshow(
        range_mat_pct,
        x=_day_labels(),
        y=hour_labels,
        labels=dict(x="Day of week", y="Hour of day", color="% covered"),
        color_continuous_scale="Viridis",
        zmin=0.0,
        zmax=100.0,
        aspect="auto",
        title=f"{station} — Selected range: % covered per day/hour (expected hours)",
    )
    fig_range_pct.update_yaxes(autorange="reversed")
    st.plotly_chart(fig_range_pct, use_container_width=True)

    st.markdown("### Timeline (expected hours only)")
    line = expected.sort_values("event_hour")
    fig_tl = go.Figure()
    fig_tl.add_trace(go.Scatter(x=line["event_hour"], y=line["file_count"], mode="lines", name="Files/hour"))
    fig_tl.add_trace(go.Scatter(
        x=line.loc[~line["has_data"], "event_hour"],
        y=[0] * int((~line["has_data"]).sum()),
        mode="markers",
        name="Missing hour",
        marker=dict(color="#d62728", size=5),
    ))
    fig_tl.update_layout(title=f"{station} — hourly counts (expected hours)", xaxis_title="Hour", yaxis_title="Files")
    st.plotly_chart(fig_tl, use_container_width=True)

with _safe_tab("Raw", tab_raw):
    st.subheader("Raw records (debugging)")
    st.caption("Use this only if you need to inspect paths/files behind the computed coverage.")
    st.dataframe(df.head(2000), use_container_width=True, height=520)

with _safe_tab("VRS", tab_vrs):
    st.subheader("VRS (Virtual Reference Station) from 4 stations")
    st.caption(
        "This creates a **virtual station location** and computes **shared-hour availability** across 4 stations. "
        "It does not yet synthesize RTCM/RINEX observations."
    )

    if "lat" not in df.columns or "lon" not in df.columns:
        st.warning("No station locations found in this manifest. Re-scan with `--parse_rinex_headers` first.")
        raise _SkipTab()

    vdf = df.copy()
    vdf["lat"] = pd.to_numeric(vdf["lat"], errors="coerce")
    vdf["lon"] = pd.to_numeric(vdf["lon"], errors="coerce")
    vdf = vdf.dropna(subset=["lat", "lon"])
    if vdf.empty:
        st.warning("No stations have lat/lon in this dataset.")
        raise _SkipTab()

    # Station selector (top by file count + search)
    station_counts = vdf.groupby(station_col, as_index=False).size().rename(columns={"size": "files"}).sort_values("files", ascending=False)
    # Always coerce to str -- the manifest column is sometimes coerced to
    # numeric on accident (e.g. an all-digit prefix) and `q.lower() in s` would
    # raise TypeError on `int`.
    stations = [str(s) for s in station_counts[station_col].tolist()]

    q = st.text_input("Filter stations", value="")
    q_lc = (q or "").lower()
    stations_filtered = [s for s in stations if q_lc in s.lower()]
    if len(stations_filtered) < 4:
        st.warning("Filter matches fewer than 4 stations.")
        raise _SkipTab()

    s1, s2 = st.columns([2, 2])
    selected = s1.multiselect("Pick 4 stations", stations_filtered, default=stations_filtered[:4], max_selections=4)
    min_files_per_hour = s2.number_input(
        "Min files/hour to count as covered",
        min_value=1,
        max_value=1000,
        value=1,
        step=1,
        key="vrs_min_files_per_hour",
    )
    if len(selected) != 4:
        st.info("Select exactly 4 stations.")
        raise _SkipTab()

    # Date range + expected schedule
    min_day = vdf["event_ts_utc"].min().date()
    max_day = vdf["event_ts_utc"].max().date()
    d1, d2, d3 = st.columns([2, 2, 2])
    start_day = d1.date_input("Start day", value=min_day, min_value=min_day, max_value=max_day, key="vrs_start")
    end_day = d2.date_input("End day", value=max_day, min_value=min_day, max_value=max_day, key="vrs_end")
    expected_mode = d3.selectbox("Expected schedule", options=["24/7", "Business hours"], index=0, key="vrs_expected")

    if expected_mode == "24/7":
        expected_dows = set(range(7))
        expected_hours = set(range(24))
    else:
        expected_dows = set(range(5))
        expected_hours = set(range(9, 18))

    start_ts = pd.Timestamp(start_day, tz="UTC")
    end_ts = pd.Timestamp(end_day, tz="UTC") + pd.Timedelta(hours=23)
    full_hours = pd.date_range(start=start_ts, end=end_ts, freq="h", tz="UTC")
    tl = pd.DataFrame({"event_hour_utc": full_hours})
    tl["dow"] = tl["event_hour_utc"].dt.dayofweek
    tl["hour"] = tl["event_hour_utc"].dt.hour
    tl = tl[tl["dow"].isin(list(expected_dows)) & tl["hour"].isin(list(expected_hours))].copy()
    if tl.empty:
        st.warning("No expected hours in the selected date range.")
        raise _SkipTab()

    # Build coverage per station per hour
    base = vdf[vdf[station_col].isin(selected)].copy()
    counts = base.groupby([station_col, "event_hour_utc"], as_index=False).agg(file_count=("file_name", "count"))
    counts["covered"] = counts["file_count"] >= int(min_files_per_hour)

    grid = pd.MultiIndex.from_product([selected, tl["event_hour_utc"].tolist()], names=[station_col, "event_hour_utc"]).to_frame(index=False)
    cov = grid.merge(counts[[station_col, "event_hour_utc", "covered"]], on=[station_col, "event_hour_utc"], how="left")
    cov["covered"] = cov["covered"].fillna(False)

    wide = cov.pivot(index="event_hour_utc", columns=station_col, values="covered").fillna(False)
    wide["all_4"] = wide.all(axis=1)
    shared_hours = int(wide["all_4"].sum())
    shared_pct = 100.0 * (shared_hours / len(wide)) if len(wide) else 0.0

    m1, m2, m3 = st.columns(3)
    m1.metric("Expected hours in range", f"{len(wide):,}")
    m2.metric("Hours covered by all 4", f"{shared_hours:,}")
    m3.metric("% shared coverage", f"{shared_pct:.1f}%")

    # VRS location: centroid or custom
    loc = vdf.groupby(station_col, as_index=False).agg(lat=("lat", "median"), lon=("lon", "median"))
    loc4 = loc[loc[station_col].isin(selected)].copy()

    c1, c2, c3 = st.columns([2, 2, 2])
    mode = c1.selectbox("VRS location mode", options=["Centroid", "Custom lat/lon"], index=0)
    if mode == "Centroid":
        vrs_lat = float(loc4["lat"].mean())
        vrs_lon = float(loc4["lon"].mean())
    else:
        vrs_lat = float(c2.number_input("VRS lat", value=float(loc4["lat"].mean())))
        vrs_lon = float(c3.number_input("VRS lon", value=float(loc4["lon"].mean())))

    # Weights (inverse distance) for “in between” interpolation readout
    dists = []
    for _, r in loc4.iterrows():
        d = _haversine_km(vrs_lat, vrs_lon, float(r["lat"]), float(r["lon"]))
        dists.append(max(d, 0.001))
    inv = [1.0 / d for d in dists]
    s = sum(inv)
    weights = [w / s for w in inv]
    loc4["distance_km"] = dists
    loc4["weight"] = weights

    st.markdown("### VRS output")
    st.write(f"**VRS lat/lon (WGS84)**: `{vrs_lat:.6f}, {vrs_lon:.6f}`")
    st.dataframe(loc4.sort_values("weight", ascending=False), use_container_width=True, height=220)

    # Map: stations + VRS point
    plot = loc4.copy()
    plot["type"] = "Station"
    vrs_row = pd.DataFrame([{station_col: "VRS", "lat": vrs_lat, "lon": vrs_lon, "type": "VRS", "distance_km": 0.0, "weight": 1.0}])
    plot2 = pd.concat([plot[[station_col, "lat", "lon", "type", "distance_km", "weight"]], vrs_row], ignore_index=True)
    center, zoom = _auto_center_zoom(plot2["lat"], plot2["lon"])
    fig, kind = _scatter_map_compat(
        data=plot2,
        lat="lat",
        lon="lon",
        color="type",
        hover_name=station_col,
        hover_data={"distance_km": ":.2f", "weight": ":.3f"},
        height=520,
    )
    if kind == "map":
        fig.update_layout(map_style="open-street-map", map_center=center, map_zoom=zoom, margin=dict(l=0, r=0, t=0, b=0))
    else:
        fig.update_layout(mapbox_style="open-street-map", mapbox_center=center, mapbox_zoom=zoom, margin=dict(l=0, r=0, t=0, b=0))
    st.plotly_chart(fig, use_container_width=True)

with _safe_tab("Map", tab_map):
    st.subheader("Station map (coverage by day)")
    st.caption("Select a day; stations are colored by % covered for that day (expected hours).")

    if "lat" not in df.columns or "lon" not in df.columns:
        st.warning("This manifest has no lat/lon. Re-scan with `--parse_rinex_headers` to extract station positions from RINEX headers, or provide a station-location source.")
        raise _SkipTab()

    mdf = df.copy()
    mdf["lat"] = pd.to_numeric(mdf["lat"], errors="coerce")
    mdf["lon"] = pd.to_numeric(mdf["lon"], errors="coerce")
    mdf = mdf.dropna(subset=["lat", "lon"])
    if mdf.empty:
        st.warning("No station locations found in this manifest.")
        raise _SkipTab()

    # Day selection from available timestamps
    day_min = mdf["event_ts_utc"].min().date()
    day_max = mdf["event_ts_utc"].max().date()
    c1, c2, c3 = st.columns([2, 2, 2])
    day = c1.date_input("Day", value=day_max, min_value=day_min, max_value=day_max)
    expected_mode_map = c2.selectbox("Expected schedule (map)", options=["24/7", "Business hours"], index=0)
    min_files_per_hour = c3.number_input(
        "Min files/hour to count as covered",
        min_value=1,
        max_value=1000,
        value=1,
        step=1,
        key="map_min_files_per_hour",
    )

    if expected_mode_map == "24/7":
        expected_dows = set(range(7))
        expected_hours = set(range(24))
    else:
        expected_dows = set(range(5))
        expected_hours = set(range(9, 18))

    # Build per-station hourly timeline for the selected day
    day_start = pd.Timestamp(day, tz="UTC")
    day_end = day_start + pd.Timedelta(hours=23)
    hours = pd.date_range(day_start, day_end, freq="h", tz="UTC")
    dow = int(day_start.dayofweek)
    expected_hours_today = sorted([h for h in range(24) if (dow in expected_dows and h in expected_hours)])

    # Count files per station per hour
    ddf = mdf[(mdf["event_hour_utc"] >= day_start) & (mdf["event_hour_utc"] <= day_end)].copy()
    counts = ddf.groupby([station_col, "event_hour_utc"], as_index=False).agg(file_count=("file_name", "count"))
    counts["covered"] = counts["file_count"] >= int(min_files_per_hour)

    # Compute coverage percent per station for expected hours
    # Start with all expected hours as missing, then mark covered where we have >= min_files_per_hour.
    expected_grid = pd.MultiIndex.from_product(
        [mdf[station_col].unique().tolist(), [day_start + pd.Timedelta(hours=h) for h in expected_hours_today]],
        names=[station_col, "event_hour_utc"],
    ).to_frame(index=False)
    merged = expected_grid.merge(counts[[station_col, "event_hour_utc", "covered"]], on=[station_col, "event_hour_utc"], how="left")
    merged["covered"] = merged["covered"].fillna(False)
    cov = merged.groupby(station_col, as_index=False).agg(coverage_pct=("covered", "mean"))
    cov["coverage_pct"] = cov["coverage_pct"] * 100.0

    # Attach one representative lat/lon per station
    loc = mdf.groupby(station_col, as_index=False).agg(lat=("lat", "median"), lon=("lon", "median"))
    station_size = mdf.groupby(station_col, as_index=False).agg(files=("file_name", "count"))
    plot_df = (
        cov.merge(loc, on=station_col, how="left")
        .merge(station_size, on=station_col, how="left")
        .dropna(subset=["lat", "lon"])
    )

    # Map usability controls (many stations -> avoid label clutter)
    f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
    station_query = f1.text_input("Filter stations (contains)", value="", key="map_station_query")
    max_points = int(f2.slider("Max stations to plot", min_value=50, max_value=2000, value=350, step=50, key="map_max_points"))
    show_labels = f3.checkbox("Show labels (worst coverage only)", value=False, key="map_show_labels")
    label_n = int(f4.slider("Labels (worst N)", min_value=5, max_value=80, value=20, step=5, key="map_label_n"))

    rmin, rmax = st.slider("Coverage range (%)", 0, 100, (0, 100), step=5, key="map_cov_range")

    if station_query.strip():
        plot_df = plot_df[plot_df[station_col].astype(str).str.contains(station_query.strip().lower())].copy()
    plot_df = plot_df[(plot_df["coverage_pct"] >= rmin) & (plot_df["coverage_pct"] <= rmax)].copy()

    # Keep plot responsive: prefer stations with more files (more representative)
    plot_df["files"] = plot_df["files"].fillna(0).astype(int)
    plot_df = plot_df.sort_values(["files", "coverage_pct"], ascending=[False, True]).head(max_points)

    center, zoom = _auto_center_zoom(plot_df["lat"], plot_df["lon"])
    fig, kind = _scatter_map_compat(
        data=plot_df,
        lat="lat",
        lon="lon",
        color="coverage_pct",
        color_continuous_scale="Viridis",
        range_color=(0, 100),
        hover_name=station_col,
        hover_data={"coverage_pct": ":.1f", "files": True, "lat": ":.4f", "lon": ":.4f"},
        height=650,
    )
    if kind == "map":
        fig.update_layout(map_style="open-street-map", map_center=center, map_zoom=zoom, margin=dict(l=0, r=0, t=0, b=0))
    else:
        fig.update_layout(mapbox_style="open-street-map", mapbox_center=center, mapbox_zoom=zoom, margin=dict(l=0, r=0, t=0, b=0))

    # Marker sizing: emphasize low-coverage stations.
    # Plotly < 6 uses trace type "scattermapbox"; Plotly >= 6 uses "scattermap".
    # Update both selectors so marker styling actually takes effect on either backend.
    for sel_type in ("scattermapbox", "scattermap"):
        try:
            fig.update_traces(marker=dict(size=9, opacity=0.85), selector=dict(type=sel_type))
        except Exception:
            pass
    if show_labels and not plot_df.empty:
        worst = plot_df.sort_values("coverage_pct", ascending=True).head(label_n)
        label_set = set(worst[station_col].astype(str).tolist())
        texts = [s if s in label_set else "" for s in plot_df[station_col].astype(str).tolist()]
        fig.update_traces(mode="markers+text", text=texts, textposition="top center")

    st.plotly_chart(fig, use_container_width=True)

