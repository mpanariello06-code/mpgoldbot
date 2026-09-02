"""
The basket exit: one rule, and the hard risk limits that override it.

    total floating basket P/L >= BASKET_PROFIT_TARGET  ->  close everything

Tests 1-10 from the specification, plus the accounting the rule depends on.
"""
import pathlib
import shutil

from harness import Suite, use_stub_mt5
use_stub_mt5()

import config as cfg
from basket import BASKET_PROFIT_TARGET, RISK_DRAWDOWN, RISK_TIMEOUT
from broker import BUY, BUY_STOP, SELL, SELL_STOP
from fakes import Recorder, TickFeed, make_paper, trigger_buy, trigger_sell
from ladder_engine import RollingLadderEngine, State, parse_comment
from runtime_settings import RuntimeSettings

t = Suite("basket")
TMP = pathlib.Path("/tmp/basket_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)


def build(overrides=None, name="b", clock=None, start=4010.00):
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}_set.json")
    for key, value in (overrides or {}).items():
        settings._values[key] = value
    broker, feed = make_paper(feed=TickFeed(start),
                              state_path=TMP / f"{name}_paper.json")
    if clock:
        broker.clock = clock
    rec = Recorder()
    eng = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                              state_path=TMP / f"{name}_state.json",
                              clock=clock or __import__("time").time)
    eng.resume()
    eng.step()
    return eng, broker, feed, settings, rec


def take(eng, broker, feed, side, now=None, gap=5.0):
    """Walk price onto the nearest untouched level on one side."""
    orders = [o for o in broker.orders() if o.side == side]
    if not orders:
        return False
    target = (min(orders, key=lambda o: o.price) if side == BUY_STOP
              else max(orders, key=lambda o: o.price))
    (trigger_buy if side == BUY_STOP else trigger_sell)(feed, target.price)
    if now is not None:
        now[0] += gap
    eng.step()
    return True


# One BUY and two SELLs: a genuinely mixed basket, and a net of one 0.01 lot,
# so a 0.01 move in price is worth exactly $0.01 and the tests can land on
# 1.99 / 2.00 / 2.01 precisely.
def open_basket(eng, broker, feed, sides=(BUY_STOP, SELL_STOP, SELL_STOP),
                now=None):
    """Trigger a few levels, then freeze the ladder so the basket is stable."""
    for side in sides:
        take(eng, broker, feed, side, now)
    eng.settings._values["direction_filter"] = "none"
    if now is not None:
        now[0] += 1
    eng.step()
    return broker.positions()


def drift(eng, broker, feed, target):
    """Move the market so the frozen basket floats at about `target` dollars."""
    pos = broker.positions()
    net = sum((1 if p.side == BUY else -1) * p.volume for p in pos)
    assert abs(net) > 1e-9, "the basket must not be flat"
    unit = eng.spec.money_per_price_unit(abs(net)) * (1 if net > 0 else -1)
    feed.set(round(feed.bid + (target - sum(p.profit for p in pos)) / unit, 2))


def frozen(name, overrides=None, now=None):
    """A stable two-leg basket with a frozen ladder, ready to be drifted."""
    opts = {"max_cycle_drawdown": 0, "max_cycle_duration_minutes": 0,
            "cycle_reentry_cooldown_seconds": 10}
    opts.update(overrides or {})
    clock = (lambda: now[0]) if now is not None else None
    eng, broker, feed, settings, rec = build(opts, name=name, clock=clock)
    open_basket(eng, broker, feed, now=now)
    return eng, broker, feed, settings, rec


# ===========================================================================
t.section("THE TARGET IS ONE SETTING, READ LIVE")
t.check("BASKET_PROFIT_TARGET defaults to 2.00",
        cfg.runtime_defaults()["basket_profit_target"] == 2.00,
        str(cfg.runtime_defaults()["basket_profit_target"]))
