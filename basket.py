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
from collections import deque
from dataclasses import dataclass

BUY = "BUY"
SELL = "SELL"

# ------------------------------------------------------------- exit reasons
# Normal strategy exits.
BASKET_PROFIT_TARGET = "BASKET_PROFIT_TARGET"
PROFIT_PROTECTION = "PROFIT_PROTECTION"
# A basket that clawed back from a deep hole and is now green: the recovery
# itself is the reason to take it, because giving it back is how a recovered
# basket becomes a loss again.
RECOVERY_PROFIT = "RECOVERY_PROFIT"
# Profit given back from a peak that never reached the protection activation.
PROFIT_GIVEBACK = "PROFIT_GIVEBACK"
# Hard risk protection - these override the strategy.
RISK_DRAWDOWN = "RISK_DRAWDOWN"
RISK_TIMEOUT = "RISK_TIMEOUT"
RISK_SPREAD = "RISK_SPREAD"
EMERGENCY_EXIT = "EMERGENCY_EXIT"
MANUAL_EXIT = "MANUAL_EXIT"

EXIT_REASONS = (BASKET_PROFIT_TARGET, PROFIT_PROTECTION, RECOVERY_PROFIT,
                PROFIT_GIVEBACK, RISK_DRAWDOWN, RISK_TIMEOUT, RISK_SPREAD,
                EMERGENCY_EXIT, MANUAL_EXIT)

RISK_REASONS = (RISK_DRAWDOWN, RISK_TIMEOUT, RISK_SPREAD, EMERGENCY_EXIT,
                MANUAL_EXIT)

# ------------------------------------------------- profit-management states
BASKET_BUILDING = "BASKET_BUILDING"
PROFIT_TARGET_REACHED = "PROFIT_TARGET_REACHED"
PROFIT_PROTECTION_STATE = "PROFIT_PROTECTION"
EXITING_STATE = "EXITING"
BASKET_STATES = (BASKET_BUILDING, PROFIT_TARGET_REACHED,
                 PROFIT_PROTECTION_STATE, EXITING_STATE)

# ------------------------------------------------------------ recovery state
# What the basket has BEEN THROUGH, not just what it is worth now. The
# difference between a basket that walked up from +0.50 to +3.00 and one that
# clawed back from -15.00 to +1.00 is invisible to a P/L number alone, and the
# two deserve different treatment.
NORMAL = "NORMAL"              # near flat, nothing notable has happened
UNDERWATER = "UNDERWATER"      # meaningfully negative, no recovery shown yet
RECOVERING = "RECOVERING"      # was deeply underwater, now climbing back
PROFITABLE = "PROFITABLE"      # meaningfully positive
PROTECTING = "PROFIT_PROTECTION"   # enough profit that it is worth defending
EXITING_RECOVERY = "EXITING"   # the exit is committed
RECOVERY_STATES = (NORMAL, UNDERWATER, RECOVERING, PROFITABLE, PROTECTING,
                   EXITING_RECOVERY)

# ------------------------------------------------------------- price movement
FAVORABLE = "FAVORABLE"        # recent movement is helping the basket
ADVERSE = "ADVERSE"            # recent movement is hurting it
FLAT_MOVE = "FLAT"             # no meaningful movement either way
PRICE_STATES = (FAVORABLE, ADVERSE, FLAT_MOVE)

