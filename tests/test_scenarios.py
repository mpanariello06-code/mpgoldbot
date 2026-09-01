"""
The observed exit scenarios, the profit fallback and the stuck-cycle guard.

These are approximations of behaviour observed in a reference robot, not a
reconstruction of its algorithm: each is a configurable reading, and the tests
pin the reading's shape rather than any magic number.
"""
import pathlib
import shutil

from harness import Suite, use_stub_mt5
use_stub_mt5()

import config as cfg
from broker import BUY_STOP, SELL_STOP
from exit_engine import (BUY, CONTINUE, EXIT, PROFIT_FALLBACK, RISK_TIMEOUT,
                         SCENARIO_1_DIRECTIONAL, SCENARIO_2_REVERSAL,
                         SCENARIO_3_EXTENDED_LADDER, SELL, LadderSequence,
                         RollingLadderExitEngine)
from fakes import Recorder, TickFeed, make_paper, trigger_buy, trigger_sell
from ladder_engine import RollingLadderEngine
from runtime_settings import RuntimeSettings

t = Suite("scenarios")
TMP = pathlib.Path("/tmp/scenario_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
SPACING = 0.30
ANCHOR = 4010.00
engine = RollingLadderExitEngine()


def walk(steps, gap=20.0, stall_after=0.0, pnl=1.0, start=1000.0):
    """Replay a scripted trigger sequence, optionally letting it go quiet."""
    seq = LadderSequence(1, ANCHOR, SPACING, started_at=start)
    price, now, index = ANCHOR, start, 0
    for side, move in steps:
        now += gap
        price = round(price + move * SPACING, 2)
        index += 1 if side == BUY else -1
        seq.record_trigger(side, index, price, ts=now)
    if stall_after:
        for i in range(10):
            now += stall_after / 10
            seq.update_price(round(price + (0.01 if i % 2 else -0.01), 2), ts=now)
    else:
        seq.update_price(price, ts=now)
    seq.update_pnl(pnl)
    return seq


# ===========================================================================
t.section("SCENARIO 1 - clean directional move that has paid")
running = walk([(BUY, 1)] * 4)                       # still accelerating
a = engine.assess(running, money_per_level=1.0)
t.check("a run still going is NOT cut short", a.decision == CONTINUE,
        f"{a.decision} score={a.exit_score:.1f}")
t.check("the directional reading stays small while it runs",
        a.directional_score < 0.35, f"{a.directional_score:.2f}")

cooling = walk([(BUY, 1)] * 4, stall_after=600, pnl=4.0)
b = engine.assess(cooling, money_per_level=1.0)
t.check("the same run reads as directional once it cools",
        b.directional_score > 0.4, f"{b.directional_score:.2f}")
t.check("directional is stronger after cooling than during",
        b.directional_score > a.directional_score,
        f"{a.directional_score:.2f} -> {b.directional_score:.2f}")
t.check("it exits", b.decision == EXIT, f"{b.decision} score={b.exit_score:.1f}")
t.check("labelled scenario 1", b.scenario == SCENARIO_1_DIRECTIONAL, b.scenario)

selling = walk([(SELL, -1)] * 4, stall_after=600, pnl=4.0)
c = engine.assess(selling, money_per_level=1.0)
t.check("the same holds for a SELL run", c.scenario == SCENARIO_1_DIRECTIONAL,
        c.scenario)

t.section("SCENARIO 2 - reversal, both ways")
seq = walk([(BUY, 1), (BUY, 1), (SELL, -1), (SELL, -1), (SELL, -1),
            (SELL, -1), (SELL, -1)])
d = engine.assess(seq, money_per_level=1.0)
t.check("BUY then SELL dominance exits", d.decision == EXIT,
        f"{d.decision} score={d.exit_score:.1f}")
t.check("labelled scenario 2", d.scenario == SCENARIO_2_REVERSAL, d.scenario)
t.check("imbalance measured", abs(seq.imbalance - 2.5) < 1e-9, str(seq.imbalance))

seq = walk([(SELL, -1), (SELL, -1), (BUY, 1), (BUY, 1), (BUY, 1), (BUY, 1),
            (BUY, 1)])
e = engine.assess(seq, money_per_level=1.0)
t.check("SELL then BUY dominance also exits", e.decision == EXIT,
        f"{e.decision} score={e.exit_score:.1f}")
t.check("labelled scenario 2", e.scenario == SCENARIO_2_REVERSAL, e.scenario)

t.section("SCENARIO 3 - extended ladder")
long_run = walk([(BUY, 1)] * 9)
f = engine.assess(long_run, money_per_level=1.0)
t.check("a long run that is still moving keeps going",
        f.decision == CONTINUE, f"{f.decision} score={f.exit_score:.1f}")
t.check("ladder depth is tracked", long_run.ladder_depth_used == 9,
        str(long_run.ladder_depth_used))

spent = walk([(BUY, 1)] * 9, stall_after=900, pnl=9.0)
g = engine.assess(spent, money_per_level=1.0)
t.check("a deep ladder that has stopped paying exits", g.decision == EXIT,
        f"{g.decision} score={g.exit_score:.1f}")
t.check("extended reading is present", g.extended_score > 0.3,
        f"{g.extended_score:.2f}")
t.check("labelled by the dominant reading",
        g.scenario in (SCENARIO_3_EXTENDED_LADDER, SCENARIO_1_DIRECTIONAL),
        g.scenario)

t.section("NONE OF THE THREE IS A TRADE COUNT")
for n in range(1, 12):
    running = walk([(BUY, 1)] * n)
    a = engine.assess(running, money_per_level=1.0)
    if a.decision == EXIT:
        t.check(f"a still-running {n}-trigger move never exits on count alone",
                False, f"exited at {n} with score {a.exit_score:.1f}")
        break
else:
    t.check("a still-running move never exits on count alone, 1..11", True)


# ===========================================================================
def build(overrides=None, name="s", feed=None, clock=None):
    """The clock must be injected at construction: cycle age is measured from it."""
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}_set.json")
    for key, value in (overrides or {}).items():
        settings._values[key] = value
    broker, feed = make_paper(feed=feed or TickFeed(4010.00),
                              state_path=TMP / f"{name}_paper.json")
    if clock:
        broker.clock = clock
    rec = Recorder()
    eng = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                              state_path=TMP / f"{name}_state.json",
                              clock=clock or __import__("time").time)
    eng.resume()
    return eng, broker, feed, settings, rec


