"""
Replay / backtest harness for the rolling ladder.

Runs the real RollingLadderEngine and the real PaperBroker over historical M5
bars, so what is measured is the shipped strategy - not a re-implementation of
it. Pending order activation, fills, TP, spread, commission, slippage, ladder
rolling and replenishment, cycle exits: all of it goes through the same code
the live bot runs.

No look-ahead: bars are fed one at a time, each bar is walked as a price path,
and the engine only ever sees prices up to the current step. The intrabar path
is a modelling assumption made from the current bar alone (never from the next
one) and is stated in the output.

    python replay.py --bars 3000                  # pull M5 bars from MT5
    python replay.py --csv gold_m5.csv            # time,open,high,low,close[,spread]
    python replay.py --csv g.csv --spacing 0.20 --target 3.00
"""

import argparse
import csv as csv_module
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config as cfg
from broker import PaperBroker, Tick
from ladder_engine import RollingLadderEngine
from price_utils import SymbolSpec
from runtime_settings import RuntimeSettings


# ===========================================================================
# PRICE FEED
# ===========================================================================
class ReplayFeed:
    """Tick provider walking a scripted price path."""

    def __init__(self, spread=0.08):
        self.bid = 0.0
        self.spread = spread
        self.time = 0.0

    def set(self, bid, ts=None, spread=None):
        self.bid = float(bid)
        if spread is not None:
            self.spread = float(spread)
        if ts is not None:
            self.time = float(ts)
        return self

    def __call__(self):
        return Tick(bid=round(self.bid, 3),
                    ask=round(self.bid + self.spread, 3), time=self.time)


def bar_path(bar, steps_per_leg=4, adverse_first=True):
    """
    Prices visited inside one bar.

    Only this bar's own OHLC is used. `adverse_first` walks open -> the extreme
    that hurts an open position first -> the other extreme -> close, which is
    the conservative reading when the true tick order is unknown.
    """
    o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), \
        float(bar["close"])
    bullish = c >= o
    first, second = (l, h) if (bullish == adverse_first) else (h, l)

    def leg(a, b):
        if steps_per_leg <= 1:
            return [b]
        return [a + (b - a) * (i + 1) / steps_per_leg for i in range(steps_per_leg)]

    return [o] + leg(o, first) + leg(first, second) + leg(second, c)


# ===========================================================================
# BAR SOURCES
# ===========================================================================
# What a bar export actually contains: an epoch, or a date/time the exporter
# wrote for a human to read.
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d",
)


def parse_bar_time(value, fallback):
    """Epoch seconds from an epoch, a timestamp string, or `fallback`."""
    if value in (None, ""):
        return float(fallback)
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    try:                                   # anything else ISO-8601 shaped
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return float(fallback)


def load_bars_csv(path):
    """time,open,high,low,close[,spread] - header required.

    `time` may be epoch seconds or a timestamp string; a bar file exported for
    a human to read is the normal case, not an error.
    """
    out = []
    skipped = 0
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv_module.DictReader(fh):
            try:
                out.append({
                    "time": parse_bar_time(row.get("time") or row.get("timestamp"),
                                           len(out)),
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "spread": float(row["spread"]) if row.get("spread") else None,
                })
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
    if skipped:
        print(f"[replay] {skipped} unreadable rows skipped in {path}")
    return out


def load_bars_mt5(symbol, timeframe, count):
    """Historical bars straight from the terminal."""
    import MetaTrader5 as mt5
    from broker import MT5_LOCK
    tf = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
          "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
          "H1": mt5.TIMEFRAME_H1}.get(timeframe.upper(), mt5.TIMEFRAME_M5)
    with MT5_LOCK:
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        info = mt5.symbol_info(symbol)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"no {timeframe} history for {symbol}")
    spec = SymbolSpec.from_mt5(info) if info else None
    bars = [{"time": float(r["time"]), "open": float(r["open"]),
             "high": float(r["high"]), "low": float(r["low"]),
             "close": float(r["close"]),
             "spread": (float(r["spread"]) * spec.point) if spec and
             "spread" in r.dtype.names else None} for r in rates]
    return bars, spec


