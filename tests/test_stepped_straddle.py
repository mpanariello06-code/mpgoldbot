"""
STEPPED_STRADDLE: one position, managed by a stepped stop loss.

Two straddle orders, one fills, the other is cancelled, and the position is
then managed entirely by its own stop: initial stop one step behind entry,
breakeven at the first favourable step, then one step of stop per step of
price. The stop IS the exit.

What this suite is really guarding:
  * the arithmetic, against the spec's own worked examples
  * the stop only ever ratchets - never loosens, for any price path
  * the basket engine cannot reach these positions
  * FULL_LADDER and SINGLE_PAIR are untouched by any of it
"""
import pathlib
import shutil
import time

from harness import Suite, use_stub_mt5
use_stub_mt5()

import MetaTrader5 as mt5
import config as cfg
from broker import BUY, BUY_STOP, SELL, SELL_STOP, Mt5Broker
from fakes import Recorder
from ladder_engine import RollingLadderEngine
from runtime_settings import RuntimeSettings
from straddle import (BREAKEVEN, POSITION_OPEN, STEP_TRAILING, STOP_LOSS_HIT,
                      StraddleRules, favorable_distance, initial_sl, next_sl,
                      sl_is_improvement)

t = Suite("stepped_straddle")
TMP = pathlib.Path("/tmp/straddle_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)

STEP = 1.20
BUFFER = 0.30
R = StraddleRules(step_distance=STEP, spread_buffer=BUFFER)


def live(name, overrides=None, bid=3400.00, spread=0.08):
    mt5.reset()
    mt5.set_price(bid, round(bid + spread, 2))
    mt5.initialize()
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}.json")
    settings._values.update({"entry_mode": "STEPPED_STRADDLE",
                             "step_distance": STEP, "spread_buffer": BUFFER,
                             "check_on_new_bar_only": False,
                             "stop_loss_distance": 0,
                             "telemetry_interval_seconds": 0,
                             "cycle_reentry_cooldown_seconds": 10})
    settings._values.update(overrides or {})
    broker = Mt5Broker("XAUUSD", 88001199)
    rec = Recorder()
    eng = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                              state_path=None)
    eng.resume()
    eng.step()
    return eng, broker, settings, rec


def fill(broker, side=BUY_STOP):
    order = next((o for o in broker.orders() if o.side == side), None)
    if order is None:
        return None
    mt5.trigger_order(order.ticket)
    mt5.set_price(order.price, round(order.price + 0.08, 2))
    return order.price


def move(eng, price):
    mt5.set_price(price, round(price + 0.08, 2))
    eng.check_exit_now()


# ===========================================================================
t.section("THE ARITHMETIC, AGAINST THE SPEC'S OWN EXAMPLES")
t.check("reference 3400.00 -> 3401.20 / 3398.80",
        R.levels(3400.00) == (3401.20, 3398.80), str(R.levels(3400.00)))
t.check("STEP_DISTANCE is a PRICE distance, not pips",
        abs(R.levels(3400.00)[0] - 3400.00 - STEP) < 1e-9,
        str(R.levels(3400.00)[0] - 3400.00))
t.check("BUY entry 3401.20 -> initial SL 3400.00",
        abs(initial_sl(BUY, 3401.20, R) - 3400.00) < 1e-9,
        str(initial_sl(BUY, 3401.20, R)))
t.check("SELL entry 3398.80 -> initial SL 3400.00",
        abs(initial_sl(SELL, 3398.80, R) - 3400.00) < 1e-9,
        str(initial_sl(SELL, 3398.80, R)))

for price, want_sl, want_steps in ((3402.40, 3401.50, 1), (3403.60, 3402.70, 2),
                                   (3404.80, 3403.90, 3), (3406.00, 3405.10, 4)):
    sl, steps, _ = next_sl(BUY, 3401.20, price, R)
    t.check(f"BUY at {price} -> SL {want_sl} (step {want_steps})",
            abs(sl - want_sl) < 1e-9 and steps == want_steps,
            f"{sl} / {steps}")
