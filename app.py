from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from gnss_scan import ScanConfig, build_week_grid, hourly_coverage, scan_gnss_tree


st.set_page_config(page_title="GNSS Recorder Dashboard", layout="wide")


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


@st.cache_data(show_spinner=False)
def _scan(root_str: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = scan_gnss_tree(ScanConfig(root=Path(root_str)))
    cov = hourly_coverage(df)
    return df, cov


def _heatmap_7x24(grid: pd.DataFrame) -> go.Figure:
    # Monday..Sunday rows
    day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    hours = list(range(24))

    # recorded matrix: 7 x 24
    pivot = (
        grid.pivot_table(index="dow", columns="hour", values="recorded", aggfunc="max")
        .reindex(index=range(7), columns=hours)
        .fillna(False)
    )
    z = pivot.values.astype(int)

    # hover: show date + hour + n_files
    nfiles = (
        grid.pivot_table(index="dow", columns="hour", values="n_files", aggfunc="max")
        .reindex(index=range(7), columns=hours)
        .fillna(0)
        .astype(int)
    )

    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=hours,
            y=day_labels,
            colorscale=[
                [0.0, "#2b2b2b"],  # missing
                [1.0, "#00b894"],  # recorded
            ],
            showscale=False,
            customdata=nfiles.values,
            hovertemplate="Day=%{y}<br>Hour=%{x}:00<br>Recorded=%{z}<br>Files=%{customdata}<extra></extra>",
        )
    )
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=320)
    fig.update_xaxes(title="Hour (0-23)", dtick=1)
    fig.update_yaxes(title="")
    return fig


st.title("GNSS Recorder Dashboard")
st.caption("Scan `.to2` / `.to4` files and visualize recording coverage by receiver and week.")

with st.sidebar:
    st.header("Scan")
    root = st.text_input(
        "Data root folder (e.g. ...\\2026)",
        value="",
        placeholder=r"D:\data\2026",
    )
    scan_clicked = st.button("Scan now", type="primary", disabled=not root.strip())

if not root.strip():
    st.info("Enter the path to your `2026` folder in the sidebar, then click **Scan now**.")
    st.stop()

if scan_clicked:
    st.cache_data.clear()

with st.spinner("Scanning files (first run can take a bit)..."):
    files_df, cov_df = _scan(root.strip())

top_cols = st.columns(4)
top_cols[0].metric("Files found", f"{len(files_df):,}")
top_cols[1].metric("Receivers (prefixes)", f"{files_df['receiver_prefix'].nunique() if not files_df.empty else 0:,}")
top_cols[2].metric("Days parsed", f"{files_df['date'].dropna().dt.date.nunique() if not files_df.empty else 0:,}")
top_cols[3].metric("Rows w/ hour parsed", f"{files_df['hour'].notna().sum() if not files_df.empty else 0:,}")

if files_df.empty:
    st.warning("No `.to2` / `.to4` files found under that folder.")
    st.stop()

if cov_df.empty:
    st.warning(
        "Files were found, but I couldn't parse `date` and `hour` well enough to build coverage.\n\n"
        "If you share 2–3 example filenames + their folder paths, I’ll adapt the parser."
    )
    st.dataframe(files_df.head(200), use_container_width=True)
    st.stop()

receiver_options = sorted(cov_df["receiver_prefix"].dropna().unique().tolist())
default_receiver = receiver_options[0] if receiver_options else "UNKNOWN"

min_day = cov_df["date"].min().date()
max_day = cov_df["date"].max().date()

controls = st.columns([2, 2, 2, 6])
receiver = controls[0].selectbox("Receiver (by filename prefix)", receiver_options, index=0)
picked_day = controls[1].date_input("Any day in week", value=_monday_of(min_day), min_value=min_day, max_value=max_day)
week_start = _monday_of(picked_day)
controls[2].write("")
controls[2].write(f"**Week start:** `{week_start.isoformat()}`")

grid = build_week_grid(cov_df, receiver_prefix=receiver, week_start=week_start)

coverage_pct = 100.0 * float(grid["recorded"].mean())
recorded_hours = int(grid["recorded"].sum())
missing_hours = int((~grid["recorded"]).sum())
files_in_week = int(grid["n_files"].sum())

stats_cols = st.columns(4)
stats_cols[0].metric("Coverage (hours recorded)", f"{coverage_pct:.1f}%")
stats_cols[1].metric("Recorded hours", f"{recorded_hours}/168")
stats_cols[2].metric("Missing hours", f"{missing_hours}/168")
stats_cols[3].metric("Files in week", f"{files_in_week:,}")

left, right = st.columns([3, 2])
with left:
    st.subheader("Weekly coverage (7×24)")
    st.plotly_chart(_heatmap_7x24(grid), use_container_width=True)

with right:
    st.subheader("Gaps (missing hours)")
    gaps = grid.loc[~grid["recorded"], ["date", "hour"]].copy()
    gaps["date"] = gaps["date"].dt.date
    gaps = gaps.sort_values(["date", "hour"])

    if gaps.empty:
        st.success("No gaps in this week.")
    else:
        # Show up to first 200 gaps; typically enough for a week
        st.dataframe(gaps.head(200), use_container_width=True, hide_index=True)

st.divider()
st.subheader("Raw file sample (for troubleshooting parsing)")
st.dataframe(
    files_df[["rel_path", "receiver_prefix", "date", "hour", "ext", "size_bytes"]].head(500),
    use_container_width=True,
    hide_index=True,
)

