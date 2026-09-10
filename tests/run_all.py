"""Run every check and print one report.

Plain Python, no pytest - `run_tests.bat` is meant to be double-clickable.

Run this after any change to detection, planning, or rendering. It exists
because the alternative was me judging screenshots by eye, and that got three
boundary labels wrong and left half the detector's output unexamined. A
committed test that checks every boundary is the part that does not forget.

    .venv\\Scripts\\python.exe -m tests.run_all           # everything
    .venv\\Scripts\\python.exe -m tests.run_all planner   # one suite
    .venv\\Scripts\\python.exe -m tests.run_all --rebuild # rebuild fixtures

Exit code is 0 when everything passed, 1 otherwise, so CI can gate on it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import (lib, test_detection, test_long_input,  # noqa: E402
                   test_planner, test_render)

SUITES = (
    ("planner", test_planner),
    ("detection", test_detection),
    ("long", test_long_input),
    ("render", test_render),
)


def main(argv: list[str]) -> int:
    wanted = [a.lower() for a in argv if not a.startswith("-")]
    if "--rebuild" in argv:
        print("rebuilding constructed-truth fixtures...")
        lib.build_truth(rebuild=True)

    print("=" * 68)
    print("Reaction Video Builder - regression suite")
    print("=" * 68)

    results: list[tuple[str, bool, float]] = []
    for key, module in SUITES:
        if wanted and key not in wanted:
            continue
        print(f"\n{module.NAME}")
        started = time.perf_counter()
        try:
            ok, lines = module.run()
        except Exception as exc:            # a crashing check is a failure
            ok = False
            lines = [f"  [FAIL] the check itself raised: "
                     f"{exc.__class__.__name__}: {exc}"]
        elapsed = time.perf_counter() - started
        for line in lines:
            print(line)
        print(f"  -> {'PASS' if ok else 'FAIL'} in {elapsed:.1f}s")
        results.append((module.NAME, ok, elapsed))

    if not results:
        print(f"\nNothing matched {wanted}. "
              f"Known suites: {', '.join(k for k, _ in SUITES)}")
        return 1

    print("\n" + "=" * 68)
    for name, ok, elapsed in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<24} {elapsed:6.1f}s")
    failed = [name for name, ok, _ in results if not ok]
    print("=" * 68)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
