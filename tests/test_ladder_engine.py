"""RollingLadderEngine: ladder creation, rolling, cycles, risk, recovery."""
import pathlib
import shutil
import time

from harness import Suite, use_stub_mt5
use_stub_mt5()

import config as cfg
from broker import BUY, BUY_STOP, SELL, SELL_STOP
from fakes import (Recorder, TickFeed, gold_spec, make_paper, reach_buy_tp,
                   reach_sell_tp, trigger_buy, trigger_sell)
from ladder_engine import RollingLadderEngine, State, level_comment, parse_comment
from runtime_settings import RuntimeSettings

t = Suite("ladder_engine")
TMP = pathlib.Path("/tmp/ladder_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)


def build(overrides=None, feed=None, spec=None, name="e", balance=1000.0):
    """A fresh engine on a paper broker, with settings overrides applied."""
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}_settings.json")
    for key, value in (overrides or {}).items():
        settings._values[key] = value          # bypass confirmation plumbing
    feed = feed or TickFeed(4010.00)
    broker, feed = make_paper(feed=feed, spec=spec,
                              state_path=TMP / f"{name}_paper.json", balance=balance)
    rec = Recorder()
    engine = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                                 state_path=TMP / f"{name}_state.json")
    engine.resume()
    return engine, broker, feed, settings, rec


def prices(orders, side):
    return sorted(round(o.price, 2) for o in orders if o.side == side)


# ===========================================================================
t.section("LADDER CREATION")
engine, broker, feed, settings, rec = build(name="create")
engine.step()
orders = broker.orders()
t.check("both sides laddered", len(orders) == 10, f"{len(orders)} orders")
t.check("5 BUY STOPs above price", len(prices(orders, BUY_STOP)) == 5)
t.check("5 SELL STOPs below price", len(prices(orders, SELL_STOP)) == 5)
buy_prices = prices(orders, BUY_STOP)
sell_prices = prices(orders, SELL_STOP)
t.check("buy levels are exactly one spacing apart",
        all(abs(round(buy_prices[i + 1] - buy_prices[i], 2) - 0.30) < 1e-9
            for i in range(4)), str(buy_prices))
t.check("sell levels are exactly one spacing apart",
        all(abs(round(sell_prices[i + 1] - sell_prices[i], 2) - 0.30) < 1e-9
            for i in range(4)), str(sell_prices))
t.check("nearest buy level is at least one spacing above the ask",
        min(buy_prices) >= feed().ask + 0.30, f"{min(buy_prices)} vs {feed().ask}")
t.check("nearest sell level is at least one spacing below the bid",
        max(sell_prices) <= feed().bid - 0.30, f"{max(sell_prices)} vs {feed().bid}")
t.check("every level sits on the cycle grid",
        all(abs((p - engine.cycle.anchor) / 0.30 -
                round((p - engine.cycle.anchor) / 0.30)) < 1e-6
            for p in buy_prices + sell_prices))
t.check("every buy stop carries a 0.30 TP",
        all(abs(o.tp - o.price - 0.30) < 1e-9 for o in orders if o.side == BUY_STOP))
t.check("every sell stop carries a 0.30 TP",
        all(abs(o.price - o.tp - 0.30) < 1e-9 for o in orders if o.side == SELL_STOP))
t.check("no stop loss by default", all(o.sl == 0 for o in orders))
t.check("fixed 0.01 lots everywhere", all(o.volume == 0.01 for o in orders))
t.check("each level has a unique identity",
        len({o.comment for o in orders}) == len(orders))
t.check("identity encodes cycle, side and level",
        parse_comment(orders[0].comment)[0] == engine.cycle.cycle_id)
t.check("LADDER_CREATED logged", rec.count("LADDER_CREATED") == 1)
t.check("ORDER_PLACED logged per level", rec.count("ORDER_PLACED") == 10)
t.check("state is LADDER_ACTIVE", engine.state == State.LADDER_ACTIVE)

t.section("IDEMPOTENCE / NO DUPLICATES")
before = [o.ticket for o in broker.orders()]
for _ in range(5):
    engine.step()
after = [o.ticket for o in broker.orders()]
t.check("repeated steps never duplicate orders", before == after, str(len(after)))
t.check("no extra placements logged", rec.count("ORDER_PLACED") == 10)
feed.set(4010.02)          # a small wiggle must not churn the grid
engine.step()
t.check("small price moves do not re-place the ladder",
        [o.ticket for o in broker.orders()] == after)

