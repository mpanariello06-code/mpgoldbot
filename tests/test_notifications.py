"""Telegram policy: important events only, deduplicated and throttled."""
from harness import Suite, use_stub_mt5
use_stub_mt5()

from notifications import TelegramNotifier

t = Suite("notifications")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Settings:
    def __init__(self, **values):
        self.values = {
            "telegram_state_alerts": True,
            "telegram_status_updates": True,
            "telegram_status_interval_minutes": 20,
            "telegram_error_throttle_seconds": 300,
        }
        self.values.update(values)

    def get(self, key):
        return self.values[key]


def notifier(**settings):
    sent = []
    clock = Clock()
    return TelegramNotifier(sent.append, Settings(**settings), clock), sent, clock


t.section("LIFECYCLE MESSAGES ARE SHORT")
n, sent, clock = notifier()
n.bot_started("XAUUSDs", "M5", 0.30, 1, "PAPER")
t.check("start message sent", len(sent) == 1)
t.check("start message names symbol, TF, spacing and cycle",
        all(x in sent[0] for x in ("XAUUSDs", "M5", "0.3", "Cycle: #1")), sent[0])
t.check("start message is compact", len(sent[0]) < 200, str(len(sent[0])))
n.bot_stopped("XAUUSDs", 18, 0, 0, 2.34)
t.check("stop message reports the session",
        "Cycle: #18" in sent[1] and "+2.34" in sent[1], sent[1])
t.check("stop message is compact", len(sent[1]) < 220, str(len(sent[1])))

t.section("ONE MESSAGE PER CYCLE, NOT PER TRADE")
n, sent, clock = notifier()
n.cycle_closed("XAUUSDs", 12, 2.34, 2, 5, "Reversal", "SELL", 102, 13)
t.check("exactly one message", len(sent) == 1)
msg = sent[0]
t.check("carries the result", "+2.34" in msg)
t.check("carries the sequence", "BUY: 2" in msg and "SELL: 5" in msg)
t.check("carries the reason", "Reversal" in msg)
t.check("carries the duration", "01:42" in msg, msg)
t.check("announces the wait, not a cycle that is not deployed yet",
        "Next ladder deploying now." in msg and "deployed" not in msg, msg)
t.check("names the exit", "Exit: Reversal" in msg, msg)
t.check("does not list individual trades", msg.count("Entry") == 0)
t.check("stays compact", len(msg) < 260, str(len(msg)))
n.cycle_closed("XAUUSDs", 13, -0.80, 4, 1, "RISK DRAWDOWN", "BUY", 60, 14)
t.check("a losing cycle is marked", sent[1].startswith("🔴"), sent[1][:12])

t.section("EXIT -> COOLDOWN -> NEW LADDER: EXACTLY TWO MESSAGES")
n, sent, clock = notifier()
n.cycle_closed("XAUUSDs", 7, 1.24, 3, 4, "Reversal", "SELL", 95, 8,
               next_ladder_seconds=10)
t.check("one message when the cooldown starts", len(sent) == 1, str(len(sent)))
close_msg = sent[0]
t.check("it names the cycle", "CYCLE #7 CLOSED" in close_msg, close_msg)
t.check("it names the exit", "Exit: Reversal" in close_msg, close_msg)
t.check("it names the result", "+1.24" in close_msg, close_msg)
t.check("it names the wait", "Next ladder in 10s." in close_msg, close_msg)
t.check("it does not claim the next ladder is live",
        "deployed" not in close_msg, close_msg)
n.cycle_started("XAUUSDs", "M5", 8, levels=10)
t.check("one message when the new ladder is live", len(sent) == 2, str(len(sent)))
start_msg = sent[1]
t.check("it names the new cycle", "CYCLE #8 STARTED" in start_msg, start_msg)
t.check("it names the symbol and timeframe",
        "XAUUSDs" in start_msg and "M5" in start_msg, start_msg)
t.check("it confirms the deployment", "Ladder deployed." in start_msg, start_msg)
t.check("no countdown messages in between", len(sent) == 2, str(sent))
import notifications
t.check("a long wait is written for a person, not in raw seconds",
        (notifications._seconds(10), notifications._seconds(900),
         notifications._seconds(930)) == ("10s", "15m", "15m 30s"),
        str([notifications._seconds(v) for v in (10, 900, 930)]))

