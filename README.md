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
tests/                  971 offline checks — python tests/run_all.py
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
alive can never coexist with #8 and #9. `create_new_ladder()` refuses outright while
anything from a previous cycle is live, and logs
`LADDER_REJECTED_ALREADY_ACTIVE`.

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
  → EXIT_RECONCILED → CYCLE_FLAT → CYCLE_COMPLETED → LADDER_CLOSED
  → COOLDOWN_STARTED → COOLDOWN_FINISHED → LADDER_DEPLOY_START → LADDER_CREATED
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

## Cycle entry

A new cycle is a **deliberate decision taken once per newly CLOSED
`ENTRY_TIMEFRAME` candle** (default `M1`) — never on the forming candle, and
never on a bare tick.

```
flat -> WAITING_FOR_ENTRY  (between candles: nothing is evaluated)
     -> a new candle closes
     -> ENTRY_EVALUATION   (exactly once for that candle, whatever the outcome)
     -> accepted -> CREATE_LADDER
     -> rejected -> WAITING_FOR_ENTRY, with the reason recorded
```

`copy_rates_from_pos(0, 2)` returns `[closed, forming]`; index `-2` is the last
closed bar, and its open time is what the engine gates on. That bar time is
stored (`last_entry_bar`) and persisted, so the same candle is never evaluated
twice — not by a faster tick, not by a restart.

**The conditions are the ones the engine already had:** the re-entry cooldown
has elapsed, the account is verified flat at the broker, risk is not blocking,
and the spread is acceptable. No indicator was added — the change is *when* the
question is asked, not what is asked.

Two consequences worth being explicit about:

* **The ladder is not rebuilt every candle.** Candle closes only decide whether
  a NEW cycle begins. While a cycle is live the entry gate is not consulted at
  all; the ladder is managed by the reconciler and closed by the exit rules.
* **The cooldown alone does not re-enter.** After `CYCLE_REENTRY_COOLDOWN`
  expires the engine sits in `WAITING_FOR_ENTRY` until the *next* candle
  closes. The cooldown is a floor on re-entry, not a trigger for it.

Every evaluated candle is written to `data/entry_evaluations.csv` — accepted or
rejected, with the reason, the cooldown left, the live position and order
counts, and the risk/spread verdicts — so accepted and rejected candles can be
compared after the fact.

If the bar feed fails or is not wired at all, the gate is **off** and the
pre-M1 behaviour stands (deploy as soon as the safety gates pass). The failure
is logged as an `ERROR`; the bot does not stall waiting for a candle it cannot
see.

## The ladder lifecycle

**One active ladder = one ladder ID.** A ladder is `LADDER_DEPTH` BUY STOP plus
`LADDER_DEPTH` SELL STOP — 11 + 11 = **22 orders**, placed once when the cycle
starts and **never replenished**:

```
IDLE -> WAITING_FOR_ENTRY -> ENTRY_EVALUATION (one closed M1 candle)
     -> CREATE_LADDER -> PLACE 22 PENDING ORDERS -> ACTIVE_LADDER
     -> MANAGE_CURRENT_LADDER -> FINAL CLOSE CONDITION -> verify flat
     -> COOLDOWN (CYCLE_REENTRY_COOLDOWN, 10s) -> WAITING_FOR_ENTRY
     -> ENTRY_EVALUATION -> CREATE_NEW_LADDER
```

`create_new_ladder()` is the only function in the engine that creates a ladder,
and it refuses — logging `LADDER_REJECTED_ALREADY_ACTIVE` — when a ladder is
already active, when the cooldown has not expired, or when anything from a
previous ladder is still at the broker. `ladder_active` and `active_ladder_id`
are the single source of truth.

Nothing that happens *inside* a live ladder ends it or starts another: a
trigger, a position opening or closing, a direction change, or the pendings
running low are all events belonging to the current ladder. Only the **final
close condition** — hard risk (`MAX_CYCLE_DRAWDOWN`, then `MAX_CYCLE_DURATION`)
or basket profit management — moves it to COOLDOWN.

`ROLL_MODE=static` with `REARM_LEVELS=false` is what makes the grid fixed: it
is pinned when the cycle starts and price consumes it. The trade-off is real —
**a pinned grid does not follow price**, so if the market leaves the grid the
ladder runs with whatever levels it has left rather than re-centring.
`ROLL_MODE=extend` restores the old rolling behaviour, where consumed levels
are replaced and a live ladder keeps placing orders all cycle; the config warns
when it is set.

