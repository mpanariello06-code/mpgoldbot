"""Execution adapters: live MT5 order plumbing and the paper simulator."""
import pathlib
import shutil

from harness import Suite, use_stub_mt5
use_stub_mt5()

import MetaTrader5 as mt5
from broker import BUY, BUY_STOP, SELL_STOP, Mt5Broker, PaperBroker
from fakes import TickFeed, gold_spec

t = Suite("broker")
TMP = pathlib.Path("/tmp/broker_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)

MAGIC = 88001199

# ===========================================================================
t.section("LIVE MT5 ADAPTER")
mt5.reset()
mt5.initialize()
mt5.set_price(4010.06)
b = Mt5Broker("XAUUSD", MAGIC)

spec = b.symbol_spec()
t.check("symbol spec read from the terminal", spec.digits == 2 and spec.point == 0.01)
tick = b.tick()
t.check("tick read", tick.bid == 4010.06 and round(tick.spread, 2) == 0.08)
t.check("account read", b.account().login == 123456)

ok, ticket, msg = b.place_stop_order(BUY_STOP, 4010.64, 0.01, tp=4010.94,
                                     comment="RL1B2")
t.check("BUY STOP placed", ok and ticket, msg)
req = mt5.STATE["sent"][-1]
t.check("uses TRADE_ACTION_PENDING", req["action"] == mt5.TRADE_ACTION_PENDING)
t.check("uses ORDER_TYPE_BUY_STOP", req["type"] == mt5.ORDER_TYPE_BUY_STOP)
t.check("carries magic + comment",
        req["magic"] == MAGIC and req["comment"] == "RL1B2")
t.check("price and TP normalized", req["price"] == 4010.64 and req["tp"] == 4010.94)

ok2, ticket2, _ = b.place_stop_order(SELL_STOP, 4009.44, 0.01, tp=4009.14,
                                     comment="RL1S-2")
t.check("SELL STOP placed", ok2)
t.check("uses ORDER_TYPE_SELL_STOP",
        mt5.STATE["sent"][-1]["type"] == mt5.ORDER_TYPE_SELL_STOP)

orders = b.orders()
t.check("orders() reports both", len(orders) == 2, str(len(orders)))
t.check("sides mapped", {o.side for o in orders} == {BUY_STOP, SELL_STOP})

mt5.STATE["orders"].append(type(mt5.STATE["orders"][0])(
    ticket=1, symbol="XAUUSD", type=mt5.ORDER_TYPE_BUY_STOP, price_open=1.0,
    volume_current=0.01, sl=0.0, tp=0.0, comment="other bot", magic=777,
    time_setup=0))
t.check("another EA's orders are invisible", len(b.orders()) == 2)

mt5.trigger_order(ticket, fill_price=4010.64)
positions = b.positions()
t.check("triggered order becomes a position", len(positions) == 1)
t.check("position side mapped", positions[0].side == BUY)
t.check("position keeps the level comment", positions[0].comment == "RL1B2")
t.check("pending list shrinks", len(b.orders()) == 1)

b.poll_closed()                                   # consume the opening deal
mt5.hit_tp(ticket)
closed = b.poll_closed()
t.check("closed trade reported once", len(closed) == 1, str(len(closed)))
t.check("close carries entry, exit and profit",
        closed and abs(closed[0].profit - 0.30) < 1e-9 and
        closed[0].price_open == 4010.64, str(closed[:1]))
t.check("TP reason detected", closed[0].reason == "TP")
t.check("no duplicate on the next poll", b.poll_closed() == [])

ok, msg = b.cancel_order(ticket2)
t.check("cancel removes the pending order", ok and not b.orders(), msg)
t.check("cancel uses TRADE_ACTION_REMOVE",
        mt5.STATE["sent"][-1]["action"] == mt5.TRADE_ACTION_REMOVE)

mt5.STATE["reject_orders"] = True
ok, ticket3, msg = b.place_stop_order(BUY_STOP, 4011.0, 0.01, comment="RL1B4")
t.check("rejection reported, not raised", not ok and "rejected" in msg, msg)
# A refused order is a fact about the broker's constraints. The report has to
# say enough to diagnose it without reproducing the failure by hand.
for fragment in ("retcode=", "requested=4011.0", "volume=0.01", "bid=", "ask=",
                 "spread=", "stops_level=", "freeze=", "tick_size=", "point=",
                 "digits=", "volume_min=", "step=", "symbol="):
    t.check(f"rejection report carries {fragment!r}", fragment in msg, msg)
t.check("the retcode is named, not just numbered",
        "(" in msg and "UNKNOWN" not in msg.split("|")[0], msg.split("|")[0])
mt5.STATE["reject_orders"] = False

t.section("LIVE CLOSE")
ok, tk, _ = b.place_stop_order(BUY_STOP, 4010.70, 0.01, tp=4011.0, comment="RL1B3")
mt5.trigger_order(tk, 4010.70)
b.poll_closed()
ok, msg = b.close_position(tk)
t.check("close_position sends a DEAL against the position", ok, msg)
t.check("position gone", not b.positions())
closed = b.poll_closed()
t.check("manual close reported", len(closed) == 1 and closed[0].reason != "TP")

# ===========================================================================
t.section("PAPER BROKER")
feed = TickFeed(4010.00)
paper = PaperBroker("XAUUSD", MAGIC, spec_provider=gold_spec, tick_provider=feed,
                    state_path=TMP / "paper.json", start_balance=1000.0)
t.check("paper reports itself as paper", paper.is_paper and paper.name == "PAPER")
paper.place_stop_order(BUY_STOP, 4010.60, 0.01, tp=4010.90, comment="RL1B2")
paper.place_stop_order(SELL_STOP, 4009.40, 0.01, tp=4009.10, comment="RL1S-2")
t.check("orders held", len(paper.orders()) == 2)
t.check("nothing was sent to MT5",
        mt5.STATE["sent"][-1]["action"] != mt5.TRADE_ACTION_PENDING or
        mt5.STATE["sent"][-1]["comment"] != "RL1B2")

feed.set(4010.30)
t.check("no fill before the level is reached", paper.poll_closed() == [] and
        not paper.positions())
feed.set(4010.52)                                  # ask 4010.60 == level
paper.poll_closed()
t.check("BUY STOP filled when the ask reaches it", len(paper.positions()) == 1)
t.check("filled at the level price",
        abs(paper.positions()[0].price_open - 4010.60) < 1e-9,
        str(paper.positions()[0].price_open))
t.check("floating P/L tracked", paper.positions()[0].profit < 0)

feed.set(4010.90)
closed = paper.poll_closed()
t.check("TP closes the position", len(closed) == 1 and closed[0].reason == "TP")
t.check("profit is the TP distance", abs(closed[0].profit - 0.30) < 1e-9,
        str(closed[0].profit))
t.check("balance credited", abs(paper.account().balance - 1000.30) < 1e-9,
        str(paper.account().balance))
t.check("closed trades are reported once", paper.poll_closed() == [])

t.section("PAPER STOP LOSS")
paper.place_stop_order(SELL_STOP, 4010.00, 0.01, tp=4009.70, sl=4010.50,
                       comment="RL1S-1")
feed.set(4010.00)
paper.poll_closed()
t.check("SELL STOP filled on a dip", len(paper.positions()) == 1)
feed.set(4010.45)                                  # ask 4010.53 > SL
closed = paper.poll_closed()
t.check("stop loss closes the position",
        len(closed) == 1 and closed[0].reason == "SL", str(closed[:1]))
t.check("loss debited", closed[0].profit < 0, str(closed[0].profit))

t.section("PAPER SLIPPAGE CAP")
paper2 = PaperBroker("XAUUSD", MAGIC, spec_provider=gold_spec,
                     tick_provider=feed, max_slippage_points=20)
paper2.place_stop_order(BUY_STOP, 4011.00, 0.01, tp=4011.30, comment="RL1B9")
feed.set(4013.00)                     # a 2.00 gap straight through level and TP
closed = paper2.poll_closed()
t.check("gap fills and closes in the same tick", len(closed) == 1, str(closed[:1]))
t.check("gapped fill capped at the slippage allowance",
        abs(closed[0].price_open - 4011.20) < 1e-9, str(closed[0].price_open))
t.check("a gapped entry still books its real profit",
        abs(closed[0].profit - 0.10) < 1e-9, str(closed[0].profit))

t.section("PAPER PERSISTENCE (restart recovery)")
feed.set(4010.00)
paper.place_stop_order(BUY_STOP, 4011.00, 0.01, tp=4011.30, comment="RL1B5")
before_orders = {o.ticket for o in paper.orders()}
before_balance = paper.account().balance
paper3 = PaperBroker("XAUUSD", MAGIC, spec_provider=gold_spec, tick_provider=feed,
                     state_path=TMP / "paper.json", start_balance=1.0)
t.check("orders restored after a restart",
        {o.ticket for o in paper3.orders()} == before_orders)
t.check("balance restored after a restart",
        abs(paper3.account().balance - before_balance) < 1e-9)
t.check("tickets never collide after a restart",
        paper3.place_stop_order(BUY_STOP, 4012.0, 0.01, comment="RL1B6")[1]
        not in before_orders)

t.done()
