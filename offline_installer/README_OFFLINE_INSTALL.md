## Offline install (Python already present)

This project can be installed on an offline PC using a local **wheelhouse** (a folder of `.whl` files).

Requirement: **Python 3.10 or newer** on the offline PC. Verify with:

```powershell
python --version
```

If `python` opens the Microsoft Store, install Python 3.10+ from
[python.org](https://www.python.org/downloads/windows/) and **check "Add python.exe to PATH"** during install.

---

### What you copy to the offline PC

Copy the entire folder:

`gnss-recorder-dashboard\`

This already contains the `offline_installer\` subfolder, the bundled
`tools\runpkr00\` and `tools\teqc\` converters, and the dashboard code.

---

### Step A (on an ONLINE computer): download wheels

From `gnss-recorder-dashboard\offline_installer\`:

```powershell
python DOWNLOAD_WHEELS.py
```

This fills a versioned folder like `offline_installer\wheelhouse_cp310\`,
`wheelhouse_cp311\`, `wheelhouse_cp313\`, etc.
(depends on the Python version you ran it with).

Then copy that `wheelhouse_cp3xx\` folder to the offline PC (same relative location).

#### Recommended: build for every supported Python in one shot

```powershell
python DOWNLOAD_WHEELS.py --all-supported
```

Builds `wheelhouse_cp310\` through `wheelhouse_cp313\` (Windows x64).
Ship them all and `INSTALL_OFFLINE.bat` auto-picks the right one for the client's Python.

#### What does "wheelhouse_cp3XX" mean?

- `cp310` = Python 3.10
- `cp311` = Python 3.11
- `cp312` = Python 3.12
- `cp313` = Python 3.13

So if your client has **Python 3.10**, you must include **`wheelhouse_cp310\`**.

---

### Step B (on the OFFLINE computer): one-click install

Double-click:

`offline_installer\INSTALL_OFFLINE.bat`

It will:
- detect the client's Python version (and bail with a clear error if Python 3.10+ is missing)
- create `..\..\.venv_offline\`
- install all dependencies from the matching `wheelhouse_cp3xx\` (no internet)
- keep the cmd window open at the end so the operator can read any error message

---

### Run the dashboard

After install, double-click:

`offline_installer\RUN_DASHBOARD_OFFLINE.bat`

This will:
- launch Streamlit in offline mode (no GeoNet calls, no telemetry, no email prompt)
- pre-export `GNSS_TEQC` / `RUNPKR00_PATH` for the bundled converters
- pick the first free TCP port between **8501 and 8520** (so it works even if another Streamlit/dev server is already on 8501)
- print the exact URL to open if the browser doesn't open automatically:
  `http://localhost:<port>`
- keep the cmd window open if Streamlit exits unexpectedly so you can capture the error

If your browser does not open automatically, paste the URL the script prints
into Chrome/Edge/Firefox.

---

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `INSTALL_OFFLINE.bat` says **"python is not on PATH or points at the Microsoft Store stub"** | No real Python installed (the typed `python` opens the Store) | Install Python 3.10+ from python.org, **check "Add python.exe to PATH"**, retry. |
| `INSTALL_OFFLINE.bat` says **"wheelhouse missing %PYTAG% wheels"** | Bundle was built for a different Python version than the client has | On an online PC matching the client's Python version, run `DOWNLOAD_WHEELS.py`, copy the new `wheelhouse_cp3xx\` over, retry. |
| Windows SmartScreen blocks the .bat or .exe with **"Windows protected your PC"** | First run of unsigned files | Click **More info** -> **Run anyway**. (One-time per file.) |
| Antivirus quarantines `tools\runpkr00\runpkr00.exe` or `tools\teqc\teqc.exe` | Corporate AV doesn't recognise the legacy Trimble/UNAVCO converters | Whitelist the `tools\` folder. The converters are open-source and required for `.T02/.T04` decoding. |
| Browser doesn't open after `RUN_DASHBOARD_OFFLINE.bat` starts | No default browser configured / Streamlit `--server.headless=true` | Open Chrome/Edge/Firefox and paste the URL the script prints (e.g. `http://localhost:8501`). |
| Dashboard shows **"Data folder not found or empty"** after pasting a path with quotes | Windows "Copy as path" wraps the path in `"…"` | Already handled — quotes are stripped automatically. If you still see this, check the path actually exists. |
| Scan returns **0 matching files** but you have GBs of `.T02` data | You picked a *subfolder* of the actual archive root | Pick the parent folder containing all station/day subfolders. Pre-flight prints the count up to 100,000 so you'll see this immediately. |
| The dashboard tab goes **blank** after a click | Old `st.stop()` in a tab body — fixed in this build | Update to the latest `dashboard.py`; the `_safe_tab` wrapper now isolates per-tab errors. |

---

### T02/T04 conversion (offline)

For Trimble `.T02/.T04` files, the conversion path is:

1. `tools\runpkr00\runpkr00.exe` (bundled) -> `.dat` + `.eph`
2. `tools\teqc\teqc.exe` (bundled) -> RINEX `.o` + `.n`

`RUN_DASHBOARD_OFFLINE.bat` exports `GNSS_TEQC` and `RUNPKR00_PATH` automatically.

If you want to override the bundled `teqc.exe`, set it before running the dashboard:

```powershell
$env:GNSS_TEQC = 'C:\path\to\teqc.exe'
```

---

### Offline self-test (verifies features without internet)

Run from the bundled venv so you exercise the same Python the dashboard uses:

```powershell
.\.venv_offline\Scripts\python gnss-recorder-dashboard\offline_installer\OFFLINE_SELF_TEST.py
```

Expected output ends with `[OK] OFFLINE_SELF_TEST complete: PASS`.
You may see `[WARN] version skew` lines — those just flag that an
installed package is a different version than the pinned one in
`requirements_offline.txt`. The dashboard usually still works fine.

For the broader regression test (validates the dashboard, pipeline,
zip-slip defenses, etc.):

```powershell
.\.venv_offline\Scripts\python gnss-recorder-dashboard\PRODUCT_SELF_TEST.py
```

Expected output ends with `[OK] PRODUCT_SELF_TEST complete: PASS`.

---

### Where logs end up

The dashboard appends a structured log to:

`gnss-recorder-dashboard\debug-c48812.log` (NDJSON)
`gnss-recorder-dashboard\debug-c48812_readable.txt` (one line per event)

If something goes wrong, send those two files (plus `offline_installer\install_offline.log`).
