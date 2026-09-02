"""RollingLadderEngine: ladder creation, rolling, cycles, risk, recovery."""
import pathlib
import shutil
import time

from harness import Suite, use_stub_mt5
use_stub_mt5()

import config as cfg
from broker import BUY, BUY_STOP, SELL, SELL_STOP
from fakes import (Recorder, TickFeed, gold_spec, make_paper, trigger_buy,
                   trigger_sell)
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
# The basket architecture: a triggered level is one leg of a cycle, not an
# independent trade with its own target.
t.check("NO individual take profit on any ladder order by default",
        all(o.tp == 0 for o in orders), str([o.tp for o in orders][:4]))
t.check("there is no TP setting to compute one from",
        not any(k.startswith("tp_") for k in settings.snapshot()))
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
# The ladder rolls when a level is consumed, not when a TP is hit - there are
# no take profits. The window extends past the level price has just eaten.
engine, broker, feed, settings, rec = build(name="roll")
engine.step()
first_buy = min(o.price for o in broker.orders() if o.side == BUY_STOP)
trigger_buy(feed, first_buy)
engine.step()
positions = broker.positions()
t.check("level triggered into a position", len(positions) == 1, str(len(positions)))
t.check("position is a BUY", positions and positions[0].side == BUY)
t.check("it carries no take profit", positions[0].tp == 0, str(positions[0].tp))
t.check("ORDER_TRIGGERED logged", rec.count("ORDER_TRIGGERED") == 1)
t.check("entry hook fired for Telegram", len(rec.entries) == 1)
t.check("state is POSITION_ACTIVE", engine.state == State.POSITION_ACTIVE)
t.check("the leg is in the basket", engine.get_cycle_floating_pnl() != 0.0 or
        len(engine.cycle_positions()) == 1)

triggered_price = positions[0].price_open
feed.set(round(first_buy + 0.35, 2))
engine.step()
t.check("a favourable move does NOT close the leg", len(broker.positions()) >= 1,
        f"{len(broker.positions())} positions")
t.check("no TP event can be logged", rec.count("TP_HIT") == 0)
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
engine, broker, feed, settings, rec = build(name="sell")
engine.step()
first_sell = max(o.price for o in broker.orders() if o.side == SELL_STOP)
trigger_sell(feed, first_sell)
engine.step()
t.check("sell stop triggered on a dip",
        broker.positions() and broker.positions()[0].side == SELL)
sell_pos = broker.positions()[0]
t.check("the sell leg carries no take profit either", sell_pos.tp == 0,
        str(sell_pos.tp))
feed.set(round(first_sell - 0.35, 2))
engine.step()
t.check("a favourable move does not close it",
        any(p.ticket == sell_pos.ticket for p in broker.positions()))
t.check("the basket is in profit", engine.get_cycle_floating_pnl() > 0,
        f"{engine.get_cycle_floating_pnl():+.2f}")

# ===========================================================================
t.section("A CLEAN RUN IS NOT CLOSED BY A TRADE COUNT")
engine, broker, feed, settings, rec = build(
    {"basket_profit_target": 0}, name="cycle")     # normal exit disabled
start_cycle = engine.cycle.cycle_id
for i in range(5):
    engine.step()
    buys = [o for o in broker.orders() if o.side == BUY_STOP]
    if not buys:
        break
    target = min(buys, key=lambda o: o.price)
    trigger_buy(feed, target.price)
    engine.step()

t.check("five clean BUY levels did not end the cycle",
        engine.cycle.cycle_id == start_cycle,
        f"cycle #{engine.cycle.cycle_id} after "
        f"{engine.sequence.total_triggers} triggers")
t.check("no CYCLE_COMPLETED logged for a trade count",
        rec.count("CYCLE_COMPLETED") == 0)
t.check("the sequence was tracked", engine.sequence.buy_triggers >= 4,
        str(engine.sequence.buy_triggers))
t.check("with the target off, only risk can end it",
        engine.cycle_active and engine.get_cycle_floating_pnl() > 2.0,
        f"{engine.get_cycle_floating_pnl():+.2f}")

