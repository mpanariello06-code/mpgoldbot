"""
DEEP LADDER: depth controls EXPOSURE, health controls EXIT.

The bot should not fear a large ladder. It should fear an UNHEALTHY large
ladder. Everything here exists to hold that line:

  * a deep basket that is green, or recovering strongly, is never closed for
    being deep - at any depth
  * a deep basket that is bleeding stops being ADDED to, without being closed
  * only the full emergency case - deep AND severely underwater AND not
    recovering AND still going the wrong way - forces an exit

The zone thresholds and health thresholds are INITIAL TEST DEFAULTS. These
tests pin the BEHAVIOUR they produce, not the numbers themselves.
"""
import pathlib
import shutil
import threading
from types import SimpleNamespace

from harness import Suite, use_stub_mt5
use_stub_mt5()

import MetaTrader5 as mt5
import config as cfg
from basket import (CRITICAL_LADDER_RISK, DEEP_ADVERSE, DEEP_CRITICAL,
                    DEEP_HEALTHY, DEEP_LADDER_RISK, DEEP_RECOVERING, DEEP_WEAK,
                    EXIT, HOLD, LADDER_CRITICAL, LADDER_DEEP, LADDER_EXTENDED,
                    LADDER_NORMAL, NO_RECOVERY, PROTECT, RISK_CRITICAL,
                    RISK_HIGH, RISK_LOW, STRONG_RECOVERY, CycleBasket,
                    ProfitRules)
from broker import BUY, BUY_STOP, SELL, Mt5Broker
from fakes import Recorder
from ladder_engine import RollingLadderEngine
from runtime_settings import RuntimeSettings

