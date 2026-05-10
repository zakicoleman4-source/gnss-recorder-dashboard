from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path

import requests


def _github_latest_asset_url(owner: str, repo: str, asset_name_contains: str, asset_endswith: str) -> str:
    api = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    r = requests.get(api, timeout=30)
    r.raise_for_status()
    data = r.json()
    for a in data.get("assets", []):
        name = a.get("name", "")
        if asset_name_contains.lower() in name.lower() and name.lower().endswith(asset_endswith.lower()):
            return a["browser_download_url"]
    raise RuntimeError(f"Could not find asset containing '{asset_name_contains}' ending with '{asset_endswith}' in {owner}/{repo} latest release")


def main() -> int:
    ap = argparse.ArgumentParser(description="Download RTKLIB tools (convbin) for offline bundling.")
    ap.add_argument("--out-dir", default="tools/rtklib", help="Where to place extracted tools")
    ap.add_argument("--owner", default="rtklibexplorer", help="GitHub owner (default rtklibexplorer)")
    ap.add_argument("--repo", default="RTKLIB", help="GitHub repo (default RTKLIB)")
    ap.add_argument("--asset-contains", default="RTKLIB", help="Asset name substring to select")
    ap.add_argument("--asset-endswith", default=".zip", help="Asset file suffix (default .zip)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    url = _github_latest_asset_url(args.owner, args.repo, args.asset_contains, args.asset_endswith)
    print(f"Downloading: {url}")
    zbytes = requests.get(url, timeout=180).content

    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    members = zf.namelist()

    # Look for convbin.exe anywhere in archive
    conv_candidates = [m for m in members if m.lower().endswith("convbin.exe")]
    if not conv_candidates:
        raise RuntimeError("Could not find convbin.exe in downloaded zip")

    # Extract the first one
    conv_member = conv_candidates[0]
    conv_out = out_dir / "convbin.exe"
    conv_out.write_bytes(zf.read(conv_member))
    print(f"Wrote: {conv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