# ------------------------------------------------------------- exit decisions
HOLD = "HOLD"
PROTECT = "PROTECT"
EXIT = "EXIT"


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
    # --- basket-state thresholds (all configurable, none validated yet) ---
    # How far under water counts as "meaningfully negative" rather than noise.
    underwater_at: float = 2.00     # UNDERWATER_THRESHOLD
    # How far back up from the worst point counts as a real recovery, as a
    # FRACTION of how deep the hole was. 0.5 = "climbed back half of it".
    recovery_fraction: float = 0.50  # RECOVERY_FRACTION
    # A recovered basket is taken at this profit rather than being asked to
    # reach the full target - it has already proved it can go the other way.
    recovery_take: float = 0.50     # RECOVERY_TAKE_PROFIT
    # Give-back from a peak that never reached `activation`, as a FRACTION of
    # that peak. This closes the dead band between target and activation.
    giveback_fraction: float = 0.40  # PROFIT_GIVEBACK_FRACTION
    # Seconds of price history the movement window looks at.
    movement_window: float = 20.0   # PRICE_MOVEMENT_WINDOW_SECONDS

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

    def giveback_limit(self, peak):
        """
        How much of an un-activated peak may be handed back before the basket
        is taken.

        This exists because of a real, observed failure: with target 2.00 and
        activation 3.00 there was a DEAD BAND. A basket that peaked at +2.45
        armed no protection at all, and the only backstop was the floor, tested
        as `pnl <= floor`. Cycle 90 went +2.35, +2.45, +1.60, -0.10 and was
        still holding at -0.10. Nothing was slow; there was simply no rule
        covering a peak between the target and the activation level.
        """
        peak = max(0.0, float(peak))
        return max(0.0, peak * max(0.0, float(self.giveback_fraction)))

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
        # PEAK AND LOWEST TRACK THE BASKET TOTAL (realized + floating), and so
        # does everything derived from them. Mixing the two is what let
        # telemetry report current_pnl (floating alone) above peak_pnl (the
        # net peak) whenever a leg had been banked at a loss. `basket_pnl` is
        # the one number these are all measured against.
        self.peak_pnl = 0.0
        self.lowest_pnl = 0.0
        self.trough_pnl = 0.0
        self.max_floating_profit = 0.0
        self.max_floating_loss = 0.0
        self.max_drawdown = 0.0

        # --- recovery (PHASE 11/12) ---
        self.recovery_state = NORMAL
        self.lowest_at = None            # seconds from start of the worst point
        self.recovery_start_at = None    # when the climb back began
        self.recovery_start_pnl = None   # what it was worth at the bottom
        self.time_underwater = 0.0
        self.time_since_peak = 0.0
        self.was_underwater = False      # sticky: it happened, even if fixed

        # --- price movement (PHASE 13) ---
        # A short rolling window of (timestamp, price). Deliberately small:
        # this is here to answer "is this basket still going my way?", not to
        # become an indicator library.
        self._price_window = deque(maxlen=64)
        self.price_state = FLAT_MOVE
        self.recent_price_change = 0.0
        self.price_velocity = 0.0        # price units per second
        self.basket_average_entry = 0.0
        self.net_volume = 0.0

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
        """
        A leg has left the book: its money moves from floating to realized.

        The subtraction matters. `floating_pnl` still holds this leg's share
        until the next mark, so adding the same money to `realized_pnl` without
        taking it out of `floating_pnl` would make `realized + floating` count
        it twice - and `peak_pnl` only ever goes up, so that inflated total
        would be latched for the rest of the cycle, arming protection early and
        reporting a peak the basket was never worth. The leg's floating share
        at the moment it closes IS its realized profit (they differ only by the
        closing costs), and the next mark overwrites the estimate with the
        truth.
        """
        profit = float(profit)
        self.closures.append(Closure(side, int(index), float(price_open),
                                     float(price_close), profit,
                                     ts or time.time(), reason))
        self.realized_pnl += profit
        self.floating_pnl -= profit
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
        """
        Peak and lowest, both measured on the BASKET TOTAL.

        Everything here is a function of `basket_pnl` (realized + floating), so
        peak >= current >= lowest holds by construction. The floating-only
        extremes are kept alongside for the record, clearly named, and are
        never compared against the peak.
        """
        total = self.basket_pnl
        if total > self.peak_pnl:
            self.peak_pnl = total
            self.peak_at = (now - self.started_at) if now is not None else None
            if now is not None:
                self._peak_wall = now
        if total < self.lowest_pnl:
            self.lowest_pnl = total
            self.lowest_at = (now - self.started_at) if now is not None else None
        self.trough_pnl = self.lowest_pnl
        self.max_floating_profit = max(self.max_floating_profit,
                                       self.floating_pnl)
        self.max_floating_loss = min(self.max_floating_loss, self.floating_pnl)
        self.max_drawdown = max(self.max_drawdown, self.drawdown)

    # ------------------------------------------------------- price movement
    def observe_price(self, price, now, positions=()):
        """
        Feed the short movement window and the basket's average entry.

        `positions` are the cycle's open legs, so the average entry and net
        volume describe the basket that actually exists rather than a
        remembered one.
        """
        price = float(price)
        window = float(getattr(self, "_movement_window", 20.0))
        self._price_window.append((now, price))
        while len(self._price_window) > 1 and \
                now - self._price_window[0][0] > window:
            self._price_window.popleft()

        first_ts, first_price = self._price_window[0]
        self.recent_price_change = round(price - first_price, 5)
        span = max(1e-6, now - first_ts)
        self.price_velocity = round(self.recent_price_change / span, 6)

        volume = 0.0
        weighted = 0.0
        net = 0.0
        for p in positions:
            volume += p.volume
            weighted += p.price_open * p.volume
            net += (p.volume if p.side == BUY else -p.volume)
        self.basket_average_entry = round(weighted / volume, 5) if volume else 0.0
        self.net_volume = round(net, 4)

        # "Favorable" means the recent move helps THIS basket, which depends on
        # which way the basket is leaning. A net-long basket likes price up.
        move = self.recent_price_change
        if abs(move) < self.spacing * 0.1 or not net:
            self.price_state = FLAT_MOVE
        elif (move > 0) == (net > 0):
            self.price_state = FAVORABLE
        else:
            self.price_state = ADVERSE
        return self.price_state

    @property
    def price_vs_anchor(self):
        return round(self.price - self.anchor, 5)

    @property
    def price_vs_average_entry(self):
        if not self.basket_average_entry:
            return 0.0
        return round(self.price - self.basket_average_entry, 5)

    # ------------------------------------------------------------- recovery
    def _update_recovery(self, rules, now):
        """
        Where this basket is in its own story.

        The point is to tell a basket that has only ever gone up apart from one
        that fell into a hole and climbed out. The second has already shown the
        market can take it back, so it is treated more defensively.
        """
        pnl = self.basket_pnl
        if pnl <= -abs(rules.underwater_at):
            self.was_underwater = True
            if self.recovery_start_at is None:
                self.recovery_start_pnl = self.lowest_pnl

        depth = abs(min(0.0, self.lowest_pnl))
        recovered = pnl - self.lowest_pnl
        # a real climb back, not a one-tick bounce
        meaningful = (self.was_underwater and depth > 0 and
                      recovered >= depth * max(0.0, rules.recovery_fraction))
        if meaningful and self.recovery_start_at is None:
            self.recovery_start_at = now - self.started_at

        if self.state == EXITING_STATE:
            self.recovery_state = EXITING_RECOVERY
        elif self.protection_active:
            self.recovery_state = PROTECTING
        elif meaningful and pnl < max(rules.target, 0.0):
            self.recovery_state = RECOVERING
        elif pnl <= -abs(rules.underwater_at):
            self.recovery_state = UNDERWATER
        elif pnl >= max(rules.recovery_take, 0.0) and pnl > 0:
            self.recovery_state = PROFITABLE
        else:
            self.recovery_state = NORMAL
        return self.recovery_state

    @property
    def recovery_amount(self):
        """How much has been clawed back from the worst point."""
        return round(self.basket_pnl - self.lowest_pnl, 2)

    @property
    def recovery_speed(self):
        """Recovery per second since the climb began. 0 when not recovering."""
        if self.recovery_start_at is None or self._last_mark is None:
            return 0.0
        elapsed = (self._last_mark - self.started_at) - self.recovery_start_at
        if elapsed <= 0:
            return 0.0
        return round(self.recovery_amount / elapsed, 4)

    # ------------------------------------------------- profit management
    def mark(self, floating, rules, now):
        """
        Fold one pass of live basket P/L into the state machine.

        Called once per poll, before any exit decision, so peak tracking, the
        timers and the state are always current when `should_exit` is asked.
        """
        self._movement_window = rules.movement_window
        self.update_pnl(floating, now=now)

        elapsed = 0.0 if self._last_mark is None else max(0.0, now - self._last_mark)
        self._last_mark = now
        if self.basket_pnl > 0:
            self.time_in_profit += elapsed
        if self.basket_pnl < 0:
            self.time_underwater += elapsed
        if self.protection_active:
            self.time_in_protection += elapsed
        peak_wall = getattr(self, "_peak_wall", None)
        self.time_since_peak = 0.0 if peak_wall is None else max(0.0, now - peak_wall)

        if self.target_at is None and rules.target > 0 and \
                self.basket_pnl >= rules.target:
            self.target_at = now - self.started_at

        # Activation is sticky: once a cycle has been worth protecting it stays
        # protected, even if the basket falls back below the activation level.
        if not self.protection_active and rules.activation > 0 and \
                self.peak_pnl >= rules.activation:
            self.protection_active = True
            self.protection_at = now - self.started_at

        if self.state == EXITING_STATE:
            pass                      # committed; nothing re-opens this
        elif self.protection_active:
            self.state = PROFIT_PROTECTION_STATE
            self.protection_threshold = round(
                max(self.peak_pnl - rules.trail_for(self.peak_pnl),
                    rules.protected_floor), 2)
        elif self.target_at is not None:
            self.state = PROFIT_TARGET_REACHED
            # The dead band, closed. A peak that reached the target but not the
            # activation level is still defended - by the larger of the floor
            # and "keep most of the peak" - instead of being left to run all
            # the way back to zero.
            self.protection_threshold = round(
                max(rules.protected_floor,
                    self.peak_pnl - rules.giveback_limit(self.peak_pnl)), 2)
        else:
            self.state = BASKET_BUILDING
            self.protection_threshold = 0.0
        self._update_recovery(rules, now)
        return self.state

    def decide(self, rules, has_exposure=True):
        """
        THE ONE BASKET DECISION. Returns (action, reason, detail).

        `action` is HOLD, PROTECT or EXIT. There is no second opinion anywhere
        in the codebase: the engine adds hard risk limits ABOVE this and then
        does what this says. Every branch names why.

        The inputs are the four the basket actually knows about:
        profit, what the basket has been through (recovery), which way price is
        moving, and how much of a peak has been handed back.
        """
        if not has_exposure:
            return HOLD, None, ""
        pnl = self.basket_pnl
        peak = self.peak_pnl

        # --- 1. the baseline target, when the runner is off ------------------
        if not rules.runner_enabled:
            if rules.target > 0 and pnl >= rules.target:
                return EXIT, BASKET_PROFIT_TARGET, (
                    f"basket P/L {pnl:+.2f} reached the {rules.target:+.2f} "
                    f"target (profit runner off)")
            return HOLD, None, ""

        # --- 2. a peak worth protecting has been given back ------------------
        if self.protection_active:
            trail = rules.trail_for(peak)
            giveback = peak - pnl
            if giveback >= trail:
                return EXIT, PROFIT_PROTECTION, (
                    f"gave back {giveback:.2f} of a {peak:+.2f} peak "
                    f"(trail {trail:.2f}) - closing at {pnl:+.2f}")
            if pnl <= rules.protected_floor:
                return EXIT, PROFIT_PROTECTION, (
                    f"basket fell to {pnl:+.2f}, at or below the "
                    f"{rules.protected_floor:+.2f} protected floor "
                    f"(peak {peak:+.2f})")
            return PROTECT, None, (
                f"protecting {peak:+.2f}, closes at "
                f"{self.protection_threshold:+.2f}")

        # --- 3. a RECOVERED basket is taken early ----------------------------
        # It has already demonstrated it can go against us by the depth it fell
        # to. Asking it to reach the full target risks repeating that.
        if self.recovery_state == RECOVERING and pnl >= rules.recovery_take > 0:
            return EXIT, RECOVERY_PROFIT, (
                f"recovered from {self.lowest_pnl:+.2f} to {pnl:+.2f} "
                f"(+{self.recovery_amount:.2f}) - taking it rather than "
                f"risking the round trip again")

        # --- 4. the target was reached; defend the peak ----------------------
        # This is the dead band that let cycle 90 ride +2.45 -> -0.10. A peak
        # above the target is now defended even when it never reached the
        # protection activation level.
        if self.target_at is not None:
            limit = rules.giveback_limit(peak)
            giveback = peak - pnl
            if pnl <= rules.protected_floor:
                return EXIT, BASKET_PROFIT_TARGET, (
                    f"basket reached the {rules.target:+.2f} target, peaked at "
                    f"{peak:+.2f} and fell back to {pnl:+.2f}, at or below the "
                    f"{rules.protected_floor:+.2f} protected floor")
            if limit > 0 and giveback >= limit:
                return EXIT, PROFIT_GIVEBACK, (
                    f"gave back {giveback:.2f} of a {peak:+.2f} peak that never "
                    f"reached the {rules.activation:+.2f} activation level "
                    f"(limit {limit:.2f}) - closing at {pnl:+.2f}")
            return PROTECT, None, (
                f"target reached, defending {peak:+.2f} down to "
                f"{self.protection_threshold:+.2f}")

        return HOLD, None, ""

    def should_exit(self, rules, has_exposure=True):
        """
        (reason, detail) for the engine, which only cares about EXIT.

        A thin adapter over `decide` so there is still exactly one place the
        decision is made.
        """
        action, reason, detail = self.decide(rules, has_exposure=has_exposure)
        if action == EXIT:
            return reason, detail
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
