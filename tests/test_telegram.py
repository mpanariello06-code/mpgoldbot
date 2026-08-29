"""Telegram control panel + ladder settings UI (offline, no network)."""
import asyncio
import pathlib
import shutil
from datetime import datetime
from types import SimpleNamespace

from harness import Suite, use_stub_mt5
use_stub_mt5()

import os
TMP = pathlib.Path("/tmp/tg_tests")
shutil.rmtree(TMP, ignore_errors=True)
os.environ.update({"DATA_DIRECTORY": str(TMP), "SYMBOL": "XAUUSD",
                   "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
                   "TELEGRAM_CHAT_ID": "111",
                   "TELEGRAM_ALLOWED_CHAT_IDS": "222, 333"})

import MetaTrader5 as mt5
import config as cfg
from broker import BUY, BUY_STOP, SELL_STOP, OpenPosition, PendingOrder
from csv_logger import CsvLogger
from runtime_settings import RuntimeSettings
from telegram_controller import TelegramController

t = Suite("telegram")
CSV = CsvLogger(cfg.DATA_PATH, "trades.csv", "events.csv", "account_snapshots.csv",
                "ladder.csv")
S = RuntimeSettings(cfg.runtime_defaults(), cfg.DATA_PATH / "runtime_settings.json")
mt5.reset()
mt5.initialize()


class FakeEngine:
    symbol = "XAUUSD"

    def __init__(self):
        self.calls = []
        self.state_name = "STOPPED"
        self.explode = False

    def symbol_info_live(self):
        return mt5.symbol_info("XAUUSD")

    def _rec(self, name):
        self.calls.append(name)

    def start(self):
        self._rec("start"); self.state_name = "RUNNING"
        return True, "Trading started."

    def pause(self):
        self._rec("pause"); self.state_name = "PAUSED"
        return True, "Trading paused."

    def resume(self):
        self._rec("resume"); self.state_name = "RUNNING"
        return True, "Trading resumed."

    def stop(self):
        self._rec("stop"); self.state_name = "STOPPED"
        return True, "Trading stopped. 10 pending orders cancelled."

    def status(self):
        self._rec("status")
        if self.explode:
            raise RuntimeError("mt5 exploded")
        snap = S.snapshot()
        return {"state": self.state_name, "icon": "🟢",
                "paused": self.state_name == "PAUSED", "symbol": "XAUUSD",
                "mt5_connected": True, "account": mt5.account_info(),
                "mode": "PAPER", "engine_state": "LADDER_ACTIVE",
                "timeframe": snap["timeframe"], "bid": 4010.06, "ask": 4010.14,
                "spread": 0.08, "spacing": snap["ladder_spacing"],
                "depth": snap["ladder_depth"], "tp_distance": snap["tp_distance"],
                "tp_mode": snap["tp_mode"], "lot": snap["lot_size"],
                "positions": 1, "orders": 9, "cycle_id": 127, "tp_count": 3,
                "cycle_target": snap["profit_cycle_target"], "cycle_profit": 1.36,
                "daily_profit": 2.10, "total_tp": 12, "total_trades": 15,
                "anchor": 4010.04, "block_reason": "", "spread_blocked": False,
                "losing_streak": 0, "last_update": datetime.now(),
                "uptime": "1h 2m", "error": "", "last_loop_at": None}

    def account(self):
        self._rec("account"); return mt5.account_info()

    def positions(self):
        self._rec("positions")
        return [OpenPosition(ticket=500001, symbol="XAUUSD", side=BUY, volume=0.01,
                             price_open=4010.64, tp=4010.94, sl=0.0, profit=0.12,
                             comment="RL127B2")]

    def orders(self):
        self._rec("orders")
        return [PendingOrder(ticket=1, symbol="XAUUSD", side=BUY_STOP,
                             price=4010.94, volume=0.01, tp=4011.24,
                             comment="RL127B3"),
                PendingOrder(ticket=2, symbol="XAUUSD", side=SELL_STOP,
                             price=4009.44, volume=0.01, tp=4009.14,
                             comment="RL127S-2")]

    def today_stats(self):
        self._rec("stats"); return CSV.ladder_stats()


