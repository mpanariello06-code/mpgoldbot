"""
M1-confirmed cycle entry: WHEN a new cycle may begin.

The gate does not decide direction and adds no indicator. It decides that a
new cycle is a deliberate choice taken exactly once per newly CLOSED
entry-timeframe candle, using the safety conditions the engine already had.
"""
import pathlib
import shutil

from harness import Suite, use_stub_mt5
use_stub_mt5()

import config as cfg
from broker import BUY_STOP
from fakes import Recorder, TickFeed, make_paper, trigger_buy
from ladder_engine import RollingLadderEngine, State
from runtime_settings import RuntimeSettings

t = Suite("entry_gate")
TMP = pathlib.Path("/tmp/entry_gate_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)

BAR = 60


class Bars:
    """A controllable entry-candle feed: bars only close when told to."""

    def __init__(self, start=1_700_000_000, period=BAR):
        self.period = period
        self.closed = start - start % period
        self.calls = 0
        self.fail = False

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("rates unavailable")
        return self.closed

    def close_next(self, n=1):
        """A new candle closed."""
        self.closed += self.period * n
        return self.closed


class Clock:
    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def build(overrides=None, name="g", bars=None, clock=None, balance=1000.0):
    settings = RuntimeSettings(cfg.runtime_defaults(), TMP / f"{name}_settings.json")
    for key, value in {"ladder_depth": 5, "max_pending_orders": 10,
                       "max_open_positions": 12, "max_ladder_depth": 12,
                       "cycle_reentry_cooldown_seconds": 10,
                       "cooldown_after_loss_minutes": 0,
                       "max_cycle_duration_minutes": 0,
                       "max_cycle_drawdown": 0}.items():
        settings._values[key] = value
    for key, value in (overrides or {}).items():
        settings._values[key] = value
    bars = bars if bars is not None else Bars()
    clock = clock or Clock()
    broker, feed = make_paper(feed=TickFeed(4010.00),
                              state_path=TMP / f"{name}_paper.json",
                              balance=balance)
    broker.clock = clock
    rec = Recorder()
    engine = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                                 state_path=TMP / f"{name}_state.json",
                                 clock=clock, bar_time=bars)
    engine.resume()
    return engine, broker, feed, settings, rec, bars, clock


def evals(rec):
    return rec.entry_evals


def close_basket(engine, broker, feed, clock, steps=40):
    """Run the real exit path: fill a level, then let the basket reach target."""
    buys = sorted([o for o in broker.orders() if o.side == BUY_STOP],
                  key=lambda o: o.price)
    trigger_buy(feed, buys[0].price)
    clock.advance(1)
    engine.step()
    for _ in range(steps):
        if not engine.cycle_active and not broker.positions() \
                and not broker.orders():
            return True
        feed.set(round(feed.bid + 0.10, 2))
        clock.advance(1)
        engine.step()
    return False


# ===========================================================================
t.section("ENTRY IS DECIDED ON A CLOSED CANDLE, NOT ON A TICK")
engine, broker, feed, settings, rec, bars, clock = build(name="first")
t.check("nothing is placed before the engine steps", not broker.orders())
engine.step()
t.check("the first closed candle starts the first ladder",
        len(broker.orders()) == 10, f"{len(broker.orders())} orders")
t.check("exactly one entry evaluation was recorded", len(evals(rec)) == 1,
        str(len(evals(rec))))
t.check("it was accepted", evals(rec)[0]["accepted"] is True,
        evals(rec)[0]["reason"])
t.check("it names the candle it decided on",
        evals(rec)[0]["bar_time"] == bars.closed, str(evals(rec)[0]["bar_time"]))
t.check("the engine remembers that candle",
        engine.last_entry_bar == bars.closed, str(engine.last_entry_bar))
t.check("ENTRY_EVALUATED is logged", rec.count("ENTRY_EVALUATED") == 1)

t.section("ONE EVALUATION PER CANDLE, HOWEVER MANY TICKS ARRIVE")
engine, broker, feed, settings, rec, bars, clock = build(
    {"max_spread": 0.01}, name="once")      # spread blocks, so it stays flat
for _ in range(20):
    feed.set(4010.00 + 0.05)
    engine.step()
t.check("20 ticks on one candle produced ONE evaluation",
        len(evals(rec)) == 1, f"{len(evals(rec))} evaluations")
t.check("the rejection names the spread",
        "spread" in evals(rec)[0]["reason"], evals(rec)[0]["reason"])
t.check("the engine waits between candles",
        engine.state == State.WAITING_FOR_ENTRY, engine.state)
t.check("and says why it is not trading",
        bool(engine.block_reason), engine.block_reason)
t.check("no ladder was built on a rejected candle", not broker.orders())
bars.close_next()
for _ in range(5):
    engine.step()
