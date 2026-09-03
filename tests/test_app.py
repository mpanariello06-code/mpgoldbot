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
    "LADDER_DEPTH": "5",
    "PAPER_START_BALANCE": "1000",
    # the cooldown is exercised properly in test_basket; here it only needs to
    # be short enough that the suite does not spend ten seconds waiting
    "CYCLE_REENTRY_COOLDOWN": "1",
})

import MetaTrader5 as mt5
import config as cfg
import optimized as bot
from broker import BUY_STOP, SELL_STOP
from ladder_engine import parse_comment

t = Suite("app")
# one ladder = LADDER_DEPTH per side
LADDER_ORDERS = cfg.LADDER_DEPTH * 2
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
             "rolling_ladder_events.csv", "rolling_ladder_cycles.csv",
             "basket_telemetry.csv"):
    t.check(f"{name} created", (TMP / name).exists())
header = open(TMP / "rolling_ladder_events.csv").readline().strip().split(",")
for column in ("timestamp", "symbol", "cycle_id", "candle_time", "event_type",
               "side", "entry_price", "ladder_price", "ladder_index",
               "buy_trigger_count", "sell_trigger_count", "last_side",
               "previous_side", "direction_changes", "ladder_depth_used",
               "price_distance_traveled", "net_levels",
               "basket_floating_pnl", "basket_realized_pnl", "basket_pnl",
               "basket_drawdown", "basket_profit_target", "action", "reason"):
    t.check(f"event log has {column!r}", column in header)
cycle_header = open(TMP / "rolling_ladder_cycles.csv").readline().strip().split(",")
for column in ("cycle_id", "triggers", "buy_triggers", "sell_triggers",
               "realized_pnl", "end_kind", "end_reason",
               # the state that caused the exit, not the empty state after it
               "initial_price", "exit_price", "positions_at_exit",
               "open_buys_at_exit", "open_sells_at_exit",
               "pending_orders_at_exit", "floating_pnl_at_exit",
               "peak_pnl", "drawdown", "duration_seconds",
               "basket_profit_target", "ladder_depth_used", "net_levels",
               "max_floating_profit", "max_floating_loss", "max_drawdown",
               "profit_giveback", "time_to_peak", "time_to_profit_target",
               "time_in_profit", "time_in_protection", "protection_active",
               "cycle_state", "exit_reason", "final_realized_pnl"):
    t.check(f"cycle log has {column!r}", column in cycle_header)
telemetry_header = open(TMP / "basket_telemetry.csv").readline().strip().split(",")
for column in ("timestamp", "symbol", "cycle_id", "elapsed_seconds", "bid",
               "ask", "spread", "current_pnl", "peak_pnl",
               "drawdown_from_peak", "realized_pnl", "open_positions",
               "open_buys", "open_sells", "pending_orders", "net_volume",
               "ladder_depth", "triggers", "buy_triggers", "sell_triggers",
               "direction_changes", "basket_profit_target",
               "protection_active", "protection_threshold", "cycle_state"):
    t.check(f"telemetry log has {column!r}", column in telemetry_header)
events = [e["event_type"] for e in rows("events.csv")]
t.check("startup recorded", "BOT_STARTED" in events and "MT5_CONNECTED" in events,
        str(sorted(set(events))))

