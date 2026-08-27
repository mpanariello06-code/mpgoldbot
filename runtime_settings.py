"""
Thread-safe runtime settings manager.

Telegram-controlled settings live here (and in DATA_DIRECTORY/runtime_settings.json)
so they survive a restart. Secrets never enter this file - MT5 credentials and
the Telegram token stay in .env.

The trading engine takes a consistent snapshot() before processing a cycle, so
a Telegram change that lands mid-calculation can never produce a half-updated
configuration for an order.
"""

import json
import os
import threading
from pathlib import Path

# --- allowed values -------------------------------------------------------
TP_MODES = ("1_pip", "2_pips", "3_pips", "4_pips", "5_pips", "custom_rr")
LOT_MODES = ("risk_percent", "fixed_lot")
SL_MODES = ("structural", "fixed")

TP_MODE_LABELS = {
    "1_pip": "1 PIP",
    "2_pips": "2 PIPS",
    "3_pips": "3 PIPS",
    "4_pips": "4 PIPS",
    "5_pips": "5 PIPS",
    "custom_rr": "CUSTOM RR",
}
LOT_MODE_LABELS = {"risk_percent": "RISK %", "fixed_lot": "FIXED LOT"}
SL_MODE_LABELS = {"structural": "STRUCTURAL", "fixed": "FIXED"}

# How many pips each TP mode targets
TP_MODE_PIPS = {"1_pip": 1.0, "2_pips": 2.0, "3_pips": 3.0, "4_pips": 4.0,
                "5_pips": 5.0}


class SettingError(ValueError):
    """Raised for an invalid settings value (never crashes the bot)."""


def _num(value, cast, name, low, high):
    try:
        out = cast(value)
    except (TypeError, ValueError):
        raise SettingError(f"{name}: '{value}' is not a number")
    if out != out or out in (float("inf"), float("-inf")):
        raise SettingError(f"{name}: '{value}' is not a valid number")
    if out < low or out > high:
        raise SettingError(f"{name}: must be between {low} and {high} (got {out})")
    return out


def _choice(value, options, name):
    value = str(value).strip().lower()
    if value not in options:
        raise SettingError(f"{name}: must be one of {', '.join(options)}")
    return value


def _flag(value, name):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise SettingError(f"{name}: must be on or off")


# key -> (validator, human label, needs confirmation before applying)
VALIDATORS = {
    "tp_mode":               (lambda v: _choice(v, TP_MODES, "TP mode"), "TP Mode", True),
    "custom_rr":             (lambda v: _num(v, float, "Custom RR", 0.1, 20.0), "Custom RR", True),
    "min_rr":                (lambda v: _num(v, float, "Minimum RR", 0.0, 20.0), "Minimum RR", True),
    "lot_mode":              (lambda v: _choice(v, LOT_MODES, "Lot mode"), "Lot Mode", True),
    "risk_percent":          (lambda v: _num(v, float, "Risk percent", 0.01, 10.0), "Risk %", True),
    "fixed_lot":             (lambda v: _num(v, float, "Fixed lot", 0.01, 100.0), "Fixed Lot", True),
    "max_open_positions":    (lambda v: _num(v, int, "Max positions", 1, 20), "Max Positions", True),
    "sl_mode":               (lambda v: _choice(v, SL_MODES, "SL mode"), "SL Mode", True),
    "sl_points_min":         (lambda v: _num(v, int, "SL min", 10, 100000), "SL Min", True),
    "sl_points_max":         (lambda v: _num(v, int, "SL max", 10, 100000), "SL Max", True),
    "sl_fixed_points":       (lambda v: _num(v, int, "Fixed SL", 10, 100000), "Fixed SL", True),
    "breakeven_enabled":     (lambda v: _flag(v, "Breakeven"), "Breakeven", False),
    "breakeven_r":           (lambda v: _num(v, float, "Breakeven trigger", 0.05, 10.0), "BE Trigger", False),
    "partial_close_enabled": (lambda v: _flag(v, "Partial close"), "Partial Close", False),
    "partial_close_r":       (lambda v: _num(v, float, "Partial trigger", 0.05, 20.0), "Partial Trigger", False),
    "partial_close_fraction": (lambda v: _num(v, float, "Partial fraction", 0.01, 0.95), "Partial Fraction", False),
    "max_spread_points":     (lambda v: _num(v, int, "Max spread", 1, 100000), "Max Spread", False),
    "pip_points":            (lambda v: _num(v, int, "Pip size", 0, 10000), "Pip Size", False),
}


