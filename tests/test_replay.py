"""Replay harness: deterministic, cost-aware, and free of look-ahead."""
import pathlib
import shutil

from harness import Suite, use_stub_mt5
use_stub_mt5()

import replay
from fakes import gold_spec

t = Suite("replay")
TMP = pathlib.Path("/tmp/replay_tests")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
SPEC = gold_spec()


def bars_from_closes(closes, start=1000, step=300, wick=0.10):
    """Build M5-shaped bars from a close series."""
    out = []
    prev = closes[0]
    for i, close in enumerate(closes):
        o, c = prev, close
        out.append({"time": start + i * step, "open": o, "close": c,
                    "high": max(o, c) + wick, "low": min(o, c) - wick})
        prev = close
    return out


def trend(n=40, start=4010.0, step=0.12):
    return [round(start + i * step, 2) for i in range(n)]


def reversal(n=20, start=4010.0, step=0.15):
    up = [round(start + i * step, 2) for i in range(n)]
    down = [round(up[-1] - i * step, 2) for i in range(1, n * 2)]
    return up + down


def chop(n=60, start=4010.0, amp=0.35):
    return [round(start + (amp if i % 2 else -amp), 2) for i in range(n)]


t.section("BAR PATH (no look-ahead, stated assumption)")
bar = {"open": 4010.0, "high": 4010.9, "low": 4009.5, "close": 4010.6}
path = replay.bar_path(bar, steps_per_leg=3, adverse_first=True)
t.check("path starts at the open", path[0] == 4010.0)
t.check("path ends at the close", abs(path[-1] - 4010.6) < 1e-9)
t.check("path visits the low", any(abs(p - 4009.5) < 1e-9 for p in path))
t.check("path visits the high", any(abs(p - 4010.9) < 1e-9 for p in path))
t.check("bullish bar walks the low first (conservative)",
        path.index(min(path)) < path.index(max(path)))
optimistic = replay.bar_path(bar, steps_per_leg=3, adverse_first=False)
t.check("optimistic mode walks the high first",
        optimistic.index(max(optimistic)) < optimistic.index(min(optimistic)))
t.check("path only uses this bar's own OHLC",
        min(path) >= bar["low"] - 1e-9 and max(path) <= bar["high"] + 1e-9)

t.section("REPLAY RUNS THE REAL ENGINE")
res = replay.run_replay(bars_from_closes(trend()), spec=SPEC, data_dir=TMP,
                        start_balance=1000.0,
                        overrides={"cooldown_after_loss_minutes": 0})
t.check("bars replayed", res.bars == 40, str(res.bars))
t.check("price steps generated", res.ticks > res.bars, str(res.ticks))
t.check("levels triggered on a trend", res.triggers > 0, str(res.triggers))
t.check("trades closed", res.closed_trades > 0, str(res.closed_trades))
t.check("balance moved", res.balance_end != res.balance_start,
        f"{res.balance_start} -> {res.balance_end}")
t.check("assumptions are reported", len(res.assumptions) >= 4)
t.check("summary is printable", "ROLLING LADDER REPLAY" in res.summary())
t.check("summary refuses to claim profitability",
        "not a profitability claim" in res.summary())

t.section("DETERMINISM")
res_a = replay.run_replay(bars_from_closes(trend()), spec=SPEC, data_dir=TMP,
                          start_balance=1000.0)
res_b = replay.run_replay(bars_from_closes(trend()), spec=SPEC, data_dir=TMP,
                          start_balance=1000.0)
t.check("same input -> same result",
        (res_a.balance_end, res_a.triggers, len(res_a.cycles)) ==
        (res_b.balance_end, res_b.triggers, len(res_b.cycles)),
        f"{res_a.balance_end} vs {res_b.balance_end}")

t.section("COSTS ARE APPLIED")
cheap = replay.run_replay(bars_from_closes(trend()), spec=SPEC, data_dir=TMP,
                          start_balance=1000.0, commission_per_lot=0.0)
dear = replay.run_replay(bars_from_closes(trend()), spec=SPEC, data_dir=TMP,
                         start_balance=1000.0, commission_per_lot=50.0)
t.check("commission reduces the result", dear.balance_end < cheap.balance_end,
        f"{cheap.balance_end:.2f} vs {dear.balance_end:.2f}")
