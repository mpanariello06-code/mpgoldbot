"""Full application: startup, lifecycle, paper trading, recovery, CSV output."""
import csv
import json
import os
import pathlib
import shutil
import threading
import time

from harness import Suite, use_stub_mt5
use_stub_mt5()

TMP = pathlib.Path("/tmp/app_tests")
shutil.rmtree(TMP, ignore_errors=True)
os.environ.update({
    "DATA_DIRECTORY": str(TMP), "SYMBOL": "XAUUSD", "TIMEFRAME": "M5",
    "TRADING_MODE": "PAPER", "AUTO_START_TRADING": "false",
    "TELEGRAM_BOT_TOKEN": "", "POLL_SECONDS": "0.05",
    "ACCOUNT_SNAPSHOT_INTERVAL": "1", "LADDER_SPACING": "0.30",
    "TP_DISTANCE": "0.20", "LADDER_DEPTH": "5", "PROFIT_CYCLE_TARGET": "4",
    "PAPER_START_BALANCE": "1000",
})

import MetaTrader5 as mt5
import config as cfg
import optimized as bot
from broker import BUY_STOP, SELL_STOP

t = Suite("app")
mt5.reset()
mt5.set_price(4010.00)

notes = []


def rows(name):
    with open(TMP / name) as fh:
        return list(csv.DictReader(fh))


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


t.section("CONFIG VALIDATION")
errors, warnings = cfg.validate()
t.check("valid config has no errors", errors == [], str(errors))
t.check("paper mode is the default", cfg.TRADING_MODE == "PAPER")
t.check("missing telegram token warns clearly",
        any("TELEGRAM_BOT_TOKEN" in w for w in warnings))
t.check("secrets masked in the safe dump",
        cfg.safe_dict()["MT5_PASSWORD"] in ("(empty)", "***set***"))

t.section("STARTUP")
app = bot.Application()
app.start()
t.check("MT5 connected", app.bot._mt5_ready)
t.check("paper broker selected", app.bot.broker.is_paper)
t.check("monitor thread alive", app.monitor.is_alive())
t.check("engine not auto-started", app.bot.state == "STOPPED")
for name in ("trades.csv", "events.csv", "account_snapshots.csv", "ladder.csv"):
    t.check(f"{name} created", (TMP / name).exists())
t.check("ladder.csv has the specified columns",
        open(TMP / "ladder.csv").readline().strip().split(",") ==
        ["timestamp", "cycle_id", "ladder_id", "symbol", "direction", "level",
         "entry_price", "exit_price", "tp", "sl_if_used", "lot_size", "spread",
         "order_ticket", "position_ticket", "event", "profit", "cycle_profit",
         "daily_profit"])
events = [e["event_type"] for e in rows("events.csv")]
t.check("startup recorded", "BOT_STARTED" in events and "MT5_CONNECTED" in events,
        str(sorted(set(events))))

t.section("START -> LADDER")
app.bot.set_notifier(lambda text: notes.append(text))
ok, msg = app.bot.start()
t.check("start() ok", ok, msg)
t.check("state RUNNING", app.bot.state == "RUNNING")
t.check("ladder built", wait_for(lambda: len(app.bot.orders()) == 10),
        f"{len(app.bot.orders())} orders")
orders = app.bot.orders()
t.check("buy stops above the market",
        all(o.price > 4010.08 for o in orders if o.side == BUY_STOP))
t.check("sell stops below the market",
        all(o.price < 4010.00 for o in orders if o.side == SELL_STOP))
t.check("no real order reached MT5",
        not any(r.get("action") == mt5.TRADE_ACTION_PENDING
                for r in mt5.STATE["sent"]))
t.check("only one engine thread",
        len([th for th in threading.enumerate() if th.name == "ladder-engine"]) == 1)
ok2, msg2 = app.bot.start()
t.check("second start refused", not ok2, msg2)
t.check("ladder events logged",
        any(r["event"] == "ORDER_PLACED" for r in rows("ladder.csv")))
t.check("ladder state persisted", (TMP / "ladder_state.json").exists())
t.check("paper state persisted", (TMP / "paper_state.json").exists())

t.section("TRIGGER -> TP -> CSV -> NOTIFICATIONS")
level = min(o.price for o in app.bot.orders() if o.side == BUY_STOP)
mt5.set_price(round(level - 0.08, 2))          # ask lands exactly on the level
t.check("level triggered", wait_for(lambda: len(app.bot.positions()) >= 1),
        f"{len(app.bot.positions())} positions")
t.check("LADDER ENTRY notification sent",
        any("LADDER ENTRY" in n for n in notes), str(notes[-1:]))
