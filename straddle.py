"""
STEPPED_STRADDLE: one position, managed by a stepped stop loss.

A deliberately small strategy, and deliberately separate from the basket:

    new closed M1 candle -> capture ONE reference price
    BUY STOP  = reference + STEP_DISTANCE
    SELL STOP = reference - STEP_DISTANCE
    one fills -> cancel the other, set the initial SL
    price moves a full step in favour -> SL to entry +/- SPREAD_BUFFER
    each further step -> SL forward one more step
    SL hit -> flat -> cooldown -> wait for the next closed M1 candle

There is no basket here: no profit target, no recovery, no profit protection,
no ladder expansion, no averaging, no martingale. The stop IS the exit.

This module is pure decision logic - no MT5, no threads, no I/O - so the
arithmetic can be tested exhaustively on its own. The engine owns execution
and calls `plan_levels`, `initial_sl` and `next_sl`.
"""

import math
import time
from dataclasses import dataclass, field

BUY = "BUY"
SELL = "SELL"

# ------------------------------------------------------------------- states
WAITING_FOR_M1 = "WAITING_FOR_M1"        # flat, waiting for a closed candle
STARTING_STRADDLE = "STARTING_STRADDLE"  # reference captured, placing orders
STRADDLE_ACTIVE = "STRADDLE_ACTIVE"      # both stops live, nothing filled
POSITION_OPEN = "POSITION_OPEN"          # filled, initial SL set
BREAKEVEN = "BREAKEVEN"                  # SL moved to entry +/- buffer
STEP_TRAILING = "STEP_TRAILING"          # SL following in discrete steps
POSITION_CLOSED = "POSITION_CLOSED"      # the stop (or anything) took it
VERIFYING_FLAT = "VERIFYING_FLAT"        # confirming 0 positions / 0 orders
COOLDOWN = "COOLDOWN"                    # the mandatory settle time
STRADDLE_STATES = (WAITING_FOR_M1, STARTING_STRADDLE, STRADDLE_ACTIVE,
                   POSITION_OPEN, BREAKEVEN, STEP_TRAILING, POSITION_CLOSED,
                   VERIFYING_FLAT, COOLDOWN)

# ------------------------------------------------------------- exit reasons
# Imported from basket.py so there is ONE spelling of every exit reason in the
# codebase, rather than a second private list here.
from basket import STOP_LOSS_HIT  # noqa: E402  (kept next to its siblings)
MANUAL_EXIT = "MANUAL_EXIT"
RISK_EXIT = "RISK_EXIT"


@dataclass
class StraddleRules:
    """
    Everything the strategy reads, in one object, built from live settings.

    Five INDEPENDENT price distances. They are deliberately not derived from
    one another - how far the breakout sits from the reference, how much room
    the trade is given, when it becomes free, and how tightly it is trailed
    are four separate decisions.

    All are XAUUSD PRICE UNITS. 0.50 means fifty cents of gold, never a
    broker-defined pip and never points.

    INITIAL TEST DEFAULTS. Nothing here is fitted to anything.
    """
    entry_offset: float = 1.00          # ENTRY_OFFSET: breakout from reference
    initial_sl_distance: float = 0.50   # INITIAL_SL_DISTANCE: room behind entry
    breakeven_trigger: float = 0.50     # BREAKEVEN_TRIGGER: move in favour
    breakeven_offset: float = 0.00      # BREAKEVEN_OFFSET: past entry, 0 = flat
    trail_trigger: float = 1.00         # TRAIL_TRIGGER: when the trail arms
    trail_distance: float = 0.50        # TRAIL_DISTANCE: behind the high water
    lot_size: float = 0.01
    cancel_opposite_on_fill: bool = True
    use_new_m1_candle_entry: bool = True

    def levels(self, reference):
        """The two breakout prices, symmetric about one reference."""
        offset = abs(float(self.entry_offset))
        ref = float(reference)
        return ref + offset, ref - offset

    def pending_sl(self, direction, entry):
        """The protective stop that rides with the pending order."""
        room = abs(float(self.initial_sl_distance))
        entry = float(entry)
        return entry - room if direction == BUY else entry + room


