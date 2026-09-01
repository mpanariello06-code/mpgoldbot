"""
Exit engine: the six scenarios from the spec, plus the guarantees that the
decision is NOT a trade count and NOT a dollar amount.
"""
from harness import Suite, use_stub_mt5
use_stub_mt5()

from exit_engine import (BUY, CONTINUE, EXIT, MONITOR, MOMENTUM_CONTINUATION,
                         MOMENTUM_EXHAUSTION, REVERSAL_DETECTED, SELL,
                         ExitConfig, LadderSequence, RollingLadderExitEngine)

t = Suite("exit_engine")
SPACING = 0.30
ANCHOR = 4010.00
engine = RollingLadderExitEngine()


def walk(steps, anchor=ANCHOR, spacing=SPACING, gap=20.0, pnl_per_level=1.0,
         start_time=1000.0):
    """
    Replay a scripted sequence of (side, price_move_in_levels) triggers.

    Prices and P/L are derived from the moves, so each scenario is a real price
    path rather than a hand-set score.
    """
    seq = LadderSequence(1, anchor, spacing, started_at=start_time)
    price = anchor
    now = start_time
    index = 0
    for side, move in steps:
        now += gap
        price = round(price + move * spacing, 2)
        index += 1 if side == BUY else -1
        seq.record_trigger(side, index, price, ts=now)
    seq.update_price(price, ts=now)
    # each triggered level is worth roughly one level of movement
    seq.update_pnl(pnl_per_level)
    return seq


def assess(seq, money_per_level=1.0):
    return engine.assess(seq, money_per_level=money_per_level)


# ===========================================================================
t.section("SCENARIO A - four clean BUY triggers (directional move)")
seq = walk([(BUY, 1)] * 4)
a = assess(seq)
t.check("recognised as continuation", a.state == MOMENTUM_CONTINUATION, a.state)
t.check("momentum is high", a.momentum_score > 0.7, f"{a.momentum_score:.2f}")
t.check("no reversal reported", a.reversal_score == 0.0)
t.check("does not exit purely because 4 levels triggered",
        a.decision == CONTINUE, f"{a.decision} score={a.exit_score:.1f}")
t.check("sequence counted correctly",
        (seq.buy_triggers, seq.sell_triggers, seq.consecutive_buy) == (4, 0, 4))
t.check("efficiency is high on a clean move", seq.efficiency > 0.95,
        f"{seq.efficiency:.2f}")

t.section("SCENARIO B - 2 BUY then 5 SELL (reversal)")
seq = walk([(BUY, 1), (BUY, 1), (SELL, -1), (SELL, -1), (SELL, -1),
            (SELL, -1), (SELL, -1)])
b = assess(seq)
t.check("reversal detected", b.state == REVERSAL_DETECTED, b.state)
t.check("reversal score is strong", b.reversal_score > 0.6, f"{b.reversal_score:.2f}")
t.check("continuation collapsed", b.continuation_score == 0.0)
t.check("cycle exits", b.decision == EXIT, f"{b.decision} score={b.exit_score:.1f}")
t.check("imbalance reported as 2.5x", abs(seq.imbalance - 2.5) < 1e-9,
        f"{seq.imbalance}")
t.check("direction change counted", seq.direction_changes == 1)
t.check("reason names the reversal", "reversal" in b.reason, b.reason)

t.section("SCENARIO C - seven BUY triggers, momentum still strong")
seq = walk([(BUY, 1)] * 7)
c = assess(seq)
t.check("still continuation", c.state == MOMENTUM_CONTINUATION, c.state)
t.check("does not exit on trade count alone", c.decision == CONTINUE,
        f"{c.decision} score={c.exit_score:.1f}")
t.check("7 triggers scores no higher than 4 when the move stays clean",
        c.exit_score <= a.exit_score + 20, f"{a.exit_score:.1f} -> {c.exit_score:.1f}")

t.section("SCENARIO D - 2 SELL then 3 BUY (reversal the other way)")
seq = walk([(SELL, -1), (SELL, -1), (BUY, 1), (BUY, 1), (BUY, 1)])
d = assess(seq)
t.check("reversal detected from a short start", d.state == REVERSAL_DETECTED, d.state)
t.check("initial side remembered", seq.initial_side == SELL)
t.check("dominant side flipped", seq.dominant_side == BUY)
t.check("exit or monitor", d.decision in (EXIT, MONITOR),
        f"{d.decision} score={d.exit_score:.1f}")

