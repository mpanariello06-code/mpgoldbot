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
tests/                  857 offline checks — python tests/run_all.py
data/                   CSVs + runtime_settings.json + state files (auto-created)
```

```
market data → rolling ladder engine → order manager → position manager
            → exit engine → risk manager → cycle manager → Telegram + CSV
```

## Continuous operation

START deploys the first ladder immediately — no candle, no signal, no
confirmation, just the safety checks. From then on the bot runs a loop for as
long as it is RUNNING and risk is OK:

```
deploy ladder → levels trigger → roll and replenish → exit engine decides
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

## The exit engine

**There is no "close after N trades" and no "close at $X".** A cycle ends when
the market structure says the move is over. `exit_engine.py` turns the trigger
sequence and the price path into readings:

| Reading | What it means |
| --- | --- |
| **CONTINUATION** | the original direction is still working — levels keep paying a full level of movement |
| **REVERSAL** (scenario 2) | the original direction failed: opposite-side runs, dominance, and give-back of the favourable excursion |
| **EXHAUSTION** | levels keep firing but price has stopped paying — chop, decaying progress, stretching gaps, stalling |
| **DIRECTIONAL** (scenario 1) | a clean same-direction run that has actually paid — and has now stopped accelerating |
| **EXTENDED** (scenario 3) | a large part of the ladder has been consumed, price travelled a long way with it, and that move is easing |

Both scenario readings are multiplied by `1 - momentum`: a run only reads as
*finished* once it stops accelerating, so a big clean move is ridden rather than
cut short. It is the cooling that ends it, never the trigger count.

They blend into an **exit score (0-100)**: reversal, exhaustion, directional,
extended, ladder depth and basket drawdown push it up; the momentum still
carrying the move holds it down. Below `EXIT_THRESHOLD_MONITOR` the cycle
continues, above `EXIT_THRESHOLD_EXIT` it closes, in between it is watched.
Every component, weight and threshold is configurable and reported.

### Order of precedence

A cycle ends for exactly one reason, and they are checked in this order:

1. `MAX_CYCLE_DRAWDOWN` — the basket is too deep under water (`RISK_DRAWDOWN`)
2. `MAX_CYCLE_DURATION` — the cycle has been open too long (`RISK_TIMEOUT`).
   **A cycle can never stay open indefinitely.** Set it to 0 and it can.
3. the exit engine (`SCENARIO_1_DIRECTIONAL`, `SCENARIO_2_REVERSAL`,
   `SCENARIO_3_EXTENDED_LADDER`, or `OTHER_RISK_EXIT` when the score is high
   with no dominant reading)
4. the **profit fallback** (`PROFIT_FALLBACK`) — last, and only when nothing
   above explains the basket

### The profit fallback

A basket can recover into profit in a market no scenario ever describes — the
ladder stalls, the readings stay flat, and the cycle would otherwise sit there
forever. The fallback takes that profit, under three conditions:

* the basket is above `PROFIT_FALLBACK_BUFFER_LEVELS × (one ladder level)` —
  **a fraction of a level, not a dollar target**: it scales with lot size and
  spacing, so changing either changes it automatically;
* it has *held* there for `PROFIT_CONFIRMATION_SECONDS`; any dip back under the
  buffer restarts the clock from zero;
* the move is not still working — a continuation reading at or above
  `PROFIT_FALLBACK_CONTINUATION_GUARD` overrides the fallback entirely.

Set `PROFIT_FALLBACK_ENABLED=false` to remove it and let the exit engine and
the risk limits be the only ways a cycle ends.