t.check("the next candle is evaluated once more", len(evals(rec)) == 2,
        f"{len(evals(rec))} evaluations")
t.check("both evaluations name different candles",
        evals(rec)[0]["bar_time"] != evals(rec)[1]["bar_time"])
bars.close_next(3)
engine.step()
t.check("a jump of several candles still evaluates once",
        len(evals(rec)) == 3, f"{len(evals(rec))} evaluations")

t.section("EVERY EVALUATED CANDLE IS RECORDED, ACCEPTED OR NOT")
record = evals(rec)[-1]
for key in ("bar_time", "timeframe", "symbol", "bid", "ask", "spread",
            "accepted", "reason", "cooldown_left", "open_positions",
            "pending_orders", "risk_ok", "spread_ok", "next_cycle_id"):
    t.check(f"entry telemetry carries {key!r}", key in record, str(record))
t.check("rejected candles carry a reason",
        all(r["reason"] for r in evals(rec) if not r["accepted"]))
t.check("the timeframe is recorded with the decision",
        record["timeframe"] == settings.get("entry_timeframe"),
        str(record["timeframe"]))

t.section("THE LADDER IS NOT REBUILT EVERY CANDLE")
engine, broker, feed, settings, rec, bars, clock = build(name="norebuild")
engine.step()
first = sorted(o.ticket for o in broker.orders())
cycle_id = engine.cycle.cycle_id
started_bar = bars.closed
for _ in range(6):
    bars.close_next()
    engine.step()
    engine.step()
t.check("six further candles closed",
        bars.closed == started_bar + 6 * BAR, str(bars.closed))
t.check("the same orders are still live",
        sorted(o.ticket for o in broker.orders()) == first,
        f"{len(broker.orders())} orders")
t.check("the cycle id never changed", engine.cycle.cycle_id == cycle_id)
t.check("no second ladder was created", rec.count("LADDER_CREATED") == 1)
t.check("no entry evaluation runs while a cycle is active",
        len(evals(rec)) == 1, f"{len(evals(rec))} evaluations")

t.section("NO IMMEDIATE RE-ENTRY AFTER THE COOLDOWN")
engine, broker, feed, settings, rec, bars, clock = build(
    {"cycle_reentry_cooldown_seconds": 10, "basket_profit_target": 0.50,
     "profit_runner_enabled": False}, name="cooldown")
engine.step()
close_basket(engine, broker, feed, clock)
t.check("flat after the close", not broker.positions() and not broker.orders())
evals_after_close = len(evals(rec))
clock.advance(3)
engine.step()
t.check("inside the cooldown the engine is in COOLDOWN_AFTER_EXIT",
        engine.state == State.COOLDOWN_AFTER_EXIT, engine.state)
t.check("no new ladder inside the cooldown", not broker.orders())
clock.advance(20)                              # cooldown well past
for _ in range(10):
    engine.step()
t.check("the elapsed cooldown ALONE does not re-enter",
        not broker.orders(), f"{len(broker.orders())} orders")
t.check("no entry was evaluated on the exit's own candle",
        len(evals(rec)) == evals_after_close,
        f"{len(evals(rec))} vs {evals_after_close}")
t.check("the engine is waiting for the next candle",
        engine.state == State.WAITING_FOR_ENTRY, engine.state)
bars.close_next()
engine.step()
t.check("the next CLOSED candle starts the next cycle",
        len(broker.orders()) == 10, f"{len(broker.orders())} orders")
t.check("that candle was evaluated and accepted",
        evals(rec)[-1]["accepted"] is True, evals(rec)[-1]["reason"])
t.check("the new cycle has a new id", engine.cycle.cycle_id == 2,
        f"#{engine.cycle.cycle_id}")

t.section("A CANDLE INSIDE THE COOLDOWN IS EVALUATED AND REJECTED")
engine, broker, feed, settings, rec, bars, clock = build(
    {"cycle_reentry_cooldown_seconds": 300, "basket_profit_target": 0.50,
     "profit_runner_enabled": False}, name="cdreject")
engine.step()
close_basket(engine, broker, feed, clock)
bars.close_next()
engine.step()
rejected = [r for r in evals(rec) if not r["accepted"]]
t.check("the candle was evaluated", bool(rejected), str(evals(rec)))
t.check("and rejected for the cooldown", "cooldown" in rejected[-1]["reason"],
        rejected[-1]["reason"])
t.check("the remaining cooldown is recorded",
        rejected[-1]["cooldown_left"] > 0, str(rejected[-1]["cooldown_left"]))
t.check("still no ladder", not broker.orders())

t.section("ONE ACTIVE CYCLE IS ENFORCED FROM BROKER STATE")
engine, broker, feed, settings, rec, bars, clock = build(name="oneactive")
engine.step()
t.check("a ladder is live", engine.ladder_active)
for _ in range(4):
    bars.close_next()
    engine.step()
