"""
Thread-safe CSV persistence layer.

Three files live inside DATA_DIRECTORY (created automatically with headers):

    trades.csv             one row per open / partial close / close
    events.csv             meaningful bot events only (never per poll cycle)
    account_snapshots.csv  periodic account state

All writes go through a single threading.Lock so the trading engine, the
background monitor and the Telegram controller can never corrupt a file.
Appends are cheap (open -> write one row -> close) so the 0.5s trading loop is
never meaningfully delayed.
"""

import csv
import os
import threading
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
    "tp_mode",
    "tp_distance",
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

LADDER_HEADER = [
    "timestamp",
    "cycle_id",
    "ladder_id",
    "symbol",
    "direction",
    "level",
    "entry_price",
    "exit_price",
    "tp",
    "sl_if_used",
    "lot_size",
    "spread",
    "order_ticket",
    "position_ticket",
    "event",
    "profit",
    "cycle_profit",
    "daily_profit",
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


class CsvLogger:
    def __init__(self, data_dir, trade_file, event_file, account_file,
                 ladder_file="ladder.csv"):
        self.dir = Path(data_dir)
        self.trade_path = self.dir / trade_file
        self.event_path = self.dir / event_file
        self.account_path = self.dir / account_file
        self.ladder_path = self.dir / ladder_file

        self._lock = threading.Lock()
        # Keys of trade rows already written -> prevents duplicates across restarts
        self._trade_keys = set()

        self._bootstrap()

    # ------------------------------------------------------------------ setup
    def _bootstrap(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self._ensure_file(self.trade_path, TRADE_HEADER)
        self._ensure_file(self.event_path, EVENT_HEADER)
        self._ensure_file(self.account_path, ACCOUNT_HEADER)
        self._ensure_file(self.ladder_path, LADDER_HEADER)
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
        with self._lock:
            with open(path, "a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(row)

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
                  tp_mode=None, tp_distance=None, spread=None):
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
                    _fmt(tp_mode),
                    _fmt(tp_distance, digits),
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
    def log_ladder(self, event, cycle_id="", ladder_id="", symbol="", direction="",
                   level="", entry_price=None, exit_price=None, tp=None,
                   sl_if_used=None, lot_size=None, spread=None, order_ticket="",
                   position_ticket="", profit=None, cycle_profit=None,
                   daily_profit=None, digits=2):
        """One row per ladder event (see LADDER_HEADER)."""
        try:
            self._append(self.ladder_path, [
                _now(),
                _fmt(cycle_id),
                _fmt(ladder_id),
                _fmt(symbol),
                _fmt(direction),
                _fmt(level),
                _fmt(entry_price, digits),
                _fmt(exit_price, digits),
                _fmt(tp, digits),
                _fmt(sl_if_used, digits),
                _fmt(lot_size),
                _fmt(spread, digits),
                _fmt(order_ticket),
                _fmt(position_ticket),
                _fmt(event),
                _fmt(profit, 2),
                _fmt(cycle_profit, 2),
                _fmt(daily_profit, 2),
            ])
        except Exception as exc:
            print(f"[csv_logger] ladder write failed: {exc}")

    def ladder_stats(self, day=None):
        """Today's ladder activity, straight from ladder.csv."""
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
            event = (row.get("event") or "").upper()
            counts[event] = counts.get(event, 0) + 1
            if row.get("cycle_id"):
                cycles.add(row["cycle_id"])
            if event in ("TP_HIT", "SL_HIT", "POSITION_CLOSED"):
                try:
                    realized += float(row.get("profit") or 0.0)
                except ValueError:
                    pass
        return {
            "available": True,
            "day": day,
            "events": counts,
            "tp_hits": counts.get("TP_HIT", 0),
            "sl_hits": counts.get("SL_HIT", 0),
            "entries": counts.get("ORDER_TRIGGERED", 0),
            "orders_placed": counts.get("ORDER_PLACED", 0),
            "cycles_completed": counts.get("CYCLE_COMPLETED", 0),
            "cycles_seen": len(cycles),
            "realized": realized,
        }

    # ------------------------------------------------------- account snapshot
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
