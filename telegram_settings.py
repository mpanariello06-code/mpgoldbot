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
        "tp_mode": "tp", "tp_distance": "tp", "stop_loss_distance": "sl",
        "pip_points": "pip",
        "lot_size": "lot", "max_lot_size": "maxlot",
        "ladder_spacing": "spacing", "ladder_depth": "depth",
        "first_level_offset": "offset", "roll_mode": "roll",
        "rearm_levels": "roll", "m5_candle_reset": "roll",
        "profit_cycle_target": "cycle", "cycle_close_positions": "cycle",
        "cycle_take_profit_money": "basket",
        "max_open_positions": "open", "max_pending_orders": "pending",
        "max_spread": "spread", "max_slippage": "risk",
        "max_daily_loss": "daily", "max_cycle_loss": "cycleloss",
        "max_consecutive_losing_cycles": "streak",
        "cooldown_after_loss_minutes": "cooldown",
        "order_max_age_seconds": "age",
        "direction_filter": "direction", "timeframe": "settings",
    }

    PROMPTS = {
        "tp_distance": "Send the TP distance in price units (e.g. 0.30).",
        "stop_loss_distance": "Send the SL distance in price units (0 = no stop loss).",
        "pip_points": "Send how many points make 1 pip (0 = auto-detect).",
        "lot_size": "Send the fixed lot size (e.g. 0.01).",
        "max_lot_size": "Send the maximum allowed lot size (e.g. 0.10).",
        "ladder_spacing": "Send the ladder spacing in price units (e.g. 0.30).",
        "ladder_depth": "Send the number of levels per side (1 - 50).",
        "first_level_offset": "Send the distance from price to the first level "
                              "(price units). The broker minimum always wins.",
        "profit_cycle_target": "Send how many successful TPs complete a cycle "
                               "(0 = never).",
        "cycle_take_profit_money": "Send the basket profit that closes the cycle "
                                   "(account currency, 0 = off).",
        "max_open_positions": "Send the maximum open positions (1 - 200).",
        "max_pending_orders": "Send the maximum pending orders (1 - 200).",
        "max_spread": "Send the maximum spread in price units (e.g. 0.50, 0 = off).",
        "max_daily_loss": "Send the daily loss limit in account currency (0 = off).",
        "max_cycle_loss": "Send the cycle loss limit in account currency (0 = off).",
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
                f"Cycle {d['profit_cycle_target']} TPs | "
                f"Max {d['max_open_positions']} pos / {d['max_pending_orders']} pend",
                f"Spread {d['max_spread']} | Daily loss {d['max_daily_loss']}",
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
            f"🔁 Profit cycle: {s['profit_cycle_target']} TPs"
            + (f" | basket {self._d('cycle_take_profit_money')}"
               if s["cycle_take_profit_money"] else ""),
            f"🔁 Close on cycle end: {'ON' if s['cycle_close_positions'] else 'OFF'}",
            "",
            f"🛡 Max open: {s['max_open_positions']} | "
            f"Max pending: {s['max_pending_orders']}",
            f"🛡 Max spread: {self._d('max_spread')}",
            f"🛡 Daily loss: {self._d('max_daily_loss')} | "
            f"Cycle loss: {self._d('max_cycle_loss')}",
            "",
            f"🧭 Direction: {DIRECTION_LABELS[s['direction_filter']]}",
            "",
            "<i>Changes apply to NEW levels only.</i>",
        ])
        markup = _rows(
            [_btn("🎯 TP SETTINGS", "settings_tp"), _btn("💰 LOT SIZE", "settings_lot")],
            [_btn("🪜 LADDER SETTINGS", "settings_ladder"),
             _btn("🛡 RISK SETTINGS", "settings_risk")],
            [_btn("🧭 DIRECTION", "settings_direction"),
             _btn("♻️ RESET SETTINGS", "confirm:reset:1")],
            [_btn("🔙 MAIN MENU", "panel")],
        )
        return text, markup

    # ---- TP ---------------------------------------------------------------
    def _menu_tp(self):
        s = self.settings.snapshot()
        mode = s["tp_mode"]
        text = "\n".join([
            "🎯 <b>TP SETTINGS</b>", "",
            f"Current: {self._tp_line()}",
            f"Ladder spacing: {self._d('ladder_spacing')}",
            f"{self._pip_line()}", "",
            "Every ladder order carries this take profit. Pip targets are",
            "converted with the symbol's own point size, never a hardcoded",
            "gold pip.",
        ])
        return text, _rows(
            [_btn(f"{self._mark(mode == 'distance')}DISTANCE "
                  f"({s['tp_distance']:g})", "confirm:tp_mode:distance"),
             _btn("✏️ SET DISTANCE", "custom:tp_distance")],
            [_btn(f"{self._mark(mode == '1_pip')}1 PIP", "confirm:tp_mode:1_pip"),
             _btn(f"{self._mark(mode == '2_pips')}2 PIPS", "confirm:tp_mode:2_pips"),
             _btn(f"{self._mark(mode == '3_pips')}3 PIPS", "confirm:tp_mode:3_pips")],
            [_btn(f"{self._mark(mode == '4_pips')}4 PIPS", "confirm:tp_mode:4_pips"),
             _btn(f"{self._mark(mode == '5_pips')}5 PIPS", "confirm:tp_mode:5_pips")],
            [_btn("🛑 STOP LOSS", "settings_sl"), _btn("📐 PIP SIZE", "settings_pip")],
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
            f"Profit cycle: {s['profit_cycle_target']} TPs", "",
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
               for v in (0.15, 0.20, 0.30, 0.50)]
        return text, _rows(row[:2], row[2:],
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
            "🔁 <b>PROFIT CYCLE</b>", "",
            f"Target: {s['profit_cycle_target']} successful TPs",
            f"Basket target: {self._d('cycle_take_profit_money')}",
            f"Close positions on cycle end: "
            f"{'ON' if s['cycle_close_positions'] else 'OFF'}", "",
            "When the target is reached the cycle closes out, pending orders",
            "are cancelled and a fresh ladder is anchored at the new price.",
        ])
        row = [_btn(f"{self._mark(s['profit_cycle_target'] == v)}{v}",
                    f"confirm:profit_cycle_target:{v}") for v in (2, 3, 4, 6)]
        return text, _rows(
            row[:2], row[2:],
            [_btn("✏️ CUSTOM", "custom:profit_cycle_target"),
             _btn("💵 BASKET TP", "settings_basket")],
            [_btn(f"CLOSE ON END: {'ON' if s['cycle_close_positions'] else 'OFF'}",
                  f"confirm:cycle_close_positions:"
                  f"{'false' if s['cycle_close_positions'] else 'true'}")],
            [_btn(BACK, "settings_ladder")],
        )

    def _menu_basket(self):
        current = self.settings.get("cycle_take_profit_money")
        text = "\n".join([
            "💵 <b>CYCLE BASKET TP</b>", "",
            f"Current: {self._d('cycle_take_profit_money')}", "",
            "Optional: close the whole cycle - winners and losers together -",
            "once its net profit reaches this amount. OFF leaves the TP count",
            "as the only cycle trigger.",
        ])
        row = [_btn(f"{self._mark(current == v)}{'OFF' if v == 0 else f'${v:g}'}",
                    f"apply:cycle_take_profit_money:{v:g}")
               for v in (0, 1, 2, 5)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:cycle_take_profit_money")],
                           [_btn(BACK, "settings_cycle")])

    # ---- risk -------------------------------------------------------------
    def _menu_risk(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "🛡 <b>RISK SETTINGS</b>", "",
            f"Max open positions: {s['max_open_positions']}",
            f"Max pending orders: {s['max_pending_orders']}",
            f"Max spread: {self._d('max_spread')}",
            f"Max slippage: {s['max_slippage']} points",
            f"Daily loss limit: {self._d('max_daily_loss')}",
            f"Cycle loss limit: {self._d('max_cycle_loss')}",
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
             _btn("💸 DAILY LOSS", "settings_daily")],
            [_btn("🔁 CYCLE LOSS", "settings_cycleloss"),
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
            "max_daily_loss", "💸 <b>DAILY LOSS LIMIT</b>", (0, 25, 50, 100),
            "settings_risk",
            "Realised loss for the day that stops new entries (0 = off).")

    def _menu_cycleloss(self):
        return self._simple_menu(
            "max_cycle_loss", "🔁 <b>CYCLE LOSS LIMIT</b>", (0, 10, 20, 50),
            "settings_risk",
            "A cycle this far under water is closed out and counted as a loss.")

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