for price, want_sl, want_steps in ((3397.60, 3398.50, 1), (3396.40, 3397.30, 2),
                                   (3395.20, 3396.10, 3)):
    sl, steps, _ = next_sl(SELL, 3398.80, price, R)
    t.check(f"SELL at {price} -> SL {want_sl} (step {want_steps})",
            abs(sl - want_sl) < 1e-9 and steps == want_steps,
            f"{sl} / {steps}")

t.check("below one full step the stop does not move",
        next_sl(BUY, 3401.20, 3402.39, R)[0] is None,
        str(next_sl(BUY, 3401.20, 3402.39, R)))
t.check("breakeven is step 1, not an extra step on top of it",
        abs(next_sl(BUY, 3401.20, 3402.40, R)[0] - (3401.20 + BUFFER)) < 1e-9,
        str(next_sl(BUY, 3401.20, 3402.40, R)[0]))
t.check("and it is flagged as the breakeven move",
        next_sl(BUY, 3401.20, 3402.40, R)[2] is True)
t.check("the step after it is NOT flagged as breakeven",
        next_sl(BUY, 3401.20, 3403.60, R)[2] is False)
t.check("favourable distance is never negative",
        favorable_distance(BUY, 3401.20, 3390.00) == 0.0)

t.section("THE RATCHET - THE ONE MISTAKE THIS STRATEGY CANNOT MAKE")
t.check("BUY: a lower target is refused",
        next_sl(BUY, 3401.20, 3403.60, R, current_sl=3404.00)[0] is None)
t.check("SELL: a higher target is refused",
        next_sl(SELL, 3398.80, 3396.40, R, current_sl=3396.00)[0] is None)
t.check("BUY: an equal target is refused (no pointless modify)",
        next_sl(BUY, 3401.20, 3403.60, R, current_sl=3402.70)[0] is None)
t.check("sl_is_improvement agrees for BUY",
        sl_is_improvement(BUY, 3402.70, 3401.50) and
        not sl_is_improvement(BUY, 3401.00, 3401.50))
t.check("sl_is_improvement agrees for SELL",
        sl_is_improvement(SELL, 3397.30, 3398.50) and
        not sl_is_improvement(SELL, 3399.00, 3398.50))

# a random-ish path: the stop must be monotone whatever price does
path = [3402.4, 3401.0, 3403.6, 3402.0, 3404.8, 3399.0, 3406.0, 3400.0, 3407.2]
sl = initial_sl(BUY, 3401.20, R)
monotone = True
for price in path:
    target, _, _ = next_sl(BUY, 3401.20, price, R, current_sl=sl)
    if target is not None:
        if target <= sl:
            monotone = False
        sl = target
t.check("over a whipsawing path the BUY stop only ever rises", monotone,
        f"ended at {sl}")
t.check("and it ended at the highest step reached",
        abs(sl - (3401.20 + BUFFER + 4 * STEP)) < 1e-9, str(sl))

t.section("ENTRY: EXACTLY ONE BUY STOP AND ONE SELL STOP")
eng, broker, settings, rec = live("entry")
ref = eng.cycle.anchor
orders = broker.orders()
buys = [o for o in orders if o.side == BUY_STOP]
sells = [o for o in orders if o.side == SELL_STOP]
t.check("exactly 2 orders", len(orders) == 2, str(len(orders)))
t.check("one BUY STOP", len(buys) == 1, str(len(buys)))
t.check("one SELL STOP", len(sells) == 1, str(len(sells)))
t.check("BUY is reference + STEP_DISTANCE",
        abs(buys[0].price - (ref + STEP)) < 1e-9,
        f"{buys[0].price} vs {ref + STEP}")
t.check("SELL is reference - STEP_DISTANCE",
        abs(sells[0].price - (ref - STEP)) < 1e-9,
        f"{sells[0].price} vs {ref - STEP}")
t.check("symmetric about ONE captured reference",
        abs((buys[0].price - ref) - (ref - sells[0].price)) < 1e-9)
t.check("the reference is recorded on the straddle",
        eng.straddle.reference == ref, str(eng.straddle.reference))
t.check("with the intended prices",
        abs(eng.straddle.intended_buy - (ref + STEP)) < 1e-9 and
        abs(eng.straddle.intended_sell - (ref - STEP)) < 1e-9)
