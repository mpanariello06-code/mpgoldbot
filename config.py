"""
Central configuration layer.

Responsibilities
----------------
* Load the .env file (python-dotenv)
* Parse environment variables into proper Python types
* Apply defaults that EXACTLY match the original main.py source values
* Validate the configuration
* Expose configuration to the rest of the application

IMPORTANT: every default below is identical to the value hard-coded in the
original bot, so a missing environment variable can never change trading
behaviour.
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
    if value is None:
        return default
    return value.strip()


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
    """Parse a comma/space separated list of integer chat ids."""
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

# Fallback paths probed when auto-detect fails (unchanged from original)
POSSIBLE_MT5_PATHS = [
    "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
    "C:\\Program Files\\Exness MetaTrader 5\\terminal64.exe",
    "C:\\Program Files (x86)\\MetaTrader 5\\terminal64.exe",
    "C:\\Program Files (x86)\\Exness MetaTrader 5\\terminal64.exe",
]

# ---------------------------------------------------------------------------
# TRADING SYMBOL
# ---------------------------------------------------------------------------
SYMBOL = _get_str("SYMBOL", "XAUUSDm")

# ---------------------------------------------------------------------------
# AGGRESSIVE RISK SETTINGS (MARGIN-SAFE) - defaults identical to original
# ---------------------------------------------------------------------------
USE_RISK_PERCENT = _get_bool("USE_RISK_PERCENT", True)
RISK_PERCENT = _get_float("RISK_PERCENT", 1.5)   # 1.5% per trade
FIXED_LOT = _get_float("FIXED_LOT", 0.05)        # Fallback if risk calc unused

MAX_OPEN_POSITIONS = _get_int("MAX_OPEN_POSITIONS", 4)

# ---------------------------------------------------------------------------
# TIGHT SCALPING STOPS
# ---------------------------------------------------------------------------
USE_STRUCTURAL_SL = _get_bool("USE_STRUCTURAL_SL", True)
SL_POINTS_MIN = _get_int("SL_POINTS_MIN", 400)
SL_POINTS_MAX = _get_int("SL_POINTS_MAX", 1000)
RR = _get_float("RR", 3.0)

# ---------------------------------------------------------------------------
# TP / RR EXECUTION LAYER (applied after the unchanged signal detection)
# ---------------------------------------------------------------------------
# "custom_rr" + CUSTOM_RR = RR reproduces the original take-profit exactly.
TP_MODE = _get_str("TP_MODE", "custom_rr")
CUSTOM_RR = _get_float("CUSTOM_RR", RR)
MIN_RR = _get_float("MIN_RR", 1.0)
# 1 pip = N points; 0 = derive from the symbol's digits/point
PIP_POINTS = _get_int("PIP_POINTS", 0)
# Stop loss distance used when SL_MODE is "fixed" (structural is the default)
SL_MODE = _get_str("SL_MODE", "structural" if USE_STRUCTURAL_SL else "fixed")
SL_FIXED_POINTS = _get_int("SL_FIXED_POINTS", SL_POINTS_MIN)

# ---------------------------------------------------------------------------
# FILTERS (AGGRESSIVE)
# ---------------------------------------------------------------------------
MAX_SPREAD_POINTS = _get_int("MAX_SPREAD_POINTS", 500)
SESSION_FILTER = _get_bool("SESSION_FILTER", False)
VOLUME_FILTER = _get_bool("VOLUME_FILTER", False)
MIN_CANDLE_RANGE_POINTS = _get_int("MIN_CANDLE_RANGE_POINTS", 25)

# ---------------------------------------------------------------------------
# TRADE MANAGEMENT
# ---------------------------------------------------------------------------
MOVE_TO_BREAKEVEN_AT_R = _get_float("MOVE_TO_BREAKEVEN_AT_R", 0.5)
PARTIAL_CLOSE_AT_R = _get_float("PARTIAL_CLOSE_AT_R", 1.5)
PARTIAL_CLOSE_FRACTION = _get_float("PARTIAL_CLOSE_FRACTION", 0.3)
COOLDOWN_MINUTES = _get_int("COOLDOWN_MINUTES", 0)

# ---------------------------------------------------------------------------
# ENGINE
# ---------------------------------------------------------------------------
MAGIC = _get_int("MAGIC", 88001199)
DEVIATION_POINTS = _get_int("DEVIATION_POINTS", 150)
M1_BARS = _get_int("M1_BARS", 600)
M5_BARS = _get_int("M5_BARS", 300)
POLL_SECONDS = _get_float("POLL_SECONDS", 0.5)

# ---------------------------------------------------------------------------
# PATTERN DETECTION (LOOSE)
# ---------------------------------------------------------------------------
SWEEP_LOOKBACK = _get_int("SWEEP_LOOKBACK", 6)
SWEEP_BUFFER_POINTS = _get_int("SWEEP_BUFFER_POINTS", 3)
REJECTION_MIN_WICK_FRAC = _get_float("REJECTION_MIN_WICK_FRAC", 0.15)
M5_BIAS_STRICT = _get_bool("M5_BIAS_STRICT", False)
REQUIRE_ENGULF = _get_bool("REQUIRE_ENGULF", False)

DIAGNOSTICS = _get_bool("DIAGNOSTICS", True)

# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _get_str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _get_str("TELEGRAM_CHAT_ID", "")
TELEGRAM_ALLOWED_CHAT_IDS = _get_int_list("TELEGRAM_ALLOWED_CHAT_IDS", [])

# Every chat id allowed to control the bot (primary + extras, de-duplicated)
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
# Send trade / error notifications to the primary chat id
TELEGRAM_NOTIFICATIONS = _get_bool("TELEGRAM_NOTIFICATIONS", True)

# ---------------------------------------------------------------------------
# DATA / CSV PERSISTENCE
# ---------------------------------------------------------------------------
DATA_DIRECTORY = _get_str("DATA_DIRECTORY", "data")
TRADE_LOG_FILE = _get_str("TRADE_LOG_FILE", "trades.csv")
EVENT_LOG_FILE = _get_str("EVENT_LOG_FILE", "events.csv")
ACCOUNT_LOG_FILE = _get_str("ACCOUNT_LOG_FILE", "account_snapshots.csv")

# Seconds between account_snapshots.csv rows (background thread, never the loop)
ACCOUNT_SNAPSHOT_INTERVAL = _get_int("ACCOUNT_SNAPSHOT_INTERVAL", 300)
# Seconds between MT5 deal-history syncs used to record closed trades
HISTORY_SYNC_SECONDS = _get_int("HISTORY_SYNC_SECONDS", 15)

# Telegram-controlled runtime settings (never contains secrets)
RUNTIME_SETTINGS_FILE = _get_str("RUNTIME_SETTINGS_FILE", "runtime_settings.json")

DATA_PATH = Path(DATA_DIRECTORY)
if not DATA_PATH.is_absolute():
    DATA_PATH = BASE_DIR / DATA_PATH

# ---------------------------------------------------------------------------
# RUNTIME / LIFECYCLE
# ---------------------------------------------------------------------------
# True keeps the original behaviour: `python optimized.py` starts trading.
AUTO_START_TRADING = _get_bool("AUTO_START_TRADING", True)
# Minimum seconds between MT5 reconnection attempts
MT5_RECONNECT_SECONDS = _get_int("MT5_RECONNECT_SECONDS", 30)


# ---------------------------------------------------------------------------
# Validation & safe reporting
# ---------------------------------------------------------------------------
def runtime_defaults():
    """
    Starting point for the Telegram-controlled settings, and the target of
    "RESET SETTINGS" - i.e. the original configuration, not arbitrary values.
    """
    return {
        "tp_mode": TP_MODE,
        "custom_rr": CUSTOM_RR,
        "min_rr": MIN_RR,
        "lot_mode": "risk_percent" if USE_RISK_PERCENT else "fixed_lot",
        "risk_percent": RISK_PERCENT,
        "fixed_lot": FIXED_LOT,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "sl_mode": SL_MODE,
        "sl_points_min": SL_POINTS_MIN,
        "sl_points_max": SL_POINTS_MAX,
        "sl_fixed_points": SL_FIXED_POINTS,
        "breakeven_enabled": bool(MOVE_TO_BREAKEVEN_AT_R),
        "breakeven_r": MOVE_TO_BREAKEVEN_AT_R or 0.5,
        "partial_close_enabled": bool(PARTIAL_CLOSE_AT_R),
        "partial_close_r": PARTIAL_CLOSE_AT_R or 1.5,
        "partial_close_fraction": PARTIAL_CLOSE_FRACTION,
        "max_spread_points": MAX_SPREAD_POINTS,
        "pip_points": PIP_POINTS,
    }


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

    if RISK_PERCENT <= 0:
        errors.append("RISK_PERCENT must be greater than 0")
    if MAX_OPEN_POSITIONS < 1:
        errors.append("MAX_OPEN_POSITIONS must be at least 1")
    if SL_POINTS_MIN > SL_POINTS_MAX:
        errors.append("SL_POINTS_MIN must be <= SL_POINTS_MAX")
    if POLL_SECONDS <= 0:
        errors.append("POLL_SECONDS must be greater than 0")
    if not 0 < PARTIAL_CLOSE_FRACTION <= 1:
        errors.append("PARTIAL_CLOSE_FRACTION must be between 0 and 1")
    if TP_MODE not in ("1_pip", "2_pips", "3_pips", "4_pips", "5_pips", "custom_rr"):
        errors.append(
            "TP_MODE must be one of 1_pip, 2_pips, 3_pips, 4_pips, 5_pips, custom_rr"
        )
    if CUSTOM_RR <= 0:
        errors.append("CUSTOM_RR must be greater than 0")
    if MIN_RR < 0:
        errors.append("MIN_RR cannot be negative")
    if SL_MODE not in ("structural", "fixed"):
        errors.append("SL_MODE must be 'structural' or 'fixed'")
    if PIP_POINTS < 0:
        errors.append("PIP_POINTS cannot be negative")
    if ACCOUNT_SNAPSHOT_INTERVAL < 30:
        warnings.append(
            "ACCOUNT_SNAPSHOT_INTERVAL below 30s will grow account_snapshots.csv quickly"
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
    """Human readable strategy configuration. Never contains secrets."""
    return {
        "Symbol": SYMBOL,
        "Risk/Trade": f"{RISK_PERCENT}%" if USE_RISK_PERCENT else f"{FIXED_LOT} lots",
        "Max Positions": MAX_OPEN_POSITIONS,
        "SL Points": f"{SL_POINTS_MIN}-{SL_POINTS_MAX}",
        "RR": RR,
        "TP Mode": TP_MODE,
        "Min RR": MIN_RR,
        "Breakeven At": f"{MOVE_TO_BREAKEVEN_AT_R}R",
        "Partial At": f"{PARTIAL_CLOSE_AT_R}R ({PARTIAL_CLOSE_FRACTION:.0%})",
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
