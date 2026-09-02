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
ladder_engine.py        RollingLadderEngine — levels, reconciliation, cycles, risk
exit_engine.py          LadderSequence + RollingLadderExitEngine — the adaptive exit
broker.py               execution adapters: live MT5 and the paper simulator
price_utils.py          symbol-aware price/pip/point/volume conversions
replay.py               historical replay / backtest over M5 bars
config.py               .env loading, typed parsing, validation
runtime_settings.py     Telegram-controlled settings, persisted as JSON
telegram_controller.py  control panel (own thread, own event loop)
telegram_settings.py    the ⚙️ SETTINGS menu tree
csv_logger.py           trades, events, account snapshots, ladder events, cycles
tests/                  780 offline checks — python tests/run_all.py
data/                   CSVs + runtime_settings.json + state files (auto-created)
```

```
market data → rolling ladder engine → order manager → position manager
            → basket exit → risk manager → cycle manager → Telegram + CSV
```

## Continuous operation

START deploys the first ladder immediately — no candle, no signal, no
confirmation, just the safety checks. From then on the bot runs a loop for as
long as it is RUNNING and risk is OK:

```
deploy ladder → levels trigger → roll and replenish → basket P/L >= target
   → cancel orders → close positions → VERIFY against MT5 → record cycle
   → CYCLE CLOSED → re-entry cooldown → verify flat
   → new cycle id → new ladder at the CURRENT price → repeat
```

### One active cycle, ever

`MAX_ACTIVE_CYCLES = 1`. Closing a cycle does **not** start the next one. In
between, the engine is deliberately cycle-less — state `COOLDOWN_AFTER_EXIT` —
and creates nothing: no ladder, no pending orders, no cycle, and the closed one
is never reopened. A new ladder is built only when **both** are true:

* `CYCLE_REENTRY_COOLDOWN` (10s by default) has elapsed since the close, and
* the account is verified flat — 0 strategy positions and 0 pending orders.

If either fails the engine waits and reconciles again, so cycle #7 still being
alive can never coexist with #8 and #9. `_start_cycle()` refuses outright while
anything from a previous cycle is live, and logs `CYCLE_REENTRY_BLOCKED`.

The new ladder is anchored on the price **at the end of the cooldown**, not on
the old grid and not at the next M5 open.

**The cooldown is between cycles, never between trades.** It is not a delay
between ladder levels, triggers or pending orders — inside an active cycle the
ladder triggers and replenishes at the poll rate. Set
`CYCLE_REENTRY_COOLDOWN=0` for the previous behaviour (re-enter on the next
pass). A risk-forced close also arms the much longer `COOLDOWN_AFTER_LOSS`; the
gate waits for whichever runs longer.

### Auditing an exit

A cycle can open and close in seconds, so every step of the exit is logged to
`rolling_ladder_events.csv` and the console:

```
EXIT_TRIGGERED → EXIT_ORDERS_FOUND → EXIT_CANCEL_SENT → ORDER_CANCELLED
  → EXIT_POSITIONS_FOUND → EXIT_CLOSE_SENT → EXIT_CLOSE_CONFIRMED
  → EXIT_RECONCILED → CYCLE_FLAT → CYCLE_COMPLETED → CYCLE_COOLDOWN_STARTED
  → CYCLE_COOLDOWN_COMPLETE → LADDER_DEPLOY_START → LADDER_CREATED
  → CYCLE_ACTIVE
```

Anything the broker refuses is logged with its exact MT5 retcode.

The transition is guarded: it is driven by a single state machine under a lock,
so one exit event can only ever produce one new cycle. If the broker refuses a
cancel or a close, the engine keeps retrying and **will not build a second
ladder** until MT5 confirms the old one is gone. Cycle ids are persisted and
never reused, including across restarts and adopted cycles — and a restart
inside a cooldown serves out what is left of it.

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

## The basket

**A triggered level is not a trade. It is one leg of a basket.**

Ladder positions carry **no individual take profit**. There is no TP setting at
all — orders go to MT5 with `tp = 0.0`. Nothing closes on its own: the cycle is
opened, developed and closed as a single managed unit, and the exit only ever
sees the *combined* number:

```
BUY  @ 4437.76   -1.20
BUY  @ 4438.06   -0.60
SELL @ 4437.46   +0.40
SELL @ 4437.16   +0.90
SELL @ 4436.86   +1.10
                 -----