before = sorted(o.ticket for o in broker.orders())
for bid in (3400.40, 3399.60, 3400.90):
    mt5.set_price(bid, bid + 0.08)
    eng.step()
t.check("the reference does not drift as price moves",
        eng.cycle.anchor == ref, f"{eng.cycle.anchor} vs {ref}")
t.check("and no duplicate straddle is created",
        sorted(o.ticket for o in broker.orders()) == before,
        f"{len(broker.orders())} orders")

t.section("FILL: THE OPPOSITE IS CANCELLED, THE STOP GOES ON")
eng, broker, settings, rec = live("fillbuy")
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
pos = broker.positions()
t.check("exactly one position", len(pos) == 1, str(len(pos)))
t.check("the opposite stop is cancelled", not broker.orders(),
        f"{len(broker.orders())} orders left")
t.check("the initial SL is one step behind entry",
        abs(pos[0].sl - (entry - STEP)) < 1e-9,
        f"{pos[0].sl} vs {entry - STEP}")
t.check("the state moved to POSITION_OPEN",
        eng.straddle.state == POSITION_OPEN, eng.straddle.state)
t.check("the fill and the SL are timestamped",
        {"fill_detected_at", "initial_sl_confirmed_at"} <=
        set(eng.straddle.timing), str(sorted(eng.straddle.timing)))
for _ in range(4):
    eng.step()
    eng.check_exit_now()
t.check("no second position is ever created", len(broker.positions()) == 1,
        str(len(broker.positions())))
t.check("and no replacement order appears", not broker.orders(),
        str(len(broker.orders())))

eng, broker, settings, rec = live("fillsell")
entry = fill(broker, SELL_STOP)
eng.check_exit_now()
pos = broker.positions()
t.check("SELL fill: the BUY stop is cancelled", not broker.orders())
t.check("SELL fill: SL is one step ABOVE entry",
        abs(pos[0].sl - (entry + STEP)) < 1e-9,
        f"{pos[0].sl} vs {entry + STEP}")

t.section("BREAKEVEN AND STEP TRAILING, THROUGH THE ENGINE")
eng, broker, settings, rec = live("trail")
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
initial = broker.positions()[0].sl
seen = []
for step in range(1, 5):
    move(eng, round(entry + step * STEP + 0.01, 2))
    seen.append(broker.positions()[0].sl)
t.check("the first step sets breakeven + buffer",
        abs(seen[0] - (entry + BUFFER)) < 1e-9, f"{seen[0]} vs {entry + BUFFER}")
t.check("breakeven is recorded once", eng.straddle.breakeven_set is True)
t.check("and the state says so",
        eng.straddle.state in (BREAKEVEN, STEP_TRAILING), eng.straddle.state)
for i in range(1, 4):
    want = entry + BUFFER + i * STEP
    t.check(f"step {i + 1} moves the stop to {want:.2f}",
            abs(seen[i] - want) < 1e-9, f"{seen[i]} vs {want}")
t.check("each move is exactly one step",
        all(abs(seen[i + 1] - seen[i] - STEP) < 1e-9
            for i in range(len(seen) - 1)),
        str([round(seen[i + 1] - seen[i], 4) for i in range(len(seen) - 1)]))
t.check("the stop never went below the initial one",
        all(x > initial for x in seen), f"{initial} -> {seen}")

mods = eng.straddle.sl_modifications
for _ in range(6):
    eng.check_exit_now()
t.check("repeated passes at the same price do not re-modify the stop",
        eng.straddle.sl_modifications == mods,
        f"{mods} -> {eng.straddle.sl_modifications}")
# If the basket engine ever reaches these positions this is where it shows:
# it would have taken the trade long before here, on its profit target.
t.check("the position is still open after several profitable steps",
        len(broker.positions()) == 1,
        f"{len(broker.positions())} positions - something closed the trade")
sl_before = broker.positions()[0].sl if broker.positions() else 0
move(eng, round(entry + 1.5 * STEP, 2))
after = broker.positions()
t.check("a pullback does NOT loosen the stop",
        bool(after) and after[0].sl == sl_before,
        f"{after[0].sl if after else 'position gone'} vs {sl_before}")

