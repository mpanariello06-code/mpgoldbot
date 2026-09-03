#!/usr/bin/env python3
"""
Run every suite. No external test dependency - just: python tests/run_all.py

Each suite is a standalone script that prints PASS/FAIL lines and exits non-zero
on failure, so they can also be run one at a time while developing.
"""
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SUITES = [
    "test_price_utils.py",
    "test_broker.py",
    "test_settings.py",
    "test_basket.py",
    "test_ladder_engine.py",
    "test_ladder_lifecycle.py",
    "test_continuous.py",
    "test_notifications.py",
    "test_replay.py",
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
        # Count from the suite's own summary, not by scanning for PASS lines:
        # suites that start background threads interleave log output with test
        # output, which loses a line here and there and makes the totals drift.
        summary = re.search(r"^\w+: (\d+) passed, (\d+) failed$",
                            out, re.M)
        fails = [ln for ln in out.splitlines() if ln.startswith("FAIL |")]
        if summary:
            passed, reported_fails = int(summary.group(1)), int(summary.group(2))
        else:
            passed, reported_fails = 0, len(fails)
        total += passed
        failed += reported_fails
        # a suite with no summary line crashed before it could finish
        crashed = summary is None or proc.returncode not in (0, 1)
        results.append((suite, passed, reported_fails, crashed))
        print(f"{suite:<28} {passed:>4} passed  {reported_fails:>3} failed"
              f"{'  <-- CRASHED' if crashed else '  <-- FAILURES' if reported_fails else ''}")
        for line in fails:
            print("   " + line)
        if crashed:
            print((proc.stdout or "")[-1500:])
            print((proc.stderr or "")[-1500:])

    print("-" * 60)
    print(f"TOTAL: {total} passed, {failed} failed across {len(SUITES)} suites")
    return 1 if failed or any(r[3] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