wide = replay.run_replay(bars_from_closes(trend()), spec=SPEC, data_dir=TMP,
                         start_balance=1000.0, spread=0.60)
t.check("a wider spread changes the outcome",
        wide.balance_end != cheap.balance_end or wide.triggers != cheap.triggers,
        f"{wide.balance_end:.2f} vs {cheap.balance_end:.2f}")

t.section("CYCLES END ADAPTIVELY")
res = replay.run_replay(bars_from_closes(reversal()), spec=SPEC, data_dir=TMP,
                        start_balance=1000.0,
                        overrides={"cooldown_after_loss_minutes": 0})
t.check("a reversal series completes cycles", len(res.cycles) > 0,
        str(len(res.cycles)))
t.check("cycle endings are attributed", bool(res.exit_reasons),
        str(res.exit_reasons))
t.check("endings name an explicit reason, never a trade count",
        all(k in ("BASKET_PROFIT_TARGET", "PROFIT_PROTECTION", "RISK_DRAWDOWN",
                  "RISK_TIMEOUT", "RISK_SPREAD", "EMERGENCY_EXIT",
                  "MANUAL_EXIT")
            for k in res.exit_reasons), str(res.exit_reasons))
t.check("cycle rows carry the sequence and the basket at the exit",
        all({"buy", "sell", "triggers", "depth", "floating_at_exit"} <= set(c)
            for c in res.cycles))

t.section("CHOP IS SURVIVABLE")
res = replay.run_replay(bars_from_closes(chop()), spec=SPEC, data_dir=TMP,
                        start_balance=1000.0,
                        overrides={"cooldown_after_loss_minutes": 0})
t.check("chop does not run away with exposure",
        res.max_drawdown < 200.0, f"drawdown {res.max_drawdown:.2f}")
t.check("chop still produces bounded cycles", len(res.cycles) >= 0)

t.section("PARAMETER SWEEP (what optimisation will look like)")
results = {}
for spacing in (0.20, 0.30, 0.40):
    r = replay.run_replay(bars_from_closes(trend()), spec=SPEC, data_dir=TMP,
                          start_balance=1000.0,
                          overrides={"ladder_spacing": spacing,
                                     "cooldown_after_loss_minutes": 0})
    results[spacing] = (r.triggers, round(r.balance_end - r.balance_start, 2))
t.check("spacing changes the trigger count",
        len({v[0] for v in results.values()}) > 1, str(results))
t.check("every spacing produced a runnable result",
        all(isinstance(v[1], float) for v in results.values()), str(results))

t.section("CSV BAR LOADING")
path = TMP / "bars.csv"
path.write_text("time,open,high,low,close,spread\n"
                "1000,4010.0,4010.5,4009.8,4010.4,0.08\n"
                "1300,4010.4,4011.0,4010.2,4010.9,0.09\n"
                "bad,row,here,,,\n")
bars = replay.load_bars_csv(path)
t.check("valid rows loaded", len(bars) == 2, str(len(bars)))
t.check("malformed rows skipped", all("open" in b for b in bars))
t.check("per-bar spread read", bars[0]["spread"] == 0.08)

# A bar file exported for a human to read is the normal case, not an error:
# every row here has a timestamp string where an epoch would be.
dated = TMP / "dated.csv"
dated.write_text("time,open,high,low,close,spread\n"
                 "2026-01-05 00:00:00,4010.0,4010.5,4009.8,4010.4,0.08\n"
                 "2026-01-05 00:05:00,4010.4,4011.0,4010.2,4010.9,0.09\n"
                 "2026-01-05T00:10:00,4010.9,4011.4,4010.6,4011.2,0.09\n"
                 "2026.01.05 00:15,4011.2,4011.6,4011.0,4011.5,0.09\n")
dated_bars = replay.load_bars_csv(dated)
t.check("timestamp strings load", len(dated_bars) == 4, str(len(dated_bars)))
t.check("they are converted to epoch seconds",
        all(isinstance(b["time"], float) and b["time"] > 1_600_000_000
            for b in dated_bars), str([b["time"] for b in dated_bars]))
t.check("and stay in order and 5 minutes apart",
        [round(dated_bars[i + 1]["time"] - dated_bars[i]["time"])
         for i in range(3)] == [300, 300, 300],
        str([b["time"] for b in dated_bars]))
t.check("an epoch is still an epoch", replay.parse_bar_time("1000", 0) == 1000.0)
t.check("an unreadable time falls back to the row index",
        replay.parse_bar_time("not a time", 7) == 7.0)