engine = FakeEngine()
tc = TelegramController(engine, CSV, S)
panel = tc.panel


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.edits = []
        self.answers = []
        self.markups = []
        self.message = SimpleNamespace(reply_text=self._reply)

    async def _reply(self, text, **kw):
        self.edits.append(text)

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)
        self.markups.append(kw.get("reply_markup"))


def upd(chat_id, data=None, text=None):
    msg = SimpleNamespace(text=text, replies=[])

    async def reply(t_, **kw):
        msg.replies.append(t_)

    msg.reply_text = reply
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id),
                           effective_user=SimpleNamespace(id=chat_id),
                           effective_message=msg,
                           callback_query=FakeQuery(data) if data else None)


def buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def press(data, chat=111):
    u = upd(chat, data)
    await tc.on_button(u, None)
    q = u.callback_query
    return (q.edits[-1] if q.edits else ""), (q.markups[-1] if q.markups else None), q


async def run():
    t.section("AUTHORIZATION")
    t.check("primary chat authorized", tc.is_authorized(111))
    t.check("extra chat ids authorized", tc.is_authorized(222) and tc.is_authorized(333))
    t.check("unknown chat rejected", not tc.is_authorized(999))
    _, _, q = await press("start", chat=999)
    t.check("unauthorized button -> Unauthorized.", q.answers == ["Unauthorized."])
    t.check("unauthorized action never runs", "start" not in engine.calls)
    t.check("unauthorized sees no data", not q.edits)
    _, _, q = await press("settings", chat=999)
    t.check("unauthorized cannot open settings", q.answers == ["Unauthorized."])
    u = upd(999)
    await tc.cmd_status(u, None)
    t.check("unauthorized /status refused", u.effective_message.replies == ["Unauthorized."])
    events = open(cfg.DATA_PATH / "events.csv").read()
    t.check("unauthorized attempts logged", events.count("UNAUTHORIZED") == 3,
            str(events.count("UNAUTHORIZED")))

    t.section("MAIN CONTROL PANEL")
    text, markup, _ = await press("refresh")
    data = buttons(markup)
    t.check("all controls present",
            data == ["start", "pause", "resume", "stop", "status", "account",
                     "positions", "stats", "ladder", "settings", "refresh"],
            str(data))
    t.check("panel shows the cycle", "Cycle #127" in text)
    t.check("panel shows the mode", "[PAPER]" in text)

    t.section("LIFECYCLE BUTTONS")
    for action, expect in [("start", "Trading Started"), ("pause", "Trading Paused"),
                           ("resume", "Trading Resumed"), ("stop", "Bot Stopped")]:
        text, _, _ = await press(action)
        t.check(f"button {action}", expect in text, text.replace("\n", " | ")[:60])
    t.check("every lifecycle button reached the engine",
            all(c in engine.calls for c in ("start", "pause", "resume", "stop")))
    t.check("stop reports the cancelled pendings",
            "pending orders cancelled" in (await press("stop"))[0])

    t.section("STATUS SCREEN")
    text, _, _ = await press("status")
    for label in ["Symbol: XAUUSD", "Timeframe", "Price:", "Spread:",
                  "Ladder spacing", "TP:", "Lot:", "Open positions: 1",
                  "Pending orders: 9", "Cycle: #127", "Successful TPs: 3/4",
                  "Cycle P/L", "Daily P/L", "Last ladder update"]:
        t.check(f"status shows {label!r}", label in text)
    t.check("status never leaks the token", "TESTTOKEN" not in text)

    t.section("LADDER VIEW")
    text, _, _ = await press("ladder")
    t.check("ladder view lists BUY STOPS", "BUY STOPS" in text and "4010.94" in text)
    t.check("ladder view lists SELL STOPS", "SELL STOPS" in text and "4009.44" in text)
    t.check("ladder view marks the market price", "price 4010.06" in text)
    t.check("ladder view shows open positions", "OPEN" in text and "4010.64" in text)
    t.check("ladder view names the cycle", "Cycle #127" in text)

    t.section("ACCOUNT / POSITIONS / STATS")
    text, _, _ = await press("account")
    t.check("account uses live values", "$450.00" in text, text.replace("\n", " | "))
    text, _, _ = await press("positions")
    t.check("position rendered",
            all(k in text for k in ("BUY", "0.01", "4010.64", "4010.94", "500001")))
    t.check("pending count included", "Pending ladder orders: 2" in text)
    text, _, _ = await press("stats")
    t.check("stats reports honestly when empty",
            "No ladder activity recorded yet today." in text)
    CSV.log_ladder("ORDER_TRIGGERED", cycle_id=127, symbol="XAUUSD", direction="BUY")
    CSV.log_ladder("TP_HIT", cycle_id=127, symbol="XAUUSD", profit=0.30)
    CSV.log_ladder("CYCLE_COMPLETED", cycle_id=127, profit=1.36)
    text, _, _ = await press("stats")
    t.check("stats counts ladder events",
            "TP hits: 1" in text and "Levels triggered: 1" in text and
            "Cycles completed: 1" in text, text.replace("\n", " | "))

    t.section("SETTINGS: EVERY MENU RENDERS")
    menus = ["settings", "settings_tp", "settings_sl", "settings_pip", "settings_lot",
             "settings_maxlot", "settings_ladder", "settings_spacing",
             "settings_depth", "settings_offset", "settings_roll", "settings_cycle",
             "settings_basket", "settings_risk", "settings_open", "settings_pending",
             "settings_spread", "settings_daily", "settings_cycleloss",
             "settings_streak", "settings_cooldown", "settings_age",
             "settings_direction"]
    seen = set()
    for m in menus:
        text, markup = panel.render(m, 111)
        seen.update(buttons(markup))
        t.check(f"menu {m}", bool(text) and markup is not None)

    t.section("SETTINGS: NO DEAD BUTTONS")
    dead = []
    for cb in sorted(seen):
        if cb == "panel":
            continue
        if not panel.handles(cb):
            dead.append(cb)
            continue
        try:
            text, markup = panel.render(cb, 111)
            if not text:
                dead.append(cb)
        except Exception as exc:
            dead.append(f"{cb} ({exc})")
    t.check("every settings button routes to a screen", not dead, str(dead[:5]))
    S.reset()

    t.section("SETTINGS SCREEN CONTENT")
    text, _ = panel.render("settings", 111)
    for label in ["TP:", "Spacing:", "Depth:", "Lot:", "Profit cycle:",
                  "Max open:", "Max spread:", "Daily loss:", "Direction:"]:
        t.check(f"settings screen shows {label!r}", label in text)
    t.check("pip size resolved from the live symbol", "1 pip = 0.01" in text,
            [l for l in text.splitlines() if "pip" in l])
    t.check("settings say changes affect new levels", "NEW levels only" in text)

    t.section("CONFIRMATION FLOW")
    text, markup = panel.render("confirm:ladder_spacing:0.5", 111)
    t.check("confirm screen shown", "CONFIRM CHANGE" in text and "0.3 → 0.5" in text,
            text.replace("\n", " | ")[:90])
    t.check("confirm offers CONFIRM + CANCEL",
            buttons(markup) == ["apply:ladder_spacing:0.5", "settings_cancel:spacing"])
    t.check("nothing changed before confirming", S.get("ladder_spacing") == 0.30)
    panel.render("settings_cancel:spacing", 111)
    t.check("cancel leaves it alone", S.get("ladder_spacing") == 0.30)
    text, _ = panel.render("apply:ladder_spacing:0.5", 111)
    t.check("confirm applies", S.get("ladder_spacing") == 0.5 and "✅" in text)

    for cb, key, want in [("confirm:tp_mode:2_pips", "tp_mode", "2_pips"),
                          ("confirm:lot_size:0.02", "lot_size", 0.02),
                          ("confirm:ladder_depth:8", "ladder_depth", 8),
                          ("confirm:max_open_positions:2", "max_open_positions", 2),
                          ("confirm:profit_cycle_target:6", "profit_cycle_target", 6),
                          ("confirm:direction_filter:buy_bias", "direction_filter",
                           "buy_bias")]:
        before = S.get(key)
        panel.render(cb, 111)
        t.check(f"{key} waits for confirmation", S.get(key) == before)
        panel.render("apply:" + cb.split(":", 1)[1], 111)
        t.check(f"{key} applied after confirming", S.get(key) == want, str(S.get(key)))

    t.section("DIRECT (LOW IMPACT) SETTINGS")
    panel.render("apply:max_spread:0.35", 111)
    t.check("spread applied without confirmation", S.get("max_spread") == 0.35)
    panel.render("apply:pip_points:10", 111)
    t.check("pip size applied", S.get("pip_points") == 10)
    text, _ = panel.render("settings_pip", 111)
    t.check("pip menu reflects the pin", "1 pip = 0.1 (10 points, pinned)" in text,
            [l for l in text.splitlines() if "pip =" in l])
    panel.render("apply:order_max_age_seconds:300", 111)
    t.check("order age applied", S.get("order_max_age_seconds") == 300)
    before = S.get("rearm_levels")
    panel.render(f"apply:rearm_levels:{'false' if before else 'true'}", 111)
    t.check("toggle flips a boolean", S.get("rearm_levels") is not before)

    t.section("CUSTOM TYPED VALUES")
    t.check("nothing pending initially", panel.awaiting_input(111) is None)
    text, markup = panel.render("custom:tp_distance", 111)
    t.check("prompt shown", "CUSTOM TP DISTANCE" in text and
            panel.awaiting_input(111) == "tp_distance")
    t.check("other chats unaffected", panel.handle_text(222, "0.5") is None)
    text, _ = panel.handle_text(111, "abc")
    t.check("invalid text explained", "INVALID VALUE" in text and "not a number" in text)
    t.check("still awaiting after an invalid value",
            panel.awaiting_input(111) == "tp_distance")
    text, _ = panel.handle_text(111, "9999")
    t.check("out of range refused", "INVALID VALUE" in text and "between" in text)
    text, _ = panel.handle_text(111, " 0.45 ")
    t.check("valid custom value asks to confirm", "CONFIRM CHANGE" in text and
            "0.45" in text)
    panel.render("apply:tp_distance:0.45", 111)
    t.check("custom value applied", S.get("tp_distance") == 0.45)
    panel.render("custom:lot_size", 111)
    text, _ = panel.handle_text(111, "5")
    t.check("cross-field check runs on typed values",
            "INVALID VALUE" in text and "MAX LOT" in text)
    t.check("lot unchanged", S.get("lot_size") == 0.02)
    panel.render("settings_cancel:lot", 111)
    t.check("cancel clears the pending prompt", panel.awaiting_input(111) is None)

    t.section("TYPED VALUES THROUGH THE CONTROLLER")
    panel.render("custom:max_daily_loss", 111)
    u = upd(111, text="25")
    await tc.on_text(u, None)
    t.check("controller handles typed values",
            u.effective_message.replies and
            "CONFIRM CHANGE" in u.effective_message.replies[0])
    u = upd(111, text="hello bot")
    await tc.on_text(u, None)
    t.check("ordinary chatter ignored", not u.effective_message.replies)
    u = upd(999, text="0.5")
    await tc.on_text(u, None)
    t.check("unauthorized text ignored", not u.effective_message.replies)

    t.section("RESET")
    text, markup = panel.render("confirm:reset:1", 111)
    t.check("reset asks first", "CONFIRM RESET" in text and
            buttons(markup) == ["apply:reset:1", "settings_cancel:main"])
    t.check("not reset yet", S.get("ladder_spacing") == 0.5)
    panel.render("apply:reset:1", 111)
    t.check("reset restores the original configuration",
            S.snapshot() == cfg.runtime_defaults())

    t.section("ERROR ISOLATION")
    engine.explode = True
    text, _, _ = await press("status")
    t.check("engine error reported, never raised", "Command failed" in text)
    engine.explode = False
    t.check("telegram errors logged to events.csv",
            "TELEGRAM" in open(cfg.DATA_PATH / "events.csv").read())
    tc.notify("no loop running - must be a no-op")
    t.check("notify without a running loop is safe", True)

    t.section("PTB WIRING")
    from telegram.ext import Application
    app = Application.builder().token("123456:TESTTOKEN").build()
    tc._register(app)
    t.check("handlers registered", len(app.handlers[0]) >= 12,
            str(len(app.handlers[0])))
    t.check("error handler registered", len(app.error_handlers) == 1)


asyncio.run(run())
t.done()