At creation the accepted orders are counted per side. A short deployment is
logged as `LADDER_CREATED … PARTIAL` plus an `ERROR` naming the shortfall, and
the ladder runs as placed — **a second ladder is never created to compensate**.

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

`ROLL_MODE=static` (default) pins the grid where the cycle started and lets
price consume it — the behaviour visible in the reference recording.
`ROLL_MODE=extend` keeps a full ladder ahead of price by replacing consumed
levels; the config warns when it is set, because it is what makes a ladder look
like it is being rebuilt.

## The exit

The normal strategy exit is **basket profit management** — one state machine,
one decision path:

```
BASKET_BUILDING        below the target
      ↓  floating P/L >= BASKET_PROFIT_TARGET
PROFIT_TARGET_REACHED  taken now if PROFIT_RUNNER_ENABLED is off;
                       otherwise allowed to run, with the floor underneath it
      ↓  peak >= PROFIT_PROTECTION_ACTIVATION
PROFIT_PROTECTION      the peak is trailed: give back more than
                       PROFIT_PROTECTION_TRAIL and the basket is taken
```

Peak P/L is tracked per cycle, only ever rises while the cycle is open, and
starts at zero for every new cycle. `drawdown_from_peak = peak_pnl -
current_pnl`. Every threshold is a setting; none of them is written twice.

| Setting | Default | What it does |
| --- | --- | --- |
| `BASKET_PROFIT_TARGET` | 2.00 | the profit at which a basket is takeable |
| `PROFIT_RUNNER_ENABLED` | true | let a basket run past the target instead of cutting it off |
| `PROFIT_PROTECTION_ACTIVATION` | 3.00 | peak at which the trail arms — and it stays armed |
| `PROFIT_PROTECTION_TRAIL` | 1.50 | give-back from the peak that closes the basket |
| `MIN_PROTECTED_PROFIT` | 1.00 | the floor a protected basket is never knowingly let through (capped at the target) |

**Why this exists.** A cycle that reached +$95 floating and closed at −$9.78 is
the failure this replaces: the $2 target alone captured nothing on the way up
and the emergency drawdown was the only thing left underneath. Now the same
path — `+2 +10 +30 +60 +95 +70 …` — arms protection at +3 and closes on the
first give-back past the trail, at +70 rather than −9. That sequence is a test.

The trail is deliberately a **fixed dollar amount** for this version: it is the
simplest thing that reliably stops a winner becoming a loser.
`ProfitRules.trail_for(peak)` is the one place it is computed — a
percentage-of-peak, volatility-adjusted or ladder-depth-adjusted trail replaces
that method body and nothing else.

### Order of precedence

A cycle ends for exactly one reason, checked in this order:

1. `MAX_CYCLE_DRAWDOWN` — the basket is too deep under water (`RISK_DRAWDOWN`)
2. `MAX_CYCLE_DURATION` — the cycle has been open too long (`RISK_TIMEOUT`).
   **A basket can never float indefinitely.** Set it to 0 and it can.
3. basket profit management — `BASKET_PROFIT_TARGET` or `PROFIT_PROTECTION`

The first two are emergency protection and override the strategy. In practice
a basket that got ahead is taken by the trail long before the emergency
drawdown is reached — that is the point of it. The exit reasons are the
complete set: `BASKET_PROFIT_TARGET`, `PROFIT_PROTECTION`, `RISK_DRAWDOWN`,
`RISK_TIMEOUT`, `RISK_SPREAD`, `EMERGENCY_EXIT`, `MANUAL_EXIT`.

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
| 📊 STATUS | mode, price, spread, spacing, lot, cycle and its profit-management state, live pendings and positions split by side, historical triggers, ladder depth, and the basket's current / peak / give-back P/L with the protection threshold |
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
| 🎯 BASKET PROFIT | the target, the profit runner on/off, the protection activation, the trail and the protected floor |
| 💰 LOT SIZE | 0.01–0.10 or custom, plus the hard MAX LOT cap |
| 🪜 LADDER SETTINGS | spacing (0.10–0.50), depth, first-level offset, roll mode, cycle |
| 🛑 STOP LOSS | emergency per-position SL (off by default), and 📐 PIP SIZE |
| 🔔 NOTIFICATIONS | status updates ON/OFF and interval, error throttle, telemetry interval |
| 🛡 RISK SETTINGS | max open, max pending, max depth, max spread, daily/cycle drawdown, losing-cycle streak, cooldown, order age |
| 🧭 DIRECTION | OFF / BOTH / BUY ONLY / SELL ONLY / NO ENTRIES |