**P/L is context, never the trigger.** Profit already banked (normalised against
the cycle's own activity, so it is never a fixed dollar amount) makes the engine
readier to act on a reversal it has *already* detected; an open loss makes it
less willing to bail without one. Neither can end a cycle alone — that is the
risk manager's job, on `MAX_CYCLE_DRAWDOWN`, and it is a loss guard, not a
profit target.

Worked examples from the test suite:

| Sequence | Reading | Decision |
| --- | --- | --- |
| BUY ×4, clean move | CONTINUATION | continue — a trade count is not an exit |
| BUY ×2 then SELL ×5 | REVERSAL (2.5x dominance) | exit |
| BUY ×7, still clean | CONTINUATION | continue |
| SELL ×2 then BUY ×3 | REVERSAL | exit / monitor |
| BUY ×4 then price dies | EXHAUSTION | monitor, rising |
| BUY/SELL alternating ×6 | EXHAUSTION (chop) | exit / monitor |
| +$3.50 with momentum | CONTINUATION | continue — profit alone never closes |
| +$1.20 with a reversal | REVERSAL | exit |
| BUY ×4, then the move cools | DIRECTIONAL | exit (scenario 1) |
| BUY ×9, then it stops paying | EXTENDED | exit (scenario 3) |
| 2 triggers, flat readings, basket +0.70 held 60s | none | exit via `PROFIT_FALLBACK` |

When a cycle ends: pending orders are cancelled, remaining positions are closed
(`CYCLE_CLOSE_POSITIONS`), the cycle is written to `rolling_ladder_cycles.csv`
with the scores that ended it, and a fresh ladder is anchored at the new price.

## Telegram

`/start` opens the control panel:

| Button | Effect |
| --- | --- |
| 🟢 START | verifies MT5 and the symbol, rebuilds state from the broker, starts the single ladder loop |
| ⏸ PAUSE | cancels pending levels, opens nothing new — open positions keep being managed |
| ▶️ RESUME | rebuilds the ladder |
| 🛑 STOP | stops the loop and cancels pending orders; **open positions are left untouched** |
| 📊 STATUS | mode, price, spread, spacing, TP, lot, cycle, BUY/SELL triggers, last direction, imbalance, positions, pendings, basket P/L and drawdown, momentum, reversal, exit score, decision and market state |
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
| a state **transition** into reversal / strong continuation / fading — once, not per trigger | order placement or replenishment |
| risk events and errors (identical errors suppressed for `TELEGRAM_ERROR_THROTTLE`) | repeats of a state the chat already knows |
| optional periodic status every `TELEGRAM_STATUS_INTERVAL` minutes | anything already visible in the CSVs |

Measured on a 5-day replay (109 level triggers, 109 closes, 15 cycles): the old
one-message-per-event style would have sent **233** messages; the policy sends
**29** (15 cycle closes + 14 state transitions), an 88% reduction, with ~15,600
would-be messages suppressed. The cycle-start message adds one per cycle after
the first — a cycle costs two messages in total, at its close and at the next
ladder's confirmed deployment, and nothing in between. The optional heartbeat is separate and toggleable
— at the default 20 minutes that is 3/hour.

State alerts are capped at two per cycle, so a cycle whose reading oscillates
between continuation and exhaustion cannot chatter.

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
| 🎯 TP SETTINGS | 1–5 LEVELS (default 1 = the next rung), or an absolute distance / pips |
| 💰 LOT SIZE | 0.01–0.10 or custom, plus the hard MAX LOT cap |
| 🪜 LADDER SETTINGS | spacing (0.10–0.50), depth, first-level offset, roll mode, cycle |
| ⚖️ EXIT ENGINE | exit score, monitor score, and every weight behind them |
| 🔔 NOTIFICATIONS | status updates ON/OFF and interval, state alerts, per-entry alerts, error throttle |
| 🛡 RISK SETTINGS | max open, max pending, max depth, max spread, daily/cycle drawdown, losing-cycle streak, cooldown, order age |
| 🧭 DIRECTION | OFF / BOTH / BUY ONLY / SELL ONLY / NO ENTRIES |

**Pips are never hardcoded.** `price_utils.get_pip_size()` derives the pip from
the symbol's `digits`/`point` (10 points on a 3/5-digit feed, 1 point otherwise),
prints it at startup and shows it in the panel. Pin it under 📐 PIP SIZE if your
broker quotes gold differently — that one setting rescales all five pip targets.

## Risk

Fixed lots only — **no martingale, no size increase after a loss, no recovery
doubling.** `MAX_OPEN_POSITIONS`, `MAX_PENDING_ORDERS`, `MAX_LOT_SIZE`,
`MAX_DAILY_LOSS`, `MAX_CYCLE_LOSS`, `MAX_CYCLE_DURATION`,
`MAX_CONSECUTIVE_LOSING_CYCLES`, `MAX_SPREAD`, `MAX_SLIPPAGE`,
`COOLDOWN_AFTER_LOSS` and `CYCLE_REENTRY_COOLDOWN` are all enforced. When a
limit trips: new entries stop and pending orders are cancelled (they would breach
the limit the moment they trigger); **open positions keep running** under their
own TP/SL. The bot resumes by itself once the condition clears — the spread
filter in particular blocks and un-blocks automatically.

## Data

> Telegram carries important events; the CSVs carry everything.

* `data/rolling_ladder_events.csv` — one row per ladder event
  (`LADDER_CREATED`, `ORDER_PLACED`, `ORDER_CANCELLED`, `ORDER_TRIGGERED`,
  `TP_HIT`, `SL_HIT`, `LEVEL_ROLLED`, `CYCLE_STARTED`, `CYCLE_COMPLETED`,
  `CYCLE_LOSS`, `LADDER_DEPLOY_START`, `LADDER_DEPTH_CAP`, `ORDER_REJECTED`,
  the exit trail (`EXIT_TRIGGERED`, `EXIT_ORDERS_FOUND`, `EXIT_CANCEL_SENT`,
  `EXIT_POSITIONS_FOUND`, `EXIT_CLOSE_SENT`, `EXIT_CLOSE_CONFIRMED`,
  `EXIT_RECONCILED`, `CYCLE_FLAT`, `CYCLE_COOLDOWN_STARTED`,
  `CYCLE_COOLDOWN_COMPLETE`, `CYCLE_REENTRY_BLOCKED`, `CYCLE_ACTIVE`),
  `RISK_BLOCK`, `SPREAD_BLOCK`, `ERROR`) with the **full market
  state at that moment**: trigger counts, consecutive runs, last/previous side,
  direction changes, ratios, imbalance, depth used, distance travelled,
  efficiency, gap since the previous trigger, volatility, basket P/L and
  drawdown, and the momentum / reversal / exhaustion / exit scores. This is the
  data the exit rules are meant to be re-fitted against.
* `data/rolling_ladder_cycles.csv` — one row per finished cycle: how it ran and
  exactly why it ended, including the state **at the moment the exit was
  decided** (`exit_price`, `positions_at_exit`, `open_buys_at_exit`,
  `open_sells_at_exit`, `pending_orders_at_exit`, `floating_pnl_at_exit`) rather
  than the empty state that follows the close, plus every reading
  (`continuation_score`, `reversal_score`, `exhaustion_score`,
  `directional_score`, `extended_score`, `exit_score`, `exit_scenario`,
  `end_kind`, `end_reason`)
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
python tests/run_all.py        # 857 checks, no MT5 or network needed
```

Covers: the exit -> flat -> cooldown -> new ladder sequence and the
one-active-cycle gate, pip/point conversion, both execution adapters, settings
validation and
persistence, ladder creation and both-sided stop placement, triggering, TP,
rolling and replenishment, partial deployment reported as partial (actual vs
intended levels) and the full broker diagnosis on a refused order, the three
observed exit scenarios plus continuation, exhaustion and chop, the guarantees
that the exit is neither a trade count nor a dollar amount, the profit fallback
(buffer, confirmation, reset on a dip, continuation guard), the stuck-cycle
timeout, adaptive and risk-forced cycle ends, duplicate prevention, restart and
reconnect recovery, spread blocking, daily and cycle drawdown guards, depth and
position caps, pause/resume/stop, every Telegram button and settings screen,
replay determinism and costing, and a paper-mode run end to end.

## What still needs fitting

The exit weights and thresholds, the ladder spacing, depth and first-level
offset, the TP size, and the cooldown are **starting values chosen to reproduce
observed behaviour** — not results. Fit them with `replay.py` on real XAUUSD M5
history before trusting any of them.

> Ladder scalping on gold is high risk. Start in PAPER, and keep the risk limits
> switched on.
