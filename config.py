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
# A new cycle is evaluated once per CLOSED candle of this timeframe, never on
# a tick. M1 or M5; the architecture takes any of the supported timeframes.
ENTRY_TIMEFRAME = _get_str("ENTRY_TIMEFRAME", "M1").upper()

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
# Levels per side. One ladder is LADDER_DEPTH BUY STOP + LADDER_DEPTH SELL
# STOP, placed once at cycle start and never replenished.
LADDER_DEPTH = _get_int("LADDER_DEPTH", 11)            # levels per side
# Nearest level distance from price; the broker's minimum stop distance always
# wins when it is larger.
# Extra standoff between the market and a NEW level, on top of the broker's own
# minimum stop distance. 0 = the intended geometry stands: the first level sits
# exactly one LADDER_SPACING from the reference price. Raising this does not
# move the grid - it only stops the nearest levels being placed until price has
# moved away from them.
FIRST_LEVEL_OFFSET = _get_float("FIRST_LEVEL_OFFSET", 0.0)
# extend = the ladder rolls with price (levels re-created ahead of the market)
# static = the grid is fixed for the cycle and consumed as price crosses it
# static: the ladder is pinned when the cycle starts and price consumes it.
# One ladder = LADDER_DEPTH BUY STOP + LADDER_DEPTH SELL STOP, placed once.
# extend: the window rolls forward and consumed levels are replaced, so a live
# ladder keeps placing new orders. That is not one fixed ladder per cycle.
ROLL_MODE = _get_str("ROLL_MODE", "static").lower()
# Re-arm a level whose position closed. Off: a consumed level stays consumed
# for the life of the ladder.
REARM_LEVELS = _get_bool("REARM_LEVELS", False)

# ---------------------------------------------------------------------------
# STOP LOSS
# ---------------------------------------------------------------------------
# There are NO individual take profits. A triggered level is one leg of the
# cycle's basket, and the basket is closed as one unit when its total floating
# P/L reaches BASKET_PROFIT_TARGET below.
STOP_LOSS_DISTANCE = _get_float("STOP_LOSS_DISTANCE", 0.0)   # 0 = no SL
# 1 pip = N points; 0 = derive from the symbol's digits/point
PIP_POINTS = _get_int("PIP_POINTS", 0)

# ---------------------------------------------------------------------------
# CYCLE EXIT
# ---------------------------------------------------------------------------
# The ONE normal strategy exit. When the current cycle's total floating basket
# P/L reaches this, every position and every pending order of the cycle is
# closed. One source of truth - nothing else in the code carries a profit
# figure. 0 disables the normal exit entirely, leaving only the hard risk
# limits, which is almost certainly not what you want.
BASKET_PROFIT_TARGET = _get_float("BASKET_PROFIT_TARGET", 2.00)
# With the profit runner ON a basket is allowed past the target instead of
# being cut off at it, and the accumulated profit is trailed instead. OFF
# restores the plain "close at the target" behaviour.
PROFIT_RUNNER_ENABLED = _get_bool("PROFIT_RUNNER_ENABLED", True)
# Peak floating P/L at which profit protection turns on for the cycle. Once on
# it stays on, even if the basket falls back below this.
PROFIT_PROTECTION_ACTIVATION = _get_float("PROFIT_PROTECTION_ACTIVATION", 3.00)
# How much give-back from the peak is tolerated before the basket is taken.
PROFIT_PROTECTION_TRAIL = _get_float("PROFIT_PROTECTION_TRAIL", 1.50)
# The protected floor: a basket that has been in profit is never knowingly let
# through this on the way down.
MIN_PROTECTED_PROFIT = _get_float("MIN_PROTECTED_PROFIT", 1.00)

