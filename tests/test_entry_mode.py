"""
ENTRY_MODE: FULL_LADDER vs SINGLE_PAIR.

SINGLE_PAIR places ONE buy stop and ONE sell stop, and replaces a level the
instant it triggers. It is the same strategy: same reference price, same 0.30
geometry, same exit engine, same risk controls, same lot size. The only
difference is how many levels are pending at once.

The bar this suite holds:
  * FULL_LADDER is byte-for-byte the behaviour it always had
  * SINGLE_PAIR never has more than one pending per side, ever
  * every level is measured from the ONE immutable cycle reference
  * replacement happens on the fast path - no candle, no ladder pass, and
    nothing that can be blocked by Telegram or CSV
  * the exit and the deep-ladder risk engine both outrank replacement
"""
import pathlib
import shutil
import time
from types import SimpleNamespace

from harness import Suite, use_stub_mt5
use_stub_mt5()

import MetaTrader5 as mt5
import config as cfg
from broker import BUY, BUY_STOP, SELL, SELL_STOP, Mt5Broker
from fakes import Recorder
from ladder_engine import RollingLadderEngine, parse_comment
from runtime_settings import RuntimeSettings

t = Suite("entry_mode")
TMP = pathlib.Path("/tmp/entry_mode_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)

SPACING = 0.30
REF_BID = 4422.00


def build(name, mode, overrides=None, bid=REF_BID, spread=0.08):
    mt5.reset()
    mt5.set_price(bid, round(bid + spread, 2))
    mt5.initialize()
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}_s.json")
    settings._values.update({"entry_mode": mode, "ladder_spacing": SPACING,
                             "ladder_depth": 11, "max_pending_orders": 22,
                             "max_open_positions": 22, "stop_loss_distance": 0,
                             "telemetry_interval_seconds": 0})
    settings._values.update(overrides or {})
    broker = Mt5Broker("XAUUSD", 88001199)
    rec = Recorder()
    eng = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                              state_path=None)
    eng.resume()
    eng.step()
    return eng, broker, settings, rec


def pend(broker):
    o = broker.orders()
    return (sorted(x.price for x in o if x.side == BUY_STOP),
            sorted((x.price for x in o if x.side == SELL_STOP), reverse=True))


def trigger(broker, side):
    """Fire the pending order nearest the market on one side."""
    orders = [x for x in broker.orders() if x.side == side]
    if not orders:
        return None
    target = (min(orders, key=lambda o: o.price) if side == BUY_STOP
              else max(orders, key=lambda o: o.price))
    mt5.trigger_order(target.ticket)
    return target.price


def no_candles(eng):
    """Make any candle read on this path an immediate, loud failure."""
    eng.bar_time = lambda: (_ for _ in ()).throw(
        AssertionError("the replacement path read a candle"))


# ===========================================================================
t.section("A. FULL_LADDER IS UNCHANGED")
eng, broker, settings, rec = build("full", "FULL_LADDER")
buys, sells = pend(broker)
ref = eng.cycle.anchor
t.check("A. it is still the default",
        cfg.runtime_defaults()["entry_mode"] == "FULL_LADDER",
        cfg.runtime_defaults()["entry_mode"])
t.check("A. 11 BUY STOP + 11 SELL STOP, placed at once",
        len(buys) == 11 and len(sells) == 11, f"{len(buys)}B/{len(sells)}S")
t.check("A. first BUY is still reference + 0.30",
        abs(buys[0] - (ref + SPACING)) < 1e-9, f"{buys[0]} vs {ref + SPACING}")
t.check("A. first SELL is still reference - 0.30",
        abs(sells[0] - (ref - SPACING)) < 1e-9, f"{sells[0]} vs {ref - SPACING}")
t.check("A. spacing is still exactly 0.30",
        all(abs(buys[i + 1] - buys[i] - SPACING) < 1e-9
            for i in range(len(buys) - 1)))
trigger(broker, BUY_STOP)
eng.step()
after_buys, after_sells = pend(broker)
t.check("A. a trigger does NOT create a replacement in FULL_LADDER",
        len(after_buys) == 10 and len(after_sells) == 11,
        f"{len(after_buys)}B/{len(after_sells)}S")
t.check("A. and no replacement was recorded", eng.replacements == 0,
        str(eng.replacements))