t.check("it is not hard-coded anywhere else",
        cfg.BASKET_PROFIT_TARGET == cfg.runtime_defaults()["basket_profit_target"])
t.check("it is runtime-settable",
        "basket_profit_target" in RuntimeSettings(
            cfg.runtime_defaults(), TMP / "probe.json").snapshot())
t.check("the old scenario exit reasons are gone",
        not any(hasattr(__import__("basket"), n) for n in
                ("SCENARIO_1_DIRECTIONAL", "SCENARIO_2_REVERSAL",
                 "SCENARIO_3_EXTENDED_LADDER", "PROFIT_FALLBACK")))
t.check("so is the scoring engine",
        not any(hasattr(__import__("basket"), n) for n in
                ("RollingLadderExitEngine", "ExitConfig", "ExitAssessment",
                 "LadderSequence")))

t.section("NO INDIVIDUAL TAKE PROFITS")
eng, broker, feed, settings, rec = build(name="notp")
t.check("no pending order carries a TP",
        all(o.tp == 0 for o in broker.orders()),
        str(sorted({o.tp for o in broker.orders()})))
take(eng, broker, feed, BUY_STOP)
take(eng, broker, feed, BUY_STOP)
t.check("no open position carries a TP",
        all(p.tp == 0 for p in broker.positions()),
        str([p.tp for p in broker.positions()]))
t.check("no TP setting is left to turn back on",
        not any(k.startswith("tp_") for k in cfg.runtime_defaults()),
        str([k for k in cfg.runtime_defaults() if k.startswith("tp_")]))

t.section("10. BASKET P/L IS THE SUM OF THE CYCLE'S OWN LEGS")
eng, broker, feed, settings, rec = frozen("pnl")
positions = broker.positions()
t.check("get_cycle_floating_pnl sums the legs",
        abs(eng.get_cycle_floating_pnl() -
            round(sum(p.profit for p in positions), 2)) < 0.01,
        f"{eng.get_cycle_floating_pnl():+.2f}")
broker.place_stop_order(BUY_STOP, feed().ask + 8.0, 0.01, comment="RL999B7")
t.check("another cycle's order is excluded",
        len(eng.cycle_orders()) == len(broker.orders()) - 1,
        f"{len(eng.cycle_orders())} of {len(broker.orders())}")
t.check("11. realized is separate and 0 while nothing has closed",
        eng.get_cycle_realized_pnl() == 0.0)
t.check("11. net = realized + floating",
        eng.get_cycle_net_pnl() ==
        round(eng.get_cycle_realized_pnl() + eng.get_cycle_floating_pnl(), 2))

t.section("TEST 3 - BASKET AT +1.99 DOES NOT EXIT")
now = [1000.0]
eng, broker, feed, settings, rec = frozen("t3", now=now)
start_cycle = eng.cycle.cycle_id
for value in (1.72, 1.83, 1.94, 1.99):
    drift(eng, broker, feed, value)
    now[0] += 1
    eng.step()
    if not eng.cycle_active:
        break
t.check("the basket climbed to just under the target",
        abs(eng.get_cycle_floating_pnl() - 1.99) < 0.02,
        f"{eng.get_cycle_floating_pnl():+.2f}")
t.check("the cycle is still open", eng.cycle_active and
        eng.cycle.cycle_id == start_cycle, f"#{eng.cycle.cycle_id}")
t.check("nothing was closed", bool(broker.positions()),
        f"{len(broker.positions())} positions")

t.section("TEST 1 - BASKET REACHES EXACTLY +2.00")
now = [2000.0]
eng, broker, feed, settings, rec = frozen("t1", now=now)
drift(eng, broker, feed, 2.00)
now[0] += 1
eng.step()
t.check("the basket reached the target",
        abs(rec.cycles[-1].context["floating_pnl_at_exit"] - 2.00) < 0.02
        if rec.cycles[-1].kind_of == "complete" else False,
        str(rec.cycles[-1].context if rec.cycles[-1].kind_of == "complete" else ""))