# --- basket state engine ---------------------------------------------------
# NONE of these defaults are validated. They are starting points chosen to be
# explainable, not optimal, and they exist to be fitted once there is enough
# telemetry to fit them on.
#
# How far under water counts as meaningfully negative rather than noise.
UNDERWATER_THRESHOLD = _get_float("UNDERWATER_THRESHOLD", 2.00)
# How much of the hole must be climbed back before it counts as a recovery,
# as a fraction of how deep the hole was.
RECOVERY_FRACTION = _get_float("RECOVERY_FRACTION", 0.50)
# A recovered basket is taken at this profit instead of the full target: it has
# already shown how far it can go the other way.
RECOVERY_TAKE_PROFIT = _get_float("RECOVERY_TAKE_PROFIT", 0.50)
# Give-back allowed from a peak that reached the target but never reached
# PROFIT_PROTECTION_ACTIVATION, as a fraction of that peak. This closes the
# dead band that let a +2.45 basket ride back to -0.10.
PROFIT_GIVEBACK_FRACTION = _get_float("PROFIT_GIVEBACK_FRACTION", 0.40)
# Seconds of price history behind the favorable/adverse movement reading.
PRICE_MOVEMENT_WINDOW = _get_float("PRICE_MOVEMENT_WINDOW", 20.0)

# --- exposure limits -------------------------------------------------------
# ABSOLUTE ladder depths at which each zone begins. Absolute rather than
# fractions of MAX_LADDER_DEPTH: "12 levels deep" is a fact about the exposure
# and should not change meaning because someone raised the ceiling.
#
# A deep ladder is NOT automatically a bad ladder - deep baskets do recover.
# The zone decides how much MORE exposure may be added; basket health decides
# whether to exit. INITIAL TEST DEFAULTS, fitted to nothing.
LADDER_EXTENDED_DEPTH = _get_int("LADDER_EXTENDED_DEPTH", 9)
LADDER_DEEP_DEPTH = _get_int("LADDER_DEEP_DEPTH", 12)
LADDER_CRITICAL_DEPTH = _get_int("LADDER_CRITICAL_DEPTH", 16)

# Deep-ladder health. Off = the zones are recorded but never gate expansion.
DEEP_LADDER_RISK_ENABLED = _get_bool("DEEP_LADDER_RISK_ENABLED", True)
# Drawdown from peak (account currency) that counts as a large hole for a deep
# basket. INITIAL TEST DEFAULT.
DEEP_LADDER_MAX_DRAWDOWN = _get_float("DEEP_LADDER_MAX_DRAWDOWN", 10.00)
# Imbalance above which a deep basket is dangerously one-sided.
DEEP_LADDER_MAX_IMBALANCE = _get_float("DEEP_LADDER_MAX_IMBALANCE", 0.60)
# Seconds a deep basket may sit underwater without a meaningful recovery before
# that counts against it.
DEEP_LADDER_RECOVERY_TIMEOUT = _get_float("DEEP_LADDER_RECOVERY_TIMEOUT", 900.0)
# Adverse movement (price units, over PRICE_MOVEMENT_WINDOW) that counts as
# "still going the wrong way" rather than noise.
DEEP_LADDER_ADVERSE_MOVEMENT = _get_float("DEEP_LADDER_ADVERSE_MOVEMENT", 0.60)
# |buy_volume - sell_volume| / gross_volume above which a basket counts as
# one-sided. 1.0 = entirely one-sided. Volume, never order counts.
MAX_DIRECTION_IMBALANCE = _get_float("MAX_DIRECTION_IMBALANCE", 0.80)
# MONITOR = record it only. STOP_NEW_EXPOSURE = also stop adding levels.
# Never "add the other side to balance it" - that is martingale.
# MONITOR by default: while we are collecting data, an imbalance should be
# RECORDED, not acted on. Switch to STOP_NEW_EXPOSURE once the telemetry says
# which imbalance levels actually precede losses.
IMBALANCE_ACTION = _get_str("IMBALANCE_ACTION", "MONITOR").upper()
# Legs required before the imbalance ratio is graded at all - one leg is
# trivially 100% one-sided and says nothing.
IMBALANCE_MIN_POSITIONS = _get_int("IMBALANCE_MIN_POSITIONS", 4)

