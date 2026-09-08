"""
Ladder GEOMETRY and EXPOSURE: where levels go, and when to stop adding them.

The geometry half holds the intended shape - first level exactly one spacing
from the reference price, exact increments after that - and proves the
telemetry can tell an intentional shape from a leaning one.

The exposure half is about not letting a cycle grow without bound: depth
grading, volume-based imbalance, and the rule that reaching a limit withholds
NEW exposure without touching what is already open.
"""
import pathlib
import shutil
import threading
import time
from types import SimpleNamespace

from harness import Suite, use_stub_mt5
use_stub_mt5()

import MetaTrader5 as mt5
import config as cfg
from basket import (BALANCED, BUY_HEAVY, EXTREMELY_IMBALANCED, LADDER_DEEP,
                    LADDER_EXTENDED, LADDER_MAX_DEPTH, LADDER_NORMAL,
                    NO_RECOVERY, SELL_HEAVY, STRONG_RECOVERY, WEAK_RECOVERY,
                    RECOVERING_QUALITY, CycleBasket, ProfitRules)
from broker import BUY, BUY_STOP, SELL, SELL_STOP, Mt5Broker
from fakes import Recorder
from ladder_engine import RollingLadderEngine
from runtime_settings import RuntimeSettings

t = Suite("ladder_geometry")
TMP = pathlib.Path("/tmp/geometry_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)

SPACING = 0.30


def live(name, overrides=None, bid=4010.00, spread=0.08, stops_level=0):
    mt5.reset()
    mt5.set_price(bid, round(bid + spread, 2))
    mt5.STATE["stops_level"] = stops_level
    mt5.initialize()
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}_s.json")
    settings._values.update({"ladder_spacing": SPACING, "ladder_depth": 11,
                             "max_pending_orders": 22, "max_open_positions": 22,
                             "max_ladder_depth": 22, "stop_loss_distance": 0,
                             "telemetry_interval_seconds": 0})
    settings._values.update(overrides or {})
    broker = Mt5Broker("XAUUSD", 88001199)
    rec = Recorder()
    eng = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                              state_path=None)
    eng.resume()
    return eng, broker, settings, rec


def sides(broker):
    o = broker.orders()
    return (sorted(x.price for x in o if x.side == BUY_STOP),
            sorted((x.price for x in o if x.side == SELL_STOP), reverse=True))


def pos(side, volume=0.01, price=4010.0):
    return SimpleNamespace(side=side, volume=volume, price_open=price)


# ===========================================================================
t.section("1-3. THE INTENDED GEOMETRY IS EXACT")
for spread in (0.08, 0.20, 0.50):
    eng, broker, settings, rec = live(f"geo{spread}", spread=spread)
    eng.step()
    buys, sells = sides(broker)
    ref = eng.cycle.anchor
    t.check(f"1. first BUY is exactly +0.30 at spread {spread}",
            abs(buys[0] - (ref + SPACING)) < 1e-9,
            f"{buys[0]} vs {ref} + {SPACING} = {ref + SPACING}")
    t.check(f"2. first SELL is exactly -0.30 at spread {spread}",
            abs(sells[0] - (ref - SPACING)) < 1e-9,
            f"{sells[0]} vs {ref} - {SPACING} = {ref - SPACING}")
    t.check(f"3. every BUY step is exactly 0.30 at spread {spread}",
            all(abs(buys[i + 1] - buys[i] - SPACING) < 1e-9
                for i in range(len(buys) - 1)),
            str([round(buys[i + 1] - buys[i], 4) for i in range(3)]))
    t.check(f"3. every SELL step is exactly 0.30 at spread {spread}",
            all(abs(sells[i] - sells[i + 1] - SPACING) < 1e-9
                for i in range(len(sells) - 1)),
            str([round(sells[i] - sells[i + 1], 4) for i in range(3)]))
    t.check(f"3. the ladder is symmetric about the reference at {spread}",
            abs((buys[0] - ref) - (ref - sells[0])) < 1e-9,
            f"+{buys[0] - ref:.4f} / -{ref - sells[0]:.4f}")
    t.check(f"and both sides are still legal stops at spread {spread}",
            buys[0] > broker.tick().ask and sells[0] < broker.tick().bid)