CYCLE #X basket  +0.60   <- the only P/L the decision is made on
```

This is the point of the architecture. A BUY that triggers just before price
reverses will sit negative while the SELLs below it go positive; closing the
winners and holding the loser is exactly what the strategy must *not* do. The
basket is allowed to develop, and it is closed in one piece.

A basket is identified by `(symbol, magic, cycle_id)` — the broker adapters
filter by symbol and magic, and every order carries `RL<cycle><B|S><level>` in
its comment. The engine exposes it directly:

| Call | Returns |
| --- | --- |
| `cycle_positions()` / `cycle_orders()` | this cycle's legs and pending levels |
| `get_cycle_floating_pnl()` | combined P/L of the open legs — **0.00 when nothing is open** |
| `get_cycle_realized_pnl()` | banked P/L; stays 0.00 until the basket is closed |
| `get_cycle_net_pnl()` | realized + floating |
| `get_cycle_drawdown()` | give-back from the basket's own peak |
| `basket()` | all of the above, plus open/pending counts split by side |

A position or order whose comment names a *different* cycle is excluded. One
whose comment cannot be parsed at all is still counted: it reached us through
the symbol+magic filter, so it is our exposure — some brokers truncate comments,
and dropping those legs would understate the basket.

**`MAX_OPEN_POSITIONS` must allow a full ladder** (12 by default): with no
individual TP the basket accumulates until the cycle closes, and a low cap would
stop the ladder after N triggers — which is a fixed trade count, not a strategy.
`config.validate()` warns when it is below `LADDER_DEPTH`.

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
* Orders carry no TP at all; an order is replaced when its price or SL no longer matches the settings,
  when it belongs to an older cycle, when it duplicates a level, or when it
  exceeds `ORDER_MAX_AGE`.
* On startup, reconnect or crash recovery the state is rebuilt **from the
  broker**, not from memory: live comments decide the cycle id and the grid
  anchor, so a restart continues the existing ladder instead of stacking a
  second one on top of it.

`ROLL_MODE=extend` (default) keeps a full ladder ahead of price.
`ROLL_MODE=static` pins the grid where the cycle started and lets price consume
it — the behaviour visible in the reference recording.

## The exit

**There is exactly one normal strategy exit:**

```
total floating basket P/L >= BASKET_PROFIT_TARGET   ->  close everything
```

`BASKET_PROFIT_TARGET` defaults to **$2.00** and is the single source of truth —
one setting in `.env`, editable at runtime under ⚙️ SETTINGS → 🎯 BASKET TARGET,
and no dollar figure is hard-coded anywhere else. Set it to 0 and the normal
exit is disabled, leaving only the risk limits.

It is evaluated on **every poll**, against the live basket. There is no candle
wait, no confirmation window and no second condition:

```
+1.72  +1.83  +1.94  +2.01   <- exits here, on the tick
```

### Order of precedence

A cycle ends for exactly one reason, checked in this order:

1. `MAX_CYCLE_DRAWDOWN` — the basket is too deep under water (`RISK_DRAWDOWN`)
2. `MAX_CYCLE_DURATION` — the cycle has been open too long (`RISK_TIMEOUT`).
   **A basket can never float indefinitely.** Set it to 0 and it can.
3. the basket profit target (`BASKET_PROFIT_TARGET`)

The first two are emergency protection and override the strategy. The exit
reasons are the complete set: `BASKET_PROFIT_TARGET`, `RISK_DRAWDOWN`,
`RISK_TIMEOUT`, `RISK_SPREAD`, `MANUAL_STOP`, `OTHER_RISK_EXIT`.

### An exit closes the entire basket

Every open leg and every pending level of the cycle goes at once. The engine
never partially closes a basket and never picks off individual winners or
losers:

```
lock the cycle -> stop new exposure -> cancel this cycle's pending orders
  -> close this cycle's positions -> re-read MT5 -> confirm 0 / 0
  -> record the final P/L and the exit reason -> mark CLOSED
