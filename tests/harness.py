"""Tiny zero-dependency test harness shared by the suites."""
import sys
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent


def use_stub_mt5():
    """Put the MetaTrader5 stub and the project on sys.path."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "stub_mt5"))
    sys.path.insert(0, str(REPO))


class Suite:
    def __init__(self, name):
        self.name = name
        self.failures = []
        self.passed = 0

    def section(self, title):
        print(f"\n=== {title} ===")

    def check(self, label, condition, extra=""):
        if condition:
            self.passed += 1
            print(f"PASS | {label} {extra}")
        else:
            self.failures.append(label)
            print(f"FAIL | {label} {extra}")
        return bool(condition)

    def raises(self, label, exc_type, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
        except exc_type as exc:
            return self.check(label, True, f"({exc})")
        except Exception as exc:
            return self.check(label, False, f"wrong error: {exc!r}")
        return self.check(label, False, "no error raised")

    def done(self):
        print("\n" + "=" * 60)
        print(f"{self.name}: {self.passed} passed, {len(self.failures)} failed")
        if self.failures:
            print("FAILURES:", self.failures)
        sys.exit(1 if self.failures else 0)
