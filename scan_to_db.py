from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from gnss_db import db_session
from gnss_scan import ScanConfig, scan_gnss_tree


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_files(conn, data_root: Path, df: pd.DataFrame) -> int:
    if df.empty:
        return 0

    # Keep only fields we store
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date.astype("string")
    out["hour"] = pd.to_numeric(out["hour"], errors="coerce")
    out["mtime"] = pd.to_datetime(out["mtime"], errors="coerce").astype("string")

    cols = [
        "path",
        "rel_path",
        "receiver_prefix",
        "receiver_folder",
        "day_folder",
        "year_folder",
        "ext",
        "date",
        "hour",
        "size_bytes",
        "mtime",
    ]
    out = out[cols]

    sql = """
    INSERT INTO files (
      path, rel_path, receiver_prefix, receiver_folder, day_folder, year_folder,
      ext, date, hour, size_bytes, mtime
    )
    VALUES (
      :path, :rel_path, :receiver_prefix, :receiver_folder, :day_folder, :year_folder,
      :ext, :date, :hour, :size_bytes, :mtime
    )
    ON CONFLICT(path) DO UPDATE SET
      rel_path=excluded.rel_path,
      receiver_prefix=excluded.receiver_prefix,
      receiver_folder=excluded.receiver_folder,
      day_folder=excluded.day_folder,
      year_folder=excluded.year_folder,
      ext=excluded.ext,
      date=excluded.date,
      hour=excluded.hour,
      size_bytes=excluded.size_bytes,
      mtime=excluded.mtime
    """

    n = 0
    cur = conn.cursor()
    for rec in out.to_dict(orient="records"):
        cur.execute(sql, rec)
        n += 1
    conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True, help=r'Path to your "2026" folder')
    ap.add_argument("--db", default="gnss.db", help="SQLite db path (default: gnss.db)")
    args = ap.parse_args()

    data_root = Path(args.data_root).expanduser().resolve()
    db_path = Path(args.db).expanduser().resolve()

    started = _utc_now_iso()
    with db_session(db_path) as conn:
        scan_id = conn.execute(
            "INSERT INTO scans (data_root, started_at) VALUES (?, ?)",
            (str(data_root), started),
        ).lastrowid
        conn.commit()

        df = scan_gnss_tree(ScanConfig(root=data_root))
        upserted = upsert_files(conn, data_root, df)

        finished = _utc_now_iso()
        conn.execute(
            "UPDATE scans SET finished_at=?, file_count=?, parsed_rows=? WHERE id=?",
            (finished, int(len(df)), int(upserted), int(scan_id)),
        )
        conn.commit()

    print(f"DB: {db_path}")
    print(f"Files scanned: {len(df)}")
    print(f"Rows upserted: {upserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

