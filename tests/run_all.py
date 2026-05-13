"""Run all test_*.py modules under tests/ and print a summary."""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path


def _run_module(mod_path: Path) -> tuple[int, int]:
    spec = importlib.util.spec_from_file_location(mod_path.stem, str(mod_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    n_pass = n_fail = 0
    for name, fn in vars(mod).items():
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                n_pass += 1
                print(f"  PASS  {name}")
            except AssertionError as e:
                n_fail += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:
                n_fail += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    return n_pass, n_fail


def main() -> int:
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here.parent))
    suites = sorted(here.glob("test_*.py"))
    total_pass = total_fail = 0
    for suite in suites:
        print(f"\n=== {suite.name} ===")
        p, f = _run_module(suite)
        total_pass += p
        total_fail += f
        print(f"  -> {p} pass, {f} fail")
    print(f"\nTOTAL: {total_pass} pass, {total_fail} fail")
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
