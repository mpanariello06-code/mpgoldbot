"""
Execution adapters.

The ladder engine never talks to MetaTrader5 directly. It talks to a Broker,
which is either:

    Mt5Broker    - real pending orders, positions and deal history
    PaperBroker  - DRY_RUN / PAPER mode: identical interface, simulated fills
                   driven by the same live ticks, no orders ever sent

Both report the same dataclasses, so every code path above this module -
reconciliation, cycles, risk, Telegram, CSV - is identical in paper and live.
"""

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import MetaTrader5 as mt5

from price_utils import SymbolSpec

# The MetaTrader5 package multiplexes one terminal pipe; every call is serialised.
MT5_LOCK = threading.RLock()

BUY = "BUY"
SELL = "SELL"
BUY_STOP = "BUY_STOP"
SELL_STOP = "SELL_STOP"


@dataclass
class Tick:
    bid: float
    ask: float
    time: float = 0.0

    @property
    def spread(self):
        return self.ask - self.bid

    @property
    def mid(self):
        return (self.ask + self.bid) / 2.0


@dataclass
class PendingOrder:
    ticket: int
    symbol: str
    side: str            # BUY_STOP / SELL_STOP
    price: float
    volume: float
    tp: float = 0.0
    sl: float = 0.0
    comment: str = ""
    magic: int = 0
    time_setup: float = 0.0


@dataclass
class OpenPosition:
    ticket: int
    symbol: str
    side: str            # BUY / SELL
    volume: float
    price_open: float
    tp: float = 0.0
    sl: float = 0.0
    profit: float = 0.0
    comment: str = ""
    magic: int = 0
    time_open: float = 0.0


@dataclass
class ClosedTrade:
    ticket: int
    symbol: str
    side: str
    volume: float
    price_open: float
    price_close: float
    profit: float
    comment: str = ""
    reason: str = "CLOSED"      # TP / SL / CLOSED
    time_close: float = 0.0


@dataclass
class AccountInfo:
    login: int = 0
    server: str = ""
    currency: str = "USD"
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 0.0
    profit: float = 0.0


class BrokerError(RuntimeError):
    pass


