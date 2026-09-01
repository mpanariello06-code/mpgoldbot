"""
XAUUSD M5 ROLLING LADDER SCALPER
================================

    MT5 market data
          v
    ROLLING LADDER ENGINE      (ladder_engine.py)
          v
    level calculation          grid anchored per cycle, rolls with price
          v
    risk / spread check
          v
    pending order manager      BUY STOP above / SELL STOP below, idempotent
          v
    execution                  broker.py: Mt5Broker (LIVE) or PaperBroker (PAPER)
          v
    TP management -> roll ladder -> profit cycle
          v
    Telegram + CSV

There is no signal engine and no indicators: the ladder itself is the entry
mechanism. This file owns the infrastructure - MT5 connection, lifecycle,
threading, notifications and CSV plumbing - and drives one RollingLadderEngine.

Run:  pip install -r requirements.txt && python optimized.py
"""

import os
import signal
import sys
import threading
import time
from datetime import datetime

import MetaTrader5 as mt5

import config as cfg
from broker import MT5_LOCK, Mt5Broker, PaperBroker
from csv_logger import CsvLogger
from ladder_engine import RollingLadderEngine, State
from notifications import TelegramNotifier
from price_utils import SymbolSpec
from runtime_settings import RuntimeSettings

# ===========================================================================
# CONFIGURATION
# ===========================================================================
LOGIN = cfg.LOGIN
PASSWORD = cfg.PASSWORD
SERVER = cfg.SERVER
SYMBOL = cfg.SYMBOL
TIMEFRAME = cfg.TIMEFRAME
MAGIC = cfg.MAGIC
POLL_SECONDS = cfg.POLL_SECONDS
DIAGNOSTICS = cfg.DIAGNOSTICS
POSSIBLE_MT5_PATHS = cfg.POSSIBLE_MT5_PATHS

MT5_TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1,
}

# ===========================================================================
# SHARED INFRASTRUCTURE
# ===========================================================================
CSV = CsvLogger(
    cfg.DATA_PATH,
    cfg.TRADE_LOG_FILE,
    cfg.EVENT_LOG_FILE,
    cfg.ACCOUNT_LOG_FILE,
    cfg.LADDER_LOG_FILE,
    cfg.CYCLE_LOG_FILE,
)


def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def _on_setting_changed(key, old_value, new_value):
    """Record every Telegram settings change in events.csv."""
    if key == "*":
        log("♻️ Settings reset to the original configuration")
        CSV.log_event("SETTINGS_RESET", "Restored original configuration",
                      symbol=SYMBOL)
        return
    log(f"⚙️ Setting changed: {key} {old_value} → {new_value}")
    CSV.log_event("SETTINGS_CHANGED", f"{key}: {old_value} -> {new_value}",
                  symbol=SYMBOL)


SETTINGS = RuntimeSettings(
    defaults=cfg.runtime_defaults(),
    path=cfg.DATA_PATH / cfg.RUNTIME_SETTINGS_FILE,
    on_change=_on_setting_changed,
)


def log_event(event_type, message="", symbol="", ticket="", status="OK",
              console=False):
    """Console (optional) + events.csv."""
    if console:
        log(message or event_type)
    CSV.log_event(event_type, message, symbol, ticket, status)


# ===========================================================================
# MT5 CONNECTION
# ===========================================================================
def find_mt5_path():
    if cfg.MT5_PATH and os.path.exists(cfg.MT5_PATH):
        return cfg.MT5_PATH
    for path in POSSIBLE_MT5_PATHS:
        if os.path.exists(path):
            return path
    return None