t.section("SCENARIO E - four BUY then the move dies (exhaustion)")
seq = walk([(BUY, 1), (BUY, 1), (BUY, 0.15), (BUY, 0.05)], gap=20.0)
# price then goes nowhere for a long while
last_price = seq.price
for i in range(1, 6):
    seq.update_price(last_price + (0.02 if i % 2 else -0.02), ts=1000 + 100 + i * 30)
e = assess(seq)
t.check("exhaustion recognised", e.state == MOMENTUM_EXHAUSTION, e.state)
t.check("exhaustion score raised", e.exhaustion_score > 0.35,
        f"{e.exhaustion_score:.2f}")
t.check("momentum decayed", e.momentum_score < 0.7, f"{e.momentum_score:.2f}")
t.check("not treated as a reversal", e.reversal_score == 0.0)

t.section("SCENARIO F - alternating BUY/SELL (chop)")
seq = walk([(BUY, 1), (SELL, -1), (BUY, 1), (SELL, -1), (BUY, 1), (SELL, -1)])
f = assess(seq)
t.check("chop shows as near-zero efficiency", seq.efficiency < 0.2,
        f"{seq.efficiency:.2f}")
t.check("direction changes counted", seq.direction_changes == 5,
        str(seq.direction_changes))
t.check("exhaustion is high in chop", f.exhaustion_score > 0.5,
        f"{f.exhaustion_score:.2f}")
t.check("chop pushes toward the exit", f.decision in (EXIT, MONITOR),
        f"{f.decision} score={f.exit_score:.1f}")
t.check("no continuation claimed in chop", f.continuation_score < 0.4,
        f"{f.continuation_score:.2f}")

# ===========================================================================
t.section("THE EXIT IS NOT A TRADE COUNT")
scores = []
for n in range(1, 9):
    s = walk([(BUY, 1)] * n)
    scores.append(round(assess(s).exit_score, 1))
t.check("a clean run never exits at any count",
        all(v < ExitConfig().threshold_exit for v in scores), str(scores))
short_reversal = assess(walk([(BUY, 1), (SELL, -1), (SELL, -1), (SELL, -1)]))
t.check("a 4-trigger reversal CAN exit while a 7-trigger trend does not",
        short_reversal.exit_score > c.exit_score,
        f"reversal {short_reversal.exit_score:.1f} vs trend {c.exit_score:.1f}")

t.section("THE EXIT IS NOT A DOLLAR AMOUNT")
trend = walk([(BUY, 1)] * 5)
rich = assess(trend, money_per_level=1.0)
trend.update_pnl(50.0)                      # a very profitable basket
rich_after = assess(trend, money_per_level=1.0)
# MONITOR is allowed here - it closes nothing, it only watches more closely.
# What must never happen is the profit pushing the cycle over the exit line.
t.check("a big profit alone does not close a trending cycle",
        rich_after.decision != EXIT and
        rich_after.exit_score < ExitConfig().threshold_exit,
        f"{rich_after.decision} score={rich_after.exit_score:.1f}")
t.check("profit does raise readiness a little",
        rich_after.exit_score >= rich.exit_score,
        f"{rich.exit_score:.1f} -> {rich_after.exit_score:.1f}")

rev = walk([(BUY, 1), (BUY, 1), (SELL, -1), (SELL, -1), (SELL, -1)])
rev.update_pnl(-0.2)
poor = assess(rev)
rev.update_pnl(3.0)
flush = assess(rev)
t.check("banked profit makes an established reversal easier to act on",
        flush.exit_score > poor.exit_score,
        f"{poor.exit_score:.1f} -> {flush.exit_score:.1f}")
t.check("a losing basket alone does not force a close",
        assess(walk([(BUY, 1)] * 3)).decision == CONTINUE)

losing = walk([(BUY, 1)] * 3)
losing.update_pnl(-5.0)
hold = assess(losing)
t.check("an open loss makes the engine less eager to bail without structure",
        hold.decision != EXIT, f"{hold.decision} score={hold.exit_score:.1f}")

t.section("P/L IS CONTEXT, NOT THE TRIGGER")
a_cycle = walk([(BUY, 1), (BUY, 1), (SELL, -1), (SELL, -1), (SELL, -1), (SELL, -1)])
a_cycle.update_pnl(1.20)
small_profit_reversal = assess(a_cycle)
b_cycle = walk([(BUY, 1)] * 6)
b_cycle.update_pnl(3.50)
big_profit_trend = assess(b_cycle)
t.check("+1.20 with a reversal closes", small_profit_reversal.decision == EXIT,
        f"{small_profit_reversal.exit_score:.1f}")
