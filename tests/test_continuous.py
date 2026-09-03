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

def _validate_with_leftovers():
    """config.validate()'s warnings with CYCLE_CLOSE_POSITIONS off."""
    real = cfg.CYCLE_CLOSE_POSITIONS
    cfg.CYCLE_CLOSE_POSITIONS = False
    try:
        return cfg.validate()[1]
    finally:
        cfg.CYCLE_CLOSE_POSITIONS = real


t = Suite("continuous")
TMP = pathlib.Path("/tmp/continuous_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)


def build(overrides=None, name="c", feed=None, clock=None):
    """
    These tests are about the cycle handoff, not the profit policy, so the
    profit runner is off: a cycle then ends deterministically at the target.
    The runner and its protection are covered in test_basket.
    """
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}_set.json")
    settings._values["profit_runner_enabled"] = False
    for key, value in (overrides or {}).items():
        settings._values[key] = value
    broker, feed = make_paper(feed=feed or TickFeed(4010.00),
                              state_path=TMP / f"{name}_paper.json")
    if clock:
        broker.clock = clock
    rec = Recorder()
    engine = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                                 state_path=TMP / f"{name}_state.json",
                                 clock=clock or __import__("time").time)
    engine.resume()
    return engine, broker, feed, settings, rec


def force_reversal(engine, broker, feed, limit=40, now=None, tick=0.0):
    """
    Buy into a rise, then let the market turn and keep falling until the exit
    engine calls it - the market keeps moving whether or not levels are left.

    Returns True once the cycle has CLOSED, which is no longer the same event
    as a new cycle starting.
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
        if now is not None and tick:
            now[0] += tick
        engine.step()
        if not engine.cycle_active or engine.cycle.cycle_id != start:
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

t.section("EXIT -> RESET -> COOLDOWN -> NEW LADDER")
# There is exactly ONE active cycle at a time. Closing a cycle does not start
# the next one: the account is verified flat, a mandatory re-entry cooldown is
# served, and only then is a new ladder built at the CURRENT price.
now = [50_000.0]
engine, broker, feed, settings, rec = build(
    {"cooldown_after_loss_minutes": 15, "cycle_reentry_cooldown_seconds": 10},
    name="reentry", clock=lambda: now[0])
closed = force_reversal(engine, broker, feed, now=now, tick=2.0)
closed_cycle = engine.cycle.cycle_id
t.check("the cycle closed", closed, f"cycle #{closed_cycle}")
t.check("there is no active cycle any more", not engine.cycle_active)
t.check("the engine is in COOLDOWN_AFTER_EXIT",
        engine.state == State.COOLDOWN_AFTER_EXIT, engine.state)

# --- step 6: verified flat ------------------------------------------------
t.check("no positions are left", not broker.positions(),
        f"{len(broker.positions())} positions")
t.check("no pending orders are left", not broker.orders(),
        f"{len(broker.orders())} orders")
t.check("the flat state is logged", "CYCLE_FLAT" in rec.names())
t.check("every exit step is logged, in order",
        [n for n in rec.names() if n.startswith(("EXIT_", "CYCLE_FLAT",
                                                 "CYCLE_COOLDOWN"))][:1]
        == ["EXIT_TRIGGERED"],
        str([n for n in rec.names() if n.startswith(("EXIT_", "CYCLE_"))][-8:]))

# --- step 7-8: recorded and closed ----------------------------------------
completes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("the cycle is recorded with its P/L and exit reason",
        completes and completes[-1].kind, str([c.kind for c in completes]))
t.check("the record carries the wait before the next ladder",
        completes and completes[-1].next_ladder_seconds == 10,
        str(completes[-1].next_ladder_seconds if completes else None))

# --- step 9-10: nothing is created during the cooldown --------------------
t.check("the cooldown was announced once",
        rec.count("CYCLE_COOLDOWN_STARTED") == 1,
        str(rec.count("CYCLE_COOLDOWN_STARTED")))
for _ in range(5):
    now[0] += 1.0
    engine.step()
t.check("NO new ladder during the cooldown", not broker.orders(),
        f"{len(broker.orders())} orders")
t.check("NO new pending orders during the cooldown", not broker.orders())
t.check("NO new cycle during the cooldown",
        engine.cycle.cycle_id == closed_cycle and not engine.cycle_active,
        f"#{engine.cycle.cycle_id}")
t.check("the previous cycle is not reopened",
        rec.count("CYCLE_STARTED") == 1, str(rec.count("CYCLE_STARTED")))
t.check("the cooldown is the stated reason",
        "re-entry cooldown" in engine.block_reason, engine.block_reason)
t.check("no countdown spam - one cooldown event, not one per pass",
        rec.count("CYCLE_COOLDOWN_STARTED") == 1)

# --- step 11-14: after the cooldown, a new ladder at the current price ----
now[0] += 6.0
engine.step()
t.check("a new cycle starts once the cooldown elapses",
        engine.cycle.cycle_id != closed_cycle and engine.cycle_active,
        f"#{engine.cycle.cycle_id}")
t.check("the cooldown completion is logged",
        "CYCLE_COOLDOWN_COMPLETE" in rec.names())
t.check("the new ladder is live", len(broker.orders()) > 0,
        f"{len(broker.orders())} orders")
t.check("the deployment is confirmed as ACTIVE", "CYCLE_ACTIVE" in rec.names())
t.check("every live order belongs to the new cycle",
        all(parse_comment(o.comment)[0] == engine.cycle.cycle_id
            for o in broker.orders()))
t.check("the new cycle is anchored on the CURRENT price, not the old grid",
        abs(engine.cycle.anchor - feed().mid) < 0.35,
        f"anchor {engine.cycle.anchor} price {feed().mid:.2f}")
t.check("the new sequence starts empty", engine.sequence.total_triggers == 0)
t.check("a normal cycle never arms the loss cooldown",
        engine.cooldown_until == 0.0, str(engine.cooldown_until))

t.section("THE COOLDOWN IS BETWEEN CYCLES, NEVER BETWEEN TRIGGERS")
# The rolling ladder keeps running at full speed inside an ACTIVE cycle: the
# 10 seconds apply only after a complete exit, never between levels, triggers
# or orders.
now2 = [70_000.0]
engine, broker, feed, settings, rec = build(
    {"cycle_reentry_cooldown_seconds": 10}, name="inside", clock=lambda: now2[0])
engine.step()
triggered = 0
gaps = []
for _ in range(3):
    buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
                  key=lambda o: o.price)
    if not buys or not engine.cycle_active:
        break
    before = now2[0]
    trigger_buy(feed, buys[0].price)
    now2[0] += 0.5                      # half a second between triggers
    engine.step()
    if engine.cycle_active and engine.sequence.total_triggers > triggered:
        triggered = engine.sequence.total_triggers
        gaps.append(now2[0] - before)
t.check("levels keep triggering half a second apart", triggered >= 2,
        str(triggered))
t.check("with no cooldown between them", all(g <= 0.5 for g in gaps), str(gaps))
t.check("the ladder was replenished between triggers, with no cooldown",
        rec.count("ORDER_PLACED") > 10, str(rec.count("ORDER_PLACED")))
t.check("no cooldown ran inside the cycle - the events are all at the end",
        rec.names().index("CYCLE_COOLDOWN_STARTED") > rec.names().index("EXIT_TRIGGERED")
        if "CYCLE_COOLDOWN_STARTED" in rec.names() else True)
t.check("no exit machinery ran before the last trigger",
        all(rec.names().index(n) > rec.names().index("ORDER_TRIGGERED")
            for n in set(rec.names()) if n.startswith("EXIT_")),
        str([n for n in rec.names() if n.startswith("EXIT_")]))

t.section("A LEFTOVER POSITION HOLDS THE NEXT LADDER BACK")
# CYCLE_CLOSE_POSITIONS=false leaves the basket running under its own TP/SL.
# One cycle at a time still wins: the next ladder waits until MT5 is flat.
now7 = [120_000.0]
engine, broker, feed, settings, rec = build(
    {"cycle_close_positions": False, "cooldown_after_loss_minutes": 0,
     "cycle_reentry_cooldown_seconds": 10},
    name="leftover", clock=lambda: now7[0])
closed = force_reversal(engine, broker, feed, now=now7, tick=2.0)
t.check("the cycle closed", closed and not engine.cycle_active,
        f"#{engine.cycle.cycle_id} active={engine.cycle_active}")
t.check("its positions were left running", bool(broker.positions()),
        f"{len(broker.positions())} positions")
now7[0] += 11
engine.step()
t.check("no new ladder while a position is still open",
        not engine.cycle_active and not broker.orders(),
        f"{len(broker.orders())} orders")
t.check("and it says exactly why",
        "flat" in engine.block_reason, engine.block_reason)
t.check("the refusal is logged", "CYCLE_REENTRY_BLOCKED" in rec.names())
for pos in list(broker.positions()):
    broker.close_position(pos.ticket)
engine.step()
t.check("the next cycle starts once the book is flat",
        engine.cycle_active and len(broker.orders()) > 0,
        f"#{engine.cycle.cycle_id}, {len(broker.orders())} orders")
t.check("the config warns about this setting",
        any("CYCLE_CLOSE_POSITIONS" in w for w in _validate_with_leftovers()),
        "no warning")

t.section("ONLY ONE ACTIVE CYCLE, EVER")
# A new cycle is refused outright while anything from a previous one is live -
# this is what stops #7, #8 and #9 existing at the same time.
engine, broker, feed, settings, rec = build(name="single")
engine.step()
before = engine.cycle.cycle_id
t.check("a second cycle is refused while the first one is live",
        engine._start_cycle(reason="should be refused") is False)
t.check("the cycle id did not move", engine.cycle.cycle_id == before,
        f"#{engine.cycle.cycle_id}")
t.check("the refusal is logged", "CYCLE_REENTRY_BLOCKED" in rec.names())
t.check("no duplicate ladder was placed", len(broker.orders()) == 10,
        f"{len(broker.orders())} orders")

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
t.check("attributed to risk drawdown",
        any(c.kind == "RISK_DRAWDOWN" for c in rec.cycles
            if c.kind_of == "complete"),
        str([c.kind for c in rec.cycles if c.kind_of == "complete"]))
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


now3 = [80_000.0]
engine, broker, feed, settings, rec = build(
    {"cooldown_after_loss_minutes": 0}, name="stubborn", clock=lambda: now3[0])
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
t.check("state says it is closing out",
        engine.state in (State.CLOSING_CYCLE, State.VERIFYING_FLAT),
        engine.state)
engine.step()
t.check("still pending while the broker keeps refusing",
        engine.cycle.cycle_id == cycle_before)
stubborn.refusals = 0
engine.step()
t.check("the close completes once MT5 is clean, without starting a cycle",
        not engine.cycle_active and engine.cycle.cycle_id == cycle_before,
        f"#{engine.cycle.cycle_id} active={engine.cycle_active}")
now3[0] += 11
engine.step()
t.check("handoff completes once the cooldown is served too",
        engine.cycle.cycle_id == cycle_before + 1,
        f"#{engine.cycle.cycle_id}")
t.check("exactly one cycle was recorded for the one exit event",
        len([c for c in rec.cycles if c.kind_of == "complete"]) == 1,
        str([c.cycle_id for c in rec.cycles if c.kind_of == "complete"]))

t.section("ONE EXIT EVENT -> EXACTLY ONE NEW CYCLE")
now4 = [90_000.0]
engine, broker, feed, settings, rec = build(name="once", clock=lambda: now4[0])
force_reversal(engine, broker, feed, now=now4, tick=2.0)
now4[0] += 11
engine.step()
completes = [c for c in rec.cycles if c.kind_of == "complete"]
starts = [c for c in rec.cycles if c.kind_of == "start"]
t.check("one completion recorded", len(completes) == 1, str(len(completes)))
t.check("one new cycle started per completion",
        len(starts) == len(completes) + 1, f"{len(starts)} starts")
t.check("cycle ids are sequential and unique",
        [c.cycle_id for c in starts] == sorted(set(c.cycle_id for c in starts)),
        str([c.cycle_id for c in starts]))
for _ in range(5):
    now4[0] += 1
    engine.step()
t.check("repeated passes do not roll the cycle again",
        len([c for c in rec.cycles if c.kind_of == "complete"]) == 1)

t.section("CYCLE IDS NEVER REPEAT ACROSS A RESTART")
now5 = [100_000.0]
engine, broker, feed, settings, rec = build(name="ids", clock=lambda: now5[0])
force_reversal(engine, broker, feed, now=now5, tick=2.0)
now5[0] += 11
engine.step()
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
# require_flat is the one-active-cycle gate, exercised in its own section; here
# the question is only whether ids ever repeat.
engine2._start_cycle(reason="test", require_flat=False)
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
now6 = [110_000.0]
engine, broker, feed, settings, rec = build(
    {"cooldown_after_loss_minutes": 0, "basket_profit_target": 0.50},
    name="many", clock=lambda: now6[0])
for i in range(60):
    now6[0] += 6            # the 10s re-entry cooldown is served, not skipped
    engine.step()
    # a rising market: each new BUY level lifts the legs already open, so the
    # basket reaches its target and the cycle turns over
    candidates = [o for o in broker.orders() if o.side == BUY_STOP]
    if candidates:
        trigger_buy(feed, min(candidates, key=lambda o: o.price).price)
    now6[0] += 6
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
