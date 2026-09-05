"""In-memory MetaTrader5 stub: positions, pending orders and deal history."""
import time
from types import SimpleNamespace

import numpy as np

TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15 = 1, 5, 15
TIMEFRAME_M30, TIMEFRAME_H1 = 30, 60
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 4, 5
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_REMOVE = 1, 5, 8
TRADE_ACTION_SLTP = 6
ORDER_TIME_GTC = 0
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_INVALID_PRICE = 10015
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 0, 1, 2

STATE = {
    "initialized": False,
    "positions": [],
    "orders": [],
    "deals": [],
    "sent": [],
    "next_ticket": 500001,
    "next_deal": 700001,
    "symbol_visible": True,
    "fail_symbol_info": False,
    "reject_orders": False,
    "bid": 4010.06,
    "ask": 4010.14,
    "digits": 2,
    "point": 0.01,
    "stops_level": 0,
    "balance": 450.0,
    "bar_offset": 0,
}


def reset(**kw):
    STATE.update({"positions": [], "orders": [], "deals": [], "sent": [],
                  "next_ticket": 500001, "next_deal": 700001,
                  "reject_orders": False, "fail_symbol_info": False})
    STATE.update(kw)


def set_price(bid, ask=None):
    STATE["bid"] = bid
    STATE["ask"] = ask if ask is not None else round(bid + 0.08, 2)


def initialize(path=None):
    STATE["initialized"] = True
    return True


def shutdown():
    STATE["initialized"] = False


def last_error():
    return (0, "ok")


def login(login, password="", server=""):
    return True


def terminal_info():
    return SimpleNamespace(connected=STATE["initialized"], name="stub")


def account_info():
    if not STATE["initialized"]:
        return None
    floating = 0.0
    for p in STATE["positions"]:
        price = STATE["bid"] if p.type == POSITION_TYPE_BUY else STATE["ask"]
        sign = 1 if p.type == POSITION_TYPE_BUY else -1
        floating += sign * (price - p.price_open) * 100 * p.volume
    return SimpleNamespace(login=123456, server="StubBroker-Demo", currency="USD",
                           balance=STATE["balance"],
                           equity=STATE["balance"] + floating, margin=10.0,
                           margin_free=STATE["balance"] - 10.0, margin_level=1000.0,
                           profit=floating)


def symbol_info(symbol):
    if not STATE["initialized"] or STATE["fail_symbol_info"]:
        return None
    return SimpleNamespace(
        name=symbol, visible=STATE["symbol_visible"], point=STATE["point"],
        digits=STATE["digits"], trade_tick_size=STATE["point"], trade_tick_value=1.0,
        trade_contract_size=100.0, volume_min=0.01, volume_max=200.0,
        volume_step=0.01, trade_stops_level=STATE["stops_level"],
        trade_freeze_level=0, filling_mode=1)


def symbol_select(symbol, enable):
    STATE["symbol_visible"] = True
    return True


def symbols_get():
    return [SimpleNamespace(name="XAUUSD")]


def symbol_info_tick(symbol):
    if not STATE["initialized"]:
        return None
    return SimpleNamespace(bid=STATE["bid"], ask=STATE["ask"], time=time.time())


# seconds per bar, so an M1 request really does return one-minute bars
_PERIOD = {TIMEFRAME_M1: 60, TIMEFRAME_M5: 300, TIMEFRAME_M15: 900,
           TIMEFRAME_M30: 1800, TIMEFRAME_H1: 3600}


def copy_rates_from_pos(symbol, timeframe, start, count):
    if not STATE["initialized"]:
        return None
    period = _PERIOD.get(timeframe, 300)
    now = int(time.time() // period * period) + STATE["bar_offset"] * period
    dtype = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
             ("close", "<f8"), ("tick_volume", "<i8")]
    rows = [(now - (count - 1 - i) * period, STATE["bid"], STATE["bid"] + 0.5,
             STATE["bid"] - 0.5, STATE["bid"], 100) for i in range(count)]
    return np.array(rows, dtype=dtype)


def _mark(pos):
    """MT5 fills `profit` in on every read; the engine's basket P/L reads it."""
    price = STATE["bid"] if pos.type == POSITION_TYPE_BUY else STATE["ask"]
    sign = 1 if pos.type == POSITION_TYPE_BUY else -1
    pos.profit = round(sign * (price - pos.price_open) * 100 * pos.volume, 2)
    return pos


def positions_get(symbol=None, ticket=None):
    poss = [_mark(p) for p in STATE["positions"]]
    if ticket is not None:
        return tuple(p for p in poss if p.ticket == ticket)
    if symbol is not None:
        return tuple(p for p in poss if p.symbol == symbol)
    return tuple(poss)


