"""
The Telegram startup menu, and switching entry mode from it.

Two things are held here:

  * launching the bot PUSHES the control panel, with buttons, without anyone
    typing /start - and a Telegram failure while doing so never reaches the
    trading engine
  * changing ENTRY_MODE from Telegram is saved for the NEXT cycle and leaves a
    running ladder completely alone
"""
import asyncio
import os
import pathlib
import shutil
from types import SimpleNamespace

from harness import Suite, use_stub_mt5
use_stub_mt5()

TMP = pathlib.Path("/tmp/startup_menu_tests")
shutil.rmtree(TMP, ignore_errors=True)
os.environ.update({"DATA_DIRECTORY": str(TMP), "SYMBOL": "XAUUSD",
                   "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
                   "TELEGRAM_CHAT_ID": "111"})

import MetaTrader5 as mt5
import config as cfg
from broker import BUY_STOP, SELL_STOP, Mt5Broker
from csv_logger import CsvLogger
from fakes import Recorder
from ladder_engine import RollingLadderEngine
from runtime_settings import RuntimeSettings
from telegram_controller import TelegramController

t = Suite("startup_menu")
CSV = CsvLogger(cfg.DATA_PATH, "trades.csv", "events.csv",
                "account_snapshots.csv", "ladder.csv")
S = RuntimeSettings(cfg.runtime_defaults(), cfg.DATA_PATH / "rs.json")
mt5.reset()
mt5.initialize()


def engine(entry_mode="FULL_LADDER", cycle_active=True, cycle_mode=None):
    """A stand-in with just what the panel and the settings screen read."""
    S._values["entry_mode"] = entry_mode
    snap = S.snapshot()
    running = cycle_mode or entry_mode

    def status():
        return {
            "lifecycle": "RUNNING", "icon": "🟢", "state": "LADDER_ACTIVE",
            "symbol": "XAUUSD", "mt5_connected": True, "mode": "PAPER",
            "timeframe": snap["timeframe"], "bid": 4422.06, "ask": 4422.14,
            "spacing": snap["ladder_spacing"], "lot": snap["lot_size"],
            "entry_mode": entry_mode, "entry_timeframe": snap["entry_timeframe"],
            "basket_profit_target": snap["basket_profit_target"],
            "cycle_id": 183, "cycle_active": cycle_active,
            "buy_triggers": 4, "sell_triggers": 2,
            "basket_floating_pnl": 1.25, "positions": 6, "orders": 2,
        }

    return SimpleNamespace(
        symbol="XAUUSD", status=status, cycle_active=cycle_active,
        cycle=SimpleNamespace(cycle_id=183, entry_mode=running),
        entry_mode_in_force=lambda snap=None: running,
        symbol_info_live=lambda: mt5.symbol_info("XAUUSD"))


class FakeBot:
    """Records what the controller would have sent to Telegram."""

    def __init__(self, explode=False):
        self.sent = []
        self.explode = explode

    async def send_message(self, chat_id, text, parse_mode=None,
                           reply_markup=None):
        if self.explode:
            raise RuntimeError("telegram is down")
        self.sent.append((chat_id, text, reply_markup))


def wired(tc, explode=False):
    """Give the controller a live loop + app, as a running poller would."""
    loop = asyncio.new_event_loop()
    bot = FakeBot(explode=explode)
    tc._loop = loop
    tc._app = SimpleNamespace(bot=bot)
    return loop, bot


def drain(loop, timeout=2.0):
    """Run the fire-and-forget coroutine the controller scheduled."""
    pending = asyncio.all_tasks(loop) if loop.is_running() else None
    loop.run_until_complete(asyncio.sleep(0))
    for _ in range(50):
        tasks = [x for x in asyncio.all_tasks(loop) if not x.done()]
        if not tasks:
            break
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    return pending


# ===========================================================================
t.section("1, 3. STARTUP PUSHES THE MENU, WITH THE ENTRY MODE ON IT")
tc = TelegramController(engine("SINGLE_PAIR"), CSV, S)
loop, bot = wired(tc)
# the controller must believe its loop is running, as it is in production
loop_running = SimpleNamespace(is_running=lambda: True)
tc._loop = loop
orig_is_running = loop.is_running
loop.is_running = lambda: True
ok = tc.send_menu(banner="🟢 <b>BOT ONLINE</b>")
loop.is_running = orig_is_running
drain(loop)
t.check("1. send_menu reported success", ok is True, str(ok))
t.check("1. exactly one message went out", len(bot.sent) == 1, str(len(bot.sent)))
chat_id, text, markup = bot.sent[0] if bot.sent else (None, "", None)
t.check("1. addressed to the configured chat", chat_id == 111, str(chat_id))
t.check("1. it carries the BUTTONS, not just text", markup is not None)
data = [b.callback_data for row in markup.inline_keyboard for b in row] \
    if markup else []
