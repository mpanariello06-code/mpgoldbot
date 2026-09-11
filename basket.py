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
# A basket that fought its way back and is climbing strongly: protect the
# recovery rather than let it round-trip.
STRONG_RECOVERY_PROTECTION = "STRONG_RECOVERY_PROTECTION"
# Exposure-driven, not P/L-driven: the ladder is at its cap, or one-sided
# enough that the next adverse leg is disproportionate.
DEEP_LADDER_RISK = "DEEP_LADDER_RISK"
DIRECTION_IMBALANCE_RISK = "DIRECTION_IMBALANCE_RISK"
# The emergency case: deep, severely underwater, no recovery, still going the
# wrong way. Depth alone never gets here - all four have to be true.
CRITICAL_LADDER_RISK = "CRITICAL_LADDER_RISK"
# Profit given back from a peak that never reached the protection activation.
PROFIT_GIVEBACK = "PROFIT_GIVEBACK"
# Hard risk protection - these override the strategy.
RISK_DRAWDOWN = "RISK_DRAWDOWN"
RISK_TIMEOUT = "RISK_TIMEOUT"
RISK_SPREAD = "RISK_SPREAD"
EMERGENCY_EXIT = "EMERGENCY_EXIT"
MANUAL_EXIT = "MANUAL_EXIT"
# STEPPED_STRADDLE: the single position left on its own stepped stop loss.
STOP_LOSS_HIT = "STOP_LOSS_HIT"

EXIT_REASONS = (BASKET_PROFIT_TARGET, PROFIT_PROTECTION, RECOVERY_PROFIT,
                PROFIT_GIVEBACK, STRONG_RECOVERY_PROTECTION, DEEP_LADDER_RISK,
                CRITICAL_LADDER_RISK, DIRECTION_IMBALANCE_RISK, RISK_DRAWDOWN,
                RISK_TIMEOUT, RISK_SPREAD, EMERGENCY_EXIT, MANUAL_EXIT)

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

# --------------------------------------------------------------- ladder depth
# How much of MAX_LADDER_DEPTH a cycle has consumed. Reaching the cap stops new
# exposure; it does NOT close the basket, which stays under the exit engine.
LADDER_NORMAL = "LADDER_NORMAL"
LADDER_EXTENDED = "LADDER_EXTENDED"
LADDER_DEEP = "LADDER_DEEP"
LADDER_CRITICAL = "LADDER_CRITICAL"
# retained: the hard ceiling MAX_LADDER_DEPTH, distinct from the zones above
LADDER_MAX_DEPTH = "LADDER_MAX_DEPTH"
LADDER_STATES = (LADDER_NORMAL, LADDER_EXTENDED, LADDER_DEEP, LADDER_CRITICAL,
                 LADDER_MAX_DEPTH)

# ------------------------------------------------------- deep-ladder health
# A deep ladder is not automatically a bad ladder. These say WHICH KIND of deep
# a basket is, from its own behaviour rather than from its depth: depth decides
# how much more exposure may be added, health decides whether to exit.
DEEP_HEALTHY = "DEEP_HEALTHY"          # deep, but green or barely underwater
DEEP_RECOVERING = "DEEP_RECOVERING"    # deep, underwater, climbing back well
DEEP_WEAK = "DEEP_WEAK"                # deep, underwater, climbing back poorly
DEEP_ADVERSE = "DEEP_ADVERSE"          # deep and still going the wrong way
DEEP_CRITICAL = "DEEP_CRITICAL"        # deep, severe, no recovery, adverse
DEEP_LADDER_STATES = (DEEP_HEALTHY, DEEP_RECOVERING, DEEP_WEAK, DEEP_ADVERSE,
                      DEEP_CRITICAL)

# ---------------------------------------------------------------- risk state
RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"
RISK_CRITICAL = "CRITICAL"
RISK_STATES = (RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL)