def orders_get(symbol=None, ticket=None):
    orders = STATE["orders"]
    if ticket is not None:
        return tuple(o for o in orders if o.ticket == ticket)
    if symbol is not None:
        return tuple(o for o in orders if o.symbol == symbol)
    return tuple(orders)


def order_check(request):
    return SimpleNamespace(retcode=TRADE_RETCODE_DONE, comment="ok")


def _ticket():
    t = STATE["next_ticket"]
    STATE["next_ticket"] += 1
    return t


def _deal():
    d = STATE["next_deal"]
    STATE["next_deal"] += 1
    return d


def order_send(request):
    STATE["sent"].append(request)
    if STATE["reject_orders"]:
        return SimpleNamespace(retcode=TRADE_RETCODE_INVALID_PRICE,
                               comment="rejected by stub", order=0, deal=0)
    action = request.get("action")

    if action == TRADE_ACTION_PENDING:
        ticket = _ticket()
        STATE["orders"].append(SimpleNamespace(
            ticket=ticket, symbol=request["symbol"], type=request["type"],
            price_open=request["price"], volume_current=request["volume"],
            sl=request.get("sl", 0.0), tp=request.get("tp", 0.0),
            comment=request.get("comment", ""), magic=request["magic"],
            time_setup=time.time()))
        return SimpleNamespace(retcode=TRADE_RETCODE_DONE, comment="placed",
                               order=ticket, deal=0, price=request["price"])

    if action == TRADE_ACTION_REMOVE:
        before = len(STATE["orders"])
        STATE["orders"] = [o for o in STATE["orders"] if o.ticket != request["order"]]
        ok = len(STATE["orders"]) < before
        return SimpleNamespace(retcode=TRADE_RETCODE_DONE if ok else 10013,
                               comment="removed" if ok else "not found",
                               order=request["order"], deal=0)

    if action == TRADE_ACTION_DEAL and "position" in request:
        pos = next((p for p in STATE["positions"]
                    if p.ticket == request["position"]), None)
        if pos is None:
            return SimpleNamespace(retcode=10013, comment="no position",
                                   order=0, deal=0)
        close_deal(pos, request["price"], "close")
        return SimpleNamespace(retcode=TRADE_RETCODE_DONE, comment="closed",
                               order=pos.ticket, deal=_deal(),
                               price=request["price"])

    return SimpleNamespace(retcode=10013, comment="unsupported", order=0, deal=0)


def history_deals_get(date_from, date_to):
    return tuple(STATE["deals"])


# ------------------------------------------------------- simulation helpers
def trigger_order(ticket, fill_price=None):
    """Turn a pending order into a position, as the broker would."""
    order = next((o for o in STATE["orders"] if o.ticket == ticket), None)
    if order is None:
        return None
    STATE["orders"] = [o for o in STATE["orders"] if o.ticket != ticket]
    is_buy = order.type == ORDER_TYPE_BUY_STOP
    price = fill_price if fill_price is not None else order.price_open
    pos = SimpleNamespace(
        ticket=order.ticket, symbol=order.symbol,
        type=POSITION_TYPE_BUY if is_buy else POSITION_TYPE_SELL,
        volume=order.volume_current, price_open=price, sl=order.sl, tp=order.tp,
        profit=0.0, comment=order.comment, magic=order.magic, time=time.time())
    STATE["positions"].append(pos)
    STATE["deals"].append(SimpleNamespace(
        ticket=_deal(), position_id=pos.ticket, symbol=pos.symbol, magic=pos.magic,
        entry=DEAL_ENTRY_IN, type=ORDER_TYPE_BUY if is_buy else ORDER_TYPE_SELL,
        volume=pos.volume, price=price, profit=0.0, commission=0.0, swap=0.0,
        comment=pos.comment, time=time.time()))
    return pos


def close_deal(pos, price, comment="tp"):
    """Close a position and append the OUT deal."""
    STATE["positions"] = [p for p in STATE["positions"] if p.ticket != pos.ticket]
    sign = 1 if pos.type == POSITION_TYPE_BUY else -1
    profit = sign * (price - pos.price_open) * 100 * pos.volume
    STATE["balance"] = round(STATE["balance"] + profit, 2)
    STATE["deals"].append(SimpleNamespace(
        ticket=_deal(), position_id=pos.ticket, symbol=pos.symbol, magic=pos.magic,
        entry=DEAL_ENTRY_OUT,
        type=ORDER_TYPE_SELL if pos.type == POSITION_TYPE_BUY else ORDER_TYPE_BUY,
        volume=pos.volume, price=price, profit=round(profit, 2), commission=0.0,
        swap=0.0, comment=comment, time=time.time()))
    return profit


def hit_tp(ticket):
    pos = next((p for p in STATE["positions"] if p.ticket == ticket), None)
    if pos is None:
        return None
    return close_deal(pos, pos.tp, "tp")
