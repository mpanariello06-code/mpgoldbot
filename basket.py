"""
The cycle basket: what one rolling-ladder cycle is holding, and why it ends.

There is exactly ONE normal strategy exit:

    total floating basket P/L >= BASKET_PROFIT_TARGET   ->  close everything

Nothing else in this module decides anything. `CycleBasket` is plain
bookkeeping - which levels triggered, how deep the ladder went, what the basket
is worth - kept for the CSV log and the status screen. Hard risk limits
(drawdown, cycle duration, spread) live in the ladder engine and override the
profit target; they are emergency protection, not strategy.

The previous scenario-based exit engine (directional / reversal / extended
readings blended into a 0-100 score) has been deleted, not disabled. If
intelligent exits are wanted again they will be built from collected data, not
resurrected from here.
"""

import time
from dataclasses import dataclass

BUY = "BUY"
SELL = "SELL"

# ------------------------------------------------------------- exit reasons
# The one normal strategy exit.
BASKET_PROFIT_TARGET = "BASKET_PROFIT_TARGET"
# Hard risk protection - these override the profit target.
RISK_DRAWDOWN = "RISK_DRAWDOWN"
RISK_TIMEOUT = "RISK_TIMEOUT"
RISK_SPREAD = "RISK_SPREAD"
MANUAL_STOP = "MANUAL_STOP"
OTHER_RISK_EXIT = "OTHER_RISK_EXIT"

EXIT_REASONS = (BASKET_PROFIT_TARGET, RISK_DRAWDOWN, RISK_TIMEOUT,
                RISK_SPREAD, MANUAL_STOP, OTHER_RISK_EXIT)

RISK_REASONS = (RISK_DRAWDOWN, RISK_TIMEOUT, RISK_SPREAD, MANUAL_STOP,
                OTHER_RISK_EXIT)


@dataclass
class Trigger:
    side: str
    index: int
    price: float
    ts: float


@dataclass
class Closure:
    side: str
    index: int
    price_open: float
    price_close: float
    profit: float
    ts: float
    reason: str = ""


class CycleBasket:
    """
    Bookkeeping for one cycle. It records; it never decides.

    Positions carry no individual take profit, so within a cycle
    `realized_pnl` stays 0.00 until the basket is closed and `floating_pnl`
    carries everything. They are tracked separately on purpose - reporting
    banked profit as floating exposure is how a bot ends up lying about what it
    holds.
    """

    def __init__(self, cycle_id, anchor, spacing, started_at=None):
        self.cycle_id = cycle_id
        self.anchor = float(anchor)
        self.spacing = float(spacing) or 0.01
        self.started_at = started_at or time.time()

        self.triggers = []
        self.closures = []

        self.buy_triggers = 0
        self.sell_triggers = 0
        self.last_side = ""
        self.previous_side = ""
        self.direction_changes = 0

        self.realized_pnl = 0.0
        self.floating_pnl = 0.0
        self.peak_pnl = 0.0
        self.trough_pnl = 0.0

        self.price = float(anchor)
        self.high_price = float(anchor)
        self.low_price = float(anchor)
        self.path_distance = 0.0            # summed absolute movement

    # ------------------------------------------------------------- feeding
    def record_trigger(self, side, index, price, ts=None):
        ts = ts or time.time()
        self.triggers.append(Trigger(side, int(index), float(price), ts))
        if side == BUY:
            self.buy_triggers += 1
        else:
            self.sell_triggers += 1
        if self.last_side and side != self.last_side:
            self.direction_changes += 1
        self.previous_side = self.last_side
        self.last_side = side
        self.update_price(price, ts)

    def record_close(self, side, index, price_open, price_close, profit,
                     reason="", ts=None):
        self.closures.append(Closure(side, int(index), float(price_open),
                                     float(price_close), float(profit),
                                     ts or time.time(), reason))
        self.realized_pnl += float(profit)
        self._update_extremes()

    def update_price(self, price, ts=None):
        price = float(price)
        self.path_distance += abs(price - self.price)
        self.price = price
        self.high_price = max(self.high_price, price)
        self.low_price = min(self.low_price, price)

    def update_pnl(self, floating):
        """The combined floating P/L of the basket's open legs."""
        self.floating_pnl = float(floating)
        self._update_extremes()

    def _update_extremes(self):
        total = self.realized_pnl + self.floating_pnl
        self.peak_pnl = max(self.peak_pnl, total)
        self.trough_pnl = min(self.trough_pnl, total)

    # ---------------------------------------------------------- derived data
    @property
    def total_triggers(self):
        return len(self.triggers)

    @property
    def dominant_side(self):
        if self.buy_triggers > self.sell_triggers:
            return BUY
        if self.sell_triggers > self.buy_triggers:
            return SELL
        return ""

    @property
    def ladder_depth_used(self):
        """How many distinct grid levels this cycle has consumed."""
        return len({(t.side, t.index) for t in self.triggers})

    @property
    def net_levels(self):
        """Signed distance from the cycle anchor, in ladder levels."""
        return (self.price - self.anchor) / self.spacing

    @property
    def path_levels(self):
        return self.path_distance / self.spacing

    @property
    def basket_pnl(self):
        return self.realized_pnl + self.floating_pnl

    @property
    def drawdown(self):
        """Give-back from the basket's own peak."""
        return max(0.0, self.peak_pnl - self.basket_pnl)

    @property
    def age_seconds(self):
        return max(0.0, time.time() - self.started_at)

    def snapshot(self):
        """Flat dict of everything tracked - used by CSV and Telegram."""
        return {
            "cycle_id": self.cycle_id,
            "buy_triggers": self.buy_triggers,
            "sell_triggers": self.sell_triggers,
            "last_side": self.last_side,
            "previous_side": self.previous_side,
            "direction_changes": self.direction_changes,
            "dominant_side": self.dominant_side,
            "ladder_depth_used": self.ladder_depth_used,
            "total_triggers": self.total_triggers,
            "net_levels": round(self.net_levels, 3),
            "path_levels": round(self.path_levels, 3),
            "price_distance_traveled": round(self.path_distance, 3),
            "basket_pnl": round(self.basket_pnl, 2),
            "basket_realized_pnl": round(self.realized_pnl, 2),
            "basket_floating_pnl": round(self.floating_pnl, 2),
            "basket_drawdown": round(self.drawdown, 2),
            "peak_pnl": round(self.peak_pnl, 2),
            "price": self.price,
        }
