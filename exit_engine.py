"""
Adaptive exit engine for the rolling ladder.

The cycle does NOT end on a trade count or a dollar amount. It ends when the
market structure says the move is over. This module turns the observable
sequence of ladder triggers plus price behaviour into three readings -

    CONTINUATION   the original direction is still working
    REVERSAL       the original direction failed and the other side took over
    EXHAUSTION     levels keep triggering but price has stopped paying for them

- blends them into an exit score (0-100) and classifies CONTINUE / MONITOR /
EXIT. Every input, weight and threshold is configurable, and every component of
the score is reported, so the decisions can be re-fitted against historical
XAUUSD data later instead of guessed at.

P/L is context, never the trigger: profit already banked makes the engine more
willing to act on a reversal it has already detected, and an open loss makes it
less willing to bail without one. Neither can by itself end a cycle - that is
the risk manager's job, on its own drawdown limits.

Nothing here reads the future: all metrics come from triggers and price samples
that have already happened.
"""

import statistics
import time
from collections import deque
from dataclasses import dataclass, field

BUY = "BUY"
SELL = "SELL"

# ---------------------------------------------------------------- decisions
CONTINUE = "CONTINUE"
MONITOR = "MONITOR"
EXIT = "EXIT"

# ------------------------------------------------------------- exit reasons
SCENARIO_1_DIRECTIONAL = "SCENARIO_1_DIRECTIONAL"
SCENARIO_2_REVERSAL = "SCENARIO_2_REVERSAL"
SCENARIO_3_EXTENDED_LADDER = "SCENARIO_3_EXTENDED_LADDER"
PROFIT_FALLBACK = "PROFIT_FALLBACK"
RISK_DRAWDOWN = "RISK_DRAWDOWN"
RISK_TIMEOUT = "RISK_TIMEOUT"
RISK_SPREAD = "RISK_SPREAD"
MANUAL_STOP = "MANUAL_STOP"
OTHER_RISK_EXIT = "OTHER_RISK_EXIT"

# --------------------------------------------------------- structural states
IDLE = "IDLE"
LADDER_ACTIVE = "LADDER_ACTIVE"
POSITION_ACTIVE = "POSITION_ACTIVE"
MOMENTUM_CONTINUATION = "MOMENTUM_CONTINUATION"
REVERSAL_DETECTED = "REVERSAL_DETECTED"
MOMENTUM_EXHAUSTION = "MOMENTUM_EXHAUSTION"
EXIT_EVALUATION = "EXIT_EVALUATION"
CLOSING_CYCLE = "CLOSING_CYCLE"
RESETTING = "RESETTING"
NEW_CYCLE = "NEW_CYCLE"


def clamp01(value):
    if value != value:                       # NaN
        return 0.0
    return 0.0 if value < 0 else (1.0 if value > 1 else float(value))


@dataclass
class Trigger:
    side: str
    index: int
    price: float
    time: float


@dataclass
class Closure:
    side: str
    index: int
    price_open: float
    price_close: float
    profit: float
    time: float
    reason: str = ""