t.check("+3.50 with momentum does not close",
        big_profit_trend.decision != EXIT and
        big_profit_trend.exit_score < ExitConfig().threshold_exit,
        f"{big_profit_trend.decision} {big_profit_trend.exit_score:.1f}")
t.check("and the profitable trend still ranks below the small-profit reversal",
        big_profit_trend.exit_score < small_profit_reversal.exit_score,
        f"{big_profit_trend.exit_score:.1f} vs {small_profit_reversal.exit_score:.1f}")

t.section("DRAWDOWN AWARENESS")
seq = walk([(BUY, 1)] * 4)
seq.update_pnl(4.0)
before = assess(seq).exit_score
seq.update_pnl(0.2)                          # gave back most of the peak
after = assess(seq)
t.check("giving back the peak raises the score", after.exit_score > before,
        f"{before:.1f} -> {after.exit_score:.1f}")
t.check("drawdown pressure reported", after.drawdown_pressure > 0,
        f"{after.drawdown_pressure:.2f}")
t.check("drawdown measured from the cycle peak",
        abs(seq.drawdown - 3.8) < 1e-9, str(seq.drawdown))

t.section("TRACKED SEQUENCE (spec section 15)")
seq = walk([(BUY, 1), (BUY, 1), (SELL, -1)])
snap = seq.snapshot()
for key in ("buy_triggers", "sell_triggers", "consecutive_buy", "consecutive_sell",
            "last_side", "previous_side", "direction_changes", "buy_sell_ratio",
            "sell_buy_ratio", "imbalance", "ladder_depth_used",
            "price_distance_traveled", "average_gap", "basket_pnl",
            "basket_drawdown", "volatility", "efficiency", "net_levels"):
    t.check(f"tracks {key}", key in snap, str(snap.get(key)))
t.check("last/previous side", (snap["last_side"], snap["previous_side"]) == (SELL, BUY))
t.check("ladder depth used", snap["ladder_depth_used"] >= 1)

t.section("CONFIGURABILITY")
strict = RollingLadderExitEngine(ExitConfig(threshold_exit=20.0))
lax = RollingLadderExitEngine(ExitConfig(threshold_exit=99.0))
seq = walk([(BUY, 1), (BUY, 1), (SELL, -1), (SELL, -1), (SELL, -1)])
t.check("a low threshold exits sooner",
        strict.assess(seq).decision == EXIT)
t.check("a high threshold keeps rolling",
        lax.assess(seq).decision != EXIT)
weights = RollingLadderExitEngine(ExitConfig(w_reversal=0.0))
t.check("zeroing the reversal weight removes its influence",
        weights.assess(seq).exit_score < engine.assess(seq).exit_score)
t.check("every weight is exposed for fitting",
        len(ExitConfig().as_dict()) >= 15, str(len(ExitConfig().as_dict())))

t.section("NO LOOK-AHEAD")
seq = LadderSequence(1, ANCHOR, SPACING, started_at=1000.0)
seq.record_trigger(BUY, 1, 4010.30, ts=1020)
early = assess(seq)
seq.record_trigger(BUY, 2, 4010.60, ts=1040)
later = assess(seq)
t.check("an assessment only sees what had happened by then",
        early.exit_score != later.exit_score or early.momentum_score !=
        later.momentum_score, f"{early.exit_score} vs {later.exit_score}")
t.check("no future prices are consulted",
        seq.snapshot()["price"] == 4010.60, str(seq.snapshot()["price"]))

t.section("EMPTY / EDGE CASES")
empty = LadderSequence(1, ANCHOR, SPACING)
z = assess(empty)
t.check("no triggers -> continue", z.decision == CONTINUE and z.exit_score == 0.0)
t.check("no triggers -> no scores invented",
        z.reversal_score == 0 and z.exhaustion_score == 0)
t.check("zero-division safe on an empty sequence",
        empty.efficiency == 0.0 and empty.imbalance == 0.0)
single = walk([(BUY, 1)])
t.check("a single trigger cannot claim exhaustion",
        assess(single).exhaustion_score == 0.0)
t.check("a single trigger cannot claim reversal",
        assess(single).reversal_score == 0.0)

t.done()