t.section("B. SINGLE_PAIR STARTS WITH EXACTLY ONE PAIR")
eng, broker, settings, rec = build("init", "SINGLE_PAIR")
buys, sells = pend(broker)
ref = eng.cycle.anchor
t.check("B. exactly 1 BUY pending", len(buys) == 1, str(len(buys)))
t.check("B. exactly 1 SELL pending", len(sells) == 1, str(len(sells)))
t.check("B. and nothing else at all", len(broker.orders()) == 2,
        str(len(broker.orders())))
t.check("B. BUY is reference + 0.30",
        abs(buys[0] - (ref + SPACING)) < 1e-9, f"{buys[0]} vs {ref + SPACING}")
t.check("B. SELL is reference - 0.30",
        abs(sells[0] - (ref - SPACING)) < 1e-9, f"{sells[0]} vs {ref - SPACING}")
t.check("B. both sides from the SAME reference",
        abs((buys[0] - ref) - (ref - sells[0])) < 1e-9,
        f"+{buys[0] - ref} / -{ref - sells[0]}")
t.check("B. the initial counts are recorded",
        eng.initial_pending_buys == 1 and eng.initial_pending_sells == 1,
        f"{eng.initial_pending_buys}/{eng.initial_pending_sells}")

t.section("C. A TRIGGERED BUY IS REPLACED, THE SELL IS LEFT ALONE")
eng, broker, settings, rec = build("cbuy", "SINGLE_PAIR")
no_candles(eng)
ref = eng.cycle.anchor
sell_before = pend(broker)[1][0]
fired = trigger(broker, BUY_STOP)
eng.check_exit_now()                       # the fast path, no _step at all
buys, sells = pend(broker)
t.check("C. the next BUY went out", len(buys) == 1, str(buys))
t.check("C. at exactly reference + 0.60",
        abs(buys[0] - (ref + 2 * SPACING)) < 1e-9,
        f"{buys[0]} vs {ref + 2 * SPACING}")
t.check("C. which is 0.30 past the level that fired",
        abs(buys[0] - fired - SPACING) < 1e-9, f"{buys[0]} vs {fired} + 0.30")
t.check("C. the SELL is untouched", len(sells) == 1 and sells[0] == sell_before,
        f"{sells} vs {sell_before}")
t.check("C. the replacement was logged once",
        rec.count("LEVEL_REPLACED") == 1, str(rec.count("LEVEL_REPLACED")))

t.section("D. MULTIPLE BUY ROLLOVERS")
eng, broker, settings, rec = build("dbuy", "SINGLE_PAIR")
no_candles(eng)
ref = eng.cycle.anchor
seen = []
for n in range(2, 7):
    trigger(broker, BUY_STOP)
    eng.check_exit_now()
    buys, sells = pend(broker)
    seen.append(buys[0])
    t.check(f"D. after {n - 1} triggers the BUY is reference + {n * SPACING:.2f}",
            abs(buys[0] - (ref + n * SPACING)) < 1e-9,
            f"{buys[0]} vs {ref + n * SPACING}")
    t.check(f"D. still exactly one BUY pending ({n})", len(buys) == 1, str(buys))
    t.check(f"D. still exactly one SELL pending ({n})", len(sells) == 1, str(sells))
t.check("D. every step was exactly 0.30",
        all(abs(seen[i + 1] - seen[i] - SPACING) < 1e-9
            for i in range(len(seen) - 1)),
        str([round(seen[i + 1] - seen[i], 4) for i in range(len(seen) - 1)]))

t.section("E. MULTIPLE SELL ROLLOVERS")
eng, broker, settings, rec = build("esell", "SINGLE_PAIR")
no_candles(eng)
ref = eng.cycle.anchor
seen = []
for n in range(2, 7):
    trigger(broker, SELL_STOP)
    eng.check_exit_now()
    buys, sells = pend(broker)
    seen.append(sells[0])
    t.check(f"E. after {n - 1} triggers the SELL is reference - {n * SPACING:.2f}",
            abs(sells[0] - (ref - n * SPACING)) < 1e-9,
            f"{sells[0]} vs {ref - n * SPACING}")
    t.check(f"E. still exactly one SELL pending ({n})", len(sells) == 1, str(sells))
    t.check(f"E. the BUY side is untouched ({n})", len(buys) == 1, str(buys))
t.check("E. every step was exactly 0.30 downward",
        all(abs(seen[i] - seen[i + 1] - SPACING) < 1e-9
            for i in range(len(seen) - 1)),
        str([round(seen[i] - seen[i + 1], 4) for i in range(len(seen) - 1)]))

