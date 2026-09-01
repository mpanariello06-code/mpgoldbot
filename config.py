"""
Central configuration layer.

Responsibilities
----------------
* Load the .env file (python-dotenv)
* Parse environment variables into proper Python types
* Apply defaults
* Validate the configuration
* Expose configuration to the rest of the application

Secrets (MT5 credentials, Telegram token) live only here / in .env. The
Telegram-adjustable trading parameters start from `runtime_defaults()` and are
then owned by runtime_settings.py.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# override=False -> real environment variables win over the .env file.
load_dotenv(dotenv_path=ENV_FILE, override=False)


# ---------------------------------------------------------------------------
# Typed parsing helpers
# ---------------------------------------------------------------------------
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _raw(name, default=""):
    value = os.getenv(name)
    return default if value is None else value.strip()


def _get_str(name, default=""):
    value = _raw(name, default)
    return value if value != "" else default


def _get_int(name, default):
    value = _raw(name, "")
    if value == "":
        return default
    try:
        return int(float(value))
    except ValueError:
        raise ValueError(f"Invalid integer for {name}: {value!r}")


def _get_float(name, default):
    value = _raw(name, "")
    if value == "":
        return default
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"Invalid float for {name}: {value!r}")


def _get_bool(name, default):
    value = _raw(name, "").lower()
    if value == "":
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"Invalid boolean for {name}: {value!r}")


def _get_int_list(name, default=None):
    value = _raw(name, "")
    if value == "":
        return list(default or [])
    out = []
    for chunk in value.replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            raise ValueError(f"Invalid chat id in {name}: {chunk!r}")
    return out


# ---------------------------------------------------------------------------
# MT5 ACCOUNT
# ---------------------------------------------------------------------------
LOGIN = _get_int("MT5_LOGIN", 0)          # Leave 0 to use current MT5 account
PASSWORD = _get_str("MT5_PASSWORD", "")   # Only used if LOGIN > 0
SERVER = _get_str("MT5_SERVER", "")       # Exact server name from MT5
MT5_PATH = _get_str("MT5_PATH", "")       # Optional explicit terminal64.exe path

POSSIBLE_MT5_PATHS = [
    "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
    "C:\\Program Files\\Exness MetaTrader 5\\terminal64.exe",
    "C:\\Program Files (x86)\\MetaTrader 5\\terminal64.exe",
    "C:\\Program Files (x86)\\Exness MetaTrader 5\\terminal64.exe",
]

# ---------------------------------------------------------------------------
# MARKET
# ---------------------------------------------------------------------------
SYMBOL = _get_str("SYMBOL", "XAUUSD")
TIMEFRAME = _get_str("TIMEFRAME", "M5").upper()

# ---------------------------------------------------------------------------
# TRADING MODE
# ---------------------------------------------------------------------------
# PAPER (default) simulates fills from live ticks and sends nothing to the
# broker. Switch to LIVE only after watching PAPER behave.
TRADING_MODE = _get_str("TRADING_MODE", "PAPER").upper()
PAPER_START_BALANCE = _get_float("PAPER_START_BALANCE", 10000.0)

# ---------------------------------------------------------------------------
# ROLLING LADDER
# ---------------------------------------------------------------------------
LADDER_SPACING = _get_float("LADDER_SPACING", 0.30)     # price units
LADDER_DEPTH = _get_int("LADDER_DEPTH", 5)              # levels per side
# Nearest level distance from price; the broker's minimum stop distance always
# wins when it is larger.
FIRST_LEVEL_OFFSET = _get_float("FIRST_LEVEL_OFFSET", LADDER_SPACING)
# extend = the ladder rolls with price (levels re-created ahead of the market)
# static = the grid is fixed for the cycle and consumed as price crosses it
ROLL_MODE = _get_str("ROLL_MODE", "extend").lower()
REARM_LEVELS = _get_bool("REARM_LEVELS", True)

# ---------------------------------------------------------------------------
# TAKE PROFIT
# ---------------------------------------------------------------------------
# none (default) = NO individual take profit. Ladder positions stay open and
# the whole cycle is closed as one basket by the exit engine - this is the
# basket architecture the strategy is built on.
# levels = TP_LEVELS x LADDER_SPACING | distance = TP_DISTANCE price units |
# 1_pip..5_pips = symbol-aware pips. Those attach a per-trade TP and turn each
# ladder level back into an independent trade.
TP_MODE = _get_str("TP_MODE", "none").lower()
TP_LEVELS = _get_int("TP_LEVELS", 1)
TP_DISTANCE = _get_float("TP_DISTANCE", LADDER_SPACING)
STOP_LOSS_DISTANCE = _get_float("STOP_LOSS_DISTANCE", 0.0)   # 0 = no SL
# 1 pip = N points; 0 = derive from the symbol's digits/point
PIP_POINTS = _get_int("PIP_POINTS", 0)

# ---------------------------------------------------------------------------
# CYCLE / ADAPTIVE EXIT
# ---------------------------------------------------------------------------
# The cycle ends when the exit engine reads reversal or exhaustion in the
# trigger sequence - never on a trade count and never on a dollar target.
# Every weight and threshold below is a starting value to be fitted against
# historical XAUUSD data, not a discovered truth.
CYCLE_CLOSE_POSITIONS = _get_bool("CYCLE_CLOSE_POSITIONS", True)

# --- profit recovery fallback ---
# A basket that quietly came good is taken rather than held forever waiting for
# a scenario that may never arrive. NOT a dollar target: the buffer is a
# fraction of what one ladder level is worth, so it scales with lot and
# spacing, and the profit must hold for the confirmation period.
PROFIT_FALLBACK_ENABLED = _get_bool("PROFIT_FALLBACK_ENABLED", True)
PROFIT_FALLBACK_BUFFER_LEVELS = _get_float("PROFIT_FALLBACK_BUFFER_LEVELS", 0.5)
PROFIT_CONFIRMATION_SECONDS = _get_float("PROFIT_CONFIRMATION_SECONDS", 60.0)
# strong continuation above this is left to run rather than harvested
PROFIT_FALLBACK_CONTINUATION_GUARD = _get_float(
    "PROFIT_FALLBACK_CONTINUATION_GUARD", 0.60)

_EXIT_DEFAULTS = {
    "recent_window": 5,
    "progress_intervals": 2,
    "consecutive_norm": 4.0,
    "depth_norm": 6.0,
    "gap_reference": 60.0,
    "min_triggers_for_exhaustion": 3,
    "min_triggers_for_reversal": 2,
    "min_triggers_for_directional": 3,
    "min_triggers_for_extended": 5,
    "w_reversal": 0.75,
    "w_exhaustion": 0.45,
    "w_directional": 0.65,
    "w_extended": 0.50,
    "w_depth": 0.20,
    "w_drawdown": 0.25,
    "w_continuation": 0.45,
    "w_harvest": 0.30,
    "w_loss_hold": 0.20,
    "threshold_exit": 70.0,
    "threshold_monitor": 40.0,
}


def _exit_settings():
    """EXIT_<FIELD> in .env overrides any exit-engine parameter."""
    out = {}
    for key, default in _EXIT_DEFAULTS.items():
        env = f"EXIT_{key.upper()}"
        if isinstance(default, bool):
            out[f"exit_{key}"] = _get_bool(env, default)
        elif isinstance(default, int):
            out[f"exit_{key}"] = _get_int(env, default)
        else:
            out[f"exit_{key}"] = _get_float(env, default)
    return out

# ---------------------------------------------------------------------------
# RISK
# ---------------------------------------------------------------------------
LOT_SIZE = _get_float("LOT_SIZE", 0.01)                 # fixed lots, no martingale
MAX_LOT_SIZE = _get_float("MAX_LOT_SIZE", 0.10)
# The basket accumulates: with no individual TP, positions stay open until the
# whole cycle is closed, so this has to allow a full ladder. Too low and the
# ladder stops after N triggers, which is the "N trades = exit" rule the
# strategy explicitly does not have.
MAX_OPEN_POSITIONS = _get_int("MAX_OPEN_POSITIONS", 12)
MAX_PENDING_ORDERS = _get_int("MAX_PENDING_ORDERS", 10)
MAX_LADDER_DEPTH = _get_int("MAX_LADDER_DEPTH", 12)     # levels used per cycle
MAX_SPREAD = _get_float("MAX_SPREAD", 0.50)             # price units, 0 = off
MAX_SLIPPAGE = _get_int("MAX_SLIPPAGE", 20)             # deviation points
# Round-turn commission per lot in account currency (paper/replay costing)
COMMISSION_PER_LOT = _get_float("COMMISSION_PER_LOT", 0.0)
# Drawdown guards, in account currency (0 = off). MAX_DAILY_LOSS /
# MAX_CYCLE_LOSS are accepted as aliases.
MAX_DAILY_DRAWDOWN = _get_float("MAX_DAILY_DRAWDOWN",
                                _get_float("MAX_DAILY_LOSS", 50.0))
MAX_CYCLE_DRAWDOWN = _get_float("MAX_CYCLE_DRAWDOWN",
                                _get_float("MAX_CYCLE_LOSS", 20.0))
MAX_CONSECUTIVE_LOSING_CYCLES = _get_int("MAX_CONSECUTIVE_LOSING_CYCLES", 3)
COOLDOWN_AFTER_LOSS = _get_float("COOLDOWN_AFTER_LOSS", 15.0)   # minutes
# Mandatory settle time between one cycle closing and the next ladder going
# out. It applies AFTER a complete cycle exit only - never between ladder
# levels, triggers or orders inside a running cycle. 0 = re-enter immediately.
CYCLE_REENTRY_COOLDOWN = _get_float("CYCLE_REENTRY_COOLDOWN", 10.0)   # seconds
# No cycle may stay open forever: past this it is closed as RISK_TIMEOUT
MAX_CYCLE_DURATION = _get_float("MAX_CYCLE_DURATION", 120.0)    # minutes, 0 = off

# ---------------------------------------------------------------------------
# ORDER HYGIENE
# ---------------------------------------------------------------------------
ORDER_MAX_AGE = _get_float("ORDER_MAX_AGE", 900.0)      # seconds, 0 = off
M5_CANDLE_RESET = _get_bool("M5_CANDLE_RESET", False)   # re-anchor each M5 close

# ---------------------------------------------------------------------------
# DIRECTION FILTER (optional, disabled by default)
# ---------------------------------------------------------------------------
# off | both | buy_bias | sell_bias | none
DIRECTION_FILTER = _get_str("DIRECTION_FILTER", "off").lower()

# ---------------------------------------------------------------------------
# ENGINE
# ---------------------------------------------------------------------------
MAGIC = _get_int("MAGIC", 88001199)
POLL_SECONDS = _get_float("POLL_SECONDS", 0.5)
DIAGNOSTICS = _get_bool("DIAGNOSTICS", True)

# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _get_str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _get_str("TELEGRAM_CHAT_ID", "")
TELEGRAM_ALLOWED_CHAT_IDS = _get_int_list("TELEGRAM_ALLOWED_CHAT_IDS", [])

AUTHORIZED_CHAT_IDS = []
if TELEGRAM_CHAT_ID:
    try:
        AUTHORIZED_CHAT_IDS.append(int(TELEGRAM_CHAT_ID))
    except ValueError:
        raise ValueError(f"Invalid TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID!r}")
for _cid in TELEGRAM_ALLOWED_CHAT_IDS:
    if _cid not in AUTHORIZED_CHAT_IDS:
        AUTHORIZED_CHAT_IDS.append(_cid)

TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN)
TELEGRAM_NOTIFICATIONS = _get_bool("TELEGRAM_NOTIFICATIONS", True)
# Telegram is event-based: cycles, state transitions, risk and errors. Per-entry
# pings are off by default - this strategy would flood the chat.
TELEGRAM_ENTRY_ALERTS = _get_bool("TELEGRAM_ENTRY_ALERTS", False)
TELEGRAM_STATE_ALERTS = _get_bool("TELEGRAM_STATE_ALERTS", True)
TELEGRAM_STATUS_UPDATES = _get_bool("TELEGRAM_STATUS_UPDATES", True)
TELEGRAM_STATUS_INTERVAL = _get_float("TELEGRAM_STATUS_INTERVAL", 20.0)  # minutes
TELEGRAM_ERROR_THROTTLE = _get_float("TELEGRAM_ERROR_THROTTLE", 300.0)   # seconds

# ---------------------------------------------------------------------------
# DATA / CSV PERSISTENCE
# ---------------------------------------------------------------------------
DATA_DIRECTORY = _get_str("DATA_DIRECTORY", "data")
TRADE_LOG_FILE = _get_str("TRADE_LOG_FILE", "trades.csv")
EVENT_LOG_FILE = _get_str("EVENT_LOG_FILE", "events.csv")
ACCOUNT_LOG_FILE = _get_str("ACCOUNT_LOG_FILE", "account_snapshots.csv")
LADDER_LOG_FILE = _get_str("LADDER_LOG_FILE", "rolling_ladder_events.csv")
CYCLE_LOG_FILE = _get_str("CYCLE_LOG_FILE", "rolling_ladder_cycles.csv")

RUNTIME_SETTINGS_FILE = _get_str("RUNTIME_SETTINGS_FILE", "runtime_settings.json")
LADDER_STATE_FILE = _get_str("LADDER_STATE_FILE", "ladder_state.json")
PAPER_STATE_FILE = _get_str("PAPER_STATE_FILE", "paper_state.json")

ACCOUNT_SNAPSHOT_INTERVAL = _get_int("ACCOUNT_SNAPSHOT_INTERVAL", 300)

DATA_PATH = Path(DATA_DIRECTORY)
if not DATA_PATH.is_absolute():
    DATA_PATH = BASE_DIR / DATA_PATH

# ---------------------------------------------------------------------------
# RUNTIME / LIFECYCLE
# ---------------------------------------------------------------------------
AUTO_START_TRADING = _get_bool("AUTO_START_TRADING", True)
MT5_RECONNECT_SECONDS = _get_int("MT5_RECONNECT_SECONDS", 30)


# ---------------------------------------------------------------------------
# Runtime (Telegram-adjustable) defaults
# ---------------------------------------------------------------------------
def runtime_defaults():
    """
    Starting point for the Telegram-controlled settings, and the target of
    RESET SETTINGS.
    """
    return {
        "timeframe": TIMEFRAME,
        # ladder
        "ladder_spacing": LADDER_SPACING,
        "ladder_depth": LADDER_DEPTH,
        "first_level_offset": FIRST_LEVEL_OFFSET,
        "roll_mode": ROLL_MODE,
        "rearm_levels": REARM_LEVELS,
        # take profit
        "tp_mode": TP_MODE,
        "tp_levels": TP_LEVELS,
        "tp_distance": TP_DISTANCE,
        "stop_loss_distance": STOP_LOSS_DISTANCE,
        "pip_points": PIP_POINTS,
        # cycle / adaptive exit
        "cycle_close_positions": CYCLE_CLOSE_POSITIONS,
        "profit_fallback_enabled": PROFIT_FALLBACK_ENABLED,
        "profit_fallback_buffer_levels": PROFIT_FALLBACK_BUFFER_LEVELS,
        "profit_confirmation_seconds": PROFIT_CONFIRMATION_SECONDS,
        "profit_fallback_continuation_guard": PROFIT_FALLBACK_CONTINUATION_GUARD,
        # risk
        "lot_size": LOT_SIZE,
        "max_lot_size": MAX_LOT_SIZE,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_pending_orders": MAX_PENDING_ORDERS,
        "max_ladder_depth": MAX_LADDER_DEPTH,
        "max_spread": MAX_SPREAD,
        "max_slippage": MAX_SLIPPAGE,
        "max_daily_drawdown": MAX_DAILY_DRAWDOWN,
        "max_cycle_drawdown": MAX_CYCLE_DRAWDOWN,
        "max_consecutive_losing_cycles": MAX_CONSECUTIVE_LOSING_CYCLES,
        "cooldown_after_loss_minutes": COOLDOWN_AFTER_LOSS,
        "cycle_reentry_cooldown_seconds": CYCLE_REENTRY_COOLDOWN,
        "max_cycle_duration_minutes": MAX_CYCLE_DURATION,
        # hygiene
        "order_max_age_seconds": ORDER_MAX_AGE,
        "m5_candle_reset": M5_CANDLE_RESET,
        # direction
        "direction_filter": DIRECTION_FILTER,
        # telegram policy
        "telegram_entry_alerts": TELEGRAM_ENTRY_ALERTS,
        "telegram_state_alerts": TELEGRAM_STATE_ALERTS,
        "telegram_status_updates": TELEGRAM_STATUS_UPDATES,
        "telegram_status_interval_minutes": TELEGRAM_STATUS_INTERVAL,
        "telegram_error_throttle_seconds": TELEGRAM_ERROR_THROTTLE,
        **_exit_settings(),
    }


# ---------------------------------------------------------------------------
# Validation & safe reporting
# ---------------------------------------------------------------------------
def validate():
    """Return (errors, warnings). Errors are fatal, warnings are informational."""
    errors = []
    warnings = []

    if not SYMBOL:
        errors.append("SYMBOL is empty - set SYMBOL in .env")

    if LOGIN and LOGIN > 0:
        if not PASSWORD:
            errors.append("MT5_LOGIN is set but MT5_PASSWORD is empty")
        if not SERVER:
            errors.append("MT5_LOGIN is set but MT5_SERVER is empty")
    else:
        warnings.append(
            "MT5_LOGIN is 0 - the bot will use the account already logged into "
            "the running MetaTrader 5 terminal"
        )

    if TRADING_MODE not in ("PAPER", "LIVE"):
        errors.append("TRADING_MODE must be PAPER or LIVE")
    elif TRADING_MODE == "LIVE":
        warnings.append("TRADING_MODE=LIVE - real orders will be sent to the broker")

    if LADDER_SPACING <= 0:
        errors.append("LADDER_SPACING must be greater than 0")
    if LADDER_DEPTH < 1:
        errors.append("LADDER_DEPTH must be at least 1")
    if TP_MODE not in ("none", "levels", "distance", "1_pip", "2_pips",
                       "3_pips", "4_pips", "5_pips"):
        errors.append("TP_MODE must be none, levels, distance or 1_pip..5_pips")
    if TP_MODE != "none":
        warnings.append(
            f"TP_MODE={TP_MODE} attaches an individual take profit to every "
            f"ladder position - the strategy is designed to manage the cycle "
            f"as ONE basket. Use TP_MODE=none unless you want per-trade exits")
    if TP_MODE == "distance" and TP_DISTANCE <= 0:
        errors.append("TP_DISTANCE must be greater than 0")
    if TP_MODE == "levels" and TP_LEVELS < 1:
        errors.append("TP_LEVELS must be at least 1")
    if LOT_SIZE <= 0:
        errors.append("LOT_SIZE must be greater than 0")
    if LOT_SIZE > MAX_LOT_SIZE:
        errors.append(f"LOT_SIZE ({LOT_SIZE}) is above MAX_LOT_SIZE ({MAX_LOT_SIZE})")
    if MAX_OPEN_POSITIONS < 1:
        errors.append("MAX_OPEN_POSITIONS must be at least 1")
    if TP_MODE == "none" and MAX_OPEN_POSITIONS < LADDER_DEPTH:
        warnings.append(
            f"MAX_OPEN_POSITIONS={MAX_OPEN_POSITIONS} is below LADDER_DEPTH="
            f"{LADDER_DEPTH}: with no individual TP the basket will stop "
            f"accumulating before the ladder is fully consumed")
    if MAX_PENDING_ORDERS < 1:
        errors.append("MAX_PENDING_ORDERS must be at least 1")
    if POLL_SECONDS <= 0:
        errors.append("POLL_SECONDS must be greater than 0")
    if ROLL_MODE not in ("extend", "static"):
        errors.append("ROLL_MODE must be 'extend' or 'static'")
    if DIRECTION_FILTER not in ("off", "both", "buy_bias", "sell_bias", "none"):
        errors.append("DIRECTION_FILTER must be off, both, buy_bias, sell_bias or none")

    if MAX_SPREAD <= 0:
        warnings.append("MAX_SPREAD is 0 - the spread filter is disabled")
    if MAX_DAILY_DRAWDOWN <= 0:
        warnings.append("MAX_DAILY_DRAWDOWN is 0 - the daily guard is disabled")
    if MAX_CYCLE_DRAWDOWN <= 0:
        warnings.append("MAX_CYCLE_DRAWDOWN is 0 - the cycle guard is disabled")
    if not CYCLE_CLOSE_POSITIONS:
        warnings.append(
            "CYCLE_CLOSE_POSITIONS=false - positions left running after a cycle "
            "ends keep the next ladder waiting, because only one cycle may be "
            "active at a time")
    if MAX_CYCLE_DURATION <= 0:
        warnings.append(
            "MAX_CYCLE_DURATION is 0 - a cycle can stay open indefinitely")
    exits = _exit_settings()
    if exits["exit_threshold_exit"] <= exits["exit_threshold_monitor"]:
        errors.append("EXIT_THRESHOLD_EXIT must be above EXIT_THRESHOLD_MONITOR")

    if not TELEGRAM_BOT_TOKEN:
        warnings.append(
            "TELEGRAM_BOT_TOKEN is missing - Telegram remote control is DISABLED. "
            "Set TELEGRAM_BOT_TOKEN in .env to enable it."
        )
    elif not AUTHORIZED_CHAT_IDS:
        warnings.append(
            "TELEGRAM_BOT_TOKEN is set but no TELEGRAM_CHAT_ID / "
            "TELEGRAM_ALLOWED_CHAT_IDS given - every chat will be rejected as "
            "Unauthorized."
        )

    return errors, warnings


def strategy_summary():
    """Human readable configuration. Never contains secrets."""
    return {
        "Symbol": SYMBOL,
        "Timeframe": TIMEFRAME,
        "Mode": TRADING_MODE,
        "Spacing": LADDER_SPACING,
        "Depth": LADDER_DEPTH,
        "TP": ("none (basket)" if TP_MODE == "none" else
               f"{TP_LEVELS} level(s)" if TP_MODE == "levels" else (
                   TP_DISTANCE if TP_MODE == "distance" else TP_MODE)),
        "Lot": LOT_SIZE,
        "Exit": f"score >= {_EXIT_DEFAULTS['threshold_exit']:.0f}",
        "Max Positions": MAX_OPEN_POSITIONS,
        "Max Pendings": MAX_PENDING_ORDERS,
        "Max Spread": MAX_SPREAD,
        "Magic": MAGIC,
        "Poll": f"{POLL_SECONDS}s",
    }


def safe_dict():
    """Configuration dump with all secrets masked (safe to print or send)."""
    def mask(value):
        return "***set***" if value else "(empty)"

    data = {
        "MT5_LOGIN": LOGIN if LOGIN else "(current terminal account)",
        "MT5_PASSWORD": mask(PASSWORD),
        "MT5_SERVER": SERVER or "(terminal default)",
        "MT5_PATH": MT5_PATH or "(auto-detect)",
        "TELEGRAM_BOT_TOKEN": mask(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_AUTHORIZED_CHATS": len(AUTHORIZED_CHAT_IDS),
        "DATA_DIRECTORY": str(DATA_PATH),
    }
    data.update(strategy_summary())
    return data
