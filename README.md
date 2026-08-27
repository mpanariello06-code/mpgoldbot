# XAUUSD M1 Scalper — MT5 + Telegram Control

The original single-file bot (`main.py`) refactored into a configurable,
remotely controllable, CSV-logging application. **The trading strategy is
unchanged** — same entries, same SL/TP maths, same lot sizing, same margin
checks, same position management, same 0.5s polling.

```
optimized.py            trading engine + strategy + lifecycle manager (entry point)
config.py               .env loading, typed parsing, defaults, validation
runtime_settings.py     thread-safe, JSON-persisted settings changed from Telegram
tp_engine.py            TP / RR execution layer (pip targets, RR validation, pip size)
telegram_controller.py  Telegram control panel (own thread, own event loop)
telegram_settings.py    the ⚙️ SETTINGS menu tree, confirmations, custom input
csv_logger.py           thread-safe CSV persistence
requirements.txt
.env.example            copy to .env and fill in
data/                   trades.csv, events.csv, account_snapshots.csv,
                        runtime_settings.json (all auto-created)
```

The signal engine is untouched: sweep detection, candle/wick logic, M5 bias,
candle-range and spread filtering and the SL calculation are byte-identical to
the original. Everything new happens *after* a signal is found:

```
signal detected -> strategy SL -> TP/RR engine -> RR validation -> lot sizing
                -> margin safety -> existing MT5 order -> Telegram + CSV
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
| ⚙️ SETTINGS | TP mode, minimum RR, lot/risk, positions, SL, breakeven, partial, spread, pip size, reset |
| 🔄 REFRESH | re-renders the panel in place (edits the existing message) |

Commands: `/start` `/status` `/account` `/positions` `/stats` `/settings`
`/pause` `/resume` `/stopbot` `/help`.

## ⚙️ Settings (TP / risk control)

Everything below is changed from Telegram, applies to the **next** trade
immediately (no restart), never touches open positions, and is stored in
`data/runtime_settings.json` so it survives a restart. High-impact changes
(TP, RR, risk, lot, positions, SL) ask for confirmation first; ♻️ RESET
restores the original configuration.

| Menu | What it controls | Options |
| --- | --- | --- |
| 🎯 TP MODE | where the take profit goes | 1–5 PIPS, or CUSTOM RR (1R–3R, or typed) |
| ⚖️ MIN RR | reject threshold | 0.5 / 0.75 / 1.0 / 1.25 / 1.5 / 2.0 / custom |
| 💰 LOT / RISK | sizing mode and values | RISK % (0.5–2.0/custom) or FIXED LOT (0.01–0.10/custom) |
| 📈 MAX POSITIONS | concurrent positions | 1–5, or custom |
| 🛑 STOP LOSS | SL mode and bounds | STRUCTURAL (default) / FIXED, SL min, SL max, fixed distance |
| 🔒 BREAKEVEN | on/off + trigger | 0.25R / 0.5R / 0.75R / 1.0R |
| 📉 PARTIAL CLOSE | on/off, trigger, size | 1.0R/1.5R/2.0R, 20%/30%/50% |
| 📏 MAX SPREAD | spread filter threshold | 300 / 400 / 500 / 600 / custom |
| 📐 PIP SIZE | what "1 pip" means | AUTO, or pin 1 / 10 / 100 points |

### TP modes and RR validation

A pip target is never sent blindly. For every signal the bot computes

```
risk   = |entry - SL|        (the strategy's own SL, unchanged)
reward = |TP - entry|        (from the selected TP mode)
RR     = reward / risk
```

and **rejects the trade** when `RR < MIN_RR` — the setup was valid, the chosen
target just makes it a bad trade. The rejection is logged to `events.csv`
(`TRADE_REJECTED_RR`), counted on the STATUS screen and pushed to Telegram:

```
⚠️ SIGNAL REJECTED
BUY XAUUSDm
Signal detected successfully.
TP Mode: 2 PIPS
SL: 4.00 pips (400 pts)
TP: 2.00 pips (2 pts)
Calculated RR: 0.01
Minimum RR: 1.00R
No order placed.
```

### Pip size

Nothing about gold is hardcoded. `get_pip_size()` derives the pip from the
symbol's `digits`/`point`: a 3- or 5-digit feed quotes tenths of a pip
(1 pip = 10 points), a 2- or 4-digit feed quotes whole pips (1 pip = 1 point).
The resolved value is printed at startup and shown in the settings panel
(`1 pip = 0.01 (1 points, auto)`). If your broker quotes gold on a different
convention, pin it under 📐 PIP SIZE — that one setting rescales all five pip
targets.

**Defaults keep the original behaviour**: TP MODE = CUSTOM RR at 3.0R with
MIN RR 1.0, so until you pick a pip target the bot trades exactly as before.

Any chat id not in `TELEGRAM_CHAT_ID` / `TELEGRAM_ALLOWED_CHAT_IDS` gets
`Unauthorized.` and can do nothing. Tokens, passwords and other secrets are
never sent to Telegram.

## Data files

* `data/trades.csv` — one row per open, partial close and close (deal-id
  de-duplicated, so restarts never duplicate or lose records). Each row also
  records the execution settings actually used — `tp_mode`, `tp_distance`,
  `sl_distance`, `rr`, `risk_mode`, `risk_percent`, `fixed_lot`, `min_rr` —
  so TP configurations can be compared later. Files written by an earlier
  version are migrated to the new columns automatically, keeping every row.
* `data/events.csv` — meaningful events only (`BOT_STARTED`, `SIGNAL_LONG`,
  `TRADE_OPENED`, `BREAKEVEN`, `PARTIAL_CLOSE`, `MT5_DISCONNECTED`, `ERROR`, …)
* `data/account_snapshots.csv` — written every `ACCOUNT_SNAPSHOT_INTERVAL`
  seconds (default 300) by a background thread
* `data/runtime_settings.json` — Telegram-controlled settings (never secrets)

## Threading

```
main thread        lifecycle + shutdown signals
trading-engine     the one and only strategy loop (0.5s)
monitor            account snapshots + closed-trade sync (seconds, not ms)
telegram           asyncio polling loop; API calls never touch the trading loop
```

All MT5 calls are serialised through one re-entrant lock, CSV writes through one
file lock, and settings through their own lock — the loop takes a single
`SETTINGS.snapshot()` at the top of each cycle, so a change landing mid-cycle
can never produce a half-updated order. Telegram notifications are fire-and-forget, so a slow or
broken Telegram connection can never delay or stop trading.

> HIGH RISK strategy — 1.5% risk per trade with 400–1000 point stops. Demo only.