**Pips are never hardcoded.** `price_utils.get_pip_size()` derives the pip from
the symbol's `digits`/`point` (10 points on a 3/5-digit feed, 1 point otherwise),
prints it at startup and shows it in the panel. Pin it under 📐 PIP SIZE if your
broker quotes gold differently.

## Risk

Fixed lots only — **no martingale, no size increase after a loss, no recovery
doubling.** `MAX_OPEN_POSITIONS`, `MAX_PENDING_ORDERS`, `MAX_LOT_SIZE`,
`MAX_LADDER_DEPTH`, `MAX_DAILY_DRAWDOWN`, `MAX_CYCLE_DRAWDOWN`,
`MAX_CYCLE_DURATION`, `MAX_CONSECUTIVE_LOSING_CYCLES`, `MAX_SPREAD`,
`MAX_SLIPPAGE`, `COOLDOWN_AFTER_LOSS` and `CYCLE_REENTRY_COOLDOWN` are all
enforced. When a limit trips: new entries stop and pending orders are cancelled
(they would breach the limit the moment they trigger); **open positions keep
running** until the basket is closed. The bot resumes by itself once the
condition clears — the spread filter in particular blocks and un-blocks
automatically.

**`MAX_LADDER_DEPTH` caps how much exposure one cycle may take on.** The ladder
is placed once and never replenished, so "place no more levels" is not enough —
the untouched pendings are already at the broker waiting to fill. When the cap
is reached (`LADDER_DEPTH_CAP`, logged once) the **remaining pendings are
cancelled**: no further exposure is added this cycle. The basket already open
is not closed by the cap — it keeps being managed by the exit rules, and the
cycle ends normally.

## Data

> Telegram carries important events; the CSVs carry everything.

* `data/rolling_ladder_events.csv` — one row per ladder event
  (`LADDER_CREATED`, `ORDER_PLACED`, `ORDER_CANCELLED`, `ORDER_TRIGGERED`,
  `SL_HIT`, `POSITION_CLOSED`, `LEVEL_ROLLED`, `CYCLE_STARTED`, `CYCLE_COMPLETED`,
  `CYCLE_LOSS`, `LADDER_DEPLOY_START`, `LADDER_DEPTH_CAP`, `ORDER_REJECTED`,
  the ladder lifecycle (`LADDER_CREATED`, `POSITION_OPENED`, `LADDER_CLOSED`,
  `COOLDOWN_STARTED`, `COOLDOWN_FINISHED`, `LADDER_REJECTED_ALREADY_ACTIVE`),
  the exit trail (`EXIT_TRIGGERED`, `EXIT_ORDERS_FOUND`, `EXIT_CANCEL_SENT`,
  `EXIT_POSITIONS_FOUND`, `EXIT_CLOSE_SENT`, `EXIT_CLOSE_CONFIRMED`,
  `EXIT_RECONCILED`, `CYCLE_FLAT`,
  `CYCLE_ACTIVE`),
  the entry gate (`WAITING_FOR_ENTRY`, `ENTRY_EVALUATED`),
  `RISK_BLOCK`, `SPREAD_BLOCK`, `ERROR`) with the **state of the basket at that
  moment**: trigger counts by side, last/previous side, direction changes, depth
  used, distance travelled, net levels, the basket's floating / realized / total
  P/L, its drawdown, and the target in force. This is what a future exit rule
  would be fitted against — it records what happened, not a score.
* `data/rolling_ladder_cycles.csv` — one row per finished cycle: how it ran and
  exactly why it ended, including the state **at the moment the exit was
  decided** (`exit_price`, `positions_at_exit`, `open_buys_at_exit`,
  `open_sells_at_exit`, `pending_orders_at_exit`, `floating_pnl_at_exit`) rather
  than the empty state that follows the close, plus how the profit-management
  state machine ran: `max_floating_profit`, `max_floating_loss`,
  `max_drawdown`, `profit_giveback`, `time_to_peak`, `time_to_profit_target`,
  `time_in_profit`, `time_in_protection`, `protection_active`, `cycle_state`,
  `exit_reason` and `final_realized_pnl`
