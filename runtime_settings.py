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
# "none" is the basket mode and the default: ladder positions carry NO
# individual take profit, and the whole cycle is closed as one basket by the
# exit engine. The other modes attach a per-trade TP and are kept for anyone
# who wants the old per-trade behaviour.
TP_MODES = ("none", "levels", "distance",
            "1_pip", "2_pips", "3_pips", "4_pips", "5_pips")
ROLL_MODES = ("extend", "static")
DIRECTION_MODES = ("off", "both", "buy_bias", "sell_bias", "none")
TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1")

# Bumped when a change to the DEFAULTS has to reach existing installations.
# 2 = the basket architecture: ladder positions carry no individual TP.
SCHEMA_KEY = "_schema"
BASKET_SCHEMA = 2

TP_MODE_LABELS = {
    "none": "NONE (BASKET)",
    "levels": "LEVELS",
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
    "tp_levels":           (lambda v: _num(v, int, "TP levels", 1, 20), "TP Levels", True),
    "tp_distance":         (lambda v: _num(v, float, "TP distance", 0.01, 1000.0), "TP Distance", True),
    "stop_loss_distance":  (lambda v: _num(v, float, "Stop loss distance", 0.0, 10000.0), "Stop Loss", True),
    "pip_points":          (lambda v: _num(v, int, "Pip size", 0, 10000), "Pip Size", False),
    # --- cycle ---
    "cycle_close_positions": (lambda v: _flag(v, "Close positions on cycle end"), "Close On Cycle End", True),
    "profit_fallback_enabled": (lambda v: _flag(v, "Profit fallback"), "Profit Fallback", True),
    "profit_fallback_buffer_levels": (lambda v: _num(v, float, "Profit buffer", 0.01, 50.0), "Profit Buffer", True),
    "profit_confirmation_seconds": (lambda v: _num(v, float, "Profit confirmation", 0.0, 3600.0), "Profit Confirmation", False),
    "profit_fallback_continuation_guard": (lambda v: _num(v, float, "Continuation guard", 0.0, 1.0), "Continuation Guard", False),
    # --- risk ---
    "lot_size":            (lambda v: _num(v, float, "Lot size", 0.001, 100.0), "Lot Size", True),
    "max_lot_size":        (lambda v: _num(v, float, "Max lot size", 0.001, 100.0), "Max Lot", True),
    "max_open_positions":  (lambda v: _num(v, int, "Max open positions", 1, 200), "Max Open", True),
    "max_pending_orders":  (lambda v: _num(v, int, "Max pending orders", 1, 200), "Max Pending", True),
    "max_ladder_depth":    (lambda v: _num(v, int, "Max ladder depth", 1, 200), "Max Depth", True),
    "max_spread":          (lambda v: _num(v, float, "Max spread", 0.0, 100.0), "Max Spread", False),
    "max_slippage":        (lambda v: _num(v, int, "Max slippage", 0, 10000), "Max Slippage", False),
    "max_daily_drawdown":  (lambda v: _num(v, float, "Max daily drawdown", 0.0, 1e6), "Daily Drawdown", True),
    "max_cycle_drawdown":  (lambda v: _num(v, float, "Max cycle drawdown", 0.0, 1e6), "Cycle Drawdown", True),
    "max_consecutive_losing_cycles": (lambda v: _num(v, int, "Max losing cycles", 0, 100), "Losing Cycles", False),
    "cooldown_after_loss_minutes": (lambda v: _num(v, float, "Cooldown after loss", 0.0, 1440.0), "Cooldown", False),
    "cycle_reentry_cooldown_seconds": (lambda v: _num(v, float, "Cycle re-entry cooldown", 0.0, 3600.0), "Re-entry Cooldown", False),
    "max_cycle_duration_minutes": (lambda v: _num(v, float, "Max cycle duration", 0.0, 10080.0), "Max Cycle Duration", True),
    # --- hygiene ---
    "order_max_age_seconds": (lambda v: _num(v, float, "Order max age", 0.0, 86400.0), "Order Max Age", False),
    "m5_candle_reset":     (lambda v: _flag(v, "Candle reset"), "Candle Reset", False),
    # --- context / direction ---
    "timeframe":           (lambda v: _upper_choice(v, TIMEFRAMES, "Timeframe"), "Timeframe", False),
    "direction_filter":    (lambda v: _choice(v, DIRECTION_MODES, "Direction filter"), "Direction", True),

    # --- telegram notification policy ---
    "telegram_entry_alerts":  (lambda v: _flag(v, "Entry alerts"), "Entry Alerts", False),
    "telegram_state_alerts":  (lambda v: _flag(v, "State alerts"), "State Alerts", False),
    "telegram_status_updates": (lambda v: _flag(v, "Status updates"), "Status Updates", False),
    "telegram_status_interval_minutes": (lambda v: _num(v, float, "Status interval", 1.0, 1440.0), "Status Interval", False),
    "telegram_error_throttle_seconds": (lambda v: _num(v, float, "Error throttle", 0.0, 86400.0), "Error Throttle", False),

    # --- adaptive exit engine (all fittable against historical data) ---
    "exit_threshold_exit":    (lambda v: _num(v, float, "Exit threshold", 1.0, 100.0), "Exit Score", True),
    "exit_threshold_monitor": (lambda v: _num(v, float, "Monitor threshold", 0.0, 99.0), "Monitor Score", False),
    "exit_w_reversal":     (lambda v: _num(v, float, "Reversal weight", 0.0, 3.0), "Reversal Weight", False),
    "exit_w_exhaustion":   (lambda v: _num(v, float, "Exhaustion weight", 0.0, 3.0), "Exhaustion Weight", False),
    "exit_w_directional":  (lambda v: _num(v, float, "Directional weight", 0.0, 3.0), "Directional Weight", False),
    "exit_w_extended":     (lambda v: _num(v, float, "Extended weight", 0.0, 3.0), "Extended Weight", False),
    "exit_min_triggers_for_directional": (lambda v: _num(v, int, "Min triggers (directional)", 1, 50), "Min Directional Triggers", False),
    "exit_min_triggers_for_extended": (lambda v: _num(v, int, "Min triggers (extended)", 1, 50), "Min Extended Triggers", False),
    "exit_w_continuation": (lambda v: _num(v, float, "Continuation weight", 0.0, 3.0), "Continuation Weight", False),
    "exit_w_depth":        (lambda v: _num(v, float, "Depth weight", 0.0, 3.0), "Depth Weight", False),
    "exit_w_drawdown":     (lambda v: _num(v, float, "Drawdown weight", 0.0, 3.0), "Drawdown Weight", False),
    "exit_w_harvest":      (lambda v: _num(v, float, "Harvest weight", 0.0, 3.0), "Harvest Weight", False),
    "exit_w_loss_hold":    (lambda v: _num(v, float, "Loss hold weight", 0.0, 3.0), "Loss Hold Weight", False),
    "exit_recent_window":  (lambda v: _num(v, int, "Recent window", 1, 50), "Recent Window", False),
    "exit_progress_intervals": (lambda v: _num(v, int, "Progress intervals", 1, 20), "Progress Intervals", False),
    "exit_consecutive_norm": (lambda v: _num(v, float, "Consecutive norm", 1.0, 50.0), "Consecutive Norm", False),
    "exit_depth_norm":     (lambda v: _num(v, float, "Depth norm", 1.0, 50.0), "Depth Norm", False),
    "exit_gap_reference":  (lambda v: _num(v, float, "Gap reference", 1.0, 3600.0), "Gap Reference", False),
    "exit_min_triggers_for_exhaustion": (lambda v: _num(v, int, "Min triggers (exhaustion)", 1, 50), "Min Exhaustion Triggers", False),
    "exit_min_triggers_for_reversal": (lambda v: _num(v, int, "Min triggers (reversal)", 1, 50), "Min Reversal Triggers", False),
}

