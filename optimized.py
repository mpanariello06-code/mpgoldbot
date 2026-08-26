"""
ULTRA-AGGRESSIVE XAUUSD M1 Scalping Bot - WITH MARGIN SAFETY
- 1.5% risk/trade, tight SL (400-1000 pts), 3:1 RR, max 4 positions
- Trades 24/5, 15-25 trades/day expected
- Target: 120-150%/month | HIGH RISK: DEMO ONLY
- Margin-safe for $5K account at 1:100 leverage

REFACTOR NOTES
--------------
The trading strategy is UNCHANGED. Entry conditions, signal generation,
SL/TP maths, lot sizing, margin checks, position limits, breakeven, partial
closes, MT5 order parameters and the 0.5s polling cadence are identical to the
original single-file bot. What was added:

  * configuration comes from .env via config.py (defaults == original values)
  * a Telegram control panel (telegram_controller.py) on its own thread
  * CSV persistence (csv_logger.py) on a background monitor thread
  * thread-safe runtime state (STOPPED / RUNNING / PAUSED / ERROR)
  * MT5 reconnection + error handling for the above

Run:  pip install -r requirements.txt && python optimized.py
"""

import math
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np

import MetaTrader5 as mt5

import config as cfg
from csv_logger import CsvLogger

# ===========================================================================
# CONFIGURATION (loaded from .env - values identical to the original source)
# ===========================================================================
LOGIN = cfg.LOGIN
PASSWORD = cfg.PASSWORD
SERVER = cfg.SERVER
SYMBOL = cfg.SYMBOL

USE_RISK_PERCENT = cfg.USE_RISK_PERCENT
RISK_PERCENT = cfg.RISK_PERCENT
FIXED_LOT = cfg.FIXED_LOT

MAX_OPEN_POSITIONS = cfg.MAX_OPEN_POSITIONS

USE_STRUCTURAL_SL = cfg.USE_STRUCTURAL_SL
SL_POINTS_MIN = cfg.SL_POINTS_MIN
SL_POINTS_MAX = cfg.SL_POINTS_MAX
RR = cfg.RR

MAX_SPREAD_POINTS = cfg.MAX_SPREAD_POINTS
SESSION_FILTER = cfg.SESSION_FILTER
VOLUME_FILTER = cfg.VOLUME_FILTER
MIN_CANDLE_RANGE_POINTS = cfg.MIN_CANDLE_RANGE_POINTS

MOVE_TO_BREAKEVEN_AT_R = cfg.MOVE_TO_BREAKEVEN_AT_R
PARTIAL_CLOSE_AT_R = cfg.PARTIAL_CLOSE_AT_R
PARTIAL_CLOSE_FRACTION = cfg.PARTIAL_CLOSE_FRACTION
COOLDOWN_MINUTES = cfg.COOLDOWN_MINUTES

MAGIC = cfg.MAGIC
DEVIATION_POINTS = cfg.DEVIATION_POINTS
M1_BARS = cfg.M1_BARS
M5_BARS = cfg.M5_BARS
POLL_SECONDS = cfg.POLL_SECONDS

SWEEP_LOOKBACK = cfg.SWEEP_LOOKBACK
SWEEP_BUFFER_POINTS = cfg.SWEEP_BUFFER_POINTS
REJECTION_MIN_WICK_FRAC = cfg.REJECTION_MIN_WICK_FRAC
M5_BIAS_STRICT = cfg.M5_BIAS_STRICT
REQUIRE_ENGULF = cfg.REQUIRE_ENGULF

DIAGNOSTICS = cfg.DIAGNOSTICS

POSSIBLE_MT5_PATHS = cfg.POSSIBLE_MT5_PATHS

# ===========================================================================
# SHARED INFRASTRUCTURE
# ===========================================================================
# The MetaTrader5 package is a thin wrapper around a single terminal pipe, so
# every call is serialised. Acquisitions are microseconds - the 0.5s trading
# loop is unaffected, and Telegram queries can never interleave mid-call.
MT5_LOCK = threading.RLock()

CSV = CsvLogger(
    cfg.DATA_PATH,
    cfg.TRADE_LOG_FILE,
    cfg.EVENT_LOG_FILE,
    cfg.ACCOUNT_LOG_FILE,
)


def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def log_event(event_type, message="", symbol="", ticket="", status="OK",
              console=False):
    """Console (optional) + events.csv. Never called per polling cycle."""
    if console:
        log(message or event_type)
    CSV.log_event(event_type, message, symbol, ticket, status)


# ===========================================================================
# MT5 CONNECTION (unchanged behaviour, now lock-guarded)
# ===========================================================================
def find_mt5_path():
    import os
    if cfg.MT5_PATH and os.path.exists(cfg.MT5_PATH):
        return cfg.MT5_PATH
    for path in POSSIBLE_MT5_PATHS:
        if os.path.exists(path):
            return path
    return None


def ensure_initialized():
    log("Connecting to MT5...")

    with MT5_LOCK:
        # Method 1: Auto-detect (if MT5 running)
        if mt5.initialize():
            log("✓ Connected (auto-detect)")
            return True

        err1 = mt5.last_error()
        log(f"Auto-detect failed: {err1}")

        # Method 2: Try known paths
        mt5_path = find_mt5_path()
        if mt5_path:
            log(f"Trying: {mt5_path}")
            if mt5.initialize(path=mt5_path):
                log("✓ Connected (explicit path)")
                return True
            log(f"Path failed: {mt5.last_error()}")

        last_error = mt5.last_error()

    # Final error
    raise RuntimeError(
        f"MT5 connection failed. Last error: {last_error}\n"
        "Fix:\n"
        "1. Open MetaTrader 5 desktop app manually\n"
        "2. Login to your Exness demo account\n"
        "3. Run this script again"
    )


