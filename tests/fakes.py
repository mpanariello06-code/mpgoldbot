"""Shared fakes: a scriptable tick feed and a ready-made paper broker."""
import time
from dataclasses import dataclass

from broker import PaperBroker, Tick
from price_utils import SymbolSpec


class TickFeed:
    """A price you can drive from a test."""

    def __init__(self, bid=4010.00, spread=0.08):
        self.bid = bid
        self.spread = spread

    def set(self, bid, spread=None):
        self.bid = round(bid, 2)
        if spread is not None:
            self.spread = spread
        return self

    def move(self, delta):
        return self.set(self.bid + delta)

    def __call__(self):
        return Tick(bid=round(self.bid, 2), ask=round(self.bid + self.spread, 2),
                    time=time.time())


def gold_spec(digits=2, point=0.01, stops_level=0, pip_points=0):
    return SymbolSpec(name="XAUUSD", digits=digits, point=point, tick_size=point,
                      tick_value=1.0, contract_size=100.0, volume_min=0.01,
                      volume_max=200.0, volume_step=0.01,
                      stops_level_points=stops_level,
                      pip_points_override=pip_points)


def make_paper(feed=None, spec=None, state_path=None, balance=1000.0, magic=88001199):
    feed = feed or TickFeed()
    spec = spec or gold_spec()
    broker = PaperBroker("XAUUSD", magic, spec_provider=lambda: spec,
                         tick_provider=feed, state_path=state_path,
                         start_balance=balance)
    return broker, feed


def trigger_buy(feed, level):
    """Move price so the ask sits exactly on a BUY STOP level."""
    return feed.set(round(level - feed.spread, 2))


def trigger_sell(feed, level):
    """Move price so the bid sits exactly on a SELL STOP level."""
    return feed.set(round(level, 2))


def reach_buy_tp(feed, tp):
    """Bid reaches a long position's take profit."""
    return feed.set(round(tp, 2))


def reach_sell_tp(feed, tp):
    """Ask reaches a short position's take profit."""
    return feed.set(round(tp - feed.spread, 2))


@dataclass
class CycleRecord:
    kind_of: str
    cycle_id: int
    total: float
    reason: str
    kind: str
    lost: bool
    sequence: object = None
    duration: float = 0.0
    next_id: int = None
    context: dict = None
    next_ladder_seconds: float = 0.0

    def __getitem__(self, i):
        return (self.kind_of, self.cycle_id, self.total, self.reason,
                self.kind, self.lost)[i]


class Recorder:
    """Collects engine hook calls so tests can assert on notifications."""

    def __init__(self):
        self.events = []
        self.entries = []
        self.closed = []
        self.cycles = []
        self.blocks = []
        self.entry_evals = []

    def hooks(self):
        return {
            "event": lambda e, m, f: self.events.append((e, m, f)),
            "entry": lambda p, i, c: self.entries.append((p, i, c.cycle_id)),
            "closed": lambda t, i, c, w: self.closed.append((t, i, c.cycle_id, w)),
            "cycle_started": lambda c, a: self.cycles.append(
                CycleRecord("start", c.cycle_id, 0.0, "", "", False)),
            "cycle_complete": lambda c, seq, tot, reason, kind, lost,
                                     duration=0.0, next_id=None, context=None,
                                     next_ladder_seconds=0.0:
                self.cycles.append(CycleRecord("complete", c.cycle_id, tot,
                                               reason, kind, lost,
                                               seq, duration, next_id,
                                               context or {},
                                               next_ladder_seconds)),
            "risk_blocked": lambda r: self.blocks.append(r),
            "entry_evaluation": lambda rec: self.entry_evals.append(rec),
        }

    def names(self):
        return [e[0] for e in self.events]

    def count(self, name):
        return self.names().count(name)
