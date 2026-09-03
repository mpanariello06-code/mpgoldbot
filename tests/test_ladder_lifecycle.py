"""
ONE ACTIVE LADDER = ONE LADDER ID.

    IDLE -> CREATE_LADDER -> PLACE 22 PENDING ORDERS -> ACTIVE_LADDER
         -> MANAGE_CURRENT_LADDER -> FINAL CLOSE CONDITION
         -> COOLDOWN (10s) -> IDLE -> CREATE_NEW_LADDER

Nothing that happens inside a live ladder - a trigger, an open, a close, a
direction change, orders running out - may create another one.
"""
import pathlib
import shutil

from harness import Suite, use_stub_mt5
use_stub_mt5()

import config as cfg
from broker import BUY, BUY_STOP, SELL, SELL_STOP
from fakes import Recorder, TickFeed, make_paper, trigger_buy, trigger_sell
from ladder_engine import RollingLadderEngine, State, parse_comment
from runtime_settings import RuntimeSettings

t = Suite("lifecycle")
TMP = pathlib.Path("/tmp/lifecycle_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)

DEPTH = cfg.LADDER_DEPTH          # 11 per side as shipped
LADDER = DEPTH * 2                # 22 orders


def build(overrides=None, name="l", clock=None, start=4010.00, deploy=True):
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
    if deploy:
        eng.step()
    return eng, broker, feed, settings, rec


def ids_live(broker):
    """Every distinct ladder id present at the broker right now."""
    return sorted({parse_comment(o.comment)[0]
                   for o in list(broker.orders()) + list(broker.positions())
                   if parse_comment(o.comment)})


def take(eng, broker, feed, side, now=None, gap=1.0):
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


# ===========================================================================
t.section("1. A LADDER IS EXACTLY 11 BUY STOP + 11 SELL STOP")
eng, broker, feed, settings, rec = build(name="create")
orders = broker.orders()
buys = [o for o in orders if o.side == BUY_STOP]
sells = [o for o in orders if o.side == SELL_STOP]
t.check("11 BUY STOP", len(buys) == DEPTH, f"{len(buys)} (want {DEPTH})")
t.check("11 SELL STOP", len(sells) == DEPTH, f"{len(sells)} (want {DEPTH})")
t.check("22 pending orders in total", len(orders) == LADDER, str(len(orders)))
t.check("buy stops sit above the market",
        all(o.price > feed().ask for o in buys))
t.check("sell stops sit below the market",
        all(o.price < feed().bid for o in sells))
t.check("all 22 belong to ONE ladder id", ids_live(broker) == [1],
        str(ids_live(broker)))
t.check("exactly one LADDER_CREATED", rec.count("LADDER_CREATED") == 1,
        str(rec.count("LADDER_CREATED")))
t.check("exactly 22 ORDER_PLACED", rec.count("ORDER_PLACED") == LADDER,
        str(rec.count("ORDER_PLACED")))
t.check("the deploy report names the count per side",
        any("11 BUY STOP + 11 SELL STOP = 22 live" in e[1]
            for e in rec.events if e[0] == "LADDER_CREATED"),
        str([e[1][:60] for e in rec.events if e[0] == "LADDER_CREATED"]))
t.check("the ladder is ACTIVE", eng.ladder_active and eng.active_ladder_id == 1,
        f"active={eng.ladder_active} id={eng.active_ladder_id}")

t.section("2. A SECOND LADDER CANNOT BE CREATED WHILE ONE IS ACTIVE")
before = eng.cycle.cycle_id
t.check("create_new_ladder() refuses",
        eng.create_new_ladder(reason="second") is False)
t.check("the refusal is logged",
        "LADDER_REJECTED_ALREADY_ACTIVE" in rec.names(), str(rec.names()[-3:]))
t.check("the ladder id did not change", eng.cycle.cycle_id == before,
        f"#{eng.cycle.cycle_id}")
t.check("no extra orders were placed", len(broker.orders()) == LADDER,
        f"{len(broker.orders())} orders")
t.check("still one ladder id live", ids_live(broker) == [1], str(ids_live(broker)))

t.section("3-5. TRIGGERS DO NOT CREATE A NEW LADDER")
placed_before = rec.count("ORDER_PLACED")
take(eng, broker, feed, BUY_STOP)
t.check("3. a BUY STOP trigger opens a position", broker.positions(),
        f"{len(broker.positions())} positions")
t.check("3. it did not create a ladder", rec.count("CYCLE_STARTED") == 1,
        str(rec.count("CYCLE_STARTED")))
take(eng, broker, feed, SELL_STOP)
t.check("4. a SELL STOP trigger did not create a ladder either",
        rec.count("CYCLE_STARTED") == 1, str(rec.count("CYCLE_STARTED")))
for side in (BUY_STOP, SELL_STOP, BUY_STOP, SELL_STOP):
    take(eng, broker, feed, side)
t.check("5. many triggers, still one ladder", rec.count("CYCLE_STARTED") == 1,
        str(rec.count("CYCLE_STARTED")))
t.check("5. still one ladder id everywhere", ids_live(broker) == [1],
        str(ids_live(broker)))
t.check("every position carries the ladder id",
        all(parse_comment(p.comment)[0] == 1 for p in broker.positions()))
t.check("POSITION_OPENED is logged per trigger",
        rec.count("POSITION_OPENED") == len(broker.positions()),
        f"{rec.count('POSITION_OPENED')} vs {len(broker.positions())}")

t.section("7. CONSUMED LEVELS ARE NOT REPLENISHED")
t.check("no order was placed after the initial 22",
        rec.count("ORDER_PLACED") == placed_before == LADDER,
        f"{rec.count('ORDER_PLACED')} placed, {LADDER} at creation")
live = len(broker.orders())
open_now = len(broker.positions())
t.check("pending + open still accounts for the whole ladder",
        live + open_now == LADDER, f"{live} pending + {open_now} open")
for _ in range(5):
    eng.step()
t.check("repeated passes place nothing new",
        rec.count("ORDER_PLACED") == LADDER, str(rec.count("ORDER_PLACED")))
t.check("remaining pendings are never read as a reason to re-ladder",
        rec.count("CYCLE_STARTED") == 1 and len(broker.orders()) == live,
        f"{len(broker.orders())} orders")

t.section("6. CLOSING POSITIONS DOES NOT CREATE A NEW LADDER")
for pos in list(broker.positions())[:2]:
    broker.close_position(pos.ticket)
eng.step()
t.check("positions closed", len(broker.positions()) == open_now - 2,
        f"{len(broker.positions())} positions")
t.check("no new ladder", rec.count("CYCLE_STARTED") == 1,
        str(rec.count("CYCLE_STARTED")))
t.check("no replacement orders", rec.count("ORDER_PLACED") == LADDER,
        str(rec.count("ORDER_PLACED")))
t.check("the ladder is still the same one", eng.active_ladder_id == 1,
        str(eng.active_ladder_id))

t.section("12. TWO ACTIVE LADDER IDS ARE IMPOSSIBLE")
now = [5000.0]
eng, broker, feed, settings, rec = build(
    {"cycle_reentry_cooldown_seconds": 10, "cooldown_after_loss_minutes": 0,
     "basket_profit_target": 0.50, "profit_runner_enabled": False,
     "max_cycle_duration_minutes": 0, "max_cycle_drawdown": 0},
    name="single", clock=lambda: now[0])
overlaps, seen_ids = [], []
for i in range(150):
    if eng.cycle_active:
        if not take(eng, broker, feed, BUY_STOP, now):
            feed.set(round(feed.bid + 0.10, 2))
            now[0] += 1
            eng.step()
    else:
        now[0] += 1
        eng.step()
    live_ids = ids_live(broker)
    if len(live_ids) > 1:
        overlaps.append(live_ids)
    if eng.ladder_active and eng.active_ladder_id not in seen_ids:
        seen_ids.append(eng.active_ladder_id)
t.check("several ladders ran, one after another", len(seen_ids) >= 3,
        str(seen_ids))
t.check("12. never two ladder ids live at the same time", not overlaps,
        str(overlaps[:3]))
t.check("11. every new ladder got a new id",
        seen_ids == sorted(set(seen_ids)), str(seen_ids))

t.section("8-10. FINAL CLOSE -> 10s COOLDOWN -> EXACTLY ONE NEW LADDER")
now = [6000.0]
eng, broker, feed, settings, rec = build(
    {"cycle_reentry_cooldown_seconds": 10, "cooldown_after_loss_minutes": 0,
     "basket_profit_target": 0.50, "profit_runner_enabled": False,
     "max_cycle_duration_minutes": 0, "max_cycle_drawdown": 0},
    name="cool", clock=lambda: now[0])
first_id = eng.active_ladder_id
take(eng, broker, feed, BUY_STOP, now)
for _ in range(30):
    if not eng.cycle_active:
        break
    feed.set(round(feed.bid + 0.10, 2))
    now[0] += 1
    eng.step()
t.check("8. the final close condition ended the ladder", not eng.ladder_active,
        f"active={eng.ladder_active}")
t.check("8. LADDER_CLOSED is logged", "LADDER_CLOSED" in rec.names())
t.check("8. COOLDOWN_STARTED is logged", "COOLDOWN_STARTED" in rec.names())
t.check("8. the account is verified flat",
        not broker.orders() and not broker.positions(),
        f"{len(broker.orders())} orders / {len(broker.positions())} positions")
t.check("8. the cooldown is 10 seconds",
        abs(eng.reentry_until - now[0] - 10) < 1.5,
        f"{eng.reentry_until - now[0]:.1f}s")
t.check("the engine state says COOLDOWN_AFTER_EXIT",
        eng.state == State.COOLDOWN_AFTER_EXIT, eng.state)

created_before = rec.count("CYCLE_STARTED")
for _ in range(8):
    now[0] += 1
    eng.step()
    if now[0] - 6000 > 0 and eng.ladder_active:
        break
t.check("9. no ladder is created during the cooldown",
        rec.count("CYCLE_STARTED") == created_before and not broker.orders(),
        f"{rec.count('CYCLE_STARTED')} starts, {len(broker.orders())} orders")
t.check("9. an explicit request during the cooldown is refused",
        eng.create_new_ladder(reason="too early") is False)
t.check("9. and logged as rejected",
        [e for e in rec.events if e[0] == "LADDER_REJECTED_ALREADY_ACTIVE"
         and "cooldown" in e[1]],
        str([e[1][:60] for e in rec.events
             if e[0] == "LADDER_REJECTED_ALREADY_ACTIVE"][-1:]))

now[0] += 5
eng.step()
t.check("10. exactly one new ladder after the cooldown",
        rec.count("CYCLE_STARTED") == created_before + 1,
        str(rec.count("CYCLE_STARTED")))
t.check("10. COOLDOWN_FINISHED is logged", "COOLDOWN_FINISHED" in rec.names())
t.check("11. it has a new ladder id", eng.active_ladder_id == first_id + 1,
        f"#{eng.active_ladder_id} after #{first_id}")
t.check("10. and a full 22-order ladder", len(broker.orders()) == LADDER,
        f"{len(broker.orders())} orders")
t.check("10. all on the new id", ids_live(broker) == [first_id + 1],
        str(ids_live(broker)))
for _ in range(5):
    now[0] += 1
    eng.step()
t.check("10. and only one - repeated passes add nothing",
        rec.count("CYCLE_STARTED") == created_before + 1 and
        len(broker.orders()) == LADDER,
        f"{rec.count('CYCLE_STARTED')} starts, {len(broker.orders())} orders")

t.section("13. RESTART PRESERVES THE ACTIVE LADDER")
eng, broker, feed, settings, rec = build(name="restart")
take(eng, broker, feed, BUY_STOP)
live_before = {o.ticket for o in broker.orders()}
positions_before = len(broker.positions())
id_before = eng.active_ladder_id
eng.save()
rec2 = Recorder()
eng2 = RollingLadderEngine(broker, settings, hooks=rec2.hooks(),
                           state_path=TMP / "restart_state.json")
eng2.resume()
t.check("13. the same ladder is adopted", eng2.active_ladder_id == id_before,
        f"#{eng2.active_ladder_id} vs #{id_before}")
t.check("13. it is still ACTIVE", eng2.ladder_active)
t.check("13. its positions are still there",
        len(broker.positions()) == positions_before)
eng2.step()
t.check("13. no second ladder was created",
        rec2.count("CYCLE_STARTED") == 0 and rec2.count("LADDER_CREATED") == 0,
        f"{rec2.count('CYCLE_STARTED')} starts")
t.check("13. its orders were not replaced",
        {o.ticket for o in broker.orders()} <= live_before,
        f"{len(broker.orders())} orders")
t.check("13. still one ladder id", ids_live(broker) == [id_before],
        str(ids_live(broker)))

t.section("ORDER COUNT SAFETY: A SHORT LADDER IS NOT COMPENSATED")
eng, broker, feed, settings, rec = build(name="short", deploy=False)
real_place = broker.place_stop_order
accepted = [0]


def flaky(*args, **kwargs):
    """A broker that accepts 4 orders and then refuses the rest."""
    if accepted[0] >= 4:
        return False, None, "retcode=10016 (INVALID_STOPS) comment='too close'"
    accepted[0] += 1
    return real_place(*args, **kwargs)


broker.place_stop_order = flaky
eng.step()
t.check("only what the broker accepted is live", len(broker.orders()) == 4,
        f"{len(broker.orders())} orders")
created = [e for e in rec.events if e[0] == "LADDER_CREATED"]
t.check("the deploy report counts each side against the wanted 11+11",
        created and f"wanted {DEPTH}+{DEPTH}={LADDER}" in created[-1][1],
        created[-1][1] if created else "none")
t.check("it is flagged PARTIAL",
        created and created[-1][2].get("status") == "PARTIAL",
        str(created[-1][2].get("status") if created else None))
t.check("the shortfall is logged as an error",
        any(e[0] == "ERROR" and "deployed SHORT" in e[1] for e in rec.events),
        str([e[1][:70] for e in rec.events if e[0] == "ERROR"]))
t.check("the broker's own refusal is logged",
        any(e[0] == "ORDER_REJECTED" and "INVALID_STOPS" in e[1]
            for e in rec.events))
t.check("NO second ladder is created to compensate",
        rec.count("CYCLE_STARTED") == 1, str(rec.count("CYCLE_STARTED")))
broker.place_stop_order = real_place
for _ in range(3):
    eng.step()
t.check("and none is created once the broker recovers either",
        rec.count("CYCLE_STARTED") == 1, str(rec.count("CYCLE_STARTED")))
t.check("the short ladder keeps its own id", ids_live(broker) == [1],
        str(ids_live(broker)))

t.section("14. TELEGRAM SHOWS THE ACTIVE LADDER")
eng, broker, feed, settings, rec = build(name="tg")
take(eng, broker, feed, BUY_STOP)
take(eng, broker, feed, SELL_STOP)
snap = eng.snapshot()
t.check("the snapshot names the active ladder",
        snap["active_ladder_id"] == 1 and snap["ladder_active"],
        str(snap["active_ladder_id"]))
t.check("status reads ACTIVE", snap["ladder_status"] == "ACTIVE",
        snap["ladder_status"])
t.check("remaining buy stops are live MT5 counts",
        snap["current_pending_buys"] ==
        len([o for o in broker.orders() if o.side == BUY_STOP]),
        str(snap["current_pending_buys"]))
t.check("remaining sell stops are live MT5 counts",
        snap["current_pending_sells"] ==
        len([o for o in broker.orders() if o.side == SELL_STOP]),
        str(snap["current_pending_sells"]))
t.check("triggered is history, and separate from what is live",
        snap["historical_buy_triggers"] + snap["historical_sell_triggers"] == 2
        and snap["positions"] == len(broker.positions()),
        f"{snap['historical_buy_triggers']}B/{snap['historical_sell_triggers']}S "
        f"triggered, {snap['positions']} open")
t.check("the ladder size is reported", snap["ladder_size"] == LADDER,
        str(snap["ladder_size"]))

t.section("15. THE FINAL CLOSE CONDITION IS THE EXISTING ONE")
# Unchanged by this work: hard risk (cycle drawdown, cycle duration) then
# basket profit management. Nothing else may end a ladder.
now = [7000.0]
eng, broker, feed, settings, rec = build(
    {"max_cycle_duration_minutes": 30, "profit_runner_enabled": False,
     "max_cycle_drawdown": 0, "basket_profit_target": 0,
     "cycle_reentry_cooldown_seconds": 10}, name="final", clock=lambda: now[0])
take(eng, broker, feed, BUY_STOP, now)
for _ in range(20):
    now[0] += 60
    eng.step()
t.check("a ladder with no exit condition met stays ACTIVE", eng.ladder_active,
        f"active={eng.ladder_active}")
t.check("and never re-ladders while it waits",
        rec.count("CYCLE_STARTED") == 1, str(rec.count("CYCLE_STARTED")))
now[0] += 31 * 60
eng.step()
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("the existing risk timeout is what closed it",
        closes and closes[-1].kind == "RISK_TIMEOUT",
        str([c.kind for c in closes]))
t.check("only then did the cooldown start",
        rec.names().index("COOLDOWN_STARTED") > rec.names().index("LADDER_CLOSED"))

t.done()