* `data/basket_telemetry.csv` — an intra-cycle snapshot every
  `TELEMETRY_INTERVAL_SECONDS` while a cycle is open: price, current / peak /
  give-back P/L, open and pending counts, net volume, ladder depth, triggers,
  the target in force, whether protection is active and the threshold it would
  close at. **This is the file the exit thresholds should be fitted on** — a
  cycle summary alone cannot show that a basket was +95 before it was −9. CSV
  only; none of it reaches Telegram.
* `data/entry_evaluations.csv` — one row per evaluated entry candle:
  `bar_time`, timeframe, bid/ask/spread, `accepted`, the `reason`, the cooldown
  left, live position and pending counts, `risk_ok`, `spread_ok` and the id the
  next cycle would take. Rejected candles are recorded in full — the file is
  there so a candle the bot *did not* trade is as auditable as one it did.
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
python replay.py --csv g.csv --spacing 0.20 --target 3.00
python replay.py --csv g.csv --entry-timeframe M5 --max-depth 12 \
                 --activation 4 --trail 2 --floor 1.25 --cycle-drawdown 25
python replay.py --csv g.csv --no-entry-gate      # the pre-M1 behaviour
```

Backtest flags: `--entry-timeframe`, `--no-entry-gate`, `--max-depth`
(`MAX_LADDER_DEPTH`), `--target` (`BASKET_PROFIT_TARGET`), `--activation`,
`--trail`, `--floor`, `--no-runner`, `--cycle-drawdown`
(`MAX_CYCLE_DRAWDOWN`), plus `--spacing`, `--depth`, `--lot`, `--spread`,
`--commission`, `--steps`, `--optimistic` and `--balance`.

The replay drives the **real** engine and the **real** paper broker, so what is
measured is the shipped strategy. Bars are fed one at a time and walked as a
price path (open → adverse extreme → other extreme → close, by default), the
engine only ever sees prices up to the current step, and the intrabar ordering
is derived from the current bar alone — no look-ahead. Spread, commission,
slippage and the broker's minimum stop distance are all applied, and simulated
time drives cooldowns, order ages and trigger gaps. The summary prints its own
assumptions, because at this timescale they matter more than the strategy.

The entry gate is replayed too: during bar *i* the most recently closed bar is
*i-1*, so a cycle can only start once per closed replay bar, exactly as live.
**The replay bar IS the entry candle** — `ENTRY_TIMEFRAME` is not resampled, so
replaying M5 data gates entries on M5 closes whatever `ENTRY_TIMEFRAME` says.
The summary states this rather than hiding it.

When fitting parameters, split the data: development → validation → a holdout
you only touch once.

## Tests

```bash
python tests/run_all.py        # 971 checks, no MT5 or network needed
```

Covers: the ladder lifecycle (exactly 11+11, no second ladder while one is
active, triggers and closes that must not re-ladder, no replenishment, the
10-second cooldown, exactly one new ladder after it with a new id, never two
ladder ids live at once, restart recovery, a short deployment that is not
compensated); the basket profit target at 1.99 / 2.00 / 2.01 and fluctuating around
it (one exit, never repeated); peak tracking that never decreases and resets
per cycle; drawdown-from-peak; the trail closing a protected basket and the
floor catching one the trail would let bleed out; the +95 → −9 sequence exiting
during the retracement; a never-profitable basket still hitting the emergency
drawdown; telemetry snapshots at the configured interval and never in Telegram; no individual TP on any order or position; the
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

`BASKET_PROFIT_TARGET`, `PROFIT_PROTECTION_ACTIVATION`,
`PROFIT_PROTECTION_TRAIL`, `MIN_PROTECTED_PROFIT`, the ladder spacing, depth
and first-level offset, the drawdown and duration limits and the cooldown are
**starting values**, not results. The trail in particular is a guess: fit it on
`basket_telemetry.csv`, which exists for exactly that. This version is deliberately simple and deterministic so it can be
measured; fit it with `replay.py` on real XAUUSD M5 history, and set
`COMMISSION_PER_LOT` to your broker's real figure before drawing any conclusion.

**A working implementation is not a profitable strategy.** Nothing here has been
shown to make money.

> Ladder scalping on gold is high risk. Start in PAPER, and keep the risk limits
> switched on.
