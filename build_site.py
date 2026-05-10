from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

from gnss_scan import ScanConfig, hourly_coverage, scan_gnss_tree


def _hours_row(n_files_by_hour: dict[int, int]) -> list[int]:
    return [1 if n_files_by_hour.get(h, 0) > 0 else 0 for h in range(24)]


def build_coverage_json(data_root: Path) -> dict:
    files_df = scan_gnss_tree(ScanConfig(root=data_root))
    cov = hourly_coverage(files_df)

    if cov.empty:
        return {
            "meta": {
                "data_root": str(data_root),
                "generated_at": pd.Timestamp.utcnow().isoformat(),
                "receivers": [],
            },
            "days": [],
        }

    cov["date"] = pd.to_datetime(cov["date"]).dt.date
    cov["hour"] = cov["hour"].astype(int)

    receivers = sorted(cov["receiver_prefix"].unique().tolist())
    days_out = []

    # Group by receiver + day, then store a 24-bit row
    for (receiver, d), g in cov.groupby(["receiver_prefix", "date"], sort=True):
        n_files_by_hour = dict(zip(g["hour"].tolist(), g["n_files"].tolist()))
        days_out.append(
            {
                "receiver": receiver,
                "date": d.isoformat() if isinstance(d, date) else str(d),
                "hours": _hours_row(n_files_by_hour),
                "n_files": {str(int(h)): int(n) for h, n in n_files_by_hour.items()},
            }
        )

    return {
        "meta": {
            "data_root": str(data_root),
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "receivers": receivers,
            "file_count": int(len(files_df)),
            "parsed_day_count": int(cov["date"].nunique()),
        },
        "days": days_out,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True, help=r'Path to your "2026" folder')
    ap.add_argument("--out-dir", default="site", help="Output folder for the website assets")
    args = ap.parse_args()

    data_root = Path(args.data_root).expanduser().resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = build_coverage_json(data_root)
    (out_dir / "coverage.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Wrote: {out_dir / 'coverage.json'}")
    print("Next:")
    print(f'  python -m http.server 8501 --directory "{out_dir}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