t.section("4. THE REFERENCE PRICE IS CAPTURED ONCE")
eng, broker, settings, rec = live("ref")
eng.step()
ref = eng.cycle.anchor
buys_before, _ = sides(broker)
for bid in (4010.50, 4011.20, 4009.40):
    mt5.set_price(bid)
    eng.step()
t.check("4. the reference does not drift with price",
        eng.cycle.anchor == ref, f"{eng.cycle.anchor} vs {ref}")
t.check("4. and levels already placed do not move",
        sides(broker)[0][:3] == buys_before[:3],
        f"{sides(broker)[0][:3]} vs {buys_before[:3]}")
t.check("4. every level still sits on the reference grid",
        all(abs((p - ref) / SPACING - round((p - ref) / SPACING)) < 1e-6
            for p in sides(broker)[0] + sides(broker)[1]))

t.section("5. A BROKER CONSTRAINT IS LOGGED, NEVER SILENTLY ABSORBED")
# stops_level 100 points = 1.00 price unit: the first three levels each side
# are inside the broker's minimum stop distance and cannot be placed.
eng, broker, settings, rec = live("stops", stops_level=100)
eng.step()
buys, sells = sides(broker)
ref = eng.cycle.anchor
skipped = [e for e in rec.events if e[0] == "LEVEL_SKIPPED"]
t.check("5. the blocked levels are reported", len(skipped) >= 2,
        f"{len(skipped)} skipped")
t.check("5. the log names the intended price and the reason",
        skipped and "intended" in skipped[0][1] and
        "minimum stop distance" in skipped[0][1], skipped[0][1] if skipped else "")
t.check("5. the levels that WERE placed keep exact 0.30 spacing",
        all(abs(buys[i + 1] - buys[i] - SPACING) < 1e-9
            for i in range(len(buys) - 1)), str(buys[:4]))
t.check("5. and they still sit on the intended grid - nothing was nudged",
        all(abs((p - ref) / SPACING - round((p - ref) / SPACING)) < 1e-6
            for p in buys + sells), str(buys[:3]))
t.check("5. the nearest placed level is outside the broker's limit",
        buys[0] >= broker.tick().ask + 1.00 - 1e-9,
        f"{buys[0]} vs {broker.tick().ask} + 1.00")

t.section("6. PLACEMENT IS MEASURED")
eng, broker, settings, rec = live("timing")
eng.step()
timing = eng.ladder_timing
for key in ("placement_start_timestamp", "placement_end_timestamp",
            "placement_duration_ms", "reference_price",
            "intended_first_buy", "actual_first_buy",
            "intended_first_sell", "actual_first_sell",
            "first_buy_distance", "first_sell_distance",
            "buy_spacing", "sell_spacing",
            "price_at_first_order", "price_at_last_order", "levels_skipped"):
    t.check(f"6. placement telemetry has {key!r}", key in timing,
            str(sorted(timing)[:5]))
t.check("6. intended and actual agree when nothing blocked them",
        timing["intended_first_buy"] == timing["actual_first_buy"] and
        timing["intended_first_sell"] == timing["actual_first_sell"],
        f"{timing['intended_first_buy']}/{timing['actual_first_buy']}")
t.check("6. the measured first-level distance is 0.30 either side",
        abs(timing["first_buy_distance"] - SPACING) < 1e-9 and
        abs(timing["first_sell_distance"] - SPACING) < 1e-9,
        f"{timing['first_buy_distance']} / {timing['first_sell_distance']}")
t.check("6. the measured spacing is 0.30 either side",
        abs(timing["buy_spacing"] - SPACING) < 1e-9 and
        abs(timing["sell_spacing"] - SPACING) < 1e-9,
        f"{timing['buy_spacing']} / {timing['sell_spacing']}")
t.check("6. placement duration is a real measurement",
        timing["placement_duration_ms"] > 0, str(timing["placement_duration_ms"]))

t.section("7-8. MAX LADDER DEPTH STOPS EXPOSURE, NOT THE BASKET")
rules = ProfitRules(max_ladder_depth=12, extended_depth=6, deep_depth=9,
                    critical_depth=11,
                    max_imbalance=0.80, imbalance_action="MONITOR")
