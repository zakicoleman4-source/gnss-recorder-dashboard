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

## Notes

- Re-running **Scan now** on the same folder uses the cache — only new or modified files are re-processed.
- The dashboard never uploads data anywhere. All conversion and analysis runs locally.
- For the RT27 limitation on Trimble Alloy receivers, see the README.