# ===========================================================================
# SEQUENCE TRACKING
# ===========================================================================
class LadderSequence:
    """
    Everything observable about the current cycle.

    Pure bookkeeping - no decisions. Fed by the ladder engine as levels trigger
    and positions close, plus a price sample on every pass so that a stalling
    market registers even when nothing is triggering.
    """

    MAX_SAMPLES = 400

    def __init__(self, cycle_id, anchor, spacing, started_at=None):
        self.cycle_id = cycle_id
        self.anchor = float(anchor)
        self.spacing = float(spacing) or 0.01
        self.started_at = started_at or time.time()

        self.triggers = []
        self.closures = []
        self.samples = deque(maxlen=self.MAX_SAMPLES)   # (time, price)

        self.buy_triggers = 0
        self.sell_triggers = 0
        self.consecutive_buy = 0
        self.consecutive_sell = 0
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
        self._last_sample_price = float(anchor)

    # ------------------------------------------------------------- feeding
    def record_trigger(self, side, index, price, ts=None):
        ts = ts or time.time()
        self.triggers.append(Trigger(side, int(index), float(price), ts))
        if side == BUY:
            self.buy_triggers += 1
            self.consecutive_buy += 1
            self.consecutive_sell = 0
        else:
            self.sell_triggers += 1
            self.consecutive_sell += 1
            self.consecutive_buy = 0
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
        ts = ts or time.time()
        self.path_distance += abs(price - self._last_sample_price)
        self._last_sample_price = price
        self.price = price
        self.high_price = max(self.high_price, price)
        self.low_price = min(self.low_price, price)
        self.samples.append((ts, price))

    def update_pnl(self, floating):
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
    def initial_side(self):
        return self.triggers[0].side if self.triggers else ""

    @property
    def dominant_side(self):
        if self.buy_triggers > self.sell_triggers:
            return BUY
        if self.sell_triggers > self.buy_triggers:
            return SELL
        return self.last_side

    @property
    def imbalance(self):
        """Dominance ratio, e.g. 2 BUY vs 5 SELL -> 2.5x."""
        high = max(self.buy_triggers, self.sell_triggers)
        low = min(self.buy_triggers, self.sell_triggers)
        if high == 0:
            return 0.0
        return high / low if low else float(high)

    @property
    def buy_sell_ratio(self):
        return self.buy_triggers / self.sell_triggers if self.sell_triggers \
            else float(self.buy_triggers)

    @property
    def sell_buy_ratio(self):
        return self.sell_triggers / self.buy_triggers if self.buy_triggers \
            else float(self.sell_triggers)

    @property
    def ladder_depth_used(self):
        if not self.triggers:
            return 0
        indexes = [t.index for t in self.triggers]
        return max(abs(min(indexes)), abs(max(indexes)))

    @property
    def basket_pnl(self):
        return self.realized_pnl + self.floating_pnl

    @property
    def drawdown(self):
        """How far the basket is off its own peak (never negative)."""
        return max(0.0, self.peak_pnl - self.basket_pnl)

    @property
    def net_levels(self):
        """Net distance from the cycle anchor, in ladder levels."""
        return (self.price - self.anchor) / self.spacing

    @property
    def path_levels(self):
        return self.path_distance / self.spacing

    @property
    def efficiency(self):
        """
        Net travel / total travel: 1.0 is a clean one-way move, near 0 is chop.
        """
        path = self.path_levels
        if path <= 1e-9:
            return 0.0
        return clamp01(abs(self.net_levels) / path)

    @property
    def trigger_gaps(self):
        return [round(b.time - a.time, 3)
                for a, b in zip(self.triggers, self.triggers[1:])]

    @property
    def last_gap(self):
        gaps = self.trigger_gaps
        return gaps[-1] if gaps else 0.0

    @property
    def average_gap(self):
        gaps = self.trigger_gaps
        return sum(gaps) / len(gaps) if gaps else 0.0

    @property
    def now(self):
        """Latest timestamp the sequence has actually been told about."""
        if self.samples:
            return self.samples[-1][0]
        return self.triggers[-1].time if self.triggers else self.started_at

    @property
    def time_since_last_trigger(self):
        if not self.triggers:
            return 0.0
        return max(0.0, self.now - self.triggers[-1].time)

    def excursion_levels(self, side):
        """Best favourable excursion for `side`, in levels from the anchor."""
        if side == BUY:
            return max(0.0, (self.high_price - self.anchor) / self.spacing)
        if side == SELL:
            return max(0.0, (self.anchor - self.low_price) / self.spacing)
        return 0.0

    def volatility_levels(self):
        """Recent price dispersion in levels (past samples only)."""
        prices = [p for _, p in list(self.samples)[-60:]]
        if len(prices) < 3:
            return 0.0
        try:
            return statistics.pstdev(prices) / self.spacing
        except statistics.StatisticsError:
            return 0.0

    def recent_progress_rate(self, intervals=2):
        """
        Levels of net price progress per trigger across the last `intervals`
        trigger gaps. 1.0 means each level is still paying a full level of
        movement; near 0 means levels keep firing (or fired recently) but price
        has stopped going anywhere.
        """
        if not self.triggers:
            return 0.0
        intervals = max(1, int(intervals))
        back = min(intervals, len(self.triggers))
        origin = self.triggers[-back].price
        span = abs(self.price - origin) / self.spacing
        return clamp01(span / back)

    def recent_sides(self, window):
        return [t.side for t in self.triggers[-window:]]

    def snapshot(self):
        """Flat dict of everything tracked - used by CSV and Telegram."""
        return {
            "cycle_id": self.cycle_id,
            "buy_triggers": self.buy_triggers,
            "sell_triggers": self.sell_triggers,
            "consecutive_buy": self.consecutive_buy,
            "consecutive_sell": self.consecutive_sell,
            "last_side": self.last_side,
            "previous_side": self.previous_side,
            "direction_changes": self.direction_changes,
            "buy_sell_ratio": round(self.buy_sell_ratio, 3),
            "sell_buy_ratio": round(self.sell_buy_ratio, 3),
            "imbalance": round(self.imbalance, 3),
            "initial_side": self.initial_side,
            "dominant_side": self.dominant_side,
            "ladder_depth_used": self.ladder_depth_used,
            "total_triggers": self.total_triggers,
            "net_levels": round(self.net_levels, 3),
            "path_levels": round(self.path_levels, 3),
            "price_distance_traveled": round(self.path_distance, 3),
            "efficiency": round(self.efficiency, 3),
            "last_gap": round(self.last_gap, 2),
            "average_gap": round(self.average_gap, 2),
            "time_since_last_trigger": round(self.time_since_last_trigger, 2),
            "basket_pnl": round(self.basket_pnl, 2),
            "basket_realized_pnl": round(self.realized_pnl, 2),
            "basket_floating_pnl": round(self.floating_pnl, 2),
            "basket_drawdown": round(self.drawdown, 2),
            "peak_pnl": round(self.peak_pnl, 2),
            "volatility": round(self.volatility_levels(), 3),
            "price": self.price,
        }