```

`EXIT_TRIGGERED` is recorded once and the transition is held under a lock, so a
basket oscillating around the target cannot run the exit twice.

### What used to be here

A scenario-based exit engine — directional / reversal / extended readings
blended into a 0–100 score, plus a profit-recovery fallback — has been
**deleted**, not disabled: `exit_engine.py` is gone, and so are its weights,
thresholds, minimum-trigger gates, state tracking, Telegram messages and CSV
columns. The current version is a deliberately simple, deterministic baseline
to measure. Anything smarter will be built from collected data rather than
restored from here.

## Telegram

`/start` opens the control panel:

| Button | Effect |
| --- | --- |
| 🟢 START | verifies MT5 and the symbol, rebuilds state from the broker, starts the single ladder loop |
| ⏸ PAUSE | cancels pending levels, opens nothing new — open positions keep being managed |
| ▶️ RESUME | rebuilds the ladder |
| 🛑 STOP | stops the loop and cancels pending orders; **open positions are left untouched** |
| 📊 STATUS | mode, price, spread, spacing, lot, cycle and its state, live pendings and positions split by side, historical triggers, ladder depth, and the basket's floating / realized / total P/L against its target |
| 💰 ACCOUNT | live balance/equity/margin |
| 📈 POSITIONS | open positions + pending count |
| 📋 TODAY'S STATS | from `ladder.csv`: orders placed, levels triggered, TP/SL hits, cycles, realised P/L |
| 🪜 LADDER | the live ladder — buy stops, market price, sell stops, open trades |
| ⚙️ SETTINGS | TP, lot, ladder and risk settings |
| 🔄 REFRESH | re-renders in place |

Commands: `/start` `/status` `/account` `/positions` `/stats` `/ladder`
`/settings` `/pause` `/resume` `/stopbot` `/help`.

### What Telegram actually sends

This strategy can trigger dozens of levels a minute, so notifications are
event-based, deduplicated and throttled:

| Sent | Not sent |
| --- | --- |
| bot started / stopped (compact) | individual level triggers |
| one message when a cycle closes: exit, result, BUY/SELL counts, duration, and how long until the next ladder | individual TPs |
| one message when the next ladder is **actually deployed** (`CYCLE #N STARTED`) | any countdown in between |
| risk events and errors (identical errors suppressed for `TELEGRAM_ERROR_THROTTLE`) | order placement or replenishment |
| optional periodic status every `TELEGRAM_STATUS_INTERVAL` minutes | any P/L update |
| | anything already visible in the CSVs |

**A cycle costs exactly two messages** — one when it closes, one when the next
ladder is confirmed live — and nothing in between. There is no message per
level, per order, per trigger or per P/L update. The optional heartbeat is
separate and toggleable; at the default 20 minutes that is 3/hour.

The detailed view lives behind the 📊 STATUS button — pulled when you want it,
never pushed.

Only chat ids in `TELEGRAM_CHAT_ID` / `TELEGRAM_ALLOWED_CHAT_IDS` may do
anything; everyone else gets `Unauthorized.` Secrets are never sent to Telegram.

### Settings

Everything below is changed from Telegram, applies to the **next** levels
immediately (no restart), never modifies open positions, and is stored in
`data/runtime_settings.json`. High-impact changes ask for confirmation;
♻️ RESET restores the original configuration.

| Menu | Controls |
| --- | --- |
| 🎯 BASKET TARGET | the one normal exit — $1 / $2 / $3 / $5 or custom, or OFF |
| 💰 LOT SIZE | 0.01–0.10 or custom, plus the hard MAX LOT cap |
| 🪜 LADDER SETTINGS | spacing (0.10–0.50), depth, first-level offset, roll mode, cycle |
| 🛑 STOP LOSS | emergency per-position SL (off by default), and 📐 PIP SIZE |
| 🔔 NOTIFICATIONS | status updates ON/OFF and interval, error throttle |
| 🛡 RISK SETTINGS | max open, max pending, max depth, max spread, daily/cycle drawdown, losing-cycle streak, cooldown, order age |
| 🧭 DIRECTION | OFF / BOTH / BUY ONLY / SELL ONLY / NO ENTRIES |

**Pips are never hardcoded.** `price_utils.get_pip_size()` derives the pip from
the symbol's `digits`/`point` (10 points on a 3/5-digit feed, 1 point otherwise),
prints it at startup and shows it in the panel. Pin it under 📐 PIP SIZE if your
broker quotes gold differently.

## Risk

Fixed lots only — **no martingale, no size increase after a loss, no recovery
doubling.** `MAX_OPEN_POSITIONS`, `MAX_PENDING_ORDERS`, `MAX_LOT_SIZE`,
`MAX_DAILY_LOSS`, `MAX_CYCLE_LOSS`, `MAX_CYCLE_DURATION`,
`MAX_CONSECUTIVE_LOSING_CYCLES`, `MAX_SPREAD`, `MAX_SLIPPAGE`,
`COOLDOWN_AFTER_LOSS` and `CYCLE_REENTRY_COOLDOWN` are all enforced. When a
limit trips: new entries stop and pending orders are cancelled (they would breach
the limit the moment they trigger); **open positions keep running** until the
basket is closed. The bot resumes by itself once the condition clears — the spread
filter in particular blocks and un-blocks automatically.

