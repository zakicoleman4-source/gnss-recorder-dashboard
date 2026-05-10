from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from gnss_db import db_session


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="gnss.db", help="SQLite db path (default: gnss.db)")
    ap.add_argument(
        "--csv",
        required=True,
        help="CSV path with columns: receiver_prefix, lat, lon (optional: name, is_vrs)",
    )
    args = ap.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    csv_path = Path(args.csv).expanduser().resolve()

    df = pd.read_csv(csv_path)
    for c in ["receiver_prefix", "lat", "lon"]:
        if c not in df.columns:
            raise SystemExit(f"Missing required column: {c}")

    df["receiver_prefix"] = df["receiver_prefix"].astype(str).str.upper().str.strip()
    if "name" not in df.columns:
        df["name"] = df["receiver_prefix"]
    if "is_vrs" not in df.columns:
        df["is_vrs"] = 0

    now = _utc_now_iso()
    with db_session(db_path) as conn:
        cur = conn.cursor()
        for rec in df.to_dict(orient="records"):
            cur.execute(
                """
                INSERT INTO receivers (receiver_prefix, name, lat, lon, is_vrs, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(receiver_prefix) DO UPDATE SET
                  name=excluded.name,
                  lat=excluded.lat,
                  lon=excluded.lon,
                  is_vrs=excluded.is_vrs,
                  updated_at=excluded.updated_at
                """,
                (
                    rec["receiver_prefix"],
                    str(rec.get("name") or rec["receiver_prefix"]),
                    float(rec["lat"]),
                    float(rec["lon"]),
                    int(rec.get("is_vrs") or 0),
                    now,
                ),
            )
        conn.commit()

    print(f"DB: {db_path}")
    print(f"Imported receivers: {len(df)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

