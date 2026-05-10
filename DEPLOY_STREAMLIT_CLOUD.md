## Deploy to Streamlit Community Cloud (shareable link)

This repo is already prepared with:
- `streamlit_app.py` entrypoint
- `requirements.txt`
- `.streamlit/config.toml`
- a dashboard that can load data from **Local**, **URL (zip)**, or **Upload (zip)**.

### 1) Create a GitHub repo (app code only)

Upload the contents of `gnss-recorder-dashboard/` (this folder) to a GitHub repository.

Recommended files to include:
- `dashboard.py`
- `streamlit_app.py`
- `requirements.txt`
- `.streamlit/config.toml`
- `scan_gnss_folder.py`
- `download_geonet_raw.py`
- `make_manifests_zip.py`
- `README.md`

### 2) Deploy on Streamlit Cloud

1. Go to Streamlit Community Cloud.
2. Create a new app from your GitHub repo.
3. Set:
   - **Main file path**: `gnss-recorder-dashboard/streamlit_app.py`

### 3) Provide data to the deployed app

Option A (best): host a `manifests.zip` somewhere and paste URL in the sidebar.

Option B: set an environment variable in Streamlit Cloud:
- `GNSS_MANIFESTS_ZIP_URL=https://.../manifests.zip`

Option C: upload the zip in the app UI (works but manual per viewer).

### Notes
- Do **not** upload raw GNSS data to Streamlit Cloud. Upload only the small manifest zip.

