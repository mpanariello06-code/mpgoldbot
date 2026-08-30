"""RuntimeSettings for the ladder: defaults, validation, persistence, reset."""
import json
import pathlib
import shutil
import threading

from harness import Suite, use_stub_mt5
use_stub_mt5()

import config as cfg
from runtime_settings import RuntimeSettings, SettingError

t = Suite("settings")
TMP = pathlib.Path("/tmp/settings_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
PATH = TMP / "runtime_settings.json"

t.section("DEFAULTS (the values from .env / the spec)")
rs = RuntimeSettings(cfg.runtime_defaults(), PATH)
s = rs.snapshot()
t.check("ladder defaults",
        (s["ladder_spacing"], s["ladder_depth"], s["tp_mode"], s["tp_levels"],
         s["lot_size"]) == (0.30, 5, "levels", 1, 0.01),
        str({k: s[k] for k in ("ladder_spacing", "ladder_depth", "tp_mode",
                               "tp_levels", "lot_size")}))
t.check("a 1-level TP equals the ladder spacing",
        s["tp_levels"] * s["ladder_spacing"] == 0.30)
t.check("risk defaults",
        (s["max_open_positions"], s["max_pending_orders"], s["max_spread"],
         s["direction_filter"]) == (4, 10, 0.50, "off"),
        str({k: s[k] for k in ("max_open_positions", "max_pending_orders",
                               "max_spread", "direction_filter")}))
t.check("TP mode default is ladder levels", s["tp_mode"] == "levels")
t.check("adaptive exit defaults",
        (s["exit_threshold_exit"], s["exit_threshold_monitor"],
         s["exit_w_reversal"]) == (70.0, 40.0, 0.75),
        str({k: v for k, v in s.items() if k.startswith("exit_")}))
t.check("no trade-count or dollar exit setting exists",
        not any(k in s for k in ("profit_cycle_target", "cycle_take_profit_money",
                                 "target_profit_usd")))
t.check("roll mode default is rolling", s["roll_mode"] == "extend")
t.check("no martingale knobs exist",
        not any("martingale" in k or "multiplier" in k for k in s))
t.check("json created on first run", PATH.exists())
t.check("no secrets in the json",
        not any(k in json.load(open(PATH)) for k in
                ("MT5_PASSWORD", "TELEGRAM_BOT_TOKEN", "password", "token")))

t.section("VALIDATION")
for key, bad in [("ladder_spacing", "abc"), ("ladder_spacing", 0),
                 ("ladder_depth", 0), ("ladder_depth", 500),
                 ("tp_distance", -1), ("lot_size", 0),
                 ("max_open_positions", 0), ("tp_mode", "6_pips"),
                 ("roll_mode", "sideways"), ("direction_filter", "maybe"),
                 ("exit_threshold_exit", 0), ("max_spread", "wide"),
                 ("cycle_close_positions", "perhaps"),
                 ("cooldown_after_loss_minutes", 99999)]:
    t.raises(f"reject {key}={bad!r}", SettingError, rs.set, key, bad)
t.check("nothing was applied by the rejected values",
        rs.snapshot() == s, "settings unchanged")

t.raises("lot above max lot is refused", SettingError, rs.set, "lot_size", 5.0)
t.raises("max lot below lot is refused", SettingError, rs.set, "max_lot_size", 0.001)
t.raises("exit score below the monitor score is refused", SettingError,
         rs.set, "exit_threshold_exit", 10.0)
t.raises("monitor score above the exit score is refused", SettingError,
         rs.set, "exit_threshold_monitor", 90.0)
t.check("lot/max lot unchanged after the rejects",
        (rs.get("lot_size"), rs.get("max_lot_size")) == (0.01, 0.10))

t.section("APPLY + PERSIST")
changed, msg, old, new = rs.set("tp_mode", "2_pips")
t.check("value applied", changed and rs.get("tp_mode") == "2_pips", msg)
t.check("persisted", json.load(open(PATH))["tp_mode"] == "2_pips")
t.check("re-setting the same value is a no-op", not rs.set("tp_mode", "2_pips")[0])
rs.set("ladder_spacing", 0.5)
rs.set("max_open_positions", 8)
t.check("floats and ints keep their type",
        isinstance(rs.get("ladder_spacing"), float) and
        isinstance(rs.get("max_open_positions"), int))
t.check("booleans parse from text", rs.set("m5_candle_reset", "true")[0] and
        rs.get("m5_candle_reset") is True)

rs2 = RuntimeSettings(cfg.runtime_defaults(), PATH)
t.check("settings survive a restart",
        rs2.get("tp_mode") == "2_pips" and rs2.get("ladder_spacing") == 0.5)

t.section("DISPLAY")
t.check("prices shown plainly", RuntimeSettings.display("ladder_spacing", 0.30) == "0.3")
t.check("money shown with a currency",
        RuntimeSettings.display("max_daily_drawdown", 50) == "$50.00")
t.check("zero money reads as OFF",
        RuntimeSettings.display("max_cycle_drawdown", 0) == "OFF")
t.check("booleans read as ON/OFF", RuntimeSettings.display("m5_candle_reset", True) == "ON")
t.check("tp mode labelled", RuntimeSettings.display("tp_mode", "3_pips") == "3 PIPS")
t.check("roll mode labelled", RuntimeSettings.display("roll_mode", "extend") == "ROLLING")
t.check("direction labelled",
        "BUY" in RuntimeSettings.display("direction_filter", "buy_bias"))
t.check("pip size auto", RuntimeSettings.display("pip_points", 0) == "AUTO")

t.section("CONFIRMATION FLAGS")
for key in ("tp_mode", "tp_levels", "tp_distance", "lot_size", "ladder_spacing",
            "ladder_depth", "max_open_positions", "stop_loss_distance",
            "max_daily_drawdown", "exit_threshold_exit", "direction_filter"):
    t.check(f"{key} needs confirmation", RuntimeSettings.needs_confirmation(key))
for key in ("max_spread", "pip_points", "order_max_age_seconds",
            "cooldown_after_loss_minutes", "exit_w_reversal",
            "exit_threshold_monitor"):
    t.check(f"{key} applies directly", not RuntimeSettings.needs_confirmation(key))

t.section("RESET")
changed = rs.reset()
s2 = rs.snapshot()
t.check("reset restores every default", s2 == cfg.runtime_defaults(), str(changed))
t.check("reset persisted", json.load(open(PATH))["ladder_spacing"] == 0.30)

t.section("CORRUPT / UNKNOWN DATA")
open(PATH, "w").write("{not json")
t.check("corrupt json falls back to defaults",
        RuntimeSettings(cfg.runtime_defaults(), PATH).get("ladder_spacing") == 0.30)
json.dump({"ladder_spacing": 0.4, "bogus": 1, "ladder_depth": "many"},
          open(PATH, "w"))
rs3 = RuntimeSettings(cfg.runtime_defaults(), PATH)
problems = rs3.load()
t.check("good values load", rs3.get("ladder_spacing") == 0.4)
t.check("unknown keys ignored", "bogus" not in rs3.snapshot())
t.check("bad values fall back", rs3.get("ladder_depth") == 5)
t.check("problems reported", any("depth" in p.lower() for p in problems), str(problems))
json.dump({"lot_size": 5.0, "max_lot_size": 0.10}, open(PATH, "w"))
rs4 = RuntimeSettings(cfg.runtime_defaults(), PATH)
t.check("impossible lot/max combination repaired on load",
        rs4.get("lot_size") <= rs4.get("max_lot_size"))

t.section("THREAD SAFETY")
rs5 = RuntimeSettings(cfg.runtime_defaults(), TMP / "concurrent.json")
stop = threading.Event()
bad = []


def writer():
    i = 0
    while not stop.is_set():
        rs5.set("ladder_spacing", 0.2 if i % 2 else 0.4)
        i += 1


def reader():
    keys = set(cfg.runtime_defaults())
    while not stop.is_set():
        snap = rs5.snapshot()
        if snap["ladder_spacing"] not in (0.2, 0.4, 0.30):
            bad.append(snap["ladder_spacing"])
        if set(snap) != keys:
            bad.append("partial snapshot")


threads = [threading.Thread(target=writer), threading.Thread(target=reader),
           threading.Thread(target=reader)]
[th.start() for th in threads]
threading.Event().wait(1.0)
stop.set()
[th.join() for th in threads]
t.check("concurrent reads never see a half-updated snapshot", not bad, str(bad[:3]))

t.done()