t.section("PROFIT FALLBACK - confirmed recovery, no primary exit")
# A basket that no scenario explains: two buys, then the operator stops new
# entries ("none") so the ladder is frozen and the trigger history can no longer
# grow. Two triggers are below every scenario's minimum, so reversal,
# directional, extended and exhaustion all read zero no matter how long it sits
# there. That is exactly the case the fallback is for - an open basket drifting
# with nothing else to close it.
now = [5000.0]
eng, broker, feed, settings, rec = build(
    {"profit_confirmation_seconds": 60, "profit_fallback_buffer_levels": 0.5,
     "max_cycle_duration_minutes": 0, "max_cycle_drawdown": 0,
     "max_open_positions": 8, "tp_levels": 40, "stop_loss_distance": 0.0,
     # the continuation guard is exercised in its own section below
     "profit_fallback_continuation_guard": 1.1}, name="fallback",
    clock=lambda: now[0])
eng.step()

for _ in range(2):
    buys = [o for o in broker.orders() if o.side == BUY_STOP]
    if buys:
        trigger_buy(feed, min(buys, key=lambda o: o.price).price)
    now[0] += 30
    eng.step()
start_cycle = eng.cycle.cycle_id
t.check("the basket has triggers but no scenario", eng.sequence.total_triggers == 2,
        str(eng.sequence.total_triggers))

settings._values["direction_filter"] = "none"
now[0] += 10
eng.step()
frozen_triggers = eng.sequence.total_triggers
t.check("the frozen ladder has no pending orders left", not broker.orders(),
        f"{len(broker.orders())} pending")
t.check("the basket is still open", bool(broker.positions()),
        f"{len(broker.positions())} positions")


def drift_to(target):
    """Move the market so the frozen basket floats at about `target` dollars."""
    positions = broker.positions()
    net = sum((1 if p.side == BUY else -1) * p.volume for p in positions)
    assert abs(net) > 1e-9, "the basket must not be flat for this test"
    per_unit = eng.spec.money_per_price_unit(abs(net)) * (1 if net > 0 else -1)
    current = sum(p.profit for p in positions)
    feed.set(round(feed.bid + (target - current) / per_unit, 2))


