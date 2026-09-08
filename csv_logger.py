"""
Thread-safe CSV persistence layer.

Three files live inside DATA_DIRECTORY (created automatically with headers):

    trades.csv             one row per open / partial close / close
    events.csv             meaningful bot events only (never per poll cycle)
    account_snapshots.csv  periodic account state

Row building and the in-memory bookkeeping (de-duplication keys) happen on the
caller's thread under a lock; the actual disk write is handed to a single
background writer thread. The trading engine therefore never waits on disk -
an exit that has to close a basket is not delayed behind a telemetry row - and
because one thread owns every append, files still cannot be interleaved or
corrupted.

Anything that READS a file flushes the queue first, so a reader never sees a
stale file while rows are still in flight.
"""

import csv
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

TRADE_HEADER = [
    "timestamp",
    "symbol",
    "ticket",
    "direction",
    "volume",
    "entry_price",
    "stop_loss",
    "take_profit",
    "close_price",
    "profit",
    "commission",
    "swap",
    "magic",
    "reason",
    "deal_id",
    # ladder context for later analysis
    "cycle_id",
    "level",
    "spread",
]

EVENT_HEADER = [
    "timestamp",
    "event_type",
    "message",
    "symbol",
    "ticket",
    "status",
]

# One row per ladder event, with the state of the basket at that moment. This
# is the data a future exit rule would be fitted against - it records what
# happened, not a score.
LADDER_HEADER = [
    "timestamp", "symbol", "cycle_id", "candle_time", "event_type", "side",
    "entry_price", "exit_price", "ladder_price", "ladder_index",
    "sl_if_used", "lot_size", "spread", "order_ticket", "position_ticket",
    "buy_pending_count", "sell_pending_count", "open_positions",
    "closed_positions",
    "buy_trigger_count", "sell_trigger_count", "last_side", "previous_side",
    "direction_changes", "ladder_depth_used", "price_distance_traveled",
    "net_levels", "basket_floating_pnl", "basket_realized_pnl", "basket_pnl",
    "basket_drawdown", "basket_profit_target", "profit", "cycle_profit",
    "daily_profit", "action", "reason",
]

# One row per completed cycle: how it ran and why it ended.
CYCLE_HEADER = [
    "timestamp", "symbol", "cycle_id", "started_at", "duration_seconds",
    "anchor", "initial_price", "exit_price", "exit_spread", "spacing",
    "triggers", "buy_triggers", "sell_triggers",
    "direction_changes", "ladder_depth_used", "net_levels",
    "path_levels", "basket_profit_target",
    "positions_at_exit", "open_buys_at_exit", "open_sells_at_exit",
    "pending_orders_at_exit",
    # floating before the close vs what MT5 actually realized after it
    "floating_pnl_at_exit", "floating_pnl_before_close",
    "realized_pnl_after_close", "pnl_slippage",
    "realized_pnl", "final_realized_pnl", "peak_pnl", "drawdown",
    # how the profit-management state machine ran this cycle
    "max_floating_profit", "max_floating_loss", "max_drawdown",
    "profit_giveback", "time_to_peak", "time_to_profit_target",
    "time_in_profit", "time_in_protection", "protection_active",
    "cycle_state", "exit_reason", "max_ladder_depth_reached", "total_triggers",
    "entry_bar_time", "entry_timeframe",
    # --- what the basket had been through when it was taken (PHASE 10-14) ---
    "lowest_pnl", "recovery_state", "was_underwater", "recovery_amount",
    "time_underwater", "time_of_max_drawdown", "price_state",
    "price_vs_anchor", "price_vs_average_entry",
    # --- EXIT LATENCY (PHASE 17): where the time actually went -------------
    "target_crossed_at", "target_detected_at", "exit_state_entered_at",
    "close_request_started_at", "close_request_completed_at",
    "pending_cancel_started_at", "pending_cancel_completed_at",
    "flat_verification_started_at", "fully_flat_at",
    "detection_latency_ms", "decision_to_close_request_ms",
    "close_request_latency_ms", "pending_cancel_latency_ms",
    "flat_verification_latency_ms", "total_exit_latency_ms",
    "positions_closed", "pending_cancelled", "close_failures",
    "cancel_failures", "close_attempts",
    "basket_pnl_at_target_detection", "basket_pnl_at_close_request",
    # --- LADDER PLACEMENT LATENCY + GEOMETRY -------------------------------
    "ladder_first_order_ms", "ladder_complete_ms", "ladder_orders_placed",
    "placement_start_timestamp", "placement_end_timestamp",
    "placement_duration_ms", "reference_price",
    "intended_first_buy", "actual_first_buy",
    "intended_first_sell", "actual_first_sell",
    "first_buy_distance", "first_sell_distance",
    "buy_spacing", "sell_spacing", "average_buy_spacing", "average_sell_spacing",
    "price_at_first_order", "price_at_last_order", "levels_skipped",
    # --- exposure and recovery, as the cycle ended -------------------------
    "max_ladder_depth", "ladder_state", "depth_zone", "deep_ladder_state",
    "risk_score", "risk_state", "expansion_allowed", "expansion_block_reason",
    "maximum_direction_imbalance",
    "direction_imbalance", "imbalance_state", "net_direction",
    "gross_volume", "net_volume",
    "recovery_quality", "recovery_count", "weak_recovery_count",
    "strong_recovery_count", "recovery_duration", "recovery_speed",
    "ladder_depth_at_recovery_start", "exit_state",
    "end_kind", "end_reason", "daily_profit",
]

