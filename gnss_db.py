from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@dataclass(frozen=True)
class DbConfig:
    path: Path


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
          id INTEGER PRIMARY KEY,
          path TEXT NOT NULL,
          rel_path TEXT NOT NULL,
          receiver_prefix TEXT NOT NULL,
          receiver_folder TEXT,
          day_folder TEXT,
          year_folder TEXT,
          ext TEXT NOT NULL,
          date TEXT,            -- ISO date YYYY-MM-DD (nullable if not parsed)
          hour INTEGER,         -- 0..23 (nullable if not parsed)
          size_bytes INTEGER,
          mtime TEXT,
          UNIQUE(path)
        );

        CREATE INDEX IF NOT EXISTS idx_files_receiver_date_hour
          ON files(receiver_prefix, date, hour);

        CREATE TABLE IF NOT EXISTS scans (
          id INTEGER PRIMARY KEY,
          data_root TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          file_count INTEGER DEFAULT 0,
          parsed_rows INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS receivers (
          receiver_prefix TEXT PRIMARY KEY,
          name TEXT,
          lat REAL,
          lon REAL,
          is_vrs INTEGER DEFAULT 0,
          updated_at TEXT
        );
        """
    )
    conn.commit()


@contextmanager
def db_session(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()


def get_receivers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT receiver_prefix, COUNT(*) AS n FROM files GROUP BY receiver_prefix ORDER BY receiver_prefix"
    ).fetchall()
    return [r["receiver_prefix"] for r in rows]


def get_date_range(conn: sqlite3.Connection, receiver_prefix: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    if receiver_prefix:
        row = conn.execute(
            "SELECT MIN(date) AS min_d, MAX(date) AS max_d FROM files WHERE receiver_prefix=? AND date IS NOT NULL",
            (receiver_prefix,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MIN(date) AS min_d, MAX(date) AS max_d FROM files WHERE date IS NOT NULL"
        ).fetchone()
    if not row:
        return None, None
    return row["min_d"], row["max_d"]


def week_coverage(conn: sqlite3.Connection, receiver_prefix: str, week_start_iso: str) -> list[dict]:
    # Return list of {date, hour, n_files}
    # Week is [week_start, week_start+6]
    row = conn.execute("SELECT date(?) AS ws", (week_start_iso,)).fetchone()
    if not row or not row["ws"]:
        return []

    rows = conn.execute(
        """
        SELECT date AS date, hour AS hour, COUNT(*) AS n_files
        FROM files
        WHERE receiver_prefix = ?
          AND date IS NOT NULL
          AND hour IS NOT NULL
          AND date >= date(?)
          AND date <= date(?, '+6 day')
        GROUP BY date, hour
        ORDER BY date, hour
        """,
        (receiver_prefix, week_start_iso, week_start_iso),
    ).fetchall()
    return [{"date": r["date"], "hour": int(r["hour"]), "n_files": int(r["n_files"])} for r in rows]


def receiver_locations(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT receiver_prefix, COALESCE(name, receiver_prefix) AS name, lat, lon, is_vrs
        FROM receivers
        WHERE lat IS NOT NULL AND lon IS NOT NULL
        ORDER BY receiver_prefix
        """
    ).fetchall()
    return [
        {
            "receiver_prefix": r["receiver_prefix"],
            "name": r["name"],
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "is_vrs": bool(r["is_vrs"]),
        }
        for r in rows
    ]


def vrs_receivers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT receiver_prefix FROM receivers WHERE is_vrs=1 ORDER BY receiver_prefix"
    ).fetchall()
    return [r["receiver_prefix"] for r in rows]