# ===========================================================================
t.section("TRIGGER -> TP -> ROLL")
# TP below the spacing keeps this scenario to a single trade; with the default
# TP == spacing a TP and the next trigger land on the same tick (see below).
engine, broker, feed, settings, rec = build({"tp_distance": 0.20}, name="roll")
engine.step()
first_buy = min(o.price for o in broker.orders() if o.side == BUY_STOP)
trigger_buy(feed, first_buy)
engine.step()
positions = broker.positions()
t.check("level triggered into a position", len(positions) == 1, str(len(positions)))
t.check("position is a BUY", positions and positions[0].side == BUY)
t.check("ORDER_TRIGGERED logged", rec.count("ORDER_TRIGGERED") == 1)
t.check("entry hook fired for Telegram", len(rec.entries) == 1)
t.check("state is POSITION_ACTIVE", engine.state == State.POSITION_ACTIVE)

triggered_price = positions[0].price_open
tp_price = positions[0].tp
reach_buy_tp(feed, tp_price)
engine.step()
t.check("position closed at TP", not broker.positions())
t.check("TP_HIT logged", rec.count("TP_HIT") == 1)
t.check("TP counted for the cycle", engine.cycle.tp_count == 1)
t.check("cycle P/L credited", abs(engine.cycle.realized - 0.20) < 0.01,
        f"{engine.cycle.realized:.2f}")
t.check("daily P/L credited", abs(engine.daily_profit - 0.20) < 0.01)
t.check("LEVEL_ROLLED logged (level re-armed)", rec.count("LEVEL_ROLLED") == 1)

engine.step()
new_buys = prices(broker.orders(), BUY_STOP)
t.check("ladder rolled forward above the new price",
        min(new_buys) > triggered_price, f"{min(new_buys)} > {triggered_price}")
t.check("still 5 buy levels after rolling", len(new_buys) == 5, str(len(new_buys)))
t.check("still 5 sell levels after rolling",
        len(prices(broker.orders(), SELL_STOP)) == 5)
t.check("levels stay on the cycle grid",
        all(abs(((p - engine.cycle.anchor) / 0.30) -
                round((p - engine.cycle.anchor) / 0.30)) < 1e-6 for p in new_buys))

t.section("SELL SIDE")
engine, broker, feed, settings, rec = build({"tp_distance": 0.20}, name="sell")
engine.step()
first_sell = max(o.price for o in broker.orders() if o.side == SELL_STOP)
trigger_sell(feed, first_sell)
engine.step()
t.check("sell stop triggered on a dip",
        broker.positions() and broker.positions()[0].side == SELL)
sell_pos = broker.positions()[0]
t.check("sell TP is below entry", sell_pos.tp < sell_pos.price_open)
reach_sell_tp(feed, sell_pos.tp)
engine.step()
t.check("sell closed at TP", engine.cycle.tp_count == 1,
        f"tp_count={engine.cycle.tp_count}")
t.check("sell profit is positive", engine.cycle.realized > 0,
        f"{engine.cycle.realized:.2f}")

# ===========================================================================
t.section("PROFIT CYCLE (4 TPs -> reset)")
engine, broker, feed, settings, rec = build({"tp_distance": 0.20}, name="cycle")
start_cycle = engine.cycle.cycle_id
for i in range(4):
    engine.step()
    buys = [o for o in broker.orders() if o.side == BUY_STOP]
    target = min(buys, key=lambda o: o.price)
    trigger_buy(feed, target.price)
    engine.step()
    pos = broker.positions()[0]
    reach_buy_tp(feed, pos.tp)
    engine.step()
    if i < 3:
        t.check(f"TP {i + 1} counted, cycle still open",
                engine.cycle.tp_count == i + 1 and
                engine.cycle.cycle_id == start_cycle,
                f"tp={engine.cycle.tp_count} cycle={engine.cycle.cycle_id}")

t.check("cycle completed after the 4th TP",
        engine.cycle.cycle_id == start_cycle + 1,
        f"cycle now #{engine.cycle.cycle_id}")
t.check("CYCLE_COMPLETED logged", rec.count("CYCLE_COMPLETED") == 1)
t.check("cycle hook carries the P/L",
        any(c[0] == "complete" and c[2] > 0 for c in rec.cycles), str(rec.cycles[-1]))
