from __future__ import annotations

from pathlib import Path
import runpy


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    runpy.run_path(str(here / "dashboard.py"), run_name="__main__")

