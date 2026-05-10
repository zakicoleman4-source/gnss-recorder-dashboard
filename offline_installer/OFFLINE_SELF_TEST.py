from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def die(msg: str, code: int = 1) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)
    raise SystemExit(code)


def ok(msg: str) -> None:
    print(f"[OK] {msg}")


# Pinned versions we ship with -- if the client somehow has a different
# version installed (manual pip install, system Python instead of the
# bundled venv, etc.), the dashboard's behaviour is undefined. Catch it here
# rather than at runtime.
_EXPECTED_VERSIONS = {
    "pandas": "2.2.3",
    "numpy": "1.26.4",
    "plotly": "5.24.1",
    "streamlit": "1.39.0",
    "requests": "2.32.3",
}


def _check_versions() -> None:
    import importlib
    skewed: list[str] = []
    for pkg, want in _EXPECTED_VERSIONS.items():
        try:
            mod = importlib.import_module(pkg)
            got = getattr(mod, "__version__", "?")
        except Exception as e:
            skewed.append(f"{pkg}: import failed ({e})")
            continue
        if got != want:
            skewed.append(f"{pkg}: have {got}, expected {want}")
    if skewed:
        # WARN, don't die. Major version skew often still works; we surface it
        # so the operator knows what to investigate first if the dashboard
        # misbehaves.
        for line in skewed:
            print(f"[WARN] version skew -> {line}")
    else:
        ok(f"Pinned versions match ({len(_EXPECTED_VERSIONS)} packages)")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    # Ensure project root is importable (so we can import to2_pipeline, scanner, etc.)
    sys.path.insert(0, str(root))
    dash = root / "dashboard.py"
    scan = root / "scan_gnss_folder.py"
    sample_root = root / "geonet_sample"
    sample_manifests = root / "geonet_sample_scanned" / "_manifests"
    manifest_csv = sample_manifests / "files_manifest.csv"
    summary_json = sample_manifests / "summary.json"

    if not dash.exists():
        die(f"Missing dashboard.py at {dash}")
    if not scan.exists():
        die(f"Missing scan_gnss_folder.py at {scan}")

    # Confirm we're being run from a venv -- catches "I ran the script with
    # system Python by mistake" before it produces confusing import errors.
    in_venv = (sys.prefix != getattr(sys, "base_prefix", sys.prefix)) or bool(os.environ.get("VIRTUAL_ENV"))
    if not in_venv:
        print(
            "[WARN] Running outside a virtual environment. The bundled venv lives at .venv_offline. "
            "If this self-test fails, run the dashboard via RUN_DASHBOARD_OFFLINE.bat.",
        )

    # 1) Import core deps (ensures wheelhouse install is sufficient)
    try:
        import pandas as pd  # noqa: F401
        import plotly.express as px  # noqa: F401
        import plotly.graph_objects as go  # noqa: F401
        import streamlit as st  # noqa: F401
        import numpy as np  # noqa: F401
        import requests  # noqa: F401
    except Exception as e:
        die(f"Dependency import failed: {e}")
    ok("Imports: pandas/plotly/streamlit/numpy/requests")

    # Versions sanity check (warns, doesn't fail).
    _check_versions()

    # 2) Static feature presence checks (guard against accidental feature
    # removal). Markers updated to match the post-refactor dashboard.py
    # (`with _safe_tab(...)` instead of bare `with tab_xxx:`). The previous
    # marker set checked for `with tab_map:` strings that no longer exist,
    # which made this self-test FAIL on a healthy install -- we removed those
    # checks during the safe-tab refactor.
    src = dash.read_text(encoding="utf-8", errors="ignore")
    required_markers = [
        "st.tabs([\"Overview\", \"Station coverage\", \"Map\", \"VRS\", \"Raw data\"]",
        '_safe_tab("Map", tab_map)',
        '_safe_tab("VRS", tab_vrs)',
        '_safe_tab("Station", tab_station)',
        '_safe_tab("Overview", tab_overview)',
        "Download manifests zip",
        "Download SQLite DB (from manifest)",
        # Anti-regression markers for fixes shipped with this build:
        "_safe_extract_zip",        # zip-slip defense
        "_stream_download",         # download streaming + size cap
        "GRID_MAX_CELLS",           # overview-tab memory cap
        "scan_cache.sqlite-wal",    # force_rescan WAL sweep
    ]
    for m in required_markers:
        if m not in src:
            die(f"Dashboard feature/safety marker missing: {m}")
    ok("Dashboard feature + safety markers present")

    # 3) Run a scan on sample data (offline-safe) to produce manifests (if sample exists)
    if sample_root.exists():
        # Invoke scanner as a module to avoid shell dependency. Use the same
        # subprocess hardening we apply to converter calls so we don't pop a
        # console window on Windows or hang on a stdin prompt.
        import subprocess
        sub_kw: dict = {"stdin": subprocess.DEVNULL}
        if os.name == "nt":
            sub_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        cmd = [
            sys.executable,
            str(scan),
            str(sample_root),
            "--include_ext",
            ".t02",
            "--skip_runpkr00",
        ]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300, **sub_kw)
        if p.returncode != 0:
            die(f"Sample scan failed:\n{p.stdout}\n{p.stderr}")
        ok("Scanner ran on sample dataset")
        if not manifest_csv.exists() or not summary_json.exists():
            die(f"Expected manifests not found at {sample_manifests}")
        ok("Manifests present (files_manifest.csv + summary.json)")
    else:
        ok("Sample dataset not shipped; skipping scan+manifest checks")
        # Still validate that the TO2 pipeline module exists (feature presence)
        try:
            import to2_pipeline  # noqa: F401
        except Exception as e:
            die(f"to2_pipeline import failed: {e}")
        ok("to2_pipeline module import OK")
        ok("OFFLINE_SELF_TEST complete: PASS")
        return 0

    # 4) Load manifests and exercise key compute paths (without opening UI)
    import pandas as pd
    import plotly.express as px

    df = pd.read_csv(manifest_csv)
    if df.empty:
        die("Manifest CSV loaded empty")
    ok(f"Loaded manifest rows: {len(df)}")

    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    if "unique_prefixes" not in summary:
        die("summary.json missing unique_prefixes")
    ok(f"Summary unique_prefixes: {summary['unique_prefixes']}")

    # Ensure station naming exists
    if "station" not in df.columns:
        die("Manifest missing station column")
    ok("Manifest contains station column")

    # Create synthetic lat/lon so Map/VRS code paths can be exercised offline
    stations = sorted(df["station"].astype(str).str.lower().unique().tolist())
    if not stations:
        die("No stations found")
    st_to_latlon = {s: (-41.0 + (i % 20) * 0.1, 173.0 + (i % 20) * 0.1) for i, s in enumerate(stations)}
    df["lat"] = df["station"].map(lambda s: st_to_latlon[str(s).lower()][0])
    df["lon"] = df["station"].map(lambda s: st_to_latlon[str(s).lower()][1])

    # Build a minimal "coverage by day" like the dashboard does
    df["modified_utc"] = pd.to_datetime(df.get("modified_utc"), utc=True, errors="coerce")
    df["event_ts_utc"] = df["modified_utc"].fillna(pd.Timestamp.now(tz="UTC"))
    df["event_hour_utc"] = df["event_ts_utc"].dt.floor("h")
    day = df["event_ts_utc"].max().date()
    day_start = pd.Timestamp(day, tz="UTC")
    day_end = day_start + pd.Timedelta(hours=23)

    ddf = df[(df["event_hour_utc"] >= day_start) & (df["event_hour_utc"] <= day_end)].copy()
    counts = ddf.groupby(["station", "event_hour_utc"], as_index=False).size().rename(columns={"size": "file_count"})
    counts["covered"] = counts["file_count"] >= 1
    cov = counts.groupby("station", as_index=False).agg(coverage_pct=("covered", "mean"))
    cov["coverage_pct"] = cov["coverage_pct"] * 100.0
    loc = df.groupby("station", as_index=False).agg(lat=("lat", "median"), lon=("lon", "median"))
    plot_df = cov.merge(loc, on="station", how="left").dropna()

    # Plotly compatibility: prefer scatter_map (Plotly>=6), fall back to scatter_mapbox.
    if hasattr(px, "scatter_map"):
        fig = px.scatter_map(
            plot_df,
            lat="lat",
            lon="lon",
            color="coverage_pct",
            color_continuous_scale="Viridis",
            range_color=(0, 100),
            hover_name="station",
            height=400,
        )
    else:
        fig = px.scatter_mapbox(
            plot_df,
            lat="lat",
            lon="lon",
            color="coverage_pct",
            color_continuous_scale="Viridis",
            range_color=(0, 100),
            hover_name="station",
            height=400,
        )
    # Just ensure figure object builds
    _ = fig.to_dict()
    ok("Map figure generation path OK (synthetic coords)")

    ok("OFFLINE_SELF_TEST complete: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

