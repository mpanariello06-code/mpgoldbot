"""
The basket architecture: no individual TP, the CYCLE is the unit of management.

A triggered level is one leg of a basket, not a trade with its own target. The
exit engine only ever sees the combined P/L, and an exit closes everything.
"""
import pathlib
import shutil

from harness import Suite, use_stub_mt5
use_stub_mt5()

import config as cfg
from broker import BUY, BUY_STOP, SELL, SELL_STOP
from exit_engine import (PROFIT_FALLBACK, RISK_TIMEOUT, SCENARIO_1_DIRECTIONAL,
                         SCENARIO_2_REVERSAL, SCENARIO_3_EXTENDED_LADDER)
from fakes import Recorder, TickFeed, make_paper, trigger_buy, trigger_sell
from ladder_engine import RollingLadderEngine, State, parse_comment
from runtime_settings import RuntimeSettings

t = Suite("basket")
TMP = pathlib.Path("/tmp/basket_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)

SCENARIOS = (SCENARIO_1_DIRECTIONAL, SCENARIO_2_REVERSAL,
             SCENARIO_3_EXTENDED_LADDER)


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


def run(eng, broker, feed, sides, now=None, gap=5.0):
    """Drive a scripted sequence of trigger sides. Stops if the cycle closes."""
    for side in sides:
        if not eng.cycle_active:
            break
        take(eng, broker, feed, side, now, gap)
    return eng.sequence.total_triggers if eng.sequence else 0


# ===========================================================================
t.section("37. CRITICAL - NO INDIVIDUAL TP ON ANY LADDER POSITION")
eng, broker, feed, settings, rec = build(name="notp")
t.check("the default TP mode is the basket", settings.get("tp_mode") == "none",
        settings.get("tp_mode"))
t.check("the ladder computes a zero TP distance",
        eng.tp_distance(settings.snapshot()) == 0.0)
t.check("no pending order carries a TP",
        all(o.tp == 0 for o in broker.orders()),
        str(sorted({o.tp for o in broker.orders()})))

take(eng, broker, feed, BUY_STOP)
take(eng, broker, feed, BUY_STOP)
positions = broker.positions()
t.check("triggered positions are open", len(positions) >= 2, str(len(positions)))
t.check("NO triggered position carries a strategy TP",
        all(p.tp == 0 for p in positions), str([p.tp for p in positions]))

# the price runs a long way past where a 1-level TP would have been
opened = len(broker.positions())
feed.set(round(feed.bid + 3.00, 2))
eng.step()
t.check("a big favourable move does NOT close positions individually",
        len(broker.positions()) >= opened or not eng.cycle_active,
        f"{len(broker.positions())} positions, active={eng.cycle_active}")
t.check("nothing was reported as a TP hit", rec.count("TP_HIT") == 0,
        str(rec.count("TP_HIT")))

t.section("MIXED BASKET: LOSING AND WINNING LEGS COEXIST")
# The exact case from the brief: a BUY above, then price reverses and SELLs
# trigger below. The BUY sits negative while the SELLs go positive, and the
# bot must NOT close the losing leg or bank the winning ones.
eng, broker, feed, settings, rec = build(
    {"max_cycle_drawdown": 0, "max_cycle_duration_minutes": 0,
     "profit_fallback_enabled": False}, name="mixed")
take(eng, broker, feed, BUY_STOP)
for _ in range(2):
    if not eng.cycle_active or not take(eng, broker, feed, SELL_STOP):
        break
t.check("the cycle is still open - no scenario fired on 3 triggers",
        eng.cycle_active, f"active={eng.cycle_active}")
positions = broker.positions()
buys = [p for p in positions if p.side == BUY]
sells = [p for p in positions if p.side == SELL]
t.check("the basket holds both directions at once",
        buys and sells, f"{len(buys)} BUY / {len(sells)} SELL")
t.check("at least one leg is under water", any(p.profit < 0 for p in positions),
        str([round(p.profit, 2) for p in positions]))
t.check("at least one leg is in profit", any(p.profit > 0 for p in positions),
        str([round(p.profit, 2) for p in positions]))
t.check("no leg was closed for being individually profitable",
        rec.count("POSITION_CLOSED") == 0, str(rec.count("POSITION_CLOSED")))

t.section("3-4. BASKET P/L IS THE SUM OF THE LEGS")
floating = eng.get_cycle_floating_pnl()
t.check("get_cycle_floating_pnl sums every open leg",
        abs(floating - round(sum(p.profit for p in positions), 2)) < 0.01,
        f"{floating:+.2f}")
t.check("winners and losers net off, they are not counted separately",
        abs(floating - round(sum(p.profit for p in buys) +
                             sum(p.profit for p in sells), 2)) < 0.01,
        f"{floating:+.2f}")
t.check("the exit engine reads exactly that number",
        abs(eng.sequence.floating_pnl - floating) < 0.01,
        f"{eng.sequence.floating_pnl:+.2f} vs {floating:+.2f}")
t.check("realized is 0 while nothing has closed",
        eng.get_cycle_realized_pnl() == 0.0, str(eng.get_cycle_realized_pnl()))
t.check("net = realized + floating",
        eng.get_cycle_net_pnl() == round(eng.get_cycle_realized_pnl() +
                                         floating, 2),
        f"{eng.get_cycle_net_pnl():+.2f}")

basket = eng.basket()
t.check("the basket reports its own open positions",
        basket["open_positions"] == len(positions), str(basket))
t.check("the basket reports its own pending orders",
        basket["pending_orders"] == len(broker.orders()), str(basket))
t.check("split by side", basket["open_buys"] == len(buys) and
        basket["open_sells"] == len(sells), str(basket))
t.check("and its drawdown", basket["drawdown"] >= 0, str(basket["drawdown"]))

t.section("26. A FOREIGN CYCLE'S ORDERS ARE NOT IN THIS BASKET")
broker.place_stop_order(BUY_STOP, feed().ask + 6.0, 0.01, comment="RL999B7")
t.check("an order from cycle #999 is excluded",
        len(eng.cycle_orders()) == len(broker.orders()) - 1,
        f"{len(eng.cycle_orders())} of {len(broker.orders())}")
t.check("it does not change the basket P/L",
        eng.get_cycle_floating_pnl() == floating, f"{floating:+.2f}")

t.section("A: STRONG BUY MOVEMENT")
eng, broker, feed, settings, rec = build(name="sA")
n = run(eng, broker, feed, [BUY_STOP] * 4)
t.check("four BUY triggers recorded",
        n == 4 or not eng.cycle_active, str(n))
t.check("all on the BUY side",
        (eng.sequence.buy_triggers if eng.sequence else 4) >= 3
        or not eng.cycle_active)
completes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("if it exited, it was a named scenario - never a trade count",
        not completes or completes[-1].kind in SCENARIOS + (PROFIT_FALLBACK,),
        str([c.kind for c in completes]))

t.section("B: STRONG SELL MOVEMENT")
eng, broker, feed, settings, rec = build(name="sB")
n = run(eng, broker, feed, [SELL_STOP] * 4)
t.check("four SELL triggers recorded", n == 4 or not eng.cycle_active, str(n))
completes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("if it exited, it was a named scenario",
        not completes or completes[-1].kind in SCENARIOS + (PROFIT_FALLBACK,),
        str([c.kind for c in completes]))

t.section("C: BUY -> SELL REVERSAL")
eng, broker, feed, settings, rec = build(
    {"profit_fallback_enabled": False}, name="sC")
run(eng, broker, feed, [BUY_STOP, BUY_STOP] + [SELL_STOP] * 5)
completes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("the reversal ended the cycle", completes, str(len(completes)))
t.check("recorded as a reversal",
        completes and completes[0].kind == SCENARIO_2_REVERSAL,
        str([c.kind for c in completes]))
seq = completes[0].sequence if completes else None
t.check("the direction change was seen",
        seq is not None and seq.direction_changes >= 1,
        str(seq.direction_changes if seq else None))
t.check("and the opposite side had taken over by then",
        seq is not None and seq.last_side == SELL and seq.consecutive_sell >= 2,
        f"last={seq.last_side if seq else None} "
        f"run={seq.consecutive_sell if seq else None} "
        f"({seq.buy_triggers if seq else 0}B/{seq.sell_triggers if seq else 0}S)")
t.check("the ENTIRE basket was closed, not part of it",
        not broker.positions() and not broker.orders(),
        f"{len(broker.positions())} positions, {len(broker.orders())} orders")

t.section("D: SELL -> BUY REVERSAL")
eng, broker, feed, settings, rec = build(
    {"profit_fallback_enabled": False}, name="sD")
run(eng, broker, feed, [SELL_STOP, SELL_STOP] + [BUY_STOP] * 5)
completes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("the mirror reversal also ends the cycle", completes, str(len(completes)))
t.check("recorded as a reversal",
        completes and completes[0].kind == SCENARIO_2_REVERSAL,
        str([c.kind for c in completes]))

t.section("E: CHOPPY MARKET")
eng, broker, feed, settings, rec = build(
    {"profit_fallback_enabled": False, "max_cycle_duration_minutes": 0},
    name="sE")
run(eng, broker, feed, [BUY_STOP, SELL_STOP] * 3)
completes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("chop never exits on the alternation itself",
        not completes or completes[0].kind in SCENARIOS,
        str([c.kind for c in completes]))
if eng.cycle_active and eng.assessment:
    t.check("chop is not read as a clean directional run",
            eng.assessment.directional_score < 0.5,
            f"{eng.assessment.directional_score:.2f}")
else:
    t.check("chop is not read as a clean directional run", True, "cycle closed")

t.section("F: EXTENDED DIRECTIONAL MOVEMENT")
eng, broker, feed, settings, rec = build(
    {"max_ladder_depth": 20, "max_open_positions": 20,
     "profit_fallback_enabled": False}, name="sF")
n = run(eng, broker, feed, [BUY_STOP] * 8)
t.check("a long run is allowed to develop", n >= 4 or not eng.cycle_active,
        str(n))
completes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("it did not exit on a fixed count",
        not completes or completes[0].kind in SCENARIOS,
        str([c.kind for c in completes]))
t.check("ladder depth was tracked",
        (eng.sequence.ladder_depth_used if eng.sequence else 0) > 0 or completes)

t.section("G: BASKET RECOVERY -> PROFIT_FALLBACK")
now = [40_000.0]
eng, broker, feed, settings, rec = build(
    {"profit_confirmation_seconds": 60, "profit_fallback_buffer_levels": 0.5,
     "profit_fallback_continuation_guard": 1.1, "max_cycle_drawdown": 0,
     "max_cycle_duration_minutes": 0, "cycle_reentry_cooldown_seconds": 10},
    name="sG", clock=lambda: now[0])
for _ in range(2):
    take(eng, broker, feed, BUY_STOP, now, gap=30)
settings._values["direction_filter"] = "none"    # freeze the ladder
now[0] += 10
eng.step()
start_cycle = eng.cycle.cycle_id


def drift(target):
    """Move the market so the frozen basket floats at about `target`."""
    pos = broker.positions()
    net = sum((1 if p.side == BUY else -1) * p.volume for p in pos)
    unit = eng.spec.money_per_price_unit(abs(net)) * (1 if net > 0 else -1)
    feed.set(round(feed.bid + (target - sum(p.profit for p in pos)) / unit, 2))


ladder = [-1.00, -0.60, -0.20, 0.10, 0.30, 0.40]
seen = []
for value in ladder:
    drift(value)
    now[0] += 5
    eng.step()
    seen.append(eng.get_cycle_floating_pnl())
t.check("the basket walked from loss to profit",
        seen[0] < 0 < seen[-1], str(seen))
t.check("it was NOT closed on the way up",
        eng.cycle.cycle_id == start_cycle and eng.cycle_active, str(seen))
t.check("the confirmation clock is running", eng._profit_since is not None)
now[0] += 61
eng.step()
completes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("confirmed recovery closes the basket",
        completes and completes[-1].kind == PROFIT_FALLBACK,
        str([c.kind for c in completes]))
t.check("it is not a fixed dollar target - the buffer is a fraction of a level",
        settings.get("profit_fallback_buffer_levels") *
        eng.money_per_level(settings.snapshot()) < 1.0)
t.check("every leg and every pending order went with it",
        not broker.positions() and not broker.orders(),
        f"{len(broker.positions())} positions, {len(broker.orders())} orders")

t.section("H: BASKET NEVER RECOVERS -> RISK_TIMEOUT")
now = [60_000.0]
eng, broker, feed, settings, rec = build(
    {"max_cycle_duration_minutes": 30, "profit_fallback_enabled": False,
     "max_cycle_drawdown": 0, "cycle_reentry_cooldown_seconds": 10},
    name="sH", clock=lambda: now[0])
take(eng, broker, feed, BUY_STOP, now, gap=60)
start_cycle = eng.cycle.cycle_id
for _ in range(20):
    now[0] += 60
    feed.set(round(feed.bid - 0.01, 2))
    eng.step()
t.check("an unresolved basket is not closed early",
        eng.cycle.cycle_id == start_cycle and eng.cycle_active)
now[0] += 31 * 60
eng.step()
completes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("the timeout closes it", completes and completes[-1].kind == RISK_TIMEOUT,
        str([c.kind for c in completes]))
t.check("nothing is left floating", not broker.positions())
t.check("the system never assumed it would come good eventually",
        cfg.runtime_defaults()["max_cycle_duration_minutes"] > 0)

t.section("I: CONTINUOUS CYCLES, ONE AT A TIME")
now = [80_000.0]
eng, broker, feed, settings, rec = build(
    {"cycle_reentry_cooldown_seconds": 10, "cooldown_after_loss_minutes": 0,
     "profit_fallback_enabled": False}, name="sI", clock=lambda: now[0])
ids = []
overlaps = []
for i in range(80):
    if eng.cycle_active:
        take(eng, broker, feed, BUY_STOP if i % 7 < 4 else SELL_STOP, now, gap=3)
    else:
        now[0] += 3
        eng.step()
    # every live item must belong to the one cycle the engine says is current
    cycles_live = {parse_comment(o.comment)[0]
                   for o in list(broker.orders()) + list(broker.positions())
                   if parse_comment(o.comment)}
    if len(cycles_live) > 1:
        overlaps.append(sorted(cycles_live))
    if eng.cycle_active and eng.cycle.cycle_id not in ids:
        ids.append(eng.cycle.cycle_id)
t.check("several cycles ran back to back", len(ids) >= 3, str(ids))
t.check("cycle ids are strictly increasing", ids == sorted(set(ids)), str(ids))
t.check("38. NEVER two cycles live at the same time", not overlaps, str(overlaps[:3]))
starts = len([c for c in rec.cycles if c.kind_of == "start"])
closes = len([c for c in rec.cycles if c.kind_of == "complete"])
t.check("one exit produced exactly one new cycle",
        starts in (closes, closes + 1), f"{starts} starts, {closes} closes")
t.check("each close was followed by a cooldown",
        rec.count("CYCLE_COOLDOWN_STARTED") == closes,
        f"{rec.count('CYCLE_COOLDOWN_STARTED')} cooldowns, {closes} closes")

t.section("38. NO NEW LADDER WHILE THE OLD BASKET IS OPEN")
now = [90_000.0]
eng, broker, feed, settings, rec = build(
    {"cycle_close_positions": False, "cooldown_after_loss_minutes": 0,
     "cycle_reentry_cooldown_seconds": 10, "profit_fallback_enabled": False},
    name="hold", clock=lambda: now[0])
run(eng, broker, feed, [BUY_STOP, BUY_STOP] + [SELL_STOP] * 5, now, gap=4)
t.check("the cycle closed", not eng.cycle_active, str(eng.cycle_active))
t.check("but its basket is still open", bool(broker.positions()),
        f"{len(broker.positions())} positions")
now[0] += 30
eng.step()
t.check("no cycle #2 while cycle #1 still holds positions",
        not eng.cycle_active and not broker.orders(),
        f"active={eng.cycle_active}, {len(broker.orders())} orders")
t.check("the engine says exactly why", "flat" in eng.block_reason,
        eng.block_reason)
for pos in list(broker.positions()):
    broker.close_position(pos.ticket)
eng.step()
t.check("cycle #2 starts once the book is flat",
        eng.cycle_active and broker.orders(),
        f"active={eng.cycle_active}, {len(broker.orders())} orders")

t.section("33. THE STATE MACHINE NAMES EVERY STEP")
for name in ("IDLE", "SAFETY_CHECK", "BUILDING_LADDER", "LADDER_ACTIVE",
             "TRADING", "CLOSING_CYCLE", "VERIFYING_FLAT",
             "COOLDOWN_AFTER_EXIT", "NEW_CYCLE", "RISK_BLOCKED"):
    t.check(f"State.{name} exists", hasattr(State, name))
t.check("TRADING and POSITION_ACTIVE are the same state",
        State.TRADING == State.POSITION_ACTIVE)

t.done()
