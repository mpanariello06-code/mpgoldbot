"""
Telegram settings panel for the rolling ladder.

Owns every ⚙️ SETTINGS screen: TP, lot size, ladder geometry, profit cycle,
risk limits and the direction filter. High-impact changes ask for confirmation;
CUSTOM values are typed and validated before anything is applied.

Callback vocabulary
-------------------
    settings                 root settings screen
    settings_<menu>          a submenu (tp, lot, ladder, risk, spacing, depth,
                             cycle, spread, open, pending, daily, cycleloss,
                             direction, pip, sl, offset, roll, age, cooldown,
                             streak, maxlot, basket)
    confirm:<key>:<value>    confirmation screen for a high-impact change
    apply:<key>:<value>      apply a validated change
    custom:<key>             ask for a typed value
    settings_cancel:<menu>   leave without changing anything

Every screen renders live values, so what you see is what the next ladder level
will use. Changes never modify orders or positions that already exist.
"""

import threading

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from runtime_settings import (
    DIRECTION_LABELS,
    ROLL_MODE_LABELS,
    TP_MODE_LABELS,
    RuntimeSettings,
    SettingError,
)

BACK = "🔙 BACK"


def _btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def _rows(*rows):
    return InlineKeyboardMarkup([list(r) for r in rows if r])


