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

COMMENT_RE = re.compile(r"^RL(\d+)([BS])(-?\d+)")


# ---------------------------------------------------------------- states
class State:
    IDLE = "IDLE"
    INITIALIZING = "INITIALIZING"
    LADDER_ACTIVE = "LADDER_ACTIVE"
    POSITION_ACTIVE = "POSITION_ACTIVE"
    ROLLING = "ROLLING"
    CYCLE_COMPLETE = "CYCLE_COMPLETE"
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

    def __init__(self, broker, settings, hooks=None, state_path=None):
        self.broker = broker
        self.settings = settings          # RuntimeSettings (thread-safe)
        self.hooks = hooks or {}
        self._path = Path(state_path) if state_path else None
        self._lock = threading.RLock()

        self.state = State.IDLE
        self.paused = False
        self.cycle = Cycle()
        self.spec = None
        self.last_tick = None
        self.last_update = None
        self.block_reason = ""
        self.spread_blocked = False

        self.daily_profit = 0.0
        self.daily_date = self._today()
        self.consecutive_losing_cycles = 0
        self.cooldown_until = 0.0
        self.total_tp = 0
        self.total_trades = 0

        self._known_positions = {}        # ticket -> OpenPosition
        self._known_orders = {}           # ticket -> PendingOrder
        self._levels_open = set()         # (side, index) currently holding a position
        self._levels_done = set()         # (side, index) consumed this cycle
        self._last_place_error = 0.0

    # =================================================================== utils
    @staticmethod
    def _today():
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
                "consecutive_losing_cycles": self.consecutive_losing_cycles,
                "cooldown_until": self.cooldown_until,
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
        self.consecutive_losing_cycles = int(data.get("consecutive_losing_cycles", 0))
        self.cooldown_until = float(data.get("cooldown_until", 0.0))
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
            if adopted != self.cycle.cycle_id:
                self.cycle.cycle_id = adopted
            if not self.cycle.anchor:
                self.cycle.anchor = self._anchor_from(orders, positions)
            self._event("LADDER_RECOVERED",
                        f"Adopted cycle #{adopted} from {len(orders)} orders / "
                        f"{len(positions)} positions")
        elif not self.cycle.anchor:
            self._start_cycle(new_id=self.cycle.cycle_id, reason="startup")

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

    def _start_cycle(self, new_id=None, reason=""):
        tick = self.last_tick or self.broker.tick()
        anchor = self.spec.normalize_price(tick.mid) if self.spec else tick.mid
        cid = new_id if new_id is not None else self.cycle.cycle_id + 1
        self.cycle = Cycle(cycle_id=cid, anchor=anchor)
        self._levels_open.clear()
        self._levels_done.clear()
        self._event("CYCLE_STARTED",
                    f"Cycle #{cid} anchored at {anchor} ({reason})",
                    cycle_id=cid, entry_price=anchor)
        self._emit("cycle_started", self.cycle, anchor)
        self.save()

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
    def desired_levels(self, tick, snap):
        """
        The levels that should be live right now.

        BUY STOPs sit above the market, SELL STOPs below, both snapped to the
        cycle's grid so their prices are stable. In `extend` mode the window
        rolls with price; in `static` mode it stays where the cycle started
        (the behaviour a fixed grid shows).
        """
        spec = self.spec
        spacing = float(snap["ladder_spacing"])
        depth = int(snap["ladder_depth"])
        anchor = self.cycle.anchor
        if spacing <= 0 or depth <= 0 or not anchor:
            return []

        # never closer than the broker allows, and never closer than configured
        offset = max(float(snap["first_level_offset"]),
                     spec.min_stop_distance + spec.point)

        buy_base = int(math.ceil((tick.ask + offset - anchor) / spacing - 1e-9))
        sell_base = int(math.floor((tick.bid - offset - anchor) / spacing + 1e-9))

        if snap["roll_mode"] == "static":
            if self.cycle.base_buy_index is None:
                self.cycle.base_buy_index = buy_base
                self.cycle.base_sell_index = sell_base
            buy_base = self.cycle.base_buy_index
            sell_base = self.cycle.base_sell_index

        allow_buy, allow_sell = DirectionFilter(snap["direction_filter"]).decide()
        tp_distance = self.tp_distance(snap)
        sl_distance = float(snap["stop_loss_distance"])

        levels = []
        if allow_buy:
            for i in range(depth):
                idx = buy_base + i
                price = spec.normalize_price(anchor + idx * spacing)
                if price <= tick.ask + spec.min_stop_distance:
                    continue
                levels.append(DesiredLevel(
                    side=BUY_STOP, index=idx, price=price,
                    tp=spec.normalize_price(price + tp_distance) if tp_distance else 0.0,
                    sl=spec.normalize_price(price - sl_distance) if sl_distance else 0.0,
                    comment=level_comment(self.cycle.cycle_id, BUY, idx),
                ))
        if allow_sell:
            for i in range(depth):
                idx = sell_base - i
                price = spec.normalize_price(anchor + idx * spacing)
                if price >= tick.bid - spec.min_stop_distance:
                    continue
                levels.append(DesiredLevel(
                    side=SELL_STOP, index=idx, price=price,
                    tp=spec.normalize_price(price - tp_distance) if tp_distance else 0.0,
                    sl=spec.normalize_price(price + sl_distance) if sl_distance else 0.0,
                    comment=level_comment(self.cycle.cycle_id, SELL, idx),
                ))
        return levels

    def tp_distance(self, snap):
        """TP in price units: either an explicit distance or N pips."""
        mode = snap["tp_mode"]
        if mode == "distance":
            return float(snap["tp_distance"])
        pips = {"1_pip": 1, "2_pips": 2, "3_pips": 3, "4_pips": 4, "5_pips": 5}
        return self.spec.pips_to_price(pips.get(mode, 1))

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

        max_daily = float(snap["max_daily_loss"])
        if max_daily > 0 and self.daily_profit <= -abs(max_daily):
            return False, f"daily loss limit hit ({self.daily_profit:.2f})"

        max_streak = int(snap["max_consecutive_losing_cycles"])
        if max_streak > 0 and self.consecutive_losing_cycles >= max_streak:
            return False, f"{self.consecutive_losing_cycles} losing cycles in a row"

        if time.time() < self.cooldown_until:
            left = int(self.cooldown_until - time.time())
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

        # 3. cycle completion / loss
        if self._check_cycle(snap, tick, positions, orders):
            return True

        # 4. paused: no NEW entries, existing positions still managed
        if self.paused:
            if orders:
                self._cancel_all(orders, "trading paused")
            self.state = State.POSITION_ACTIVE if positions else State.IDLE
            self.last_update = datetime.now()
            return True

        # 5. risk + spread gates
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
        live_orders = self._reconcile(snap, tick, positions, orders)

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

    # --------------------------------------------------------- closed trades
    def _on_closed(self, trade, snap):
        parsed = parse_comment(trade.comment)
        index = parsed[2] if parsed else 0
        cycle_id = parsed[0] if parsed else self.cycle.cycle_id
        side = parsed[1] if parsed else trade.side

        self._levels_open.discard((side, index))
        self.daily_profit += trade.profit
        if cycle_id == self.cycle.cycle_id:
            self.cycle.realized += trade.profit

        # Only money decides: a TP close that still lost (a gapped fill, say)
        # must not count toward the profit cycle.
        is_win = trade.profit > 0
        if is_win and cycle_id == self.cycle.cycle_id:
            self.cycle.tp_count += 1
            self.total_tp += 1

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
        """Complete or abandon the cycle. Returns True when the cycle rolled."""
        target = int(snap["profit_cycle_target"])
        basket = float(snap["cycle_take_profit_money"])
        floating = sum(p.profit for p in positions)

        done = target > 0 and self.cycle.tp_count >= target
        basket_hit = basket > 0 and (self.cycle.realized + floating) >= basket

        max_cycle_loss = float(snap["max_cycle_loss"])
        lost = max_cycle_loss > 0 and \
            (self.cycle.realized + floating) <= -abs(max_cycle_loss)

        if not (done or basket_hit or lost):
            return False

        self.state = State.CYCLE_COMPLETE
        reason = "profit cycle target" if done else (
            "basket profit target" if basket_hit else "cycle loss limit")

        self._cancel_all(orders, f"cycle end: {reason}")
        if lost or snap["cycle_close_positions"]:
            for pos in positions:
                ok, msg = self.broker.close_position(pos.ticket, comment="cycle end")
                if not ok:
                    self._event("ERROR", f"close {pos.ticket} failed: {msg}",
                                status="ERROR")
            for trade in self.broker.poll_closed():
                self._on_closed(trade, snap)

        total = self.cycle.realized
        if lost:
            self.consecutive_losing_cycles += 1
            cooldown = float(snap["cooldown_after_loss_minutes"]) * 60.0
            if cooldown > 0:
                self.cooldown_until = time.time() + cooldown
            self._event("CYCLE_LOSS",
                        f"Cycle #{self.cycle.cycle_id} closed at {total:+.2f} "
                        f"({reason})", cycle_id=self.cycle.cycle_id,
                        profit=total, cycle_profit=total,
                        daily_profit=self.daily_profit, status="LOSS")
        else:
            self.consecutive_losing_cycles = 0
            self._event("CYCLE_COMPLETED",
                        f"Cycle #{self.cycle.cycle_id}: {self.cycle.tp_count} TPs, "
                        f"P/L {total:+.2f} ({reason})",
                        cycle_id=self.cycle.cycle_id, profit=total,
                        cycle_profit=total, daily_profit=self.daily_profit)
        self._emit("cycle_complete", self.cycle, total, reason, lost)

        self._start_cycle(reason=reason)
        return True

    # ---------------------------------------------------------- reconciliation
    def _reconcile(self, snap, tick, positions, orders):
        """Place what is missing, cancel what should not be there. Idempotent."""
        desired = self.desired_levels(tick, snap)
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
                    (time.time() - order.time_setup) > max_age:
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
        room_orders = int(snap["max_pending_orders"]) - len(seen)
        lot = self.spec.normalize_volume(min(float(snap["lot_size"]),
                                             float(snap["max_lot_size"])))
        placed = 0
        for key, level in sorted(desired_by_key.items(),
                                 key=lambda kv: abs(kv[1].price - tick.mid)):
            if room_orders <= 0:
                break
            if key in seen:
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
                if time.time() - self._last_place_error > 30:
                    self._last_place_error = time.time()
                    self._event("ERROR",
                                f"place {level.side} @ {level.price} failed: {msg}",
                                status="ERROR")
                break                        # stop hammering a rejecting broker

        if placed and not orders:
            self._event("LADDER_CREATED",
                        f"Cycle #{self.cycle.cycle_id}: {placed} levels around "
                        f"{tick.mid:.2f} (spacing {snap['ladder_spacing']})",
                        cycle_id=self.cycle.cycle_id)
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
        floating = sum(p.profit for p in positions)
        return {
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
            "positions": len(positions),
            "orders": len(orders),
            "cycle_id": self.cycle.cycle_id,
            "tp_count": self.cycle.tp_count,
            "cycle_target": snap["profit_cycle_target"],
            "cycle_profit": self.cycle.realized + floating,
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