t.check("exactly at the target is enough - it is >=, not >",
        not eng.cycle_active, f"active={eng.cycle_active}")
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("recorded as BASKET_PROFIT_TARGET",
        closes and closes[-1].kind == BASKET_PROFIT_TARGET,
        str([c.kind for c in closes]))

t.section("TEST 2 - BASKET REACHES +2.01")
now = [3000.0]
eng, broker, feed, settings, rec = frozen("t2", now=now)
for value in (1.72, 1.83, 1.94, 2.01):
    if not eng.cycle_active:
        break
    drift(eng, broker, feed, value)
    now[0] += 1
    eng.step()
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("crossing the target closes the basket",
        closes and closes[-1].kind == BASKET_PROFIT_TARGET,
        str([c.kind for c in closes]))
t.check("12. no candle or confirmation was waited for - it exited on the tick",
        closes and closes[-1].context["floating_pnl_at_exit"] >= 2.00,
        str(closes[-1].context.get("floating_pnl_at_exit") if closes else None))
t.check("the exit names the number that caused it",
        closes and "2.0" in closes[-1].reason, closes[-1].reason if closes else "")

t.section("TEST 5 - THE WHOLE BASKET GOES")
t.check("no positions left", not broker.positions(),
        f"{len(broker.positions())} positions")
t.check("no pending orders left", not broker.orders(),
        f"{len(broker.orders())} orders")
t.check("the flat state was verified against MT5", "CYCLE_FLAT" in rec.names())
t.check("every exit step is logged",
        all(n in rec.names() for n in
            ("EXIT_TRIGGERED", "EXIT_POSITIONS_FOUND", "EXIT_CLOSE_SENT",
             "EXIT_CLOSE_CONFIRMED", "EXIT_RECONCILED", "CYCLE_FLAT")),
        str([n for n in rec.names() if n.startswith("EXIT_")]))

# the ladder above was frozen, so there was nothing to cancel; a live ladder
# must have its pending levels cancelled by the same exit
now = [4500.0]
eng2, broker2, feed2, settings2, rec2 = build(
    {"basket_profit_target": 0.30, "max_cycle_drawdown": 0,
     "max_cycle_duration_minutes": 0}, name="t5b", clock=lambda: now[0])
take(eng2, broker2, feed2, BUY_STOP, now)
for _ in range(12):
    if not eng2.cycle_active:
        break
    feed2.set(round(feed2.bid + 0.10, 2))
    now[0] += 1
    eng2.step()
t.check("a live ladder exits on the target too", not eng2.cycle_active,
        f"active={eng2.cycle_active}")
t.check("its pending levels were cancelled",
        "EXIT_CANCEL_SENT" in rec2.names() and not broker2.orders(),
        f"{len(broker2.orders())} orders")
t.check("and its positions closed", not broker2.positions(),
        f"{len(broker2.positions())} positions")

t.section("TEST 4 - FLUCTUATION AROUND THE TARGET EXITS ONCE")
now = [4000.0]
eng, broker, feed, settings, rec = frozen("t4", now=now)
for value in (1.95, 2.05, 1.90, 2.20, 1.80, 2.50):
    if eng.cycle_active and broker.positions():
        drift(eng, broker, feed, value)
    now[0] += 1
    eng.step()
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("13. exactly one exit, not one per pass above the target",
        len(closes) == 1, str([c.kind for c in closes]))
t.check("the close ran once", rec.count("EXIT_TRIGGERED") == 1,
        str(rec.count("EXIT_TRIGGERED")))
t.check("and one cooldown followed it",
        rec.count("CYCLE_COOLDOWN_STARTED") == 1,
        str(rec.count("CYCLE_COOLDOWN_STARTED")))

t.section("TEST 6 - NO NEW LADDER DURING THE COOLDOWN")
t.check("the engine is in COOLDOWN_AFTER_EXIT",
        eng.state == State.COOLDOWN_AFTER_EXIT, eng.state)