def login_if_needed():
    if LOGIN and LOGIN > 0:
        log(f"Logging into account {LOGIN}...")
        with MT5_LOCK:
            ok = mt5.login(LOGIN, password=PASSWORD, server=SERVER)
            err = None if ok else mt5.last_error()
        if ok:
            log(f"✓ Logged into {LOGIN}")
        else:
            log(f"⚠ Login failed: {err}")
            log("Continuing with current account")
    else:
        account = account_info()
        if account:
            log(f"Using account: {account.login} | Balance: ${account.balance:.2f}")
            log(f"Margin: Used ${account.margin:.2f} | Free ${account.margin_free:.2f} | Level {account.margin_level:.0f}%")
        else:
            log("⚠ No account info")


def ensure_symbol(symbol):
    with MT5_LOCK:
        info = mt5.symbol_info(symbol)
        if info and info.visible:
            return info

        # Try to enable in Market Watch
        if info and not info.visible:
            log(f"Enabling {symbol} in Market Watch...")
            if mt5.symbol_select(symbol, True):
                return mt5.symbol_info(symbol)

        # Suggest alternatives
        all_symbols = mt5.symbols_get()

    if all_symbols:
        cands = [s.name for s in all_symbols if "XAUUSDm" in s.name.upper() or "GOLD" in s.name.upper()]
        if cands:
            log(f"Symbol '{symbol}' not found. Try: {cands[:5]}")

    raise RuntimeError(f"Symbol '{symbol}' unavailable. Check Market Watch in MT5.")


def normalize_price(price, digits):
    factor = 10 ** digits
    return math.floor(price * factor + 0.5) / factor


def utc_now():
    return datetime.now(timezone.utc)


# ---- thin lock-guarded MT5 accessors --------------------------------------
def symbol_info(symbol):
    with MT5_LOCK:
        return mt5.symbol_info(symbol)


def account_info():
    with MT5_LOCK:
        return mt5.account_info()


def terminal_connected():
    with MT5_LOCK:
        info = mt5.terminal_info()
    return bool(info and getattr(info, "connected", False))


def get_rates(symbol, timeframe, count):
    with MT5_LOCK:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    return rates if rates is not None else np.array([])


def get_tick(symbol):
    with MT5_LOCK:
        return mt5.symbol_info_tick(symbol)


def spread_points(symbol_info, tick):
    if tick is None:
        return 999
    return (tick.ask - tick.bid) / symbol_info.point


# ===========================================================================
# PRICE ACTION (unchanged)
# ===========================================================================
def candle_parts(c):
    o, h, l, cl = c['open'], c['high'], c['low'], c['close']
    rng = max(h - l, 1e-9)
    bull = cl > o
    bear = cl < o
    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l
    body = abs(cl - o)
    return {
        "open": o, "high": h, "low": l, "close": cl,
        "range": rng, "bull": bull, "bear": bear,
        "upper_wick": upper_wick, "lower_wick": lower_wick,
        "body": body
    }


def m5_bias(symbol):
    if not M5_BIAS_STRICT:
        return 1  # Always allow both directions
    rates = get_rates(symbol, mt5.TIMEFRAME_M5, M5_BARS)
    if len(rates) < 5:
        return 0
    return 1 if rates[-1]['close'] > rates[-5]['close'] else -1


def swept_prior_low(m1_rates, c1, symbol_info):
    point = symbol_info.point
    lows = m1_rates['low'][-(SWEEP_LOOKBACK+2):-2]
    if lows.size == 0:
        return False
    prior_min = float(lows.min())
    return c1['low'] < prior_min - SWEEP_BUFFER_POINTS * point


def swept_prior_high(m1_rates, c1, symbol_info):
    point = symbol_info.point
    highs = m1_rates['high'][-(SWEEP_LOOKBACK+2):-2]
    if highs.size == 0:
        return False
    prior_max = float(highs.max())
    return c1['high'] > prior_max + SWEEP_BUFFER_POINTS * point


def last_closed_m1(symbol):
    rates = get_rates(symbol, mt5.TIMEFRAME_M1, M1_BARS)
    if len(rates) < SWEEP_LOOKBACK + 3:
        return None, None
    return rates, rates[-2]


# ===========================================================================
# ORDERS WITH MARGIN SAFETY (unchanged)
# ===========================================================================
def get_open_positions(symbol):
    with MT5_LOCK:
        poss = mt5.positions_get(symbol=symbol)
    return poss if poss else []


def choose_filling_mode(symbol):
    info = symbol_info(symbol)
    tick = get_tick(symbol)
    if info is None or tick is None:
        return mt5.ORDER_FILLING_FOK

    test_price = tick.ask
    test_vol = max(info.volume_min, info.volume_step)

    for mode in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": test_vol,
            "type": mt5.ORDER_TYPE_BUY,
            "price": test_price,
            "deviation": 20,
            "type_filling": mode,
            "type_time": mt5.ORDER_TIME_GTC,
        }
        with MT5_LOCK:
            chk = mt5.order_check(req)
        if chk and getattr(chk, "retcode", None) == mt5.TRADE_RETCODE_DONE:
            return mode

    return mt5.ORDER_FILLING_IOC