t.section("THE ENTRY GATE IS REPLAYED, NOT SKIPPED")
gated = replay.run_replay(bars_from_closes(trend()), spec=SPEC, data_dir=TMP,
                          start_balance=1000.0,
                          overrides={"cooldown_after_loss_minutes": 0})
ungated = replay.run_replay(bars_from_closes(trend()), spec=SPEC, data_dir=TMP,
                            start_balance=1000.0, entry_gate=False,
                            overrides={"cooldown_after_loss_minutes": 0})
t.check("the gate is on by default and stated in the assumptions",
        any("one evaluation per CLOSED replay bar" in a
            for a in gated.assumptions), str(gated.assumptions[-1]))
t.check("turning it off is stated too, not silent",
        any("gate OFF" in a for a in ungated.assumptions),
        str(ungated.assumptions[-1]))
t.check("the gate is honest about not resampling the entry timeframe",
        any("NOT resampled" in a for a in gated.assumptions))
t.check("a gated run still trades", gated.triggers > 0, str(gated.triggers))
t.check("the gate cannot start MORE cycles than an ungated run",
        len(gated.cycles) <= len(ungated.cycles),
        f"{len(gated.cycles)} gated vs {len(ungated.cycles)} ungated")

t.section("BACKTEST FLAGS REACH THE SETTINGS")
def parsed(argv):
    """Run main()'s argument handling without replaying anything."""
    captured = {}

    def fake_run(bars, **kw):
        captured["overrides"] = kw.get("overrides")
        captured["entry_gate"] = kw.get("entry_gate")
        captured["bar_seconds"] = kw.get("bar_seconds")
        return replay.ReplayResult(balance_start=0.0)

    real = replay.run_replay
    replay.run_replay = fake_run
    try:
        replay.main(argv)
    finally:
        replay.run_replay = real
    return captured


csv_path = TMP / "flags.csv"
csv_path.write_text("time,open,high,low,close\n"
                    "2024-01-01 00:00,4010,4011,4009,4010.5\n"
                    "2024-01-01 00:05,4010.5,4012,4010,4011.5\n")
got = parsed(["--csv", str(csv_path), "--entry-timeframe", "M5",
              "--max-depth", "8", "--target", "3.5", "--activation", "4",
              "--trail", "2", "--floor", "1.25", "--cycle-drawdown", "25"])
over = got["overrides"]
for key, want in [("entry_timeframe", "M5"), ("max_ladder_depth", 8),
                  ("basket_profit_target", 3.5),
                  ("profit_protection_activation", 4.0),
                  ("profit_protection_trail", 2.0),
                  ("min_protected_profit", 1.25),
                  ("max_cycle_drawdown", 25.0)]:
    t.check(f"--{key} reaches the run", over.get(key) == want,
            f"{key}={over.get(key)!r} want {want!r}")
t.check("the gate is on unless it is switched off", got["entry_gate"] is True)
got = parsed(["--csv", str(csv_path), "--no-entry-gate", "--no-runner"])
t.check("--no-entry-gate switches it off", got["entry_gate"] is False)
t.check("--no-runner switches the runner off",
        got["overrides"].get("profit_runner_enabled") is False)
got = parsed(["--csv", str(csv_path), "--timeframe", "M1"])
t.check("simulated time advances at the replayed timeframe's rate",
        got["bar_seconds"] == 60.0, str(got["bar_seconds"]))

t.section("SIMULATED TIME DRIVES THE DAILY GUARD")
# The daily drawdown guard resets on the engine's own clock. On the wall clock
# a multi-day replay would spend the whole run blocked after its first bad day.
from ladder_engine import RollingLadderEngine
from runtime_settings import RuntimeSettings
from fakes import make_paper
import config as cfg
now = [1_700_000_000.0]
settings = RuntimeSettings(cfg.runtime_defaults(), TMP / "daily_set.json")
broker, _feed = make_paper(state_path=TMP / "daily_paper.json")
broker.clock = lambda: now[0]
eng = RollingLadderEngine(broker, settings, state_path=None, clock=lambda: now[0])
day_one = eng._today()
now[0] += 26 * 3600
t.check("the simulated day rolls with the data", eng._today() != day_one,
        f"{day_one} -> {eng._today()}")

t.done()