# ===========================================================================
# LIVE MT5
# ===========================================================================
class Mt5Broker:
    """Real MetaTrader 5 execution."""

    is_paper = False
    name = "LIVE"

    def __init__(self, symbol, magic, deviation_points=20, pip_points_override=0):
        self.symbol = symbol
        self.magic = int(magic)
        self.deviation = int(deviation_points)
        self.pip_points_override = pip_points_override
        self._filling = None
        self._seen_positions = {}
        self._reported_deals = set()
        self._history_from = time.time() - 3600

    # ------------------------------------------------------------ market data
    def symbol_spec(self):
        with MT5_LOCK:
            info = mt5.symbol_info(self.symbol)
        if info is None:
            raise BrokerError(f"symbol_info({self.symbol}) unavailable")
        return SymbolSpec.from_mt5(info, self.pip_points_override)

    def tick(self):
        with MT5_LOCK:
            t = mt5.symbol_info_tick(self.symbol)
        if t is None:
            raise BrokerError(f"no tick for {self.symbol}")
        return Tick(bid=float(t.bid), ask=float(t.ask), time=float(t.time))

    def account(self):
        with MT5_LOCK:
            a = mt5.account_info()
        if a is None:
            raise BrokerError("account_info unavailable")
        return AccountInfo(
            login=a.login, server=getattr(a, "server", ""),
            currency=getattr(a, "currency", "USD"), balance=a.balance,
            equity=a.equity, margin=a.margin, margin_free=a.margin_free,
            margin_level=a.margin_level, profit=a.profit,
        )

    # ------------------------------------------------------------------ state
    def positions(self):
        with MT5_LOCK:
            raw = mt5.positions_get(symbol=self.symbol) or ()
        out = []
        for p in raw:
            if p.magic != self.magic:
                continue
            out.append(OpenPosition(
                ticket=p.ticket, symbol=p.symbol,
                side=BUY if p.type == mt5.POSITION_TYPE_BUY else SELL,
                volume=p.volume, price_open=p.price_open, tp=p.tp, sl=p.sl,
                profit=p.profit, comment=p.comment, magic=p.magic,
                time_open=float(p.time),
            ))
        return out

    def orders(self):
        with MT5_LOCK:
            raw = mt5.orders_get(symbol=self.symbol) or ()
        types = {mt5.ORDER_TYPE_BUY_STOP: BUY_STOP,
                 mt5.ORDER_TYPE_SELL_STOP: SELL_STOP}
        out = []
        for o in raw:
            if o.magic != self.magic or o.type not in types:
                continue
            out.append(PendingOrder(
                ticket=o.ticket, symbol=o.symbol, side=types[o.type],
                price=o.price_open, volume=o.volume_current, tp=o.tp, sl=o.sl,
                comment=o.comment, magic=o.magic, time_setup=float(o.time_setup),
            ))
        return out

    # -------------------------------------------------------------- execution
    def _filling_mode(self):
        if self._filling is not None:
            return self._filling
        with MT5_LOCK:
            info = mt5.symbol_info(self.symbol)
        mode = mt5.ORDER_FILLING_RETURN
        if info is not None:
            allowed = getattr(info, "filling_mode", 0)
            if allowed & 1:
                mode = mt5.ORDER_FILLING_FOK
            elif allowed & 2:
                mode = mt5.ORDER_FILLING_IOC
        self._filling = mode
        return mode

    def place_stop_order(self, side, price, volume, tp=0.0, sl=0.0, comment=""):
        spec = self.symbol_spec()
        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol,
            "volume": spec.normalize_volume(volume),
            "type": mt5.ORDER_TYPE_BUY_STOP if side == BUY_STOP else mt5.ORDER_TYPE_SELL_STOP,
            "price": spec.normalize_price(price),
            "sl": spec.normalize_price(sl) if sl else 0.0,
            "tp": spec.normalize_price(tp) if tp else 0.0,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(),
        }
        with MT5_LOCK:
            result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True, int(result.order), "OK"
        msg = f"retcode={getattr(result, 'retcode', '?')} {getattr(result, 'comment', 'no result')}"
        return False, None, msg

    def cancel_order(self, ticket):
        with MT5_LOCK:
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE,
                                     "order": int(ticket)})
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True, "OK"
        return False, f"retcode={getattr(result, 'retcode', '?')} {getattr(result, 'comment', '')}"

    def close_position(self, ticket, comment="cycle"):
        with MT5_LOCK:
            found = mt5.positions_get(ticket=int(ticket))
        if not found:
            return False, "position not found"
        pos = found[0]
        tick = self.tick()
        spec = self.symbol_spec()
        is_buy = pos.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": int(ticket),
            "price": spec.normalize_price(tick.bid if is_buy else tick.ask),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(),
        }
        with MT5_LOCK:
            result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True, "OK"
        return False, f"retcode={getattr(result, 'retcode', '?')} {getattr(result, 'comment', '')}"

    # --------------------------------------------------------- closed trades
    def poll_closed(self):
        """
        Newly closed positions since the last call, read from MT5 deal history.

        Deal ids are remembered, so a trade is never reported (or counted) twice
        - including across reconnects.
        """
        import datetime as _dt
        now = time.time()
        frm = _dt.datetime.fromtimestamp(min(self._history_from, now) - 300)
        to = _dt.datetime.fromtimestamp(now + 60)
        with MT5_LOCK:
            deals = mt5.history_deals_get(frm, to) or ()

        opens = {d.position_id: d for d in deals
                 if getattr(d, "entry", None) == mt5.DEAL_ENTRY_IN}
        closed = []
        for d in deals:
            if d.symbol != self.symbol or d.magic != self.magic:
                continue
            if getattr(d, "entry", None) not in (mt5.DEAL_ENTRY_OUT,
                                                 mt5.DEAL_ENTRY_OUT_BY):
                continue
            if d.ticket in self._reported_deals:
                continue
            self._reported_deals.add(d.ticket)
            opener = opens.get(d.position_id)
            side = SELL if d.type == mt5.ORDER_TYPE_BUY else BUY
            if opener is not None:
                side = BUY if opener.type == mt5.ORDER_TYPE_BUY else SELL
            reason = "CLOSED"
            text = (getattr(d, "comment", "") or "").lower()
            if "tp" in text:
                reason = "TP"
            elif "sl" in text:
                reason = "SL"
            closed.append(ClosedTrade(
                ticket=d.position_id, symbol=d.symbol, side=side, volume=d.volume,
                price_open=opener.price if opener is not None else 0.0,
                price_close=d.price,
                profit=d.profit + d.commission + d.swap,
                comment=(opener.comment if opener is not None else d.comment) or "",
                reason=reason, time_close=float(d.time),
            ))
        self._history_from = now
        return closed