# under water: the fallback must stay silent
drift_to(-0.80)
now[0] += 10
eng.step()
t.check("a losing basket does not trigger the fallback",
        eng.cycle.cycle_id == start_cycle and eng._profit_since is None,
        f"basket {eng.sequence.basket_pnl:+.2f}")

# the market comes back and the basket goes green
drift_to(0.60)
now[0] += 10
eng.step()
t.check("the basket recovered", eng.sequence.basket_pnl > 0,
        f"{eng.sequence.basket_pnl:+.2f}")
t.check("a fresh profit is not enough on its own",
        eng.cycle.cycle_id == start_cycle, f"#{eng.cycle.cycle_id}")
t.check("the confirmation clock started", eng._profit_since is not None)

# it dips back under: the confirmation must restart
drift_to(-0.50)
now[0] += 10
eng.step()
t.check("dipping back under resets the confirmation",
        eng._profit_since is None, f"basket {eng.sequence.basket_pnl:+.2f}")

drift_to(0.70)
now[0] += 10
eng.step()
t.check("the clock restarts on the next recovery", eng._profit_since is not None)
t.check("still not closed before the confirmation elapses",
        eng.cycle.cycle_id == start_cycle, f"#{eng.cycle.cycle_id}")
t.check("no primary exit explains this basket",
        eng.assessment is not None and eng.assessment.decision != EXIT,
        f"{eng.assessment.decision} score={eng.assessment.exit_score:.1f}")
t.check("the frozen ladder never grew", eng.sequence.total_triggers ==
        frozen_triggers, str(eng.sequence.total_triggers))

settings._values["direction_filter"] = "off"      # entries allowed again
now[0] += 61
eng.step()
t.check("confirmed profit with no primary exit closes the cycle",
        not eng.cycle_active, f"#{eng.cycle.cycle_id} active={eng.cycle_active}")
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("recorded as PROFIT_FALLBACK",
        closes and closes[-1].kind == PROFIT_FALLBACK,
        str([c.kind for c in closes]))
t.check("it banked a profit", closes and closes[-1].total > 0,
        str(closes[-1].total if closes else None))
ctx = closes[-1].context if closes else {}
t.check("the exit records the state that CAUSED it, not the empty state after",
        ctx.get("open_positions_at_exit") == 2 and
        ctx.get("open_buys_at_exit") == 2 and
        ctx.get("pending_orders_at_exit") == 0, str(ctx))
t.check("with the price it exited at", ctx.get("exit_price"), str(ctx.get("exit_price")))
t.check("and the floating P/L it was carrying",
        ctx.get("floating_pnl_at_exit", 0) > 0,
        str(ctx.get("floating_pnl_at_exit")))
t.check("nothing is left live while the cycle is closed",
        not broker.orders() and not broker.positions(),
        f"{len(broker.orders())} orders, {len(broker.positions())} positions")
now[0] += 11                                  # the re-entry cooldown elapses
eng.step()
t.check("a new ladder follows the cooldown",
        eng.cycle_active and (len(broker.orders()) > 0 or eng.block_reason),
        f"{len(broker.orders())} orders, block {eng.block_reason!r}")
t.check("the fallback is not a dollar target - the buffer scales with the lot",
        settings.get("profit_fallback_buffer_levels") *
        eng.money_per_level(settings.snapshot()) < 1.0,
        f"buffer {settings.get('profit_fallback_buffer_levels') * eng.money_per_level(settings.snapshot()):.2f}")

t.section("PROFIT FALLBACK DOES NOT OVERRIDE STRONG CONTINUATION")
# An A/B on the same scripted run: the only difference is where the guard sits
# relative to the continuation the move actually reads. Nothing here pins a
# magic continuation value - the second run's guard is derived from the first.


def working_run(guard, name):
    """Two quick buys in a rising market - a basket in profit that is still moving."""
    now = [9000.0]
    eng, broker, feed, settings, rec = build(
        {"profit_confirmation_seconds": 1, "profit_fallback_buffer_levels": 0.1,
         "max_cycle_duration_minutes": 0, "max_cycle_drawdown": 0,
         "max_open_positions": 8, "tp_levels": 40, "stop_loss_distance": 0.0,
         "profit_fallback_continuation_guard": guard}, name=name,
        clock=lambda: now[0])
    eng.step()
    first = eng.cycle.cycle_id
    for _ in range(2):
        buys = [o for o in broker.orders() if o.side == BUY_STOP]
        if buys:
            trigger_buy(feed, min(buys, key=lambda o: o.price).price)
        now[0] += 5
        eng.step()
    held = eng.assessment                     # the reading the guard is tested against
    basket = eng.sequence.basket_pnl
    now[0] += 5                               # the confirmation window elapses
    eng.step()
    return eng, broker, rec, first, held, basket