# ===========================================================================
# RESULT
# ===========================================================================
@dataclass
class ReplayResult:
    bars: int = 0
    ticks: int = 0
    cycles: list = field(default_factory=list)
    triggers: int = 0
    closed_trades: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    balance_start: float = 0.0
    balance_end: float = 0.0
    max_drawdown: float = 0.0
    exit_reasons: dict = field(default_factory=dict)
    assumptions: list = field(default_factory=list)

    @property
    def net(self):
        return self.balance_end - self.balance_start

    @property
    def wins(self):
        return sum(1 for c in self.cycles if c["realized"] > 0)

    @property
    def losses(self):
        return sum(1 for c in self.cycles if c["realized"] < 0)

    def summary(self):
        lines = [
            "=" * 62,
            "ROLLING LADDER REPLAY",
            "=" * 62,
            f"Bars replayed      : {self.bars}",
            f"Price steps        : {self.ticks}",
            f"Levels triggered   : {self.triggers}",
            f"Trades closed      : {self.closed_trades}",
            f"Cycles completed   : {len(self.cycles)}"
            f"  ({self.wins} up / {self.losses} down)",
            f"Balance            : {self.balance_start:.2f} -> "
            f"{self.balance_end:.2f}  ({self.net:+.2f})",
            f"Max equity drawdown: {self.max_drawdown:.2f}",
            "",
            "Cycle endings:",
        ]
        for reason, count in sorted(self.exit_reasons.items(),
                                    key=lambda kv: -kv[1]):
            lines.append(f"  {count:>4}  {reason}")
        if self.cycles:
            avg = sum(c["realized"] for c in self.cycles) / len(self.cycles)
            avg_trig = sum(c["triggers"] for c in self.cycles) / len(self.cycles)
            lines += ["",
                      f"Average per cycle  : {avg:+.3f} over "
                      f"{avg_trig:.1f} triggers"]
        lines += ["", "Assumptions:"] + [f"  - {a}" for a in self.assumptions]
        lines += ["", "This is a mechanical replay, not a profitability claim:",
                  "fill assumptions and the intrabar path materially affect a",
                  "scalper at this timescale.", "=" * 62]
        return "\n".join(lines)


