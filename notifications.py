"""
Telegram notification policy.

The ladder can trigger dozens of levels a minute, so Telegram is deliberately
event-based:

    Telegram = important events        CSV / console = everything

Sent:      bot started, bot stopped, cycle closed, a state TRANSITION into
           risk events, throttled errors, and
           an optional periodic status.
Not sent:  individual level triggers, individual TPs, order placement,
           replenishment, or a repeat of a state the chat already knows about.

Everything here is throttled and de-duplicated: repeats of the same message key
inside its interval are dropped rather than queued.
"""

import threading
import time

def _duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _seconds(value):
    """A wait, written the way a person reads it: 10s, 90s, 15m 30s."""
    value = max(0.0, float(value))
    if value < 120:
        return f"{value:.0f}s"
    minutes, rest = divmod(int(round(value)), 60)
    return f"{minutes}m" if not rest else f"{minutes}m {rest}s"


class TelegramNotifier:
    """
    Wraps the raw send function with a policy.

    `send` must be non-blocking (the controller's fire-and-forget notify).
    """

    # at most this many state messages per cycle, and a continuation run this
    # short is just the ladder doing its job

    def __init__(self, send, settings=None, clock=time.time):
        self._send = send
        self.settings = settings
        self.clock = clock
        self._lock = threading.RLock()
        self._last_sent = {}        # key -> timestamp
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
            f"{timeframe}   [{mode}]\n\n"
            f"Cycle: #{cycle_id}\n"
            f"Spacing: {spacing:g}\n"
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
                     direction, duration_seconds, next_cycle_id,
                     next_ladder_seconds=0.0, kind="", peak=None,
                     giveback=None):
        """
        One message when the cycle is confirmed closed and flat.

        It announces the wait, not the next cycle: the next ladder is announced
        by `cycle_started` when it has actually been deployed. There is no
        countdown in between - one message here, one message there.
        """
        # 🛡 says a risk rule ended it, not the strategy - that distinction
        # matters more at a glance than the sign of the result.
        icon = ("🛡" if str(kind).startswith(("RISK", "OTHER_RISK", "MANUAL"))
                else "🟢" if total >= 0 else "🔴")
        wait = (f"Next ladder in {_seconds(next_ladder_seconds)}."
                if next_ladder_seconds > 0 else "Next ladder deploying now.")
        return self._emit(
            f"{icon} <b>CYCLE #{cycle_id} CLOSED</b>\n\n"
            f"{symbol}\n\n"
            f"Result: {total:+.2f}\n"
            + (f"Peak: {peak:+.2f}   Giveback: {giveback:.2f}\n"
               if peak is not None else "")
            + f"\nBUY: {buys}\n"
            f"SELL: {sells}\n\n"
            f"Exit: {reason_word}\n"
            f"Direction: {direction or '-'}\n"
            f"Duration: {_duration(duration_seconds)}\n\n"
            f"{wait}"
        )

    def cycle_started(self, symbol, timeframe, cycle_id, levels=0):
        """One message when the new ladder is actually live in MT5."""
        return self._emit(
            f"🟢 <b>CYCLE #{cycle_id} STARTED</b>\n\n"
            f"{symbol}\n"
            f"{timeframe}\n"
            f"Ladder deployed." + (f" {levels} levels live." if levels else "")
        )

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

        target = status.get("basket_profit_target", 0.0)
        return self._emit(
            f"📊 <b>LADDER STATUS</b>\n\n"
            f"{status.get('symbol', '')}\n"
            f"Cycle: #{status.get('cycle_id', 0)}\n"
            f"State: {'ACTIVE' if status.get('cycle_active', True) else 'CLOSED'}"
            f"\n\n"
            f"Pending: {status.get('current_pending_buys', 0)}B / "
            f"{status.get('current_pending_sells', 0)}S\n"
            f"Open: {status.get('current_open_buys', 0)}B / "
            f"{status.get('current_open_sells', 0)}S\n\n"
            f"Triggers so far: {status.get('historical_buy_triggers', 0)}B / "
            f"{status.get('historical_sell_triggers', 0)}S\n"
            f"Ladder depth: {status.get('ladder_depth_used', 0)}\n\n"
            f"Floating basket: {status.get('basket_floating_pnl', 0):+.2f}"
            + (f" / {target:.2f}" if target else "") + "\n"
            f"Peak: {status.get('basket_peak_pnl', 0):+.2f}   "
            f"Giveback: {status.get('basket_giveback', 0):.2f}\n"
            + ("Protection: ACTIVE\n" if status.get("protection_active") else "")
            + f"Realized: {status.get('basket_realized_pnl', 0):+.2f}"
        )

    # --------------------------------------------------------------- metrics
    def stats(self):
        with self._lock:
            return {"sent": self.sent, "suppressed": self.suppressed}