t.section("F. LEVELS COME FROM THE REFERENCE, NOT FROM THE MARKET")
eng, broker, settings, rec = build("fref", "SINGLE_PAIR")
no_candles(eng)
ref = eng.cycle.anchor
# Move the market between triggers. If the next level were computed from price
# it would follow the market; it must not. The market is kept BELOW the next
# intended level here so the level stays placeable - the gap case is its own
# test below.
for n, bid in ((2, 4422.20), (3, 4421.00), (4, 4422.50)):
    trigger(broker, BUY_STOP)
    mt5.set_price(bid, round(bid + 0.08, 2))
    eng.check_exit_now()
    buys, _ = pend(broker)
    t.check(f"F. level {n} is reference-anchored despite price at {bid}",
            abs(buys[0] - (ref + n * SPACING)) < 1e-9,
            f"{buys[0]} vs {ref + n * SPACING}")
t.check("F. the reference itself never moved", eng.cycle.anchor == ref,
        f"{eng.cycle.anchor} vs {ref}")

t.section("F2. A GAP PAST THE NEXT LEVEL WAITS, IT DOES NOT CHASE")
# the profit target is put out of reach so the exit engine cannot fire while
# this section is testing placement behaviour
eng, broker, settings, rec = build("fgap", "SINGLE_PAIR",
                                   {"basket_profit_target": 500.0,
                                    "profit_runner_enabled": False,
                                    "max_cycle_drawdown": 0})
no_candles(eng)
ref = eng.cycle.anchor
trigger(broker, BUY_STOP)
mt5.set_price(4430.00, 4430.08)            # gapped far past level +2
eng.check_exit_now()
buys, sells = pend(broker)
t.check("F2. the un-placeable level is NOT moved to a legal price",
        not buys or abs(buys[0] - (ref + 2 * SPACING)) < 1e-9, str(buys))
t.check("F2. the side waits rather than chasing the market",
        len(buys) == 0, str(buys))
t.check("F2. and it is logged, not silent",
        rec.count("REPLACEMENT_DEFERRED") >= 1,
        str(rec.count("REPLACEMENT_DEFERRED")))
t.check("F2. the SELL side is unaffected", len(sells) == 1, str(sells))
mt5.set_price(4422.00, 4422.08)            # price comes back
eng.check_exit_now()
buys, _ = pend(broker)
t.check("F2. the level goes out once price is below it again",
        len(buys) == 1 and abs(buys[0] - (ref + 2 * SPACING)) < 1e-9,
        str(buys))

t.section("G. NEVER MORE THAN ONE PENDING PER SIDE")
eng, broker, settings, rec = build("gone", "SINGLE_PAIR")
no_candles(eng)
worst_buy = worst_sell = 0
for i in range(12):
    trigger(broker, BUY_STOP if i % 3 else SELL_STOP)
    eng.check_exit_now()
    eng.check_exit_now()                   # idle passes must add nothing
    buys, sells = pend(broker)
    worst_buy = max(worst_buy, len(buys))
    worst_sell = max(worst_sell, len(sells))
t.check("G. never more than 1 BUY pending", worst_buy <= 1, str(worst_buy))
t.check("G. never more than 1 SELL pending", worst_sell <= 1, str(worst_sell))
# Any shortfall must be the risk engine withholding a level, never a silent
# miss - at depth 12 the deep-ladder rules legitimately pause expansion.
withheld = rec.count("REPLACEMENT_WITHHELD") + rec.count("REPLACEMENT_DEFERRED")
t.check("G. every trigger either replaced or was explicitly withheld",
        eng.replacements + withheld >= 12,
        f"{eng.replacements} replaced + {withheld} withheld, for 12 triggers")
t.check("G. idle passes never add a replacement of their own",
        eng.replacements <= 12, f"{eng.replacements} for 12 triggers")

t.section("H. REPLACEMENT DOES NOT WAIT FOR AN M1 CANDLE")
eng, broker, settings, rec = build("hm1", "SINGLE_PAIR")
no_candles(eng)                            # any candle read raises
before = len(broker.orders())
trigger(broker, BUY_STOP)
eng.check_exit_now()
t.check("H. the replacement went out with the candle feed poisoned",
        len(broker.orders()) == before, f"{before} -> {len(broker.orders())}")
t.check("H. and it was a real replacement", eng.replacements == 1,
        str(eng.replacements))

