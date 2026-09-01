"""
Rolling ladder scalping engine.

    PRICE MOVES -> LADDER LEVEL REACHED -> TRADE ENTERS -> SMALL TP ->
    TRADE CLOSES -> LADDER ROLLS FORWARD -> NEW LEVEL CREATED -> REPEAT

There is no signal engine: the ladder itself is the entry mechanism. Levels sit
on a fixed grid anchored at the start of each cycle, so they never churn while
price wiggles, and the *set* of live levels rolls with price.

Every level carries a stable identity (`RL<cycle><B|S><grid index>`) written
into the order comment, which is what makes the whole thing idempotent: on
every step the engine reconciles the ladder it wants against the orders and
positions MT5 actually reports, and only places or cancels the difference. That
also means it recovers from a restart, a reconnect or a crash by reading the
broker rather than trusting in-memory state.
"""

import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from broker import BUY, BUY_STOP, SELL, SELL_STOP
from exit_engine import (CONTINUE, EXIT, PROFIT_FALLBACK, RISK_DRAWDOWN,
                         RISK_TIMEOUT, ExitConfig, LadderSequence,
                         RollingLadderExitEngine)

COMMENT_RE = re.compile(r"^RL(\d+)([BS])(-?\d+)")


# ---------------------------------------------------------------- states
class State:
    """
    The cycle state machine.

        IDLE -> SAFETY_CHECK -> BUILDING_LADDER -> LADDER_ACTIVE -> TRADING
             -> CLOSING_CYCLE -> VERIFYING_FLAT -> COOLDOWN_AFTER_EXIT
             -> NEW_CYCLE -> BUILDING_LADDER ...

    The market reading (continuation / reversal / uncertain / exit evaluation)
    is the exit engine's own `state`, reported alongside this one: this is what
    the bot is DOING, that is what it is SEEING.
    """
    IDLE = "IDLE"
    INITIALIZING = "INITIALIZING"
    SAFETY_CHECK = "SAFETY_CHECK"        # risk + spread gates before deploying
    BUILDING_LADDER = "BUILDING_LADDER"  # placing the first ladder of a cycle
    LADDER_ACTIVE = "LADDER_ACTIVE"      # ladder live, no position yet
    POSITION_ACTIVE = "POSITION_ACTIVE"  # a.k.a. TRADING: the basket is open
    TRADING = "POSITION_ACTIVE"          # alias, same state
    ROLLING = "ROLLING"
    CLOSING_CYCLE = "CLOSING_CYCLE"      # closing the basket out
    VERIFYING_FLAT = "VERIFYING_FLAT"    # re-reading MT5 until it confirms flat
    CYCLE_COMPLETE = "CYCLE_COMPLETE"
    COOLDOWN_AFTER_EXIT = "COOLDOWN_AFTER_EXIT"   # flat, settling before re-entry
    NEW_CYCLE = "NEW_CYCLE"
    RISK_BLOCKED = "RISK_BLOCKED"
    ERROR = "ERROR"


# ---------------------------------------------------------------- direction
class Direction:
    OFF = "off"            # ladder both ways (filter disabled)
    BOTH = "both"
    BUY_BIAS = "buy_bias"
    SELL_BIAS = "sell_bias"
    NONE = "none"          # no new entries


class DirectionFilter:
    """
    Optional bias source. Disabled by default - the base strategy is purely
    price driven. A model (PPO or anything else) can be plugged in later by
    passing an object with the same `decide()` signature.
    """

    def __init__(self, mode=Direction.OFF):
        self.mode = mode

    def decide(self, context=None):
        """Return (allow_buy, allow_sell)."""
        mode = (self.mode or Direction.OFF).lower()
        if mode in (Direction.OFF, Direction.BOTH):
            return True, True
        if mode == Direction.BUY_BIAS:
            return True, False
        if mode == Direction.SELL_BIAS:
            return False, True
        return False, False


def _seconds(value):
    """A wait, written the way a person reads it: 10s, 90s, 15m 30s."""
    value = max(0.0, float(value))
    if value < 120:
        return f"{value:.0f}s"
    minutes, rest = divmod(int(round(value)), 60)
    return f"{minutes}m" if not rest else f"{minutes}m {rest}s"


def level_comment(cycle_id, side, index):
    return f"RL{int(cycle_id)}{'B' if side in (BUY, BUY_STOP) else 'S'}{int(index)}"


def parse_comment(comment):
    """(cycle_id, side, index) from a level comment, or None."""
    m = COMMENT_RE.match((comment or "").strip())
    if not m:
        return None
    return int(m.group(1)), (BUY if m.group(2) == "B" else SELL), int(m.group(3))


@dataclass
class DesiredLevel:
    side: str            # BUY_STOP / SELL_STOP
    index: int
    price: float
    tp: float
    sl: float
    comment: str
    # False while the level sits inside the minimum creation distance: it may
    # stay alive if it is already placed, but a new one is not created there.
    placeable: bool = True


@dataclass
class Cycle:
    cycle_id: int = 1
    anchor: float = 0.0
    started_at: float = field(default_factory=time.time)
    tp_count: int = 0
    trades: int = 0
    realized: float = 0.0
    base_buy_index: int = None      # static roll mode pins the ladder here
    base_sell_index: int = None

    def to_dict(self):
        return {k: getattr(self, k) for k in
                ("cycle_id", "anchor", "started_at", "tp_count", "trades",
                 "realized", "base_buy_index", "base_sell_index")}