# ===========================================================================
# RUNNER
# ===========================================================================
def run_replay(bars, spec=None, overrides=None, data_dir=None, spread=0.08,
               commission_per_lot=0.0, steps_per_leg=4, adverse_first=True,
               start_balance=1000.0, progress=None, bar_seconds=300.0,
               entry_gate=True):
    """Replay `bars` through the live engine. Returns a ReplayResult."""
    spec = spec or SymbolSpec(name=cfg.SYMBOL, digits=2, point=0.01,
                              tick_size=0.01, tick_value=1.0)
    data_dir = Path(data_dir or (cfg.DATA_PATH / "replay"))
    data_dir.mkdir(parents=True, exist_ok=True)

    settings = RuntimeSettings(cfg.runtime_defaults(),
                               data_dir / "replay_settings.json")
    for key, value in (overrides or {}).items():
        settings._values[key] = settings.coerce(key, value)

    feed = ReplayFeed(spread=spread)
    # Everything runs on simulated time: a cooldown of 15 minutes must cost
    # 15 minutes of replayed market, not 15 minutes of wall clock.
    sim_clock = lambda: feed.time
    broker = PaperBroker(cfg.SYMBOL, cfg.MAGIC, spec_provider=lambda: spec,
                         tick_provider=feed, state_path=None,
                         start_balance=start_balance,
                         max_slippage_points=int(settings.get("max_slippage")),
                         commission_per_lot=commission_per_lot,
                         clock=sim_clock)

    # The entry gate, replayed honestly: during bar i the most recently CLOSED
    # bar is i-1. The forming bar is never visible, so there is no look-ahead,
    # and a new cycle can only begin once per closed bar - exactly as live.
    closed_bar = {"time": None}
    bar_time = (lambda: closed_bar["time"]) if entry_gate else None

    result = ReplayResult(balance_start=start_balance)
    result.assumptions = [
        f"intrabar path: open -> "
        f"{'adverse' if adverse_first else 'favourable'} extreme -> other "
        f"extreme -> close, {steps_per_leg} steps per leg",
        f"spread: {'from the bar data' if any(b.get('spread') for b in bars) else f'fixed {spread}'}",
        f"commission: {commission_per_lot:.2f} per lot round turn",
        f"slippage: capped at {settings.get('max_slippage')} points on stop fills",
        "pending stop orders fill at the level unless price gapped past it",
        (f"entry: one evaluation per CLOSED replay bar "
         f"({bar_seconds:g}s). ENTRY_TIMEFRAME="
         f"{settings.get('entry_timeframe')} is NOT resampled - the replay "
         f"bar IS the entry candle" if entry_gate
         else "entry: gate OFF - a cycle starts as soon as the safety gates "
              "pass (pre-M1 behaviour)"),
    ]

    def on_cycle(cycle, sequence, total, reason, kind, lost,
                 duration=0.0, next_cycle_id=None, context=None,
                 next_ladder_seconds=0.0):
        seq = sequence.snapshot() if sequence else {}
        ctx = context or {}
        result.cycles.append({
            "cycle_id": cycle.cycle_id, "realized": total,
            "triggers": seq.get("total_triggers", cycle.trades),
            "buy": seq.get("buy_triggers", 0), "sell": seq.get("sell_triggers", 0),
            "depth": seq.get("ladder_depth_used", 0),
            "kind": kind, "reason": reason,
            "duration_seconds": round(duration, 1),
            "exit_price": ctx.get("exit_price", ""),
            "floating_at_exit": ctx.get("floating_pnl_at_exit", ""),
            "open_positions_at_exit": ctx.get("open_positions_at_exit", ""),
        })
        result.exit_reasons[kind] = result.exit_reasons.get(kind, 0) + 1

    def on_closed(trade, index, cycle, is_win):
        result.closed_trades += 1
        if trade.profit >= 0:
            result.gross_profit += trade.profit
        else:
            result.gross_loss += trade.profit

    engine = RollingLadderEngine(
        broker, settings,
        hooks={"cycle_complete": on_cycle,
               "closed": on_closed,
               "entry": lambda *a: None,
               "event": lambda *a: None,
               "cycle_started": lambda *a: None,
               "risk_blocked": lambda *a: None},
        state_path=None, clock=sim_clock, bar_time=bar_time)

    first = bars[0]
    feed.set(first["open"], ts=first["time"], spread=first.get("spread") or spread)
    engine.spec = spec
    engine.resume()

    peak_equity = start_balance
    for i, bar in enumerate(bars):
        bar_spread = bar.get("spread") or spread
        path = bar_path(bar, steps_per_leg, adverse_first)
        seconds = bar_seconds / max(1, len(path))
        # bar i is forming; the last CLOSED bar is the one before it
        closed_bar["time"] = int(bars[i - 1]["time"]) if i else None
        for step, price in enumerate(path):
            # walk simulated time across the bar so trigger gaps, order ages
            # and cooldowns are measured in market time
            feed.set(price, ts=bar["time"] + step * seconds, spread=bar_spread)
            engine.step()
            result.ticks += 1
        equity = broker.account().equity
        peak_equity = max(peak_equity, equity)
        result.max_drawdown = max(result.max_drawdown, peak_equity - equity)
        if progress and i % progress == 0:
            print(f"  bar {i}/{len(bars)}  equity {equity:.2f}", flush=True)

    result.bars = len(bars)
    result.triggers = engine.total_trades
    result.balance_end = broker.account().balance
    return result


