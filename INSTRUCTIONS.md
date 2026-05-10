## GNSS Recorder Dashboard — Setup & Usage

This project is designed so a user can **scan a folder of GNSS files** and then use a local dashboard to view:
- coverage (hour-by-hour / week-by-week),
- a station map,
- VRS panel,
- raw tables + exports.

It is **offline-friendly**: you can install without internet using a local folder of Python wheels (“wheelhouse”).

---

## 1) Install (offline-friendly)

### 1A) On an ONLINE PC (one-time): build the wheelhouse

This downloads all dependency `.whl` files into `offline_installer/wheelhouse/`.

```powershell
cd C:\Aj\LINGBOT\gnss-recorder-dashboard\offline_installer
python DOWNLOAD_WHEELS.py
```

Now copy the whole `gnss-recorder-dashboard/` folder (including `offline_installer/wheelhouse/`) to the offline PC.

### 1B) On the OFFLINE PC: one-click install

Double-click:

- `offline_installer\INSTALL_OFFLINE.bat`

It creates:
- `C:\Aj\LINGBOT\.venv_offline\` (virtual environment)
- installs dependencies **without internet**.

### 1C) Optional: confirm “offline readiness”

```powershell
cd C:\Aj\LINGBOT\gnss-recorder-dashboard
.\.venv_offline\Scripts\python offline_installer\OFFLINE_SELF_TEST.py
```

---

## 2) Run the dashboard (offline)

Double-click:

- `offline_installer\RUN_DASHBOARD_OFFLINE.bat`

Or run manually:

```powershell
cd C:\Aj\LINGBOT\gnss-recorder-dashboard
$env:GNSS_OFFLINE="1"
.\.venv_offline\Scripts\python -m streamlit run streamlit_app.py
```

Streamlit will print a local URL (usually `http://localhost:8501`). Open it in your browser.

---

## 3) Scan a folder (normal workflow)

In the dashboard sidebar:

- **Data source**: choose **Local folder**
- **Manifests folder**:
  - point it at an existing manifests folder (fast), OR
  - generate manifests once using the scanner (below)

### Scan directly in the UI (TO2/T02)

If the user only has raw `.TO2/.T02` files and you want minimal command line usage:

- Data source: **Scan folder (TO2/T02)**
- Choose the **Data folder to scan**
- Click **Scan now**

This writes a persistent cache to the Cache folder so the user does **not** rescan every time.

### Generate manifests (recommended once per dataset)

This scans the folder and writes `_manifests/files_manifest.csv` and `_manifests/summary.json`.

```powershell
cd C:\Aj\LINGBOT\gnss-recorder-dashboard
.\.venv_offline\Scripts\python scan_gnss_folder.py "D:\PATH\TO\YOUR\DATA" --skip_runpkr00
```

Output goes to:
- `D:\PATH\TO\YOUR\DATA_scanned\_manifests\`

Then in the dashboard set **Manifests folder** to that `_manifests` path.

---

## 4) Station names (robust rule)

Station is inferred from the filename only:
- **first 3–4 letters** at the start of the filename
- folders and trailing digits are ignored

Example:
- `AUCK1234.T02` → `AUCK`

---

## 5) Exports (for sharing / “same results” on another PC)

In the dashboard sidebar → **Utilities**:
- **Download manifests zip** (portable scan output)
- **Download SQLite DB (from manifest)** (portable DB derived from the scan)

If two computers load the **same exported manifests/DB**, the dashboard will show **1:1 identical results**.

---

## 6) Notes for offline mode

- The dashboard will not depend on internet.
- GeoNet coordinate autofill is **disabled by default** in offline mode (`GNSS_OFFLINE=1`) to avoid hangs.
- If you do have internet and your stations are GeoNet stations, you can enable GeoNet coordinate autofill in the sidebar.

