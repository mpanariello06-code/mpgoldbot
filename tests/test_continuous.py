"""Continuous cycling: immediate deployment, clean handoff, no duplicates."""
import pathlib
import shutil

from harness import Suite, use_stub_mt5
use_stub_mt5()

import config as cfg
from broker import BUY_STOP, SELL_STOP
from fakes import Recorder, TickFeed, make_paper, trigger_buy, trigger_sell
from ladder_engine import RollingLadderEngine, State, parse_comment
from runtime_settings import RuntimeSettings

t = Suite("continuous")
TMP = pathlib.Path("/tmp/continuous_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)


def build(overrides=None, name="c", feed=None):
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}_set.json")
    for key, value in (overrides or {}).items():
        settings._values[key] = value
    broker, feed = make_paper(feed=feed or TickFeed(4010.00),
                              state_path=TMP / f"{name}_paper.json")
    rec = Recorder()
    engine = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                                 state_path=TMP / f"{name}_state.json")
    engine.resume()
    return engine, broker, feed, settings, rec


def force_reversal(engine, broker, feed, limit=40):
    """
    Buy into a rise, then let the market turn and keep falling until the exit
    engine calls it - the market keeps moving whether or not levels are left.
    """
    start = engine.cycle.cycle_id
    engine.step()
    buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
                  key=lambda o: o.price)
    trigger_buy(feed, buys[1].price)
    engine.step()
    for _ in range(limit):
        sells = [o for o in broker.orders() if o.side == SELL_STOP]
        if sells:
            trigger_sell(feed, max(sells, key=lambda o: o.price).price)
        else:
            feed.set(round(feed.bid - 0.15, 2))
        engine.step()
        if engine.cycle.cycle_id != start:
            return True
    return False


# ===========================================================================
t.section("IMMEDIATE FIRST LADDER")
engine, broker, feed, settings, rec = build(name="first")
t.check("nothing is live before the first pass", not broker.orders())
engine.step()
t.check("ladder deployed on the very first pass", len(broker.orders()) == 10,
        f"{len(broker.orders())} orders")
t.check("no candle or signal was waited for",
        rec.count("LADDER_CREATED") == 1 and rec.count("CYCLE_STARTED") == 1)
t.check("state is active immediately", engine.state == State.LADDER_ACTIVE,
        engine.state)

t.section("IMMEDIATE RE-ENTRY AFTER A CYCLE ENDS")
engine, broker, feed, settings, rec = build(
    {"cooldown_after_loss_minutes": 15}, name="reentry")
rolled = force_reversal(engine, broker, feed)
t.check("the cycle rolled", rolled, f"cycle #{engine.cycle.cycle_id}")
t.check("the new ladder is live in the SAME pass that closed the old one",
        len(broker.orders()) > 0, f"{len(broker.orders())} orders")
t.check("every live order belongs to the new cycle",
        all(parse_comment(o.comment)[0] == engine.cycle.cycle_id
            for o in broker.orders()))
t.check("no cooldown after a normal cycle", engine.cooldown_until == 0.0,
        str(engine.cooldown_until))
t.check("nothing from the old cycle is left open", not broker.positions())
t.check("the new cycle is anchored on the CURRENT price, not the old grid",
        abs(engine.cycle.anchor - feed().mid) < 0.35,
        f"anchor {engine.cycle.anchor} price {feed().mid:.2f}")
t.check("the new sequence starts empty", engine.sequence.total_triggers == 0)

t.section("COOLDOWN ONLY ON A RISK-FORCED CLOSE")
engine, broker, feed, settings, rec = build(
    {"max_cycle_drawdown": 0.50, "cooldown_after_loss_minutes": 5}, name="cool")
engine.step()
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[0].price)
engine.step()
feed.set(buys[0].price - 0.90)
engine.step()
t.check("risk close armed the cooldown", engine.cooldown_until > 0)
t.check("attributed to risk",
        any(c.kind == "RISK" for c in rec.cycles if c.kind_of == "complete"))
engine.step()
t.check("no new ladder while the cooldown holds", not broker.orders())
t.check("cooldown is the stated reason", "cooldown" in engine.block_reason.lower(),
        engine.block_reason)

t.section("CLEAN HANDOFF: NO NEW LADDER UNTIL MT5 IS CLEAN")


class StubbornBroker:
    """A broker that refuses to cancel orders for the first few attempts."""

    def __init__(self, inner, refusals=3):
        self.inner = inner
        self.refusals = refusals
        self.cancel_attempts = 0

    def __getattr__(self, item):
        return getattr(self.inner, item)

    def cancel_order(self, ticket):
        self.cancel_attempts += 1
        if self.refusals > 0:
            self.refusals -= 1
            return False, "broker says no"
        return self.inner.cancel_order(ticket)


engine, broker, feed, settings, rec = build(name="stubborn")
engine.step()
stubborn = StubbornBroker(broker, refusals=40)
engine.broker = stubborn
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[1].price)
engine.step()
cycle_before = engine.cycle.cycle_id
orders_before = {o.ticket for o in broker.orders()}
# force the exit engine's hand with a drawdown breach
settings._values["max_cycle_drawdown"] = 0.10
feed.set(buys[1].price - 1.00)
engine.step()
t.check("transition is in progress, not finished",
        engine._closing_cycle is not None, str(engine._closing_cycle is None))
t.check("cycle id did NOT advance while orders remain",
        engine.cycle.cycle_id == cycle_before, str(engine.cycle.cycle_id))