t.check("entry notification names the cycle and level",
        any("Cycle: #" in n and "Level:" in n for n in notes))
open_rows = [r for r in rows("trades.csv") if r["reason"] == "OPEN"]
t.check("trade recorded with ladder context",
        open_rows and open_rows[-1]["cycle_id"] and open_rows[-1]["level"],
        str(open_rows[-1:]))

pos = app.bot.positions()[0]
mt5.set_price(round(pos.tp, 2))
t.check("TP executed", wait_for(lambda: not app.bot.positions()))
t.check("TP HIT notification sent", any("TP HIT" in n for n in notes))
t.check("TP recorded in ladder.csv",
        any(r["event"] == "TP_HIT" for r in rows("ladder.csv")))
t.check("close recorded in trades.csv",
        any(r["reason"] == "CLOSE" for r in rows("trades.csv")))
t.check("paper balance grew", app.bot.account().balance > 1000.0,
        str(app.bot.account().balance))
t.check("ladder rolled forward",
        wait_for(lambda: len([o for o in app.bot.orders()
                              if o.side == BUY_STOP]) == 5))

t.section("STATUS SNAPSHOT")
status = app.bot.status()
for key in ("state", "mode", "symbol", "timeframe", "bid", "spread", "spacing",
            "tp_distance", "lot", "positions", "orders", "cycle_id", "tp_count",
            "cycle_profit", "daily_profit", "last_update"):
    t.check(f"status carries {key}", key in status and status[key] is not None,
            str(status.get(key)))
t.check("status reports PAPER", status["mode"] == "PAPER")

t.section("PAUSE / RESUME")
ok, msg = app.bot.pause()
t.check("pause() ok", ok and app.bot.state == "PAUSED", msg)
t.check("pause cancels pending levels", wait_for(lambda: not app.bot.orders()))
level = 4012.00
mt5.set_price(level)
time.sleep(0.3)
t.check("no new entries while paused", not app.bot.positions())
ok, msg = app.bot.resume()
t.check("resume() ok", ok and app.bot.state == "RUNNING", msg)
t.check("ladder rebuilt on resume", wait_for(lambda: len(app.bot.orders()) == 10))

t.section("MT5 DISCONNECT / RECONNECT")
cycle_before = app.bot.engine.cycle.cycle_id
mt5.STATE["fail_symbol_info"] = True
app.bot._reconnect_at = 0
time.sleep(0.4)
mt5.STATE["fail_symbol_info"] = False
time.sleep(0.5)
events = [e["event_type"] for e in rows("events.csv")]
t.check("disconnect logged", "MT5_DISCONNECTED" in events)
t.check("engine survived the dropout", app.bot.state == "RUNNING")
t.check("cycle preserved across the reconnect",
        app.bot.engine.cycle.cycle_id == cycle_before)
t.check("ladder still maintained", wait_for(lambda: len(app.bot.orders()) > 0))

t.section("STOP")
positions_before = len(app.bot.positions())
ok, msg = app.bot.stop()
t.check("stop() ok", ok, msg)
t.check("state STOPPED", app.bot.state == "STOPPED")
t.check("engine thread gone",
        not any(th.name == "ladder-engine" for th in threading.enumerate()))
t.check("pending orders cancelled on stop", not app.bot.orders())
t.check("positions NOT closed by stop",
        len(app.bot.positions()) == positions_before)
t.check("second stop refused", not app.bot.stop()[0])

t.section("RESTART RECOVERY")
state = json.load(open(TMP / "ladder_state.json"))
t.check("cycle persisted", state["cycle"]["cycle_id"] == cycle_before)
rows_before = len(rows("ladder.csv"))
app2 = bot.Application()
app2.start()
ok, msg = app2.bot.start()
t.check("restart starts cleanly", ok, msg)
t.check("restart adopts the stored cycle",
        app2.bot.engine.cycle.cycle_id == cycle_before,
        f"{app2.bot.engine.cycle.cycle_id} vs {cycle_before}")
t.check("restart recovers the open position",
        len(app2.bot.positions()) == positions_before)
t.check("no duplicate ladder after restart",
        wait_for(lambda: 0 < len(app2.bot.orders()) <= 10),
        f"{len(app2.bot.orders())} orders")
t.check("CSV history preserved", len(rows("ladder.csv")) >= rows_before)
app2.bot.stop()

t.section("SHUTDOWN")
app.shutdown()
t.check("monitor stopped", not app.monitor.is_alive())
t.check("account snapshots written", len(rows("account_snapshots.csv")) >= 1)
t.check("csv files intact", (TMP / "ladder.csv").stat().st_size > 0)
app2.shutdown()

t.done()