# ----------------------------------------------------------- exposure balance
# BUY 6 / SELL 6 and BUY 11 / SELL 1 are the same ladder depth and completely
# different risks. Measured on VOLUME, not order counts, so uneven lot sizes
# cannot hide an imbalance.
BALANCED = "BALANCED"
BUY_HEAVY = "BUY_HEAVY"
SELL_HEAVY = "SELL_HEAVY"
EXTREMELY_IMBALANCED = "EXTREMELY_IMBALANCED"
IMBALANCE_STATES = (BALANCED, BUY_HEAVY, SELL_HEAVY, EXTREMELY_IMBALANCED)

# What to do when MAX_DIRECTION_IMBALANCE is exceeded.
IMBALANCE_MONITOR = "MONITOR"                    # record it only
IMBALANCE_STOP_NEW = "STOP_NEW_EXPOSURE"         # also stop adding levels
IMBALANCE_ACTIONS = (IMBALANCE_MONITOR, IMBALANCE_STOP_NEW)

# ----------------------------------------------------------- recovery quality
# Not every uptick off the bottom is a recovery. These grade HOW WELL a basket
# that was genuinely underwater is climbing back.
NO_RECOVERY = "NO_RECOVERY"
WEAK_RECOVERY = "WEAK_RECOVERY"
RECOVERING_QUALITY = "RECOVERING"
STRONG_RECOVERY = "STRONG_RECOVERY"
RECOVERY_QUALITIES = (NO_RECOVERY, WEAK_RECOVERY, RECOVERING_QUALITY,
                      STRONG_RECOVERY)

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
    # --- exposure limits ---
    max_ladder_depth: int = 22          # MAX_LADDER_DEPTH (hard ceiling)
    # ABSOLUTE depths at which the ladder enters each zone. Absolute, not
    # fractions of the ceiling: "12 levels deep" is a fact about the exposure,
    # and it should not change meaning because someone raised the ceiling.
    #
    # INITIAL TEST DEFAULTS - chosen to be explainable, fitted to nothing.
    extended_depth: int = 9             # LADDER_EXTENDED_DEPTH
    deep_depth: int = 12                # LADDER_DEEP_DEPTH
    critical_depth: int = 16            # LADDER_CRITICAL_DEPTH
    # --- deep-ladder health (INITIAL TEST DEFAULTS) ---
    deep_risk_enabled: bool = True      # DEEP_LADDER_RISK_ENABLED
    # Drawdown from peak, in account currency, that counts as a large hole for
    # a deep basket.
    deep_max_drawdown: float = 10.00    # DEEP_LADDER_MAX_DRAWDOWN
    # Imbalance above which a deep basket is treated as dangerously one-sided.
    deep_max_imbalance: float = 0.60    # DEEP_LADDER_MAX_IMBALANCE
    # How long a deep basket may stay underwater without a meaningful recovery
    # before that itself counts against it, in seconds.
    deep_recovery_timeout: float = 900.0   # DEEP_LADDER_RECOVERY_TIMEOUT
    # Adverse price movement, in price units over the movement window, that
    # counts as "still going the wrong way" rather than noise.
    deep_adverse_move: float = 0.60     # DEEP_LADDER_ADVERSE_MOVEMENT_THRESHOLD
    # |buy_vol - sell_vol| / gross_vol above which the basket is one-sided
    # enough to stop adding to it. 1.0 = entirely one-sided.
    max_imbalance: float = 0.80         # MAX_DIRECTION_IMBALANCE
    imbalance_action: str = "MONITOR"   # IMBALANCE_ACTION
    # A basket with one leg is trivially 100% one-sided and means nothing. The
    # imbalance grading only applies once there is enough exposure for the
    # ratio to say something.
    imbalance_min_positions: int = 4    # IMBALANCE_MIN_POSITIONS
    # --- recovery quality ---
    # Fraction of the hole climbed back that counts as a weak / strong
    # recovery. `recovery_fraction` above remains the floor for calling it a
    # recovery at all.
    weak_recovery_at: float = 0.25      # WEAK_RECOVERY_FRACTION
    strong_recovery_at: float = 0.75    # STRONG_RECOVERY_FRACTION

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

        # --- exposure (PHASE 2/3) ---
        self.net_volume = 0.0
        self.gross_volume = 0.0
        self.buy_volume = 0.0
        self.sell_volume = 0.0
        self.open_buys = 0
        self.open_sells = 0
        self.position_count = 0
        self.pending_buys = 0
        self.pending_sells = 0
        self.direction_imbalance = 0.0
        self.imbalance_state = BALANCED
        self.max_direction_imbalance = 0.0
        self.ladder_state = LADDER_NORMAL
        self.depth_zone = LADDER_NORMAL
        self.deep_ladder_state = DEEP_HEALTHY
        self.risk_score = 0.0
        self.risk_state = RISK_LOW
        self.expansion_allowed = True
        self.expansion_block_reason = ""
        self.exposure_capped = False       # new exposure is being withheld
        self.exposure_cap_reason = ""

        # --- recovery quality (PHASE 4) ---
        self.recovery_quality = NO_RECOVERY
        self.recovery_duration = 0.0
        self.ladder_depth_at_recovery_start = 0
        self.recovery_count = 0
        self.weak_recovery_count = 0
        self.strong_recovery_count = 0
        self._recovery_open = False        # inside a recovery episode
        self.favorable_ticks = 0
        self.adverse_ticks = 0

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
        Peak and lowest, both measured on the basket's FLOATING P/L.

        For an ACTIVE basket the managed quantity is what is still open, so
        current, peak and lowest are all floating and
        drawdown_from_peak = peak - current is a like-for-like subtraction.
        Realized P/L is deliberately excluded and reported separately: mixing
        banked profit into a floating figure is how a bot ends up defending a
        peak it no longer holds.

        Under the basket architecture nothing closes mid-cycle, so realized is
        0.00 for the life of a normal cycle and the two agree anyway; they only
        diverge when a leg is closed early (a stop-out, or a manual close).
        """
        total = self.floating_pnl
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

        # "Favorable" means the recent move helps THIS basket, which depends on
        # which way the basket is leaning - so the exposure has to be current
        # before the movement can be classified.
        self.update_exposure(positions)
        move = self.recent_price_change
        net = self.net_volume
        if abs(move) < self.spacing * 0.1 or not net:
            self.price_state = FLAT_MOVE
        elif (move > 0) == (net > 0):
            self.price_state = FAVORABLE
            self.favorable_ticks += 1
        else:
            self.price_state = ADVERSE
            self.adverse_ticks += 1
        return self.price_state

    # -------------------------------------------------------------- exposure
    def update_exposure(self, positions=(), pending_buys=0, pending_sells=0):
        """
        What the basket is actually carrying, measured in VOLUME.

        Counting orders would call BUY 11 x 0.01 and SELL 1 x 0.11 balanced;
        they are not. Everything downstream - the imbalance state, the price
        reading, the exit engine - uses these numbers.
        """
        buy_vol = sum(p.volume for p in positions if p.side == BUY)
        sell_vol = sum(p.volume for p in positions if p.side == SELL)
        weighted = sum(p.price_open * p.volume for p in positions)
        gross = buy_vol + sell_vol

        self.buy_volume = round(buy_vol, 4)
        self.sell_volume = round(sell_vol, 4)
        self.gross_volume = round(gross, 4)
        self.net_volume = round(buy_vol - sell_vol, 4)
        self.open_buys = sum(1 for p in positions if p.side == BUY)
        self.open_sells = sum(1 for p in positions if p.side == SELL)
        self.position_count = len(positions)
        self.pending_buys = pending_buys
        self.pending_sells = pending_sells
        self.basket_average_entry = round(weighted / gross, 5) if gross else 0.0
        # 0.0 = perfectly balanced, 1.0 = entirely one-sided
        self.direction_imbalance = round(abs(buy_vol - sell_vol) / gross, 4) \
            if gross > 0 else 0.0
        self.max_direction_imbalance = max(self.max_direction_imbalance,
                                           self.direction_imbalance)
        return self.direction_imbalance

    @property
    def net_direction(self):
        if self.net_volume > 0:
            return BUY
        if self.net_volume < 0:
            return SELL
        return ""

    def grade_exposure(self, rules):
        """
        Ladder depth and one-sidedness, graded. Returns (ladder_state,
        imbalance_state).

        Grading only: nothing here closes a basket. It decides whether MORE
        exposure may be added, and it feeds the exit engine.
        """
        used = self.ladder_depth_used
        cap = max(0, int(rules.max_ladder_depth))
        # Absolute zones. The hard ceiling is checked first because it is a
        # risk control, not a zone - it is the one place depth alone still
        # stops expansion outright.
        if cap and used >= cap:
            self.ladder_state = LADDER_MAX_DEPTH
        elif rules.critical_depth and used >= rules.critical_depth:
            self.ladder_state = LADDER_CRITICAL
        elif rules.deep_depth and used >= rules.deep_depth:
            self.ladder_state = LADDER_DEEP
        elif rules.extended_depth and used >= rules.extended_depth:
            self.ladder_state = LADDER_EXTENDED
        else:
            self.ladder_state = LADDER_NORMAL
        self.depth_zone = self.ladder_state

        imbalance = self.direction_imbalance
        graded = self.position_count >= max(1, int(rules.imbalance_min_positions))
        if self.gross_volume <= 0 or not graded:
            # not enough legs for the ratio to mean anything yet
            self.imbalance_state = BALANCED
        elif imbalance >= max(rules.max_imbalance, 1e-9):
            self.imbalance_state = EXTREMELY_IMBALANCED
        elif imbalance >= rules.max_imbalance * 0.5:
            self.imbalance_state = BUY_HEAVY if self.net_volume > 0 else SELL_HEAVY
        else:
            self.imbalance_state = BALANCED

        self._grade_deep_health(rules)
        self._decide_expansion(rules)
        return self.ladder_state, self.imbalance_state

    # ------------------------------------------------- deep-ladder health
    def _grade_deep_health(self, rules):
        """
        WHICH KIND of deep this basket is, and how risky that is.

        A deep ladder is not automatically a bad ladder - we have watched deep
        baskets recover. So depth alone decides nothing here; it only decides
        that these questions are worth asking. The answers come from the
        basket's own behaviour: how far under it is, how one-sided, whether it
        is climbing back, and whether price is still going the wrong way.
        """
        deep = self.ladder_state in (LADDER_DEEP, LADDER_CRITICAL,
                                     LADDER_MAX_DEPTH)
        pnl = self.floating_pnl
        drawdown = self.drawdown
        adverse = (self.price_state == ADVERSE and
                   abs(self.recent_price_change) >= rules.deep_adverse_move)
        stale = (self.time_underwater >= rules.deep_recovery_timeout and
                 self.recovery_quality in (NO_RECOVERY, WEAK_RECOVERY))
        one_sided = self.direction_imbalance >= rules.deep_max_imbalance
        big_hole = drawdown >= rules.deep_max_drawdown

        # --- the health reading -------------------------------------------
        if not deep:
            state = DEEP_HEALTHY
        elif pnl >= 0 or self.recovery_quality == STRONG_RECOVERY:
            # green, or fighting its way back convincingly
            state = DEEP_RECOVERING if pnl < 0 else DEEP_HEALTHY
        elif self.recovery_quality == RECOVERING_QUALITY and not adverse:
            state = DEEP_RECOVERING
        elif (self.ladder_state in (LADDER_CRITICAL, LADDER_MAX_DEPTH) and
              big_hole and adverse and
              self.recovery_quality in (NO_RECOVERY, WEAK_RECOVERY)):
            # every one of these has to be true - this is the emergency case
            state = DEEP_CRITICAL
        elif adverse or stale:
            state = DEEP_ADVERSE
        else:
            state = DEEP_WEAK
        self.deep_ladder_state = state

        # --- a score, so the reading is auditable rather than a vibe -------
        # Each term is 0..1 and contributes its own weight. They are summed,
        # not multiplied, so no single term can silently dominate, and the
        # weights are visible here rather than scattered through branches.
        depth_term = 0.0
        if rules.critical_depth > 0:
            depth_term = min(1.0, self.ladder_depth_used / rules.critical_depth)
        dd_term = (min(1.0, drawdown / rules.deep_max_drawdown)
                   if rules.deep_max_drawdown > 0 else 0.0)
        imb_term = (min(1.0, self.direction_imbalance / rules.deep_max_imbalance)
                    if rules.deep_max_imbalance > 0 else 0.0)
        move_term = 1.0 if adverse else (0.5 if self.price_state == FLAT_MOVE
                                         else 0.0)
        recovery_term = {STRONG_RECOVERY: 0.0, RECOVERING_QUALITY: 0.25,
                         WEAK_RECOVERY: 0.75, NO_RECOVERY: 1.0}.get(
                             self.recovery_quality, 1.0)
        if pnl >= 0:
            recovery_term = 0.0        # nothing to recover from
        self.risk_score = round(
            0.30 * depth_term + 0.25 * dd_term + 0.15 * imb_term +
            0.15 * move_term + 0.15 * recovery_term, 4)

        if self.deep_ladder_state == DEEP_CRITICAL:
            self.risk_state = RISK_CRITICAL
        elif self.risk_score >= 0.70:
            self.risk_state = RISK_HIGH
        elif self.risk_score >= 0.45:
            self.risk_state = RISK_MEDIUM
        else:
            self.risk_state = RISK_LOW
        return self.deep_ladder_state

    # ------------------------------------------------- expansion control
    def _decide_expansion(self, rules):
        """
        May the ladder add MORE exposure right now?

        This is the only place that question is answered. It never closes
        anything: a basket whose expansion is blocked keeps every position it
        has and stays under the exit engine exactly as before.

        The zones:
            NORMAL / EXTENDED  - expand freely
            DEEP               - expand only while the basket looks healthy
            CRITICAL           - expand only on a clear recovery
            MAX_LADDER_DEPTH   - the hard ceiling, never expand
        """
        reasons = []
        zone = self.ladder_state
        healthy = self.deep_ladder_state in (DEEP_HEALTHY, DEEP_RECOVERING)

        if zone == LADDER_MAX_DEPTH:
            reasons.append(
                f"ladder depth {self.ladder_depth_used}/"
                f"{rules.max_ladder_depth} (hard ceiling)")
        elif zone == LADDER_CRITICAL and rules.deep_risk_enabled:
            # At critical depth the bar is a genuine recovery, not merely the
            # absence of bad news.
            if self.recovery_quality != STRONG_RECOVERY and \
                    self.floating_pnl < 0:
                reasons.append(
                    f"depth {self.ladder_depth_used} is CRITICAL and the "
                    f"basket is not recovering strongly "
                    f"({self.recovery_quality})")
        elif zone == LADDER_DEEP and rules.deep_risk_enabled:
            if not healthy:
                reasons.append(
                    f"depth {self.ladder_depth_used} is DEEP and the basket "
                    f"is {self.deep_ladder_state} "
                    f"(drawdown {self.drawdown:.2f}, imbalance "
                    f"{self.direction_imbalance:.2f}, {self.price_state})")

        # imbalance is its own control and applies at any depth
        if self.imbalance_state == EXTREMELY_IMBALANCED and \
                rules.imbalance_action == IMBALANCE_STOP_NEW:
            reasons.append(f"direction imbalance {self.direction_imbalance:.2f} "
                           f">= {rules.max_imbalance:.2f}")

        self.expansion_block_reason = "; ".join(reasons)
        self.expansion_allowed = not reasons
        # kept as the existing name the engine already reads
        self.exposure_capped = bool(reasons)
        self.exposure_cap_reason = self.expansion_block_reason
        return self.expansion_allowed

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
        pnl = self.floating_pnl
        if pnl <= -abs(rules.underwater_at):
            # The episode starts the moment the basket is meaningfully under
            # water, so recovery is measured from the hole it actually dug -
            # not from wherever we happened to notice it climbing.
            if not self._recovery_open:
                self._recovery_open = True
                self.recovery_start_pnl = self.lowest_pnl
                self._recovery_start_wall = now
                self.ladder_depth_at_recovery_start = self.ladder_depth_used
                self.recovery_count += 1
            self.was_underwater = True
            self.recovery_start_pnl = min(self.recovery_start_pnl, self.lowest_pnl)

        depth = abs(min(0.0, self.lowest_pnl))
        recovered = pnl - self.lowest_pnl
        # a real climb back, not a one-tick bounce
        meaningful = (self.was_underwater and depth > 0 and
                      recovered >= depth * max(0.0, rules.recovery_fraction))
        if meaningful and self.recovery_start_at is None:
            self.recovery_start_at = now - self.started_at

        # --- how WELL it is climbing back ---------------------------------
        start_wall = getattr(self, "_recovery_start_wall", None)
        self.recovery_duration = (round(max(0.0, now - start_wall), 1)
                                  if start_wall is not None else 0.0)
        fraction = (recovered / depth) if depth > 0 else 0.0
        if not self.was_underwater or depth <= 0 or recovered <= 0:
            quality = NO_RECOVERY
        elif fraction >= rules.strong_recovery_at:
            quality = STRONG_RECOVERY
        elif fraction >= max(rules.recovery_fraction, rules.weak_recovery_at):
            quality = RECOVERING_QUALITY
        elif fraction >= rules.weak_recovery_at:
            quality = WEAK_RECOVERY
        else:
            quality = NO_RECOVERY
        if quality != self.recovery_quality:
            if quality == STRONG_RECOVERY:
                self.strong_recovery_count += 1
            elif quality == WEAK_RECOVERY:
                self.weak_recovery_count += 1
        self.recovery_quality = quality

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
        return round(self.floating_pnl - self.lowest_pnl, 2)

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
        if self.floating_pnl > 0:
            self.time_in_profit += elapsed
        if self.floating_pnl < 0:
            self.time_underwater += elapsed
        if self.protection_active:
            self.time_in_protection += elapsed
        peak_wall = getattr(self, "_peak_wall", None)
        self.time_since_peak = 0.0 if peak_wall is None else max(0.0, now - peak_wall)

        if self.target_at is None and rules.target > 0 and \
                self.floating_pnl >= rules.target:
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
        self.grade_exposure(rules)
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
        pnl = self.floating_pnl
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

        # --- 2b. a STRONG recovery that is now green is protected ------------
        # It fought back from a real hole. Handing that back is how a recovered
        # basket becomes a loss again, so it is defended rather than left to
        # find out whether the move continues.
        if self.recovery_quality == STRONG_RECOVERY and pnl > 0 and \
                self.price_state == ADVERSE:
            return EXIT, STRONG_RECOVERY_PROTECTION, (
                f"recovered {self.recovery_amount:+.2f} from "
                f"{self.lowest_pnl:+.2f} and price has turned against the "
                f"basket at {pnl:+.2f} - protecting the recovery")

        # --- 2c. the deep ladder ---------------------------------------------
        # DEPTH NEVER CLOSES A BASKET. A deep ladder is not a bad ladder - deep
        # baskets recover, and force-closing one because it is deep destroys
        # exactly the winners we have watched come back. Depth decides how much
        # MORE exposure may be added (that is _decide_expansion); HEALTH decides
        # whether to exit, and that is what these branches read.
        if rules.deep_risk_enabled:
            # The emergency: deep AND severely underwater AND not recovering
            # AND still going the wrong way. All four, or this does not fire.
            if self.deep_ladder_state == DEEP_CRITICAL:
                return EXIT, CRITICAL_LADDER_RISK, (
                    f"depth {self.ladder_depth_used} at {pnl:+.2f}, "
                    f"{self.drawdown:.2f} off a {peak:+.2f} peak, "
                    f"{self.recovery_quality} and price still adverse "
                    f"(risk {self.risk_score:.2f}) - recovery has stopped "
                    f"looking likely")
            # A deep basket that is GREEN and turning is taken rather than
            # carried further: its exposure cannot be reduced by adding to it.
            if pnl > 0 and self.price_state == ADVERSE and \
                    self.deep_ladder_state in (DEEP_WEAK, DEEP_ADVERSE):
                return EXIT, DEEP_LADDER_RISK, (
                    f"depth {self.ladder_depth_used} is {self.ladder_state} "
                    f"and {self.deep_ladder_state} with {pnl:+.2f} on the "
                    f"table and price turning - taking it rather than "
                    f"carrying deep exposure further")

        # A dangerously one-sided basket that is green and turning, at any
        # depth. Adding the other side to balance it would be martingale.
        if pnl > 0 and self.price_state == ADVERSE and \
                self.imbalance_state == EXTREMELY_IMBALANCED:
            return EXIT, DIRECTION_IMBALANCE_RISK, (
                f"basket is {self.direction_imbalance:.0%} one-sided "
                f"({self.net_direction or 'flat'}) at {pnl:+.2f} with price "
                f"turning - the next adverse leg is disproportionate")

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
        """
        Give-back from the basket's own peak. Never negative.

        Floating against floating: peak_pnl is the highest FLOATING P/L this
        cycle reached, so this subtracts like from like.
        """
        return max(0.0, self.peak_pnl - self.floating_pnl)

    # the spec's name for the same number, used by the telemetry log
    drawdown_from_peak = drawdown

    @property
    def profit_giveback(self):
        """How much of the best excursion was handed back by the close."""
        return round(max(0.0, self.peak_pnl - self.floating_pnl), 2)

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
            "lowest_pnl": round(self.lowest_pnl, 2),
            # --- recovery ---
            "recovery_state": self.recovery_state,
            "was_underwater": self.was_underwater,
            "time_underwater": round(self.time_underwater, 1),
            "time_since_peak": round(self.time_since_peak, 1),
            "recovery_amount": self.recovery_amount,
            "recovery_speed": self.recovery_speed,
            "recovery_start_pnl": ("" if self.recovery_start_pnl is None
                                   else round(self.recovery_start_pnl, 2)),
            "time_of_max_drawdown": ("" if self.lowest_at is None
                                     else round(self.lowest_at, 1)),
            # --- price movement ---
            "price_state": self.price_state,
            "price_vs_anchor": self.price_vs_anchor,
            "price_vs_average_entry": self.price_vs_average_entry,
            "basket_average_entry": self.basket_average_entry,
            "recent_price_change": self.recent_price_change,
            "price_velocity": self.price_velocity,
            "net_volume": self.net_volume,
            # --- exposure ---
            "gross_volume": self.gross_volume,
            "buy_volume": self.buy_volume,
            "sell_volume": self.sell_volume,
            "net_direction": self.net_direction,
            "direction_imbalance": self.direction_imbalance,
            "imbalance_state": self.imbalance_state,
            "max_direction_imbalance": round(self.max_direction_imbalance, 4),
            "ladder_state": self.ladder_state,
            "depth_zone": self.depth_zone,
            "deep_ladder_state": self.deep_ladder_state,
            "risk_score": self.risk_score,
            "risk_state": self.risk_state,
            "expansion_allowed": self.expansion_allowed,
            "expansion_block_reason": self.expansion_block_reason,
            "exposure_capped": self.exposure_capped,
            "exposure_cap_reason": self.exposure_cap_reason,
            "position_count": self.position_count,
            # --- recovery quality ---
            "recovery_quality": self.recovery_quality,
            "recovery_duration": self.recovery_duration,
            "ladder_depth_at_recovery_start": self.ladder_depth_at_recovery_start,
            "recovery_count": self.recovery_count,
            "weak_recovery_count": self.weak_recovery_count,
            "strong_recovery_count": self.strong_recovery_count,
            "favorable_price_movement": self.favorable_ticks,
            "adverse_price_movement": self.adverse_ticks,
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