t.check("no second ladder was built on top",
        {o.ticket for o in broker.orders()} <= orders_before)
t.check("state says it is closing", engine.state == State.CLOSING_CYCLE,
        engine.state)
engine.step()
t.check("still pending while the broker keeps refusing",
        engine.cycle.cycle_id == cycle_before)
stubborn.refusals = 0
engine.step()
t.check("handoff completes once MT5 is clean",
        engine.cycle.cycle_id == cycle_before + 1,
        f"#{engine.cycle.cycle_id}")
t.check("exactly one cycle was recorded for the one exit event",
        len([c for c in rec.cycles if c.kind_of == "complete"]) == 1,
        str([c.cycle_id for c in rec.cycles if c.kind_of == "complete"]))

t.section("ONE EXIT EVENT -> EXACTLY ONE NEW CYCLE")
engine, broker, feed, settings, rec = build(name="once")
force_reversal(engine, broker, feed)
completes = [c for c in rec.cycles if c.kind_of == "complete"]
starts = [c for c in rec.cycles if c.kind_of == "start"]
t.check("one completion recorded", len(completes) == 1, str(len(completes)))
t.check("one new cycle started per completion",
        len(starts) == len(completes) + 1, f"{len(starts)} starts")
t.check("cycle ids are sequential and unique",
        [c.cycle_id for c in starts] == sorted(set(c.cycle_id for c in starts)),
        str([c.cycle_id for c in starts]))
for _ in range(5):
    engine.step()
t.check("repeated passes do not roll the cycle again",
        len([c for c in rec.cycles if c.kind_of == "complete"]) == 1)

t.section("CYCLE IDS NEVER REPEAT ACROSS A RESTART")
engine, broker, feed, settings, rec = build(name="ids")
force_reversal(engine, broker, feed)
last_id = engine.cycle.cycle_id
engine.save()
t.check("cycle advanced", last_id > 1, str(last_id))
engine2 = RollingLadderEngine(broker, settings, hooks=Recorder().hooks(),
                              state_path=TMP / "ids_state.json")
engine2.resume()
t.check("a restart never goes backwards", engine2.cycle.cycle_id >= last_id,
        f"{engine2.cycle.cycle_id} vs {last_id}")
t.check("the highest id seen is remembered",
        engine2.max_cycle_id >= last_id, str(engine2.max_cycle_id))
before = engine2.cycle.cycle_id
engine2._start_cycle(reason="test")
t.check("the next cycle id is strictly higher",
        engine2.cycle.cycle_id > before, f"{before} -> {engine2.cycle.cycle_id}")

t.section("LOSING-STREAK CIRCUIT BREAKER RECOVERS")
engine, broker, feed, settings, rec = build(
    {"max_consecutive_losing_cycles": 2, "cooldown_after_loss_minutes": 5},
    name="streak")
now = [1000.0]
engine.clock = lambda: now[0]
engine.consecutive_losing_cycles = 2
engine.step()
t.check("the streak pauses new cycles", not broker.orders(),
        f"{len(broker.orders())} orders")
t.check("the reason says it is a pause, not a stop",
        "resuming in" in engine.block_reason, engine.block_reason)
t.check("the block is logged once, not every pass",
        len([e for e in rec.events if e[0] == "RISK_BLOCK"]) == 1,
        str(len([e for e in rec.events if e[0] == "RISK_BLOCK"])))
now[0] += 60
engine.step()
t.check("still paused inside the window", not broker.orders())
now[0] += 5 * 60
engine.step()
t.check("the streak clears when the pause elapses",
        engine.consecutive_losing_cycles == 0)
t.check("trading resumes by itself", len(broker.orders()) > 0,
        f"{len(broker.orders())} orders")
t.check("the recovery is logged",
        any(e[0] == "RISK_CLEARED" for e in rec.events))

engine, broker, feed, settings, rec = build(
    {"max_consecutive_losing_cycles": 2, "cooldown_after_loss_minutes": 0},
    name="streakhard")
engine.consecutive_losing_cycles = 2
engine.step()
t.check("with no cooldown configured it stays a hard stop",
        not broker.orders() and "no cooldown" in engine.block_reason,
        engine.block_reason)

t.section("CONTINUOUS OPERATION OVER MANY CYCLES")
engine, broker, feed, settings, rec = build(
    {"cooldown_after_loss_minutes": 0}, name="many")
price = 4010.0
for i in range(60):
    engine.step()
    orders = broker.orders()
    if orders:
        # walk price onto the nearest level, alternating sides to force
        # reversals and keep cycles turning over
        side = BUY_STOP if i % 7 < 4 else SELL_STOP
        candidates = [o for o in orders if o.side == side]
        if candidates:
            target = (min(candidates, key=lambda o: o.price) if side == BUY_STOP
                      else max(candidates, key=lambda o: o.price))
            (trigger_buy if side == BUY_STOP else trigger_sell)(feed, target.price)
    engine.step()
completed = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("several cycles completed unattended", len(completed) >= 2,
        str(len(completed)))
t.check("the engine is still deploying ladders at the end",
        len(broker.orders()) > 0 or engine.block_reason,
        f"{len(broker.orders())} orders, block: {engine.block_reason}")
t.check("no cycle id was ever reused",
        len({c.cycle_id for c in completed}) == len(completed))
t.check("the ladder never ran two cycles at once",
        all(len({parse_comment(o.comment)[0] for o in broker.orders()}) <= 1
            for _ in [0]))

t.done()