t.check("new candles cannot open a second ladder",
        len(broker.orders()) == 10, f"{len(broker.orders())} orders")
t.check("an explicit second ladder is refused",
        engine.create_new_ladder(engine.cycle.cycle_id + 1, "forced") is False)
t.check("the refusal is logged",
        rec.count("LADDER_REJECTED_ALREADY_ACTIVE") >= 1)

t.section("RISK AND SPREAD ARE STILL THE ENTRY CONDITIONS")
engine, broker, feed, settings, rec, bars, clock = build(
    {"max_spread": 0.20}, name="conds")
feed.set(4010.00, spread=0.50)
engine.step()
t.check("a wide spread rejects the candle",
        evals(rec)[-1]["accepted"] is False, evals(rec)[-1]["reason"])
t.check("spread_ok records the failing condition",
        evals(rec)[-1]["spread_ok"] is False)
t.check("risk_ok is unaffected", evals(rec)[-1]["risk_ok"] is True)
feed.set(4010.00, spread=0.08)
engine.step()
t.check("the same candle is NOT re-evaluated when the spread narrows",
        len(evals(rec)) == 1, f"{len(evals(rec))} evaluations")
t.check("and no ladder is built mid-candle", not broker.orders())
bars.close_next()
engine.step()
t.check("the next candle accepts it", evals(rec)[-1]["accepted"] is True,
        evals(rec)[-1]["reason"])
t.check("and the ladder goes out", len(broker.orders()) == 10)

engine, broker, feed, settings, rec, bars, clock = build(
    {"max_daily_drawdown": 1.0}, name="riskblock")
engine.daily_profit = -5.0
engine.step()
t.check("a risk block rejects the candle",
        evals(rec)[-1]["accepted"] is False, evals(rec)[-1]["reason"])
t.check("the reason names risk", evals(rec)[-1]["reason"].startswith("risk:"),
        evals(rec)[-1]["reason"])
t.check("risk_ok records the failing condition",
        evals(rec)[-1]["risk_ok"] is False)
t.check("no ladder while risk blocks", not broker.orders())

t.section("THE GATE SURVIVES A BROKEN BAR FEED")
engine, broker, feed, settings, rec, bars, clock = build(name="barfail")
bars.fail = True
engine.step()
t.check("a failing bar feed does not crash the engine", True)
t.check("the failure is logged",
        any(e[0] == "ERROR" and "bar feed" in e[1] for e in rec.events),
        str([e[1] for e in rec.events if e[0] == "ERROR"][:2]))
t.check("with no bar the engine falls back to the old behaviour",
        len(broker.orders()) == 10, f"{len(broker.orders())} orders")

t.section("NO BAR FEED WIRED = GATE OFF (BACKWARDS COMPATIBLE)")
settings = RuntimeSettings(cfg.runtime_defaults(), TMP / "nogate_settings.json")
settings._values.update({"ladder_depth": 5, "max_pending_orders": 10})
broker, feed = make_paper(feed=TickFeed(4010.00),
                          state_path=TMP / "nogate_paper.json", balance=1000.0)
rec = Recorder()
engine = RollingLadderEngine(broker, settings, hooks=rec.hooks(),
                             state_path=TMP / "nogate_state.json")
engine.resume()
engine.step()
t.check("without a bar feed the ladder deploys immediately",
        len(broker.orders()) == 10, f"{len(broker.orders())} orders")
t.check("and nothing pretends a candle was evaluated", not evals(rec))

t.section("THE DECIDED CANDLE SURVIVES A RESTART")
engine, broker, feed, settings, rec, bars, clock = build(
    {"max_spread": 0.01}, name="restart")
engine.step()
decided = engine.last_entry_bar
t.check("a candle was decided", decided == bars.closed, str(decided))
engine.save()
rec2 = Recorder()
engine2 = RollingLadderEngine(broker, settings, hooks=rec2.hooks(),
                              state_path=TMP / "restart_state.json",
                              clock=clock, bar_time=bars)
engine2.resume()
t.check("the restarted engine remembers it",
        engine2.last_entry_bar == decided, str(engine2.last_entry_bar))
engine2.step()
t.check("and does not re-decide the same candle", not evals(rec2),
        str(evals(rec2)))
bars.close_next()
engine2.step()
t.check("a genuinely new candle is evaluated", len(evals(rec2)) == 1)

t.section("STATUS AND TELEMETRY EXPOSE THE GATE")
engine, broker, feed, settings, rec, bars, clock = build(name="status")
engine.step()
status = engine.snapshot()
for key in ("entry_timeframe", "last_entry_bar", "waiting_for_entry"):
    t.check(f"status carries {key!r}", key in status, str(status.get(key)))
t.check("status reports the candle the cycle started on",
        status["last_entry_bar"] == bars.closed, str(status["last_entry_bar"]))

t.done()
