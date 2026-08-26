# XAUUSD M1 Scalper — MT5 + Telegram Control

The original single-file bot (`main.py`) refactored into a configurable,
remotely controllable, CSV-logging application. **The trading strategy is
unchanged** — same entries, same SL/TP maths, same lot sizing, same margin
checks, same position management, same 0.5s polling.

```
optimized.py            trading engine + strategy + lifecycle manager (entry point)
config.py               .env loading, typed parsing, defaults, validation
telegram_controller.py  Telegram control panel (own thread, own event loop)
csv_logger.py           thread-safe CSV persistence
requirements.txt
.env.example            copy to .env and fill in
data/                   trades.csv, events.csv, account_snapshots.csv (auto-created)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit .env
python optimized.py
```

`.env` is git-ignored — credentials never live in source.

### Minimum configuration

| Variable | Meaning |
| --- | --- |
| `MT5_LOGIN` | `0` = trade the account already logged into the running MT5 terminal |
| `MT5_PASSWORD` / `MT5_SERVER` | only needed when `MT5_LOGIN > 0` |
| `SYMBOL` | trading symbol (default `XAUUSDm`) |
| `TELEGRAM_BOT_TOKEN` | from @BotFather; empty = remote control disabled |
| `TELEGRAM_CHAT_ID` | your chat id — only listed ids may control the bot |
| `TELEGRAM_ALLOWED_CHAT_IDS` | optional extra ids, comma separated |
| `AUTO_START_TRADING` | `true` (default) starts trading on launch, like the original script |

Every strategy setting (`RISK_PERCENT`, `RR`, `SL_POINTS_MIN`, `MAX_OPEN_POSITIONS`, …)
can also be set in `.env`. **Their defaults are exactly the values hard-coded in
the original bot**, so a missing variable can never change trading behaviour.

## Telegram control panel

Send `/start` to your bot:

| Button | Effect |
| --- | --- |
| 🟢 START TRADING | verifies MT5 + symbol, starts the single trading loop (pressing twice never starts a second one) |
| ⏸ PAUSE TRADING | stops **new** entries only — breakeven, partial closes and position monitoring keep running |
| ▶️ RESUME TRADING | allows new entries again |
| 🛑 STOP BOT | stops the loop cleanly; **open positions are left untouched** and the process stays alive |
| 📊 STATUS | state, symbol, MT5 link, positions, trades today, risk/RR |
| 💰 ACCOUNT | live `mt5.account_info()` — balance, equity, margin, free margin, margin level |
| 📈 POSITIONS | live open positions with entry/SL/TP/profit/ticket |
| 📋 TODAY'S STATS | wins, losses, win rate and realized P/L from `data/trades.csv` |
| 🔄 REFRESH | re-renders the panel in place (edits the existing message) |

Commands: `/start` `/status` `/account` `/positions` `/stats` `/pause` `/resume`
`/stopbot` `/help`.

Any chat id not in `TELEGRAM_CHAT_ID` / `TELEGRAM_ALLOWED_CHAT_IDS` gets
`Unauthorized.` and can do nothing. Tokens, passwords and other secrets are
never sent to Telegram.

## Data files

* `data/trades.csv` — one row per open, partial close and close (deal-id
  de-duplicated, so restarts never duplicate or lose records)
* `data/events.csv` — meaningful events only (`BOT_STARTED`, `SIGNAL_LONG`,
  `TRADE_OPENED`, `BREAKEVEN`, `PARTIAL_CLOSE`, `MT5_DISCONNECTED`, `ERROR`, …)
* `data/account_snapshots.csv` — written every `ACCOUNT_SNAPSHOT_INTERVAL`
  seconds (default 300) by a background thread

## Threading

```
main thread        lifecycle + shutdown signals
trading-engine     the one and only strategy loop (0.5s)
monitor            account snapshots + closed-trade sync (seconds, not ms)
telegram           asyncio polling loop; API calls never touch the trading loop
```

All MT5 calls are serialised through one re-entrant lock, and all CSV writes
through one file lock. Telegram notifications are fire-and-forget, so a slow or
broken Telegram connection can never delay or stop trading.

> HIGH RISK strategy — 1.5% risk per trade with 400–1000 point stops. Demo only.