for used, want in ((0, LADDER_NORMAL), (5, LADDER_NORMAL),
                   (6, LADDER_EXTENDED), (9, LADDER_DEEP),
                   (12, LADDER_MAX_DEPTH)):
    b = CycleBasket(1, 4010.0, SPACING, started_at=0.0)
    b.triggers = [SimpleNamespace(side=BUY if i % 2 else SELL, index=i,
                                  price=0.0, ts=0.0) for i in range(used)]
    b.update_exposure([pos(BUY)] * max(1, used // 2) +
                      [pos(SELL)] * max(1, used // 2))
    b.grade_exposure(rules)
    t.check(f"7. depth {used}/12 grades as {want}", b.ladder_state == want,
            b.ladder_state)
t.check("7. only the cap withholds new exposure", b.exposure_capped)

eng, broker, settings, rec = live("depthcap", {"max_ladder_depth": 3,
                                               "ladder_depth": 6,
                                               "max_pending_orders": 12})
eng.step()
for o in sorted([x for x in broker.orders() if x.side == BUY_STOP],
                key=lambda x: x.price)[:3]:
    mt5.trigger_order(o.ticket)
eng.step()
eng.step()
t.check("7. the cap was reached", eng.sequence.ladder_state == LADDER_MAX_DEPTH,
        eng.sequence.ladder_state)
t.check("7. no pending levels are left to add exposure", not broker.orders(),
        f"{len(broker.orders())} orders")
t.check("8. the OPEN positions are untouched", len(broker.positions()) == 3,
        f"{len(broker.positions())} positions")
t.check("8. the cycle is still active and managed",
        eng.cycle_active and eng.sequence is not None)
t.check("8. and the exit engine still gets a decision",
        eng.sequence.decide(eng.profit_rules(settings.snapshot()),
                            has_exposure=True)[0] in ("HOLD", "PROTECT", "EXIT"))
t.check("8. expansion is paused, and logged once",
        len([e for e in rec.events if e[0] == "LADDER_EXPANSION_PAUSED"]) == 1,
        str([e[0] for e in rec.events if e[0] == "LADDER_EXPANSION_PAUSED"]))

t.section("9. IMBALANCE IS MEASURED ON VOLUME, NOT ORDER COUNT")
rules = ProfitRules(max_imbalance=0.80, imbalance_action="MONITOR",
                    imbalance_min_positions=4)
cases = [
    ("6 BUY / 6 SELL", [pos(BUY)] * 6 + [pos(SELL)] * 6, 0.00, BALANCED),
    ("11 BUY / 1 SELL", [pos(BUY)] * 11 + [pos(SELL)], 0.8333,
     EXTREMELY_IMBALANCED),
    ("8 BUY / 4 SELL", [pos(BUY)] * 8 + [pos(SELL)] * 4, 0.3333, BALANCED),
    ("9 BUY / 3 SELL", [pos(BUY)] * 9 + [pos(SELL)] * 3, 0.50, BUY_HEAVY),
    ("3 BUY / 9 SELL", [pos(BUY)] * 3 + [pos(SELL)] * 9, 0.50, SELL_HEAVY),
    # counts say 11-vs-1; volume says perfectly balanced, and volume is right
    ("11x0.01 BUY / 1x0.11 SELL", [pos(BUY, 0.01)] * 11 + [pos(SELL, 0.11)],
     0.00, BALANCED),
]
for label, legs, want_imb, want_state in cases:
    b = CycleBasket(1, 4010.0, SPACING, started_at=0.0)
    b.update_exposure(legs)
    b.grade_exposure(rules)
    t.check(f"9. {label} -> imbalance {want_imb:.2f}",
            abs(b.direction_imbalance - want_imb) < 0.001,
            f"{b.direction_imbalance}")
    t.check(f"9. {label} -> {want_state}", b.imbalance_state == want_state,
            b.imbalance_state)
b = CycleBasket(1, 4010.0, SPACING, started_at=0.0)
b.update_exposure([pos(BUY)])
b.grade_exposure(rules)
t.check("9. a single leg is not called imbalanced (nothing to compare)",
        b.imbalance_state == BALANCED, b.imbalance_state)
t.check("9. net direction follows the volume",
        b.net_direction == BUY, b.net_direction)

t.section("10-11. IMBALANCE ACTION IS CONFIGURABLE")
one_sided = [pos(BUY)] * 11 + [pos(SELL)]
monitor = ProfitRules(max_imbalance=0.80, imbalance_action="MONITOR",
                      imbalance_min_positions=4, max_ladder_depth=0)
stop = ProfitRules(max_imbalance=0.80, imbalance_action="STOP_NEW_EXPOSURE",
                   imbalance_min_positions=4, max_ladder_depth=0)
b = CycleBasket(1, 4010.0, SPACING, started_at=0.0)
b.update_exposure(one_sided)
b.grade_exposure(monitor)
t.check("10. MONITOR records the imbalance without acting",
        b.imbalance_state == EXTREMELY_IMBALANCED and not b.exposure_capped,
        f"{b.imbalance_state} capped={b.exposure_capped}")
b.grade_exposure(stop)
t.check("10. STOP_NEW_EXPOSURE withholds new exposure",
        b.exposure_capped and "imbalance" in b.exposure_cap_reason,
        b.exposure_cap_reason)
t.check("10. it never closes anything by itself",
        b.decide(stop, has_exposure=True)[0] in ("HOLD", "PROTECT"),
        str(b.decide(stop, has_exposure=True)))
balanced = CycleBasket(2, 4010.0, SPACING, started_at=0.0)
balanced.update_exposure([pos(BUY)] * 6 + [pos(SELL)] * 6)
balanced.grade_exposure(stop)
t.check("11. a balanced basket is never capped for imbalance",
        not balanced.exposure_capped, balanced.exposure_cap_reason)
t.check("11. the peak imbalance is remembered for the cycle summary",
        b.max_direction_imbalance > 0.8, str(b.max_direction_imbalance))

t.section("12-14. RECOVERY DETECTION AND QUALITY")
rr = ProfitRules(underwater_at=2.0, recovery_fraction=0.50,
                 weak_recovery_at=0.25, strong_recovery_at=0.75,
                 target=2.0, recovery_take=0.50)
b = CycleBasket(1, 4010.0, SPACING, started_at=0.0)
seen = []
for i, pnl in enumerate([-0.5, -5.0, -20.0, -18.0, -14.0, -8.0, -3.0], start=1):
    b.mark(float(pnl), rr, float(i))
    seen.append((pnl, b.recovery_quality))
t.check("12. a small dip is not a recovery episode",
        seen[0][1] == NO_RECOVERY, str(seen[0]))
t.check("12. the hole is remembered", b.lowest_pnl == -20.0, str(b.lowest_pnl))
t.check("12. the episode start is recorded",
        b.recovery_start_pnl is not None and b.recovery_count == 1,
        f"{b.recovery_start_pnl} / {b.recovery_count}")
t.check("12. the depth at the start of recovery is captured",
        b.ladder_depth_at_recovery_start == 0,
        str(b.ladder_depth_at_recovery_start))
t.check("13. a 30% climb back is WEAK",
        dict(seen)[-14.0] == WEAK_RECOVERY, dict(seen)[-14.0])
t.check("13. a 60% climb back is RECOVERING",
        dict(seen)[-8.0] == RECOVERING_QUALITY, dict(seen)[-8.0])
t.check("14. an 85% climb back is STRONG",
        dict(seen)[-3.0] == STRONG_RECOVERY, dict(seen)[-3.0])
t.check("14. the recovery is quantified",
        b.recovery_amount == 17.0 and b.recovery_duration > 0,
        f"{b.recovery_amount} over {b.recovery_duration}s")
t.check("14. recovery speed is amount per second",
        b.recovery_speed > 0, str(b.recovery_speed))

t.section("15. PRICE MOVEMENT IS READ RELATIVE TO THE BASKET")
def moved(legs, prices):
    b = CycleBasket(1, 4010.0, SPACING, started_at=0.0)
    for i, p in enumerate(prices, start=1):
        b.observe_price(p, float(i), legs)
    return b.price_state


t.check("15. BUY-heavy + rising = FAVORABLE",
        moved([pos(BUY)] * 4, [4010.0, 4010.5, 4011.0]) == "FAVORABLE")
t.check("15. BUY-heavy + falling = ADVERSE",
        moved([pos(BUY)] * 4, [4010.0, 4009.5, 4009.0]) == "ADVERSE")
t.check("15. SELL-heavy + falling = FAVORABLE",
        moved([pos(SELL)] * 4, [4010.0, 4009.5, 4009.0]) == "FAVORABLE")
t.check("15. SELL-heavy + rising = ADVERSE",
        moved([pos(SELL)] * 4, [4010.0, 4010.5, 4011.0]) == "ADVERSE")
t.check("15. a balanced basket has no directional reading",
        moved([pos(BUY)] * 3 + [pos(SELL)] * 3,
              [4010.0, 4011.0]) == "FLAT")
t.check("15. a move smaller than a tenth of a level is FLAT",
        moved([pos(BUY)] * 4, [4010.0, 4010.01]) == "FLAT")

t.section("16-18. FLOATING P/L IS THE MANAGED QUANTITY")
b = CycleBasket(1, 4010.0, SPACING, started_at=0.0)
for pnl in (1.0, 4.0, 2.5):
    b.mark(pnl, rr, 1.0)
t.check("16. peak is the highest floating P/L reached",
        b.peak_pnl == 4.0, str(b.peak_pnl))
t.check("16. peak never moves backwards", b.peak_pnl >= b.floating_pnl)
t.check("17. drawdown from peak is peak - current",
        abs(b.drawdown - 1.5) < 1e-9, str(b.drawdown))
b.record_close(BUY, 1, 4010.0, 4012.0, 2.00)
b.mark(0.5, rr, 2.0)
t.check("18. realized is banked separately", b.realized_pnl == 2.0,
        str(b.realized_pnl))
t.check("18. current P/L is what is still OPEN", b.floating_pnl == 0.5,
        str(b.floating_pnl))
t.check("18. realized is NOT folded into the managed figure",
        b.peak_pnl == 4.0 and b.drawdown == 3.5,
        f"peak {b.peak_pnl} drawdown {b.drawdown}")
t.check("18. the cycle total is still available, separately",
        abs(b.basket_pnl - 2.5) < 1e-9, str(b.basket_pnl))

t.section("19-20. THE EXIT DECISION IS DETERMINISTIC AND SINGULAR")
def decide_twice(pnls):
    out = []
    for _ in range(2):
        b = CycleBasket(1, 4010.0, SPACING, started_at=0.0)
        for i, p in enumerate(pnls, start=1):
            b.mark(float(p), rr, float(i))
        out.append(b.decide(rr, has_exposure=True))
    return out


for path in ([1.0, 2.5], [-20.0, -3.0, 1.0], [5.0, 4.0, 2.0]):
    a, c = decide_twice(path)
    t.check(f"19. {path} decides identically every time", a == c,
            f"{a} vs {c}")
import basket as basket_mod
import ladder_engine as engine_mod
import inspect
sources = inspect.getsource(engine_mod)
t.check("20. the engine reaches the decision through ONE method",
        sources.count("self.sequence.should_exit(") == 1,
        str(sources.count("self.sequence.should_exit(")))
t.check("20. and should_exit is a thin adapter over decide()",
        "self.decide(" in inspect.getsource(basket_mod.CycleBasket.should_exit))
t.check("20. every exit reason is in the declared set",
        all(r in basket_mod.EXIT_REASONS for r in
            (basket_mod.BASKET_PROFIT_TARGET, basket_mod.PROFIT_PROTECTION,
             basket_mod.RECOVERY_PROFIT, basket_mod.PROFIT_GIVEBACK,
             basket_mod.STRONG_RECOVERY_PROTECTION,
             basket_mod.DEEP_LADDER_RISK,
             basket_mod.DIRECTION_IMBALANCE_RISK)))

t.section("21-23. THE EXIT PATH DEPENDS ON NOTHING SLOW")
# runner off: this section is about the exit PATH, so the target should be a
# hard exit rather than something the trail holds while price still climbs
eng, broker, settings, rec = live("independent",
                                  {"profit_runner_enabled": False})
eng.step()
for o in sorted([x for x in broker.orders() if x.side == BUY_STOP],
                key=lambda x: x.price)[:5]:
    mt5.trigger_order(o.ticket)
eng.step()
calls = {"csv": 0, "telegram": 0, "rates": 0}
hooks = rec.hooks()
_ev = hooks["event"]
hooks["event"] = lambda e, m, f: (_ev(e, m, f), calls.__setitem__(
    "csv", calls["csv"] + 1))[0]
hooks["telemetry"] = lambda row: calls.__setitem__("csv", calls["csv"] + 1)
eng.hooks = hooks
real_rates = mt5.copy_rates_from_pos
mt5.copy_rates_from_pos = lambda *a, **k: (
    calls.__setitem__("rates", calls["rates"] + 1), real_rates(*a, **k))[1]
eng.bar_time = lambda: (_ for _ in ()).throw(
    AssertionError("the exit path must not ask for a candle"))
try:
    pos_now = broker.positions()
    target = round(sum(p.price_open for p in pos_now) / len(pos_now) + 5.0, 2)
    mt5.set_price(target)
    reason = eng.check_exit_now()
finally:
    mt5.copy_rates_from_pos = real_rates
t.check("21. the exit fired", reason is not None, str(reason))
t.check("23. it never asked for a candle", calls["rates"] == 0,
        f"{calls['rates']} rate calls")
t.check("22-23. and the account is flat",
        not broker.positions() and not broker.orders())

t.section("24. LADDER PLACEMENT RACING AN EXIT")
eng, broker, settings, rec = live("race", {"ladder_depth": 11,
                                          "profit_runner_enabled": False})
eng.step()
for o in sorted([x for x in broker.orders() if x.side == BUY_STOP],
                key=lambda x: x.price)[:4]:
    mt5.trigger_order(o.ticket)
eng.step()
pos_now = broker.positions()
mt5.set_price(round(sum(p.price_open for p in pos_now) / len(pos_now) + 5.0, 2))
barrier = threading.Barrier(2, timeout=5)
results = {}


def placer():
    barrier.wait()
    for _ in range(6):
        try:
            eng.step()
        except Exception as exc:
            results["place_error"] = repr(exc)


def exiter():
    barrier.wait()
    try:
        results["reason"] = eng.check_exit_now()
    except Exception as exc:
        results["exit_error"] = repr(exc)


threads = [threading.Thread(target=placer), threading.Thread(target=exiter)]
for th in threads:
    th.start()
for th in threads:
    th.join(timeout=10)
t.check("24. neither thread raised",
        "place_error" not in results and "exit_error" not in results,
        str(results))
t.check("24. the exit still committed", results.get("reason") is not None,
        str(results))
for _ in range(4):
    eng.step()
t.check("25. flat verification cleans up anything that raced through",
        not broker.positions() and not broker.orders(),
        f"{len(broker.positions())}p / {len(broker.orders())}o")
t.check("26. and no second cycle was created while closing",
        rec.count("CYCLE_STARTED") == 1, str(rec.count("CYCLE_STARTED")))

t.section("30. TELEMETRY CARRIES THE NEW FIELDS")
import csv_logger
for column in ("lowest_pnl", "recovery_state", "recovery_quality",
               "recovery_start_pnl", "recovery_duration",
               "ladder_depth_at_recovery_start",
               "pending_buys", "pending_sells", "gross_volume",
               "net_direction", "direction_imbalance", "imbalance_state",
               "max_ladder_depth", "ladder_state", "exposure_capped",
               "price_state", "recent_price_change",
               "price_velocity", "favorable_price_movement",
               "adverse_price_movement", "basket_state", "exit_decision",
               "exit_reason", "reference_price", "first_buy_distance",
               "first_sell_distance", "protection_active",
               "protection_threshold", "elapsed_seconds", "time_underwater",
               "time_in_profit"):
    t.check(f"30. basket telemetry has {column!r}",
            column in csv_logger.TELEMETRY_HEADER, column)
for column in ("reference_price", "initial_price", "exit_price",
               "first_buy_distance", "first_sell_distance",
               "average_buy_spacing", "average_sell_spacing",
               "duration_seconds", "triggers", "buy_triggers", "sell_triggers",
               "direction_changes", "max_ladder_depth", "ladder_depth_used",
               "max_drawdown", "max_floating_loss", "peak_pnl",
               "floating_pnl_before_close", "recovery_count",
               "strong_recovery_count", "weak_recovery_count",
               "time_underwater", "recovery_duration", "recovery_speed",
               "maximum_direction_imbalance", "exit_reason", "exit_state",
               "detection_latency_ms", "close_request_latency_ms",
               "total_exit_latency_ms", "realized_pnl_after_close",
               "final_realized_pnl"):
    t.check(f"30. the cycle summary has {column!r}",
            column in csv_logger.CYCLE_HEADER, column)

t.done()