t.section("I. AN EXIT IN PROGRESS OUTRANKS REPLACEMENT")
eng, broker, settings, rec = build("iexit", "SINGLE_PAIR")
no_candles(eng)
trigger(broker, BUY_STOP)
eng.check_exit_now()
positions = broker.positions()
eng.commit_exit("MANUAL_EXIT", "priority test", broker.tick(), positions, (), 1.0)
before = len(broker.orders())
reps_before = eng.replacements
trigger(broker, SELL_STOP)
eng.check_exit_now()
eng._roll_single_pair(settings.snapshot(), broker.tick(), broker.positions())
t.check("I. no replacement is placed while EXITING",
        eng.replacements == reps_before, f"{reps_before} -> {eng.replacements}")
t.check("I. and nothing new appeared at the broker",
        len(broker.orders()) <= before, f"{before} -> {len(broker.orders())}")

t.section("J. THE DEEP-LADDER RISK ENGINE OUTRANKS REPLACEMENT")
eng, broker, settings, rec = build("jrisk", "SINGLE_PAIR")
no_candles(eng)
trigger(broker, BUY_STOP)
eng.check_exit_now()
reps_before = eng.replacements
# Drive the REAL condition rather than poking the flag: the permission is
# recomputed from the basket on every pass, so a hand-set value would simply be
# overwritten. Dropping the hard ceiling below the depth already used is the
# same permission the full ladder reads.
settings._values["max_ladder_depth"] = 1
trigger(broker, BUY_STOP)
eng.check_exit_now()
t.check("J. the engine recomputes the permission live",
        not eng.sequence.expansion_allowed,
        f"allowed={eng.sequence.expansion_allowed}")
t.check("J. expansion_allowed=False stops the replacement",
        eng.replacements == reps_before, f"{reps_before} -> {eng.replacements}")
t.check("J. and it is recorded, not silent",
        rec.count("REPLACEMENT_WITHHELD") >= 1,
        str(rec.count("REPLACEMENT_WITHHELD")))
t.check("J. the existing basket is NOT closed",
        eng.cycle_active and broker.positions(),
        f"active={eng.cycle_active} positions={len(broker.positions())}")
settings._values["max_ladder_depth"] = 22
eng.check_exit_now()
t.check("J. and replacement resumes when permission returns",
        eng.replacements > reps_before, str(eng.replacements))

t.section("K. REPLACEMENTS DO NOT TOUCH TRIGGER COUNTERS")
eng, broker, settings, rec = build("kcount", "SINGLE_PAIR")
no_candles(eng)
for _ in range(3):
    trigger(broker, BUY_STOP)
    eng.check_exit_now()
trigger(broker, SELL_STOP)
eng.check_exit_now()
eng.bar_time = None                        # let the ordinary pass run
eng.step()
seq = eng.sequence
t.check("K. buy triggers counted once each", seq.buy_triggers == 3,
        str(seq.buy_triggers))
t.check("K. sell triggers counted once each", seq.sell_triggers == 1,
        str(seq.sell_triggers))
t.check("K. total triggers is the sum", seq.total_triggers == 4,
        str(seq.total_triggers))
t.check("K. ladder depth is the furthest level consumed, not the pendings",
        seq.ladder_depth_used == 4, str(seq.ladder_depth_used))
t.check("K. 4 replacements for 4 triggers", eng.replacements == 4,
        str(eng.replacements))
before = seq.total_triggers
for _ in range(4):
    eng.check_exit_now()
t.check("K. idle replacement passes do not inflate the counters",
        seq.total_triggers == before, f"{before} -> {seq.total_triggers}")

t.section("L. REPLACEMENTS USE THE EXISTING LOT SIZE")
eng, broker, settings, rec = build("llot", "SINGLE_PAIR", {"lot_size": 0.03})
no_candles(eng)
trigger(broker, BUY_STOP)
eng.check_exit_now()
lots = {o.volume for o in broker.orders()}
t.check("L. the replacement carries the configured lot", lots == {0.03},
        str(lots))
t.check("L. no size increase after consecutive triggers",
        all(o.volume == 0.03 for o in broker.orders()),
        str([o.volume for o in broker.orders()]))
for _ in range(3):
    trigger(broker, BUY_STOP)
    eng.check_exit_now()
t.check("L. still the same lot after four triggers",
        {o.volume for o in broker.orders()} == {0.03},
        str({o.volume for o in broker.orders()}))
t.check("L. and every open position too",
        {p.volume for p in broker.positions()} == {0.03},
        str({p.volume for p in broker.positions()}))