# --- recovery quality ------------------------------------------------------
# Fractions of the hole climbed back that grade the recovery.
WEAK_RECOVERY_FRACTION = _get_float("WEAK_RECOVERY_FRACTION", 0.25)
STRONG_RECOVERY_FRACTION = _get_float("STRONG_RECOVERY_FRACTION", 0.75)

CYCLE_CLOSE_POSITIONS = _get_bool("CYCLE_CLOSE_POSITIONS", True)

# ---------------------------------------------------------------------------
# RISK
# ---------------------------------------------------------------------------
LOT_SIZE = _get_float("LOT_SIZE", 0.01)                 # fixed lots, no martingale
MAX_LOT_SIZE = _get_float("MAX_LOT_SIZE", 0.10)
# The basket accumulates: with no individual TP, positions stay open until the
# whole cycle is closed, so this has to allow a full ladder. Too low and the
# ladder stops after N triggers, which is the "N trades = exit" rule the
# strategy explicitly does not have.
MAX_OPEN_POSITIONS = _get_int("MAX_OPEN_POSITIONS", 22)
# Must allow a whole ladder (2 x LADDER_DEPTH) or it deploys short.
MAX_PENDING_ORDERS = _get_int("MAX_PENDING_ORDERS", 22)
# Levels a single cycle may consume before it stops adding exposure. The
# remaining pending orders are cancelled at the cap; the basket already open is
# still managed by the exit rules. Worth testing across 5..12 and up.
MAX_LADDER_DEPTH = _get_int("MAX_LADDER_DEPTH", 22)    # levels used per cycle
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
# How often the basket exit monitor re-checks live P/L. This is the highest
# priority loop in the bot and does two MT5 reads per pass, nothing else.
# It is deliberately far faster than POLL_SECONDS: the ladder can be
# reconciled twice a second, but a basket at its target cannot wait that long.
EXIT_POLL_SECONDS = _get_float("EXIT_POLL_SECONDS", 0.05)
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
# How often an intra-cycle basket snapshot is written to basket_telemetry.csv
# while a cycle is open. CSV only - never Telegram. 0 = off.
TELEMETRY_INTERVAL_SECONDS = _get_float("TELEMETRY_INTERVAL_SECONDS", 2.0)
TELEMETRY_FILE = _get_str("TELEMETRY_FILE", "basket_telemetry.csv")
ENTRY_LOG_FILE = _get_str("ENTRY_LOG_FILE", "entry_evaluations.csv")

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
        "entry_timeframe": ENTRY_TIMEFRAME,
        # ladder
        "ladder_spacing": LADDER_SPACING,
        "ladder_depth": LADDER_DEPTH,
        "first_level_offset": FIRST_LEVEL_OFFSET,
        "roll_mode": ROLL_MODE,
        "rearm_levels": REARM_LEVELS,
        # take profit
        "basket_profit_target": BASKET_PROFIT_TARGET,
        "profit_runner_enabled": PROFIT_RUNNER_ENABLED,
        "profit_protection_activation": PROFIT_PROTECTION_ACTIVATION,
        "profit_protection_trail": PROFIT_PROTECTION_TRAIL,
        "min_protected_profit": MIN_PROTECTED_PROFIT,
        "underwater_threshold": UNDERWATER_THRESHOLD,
        "recovery_fraction": RECOVERY_FRACTION,
        "recovery_take_profit": RECOVERY_TAKE_PROFIT,
        "profit_giveback_fraction": PROFIT_GIVEBACK_FRACTION,
        "price_movement_window": PRICE_MOVEMENT_WINDOW,
        "ladder_extended_depth": LADDER_EXTENDED_DEPTH,
        "ladder_deep_depth": LADDER_DEEP_DEPTH,
        "ladder_critical_depth": LADDER_CRITICAL_DEPTH,
        "deep_ladder_risk_enabled": DEEP_LADDER_RISK_ENABLED,
        "deep_ladder_max_drawdown": DEEP_LADDER_MAX_DRAWDOWN,
        "deep_ladder_max_imbalance": DEEP_LADDER_MAX_IMBALANCE,
        "deep_ladder_recovery_timeout": DEEP_LADDER_RECOVERY_TIMEOUT,
        "deep_ladder_adverse_movement": DEEP_LADDER_ADVERSE_MOVEMENT,
        "max_direction_imbalance": MAX_DIRECTION_IMBALANCE,
        "imbalance_action": IMBALANCE_ACTION,
        "imbalance_min_positions": IMBALANCE_MIN_POSITIONS,
        "weak_recovery_fraction": WEAK_RECOVERY_FRACTION,
        "strong_recovery_fraction": STRONG_RECOVERY_FRACTION,
        "telemetry_interval_seconds": TELEMETRY_INTERVAL_SECONDS,
        "stop_loss_distance": STOP_LOSS_DISTANCE,
        "pip_points": PIP_POINTS,
        # cycle / adaptive exit
        "cycle_close_positions": CYCLE_CLOSE_POSITIONS,
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
        "telegram_status_updates": TELEGRAM_STATUS_UPDATES,
        "telegram_status_interval_minutes": TELEGRAM_STATUS_INTERVAL,
        "telegram_error_throttle_seconds": TELEGRAM_ERROR_THROTTLE,
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
    if BASKET_PROFIT_TARGET < 0:
        errors.append("BASKET_PROFIT_TARGET cannot be negative")
    if PROFIT_PROTECTION_TRAIL <= 0 and PROFIT_RUNNER_ENABLED:
        errors.append("PROFIT_PROTECTION_TRAIL must be greater than 0 when "
                      "PROFIT_RUNNER_ENABLED is true")
    if MIN_PROTECTED_PROFIT > BASKET_PROFIT_TARGET > 0:
        warnings.append(
            f"MIN_PROTECTED_PROFIT ({MIN_PROTECTED_PROFIT:.2f}) is above "
            f"BASKET_PROFIT_TARGET ({BASKET_PROFIT_TARGET:.2f}) - the floor is "
            f"capped at the target, since a basket is never held for more "
            f"profit than it would have been closed at")
    if MIN_PROTECTED_PROFIT > PROFIT_PROTECTION_ACTIVATION:
        errors.append("MIN_PROTECTED_PROFIT cannot be above "
                      "PROFIT_PROTECTION_ACTIVATION")
    if PROFIT_RUNNER_ENABLED and PROFIT_PROTECTION_ACTIVATION < BASKET_PROFIT_TARGET:
        warnings.append(
            f"PROFIT_PROTECTION_ACTIVATION ({PROFIT_PROTECTION_ACTIVATION:.2f}) "
            f"is below BASKET_PROFIT_TARGET ({BASKET_PROFIT_TARGET:.2f}) - "
            f"protection will be active before the target is ever reached")
    if BASKET_PROFIT_TARGET == 0:
        warnings.append(
            "BASKET_PROFIT_TARGET is 0 - the normal exit is disabled and only "
            "the hard risk limits can end a cycle")
    if LOT_SIZE <= 0:
        errors.append("LOT_SIZE must be greater than 0")
    if LOT_SIZE > MAX_LOT_SIZE:
        errors.append(f"LOT_SIZE ({LOT_SIZE}) is above MAX_LOT_SIZE ({MAX_LOT_SIZE})")
    if MAX_OPEN_POSITIONS < 1:
        errors.append("MAX_OPEN_POSITIONS must be at least 1")
    if MAX_OPEN_POSITIONS < LADDER_DEPTH:
        warnings.append(
            f"MAX_OPEN_POSITIONS={MAX_OPEN_POSITIONS} is below LADDER_DEPTH="
            f"{LADDER_DEPTH}: with no individual TP the basket will stop "
            f"accumulating before the ladder is fully consumed")
    if MAX_PENDING_ORDERS < 1:
        errors.append("MAX_PENDING_ORDERS must be at least 1")
    if MAX_PENDING_ORDERS < LADDER_DEPTH * 2:
        warnings.append(
            f"MAX_PENDING_ORDERS={MAX_PENDING_ORDERS} is below a whole ladder "
            f"({LADDER_DEPTH} x 2 = {LADDER_DEPTH * 2}) - every ladder will "
            f"deploy short")
    if ROLL_MODE != "static" or REARM_LEVELS:
        warnings.append(
            f"ROLL_MODE={ROLL_MODE} / REARM_LEVELS={REARM_LEVELS}: consumed "
            f"levels will be replaced inside a live ladder. Use "
            f"ROLL_MODE=static with REARM_LEVELS=false for one fixed "
            f"{LADDER_DEPTH}+{LADDER_DEPTH} ladder per cycle")
    if POLL_SECONDS <= 0:
        errors.append("POLL_SECONDS must be greater than 0")
    if IMBALANCE_ACTION not in ("MONITOR", "STOP_NEW_EXPOSURE"):
        errors.append(
            f"IMBALANCE_ACTION must be MONITOR or STOP_NEW_EXPOSURE, "
            f"got {IMBALANCE_ACTION!r}")
    if not 0.0 <= MAX_DIRECTION_IMBALANCE <= 1.0:
        errors.append("MAX_DIRECTION_IMBALANCE must be between 0 and 1")
    zones = (("LADDER_EXTENDED_DEPTH", LADDER_EXTENDED_DEPTH),
             ("LADDER_DEEP_DEPTH", LADDER_DEEP_DEPTH),
             ("LADDER_CRITICAL_DEPTH", LADDER_CRITICAL_DEPTH))
    for (lo_name, lo), (hi_name, hi) in zip(zones, zones[1:]):
        if hi <= lo:
            errors.append(f"{hi_name} ({hi}) must be greater than "
                          f"{lo_name} ({lo})")
    if MAX_LADDER_DEPTH and LADDER_CRITICAL_DEPTH > MAX_LADDER_DEPTH:
        warnings.append(
            f"LADDER_CRITICAL_DEPTH ({LADDER_CRITICAL_DEPTH}) is above "
            f"MAX_LADDER_DEPTH ({MAX_LADDER_DEPTH}) - the critical zone can "
            f"never be reached")
    if STRONG_RECOVERY_FRACTION < WEAK_RECOVERY_FRACTION:
        warnings.append(
            f"STRONG_RECOVERY_FRACTION ({STRONG_RECOVERY_FRACTION}) is below "
            f"WEAK_RECOVERY_FRACTION ({WEAK_RECOVERY_FRACTION})")
    if EXIT_POLL_SECONDS <= 0:
        errors.append("EXIT_POLL_SECONDS must be greater than 0")
    if EXIT_POLL_SECONDS > POLL_SECONDS:
        warnings.append(
            f"EXIT_POLL_SECONDS ({EXIT_POLL_SECONDS}) is slower than "
            f"POLL_SECONDS ({POLL_SECONDS}) - the exit monitor is supposed to "
            f"be the fastest loop in the bot")
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
        "TP": "none - basket exit only",
        "Lot": LOT_SIZE,
        "Exit": f"basket floating P/L >= {BASKET_PROFIT_TARGET:.2f}",
        "Max Positions": MAX_OPEN_POSITIONS,
        "Max Pendings": MAX_PENDING_ORDERS,
        "Max Spread": MAX_SPREAD,
        "Magic": MAGIC,
        "Poll": f"{POLL_SECONDS}s",
        "Exit poll": f"{EXIT_POLL_SECONDS}s",
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
