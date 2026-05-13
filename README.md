# GNSS Recorder Dashboard

Offline Windows dashboard for scanning Trimble GNSS recordings (`.T02` / `.T04` / `.T01` / `.T00`) and visualising station coverage, sample rates, and gaps.

## Install

1. Download or clone this repository to any folder on Windows.
2. Double-click `INSTALL.bat`. It checks for Python 3.8+, creates `.venv\` inside the project folder, and installs dependencies.
3. Double-click `RUN_DASHBOARD.bat`. It launches Streamlit on the first free port between 8501–8520.
4. The browser opens at `http://localhost:850X`. Paste the data folder path into the sidebar and click **Scan now**.

## Requirements

- Windows 10 / 11
- Python 3.8 – 3.13 (download from <https://www.python.org/downloads/windows/>, tick **Add python.exe to PATH** during install)
- Internet for the first run only (`INSTALL.bat` downloads packages via pip)

## What gets scanned

The pipeline reads each `.T02` / `.T04` file's bzip2-embedded metadata block to extract:

- Station / marker name
- Receiver coordinates (lat / lon / height)
- Session date and hour
- Sample interval (`SessionMeasIntervalMsecs`)

It then attempts to convert the binary to RINEX 3 using bundled `runpkr00.exe` (Trimble unpacker) → `convbin.exe` (RTKLIB). When the conversion succeeds, the dashboard also shows per-file completeness, epoch counts, intra-file gaps, and the constellation / signal list.

## Modern Trimble Alloy receivers (RT27)

Modern Trimble Alloy receivers record measurement data in the **RT27** format (extended RT17 with multi-constellation records). RTKLIB `convbin` only decodes the older RT17 format and cannot process RT27.

When `tools\convert_to_rinex\convertToRinex_cli.exe` is present, the pipeline routes RT27 files through it automatically and produces full RINEX 3.04 output (GPS, GLONASS, Galileo, BeiDou, QZSS). RT17 files continue to use `runpkr00` + `convbin` as before.

The **CTR-first mode** checkbox (sidebar) skips the `runpkr00` attempt entirely — use this when your dataset is known to be all-Alloy (saves ~1 min per 1,000 files).

## Bundled tools (`tools/`)

- `tools\runpkr00\runpkr00.exe` — Trimble T02 / T04 -> DAT unpacker (RT17)
- `tools\rtklib\convbin.exe` — RTKLIB DAT -> RINEX 3 converter (RT17)
- `tools\rtklib\rnx2rtkp.exe` — RTKLIB single-point position solver (coord fallback when T02 header has no `RefStationLLH`)
- `tools\convert_to_rinex\convertToRinex_cli.exe` — Trimble RT27 / Alloy -> RINEX 3.04 converter (CLI build)

## Repository layout

- `dashboard.py` — Streamlit UI (the entry point)
- `to2_pipeline.py` — scan + probe + convert + manifest export
- `probe_t02_files.py` — standalone T02 inventory tool
- `analyze_station_manifest.py` — per-station manifest analyzer
- `requirements.txt` — `pandas`, `numpy`, `plotly`, `streamlit`, `requests`
- `INSTALL.bat` / `RUN_DASHBOARD.bat` / `PROBE_FILES.bat` — Windows-friendly install + launch