class RollingLadderEngine:
    """One ladder, one cycle at a time, reconciled against the broker."""

    def __init__(self, broker, settings, hooks=None, state_path=None,
                 clock=time.time):
        # Injectable so a replay can run on simulated time: cooldowns, order
        # ages and trigger gaps then behave the same as they would live.
        self.clock = clock
        self.broker = broker
        self.settings = settings          # RuntimeSettings (thread-safe)
        self.hooks = hooks or {}
        self._path = Path(state_path) if state_path else None
        self._lock = threading.RLock()

        self.state = State.IDLE
        self.paused = False
        self.cycle = Cycle()
        # Adaptive exit: the cycle ends on market structure, never on a trade
        # count or a dollar amount.
        self.exit_engine = RollingLadderExitEngine()
        self.sequence = None
        self.assessment = None
        # Cycle transitions are guarded so one exit event can only ever produce
        # one new cycle, however many passes the close takes to verify.
        self._transition_lock = threading.RLock()
        self._closing_cycle = None
        self.max_cycle_id = 0
        self.session_profit = 0.0
        self.spec = None
        self.last_tick = None
        self.last_update = None
        self.block_reason = ""
        self.spread_blocked = False

        self.daily_profit = 0.0
        self.daily_date = self._today()
        self.consecutive_losing_cycles = 0
        self.cooldown_until = 0.0
        self._streak_paused = False
        self.total_tp = 0
        self.total_trades = 0

        self._known_positions = {}        # ticket -> OpenPosition
        self._known_orders = {}           # ticket -> PendingOrder
        self._levels_open = set()         # (side, index) currently holding a position
        self._levels_done = set()         # (side, index) consumed this cycle
        self._triggered_keys = set()      # (side, index) already in the sequence
        self._depth_capped_logged = False
        self._profit_since = None         # when the basket first cleared the buffer
        self._last_place_error = 0.0
        # There is exactly one active cycle at a time. Between a cycle closing
        # and the next ladder going out the engine is deliberately cycle-less:
        # no ladder, no orders, no new cycle, until the re-entry cooldown has
        # elapsed AND the account is verified flat.
        self.cycle_active = True
        self.reentry_until = 0.0
        self._cycle_announced = False     # the new ladder's one deploy message

    # =================================================================== utils
    def _today(self):
        # Read from the engine's own clock, not the wall clock: under a
        # simulated clock (replay) the day has to roll with the data, or the
        # daily drawdown guard becomes a permanent stop after the first bad day.
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")

    def _emit(self, name, *args, **kwargs):
        fn = self.hooks.get(name)
        if not fn:
            return
        try:
            fn(*args, **kwargs)
        except Exception as exc:                      # hooks never break trading
            print(f"[ladder] hook {name} failed: {exc}")

    def _event(self, event, message="", **fields):
        self._emit("event", event, message, fields)

    # ============================================================ persistence
    def save(self):
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "cycle": self.cycle.to_dict(),
                "daily_profit": self.daily_profit,
                "daily_date": self.daily_date,
                "max_cycle_id": self.max_cycle_id,
                "consecutive_losing_cycles": self.consecutive_losing_cycles,
                "cooldown_until": self.cooldown_until,
                "cycle_active": self.cycle_active,
                "reentry_until": self.reentry_until,
                "streak_paused": self._streak_paused,
                "total_tp": self.total_tp,
                "total_trades": self.total_trades,
                "levels_done": sorted(f"{s}:{i}" for s, i in self._levels_done),
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self._path)
        except Exception as exc:
            print(f"[ladder] state save failed: {exc}")

    def load(self):
        if not self._path or not self._path.exists():
            return False
        try:
            data = json.loads(self._path.read_text())
        except Exception as exc:
            print(f"[ladder] state load failed: {exc}")
            return False
        cyc = data.get("cycle") or {}
        self.cycle = Cycle(**{k: cyc[k] for k in cyc if k in Cycle.__annotations__})
        if data.get("daily_date") == self._today():
            self.daily_profit = float(data.get("daily_profit", 0.0))
            self.daily_date = data["daily_date"]
        self.max_cycle_id = int(data.get("max_cycle_id", 0))
        self.consecutive_losing_cycles = int(data.get("consecutive_losing_cycles", 0))
        self.cooldown_until = float(data.get("cooldown_until", 0.0))
        # A restart inside the re-entry cooldown honours what is left of it.
        self.cycle_active = bool(data.get("cycle_active", True))
        self.reentry_until = float(data.get("reentry_until", 0.0))
        self._streak_paused = bool(data.get("streak_paused", False))
        self.total_tp = int(data.get("total_tp", 0))
        self.total_trades = int(data.get("total_trades", 0))
        for item in data.get("levels_done", []):
            side, _, idx = item.partition(":")
            try:
                self._levels_done.add((side, int(idx)))
            except ValueError:
                pass
        return True

    # ================================================================ startup
    def resume(self):
        """
        Rebuild the ladder state from the broker, not from memory.

        Anything MT5 reports wins: the cycle id comes from live order/position
        comments when they exist, so a restart mid-cycle continues that cycle
        instead of opening a second ladder on top of it.
        """
        self.state = State.INITIALIZING
        self.spec = self.broker.symbol_spec()
        self.load()

        positions = self.broker.positions()
        orders = self.broker.orders()

        live_cycles = set()
        for item in list(positions) + list(orders):
            parsed = parse_comment(item.comment)
            if parsed:
                live_cycles.add(parsed[0])

        if live_cycles:
            adopted = max(live_cycles)
            self.max_cycle_id = max(self.max_cycle_id, adopted)
            if adopted != self.cycle.cycle_id:
                self.cycle.cycle_id = adopted
            if not self.cycle.anchor:
                self.cycle.anchor = self._anchor_from(orders, positions)
            self.cycle_active = True
            self.reentry_until = 0.0
            self._event("LADDER_RECOVERED",
                        f"Adopted cycle #{adopted} from {len(orders)} orders / "
                        f"{len(positions)} positions")
        elif not self.cycle_active and self._reentry_wait() > 0:
            # restarted inside a re-entry cooldown: serve out what is left of it
            self.state = State.COOLDOWN_AFTER_EXIT
            self._event("CYCLE_COOLDOWN_STARTED",
                        f"restart during the re-entry cooldown - next ladder in "
                        f"{_seconds(self._reentry_wait())}", status="COOLDOWN")
        elif not self.cycle.anchor or not self.cycle_active:
            self._start_cycle(new_id=self.cycle.cycle_id if not self.cycle.anchor
                              else None, reason="startup")

        if self.cycle_active and (
                self.sequence is None or
                self.sequence.cycle_id != self.cycle.cycle_id):
            # after a restart the sequence restarts empty: only what MT5 can
            # still show us is trustworthy, and trigger history is not stored
            # on the broker.
            self.sequence = LadderSequence(
                self.cycle.cycle_id, self.cycle.anchor,
                float(self.settings.get("ladder_spacing")),
                started_at=self.clock())

        for p in positions:
            self._known_positions[p.ticket] = p
            parsed = parse_comment(p.comment)
            if parsed and parsed[0] == self.cycle.cycle_id:
                self._levels_open.add((parsed[1], parsed[2]))
                self._levels_done.add((parsed[1], parsed[2]))
        for o in orders:
            self._known_orders[o.ticket] = o

        self.state = State.LADDER_ACTIVE if orders else State.INITIALIZING
        self.save()
        return True

    def _anchor_from(self, orders, positions):
        """Recover the grid anchor from any live level's price and index."""
        spacing = float(self.settings.get("ladder_spacing"))
        for item in list(orders) + list(positions):
            parsed = parse_comment(item.comment)
            if parsed and parsed[2] != 0:
                price = getattr(item, "price", None) or getattr(item, "price_open", 0.0)
                return price - parsed[2] * spacing
        tick = self.broker.tick()
        return tick.mid

    def _start_cycle(self, new_id=None, reason="", require_flat=True):
        """
        Open a new cycle. Returns True when one was started.

        MAX_ACTIVE_CYCLES = 1. A new cycle is refused outright while anything
        from a previous one is still live - that is the only way #7, #8 and #9
        can never exist at the same time.
        """
        if require_flat:
            leftovers = [item for item in
                         list(self.broker.positions()) + list(self.broker.orders())
                         if parse_comment(item.comment)]
            if leftovers:
                self._event("CYCLE_REENTRY_BLOCKED",
                            f"refusing to start a new cycle: {len(leftovers)} "
                            f"positions/orders from cycle(s) "
                            f"{sorted({parse_comment(i.comment)[0] for i in leftovers})} "
                            f"are still live", status="RETRY")
                return False
        tick = self.last_tick or self.broker.tick()
        anchor = self.spec.normalize_price(tick.mid) if self.spec else tick.mid
        # a cycle id is never reused, even after a restart or an adopted cycle
        self.max_cycle_id = max(self.max_cycle_id, self.cycle.cycle_id)
        cid = new_id if new_id is not None else self.max_cycle_id + 1
        self.max_cycle_id = max(self.max_cycle_id, cid)
        # started_at must come from the engine's clock, not the wall clock, or
        # cycle age (and therefore the timeout) is meaningless under replay.
        self.cycle = Cycle(cycle_id=cid, anchor=anchor,
                           started_at=self.clock())
        self.sequence = LadderSequence(cid, anchor,
                                       float(self.settings.get("ladder_spacing")),
                                       started_at=self.clock())
        self.assessment = None
        self._levels_open.clear()
        self._levels_done.clear()
        self._triggered_keys.clear()
        self._depth_capped_logged = False
        self._profit_since = None
        self.cycle_active = True
        self.reentry_until = 0.0
        self._cycle_announced = False
        self._event("CYCLE_STARTED",
                    f"Cycle #{cid} anchored at {anchor} ({reason})",
                    cycle_id=cid, entry_price=anchor)
        self._emit("cycle_started", self.cycle, anchor)
        self.save()
        return True

    def reanchor(self, reason="reset"):
        """
        Move the grid to the current price without ending the cycle.

        Pending orders are cancelled (they belong to the old grid); open
        positions and the cycle's TP count are untouched.
        """
        try:
            tick = self.broker.tick()
            self._cancel_all(self.broker.orders(), f"re-anchor: {reason}")
            anchor = self.spec.normalize_price(tick.mid) if self.spec else tick.mid
            self.cycle.anchor = anchor
            self.cycle.base_buy_index = None
            self.cycle.base_sell_index = None
            self._levels_done = set(self._levels_open)
            self._event("LADDER_CREATED",
                        f"Ladder re-anchored at {anchor} ({reason})",
                        cycle_id=self.cycle.cycle_id, entry_price=anchor)
            self.save()
            return True
        except Exception as exc:
            self._event("ERROR", f"Re-anchor failed: {exc}", status="ERROR")
            return False

    # ============================================================ ladder maths
    def desired_levels(self, tick, snap, orders=()):
        """
        The levels that should be live right now.

        The window ROLLS, it does not slide: a level that has already been
        placed stays exactly where it is until price reaches it (or it drifts
        far out of range), and the ladder extends further out as levels are
        consumed. Recomputing the near edge from price on every pass would push
        the next level away from the market each time price advanced, and
        nothing would ever trigger.

        `first_level_offset` therefore gates where a NEW level may be created;
        the broker's minimum stop distance is what keeps an existing one alive.
        """
        spec = self.spec
        spacing = float(snap["ladder_spacing"])
        depth = int(snap["ladder_depth"])
        anchor = self.cycle.anchor
        if spacing <= 0 or depth <= 0 or not anchor:
            return []

        min_stop = spec.min_stop_distance + spec.point
        offset = max(float(snap["first_level_offset"]), min_stop)
        # a level this far from price has been left behind: let the ladder
        # re-centre rather than keep orders nobody will reach
        out_of_range = depth * spacing + offset

        allow_buy, allow_sell = DirectionFilter(snap["direction_filter"]).decide()
        tp_distance = self.tp_distance(snap)
        sl_distance = float(snap["stop_loss_distance"])

        live = {BUY_STOP: set(), SELL_STOP: set()}
        for order in orders:
            parsed = parse_comment(order.comment)
            if parsed and parsed[0] == self.cycle.cycle_id:
                side = BUY_STOP if parsed[1] == BUY else SELL_STOP
                live[side].add(parsed[2])

        def build(side, direction):
            """direction: +1 for the buy side above price, -1 for sells below."""
            # A live level is never pulled just because price got close to it:
            # that is the moment it is about to do its job. Only distance from
            # the market retires it.
            if direction > 0:
                create_ok = lambda price: price >= tick.ask + offset
                in_range = lambda price: price <= tick.ask + out_of_range
            else:
                create_ok = lambda price: price <= tick.bid - offset
                in_range = lambda price: price >= tick.bid - out_of_range

            def price_at(index):
                return spec.normalize_price(anchor + index * spacing)

            kept = sorted((i for i in live[side] if in_range(price_at(i))),
                          reverse=direction < 0)
            indexes = list(kept)

            # extend beyond the outermost live level, or start fresh from the
            # first index the offset allows
            if indexes:
                nxt = indexes[-1] + direction
            else:
                start = (tick.ask + offset) if direction > 0 else (tick.bid - offset)
                nxt = int(math.ceil((start - anchor) / spacing - 1e-9)) if direction > 0 \
                    else int(math.floor((start - anchor) / spacing + 1e-9))

            guard = 0
            while len(indexes) < depth and guard < depth * 8:
                guard += 1
                price = price_at(nxt)
                if create_ok(price) and nxt not in indexes:
                    indexes.append(nxt)
                nxt += direction
            return indexes

        if snap["roll_mode"] == "static":
            # the cycle's grid is fixed: pin the window once and let price
            # consume it
            if self.cycle.base_buy_index is None:
                self.cycle.base_buy_index = int(math.ceil(
                    (tick.ask + offset - anchor) / spacing - 1e-9))
                self.cycle.base_sell_index = int(math.floor(
                    (tick.bid - offset - anchor) / spacing + 1e-9))
            buy_indexes = [self.cycle.base_buy_index + i for i in range(depth)]
            sell_indexes = [self.cycle.base_sell_index - i for i in range(depth)]
        else:
            buy_indexes = build(BUY_STOP, +1)
            sell_indexes = build(SELL_STOP, -1)

        levels = []
        if allow_buy:
            for idx in buy_indexes:
                price = spec.normalize_price(anchor + idx * spacing)
                levels.append(DesiredLevel(
                    side=BUY_STOP, index=idx, price=price,
                    tp=spec.normalize_price(price + tp_distance) if tp_distance else 0.0,
                    sl=spec.normalize_price(price - sl_distance) if sl_distance else 0.0,
                    comment=level_comment(self.cycle.cycle_id, BUY, idx),
                    placeable=price >= tick.ask + offset,
                ))
        if allow_sell:
            for idx in sell_indexes:
                price = spec.normalize_price(anchor + idx * spacing)
                levels.append(DesiredLevel(
                    side=SELL_STOP, index=idx, price=price,
                    tp=spec.normalize_price(price - tp_distance) if tp_distance else 0.0,
                    sl=spec.normalize_price(price + sl_distance) if sl_distance else 0.0,
                    comment=level_comment(self.cycle.cycle_id, SELL, idx),
                    placeable=price <= tick.bid - offset,
                ))
        return levels

    def tp_distance(self, snap):
        """
        Individual TP in price units, or 0 for the basket architecture.

        0 is the default and the point of the strategy: a triggered level is
        NOT an independent trade with its own target, it is one leg of a basket
        the exit engine closes as a whole. The other modes exist for anyone who
        deliberately wants per-trade exits back.
        """
        mode = snap["tp_mode"]
        if mode == "none":
            return 0.0
        if mode == "levels":
            return int(snap["tp_levels"]) * float(snap["ladder_spacing"])
        if mode == "distance":
            return float(snap["tp_distance"])
        pips = {"1_pip": 1, "2_pips": 2, "3_pips": 3, "4_pips": 4, "5_pips": 5}
        return self.spec.pips_to_price(pips.get(mode, 1))

    # ============================================================ exit engine
    def exit_config(self, snap):
        """Build the exit engine's configuration from the live settings."""
        cfg = ExitConfig()
        for key in cfg.as_dict():
            value = snap.get(f"exit_{key}")
            if value is not None:
                setattr(cfg, key, type(getattr(cfg, key))(value))
        return cfg

    def money_per_level(self, snap):
        """Account currency one ladder level is worth at the configured lot."""
        if not self.spec:
            return 0.0
        return self.spec.money_per_price_unit(snap["lot_size"]) * \
            float(snap["ladder_spacing"])

    # ============================================================ the basket
    # The cycle - not the individual trade - is the unit of management. Every
    # order and position the ladder creates carries `RL<cycle><B|S><index>` in
    # its comment, and the broker adapters already filter by magic and symbol,
    # so a basket is identified by (symbol, magic, cycle_id).
    def _mine(self, item, cycle_id):
        """
        Does this order/position belong to the given cycle?

        A parsed comment is definitive. An unparseable one is still counted:
        it reached us through the magic+symbol filter, so it is our exposure -
        some brokers truncate or strip comments, and silently dropping those
        positions would understate the basket. Only a comment naming a
        DIFFERENT cycle is excluded.
        """
        parsed = parse_comment(item.comment)
        return parsed is None or parsed[0] == cycle_id

    def cycle_positions(self, cycle_id=None, positions=None):
        """Open positions belonging to a cycle (default: the current one)."""
        cid = self.cycle.cycle_id if cycle_id is None else cycle_id
        items = self.broker.positions() if positions is None else positions
        return [p for p in items if self._mine(p, cid)]

    def cycle_orders(self, cycle_id=None, orders=None):
        """Pending orders belonging to a cycle (default: the current one)."""
        cid = self.cycle.cycle_id if cycle_id is None else cycle_id
        items = self.broker.orders() if orders is None else orders
        return [o for o in items if self._mine(o, cid)]

    def get_cycle_floating_pnl(self, cycle_id=None, positions=None):
        """
        Combined floating P/L of every OPEN position in the basket.

        With no open positions this is 0.00 - never the cycle's realized
        profit wearing a floating label.
        """
        return round(sum(p.profit for p in
                         self.cycle_positions(cycle_id, positions)), 2)

    def get_cycle_realized_pnl(self, cycle_id=None):
        """Banked P/L for the cycle. Under the basket architecture this stays
        0.00 until the basket is closed, because nothing closes on its own."""
        if cycle_id is not None and cycle_id != self.cycle.cycle_id:
            return 0.0
        return round(self.cycle.realized, 2)

    def get_cycle_net_pnl(self, cycle_id=None, positions=None):
        """Realized + floating: what the cycle is actually worth right now."""
        return round(self.get_cycle_realized_pnl(cycle_id) +
                     self.get_cycle_floating_pnl(cycle_id, positions), 2)

    def get_cycle_drawdown(self, cycle_id=None):
        """How far the basket has given back from its own peak."""
        if self.sequence is None or (cycle_id is not None and
                                     cycle_id != self.sequence.cycle_id):
            return 0.0
        return round(self.sequence.drawdown, 2)

    def basket(self, positions=None, orders=None):
        """Everything about the current basket, in one call."""
        cid = self.cycle.cycle_id
        pos = self.cycle_positions(cid, positions)
        ords = self.cycle_orders(cid, orders)
        return {
            "cycle_id": cid,
            "active": self.cycle_active,
            "open_positions": len(pos),
            "open_buys": len([p for p in pos if p.side == BUY]),
            "open_sells": len([p for p in pos if p.side == SELL]),
            "pending_orders": len(ords),
            "pending_buys": len([o for o in ords if o.side == BUY_STOP]),
            "pending_sells": len([o for o in ords if o.side == SELL_STOP]),
            "floating_pnl": self.get_cycle_floating_pnl(cid, pos),
            "realized_pnl": self.get_cycle_realized_pnl(cid),
            "net_pnl": self.get_cycle_net_pnl(cid, pos),
            "drawdown": self.get_cycle_drawdown(cid),
        }

    def evaluate_exit(self, snap, tick, positions):
        """
        Score the CYCLE, not any single trade.

        The exit engine only ever sees the basket's combined P/L: a BUY leg
        sitting at -1.20 while three SELL legs sit at +2.40 is a basket at
        +1.20, and that is the number the decision is made on. No position is
        ever closed for being individually profitable or individually losing.
        """
        if self.sequence is None:
            return None
        basket = self.cycle_positions(self.cycle.cycle_id, positions)
        self.sequence.update_price(tick.mid, ts=tick.time or self.clock())
        self.sequence.update_pnl(self.get_cycle_floating_pnl(
            self.cycle.cycle_id, basket))
        self.exit_engine.config = self.exit_config(snap)
        self.assessment = self.exit_engine.assess(
            self.sequence,
            money_per_level=self.money_per_level(snap),
            has_exposure=bool(basket),
        )
        return self.assessment

    # ================================================================= risk
    def risk_check(self, snap, tick, positions, orders):
        """
        Returns (allow_new_entries, reason).

        A breach never closes anything by itself: new entries stop and pending
        orders are cancelled, open positions keep running under their own TP/SL.
        """
        if self.daily_date != self._today():
            self.daily_date = self._today()
            self.daily_profit = 0.0

        max_daily = float(snap["max_daily_drawdown"])
        if max_daily > 0 and self.daily_profit <= -abs(max_daily):
            return False, f"daily drawdown limit hit ({self.daily_profit:.2f})"

        # Losing streak: a circuit breaker, not a kill switch. It pauses for the
        # cooldown and then lets the bot try again with a clean count - the
        # streak can only reset on a winning cycle, which can never happen while
        # entries are blocked. With no cooldown configured it stays a hard stop,
        # which is then the operator's explicit choice.
        max_streak = int(snap["max_consecutive_losing_cycles"])
        cooldown = float(snap["cooldown_after_loss_minutes"]) * 60.0
        if max_streak > 0 and self.consecutive_losing_cycles >= max_streak:
            if cooldown <= 0:
                return False, (f"{self.consecutive_losing_cycles} losing cycles "
                               f"in a row (no cooldown configured)")
            if not self._streak_paused:
                # the step loop logs the block itself when the reason changes
                self._streak_paused = True
                self.cooldown_until = max(self.cooldown_until,
                                          self.clock() + cooldown)
            if self.clock() < self.cooldown_until:
                left = int(self.cooldown_until - self.clock())
                return False, (f"{self.consecutive_losing_cycles} losing cycles "
                               f"in a row (resuming in {left}s)")
            self.consecutive_losing_cycles = 0
            self._streak_paused = False
            self._event("RISK_CLEARED",
                        "losing-streak pause elapsed - resuming new cycles")

        if self.clock() < self.cooldown_until:
            left = int(self.cooldown_until - self.clock())
            return False, f"cooldown after loss ({left}s left)"

        # Position cap stops new entries and pulls the pending levels, which
        # would otherwise breach the cap the moment they trigger.
        if len(positions) >= int(snap["max_open_positions"]):
            return False, f"max open positions ({len(positions)})"

        lot = float(snap["lot_size"])
        if lot > float(snap["max_lot_size"]):
            return False, f"lot {lot} above MAX_LOT_SIZE {snap['max_lot_size']}"

        return True, ""

    def spread_ok(self, snap, tick):
        limit = float(snap["max_spread"])
        if limit <= 0:
            return True
        return tick.spread <= limit + 1e-12

    # ================================================================== step
    def step(self):
        """One reconciliation pass. Safe to call every poll."""
        try:
            return self._step()
        except Exception as exc:
            self.state = State.ERROR
            self.block_reason = str(exc)
            self._event("ERROR", f"Ladder step failed: {exc}", status="ERROR")
            return False

    def _step(self):
        snap = self.settings.snapshot()
        self.spec = self.broker.symbol_spec().with_pip_override(snap["pip_points"])

        # 1. closed trades first, so cycle accounting is current
        for trade in self.broker.poll_closed():
            self._on_closed(trade, snap)

        tick = self.broker.tick()
        self.last_tick = tick
        positions = self.broker.positions()
        orders = self.broker.orders()

        # 2. newly triggered levels
        self._detect_triggers(positions, orders, snap, tick)

        # 3. cycle completion / loss - and, on a clean handoff, immediately
        #    continue this same pass so the next ladder goes out at once
        rolled = self._check_cycle(snap, tick, positions, orders)
        if rolled == "pending":
            self.last_update = datetime.now()
            return True
        if rolled == "restarted":
            positions = self.broker.positions()
            orders = self.broker.orders()

        # belt and braces: with no active cycle nothing may be placed, whatever
        # else happened above
        if not self.cycle_active:
            self.state = State.COOLDOWN_AFTER_EXIT
            self.last_update = datetime.now()
            return True

        # 4. paused: no NEW entries, existing positions still managed
        if self.paused:
            if orders:
                self._cancel_all(orders, "trading paused")
            self.state = State.POSITION_ACTIVE if positions else State.IDLE
            self.last_update = datetime.now()
            return True

        # 5. risk + spread gates
        self.state = State.SAFETY_CHECK
        allow, reason = self.risk_check(snap, tick, positions, orders)
        if not allow:
            if self.block_reason != reason:
                self.block_reason = reason
                self._event("RISK_BLOCK", reason, status="BLOCKED")
                self._emit("risk_blocked", reason)
            self._cancel_all(orders, "risk block")
            self.state = State.RISK_BLOCKED if not positions else State.POSITION_ACTIVE
            self.last_update = datetime.now()
            return True
        if self.block_reason:
            self._event("RISK_CLEARED", f"resumed after: {self.block_reason}")
            self.block_reason = ""

        if not self.spread_ok(snap, tick):
            if not self.spread_blocked:
                self.spread_blocked = True
                self._event("SPREAD_BLOCK",
                            f"spread {tick.spread:.2f} > limit {snap['max_spread']}",
                            status="BLOCKED")
            self.state = State.POSITION_ACTIVE if positions else State.LADDER_ACTIVE
            self.last_update = datetime.now()
            return True
        if self.spread_blocked:
            self.spread_blocked = False
            self._event("SPREAD_CLEARED", f"spread back to {tick.spread:.2f}")

        # 6. reconcile the ladder
        if not orders and not positions:
            self.state = State.BUILDING_LADDER
        live_orders = self._reconcile(snap, tick, positions, orders)

        # POSITION_ACTIVE is the TRADING state: the basket is open and being
        # managed as one unit.
        self.state = (State.POSITION_ACTIVE if positions
                      else State.LADDER_ACTIVE if live_orders else State.ROLLING)
        self.last_update = datetime.now()
        return True

    # ------------------------------------------------------------ triggers
    def _detect_triggers(self, positions, orders, snap, tick):
        current = {p.ticket: p for p in positions}
        for ticket, pos in current.items():
            if ticket in self._known_positions:
                continue
            parsed = parse_comment(pos.comment)
            index = parsed[2] if parsed else 0
            side = parsed[1] if parsed else pos.side
            self._levels_open.add((side, index))
            self._levels_done.add((side, index))
            self.cycle.trades += 1
            self.total_trades += 1
            self._record_trigger(side, index, pos.price_open,
                                 pos.time_open or self.clock())
            self._event("ORDER_TRIGGERED",
                        f"{pos.side} {pos.volume} @ {pos.price_open} (level {index:+d})",
                        cycle_id=self.cycle.cycle_id, direction=pos.side, level=index,
                        entry_price=pos.price_open, tp=pos.tp, sl=pos.sl,
                        lot_size=pos.volume, position_ticket=ticket,
                        spread=tick.spread)
            self._emit("entry", pos, index, self.cycle)
        self._known_positions = current

        live = {o.ticket: o for o in orders}
        self._known_orders = live

    def _record_trigger(self, side, index, price, ts):
        """Add a level to the sequence exactly once per cycle."""
        if self.sequence is None:
            return False
        key = (side, index)
        if key in self._triggered_keys:
            return False
        self._triggered_keys.add(key)
        self.sequence.record_trigger(side, index, price, ts=ts)
        return True

    # --------------------------------------------------------- closed trades
    def _on_closed(self, trade, snap):
        parsed = parse_comment(trade.comment)
        index = parsed[2] if parsed else 0
        cycle_id = parsed[0] if parsed else self.cycle.cycle_id
        side = parsed[1] if parsed else trade.side

        self._levels_open.discard((side, index))
        self.daily_profit += trade.profit
        self.session_profit += trade.profit
        if cycle_id == self.cycle.cycle_id:
            self.cycle.realized += trade.profit

        # Only money decides: a TP close that still lost (a gapped fill, say)
        # must not count toward the profit cycle.
        is_win = trade.profit > 0
        if is_win and cycle_id == self.cycle.cycle_id:
            self.cycle.tp_count += 1
            self.total_tp += 1

        if self.sequence is not None and cycle_id == self.cycle.cycle_id:
            # A level can fill and hit its TP inside a single poll, so it never
            # shows up in positions(). Record the entry here or the sequence
            # would silently lose a trigger.
            self._record_trigger(side, index, trade.price_open, trade.time_close)
            self.sequence.record_close(side, index, trade.price_open,
                                       trade.price_close, trade.profit,
                                       reason=trade.reason, ts=trade.time_close)

        event = "TP_HIT" if trade.reason == "TP" else (
            "SL_HIT" if trade.reason == "SL" else "POSITION_CLOSED")
        self._event(event,
                    f"{trade.side} level {index:+d} {trade.price_open} -> "
                    f"{trade.price_close} = {trade.profit:+.2f}",
                    cycle_id=cycle_id, direction=trade.side, level=index,
                    entry_price=trade.price_open, exit_price=trade.price_close,
                    lot_size=trade.volume, position_ticket=trade.ticket,
                    profit=trade.profit, cycle_profit=self.cycle.realized,
                    daily_profit=self.daily_profit)
        self._emit("closed", trade, index, self.cycle, is_win)

        # rolling forward: the level is free to re-arm once its trade is done
        if snap["roll_mode"] == "extend" and snap["rearm_levels"]:
            self._levels_done.discard((side, index))
            self._event("LEVEL_ROLLED", f"level {index:+d} re-armed",
                        cycle_id=cycle_id, direction=side, level=index)
        self.save()

    # --------------------------------------------------------------- cycles
    def _check_cycle(self, snap, tick, positions, orders):
        """
        Decide whether this cycle is over, and drive the transition.

        Two independent authorities:

          1. the risk manager, which force-closes a cycle that has drawn down
             past its limit - a loss guard, never a profit target;
          2. the exit engine, which reads continuation / reversal / exhaustion
             from the trigger sequence and the price path.

        There is deliberately no "N trades" and no "X dollars" branch here.

        Returns "pending" while a close is still being verified, "restarted"
        once the next cycle is live (the caller then re-reads state and deploys
        the new ladder in the same pass), or False when nothing happened.
        """
        with self._transition_lock:
            if self._closing_cycle is not None:
                return self._advance_close(snap, positions, orders)

            # Between cycles there is no ladder to evaluate: the only thing to
            # do is verify the account is flat and wait out the cooldown.
            if not self.cycle_active:
                return self._advance_reentry(snap, tick, positions, orders)

            assessment = self.evaluate_exit(snap, tick, positions)
            basket = self.sequence.basket_pnl if self.sequence else 0.0
            reason_code, detail = self._exit_reason(snap, assessment, basket,
                                                    positions)
            if reason_code is None:
                return False

            # What the cycle looked like at the moment the exit was decided.
            # Captured here, not after the close, so the CSV records the state
            # that caused the exit rather than the empty state that follows it.
            self._event("EXIT_TRIGGERED",
                        f"Cycle #{self.cycle.cycle_id} exit [{reason_code}]: "
                        f"{detail} - {len(positions)} positions / "
                        f"{len(orders)} pending orders to clear",
                        cycle_id=self.cycle.cycle_id, status="CLOSING")
            self._closing_cycle = {
                "cycle_id": self.cycle.cycle_id,
                "forced": reason_code.startswith("RISK"),
                "kind": reason_code,
                "reason": detail,
                "assessment": assessment,
                "attempts": 0,
                "context": {
                    "exit_price": tick.mid if tick else "",
                    "exit_bid": tick.bid if tick else "",
                    "exit_ask": tick.ask if tick else "",
                    "exit_spread": tick.spread if tick else "",
                    "open_positions_at_exit": len(positions),
                    "open_buys_at_exit": len([p for p in positions
                                              if p.side == BUY]),
                    "open_sells_at_exit": len([p for p in positions
                                               if p.side == SELL]),
                    "pending_orders_at_exit": len(orders),
                    "floating_pnl_at_exit": sum(p.profit for p in positions),
                },
            }
            return self._advance_close(snap, positions, orders)

    def _exit_reason(self, snap, assessment, basket, positions):
        """
        Decide whether the cycle ends, and name the reason.

        Priority, highest first:
          1. hard risk - drawdown, then cycle timeout;
          2. the adaptive exit engine (directional / reversal / extended);
          3. the profit-recovery fallback, so a basket that quietly came good
             is not held forever waiting for a scenario that never arrives.

        Returns (reason_code, detail) or (None, "") to keep the cycle running.
        """
        # --- 1. hard risk ----------------------------------------------------
        max_dd = float(snap["max_cycle_drawdown"])
        if max_dd > 0 and basket <= -abs(max_dd):
            return RISK_DRAWDOWN, f"cycle drawdown limit hit ({basket:+.2f})"

        max_minutes = float(snap["max_cycle_duration_minutes"])
        age = self.clock() - self.cycle.started_at
        if max_minutes > 0 and age >= max_minutes * 60 and \
                (positions or (self.sequence and self.sequence.total_triggers)):
            return RISK_TIMEOUT, (f"cycle open for {age / 60:.0f} min "
                                  f"(limit {max_minutes:.0f}) - closing out")

        # --- 2. the strategy's own reading ------------------------------------
        if assessment is not None and assessment.decision == EXIT:
            return (assessment.scenario or "EXIT_ENGINE"), assessment.reason

        # --- 3. profit recovery fallback --------------------------------------
        code, detail = self._profit_fallback(snap, assessment, basket)
        if code:
            return code, detail
        return None, ""

    def _profit_fallback(self, snap, assessment, basket):
        """
        A basket that has recovered into confirmed profit is taken, rather than
        held indefinitely waiting for a scenario that may never come.

        This is NOT a dollar target: the buffer is a fraction of what one ladder
        level is worth, so it scales with lot size and spacing, and the profit
        has to hold for a confirmation period before it counts. Strong
        continuation still wins - a run that is working is not cut short for a
        few cents.
        """
        if not snap["profit_fallback_enabled"]:
            self._profit_since = None
            return None, ""
        if self.sequence is None or self.sequence.total_triggers == 0:
            return None, ""

        buffer_money = float(snap["profit_fallback_buffer_levels"]) * \
            self.money_per_level(snap)
        if basket < buffer_money or buffer_money <= 0:
            self._profit_since = None
            return None, ""

        now = self.clock()
        if self._profit_since is None:
            self._profit_since = now
            return None, ""

        held = now - self._profit_since
        if held < float(snap["profit_confirmation_seconds"]):
            return None, ""

        guard = float(snap["profit_fallback_continuation_guard"])
        if assessment is not None and assessment.continuation_score >= guard:
            return None, ""          # the move is still working; let it run

        return PROFIT_FALLBACK, (f"basket recovered to {basket:+.2f} "
                                 f"(buffer {buffer_money:.2f}) and held for "
                                 f"{held:.0f}s with no primary exit")

    def _advance_close(self, snap, positions, orders):
        """
        Close the cycle out. The next cycle is NOT started here.

        EXIT -> cancel -> close -> VERIFY against what MT5 actually reports ->
        record -> FLAT -> cooldown. Every step is logged, because a cycle that
        opens and closes in seconds is otherwise impossible to audit. If the
        broker refused something, the leftovers are retried on the next pass
        instead of building a second ladder on top of the first.
        """
        info = self._closing_cycle
        info["attempts"] += 1
        self.state = State.CLOSING_CYCLE
        first = info["attempts"] == 1
        cid = info["cycle_id"]
        # the basket, not "whatever is open": only this cycle's legs are touched
        orders = self.cycle_orders(cid, orders)
        positions = self.cycle_positions(cid, positions)

        # --- cancel every pending order belonging to the cycle ---------------
        if orders:
            if first:
                self._event("EXIT_ORDERS_FOUND",
                            f"Cycle #{cid}: {len(orders)} pending orders to "
                            f"cancel ({', '.join(str(o.ticket) for o in orders)})",
                            cycle_id=cid, status="CLOSING")
            self._event("EXIT_CANCEL_SENT",
                        f"Cycle #{cid}: cancel requests sent for "
                        f"{len(orders)} orders", cycle_id=cid, status="CLOSING")
            self._cancel_all(orders, f"cycle end: {info['reason']}")

        # --- close every open position belonging to the cycle ----------------
        close_positions = info["forced"] or snap["cycle_close_positions"]
        if close_positions and positions:
            if first:
                self._event("EXIT_POSITIONS_FOUND",
                            f"Cycle #{cid}: {len(positions)} open positions to "
                            f"close ({', '.join(str(p.ticket) for p in positions)})",
                            cycle_id=cid, status="CLOSING")
            self._event("EXIT_CLOSE_SENT",
                        f"Cycle #{cid}: close requests sent for "
                        f"{len(positions)} positions",
                        cycle_id=cid, status="CLOSING")
            for pos in positions:
                ok, msg = self.broker.close_position(pos.ticket, comment="cycle end")
                if not ok:
                    # the broker's own retcode, never a bare failure
                    self._event("ERROR", f"close {pos.ticket} failed: {msg}",
                                cycle_id=cid, position_ticket=pos.ticket,
                                status="ERROR")
            closed = self.broker.poll_closed()
            for trade in closed:
                self._on_closed(trade, snap)
            if closed:
                self._event("EXIT_CLOSE_CONFIRMED",
                            f"Cycle #{cid}: {len(closed)} closes confirmed, "
                            f"P/L {sum(t.profit for t in closed):+.2f}",
                            cycle_id=cid, status="CLOSING")

        # --- verify against MT5: the old cycle must be gone ------------------
        self.state = State.VERIFYING_FLAT
        left_orders = self.cycle_orders(cid)
        left_positions = self.cycle_positions(cid) if close_positions else []
        self._event("EXIT_RECONCILED",
                    f"Cycle #{cid}: MT5 reports {len(left_positions)} positions "
                    f"/ {len(left_orders)} orders after attempt "
                    f"{info['attempts']}", cycle_id=cid,
                    status="CLOSING" if (left_orders or left_positions) else "OK")
        if left_orders or left_positions:
            if info["attempts"] in (3, 10) or info["attempts"] % 50 == 0:
                self._event(
                    "CYCLE_CLOSE_PENDING",
                    f"Cycle #{cid} still has "
                    f"{len(left_positions)} positions / {len(left_orders)} orders "
                    f"after {info['attempts']} attempts - retrying, no new "
                    f"ladder until it is clean",
                    cycle_id=cid, status="RETRY")
            return "pending"

        self._event("CYCLE_FLAT",
                    f"Cycle #{cid} confirmed FLAT: 0 positions, 0 pending orders",
                    cycle_id=cid, status="OK")

        # --- record ----------------------------------------------------------
        total = self.cycle.realized
        lost = total < 0
        sequence = self.sequence
        assessment = info["assessment"]

        if lost:
            self.consecutive_losing_cycles += 1
        else:
            self.consecutive_losing_cycles = 0
        # A risk-forced close arms the (much longer) loss cooldown on top of the
        # ordinary re-entry cooldown; the re-entry gate waits for whichever runs
        # longer.
        if info["forced"]:
            cooldown = float(snap["cooldown_after_loss_minutes"]) * 60.0
            if cooldown > 0:
                self.cooldown_until = self.clock() + cooldown

        self._event("CYCLE_LOSS" if lost else "CYCLE_COMPLETED",
                    f"Cycle #{cid}: {self.cycle.trades} triggers, "
                    f"{self.cycle.tp_count} TPs, P/L {total:+.2f} "
                    f"[{info['kind']}] {info['reason']}",
                    cycle_id=cid, profit=total,
                    cycle_profit=total, daily_profit=self.daily_profit,
                    status="LOSS" if lost else "OK")

        duration = self.clock() - self.cycle.started_at
        closed_cycle = self.cycle
        self._closing_cycle = None

        # --- the cycle is CLOSED; there is now no active cycle ---------------
        self.cycle_active = False
        self.sequence = None
        self.reentry_until = self.clock() + self._reentry_cooldown(snap)
        self.state = State.COOLDOWN_AFTER_EXIT
        wait = self._reentry_wait()
        self._event("CYCLE_COOLDOWN_STARTED",
                    f"Cycle #{cid} closed - no active cycle. Next ladder in "
                    f"{_seconds(wait)}.",
                    cycle_id=cid, status="COOLDOWN")
        self.save()
        self._emit("cycle_complete", closed_cycle, sequence, assessment, total,
                   info["reason"], info["kind"], lost, duration,
                   self.max_cycle_id + 1, info.get("context") or {}, wait)
        return "pending"

    def _reentry_cooldown(self, snap):
        """Mandatory settle time between one cycle closing and the next ladder."""
        return max(0.0, float(snap.get("cycle_reentry_cooldown_seconds", 0.0)))

    def _reentry_wait(self):
        """Seconds left before a new ladder may be built. 0 = go now."""
        return max(0.0, max(self.reentry_until, self.cooldown_until) - self.clock())

    def _advance_reentry(self, snap, tick, positions, orders):
        """
        EXIT -> RESET -> COOLDOWN -> NEW LADDER, the last two steps.

        Nothing is created here until the cooldown has elapsed AND the account
        is verified flat: no ladder, no pending orders, no cycle. Anything the
        close left behind is cleaned up and re-verified instead.
        """
        self.state = State.COOLDOWN_AFTER_EXIT

        # Leftovers cannot exist at this point, but if the broker produced one
        # anyway (a late fill, a manual order) it is cleared before re-entry
        # rather than being adopted by the new cycle.
        if orders:
            self._event("EXIT_ORDERS_FOUND",
                        f"{len(orders)} pending orders survived the close - "
                        f"cancelling before re-entry", status="RETRY")
            self._cancel_all(orders, "left over from the previous cycle")
        if positions and snap["cycle_close_positions"]:
            self._event("EXIT_POSITIONS_FOUND",
                        f"{len(positions)} positions survived the close - "
                        f"closing before re-entry", status="RETRY")
            for pos in positions:
                ok, msg = self.broker.close_position(pos.ticket,
                                                     comment="cycle end")
                if not ok:
                    self._event("ERROR", f"close {pos.ticket} failed: {msg}",
                                position_ticket=pos.ticket, status="ERROR")
            for trade in self.broker.poll_closed():
                self._on_closed(trade, snap)

        wait = self._reentry_wait()
        if wait > 0:
            self.block_reason = f"cycle re-entry cooldown ({_seconds(wait)} left)"
            return "pending"

        # MAX_ACTIVE_CYCLES = 1: the new ladder is only built from a flat book.
        live_positions = self.broker.positions()
        live_orders = self.broker.orders()
        if live_positions or live_orders:
            self.block_reason = (
                f"waiting to go flat before the next cycle: "
                f"{len(live_positions)} positions / {len(live_orders)} orders")
            self._event("CYCLE_REENTRY_BLOCKED", self.block_reason, status="RETRY")
            return "pending"

        self._event("CYCLE_COOLDOWN_COMPLETE",
                    "cooldown elapsed and the account is flat - building the "
                    "next ladder at the current price", status="OK")
        if self.block_reason.startswith(("cycle re-entry cooldown",
                                         "waiting to go flat")):
            self.block_reason = ""
        # anchored on the CURRENT price, never the old grid
        if not self._start_cycle(reason="continuous re-entry"):
            return "pending"
        return "restarted"

    # ---------------------------------------------------------- reconciliation
    def _reconcile(self, snap, tick, positions, orders):
        """Place what is missing, cancel what should not be there. Idempotent."""
        desired = self.desired_levels(tick, snap, orders)
        desired_by_key = {(d.side, d.index): d for d in desired}

        # --- cancel stale / duplicate / out-of-window orders ------------------
        seen = set()
        max_age = float(snap["order_max_age_seconds"])
        for order in orders:
            parsed = parse_comment(order.comment)
            key = None
            if parsed:
                cycle_id, side, index = parsed
                key = (BUY_STOP if side == BUY else SELL_STOP, index)
                if cycle_id != self.cycle.cycle_id:
                    self._cancel(order, "belongs to an older cycle")
                    continue
            if key is None or key not in desired_by_key:
                self._cancel(order, "not part of the current ladder")
                continue
            if key in seen:
                self._cancel(order, "duplicate level")
                continue
            if max_age > 0 and order.time_setup and \
                    (self.clock() - order.time_setup) > max_age:
                self._cancel(order, f"stale (> {int(max_age)}s)")
                continue
            # A live order must still match what the settings ask for: a changed
            # spacing, TP or SL makes it a stale level, so it is replaced rather
            # than left behind at the old price.
            want = desired_by_key[key]
            tol = self.spec.point / 2
            if abs(order.price - want.price) > tol:
                self._cancel(order, "level price no longer matches the ladder")
                continue
            if abs((order.tp or 0.0) - (want.tp or 0.0)) > tol or \
                    abs((order.sl or 0.0) - (want.sl or 0.0)) > tol:
                self._cancel(order, "TP/SL changed in settings")
                continue
            seen.add(key)

        # --- place missing levels -------------------------------------------
        # The depth cap limits how far a cycle may EXTEND. It deliberately does
        # not cancel the live ladder: pulling every pending order while
        # positions are still open removes the strategy's eyes without reducing
        # exposure, and leaves the cycle with nothing left to react to.
        max_depth = int(snap["max_ladder_depth"])
        used = self.sequence.ladder_depth_used if self.sequence else 0
        depth_capped = max_depth > 0 and used >= max_depth
        if depth_capped and not self._depth_capped_logged:
            self._depth_capped_logged = True
            self._event("LADDER_DEPTH_CAP",
                        f"depth {used}/{max_depth} reached - no further levels "
                        f"this cycle; the live ladder and the exit logic carry on",
                        cycle_id=self.cycle.cycle_id, status="CAPPED")
        room_orders = 0 if depth_capped else \
            int(snap["max_pending_orders"]) - len(seen)
        failures = 0
        first_deploy = not orders and room_orders > 0
        if first_deploy:
            self._event("LADDER_DEPLOY_START",
                        f"{self.broker.symbol} bid={tick.bid} ask={tick.ask} "
                        f"spacing={snap['ladder_spacing']} "
                        f"levels={snap['ladder_depth']}/side "
                        f"lot={snap['lot_size']} cycle=#{self.cycle.cycle_id} "
                        f"min_stop={self.spec.min_stop_distance:g}",
                        cycle_id=self.cycle.cycle_id, spread=tick.spread)
        lot = self.spec.normalize_volume(min(float(snap["lot_size"]),
                                             float(snap["max_lot_size"])))
        placed = 0
        for key, level in sorted(desired_by_key.items(),
                                 key=lambda kv: abs(kv[1].price - tick.mid)):
            if room_orders <= 0:
                break
            if key in seen or not level.placeable:
                continue
            level_key = (BUY if level.side == BUY_STOP else SELL, level.index)
            if level_key in self._levels_open:
                continue                     # its position is still running
            if level_key in self._levels_done and \
                    not (snap["roll_mode"] == "extend" and snap["rearm_levels"]):
                continue                     # consumed and not re-armed
            ok, ticket, msg = self.broker.place_stop_order(
                side=level.side, price=level.price, volume=lot,
                tp=level.tp, sl=level.sl, comment=level.comment,
            )
            if ok:
                placed += 1
                room_orders -= 1
                seen.add(key)
                self._event("ORDER_PLACED",
                            f"{level.side} {lot} @ {level.price} "
                            f"tp {level.tp or '-'} (level {level.index:+d})",
                            cycle_id=self.cycle.cycle_id,
                            direction=level.side, level=level.index,
                            entry_price=level.price, tp=level.tp, sl=level.sl,
                            lot_size=lot, order_ticket=ticket, spread=tick.spread)
            else:
                failures += 1
                # A refused order is never swallowed: the first one carries the
                # broker's full diagnosis, the rest are throttled.
                if self.clock() - self._last_place_error > 30:
                    self._last_place_error = self.clock()
                    self._event("ORDER_REJECTED",
                                f"place {level.side} {lot} @ {level.price} "
                                f"(level {level.index:+d}) refused: {msg}",
                                cycle_id=self.cycle.cycle_id,
                                direction=level.side, level=level.index,
                                entry_price=level.price, tp=level.tp,
                                sl=level.sl, lot_size=lot, spread=tick.spread,
                                status="ERROR")
                break                        # stop hammering a rejecting broker

        if first_deploy:
            # what was actually accepted, never what was intended
            intended = len([d for d in desired_by_key.values() if d.placeable])
            self._event("LADDER_CREATED",
                        f"Cycle #{self.cycle.cycle_id}: {placed} of "
                        f"{intended} levels live around {tick.mid:.2f} "
                        f"(spacing {snap['ladder_spacing']}"
                        + (f", {failures} rejected" if failures else "") + ")",
                        cycle_id=self.cycle.cycle_id,
                        status="PARTIAL" if placed < intended else "OK")
            # the deployment is verified against what the broker accepted, and
            # only then is the cycle announced as running - once
            if placed > 0 and not self._cycle_announced:
                self._cycle_announced = True
                self._event("CYCLE_ACTIVE",
                            f"Cycle #{self.cycle.cycle_id} ACTIVE: ladder "
                            f"deployed, {placed} levels live",
                            cycle_id=self.cycle.cycle_id, levels_live=placed,
                            entry_price=self.cycle.anchor, status="OK")
        return len(seen)

    def _cancel(self, order, reason):
        ok, msg = self.broker.cancel_order(order.ticket)
        if ok:
            self._event("ORDER_CANCELLED", f"{order.side} @ {order.price}: {reason}",
                        cycle_id=self.cycle.cycle_id, direction=order.side,
                        entry_price=order.price, order_ticket=order.ticket)
        else:
            self._event("ERROR", f"cancel {order.ticket} failed: {msg}", status="ERROR")
        return ok

    def _cancel_all(self, orders, reason):
        for order in orders:
            self._cancel(order, reason)

    # ================================================================ reporting
    def snapshot(self):
        """Everything the Telegram STATUS screen needs."""
        snap = self.settings.snapshot()
        positions = []
        orders = []
        tick = self.last_tick
        try:
            positions = self.broker.positions()
            orders = self.broker.orders()
            tick = self.broker.tick()
        except Exception:
            pass
        # MT5 is the source of truth for what is live right now; the sequence is
        # the history of what this cycle has already done. They are reported as
        # separate things on purpose - conflating them is how a bot ends up
        # claiming an active ladder it does not have.
        basket = self.basket(positions, orders)
        floating = basket["floating_pnl"]
        pending_buys = basket["pending_buys"]
        pending_sells = basket["pending_sells"]
        open_buys = basket["open_buys"]
        open_sells = basket["open_sells"]
        data = {
            "state": self.state,
            "mode": self.broker.name,
            "symbol": self.broker.symbol,
            "timeframe": snap.get("timeframe", "M5"),
            "bid": tick.bid if tick else None,
            "ask": tick.ask if tick else None,
            "spread": tick.spread if tick else None,
            "spacing": snap["ladder_spacing"],
            "depth": snap["ladder_depth"],
            "tp_distance": self.tp_distance(snap) if self.spec else snap["tp_distance"],
            "tp_mode": snap["tp_mode"],
            "lot": snap["lot_size"],
            # --- CURRENT MT5 STATE ---
            "positions": len(positions),
            "orders": len(orders),
            "current_pending_buys": pending_buys,
            "current_pending_sells": pending_sells,
            "current_open_buys": open_buys,
            "current_open_sells": open_sells,
            "ladder_live": bool(orders),
            # --- the basket, as one managed unit ---
            "basket": basket,
            "basket_open_positions": basket["open_positions"],
            "basket_pending_orders": basket["pending_orders"],
            "basket_floating_pnl": basket["floating_pnl"],
            "basket_realized_pnl": basket["realized_pnl"],
            "basket_net_pnl": basket["net_pnl"],
            "basket_drawdown_now": basket["drawdown"],
            # --- one active cycle at a time ---
            "cycle_active": self.cycle_active,
            "reentry_wait_seconds": round(self._reentry_wait(), 1),
            "in_reentry_cooldown": (not self.cycle_active) and self._reentry_wait() > 0,
            # --- P/L, kept distinct ---
            "floating_pnl": floating,
            "cycle_id": self.cycle.cycle_id,
            "tp_count": self.cycle.tp_count,
            "cycle_triggers": self.cycle.trades,
            "cycle_profit": round(self.cycle.realized + floating, 2),
            "cycle_realized": self.cycle.realized,
            "floating": floating,
            "daily_profit": self.daily_profit,
            "total_tp": self.total_tp,
            "total_trades": self.total_trades,
            "block_reason": self.block_reason,
            "spread_blocked": self.spread_blocked,
            "losing_streak": self.consecutive_losing_cycles,
            "last_update": self.last_update,
            "anchor": self.cycle.anchor,
            "pip_size": self.spec.pip_size if self.spec else None,
        }
        # everything the exit engine is currently reading
        if self.sequence is not None:
            data.update(self.sequence.snapshot())
            # --- HISTORICAL, this cycle: triggers that already happened ---
            data["historical_buy_triggers"] = self.sequence.buy_triggers
            data["historical_sell_triggers"] = self.sequence.sell_triggers
            data["realized_pnl"] = self.sequence.realized_pnl
            data["floating_pnl"] = floating
            data["cycle_total_pnl"] = self.sequence.realized_pnl + floating
            data["cycle_profit"] = data["cycle_total_pnl"]
            data["cycle_age_seconds"] = self.clock() - self.cycle.started_at
        # `engine_state` is what the bot is DOING (the cycle state machine);
        # `market_state` is what the exit engine is SEEING. They are different
        # questions and are reported under different names.
        data["engine_state"] = self.state
        if self.assessment is not None:
            data.update(self.assessment.as_dict())
            data["market_state"] = self.assessment.state
        else:
            data.update({"exit_score": 0.0, "decision": CONTINUE,
                         "momentum_score": 0.0, "reversal_score": 0.0,
                         "exhaustion_score": 0.0, "continuation_score": 0.0,
                         "reason": "", "phase": self.state})
            data["market_state"] = self.state
        return data
