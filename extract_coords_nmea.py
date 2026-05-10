from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from gnss_db import db_session


NMEA_RE = re.compile(rb"\$(GP|GN)GGA,[^\r\n]*")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dm_to_deg(dm: str, hemi: str) -> float | None:
    """
    Convert NMEA ddmm.mmmm (lat) / dddmm.mmmm (lon) into decimal degrees.
    """
    dm = dm.strip()
    if not dm:
        return None
    try:
        v = float(dm)
    except Exception:
        return None

    deg = int(v // 100)
    minutes = v - deg * 100
    dec = deg + minutes / 60.0
    hemi = hemi.strip().upper()
    if hemi in ("S", "W"):
        dec = -dec
    return dec


def _parse_gga(sentence: bytes) -> tuple[float, float] | None:
    # $GNGGA,time,lat,N,lon,E,fix,...
    try:
        s = sentence.decode("ascii", errors="ignore")
    except Exception:
        return None
    if not s.startswith("$") or "GGA" not in s:
        return None
    parts = s.split(",")
    if len(parts) < 7:
        return None
    lat_dm = parts[2]
    lat_hemi = parts[3]
    lon_dm = parts[4]
    lon_hemi = parts[5]
    fix = parts[6].strip()  # 0 = invalid
    if fix == "0":
        return None

    lat = _dm_to_deg(lat_dm, lat_hemi)
    lon = _dm_to_deg(lon_dm, lon_hemi)
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def iter_gga_points(file_path: Path, max_bytes: int) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    read = 0
    chunk_size = 1024 * 1024

    with file_path.open("rb") as f:
        carry = b""
        while True:
            if max_bytes > 0 and read >= max_bytes:
                break
            to_read = chunk_size
            if max_bytes > 0:
                to_read = min(to_read, max_bytes - read)
            data = f.read(to_read)
            if not data:
                break
            read += len(data)
            buf = carry + data
            # Keep tail in case sentence spans chunks
            carry = buf[-200:]

            for m in NMEA_RE.finditer(buf):
                sent = m.group(0)
                p = _parse_gga(sent)
                if p:
                    pts.append(p)
            # soft cap per file to keep runtime bounded
            if len(pts) >= 500:
                break
    return pts


def upsert_receiver_coords(conn, receiver_prefix: str, lat: float, lon: float) -> None:
    now = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO receivers (receiver_prefix, name, lat, lon, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(receiver_prefix) DO UPDATE SET
          lat=excluded.lat,
          lon=excluded.lon,
          updated_at=excluded.updated_at
        """,
        (receiver_prefix, receiver_prefix, float(lat), float(lon), now),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="gnss.db", help="SQLite db path (default: gnss.db)")
    ap.add_argument("--limit-files", type=int, default=2000, help="Max files to sample (default 2000)")
    ap.add_argument(
        "--bytes-per-file",
        type=int,
        default=5_000_000,
        help="Max bytes to scan per file (default 5,000,000). 0 = full file (slow).",
    )
    args = ap.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    limit_files = max(1, int(args.limit_files))
    max_bytes = int(args.bytes_per_file)

    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT receiver_prefix, path
            FROM files
            WHERE path IS NOT NULL
            ORDER BY receiver_prefix, mtime DESC
            """
        ).fetchall()

    if not rows:
        print("No files in DB to scrape.")
        return 0

    df = pd.DataFrame([{"receiver_prefix": r["receiver_prefix"], "path": r["path"]} for r in rows])

    updated = 0
    for receiver, g in df.groupby("receiver_prefix", sort=True):
        # sample up to N recent files per receiver, but also cap overall effort
        paths = [Path(p) for p in g["path"].head(10).tolist()]  # 10 recent files per receiver
        pts: list[tuple[float, float]] = []
        for p in paths:
            if len(pts) >= 50:
                break
            if not p.exists():
                continue
            pts.extend(iter_gga_points(p, max_bytes=max_bytes))
        if not pts:
            continue

        lats = np.array([x[0] for x in pts], dtype=float)
        lons = np.array([x[1] for x in pts], dtype=float)
        lat = float(np.median(lats))
        lon = float(np.median(lons))

        with db_session(db_path) as conn:
            upsert_receiver_coords(conn, receiver_prefix=str(receiver), lat=lat, lon=lon)
            conn.commit()
        updated += 1

        if updated >= limit_files:
            break

    print(f"DB: {db_path}")
    print(f"Receivers updated with coords: {updated}")
    print("Note: coords are extracted from embedded NMEA GGA if present in logs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