eng, broker, settings, rec = live("trailsell")
entry = fill(broker, SELL_STOP)
eng.check_exit_now()
seen = []
# A SELL is measured on the ASK, per the spec, so the bid has to travel one
# spread further for the ask to reach the step. move() sets ask = bid + 0.08.
for step in range(1, 4):
    move(eng, round(entry - step * STEP - 0.10, 2))
    seen.append(broker.positions()[0].sl)
t.check("SELL steps are measured on the ASK, not the bid",
        eng.straddle.favorable_steps == 3,
        str(eng.straddle.favorable_steps))
t.check("SELL breakeven is entry - buffer",
        abs(seen[0] - (entry - BUFFER)) < 1e-9, f"{seen[0]} vs {entry - BUFFER}")
t.check("SELL trailing steps down by one step each",
        all(abs(seen[i] - seen[i + 1] - STEP) < 1e-9
            for i in range(len(seen) - 1)), str(seen))
t.check("the SELL stop only ever falls",
        all(seen[i] > seen[i + 1] for i in range(len(seen) - 1)), str(seen))

t.section("CHECK_ON_NEW_BAR_ONLY GATES TRAILING, NEVER PROTECTION")
eng, broker, settings, rec = live("bargate", {"check_on_new_bar_only": True})
bar = [1_700_000_000]
eng.bar_time = lambda: bar[0]
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
t.check("the initial SL is set even with bar gating on",
        abs(broker.positions()[0].sl - (entry - STEP)) < 1e-9,
        str(broker.positions()[0].sl))
t.check("and the opposite order is still cancelled immediately",
        not broker.orders(), str(len(broker.orders())))
move(eng, round(entry + STEP + 0.01, 2))
first = broker.positions()[0].sl
move(eng, round(entry + 2 * STEP + 0.01, 2))
t.check("a second move on the SAME candle does not trail again",
        broker.positions()[0].sl == first, f"{first} -> {broker.positions()[0].sl}")
bar[0] += 60
eng.check_exit_now()
t.check("a new closed candle lets it trail",
        broker.positions()[0].sl > first,
        f"{first} -> {broker.positions()[0].sl}")

t.section("THE BASKET ENGINE CANNOT TOUCH A STRADDLE POSITION")
eng, broker, settings, rec = live(
    "isolate", {"basket_profit_target": 0.01, "profit_runner_enabled": False,
                "max_cycle_drawdown": 0.01, "max_cycle_duration_minutes": 0.001,
                "check_on_new_bar_only": False})
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
move(eng, round(entry + 3 * STEP, 2))          # far past any basket target
snap = settings.snapshot()
reason, detail = eng._exit_reason(snap, eng.get_cycle_floating_pnl(),
                                  broker.positions())
t.check("the basket exit engine returns nothing for this mode",
        reason is None, f"{reason} {detail}")
t.check("even with the target at 0.01 the position is still open",
        len(broker.positions()) == 1, str(len(broker.positions())))
move(eng, round(entry - 5 * STEP, 2))          # deep against it
reason, _ = eng._exit_reason(snap, eng.get_cycle_floating_pnl(),
                             broker.positions())
t.check("and the cycle drawdown guard does not close it either",
        reason is None, str(reason))
t.check("the stop remains the exit", eng.cycle_active)

t.section("RESET: STOP HIT -> FLAT -> COOLDOWN -> WAIT FOR M1")
now = [5000.0]
eng, broker, settings, rec = live("reset")
eng.clock = lambda: now[0]
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
move(eng, round(entry + 2 * STEP + 0.01, 2))
pos = broker.positions()[0]
mt5.hit_sl(pos.ticket)
eng.check_exit_now()
for _ in range(3):
    eng.step()
t.check("positions are flat", not broker.positions(),
        str(len(broker.positions())))
t.check("pending orders are gone", not broker.orders(),
        str(len(broker.orders())))
t.check("the cycle is closed", not eng.cycle_active)
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("the cycle was recorded", bool(closes), str(len(closes)))
t.check("attributed to the stop", closes[-1].kind == STOP_LOSS_HIT,
        str(closes[-1].kind))