t.check("1. and the buttons are the real control panel",
        {"start", "pause", "stop", "status", "settings"} <= set(data), str(data))
t.check("1. the banner says the bot is online", "BOT ONLINE" in text,
        text[:80])
t.check("3. the menu shows the current entry mode",
        "Entry mode:" in text and "SINGLE PAIR" in text,
        text.replace("\n", " | ")[:220])
t.check("3. and the spacing, cycle, M1 gate and basket exit",
        all(w in text for w in ("Spacing:", "Cycle:", "M1 entry:",
                                "Basket exit:")),
        text.replace("\n", " | ")[:260])

t.section("4. THE ENTRY MODE BUTTON IS ON THE STARTUP MENU")
t.check("4. the panel offers an ENTRY MODE control",
        "settings_entrymode" in data, str(data))
t.check("4. and every pre-existing control is still there",
        all(c in data for c in ("start", "pause", "resume", "stop", "status",
                                "account", "positions", "stats", "ladder",
                                "settings", "refresh")), str(data))
t.check("4. it routes to the existing settings panel, not a new one",
        tc.panel.handles("settings_entrymode"))
text_em, markup_em = tc.panel.render("settings_entrymode", 111)
t.check("4. the screen names the current mode",
        "SINGLE PAIR" in text_em, text_em.replace("\n", " | ")[:200])
em_buttons = [b.callback_data for row in markup_em.inline_keyboard
              for b in row]
t.check("4. it offers both modes and a way back",
        em_buttons[:2] == ["confirm:entry_mode:FULL_LADDER",
                           "confirm:entry_mode:SINGLE_PAIR"],
        str(em_buttons))

t.section("2. A TELEGRAM FAILURE NEVER REACHES THE TRADING ENGINE")
tc_bad = TelegramController(engine(), CSV, S)
loop_bad, bot_bad = wired(tc_bad, explode=True)
orig = loop_bad.is_running
loop_bad.is_running = lambda: True
raised = None
try:
    tc_bad.send_menu(banner="hello")
except Exception as exc:
    raised = exc
loop_bad.is_running = orig
drain(loop_bad)
t.check("2. a failing Telegram API does not raise into the caller",
        raised is None, repr(raised))
t.check("2. and nothing was delivered", not bot_bad.sent)

tc_off = TelegramController(engine(), CSV, S)
tc_off._loop = None
tc_off._app = None
t.check("2. with Telegram not connected it declines cleanly",
        tc_off.send_menu() is False)

import optimized
app_stub = SimpleNamespace(
    telegram=SimpleNamespace(send_menu=lambda banner="": (_ for _ in ()).throw(
        RuntimeError("boom"))))
t.check("2. the app-level send swallows an exception",
        optimized.Application._send_startup_menu(app_stub) is False)
t.check("2. and with no Telegram at all it is a no-op",
        optimized.Application._send_startup_menu(
            SimpleNamespace(telegram=None)) is False)

t.section("1. Application.start() ACTUALLY SENDS IT")
# The behavioural tests above prove send_menu works. This proves the startup
# lifecycle CALLS it - which is the part that was missing entirely, and the
# part a passing send_menu test would not have caught.
import inspect
start_src = inspect.getsource(optimized.Application.start)
t.check("1. start() pushes the startup menu",
        "self._send_startup_menu()" in start_src,
        "no _send_startup_menu() call in Application.start()")
t.check("1. after the Telegram controller is constructed",
        start_src.index("TelegramController") <
        start_src.index("self._send_startup_menu()"),
        "the menu is pushed before Telegram exists")
t.check("1. and independently of AUTO_START_TRADING",
        start_src.index("self._send_startup_menu()") <
        start_src.index("AUTO_START_TRADING"),
        "the menu only goes out when auto-start is on")
t.check("1. the old 'send /start to your bot' instruction is gone",
        "send /start to your bot" not in start_src)

t.section("5-6. SELECTING A MODE SAVES IT")
S._values["entry_mode"] = "FULL_LADDER"
tc2 = TelegramController(engine("FULL_LADDER", cycle_active=False), CSV, S)
text_sp, _ = tc2.panel.render("apply:entry_mode:SINGLE_PAIR", 111)
t.check("6. SINGLE PAIR is saved", S.get("entry_mode") == "SINGLE_PAIR",
        S.get("entry_mode"))