def ensure_initialized():
    log("Connecting to MT5...")

    with MT5_LOCK:
        if mt5.initialize():
            log("✓ Connected (auto-detect)")
            return True

        log(f"Auto-detect failed: {mt5.last_error()}")

        mt5_path = find_mt5_path()
        if mt5_path:
            log(f"Trying: {mt5_path}")
            if mt5.initialize(path=mt5_path):
                log("✓ Connected (explicit path)")
                return True
            log(f"Path failed: {mt5.last_error()}")

        last_error = mt5.last_error()

    raise RuntimeError(
        f"MT5 connection failed. Last error: {last_error}\n"
        "Fix:\n"
        "1. Open MetaTrader 5 desktop app manually\n"
        "2. Login to your account\n"
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
        else:
            log("⚠ No account info")


def ensure_symbol(symbol):
    with MT5_LOCK:
        info = mt5.symbol_info(symbol)
        if info and info.visible:
            return info

        if info and not info.visible:
            log(f"Enabling {symbol} in Market Watch...")
            if mt5.symbol_select(symbol, True):
                return mt5.symbol_info(symbol)

        all_symbols = mt5.symbols_get()

    if all_symbols:
        needle = symbol.upper().rstrip("._-")[:6]
        cands = [s.name for s in all_symbols
                 if needle in s.name.upper() or "GOLD" in s.name.upper()]
        if cands:
            log(f"Symbol '{symbol}' not found. Try: {cands[:5]}")

    raise RuntimeError(f"Symbol '{symbol}' unavailable. Check Market Watch in MT5.")


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
    return rates if rates is not None else []


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
# LADDER BOT - lifecycle + threading around the ladder engine
# ===========================================================================
class LadderBot:
    """
    Owns the one and only ladder loop.

    START/PAUSE/RESUME/STOP are idempotent and thread safe: pressing START
    twice can never create a second loop, and PAUSE stops new entries while the
    existing positions keep being managed.
    """

    def __init__(self, csv_logger=CSV, settings=SETTINGS):
        self.csv = csv_logger
        self.settings = settings

        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._pause = threading.Event()

        self._state = BotState.STOPPED
        self._last_error = ""
        self._mt5_ready = False
        self._connected = False

        self.broker = None
        self.engine = None
        self.started_at = None
        self.last_loop_at = None
        self.last_candle_time = None

        self._notifier = None
        # Telegram policy: important events only. Everything else goes to CSV.
        self.notifier = TelegramNotifier(self.notify, settings=self.settings)
        self._reconnect_at = 0.0
        self._last_candle_check = 0.0

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
        except Exception as exc:
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
        self._stop.wait(seconds)

    @property
    def symbol(self):
        return SYMBOL

    def symbol_info_live(self):
        return symbol_info(SYMBOL)

    # ------------------------------------------------------- MT5 connection
    def connect(self):
        """Initialise MT5 + symbol + broker. Safe to call repeatedly."""
        with self._lock:
            if self._mt5_ready and terminal_connected() and symbol_info(SYMBOL):
                self._connected = True
                return True, "Already connected"

            ensure_initialized()
            login_if_needed()
            info = ensure_symbol(SYMBOL)
            self._mt5_ready = True
            self._connected = True
            self._build_broker()

        ok, why = self.broker.tradable()
        if not ok:
            log(f"⚠ {why}")
            log_event("SYMBOL_NOT_TRADABLE", why, symbol=SYMBOL, status="ERROR")
        spec = self.broker.symbol_spec()
        log(spec.describe())
        if spec.min_stop_distance > float(self.settings.get("ladder_spacing")):
            msg = (f"broker minimum stop distance {spec.min_stop_distance:g} is "
                   f"wider than the ladder spacing "
                   f"{self.settings.get('ladder_spacing')} - levels will be "
                   f"pushed further out than configured")
            log(f"⚠ {msg}")
            log_event("SPACING_BELOW_MIN_STOP", msg, symbol=SYMBOL,
                      status="WARNING")

        account = account_info()
        log_event("MT5_CONNECTED",
                  f"Account {getattr(account, 'login', '?')} | "
                  f"Server {getattr(account, 'server', '') or SERVER or 'default'} | "
                  f"Symbol {SYMBOL} | Mode {self.broker.name}",
                  symbol=SYMBOL)
        return True, f"Connected (point {info.point}, digits {info.digits})"

    def _build_broker(self):
        """Live or paper execution, decided once by TRADING_MODE."""
        if self.broker is not None:
            return
        snap = self.settings.snapshot()
        if cfg.TRADING_MODE == "LIVE":
            self.broker = Mt5Broker(SYMBOL, MAGIC,
                                    deviation_points=snap["max_slippage"],
                                    pip_points_override=snap["pip_points"])
        else:
            self.broker = PaperBroker(
                SYMBOL, MAGIC,
                spec_provider=lambda: SymbolSpec.from_mt5(
                    symbol_info(SYMBOL), self.settings.get("pip_points")),
                tick_provider=self._live_tick,
                state_path=cfg.DATA_PATH / cfg.PAPER_STATE_FILE,
                start_balance=cfg.PAPER_START_BALANCE,
                max_slippage_points=snap["max_slippage"],
                commission_per_lot=cfg.COMMISSION_PER_LOT,
            )
        self.engine = RollingLadderEngine(
            broker=self.broker,
            settings=self.settings,
            hooks=self._hooks(),
            state_path=cfg.DATA_PATH / cfg.LADDER_STATE_FILE,
        )

    def _live_tick(self):
        from broker import Tick
        with MT5_LOCK:
            t = mt5.symbol_info_tick(SYMBOL)
        if t is None:
            raise RuntimeError(f"no tick for {SYMBOL}")
        return Tick(bid=float(t.bid), ask=float(t.ask), time=float(t.time))

    def _attempt_reconnect(self):
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
                mt5.shutdown()
            self._mt5_ready = False
            self.connect()
            # the engine re-reads MT5 rather than trusting its own memory
            self.engine.resume()
            log("✓ MT5 reconnected, ladder state re-read from the broker")
            self.notify("✅ MT5 reconnected - ladder state re-synchronised.")
            return True
        except Exception as exc:
            log(f"✗ Reconnect failed: {exc}")
            log_event("ERROR", f"Reconnect failed: {exc}", symbol=SYMBOL,
                      status="ERROR")
            return False

    def _mark_connected(self):
        if self._connected and self._mt5_ready:
            return
        self._connected = True
        self._mt5_ready = True
        self._reconnect_at = 0.0
        log("✓ MT5 link restored")
        log_event("MT5_CONNECTED", "Link restored", symbol=SYMBOL)
        self.notify("✅ MT5 connection restored.")

    # ---------------------------------------------------------- lifecycle
    def start(self):
        """Start (or resume) the single ladder loop. Idempotent."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self.is_paused():
                    return self.resume()
                return False, "Ladder engine is already running."

            if self._thread:
                self._thread.join(timeout=5)
                self._thread = None

            try:
                self.connect()
                self.engine.resume()          # rebuild state from MT5
            except Exception as exc:
                self._set_state(BotState.ERROR, str(exc))
                log(f"✗ Cannot start: {exc}")
                log_event("ERROR", f"Start failed: {exc}", symbol=SYMBOL,
                          status="ERROR")
                return False, f"Cannot start - {exc}"

            self._stop.clear()
            self._pause.clear()
            self.engine.paused = False
            self._set_state(BotState.RUNNING)
            self.started_at = datetime.now()
            self._thread = threading.Thread(
                target=self._run, name="ladder-engine", daemon=True)
            self._thread.start()

        log_event("BOT_STARTED", f"Rolling ladder started on {SYMBOL} "
                                 f"({self.broker.name})", symbol=SYMBOL)
        log("🟢 Rolling ladder engine started - deploying the ladder now")
        snap = self.settings.snapshot()
        # the START message already announces the first ladder; only cycles
        # that follow a close get their own deploy message
        self._cycles_closed = 0
        self.notifier.bot_started(SYMBOL, snap["timeframe"],
                                  snap["ladder_spacing"],
                                  self.engine.cycle.cycle_id, cfg.TRADING_MODE)
        return True, "Trading started."

    def pause(self):
        with self._lock:
            if not self.is_running():
                return False, "Engine is not running."
            if self.is_paused():
                return False, "Trading is already paused."
            self._pause.set()
            if self.engine:
                self.engine.paused = True
            self._set_state(BotState.PAUSED)
        log_event("TRADING_PAUSED",
                  "New ladder entries disabled; open positions still managed",
                  symbol=SYMBOL)
        log("⏸ Trading paused (pending orders cancelled, positions still managed)")
        return True, "Trading paused."

    def resume(self):
        with self._lock:
            if not self.is_running():
                return False, "Engine is not running."
            if not self.is_paused():
                return False, "Trading is already active."
            self._pause.clear()
            if self.engine:
                self.engine.paused = False
            self._set_state(BotState.RUNNING)
        log_event("TRADING_RESUMED", "Ladder entries enabled", symbol=SYMBOL)
        log("▶️ Trading resumed")
        return True, "Trading resumed."

    def stop(self):
        """
        Stop the loop cleanly.

        Pending orders are cancelled (they would open trades nobody is managing),
        open positions are left exactly as they are - stopping the bot is not
        'close everything'.
        """
        with self._lock:
            thread = self._thread
            if not (thread and thread.is_alive()):
                self._set_state(BotState.STOPPED)
                return False, "Engine is already stopped."
            self._stop.set()

        thread.join(timeout=max(5.0, POLL_SECONDS * 20))
        cancelled = 0
        try:
            if self.engine and cfg.TRADING_MODE:
                for order in self.broker.orders():
                    ok, _ = self.broker.cancel_order(order.ticket)
                    cancelled += 1 if ok else 0
        except Exception as exc:
            log(f"⚠ Could not cancel pending orders on stop: {exc}")

        with self._lock:
            self._thread = None
            self._pause.clear()
            self._set_state(BotState.STOPPED)

        log_event("BOT_STOPPED",
                  f"Ladder stopped by request ({cancelled} pending orders cancelled)",
                  symbol=SYMBOL)
        log(f"🛑 Ladder engine stopped ({cancelled} pending orders cancelled, "
            f"open positions untouched)")
        self.notifier.bot_stopped(
            SYMBOL, self.engine.cycle.cycle_id if self.engine else 0,
            len(self.positions()), len(self.orders()),
            self.engine.session_profit if self.engine else 0.0)
        return True, f"Trading stopped. {cancelled} pending orders cancelled."

    # ------------------------------------------------------------ reporting
    def status(self):
        connected = terminal_connected()
        state = self.state
        with self._lock:
            error = self._last_error
        uptime = ""
        if self.started_at and self.is_running():
            delta = datetime.now() - self.started_at
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            uptime = f"{hours}h {rem // 60}m"

        data = {
            "state": state,
            "icon": STATE_ICONS.get(state, "⚪"),
            "paused": self.is_paused(),
            "symbol": SYMBOL,
            "mt5_connected": connected,
            "account": account_info(),
            "uptime": uptime,
            "error": error,
            "last_loop_at": self.last_loop_at,
            "mode": cfg.TRADING_MODE,
        }
        if self.engine:
            data.update(self.engine.snapshot())
            # `state` is the market reading from the exit engine; `lifecycle`
            # is what the bot itself is doing.
            data["lifecycle"] = state
            data["engine_state"] = self.engine.state
            data["icon"] = STATE_ICONS.get(state, "⚪")
        else:
            data.update({"lifecycle": state, "engine_state": State.IDLE,
                         "positions": 0, "orders": 0,
                         "cycle_id": 0, "tp_count": 0, "cycle_profit": 0.0,
                         "daily_profit": 0.0, "spread": None, "bid": None,
                         "ask": None})
            snap = self.settings.snapshot()
            data.update({"spacing": snap["ladder_spacing"],
                         "depth": snap["ladder_depth"],
                         "tp_mode": snap["tp_mode"],
                         "tp_distance": snap["tp_distance"],
                         "lot": snap["lot_size"],
                         "exit_score": 0.0, "decision": "CONTINUE",
                         "timeframe": snap["timeframe"]})
        return data

    def positions(self):
        try:
            return self.broker.positions() if self.broker else []
        except Exception:
            return []

    def orders(self):
        try:
            return self.broker.orders() if self.broker else []
        except Exception:
            return []

    def account(self):
        if self.broker and self.broker.is_paper:
            try:
                return self.broker.account()
            except Exception:
                pass
        return account_info()

    def today_stats(self):
        stats = self.csv.ladder_stats()
        trades = self.csv.today_stats()
        if stats.get("available") and trades.get("available"):
            stats["closed"] = trades.get("closed", 0)
            stats["wins"] = trades.get("wins", 0)
            stats["losses"] = trades.get("losses", 0)
            stats["win_rate"] = trades.get("win_rate")
        return stats

    # ------------------------------------------------------------- the loop
    def _run(self):
        try:
            spec = self.broker.symbol_spec()
            snap = self.settings.snapshot()
            log("=" * 70)
            log(f"ROLLING LADDER SCALPER: {SYMBOL} {TIMEFRAME} [{self.broker.name}]")
            log(f"Spacing {snap['ladder_spacing']} | Depth {snap['ladder_depth']} "
                f"| TP {snap['tp_mode']} | Lot {snap['lot_size']}")
            log(f"Adaptive exit at score {snap['exit_threshold_exit']:g} | "
                f"Max {snap['max_open_positions']} positions / "
                f"{snap['max_pending_orders']} pendings / "
                f"depth {snap['max_ladder_depth']}")
            log(spec.describe())
            log("=" * 70)

            while not self._stop.is_set():
                try:
                    self.last_loop_at = datetime.now()

                    info = symbol_info(SYMBOL)
                    if info is None:
                        self._attempt_reconnect()
                        self._sleep(POLL_SECONDS)
                        continue
                    self._mark_connected()

                    self._check_new_candle()
                    self.engine.step()
                    self._state_alert()

                    self._sleep(POLL_SECONDS)

                except KeyboardInterrupt:
                    log("Bot stopped by user")
                    break
                except Exception as exc:
                    log(f"Loop error: {exc}")
                    log_event("ERROR", f"Loop error: {exc}", symbol=SYMBOL,
                              status="ERROR")
                    self._sleep(POLL_SECONDS)

        except Exception as exc:
            self._set_state(BotState.ERROR, str(exc))
            log(f"FATAL ERROR: {exc}")
            log_event("ERROR", f"Engine crashed: {exc}", symbol=SYMBOL,
                      status="FATAL")
            self.notify(f"🔴 Ladder engine error: {exc}")
            return

        if self.state != BotState.ERROR:
            self._set_state(BotState.STOPPED)

    def _state_alert(self):
        """
        Announce a change of market state, once per transition.

        The notifier drops repeats, so calling this every pass costs nothing and
        the chat only hears about NORMAL -> REVERSAL and similar transitions.
        """
        engine = self.engine
        if not (engine and engine.assessment and engine.sequence):
            return
        seq = engine.sequence
        self.notifier.state_change(
            symbol=SYMBOL, cycle_id=engine.cycle.cycle_id,
            market_state=engine.assessment.state,
            buys=seq.buy_triggers, sells=seq.sell_triggers,
            imbalance=seq.imbalance, dominant=seq.dominant_side)

    def _check_new_candle(self):
        """
        M5 candles are context only - the ladder never waits for a candle close,
        neither to start nor after a cycle ends. Polling for one every 0.5s
        would be wasted work, so it is checked a few times a minute.
        """
        now = time.time()
        if now - self._last_candle_check < 5.0:
            return
        self._last_candle_check = now
        tf = MT5_TIMEFRAMES.get(self.settings.get("timeframe"), mt5.TIMEFRAME_M5)
        rates = get_rates(SYMBOL, tf, 2)
        if len(rates) < 2:
            return
        candle_time = int(rates[-1]["time"])
        if self.last_candle_time is None:
            self.last_candle_time = candle_time
            return
        if candle_time == self.last_candle_time:
            return
        self.last_candle_time = candle_time
        if self.settings.get("m5_candle_reset"):
            self.engine.reanchor(reason="new candle")

    # --------------------------------------------------------------- hooks
    def _hooks(self):
        return {
            "event": self._on_event,
            "entry": self._on_entry,
            "closed": self._on_closed,
            "cycle_started": self._on_cycle_started,
            "cycle_complete": self._on_cycle_complete,
            "risk_blocked": self._on_risk_blocked,
        }

    def _on_event(self, event, message, fields):
        """
        Every ladder event -> console + events.csv + rolling_ladder_events.csv.

        The ladder row carries the full market state at the moment of the
        event - trigger sequence, imbalance, momentum/reversal/exhaustion and
        the exit score - so the exit rules can be re-fitted from the log later
        instead of from memory.
        """
        noisy = event in ("ORDER_PLACED", "ORDER_CANCELLED")
        if not noisy or DIAGNOSTICS:
            log(f"[{event}] {message}")
        if event == "ERROR":
            self.notifier.error(message, action="Retrying")
        elif event == "CYCLE_ACTIVE" and getattr(self, "_cycles_closed", 0):
            # the new ladder is confirmed live in MT5 - the second and last
            # message of the exit -> cooldown -> re-entry sequence. The first
            # ladder of a run is already covered by the START message.
            self.notifier.cycle_started(
                symbol=SYMBOL,
                timeframe=self.settings.get("timeframe"),
                cycle_id=fields.get("cycle_id", ""),
                levels=fields.get("levels_live", 0))
        elif event == "ORDER_REJECTED":
            # the broker refusing an order is never silent - throttled per side
            self.notifier.error(message, action="Retrying the level",
                                key=f"rejected:{fields.get('direction')}")
        elif event == "CYCLE_CLOSE_PENDING":
            self.notifier.error(message, action="Reconciling before the next cycle",
                                key=f"pending:{fields.get('cycle_id')}")
        CSV.log_event(event, message, symbol=SYMBOL,
                      ticket=fields.get("position_ticket") or
                      fields.get("order_ticket") or "",
                      status=fields.get("status", "OK"))

        engine = self.engine
        spec = engine.spec if engine else None
        row = {
            "symbol": SYMBOL,
            "candle_time": self.last_candle_time or "",
            "side": fields.get("direction", ""),
            "ladder_index": fields.get("level", ""),
            "ladder_price": fields.get("entry_price"),
            "action": fields.get("status", ""),
            "reason": message,
        }
        for key in ("cycle_id", "entry_price", "exit_price", "tp", "lot_size",
                    "spread", "order_ticket", "position_ticket", "profit",
                    "cycle_profit", "daily_profit"):
            if key in fields:
                row[key] = fields[key]
        row["sl_if_used"] = fields.get("sl", "")

        if engine is not None and engine.sequence is not None:
            seq = engine.sequence.snapshot()
            row.update({
                "buy_trigger_count": seq["buy_triggers"],
                "sell_trigger_count": seq["sell_triggers"],
                "consecutive_buy": seq["consecutive_buy"],
                "consecutive_sell": seq["consecutive_sell"],
                "last_side": seq["last_side"],
                "previous_side": seq["previous_side"],
                "direction_changes": seq["direction_changes"],
                "buy_sell_ratio": seq["buy_sell_ratio"],
                "sell_buy_ratio": seq["sell_buy_ratio"],
                "imbalance": seq["imbalance"],
                "ladder_depth_used": seq["ladder_depth_used"],
                "price_distance_traveled": seq["price_distance_traveled"],
                "net_levels": seq["net_levels"],
                "efficiency": seq["efficiency"],
                "time_since_previous_trigger": seq["time_since_last_trigger"],
                "volatility": seq["volatility"],
                "basket_pnl": seq["basket_pnl"],
                "basket_drawdown": seq["basket_drawdown"],
            })
            row.setdefault("cycle_id", seq["cycle_id"])
        if engine is not None and engine.assessment is not None:
            a = engine.assessment
            row.update({
                "momentum_score": round(a.momentum_score, 3),
                "continuation_score": round(a.continuation_score, 3),
                "reversal_score": round(a.reversal_score, 3),
                "exhaustion_score": round(a.exhaustion_score, 3),
                "exit_score": round(a.exit_score, 1),
                "decision": a.decision,
                "market_state": a.state,
            })
        CSV.log_ladder(event, digits=spec.digits if spec else 2, **row)

    def _on_entry(self, position, index, cycle):
        """
        A level triggered: recorded in full, announced only if the operator
        explicitly asked for per-entry pings (off by default - this strategy
        would flood the chat).
        """
        snap = self.settings.snapshot()
        spec = self.engine.spec
        self.csv.log_trade(
            symbol=SYMBOL, ticket=position.ticket, direction=position.side,
            volume=position.volume, reason="OPEN",
            entry_price=position.price_open, stop_loss=position.sl,
            take_profit=position.tp, magic=MAGIC, digits=spec.digits,
            cycle_id=cycle.cycle_id, level=index, tp_mode=snap["tp_mode"],
            tp_distance=self.engine.tp_distance(snap),
            spread=self.engine.last_tick.spread if self.engine.last_tick else None,
        )
        if not snap.get("telegram_entry_alerts"):
            return                        # the default: no per-entry messages
        d = spec.digits
        target = (f" → {position.tp:.{d}f}" if position.tp else " (basket)")
        self.notify(
            f"🟢 <b>LADDER ENTRY</b>  {position.side} {position.volume}\n"
            f"{position.price_open:.{d}f}{target}  "
            f"(cycle #{cycle.cycle_id}, level {index:+d})"
        )

    def _on_closed(self, trade, index, cycle, is_win):
        """A position closed: recorded in full, never announced individually."""
        spec = self.engine.spec
        snap = self.settings.snapshot()
        self.csv.log_trade(
            symbol=SYMBOL, ticket=trade.ticket, direction=trade.side,
            volume=trade.volume, reason="CLOSE", entry_price=trade.price_open,
            close_price=trade.price_close, profit=trade.profit, magic=MAGIC,
            digits=spec.digits, deal_id=f"{trade.ticket}-{int(trade.time_close)}",
            cycle_id=cycle.cycle_id, level=index, tp_mode=snap["tp_mode"],
        )

    def _on_cycle_started(self, cycle, anchor):
        log(f"🪜 Cycle #{cycle.cycle_id} anchored at {anchor}")

    def _on_cycle_complete(self, cycle, sequence, assessment, total, reason,
                           kind, lost, duration=0.0, next_cycle_id=None,
                           context=None, next_ladder_seconds=0.0):
        seq = sequence.snapshot() if sequence is not None else {}
        spec = self.engine.spec if self.engine else None
        # the market/exposure state at the moment the exit was decided
        ctx = context or {}
        CSV.log_cycle(
            digits=spec.digits if spec else 2,
            symbol=SYMBOL,
            cycle_id=cycle.cycle_id,
            started_at=datetime.fromtimestamp(cycle.started_at).strftime(
                "%Y-%m-%d %H:%M:%S"),
            duration_seconds=round(time.time() - cycle.started_at, 1),
            anchor=cycle.anchor,
            initial_price=cycle.anchor,
            exit_price=ctx.get("exit_price", ""),
            exit_spread=ctx.get("exit_spread", ""),
            spacing=self.settings.get("ladder_spacing"),
            triggers=seq.get("total_triggers", cycle.trades),
            buy_triggers=seq.get("buy_triggers", ""),
            sell_triggers=seq.get("sell_triggers", ""),
            direction_changes=seq.get("direction_changes", ""),
            imbalance=seq.get("imbalance", ""),
            ladder_depth_used=seq.get("ladder_depth_used", ""),
            net_levels=seq.get("net_levels", ""),
            path_levels=seq.get("path_levels", ""),
            efficiency=seq.get("efficiency", ""),
            tp_count=cycle.tp_count,
            positions_at_exit=ctx.get("open_positions_at_exit", ""),
            open_buys_at_exit=ctx.get("open_buys_at_exit", ""),
            open_sells_at_exit=ctx.get("open_sells_at_exit", ""),
            pending_orders_at_exit=ctx.get("pending_orders_at_exit", ""),
            floating_pnl_at_exit=ctx.get("floating_pnl_at_exit", ""),
            realized_pnl=total,
            peak_pnl=seq.get("peak_pnl", ""),
            drawdown=seq.get("basket_drawdown", ""),
            momentum_score=round(assessment.momentum_score, 3) if assessment else "",
            continuation_score=round(assessment.continuation_score, 3) if assessment else "",
            reversal_score=round(assessment.reversal_score, 3) if assessment else "",
            exhaustion_score=round(assessment.exhaustion_score, 3) if assessment else "",
            directional_score=round(assessment.directional_score, 3) if assessment else "",
            extended_score=round(assessment.extended_score, 3) if assessment else "",
            exit_score=round(assessment.exit_score, 1) if assessment else "",
            market_state=assessment.state if assessment else "",
            exit_scenario=assessment.scenario if assessment else "",
            end_kind=kind,
            end_reason=reason,
            daily_profit=self.engine.daily_profit if self.engine else "",
        )

        reason_word = {
            "SCENARIO_1_DIRECTIONAL": "Directional move",
            "SCENARIO_2_REVERSAL": "Reversal",
            "SCENARIO_3_EXTENDED_LADDER": "Extended ladder",
            "PROFIT_FALLBACK": "Profit fallback",
            "RISK_DRAWDOWN": "Risk drawdown",
            "RISK_TIMEOUT": "Risk timeout",
            "RISK_SPREAD": "Risk spread",
            "OTHER_RISK_EXIT": "Risk exit",
            "EXIT_ENGINE": "Exit engine",
        }.get(kind, kind.replace("_", " ").title())
        self._cycles_closed = getattr(self, "_cycles_closed", 0) + 1
        wait = float(next_ladder_seconds or 0.0)
        log(f"🔄 Cycle #{cycle.cycle_id} closed ({reason_word}, {total:+.2f}) "
            + (f"-> next ladder in {wait:.0f}s" if wait > 0
               else "-> next ladder deploying now"))
        self.notifier.cycle_closed(
            symbol=SYMBOL, cycle_id=cycle.cycle_id, total=total,
            buys=seq.get("buy_triggers", 0), sells=seq.get("sell_triggers", 0),
            reason_word=reason_word, direction=seq.get("dominant_side", ""),
            duration_seconds=duration,
            next_cycle_id=next_cycle_id if next_cycle_id is not None
            else cycle.cycle_id + 1,
            next_ladder_seconds=wait, kind=kind)

    def _on_risk_blocked(self, reason):
        self.notifier.risk_event(
            "RISK BLOCK", f"{reason}\n\nNew entries stopped, pending orders "
                          f"cancelled.\nOpen positions keep running.",
            key=f"risk:{reason[:40]}")


# ===========================================================================
# BACKGROUND MONITOR (account snapshots)
# ===========================================================================
class MonitorThread(threading.Thread):
    """Low-frequency account snapshots. Never touches the ladder loop."""

    TICK_SECONDS = 2.0

    def __init__(self, bot, csv_logger=CSV):
        super().__init__(name="monitor", daemon=True)
        self.bot = bot
        self.csv = csv_logger
        self._stopped = threading.Event()
        self._last_snapshot = 0.0
        self._last_status = 0.0

    def stop(self):
        self._stopped.set()

    def run(self):
        while not self._stopped.is_set():
            try:
                now = time.time()
                if self.bot._mt5_ready and \
                        now - self._last_snapshot >= cfg.ACCOUNT_SNAPSHOT_INTERVAL:
                    self._last_snapshot = now
                    self._snapshot()
                # the periodic heartbeat is built off the trading loop, and the
                # notifier decides whether it is due at all
                if self.bot.is_running() and now - self._last_status >= 30:
                    self._last_status = now
                    self.bot.notifier.periodic_status(self.bot.status())
            except Exception as exc:
                log(f"⚠ Monitor error: {exc}")
                log_event("ERROR", f"Monitor error: {exc}", status="ERROR")
            self._stopped.wait(self.TICK_SECONDS)

    def _snapshot(self):
        account = self.bot.account()
        if not account:
            return
        self.csv.log_account(
            balance=account.balance, equity=account.equity,
            margin=getattr(account, "margin", 0.0),
            free_margin=getattr(account, "margin_free", 0.0),
            margin_level=getattr(account, "margin_level", 0.0),
            open_positions=len(self.bot.positions()),
        )


# ===========================================================================
# APPLICATION LIFECYCLE
# ===========================================================================
class Application:
    """Wires the ladder bot, the monitor and the Telegram controller."""

    def __init__(self):
        self.bot = LadderBot(CSV, SETTINGS)
        self.engine = self.bot            # alias: Telegram talks to this object
        self.monitor = MonitorThread(self.bot, CSV)
        self.telegram = None
        self._shutdown = threading.Event()

    def banner(self):
        account = account_info()
        snap = SETTINGS.snapshot()
        log("=" * 70)
        log(f"{SYMBOL} ROLLING LADDER SCALPER - MT5 + TELEGRAM")
        log("=" * 70)
        if account:
            log(f"MT5 Account: {account.login}")
            log(f"Server: {getattr(account, 'server', '') or SERVER or '(default)'}")
            log(f"Balance: ${account.balance:.2f} | Equity: ${account.equity:.2f}")
            log(f"MT5 Connected: {'YES' if terminal_connected() else 'NO'}")
        else:
            log("MT5 Account: (not connected)")
        log(f"Symbol: {SYMBOL} | Timeframe: {snap['timeframe']}")
        log(f"TRADING MODE: {cfg.TRADING_MODE}"
            f"{'  (simulated fills, no orders sent)' if cfg.TRADING_MODE == 'PAPER' else '  *** REAL ORDERS ***'}")
        log(f"Ladder: spacing {snap['ladder_spacing']} x depth {snap['ladder_depth']} "
            f"| first level {snap['first_level_offset']} | {snap['roll_mode']}")
        tp_txt = ("NONE - the cycle is closed as one basket"
                  if snap["tp_mode"] == "none"
                  else f"{snap['tp_levels']} level(s) = "
                       f"{snap['tp_levels'] * snap['ladder_spacing']:g}"
                  if snap["tp_mode"] == "levels"
                  else f"{snap['tp_mode']} ({snap['tp_distance']})")
        log(f"TP: {tp_txt} | SL: {snap['stop_loss_distance'] or 'none'} | "
            f"Lot: {snap['lot_size']}")
        log(f"Exit: adaptive (score >= {snap['exit_threshold_exit']:g}, "
            f"monitor {snap['exit_threshold_monitor']:g}) - no trade count, "
            f"no dollar target")
        log(f"Risk: {snap['max_open_positions']} pos / "
            f"{snap['max_pending_orders']} pend / depth {snap['max_ladder_depth']} "
            f"/ spread {snap['max_spread']} / daily {snap['max_daily_drawdown']} "
            f"/ cycle {snap['max_cycle_drawdown']}")
        log(f"Data directory: {cfg.DATA_PATH}")
        log(f"Telegram control: {'ENABLED' if cfg.TELEGRAM_ENABLED else 'DISABLED'}")
        log("=" * 70)

        log_event("BOT_STARTED",
                  f"App start | account={getattr(account, 'login', 'n/a')} | "
                  f"symbol={SYMBOL} | mode={cfg.TRADING_MODE}", symbol=SYMBOL)

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
        for problem in SETTINGS.load():
            log(f"⚠ SETTINGS: {problem}")

        try:
            self.bot.connect()
        except Exception as exc:
            log(f"✗ MT5 connection failed: {exc}")
            log_event("ERROR", f"MT5 connection failed: {exc}", status="ERROR")

        self.banner()
        self.monitor.start()

        if cfg.TELEGRAM_ENABLED:
            try:
                from telegram_controller import TelegramController
                self.telegram = TelegramController(self.bot, CSV, SETTINGS)
                self.telegram.start()
                self.bot.set_notifier(self.telegram.notify)
                log("✓ Telegram controller started - send /start to your bot")
            except Exception as exc:
                self.telegram = None
                log(f"⚠ Telegram controller failed to start: {exc}")
                log(" Trading continues without remote control.")
                log_event("ERROR", f"Telegram start failed: {exc}", status="ERROR")
        else:
            log("⚠ TELEGRAM_BOT_TOKEN is not set in .env - remote control disabled.")

        if cfg.AUTO_START_TRADING:
            ok, msg = self.bot.start()
            if not ok:
                log(f"⚠ Auto-start: {msg}")
        else:
            log("AUTO_START_TRADING=false - press 🟢 START in Telegram.")

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
            self.bot.stop()
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
        pass

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