def compute_lot(symbol_info, sl_points, account_info):
    """Calculate lot size with margin safety checks"""
    if not USE_RISK_PERCENT:
        return FIXED_LOT

    # Risk-based calculation
    risk_money = account_info.balance * (RISK_PERCENT / 100.0)
    tick_size = symbol_info.trade_tick_size or symbol_info.point
    tick_value = symbol_info.trade_tick_value or 1.0

    if tick_size <= 0:
        tick_size = symbol_info.point

    value_per_point = tick_value / (tick_size / symbol_info.point)
    if value_per_point <= 0:
        value_per_point = 1.0

    lots = risk_money / max(sl_points * value_per_point, 1e-9)

    # Apply min/max/step
    lot_min = symbol_info.volume_min
    lot_max = symbol_info.volume_max
    lot_step = symbol_info.volume_step
    lots = max(lot_min, min(lots, lot_max))
    lots = round(lots / lot_step) * lot_step

    # MARGIN SAFETY: Reduce lot if insufficient free margin
    free_margin = account_info.margin_free
    tick = get_tick(symbol_info.name)
    if tick and free_margin > 0:
        price = tick.ask
        # Estimate required margin (conservative for 1:100 leverage)
        # XAUUSD contract size typically 100 oz
        estimated_margin = (lots * 100 * price) / 100  # 1:100 leverage

        # If estimated margin > 70% of free margin, reduce lot size
        max_safe_margin = free_margin * 0.7
        if estimated_margin > max_safe_margin:
            safe_lots = (max_safe_margin * 100) / (100 * price)
            safe_lots = max(lot_min, round(safe_lots / lot_step) * lot_step)
            if safe_lots < lots:
                log(f"⚠ Lot reduced {lots:.2f} → {safe_lots:.2f} (margin safety)")
                lots = safe_lots

    return lots


def check_margin_available(symbol, lot_size):
    """Pre-check if sufficient margin for new position"""
    account = account_info()
    if not account:
        return False, "No account info"

    info = symbol_info(symbol)
    tick = get_tick(symbol)
    if not info or not tick:
        return False, "No symbol/tick"

    # Estimate required margin
    price = tick.ask
    estimated_margin = (lot_size * 100 * price) / 100  # Conservative 1:100

    free_margin = account.margin_free

    # Require at least 20% buffer
    if estimated_margin > free_margin * 0.8:
        return False, f"Low margin: need ~${estimated_margin:.0f}, free ${free_margin:.0f}"

    return True, "OK"


def place_order(symbol, order_type, volume, sl, tp, deviation_points):
    """Place order with margin pre-check"""
    info = symbol_info(symbol)
    tick = get_tick(symbol)

    if tick is None or info is None:
        log("⚠ No tick/info")
        log_event("TRADE_FAILED", "No tick/info", symbol=symbol, status="FAILED")
        return None

    # PRE-CHECK MARGIN
    margin_ok, margin_msg = check_margin_available(symbol, volume)
    if not margin_ok:
        log(f"✗ {margin_msg}")
        log_event("TRADE_FAILED", margin_msg, symbol=symbol, status="REJECTED")
        return None

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    filling = choose_filling_mode(symbol)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": normalize_price(price, info.digits),
        "sl": normalize_price(sl, info.digits),
        "tp": normalize_price(tp, info.digits),
        "deviation": int(deviation_points),
        "magic": MAGIC,
        "comment": "Aggressive",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    with MT5_LOCK:
        result = mt5.order_send(request)

    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        sl_pts = int((price - sl) / info.point) if order_type == mt5.ORDER_TYPE_BUY else int((sl - price) / info.point)
        account = account_info()
        log(f"✓ {'BUY' if order_type == mt5.ORDER_TYPE_BUY else 'SELL'} {volume} lots | SL {sl_pts} pts | TP {int(sl_pts * RR)} pts")
        log(f"  Margin used: ${account.margin:.0f} | Free: ${account.margin_free:.0f}")
        return result

    if result:
        log(f"✗ Order failed: {result.comment}")
        log_event("TRADE_FAILED",
                  f"retcode={result.retcode} {result.comment}",
                  symbol=symbol, status="FAILED")
        if result.retcode == 10019:  # Not enough money
            account = account_info()
            log(f"  Balance ${account.balance:.2f} | Free margin ${account.margin_free:.2f}")
            log(f"  SOLUTION: Reduce RISK_PERCENT or MAX_OPEN_POSITIONS")
    else:
        log("✗ Order failed: No result")
        log_event("TRADE_FAILED", "No result from order_send", symbol=symbol,
                  status="FAILED")

    return None


def modify_sl(position_ticket, new_sl, new_tp):
    with MT5_LOCK:
        pos_list = mt5.positions_get(ticket=position_ticket)
        if not pos_list:
            return False

        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": position_ticket,
            "sl": new_sl,
            "tp": new_tp,
            "magic": MAGIC,
        }

        result = mt5.order_send(req)
    return result and result.retcode == mt5.TRADE_RETCODE_DONE


def close_partial(position, fraction):
    symbol = position.symbol
    info = symbol_info(symbol)
    tick = get_tick(symbol)

    if tick is None or info is None:
        return False

    vol = position.volume * fraction
    step = info.volume_step
    vol = max(info.volume_min, min(info.volume_max, round(vol / step) * step))

    if vol <= 0:
        return False

    order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": vol,
        "type": order_type,
        "position": position.ticket,
        "price": normalize_price(price, info.digits),
        "deviation": DEVIATION_POINTS,
        "magic": MAGIC,
        "comment": "Partial",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": choose_filling_mode(symbol),
    }

    with MT5_LOCK:
        result = mt5.order_send(req)
    return result and result.retcode == mt5.TRADE_RETCODE_DONE