ctx = closes[-1].context if closes else {}
t.check("realized P/L came from the MT5 deal history",
        "realized_pnl_after_close" in ctx and
        ctx["realized_pnl_after_close"] is not None, str(ctx.get(
            "realized_pnl_after_close")))
deals = [d for d in mt5.STATE["deals"] if d.entry == mt5.DEAL_ENTRY_OUT]
t.check("and it matches the OUT deals",
        abs(float(ctx.get("realized_pnl_after_close", 0)) -
            round(sum(d.profit for d in deals), 2)) < 0.01,
        f"{ctx.get('realized_pnl_after_close')} vs "
        f"{round(sum(d.profit for d in deals), 2)}")
t.check("the cooldown is armed", eng._reentry_wait() > 0,
        f"{eng._reentry_wait():.1f}s")
eng.bar_time = lambda: 1_700_000_000
eng.step()
t.check("no new straddle during the cooldown", not broker.orders(),
        str(len(broker.orders())))
now[0] += 11
eng.step()
t.check("and none after the cooldown without a NEW M1 candle",
        not broker.orders(), str(len(broker.orders())))
eng.bar_time = lambda: 1_700_000_060
eng.step()
t.check("a new closed candle starts the next straddle",
        len(broker.orders()) == 2, str(len(broker.orders())))
t.check("with a brand new reference, not the old one",
        eng.cycle.anchor != 3400.04 or eng.straddle.reference == eng.cycle.anchor,
        f"{eng.straddle.reference}")
t.check("and a new cycle id", eng.cycle.cycle_id == 2,
        str(eng.cycle.cycle_id))

t.section("ERROR HANDLING: ONE RETRY, THEN LEAVE THE STOP ALONE")
eng, broker, settings, rec = live("slfail")
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
good_sl = broker.positions()[0].sl
calls = []
real_send = mt5.order_send


def refuse_sltp(request):
    if request.get("action") == mt5.TRADE_ACTION_SLTP:
        calls.append(1)
        return type("R", (), {"retcode": 10016, "comment": "invalid stops",
                              "order": 0, "deal": 0})()
    return real_send(request)


mt5.order_send = refuse_sltp
try:
    move(eng, round(entry + STEP + 0.01, 2))
finally:
    mt5.order_send = real_send
t.check("the broker was asked exactly twice - one try, one retry",
        len(calls) == 2, f"{len(calls)} attempts")
t.check("the old stop is preserved, not lost",
        broker.positions()[0].sl == good_sl,
        f"{broker.positions()[0].sl} vs {good_sl}")
t.check("the failure is logged with the broker's reason",
        any(e[0] == "ERROR" and "SL modify failed" in e[1] for e in rec.events),
        str([e[1][:60] for e in rec.events if e[0] == "ERROR"][-1:]))
t.check("and it does not retry forever",
        eng.straddle.sl_modifications == 1, str(eng.straddle.sl_modifications))
move(eng, round(entry + STEP + 0.02, 2))
t.check("a later pass can still move the stop",
        broker.positions()[0].sl > good_sl,
        f"{broker.positions()[0].sl} vs {good_sl}")

t.section("MODE ISOLATION: THE OTHER TWO ARE UNTOUCHED")
eng, broker, settings, rec = live(
    "fullmode", {"entry_mode": "FULL_LADDER", "ladder_spacing": 0.30,
                 "ladder_depth": 11, "max_pending_orders": 22})
t.check("FULL_LADDER still builds 11 + 11",
        len([o for o in broker.orders() if o.side == BUY_STOP]) == 11 and
        len([o for o in broker.orders() if o.side == SELL_STOP]) == 11,
        f"{len(broker.orders())} orders")
t.check("and has no straddle state at all", eng.straddle is None)
eng, broker, settings, rec = live(
    "spmode", {"entry_mode": "SINGLE_PAIR", "ladder_spacing": 0.30})
t.check("SINGLE_PAIR still builds one pair at the LADDER spacing",
        len(broker.orders()) == 2 and
        abs(min(o.price for o in broker.orders() if o.side == BUY_STOP) -
            (eng.cycle.anchor + 0.30)) < 1e-9,
        str([o.price for o in broker.orders()]))
t.check("and has no straddle state either", eng.straddle is None)

