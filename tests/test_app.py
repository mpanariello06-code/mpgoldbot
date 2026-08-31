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
    "TP_MODE": "distance", "TP_DISTANCE": "0.20", "LADDER_DEPTH": "5",
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
for name in ("trades.csv", "events.csv", "account_snapshots.csv",
             "rolling_ladder_events.csv", "rolling_ladder_cycles.csv"):
    t.check(f"{name} created", (TMP / name).exists())
header = open(TMP / "rolling_ladder_events.csv").readline().strip().split(",")
for column in ("timestamp", "symbol", "cycle_id", "candle_time", "event_type",
               "side", "entry_price", "ladder_price", "ladder_index",
               "buy_trigger_count", "sell_trigger_count", "consecutive_buy",
               "consecutive_sell", "last_side", "previous_side",
               "direction_changes", "buy_sell_ratio", "sell_buy_ratio",
               "price_distance_traveled", "time_since_previous_trigger",
               "basket_pnl", "basket_drawdown", "momentum_score",
               "reversal_score", "exhaustion_score", "exit_score", "action",
               "reason"):
    t.check(f"event log has {column!r}", column in header)
cycle_header = open(TMP / "rolling_ladder_cycles.csv").readline().strip().split(",")
for column in ("cycle_id", "triggers", "buy_triggers", "sell_triggers",
               "imbalance", "realized_pnl", "exit_score", "end_kind",
               "end_reason"):
    t.check(f"cycle log has {column!r}", column in cycle_header)
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
        any(r["event_type"] == "ORDER_PLACED"
            for r in rows("rolling_ladder_events.csv")))
t.check("ladder state persisted", (TMP / "ladder_state.json").exists())
t.check("paper state persisted", (TMP / "paper_state.json").exists())

t.section("TRIGGER -> TP -> CSV -> NOTIFICATIONS")
level = min(o.price for o in app.bot.orders() if o.side == BUY_STOP)
mt5.set_price(round(level - 0.08, 2))          # ask lands exactly on the level
t.check("level triggered", wait_for(lambda: len(app.bot.positions()) >= 1),
        f"{len(app.bot.positions())} positions")
t.check("start message sent once", sum("STARTED" in n for n in notes) == 1,
        str(notes[:1]))
t.check("NO per-entry Telegram message (low-spam policy)",
        not any("LADDER ENTRY" in n for n in notes), str(notes))
# the position is visible to the broker a moment before the hook writes the
# row, so wait for the record rather than assuming it is already there
t.check("trade recorded with ladder context",
        wait_for(lambda: any(r["reason"] == "OPEN" and r["cycle_id"] and
                             r["level"] for r in rows("trades.csv"))),
        str([r for r in rows("trades.csv") if r["reason"] == "OPEN"][-1:]))

pos = app.bot.positions()[0]
mt5.set_price(round(pos.tp, 2))
t.check("TP executed", wait_for(lambda: not app.bot.positions()))
t.check("NO per-TP Telegram message", not any("TP HIT" in n for n in notes),
        str(notes))
t.check("entries are still recorded in full",
        any(r["event_type"] == "ORDER_TRIGGERED"
            for r in rows("rolling_ladder_events.csv")))
events = rows("rolling_ladder_events.csv")
t.check("TP recorded in the ladder log",
        any(r["event_type"] == "TP_HIT" for r in events))
triggered = [r for r in events if r["event_type"] == "ORDER_TRIGGERED"]
t.check("trigger rows carry the sequence state",
        triggered and triggered[-1]["buy_trigger_count"] != "",
        str(triggered[-1] if triggered else None))
t.check("trigger rows carry the exit score",
        triggered and triggered[-1]["exit_score"] != "")
t.check("close recorded in trades.csv",
        any(r["reason"] == "CLOSE" for r in rows("trades.csv")))
t.check("paper balance grew", app.bot.account().balance > 1000.0,
        str(app.bot.account().balance))
t.check("ladder rolled forward",
        wait_for(lambda: len([o for o in app.bot.orders()
                              if o.side == BUY_STOP]) == 5))

t.section("STATUS SNAPSHOT")
status = app.bot.status()
for key in ("state", "lifecycle", "mode", "symbol", "timeframe", "bid", "spread",
            "spacing", "tp_distance", "lot", "positions", "orders", "cycle_id",
            "buy_triggers", "sell_triggers", "imbalance", "exit_score",
            "momentum_score", "reversal_score", "exhaustion_score", "decision",
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
rows_before = len(rows("rolling_ladder_events.csv"))
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
t.check("CSV history preserved",
        len(rows("rolling_ladder_events.csv")) >= rows_before)
app2.bot.stop()

t.section("SHUTDOWN")
app.shutdown()
t.check("monitor stopped", not app.monitor.is_alive())
t.check("account snapshots written", len(rows("account_snapshots.csv")) >= 1)
t.check("csv files intact",
        (TMP / "rolling_ladder_events.csv").stat().st_size > 0)
app2.shutdown()

t.done()