# ===========================================================================
# PAPER / DRY RUN
# ===========================================================================
class PaperBroker:
    """
    Simulated execution against the same live ticks.

    Pending orders trigger when price crosses them, positions close on TP/SL,
    and state is persisted so a restart recovers exactly like the live broker.
    No order ever reaches the broker.
    """

    is_paper = True
    name = "PAPER"

    def __init__(self, symbol, magic, spec_provider, tick_provider,
                 state_path=None, start_balance=10000.0, max_slippage_points=20,
                 commission_per_lot=0.0, clock=time.time):
        self.symbol = symbol
        self.magic = int(magic)
        # A stop order becomes a market order when it triggers. Paper fills
        # assume the broker fills at the level, or up to this much worse - the
        # same allowance MAX_SLIPPAGE gives the live path.
        self.max_slippage_points = int(max_slippage_points)
        # Round-turn commission in account currency per lot, charged on close.
        self.commission_per_lot = float(commission_per_lot)
        self.clock = clock          # replay drives this with simulated time
        self._spec_provider = spec_provider
        self._tick_provider = tick_provider
        self._path = Path(state_path) if state_path else None
        self._lock = threading.RLock()

        self._orders = {}
        self._positions = {}
        self._closed = []
        self._next_ticket = 900000001
        self.balance = float(start_balance)
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self):
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception:
            return
        with self._lock:
            self.balance = float(data.get("balance", self.balance))
            self._next_ticket = int(data.get("next_ticket", self._next_ticket))
            for o in data.get("orders", []):
                self._orders[int(o["ticket"])] = PendingOrder(**o)
            for p in data.get("positions", []):
                self._positions[int(p["ticket"])] = OpenPosition(**p)

    def _save(self):
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "balance": self.balance,
                "next_ticket": self._next_ticket,
                "orders": [asdict(o) for o in self._orders.values()],
                "positions": [asdict(p) for p in self._positions.values()],
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self._path)
        except Exception as exc:
            print(f"[paper] state save failed: {exc}")

    # ------------------------------------------------------------ market data
    def symbol_spec(self):
        return self._spec_provider()

    def tick(self):
        return self._tick_provider()

    def account(self):
        floating = 0.0
        spec = self.symbol_spec()
        try:
            t = self.tick()
            for p in self._positions.values():
                price = t.bid if p.side == BUY else t.ask
                floating += spec.profit(p.side, p.price_open, price, p.volume)
        except Exception:
            pass
        return AccountInfo(login=0, server="PAPER", currency="USD",
                           balance=round(self.balance, 2),
                           equity=round(self.balance + floating, 2),
                           margin=0.0, margin_free=round(self.balance + floating, 2),
                           margin_level=0.0, profit=round(floating, 2))

    # ------------------------------------------------------------------ state
    def positions(self):
        spec = self.symbol_spec()
        try:
            t = self.tick()
        except Exception:
            t = None
        with self._lock:
            out = []
            for p in self._positions.values():
                if t is not None:
                    price = t.bid if p.side == BUY else t.ask
                    p.profit = round(spec.profit(p.side, p.price_open, price, p.volume), 2)
                out.append(OpenPosition(**asdict(p)))
        return out

    def orders(self):
        with self._lock:
            return [PendingOrder(**asdict(o)) for o in self._orders.values()]

    # -------------------------------------------------------------- execution
    def place_stop_order(self, side, price, volume, tp=0.0, sl=0.0, comment=""):
        spec = self.symbol_spec()
        with self._lock:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._orders[ticket] = PendingOrder(
                ticket=ticket, symbol=self.symbol, side=side,
                price=spec.normalize_price(price),
                volume=spec.normalize_volume(volume),
                tp=spec.normalize_price(tp) if tp else 0.0,
                sl=spec.normalize_price(sl) if sl else 0.0,
                comment=comment[:31], magic=self.magic, time_setup=self.clock(),
            )
            self._save()
        return True, ticket, "OK (paper)"

    def cancel_order(self, ticket):
        with self._lock:
            if int(ticket) not in self._orders:
                return False, "order not found"
            del self._orders[int(ticket)]
            self._save()
        return True, "OK (paper)"

    def close_position(self, ticket, comment="cycle"):
        with self._lock:
            pos = self._positions.get(int(ticket))
        if pos is None:
            return False, "position not found"
        t = self.tick()
        self._close(pos, t.bid if pos.side == BUY else t.ask, "CLOSED")
        return True, "OK (paper)"

    # ------------------------------------------------------------- simulation
    def _close(self, pos, price, reason):
        spec = self.symbol_spec()
        profit = spec.profit(pos.side, pos.price_open, price, pos.volume)
        profit -= self.commission_per_lot * pos.volume
        with self._lock:
            self._positions.pop(pos.ticket, None)
            self.balance += profit
            self._closed.append(ClosedTrade(
                ticket=pos.ticket, symbol=pos.symbol, side=pos.side,
                volume=pos.volume, price_open=pos.price_open,
                price_close=spec.normalize_price(price), profit=round(profit, 2),
                comment=pos.comment, reason=reason, time_close=self.clock(),
            ))
            self._save()

    def poll_closed(self):
        """Run one simulation step against the latest tick, then report closures."""
        try:
            t = self.tick()
        except Exception:
            return []
        spec = self.symbol_spec()

        slip = spec.points_to_price(self.max_slippage_points)
        with self._lock:
            triggered = []
            for order in list(self._orders.values()):
                if order.side == BUY_STOP and t.ask >= order.price:
                    triggered.append((order, min(t.ask, order.price + slip), BUY))
                elif order.side == SELL_STOP and t.bid <= order.price:
                    triggered.append((order, max(t.bid, order.price - slip), SELL))
            for order, fill, side in triggered:
                self._orders.pop(order.ticket, None)
                self._positions[order.ticket] = OpenPosition(
                    ticket=order.ticket, symbol=order.symbol, side=side,
                    volume=order.volume, price_open=spec.normalize_price(fill),
                    tp=order.tp, sl=order.sl, profit=0.0, comment=order.comment,
                    magic=self.magic, time_open=self.clock(),
                )
            positions = list(self._positions.values())

        for pos in positions:
            if pos.side == BUY:
                if pos.tp and t.bid >= pos.tp:
                    self._close(pos, pos.tp, "TP")
                elif pos.sl and t.bid <= pos.sl:
                    self._close(pos, pos.sl, "SL")
            else:
                if pos.tp and t.ask <= pos.tp:
                    self._close(pos, pos.tp, "TP")
                elif pos.sl and t.ask >= pos.sl:
                    self._close(pos, pos.sl, "SL")

        with self._lock:
            out, self._closed = self._closed, []
        if triggered:
            self._save()
        return out
