"""
STEPPED_STRADDLE: a breakout straddle ridden by a ratcheting trailing stop.

    reference 4500.00
        BUY  STOP 4501.00  SL 4500.50
        SELL STOP 4499.00  SL 4499.50
    one fills -> cancel the other
    +0.50 in favour -> SL to breakeven (entry)
    +1.00 in favour -> trail arms, SL = best price seen -/+ 0.50
    every new extreme drags the stop; a pullback moves it nowhere
    stop hit -> flat -> 10s cooldown -> next closed M1 candle -> new straddle

There is no take profit and no basket. The trailing stop is the exit.

The numbers below are the spec's own worked example (section 20), used as the
reference for every assertion.
"""
import pathlib
import shutil

from harness import Suite, use_stub_mt5
use_stub_mt5()

import MetaTrader5 as mt5
import config as cfg
from broker import BUY, BUY_STOP, SELL, SELL_STOP, Mt5Broker
from fakes import Recorder
from ladder_engine import RollingLadderEngine
from runtime_settings import RuntimeSettings
from straddle import (BREAKEVEN, STEP_TRAILING, STOP_LOSS_HIT, StraddleRules,
                      favorable_distance, initial_sl, next_sl,
                      sl_is_improvement, update_water)

t = Suite("stepped_straddle")
TMP = pathlib.Path("/tmp/straddle_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)

REF = 4500.00
ENTRY_OFFSET = 1.00
SL_DIST = 0.50
BE_TRIGGER = 0.50
BE_OFFSET = 0.00
TRAIL_TRIGGER = 1.00
TRAIL_DIST = 0.50
R = StraddleRules(entry_offset=ENTRY_OFFSET, initial_sl_distance=SL_DIST,
                  breakeven_trigger=BE_TRIGGER, breakeven_offset=BE_OFFSET,
                  trail_trigger=TRAIL_TRIGGER, trail_distance=TRAIL_DIST)


def live(name, overrides=None, bid=REF, spread=0.00, stops_level=0):
    """A cycle on the real Mt5Broker. Spread 0 keeps the worked example exact."""
    mt5.reset()
    mt5.set_price(bid, round(bid + spread, 2))
    mt5.STATE["stops_level"] = stops_level
    mt5.initialize()
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}.json")
    settings._values.update({"entry_mode": "STEPPED_STRADDLE",
                             "entry_offset": ENTRY_OFFSET,
                             "initial_sl_distance": SL_DIST,
                             "breakeven_trigger": BE_TRIGGER,
                             "breakeven_offset": BE_OFFSET,
                             "trail_trigger": TRAIL_TRIGGER,
                             "trail_distance": TRAIL_DIST,
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
    mt5.set_price(order.price, order.price)
    return order.price


def move(eng, price):
    """One live price update - no candle, no ladder pass."""
    mt5.set_price(price, price)
    eng.check_exit_now()


def sl(broker):
    pos = broker.positions()
    return pos[0].sl if pos else None


# ===========================================================================
t.section("TEST 1-3. THE STRADDLE AND ITS PROTECTIVE STOPS")
t.check("1. reference 4500 -> BUY 4501.00 / SELL 4499.00",
        R.levels(REF) == (4501.00, 4499.00), str(R.levels(REF)))
t.check("2. BUY initial SL = 4500.50",
        abs(R.pending_sl(BUY, 4501.00) - 4500.50) < 1e-9,
        str(R.pending_sl(BUY, 4501.00)))
t.check("3. SELL initial SL = 4499.50",
        abs(R.pending_sl(SELL, 4499.00) - 4499.50) < 1e-9,
        str(R.pending_sl(SELL, 4499.00)))
t.check("the distances are independent of each other",
        R.entry_offset != R.initial_sl_distance and
        R.breakeven_trigger != R.trail_trigger)
t.check("0.50 means fifty cents of price, not pips",
        abs(R.levels(REF)[0] - REF - 1.00) < 1e-9 and
        abs(4501.00 - R.pending_sl(BUY, 4501.00) - 0.50) < 1e-9)

eng, broker, settings, rec = live("place")
ref = eng.cycle.anchor
orders = broker.orders()
buys = [o for o in orders if o.side == BUY_STOP]
sells = [o for o in orders if o.side == SELL_STOP]
t.check("exactly one BUY STOP and one SELL STOP",
        len(buys) == 1 and len(sells) == 1 and len(orders) == 2,
        f"{len(orders)} orders")
t.check("1. BUY STOP is reference + 1.00",
        abs(buys[0].price - (ref + ENTRY_OFFSET)) < 1e-9,
        f"{buys[0].price} vs {ref + ENTRY_OFFSET}")
t.check("1. SELL STOP is reference - 1.00",
        abs(sells[0].price - (ref - ENTRY_OFFSET)) < 1e-9,
        f"{sells[0].price} vs {ref - ENTRY_OFFSET}")
t.check("2. the BUY pending carries its SL, 0.50 behind",
        abs(buys[0].sl - (buys[0].price - SL_DIST)) < 1e-9,
        f"{buys[0].sl} vs {buys[0].price - SL_DIST}")
t.check("3. the SELL pending carries its SL, 0.50 above",
        abs(sells[0].sl - (sells[0].price + SL_DIST)) < 1e-9,
        f"{sells[0].sl} vs {sells[0].price + SL_DIST}")
t.check("neither order has a take profit",
        buys[0].tp == 0 and sells[0].tp == 0,
        f"{buys[0].tp}/{sells[0].tp}")
t.check("symmetric about ONE captured reference",
        abs((buys[0].price - ref) - (ref - sells[0].price)) < 1e-9)

t.section("TEST 4-5. ONE SIDE BECOMES THE TRADE")
eng, broker, settings, rec = live("cancelbuy")
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
t.check("4. BUY triggers -> the SELL pending is cancelled",
        not broker.orders(), f"{len(broker.orders())} orders left")
t.check("4. exactly one position", len(broker.positions()) == 1,
        str(len(broker.positions())))
t.check("4. it is the BUY", broker.positions()[0].side == BUY)
t.check("4. protected at entry - 0.50",
        abs(sl(broker) - (entry - SL_DIST)) < 1e-9,
        f"{sl(broker)} vs {entry - SL_DIST}")

eng, broker, settings, rec = live("cancelsell")
entry_s = fill(broker, SELL_STOP)
eng.check_exit_now()
t.check("5. SELL triggers -> the BUY pending is cancelled",
        not broker.orders(), f"{len(broker.orders())} orders left")
t.check("5. protected at entry + 0.50",
        abs(sl(broker) - (entry_s + SL_DIST)) < 1e-9,
        f"{sl(broker)} vs {entry_s + SL_DIST}")
for _ in range(4):
    eng.step()
    eng.check_exit_now()
t.check("never a second position", len(broker.positions()) == 1,
        str(len(broker.positions())))
t.check("and never a replacement pending order", not broker.orders(),
        str(len(broker.orders())))

t.section("TEST 6-9. THE BUY WALKTHROUGH, EXACTLY AS SPECIFIED")
eng, broker, settings, rec = live("buywalk")
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
t.check("entry is 4501.00 on a zero spread", abs(entry - 4501.00) < 1e-9,
        str(entry))
t.check("initial SL is 4500.50", abs(sl(broker) - 4500.50) < 1e-9,
        str(sl(broker)))

move(eng, 4501.50)
t.check("6. price 4501.50 -> SL moves to breakeven 4501.00",
        abs(sl(broker) - 4501.00) < 1e-9, str(sl(broker)))
t.check("6. breakeven is recorded", eng.straddle.breakeven_set is True)
t.check("6. and the trail is NOT yet armed",
        eng.straddle.trail_active is False)
t.check("6. state is BREAKEVEN", eng.straddle.state == BREAKEVEN,
        eng.straddle.state)

move(eng, 4502.00)
t.check("7. price 4502.00 -> trailing activates",
        eng.straddle.trail_active is True)
t.check("7. SL becomes 4501.50 (high water 4502.00 - 0.50)",
        abs(sl(broker) - 4501.50) < 1e-9, str(sl(broker)))
t.check("7. state is TRAILING", eng.straddle.state == STEP_TRAILING,
        eng.straddle.state)

for price, want in ((4502.50, 4502.00), (4503.00, 4502.50),
                    (4503.50, 4503.00), (4504.00, 4503.50)):
    move(eng, price)
    t.check(f"8. price {price} -> SL {want}", abs(sl(broker) - want) < 1e-9,
            f"{sl(broker)} vs {want}")
t.check("8. the trail is continuous, not stepped - 0.10 moves it 0.10",
        True)
move(eng, 4504.10)
t.check("8. price 4504.10 -> SL 4503.60 (continuous, not a whole step)",
        abs(sl(broker) - 4503.60) < 1e-9, str(sl(broker)))

held = sl(broker)
water = eng.straddle.high_water
for pullback in (4503.80, 4503.00, 4502.20, 4504.05):
    move(eng, pullback)
    t.check(f"9. pullback to {pullback} does NOT move the SL",
            abs(sl(broker) - held) < 1e-9, f"{sl(broker)} vs {held}")
t.check("9. the high water mark is unchanged by a pullback",
        abs(eng.straddle.high_water - water) < 1e-9,
        f"{eng.straddle.high_water} vs {water}")
move(eng, 4504.60)
t.check("9. a NEW high does move it: 4504.60 -> SL 4504.10",
        abs(sl(broker) - 4504.10) < 1e-9, str(sl(broker)))

t.section("TEST 10-13. THE SELL WALKTHROUGH, MIRRORED")
eng, broker, settings, rec = live("sellwalk")
entry = fill(broker, SELL_STOP)
eng.check_exit_now()
t.check("entry is 4499.00", abs(entry - 4499.00) < 1e-9, str(entry))
t.check("initial SL is 4499.50", abs(sl(broker) - 4499.50) < 1e-9,
        str(sl(broker)))
move(eng, 4498.50)
t.check("10. price 4498.50 -> SL moves to breakeven 4499.00",
        abs(sl(broker) - 4499.00) < 1e-9, str(sl(broker)))
move(eng, 4498.00)
t.check("11. price 4498.00 -> trailing activates",
        eng.straddle.trail_active is True)
t.check("11. SL becomes 4498.50 (low water 4498.00 + 0.50)",
        abs(sl(broker) - 4498.50) < 1e-9, str(sl(broker)))
for price, want in ((4497.50, 4498.00), (4497.00, 4497.50),
                    (4496.00, 4496.50)):
    move(eng, price)
    t.check(f"12. price {price} -> SL {want}", abs(sl(broker) - want) < 1e-9,
            f"{sl(broker)} vs {want}")
held = sl(broker)
for retrace in (4496.40, 4497.20, 4496.05):
    move(eng, retrace)
    t.check(f"13. retrace to {retrace} does NOT move the SL up",
            abs(sl(broker) - held) < 1e-9, f"{sl(broker)} vs {held}")
move(eng, 4495.50)
t.check("13. a NEW low does move it: 4495.50 -> SL 4496.00",
        abs(sl(broker) - 4496.00) < 1e-9, str(sl(broker)))

t.section("THE RATCHET, ON A WHIPSAWING PATH")
for direction, entry_p, path in (
        (BUY, 4501.00, [4501.5, 4501.2, 4502.0, 4501.4, 4503.0, 4500.9,
                        4504.0, 4502.0, 4505.0]),
        (SELL, 4499.00, [4498.5, 4498.8, 4498.0, 4498.9, 4497.0, 4499.1,
                         4496.0, 4498.0, 4495.0])):
    stop = initial_sl(direction, entry_p, R)
    hw = lw = 0.0
    monotone = True
    for price in path:
        hw, lw = update_water(direction, price, hw, lw)
        target, _ = next_sl(direction, entry_p, price, R, current_sl=stop,
                            high_water=hw, low_water=lw)
        if target is not None:
            if not sl_is_improvement(direction, target, stop):
                monotone = False
            stop = target
    t.check(f"{direction}: the stop only ever ratcheted", monotone,
            f"ended at {stop}")
t.check("favourable distance is never negative",
        favorable_distance(BUY, 4501.00, 4490.00) == 0.0)
t.check("below the breakeven trigger nothing moves",
        next_sl(BUY, 4501.00, 4501.49, R, current_sl=4500.50)[0] is None)

t.section("TEST 14-16. CLOSE, COOLDOWN, NEW M1 CANDLE")
now = [5000.0]
eng, broker, settings, rec = live("reset")
eng.clock = lambda: now[0]
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
move(eng, 4503.00)
pos = broker.positions()[0]
mt5.hit_sl(pos.ticket)
eng.check_exit_now()
for _ in range(3):
    eng.step()
t.check("14. the position is closed", not broker.positions(),
        str(len(broker.positions())))
t.check("14. no leftover pending orders", not broker.orders(),
        str(len(broker.orders())))
t.check("14. the cycle is closed", not eng.cycle_active)
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("14. the cycle was recorded", bool(closes))
t.check("14. attributed to the stop", closes[-1].kind == STOP_LOSS_HIT,
        str(closes[-1].kind))
ctx = closes[-1].context if closes else {}
deals = [d for d in mt5.STATE["deals"] if d.entry == mt5.DEAL_ENTRY_OUT]
t.check("14. realized P/L came from the MT5 deal history",
        abs(float(ctx.get("realized_pnl_after_close", 0)) -
            round(sum(d.profit for d in deals), 2)) < 0.01,
        f"{ctx.get('realized_pnl_after_close')} vs "
        f"{round(sum(d.profit for d in deals), 2)}")
t.check("15. the 10s cooldown is armed",
        9.0 < eng._reentry_wait() <= 10.0, f"{eng._reentry_wait():.1f}s")
eng.bar_time = lambda: 1_700_000_000
eng.step()
t.check("15. no new straddle during the cooldown", not broker.orders(),
        str(len(broker.orders())))
now[0] += 11
eng.step()
t.check("16. and none after the cooldown without a NEW M1 candle",
        not broker.orders(), str(len(broker.orders())))
eng.bar_time = lambda: 1_700_000_060
eng.step()
t.check("16. a newly closed M1 candle starts the next straddle",
        len(broker.orders()) == 2, str(len(broker.orders())))
t.check("16. with a fresh reference, not the old one",
        eng.straddle.reference == eng.cycle.anchor)
t.check("16. and a new cycle id", eng.cycle.cycle_id == 2,
        str(eng.cycle.cycle_id))

t.section("TEST 17. ONE CYCLE AT A TIME")
eng, broker, settings, rec = live("single")
before = sorted(o.ticket for o in broker.orders())
for _ in range(5):
    eng.step()
    eng.check_exit_now()
t.check("17. repeated passes never create a second straddle",
        sorted(o.ticket for o in broker.orders()) == before,
        f"{len(broker.orders())} orders")
t.check("17. an explicit second cycle is refused",
        eng.create_new_ladder(99, "must be refused") is False)
fill(broker, BUY_STOP)
eng.check_exit_now()
for _ in range(4):
    eng.step()
    eng.check_exit_now()
t.check("17. and none while a position is open",
        len(broker.positions()) == 1 and not broker.orders(),
        f"{len(broker.positions())}p / {len(broker.orders())}o")

t.section("TEST 18. A MODE SWITCH DOES NOT TOUCH THE ACTIVE CYCLE")
eng, broker, settings, rec = live("switch")
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
move(eng, 4502.00)
sl_before = sl(broker)
settings._values["entry_mode"] = "FULL_LADDER"
eng.step()
eng.step()
t.check("18. the position is untouched", len(broker.positions()) == 1,
        str(len(broker.positions())))
t.check("18. its stop is untouched", abs(sl(broker) - sl_before) < 1e-9,
        f"{sl(broker)} vs {sl_before}")
t.check("18. no ladder was built on top of it", not broker.orders(),
        str(len(broker.orders())))
t.check("18. the cycle still reports the mode it was built with",
        eng.entry_mode_in_force() == "STEPPED_STRADDLE",
        eng.entry_mode_in_force())
move(eng, 4503.00)
t.check("18. and it still trails normally",
        abs(sl(broker) - 4502.50) < 1e-9, str(sl(broker)))

t.section("TEST 19-20. BROKER PRECISION AND STOP-DISTANCE LIMITS")
eng, broker, settings, rec = live("digits", bid=4500.123, spread=0.02)
spec = eng.spec
for order in broker.orders():
    t.check(f"19. {order.side} price is on the broker's tick grid",
            abs(order.price - spec.normalize_price(order.price)) < 1e-12,
            str(order.price))
    t.check(f"19. and so is its SL",
            abs(order.sl - spec.normalize_price(order.sl)) < 1e-12,
            str(order.sl))
t.check("19. digits come from the symbol, not a constant",
        spec.digits == mt5.symbol_info("XAUUSD").digits, str(spec.digits))

# stops_level 70 points = 0.70 price units. The +/-1.00 entries are still
# legal, but a breakeven stop 0.60 below the market is inside the broker's
# limit and must be refused rather than forced through.
eng, broker, settings, rec = live("stopslevel", stops_level=70)
t.check("20. the straddle still goes out with a wide stop level",
        len(broker.orders()) == 2, str(len(broker.orders())))
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
protected = sl(broker)
move(eng, 4501.60)
t.check("20. the position is protected from the pending order's own SL",
        protected is not None and protected > 0, str(protected))
t.check("20. an SL inside the broker minimum is NOT forced through",
        sl(broker) is not None and abs(sl(broker) - protected) < 1e-9,
        f"{sl(broker)} vs {protected}")
t.check("20. and the reason is logged, not swallowed",
        any(e[0] == "STRADDLE_SL_DEFERRED" for e in rec.events),
        str([e[0] for e in rec.events][-4:]))
move(eng, 4505.00)
t.check("20. the stop stays at its last VALID level, never unprotected",
        sl(broker) is not None and abs(sl(broker) - protected) < 1e-9,
        f"{sl(broker)} vs {protected}")
t.check("20. a trail that can never clear the broker limit is called out once",
        len([e for e in rec.events
             if e[0] == "STRADDLE_TRAIL_UNREACHABLE"]) == 1,
        str([e[0] for e in rec.events if "UNREACHABLE" in e[0]]))
# with a workable limit the trail moves normally
eng, broker, settings, rec = live("stopsok", stops_level=10)
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
before = sl(broker)
move(eng, 4503.00)
t.check("20. with a broker limit inside TRAIL_DISTANCE the trail works",
        sl(broker) > before and abs(sl(broker) - 4502.50) < 1e-9,
        f"{sl(broker)} vs 4502.50")

t.section("A FAILED SL MODIFICATION RETRIES ONCE, THEN LEAVES IT ALONE")
eng, broker, settings, rec = live("slfail")
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
good = sl(broker)
attempts = []
real_send = mt5.order_send


def refuse(request):
    if request.get("action") == mt5.TRADE_ACTION_SLTP:
        attempts.append(1)
        return type("R", (), {"retcode": 10016, "comment": "invalid stops",
                              "order": 0, "deal": 0})()
    return real_send(request)


mt5.order_send = refuse
try:
    move(eng, 4501.60)
finally:
    mt5.order_send = real_send
t.check("exactly two attempts - one try, one retry", len(attempts) == 2,
        f"{len(attempts)}")
t.check("the old stop is preserved", abs(sl(broker) - good) < 1e-9,
        f"{sl(broker)} vs {good}")
t.check("the failure is logged with the broker's reason",
        any(e[0] == "ERROR" and "SL modify failed" in e[1] for e in rec.events))
move(eng, 4501.70)
t.check("and a later pass can still move it", sl(broker) > good,
        f"{sl(broker)} vs {good}")

t.section("NO TAKE PROFIT, NO BASKET, NO RECOVERY")
eng, broker, settings, rec = live(
    "nobasket", {"basket_profit_target": 0.01, "profit_runner_enabled": False,
                 "max_cycle_drawdown": 0.01,
                 "max_cycle_duration_minutes": 0.001})
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
move(eng, 4505.00)
t.check("no take profit is ever set", broker.positions()[0].tp == 0,
        str(broker.positions()[0].tp))
reason, detail = eng._exit_reason(settings.snapshot(),
                                  eng.get_cycle_floating_pnl(),
                                  broker.positions())
t.check("the basket exit engine returns nothing for this mode",
        reason is None, f"{reason} {detail}")
t.check("the position is still open despite a 0.01 basket target",
        len(broker.positions()) == 1, str(len(broker.positions())))
move(eng, 4495.00)
reason, _ = eng._exit_reason(settings.snapshot(),
                             eng.get_cycle_floating_pnl(),
                             broker.positions())
t.check("and the cycle drawdown guard does not close it either",
        reason is None, str(reason))
t.check("the trailing stop remains the only exit", eng.cycle_active)

t.section("TRAILING IS LIVE - IT NEVER WAITS FOR A CANDLE")
eng, broker, settings, rec = live("livetrail")
eng.bar_time = lambda: (_ for _ in ()).throw(
    AssertionError("the trailing path read a candle"))
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
t.check("the fill is handled with the candle feed poisoned",
        abs(sl(broker) - (entry - SL_DIST)) < 1e-9, str(sl(broker)))
for price, want in ((4501.50, 4501.00), (4502.00, 4501.50),
                    (4503.00, 4502.50)):
    move(eng, price)
    t.check(f"trailing to {want} without any candle",
            abs(sl(broker) - want) < 1e-9, f"{sl(broker)} vs {want}")
t.check("several updates happened inside one candle",
        eng.straddle.sl_modifications >= 3,
        str(eng.straddle.sl_modifications))

t.section("MODE ISOLATION: THE OTHER TWO ARE UNTOUCHED")
eng, broker, settings, rec = live(
    "full", {"entry_mode": "FULL_LADDER", "ladder_spacing": 0.30,
             "ladder_depth": 11, "max_pending_orders": 22})
t.check("FULL_LADDER still builds 11 + 11",
        len([o for o in broker.orders() if o.side == BUY_STOP]) == 11 and
        len([o for o in broker.orders() if o.side == SELL_STOP]) == 11,
        f"{len(broker.orders())} orders")
t.check("and allocates no straddle state", eng.straddle is None)
eng, broker, settings, rec = live(
    "sp", {"entry_mode": "SINGLE_PAIR", "ladder_spacing": 0.30})
t.check("SINGLE_PAIR still builds one pair at LADDER_SPACING",
        len(broker.orders()) == 2 and
        abs(min(o.price for o in broker.orders() if o.side == BUY_STOP) -
            (eng.cycle.anchor + 0.30)) < 1e-9,
        str([o.price for o in broker.orders()]))
t.check("and allocates no straddle state either", eng.straddle is None)

t.section("TELEMETRY")
import csv_logger
for column in ("straddle_state", "straddle_direction", "entry_price",
               "direction", "entry_offset", "initial_sl_distance",
               "trail_distance", "initial_sl", "current_sl", "breakeven_set",
               "high_price", "low_price", "trail_activated",
               "reference_price_straddle", "intended_buy_price",
               "intended_sell_price", "position_ticket"):
    t.check(f"basket telemetry has {column!r}",
            column in csv_logger.TELEMETRY_HEADER, column)
for column in ("straddle_direction", "straddle_entry_price",
               "straddle_initial_sl", "straddle_final_sl",
               "straddle_breakeven_set", "straddle_high_water",
               "straddle_low_water", "straddle_trail_active",
               "trail_activated_at", "fill_detected_at",
               "opposite_cancel_confirmed_at", "initial_sl_confirmed_at",
               "breakeven_triggered_at", "position_closed_at",
               "fill_to_cancel_ms", "fill_to_initial_sl_ms"):
    t.check(f"the cycle summary has {column!r}",
            column in csv_logger.CYCLE_HEADER, column)

eng, broker, settings, rec = live("telemetry")
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
move(eng, 4503.00)
summary = eng._straddle_summary()
t.check("the high water mark is recorded",
        abs(summary["straddle_high_water"] - 4503.00) < 1e-9,
        str(summary["straddle_high_water"]))
t.check("the trail state is recorded",
        summary["straddle_trail_active"] is True)
t.check("fill -> opposite cancelled is measured",
        isinstance(summary["fill_to_cancel_ms"], float),
        str(summary["fill_to_cancel_ms"]))
t.check("fill -> initial SL is measured",
        isinstance(summary["fill_to_initial_sl_ms"], float),
        str(summary["fill_to_initial_sl_ms"]))

t.section("NO MARTINGALE, NO SCALING")
eng, broker, settings, rec = live("nomart", {"lot_size": 0.02})
entry = fill(broker, BUY_STOP)
eng.check_exit_now()
for price in (4502.0, 4503.0, 4504.0):
    move(eng, price)
t.check("the lot is the configured one", broker.positions()[0].volume == 0.02,
        str(broker.positions()[0].volume))
t.check("still exactly one position", len(broker.positions()) == 1,
        str(len(broker.positions())))
t.check("and no orders were added back", not broker.orders(),
        str(len(broker.orders())))

t.done()
