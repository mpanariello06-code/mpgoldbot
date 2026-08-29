#!/usr/bin/env python3
"""
Run every suite. No external test dependency - just: python tests/run_all.py

Each suite is a standalone script that prints PASS/FAIL lines and exits non-zero
on failure, so they can also be run one at a time while developing.
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SUITES = [
    "test_price_utils.py",
    "test_broker.py",
    "test_settings.py",
    "test_ladder_engine.py",
    "test_telegram.py",
    "test_app.py",
]


def main():
    verbose = "-v" in sys.argv
    total = failed = 0
    results = []
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], cwd=HERE,
                              capture_output=True, text=True, timeout=600)
        out = proc.stdout
        if verbose:
            print(out)
        passed = out.count("\nPASS |") + (1 if out.startswith("PASS |") else 0)
        fails = [ln for ln in out.splitlines() if ln.startswith("FAIL |")]
        total += passed
        failed += len(fails)
        results.append((suite, passed, len(fails), proc.returncode))
        print(f"{suite:<28} {passed:>4} passed  {len(fails):>3} failed"
              f"{'  <-- FAILURES' if fails else ''}")
        for line in fails:
            print("   " + line)
        if proc.returncode not in (0, 1):
            print(proc.stdout[-2000:])
            print(proc.stderr[-2000:])

    print("-" * 60)
    print(f"TOTAL: {total} passed, {failed} failed across {len(SUITES)} suites")
    return 1 if failed or any(r[3] not in (0, 1) for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