# how long one bar of each timeframe lasts, so simulated time advances at the
# rate the replayed data actually represents
_BAR_SECONDS = {"M1": 60.0, "M5": 300.0, "M15": 900.0, "M30": 1800.0,
                "H1": 3600.0}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Replay the rolling ladder")
    ap.add_argument("--csv", help="bar file: time,open,high,low,close[,spread]")
    ap.add_argument("--bars", type=int, default=2000, help="bars to pull from MT5")
    ap.add_argument("--symbol", default=cfg.SYMBOL)
    ap.add_argument("--timeframe", default=cfg.TIMEFRAME)
    ap.add_argument("--spacing", type=float)
    ap.add_argument("--depth", type=int)
    ap.add_argument("--target", type=float,
                    help="basket profit target in account currency")
    ap.add_argument("--lot", type=float)
    ap.add_argument("--entry-timeframe", choices=("M1", "M5", "M15", "M30", "H1"),
                    help="timeframe whose CLOSED candle may start a cycle")
    ap.add_argument("--no-entry-gate", action="store_true",
                    help="start cycles on any tick (the pre-M1 behaviour)")
    ap.add_argument("--max-depth", type=int,
                    help="MAX_LADDER_DEPTH: levels a cycle may consume")
    ap.add_argument("--activation", type=float,
                    help="peak profit at which protection turns on")
    ap.add_argument("--trail", type=float,
                    help="give-back from the peak that closes the basket")
    ap.add_argument("--floor", type=float,
                    help="protected floor once the basket has run")
    ap.add_argument("--no-runner", action="store_true",
                    help="close at the target instead of trailing past it")
    ap.add_argument("--cycle-drawdown", type=float,
                    help="MAX_CYCLE_DRAWDOWN in account currency (0 = off)")
    ap.add_argument("--spread", type=float, default=0.08)
    ap.add_argument("--commission", type=float, default=cfg.COMMISSION_PER_LOT)
    ap.add_argument("--steps", type=int, default=4, help="price steps per bar leg")
    ap.add_argument("--optimistic", action="store_true",
                    help="walk the favourable extreme first (default: adverse)")
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--json", help="write the cycle table to this file")
    args = ap.parse_args(argv)

    spec = None
    if args.csv:
        bars = load_bars_csv(args.csv)
        if not bars:
            print(f"no usable bars in {args.csv}")
            return 2
    else:
        bars, spec = load_bars_mt5(args.symbol, args.timeframe, args.bars)

    overrides = {}
    if args.spacing:
        overrides["ladder_spacing"] = args.spacing
    if args.depth:
        overrides["ladder_depth"] = args.depth
    if args.target is not None:
        overrides["basket_profit_target"] = args.target
    if args.lot:
        overrides["lot_size"] = args.lot
    if args.entry_timeframe:
        overrides["entry_timeframe"] = args.entry_timeframe
    if args.max_depth is not None:
        overrides["max_ladder_depth"] = args.max_depth
    if args.activation is not None:
        overrides["profit_protection_activation"] = args.activation
    if args.trail is not None:
        overrides["profit_protection_trail"] = args.trail
    if args.floor is not None:
        overrides["min_protected_profit"] = args.floor
    if args.no_runner:
        overrides["profit_runner_enabled"] = False
    if args.cycle_drawdown is not None:
        overrides["max_cycle_drawdown"] = args.cycle_drawdown

    started = time.time()
    result = run_replay(bars, spec=spec, overrides=overrides,
                        spread=args.spread, commission_per_lot=args.commission,
                        steps_per_leg=args.steps,
                        adverse_first=not args.optimistic,
                        start_balance=args.balance,
                        bar_seconds=_BAR_SECONDS.get(args.timeframe, 300.0),
                        entry_gate=not args.no_entry_gate,
                        progress=max(1, len(bars) // 10))
    print(result.summary())
    print(f"(replayed in {time.time() - started:.1f}s)")
    if args.json:
        Path(args.json).write_text(json.dumps(result.cycles, indent=2))
        print(f"cycle table written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