@dataclass
class StraddleCycle:
    """One straddle: its reference, its position, and its stop."""
    cycle_id: int = 0
    reference: float = 0.0
    state: str = WAITING_FOR_M1

    intended_buy: float = 0.0
    intended_sell: float = 0.0
    actual_buy: float = 0.0
    actual_sell: float = 0.0
    buy_ticket: int = 0
    sell_ticket: int = 0

    direction: str = ""
    entry_price: float = 0.0
    position_ticket: int = 0
    initial_sl: float = 0.0
    current_sl: float = 0.0
    breakeven_set: bool = False
    trail_active: bool = False
    # The best price this trade has SEEN, which is what the trail hangs off.
    # Not the current price: a pullback must never drag the stop back.
    high_water: float = 0.0
    low_water: float = 0.0
    favorable_steps: int = 0        # kept for the status screen
    sl_modifications: int = 0

    m1_bar_time: int = 0
    opened_at: float = 0.0
    closed_at: float = 0.0
    exit_reason: str = ""
    realized_pnl: float = 0.0
    # every timestamp the spec asks for, filled in as it happens
    timing: dict = field(default_factory=dict)

    def stamp(self, key, when=None):
        self.timing.setdefault(key, when or time.time())
        return self.timing[key]


def initial_sl(direction, entry_price, rules):
    """The protective stop that goes on with (or immediately after) the fill."""
    return rules.pending_sl(direction, entry_price)


def favorable_distance(direction, entry_price, price):
    """How far price has moved IN FAVOUR of the position. Never negative."""
    entry = float(entry_price)
    move = (float(price) - entry) if direction == BUY else (entry - float(price))
    return max(0.0, move)


def update_water(direction, price, high_water, low_water):
    """
    Track the best price this trade has seen.

    The trail hangs off THIS, not off the current price - that is the whole
    difference between a ratchet and a stop that follows price back down.
    """
    price = float(price)
    if direction == BUY:
        return (max(price, high_water) if high_water else price), low_water
    return high_water, (min(price, low_water) if low_water else price)


def next_sl(direction, entry_price, price, rules, current_sl=None,
            breakeven_set=False, high_water=0.0, low_water=0.0):
    """
    Where the stop SHOULD be right now. Returns (sl, stage) or (None, stage).

    Three stages, in order of how much the trade has earned:

        moved < BREAKEVEN_TRIGGER   ->  leave the initial stop alone
        moved >= BREAKEVEN_TRIGGER  ->  entry +/- BREAKEVEN_OFFSET
        moved >= TRAIL_TRIGGER      ->  high_water -/+ TRAIL_DISTANCE

    The trail is CONTINUOUS, not stepped: every new extreme the trade reaches
    drags the stop with it, and nothing else does. A pullback moves the stop
    nowhere, because the water mark it hangs off has not moved.

    `current_sl` is passed in so the ratchet is enforced HERE, in one place:
    the returned stop is never less protective than the one already set.
    """
    entry = float(entry_price)
    moved = favorable_distance(direction, entry, price)
    water = high_water if direction == BUY else low_water

    stage = "INITIAL"
    target = None
    if moved >= abs(float(rules.trail_trigger)) and water:
        # trailing: hang the stop off the best price SEEN, not the price now
        gap = abs(float(rules.trail_distance))
        target = (water - gap) if direction == BUY else (water + gap)
        stage = "TRAILING"
    elif moved >= abs(float(rules.breakeven_trigger)):
        off = abs(float(rules.breakeven_offset))
        target = (entry + off) if direction == BUY else (entry - off)
        stage = "BREAKEVEN"

    if target is None:
        return None, stage
    if current_sl and not sl_is_improvement(direction, target, current_sl):
        return None, stage
    return target, stage


def sl_is_improvement(direction, new_sl, current_sl):
    """True only when `new_sl` protects more than `current_sl`."""
    if not current_sl:
        return bool(new_sl)
    if direction == BUY:
        return float(new_sl) > float(current_sl) + 1e-9
    return float(new_sl) < float(current_sl) - 1e-9