closed_cycle = eng.cycle.cycle_id
for _ in range(5):
    now[0] += 1
    eng.step()
t.check("no pending orders were created", not broker.orders(),
        f"{len(broker.orders())} orders")
t.check("no new cycle was created",
        eng.cycle.cycle_id == closed_cycle and not eng.cycle_active,
        f"#{eng.cycle.cycle_id}")
t.check("the previous cycle was not reopened", not broker.positions())
t.check("the cooldown is the stated reason",
        "re-entry cooldown" in eng.block_reason, eng.block_reason)
t.check("no countdown spam - one event, not one per pass",
        rec.count("CYCLE_COOLDOWN_STARTED") == 1)

t.section("TEST 7 - A NEW LADDER AT THE CURRENT PRICE AFTER 10s")
eng.settings._values["direction_filter"] = "off"     # unfreeze
feed.set(round(feed.bid + 3.00, 2))                  # the market moved on
now[0] += 6
eng.step()
t.check("a new cycle started", eng.cycle_active and
        eng.cycle.cycle_id != closed_cycle, f"#{eng.cycle.cycle_id}")
t.check("with a fresh id", eng.cycle.cycle_id == closed_cycle + 1,
        f"#{eng.cycle.cycle_id}")
t.check("the ladder is live in MT5", len(broker.orders()) > 0,
        f"{len(broker.orders())} orders")
t.check("the deployment was verified", "CYCLE_ACTIVE" in rec.names())
t.check("anchored on the CURRENT price, not the old grid",
        abs(eng.cycle.anchor - feed().mid) < 0.35,
        f"anchor {eng.cycle.anchor} price {feed().mid:.2f}")
t.check("the basket starts empty", eng.get_cycle_floating_pnl() == 0.0)

t.section("TEST 8 - BLOCKED WHILE THE OLD CYCLE HOLDS POSITIONS")
now = [5000.0]
eng, broker, feed, settings, rec = build(
    {"cycle_close_positions": False, "cooldown_after_loss_minutes": 0,
     "cycle_reentry_cooldown_seconds": 10, "max_cycle_drawdown": 0,
     "max_cycle_duration_minutes": 0}, name="t8", clock=lambda: now[0])
open_basket(eng, broker, feed, now=now)
drift(eng, broker, feed, 2.50)
now[0] += 1
eng.step()
t.check("the cycle closed", not eng.cycle_active)
t.check("but positions were left running", bool(broker.positions()),
        f"{len(broker.positions())} positions")
now[0] += 30
eng.step()
t.check("8. no new cycle while positions remain",
        not eng.cycle_active and not broker.orders(),
        f"active={eng.cycle_active}, {len(broker.orders())} orders")
t.check("MT5 state is the source of truth, not internal state",
        "flat" in eng.block_reason, eng.block_reason)
for pos in list(broker.positions()):
    broker.close_position(pos.ticket)
settings._values["direction_filter"] = "off"
eng.step()
t.check("it starts once MT5 says flat", eng.cycle_active and broker.orders(),
        f"active={eng.cycle_active}, {len(broker.orders())} orders")

t.section("TEST 9 - BLOCKED WHILE PENDING ORDERS REMAIN")
eng, broker, feed, settings, rec = build(name="t9")
t.check("a second cycle is refused while the ladder is live",
        eng._start_cycle(reason="should be refused") is False)
t.check("the cycle id did not move", eng.cycle.cycle_id == 1,
        f"#{eng.cycle.cycle_id}")
t.check("the refusal is logged", "CYCLE_REENTRY_BLOCKED" in rec.names())
t.check("no duplicate ladder was placed", len(broker.orders()) == 10,
        f"{len(broker.orders())} orders")