# A: the guard is disabled, so the confirmed profit is taken
eng, broker, rec, first, held, basket = working_run(1.1, "guard_off")
buffer_money = eng.money_per_level(eng.settings.snapshot()) * 0.1
t.check("the control run is in profit past the fallback buffer",
        basket > buffer_money > 0, f"{basket:+.2f} vs {buffer_money:.2f}")
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("with the guard disabled the profit is taken",
        closes and closes[-1].kind == PROFIT_FALLBACK,
        str([c.kind for c in closes]))
t.check("the control cycle did close", not eng.cycle_active,
        f"#{eng.cycle.cycle_id} active={eng.cycle_active}")
continuation = held.continuation_score
t.check("the move was reading as continuing at the time", continuation > 0,
        f"{continuation:.2f}")

# B: identical run, but the guard now sits below that reading
eng, broker, rec, first, held, basket = working_run(
    round(continuation * 0.5, 4), "guard_on")
t.check("the same reading is produced",
        abs(held.continuation_score - continuation) < 1e-6,
        f"{held.continuation_score:.4f} vs {continuation:.4f}")
t.check("a strongly continuing run is held despite a confirmed profit",
        eng.cycle_active and eng.cycle.cycle_id == first,
        f"#{eng.cycle.cycle_id} active={eng.cycle_active}")
t.check("nothing was closed", not [c for c in rec.cycles if c.kind_of == "complete"],
        str([c.kind for c in rec.cycles]))
t.check("and the basket is still open, profit and all", bool(broker.positions())
        and basket > buffer_money,
        f"{len(broker.positions())} positions, {basket:+.2f}")

t.section("STUCK CYCLE - RISK TIMEOUT")
now = [20000.0]
eng, broker, feed, settings, rec = build(
    {"max_cycle_duration_minutes": 30, "profit_fallback_enabled": False,
     "max_cycle_drawdown": 0}, name="timeout", clock=lambda: now[0])
eng.step()
buys = [o for o in broker.orders() if o.side == BUY_STOP]
trigger_buy(feed, min(buys, key=lambda o: o.price).price)
now[0] += 60
eng.step()
start_cycle = eng.cycle.cycle_id
# the market goes nowhere and the basket never recovers
for i in range(20):
    now[0] += 60
    feed.set(round(feed.bid - 0.01, 2))
    eng.step()
t.check("still open before the limit", eng.cycle.cycle_id == start_cycle,
        f"#{eng.cycle.cycle_id}")
now[0] += 31 * 60
eng.step()
t.check("the cycle is closed once the limit passes",
        not eng.cycle_active, f"#{eng.cycle.cycle_id} active={eng.cycle_active}")
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("recorded as RISK_TIMEOUT",
        closes and closes[-1].kind == RISK_TIMEOUT,
        str([c.kind for c in closes]))
t.check("nothing is left open", not broker.positions())
now[0] += 11                                  # the 10s re-entry cooldown alone
eng.step()
t.check("a risk-forced close waits out the LONGER loss cooldown too",
        not eng.cycle_active and "cooldown" in eng.block_reason.lower(),
        f"#{eng.cycle.cycle_id} {eng.block_reason!r}")
now[0] += settings.get("cooldown_after_loss_minutes") * 60 + 1
eng.step()
t.check("a new cycle starts once both cooldowns are served",
        eng.cycle_active and eng.sequence.total_triggers == 0,
        f"#{eng.cycle.cycle_id}")

t.section("A CYCLE CAN NEVER STAY OPEN INDEFINITELY")
t.check("the timeout is on by default",
        cfg.runtime_defaults()["max_cycle_duration_minutes"] > 0,
        str(cfg.runtime_defaults()["max_cycle_duration_minutes"]))
t.check("risk outranks the strategy",
        cfg.runtime_defaults()["max_cycle_drawdown"] > 0)

t.done()
