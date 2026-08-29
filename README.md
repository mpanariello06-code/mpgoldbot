# XAUUSD M5 Rolling Ladder Scalper

A price-driven rolling ladder for gold on MT5, with Telegram control, CSV
persistence and a paper mode. There is **no signal engine and no indicators** —
the ladder itself is the entry mechanism:

```
price moves → a ladder level is reached → trade enters → small TP →
trade closes → ladder rolls forward → new level created → repeat
```

```
MT5 market data
      ↓
ROLLING LADDER ENGINE      ladder_engine.py
      ↓
level calculation          grid anchored per cycle, window rolls with price
      ↓
risk / spread check
      ↓
pending order manager      BUY STOP above, SELL STOP below, idempotent
      ↓
execution                  broker.py — Mt5Broker (LIVE) | PaperBroker (PAPER)
      ↓
TP management → roll ladder → profit cycle
      ↓
Telegram + CSV
```

## Files

```
optimized.py            entry point: MT5 connection, lifecycle, threads, hooks
ladder_engine.py        RollingLadderEngine — levels, cycles, reconciliation, state machine
broker.py               execution adapters: live MT5 and the paper simulator
price_utils.py          symbol-aware price/pip/point/volume conversions
config.py               .env loading, typed parsing, validation
runtime_settings.py     Telegram-controlled settings, persisted as JSON
telegram_controller.py  control panel (own thread, own event loop)
telegram_settings.py    the ⚙️ SETTINGS menu tree
csv_logger.py           trades.csv, events.csv, account_snapshots.csv, ladder.csv
tests/                  442 offline checks — python tests/run_all.py
data/                   CSVs + runtime_settings.json + state files (auto-created)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit .env
python optimized.py
```

`.env` is git-ignored — credentials never live in source.

**It starts in PAPER mode.** The ladder is calculated and filled against live
ticks, but nothing is sent to the broker. Switch to `TRADING_MODE=LIVE` only
after watching a paper session behave. The startup banner always prints the
account, the symbol and the mode in force.

## How the ladder works

* Levels sit on a **grid anchored when the cycle starts** (`anchor + n × spacing`),
  so their prices are stable — price wiggling inside a level never re-places
  orders. What rolls is the *window*: as price advances, further-out grid points
  enter the live set and consumed ones drop out.
* The nearest level is at least `FIRST_LEVEL_OFFSET` away, and never closer than
  the broker's own minimum stop distance.
* Each level carries a stable identity in the order comment —
  `RL<cycle><B|S><level>`, e.g. `RL127B3` — which is what makes the engine
  idempotent. Every pass compares the ladder it *wants* against the orders and
  positions MT5 actually reports and places or cancels only the difference.
* An order is replaced when its price, TP or SL no longer matches the settings,
  when it belongs to an older cycle, when it duplicates a level, or when it
  exceeds `ORDER_MAX_AGE`.
* On startup, reconnect or crash recovery the state is rebuilt **from the
  broker**, not from memory: live comments decide the cycle id and the grid
  anchor, so a restart continues the existing ladder instead of stacking a
  second one on top of it.

`ROLL_MODE=extend` (default) keeps a full ladder ahead of price.
`ROLL_MODE=static` pins the grid where the cycle started and lets price consume
it — the behaviour visible in the reference recording.

## Profit cycle

A cycle ends when `PROFIT_CYCLE_TARGET` trades have closed in profit (default 4).
Then: pending orders are cancelled, remaining positions are closed
(`CYCLE_CLOSE_POSITIONS`), the result is recorded, and a fresh ladder is anchored
at the new price. `CYCLE_TAKE_PROFIT_MONEY` optionally ends a cycle on a net
basket profit instead — the "close everything at once, re-anchor" pattern the
recording shows. `MAX_CYCLE_LOSS` closes a cycle out the other way and starts
the cooldown.

## Telegram

`/start` opens the control panel:

| Button | Effect |
| --- | --- |
| 🟢 START | verifies MT5 and the symbol, rebuilds state from the broker, starts the single ladder loop |
| ⏸ PAUSE | cancels pending levels, opens nothing new — open positions keep being managed |
| ▶️ RESUME | rebuilds the ladder |
| 🛑 STOP | stops the loop and cancels pending orders; **open positions are left untouched** |
| 📊 STATUS | state, mode, price, spread, spacing, TP, lot, positions, pendings, cycle, TPs, cycle P/L, daily P/L, last update |
| 💰 ACCOUNT | live balance/equity/margin |
| 📈 POSITIONS | open positions + pending count |
| 📋 TODAY'S STATS | from `ladder.csv`: orders placed, levels triggered, TP/SL hits, cycles, realised P/L |
| 🪜 LADDER | the live ladder — buy stops, market price, sell stops, open trades |
| ⚙️ SETTINGS | TP, lot, ladder and risk settings |
| 🔄 REFRESH | re-renders in place |