t = Suite("deep_ladder")
TMP = pathlib.Path("/tmp/deep_ladder_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)

RULES = dict(target=2.0, runner_enabled=True, activation=3.0, trail=1.5,
             floor=1.0, underwater_at=2.0, recovery_fraction=0.5,
             weak_recovery_at=0.25, strong_recovery_at=0.75,
             max_ladder_depth=22, extended_depth=9, deep_depth=12,
             critical_depth=16, deep_risk_enabled=True,
             deep_max_drawdown=10.0, deep_max_imbalance=0.60,
             deep_recovery_timeout=900.0, deep_adverse_move=0.60,
             max_imbalance=0.80, imbalance_action="MONITOR",
             imbalance_min_positions=4)
R = ProfitRules(**RULES)


def leg(side, volume=0.01, price=4010.0):
    return SimpleNamespace(side=side, volume=volume, price_open=price)


def basket(depth, pnl_path, buys=6, sells=6, prices=None, rules=R, t0=0.0):
    """A basket driven to `depth` levels along a P/L and price path."""
    b = CycleBasket(1, 4010.0, 0.30, started_at=t0)
    b.triggers = [SimpleNamespace(side=BUY if i % 2 else SELL, index=i,
                                  price=0.0, ts=0.0) for i in range(depth)]
    legs = [leg(BUY)] * buys + [leg(SELL)] * sells
    prices = prices or [4010.0] * len(pnl_path)
    for i, (pnl, price) in enumerate(zip(pnl_path, prices), start=1):
        b.observe_price(price, t0 + i, legs)
        b.mark(float(pnl), rules, t0 + i)
    return b


RISING = [4010.0, 4010.4, 4010.8, 4011.2]
FALLING = [4011.0, 4010.4, 4009.8, 4009.2]
STRONG_CLIMB = [-16.0, -11.0, -6.0, -2.0]
NO_CLIMB = [-2.0, -8.0, -14.0, -20.0]
WEAK_CLIMB = [-15.0, -13.0, -12.0, -11.0]


# ===========================================================================
t.section("1-3. THE DEPTH ZONES")
for depth, zone in ((1, LADDER_NORMAL), (8, LADDER_NORMAL),
                    (9, LADDER_EXTENDED), (11, LADDER_EXTENDED),
                    (12, LADDER_DEEP), (15, LADDER_DEEP),
                    (16, LADDER_CRITICAL), (20, LADDER_CRITICAL)):
    b = basket(depth, [1.0])
    t.check(f"depth {depth} is {zone}", b.ladder_state == zone, b.ladder_state)
t.check("1. depth 8 still expands freely",
        basket(8, [1.0]).expansion_allowed)
t.check("2. depth 9 is EXTENDED and still expands freely",
        basket(9, [1.0]).expansion_allowed)
t.check("3. depth 12 is DEEP - and a HEALTHY deep basket still expands",
        basket(12, [1.0]).expansion_allowed)
t.check("the zone is exposed under both names",
        basket(12, [1.0]).depth_zone == LADDER_DEEP)

t.section("4, 8, 9. DEPTH ALONE NEVER CLOSES A BASKET")
# This is the whole point. A deep ladder is not a bad ladder.
for depth in (12, 14, 16, 18, 20):
    green = basket(depth, [0.5, 1.0, 1.4], prices=RISING)
    action, reason, _ = green.decide(R, has_exposure=True)
    t.check(f"8. depth {depth}, green: not closed for being deep",
            action != EXIT, f"{action} {reason}")
    recovering = basket(depth, STRONG_CLIMB, buys=9, sells=5, prices=RISING)
    action, reason, _ = recovering.decide(R, has_exposure=True)
    t.check(f"9. depth {depth}, STRONG recovery: not closed for being deep",
            action != EXIT, f"{action} {reason}")
    t.check(f"9. depth {depth}, STRONG recovery: still allowed to expand",
            recovering.expansion_allowed, recovering.expansion_block_reason)
    t.check(f"4. depth {depth} recovering reads as DEEP_RECOVERING or better",
            recovering.deep_ladder_state in (DEEP_RECOVERING, DEEP_HEALTHY),
            recovering.deep_ladder_state)

t.section("5. DEEP + WEAK RECOVERY + ADVERSE STOPS EXPANSION")
b = basket(13, WEAK_CLIMB, buys=7, sells=6, prices=FALLING)
t.check("5. the health reading is not healthy",
        b.deep_ladder_state in (DEEP_WEAK, DEEP_ADVERSE), b.deep_ladder_state)
t.check("5. expansion stops", not b.expansion_allowed)
t.check("5. and it says why",
        "DEEP" in b.expansion_block_reason and str(13) in b.expansion_block_reason,
        b.expansion_block_reason)
t.check("5. but the basket is NOT closed",
        b.decide(R, has_exposure=True)[0] != EXIT,
        str(b.decide(R, has_exposure=True)[:2]))

t.section("6. DEEP + HIGH IMBALANCE + ADVERSE STOPS EXPANSION")
b = basket(13, [-1.0, -5.0, -9.0, -12.0], buys=12, sells=1, prices=FALLING)
t.check("6. the basket is one-sided", b.direction_imbalance > 0.60,
        f"{b.direction_imbalance:.2f}")
t.check("6. expansion stops", not b.expansion_allowed,
        b.expansion_block_reason)
t.check("6. the reason names the imbalance",
        "imbalance" in b.expansion_block_reason, b.expansion_block_reason)
t.check("6. the basket is still NOT closed",
        b.decide(R, has_exposure=True)[0] != EXIT,
        str(b.decide(R, has_exposure=True)[:2]))
t.check("6. risk is reported as elevated", b.risk_state in ("MEDIUM", "HIGH"),
        f"{b.risk_state} {b.risk_score}")

t.section("7. CRITICAL + SEVERE + NO RECOVERY + ADVERSE CAN EXIT")
b = basket(16, NO_CLIMB, buys=15, sells=1, prices=FALLING)
t.check("7. the zone is CRITICAL", b.ladder_state == LADDER_CRITICAL,
        b.ladder_state)
t.check("7. the health reading is DEEP_CRITICAL",
        b.deep_ladder_state == DEEP_CRITICAL, b.deep_ladder_state)
t.check("7. risk is CRITICAL", b.risk_state == RISK_CRITICAL,
        f"{b.risk_state} {b.risk_score}")
action, reason, detail = b.decide(R, has_exposure=True)
t.check("7. the risk manager exits", action == EXIT, f"{action} {reason}")
t.check("7. attributed to CRITICAL_LADDER_RISK", reason == CRITICAL_LADDER_RISK,
        str(reason))
t.check("7. and the reason names all four conditions",
        all(w in detail for w in ("depth", "peak", "NO_RECOVERY", "adverse")),
        detail)

t.section("EVERY ONE OF THE FOUR IS REQUIRED FOR CRITICAL")
# Drop one condition at a time; none of these may reach DEEP_CRITICAL.
t.check("not critical when shallow",
        basket(10, NO_CLIMB, buys=9, sells=1,
               prices=FALLING).deep_ladder_state != DEEP_CRITICAL)
t.check("not critical when the hole is small",
        basket(16, [-0.5, -1.0, -1.5, -2.0], buys=15, sells=1,
               prices=FALLING).deep_ladder_state != DEEP_CRITICAL)
t.check("not critical when it is recovering",
        basket(16, STRONG_CLIMB, buys=15, sells=1,
               prices=FALLING).deep_ladder_state != DEEP_CRITICAL)
t.check("not critical when price has turned favorable",
        basket(16, NO_CLIMB, buys=15, sells=1,
               prices=RISING).deep_ladder_state != DEEP_CRITICAL)

t.section("THE RISK SCORE IS ORDERED AND AUDITABLE")
healthy = basket(12, [1.0, 1.5], prices=RISING)
weak = basket(13, WEAK_CLIMB, buys=7, sells=6, prices=FALLING)
critical = basket(16, NO_CLIMB, buys=15, sells=1, prices=FALLING)
t.check("a healthy deep basket scores low", healthy.risk_state == RISK_LOW,
        f"{healthy.risk_state} {healthy.risk_score}")
t.check("risk rises with deterioration",
        healthy.risk_score < weak.risk_score < critical.risk_score,
        f"{healthy.risk_score} < {weak.risk_score} < {critical.risk_score}")
t.check("the score stays inside 0..1",
        all(0.0 <= b.risk_score <= 1.0 for b in (healthy, weak, critical)),
        f"{healthy.risk_score}/{weak.risk_score}/{critical.risk_score}")
t.check("deep risk can be switched off entirely",
        basket(16, NO_CLIMB, buys=15, sells=1, prices=FALLING,
               rules=ProfitRules(**{**RULES, "deep_risk_enabled": False})
               ).expansion_allowed)

t.section("EXPANSION IS CONDITIONAL AT DEEP, STRICTER AT CRITICAL")
deep_weak = basket(13, WEAK_CLIMB, buys=7, sells=6, prices=FALLING)
crit_weak = basket(17, WEAK_CLIMB, buys=9, sells=8, prices=RISING)
crit_strong = basket(17, STRONG_CLIMB, buys=9, sells=8, prices=RISING)
t.check("DEEP + unhealthy -> paused", not deep_weak.expansion_allowed)
t.check("CRITICAL + merely 'not adverse' is NOT enough",
        not crit_weak.expansion_allowed, crit_weak.expansion_block_reason)
t.check("CRITICAL + STRONG recovery -> allowed",
        crit_strong.expansion_allowed, crit_strong.expansion_block_reason)
t.check("the critical block names the recovery quality",
        "recovering strongly" in crit_weak.expansion_block_reason,
        crit_weak.expansion_block_reason)

t.section("10. THE EXIT LATCH STOPS EXPANSION AT ANY DEPTH")
mt5.reset()
mt5.set_price(4010.00)
mt5.initialize()
settings = RuntimeSettings(cfg.runtime_defaults(), TMP / "latch_s.json")
settings._values.update({"ladder_spacing": 0.30, "ladder_depth": 11,
                         "max_pending_orders": 22, "max_open_positions": 22,
                         "stop_loss_distance": 0,
                         "telemetry_interval_seconds": 0})
broker = Mt5Broker("XAUUSD", 88001199)
rec = Recorder()
eng = RollingLadderEngine(broker, settings, hooks=rec.hooks(), state_path=None)
eng.resume()
eng.step()
for o in sorted([x for x in broker.orders() if x.side == BUY_STOP],
                key=lambda x: x.price)[:4]:
    mt5.trigger_order(o.ticket)
eng.step()
tick, positions = broker.tick(), broker.positions()
eng.commit_exit("MANUAL_EXIT", "latch test", tick, positions, (), 1.0)
before = len(broker.orders())
eng._reconcile(settings.snapshot(), tick, positions, broker.orders())
t.check("10. an exit in progress places nothing more",
        len(broker.orders()) <= before, f"{before} -> {len(broker.orders())}")
t.check("10. and no ladder may be created",
        eng.create_new_ladder(99, "must be refused") is False)

t.section("11. BOTH FIRST STOPS COME FROM ONE CAPTURED REFERENCE")
mt5.reset()
mt5.set_price(4010.00, 4010.30)          # a wide spread, to make it obvious
mt5.initialize()
settings = RuntimeSettings(cfg.runtime_defaults(), TMP / "ref_s.json")
settings._values.update({"ladder_spacing": 0.30, "ladder_depth": 6,
                         "max_pending_orders": 12, "stop_loss_distance": 0,
                         "telemetry_interval_seconds": 0})
broker = Mt5Broker("XAUUSD", 88001199)
eng = RollingLadderEngine(broker, settings, hooks=Recorder().hooks(),
                          state_path=None)
eng.resume()
eng.step()
ref = eng.cycle.anchor
buys = sorted(o.price for o in broker.orders() if o.side == BUY_STOP)
sells = sorted((o.price for o in broker.orders() if o.side != BUY_STOP),
               reverse=True)
t.check("11. first BUY is reference + 0.30",
        abs(buys[0] - (ref + 0.30)) < 1e-9, f"{buys[0]} vs {ref + 0.30}")
t.check("11. first SELL is reference - 0.30",
        abs(sells[0] - (ref - 0.30)) < 1e-9, f"{sells[0]} vs {ref - 0.30}")
t.check("11. both sides came from the SAME reference",
        abs((buys[0] - ref) - (ref - sells[0])) < 1e-9,
        f"+{buys[0] - ref} / -{ref - sells[0]}")
timing = eng.ladder_timing
t.check("11. the reference is recorded with the placement",
        timing["reference_price"] == ref, str(timing.get("reference_price")))
t.check("11. so is the placement latency",
        timing["placement_duration_ms"] > 0,
        str(timing.get("placement_duration_ms")))

t.section("18. ONE ACTIVE CYCLE IS STILL ENFORCED")
t.check("18. a second ladder is refused while one is active",
        eng.create_new_ladder(50, "should be refused") is False)
t.check("18. the active ladder id is unchanged", eng.active_ladder_id == 1,
        str(eng.active_ladder_id))

t.section("12-13. TELEGRAM AND CSV STILL CANNOT BLOCK AN EXIT")
mt5.reset()
mt5.set_price(4010.00)
mt5.initialize()
settings = RuntimeSettings(cfg.runtime_defaults(), TMP / "block_s.json")
settings._values.update({"ladder_spacing": 0.30, "ladder_depth": 11,
                         "max_pending_orders": 22, "max_open_positions": 22,
                         "stop_loss_distance": 0, "profit_runner_enabled": False,
                         "basket_profit_target": 2.00,
                         "telemetry_interval_seconds": 0})
broker = Mt5Broker("XAUUSD", 88001199)
rec = Recorder()
eng = RollingLadderEngine(broker, settings, hooks=rec.hooks(), state_path=None)
eng.resume()
eng.step()
for o in sorted([x for x in broker.orders() if x.side == BUY_STOP],
                key=lambda x: x.price)[:5]:
    mt5.trigger_order(o.ticket)
eng.step()
import time as _time
slow = []
hooks = rec.hooks()
_ev = hooks["event"]
hooks["event"] = lambda e, m, f: (_ev(e, m, f), slow.append(m),
                                  _time.sleep(0.2))[0]
hooks["telemetry"] = lambda row: _time.sleep(0.2)
eng.hooks = hooks
eng.bar_time = lambda: (_ for _ in ()).throw(
    AssertionError("the exit path must not read a candle"))
legs = broker.positions()
mt5.set_price(round(sum(p.price_open for p in legs) / len(legs) + 5.0, 2))
reason = eng.check_exit_now()
t.check("12-13. the exit fired despite slow hooks", reason is not None,
        str(reason))
t.check("12-13. slow hooks were genuinely in the path", len(slow) >= 1,
        f"{len(slow)} calls")
t.check("12-13. and the account is flat",
        not broker.positions() and not broker.orders(),
        f"{len(broker.positions())}p / {len(broker.orders())}o")

t.section("14. REALIZED P/L COMES FROM MT5 HISTORY, NOT FROM FLOATING")
for _ in range(3):
    eng.step()
closes = [c for c in rec.cycles if c.kind_of == "complete"]
ctx = closes[-1].context if closes else {}
t.check("14. the cycle recorded a realized figure",
        "realized_pnl_after_close" in ctx, str(sorted(ctx)[:6]))
t.check("14. and the floating figure it was decided on, separately",
        "floating_pnl_before_close" in ctx)
deals = [d for d in mt5.STATE["deals"] if d.entry == mt5.DEAL_ENTRY_OUT]
from_history = round(sum(d.profit for d in deals), 2)
t.check("14. realized matches the sum of the MT5 OUT deals",
        abs(float(ctx.get("realized_pnl_after_close", 0)) - from_history) < 0.01,
        f"{ctx.get('realized_pnl_after_close')} vs {from_history}")
t.check("14. the difference from floating is recorded as slippage",
        "pnl_slippage" in ctx, str(ctx.get("pnl_slippage")))

t.section("15-17. THE EXISTING EXITS ARE UNTOUCHED")
protect = basket(3, [1.0, 4.0, 5.0, 3.2], prices=RISING)
action, reason, _ = protect.decide(R, has_exposure=True)
t.check("15. profit protection still fires on a give-back",
        action == EXIT and reason == "PROFIT_PROTECTION", f"{action} {reason}")
shallow_deep = basket(3, [1.0, 1.5], prices=RISING)
t.check("15. and a shallow basket is unaffected by the deep rules",
        shallow_deep.expansion_allowed and
        shallow_deep.deep_ladder_state == DEEP_HEALTHY,
        shallow_deep.deep_ladder_state)
import inspect
import ladder_engine as engine_mod
src = inspect.getsource(engine_mod.RollingLadderEngine._exit_reason)
t.check("16. the hard cycle drawdown guard is still ahead of the strategy",
        "max_cycle_drawdown" in src and
        src.index("max_cycle_drawdown") < src.index("should_exit"), src[:80])
t.check("17. so is the cycle duration guard",
        "max_cycle_duration_minutes" in src and
        src.index("max_cycle_duration_minutes") < src.index("should_exit"))
t.check("there is still exactly ONE strategy decision site",
        inspect.getsource(engine_mod).count("self.sequence.should_exit(") == 1)

t.section("TELEMETRY CARRIES THE DEEP-LADDER FIELDS")
import csv_logger
for column in ("depth_zone", "deep_ladder_state", "directional_imbalance"
               if False else "direction_imbalance", "recovery_state",
               "recovery_start_pnl", "recovery_amount", "recovery_duration",
               "recovery_speed", "price_state", "price_velocity",
               "expansion_allowed", "expansion_block_reason", "risk_score",
               "risk_state", "current_pnl", "peak_pnl", "lowest_pnl",
               "drawdown_from_peak", "max_floating_loss", "open_positions",
               "pending_orders", "buy_volume", "sell_volume", "ladder_depth",
               "triggers", "direction_changes", "cycle_state"):
    t.check(f"basket telemetry has {column!r}",
            column in csv_logger.TELEMETRY_HEADER, column)
for column in ("depth_zone", "deep_ladder_state", "risk_score", "risk_state",
               "expansion_allowed", "expansion_block_reason"):
    t.check(f"the cycle summary has {column!r}",
            column in csv_logger.CYCLE_HEADER, column)

t.done()