t.check("new cycle starts flat", engine.cycle.tp_count == 0 and
        engine.cycle.realized == 0.0)
t.check("CYCLE_STARTED logged for the new cycle", rec.count("CYCLE_STARTED") >= 1)
t.check("ladder re-anchored at the new price",
        abs(engine.cycle.anchor - feed().mid) < 0.30,
        f"anchor {engine.cycle.anchor} price {feed().mid}")
engine.step()
t.check("fresh ladder built for the new cycle", len(broker.orders()) == 10)
t.check("new orders carry the new cycle id",
        all(parse_comment(o.comment)[0] == engine.cycle.cycle_id
            for o in broker.orders()))
t.check("cycle completed only once", rec.count("CYCLE_COMPLETED") == 1)

t.section("BASKET CYCLE TARGET (observed behaviour)")
engine, broker, feed, settings, rec = build(
    {"profit_cycle_target": 0, "cycle_take_profit_money": 0.50,
     "cycle_close_positions": True}, name="basket")
engine.step()
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[1].price)          # ask crosses the first two levels
engine.step()
t.check("two levels triggered", len(broker.positions()) == 2, str(len(broker.positions())))
feed.set(buys[1].price + 1.00)            # both deeply in profit
engine.step()
t.check("basket target closed the whole cycle", not broker.positions())
t.check("cycle rolled on the basket target",
        any(c[0] == "complete" and "basket" in str(c[3]) for c in rec.cycles),
        str([c for c in rec.cycles if c[0] == "complete"]))

# ===========================================================================
t.section("RISK: MAX POSITIONS")
engine, broker, feed, settings, rec = build(
    {"max_open_positions": 2, "profit_cycle_target": 99}, name="maxpos")
engine.step()
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[1].price)
engine.step()
t.check("two positions open", len(broker.positions()) == 2,
        str(len(broker.positions())))
engine.step()
t.check("no new levels while at the position cap", not broker.orders(),
        f"{len(broker.orders())} orders")
t.check("RISK_BLOCK logged", rec.count("RISK_BLOCK") >= 1)
t.check("state is RISK_BLOCKED or POSITION_ACTIVE",
        engine.state in (State.RISK_BLOCKED, State.POSITION_ACTIVE), engine.state)
t.check("open positions were NOT closed by the block", len(broker.positions()) == 2)

t.section("RISK: MAX PENDING ORDERS")
engine, broker, feed, settings, rec = build({"max_pending_orders": 6}, name="maxpend")
engine.step()
t.check("pending orders capped", len(broker.orders()) <= 6,
        f"{len(broker.orders())} orders")

t.section("RISK: DAILY LOSS")
engine, broker, feed, settings, rec = build(
    {"max_daily_loss": 0.50, "stop_loss_distance": 0.30}, name="daily")
engine.step()
sells = [o for o in broker.orders() if o.side == SELL_STOP]
target = max(sells, key=lambda o: o.price)
trigger_sell(feed, target.price)
engine.step()
pos = broker.positions()[0]
feed.set(pos.sl + 0.10)                   # ask hits the sell's stop loss
engine.step()
t.check("stop loss closed the position", not broker.positions())
t.check("SL_HIT logged", rec.count("SL_HIT") == 1)
t.check("daily loss recorded", engine.daily_profit < 0, f"{engine.daily_profit:.2f}")
engine.daily_profit = -1.0                # push past the limit
engine.step()
t.check("daily loss limit blocks new entries", not broker.orders())
t.check("risk block reason mentions the daily loss",
        "daily" in engine.block_reason.lower(), engine.block_reason)

t.section("RISK: SPREAD FILTER")
engine, broker, feed, settings, rec = build({"max_spread": 0.20}, name="spread")
feed.set(4010.00, spread=0.50)
engine.step()
t.check("no ladder while the spread is too wide", not broker.orders())
t.check("SPREAD_BLOCK logged", rec.count("SPREAD_BLOCK") == 1)
feed.set(4010.00, spread=0.08)
engine.step()
t.check("ladder resumes automatically when the spread narrows",
        len(broker.orders()) == 10, f"{len(broker.orders())} orders")
t.check("SPREAD_CLEARED logged", rec.count("SPREAD_CLEARED") == 1)
t.check("spread block logged only once while blocked",
        rec.count("SPREAD_BLOCK") == 1)