# ===========================================================================
# SIGNAL GENERATION (unchanged)
# ===========================================================================
def build_signal(symbol):
    info = symbol_info(symbol)
    m1_rates, c1 = last_closed_m1(symbol)

    if c1 is None:
        return None, "no_data"

    tick = get_tick(symbol)
    if tick is None:
        return None, "no_tick"

    # Skip only extreme spreads
    spr = spread_points(info, tick)
    if spr > MAX_SPREAD_POINTS:
        return None, f"spread:{int(spr)}"

    # Skip tiny candles
    c1p = candle_parts(c1)
    if c1p["range"] < MIN_CANDLE_RANGE_POINTS * info.point:
        return None, "tiny_candle"

    bias = m5_bias(symbol)

    # BULLISH: Sweep low + bullish candle + lower wick
    if swept_prior_low(m1_rates, c1, info) and c1p["bull"]:
        lower_wick_frac = c1p["lower_wick"] / c1p["range"]
        if lower_wick_frac >= REJECTION_MIN_WICK_FRAC:
            entry_price = tick.ask

            # Tight structural SL
            sl_distance = max(c1p["range"] * 1.2, SL_POINTS_MIN * info.point)
            sl_distance = min(sl_distance, SL_POINTS_MAX * info.point)

            sl = entry_price - sl_distance
            sl_points = sl_distance / info.point
            tp = entry_price + RR * sl_distance

            return {
                "type": mt5.ORDER_TYPE_BUY,
                "sl": sl,
                "tp": tp,
                "sl_points": sl_points
            }, "long"

    # BEARISH: Sweep high + bearish candle + upper wick
    if swept_prior_high(m1_rates, c1, info) and c1p["bear"]:
        upper_wick_frac = c1p["upper_wick"] / c1p["range"]
        if upper_wick_frac >= REJECTION_MIN_WICK_FRAC:
            entry_price = tick.bid

            sl_distance = max(c1p["range"] * 1.2, SL_POINTS_MIN * info.point)
            sl_distance = min(sl_distance, SL_POINTS_MAX * info.point)

            sl = entry_price + sl_distance
            sl_points = sl_distance / info.point
            tp = entry_price - RR * sl_distance

            return {
                "type": mt5.ORDER_TYPE_SELL,
                "sl": sl,
                "tp": tp,
                "sl_points": sl_points
            }, "short"

    return None, "no_setup"


# ===========================================================================
# RUNTIME STATE
# ===========================================================================
class BotState:
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


STATE_ICONS = {
    BotState.STOPPED: "⚪",
    BotState.RUNNING: "🟢",
    BotState.PAUSED: "⏸",
    BotState.ERROR: "🔴",
}


