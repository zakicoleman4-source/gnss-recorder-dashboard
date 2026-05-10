from __future__ import annotations

import sqlite3
from pathlib import Path

from to2_pipeline import PipelineConfig, export_manifests, run_pipeline


def main() -> None:
    root = Path(__file__).resolve().parent
    data_root = (root / "geonet_2026_060-119_all").resolve()
    cache_dir = (root / "._cache_geonet_probe").resolve()

    cfg = PipelineConfig(
        data_root=data_root,
        cache_dir=cache_dir,
        runpkr00_path=(root / "tools" / "runpkr00" / "runpkr00.exe"),
        teqc_path=Path(r"C:\Aj\gps\Office\Tools\3rdParts\teqc.exe"),
        max_files_per_station=1,
    )

    db = run_pipeline(cfg)
    manifests = export_manifests(db, out_dir=(cache_dir / "exported"))

    con = sqlite3.connect(db)
    ok = con.execute("select count(distinct station) from files where convert_status='ok'").fetchone()[0]
    seen = con.execute("select count(distinct station) from files").fetchone()[0]

    print("db:", db)
    print("manifests:", manifests)
    print("stations_ok:", ok, "stations_seen:", seen)


if __name__ == "__main__":
    main()