t.section("START -> LADDER")
app.bot.set_notifier(lambda text: notes.append(text))
ok, msg = app.bot.start()
t.check("start() ok", ok, msg)
t.check("state RUNNING", app.bot.state == "RUNNING")
t.check("ladder built", wait_for(lambda: len(app.bot.orders()) == LADDER_ORDERS),
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
# The first ladder of a run is announced by the START message; a CYCLE_ACTIVE
# for it must not add a second one.
app.bot._on_event("CYCLE_ACTIVE", "Cycle #1 ACTIVE: ladder deployed",
                  {"cycle_id": 1, "levels_live": 10, "status": "OK"})
t.check("the first ladder does not get a duplicate STARTED message",
        sum("STARTED" in n for n in notes) == 1, str(notes[-1:]))
# after a cycle has closed, the next deployment gets its own message
app.bot._cycles_closed = 1
app.bot._on_event("CYCLE_ACTIVE", "Cycle #2 ACTIVE: ladder deployed",
                  {"cycle_id": 2, "levels_live": 10, "status": "OK"})
t.check("a ladder that follows a close is announced once",
        sum("CYCLE #2 STARTED" in n for n in notes) == 1, str(notes[-1:]))
t.check("it confirms the deployment, not an intention",
        any("Ladder deployed." in n for n in notes), str(notes[-1:]))
app.bot._cycles_closed = 0
t.check("NO per-entry Telegram message (low-spam policy)",
        not any("LADDER ENTRY" in n for n in notes), str(notes))
# the position is visible to the broker a moment before the hook writes the
# row, so wait for the record rather than assuming it is already there
t.check("trade recorded with ladder context",
        wait_for(lambda: any(r["reason"] == "OPEN" and r["cycle_id"] and
                             r["level"] for r in rows("trades.csv"))),
        str([r for r in rows("trades.csv") if r["reason"] == "OPEN"][-1:]))

pos = app.bot.positions()[0]
t.check("the triggered position has NO take profit", pos.tp == 0, str(pos.tp))
# A level does not close on its own; the whole basket exits together, and with
# the profit runner on it is the trail that takes it, not the bare target.
mt5.set_price(round(pos.price_open + 6.00, 2))     # well past activation
t.check("the basket ran past the target under protection",
        wait_for(lambda: app.bot.status().get("protection_active")),
        str(app.bot.status().get("basket_peak_pnl")))
peak = app.bot.status().get("basket_peak_pnl", 0)
mt5.set_price(round(pos.price_open + 6.00 - 2.50, 2))   # give back past the trail
t.check("the trail closed the whole basket",
        wait_for(lambda: not app.bot.positions()),
        f"{len(app.bot.positions())} positions, peak {peak}")
t.check("NO per-TP Telegram message", not any("TP HIT" in n for n in notes),
        str(notes))
t.check("the close names profit protection",
        any("PROFIT PROTECTION" in n for n in notes), str(notes[-1:]))
t.check("and reports the peak and the give-back",
        any("Peak:" in n and "Giveback:" in n for n in notes), str(notes[-1:]))
t.check("entries are still recorded in full",
        any(r["event_type"] == "ORDER_TRIGGERED"
            for r in rows("rolling_ladder_events.csv")))
events = rows("rolling_ladder_events.csv")
t.check("no TP event can be logged any more",
        not any(r["event_type"] == "TP_HIT" for r in events))
triggered = [r for r in events if r["event_type"] == "ORDER_TRIGGERED"]
t.check("trigger rows carry the sequence state",
        triggered and triggered[-1]["buy_trigger_count"] != "",
        str(triggered[-1] if triggered else None))
t.check("trigger rows carry the basket P/L and its target",
        triggered and triggered[-1]["basket_floating_pnl"] != ""
        and triggered[-1]["basket_profit_target"] != "")
t.check("close recorded in trades.csv",
        any(r["reason"] == "CLOSE" for r in rows("trades.csv")))
t.check("paper balance grew", app.bot.account().balance > 1000.0,
        str(app.bot.account().balance))
t.check("the next ladder follows the cooldown",
        wait_for(lambda: len([o for o in app.bot.orders()
                              if o.side == BUY_STOP]) == 5, timeout=8.0),
        f"{len(app.bot.orders())} orders")

t.section("STATUS SNAPSHOT")
status = app.bot.status()
for key in ("state", "lifecycle", "mode", "symbol", "timeframe", "bid", "spread",
            "spacing", "lot", "positions", "orders", "cycle_id",
            "basket_profit_target",
            "basket_floating_pnl", "basket_realized_pnl", "basket_net_pnl",
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
# The grid is pinned for the life of the ladder, so after a 2.00 move only the
# levels still reachable from the pinned grid come back - and, critically, the
# SAME ladder comes back rather than a new one.
ladder_before = app.bot.status().get("cycle_id")
t.check("the same ladder resumes, not a new one",
        wait_for(lambda: app.bot.orders()) and
        app.bot.status().get("cycle_id") == ladder_before,
        f"#{app.bot.status().get('cycle_id')} vs #{ladder_before}")
t.check("its orders all carry that ladder id",
        all(parse_comment(o.comment)[0] == ladder_before
            for o in app.bot.orders()),
        str(sorted({parse_comment(o.comment)[0] for o in app.bot.orders()})))

t.section("MT5 DISCONNECT / RECONNECT")
cycle_before = app.bot.engine.cycle.cycle_id
mt5.STATE["fail_symbol_info"] = True
app.bot._reconnect_at = 0
time.sleep(0.4)
mt5.STATE["fail_symbol_info"] = False
time.sleep(0.5)
telemetry_header = open(TMP / "basket_telemetry.csv").readline().strip().split(",")
for column in ("timestamp", "symbol", "cycle_id", "elapsed_seconds", "bid",
               "ask", "spread", "current_pnl", "peak_pnl",
               "drawdown_from_peak", "realized_pnl", "open_positions",
               "open_buys", "open_sells", "pending_orders", "net_volume",
               "ladder_depth", "triggers", "buy_triggers", "sell_triggers",
               "direction_changes", "basket_profit_target",
               "protection_active", "protection_threshold", "cycle_state"):
    t.check(f"telemetry log has {column!r}", column in telemetry_header)
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
