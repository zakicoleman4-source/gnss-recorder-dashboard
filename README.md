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

## Modern Trimble Alloy receivers (RT27 limitation)

Modern Trimble Alloy receivers record measurement data in the **RT27** format (extended RT17 with multi-constellation records). RTKLIB `convbin` only decodes the older RT17 format; the open-source tool chain cannot extract observations from RT27. Files that hit this case are marked `convert_status = unsupported_rt27` in the manifest, and only the bzip2 header metadata is used. Coverage analysis, station maps, and date/hour gap detection still work — only per-file epoch analytics are missing.

Trimble's proprietary `convertToRINEX` GUI is the only tool that handles RT27, and Trimble has no working command-line mode (verified on 2.1.1.0 and 3.14.0). If full RINEX conversion is required, run that tool by hand on the affected files.

## Bundled tools (`tools/`)

- `tools\runpkr00\runpkr00.exe` — Trimble proprietary T02 / T04 → DAT unpacker
- `tools\rtklib\convbin.exe` — RTKLIB DAT (RT17) → RINEX 3 converter
- `tools\rtklib\rnx2rtkp.exe` — RTKLIB single-point position solver (used as a coord fallback when the T02 header has no `RefStationLLH`)

## Repository layout

- `dashboard.py` — Streamlit UI (the entry point)
- `to2_pipeline.py` — scan + probe + convert + manifest export
- `scan_gnss_folder.py` — standalone CLI scanner (kept for parity with the dashboard's "scan now" path)
- `requirements.txt` — `pandas`, `numpy`, `plotly`, `streamlit`, `requests`
- `INSTALL.bat` / `RUN_DASHBOARD.bat` — Windows-friendly install + launch