# ===========================================================================
# EXIT ENGINE
# ===========================================================================
@dataclass
class ExitConfig:
    """
    Every number the exit decision depends on. These are starting values, not
    discovered truth - they are the parameters to fit against historical data.
    """

    recent_window: int = 5            # triggers considered "recent"
    progress_intervals: int = 2       # trigger gaps used to read current progress
    consecutive_norm: float = 4.0     # consecutive triggers that read as a full run
    depth_norm: float = 6.0           # ladder depth that reads as fully extended
    gap_reference: float = 60.0       # seconds between triggers treated as brisk
    min_triggers_for_exhaustion: int = 3
    min_triggers_for_reversal: int = 2
    min_triggers_for_directional: int = 3
    min_triggers_for_extended: int = 5

    w_reversal: float = 0.75
    w_exhaustion: float = 0.45
    w_directional: float = 0.65
    w_extended: float = 0.50
    w_depth: float = 0.20
    w_drawdown: float = 0.25
    w_continuation: float = 0.45      # momentum suppresses exiting
    w_harvest: float = 0.30           # banked profit makes acting easier
    w_loss_hold: float = 0.20         # an open loss makes bailing harder

    threshold_exit: float = 70.0
    threshold_monitor: float = 40.0

    def as_dict(self):
        return dict(self.__dict__)


@dataclass
class ExitAssessment:
    decision: str = CONTINUE
    state: str = LADDER_ACTIVE          # structural reading of the market
    phase: str = LADDER_ACTIVE          # what the cycle does about it
    exit_score: float = 0.0
    momentum_score: float = 0.0
    continuation_score: float = 0.0
    reversal_score: float = 0.0
    exhaustion_score: float = 0.0
    directional_score: float = 0.0
    extended_score: float = 0.0
    scenario: str = ""            # which reading drove the exit
    harvest: float = 0.0
    drawdown_pressure: float = 0.0
    reason: str = ""
    contributions: dict = field(default_factory=dict)

    def as_dict(self):
        data = dict(self.__dict__)
        data.pop("contributions", None)
        for key in ("exit_score", "momentum_score", "continuation_score",
                    "reversal_score", "exhaustion_score", "directional_score",
                    "extended_score", "harvest", "drawdown_pressure"):
            data[key] = round(data[key], 3)
        return data


