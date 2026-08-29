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
# distance = TP_DISTANCE price units; 1_pip..5_pips = symbol-aware pips
TP_MODE = _get_str("TP_MODE", "distance").lower()
TP_DISTANCE = _get_float("TP_DISTANCE", 0.30)
STOP_LOSS_DISTANCE = _get_float("STOP_LOSS_DISTANCE", 0.0)   # 0 = no SL
# 1 pip = N points; 0 = derive from the symbol's digits/point
PIP_POINTS = _get_int("PIP_POINTS", 0)

# ---------------------------------------------------------------------------
# PROFIT CYCLE
# ---------------------------------------------------------------------------
PROFIT_CYCLE_TARGET = _get_int("PROFIT_CYCLE_TARGET", 4)     # successful TPs
CYCLE_CLOSE_POSITIONS = _get_bool("CYCLE_CLOSE_POSITIONS", True)
# Optional basket target in account currency (0 = off). This is the behaviour
# the reference recording shows: everything closes together on a net profit.
CYCLE_TAKE_PROFIT_MONEY = _get_float("CYCLE_TAKE_PROFIT_MONEY", 0.0)

# ---------------------------------------------------------------------------
# RISK
# ---------------------------------------------------------------------------
LOT_SIZE = _get_float("LOT_SIZE", 0.01)                 # fixed lots, no martingale
MAX_LOT_SIZE = _get_float("MAX_LOT_SIZE", 0.10)
MAX_OPEN_POSITIONS = _get_int("MAX_OPEN_POSITIONS", 4)
MAX_PENDING_ORDERS = _get_int("MAX_PENDING_ORDERS", 10)
MAX_SPREAD = _get_float("MAX_SPREAD", 0.50)             # price units, 0 = off
MAX_SLIPPAGE = _get_int("MAX_SLIPPAGE", 20)             # deviation points
MAX_DAILY_LOSS = _get_float("MAX_DAILY_LOSS", 50.0)     # account currency, 0 = off
MAX_CYCLE_LOSS = _get_float("MAX_CYCLE_LOSS", 20.0)     # account currency, 0 = off
MAX_CONSECUTIVE_LOSING_CYCLES = _get_int("MAX_CONSECUTIVE_LOSING_CYCLES", 3)
COOLDOWN_AFTER_LOSS = _get_float("COOLDOWN_AFTER_LOSS", 15.0)   # minutes

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
# Ladder entries are frequent; keep per-entry pings optional.
TELEGRAM_ENTRY_NOTIFICATIONS = _get_bool("TELEGRAM_ENTRY_NOTIFICATIONS", True)

# ---------------------------------------------------------------------------
# DATA / CSV PERSISTENCE
# ---------------------------------------------------------------------------
DATA_DIRECTORY = _get_str("DATA_DIRECTORY", "data")
TRADE_LOG_FILE = _get_str("TRADE_LOG_FILE", "trades.csv")
EVENT_LOG_FILE = _get_str("EVENT_LOG_FILE", "events.csv")
ACCOUNT_LOG_FILE = _get_str("ACCOUNT_LOG_FILE", "account_snapshots.csv")
LADDER_LOG_FILE = _get_str("LADDER_LOG_FILE", "ladder.csv")

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
        "tp_distance": TP_DISTANCE,
        "stop_loss_distance": STOP_LOSS_DISTANCE,
        "pip_points": PIP_POINTS,
        # cycle
        "profit_cycle_target": PROFIT_CYCLE_TARGET,
        "cycle_close_positions": CYCLE_CLOSE_POSITIONS,
        "cycle_take_profit_money": CYCLE_TAKE_PROFIT_MONEY,
        # risk
        "lot_size": LOT_SIZE,
        "max_lot_size": MAX_LOT_SIZE,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_pending_orders": MAX_PENDING_ORDERS,
        "max_spread": MAX_SPREAD,
        "max_slippage": MAX_SLIPPAGE,
        "max_daily_loss": MAX_DAILY_LOSS,
        "max_cycle_loss": MAX_CYCLE_LOSS,
        "max_consecutive_losing_cycles": MAX_CONSECUTIVE_LOSING_CYCLES,
        "cooldown_after_loss_minutes": COOLDOWN_AFTER_LOSS,
        # hygiene
        "order_max_age_seconds": ORDER_MAX_AGE,
        "m5_candle_reset": M5_CANDLE_RESET,
        # direction
        "direction_filter": DIRECTION_FILTER,
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
    if TP_MODE not in ("distance", "1_pip", "2_pips", "3_pips", "4_pips", "5_pips"):
        errors.append("TP_MODE must be distance or 1_pip..5_pips")
    if TP_MODE == "distance" and TP_DISTANCE <= 0:
        errors.append("TP_DISTANCE must be greater than 0")
    if LOT_SIZE <= 0:
        errors.append("LOT_SIZE must be greater than 0")
    if LOT_SIZE > MAX_LOT_SIZE:
        errors.append(f"LOT_SIZE ({LOT_SIZE}) is above MAX_LOT_SIZE ({MAX_LOT_SIZE})")
    if MAX_OPEN_POSITIONS < 1:
        errors.append("MAX_OPEN_POSITIONS must be at least 1")
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
    if MAX_DAILY_LOSS <= 0:
        warnings.append("MAX_DAILY_LOSS is 0 - the daily loss guard is disabled")
    if PROFIT_CYCLE_TARGET <= 0 and CYCLE_TAKE_PROFIT_MONEY <= 0:
        warnings.append(
            "PROFIT_CYCLE_TARGET and CYCLE_TAKE_PROFIT_MONEY are both 0 - "
            "cycles will only end on a loss limit"
        )

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
        "TP": TP_DISTANCE if TP_MODE == "distance" else TP_MODE,
        "Lot": LOT_SIZE,
        "Cycle Target": PROFIT_CYCLE_TARGET,
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
