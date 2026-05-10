from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests

from gnss_db import db_session


GEONET_STATION_TEXT_URL = (
    "https://service.geonet.org.nz/fdsnws/station/1/query"
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def fetch_station_text(stations: list[str], network: str = "NZ", timeout_s: int = 20) -> str:
    params = {
        "network": network,
        "station": ",".join(stations),
        "level": "station",
        "format": "text",
    }
    r = requests.get(GEONET_STATION_TEXT_URL, params=params, timeout=timeout_s)
    r.raise_for_status()
    return r.text


def parse_station_text(text: str) -> list[dict]:
    """
    GeoNet FDSN station text is '|' delimited.
    Expected header:
      #Network|Station|Latitude|Longitude|Elevation|SiteName|StartTime|EndTime
    """
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        net, sta, lat, lon, _elev, site = parts[:6]
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except Exception:
            continue
        rows.append(
            {
                "network": net.strip(),
                "station": sta.strip().upper(),
                "lat": lat_f,
                "lon": lon_f,
                "name": site.strip() or sta.strip().upper(),
            }
        )
    return rows


def upsert_receiver(conn, station: str, name: str, lat: float, lon: float) -> None:
    now = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO receivers (receiver_prefix, name, lat, lon, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(receiver_prefix) DO UPDATE SET
          name=COALESCE(excluded.name, receivers.name),
          lat=excluded.lat,
          lon=excluded.lon,
          updated_at=excluded.updated_at
        """,
        (station, name, float(lat), float(lon), now),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="gnss.db", help="SQLite db path")
    ap.add_argument("--network", default="NZ", help="FDSN network code (default NZ)")
    ap.add_argument("--batch", type=int, default=50, help="Stations per request (default 50)")
    ap.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds (default 20)")
    args = ap.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    with db_session(db_path) as conn:
        stations = [
            r["receiver_prefix"]
            for r in conn.execute("SELECT DISTINCT receiver_prefix FROM files ORDER BY receiver_prefix").fetchall()
        ]

    if not stations:
        print("No receiver prefixes in DB.")
        return 0

    updated = 0
    failed_batches = 0
    for batch in chunked(stations, max(1, int(args.batch))):
        try:
            text = fetch_station_text(batch, network=args.network, timeout_s=int(args.timeout))
            parsed = parse_station_text(text)
        except Exception:
            failed_batches += 1
            continue

        if not parsed:
            continue

        with db_session(db_path) as conn:
            for row in parsed:
                upsert_receiver(conn, row["station"], row["name"], row["lat"], row["lon"])
                updated += 1
            conn.commit()

    print(f"DB: {db_path}")
    print(f"Stations attempted: {len(stations)}")
    print(f"Stations updated with coords: {updated}")
    if failed_batches:
        print(f"Failed batches (network/offline/etc): {failed_batches}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

