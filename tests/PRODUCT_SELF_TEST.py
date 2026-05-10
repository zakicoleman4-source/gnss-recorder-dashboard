from __future__ import annotations

"""
PRODUCT_SELF_TEST.py

Goal: ship-confidence checks that require ONLY replacing Python files on client PCs.

This is intentionally strict and tries to mimic common failure modes:
- nested dataset folder trees
- uppercase extensions (.TO2/.T02)
- 0-byte / unreadable files
- "probe" vs "full scan" behavior
- manifests export integrity

Run:
  python PRODUCT_SELF_TEST.py

Exit codes:
  0 = PASS
  1 = FAIL
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def die(msg: str, code: int = 1) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)
    raise SystemExit(code)


def ok(msg: str) -> None:
    print(f"[OK] {msg}")


def _must_have_snippets(py_src: str, snippets: list[str]) -> None:
    for s in snippets:
        if s not in py_src:
            die(f"Missing expected snippet in source:\n{s}")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))

    dash = root / "dashboard.py"
    pipe = root / "to2_pipeline.py"
    scan = root / "scan_gnss_folder.py"

    if not dash.exists():
        die(f"Missing {dash}")
    if not pipe.exists():
        die(f"Missing {pipe}")
    if not scan.exists():
        die(f"Missing {scan}")

    dash_src = dash.read_text(encoding="utf-8", errors="ignore")
    pipe_src = pipe.read_text(encoding="utf-8", errors="ignore")

    # Static sanity: critical robustness markers present (cheap but catches regressions).
    _must_have_snippets(
        dash_src,
        [
            'sys.path.insert(0, str(_THIS_DIR))',
            "def _scatter_map_compat",
            # Tabs are now wrapped in _safe_tab so a per-tab error never blanks the app.
            'with _safe_tab("Station", tab_station):',
            'with _safe_tab("Map", tab_map):',
            'with _safe_tab("VRS", tab_vrs):',
            'class _SkipTab(Exception)',
        ],
    )
    _must_have_snippets(
        pipe_src,
        [
            # Pipeline robustness markers:
            "except Exception as e:",
            'convert_detail = f"{type(e).__name__}: {e}"',
            "empty or unreadable file",
            # New: pipeline must skip its own cache when scanning.
            "exclude_dirs",
            # New: ConverterError surfaces actionable detail to the manifest.
            "class ConverterError",
        ],
    )

    ok("Static markers present (dashboard/to2_pipeline)")

    # Import pipeline module without launching Streamlit UI.
    try:
        import to2_pipeline  # noqa: WPS433 (runtime import for smoke test)
    except Exception as e:
        die(f"Failed importing to2_pipeline: {e}")

    ok("Import to2_pipeline OK")

    # Create synthetic dataset: nested dirs, uppercase exts, 0-byte file, weird names.
    tmp = Path(tempfile.mkdtemp(prefix="gnss_prod_selftest_")).resolve()
    data_root = (tmp / "data" / "2026").resolve()
    (data_root / "001").mkdir(parents=True, exist_ok=True)
    (data_root / "002").mkdir(parents=True, exist_ok=True)

    # Two stations with multiple files each
    (data_root / "001" / "ABCD202601010000.TO2").write_bytes(b"\x00" * 256)
    (data_root / "001" / "ABCD202601010100.TO2").write_bytes(b"\x00" * 128)
    (data_root / "002" / "WXYZ202601020000.TO2").write_bytes(b"\x00" * 512)
    (data_root / "002" / "WXYZ202601020100.TO2").write_bytes(b"\x00" * 64)
    # 0-byte file (should not crash, should be recorded)
    (data_root / "002" / "EMPTY202601020200.TO2").write_bytes(b"")
    # No leading letters -> UNKNOWN station
    (data_root / "002" / "1234202601020300.TO2").write_bytes(b"\x00" * 16)

    # ---- Pipeline: FULL scan (no converters) must not crash and must record all files
    cache_full = tmp / "cache_full"
    cfg_full = to2_pipeline.PipelineConfig(
        data_root=data_root,
        cache_dir=cache_full,
        convbin_path=None,
        runpkr00_path=None,
        teqc_path=None,
        max_files_per_station=None,
    )

    db_full = to2_pipeline.run_pipeline(cfg_full)
    if not db_full.exists():
        die(f"Expected sqlite db at {db_full}")

    con = sqlite3.connect(db_full)
    rows = con.execute("select path,size_bytes,convert_status,convert_detail from files order by path").fetchall()
    con.close()

    # Expect all 6 files present in DB
    if len(rows) != 6:
        die(f"Expected 6 rows in DB, got {len(rows)}")
    # Expect the empty file to be marked skipped with detail
    empty_rows = [r for r in rows if r[0].lower().endswith("empty202601020200.to2")]
    if not empty_rows:
        die("Expected EMPTY*.TO2 row present")
    if empty_rows[0][2] not in ("skipped", "failed"):
        die(f"Expected empty file status skipped/failed, got {empty_rows[0]}")

    ok(f"Pipeline FULL scan processed rows={len(rows)}")

    # ---- Pipeline: PROBE scan must cap to 1 file per station (including UNKNOWN)
    cache_probe = tmp / "cache_probe"
    cfg_probe = to2_pipeline.PipelineConfig(
        data_root=data_root,
        cache_dir=cache_probe,
        convbin_path=None,
        runpkr00_path=None,
        teqc_path=None,
        max_files_per_station=1,
        probe_max_total_files=10,
    )
    db_probe = to2_pipeline.run_pipeline(cfg_probe)
    con = sqlite3.connect(db_probe)
    stations = con.execute("select station, count(*) from files group by station").fetchall()
    con.close()
    # At most 1 per station because we only pick 1 per station in probe input set
    if any(c > 1 for _s, c in stations):
        die(f"Probe mode expected <=1 row per station, got: {stations}")
    ok(f"Pipeline PROBE scan stations={len(stations)}")

    # ---- Export manifests integrity (full)
    manifests_dir = to2_pipeline.export_manifests(db_full, out_dir=(cache_full / "exported"))
    mf_csv = manifests_dir / "files_manifest.csv"
    mf_sum = manifests_dir / "summary.json"
    if not mf_csv.exists() or not mf_sum.exists():
        die("export_manifests did not create expected outputs")
    summary = json.loads(mf_sum.read_text(encoding="utf-8"))
    if summary.get("files_in_manifest") != 6:
        die(f"Expected summary files_in_manifest=6, got {summary.get('files_in_manifest')}")
    ok("export_manifests created files_manifest.csv + summary.json")

    # Optional: if sample exists on developer machine, ensure scanner doesn't crash.
    sample_root = root / "geonet_sample"
    sample_manifests = root / "geonet_sample_scanned" / "_manifests"
    if sample_root.exists():
        cmd = [sys.executable, str(scan), str(sample_root), "--skip_runpkr00", "--include_ext", ".t02"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            die(f"scan_gnss_folder failed:\n{p.stdout}\n{p.stderr}")
        ok("scan_gnss_folder ran on geonet_sample")

        if (sample_manifests / "files_manifest.csv").exists():
            ok(f"Manifest csv exists: {sample_manifests / 'files_manifest.csv'}")
    else:
        ok("geonet_sample not present; skipping scanner integration check")

    # ---- scan_gnss_folder integration on synthetic dataset (should include caps .TO2 when include_ext=.to2)
    out_dir = tmp / "scan_out"
    cmd2 = [
        sys.executable,
        str(scan),
        str(data_root),
        "--out_dir",
        str(out_dir),
        "--include_ext",
        ".to2",
        "--skip_runpkr00",
    ]
    p2 = subprocess.run(cmd2, capture_output=True, text=True)
    if p2.returncode != 0:
        die(f"scan_gnss_folder failed on synthetic dataset:\n{p2.stdout}\n{p2.stderr}")
    out_m = out_dir / "_manifests" / "summary.json"
    if not out_m.exists():
        die("scan_gnss_folder did not write summary.json for synthetic dataset")
    sm2 = json.loads(out_m.read_text(encoding="utf-8"))
    if sm2.get("files_in_manifest") != 6:
        die(f"scan_gnss_folder expected 6 files, got {sm2.get('files_in_manifest')}")
    ok("scan_gnss_folder handled caps .TO2 + empty file")

    # ---- Converter failure path (must not crash; should mark failed with detail)
    cache_fail = tmp / "cache_fail"
    # A command that always fails on Windows cmd
    cfg_fail = to2_pipeline.PipelineConfig(
        data_root=data_root,
        cache_dir=cache_fail,
        convert_cmd_template='cmd /c exit 1 "{input}" "{out_dir}"',
        max_files_per_station=1,
    )
    db_fail = to2_pipeline.run_pipeline(cfg_fail)
    con = sqlite3.connect(db_fail)
    bad = con.execute("select count(*) from files where convert_status='failed'").fetchone()[0]
    con.close()
    if bad <= 0:
        die("Expected some failed conversions when GNSS_CONVERT_CMD always fails")
    ok("Converter-failure path recorded failures without crashing")

    # ---- Path weirdness: spaces + unicode in filenames and directories
    weird_root = (tmp / "data weird Ω" / "2026" / "001").resolve()
    weird_root.mkdir(parents=True, exist_ok=True)
    (weird_root / "ABCD202601010000.TO2").write_bytes(b"\x00" * 16)
    (weird_root / "ABCD 202601010100.TO2").write_bytes(b"\x00" * 16)
    (weird_root / "ÄBCD202601010200.TO2").write_bytes(b"\x00" * 16)
    cache_weird = tmp / "cache_weird"
    cfg_weird = to2_pipeline.PipelineConfig(data_root=weird_root.parent, cache_dir=cache_weird, max_files_per_station=None)
    db_weird = to2_pipeline.run_pipeline(cfg_weird)
    if not db_weird.exists():
        die("Expected db for weird path dataset")
    ok("Weird path dataset scanned without crashing")

    # ---- ECEF degenerate inputs must not raise (crashed a real client scan).
    bad_inputs = [
        (0.0, 0.0, 0.0),
        (float("nan"), 1.0, 2.0),
        (float("inf"), 0.0, 0.0),
        (1e9, 1e9, 1e9),  # absurd radius -> caller should reject upstream
    ]
    for x, y, z in bad_inputs:
        try:
            _ = to2_pipeline._ecef_to_llh_wgs84(x, y, z)
        except Exception as e:
            die(f"_ecef_to_llh_wgs84 raised on degenerate input ({x},{y},{z}): {type(e).__name__}: {e}")
    ok("ECEF degenerate inputs handled without crashing (to2_pipeline)")

    import scan_gnss_folder  # noqa: WPS433
    for x, y, z in bad_inputs:
        try:
            _ = scan_gnss_folder._ecef_to_llh_wgs84(x, y, z)
        except Exception as e:
            die(f"scan_gnss_folder._ecef_to_llh_wgs84 raised on ({x},{y},{z}): {type(e).__name__}: {e}")
    ok("ECEF degenerate inputs handled without crashing (scan_gnss_folder)")

    # ---- _iter_to_files must skip the cache_dir even when nested inside data_root.
    nested_root = (tmp / "nested" / "data").resolve()
    (nested_root / "001").mkdir(parents=True, exist_ok=True)
    (nested_root / "001" / "ABCD202601010000.TO2").write_bytes(b"\x00" * 32)
    nested_cache = nested_root / "_cache_inside"
    nested_cache.mkdir(parents=True, exist_ok=True)
    # Drop a fake .to2 inside the cache dir; it must NOT be picked up.
    (nested_cache / "BOGUS_should_not_appear.to2").write_bytes(b"\x00" * 8)
    cfg_nested = to2_pipeline.PipelineConfig(data_root=nested_root, cache_dir=nested_cache, max_files_per_station=None)
    db_nested = to2_pipeline.run_pipeline(cfg_nested)
    con = sqlite3.connect(db_nested)
    paths = [r[0] for r in con.execute("select path from files").fetchall()]
    con.close()
    if any("BOGUS_should_not_appear" in p for p in paths):
        die("Pipeline picked up a file from inside its own cache directory")
    if not any("ABCD202601010000.TO2" in p for p in paths):
        die("Pipeline missed the legitimate file when cache lives under data_root")
    ok("Pipeline excludes its own cache subtree under data_root")

    # ---- RINEX header parsing: time + xyz must be tolerant of fixed-column variants.
    sample_lines = [
        "  2026     3     1     0     0    0.0000000     GPS         TIME OF FIRST OBS",
        "  -4744528.4710  2796212.5030 -3393068.4220                  APPROX POSITION XYZ",
        "G   12 C1C L1C D1C S1C C2W L2W D2W S2W C5Q L5Q D5Q S5Q       SYS / # / OBS TYPES",
        "                                                            END OF HEADER",
    ]
    ts = to2_pipeline._parse_rinex_time(sample_lines[0])
    if ts is None or ts.year != 2026:
        die(f"_parse_rinex_time failed on a normal line: got {ts}")
    xyz = to2_pipeline._parse_rinex_position_xyz(sample_lines[1])
    if xyz is None or abs(xyz[0] + 4744528.471) > 1e-3:
        die(f"_parse_rinex_position_xyz failed on a normal line: got {xyz}")
    cs, sigs = to2_pipeline._parse_rinex_signals(sample_lines)
    if cs != "G" or "C1C" not in (sigs or ""):
        die(f"_parse_rinex_signals failed: cs={cs!r} sigs={sigs!r}")
    # Junk inputs must NOT raise.
    if to2_pipeline._parse_rinex_time("garbage") is not None:
        die("_parse_rinex_time should have returned None on garbage")
    if to2_pipeline._parse_rinex_position_xyz("        0.0000        0.0000        0.0000   APPROX POSITION XYZ") is not None:
        die("_parse_rinex_position_xyz should reject all-zero")
    ok("RINEX header parsing: well-formed and junk inputs both handled")

    # ---- PipelineConfig defaults: stop_after_success_per_station MUST be False.
    cfg_defaults = to2_pipeline.PipelineConfig(data_root=tmp, cache_dir=(tmp / "cdef"))
    if cfg_defaults.stop_after_success_per_station is not False:
        die("PipelineConfig.stop_after_success_per_station default must be False (was True -> silent FULL-scan truncation).")
    ok("PipelineConfig defaults are FULL-scan friendly")

    # ---- export_manifests must derive `ext` from the FILE NAME, not the first
    # dot in the full path. Earlier code used `substr(path, instr(path, '.'))`
    # which produced ".bar\baz\station.t02" for paths under e.g. "C:\my.folder\".
    dotty_root = (tmp / "my.folder.with.dots" / "data" / "001").resolve()
    dotty_root.mkdir(parents=True, exist_ok=True)
    (dotty_root / "ABCD202601010000.TO2").write_bytes(b"\x00" * 64)
    cache_dotty = tmp / "cache_dotty"
    cfg_dotty = to2_pipeline.PipelineConfig(data_root=dotty_root.parent.parent, cache_dir=cache_dotty, max_files_per_station=None)
    db_dotty = to2_pipeline.run_pipeline(cfg_dotty)
    manifests_dotty = to2_pipeline.export_manifests(db_dotty, out_dir=(cache_dotty / "exported"))
    import csv as _csv
    with (manifests_dotty / "files_manifest.csv").open("r", encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            ext = (row.get("ext") or "").lower()
            if ext != ".to2":
                die(f"export_manifests produced bogus ext={ext!r} for path with dots in dirs (regression of substr-based bug)")
    ok("export_manifests derives ext from file_name (no dotty-dir regression)")

    # ---- export_manifests on an empty DB must not crash and must still produce
    # the expected files with all columns.
    empty_root = tmp / "empty_root"
    empty_root.mkdir(exist_ok=True)
    empty_cache = tmp / "empty_cache"
    cfg_empty = to2_pipeline.PipelineConfig(data_root=empty_root, cache_dir=empty_cache)
    db_empty = to2_pipeline.run_pipeline(cfg_empty)
    manifests_empty = to2_pipeline.export_manifests(db_empty, out_dir=(empty_cache / "exported"))
    if not (manifests_empty / "files_manifest.csv").exists():
        die("export_manifests on empty DB did not write files_manifest.csv")
    if not (manifests_empty / "summary.json").exists():
        die("export_manifests on empty DB did not write summary.json")
    ok("export_manifests handles empty DB without KeyError")

    # ---- Safe zip extraction must reject Zip Slip / absolute paths and
    # accept legitimate members.
    # Importing dashboard runs its top-level code (sidebar, etc.). Make sure we
    # point the data/manifest paths at non-existent dirs first so we don't kick
    # off a multi-minute filesystem walk in bare mode (where st.stop is a no-op).
    import importlib as _il
    _safe_env_dir = Path(tempfile.mkdtemp(prefix="gnss_safe_env_"))
    os.environ["GNSS_DATA_ROOT"] = str(_safe_env_dir / "no_data_here")
    os.environ["GNSS_MANIFESTS_DIR"] = str(_safe_env_dir / "no_manifests_here")
    os.environ["GNSS_OFFLINE"] = "1"
    try:
        dash_mod = _il.import_module("dashboard")
    finally:
        for _k in ("GNSS_DATA_ROOT", "GNSS_MANIFESTS_DIR", "GNSS_OFFLINE"):
            os.environ.pop(_k, None)
        shutil.rmtree(_safe_env_dir, ignore_errors=True)
    safe_dir = Path(tempfile.mkdtemp(prefix="gnss_safezip_"))
    bad_zip = safe_dir / "bad.zip"
    import zipfile as _zf
    with _zf.ZipFile(bad_zip, "w") as z:
        z.writestr("ok.txt", "hello")
        # ../escape attempt
        z.writestr("..\\..\\escape.txt", "pwned")
        z.writestr("../escape2.txt", "pwned2")
        # absolute path attempt
        z.writestr("/abs.txt", "pwned3")
    out_dir = safe_dir / "extracted"
    out_dir.mkdir()
    dash_mod._safe_extract_zip(bad_zip, out_dir)
    written = sorted(p.name for p in out_dir.rglob("*") if p.is_file())
    if written != ["ok.txt"]:
        die(f"_safe_extract_zip allowed unsafe members through: {written}")
    # Confirm no escapees landed next to safe_dir
    escapees = [
        safe_dir / "escape.txt",
        safe_dir / "escape2.txt",
        Path("/abs.txt"),
        safe_dir.parent / "escape.txt",
    ]
    for p in escapees:
        if p.exists():
            die(f"_safe_extract_zip ALLOWED traversal escape: {p}")
    shutil.rmtree(safe_dir, ignore_errors=True)
    ok("_safe_extract_zip rejects Zip Slip + absolute paths")

    # ---- Streaming download cap: fake a >cap response and ensure we abort.
    safe_dir = Path(tempfile.mkdtemp(prefix="gnss_dlcap_"))
    big_path = safe_dir / "big.bin"
    class _FakeResp:
        headers = {"Content-Length": str(dash_mod._MAX_DOWNLOAD_BYTES + 1)}
        status_code = 200
        def raise_for_status(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def iter_content(self, chunk_size): yield b"x" * chunk_size
    import requests as _rq
    _orig = _rq.get
    _rq.get = lambda *a, **kw: _FakeResp()
    try:
        try:
            dash_mod._stream_download("http://fake/example.zip", big_path)
        except ValueError:
            pass  # expected
        else:
            die("_stream_download did not enforce Content-Length cap")
    finally:
        _rq.get = _orig
    shutil.rmtree(safe_dir, ignore_errors=True)
    ok("_stream_download enforces size cap")

    # ---- Overview-grid cap: GRID_MAX_CELLS guard exists.
    dash_text = (root /"dashboard.py").read_text(encoding="utf-8", errors="ignore")
    if "GRID_MAX_CELLS" not in dash_text:
        die("Overview tab is missing GRID_MAX_CELLS cap (memory bomb risk).")
    ok("Overview station grid is capped (GRID_MAX_CELLS present)")

    # ---- force_rescan must also clean -wal/-shm sidecars.
    if "scan_cache.sqlite-wal" not in dash_text or "scan_cache.sqlite-shm" not in dash_text:
        die("force_rescan does not sweep WAL/SHM sidecars (stale snapshot risk).")
    ok("force_rescan cleans WAL/SHM sidecars")

    # ---- Subprocess defaults: stdin=DEVNULL + (Windows) CREATE_NO_WINDOW.
    pipe_text = (root /"to2_pipeline.py").read_text(encoding="utf-8", errors="ignore")
    if "_SUBPROC_KW" not in pipe_text or "stdin" not in pipe_text:
        die("to2_pipeline subprocess calls do not use _SUBPROC_KW (hang / window-flicker risk).")
    ok("Converter subprocess calls use safe defaults (stdin closed, no console window)")

    # ---- Station tab must not silently empty when CSV mixes int/float station IDs:
    # `3563` vs `3563.0` breaks df == selectbox joins. Also reset stale
    # `station_select_value` when options change (Streamlit ignores `index=`).
    dash_src = (root /"dashboard.py").read_text(
        encoding="utf-8", errors="ignore"
    )
    if "def _normalize_station_id_series" not in dash_src:
        die("dashboard.py missing _normalize_station_id_series (numeric-prefix regression).")
    if "prior not in opts" not in dash_src or 'st.session_state["station_select_value"]' not in dash_src:
        die("dashboard.py missing stale Streamlit station_select_value reset (prior not in opts).")
    ok("Dashboard station-ID normalization + stale selectbox-state guard present")

    if "_gnss_last_scan_manifests_dir" not in dash_src or "_gnss_last_scan_cache_key" not in dash_src:
        die(
            "dashboard.py missing scan-folder session persistence — "
            "main body would never load after Streamlit reruns (app looks blank after scan)."
        )
    if "def _scan_folder_session_key" not in dash_src or "def _normalize_saved_scan_cache_key" not in dash_src:
        die("dashboard.py missing scan-folder cache key normalization (Windows path resume bug).")
    ok("Scan-folder mode persists last manifests path across reruns (streamlit stop fix)")

    if "_gnss_last_zip_url" not in dash_src or "_gnss_last_zip_manifests_dir" not in dash_src:
        die(
            "dashboard.py missing URL-manifest session persistence — "
            "every widget rerun re-downloads the zip or blanks the app when the URL field is empty."
        )
    ok("URL zip mode caches last extract + survives empty URL field across reruns")

    if "_gnss_upload_sig" not in dash_src or "_gnss_last_upload_manifests_dir" not in dash_src:
        die(
            "dashboard.py missing upload-manifest session persistence — "
            "every rerun re-extracts the zip or blanks the app when the uploader is empty."
        )
    ok("Upload zip mode caches extract + survives cleared uploader across reruns")

    # ---- OFFLINE_SELF_TEST.py must reference the CURRENT dashboard markers
    # (we rewrote them for `_safe_tab` and the previous static checks would
    # FAIL on a healthy install). This test guards against future drift where
    # one self-test diverges from the other.
    offline_test = (root /"offline_installer" / "OFFLINE_SELF_TEST.py")
    if not offline_test.exists():
        die(f"Missing offline self-test at {offline_test}")
    offline_text = offline_test.read_text(encoding="utf-8", errors="ignore")
    must_contain = [
        '_safe_tab("Map", tab_map)',
        '_safe_tab("VRS", tab_vrs)',
        "_safe_extract_zip",
        "_stream_download",
    ]
    for m in must_contain:
        if m not in offline_text:
            die(f"OFFLINE_SELF_TEST.py is out of date -- missing marker check for: {m}")
    must_not_contain = [
        '"with tab_map:"',
        '"with tab_vrs:"',
    ]
    for m in must_not_contain:
        if m in offline_text:
            die(f"OFFLINE_SELF_TEST.py still checks stale marker: {m} (would FAIL on healthy install)")
    ok("OFFLINE_SELF_TEST.py uses current dashboard markers (no stale checks)")

    # ---- INSTALL_OFFLINE.bat must guard against the Microsoft Store stub /
    # missing python.exe -- otherwise we silently produce a confusing
    # 'wheelhouse missing' error several screens later.
    install_bat = (root /"offline_installer" / "INSTALL_OFFLINE.bat")
    if install_bat.exists():
        bat_text = install_bat.read_text(encoding="utf-8", errors="ignore")
        if "python --version" not in bat_text or "Microsoft Store stub" not in bat_text:
            die("INSTALL_OFFLINE.bat is missing the python-on-PATH guard (Microsoft Store stub regression).")
        ok("INSTALL_OFFLINE.bat guards against missing python / Store stub")

    # ---- RUN_DASHBOARD_OFFLINE.bat must suppress Streamlit's first-run email
    # prompt -- otherwise the dashboard pauses on launch waiting for input
    # the operator may not see.
    run_bat = (root /"offline_installer" / "RUN_DASHBOARD_OFFLINE.bat")
    if run_bat.exists():
        run_text = run_bat.read_text(encoding="utf-8", errors="ignore")
        if "STREAMLIT_BROWSER_GATHER_USAGE_STATS" not in run_text or "credentials.toml" not in run_text:
            die("RUN_DASHBOARD_OFFLINE.bat does not suppress the Streamlit email-collection welcome prompt.")
        ok("RUN_DASHBOARD_OFFLINE.bat skips the Streamlit welcome prompt")

    # ---- _dbg writes to a deterministic log dir (next to the script), not cwd.
    import importlib as _il
    pipe_mod = _il.import_module("to2_pipeline")
    log_dir = pipe_mod._debug_log_dir()
    if log_dir != Path(pipe_mod.__file__).resolve().parent:
        # Allow override-via-env to point elsewhere, but make sure default lands next to script.
        if not os.environ.get("GNSS_DEBUG_DIR"):
            die(f"_debug_log_dir default should be next to to2_pipeline.py; got {log_dir}")
    ok("_dbg log directory is deterministic (next to script)")

    # ---- Dashboard imports cleanly (catches syntax errors without launching Streamlit).
    # In bare mode (no `streamlit run`), st.stop() is a silent no-op, so we have to
    # be very careful that the dashboard doesn't accidentally run something heavy
    # (e.g. walk the entire workspace looking for .to2 files). We point all data
    # paths at a freshly-created throw-away dir so the dashboard bails out early.
    safe_dir = Path(tempfile.mkdtemp(prefix="gnss_dash_smoke_"))
    safe_data_root = safe_dir / "no_data_here"  # intentionally non-existent
    os.environ["GNSS_DATA_ROOT"] = str(safe_data_root)
    os.environ["GNSS_MANIFESTS_DIR"] = str(safe_dir / "no_manifests_here")
    os.environ["GNSS_OFFLINE"] = "1"
    try:
        import importlib
        importlib.import_module("dashboard")
    except SystemExit:
        # st.stop() under streamlit-run raises a control-flow exception we treat as PASS.
        pass
    except FileNotFoundError:
        # Bare-mode runs past st.stop() and tries to load the manifest. Acceptable.
        pass
    except Exception as e:
        die(f"dashboard.py failed to import: {type(e).__name__}: {e}")
    finally:
        for k in ("GNSS_DATA_ROOT", "GNSS_MANIFESTS_DIR", "GNSS_OFFLINE"):
            os.environ.pop(k, None)
        shutil.rmtree(safe_dir, ignore_errors=True)
    ok("dashboard.py imports without syntax/runtime errors")

    ok("PRODUCT_SELF_TEST complete: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
