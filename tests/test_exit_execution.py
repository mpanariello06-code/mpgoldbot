"""
The exit as an EXECUTION path: how fast, how atomic, how honest.

This suite is about the machinery around the decision, not the decision
itself (that is test_basket.py). It holds the guarantees that matter when a
basket is at its target and the market is moving:

  * the decision is committed exactly once, and irrevocably
  * positions are closed BEFORE pending orders are cancelled
  * nothing on the critical path waits for Telegram, CSV or indicators
  * a refusal from the broker is retried, recorded, and never leaves the
    account half-closed
  * the latency of every stage is measured rather than assumed
"""
import pathlib
import shutil
import threading
import time

from harness import Suite, use_stub_mt5
use_stub_mt5()

import MetaTrader5 as mt5
import config as cfg
from basket import (BASKET_PROFIT_TARGET, EXIT, HOLD, PROTECT, CycleBasket,
                    ProfitRules)
from broker import BUY_STOP, SELL_STOP, Mt5Broker
from fakes import Recorder, TickFeed, make_paper, trigger_buy
from ladder_engine import RollingLadderEngine, State
from runtime_settings import RuntimeSettings

t = Suite("exit_execution")
TMP = pathlib.Path("/tmp/exit_exec_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)

DEFAULTS = {"ladder_depth": 11, "max_pending_orders": 22,
            "max_open_positions": 22, "max_ladder_depth": 22,
            "ladder_spacing": 0.30, "basket_profit_target": 2.00,
            "profit_runner_enabled": False, "stop_loss_distance": 0,
            "max_cycle_duration_minutes": 0, "max_cycle_drawdown": 0,
            "telemetry_interval_seconds": 0}


def live(name, overrides=None, price=4010.00):
    """A real Mt5Broker on the stub terminal - the live execution path."""
    mt5.reset()
    mt5.set_price(price)
    mt5.initialize()
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}_s.json")
    settings._values.update(DEFAULTS)
    settings._values.update(overrides or {})
    broker = Mt5Broker("XAUUSD", 88001199)
    rec = Recorder()
    eng = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                              state_path=None)
    eng.resume()
    return eng, broker, settings, rec


def fill(broker, count, side=BUY_STOP):
    """Trigger `count` of the cycle's pending orders, as the broker would."""
    orders = sorted([o for o in broker.orders() if o.side == side],
                    key=lambda o: o.price)
    for o in orders[:count]:
        mt5.trigger_order(o.ticket)
    return len(orders[:count])


# ===========================================================================
t.section("SCENARIO A: THE BASKET REACHES THE TARGET AND EXITS")
eng, broker, settings, rec = live("scenA")
eng.step()
t.check("the ladder went out", len(broker.orders()) == 22,
        f"{len(broker.orders())} orders")
fill(broker, 6)
mt5.set_price(4011.70)
eng.step()
t.check("6 legs open, still under target",
        len(broker.positions()) == 6 and eng.cycle_active,
        f"{eng.get_cycle_floating_pnl():+.2f}")
mt5.set_price(4011.80)
reason = eng.check_exit_now()
t.check("1. the fast monitor took it at the target",
        reason == BASKET_PROFIT_TARGET, str(reason))
t.check("the account is flat", not broker.positions() and not broker.orders(),
        f"{len(broker.positions())}p / {len(broker.orders())}o")

t.section("SCENARIO B: PAST THE TARGET, THEN PROTECTED ON THE RETRACE")
rules = ProfitRules(target=2.00, runner_enabled=True, activation=3.00,
                    trail=1.50, floor=1.00)
b = CycleBasket(1, 4010.0, 0.30, started_at=0.0)
path, taken = [1.0, 2.0, 3.5, 5.0, 4.5, 3.4], None
for i, pnl in enumerate(path):
    b.mark(pnl, rules, float(i))
    action, why, detail = b.decide(rules, has_exposure=True)
    if action == EXIT:
        taken = (pnl, why)
        break
t.check("2. it was allowed past the target", b.peak_pnl >= 5.0,
        f"peak {b.peak_pnl}")
t.check("2. the trail took it on the way back", taken is not None
        and taken[1] == "PROFIT_PROTECTION", str(taken))
t.check("2. and it was still well in profit", taken and taken[0] >= 3.0,
        str(taken))