t.section("THE BASKET TARGET ENDS THE CYCLE")
# the re-entry cooldown has its own section in test_continuous; here the
# question is only whether the target ends the cycle cleanly
engine, broker, feed, settings, rec = build(
    {"basket_profit_target": 0.50,
     "cycle_reentry_cooldown_seconds": 0}, name="target")
engine.step()
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[1].price)          # two BUY levels trigger
engine.step()
start_cycle = engine.cycle.cycle_id
t.check("two BUY triggers recorded", engine.sequence.buy_triggers == 2,
        str(engine.sequence.buy_triggers))
t.check("the basket is still short of the target",
        engine.cycle_active and
        engine.get_cycle_floating_pnl() < settings.get("basket_profit_target"),
        f"{engine.get_cycle_floating_pnl():+.2f}")

# the market moves the basket into profit
for _ in range(10):
    if not engine.cycle_active:
        break
    feed.set(round(feed.bid + 0.15, 2))
    engine.step()

# Closing a cycle no longer starts the next one: the re-entry cooldown sits in
# between, so "closed" is `cycle_active`, not a bumped id.
t.check("the cycle closed on the target",
        not engine.cycle_active or engine.cycle.cycle_id != start_cycle,
        f"cycle #{engine.cycle.cycle_id} active={engine.cycle_active}")
t.check("CYCLE_COMPLETED or CYCLE_LOSS logged",
        rec.count("CYCLE_COMPLETED") + rec.count("CYCLE_LOSS") == 1)
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("the close names the one normal exit reason",
        [c.kind for c in closes] == ["BASKET_PROFIT_TARGET"],
        str([c.kind for c in closes]))
t.check("the reason names the number that caused it",
        closes and "target" in closes[0].reason, str([c.reason for c in closes]))
t.check("the basket state at the exit is handed to the logger",
        closes and closes[0].context.get("floating_pnl_at_exit", 0) >= 0.50,
        str(closes[0].context if closes else None))
t.check("positions were closed out", not broker.positions())
engine.step()          # the (zero-length) re-entry cooldown is served
t.check("pending orders were cancelled or rebuilt for the new cycle",
        all(parse_comment(o.comment)[0] == engine.cycle.cycle_id
            for o in broker.orders()))
t.check("a fresh sequence started", engine.sequence.total_triggers == 0)
t.check("new cycle re-anchored at the current price",
        abs(engine.cycle.anchor - feed().mid) < 0.60,
        f"anchor {engine.cycle.anchor} price {feed().mid}")
engine.step()
t.check("a fresh ladder is built, unless a cooldown is holding it",
        len(broker.orders()) > 0 or "cooldown" in engine.block_reason.lower(),
        f"{len(broker.orders())} orders, reason: {engine.block_reason}")

t.section("RISK-FORCED CYCLE CLOSE (drawdown, not a profit target)")
engine, broker, feed, settings, rec = build(
    {"max_cycle_drawdown": 0.50, "cooldown_after_loss_minutes": 5},
    name="forced")
engine.step()
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[0].price)
engine.step()
feed.set(buys[0].price - 0.80)            # basket well past the drawdown limit
engine.step()
t.check("cycle force-closed on drawdown", not broker.positions())
t.check("logged as a cycle loss", rec.count("CYCLE_LOSS") == 1)
t.check("attributed to risk, not the exit engine",
        any(c.kind == "RISK_DRAWDOWN" for c in rec.cycles
            if c.kind_of == "complete"),
        str([c.kind for c in rec.cycles if c.kind_of == "complete"]))
t.check("losing streak counted", engine.consecutive_losing_cycles == 1)
t.check("cooldown armed", engine.cooldown_until > time.time())

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
    {"max_daily_drawdown": 0.50, "stop_loss_distance": 0.30}, name="daily")
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
t.check("daily drawdown limit blocks new entries", not broker.orders())
t.check("risk block reason mentions the daily drawdown",
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

t.section("LADDER DEPTH CAP LIMITS GROWTH, IT DOES NOT BLIND THE BOT")
engine, broker, feed, settings, rec = build({"max_ladder_depth": 2}, name="depthcap")
engine.step()
buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
              key=lambda o: o.price)