t.section("THE SCENARIO MESSAGES ARE GONE")
n, sent, clock = notifier()
for name in ("state_change", "reversal_alert", "momentum_alert"):
    t.check(f"{name}() no longer exists", not hasattr(n, name))
t.check("nothing announces a reversal or continuation any more",
        not any(w in open(notifications.__file__).read().upper()
                for w in ("REVERSAL DETECTED", "STRONG {DOMINANT} MOMENTUM")),
        "scenario text still present")

t.section("ERRORS ARE DEDUPLICATED")
n, sent, clock = notifier()
for _ in range(20):
    n.error("Pending order rejected")
t.check("a repeating error sends once", len(sent) == 1, str(len(sent)))
t.check("error message states problem and action",
        "Pending order rejected" in sent[0] and "Action" in sent[0])
n.error("Different failure entirely")
t.check("a different error still gets through", len(sent) == 2)
clock.advance(301)
n.error("Pending order rejected")
t.check("the same error returns after the throttle window", len(sent) == 3)

t.section("RISK EVENTS ARE THROTTLED")
n, sent, clock = notifier()
for _ in range(6):
    n.risk_event("RISK BLOCK", "daily drawdown limit hit", key="risk:daily")
t.check("one risk message per window", len(sent) == 1, str(len(sent)))
clock.advance(121)
n.risk_event("RISK BLOCK", "daily drawdown limit hit", key="risk:daily")
t.check("it repeats after the window", len(sent) == 2)

t.section("PERIODIC STATUS")
status = {"symbol": "XAUUSDs", "cycle_id": 18, "state": "LADDER_ACTIVE",
          "historical_buy_triggers": 3, "historical_sell_triggers": 1,
          "current_pending_buys": 4, "current_pending_sells": 4,
          "current_open_buys": 2, "current_open_sells": 0,
          "last_side": "BUY", "positions": 2, "ladder_depth_used": 6,
          "orders": 8, "cycle_active": True, "basket_profit_target": 2.00,
          "basket_floating_pnl": 1.18, "basket_realized_pnl": 0.42}
n, sent, clock = notifier()
n.periodic_status(status)
t.check("no heartbeat right after startup", not sent, str(sent))
clock.advance(20 * 60 + 1)
n.periodic_status(status)
t.check("the first heartbeat arrives one interval in", len(sent) == 1)
t.check("status separates live state from history",
        all(x in sent[0] for x in ("Cycle: #18", "Pending: 4B / 4S",
                                   "Open: 2B / 0S", "Triggers so far: 3B / 1S",
                                   "Ladder depth: 6")), sent[0])
t.check("floating and realized are reported apart",
        "Floating basket: +1.18" in sent[0] and "Realized: +0.42" in sent[0],
        sent[0])
t.check("the basket is shown against its target",
        "+1.18 / 2.00" in sent[0], sent[0])
for _ in range(50):
    n.periodic_status(status)
t.check("no repeats inside the interval", len(sent) == 1, str(len(sent)))
clock.advance(20 * 60 + 1)
n.periodic_status(status)
t.check("it returns on the next interval", len(sent) == 2)
clock.advance(20 * 60 + 1)
n.periodic_status(status)
t.check("it keeps a steady cadence", len(sent) == 3)

n, sent, clock = notifier(telegram_status_updates=False)
n.periodic_status(status)
t.check("periodic status can be switched off", not sent)
n, sent, clock = notifier(telegram_status_interval_minutes=0)
n.periodic_status(status)
t.check("a zero interval also means off", not sent)

t.section("A BROKEN SEND NEVER PROPAGATES")
def boom(_text):
    raise RuntimeError("telegram is down")


n = TelegramNotifier(boom, Settings(), Clock())
t.check("send failures are swallowed", n.bot_started("X", "M5", 0.3, 1, "PAPER")
        is False)
t.check("the notifier keeps working afterwards",
        n.cycle_closed("X", 1, 0.0, 0, 0, "Exit", "", 0, 2) is False)

t.done()