t.section("RISK: CYCLE LOSS + COOLDOWN")
engine, broker, feed, settings, rec = build(
    {"max_cycle_loss": 0.40, "cooldown_after_loss_minutes": 5,
     "stop_loss_distance": 1.00}, name="cycleloss")
engine.step()
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[0].price)
engine.step()
feed.set(buys[0].price - 0.60)            # deep drawdown, no SL hit yet
engine.step()
t.check("cycle loss limit closed the cycle out", not broker.positions())
t.check("CYCLE_LOSS logged", rec.count("CYCLE_LOSS") == 1)
t.check("losing streak counted", engine.consecutive_losing_cycles == 1)
t.check("cooldown armed", engine.cooldown_until > time.time())
engine.step()
t.check("no new ladder during the cooldown", not broker.orders())
t.check("cooldown is the stated reason", "cooldown" in engine.block_reason.lower(),
        engine.block_reason)

t.section("RISK: LOSING CYCLE STREAK")
engine, broker, feed, settings, rec = build(
    {"max_consecutive_losing_cycles": 2}, name="streak")
engine.consecutive_losing_cycles = 2
engine.step()
t.check("streak limit stops new entries", not broker.orders())
t.check("streak is the stated reason", "losing cycles" in engine.block_reason,
        engine.block_reason)

t.section("RISK: LOT CAP (no martingale)")
engine, broker, feed, settings, rec = build(
    {"lot_size": 0.05, "max_lot_size": 0.02}, name="lotcap")
engine.step()
t.check("lot above the cap blocks entries", not broker.orders())
engine, broker, feed, settings, rec = build({"lot_size": 0.03}, name="lotok")
engine.step()
t.check("lot size is used verbatim, never scaled",
        all(o.volume == 0.03 for o in broker.orders()))

# ===========================================================================
t.section("PAUSE (positions still managed)")
engine, broker, feed, settings, rec = build(name="pause")
engine.step()
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[0].price)
engine.step()
t.check("one position open", len(broker.positions()) == 1)
engine.paused = True
engine.step()
t.check("pause cancels pending levels", not broker.orders())
t.check("pause does not close positions", len(broker.positions()) == 1)
pos = broker.positions()[0]
reach_buy_tp(feed, pos.tp)
engine.step()
t.check("TP still executes while paused", not broker.positions())
t.check("TP still counted while paused", engine.cycle.tp_count == 1)
engine.paused = False
engine.step()
t.check("resume rebuilds the ladder", len(broker.orders()) == 10)

t.section("DIRECTION FILTER")
engine, broker, feed, settings, rec = build({"direction_filter": "buy_bias"},
                                            name="dirbuy")
engine.step()
t.check("buy bias places only BUY STOPs",
        all(o.side == BUY_STOP for o in broker.orders()) and broker.orders())
engine, broker, feed, settings, rec = build({"direction_filter": "sell_bias"},
                                            name="dirsell")
engine.step()
t.check("sell bias places only SELL STOPs",
        all(o.side == SELL_STOP for o in broker.orders()) and broker.orders())
engine, broker, feed, settings, rec = build({"direction_filter": "none"},
                                            name="dirnone")
engine.step()
t.check("'none' places nothing", not broker.orders())
engine, broker, feed, settings, rec = build({"direction_filter": "off"}, name="diroff")
engine.step()
t.check("'off' ladders both ways (default)",
        len({o.side for o in broker.orders()}) == 2)

t.section("ROLL MODES")
engine, broker, feed, settings, rec = build({"roll_mode": "static"}, name="static")
engine.step()
top_before = max(o.price for o in broker.orders() if o.side == BUY_STOP)
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[1].price)
engine.step()
engine.step()
top_after = max(o.price for o in broker.orders() if o.side == BUY_STOP)
t.check("static grid does not extend beyond its cycle window",
        abs(top_after - top_before) < 1e-9, f"{top_before} -> {top_after}")
t.check("consumed static levels are not re-placed",
        len([o for o in broker.orders() if o.side == BUY_STOP]) < 5)

t.section("STALE ORDER PROTECTION")
engine, broker, feed, settings, rec = build({"order_max_age_seconds": 1},
                                            name="stale")
engine.step()
tickets = {o.ticket for o in broker.orders()}
time.sleep(1.1)
engine.step()
t.check("stale orders cancelled and re-placed",
        {o.ticket for o in broker.orders()} != tickets)
t.check("ORDER_CANCELLED logged for stale levels",
        any("stale" in m for e, m, f in rec.events if e == "ORDER_CANCELLED"))
