"""
Telegram notification policy.

The ladder can trigger dozens of levels a minute, so Telegram is deliberately
event-based:

    Telegram = important events        CSV / console = everything

Sent:      bot started, bot stopped, cycle closed, a state TRANSITION into
           reversal or strong continuation, risk events, throttled errors, and
           an optional periodic status.
Not sent:  individual level triggers, individual TPs, order placement,
           replenishment, or a repeat of a state the chat already knows about.

Everything here is throttled and de-duplicated: repeats of the same message key
inside its interval are dropped rather than queued.
"""

import threading
import time

# state labels used for transition detection
NORMAL = "NORMAL"
REVERSAL = "REVERSAL"
CONTINUATION = "CONTINUATION"
EXHAUSTION = "EXHAUSTION"


def _duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class TelegramNotifier:
    """
    Wraps the raw send function with a policy.

    `send` must be non-blocking (the controller's fire-and-forget notify).
    """

    # at most this many state messages per cycle, and a continuation run this
    # short is just the ladder doing its job
    MAX_STATE_ALERTS = 2
    CONTINUATION_MIN_TRIGGERS = 3

    def __init__(self, send, settings=None, clock=time.time):
        self._send = send
        self.settings = settings
        self.clock = clock
        self._lock = threading.RLock()
        self._last_sent = {}        # key -> timestamp
        self._cycle_state = {}      # cycle_id -> labels already announced
        # the first heartbeat is due one interval after startup, so it never
        # lands right behind the start message
        self._last_status = clock()
        self.suppressed = 0         # messages dropped by the policy
        self.sent = 0

    # ------------------------------------------------------------- plumbing
    def _setting(self, key, default):
        if self.settings is None:
            return default
        try:
            return self.settings.get(key)
        except Exception:
            return default

    def _emit(self, text, key=None, min_interval=0.0):
        """Send unless an identical key went out inside `min_interval`."""
        now = self.clock()
        with self._lock:
            if key is not None and min_interval > 0:
                last = self._last_sent.get(key, 0.0)
                if now - last < min_interval:
                    self.suppressed += 1
                    return False
            if key is not None:
                self._last_sent[key] = now
            self.sent += 1
        try:
            self._send(text)
        except Exception as exc:                  # never break the engine
            print(f"[notifications] send failed: {exc}")
            return False
        return True

    # -------------------------------------------------------------- startup
    def bot_started(self, symbol, timeframe, spacing, cycle_id, mode):
        return self._emit(
            f"🟢 <b>ROLLING LADDER STARTED</b>\n\n"
            f"{symbol}\n"
            f"TF: {timeframe}   Mode: {mode}\n"
            f"Spacing: {spacing:g}\n\n"
            f"Cycle #{cycle_id}\n"
            f"Ladder deployed"
        )

    def bot_stopped(self, symbol, cycle_id, positions, orders, session_pnl):
        return self._emit(
            f"🔴 <b>ROLLING LADDER STOPPED</b>\n\n"
            f"{symbol}\n"
            f"Cycle: #{cycle_id}\n\n"
            f"Open positions: {positions}\n"
            f"Pending orders: {orders}\n\n"
            f"Session P/L: {session_pnl:+.2f}"
        )

    # --------------------------------------------------------------- cycles
    def cycle_closed(self, symbol, cycle_id, total, buys, sells, reason_word,
                     direction, duration_seconds, next_cycle_id):
        icon = "🟢" if total >= 0 else "🔴"
        return self._emit(
            f"{icon} <b>CYCLE #{cycle_id} CLOSED</b>\n\n"
            f"{symbol}\n"
            f"Result: {total:+.2f}\n\n"
            f"BUY: {buys}\n"
            f"SELL: {sells}\n\n"
            f"Reason: {reason_word}\n"
            f"Direction: {direction or '-'}\n"
            f"Duration: {_duration(duration_seconds)}\n\n"
            f"Cycle #{next_cycle_id} deployed"
        )

    # ------------------------------------------------- state transitions only
    def state_change(self, symbol, cycle_id, market_state, buys, sells,
                     imbalance, dominant):
        """
        One message per meaningful transition, at most twice per cycle.

        A cycle whose reading oscillates between continuation and exhaustion is
        not news every time it flips: each label is announced once per cycle,
        and only reversal and strong continuation are announced at all. Fading
        momentum shows up in the cycle-closed message instead.
        """
        if not self._setting("telegram_state_alerts", True):
            return False
        label = {
            "REVERSAL_DETECTED": REVERSAL,
            "MOMENTUM_CONTINUATION": CONTINUATION,
        }.get(market_state)
        if label is None:
            return False
        if label == CONTINUATION and (buys + sells) < self.CONTINUATION_MIN_TRIGGERS:
            return False        # an ordinary opening run is not news

        with self._lock:
            announced = self._cycle_state.setdefault(cycle_id, set())
            if label in announced or len(announced) >= self.MAX_STATE_ALERTS:
                self.suppressed += 1
                return False
            announced.add(label)
            if len(self._cycle_state) > 64:
                for key in sorted(self._cycle_state)[:32]:
                    self._cycle_state.pop(key, None)

        if label == REVERSAL:
            return self._emit(
                f"🔄 <b>REVERSAL DETECTED</b>\n\n"
                f"Cycle #{cycle_id}\n{symbol}\n\n"
                f"BUY: {buys}\nSELL: {sells}\n\n"
                f"Dominance: {imbalance:.2f}x {dominant}\n\n"
                f"Managing basket...",
                key=f"rev:{cycle_id}", min_interval=30)
        return self._emit(
            f"📈 <b>CYCLE #{cycle_id}</b>\n"
            f"Strong {dominant} momentum\n\n"
            f"BUY: {buys}\nSELL: {sells}\n\n"
            f"Ladder continuing...",
            key=f"cont:{cycle_id}", min_interval=60)

    # ----------------------------------------------------------------- risk
    def risk_event(self, title, detail, key="risk"):
        return self._emit(f"⛔ <b>{title}</b>\n\n{detail}",
                          key=key, min_interval=120)

    def error(self, problem, action="Retrying", key=None):
        """Errors are deduplicated by their text: a loop cannot flood the chat."""
        throttle = float(self._setting("telegram_error_throttle_seconds", 300))
        return self._emit(
            f"⚠️ <b>LADDER ERROR</b>\n\n"
            f"Problem:\n{problem}\n\n"
            f"Action:\n{action}",
            key=key or f"err:{problem[:60]}", min_interval=throttle)

    # ------------------------------------------------------- periodic status
    def periodic_status(self, status):
        """Compact heartbeat, only if enabled and only on its own interval."""
        if not self._setting("telegram_status_updates", True):
            return False
        interval = float(self._setting("telegram_status_interval_minutes", 20)) * 60
        if interval <= 0:
            return False
        now = self.clock()
        with self._lock:
            if now - self._last_status < interval:
                return False
            self._last_status = now

        momentum = status.get("momentum_score", 0.0)
        word = ("STRONG" if momentum >= 0.66 else
                "WEAK" if momentum <= 0.33 else "NEUTRAL")
        return self._emit(
            f"📊 <b>LADDER STATUS</b>\n\n"
            f"{status.get('symbol', '')}\n"
            f"Cycle: #{status.get('cycle_id', 0)}\n"
            f"State: {(status.get('state') or 'ACTIVE').replace('_', ' ')}\n\n"
            f"Pending: {status.get('current_pending_buys', 0)}B / "
            f"{status.get('current_pending_sells', 0)}S\n"
            f"Open: {status.get('current_open_buys', 0)}B / "
            f"{status.get('current_open_sells', 0)}S\n\n"
            f"Triggers so far: {status.get('historical_buy_triggers', 0)}B / "
            f"{status.get('historical_sell_triggers', 0)}S\n"
            f"Direction: {status.get('last_side') or '-'}\n"
            f"Momentum: {word}\n\n"
            f"Floating: {status.get('floating_pnl', 0):+.2f}\n"
            f"Realized: {status.get('realized_pnl', 0):+.2f}"
        )

    # --------------------------------------------------------------- metrics
    def stats(self):
        with self._lock:
            return {"sent": self.sent, "suppressed": self.suppressed}