# One row every TELEMETRY_INTERVAL_SECONDS while a cycle is open. This is the
# intra-cycle record the exit rules are meant to be optimised against: the
# cycle summary alone cannot show that a basket was +95 before it was -9.
TELEMETRY_HEADER = [
    "timestamp", "symbol", "cycle_id", "elapsed_seconds",
    "bid", "ask", "spread",
    "current_pnl", "floating_pnl", "peak_pnl", "drawdown_from_peak",
    "realized_pnl",
    "open_positions", "open_buys", "open_sells", "pending_orders",
    "net_volume", "ladder_depth", "triggers", "total_triggers",
    "buy_triggers", "sell_triggers",
    "direction_changes", "basket_profit_target", "protection_active",
    "protection_activation", "protection_trail", "protection_threshold",
    "m1_bar_time", "cycle_state", "time_in_profit",
    # --- what the basket has been through, not just what it is worth now ---
    "lowest_pnl", "recovery_state", "time_underwater", "time_since_peak",
    "recovery_amount", "recovery_speed",
    # --- where price is, relative to the things the basket cares about ---
    "price_state", "price_vs_anchor", "price_vs_average_entry",
    "basket_average_entry", "recent_price_change", "price_velocity",
    "favorable_price_movement", "adverse_price_movement",
    # --- exposure: depth alone cannot tell BUY 6/SELL 6 from BUY 11/SELL 1 ---
    "pending_buys", "pending_sells", "gross_volume", "net_direction",
    "direction_imbalance", "imbalance_state", "max_ladder_depth",
    "ladder_state", "exposure_capped",
    # --- deep-ladder health: depth controls exposure, health controls exit ---
    "depth_zone", "deep_ladder_state", "risk_score", "risk_state",
    "expansion_allowed", "expansion_block_reason",
    "buy_volume", "sell_volume", "max_floating_loss",
    # --- how well a basket that was underwater is climbing back -----------
    "recovery_quality", "recovery_start_pnl", "recovery_duration",
    "ladder_depth_at_recovery_start",
    # --- what the exit engine decided on this pass ------------------------
    "basket_state", "exit_decision", "exit_reason",
    "reference_price", "first_buy_distance", "first_sell_distance",
    "exit_in_progress",
]

# One row per entry evaluation - accepted or rejected. This is what makes it
# possible to compare the candles a cycle was started on against the ones it
# was not.
ENTRY_HEADER = [
    "timestamp", "bar_time", "timeframe", "symbol", "bid", "ask", "spread",
    "accepted", "reason", "cooldown_left", "open_positions", "pending_orders",
    "risk_ok", "spread_ok", "next_cycle_id",
]