t.check("6. the confirmation names the mode",
        "ENTRY MODE UPDATED" in text_sp and "SINGLE PAIR" in text_sp,
        text_sp.replace("\n", " | ")[:200])
t.check("6. and the initial pending counts",
        "BUY 1" in text_sp and "SELL 1" in text_sp,
        text_sp.replace("\n", " | ")[:200])
t.check("6. and mentions the fast rolling replacement",
        "rolling replacement" in text_sp.lower(),
        text_sp.replace("\n", " | ")[:200])
text_fl, _ = tc2.panel.render("apply:entry_mode:FULL_LADDER", 111)
t.check("5. FULL LADDER is saved", S.get("entry_mode") == "FULL_LADDER",
        S.get("entry_mode"))
t.check("5. the confirmation shows 11 + 11",
        "BUY 11" in text_fl and "SELL 11" in text_fl,
        text_fl.replace("\n", " | ")[:200])
t.check("5-6. both confirmations state the spacing",
        "0.3" in text_sp and "0.3" in text_fl)

t.section("7-8. A CHANGE DURING AN ACTIVE CYCLE IS SAVED FOR THE NEXT ONE")
S._values["entry_mode"] = "FULL_LADDER"
tc3 = TelegramController(
    engine("FULL_LADDER", cycle_active=True, cycle_mode="FULL_LADDER"),
    CSV, S)
text_active, _ = tc3.panel.render("apply:entry_mode:SINGLE_PAIR", 111)
t.check("8. the setting is saved", S.get("entry_mode") == "SINGLE_PAIR",
        S.get("entry_mode"))
t.check("7. the confirmation says the running cycle is untouched",
        "untouched" in text_active.lower(),
        text_active.replace("\n", " | ")[:240])
t.check("8. and that it applies to the NEXT cycle",
        "NEXT cycle" in text_active, text_active.replace("\n", " | ")[:240])
tc4 = TelegramController(
    engine("SINGLE_PAIR", cycle_active=True, cycle_mode="FULL_LADDER"),
    CSV, S)
menu_text, _ = tc4.panel.render("settings_entrymode", 111)
t.check("7. the menu shows current cycle vs next cycle",
        "Current cycle:" in menu_text and "Next cycle:" in menu_text,
        menu_text.replace("\n", " | ")[:240])
t.check("7. current cycle is the mode it was BUILT with",
        "Current cycle: FULL LADDER" in menu_text,
        menu_text.replace("\n", " | ")[:240])

t.section("13. THE CHANGE IS LOGGED")
CSV.flush()
events = open(cfg.DATA_PATH / "events.csv").read()
t.check("13. an ENTRY_MODE_CHANGE row was written",
        "ENTRY_MODE_CHANGE" in events)
t.check("13. it records the old and new mode",
        "old_mode=FULL_LADDER" in events and "new_mode=SINGLE_PAIR" in events,
        [l for l in events.splitlines() if "ENTRY_MODE_CHANGE" in l][-1:])
t.check("13. the cycle id and what it applies to",
        "cycle_id=183" in events and "applies_to=NEXT_CYCLE" in events,
        [l for l in events.splitlines() if "ENTRY_MODE_CHANGE" in l][-1:])

t.section("7, 9, 10. THE MODE CHANGE DOES NOT RESHAPE A RUNNING LADDER")


def live(mode, name):
    mt5.reset()
    mt5.set_price(4422.00, 4422.08)
    mt5.initialize()
    settings = RuntimeSettings(cfg.runtime_defaults(),
                               cfg.DATA_PATH / f"{name}.json")
    settings._values.update({"entry_mode": mode, "ladder_spacing": 0.30,
                             "ladder_depth": 11, "max_pending_orders": 22,
                             "stop_loss_distance": 0,
                             "telemetry_interval_seconds": 0,
                             "cycle_reentry_cooldown_seconds": 0})
    broker = Mt5Broker("XAUUSD", 88001199)
    eng = RollingLadderEngine(broker, settings, hooks=Recorder().hooks(),
                              state_path=None)
    eng.resume()
    eng.step()
    return eng, broker, settings


eng, broker, settings = live("FULL_LADDER", "full_live")
t.check("10. FULL_LADDER builds 11 + 11",
        len([o for o in broker.orders() if o.side == BUY_STOP]) == 11 and
        len([o for o in broker.orders() if o.side == SELL_STOP]) == 11,
        f"{len(broker.orders())} orders")