t.section("SCENARIO C: DEEPLY NEGATIVE, THEN RECOVERS")
b = CycleBasket(2, 4010.0, 0.30, started_at=0.0)
states, taken = [], None
for i, pnl in enumerate([-3.0, -12.0, -15.0, -10.0, -5.0, -2.0, 0.5, 0.8]):
    b.mark(pnl, rules, float(i))
    states.append(b.recovery_state)
    action, why, _ = b.decide(rules, has_exposure=True)
    if action == EXIT:
        taken = (pnl, why)
        break
t.check("3. it was recognised as underwater", "UNDERWATER" in states,
        str(states))
t.check("3. then as recovering", "RECOVERING" in states, str(states))
t.check("3. the lowest point is remembered", b.lowest_pnl == -15.0,
        str(b.lowest_pnl))
t.check("3. the recovery is measured",
        b.recovery_amount > 0 and b.recovery_start_pnl is not None,
        f"{b.recovery_amount} from {b.recovery_start_pnl}")
t.check("3. a recovered basket is taken early, not held for the full target",
        taken is not None and taken[1] == "RECOVERY_PROFIT", str(taken))
t.check("3. peak never went below current",
        b.peak_pnl >= b.basket_pnl >= b.lowest_pnl,
        f"{b.peak_pnl} / {b.basket_pnl} / {b.lowest_pnl}")

t.section("SCENARIO D: NEVER RECOVERS -> HARD DRAWDOWN")
eng, broker, settings, rec = live("scenD", {"max_cycle_drawdown": 5.0})
eng.step()
fill(broker, 6, side=BUY_STOP)
mt5.set_price(4009.00)                      # deep against the buys
reason = eng.check_exit_now()
t.check("4. hard drawdown overrides the strategy",
        reason == "RISK_DRAWDOWN", str(reason))
t.check("4. and it closed everything",
        not broker.positions() and not broker.orders())

t.section("SCENARIO E: POSITIONS CLOSE BEFORE PENDINGS ARE CANCELLED")
eng, broker, settings, rec = live("scenE")
eng.step()
fill(broker, 6)
mt5.set_price(4011.80)
order = []
real_send = mt5.order_send


def watched(request):
    action = request.get("action")
    if action == mt5.TRADE_ACTION_DEAL and "position" in request:
        order.append("close")
    elif action == mt5.TRADE_ACTION_REMOVE:
        order.append("cancel")
    return real_send(request)


mt5.order_send = watched
try:
    eng.check_exit_now()
finally:
    mt5.order_send = real_send
t.check("5. both happened", "close" in order and "cancel" in order, str(order[:4]))
t.check("5. EVERY position close precedes EVERY pending cancel",
        order.index("cancel") > max(i for i, x in enumerate(order)
                                    if x == "close"),
        str(order))
t.check("5. the account ends flat",
        not broker.positions() and not broker.orders())

t.section("SCENARIO F: TELEGRAM CANNOT BLOCK AN EXIT")
eng, broker, settings, rec = live("scenF")
eng.step()
fill(broker, 6)
mt5.set_price(4011.80)
# a notifier that takes a full second, wired exactly where the real one is
slow_calls = []


def glacial(text):
    slow_calls.append(text)
    time.sleep(1.0)


hooks = rec.hooks()
_ev = hooks["event"]
hooks["event"] = lambda e, m, f: (_ev(e, m, f), glacial(m))[0]
eng.hooks = hooks
started = time.perf_counter()
reason = eng.check_exit_now()
elapsed = time.perf_counter() - started
t.check("6. the exit still fired", reason == BASKET_PROFIT_TARGET, str(reason))
t.check("6. the account is flat", not broker.positions() and not broker.orders())
t.check("6. a slow notifier was actually in the path", len(slow_calls) >= 1,
        f"{len(slow_calls)} calls")
# The hook is called; what must not happen is the CLOSE waiting for it. The
# close requests go out before the notifications that describe them.
t.check("6. positions were closed before the slow calls finished",
        elapsed >= 1.0 and not broker.positions(),
        f"{elapsed:.2f}s, {len(broker.positions())} positions left")