ACCOUNT_HEADER = [
    "timestamp",
    "balance",
    "equity",
    "margin",
    "free_margin",
    "margin_level",
    "open_positions",
]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt(value, digits=None):
    """Format a value for CSV; None/'' stay empty so unknown fields are blank."""
    if value is None or value == "":
        return ""
    if digits is not None:
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


class AsyncWriter:
    """
    A single background thread that owns every CSV append.

    The trading thread must never wait on disk. Callers enqueue a
    (path, row) pair and return immediately; this thread does the open/write/
    close. One thread and one FIFO queue means rows keep the order they were
    produced in, per file and across files.

    A logging failure is reported and dropped - it never propagates into the
    trading path. Set `queue_limit` to bound memory; when the queue is full the
    OLDEST pending row is dropped rather than blocking a close request.
    """

    def __init__(self, queue_limit=10000):
        self._queue = queue.Queue(maxsize=queue_limit)
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self.dropped = 0
        self.written = 0
        self._thread = threading.Thread(target=self._run, name="csv-writer",
                                        daemon=True)
        self._thread.start()

    def submit(self, path, row):
        """Queue one row. Never blocks, never raises."""
        try:
            self._idle.clear()
            self._queue.put_nowait((path, row))
        except queue.Full:
            # Losing the oldest telemetry row is strictly better than making a
            # close request wait for a disk that is not keeping up.
            self.dropped += 1
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._queue.put_nowait((path, row))
            except queue.Empty:
                pass
            except queue.Full:
                pass
        except Exception as exc:
            self.dropped += 1
            print(f"[csv_logger] enqueue failed: {exc}")

    def _run(self):
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                self._idle.set()
                if self._stop.is_set():
                    return
                continue
            try:
                path, row = item
                with open(path, "a", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(row)
                self.written += 1
            except Exception as exc:                # logging never kills trading
                self.dropped += 1
                print(f"[csv_logger] write failed: {exc}")
            finally:
                self._queue.task_done()

    def flush(self, timeout=5.0):
        """Block until everything queued so far is on disk. Returns True if so."""
        end = time.time() + timeout
        while time.time() < end:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def stop(self, timeout=5.0):
        """Flush what is queued, then end the thread."""
        self.flush(timeout=timeout)
        self._stop.set()
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    @property
    def pending(self):
        return self._queue.unfinished_tasks


class CsvLogger:
    def __init__(self, data_dir, trade_file, event_file, account_file,
                 ladder_file="rolling_ladder_events.csv",
                 cycle_file="rolling_ladder_cycles.csv",
                 telemetry_file="basket_telemetry.csv",
                 entry_file="entry_evaluations.csv"):
        self.dir = Path(data_dir)
        self.trade_path = self.dir / trade_file
        self.event_path = self.dir / event_file
        self.account_path = self.dir / account_file
        self.ladder_path = self.dir / ladder_file
        self.cycle_path = self.dir / cycle_file
        self.telemetry_path = self.dir / telemetry_file
        self.entry_path = self.dir / entry_file

        self._lock = threading.Lock()
        # Keys of trade rows already written -> prevents duplicates across restarts
        self._trade_keys = set()
        # Every append goes to this thread; the caller never waits on disk.
        self._writer = AsyncWriter()

        self._bootstrap()

    # ------------------------------------------------------------------ setup
    def _bootstrap(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self._ensure_file(self.trade_path, TRADE_HEADER)
        self._ensure_file(self.event_path, EVENT_HEADER)
        self._ensure_file(self.account_path, ACCOUNT_HEADER)
        self._ensure_file(self.ladder_path, LADDER_HEADER)
        self._ensure_file(self.cycle_path, CYCLE_HEADER)
        self._ensure_file(self.telemetry_path, TELEMETRY_HEADER)
        self._ensure_file(self.entry_path, ENTRY_HEADER)
        self._load_trade_keys()

    @staticmethod
    def _ensure_file(path, header):
        """Create the file with its header if missing/empty. Never truncates."""
        if path.exists() and path.stat().st_size > 0:
            CsvLogger._migrate_header(path, header)
            return
        with open(path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(header)

    @staticmethod
    def _migrate_header(path, header):
        """
        Bring a file written by an older version up to the current columns.

        Existing rows are preserved and re-keyed by column name; new columns
        are left empty. Written to a temp file and swapped in atomically, so an
        interrupted migration cannot lose history.
        """
        try:
            with open(path, "r", newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                existing = next(reader, None)
                if existing is None or existing == header:
                    return
                rows = [dict(zip(existing, row)) for row in reader]
        except Exception as exc:
            print(f"[csv_logger] header check failed for {path}: {exc}")
            return

        try:
            tmp = path.with_suffix(".migrating")
            with open(tmp, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(header)
                for row in rows:
                    writer.writerow([row.get(col, "") for col in header])
            os.replace(tmp, path)
            added = [c for c in header if c not in existing]
            print(f"[csv_logger] {path.name}: added columns {added} "
                  f"({len(rows)} rows preserved)")
        except Exception as exc:
            print(f"[csv_logger] header migration failed for {path}: {exc}")

    def _load_trade_keys(self):
        """Rebuild the de-duplication index from the existing trades.csv."""
        try:
            with open(self.trade_path, "r", newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    key = self._key(row.get("ticket"), row.get("reason"),
                                    row.get("deal_id"))
                    if key:
                        self._trade_keys.add(key)
        except FileNotFoundError:
            pass
        except Exception as exc:  # never let a corrupt file stop the bot
            print(f"[csv_logger] could not index {self.trade_path}: {exc}")

    @staticmethod
    def _key(ticket, reason, deal_id):
        deal_id = "" if deal_id is None else str(deal_id).strip()
        if deal_id:
            return f"deal:{deal_id}"
        ticket = "" if ticket is None else str(ticket).strip()
        reason = "" if reason is None else str(reason).strip().upper()
        if not ticket:
            return None
        return f"pos:{ticket}:{reason}"

    # ------------------------------------------------------------ raw writing
    def _append(self, path, row):
        """Hand the row to the writer thread. Returns without touching disk."""
        self._writer.submit(path, row)

    def flush(self, timeout=5.0):
        """Wait for queued rows to reach disk. Used by readers and shutdown."""
        return self._writer.flush(timeout=timeout)

    def close(self, timeout=5.0):
        """Flush and stop the writer thread. Safe to call more than once."""
        return self._writer.stop(timeout=timeout)

    @property
    def pending_writes(self):
        return self._writer.pending

    # ---------------------------------------------------------------- events
    def log_event(self, event_type, message="", symbol="", ticket="", status="OK"):
        """Record a meaningful bot event. Never called per polling cycle."""
        try:
            self._append(self.event_path, [
                _now(),
                str(event_type),
                str(message).replace("\n", " ").strip(),
                _fmt(symbol),
                _fmt(ticket),
                _fmt(status),
            ])
        except Exception as exc:
            print(f"[csv_logger] event write failed: {exc}")

    # ---------------------------------------------------------------- trades
    def log_trade(self, symbol, ticket, direction, volume, reason,
                  entry_price=None, stop_loss=None, take_profit=None,
                  close_price=None, profit=None, commission=None, swap=None,
                  magic=None, deal_id=None, digits=2, cycle_id=None, level=None,
                  spread=None):
        """
        Append a trade row, skipping anything already recorded.

        Returns True when a row was written, False when it was a duplicate or
        the write failed.
        """
        key = self._key(ticket, reason, deal_id)
        try:
            with self._lock:
                if key and key in self._trade_keys:
                    return False
                row = [
                    _now(),
                    _fmt(symbol),
                    _fmt(ticket),
                    _fmt(direction),
                    _fmt(volume),
                    _fmt(entry_price, digits),
                    _fmt(stop_loss, digits),
                    _fmt(take_profit, digits),
                    _fmt(close_price, digits),
                    _fmt(profit, 2),
                    _fmt(commission, 2),
                    _fmt(swap, 2),
                    _fmt(magic),
                    _fmt(reason),
                    _fmt(deal_id),
                    _fmt(cycle_id),
                    _fmt(level),
                    _fmt(spread, digits),
                ]
                with open(self.trade_path, "a", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(row)
                if key:
                    self._trade_keys.add(key)
            return True
        except Exception as exc:
            print(f"[csv_logger] trade write failed: {exc}")
            return False

    def has_trade(self, ticket=None, reason=None, deal_id=None):
        key = self._key(ticket, reason, deal_id)
        if not key:
            return False
        with self._lock:
            return key in self._trade_keys

    # ------------------------------------------------------------ ladder log
    def log_ladder(self, event, digits=2, **fields):
        """
        One ladder event row. Unknown keys are ignored and missing ones stay
        empty, so callers pass whatever context they have.
        """
        fields["event_type"] = event
        fields.setdefault("timestamp", _now())
        price_keys = {"entry_price", "exit_price", "ladder_price",
                      "sl_if_used", "spread"}
        money_keys = {"profit", "cycle_profit", "daily_profit", "basket_pnl",
                      "basket_floating_pnl", "basket_realized_pnl",
                      "basket_drawdown", "basket_profit_target"}
        row = []
        for column in LADDER_HEADER:
            value = fields.get(column, "")
            if column in price_keys:
                row.append(_fmt(value, digits))
            elif column in money_keys:
                row.append(_fmt(value, 2))
            else:
                row.append(_fmt(value))
        try:
            self._append(self.ladder_path, row)
        except Exception as exc:
            print(f"[csv_logger] ladder write failed: {exc}")

    def log_cycle(self, digits=2, **fields):
        """One row per finished cycle."""
        fields.setdefault("timestamp", _now())
        row = []
        for column in CYCLE_HEADER:
            value = fields.get(column, "")
            if column in ("anchor", "spacing", "initial_price", "exit_price"):
                row.append(_fmt(value, digits))
            elif column in ("realized_pnl", "final_realized_pnl", "peak_pnl",
                            "drawdown", "floating_pnl_at_exit", "daily_profit",
                            "basket_profit_target", "max_floating_profit",
                            "max_floating_loss", "max_drawdown",
                            "profit_giveback", "floating_pnl_before_close",
                            "realized_pnl_after_close", "pnl_slippage"):
                row.append(_fmt(value, 2))
            else:
                row.append(_fmt(value))
        try:
            self._append(self.cycle_path, row)
        except Exception as exc:
            print(f"[csv_logger] cycle write failed: {exc}")

    def ladder_stats(self, day=None):
        """Today's ladder activity, straight from ladder.csv."""
        self.flush()          # rows may still be in the writer queue
        day = day or datetime.now().strftime("%Y-%m-%d")
        counts = {}
        realized = 0.0
        cycles = set()
        with self._lock:
            try:
                with open(self.ladder_path, "r", newline="", encoding="utf-8") as fh:
                    rows = list(csv.DictReader(fh))
            except FileNotFoundError:
                return {"available": False, "error": "ladder.csv missing"}
            except Exception as exc:
                return {"available": False, "error": str(exc)}
        for row in rows:
            if not (row.get("timestamp") or "").startswith(day):
                continue
            event = (row.get("event_type") or "").upper()
            counts[event] = counts.get(event, 0) + 1
            if row.get("cycle_id"):
                cycles.add(row["cycle_id"])
            if event in ("SL_HIT", "POSITION_CLOSED"):
                try:
                    realized += float(row.get("profit") or 0.0)
                except ValueError:
                    pass
        return {
            "available": True,
            "day": day,
            "events": counts,
            "sl_hits": counts.get("SL_HIT", 0),
            "legs_closed": counts.get("POSITION_CLOSED", 0),
            "entries": counts.get("ORDER_TRIGGERED", 0),
            "orders_placed": counts.get("ORDER_PLACED", 0),
            "cycles_completed": counts.get("CYCLE_COMPLETED", 0),
            "cycles_seen": len(cycles),
            "realized": realized,
        }

    # ------------------------------------------------------- account snapshot
    def log_telemetry(self, digits=2, **fields):
        """
        One intra-cycle basket snapshot. CSV only - never Telegram.

        Written straight through with no buffering: the point of this file is
        to survive whatever ends the cycle.
        """
        fields.setdefault("timestamp", _now())
        price_keys = {"bid", "ask", "spread"}
        money_keys = {"current_pnl", "peak_pnl", "drawdown_from_peak",
                      "realized_pnl", "basket_profit_target",
                      "protection_threshold"}
        row = []
        for column in TELEMETRY_HEADER:
            value = fields.get(column, "")
            if column in price_keys:
                row.append(_fmt(value, digits))
            elif column in money_keys:
                row.append(_fmt(value, 2))
            else:
                row.append(_fmt(value))
        try:
            self._append(self.telemetry_path, row)
        except Exception as exc:
            print(f"[csv_logger] telemetry write failed: {exc}")

    def log_entry_evaluation(self, digits=2, **fields):
        """One row per entry evaluation, accepted or rejected. CSV only."""
        fields.setdefault("timestamp", _now())
        row = []
        for column in ENTRY_HEADER:
            value = fields.get(column, "")
            if column in ("bid", "ask", "spread"):
                row.append(_fmt(value, digits))
            else:
                row.append(_fmt(value))
        try:
            self._append(self.entry_path, row)
        except Exception as exc:
            print(f"[csv_logger] entry write failed: {exc}")

    def log_account(self, balance, equity, margin, free_margin, margin_level,
                    open_positions):
        try:
            self._append(self.account_path, [
                _now(),
                _fmt(balance, 2),
                _fmt(equity, 2),
                _fmt(margin, 2),
                _fmt(free_margin, 2),
                _fmt(margin_level, 2),
                _fmt(open_positions),
            ])
        except Exception as exc:
            print(f"[csv_logger] account write failed: {exc}")

    # ----------------------------------------------------------------- stats
    def _read_trades(self):
        self.flush()          # a reader must never see a half-written history
        with self._lock:
            try:
                with open(self.trade_path, "r", newline="", encoding="utf-8") as fh:
                    return list(csv.DictReader(fh))
            except FileNotFoundError:
                return []
            except Exception as exc:
                print(f"[csv_logger] trade read failed: {exc}")
                return None

    def today_stats(self, day=None):
        """
        Statistics computed from the recorded trade history.

        Returns a dict, or {"available": False, ...} when the data cannot be
        read - statistics are never fabricated.
        """
        rows = self._read_trades()
        if rows is None:
            return {"available": False, "error": "trades.csv could not be read"}

        day = day or datetime.now().strftime("%Y-%m-%d")
        opened = 0
        closed = 0
        wins = 0
        losses = 0
        flat = 0
        realized = 0.0

        for row in rows:
            ts = (row.get("timestamp") or "")
            if not ts.startswith(day):
                continue
            reason = (row.get("reason") or "").upper()
            if reason == "OPEN":
                opened += 1
                continue
            if reason != "CLOSE":
                continue
            closed += 1
            try:
                pl = float(row.get("profit") or 0.0)
                pl += float(row.get("commission") or 0.0)
                pl += float(row.get("swap") or 0.0)
            except ValueError:
                pl = 0.0
            realized += pl
            if pl > 0:
                wins += 1
            elif pl < 0:
                losses += 1
            else:
                flat += 1

        win_rate = (wins / closed * 100.0) if closed else None
        return {
            "available": True,
            "day": day,
            "opened": opened,
            "closed": closed,
            "wins": wins,
            "losses": losses,
            "flat": flat,
            "win_rate": win_rate,
            "realized": realized,
        }

    def trades_opened_today(self, day=None):
        stats = self.today_stats(day=day)
        return stats.get("opened", 0) if stats.get("available") else 0