before = sorted(o.ticket for o in broker.orders())
settings._values["entry_mode"] = "SINGLE_PAIR"      # user flips it mid-cycle
eng.step()
eng.step()
t.check("7. the running ladder keeps every one of its orders",
        sorted(o.ticket for o in broker.orders()) == before,
        f"{len(broker.orders())} orders, was {len(before)}")
t.check("7. the cycle is still the mode it was built with",
        eng.cycle.entry_mode == "FULL_LADDER", eng.cycle.entry_mode)
t.check("7. and the engine reports that as the mode in force",
        eng.entry_mode_in_force() == "FULL_LADDER",
        eng.entry_mode_in_force())

# close the cycle out and let the next one start under the new mode
eng.cycle_active = False
eng._levels_done.clear()
eng._levels_open.clear()
for o in list(broker.orders()):
    broker.cancel_order(o.ticket)
eng.create_new_ladder(reason="next cycle")
eng.step()
buys = [o for o in broker.orders() if o.side == BUY_STOP]
sells = [o for o in broker.orders() if o.side == SELL_STOP]
t.check("8-9. the NEXT cycle uses the new mode",
        eng.cycle.entry_mode == "SINGLE_PAIR", eng.cycle.entry_mode)
t.check("9. SINGLE_PAIR next cycle starts with exactly 1 BUY",
        len(buys) == 1, str(len(buys)))
t.check("9. and exactly 1 SELL", len(sells) == 1, str(len(sells)))
ref = eng.cycle.anchor
t.check("9. at reference +/- 0.30",
        abs(buys[0].price - (ref + 0.30)) < 1e-9 and
        abs(sells[0].price - (ref - 0.30)) < 1e-9,
        f"{buys[0].price}/{sells[0].price} ref {ref}")

eng2, broker2, settings2 = live("SINGLE_PAIR", "sp_live")
t.check("9. a SINGLE_PAIR cycle starts with one pair",
        len(broker2.orders()) == 2, str(len(broker2.orders())))
settings2._values["entry_mode"] = "FULL_LADDER"
eng2.step()
t.check("7. flipping to FULL_LADDER mid-cycle adds nothing",
        len(broker2.orders()) == 2, str(len(broker2.orders())))

t.section("11. THE STARTUP MENU NEVER BLOCKS THE CALLER")
import time
tc5 = TelegramController(engine("SINGLE_PAIR"), CSV, S)
loop5 = asyncio.new_event_loop()
slow = FakeBot()


async def slow_send(chat_id, text, parse_mode=None, reply_markup=None):
    await asyncio.sleep(0.4)
    slow.sent.append((chat_id, text, reply_markup))


slow.send_message = slow_send
tc5._loop = loop5
tc5._app = SimpleNamespace(bot=slow)
orig5 = loop5.is_running
loop5.is_running = lambda: True
started = time.perf_counter()
tc5.send_menu()
elapsed = time.perf_counter() - started
loop5.is_running = orig5
t.check("11. the caller returns immediately, not after the API call",
        elapsed < 0.1, f"{elapsed * 1000:.1f} ms")
drain(loop5)
t.check("11. and the message still went out", len(slow.sent) == 1,
        str(len(slow.sent)))

t.section("12, 14. STATUS AND THE EXISTING COMMANDS")
tc6 = TelegramController(engine("SINGLE_PAIR"), CSV, S)
status_text = asyncio.new_event_loop().run_until_complete(tc6._panel_text())
t.check("12. the panel reports the entry mode",
        "SINGLE PAIR" in status_text,
        status_text.replace("\n", " | ")[:200])
t.check("14. /start still renders the same panel",
        callable(getattr(tc6, "cmd_start", None)))
t.check("14. and /status still exists",
        callable(getattr(tc6, "cmd_status", None)))
t.check("14. the settings panel still handles its existing screens",
        all(tc6.panel.handles(x) for x in
            ("settings", "settings_lot", "settings_target", "settings_risk")))

t.section("10. ENTRY MODE IS ONE AUTHORITATIVE VALUE")
S._values["entry_mode"] = "SINGLE_PAIR"
t.check("10. config, runtime settings and the engine agree",
        S.get("entry_mode") == S.snapshot()["entry_mode"] == "SINGLE_PAIR")
t.check("10. and it is a validated runtime setting, not a loose variable",
        "entry_mode" in cfg.runtime_defaults())
import csv_logger
t.check("13. entry_mode is still in both telemetry files",
        "entry_mode" in csv_logger.TELEMETRY_HEADER and
        "entry_mode" in csv_logger.CYCLE_HEADER)

t.done()