t.check("ladder is still complete after the refresh", len(broker.orders()) == 10)

t.section("BROKER MINIMUM STOP DISTANCE")
engine, broker, feed, settings, rec = build(
    {"first_level_offset": 0.01}, spec=gold_spec(stops_level=50), name="minstop")
engine.step()
buy_min = min(o.price for o in broker.orders() if o.side == BUY_STOP)
t.check("first level respects the broker minimum",
        buy_min >= feed().ask + 0.50, f"{buy_min} vs ask {feed().ask}")

t.section("TP MODES")
engine, broker, feed, settings, rec = build(
    {"tp_mode": "3_pips", "pip_points": 10}, name="pips")
engine.step()
order = next(o for o in broker.orders() if o.side == BUY_STOP)
t.check("3 pips with 10-point pips = 0.30 TP",
        abs(order.tp - order.price - 0.30) < 1e-9, f"{order.tp - order.price:.2f}")
engine, broker, feed, settings, rec = build(
    {"tp_mode": "1_pip", "pip_points": 100}, name="pips2")
engine.step()
order = next(o for o in broker.orders() if o.side == BUY_STOP)
t.check("pip size setting rescales the TP",
        abs(order.tp - order.price - 1.00) < 1e-9, f"{order.tp - order.price:.2f}")

t.section("SETTINGS CHANGE AT RUNTIME")
engine, broker, feed, settings, rec = build(name="live")
engine.step()
old_tp = next(o for o in broker.orders() if o.side == BUY_STOP).tp
settings._values["tp_distance"] = 0.60
settings._values["ladder_spacing"] = 0.50
engine.step()
levels = prices(broker.orders(), BUY_STOP)
t.check("new spacing applied without a restart",
        all(abs(round(levels[i + 1] - levels[i], 2) - 0.50) < 1e-9
            for i in range(len(levels) - 1)), str(levels))
new_tp = next(o for o in broker.orders() if o.side == BUY_STOP).tp
t.check("new TP applied to new levels", new_tp != old_tp)

t.section("RESTART RECOVERY")
engine, broker, feed, settings, rec = build(name="recover")
engine.step()
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[0].price)
engine.step()
cycle_before = engine.cycle.cycle_id
anchor_before = engine.cycle.anchor
tickets_before = {o.ticket for o in broker.orders()}
positions_before = len(broker.positions())

rec2 = Recorder()
engine2 = RollingLadderEngine(broker, settings, hooks=rec2.hooks(),
                              state_path=TMP / "recover_state.json")
engine2.resume()
t.check("restart adopts the live cycle", engine2.cycle.cycle_id == cycle_before,
        f"{engine2.cycle.cycle_id} vs {cycle_before}")
t.check("restart recovers the grid anchor",
        abs(engine2.cycle.anchor - anchor_before) < 1e-9)
t.check("restart sees the open position",
        len(broker.positions()) == positions_before)
engine2.step()
t.check("restart does not duplicate the ladder",
        {o.ticket for o in broker.orders()} >= tickets_before and
        len(broker.orders()) <= 10, f"{len(broker.orders())} orders")
t.check("no second ladder created", rec2.count("LADDER_CREATED") == 0)
t.check("open position not re-entered", len(broker.positions()) == positions_before)

t.section("ORPHAN / FOREIGN ORDER CLEANUP")
engine, broker, feed, settings, rec = build(name="orphan")
engine.step()
broker.place_stop_order(BUY_STOP, feed().ask + 5.0, 0.01, comment="RL999B7")
engine.step()
t.check("orders from another cycle are cancelled",
        all(parse_comment(o.comment)[0] == engine.cycle.cycle_id
            for o in broker.orders()))
broker.place_stop_order(BUY_STOP, feed().ask + 4.0, 0.01, comment="manual trade")
engine.step()
t.check("unrecognised orders are cancelled",
        all(o.comment.startswith("RL") for o in broker.orders()))

t.section("LEVEL IDENTITY")
t.check("comment round trip",
        parse_comment(level_comment(127, BUY, 3)) == (127, BUY, 3))
t.check("negative levels round trip",
        parse_comment(level_comment(127, SELL, -4)) == (127, SELL, -4))
t.check("comment fits MT5's limit", len(level_comment(99999, BUY, -99)) <= 31)
t.check("foreign comments are ignored", parse_comment("manual") is None)

t.done()
