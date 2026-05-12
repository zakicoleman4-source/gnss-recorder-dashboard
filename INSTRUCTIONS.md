# Usage

## First run

1. Double-click `INSTALL.bat` (one-time setup).
2. Double-click `RUN_DASHBOARD.bat` to start the dashboard.
3. The browser opens automatically at `http://localhost:8501` (or the next free port).

## Scanning a folder

1. In the sidebar, paste the path to a folder containing `.T02` / `.T04` files (recursively).
2. Pick a cache folder (any local path — the SQLite scan cache and converted RINEX files go here).
3. Click **Scan now**.
4. When the scan finishes, all tabs are populated:
   - **Overview** — totals, station count, sample interval distribution
   - **Coverage** — per-station hourly coverage matrix
   - **Map** — receiver locations
   - **Station** — per-station detail with start / end date filters
   - **Manifests** — download / load `files_manifest.csv` and `summary.json`

## Sharing scan output

In the **Utilities** section of the sidebar:

- **Download manifests zip** — bundles `files_manifest.csv`, `summary.json`, `coverage_gaps.csv`, `intra_file_gaps.csv`. Open the same zip on another machine via **Load manifest zip** to get an identical view.
- **Download SQLite DB** — full scan cache for backup.

## Trimble Alloy / RT27 datasets

If your files are from Trimble Alloy receivers (modern multi-constellation units), tick **CTR-first mode** in the sidebar before clicking Scan now:

> **CTR-first mode (skip runpkr00 — for RT27/Alloy-only datasets)**

This sends all files straight to the bundled `convertToRinex_cli.exe` and skips the older `runpkr00` step. Without it, each file wastes ~0.6 s on a converter that cannot handle RT27 — adds ~10 min to a 10,000-file scan.

Not sure what receiver type you have? Run `PROBE_FILES.bat` first (see below).

## Pre-scan inventory (PROBE_FILES.bat)

Before running a full scan, use `PROBE_FILES.bat` to inspect a data folder quickly:

1. Double-click `PROBE_FILES.bat`.
2. Type (or drag-drop) your data folder path and press Enter.
3. It prints a summary: receiver models, station names, date range, sample intervals, converter recommendation.
4. Full results saved to `probe_results.csv` in the dashboard folder.

Fast (~60 files/s, no conversion needed). Run this first on any new dataset.

## Notes

- Re-running **Scan now** reuses the cache — only new or changed files are reprocessed.
- The dashboard never uploads data anywhere. All processing runs locally.
- Station names, timestamps, and coordinates come from the converted RINEX output — files with no embedded metadata are handled automatically.
