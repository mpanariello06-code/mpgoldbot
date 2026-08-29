"""
Thread-safe runtime settings manager.

Telegram-controlled settings live here (and in DATA_DIRECTORY/runtime_settings.json)
so they survive a restart. Secrets never enter this file - MT5 credentials and
the Telegram token stay in .env.

The ladder engine takes a consistent snapshot() at the start of every
reconciliation pass, so a Telegram change landing mid-pass can never produce a
half-updated ladder.
"""

import json
import os
import threading
from pathlib import Path

# --- allowed values -------------------------------------------------------
TP_MODES = ("distance", "1_pip", "2_pips", "3_pips", "4_pips", "5_pips")
ROLL_MODES = ("extend", "static")
DIRECTION_MODES = ("off", "both", "buy_bias", "sell_bias", "none")
TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1")

TP_MODE_LABELS = {
    "distance": "DISTANCE",
    "1_pip": "1 PIP",
    "2_pips": "2 PIPS",
    "3_pips": "3 PIPS",
    "4_pips": "4 PIPS",
    "5_pips": "5 PIPS",
}
ROLL_MODE_LABELS = {"extend": "ROLLING", "static": "STATIC GRID"}
DIRECTION_LABELS = {
    "off": "OFF (both sides)",
    "both": "BOTH",
    "buy_bias": "BUY ONLY",
    "sell_bias": "SELL ONLY",
    "none": "NO NEW ENTRIES",
}


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


def _upper_choice(value, options, name):
    value = str(value).strip().upper()
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
    # --- ladder ---
    "ladder_spacing":      (lambda v: _num(v, float, "Ladder spacing", 0.01, 1000.0), "Spacing", True),
    "ladder_depth":        (lambda v: _num(v, int, "Ladder depth", 1, 50), "Depth", True),
    "first_level_offset":  (lambda v: _num(v, float, "First level offset", 0.0, 1000.0), "First Level Offset", False),
    "roll_mode":           (lambda v: _choice(v, ROLL_MODES, "Roll mode"), "Roll Mode", True),
    "rearm_levels":        (lambda v: _flag(v, "Re-arm levels"), "Re-arm Levels", False),
    # --- take profit ---
    "tp_mode":             (lambda v: _choice(v, TP_MODES, "TP mode"), "TP Mode", True),
    "tp_distance":         (lambda v: _num(v, float, "TP distance", 0.01, 1000.0), "TP Distance", True),
    "stop_loss_distance":  (lambda v: _num(v, float, "Stop loss distance", 0.0, 10000.0), "Stop Loss", True),
    "pip_points":          (lambda v: _num(v, int, "Pip size", 0, 10000), "Pip Size", False),
    # --- cycle ---
    "profit_cycle_target": (lambda v: _num(v, int, "Profit cycle target", 0, 100), "Profit Cycle", True),
    "cycle_close_positions": (lambda v: _flag(v, "Close positions on cycle end"), "Close On Cycle End", True),
    "cycle_take_profit_money": (lambda v: _num(v, float, "Cycle basket target", 0.0, 1e6), "Cycle Basket TP", False),
    # --- risk ---
    "lot_size":            (lambda v: _num(v, float, "Lot size", 0.001, 100.0), "Lot Size", True),
    "max_lot_size":        (lambda v: _num(v, float, "Max lot size", 0.001, 100.0), "Max Lot", True),
    "max_open_positions":  (lambda v: _num(v, int, "Max open positions", 1, 200), "Max Open", True),
    "max_pending_orders":  (lambda v: _num(v, int, "Max pending orders", 1, 200), "Max Pending", True),
    "max_spread":          (lambda v: _num(v, float, "Max spread", 0.0, 100.0), "Max Spread", False),
    "max_slippage":        (lambda v: _num(v, int, "Max slippage", 0, 10000), "Max Slippage", False),
    "max_daily_loss":      (lambda v: _num(v, float, "Max daily loss", 0.0, 1e6), "Daily Loss", True),
    "max_cycle_loss":      (lambda v: _num(v, float, "Max cycle loss", 0.0, 1e6), "Cycle Loss", True),
    "max_consecutive_losing_cycles": (lambda v: _num(v, int, "Max losing cycles", 0, 100), "Losing Cycles", False),
    "cooldown_after_loss_minutes": (lambda v: _num(v, float, "Cooldown after loss", 0.0, 1440.0), "Cooldown", False),
    # --- hygiene ---
    "order_max_age_seconds": (lambda v: _num(v, float, "Order max age", 0.0, 86400.0), "Order Max Age", False),
    "m5_candle_reset":     (lambda v: _flag(v, "Candle reset"), "Candle Reset", False),
    # --- context / direction ---
    "timeframe":           (lambda v: _upper_choice(v, TIMEFRAMES, "Timeframe"), "Timeframe", False),
    "direction_filter":    (lambda v: _choice(v, DIRECTION_MODES, "Direction filter"), "Direction", True),
}

PRICE_KEYS = ("ladder_spacing", "tp_distance", "first_level_offset",
              "stop_loss_distance", "max_spread")
MONEY_KEYS = ("max_daily_loss", "max_cycle_loss", "cycle_take_profit_money")


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
        if self._values["lot_size"] > self._values["max_lot_size"]:
            problems.append(
                f"lot_size ({self._values['lot_size']}) above max_lot_size "
                f"({self._values['max_lot_size']}) - restored defaults"
            )
            self._values["lot_size"] = self._defaults["lot_size"]
            self._values["max_lot_size"] = self._defaults["max_lot_size"]
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
        if key == "roll_mode":
            return ROLL_MODE_LABELS.get(value, str(value))
        if key == "direction_filter":
            return DIRECTION_LABELS.get(value, str(value))
        if isinstance(value, bool):
            return "ON" if value else "OFF"
        if key in PRICE_KEYS:
            return "OFF" if not value else f"{float(value):g}"
        if key in MONEY_KEYS:
            return "OFF" if not value else f"${float(value):,.2f}"
        if key == "pip_points":
            return "AUTO" if not value else f"{value} pts"
        if key == "cooldown_after_loss_minutes":
            return "OFF" if not value else f"{float(value):g} min"
        if key == "order_max_age_seconds":
            return "OFF" if not value else f"{float(value):g}s"
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
            if key == "lot_size" and new > self._values["max_lot_size"]:
                raise SettingError(
                    f"Lot size ({new}) must not exceed MAX LOT "
                    f"({self._values['max_lot_size']})"
                )
            if key == "max_lot_size" and new < self._values["lot_size"]:
                raise SettingError(
                    f"Max lot ({new}) must not be below the lot size "
                    f"({self._values['lot_size']})"
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