trigger_buy(feed, buys[1].price)
engine.step()
engine.step()
live = broker.orders()
t.check("the live ladder is NOT cancelled by the cap", len(live) > 0,
        f"{len(live)} orders")
t.check("the cap is logged once",
        len([e for e in rec.events if e[0] == "LADDER_DEPTH_CAP"]) == 1,
        str([e[0] for e in rec.events if e[0] == "LADDER_DEPTH_CAP"]))
t.check("no risk block is raised for depth", "depth" not in engine.block_reason.lower(),
        engine.block_reason)
before = len(broker.orders())
for _ in range(5):
    engine.step()
t.check("no further levels are added beyond the cap",
        len(broker.orders()) <= before, f"{before} -> {len(broker.orders())}")
t.check("the cycle can still reach an exit decision",
        engine.sequence is not None and engine.cycle_active)

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
t.check("the paused position has no TP to close it",
        broker.positions()[0].tp == 0)
feed.set(round(feed.bid + 1.00, 2))
engine.step()
t.check("a paused basket is still held, not closed",
        len(broker.positions()) == 1, f"{len(broker.positions())} positions")
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

t.section("SETTINGS CHANGE AT RUNTIME")
engine, broker, feed, settings, rec = build(name="live")
engine.step()
settings._values["ladder_spacing"] = 0.50
engine.step()
levels = prices(broker.orders(), BUY_STOP)
t.check("new spacing applied without a restart",
        all(abs(round(levels[i + 1] - levels[i], 2) - 0.50) < 1e-9
            for i in range(len(levels) - 1)), str(levels))
t.check("levels still carry no TP after a settings change",
        all(o.tp == 0 for o in broker.orders()))

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

t.section("A PARTIAL DEPLOYMENT IS REPORTED AS PARTIAL")
# The log must say what the broker ACCEPTED, never what the bot intended: a
# ladder that is half live has to be visible as half live.
engine, broker, feed, settings, rec = build(name="partial")
real_place = broker.place_stop_order
accepted = [0]


def flaky_place(*args, **kwargs):
    if accepted[0] >= 3:
        return False, None, ("retcode=10016 (INVALID_STOPS) comment='too close' | "
                             "requested=? volume=0.01 | stops_level=50pts")
    accepted[0] += 1
    return real_place(*args, **kwargs)


broker.place_stop_order = flaky_place
engine.step()
t.check("only what the broker accepted is live", len(broker.orders()) == 3,
        f"{len(broker.orders())} orders")
created = [e for e in rec.events if e[0] == "LADDER_CREATED"]
t.check("LADDER_CREATED reports actual of intended",
        created and created[-1][1].startswith("Cycle #1: 3 of 10 levels live"),
        created[-1][1] if created else "none")
t.check("the deployment is flagged PARTIAL",
        created and created[-1][2].get("status") == "PARTIAL",
        str(created[-1][2].get("status") if created else None))
t.check("the rejection count is reported",
        created and "1 rejected" in created[-1][1], created[-1][1] if created else "")
rejected = [e for e in rec.events if e[0] == "ORDER_REJECTED"]
t.check("the refusal itself is logged, never swallowed", len(rejected) == 1,
        str(len(rejected)))
t.check("with the broker's own diagnosis attached",
        rejected and "INVALID_STOPS" in rejected[0][1] and
        "stops_level" in rejected[0][1], rejected[0][1] if rejected else "")
t.check("a partial ladder does not stop the cycle",
        engine.cycle.cycle_id == 1 and not engine.block_reason,
        f"#{engine.cycle.cycle_id} {engine.block_reason!r}")

broker.place_stop_order = real_place        # the broker recovers
engine.step()
t.check("the missing levels are filled in on the next pass",
        len(broker.orders()) == 10, f"{len(broker.orders())} orders")

t.section("LEVEL IDENTITY")
t.check("comment round trip",
        parse_comment(level_comment(127, BUY, 3)) == (127, BUY, 3))
t.check("negative levels round trip",
        parse_comment(level_comment(127, SELL, -4)) == (127, SELL, -4))
t.check("comment fits MT5's limit", len(level_comment(99999, BUY, -99)) <= 31)
t.check("foreign comments are ignored", parse_comment("manual") is None)

t.done()