t.section("M-N. NEITHER TELEGRAM NOR CSV CAN BLOCK A REPLACEMENT")
eng, broker, settings, rec = build("mn", "SINGLE_PAIR")
no_candles(eng)
slow = []
hooks = rec.hooks()
_ev = hooks["event"]
hooks["event"] = lambda e, m, f: (_ev(e, m, f), slow.append(m),
                                  time.sleep(0.15))[0]
hooks["telemetry"] = lambda row: time.sleep(0.15)
eng.hooks = hooks
trigger(broker, BUY_STOP)
started = time.perf_counter()
eng.check_exit_now()
elapsed = time.perf_counter() - started
timing = eng.replacement_timing
t.check("M-N. the replacement went out", eng.replacements == 1,
        str(eng.replacements))
t.check("M-N. slow hooks really were in the path", len(slow) >= 1,
        f"{len(slow)} calls")
t.check("M-N. the ORDER went out before the slow logging finished",
        timing["trigger_to_replacement_request_ms"] < elapsed * 1000 / 2,
        f"{timing['trigger_to_replacement_request_ms']:.2f}ms of "
        f"{elapsed * 1000:.0f}ms total")
t.check("M-N. request latency is sub-millisecond of application time",
        timing["trigger_to_replacement_request_ms"] < 5.0,
        f"{timing['trigger_to_replacement_request_ms']:.3f} ms")

t.section("O. THE REPLACEMENT TIMESTAMPS ARE RECORDED")
eng, broker, settings, rec = build("otime", "SINGLE_PAIR")
no_candles(eng)
trigger(broker, BUY_STOP)
eng.check_exit_now()
timing = eng.replacement_timing
for key in ("trigger_detected_at", "replacement_calculation_at",
            "replacement_request_sent_at", "replacement_confirmed_at",
            "trigger_to_replacement_request_ms",
            "trigger_to_replacement_confirmed_ms"):
    t.check(f"O. timing has {key!r}", key in timing, str(sorted(timing)))
t.check("O. the timestamps are in order",
        timing["trigger_detected_at"] <= timing["replacement_calculation_at"]
        <= timing["replacement_request_sent_at"]
        <= timing["replacement_confirmed_at"],
        str(timing))
t.check("O. request latency is a real, non-negative measurement",
        0.0 <= timing["trigger_to_replacement_request_ms"] < 1000.0,
        str(timing["trigger_to_replacement_request_ms"]))
t.check("O. confirmation is at or after the request",
        timing["trigger_to_replacement_confirmed_ms"] >=
        timing["trigger_to_replacement_request_ms"])

t.section("THE EXIT STILL WORKS IDENTICALLY IN SINGLE_PAIR")
eng, broker, settings, rec = build("exit", "SINGLE_PAIR",
                                   {"profit_runner_enabled": False,
                                    "basket_profit_target": 2.00})
no_candles(eng)
for _ in range(4):
    trigger(broker, BUY_STOP)
    eng.check_exit_now()
legs = broker.positions()
mt5.set_price(round(sum(p.price_open for p in legs) / len(legs) + 5.0, 2))
reason = eng.check_exit_now()
t.check("the basket exit fired", reason == "BASKET_PROFIT_TARGET", str(reason))
t.check("positions closed and pendings cancelled",
        not broker.positions() and not broker.orders(),
        f"{len(broker.positions())}p / {len(broker.orders())}o")
eng.bar_time = None
for _ in range(3):
    eng.step()
closes = [c for c in rec.cycles if c.kind_of == "complete"]
t.check("the cycle was recorded", bool(closes), str(len(closes)))
ctx = closes[-1].context if closes else {}
t.check("realized P/L still comes from the deal history",
        "realized_pnl_after_close" in ctx and
        "floating_pnl_before_close" in ctx, str(sorted(ctx)[:6]))

t.section("TELEMETRY CARRIES THE ENTRY-MODE FIELDS")
import csv_logger
for column in ("entry_mode", "initial_pending_buy_count",
               "initial_pending_sell_count", "current_pending_buy_count",
               "current_pending_sell_count", "trigger_detected_at",
               "replacement_request_sent_at", "replacement_confirmed_at",
               "trigger_to_replacement_request_ms",
               "trigger_to_replacement_confirmed_ms"):
    t.check(f"basket telemetry has {column!r}",
            column in csv_logger.TELEMETRY_HEADER, column)
t.check("the cycle summary records the mode it ran in",
        "entry_mode" in csv_logger.CYCLE_HEADER)

t.done()