class RuntimeSettings:
    """Locked dict of runtime settings, persisted as JSON."""

    def __init__(self, defaults, path, on_change=None):
        self._lock = threading.RLock()
        self._defaults = dict(defaults)
        self._path = Path(path)
        self._on_change = on_change
        self._values = dict(defaults)
        self.load()

    # ------------------------------------------------------------ persistence
    def load(self):
        """Merge the JSON file over the defaults. Bad values fall back safely."""
        problems = []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
        except FileNotFoundError:
            self.save()
            return problems
        except Exception as exc:
            return [f"{self._path.name} unreadable ({exc}) - using defaults"]

        if not isinstance(stored, dict):
            return [f"{self._path.name} is not a JSON object - using defaults"]

        with self._lock:
            for key, value in stored.items():
                if key not in self._defaults:
                    problems.append(f"unknown setting '{key}' ignored")
                    continue
                try:
                    self._values[key] = VALIDATORS[key][0](value)
                except SettingError as exc:
                    problems.append(f"{exc} - kept default {self._defaults[key]}")
            problems.extend(self._repair())
        return problems

    def save(self):
        """Atomic write so a crash mid-save cannot corrupt the file."""
        with self._lock:
            data = dict(self._values)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=4, sort_keys=True)
            os.replace(tmp, self._path)
            return True
        except Exception as exc:
            print(f"[runtime_settings] save failed: {exc}")
            return False

    def _repair(self):
        """Keep cross-field invariants true (caller holds the lock)."""
        problems = []
        if self._values["sl_points_min"] >= self._values["sl_points_max"]:
            problems.append(
                f"SL min ({self._values['sl_points_min']}) >= SL max "
                f"({self._values['sl_points_max']}) - restored defaults"
            )
            self._values["sl_points_min"] = self._defaults["sl_points_min"]
            self._values["sl_points_max"] = self._defaults["sl_points_max"]
        return problems

    # -------------------------------------------------------------- accessors
    def snapshot(self):
        """A consistent copy for one trading cycle."""
        with self._lock:
            return dict(self._values)

    def get(self, key):
        with self._lock:
            return self._values[key]

    def defaults(self):
        return dict(self._defaults)

    @staticmethod
    def label(key):
        entry = VALIDATORS.get(key)
        return entry[1] if entry else key

    @staticmethod
    def needs_confirmation(key):
        entry = VALIDATORS.get(key)
        return bool(entry and entry[2])

    @staticmethod
    def coerce(key, value):
        """Validate a value without applying it. Raises SettingError."""
        if key not in VALIDATORS:
            raise SettingError(f"unknown setting '{key}'")
        return VALIDATORS[key][0](value)

    @staticmethod
    def display(key, value):
        """Format a value the way the Telegram panel shows it."""
        if key == "tp_mode":
            return TP_MODE_LABELS.get(value, str(value))
        if key == "lot_mode":
            return LOT_MODE_LABELS.get(value, str(value))
        if key == "sl_mode":
            return SL_MODE_LABELS.get(value, str(value))
        if key in ("breakeven_enabled", "partial_close_enabled"):
            return "ON" if value else "OFF"
        if key == "partial_close_fraction":
            return f"{float(value) * 100:.0f}%"
        if key == "risk_percent":
            return f"{value}%"
        if key in ("min_rr", "custom_rr", "breakeven_r", "partial_close_r"):
            return f"{float(value)}R"
        if key in ("sl_points_min", "sl_points_max", "sl_fixed_points",
                   "max_spread_points"):
            return f"{value} pts"
        if key == "pip_points":
            return "AUTO" if not value else f"{value} pts"
        return str(value)

    # ---------------------------------------------------------------- mutation
    def precheck(self, key, value):
        """
        Full validation - range and cross-field - without applying anything.

        Used before a confirmation screen is shown, so an impossible value is
        refused up front rather than after the user confirms it.
        """
        new = self.coerce(key, value)
        with self._lock:
            if key == "sl_points_min" and new >= self._values["sl_points_max"]:
                raise SettingError(
                    f"SL min ({new}) must be below SL max "
                    f"({self._values['sl_points_max']})"
                )
            if key == "sl_points_max" and new <= self._values["sl_points_min"]:
                raise SettingError(
                    f"SL max ({new}) must be above SL min "
                    f"({self._values['sl_points_min']})"
                )
        return new

    def set(self, key, value):
        """
        Validate, apply and persist one setting.

        Returns (changed, message, old_value, new_value).
        Raises SettingError for invalid input - callers surface it to the user.
        """
        new = self.precheck(key, value)
        with self._lock:
            old = self._values.get(key)
            if old == new:
                return False, f"{self.label(key)} is already {self.display(key, new)}", old, new
            self._values[key] = new
            self.save()

        if self._on_change:
            try:
                self._on_change(key, old, new)
            except Exception as exc:
                print(f"[runtime_settings] change hook failed: {exc}")
        return True, (f"{self.label(key)}: {self.display(key, old)} → "
                      f"{self.display(key, new)}"), old, new

    def reset(self):
        """Restore the original configuration (the values shipped in .env/source)."""
        with self._lock:
            old = dict(self._values)
            self._values = dict(self._defaults)
            self.save()
        changed = {k: (old[k], self._values[k]) for k in self._values
                   if old.get(k) != self._values[k]}
        if self._on_change:
            try:
                self._on_change("*", old, dict(self._values))
            except Exception as exc:
                print(f"[runtime_settings] change hook failed: {exc}")
        return changed