class SettingsPanel:
    """Renders the settings menus and applies changes to RuntimeSettings."""

    KEY_MENU = {
        "tp_mode": "tp", "tp_levels": "tp", "tp_distance": "tp",
        "stop_loss_distance": "sl",
        "pip_points": "pip",
        "lot_size": "lot", "max_lot_size": "maxlot",
        "ladder_spacing": "spacing", "ladder_depth": "depth",
        "first_level_offset": "offset", "roll_mode": "roll",
        "rearm_levels": "roll", "m5_candle_reset": "roll",
        "cycle_close_positions": "cycle",
        "max_open_positions": "open", "max_pending_orders": "pending",
        "max_spread": "spread", "max_slippage": "risk",
        "max_ladder_depth": "maxdepth",
        "max_daily_drawdown": "daily", "max_cycle_drawdown": "cycleloss",
        "max_consecutive_losing_cycles": "streak",
        "cooldown_after_loss_minutes": "cooldown",
        "order_max_age_seconds": "age",
        "direction_filter": "direction", "timeframe": "settings",
        "telegram_status_updates": "notify", "telegram_state_alerts": "notify",
        "telegram_entry_alerts": "notify",
        "telegram_status_interval_minutes": "notify",
        "telegram_error_throttle_seconds": "notify",
        "exit_threshold_exit": "exit", "exit_threshold_monitor": "exit",
        "exit_w_reversal": "exitweights", "exit_w_exhaustion": "exitweights",
        "exit_w_continuation": "exitweights", "exit_w_depth": "exitweights",
        "exit_w_drawdown": "exitweights", "exit_w_harvest": "exitweights",
        "exit_w_loss_hold": "exitweights",
    }

    PROMPTS = {
        "tp_levels": "Send the TP size in ladder levels (1 = the next rung).",
        "tp_distance": "Send the TP distance in price units (e.g. 0.30).",
        "stop_loss_distance": "Send the SL distance in price units (0 = no stop loss).",
        "pip_points": "Send how many points make 1 pip (0 = auto-detect).",
        "lot_size": "Send the fixed lot size (e.g. 0.01).",
        "max_lot_size": "Send the maximum allowed lot size (e.g. 0.10).",
        "ladder_spacing": "Send the ladder spacing in price units (e.g. 0.30).",
        "ladder_depth": "Send the number of levels per side (1 - 50).",
        "first_level_offset": "Send the distance from price to the first level "
                              "(price units). The broker minimum always wins.",
        "max_ladder_depth": "Send the maximum ladder depth used per cycle.",
        "telegram_status_interval_minutes": "Send how often the periodic status "
                                            "should be posted, in minutes.",
        "telegram_error_throttle_seconds": "Send how long an identical error is "
                                           "suppressed for, in seconds.",
        "exit_threshold_exit": "Send the exit score that closes a cycle (1-100).",
        "exit_threshold_monitor": "Send the score where the cycle moves to "
                                  "MONITOR (below the exit score).",
        "exit_w_reversal": "Weight for the reversal reading (0 removes it).",
        "exit_w_exhaustion": "Weight for the exhaustion reading (0 removes it).",
        "exit_w_continuation": "How strongly momentum holds a cycle open.",
        "exit_w_depth": "Weight for how far the ladder has extended.",
        "exit_w_drawdown": "Weight for give-back from the cycle peak.",
        "exit_w_harvest": "How much banked profit sharpens an exit signal.",
        "exit_w_loss_hold": "How strongly an open loss holds a cycle open.",
        "max_open_positions": "Send the maximum open positions (1 - 200).",
        "max_pending_orders": "Send the maximum pending orders (1 - 200).",
        "max_spread": "Send the maximum spread in price units (e.g. 0.50, 0 = off).",
        "max_daily_drawdown": "Send the daily drawdown limit in account currency "
                              "(0 = off).",
        "max_cycle_drawdown": "Send the cycle drawdown limit in account currency "
                              "(0 = off).",
        "max_consecutive_losing_cycles": "Send how many losing cycles in a row stop "
                                         "the bot (0 = off).",
        "cooldown_after_loss_minutes": "Send the cooldown after a losing cycle, in "
                                       "minutes (0 = off).",
        "order_max_age_seconds": "Send the maximum pending order age in seconds "
                                 "(0 = never expire).",
        "max_slippage": "Send the maximum slippage/deviation in points.",
    }

    def __init__(self, engine, settings, csv_logger=None):
        self.engine = engine
        self.settings = settings
        self.csv = csv_logger
        self._pending_lock = threading.Lock()
        self._pending = {}

    # ================================================================ routing
    def handles(self, data):
        if self.settings is None:
            return False
        return data.startswith(("settings", "apply:", "confirm:", "custom:"))

    def render(self, data, chat_id=None):
        try:
            if data.startswith("apply:"):
                return self._apply(data[len("apply:"):])
            if data.startswith("confirm:"):
                return self._confirm(data[len("confirm:"):])
            if data.startswith("custom:"):
                return self._ask_custom(data[len("custom:"):], chat_id)
            if data.startswith("settings_cancel:"):
                self._clear_pending(chat_id)
                return self.menu(data.split(":", 1)[1],
                                 banner="❌ Cancelled - nothing changed.")
            if data == "settings":
                self._clear_pending(chat_id)
                return self.menu("main")
            if data.startswith("settings_"):
                return self.menu(data[len("settings_"):])
        except SettingError as exc:
            return self.menu("main", banner=f"⚠️ {exc}")
        except Exception as exc:
            return self.menu("main", banner=f"⚠️ Settings error: {exc}")
        return self.menu("main")

    # ============================================================ text input
    def _clear_pending(self, chat_id):
        if chat_id is None:
            return
        with self._pending_lock:
            self._pending.pop(chat_id, None)

    def awaiting_input(self, chat_id):
        with self._pending_lock:
            return self._pending.get(chat_id)

    def _ask_custom(self, key, chat_id):
        if key not in self.PROMPTS:
            return self.menu("main", banner="⚠️ That value cannot be typed in.")
        with self._pending_lock:
            self._pending[chat_id] = key
        menu = self.KEY_MENU.get(key, "main")
        text = "\n".join([
            f"✏️ <b>CUSTOM {RuntimeSettings.label(key).upper()}</b>",
            "",
            f"Current: {RuntimeSettings.display(key, self.settings.get(key))}",
            "",
            self.PROMPTS[key],
            "",
            "Send the value as a normal message.",
        ])
        return text, _rows([_btn("❌ CANCEL", f"settings_cancel:{menu}")])

    def handle_text(self, chat_id, text):
        key = self.awaiting_input(chat_id)
        if not key:
            return None
        raw = (text or "").strip().replace("$", "").replace(",", ".")
        try:
            value = self.settings.precheck(key, raw)
        except SettingError as exc:
            menu = self.KEY_MENU.get(key, "main")
            return ("\n".join([
                "⚠️ <b>INVALID VALUE</b>", "", str(exc), "",
                self.PROMPTS.get(key, ""), "",
                "Send another value, or cancel.",
            ]), _rows([_btn("❌ CANCEL", f"settings_cancel:{menu}")]))

        self._clear_pending(chat_id)
        if RuntimeSettings.needs_confirmation(key):
            return self._confirm(f"{key}:{value}")
        return self._apply(f"{key}:{value}")

    # ============================================================== applying
    def _confirm(self, payload):
        key, _, value = payload.partition(":")
        if key == "reset":
            d = self.settings.defaults()
            return ("\n".join([
                "⚠️ <b>CONFIRM RESET</b>", "",
                "Restore the original configuration?", "",
                f"Spacing {d['ladder_spacing']} × depth {d['ladder_depth']}",
                f"TP {d['tp_mode']} {d['tp_distance']} | Lot {d['lot_size']}",
                f"Max {d['max_open_positions']} pos / "
                f"{d['max_pending_orders']} pend / depth {d['max_ladder_depth']}",
                f"Exit score {d['exit_threshold_exit']:g} | "
                f"Spread {d['max_spread']} | Daily {d['max_daily_drawdown']}",
                "", "Open positions and orders are not touched.",
            ]), _rows([_btn("✅ CONFIRM", "apply:reset:1")],
                      [_btn("❌ CANCEL", "settings_cancel:main")]))

        coerced = self.settings.precheck(key, value)
        old_display = RuntimeSettings.display(key, self.settings.get(key))
        new_display = RuntimeSettings.display(key, coerced)
        menu = self.KEY_MENU.get(key, "main")
        text = "\n".join([
            "⚠️ <b>CONFIRM CHANGE</b>", "",
            f"Change {RuntimeSettings.label(key)}:", "",
            f"<b>{old_display} → {new_display}</b>", "",
            "This affects NEW ladder levels only.",
            "Open positions and live orders are not modified.",
        ])
        return text, _rows([_btn("✅ CONFIRM", f"apply:{key}:{value}")],
                           [_btn("❌ CANCEL", f"settings_cancel:{menu}")])

    def _apply(self, payload):
        key, _, value = payload.partition(":")
        if key == "tp_levels":
            self.settings.set("tp_mode", "levels")
        if key == "reset":
            changed = self.settings.reset()
            return self.menu("main", banner=f"♻️ Settings reset to the original "
                                            f"configuration ({len(changed)} changed).")
        changed, message, _old, _new = self.settings.set(key, value)
        banner = ("✅ " if changed else "ℹ️ ") + message
        return self.menu(self.KEY_MENU.get(key, "main"), banner=banner)

    # ================================================================= menus
    def menu(self, name, banner=""):
        builder = getattr(self, f"_menu_{name}", None) or self._menu_main
        text, markup = builder()
        if banner:
            text = f"{banner}\n\n{text}"
        return text, markup

    @staticmethod
    def _mark(active):
        return "✅ " if active else ""

    def _d(self, key):
        return RuntimeSettings.display(key, self.settings.get(key))

    def _pip_line(self):
        snap = self.settings.snapshot()
        try:
            info = self.engine.symbol_info_live()
            if info is not None:
                from price_utils import SymbolSpec
                spec = SymbolSpec.from_mt5(info, snap["pip_points"])
                return (f"1 pip = {spec.pip_size:g} ({spec.points_per_pip} points, "
                        f"{'pinned' if snap['pip_points'] else 'auto'})")
        except Exception:
            pass
        return ("1 pip = auto-detected from the symbol" if not snap["pip_points"]
                else f"1 pip = {snap['pip_points']} points (pinned)")

    def _tp_line(self):
        snap = self.settings.snapshot()
        if snap["tp_mode"] == "levels":
            n = snap["tp_levels"]
            return (f"{n} level{'s' if n > 1 else ''} "
                    f"({n * snap['ladder_spacing']:g})")
        if snap["tp_mode"] == "distance":
            return f"{snap['tp_distance']:g} price units"
        return f"{TP_MODE_LABELS[snap['tp_mode']]} ({self._pip_line().split('=')[1].strip()})"

    # ---- root -------------------------------------------------------------
    def _menu_main(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "⚙️ <b>CURRENT SETTINGS</b>", "",
            f"Symbol: {getattr(self.engine, 'symbol', '')}  ({s['timeframe']})",
            "",
            f"🎯 TP: {self._tp_line()}",
            f"🛑 SL: {self._d('stop_loss_distance')}",
            f"📐 {self._pip_line()}",
            "",
            f"🪜 Spacing: {self._d('ladder_spacing')}",
            f"🪜 Depth: {s['ladder_depth']} levels per side",
            f"🪜 First level: {self._d('first_level_offset')}",
            f"🪜 Roll mode: {ROLL_MODE_LABELS[s['roll_mode']]}",
            "",
            f"💰 Lot: {s['lot_size']} (max {s['max_lot_size']})",
            f"⚖️ Exit at score: {s['exit_threshold_exit']:g} "
            f"(monitor {s['exit_threshold_monitor']:g})",
            f"🔁 Close on cycle end: {'ON' if s['cycle_close_positions'] else 'OFF'}",
            "",
            f"🛡 Max open: {s['max_open_positions']} | "
            f"Max pending: {s['max_pending_orders']}",
            f"🛡 Max spread: {self._d('max_spread')}",
            f"🛡 Daily drawdown: {self._d('max_daily_drawdown')} | "
            f"Cycle drawdown: {self._d('max_cycle_drawdown')}",
            "",
            f"🧭 Direction: {DIRECTION_LABELS[s['direction_filter']]}",
            "🔔 Telegram: cycle events"
            + (f" + status every {s['telegram_status_interval_minutes']:g}m"
               if s["telegram_status_updates"] else " only"),
            "",
            "<i>Changes apply to NEW levels only.</i>",
        ])
        markup = _rows(
            [_btn("🎯 TP SETTINGS", "settings_tp"), _btn("💰 LOT SIZE", "settings_lot")],
            [_btn("🪜 LADDER SETTINGS", "settings_ladder"),
             _btn("🛡 RISK SETTINGS", "settings_risk")],
            [_btn("⚖️ EXIT ENGINE", "settings_exit"),
             _btn("🧭 DIRECTION", "settings_direction")],
            [_btn("🔔 NOTIFICATIONS", "settings_notify"),
             _btn("♻️ RESET SETTINGS", "confirm:reset:1")],
            [_btn("🔙 MAIN MENU", "panel")],
        )
        return text, markup

    # ---- TP ---------------------------------------------------------------
    def _menu_tp(self):
        s = self.settings.snapshot()
        mode = s["tp_mode"]
        levels = s["tp_levels"]
        text = "\n".join([
            "🎯 <b>TP SETTINGS</b>", "",
            f"Current: {self._tp_line()}",
            f"Ladder spacing: {self._d('ladder_spacing')}",
            f"{self._pip_line()}", "",
            "A TP of 1 level targets the next rung of the ladder. Pip and",
            "absolute-distance modes are there for testing other targets.",
        ])
        level_buttons = [
            _btn(f"{self._mark(mode == 'levels' and levels == n)}{n} LEVEL"
                 f"{'S' if n > 1 else ''}", f"confirm:tp_levels:{n}")
            for n in (1, 2, 3, 4, 5)
        ]
        return text, _rows(
            level_buttons[:3], level_buttons[3:],
            [_btn(f"{self._mark(mode == 'levels')}MODE: LEVELS",
                  "confirm:tp_mode:levels"),
             _btn(f"{self._mark(mode == 'distance')}MODE: DISTANCE",
                  "confirm:tp_mode:distance")],
            [_btn("✏️ SET DISTANCE", "custom:tp_distance"),
             _btn("📐 PIP SIZE", "settings_pip")],
            [_btn("🛑 STOP LOSS", "settings_sl")],
            [_btn(BACK, "settings")],
        )

    def _menu_sl(self):
        current = self.settings.get("stop_loss_distance")
        text = "\n".join([
            "🛑 <b>STOP LOSS</b>", "",
            f"Current: {self._d('stop_loss_distance')}", "",
            "Distance in price units attached to every ladder order.",
            "OFF means positions are managed by TP and the cycle rules only.",
        ])
        row = [_btn(f"{self._mark(current == v)}"
                    f"{'OFF' if v == 0 else f'{v:g}'}", f"confirm:stop_loss_distance:{v:g}")
               for v in (0, 0.5, 1.0, 2.0)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:stop_loss_distance")],
                           [_btn(BACK, "settings_tp")])

    def _menu_pip(self):
        current = self.settings.get("pip_points")
        text = "\n".join([
            "📐 <b>PIP SIZE</b>", "",
            f"Current: {'AUTO' if not current else str(current) + ' points'}",
            f"{self._pip_line()}", "",
            "AUTO derives the pip from the symbol's digits and point size.",
            "Pin it if your broker quotes gold on a different convention.",
        ])
        row = [_btn(f"{self._mark(current == v)}"
                    f"{'AUTO' if v == 0 else str(v) + ' pts'}", f"apply:pip_points:{v}")
               for v in (0, 1, 10, 100)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:pip_points")],
                           [_btn(BACK, "settings_tp")])

    # ---- lot --------------------------------------------------------------
    def _menu_lot(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "💰 <b>LOT SIZE</b>", "",
            f"Current: {s['lot_size']}",
            f"Hard cap: {s['max_lot_size']}", "",
            "Fixed lots on every level - no martingale, no size increase after",
            "a loss. The volume is validated against the symbol's",
            "volume_min / volume_max and snapped to volume_step.",
        ])
        row = [_btn(f"{self._mark(s['lot_size'] == v)}{v:g}", f"confirm:lot_size:{v:g}")
               for v in (0.01, 0.02, 0.05, 0.10)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:lot_size"),
                            _btn("🔒 MAX LOT", "settings_maxlot")],
                           [_btn(BACK, "settings")])

    def _menu_maxlot(self):
        current = self.settings.get("max_lot_size")
        text = "\n".join([
            "🔒 <b>MAX LOT</b>", "",
            f"Current: {current}", "",
            "Upper bound the engine will never exceed, whatever the lot",
            "setting says.",
        ])
        row = [_btn(f"{self._mark(current == v)}{v:g}", f"confirm:max_lot_size:{v:g}")
               for v in (0.05, 0.10, 0.50, 1.0)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:max_lot_size")],
                           [_btn(BACK, "settings_lot")])

    # ---- ladder -----------------------------------------------------------
    def _menu_ladder(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "🪜 <b>LADDER SETTINGS</b>", "",
            f"Spacing: {self._d('ladder_spacing')}",
            f"Depth: {s['ladder_depth']} levels per side",
            f"First level: {self._d('first_level_offset')} from price",
            f"Roll mode: {ROLL_MODE_LABELS[s['roll_mode']]}",
            f"Exit at score: {s['exit_threshold_exit']:g}", "",
            "BUY STOPs sit above the market, SELL STOPs below, on a grid",
            "anchored when the cycle started.",
        ])
        return text, _rows(
            [_btn("📏 SPACING", "settings_spacing"), _btn("🔢 DEPTH", "settings_depth")],
            [_btn("🔁 PROFIT CYCLE", "settings_cycle"),
             _btn("↔️ FIRST LEVEL", "settings_offset")],
            [_btn("🔄 ROLL MODE", "settings_roll")],
            [_btn(BACK, "settings")],
        )

    def _menu_spacing(self):
        current = self.settings.get("ladder_spacing")
        text = "\n".join([
            "📏 <b>LADDER SPACING</b>", "",
            f"Current: {current:g} price units", "",
            "Distance between neighbouring ladder levels.",
        ])
        row = [_btn(f"{self._mark(current == v)}{v:g}", f"confirm:ladder_spacing:{v:g}")
               for v in (0.10, 0.20, 0.30, 0.40, 0.50)]
        return text, _rows(row[:3], row[3:],
                           [_btn("✏️ CUSTOM", "custom:ladder_spacing")],
                           [_btn(BACK, "settings_ladder")])

    def _menu_depth(self):
        current = self.settings.get("ladder_depth")
        text = "\n".join([
            "🔢 <b>LADDER DEPTH</b>", "",
            f"Current: {current} levels per side", "",
            "How many pending levels are kept live on each side.",
            f"Max pending orders: {self.settings.get('max_pending_orders')}",
        ])
        row = [_btn(f"{self._mark(current == v)}{v}", f"confirm:ladder_depth:{v}")
               for v in (3, 5, 8, 10)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:ladder_depth")],
                           [_btn(BACK, "settings_ladder")])

    def _menu_offset(self):
        current = self.settings.get("first_level_offset")
        spacing = self.settings.get("ladder_spacing")
        text = "\n".join([
            "↔️ <b>FIRST LEVEL OFFSET</b>", "",
            f"Current: {current:g}", "",
            "Distance from the market to the nearest level. The broker's own",
            "minimum stop distance is always respected on top of this.",
        ])
        opts = (spacing, spacing * 2, spacing * 3)
        row = [_btn(f"{self._mark(abs(current - v) < 1e-9)}{v:g}",
                    f"apply:first_level_offset:{v:g}") for v in opts]
        return text, _rows(row, [_btn("✏️ CUSTOM", "custom:first_level_offset")],
                           [_btn(BACK, "settings_ladder")])

    def _menu_roll(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "🔄 <b>ROLL MODE</b>", "",
            f"Current: {ROLL_MODE_LABELS[s['roll_mode']]}",
            f"Re-arm levels: {'ON' if s['rearm_levels'] else 'OFF'}",
            f"Candle re-anchor: {'ON' if s['m5_candle_reset'] else 'OFF'}", "",
            "ROLLING keeps a full ladder ahead of price: as levels trigger,",
            "new ones are created further out.",
            "STATIC GRID pins the grid where the cycle started and lets price",
            "consume it.",
        ])
        return text, _rows(
            [_btn(f"{self._mark(s['roll_mode'] == 'extend')}ROLLING",
                  "confirm:roll_mode:extend"),
             _btn(f"{self._mark(s['roll_mode'] == 'static')}STATIC",
                  "confirm:roll_mode:static")],
            [_btn(f"RE-ARM: {'ON' if s['rearm_levels'] else 'OFF'}",
                  f"apply:rearm_levels:{'false' if s['rearm_levels'] else 'true'}"),
             _btn(f"CANDLE RESET: {'ON' if s['m5_candle_reset'] else 'OFF'}",
                  f"apply:m5_candle_reset:{'false' if s['m5_candle_reset'] else 'true'}")],
            [_btn(BACK, "settings_ladder")],
        )

    def _menu_cycle(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "🔁 <b>CYCLE</b>", "",
            f"Close positions on cycle end: "
            f"{'ON' if s['cycle_close_positions'] else 'OFF'}",
            f"Exit score to close: {s['exit_threshold_exit']:g}",
            f"Max ladder depth: {s['max_ladder_depth']}", "",
            "A cycle ends when the exit engine reads a reversal or exhaustion,",
            "or when a risk limit forces it - never on a trade count and never",
            "on a dollar target. Then pending orders are cancelled and a fresh",
            "ladder is anchored at the new price.",
        ])
        return text, _rows(
            [_btn(f"CLOSE ON END: {'ON' if s['cycle_close_positions'] else 'OFF'}",
                  f"confirm:cycle_close_positions:"
                  f"{'false' if s['cycle_close_positions'] else 'true'}")],
            [_btn("⚖️ EXIT ENGINE", "settings_exit"),
             _btn("📐 MAX DEPTH", "settings_maxdepth")],
            [_btn(BACK, "settings_ladder")],
        )

    def _menu_exit(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "⚖️ <b>EXIT ENGINE</b>", "",
            f"Exit at score: {s['exit_threshold_exit']:g}",
            f"Monitor from: {s['exit_threshold_monitor']:g}", "",
            "The score blends reversal, exhaustion, ladder depth and basket",
            "drawdown, minus the momentum still carrying the move. Banked",
            "profit sharpens a signal that is already there; an open loss",
            "holds the cycle open unless a reversal is real.", "",
            "Lower it to leave sooner, raise it to ride longer. These are",
            "starting values - fit them on historical data.",
        ])
        row = [_btn(f"{self._mark(s['exit_threshold_exit'] == v)}{v:g}",
                    f"confirm:exit_threshold_exit:{v:g}")
               for v in (50, 60, 70, 80)]
        return text, _rows(
            row[:2], row[2:],
            [_btn("✏️ EXIT SCORE", "custom:exit_threshold_exit"),
             _btn("✏️ MONITOR SCORE", "custom:exit_threshold_monitor")],
            [_btn("🎚 WEIGHTS", "settings_exitweights")],
            [_btn(BACK, "settings_cycle")],
        )

    def _menu_exitweights(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "🎚 <b>EXIT WEIGHTS</b>", "",
            f"Reversal: {s['exit_w_reversal']:g}",
            f"Exhaustion: {s['exit_w_exhaustion']:g}",
            f"Continuation (holds the cycle): {s['exit_w_continuation']:g}",
            f"Depth: {s['exit_w_depth']:g}",
            f"Drawdown: {s['exit_w_drawdown']:g}",
            f"Harvest: {s['exit_w_harvest']:g}",
            f"Loss hold: {s['exit_w_loss_hold']:g}", "",
            "Each weight scales one input of the exit score. Set any of them",
            "to 0 to take that input out of the decision entirely.",
        ])
        return text, _rows(
            [_btn("REVERSAL", "custom:exit_w_reversal"),
             _btn("EXHAUSTION", "custom:exit_w_exhaustion")],
            [_btn("CONTINUATION", "custom:exit_w_continuation"),
             _btn("DEPTH", "custom:exit_w_depth")],
            [_btn("DRAWDOWN", "custom:exit_w_drawdown"),
             _btn("HARVEST", "custom:exit_w_harvest")],
            [_btn("LOSS HOLD", "custom:exit_w_loss_hold")],
            [_btn(BACK, "settings_exit")],
        )

    def _menu_maxdepth(self):
        return self._simple_menu(
            "max_ladder_depth", "📐 <b>MAX LADDER DEPTH</b>", (6, 12, 20, 40),
            "settings_cycle",
            "Levels a single cycle may use before new entries stop.\n"
            "The cycle can still close normally through the exit engine.")

    # ---- risk ---    # ---- risk -------------------------------------------------------------
    def _menu_risk(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "🛡 <b>RISK SETTINGS</b>", "",
            f"Max open positions: {s['max_open_positions']}",
            f"Max pending orders: {s['max_pending_orders']}",
            f"Max ladder depth: {s['max_ladder_depth']}",
            f"Max spread: {self._d('max_spread')}",
            f"Max slippage: {s['max_slippage']} points",
            f"Daily drawdown limit: {self._d('max_daily_drawdown')}",
            f"Cycle drawdown limit: {self._d('max_cycle_drawdown')}",
            f"Losing cycles allowed: {s['max_consecutive_losing_cycles']}",
            f"Cooldown after loss: {self._d('cooldown_after_loss_minutes')}",
            f"Order max age: {self._d('order_max_age_seconds')}", "",
            "When a limit trips: new entries stop and pending orders are",
            "cancelled. Open positions keep running under their own rules.",
        ])
        return text, _rows(
            [_btn("📈 MAX OPEN", "settings_open"),
             _btn("📋 MAX PENDING", "settings_pending")],
            [_btn("📏 MAX SPREAD", "settings_spread"),
             _btn("💸 DAILY DRAWDOWN", "settings_daily")],
            [_btn("🔁 CYCLE DRAWDOWN", "settings_cycleloss"),
             _btn("🚫 LOSING CYCLES", "settings_streak")],
            [_btn("⏳ COOLDOWN", "settings_cooldown"),
             _btn("🕒 ORDER AGE", "settings_age")],
            [_btn(BACK, "settings")],
        )

    def _simple_menu(self, key, title, values, back, note="", confirm=None,
                     fmt=None):
        current = self.settings.get(key)
        fmt = fmt or (lambda v: f"{v:g}" if isinstance(v, float) else str(v))
        confirm = RuntimeSettings.needs_confirmation(key) if confirm is None else confirm
        verb = "confirm" if confirm else "apply"
        text = "\n".join([f"{title}", "",
                          f"Current: {RuntimeSettings.display(key, current)}", "",
                          note])
        row = [_btn(f"{self._mark(current == v)}{fmt(v)}", f"{verb}:{key}:{fmt(v)}")
               for v in values]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", f"custom:{key}")],
                           [_btn(BACK, back)])

    def _menu_open(self):
        return self._simple_menu(
            "max_open_positions", "📈 <b>MAX OPEN POSITIONS</b>", (1, 2, 4, 8),
            "settings_risk",
            "No new level is placed while this many positions are open.")

    def _menu_pending(self):
        return self._simple_menu(
            "max_pending_orders", "📋 <b>MAX PENDING ORDERS</b>", (4, 10, 16, 20),
            "settings_risk",
            "Hard cap on live ladder orders across both sides.")

    def _menu_spread(self):
        return self._simple_menu(
            "max_spread", "📏 <b>MAX SPREAD</b>", (0.20, 0.35, 0.50, 1.0),
            "settings_risk",
            "No new levels while the spread is wider than this. Existing\n"
            "positions keep being managed, and the ladder resumes by itself.")

    def _menu_daily(self):
        return self._simple_menu(
            "max_daily_drawdown", "💸 <b>DAILY DRAWDOWN LIMIT</b>",
            (0, 25, 50, 100), "settings_risk",
            "Realised loss for the day that stops new entries (0 = off).")

    def _menu_cycleloss(self):
        return self._simple_menu(
            "max_cycle_drawdown", "🔁 <b>CYCLE DRAWDOWN LIMIT</b>",
            (0, 10, 20, 50), "settings_risk",
            "A cycle this far under water is force-closed and counted as a "
            "loss.\nThis is a loss guard - there is no profit target.")

    def _menu_streak(self):
        return self._simple_menu(
            "max_consecutive_losing_cycles", "🚫 <b>LOSING CYCLES</b>",
            (0, 2, 3, 5), "settings_risk",
            "Consecutive losing cycles allowed before new entries stop.")

    def _menu_cooldown(self):
        return self._simple_menu(
            "cooldown_after_loss_minutes", "⏳ <b>COOLDOWN AFTER LOSS</b>",
            (0, 5, 15, 30), "settings_risk",
            "Pause after a losing cycle before the ladder is rebuilt.")

    def _menu_age(self):
        return self._simple_menu(
            "order_max_age_seconds", "🕒 <b>ORDER MAX AGE</b>",
            (0, 300, 900, 1800), "settings_risk",
            "Pending orders older than this are cancelled and recalculated,\n"
            "so stale levels never fire into a different market.")

    # ---- direction --------------------------------------------------------
    def _menu_notify(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "🔔 <b>NOTIFICATIONS</b>", "",
            f"Status updates: {'ON' if s['telegram_status_updates'] else 'OFF'}"
            f"  (every {s['telegram_status_interval_minutes']:g} min)",
            f"State alerts: {'ON' if s['telegram_state_alerts'] else 'OFF'}",
            f"Per-entry alerts: "
            f"{'ON' if s['telegram_entry_alerts'] else 'OFF'}",
            f"Error throttle: {s['telegram_error_throttle_seconds']:g}s", "",
            "Telegram carries important events only: cycle results, reversals,",
            "risk and errors. Every level, order, trigger and TP still goes to",
            "the CSV logs in full.", "",
            "Per-entry alerts are off by design - this strategy would flood",
            "the chat.",
        ])
        return text, _rows(
            [_btn(f"STATUS UPDATES: "
                  f"{'ON' if s['telegram_status_updates'] else 'OFF'}",
                  f"apply:telegram_status_updates:"
                  f"{'false' if s['telegram_status_updates'] else 'true'}"),
             _btn("⏱ INTERVAL", "custom:telegram_status_interval_minutes")],
            [_btn(f"STATE ALERTS: "
                  f"{'ON' if s['telegram_state_alerts'] else 'OFF'}",
                  f"apply:telegram_state_alerts:"
                  f"{'false' if s['telegram_state_alerts'] else 'true'}")],
            [_btn(f"ENTRY ALERTS: "
                  f"{'ON' if s['telegram_entry_alerts'] else 'OFF'}",
                  f"apply:telegram_entry_alerts:"
                  f"{'false' if s['telegram_entry_alerts'] else 'true'}")],
            [_btn("⏳ ERROR THROTTLE", "custom:telegram_error_throttle_seconds")],
            [_btn(BACK, "settings")],
        )

    def _menu_direction(self):
        current = self.settings.get("direction_filter")
        text = "\n".join([
            "🧭 <b>DIRECTION FILTER</b>", "",
            f"Current: {DIRECTION_LABELS[current]}", "",
            "The base strategy is purely price driven and ladders both ways.",
            "This filter is the hook where a model can later impose a bias;",
            "OFF keeps the plain two-sided ladder.",
        ])
        opts = [("off", "OFF"), ("both", "BOTH"), ("buy_bias", "BUY ONLY"),
                ("sell_bias", "SELL ONLY"), ("none", "NO ENTRIES")]
        rows = [[_btn(f"{self._mark(current == k)}{lbl}", f"confirm:direction_filter:{k}")
                 for k, lbl in opts[i:i + 2]] for i in (0, 2)]
        rows.append([_btn(f"{self._mark(current == 'none')}NO ENTRIES",
                          "confirm:direction_filter:none")])
        return text, _rows(*rows, [_btn(BACK, "settings")])