Commands: `/start` `/status` `/account` `/positions` `/stats` `/ladder`
`/settings` `/pause` `/resume` `/stopbot` `/help`.

Only chat ids in `TELEGRAM_CHAT_ID` / `TELEGRAM_ALLOWED_CHAT_IDS` may do
anything; everyone else gets `Unauthorized.` Secrets are never sent to Telegram.

### Settings

Everything below is changed from Telegram, applies to the **next** levels
immediately (no restart), never modifies open positions, and is stored in
`data/runtime_settings.json`. High-impact changes ask for confirmation;
♻️ RESET restores the original configuration.

| Menu | Controls |
| --- | --- |
| 🎯 TP SETTINGS | DISTANCE (any value) or 1–5 PIPS, stop loss, pip size |
| 💰 LOT SIZE | 0.01–0.10 or custom, plus the hard MAX LOT cap |
| 🪜 LADDER SETTINGS | spacing, depth, first-level offset, roll mode, profit cycle, basket TP |
| 🛡 RISK SETTINGS | max open, max pending, max spread, daily loss, cycle loss, losing-cycle streak, cooldown, order age |
| 🧭 DIRECTION | OFF / BOTH / BUY ONLY / SELL ONLY / NO ENTRIES |

**Pips are never hardcoded.** `price_utils.get_pip_size()` derives the pip from
the symbol's `digits`/`point` (10 points on a 3/5-digit feed, 1 point otherwise),
prints it at startup and shows it in the panel. Pin it under 📐 PIP SIZE if your
broker quotes gold differently — that one setting rescales all five pip targets.

## Risk

Fixed lots only — **no martingale, no size increase after a loss, no recovery
doubling.** `MAX_OPEN_POSITIONS`, `MAX_PENDING_ORDERS`, `MAX_LOT_SIZE`,
`MAX_DAILY_LOSS`, `MAX_CYCLE_LOSS`, `MAX_CONSECUTIVE_LOSING_CYCLES`,
`MAX_SPREAD`, `MAX_SLIPPAGE` and `COOLDOWN_AFTER_LOSS` are all enforced. When a
limit trips: new entries stop and pending orders are cancelled (they would breach
the limit the moment they trigger); **open positions keep running** under their
own TP/SL. The bot resumes by itself once the condition clears — the spread
filter in particular blocks and un-blocks automatically.

## Data

* `data/ladder.csv` — one row per ladder event: `LADDER_CREATED`, `ORDER_PLACED`,
  `ORDER_CANCELLED`, `ORDER_TRIGGERED`, `TP_HIT`, `SL_HIT`, `LEVEL_ROLLED`,
  `CYCLE_STARTED`, `CYCLE_COMPLETED`, `CYCLE_LOSS`, `RISK_BLOCK`,
  `SPREAD_BLOCK`, `ERROR` — with cycle, level, prices, lot, spread, tickets and
  running P/L
* `data/trades.csv` — one row per open and close, with cycle and level context
* `data/events.csv` — bot events (start/stop/pause/settings/errors)
* `data/account_snapshots.csv` — periodic balance/equity/margin
* `data/runtime_settings.json`, `ladder_state.json`, `paper_state.json` — state

## Threading

```
main thread        lifecycle + shutdown signals
ladder-engine      the one and only ladder loop (POLL_SECONDS, default 0.5s)
monitor            account snapshots
telegram           asyncio polling; API calls never touch the ladder loop
```

MT5 calls are serialised through one re-entrant lock, CSV writes through one file
lock, settings through their own lock — the engine takes a single
`SETTINGS.snapshot()` per pass, so a change landing mid-pass can never produce a
half-updated ladder.

## Tests

```bash
python tests/run_all.py        # 442 checks, no MT5 or network needed
```

Covers: pip/point conversion, the paper and live execution adapters, settings
validation and persistence, ladder creation, both-sided stop placement,
triggering, TP, rolling, a full 4-TP cycle and reset, duplicate prevention,
restart and reconnect recovery, spread blocking, daily-loss, cycle-loss and
position caps, pause/resume/stop, every Telegram button and settings screen, and
a paper-mode run end to end.

> Ladder scalping on gold is high risk. Start in PAPER, and keep the risk limits
> switched on.
