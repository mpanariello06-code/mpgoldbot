"""
The cycle basket: what one rolling-ladder cycle is holding, and why it ends.

The normal strategy exit is basket profit management, in one state machine:

    BASKET_BUILDING        below the target
    PROFIT_TARGET_REACHED  hit BASKET_PROFIT_TARGET; taken now unless the
                           profit runner is on, in which case it runs
    PROFIT_PROTECTION      the peak passed PROFIT_PROTECTION_ACTIVATION, so
                           the accumulated profit is now trailed

`CycleBasket` owns that state and the bookkeeping behind it - which levels
triggered, how deep the ladder went, what the basket is worth, and what it was
worth at its best. Hard risk limits (drawdown, cycle duration, spread) live in
the ladder engine and override everything here; they are emergency protection,
not strategy.

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
# Normal strategy exits.
BASKET_PROFIT_TARGET = "BASKET_PROFIT_TARGET"
PROFIT_PROTECTION = "PROFIT_PROTECTION"
# Hard risk protection - these override the strategy.
RISK_DRAWDOWN = "RISK_DRAWDOWN"
RISK_TIMEOUT = "RISK_TIMEOUT"
RISK_SPREAD = "RISK_SPREAD"
EMERGENCY_EXIT = "EMERGENCY_EXIT"
MANUAL_EXIT = "MANUAL_EXIT"

EXIT_REASONS = (BASKET_PROFIT_TARGET, PROFIT_PROTECTION, RISK_DRAWDOWN,
                RISK_TIMEOUT, RISK_SPREAD, EMERGENCY_EXIT, MANUAL_EXIT)

RISK_REASONS = (RISK_DRAWDOWN, RISK_TIMEOUT, RISK_SPREAD, EMERGENCY_EXIT,
                MANUAL_EXIT)

# ------------------------------------------------- profit-management states
BASKET_BUILDING = "BASKET_BUILDING"
PROFIT_TARGET_REACHED = "PROFIT_TARGET_REACHED"
PROFIT_PROTECTION_STATE = "PROFIT_PROTECTION"
BASKET_STATES = (BASKET_BUILDING, PROFIT_TARGET_REACHED,
                 PROFIT_PROTECTION_STATE)


@dataclass
class ProfitRules:
    """
    Everything the profit-management state machine reads, in one object.

    Built from the live settings once per pass, so there is no second copy of
    these numbers anywhere in the code.
    """
    target: float = 2.00            # BASKET_PROFIT_TARGET
    runner_enabled: bool = True     # PROFIT_RUNNER_ENABLED
    activation: float = 3.00        # PROFIT_PROTECTION_ACTIVATION
    trail: float = 1.50             # PROFIT_PROTECTION_TRAIL
    floor: float = 1.00             # MIN_PROTECTED_PROFIT

    @property
    def protected_floor(self):
        """
        The floor, never above the target.

        You cannot protect more profit than you were willing to take: a floor
        above the target would close every basket the instant it reached the
        target, which is not what a floor is for.
        """
        if self.target > 0:
            return min(float(self.floor), float(self.target))
        return float(self.floor)

    def trail_for(self, peak):
        """
        How much give-back from `peak` is tolerated before the basket is taken.

        This is the ONE place the trail is computed. Today it is a fixed
        dollar amount, deliberately - it is the simplest thing that reliably
        stops a winning basket becoming a losing one. A percentage-of-peak,
        volatility-adjusted or ladder-depth-adjusted trail replaces the body of
        this method and nothing else.
        """
        return max(0.0, float(self.trail))


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
        # Peak floating P/L for THIS cycle. It only ever goes up while the
        # cycle is open, and it starts at 0 for every new cycle because a new
        # CycleBasket is built with it.
        self.peak_pnl = 0.0
        self.trough_pnl = 0.0
        self.max_floating_profit = 0.0
        self.max_floating_loss = 0.0
        self.max_drawdown = 0.0

        # --- profit management ---
        self.state = BASKET_BUILDING
        self.protection_active = False
        self.protection_threshold = 0.0   # the P/L the trail would close at
        self.peak_at = None               # seconds from cycle start
        self.target_at = None             # first time the target was reached
        self.protection_at = None         # when protection activated
        self.time_in_profit = 0.0
        self.time_in_protection = 0.0
        self._last_mark = None            # for the two accumulators above

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

    def update_pnl(self, floating, now=None):
        """The combined floating P/L of the basket's open legs."""
        self.floating_pnl = float(floating)
        self._update_extremes(now)

    def _update_extremes(self, now=None):
        total = self.realized_pnl + self.floating_pnl
        if total > self.peak_pnl:
            self.peak_pnl = total
            self.peak_at = (now - self.started_at) if now is not None else None
        self.trough_pnl = min(self.trough_pnl, total)
        self.max_floating_profit = max(self.max_floating_profit,
                                       self.floating_pnl)
        self.max_floating_loss = min(self.max_floating_loss, self.floating_pnl)
        self.max_drawdown = max(self.max_drawdown, self.drawdown)

    # ------------------------------------------------- profit management
    def mark(self, floating, rules, now):
        """
        Fold one pass of live basket P/L into the state machine.

        Called once per poll, before any exit decision, so peak tracking, the
        timers and the state are always current when `should_exit` is asked.
        """
        self.update_pnl(floating, now=now)

        elapsed = 0.0 if self._last_mark is None else max(0.0, now - self._last_mark)
        self._last_mark = now
        if self.floating_pnl > 0:
            self.time_in_profit += elapsed
        if self.protection_active:
            self.time_in_protection += elapsed

        if self.target_at is None and rules.target > 0 and \
                self.floating_pnl >= rules.target:
            self.target_at = now - self.started_at

        # Activation is sticky: once a cycle has been worth protecting it stays
        # protected, even if the basket falls back below the activation level.
        if not self.protection_active and rules.activation > 0 and \
                self.peak_pnl >= rules.activation:
            self.protection_active = True
            self.protection_at = now - self.started_at

        if self.protection_active:
            self.state = PROFIT_PROTECTION_STATE
            self.protection_threshold = round(
                max(self.peak_pnl - rules.trail_for(self.peak_pnl),
                    rules.protected_floor), 2)
        elif self.target_at is not None:
            self.state = PROFIT_TARGET_REACHED
            self.protection_threshold = round(rules.protected_floor, 2)
        else:
            self.state = BASKET_BUILDING
            self.protection_threshold = 0.0
        return self.state

    def should_exit(self, rules, has_exposure=True):
        """
        The one normal-strategy exit decision. Returns (reason, detail) or
        (None, "").

        Hard risk is checked by the engine BEFORE this and overrides it.
        """
        if not has_exposure:
            return None, ""
        pnl = self.floating_pnl

        # 1. the plain target, when the runner is switched off
        if not rules.runner_enabled:
            if rules.target > 0 and pnl >= rules.target:
                return BASKET_PROFIT_TARGET, (
                    f"basket floating P/L {pnl:+.2f} reached the "
                    f"{rules.target:+.2f} target (profit runner off)")
            return None, ""

        # 2. the runner is on: the basket is allowed past the target, and the
        #    accumulated profit is trailed instead of taken immediately.
        if self.protection_active:
            trail = rules.trail_for(self.peak_pnl)
            giveback = self.peak_pnl - pnl
            if giveback >= trail:
                return PROFIT_PROTECTION, (
                    f"gave back {giveback:.2f} of a {self.peak_pnl:+.2f} peak "
                    f"(trail {trail:.2f}) - closing at {pnl:+.2f}")
            # The floor is the backstop for a peak so large that the trail
            # alone would still let the basket bleed out.
            if pnl <= rules.protected_floor:
                return PROFIT_PROTECTION, (
                    f"basket fell to {pnl:+.2f}, at or below the "
                    f"{rules.protected_floor:+.2f} protected floor (peak "
                    f"{self.peak_pnl:+.2f})")
            return None, ""

        # 3. the target was reached but the peak never got as far as the
        #    activation level: protect what was banked rather than let it go.
        if self.target_at is not None and pnl <= rules.protected_floor:
            return BASKET_PROFIT_TARGET, (
                f"basket reached the {rules.target:+.2f} target, peaked at "
                f"{self.peak_pnl:+.2f} and fell back to {pnl:+.2f}, at or "
                f"below the {rules.protected_floor:+.2f} protected floor")
        return None, ""

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
        """Give-back from the basket's own peak. Never negative."""
        return max(0.0, self.peak_pnl - self.basket_pnl)

    # the spec's name for the same number, used by the telemetry log
    drawdown_from_peak = drawdown

    @property
    def profit_giveback(self):
        """How much of the best excursion was handed back by the close."""
        return round(max(0.0, self.peak_pnl - self.basket_pnl), 2)

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
            "drawdown_from_peak": round(self.drawdown, 2),
            "peak_pnl": round(self.peak_pnl, 2),
            "max_floating_profit": round(self.max_floating_profit, 2),
            "max_floating_loss": round(self.max_floating_loss, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "profit_giveback": self.profit_giveback,
            "cycle_state": self.state,
            "protection_active": self.protection_active,
            "protection_threshold": round(self.protection_threshold, 2),
            "time_to_peak": (round(self.peak_at, 1)
                             if self.peak_at is not None else ""),
            "time_to_profit_target": (round(self.target_at, 1)
                                      if self.target_at is not None else ""),
            "time_in_profit": round(self.time_in_profit, 1),
            "time_in_protection": round(self.time_in_protection, 1),
            "price": self.price,
        }