# ===========================================================================
# TRADING ENGINE
# ===========================================================================
class TradingEngine:
    """
    Owns the one and only trading loop.

    Lifecycle is guarded by a lock so START can never spawn a second loop, and
    PAUSE only blocks NEW entries - breakeven, partial closes and every other
    piece of position management keep running.
    """

    def __init__(self, csv_logger=CSV):
        self.csv = csv_logger

        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._pause = threading.Event()

        self._state = BotState.STOPPED
        self._last_error = ""
        self._mt5_ready = False
        self._connected = False

        self.trades_today = 0
        self._trades_day = datetime.now().strftime("%Y-%m-%d")
        self.started_at = None
        self.last_signal_reason = ""
        self.last_loop_at = None

        self._notifier = None
        self._reconnect_at = 0.0

    # ------------------------------------------------------------ plumbing
    def set_notifier(self, fn):
        """fn(text) must never block; used for Telegram push notifications."""
        self._notifier = fn

    def notify(self, text):
        fn = self._notifier
        if not fn:
            return
        try:
            fn(text)
        except Exception as exc:  # Telegram must never break trading
            log(f"⚠ Notify failed: {exc}")

    @property
    def state(self):
        with self._lock:
            return self._state

    def _set_state(self, state, error=""):
        with self._lock:
            self._state = state
            self._last_error = error

    def is_running(self):
        return self.state in (BotState.RUNNING, BotState.PAUSED)

    def is_paused(self):
        return self._pause.is_set()

    def _sleep(self, seconds):
        """Interruptible sleep - identical cadence to time.sleep(POLL_SECONDS)."""
        self._stop.wait(seconds)

    def _roll_day(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._trades_day:
            self._trades_day = today
            self.trades_today = 0

    # ------------------------------------------------------- MT5 connection
    def connect(self):
        """Initialise MT5 + symbol. Safe to call repeatedly (no duplicate init)."""
        with self._lock:
            if self._mt5_ready and terminal_connected() and symbol_info(SYMBOL):
                self._connected = True
                return True, "Already connected"

            ensure_initialized()
            login_if_needed()
            info = ensure_symbol(SYMBOL)
            self._mt5_ready = True
            self._connected = True

        account = account_info()
        acc_txt = f"{account.login}" if account else "unknown"
        srv_txt = getattr(account, "server", "") if account else ""
        log_event("MT5_CONNECTED",
                  f"Account {acc_txt} | Server {srv_txt or SERVER or 'default'} | "
                  f"Symbol {SYMBOL}",
                  symbol=SYMBOL)
        return True, f"Connected (point {info.point}, digits {info.digits})"

    def _mark_connected(self):
        """Flag the MT5 link healthy again after a dropout (cheap fast path)."""
        if self._connected and self._mt5_ready:
            return
        self._connected = True
        self._mt5_ready = True
        self._reconnect_at = 0.0
        log("✓ MT5 link restored")
        log_event("MT5_CONNECTED", "Link restored", symbol=SYMBOL)
        self.notify("✅ MT5 connection restored.")

    def _attempt_reconnect(self):
        """Rate-limited reconnection used when the terminal drops out."""
        now = time.time()
        if now - self._reconnect_at < cfg.MT5_RECONNECT_SECONDS:
            return False
        self._reconnect_at = now

        if self._connected:
            self._connected = False
            log("⚠ MT5 connection lost - attempting reconnect...")
            log_event("MT5_DISCONNECTED", "Symbol info unavailable",
                      symbol=SYMBOL, status="ERROR")
            self.notify("⚠️ MT5 disconnected - attempting reconnect...")

        try:
            with MT5_LOCK:
                mt5.shutdown()          # drop the stale pipe, never duplicate it
            self._mt5_ready = False
            self.connect()
            log("✓ MT5 reconnected")
            self.notify("✅ MT5 reconnected.")
            return True
        except Exception as exc:
            log(f"✗ Reconnect failed: {exc}")
            log_event("ERROR", f"Reconnect failed: {exc}", symbol=SYMBOL,
                      status="ERROR")
            return False

    # ---------------------------------------------------------- lifecycle
    def start(self):
        """Start (or resume) the single trading loop. Idempotent."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self.is_paused():
                    return self.resume()
                return False, "Trading engine is already running."

            # A previous thread may be finishing - make sure it is gone.
            if self._thread:
                self._thread.join(timeout=5)
                self._thread = None

            try:
                self.connect()
            except Exception as exc:
                self._set_state(BotState.ERROR, str(exc))
                log(f"✗ Cannot start: {exc}")
                log_event("ERROR", f"Start failed: {exc}", symbol=SYMBOL,
                          status="ERROR")
                return False, f"Cannot start - {exc}"

            self._stop.clear()
            self._pause.clear()
            self._set_state(BotState.RUNNING)
            self.started_at = datetime.now()
            self._thread = threading.Thread(
                target=self._run, name="trading-engine", daemon=True
            )
            self._thread.start()

        log_event("BOT_STARTED", f"Trading engine started on {SYMBOL}",
                  symbol=SYMBOL)
        log("🟢 Trading engine started")
        return True, "Trading started."

    def pause(self):
        with self._lock:
            if not self.is_running():
                return False, "Engine is not running."
            if self.is_paused():
                return False, "Trading is already paused."
            self._pause.set()
            self._set_state(BotState.PAUSED)
        log_event("TRADING_PAUSED", "New entries disabled; positions still managed",
                  symbol=SYMBOL)
        log("⏸ Trading paused (existing positions still managed)")
        return True, "Trading paused."

    def resume(self):
        with self._lock:
            if not self.is_running():
                return False, "Engine is not running."
            if not self.is_paused():
                return False, "Trading is already active."
            self._pause.clear()
            self._set_state(BotState.RUNNING)
        log_event("TRADING_RESUMED", "New entries enabled", symbol=SYMBOL)
        log("▶️ Trading resumed")
        return True, "Trading resumed."

    def stop(self):
        """
        Stop the loop cleanly. Open positions are NEVER closed here - stopping
        the bot is not 'close all trades'. MT5 stays connected so STATUS /
        ACCOUNT / POSITIONS keep working.
        """
        with self._lock:
            thread = self._thread
            if not (thread and thread.is_alive()):
                self._set_state(BotState.STOPPED)
                return False, "Engine is already stopped."
            self._stop.set()

        thread.join(timeout=max(5.0, POLL_SECONDS * 10))
        with self._lock:
            self._thread = None
            self._pause.clear()
            self._set_state(BotState.STOPPED)

        log_event("BOT_STOPPED", "Trading loop stopped by request", symbol=SYMBOL)
        log("🛑 Trading engine stopped (positions left untouched)")
        return True, "Trading stopped."

    # ------------------------------------------------------------ reporting
    def status(self):
        account = account_info()
        positions = get_open_positions(SYMBOL) if self._mt5_ready else []
        connected = terminal_connected()
        state = self.state
        with self._lock:
            error = self._last_error
        uptime = ""
        if self.started_at and self.is_running():
            delta = datetime.now() - self.started_at
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            uptime = f"{hours}h {rem // 60}m"
        return {
            "state": state,
            "icon": STATE_ICONS.get(state, "⚪"),
            "paused": self.is_paused(),
            "symbol": SYMBOL,
            "mt5_connected": connected,
            "account": account,
            "open_positions": len(positions),
            "max_positions": MAX_OPEN_POSITIONS,
            "trades_today": self.trades_today,
            "risk_percent": RISK_PERCENT,
            "use_risk_percent": USE_RISK_PERCENT,
            "fixed_lot": FIXED_LOT,
            "rr": RR,
            "last_reason": self.last_signal_reason,
            "uptime": uptime,
            "error": error,
            "last_loop_at": self.last_loop_at,
        }

    def positions(self):
        return get_open_positions(SYMBOL)

    def account(self):
        return account_info()

    def today_stats(self):
        return self.csv.today_stats()

    # ------------------------------------------------------------- the loop
    def _run(self):
        """
        The original main loop, unchanged in ordering and timing. The only
        additions are the stop check, the pause gate for NEW entries, CSV
        hooks and reconnect handling.
        """
        try:
            info = symbol_info(SYMBOL)
            log("=" * 70)
            log(f"AGGRESSIVE SCALPER: {SYMBOL} (Margin-Safe)")
            log(f"Risk: {RISK_PERCENT}% per trade | Max {MAX_OPEN_POSITIONS} positions")
            log(f"SL: {SL_POINTS_MIN}-{SL_POINTS_MAX} pts | RR: {RR}:1")
            log(f"Target: 120-150%/month (15-25 trades/day) | HIGH RISK - DEMO ONLY")
            log("=" * 70)

            filling = choose_filling_mode(SYMBOL)
            log(f"Filling mode: {filling}")
            if info is not None:
                log(f"Point size: {info.point} | Digits: {info.digits}")

            last_m1_time = None
            start_time = datetime.now()
            paused_bar_logged = None

            while not self._stop.is_set():
                try:
                    self.last_loop_at = datetime.now()
                    self._roll_day()

                    info = symbol_info(SYMBOL)
                    if info is None:
                        self._attempt_reconnect()
                        self._sleep(POLL_SECONDS)
                        continue

                    self._mark_connected()

                    # Check position limit
                    open_positions = get_open_positions(SYMBOL)
                    if len(open_positions) >= MAX_OPEN_POSITIONS:
                        self._sleep(POLL_SECONDS)
                        continue

                    m1_rates = get_rates(SYMBOL, mt5.TIMEFRAME_M1, 3)
                    if len(m1_rates) < 2:
                        self._sleep(POLL_SECONDS)
                        continue

                    prev_time = datetime.fromtimestamp(m1_rates[-2]['time'], tz=timezone.utc)

                    if last_m1_time and prev_time <= last_m1_time:
                        self._sleep(POLL_SECONDS)
                        continue

                    # Manage open positions (runs while PAUSED too)
                    for position in open_positions:
                        tick = get_tick(SYMBOL)
                        if tick:
                            entry = position.price_open
                            point = info.point
                            direction = 1 if position.type == mt5.POSITION_TYPE_BUY else -1
                            price = tick.bid if direction == 1 else tick.ask
                            moved_points = direction * (price - entry) / point
                            sl_dist = direction * (entry - position.sl) / point if position.sl > 0 else None

                            # BE at 0.5R
                            if MOVE_TO_BREAKEVEN_AT_R and sl_dist:
                                if moved_points >= MOVE_TO_BREAKEVEN_AT_R * sl_dist:
                                    if abs(position.sl - entry) > point:
                                        new_sl = normalize_price(entry, info.digits)
                                        if modify_sl(position.ticket, new_sl, position.tp):
                                            log(f"→ BE (ticket {position.ticket})")
                                            log_event("BREAKEVEN",
                                                      f"SL moved to entry {new_sl}",
                                                      symbol=SYMBOL,
                                                      ticket=position.ticket)
                                            self.notify(
                                                f"🔒 Breakeven set on #{position.ticket} @ {new_sl}"
                                            )

                            # Partial at 1.5R
                            if PARTIAL_CLOSE_AT_R and sl_dist:
                                if moved_points >= PARTIAL_CLOSE_AT_R * sl_dist:
                                    if close_partial(position, PARTIAL_CLOSE_FRACTION):
                                        log(f"→ Partial (ticket {position.ticket})")
                                        log_event("PARTIAL_CLOSE",
                                                  f"Closed {PARTIAL_CLOSE_FRACTION:.0%} of "
                                                  f"{position.volume}",
                                                  symbol=SYMBOL,
                                                  ticket=position.ticket)
                                        self.notify(
                                            f"💸 Partial close on #{position.ticket} "
                                            f"({PARTIAL_CLOSE_FRACTION:.0%})"
                                        )

                    if len(open_positions) > 0:
                        self._sleep(POLL_SECONDS)
                        continue

                    # PAUSE gate: no NEW entries, everything above still ran
                    if self.is_paused():
                        last_m1_time = prev_time
                        if DIAGNOSTICS and paused_bar_logged != prev_time:
                            paused_bar_logged = prev_time
                            log("Skip: paused (new entries disabled)")
                        self._sleep(POLL_SECONDS)
                        continue

                    # New signal
                    signal, reason = build_signal(SYMBOL)
                    last_m1_time = prev_time
                    self.last_signal_reason = reason

                    if signal:
                        account = account_info()
                        lots = compute_lot(info, signal["sl_points"], account)

                        side = "BUY" if signal["type"] == mt5.ORDER_TYPE_BUY else "SELL"
                        log_event(
                            "SIGNAL_LONG" if reason == "long" else "SIGNAL_SHORT",
                            f"{side} setup | SL {int(signal['sl_points'])} pts | "
                            f"lots {lots}",
                            symbol=SYMBOL,
                        )

                        res = place_order(
                            SYMBOL,
                            signal["type"],
                            lots,
                            signal["sl"],
                            signal["tp"],
                            DEVIATION_POINTS
                        )

                        if res:
                            self.trades_today += 1
                            runtime = (datetime.now() - start_time).total_seconds() / 3600
                            log(f"Trade #{self.trades_today} | Bal: ${account.balance:.0f} | Runtime: {runtime:.1f}h")
                            self._record_open(res, signal, lots, side, info)

                            # Stats every 5 trades
                            if self.trades_today % 5 == 0:
                                log(f"📊 Used margin: ${account.margin:.0f} | Free: ${account.margin_free:.0f} | Level: {account.margin_level:.0f}%")
                    else:
                        if DIAGNOSTICS:
                            log(f"Skip: {reason}")

                    self._sleep(POLL_SECONDS)

                except KeyboardInterrupt:
                    log("Bot stopped by user")
                    break
                except Exception as e:
                    log(f"Loop error: {e}")
                    log_event("ERROR", f"Loop error: {e}", symbol=SYMBOL,
                              status="ERROR")
                    self._sleep(POLL_SECONDS)

        except Exception as exc:
            self._set_state(BotState.ERROR, str(exc))
            log(f"FATAL ERROR: {exc}")
            log_event("ERROR", f"Engine crashed: {exc}", symbol=SYMBOL,
                      status="FATAL")
            self.notify(f"🔴 Trading engine error: {exc}")
            return

        if self.state != BotState.ERROR:
            self._set_state(BotState.STOPPED)

    # ------------------------------------------------------- trade recording
    def _record_open(self, result, signal, lots, side, info):
        """Write the freshly opened trade to trades.csv (fields known so far)."""
        try:
            ticket = getattr(result, "order", None)
            price = getattr(result, "price", None)
            sl = signal["sl"]
            tp = signal["tp"]
            volume = getattr(result, "volume", None) or lots

            with MT5_LOCK:
                pos = mt5.positions_get(ticket=ticket) if ticket else None
            if pos:
                p = pos[0]
                ticket = p.ticket
                price = p.price_open
                sl = p.sl
                tp = p.tp
                volume = p.volume

            self.csv.log_trade(
                symbol=SYMBOL,
                ticket=ticket,
                direction=side,
                volume=volume,
                reason="OPEN",
                entry_price=price,
                stop_loss=sl,
                take_profit=tp,
                magic=MAGIC,
                digits=info.digits if info else 2,
            )
            log_event("TRADE_OPENED",
                      f"{side} {volume} @ {price} SL {sl} TP {tp}",
                      symbol=SYMBOL, ticket=ticket)
            self.notify(
                f"✅ <b>{side} {volume} {SYMBOL}</b>\n"
                f"Entry: {price}\nSL: {sl}\nTP: {tp}\nTicket: {ticket}"
            )
        except Exception as exc:
            log(f"⚠ Trade record failed: {exc}")
            log_event("ERROR", f"Trade record failed: {exc}", symbol=SYMBOL,
                      status="ERROR")


# ===========================================================================
# BACKGROUND MONITOR (CSV persistence - never touches the trading loop)
# ===========================================================================
class MonitorThread(threading.Thread):
    """
    Periodically writes account snapshots and reconciles closed trades from the
    MT5 deal history into trades.csv. Runs at a low frequency (seconds, not
    milliseconds) on its own thread so the 0.5s trading loop is untouched.
    """

    TICK_SECONDS = 2.0

    def __init__(self, engine, csv_logger=CSV):
        super().__init__(name="monitor", daemon=True)
        self.engine = engine
        self.csv = csv_logger
        self._stopped = threading.Event()
        self._last_snapshot = 0.0
        self._last_history = 0.0
        self._first_sync = True

    def stop(self):
        self._stopped.set()

    def run(self):
        while not self._stopped.is_set():
            try:
                now = time.time()
                if self.engine._mt5_ready:
                    if now - self._last_history >= cfg.HISTORY_SYNC_SECONDS:
                        self._last_history = now
                        self._sync_closed_trades()
                    if now - self._last_snapshot >= cfg.ACCOUNT_SNAPSHOT_INTERVAL:
                        self._last_snapshot = now
                        self._snapshot_account()
            except Exception as exc:
                log(f"⚠ Monitor error: {exc}")
                log_event("ERROR", f"Monitor error: {exc}", status="ERROR")
            self._stopped.wait(self.TICK_SECONDS)

    # ------------------------------------------------------------ snapshots
    def _snapshot_account(self):
        account = account_info()
        if not account:
            return
        positions = get_open_positions(SYMBOL)
        self.csv.log_account(
            balance=account.balance,
            equity=account.equity,
            margin=account.margin,
            free_margin=account.margin_free,
            margin_level=account.margin_level,
            open_positions=len(positions),
        )

    # -------------------------------------------------------- closed trades
    def _sync_closed_trades(self):
        """
        Record every closing deal exactly once. Deal ids make this idempotent,
        so restarts never duplicate - or lose - trade records.
        """
        now = datetime.now()
        frm = datetime.combine(now.date(), datetime.min.time()) - timedelta(days=1)
        with MT5_LOCK:
            deals = mt5.history_deals_get(frm, now + timedelta(minutes=1))
        if not deals:
            return

        entries = {}
        for deal in deals:
            if getattr(deal, "entry", None) == mt5.DEAL_ENTRY_IN:
                entries[deal.position_id] = deal

        info = symbol_info(SYMBOL)
        digits = info.digits if info else 2

        for deal in deals:
            if deal.symbol != SYMBOL or deal.magic != MAGIC:
                continue
            if getattr(deal, "entry", None) not in (mt5.DEAL_ENTRY_OUT,
                                                    mt5.DEAL_ENTRY_OUT_BY):
                continue
            if self.csv.has_trade(deal_id=deal.ticket):
                continue

            opener = entries.get(deal.position_id)
            # A SELL deal closes a BUY position and vice versa
            direction = "BUY" if deal.type == mt5.ORDER_TYPE_SELL else "SELL"
            if opener is not None:
                direction = "BUY" if opener.type == mt5.ORDER_TYPE_BUY else "SELL"

            with MT5_LOCK:
                still_open = mt5.positions_get(ticket=deal.position_id)
            reason = "PARTIAL_CLOSE" if still_open else "CLOSE"

            written = self.csv.log_trade(
                symbol=deal.symbol,
                ticket=deal.position_id,
                direction=direction,
                volume=deal.volume,
                reason=reason,
                entry_price=opener.price if opener is not None else None,
                close_price=deal.price,
                profit=deal.profit,
                commission=deal.commission,
                swap=deal.swap,
                magic=deal.magic,
                deal_id=deal.ticket,
                digits=digits,
            )
            if not written or self._first_sync:
                continue

            net = deal.profit + deal.commission + deal.swap
            emoji = "🟢" if net > 0 else ("🔴" if net < 0 else "⚪")
            log(f"{emoji} {reason} #{deal.position_id} {direction} {deal.volume} "
                f"@ {deal.price} | P/L ${net:.2f}")
            log_event("TRADE_CLOSED" if reason == "CLOSE" else "PARTIAL_CLOSE",
                      f"{direction} {deal.volume} @ {deal.price} | P/L ${net:.2f}",
                      symbol=deal.symbol, ticket=deal.position_id)
            self.engine.notify(
                f"{emoji} <b>{reason.replace('_', ' ').title()}</b> #{deal.position_id}\n"
                f"{direction} {deal.volume} @ {deal.price}\n"
                f"P/L: ${net:.2f}"
            )

        self._first_sync = False


# ===========================================================================
# APPLICATION LIFECYCLE
# ===========================================================================
class Application:
    """Wires the single trading engine, the monitor and the Telegram bot."""

    def __init__(self):
        self.engine = TradingEngine(CSV)
        self.monitor = MonitorThread(self.engine, CSV)
        self.telegram = None
        self._shutdown = threading.Event()

    # --------------------------------------------------------------- startup
    def banner(self):
        account = account_info()
        log("=" * 70)
        log("XAUUSD SCALPER - MT5 + TELEGRAM CONTROL")
        log("=" * 70)
        if account:
            log(f"MT5 Account: {account.login}")
            log(f"Server: {getattr(account, 'server', '') or SERVER or '(terminal default)'}")
            log(f"Account Type: {'DEMO' if 'demo' in str(getattr(account, 'server', '')).lower() else 'CHECK MT5 (live/demo)'}")
            log(f"Balance: ${account.balance:.2f} | Equity: ${account.equity:.2f}")
            log(f"MT5 Connected: {'YES' if terminal_connected() else 'NO'}")
        else:
            log("MT5 Account: (not connected)")
        log(f"Symbol: {SYMBOL}")
        log(f"Data directory: {cfg.DATA_PATH}")
        log(f"Telegram control: {'ENABLED' if cfg.TELEGRAM_ENABLED else 'DISABLED'}")
        log("=" * 70)

        log_event(
            "BOT_STARTED",
            f"App start | account={account.login if account else 'n/a'} | "
            f"server={getattr(account, 'server', '') if account else ''} | "
            f"symbol={SYMBOL}",
            symbol=SYMBOL,
        )

    def start(self):
        errors, warnings = cfg.validate()
        for warning in warnings:
            log(f"⚠ CONFIG: {warning}")
        if errors:
            for error in errors:
                log(f"✗ CONFIG ERROR: {error}")
            log_event("ERROR", "; ".join(errors), status="FATAL")
            raise SystemExit(
                "Configuration is invalid - fix the errors above in your .env file."
            )

        # MT5 first; if the terminal is not ready the app still comes up so the
        # Telegram panel can report the error and retry with START.
        try:
            self.engine.connect()
        except Exception as exc:
            log(f"✗ MT5 connection failed: {exc}")
            log_event("ERROR", f"MT5 connection failed: {exc}", status="ERROR")

        self.banner()
        self.monitor.start()

        if cfg.TELEGRAM_ENABLED:
            try:
                from telegram_controller import TelegramController
                self.telegram = TelegramController(self.engine, CSV)
                self.telegram.start()
                self.engine.set_notifier(self.telegram.notify)
                log("✓ Telegram controller started - send /start to your bot")
            except Exception as exc:
                self.telegram = None
                log(f"⚠ Telegram controller failed to start: {exc}")
                log(" Trading continues without remote control.")
                log_event("ERROR", f"Telegram start failed: {exc}", status="ERROR")
        else:
            log("⚠ TELEGRAM_BOT_TOKEN is not set in .env - remote control disabled.")

        if cfg.AUTO_START_TRADING:
            ok, msg = self.engine.start()
            if not ok:
                log(f"⚠ Auto-start: {msg}")
        else:
            log("AUTO_START_TRADING=false - press 🟢 START TRADING in Telegram.")

    # -------------------------------------------------------------- shutdown
    def request_shutdown(self, *_args):
        self._shutdown.set()

    def wait(self):
        try:
            while not self._shutdown.is_set():
                self._shutdown.wait(1.0)
        except KeyboardInterrupt:
            pass

    def shutdown(self):
        log("Shutting down...")
        try:
            self.engine.stop()
        except Exception as exc:
            log(f"⚠ Engine stop error: {exc}")
        try:
            self.monitor.stop()
            self.monitor.join(timeout=5)
        except Exception as exc:
            log(f"⚠ Monitor stop error: {exc}")
        if self.telegram:
            try:
                self.telegram.stop()
            except Exception as exc:
                log(f"⚠ Telegram stop error: {exc}")
        log_event("BOT_STOPPED", "Application shutdown", symbol=SYMBOL)
        with MT5_LOCK:
            mt5.shutdown()
        log("MT5 connection closed")


def main():
    app = Application()
    try:
        signal.signal(signal.SIGINT, app.request_shutdown)
        signal.signal(signal.SIGTERM, app.request_shutdown)
    except (ValueError, AttributeError):
        pass  # not the main thread / platform without SIGTERM

    try:
        app.start()
        app.wait()
    except SystemExit:
        raise
    except Exception as exc:
        log(f"FATAL ERROR: {exc}")
        log("\nTroubleshooting:")
        log("1. Make sure MT5 desktop is open and logged in")
        log("2. Check the symbol is in Market Watch")
        log("3. Verify server name matches MT5")
        log_event("ERROR", f"Fatal: {exc}", status="FATAL")
        sys.exit(1)
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