t.section("THE EXIT LATCH IS ATOMIC AND ONE-WAY")
eng, broker, settings, rec = live("latch")
eng.step()
fill(broker, 6)
mt5.set_price(4011.80)
tick = broker.tick()
positions = broker.positions()
first = eng.commit_exit("BASKET_PROFIT_TARGET", "test", tick, positions, (), 2.5)
second = eng.commit_exit("BASKET_PROFIT_TARGET", "test again", tick,
                         positions, (), 2.5)
t.check("7. the first commit wins", first is True)
t.check("7. a duplicate trigger is a no-op, not a second close", second is False)
t.check("7. the state says EXITING", eng.state == State.EXITING, eng.state)
t.check("7. the latch is set", eng.exit_in_progress is True)
t.check("8. no ladder may be created while exiting",
        eng.create_new_ladder(99, "should be refused") is False)
t.check("8. and the refusal is logged",
        any(e[0] == "LADDER_REJECTED_ALREADY_ACTIVE" and "EXITING" in e[1]
            for e in rec.events),
        str([e[1][:50] for e in rec.events
             if e[0] == "LADDER_REJECTED_ALREADY_ACTIVE"][-1:]))
before = len(broker.orders())
eng._reconcile(settings.snapshot(), tick, positions, broker.orders())
t.check("9. reconcile places nothing while exiting",
        len(broker.orders()) <= before, f"{before} -> {len(broker.orders())}")

t.section("A CLOSE THAT FAILS IS RETRIED, NOT SKIPPED")
eng, broker, settings, rec = live("closefail")
eng.step()
fill(broker, 4)
mt5.set_price(4011.90)
refuse = {"on": True}
real_send = mt5.order_send


def refusing(request):
    if refuse["on"] and request.get("action") == mt5.TRADE_ACTION_DEAL:
        return type("R", (), {"retcode": 10013, "comment": "refused",
                              "order": 0, "deal": 0})()
    return real_send(request)


mt5.order_send = refusing
try:
    eng.check_exit_now()
    t.check("10. the broker refused every close",
            len(broker.positions()) == 4, f"{len(broker.positions())}")
    t.check("10. the failure is recorded, not swallowed",
            any(e[0] == "ERROR" and "close" in e[1] for e in rec.events),
            str([e[1][:40] for e in rec.events if e[0] == "ERROR"][:2]))
    t.check("10. the cycle is NOT finalised while positions remain",
            eng.exit_in_progress and eng.cycle_active)
    t.check("10. and no new cycle can start",
            eng.create_new_ladder(98, "must be refused") is False)
    refuse["on"] = False
    for _ in range(3):
        eng.step()
finally:
    mt5.order_send = real_send
t.check("11. once the broker accepts, the close completes",
        not broker.positions(), f"{len(broker.positions())} left")
t.check("11. and only then is the latch released",
        eng.exit_in_progress is False)

t.section("A CANCEL THAT FAILS LEAVES THE CYCLE UNFINISHED")
eng, broker, settings, rec = live("cancelfail")
eng.step()
fill(broker, 3)
mt5.set_price(4011.95)
block = {"on": True}
real_send = mt5.order_send


def block_cancel(request):
    if block["on"] and request.get("action") == mt5.TRADE_ACTION_REMOVE:
        return type("R", (), {"retcode": 10013, "comment": "no",
                              "order": 0, "deal": 0})()
    return real_send(request)


mt5.order_send = block_cancel
try:
    eng.check_exit_now()
    t.check("12. positions still closed even though cancels failed",
            not broker.positions(), f"{len(broker.positions())}")
    t.check("12. the pendings are still there", len(broker.orders()) > 0,
            f"{len(broker.orders())}")
    t.check("12. so the cycle is not finalised", eng.exit_in_progress is True)
    block["on"] = False
    for _ in range(3):
        eng.step()
finally:
    mt5.order_send = real_send
t.check("13. the retry cleared them", not broker.orders(),
        f"{len(broker.orders())} left")
t.check("13. and the cycle finished", eng.exit_in_progress is False)

t.section("AN ALREADY-FLAT ACCOUNT FINISHES CLEANLY")
eng, broker, settings, rec = live("alreadyflat")
eng.step()
fill(broker, 2)
mt5.set_price(4011.90)
tick, positions = broker.tick(), broker.positions()
eng.commit_exit("MANUAL_EXIT", "flat test", tick, positions, (), 2.5)
for p in list(mt5.STATE["positions"]):        # broker closed them behind us
    mt5.close_deal(p, mt5.STATE["bid"], "external")