## Data

> Telegram carries important events; the CSVs carry everything.

* `data/rolling_ladder_events.csv` — one row per ladder event
  (`LADDER_CREATED`, `ORDER_PLACED`, `ORDER_CANCELLED`, `ORDER_TRIGGERED`,
  `SL_HIT`, `POSITION_CLOSED`, `LEVEL_ROLLED`, `CYCLE_STARTED`, `CYCLE_COMPLETED`,
  `CYCLE_LOSS`, `LADDER_DEPLOY_START`, `LADDER_DEPTH_CAP`, `ORDER_REJECTED`,
  the exit trail (`EXIT_TRIGGERED`, `EXIT_ORDERS_FOUND`, `EXIT_CANCEL_SENT`,
  `EXIT_POSITIONS_FOUND`, `EXIT_CLOSE_SENT`, `EXIT_CLOSE_CONFIRMED`,
  `EXIT_RECONCILED`, `CYCLE_FLAT`, `CYCLE_COOLDOWN_STARTED`,
  `CYCLE_COOLDOWN_COMPLETE`, `CYCLE_REENTRY_BLOCKED`, `CYCLE_ACTIVE`),
  `RISK_BLOCK`, `SPREAD_BLOCK`, `ERROR`) with the **state of the basket at that
  moment**: trigger counts by side, last/previous side, direction changes, depth
  used, distance travelled, net levels, the basket's floating / realized / total
  P/L, its drawdown, and the target in force. This is what a future exit rule
  would be fitted against — it records what happened, not a score.
* `data/rolling_ladder_cycles.csv` — one row per finished cycle: how it ran and
  exactly why it ended, including the state **at the moment the exit was
  decided** (`exit_price`, `positions_at_exit`, `open_buys_at_exit`,
  `open_sells_at_exit`, `pending_orders_at_exit`, `floating_pnl_at_exit`) rather
  than the empty state that follows the close, plus `basket_profit_target`,
  `ladder_depth_used`, `peak_pnl`, `drawdown`, `end_kind` and `end_reason`
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

## Replay / backtest

```bash
python replay.py --bars 3000                      # M5 bars straight from MT5
python replay.py --csv gold_m5.csv                # time,open,high,low,close[,spread]
python replay.py --csv g.csv --spacing 0.20 --tp-levels 2 --exit-threshold 60
```

The replay drives the **real** engine and the **real** paper broker, so what is
measured is the shipped strategy. Bars are fed one at a time and walked as a
price path (open → adverse extreme → other extreme → close, by default), the
engine only ever sees prices up to the current step, and the intrabar ordering
is derived from the current bar alone — no look-ahead. Spread, commission,
slippage and the broker's minimum stop distance are all applied, and simulated
time drives cooldowns, order ages and trigger gaps. The summary prints its own
assumptions, because at this timescale they matter more than the strategy.

When fitting parameters, split the data: development → validation → a holdout
you only touch once.

## Tests

```bash
python tests/run_all.py        # 780 checks, no MT5 or network needed
```

Covers: the basket profit target at 1.99 / 2.00 / 2.01 and fluctuating around
it (one exit, never repeated); no individual TP on any order or position; the
basket's floating / realized / net P/L and its cycle scoping; the whole basket
closing at once; the 10-second cooldown creating nothing; the next ladder
anchored on the current price; the one-active-cycle gate blocked by leftover
positions and by leftover pending orders; hard risk overriding the target;
pip/point conversion; both execution adapters; settings validation and
persistence; ladder creation, rolling and replenishment; partial deployment
reported as partial and the full broker diagnosis on a refused order; restart
and reconnect recovery; spread blocking; daily and cycle drawdown guards; depth
and position caps; pause/resume/stop; every Telegram button and settings screen;
replay determinism and costing; and a paper-mode run end to end.

## What still needs fitting

`BASKET_PROFIT_TARGET`, the ladder spacing, depth and first-level offset, the
drawdown and duration limits and the cooldown are **starting values**, not
results. This version is deliberately simple and deterministic so it can be
measured; fit it with `replay.py` on real XAUUSD M5 history, and set
`COMMISSION_PER_LOT` to your broker's real figure before drawing any conclusion.

**A working implementation is not a profitable strategy.** Nothing here has been
shown to make money.

> Ladder scalping on gold is high risk. Start in PAPER, and keep the risk limits
> switched on.