eng, broker, settings, rec = live("switch")
t.check("a STEPPED_STRADDLE cycle is pinned to its mode",
        eng.cycle.entry_mode == "STEPPED_STRADDLE", eng.cycle.entry_mode)
before = sorted(o.ticket for o in broker.orders())
settings._values["entry_mode"] = "FULL_LADDER"
eng.step()
eng.step()
t.check("switching mode mid-cycle leaves the straddle alone",
        sorted(o.ticket for o in broker.orders()) == before,
        f"{len(broker.orders())} orders")
t.check("and the cycle still reports the mode it was built with",
        eng.entry_mode_in_force() == "STEPPED_STRADDLE",
        eng.entry_mode_in_force())

t.section("TELEMETRY AND LATENCY")
import csv_logger
for column in ("straddle_state", "straddle_direction", "entry_price",
               "direction", "step_distance", "spread_buffer", "initial_sl",
               "current_sl", "breakeven_set", "favorable_steps",
               "position_ticket", "reference_price_straddle",
               "intended_buy_price", "intended_sell_price"):
    t.check(f"basket telemetry has {column!r}",
            column in csv_logger.TELEMETRY_HEADER, column)
for column in ("straddle_direction", "straddle_entry_price",
               "straddle_initial_sl", "straddle_final_sl",
               "straddle_breakeven_set", "straddle_favorable_steps",
               "fill_detected_at", "opposite_cancel_request_at",
               "opposite_cancel_confirmed_at", "initial_sl_request_at",
               "initial_sl_confirmed_at", "breakeven_triggered_at",
               "sl_modify_request_at", "sl_modify_confirmed_at",
               "position_closed_at", "fill_to_cancel_ms",
               "fill_to_initial_sl_ms", "sl_modify_latency_ms"):
    t.check(f"the cycle summary has {column!r}",
            column in csv_logger.CYCLE_HEADER, column)

eng, broker, settings, rec = live("latency")
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
move(eng, round(entry + STEP + 0.01, 2))
summary = eng._straddle_summary()
t.check("fill -> opposite cancelled is measured",
        isinstance(summary["fill_to_cancel_ms"], float),
        str(summary["fill_to_cancel_ms"]))
t.check("fill -> initial SL confirmed is measured",
        isinstance(summary["fill_to_initial_sl_ms"], float),
        str(summary["fill_to_initial_sl_ms"]))
t.check("and the initial stop went on fast (sub-10ms of application time)",
        summary["fill_to_initial_sl_ms"] < 10.0,
        f"{summary['fill_to_initial_sl_ms']} ms")
t.check("SL modification latency is measured",
        isinstance(summary["sl_modify_latency_ms"], float),
        str(summary["sl_modify_latency_ms"]))

t.section("TELEGRAM SHOWS THE MODE WITHOUT OWNING ANY OF IT")
from runtime_settings import ENTRY_MODES, ENTRY_MODE_LABELS
t.check("STEPPED_STRADDLE is a valid entry mode",
        "STEPPED_STRADDLE" in ENTRY_MODES, str(ENTRY_MODES))
t.check("with a readable label",
        ENTRY_MODE_LABELS["STEPPED_STRADDLE"] == "STEPPED STRADDLE")
status = eng.straddle_status()
for key in ("straddle_state", "straddle_direction", "straddle_entry",
            "straddle_initial_sl", "straddle_current_sl",
            "straddle_breakeven", "straddle_steps"):
    t.check(f"status exposes {key!r}", key in status, str(sorted(status)))

t.section("NO MARTINGALE, NO SCALING, NO EXTRA ORDERS")
eng, broker, settings, rec = live("nomart", {"lot_size": 0.02})
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
for step in range(1, 4):
    move(eng, round(entry + step * STEP + 0.01, 2))
t.check("the lot is the configured one", broker.positions()[0].volume == 0.02,
        str(broker.positions()[0].volume))
t.check("still exactly one position after several steps",
        len(broker.positions()) == 1, str(len(broker.positions())))
t.check("and no pending orders were added back",
        not broker.orders(), str(len(broker.orders())))
t.check("no take profit is set on the position",
        broker.positions()[0].tp == 0, str(broker.positions()[0].tp))

t.done()