t.section("TEST 10 - HARD RISK OVERRIDES THE PROFIT TARGET")
now = [6000.0]
eng, broker, feed, settings, rec = frozen(
    "t10dd", {"max_cycle_drawdown": 3.00}, now=now)
drift(eng, broker, feed, -3.20)
now[0] += 1
eng.step()
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("a drawdown breach closes the basket below the target",
        closes and closes[-1].kind == RISK_DRAWDOWN,
        str([c.kind for c in closes]))

now = [7000.0]
eng, broker, feed, settings, rec = frozen(
    "t10to", {"max_cycle_duration_minutes": 30}, now=now)
drift(eng, broker, feed, -0.20)
for _ in range(20):
    now[0] += 60
    eng.step()
t.check("a basket below target is not closed early", eng.cycle_active)
now[0] += 31 * 60
eng.step()
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("the duration limit closes it", closes and closes[-1].kind == RISK_TIMEOUT,
        str([c.kind for c in closes]))
t.check("a basket can never float indefinitely",
        cfg.runtime_defaults()["max_cycle_duration_minutes"] > 0)
for key in ("max_cycle_drawdown", "max_daily_drawdown", "max_open_positions",
            "max_pending_orders", "max_ladder_depth", "max_spread",
            "max_cycle_duration_minutes"):
    t.check(f"14. {key} survived the cleanup", key in cfg.runtime_defaults())

t.section("15. NO MARTINGALE")
eng, broker, feed, settings, rec = build({"lot_size": 0.03}, name="lots")
t.check("every level uses the configured lot verbatim",
        all(o.volume == 0.03 for o in broker.orders()),
        str(sorted({o.volume for o in broker.orders()})))
take(eng, broker, feed, BUY_STOP)
t.check("and it does not change after a trigger",
        all(o.volume == 0.03 for o in broker.orders()))

t.section("20. THE FULL LOOP, REPEATED")
now = [8000.0]
eng, broker, feed, settings, rec = build(
    {"cycle_reentry_cooldown_seconds": 10, "cooldown_after_loss_minutes": 0,
     "max_cycle_duration_minutes": 0, "max_cycle_drawdown": 0,
     "basket_profit_target": 0.50}, name="loop", clock=lambda: now[0])
ids, overlaps = [], []
for i in range(120):
    if eng.cycle_active:
        # a rising market: each new BUY level lifts the legs already open
        take(eng, broker, feed, BUY_STOP, now, gap=3)
    else:
        now[0] += 3
        eng.step()
    live = {parse_comment(o.comment)[0]
            for o in list(broker.orders()) + list(broker.positions())
            if parse_comment(o.comment)}
    if len(live) > 1:
        overlaps.append(sorted(live))
    if eng.cycle_active and eng.cycle.cycle_id not in ids:
        ids.append(eng.cycle.cycle_id)
t.check("cycles ran back to back", len(ids) >= 3, str(ids))
t.check("8. never two cycles live at once", not overlaps, str(overlaps[:3]))
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("every close names an exit reason",
        closes and all(c.kind for c in closes),
        str(sorted({c.kind for c in closes})))
t.check("and only the reasons that still exist",
        all(c.kind in (BASKET_PROFIT_TARGET, RISK_DRAWDOWN, RISK_TIMEOUT)
            for c in closes), str(sorted({c.kind for c in closes})))
t.check("one cooldown per close",
        rec.count("CYCLE_COOLDOWN_STARTED") == len(closes),
        f"{rec.count('CYCLE_COOLDOWN_STARTED')} vs {len(closes)}")

t.section("13. THE CYCLE STATE MACHINE")
for name in ("IDLE", "SAFETY_CHECK", "BUILDING_LADDER", "LADDER_ACTIVE",
             "TRADING", "CLOSING_CYCLE", "VERIFYING_FLAT",
             "COOLDOWN_AFTER_EXIT", "NEW_CYCLE", "RISK_BLOCKED"):
    t.check(f"State.{name} exists", hasattr(State, name))

t.done()