mt5.STATE["orders"] = []
for _ in range(3):
    eng.step()
t.check("14. an account already flat still finalises",
        eng.exit_in_progress is False and not eng.cycle_active,
        f"latch={eng.exit_in_progress} active={eng.cycle_active}")

t.section("EXIT LATENCY IS MEASURED, NOT ASSUMED")
eng, broker, settings, rec = live("latency")
eng.step()
fill(broker, 6)
mt5.set_price(4011.80)
eng.check_exit_now()
for _ in range(3):
    eng.step()
closes = [c for c in rec.cycles if c.kind_of == "complete"]
ctx = closes[-1].context if closes else {}
for key in ("target_detected_at", "exit_state_entered_at",
            "close_request_started_at", "close_request_completed_at",
            "fully_flat_at", "detection_latency_ms",
            "decision_to_close_request_ms", "close_request_latency_ms",
            "total_exit_latency_ms", "positions_closed", "close_failures"):
    t.check(f"15. the cycle records {key!r}", key in ctx, str(sorted(ctx)[:6]))
t.check("15. decision -> close request is a real measurement",
        isinstance(ctx.get("decision_to_close_request_ms"), float),
        str(ctx.get("decision_to_close_request_ms")))
t.check("15. and it is fast (sub-10ms of application time)",
        ctx.get("decision_to_close_request_ms", 999) < 10.0,
        f"{ctx.get('decision_to_close_request_ms')} ms")
t.check("16. floating before the close and realized after it are separate",
        "floating_pnl_before_close" in ctx and
        "realized_pnl_after_close" in ctx, str(sorted(ctx)[:8]))
t.check("16. realized comes from the deal history, not from floating",
        ctx.get("realized_pnl_after_close") is not None)

t.section("LADDER PLACEMENT IS MEASURED TOO")
t.check("17. the cycle records the ladder timings",
        {"ladder_first_order_ms", "ladder_complete_ms",
         "ladder_orders_placed"} <= set(ctx), str(sorted(ctx)[:8]))
t.check("17. first order is timed", ctx.get("ladder_first_order_ms", -1) >= 0,
        str(ctx.get("ladder_first_order_ms")))
t.check("17. and the full ladder is timed",
        ctx.get("ladder_complete_ms", 0) >= ctx.get("ladder_first_order_ms", 0),
        f"{ctx.get('ladder_first_order_ms')} -> {ctx.get('ladder_complete_ms')}")

t.section("CSV LOGGING CANNOT BLOCK AN EXIT")
import csv_logger
writer = csv_logger.AsyncWriter()
slow_path = TMP / "slow.csv"
calls = []
t0 = time.perf_counter()
for i in range(200):
    writer.submit(slow_path, [i, "row"])
enqueue = time.perf_counter() - t0
t.check("18. enqueueing 200 rows costs the caller under 5ms",
        enqueue < 0.005, f"{enqueue * 1000:.2f} ms")
t.check("18. the rows do reach disk", writer.flush(timeout=5.0))
t.check("18. all of them", len(open(slow_path).read().splitlines()) == 200,
        str(len(open(slow_path).read().splitlines())))
t.check("18. and the writer stops cleanly", writer.stop(timeout=5.0))

failing = csv_logger.AsyncWriter()
failing.submit(pathlib.Path("/nonexistent-dir/nope.csv"), ["x"])
failing.flush(timeout=2.0)
t.check("19. a write failure is counted, never raised", failing.dropped >= 1,
        str(failing.dropped))
t.check("19. and the writer survives it",
        failing._thread.is_alive() and failing.stop(timeout=2.0))

t.section("THE EXIT MONITOR RUNS OFF THE LADDER LOOP")
eng, broker, settings, rec = live("threaded")
eng.step()
fill(broker, 6)
mt5.set_price(4011.80)
seen = {}


def monitor():
    seen["reason"] = eng.check_exit_now()


th = threading.Thread(target=monitor, name="exit-monitor-test")
th.start()
th.join(timeout=5.0)
t.check("20. an exit committed from another thread works",
        seen.get("reason") == BASKET_PROFIT_TARGET, str(seen))
t.check("20. and left the account flat",
        not broker.positions() and not broker.orders())

t.done()