class RollingLadderExitEngine:
    """
    Reads a LadderSequence and decides whether the cycle should keep rolling.

    Deliberately NOT implemented: `if trades >= N: close` and
    `if pnl >= $X: close`. Counts and money feed normalised sub-scores; no
    single one of them can end a cycle on its own.
    """

    def __init__(self, config=None):
        self.config = config or ExitConfig()

    # ------------------------------------------------------------ components
    def momentum(self, seq):
        """
        Directional momentum, 0-1: clean travel, consecutive same-side triggers
        and how briskly levels are firing.
        """
        cfg = self.config
        if not seq.triggers:
            return 0.0
        consec = max(seq.consecutive_buy, seq.consecutive_sell)
        run = clamp01(consec / cfg.consecutive_norm)
        gap = seq.last_gap or seq.average_gap
        if gap <= 0:
            speed = 1.0 if seq.total_triggers > 1 else 0.5
        else:
            speed = clamp01(cfg.gap_reference / gap)
        # a stalling market loses momentum even without new triggers
        idle = seq.time_since_last_trigger
        if idle > cfg.gap_reference:
            speed *= clamp01(cfg.gap_reference / idle)
        # Progress is measured between triggers, so a market that simply stops
        # would otherwise keep momentum pinned at whatever the last two levels
        # covered - and continuation would suppress the exit forever. The
        # reading is only as good as it is recent.
        rate = seq.recent_progress_rate(cfg.progress_intervals)
        freshness = 1.0
        if idle > cfg.gap_reference:
            freshness = clamp01(cfg.gap_reference / idle)
        return clamp01(0.55 * rate * freshness + 0.20 * seq.efficiency +
                       0.10 * run + 0.15 * speed)

    def continuation(self, seq, momentum):
        """Momentum only counts as continuation while the original side leads."""
        if not seq.triggers:
            return 0.0
        if seq.dominant_side and seq.dominant_side != seq.initial_side:
            return 0.0
        if seq.last_side and seq.last_side != seq.initial_side:
            momentum *= 0.5          # the newest trigger already went the other way
        return clamp01(momentum)

    def reversal(self, seq, momentum):
        """
        Did the original direction fail and the other side take over?

        Opposite-side runs, dominance, and how much of the original favourable
        excursion has been given back.
        """
        cfg = self.config
        if seq.total_triggers < cfg.min_triggers_for_reversal:
            return 0.0
        initial = seq.initial_side
        if not initial or seq.direction_changes == 0:
            return 0.0

        opposite = seq.sell_triggers if initial == BUY else seq.buy_triggers
        same = seq.buy_triggers if initial == BUY else seq.sell_triggers
        if opposite == 0:
            return 0.0

        opp_side = SELL if initial == BUY else BUY
        consec_opp = seq.consecutive_sell if opp_side == SELL else seq.consecutive_buy
        run = clamp01(consec_opp / cfg.consecutive_norm)

        recent = seq.recent_sides(cfg.recent_window)
        recent_share = clamp01(sum(1 for s in recent if s == opp_side) /
                               max(1, len(recent)))

        peak = seq.excursion_levels(initial)
        if peak > 0:
            progress = (seq.net_levels if initial == BUY else -seq.net_levels)
            retrace = clamp01((peak - progress) / max(peak, 1.0))
        else:
            retrace = clamp01(0.5 * opposite / max(1.0, cfg.consecutive_norm))

        dominance = clamp01((opposite - same) / cfg.consecutive_norm)
        opp_momentum = momentum if seq.dominant_side == opp_side else 0.0

        score = (0.34 * run + 0.24 * retrace + 0.16 * recent_share +
                 0.16 * dominance + 0.10 * opp_momentum)
        return clamp01(score)

    def directional(self, seq, momentum, harvest):
        """
        Scenario 1: a clean same-direction run that has actually paid.

        Not "four trades and out" - a run only reads as complete when the moves
        were one-way, the distance is real, and the basket has banked something
        for it. A run that is still accelerating keeps its momentum and is held
        by the continuation term instead.
        """
        cfg = self.config
        if seq.total_triggers < cfg.min_triggers_for_directional:
            return 0.0
        if seq.dominant_side != seq.initial_side:
            return 0.0                      # that is a reversal, not a run
        run = clamp01(max(seq.consecutive_buy, seq.consecutive_sell) /
                      cfg.consecutive_norm)
        distance = clamp01(abs(seq.net_levels) / max(cfg.depth_norm, 1e-9))
        paid = clamp01(harvest)
        raw = (0.40 * run + 0.25 * seq.efficiency + 0.20 * distance +
               0.15 * paid)
        # A run only reads as FINISHED once it stops accelerating. While
        # momentum is still strong this stays near zero, so a big clean move is
        # ridden rather than cut short - it is the cooling that ends it.
        return clamp01(raw * clamp01(1.0 - momentum))

    def extended(self, seq, momentum):
        """
        Scenario 3: a substantial part of the ladder has been consumed and the
        market has travelled a long way with it - and that move is now easing.
        """
        cfg = self.config
        if seq.total_triggers < cfg.min_triggers_for_extended:
            return 0.0
        consumed = clamp01(seq.ladder_depth_used / max(cfg.depth_norm, 1e-9))
        distance = clamp01(abs(seq.net_levels) / max(cfg.depth_norm, 1e-9))
        persistence = clamp01(max(seq.buy_triggers, seq.sell_triggers) /
                              max(1.0, seq.total_triggers))
        raw = 0.45 * consumed + 0.35 * distance + 0.20 * persistence
        # same rule: a deep ladder in a still-running move is not a reason to
        # leave, a deep ladder in a stalling one is
        return clamp01(raw * clamp01(1.0 - momentum))

    def exhaustion(self, seq):
        """
        Levels keep triggering but the market has stopped paying: choppy travel,
        shrinking progress per trigger, stretching gaps between triggers.
        """
        cfg = self.config
        if seq.total_triggers < cfg.min_triggers_for_exhaustion:
            return 0.0

        chop = 1.0 - seq.efficiency
        flip_rate = clamp01(seq.direction_changes /
                            max(1.0, seq.total_triggers - 1))

        half = max(1, seq.total_triggers // 2)
        early = seq.triggers[:half]
        late = seq.triggers[half:]
        decay = 0.0
        if early and late:
            early_span = abs(early[-1].price - early[0].price) / seq.spacing \
                if len(early) > 1 else abs(early[0].price - seq.anchor) / seq.spacing
            late_span = abs(late[-1].price - late[0].price) / seq.spacing \
                if len(late) > 1 else abs(late[0].price - early[-1].price) / seq.spacing
            if early_span > 0:
                decay = clamp01(1.0 - (late_span / early_span))

        gaps = seq.trigger_gaps
        slowdown = 0.0
        if len(gaps) >= 2:
            first_half = gaps[:max(1, len(gaps) // 2)]
            second_half = gaps[max(1, len(gaps) // 2):]
            early_gap = sum(first_half) / len(first_half)
            late_gap = sum(second_half) / len(second_half)
            if early_gap > 0:
                slowdown = clamp01((late_gap - early_gap) / max(early_gap, 1e-9))
        stall = clamp01(seq.time_since_last_trigger / max(cfg.gap_reference, 1e-9)) \
            if seq.total_triggers else 0.0

        stalled_progress = 1.0 - seq.recent_progress_rate(cfg.progress_intervals)

        return clamp01(0.25 * chop + 0.15 * flip_rate + 0.25 * stalled_progress +
                       0.15 * decay + 0.10 * slowdown + 0.10 * stall)

    # ------------------------------------------------------- P/L as context
    def harvest(self, seq, money_per_level):
        """
        How much of what this cycle could plausibly have made is already banked.

        Normalised against the cycle's own activity, so it is never a fixed
        dollar target: it only makes the engine readier to act on a reversal or
        exhaustion it has already detected.
        """
        if money_per_level <= 0:
            return 0.0
        reference = money_per_level * max(1, seq.total_triggers)
        return clamp01(seq.basket_pnl / reference)

    def loss_hold(self, seq, money_per_level):
        """Reluctance to close a cycle that is currently under water."""
        if money_per_level <= 0 or seq.basket_pnl >= 0:
            return 0.0
        reference = money_per_level * max(1, seq.total_triggers)
        return clamp01(-seq.basket_pnl / reference)

    def drawdown_pressure(self, seq, money_per_level):
        """Give-back from the cycle's own peak, normalised by activity."""
        if money_per_level <= 0 or seq.peak_pnl <= 0:
            return 0.0
        reference = money_per_level * max(1, seq.total_triggers)
        return clamp01(seq.drawdown / reference)

    # ------------------------------------------------------------ assessment
    def assess(self, seq, money_per_level=1.0, has_exposure=True):
        cfg = self.config
        out = ExitAssessment()

        if seq.total_triggers == 0:
            out.state = LADDER_ACTIVE
            out.reason = "no levels triggered yet"
            return out

        momentum = self.momentum(seq)
        continuation = self.continuation(seq, momentum)
        reversal = self.reversal(seq, momentum)
        exhaustion = self.exhaustion(seq)
        harvest = self.harvest(seq, money_per_level)
        directional = self.directional(seq, momentum, harvest)
        extended = self.extended(seq, momentum)
        loss_hold = self.loss_hold(seq, money_per_level)
        dd = self.drawdown_pressure(seq, money_per_level)
        depth = clamp01(seq.ladder_depth_used / cfg.depth_norm)

        contributions = {
            "reversal": cfg.w_reversal * reversal,
            "exhaustion": cfg.w_exhaustion * exhaustion,
            "directional": cfg.w_directional * directional,
            "extended": cfg.w_extended * extended,
            "depth": cfg.w_depth * depth,
            "drawdown": cfg.w_drawdown * dd,
            # banked profit only amplifies structure that is already there
            "harvest": cfg.w_harvest * harvest * max(reversal, exhaustion,
                                                      directional, extended),
            "continuation": -cfg.w_continuation * continuation,
            "loss_hold": -cfg.w_loss_hold * loss_hold * (1.0 - reversal),
        }
        # Reasons to leave add up; momentum and an open loss hold the cycle back
        # proportionally rather than by subtraction, so the pressure to exit
        # still ranks correctly while a strong move is suppressing it.
        pressure = sum(v for v in contributions.values() if v > 0)
        restraint = clamp01(-sum(v for v in contributions.values() if v < 0))
        score = 100.0 * clamp01(pressure * (1.0 - restraint))

        out.momentum_score = momentum
        out.continuation_score = continuation
        out.reversal_score = reversal
        out.exhaustion_score = exhaustion
        out.directional_score = directional
        out.extended_score = extended
        out.harvest = harvest
        out.drawdown_pressure = dd
        out.exit_score = score
        out.contributions = {k: round(v, 4) for k, v in contributions.items()}

        # structural reading, independent of the score
        if reversal >= max(exhaustion, continuation) and reversal > 0.35:
            out.state = REVERSAL_DETECTED
        elif exhaustion >= max(reversal, continuation) and exhaustion > 0.35:
            out.state = MOMENTUM_EXHAUSTION
        elif continuation > 0.35 and score < cfg.threshold_exit:
            out.state = MOMENTUM_CONTINUATION
        else:
            out.state = EXIT_EVALUATION

        # `state` stays the structural reading of the market; `phase` is what
        # the cycle is about to do about it.
        # which of the observed scenarios best explains this exit
        scenarios = {
            SCENARIO_2_REVERSAL: contributions["reversal"],
            SCENARIO_1_DIRECTIONAL: contributions["directional"],
            SCENARIO_3_EXTENDED_LADDER: contributions["extended"],
        }
        best, best_value = max(scenarios.items(), key=lambda kv: kv[1])
        out.scenario = best if best_value > 0 else ""

        if score >= cfg.threshold_exit:
            out.decision = EXIT
            out.phase = CLOSING_CYCLE if has_exposure else RESETTING
            if not out.scenario:
                out.scenario = OTHER_RISK_EXIT
        elif score >= cfg.threshold_monitor:
            out.decision = MONITOR
            out.phase = EXIT_EVALUATION
        else:
            out.decision = CONTINUE
            out.phase = POSITION_ACTIVE if has_exposure else LADDER_ACTIVE

        out.reason = self._explain(out, seq)
        return out

    @staticmethod
    def _explain(assessment, seq):
        drivers = sorted(assessment.contributions.items(),
                         key=lambda kv: -abs(kv[1]))[:3]
        parts = [f"{name} {value:+.2f}" for name, value in drivers if abs(value) > 0.01]
        head = {
            REVERSAL_DETECTED: "reversal",
            MOMENTUM_EXHAUSTION: "exhaustion",
            MOMENTUM_CONTINUATION: "continuation",
        }.get(assessment.state, assessment.state.lower().replace("_", " "))
        return (f"{head}: {seq.buy_triggers}B/{seq.sell_triggers}S "
                f"imbalance {seq.imbalance:.2f}x eff {seq.efficiency:.2f}"
                + (f" | {', '.join(parts)}" if parts else ""))
