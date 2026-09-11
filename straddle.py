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

    INITIAL TEST DEFAULTS. Nothing here is fitted to anything.
    """
    step_distance: float = 1.20       # STEP_DISTANCE, in PRICE units
    spread_buffer: float = 0.30       # SPREAD_BUFFER, in PRICE units
    lot_size: float = 0.01            # LOT_SIZE (the existing sizing setting)
    cancel_opposite_on_fill: bool = True
    check_on_new_bar_only: bool = True

    def levels(self, reference):
        """The two intended stop prices, symmetric about one reference."""
        step = abs(float(self.step_distance))
        ref = float(reference)
        return ref + step, ref - step


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
    favorable_steps: int = 0
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
    """
    The protective stop that goes on immediately after the fill.

    One full step behind entry - which, by construction, is the reference the
    straddle was built around.
    """
    step = abs(float(rules.step_distance))
    entry = float(entry_price)
    return entry - step if direction == BUY else entry + step


def favorable_distance(direction, entry_price, price):
    """How far price has moved IN FAVOUR of the position. Never negative."""
    entry = float(entry_price)
    move = (float(price) - entry) if direction == BUY else (entry - float(price))
    return max(0.0, move)


def next_sl(direction, entry_price, price, rules, current_sl=None,
            breakeven_set=False):
    """
    Where the stop SHOULD be, given how far price has run in our favour.

    Returns (sl, steps, is_breakeven) - or (None, steps, False) when the stop
    should not move at all.

        0 full steps   ->  leave the initial stop alone
        1 full step    ->  breakeven: entry +/- SPREAD_BUFFER
        n full steps   ->  that, plus (n-1) further steps

    The breakeven move is step 1, NOT an extra step on top of it: counting it
    twice is the classic off-by-one in this kind of trail, and it would put
    the stop a whole step further forward than the price has actually earned.

    The caller supplies `current_sl` so the ratchet is enforced here, in one
    place: the returned stop is never worse than the one already in place.
    """
    step = abs(float(rules.step_distance))
    if step <= 0:
        return None, 0, False
    buffer_ = abs(float(rules.spread_buffer))
    entry = float(entry_price)

    moved = favorable_distance(direction, entry, price)
    steps = int(math.floor(moved / step + 1e-9))
    if steps < 1:
        return None, 0, False

    # step 1 IS breakeven; every step after it adds one more step of distance
    extra = (steps - 1) * step
    if direction == BUY:
        target = entry + buffer_ + extra
    else:
        target = entry - buffer_ - extra

    # --- the ratchet: a stop only ever moves the protective way ------------
    if current_sl:
        if direction == BUY and target <= float(current_sl) + 1e-9:
            return None, steps, False
        if direction == SELL and target >= float(current_sl) - 1e-9:
            return None, steps, False
    return target, steps, (steps == 1 and not breakeven_set)


def sl_is_improvement(direction, new_sl, current_sl):
    """True only when `new_sl` protects more than `current_sl`."""
    if not current_sl:
        return bool(new_sl)
    if direction == BUY:
        return float(new_sl) > float(current_sl) + 1e-9
    return float(new_sl) < float(current_sl) - 1e-9
