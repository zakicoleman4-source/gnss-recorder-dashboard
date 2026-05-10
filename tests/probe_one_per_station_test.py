from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from to2_pipeline import PipelineConfig, run_pipeline


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    data_root = (root / "geonet_sample").resolve()
    cache_dir = (root / "._cache_probe").resolve()

    cfg = PipelineConfig(
        data_root=data_root,
        cache_dir=cache_dir,
        runpkr00_path=(root / "tools" / "runpkr00" / "runpkr00.exe"),
        teqc_path=Path(r"C:\Aj\gps\Office\Tools\3rdParts\teqc.exe"),
        max_files_per_station=1,
    )

    db = run_pipeline(cfg)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    ok = con.execute(
        """
        SELECT station, convert_status, lat, lon, constellations, signals, rinex_obs_path
        FROM files
        WHERE convert_status='ok'
        ORDER BY station
        """
    ).fetchall()

    print("db:", db)
    print("ok_rows:", len(ok))
    for r in ok[:25]:
        print(dict(r))


if __name__ == "__main__":
    main()

