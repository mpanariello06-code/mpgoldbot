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
]

EVENT_HEADER = [
    "timestamp",
    "event_type",
    "message",
    "symbol",
    "ticket",
    "status",
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
    def __init__(self, data_dir, trade_file, event_file, account_file):
        self.dir = Path(data_dir)
        self.trade_path = self.dir / trade_file
        self.event_path = self.dir / event_file
        self.account_path = self.dir / account_file

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
        self._load_trade_keys()

    @staticmethod
    def _ensure_file(path, header):
        """Create the file with its header if missing/empty. Never truncates."""
        if path.exists() and path.stat().st_size > 0:
            return
        with open(path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(header)

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
                  magic=None, deal_id=None, digits=2):
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
        breakeven = 0
        realized = 0.0

        for row in rows:
            ts = (row.get("timestamp") or "")
            if not ts.startswith(day):
                continue
            reason = (row.get("reason") or "").upper()
            if reason == "OPEN":
                opened += 1
                continue
            if reason not in ("CLOSE", "PARTIAL_CLOSE"):
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
                breakeven += 1

        win_rate = (wins / closed * 100.0) if closed else None
        return {
            "available": True,
            "day": day,
            "opened": opened,
            "closed": closed,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": win_rate,
            "realized": realized,
        }

    def trades_opened_today(self, day=None):
        stats = self.today_stats(day=day)
        return stats.get("opened", 0) if stats.get("available") else 0