PRICE_KEYS = ("ladder_spacing", "tp_distance", "first_level_offset",
              "stop_loss_distance", "max_spread")
MONEY_KEYS = ("max_daily_drawdown", "max_cycle_drawdown")


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
            # A settings file written before the basket architecture carries a
            # per-trade tp_mode as a leftover DEFAULT, not as a choice anyone
            # made. Migrating it is the difference between shipping the basket
            # strategy and silently shipping the old one.
            migrated = None
            if int(stored.get(SCHEMA_KEY, 0)) < BASKET_SCHEMA and \
                    stored.get("tp_mode") not in (None, "none"):
                migrated = stored.get("tp_mode")
                stored = dict(stored)
                stored["tp_mode"] = self._defaults.get("tp_mode", "none")
            for key, value in stored.items():
                if key == SCHEMA_KEY:
                    continue
                if key not in self._defaults:
                    problems.append(f"unknown setting '{key}' ignored")
                    continue
                try:
                    self._values[key] = VALIDATORS[key][0](value)
                except SettingError as exc:
                    problems.append(f"{exc} - kept default {self._defaults[key]}")
            problems.extend(self._repair())
        if migrated:
            problems.append(
                f"tp_mode '{migrated}' was an old default and attaches an "
                f"individual TP to every ladder position - migrated to "
                f"'{self._values['tp_mode']}' (the cycle is closed as one "
                f"basket). Set it back under SETTINGS if that was deliberate")
            self.save()
        return problems

    def save(self):
        """Atomic write so a crash mid-save cannot corrupt the file."""
        with self._lock:
            data = dict(self._values)
            data[SCHEMA_KEY] = BASKET_SCHEMA
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
        if key in ("cooldown_after_loss_minutes", "max_cycle_duration_minutes"):
            return "OFF" if not value else f"{float(value):g} min"
        if key in ("profit_confirmation_seconds", "cycle_reentry_cooldown_seconds"):
            return "OFF" if not value else f"{float(value):g}s"
        if key == "profit_fallback_buffer_levels":
            return f"{float(value):g} levels"
        if key == "telegram_status_interval_minutes":
            return f"{float(value):g} min"
        if key == "telegram_error_throttle_seconds":
            return "OFF" if not value else f"{float(value):g}s"
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
            if key == "exit_threshold_exit" and \
                    new <= self._values["exit_threshold_monitor"]:
                raise SettingError(
                    f"Exit score ({new:g}) must be above the monitor score "
                    f"({self._values['exit_threshold_monitor']:g})"
                )
            if key == "exit_threshold_monitor" and \
                    new >= self._values["exit_threshold_exit"]:
                raise SettingError(
                    f"Monitor score ({new:g}) must be below the exit score "
                    f"({self._values['exit_threshold_exit']:g})"
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
