"""
Telegram settings panel.

Owns every ⚙️ SETTINGS screen: the menu tree, the confirmation screens for
high-impact changes, and free-text entry for CUSTOM values. Callback routing is
table-driven (one small builder per menu) rather than one giant if/else.

Callback vocabulary
-------------------
    settings                      main settings screen
    settings_<menu>               a submenu (tp, minrr, lot, risk, fixedlot,
                                  positions, sl, slmin, slmax, slfixed, be,
                                  partial, spread, pip, reset)
    confirm:<key>:<value>         confirmation screen for a high-impact change
    apply:<key>:<value>           apply a validated change
    custom:<key>                  ask for a typed value
    settings_cancel:<menu>        leave a confirmation without changing anything

Every screen renders live values from RuntimeSettings, so what you see is what
the next trade will use. Changes never touch open positions.
"""

import threading

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import tp_engine

from runtime_settings import (
    LOT_MODE_LABELS,
    SL_MODE_LABELS,
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

    # which menu a key's confirmation screen returns to
    KEY_MENU = {
        "tp_mode": "tp",
        "tp_rr": "tp",
        "custom_rr": "tp",
        "min_rr": "minrr",
        "lot_mode": "lot",
        "risk_percent": "risk",
        "fixed_lot": "fixedlot",
        "max_open_positions": "positions",
        "sl_mode": "sl",
        "sl_points_min": "slmin",
        "sl_points_max": "slmax",
        "sl_fixed_points": "slfixed",
        "breakeven_enabled": "be",
        "breakeven_r": "be",
        "partial_close_enabled": "partial",
        "partial_close_r": "partial",
        "partial_close_fraction": "partial",
        "max_spread_points": "spread",
        "pip_points": "pip",
    }

    PROMPTS = {
        "custom_rr": "Send the RR multiple (e.g. 2.5). Range 0.1 - 20.",
        "min_rr": "Send the minimum RR (e.g. 1.25). Range 0 - 20.",
        "risk_percent": "Send the risk per trade in percent (e.g. 1.25). Range 0.01 - 10.",
        "fixed_lot": "Send the fixed lot size (e.g. 0.03). Range 0.01 - 100.",
        "max_open_positions": "Send the maximum open positions (1 - 20).",
        "sl_points_min": "Send the minimum SL in points (e.g. 350).",
        "sl_points_max": "Send the maximum SL in points (e.g. 900).",
        "sl_fixed_points": "Send the fixed SL distance in points (e.g. 500).",
        "max_spread_points": "Send the maximum spread in points (e.g. 450).",
        "pip_points": "Send how many points make 1 pip (0 = auto-detect).",
    }

    def __init__(self, engine, settings, csv_logger=None):
        self.engine = engine
        self.settings = settings
        self.csv = csv_logger
        self._pending_lock = threading.Lock()
        self._pending = {}          # chat_id -> settings key awaiting text input

    # ================================================================ routing
    def handles(self, data):
        if self.settings is None:      # panel not wired -> route nothing here
            return False
        return data.startswith(("settings", "apply:", "confirm:", "custom:"))

    def render(self, data, chat_id=None):
        """Return (text, InlineKeyboardMarkup) for a settings callback."""
        try:
            if data.startswith("apply:"):
                return self._apply(data[len("apply:"):])
            if data.startswith("confirm:"):
                return self._confirm(data[len("confirm:"):])
            if data.startswith("custom:"):
                return self._ask_custom(data[len("custom:"):], chat_id)
            if data.startswith("settings_cancel:"):
                self._clear_pending(chat_id)
                return self.menu(data.split(":", 1)[1], banner="❌ Cancelled - nothing changed.")
            if data == "settings":
                self._clear_pending(chat_id)
                return self.menu("main")
            if data.startswith("settings_"):
                return self.menu(data[len("settings_"):])
        except SettingError as exc:
            return self.menu("main", banner=f"⚠️ {exc}")
        except Exception as exc:                        # never break the panel
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
        """
        Handle a typed CUSTOM value. Returns (text, keyboard) or None when this
        chat was not asked for anything (so ordinary chatter is ignored).
        """
        key = self.awaiting_input(chat_id)
        if not key:
            return None
        raw = (text or "").strip().replace("%", "").replace(",", ".")
        try:
            value = self.settings.precheck(key, raw)
        except SettingError as exc:
            menu = self.KEY_MENU.get(key, "main")
            return ("\n".join([
                "⚠️ <b>INVALID VALUE</b>",
                "",
                str(exc),
                "",
                self.PROMPTS.get(key, ""),
                "",
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
            return ("\n".join([
                "⚠️ <b>CONFIRM RESET</b>",
                "",
                "Restore the original configuration?",
                "",
                "TP Mode: CUSTOM RR (3.0R)",
                "Risk: 1.5%   Max Positions: 4",
                "SL: 400 - 1000 pts   Max Spread: 500 pts",
                "Breakeven 0.5R   Partial 1.5R / 30%",
                "",
                "This affects NEW trades only.",
            ]), _rows(
                [_btn("✅ CONFIRM", "apply:reset:1")],
                [_btn("❌ CANCEL", "settings_cancel:main")],
            ))

        if key == "tp_rr":
            new_display = f"CUSTOM RR ({float(value)}R)"
            old_display = self._tp_display()
            label = "TP mode"
        else:
            coerced = self.settings.precheck(key, value)
            old_display = (self._tp_display() if key == "tp_mode"
                           else RuntimeSettings.display(key, self.settings.get(key)))
            new_display = RuntimeSettings.display(key, coerced)
            label = RuntimeSettings.label(key)

        menu = self.KEY_MENU.get(key, "main")
        text = "\n".join([
            "⚠️ <b>CONFIRM CHANGE</b>",
            "",
            f"Change {label}:",
            "",
            f"<b>{old_display} → {new_display}</b>",
            "",
            "This will affect NEW trades only.",
            "Open positions are not modified.",
        ])
        return text, _rows(
            [_btn("✅ CONFIRM", f"apply:{key}:{value}")],
            [_btn("❌ CANCEL", f"settings_cancel:{menu}")],
        )

    def _apply(self, payload):
        key, _, value = payload.partition(":")

        if key == "reset":
            changed = self.settings.reset()
            banner = (f"♻️ Settings reset to the original configuration "
                      f"({len(changed)} changed).")
            return self.menu("main", banner=banner)

        if key == "tp_rr":
            rr = RuntimeSettings.coerce("custom_rr", value)
            self.settings.set("custom_rr", rr)
            self.settings.set("tp_mode", "custom_rr")
            return self.menu("tp", banner=f"✅ TP mode: CUSTOM RR ({rr}R)")

        changed, message, _old, _new = self.settings.set(key, value)
        banner = ("✅ " if changed else "ℹ️ ") + message
        return self.menu(self.KEY_MENU.get(key, "main"), banner=banner)

    # ================================================================= menus
    def menu(self, name, banner=""):
        builder = getattr(self, f"_menu_{name}", None)
        if builder is None:
            builder = self._menu_main
        text, markup = builder()
        if banner:
            text = f"{banner}\n\n{text}"
        return text, markup

    # ---- helpers ----------------------------------------------------------
    def _tp_display(self):
        snap = self.settings.snapshot()
        label = TP_MODE_LABELS.get(snap["tp_mode"], snap["tp_mode"])
        if snap["tp_mode"] == "custom_rr":
            label = f"{label} ({snap['custom_rr']}R)"
        return label

    def _pip_line(self):
        """Show what '1 pip' actually resolves to on the live symbol."""
        snap = self.settings.snapshot()
        try:
            info = self.engine.symbol_info_live() if hasattr(
                self.engine, "symbol_info_live") else None
            if info is not None:
                pip_size, pts = tp_engine.get_pip_size(info, snap["pip_points"])
                origin = "pinned" if snap["pip_points"] else "auto"
                return f"1 pip = {pip_size:g} ({pts} points, {origin})"
        except Exception:
            pass
        return ("1 pip = auto-detected from the symbol"
                if not snap["pip_points"]
                else f"1 pip = {snap['pip_points']} points (pinned)")

    @staticmethod
    def _mark(active):
        return "✅ " if active else ""

    # ---- main -------------------------------------------------------------
    def _menu_main(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "⚙️ <b>CURRENT SETTINGS</b>",
            "",
            f"Symbol: {getattr(self.engine, 'symbol', '')}",
            "",
            f"🎯 TP: {self._tp_display()}",
            f"⚖️ Minimum RR: {s['min_rr']}R",
            f"📐 {self._pip_line()}",
            "",
            f"💰 Risk Mode: {LOT_MODE_LABELS[s['lot_mode']]}",
            f"💰 Risk: {s['risk_percent']}%",
            f"📦 Fixed Lot: {s['fixed_lot']}",
            "",
            f"📈 Max Positions: {s['max_open_positions']}",
            "",
            f"🛑 SL Mode: {SL_MODE_LABELS[s['sl_mode']]}",
            f"🛑 SL Min: {s['sl_points_min']} pts",
            f"🛑 SL Max: {s['sl_points_max']} pts",
            (f"🛑 Fixed SL: {s['sl_fixed_points']} pts"
             if s["sl_mode"] == "fixed" else ""),
            "",
            f"🔒 Breakeven: {'ON' if s['breakeven_enabled'] else 'OFF'}"
            + (f" @ {s['breakeven_r']}R" if s["breakeven_enabled"] else ""),
            f"📉 Partial: {'ON' if s['partial_close_enabled'] else 'OFF'}"
            + (f" @ {s['partial_close_r']}R" if s["partial_close_enabled"] else ""),
            f"📉 Partial Size: {s['partial_close_fraction'] * 100:.0f}%",
            "",
            f"📏 Max Spread: {s['max_spread_points']} pts",
            "",
            "<i>Changes apply to NEW trades only.</i>",
        ])
        markup = _rows(
            [_btn("🎯 TP MODE", "settings_tp"), _btn("⚖️ MIN RR", "settings_minrr")],
            [_btn("💰 LOT / RISK", "settings_lot"),
             _btn("📈 MAX POSITIONS", "settings_positions")],
            [_btn("🛑 STOP LOSS", "settings_sl"),
             _btn("📏 MAX SPREAD", "settings_spread")],
            [_btn("🔒 BREAKEVEN", "settings_be"),
             _btn("📉 PARTIAL CLOSE", "settings_partial")],
            [_btn("📐 PIP SIZE", "settings_pip"),
             _btn("♻️ RESET SETTINGS", "confirm:reset:1")],
            [_btn("🔙 MAIN MENU", "panel")],
        )
        return text, markup

    # ---- TP ---------------------------------------------------------------
    def _menu_tp(self):
        s = self.settings.snapshot()
        mode = s["tp_mode"]
        text = "\n".join([
            "🎯 <b>SELECT TP MODE</b>",
            "",
            f"Current: {self._tp_display()}",
            f"Minimum RR: {s['min_rr']}R",
            f"{self._pip_line()}",
            "",
            "Pip targets are validated against the signal's SL before any",
            "order is sent - a target that lands below the minimum RR is",
            "rejected instead of traded.",
        ])
        markup = _rows(
            [_btn(f"{self._mark(mode == '1_pip')}1 PIP", "confirm:tp_mode:1_pip"),
             _btn(f"{self._mark(mode == '2_pips')}2 PIPS", "confirm:tp_mode:2_pips")],
            [_btn(f"{self._mark(mode == '3_pips')}3 PIPS", "confirm:tp_mode:3_pips"),
             _btn(f"{self._mark(mode == '4_pips')}4 PIPS", "confirm:tp_mode:4_pips")],
            [_btn(f"{self._mark(mode == '5_pips')}5 PIPS", "confirm:tp_mode:5_pips")],
            [_btn(f"{self._mark(mode == 'custom_rr')}CUSTOM RR", "settings_tprr")],
            [_btn(BACK, "settings")],
        )
        return text, markup

    def _menu_tprr(self):
        s = self.settings.snapshot()
        current = s["custom_rr"]
        text = "\n".join([
            "🎯 <b>CUSTOM RR</b>",
            "",
            f"Current: {self._tp_display()}",
            "",
            "TP distance = SL distance × RR.",
            "This is the original take-profit behaviour.",
        ])
        opts = [1.0, 1.5, 2.0, 2.5, 3.0]
        row1 = [_btn(f"{self._mark(s['tp_mode'] == 'custom_rr' and current == v)}{v:g}R",
                     f"confirm:tp_rr:{v:g}") for v in opts[:3]]
        row2 = [_btn(f"{self._mark(s['tp_mode'] == 'custom_rr' and current == v)}{v:g}R",
                     f"confirm:tp_rr:{v:g}") for v in opts[3:]]
        return text, _rows(row1, row2,
                           [_btn("✏️ CUSTOM VALUE", "custom:custom_rr")],
                           [_btn(BACK, "settings_tp")])

    # ---- minimum RR -------------------------------------------------------
    def _menu_minrr(self):
        s = self.settings.snapshot()
        current = s["min_rr"]
        text = "\n".join([
            "⚖️ <b>MINIMUM RR</b>",
            "",
            f"Current: {current}R",
            "",
            "A detected signal is rejected - not traded - when the chosen TP",
            "produces a reward/risk below this value.",
            "",
            "Set 0 to accept every RR (not recommended with pip targets).",
        ])
        opts = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
        rows = [[_btn(f"{self._mark(current == v)}{v:g}R", f"confirm:min_rr:{v:g}")
                 for v in opts[i:i + 3]] for i in (0, 3)]
        return text, _rows(*rows,
                           [_btn("✏️ CUSTOM VALUE", "custom:min_rr")],
                           [_btn(BACK, "settings")])

    # ---- lot / risk -------------------------------------------------------
    def _menu_lot(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "💰 <b>LOT / RISK</b>",
            "",
            f"Mode: {LOT_MODE_LABELS[s['lot_mode']]}",
            f"Risk: {s['risk_percent']}%",
            f"Fixed Lot: {s['fixed_lot']}",
            "",
            "Risk % sizes each trade from the account balance and the signal's",
            "SL distance. Fixed lot always sends the same volume.",
            "Margin safety applies in both modes.",
        ])
        return text, _rows(
            [_btn(f"{self._mark(s['lot_mode'] == 'risk_percent')}RISK %",
                  "confirm:lot_mode:risk_percent"),
             _btn(f"{self._mark(s['lot_mode'] == 'fixed_lot')}FIXED LOT",
                  "confirm:lot_mode:fixed_lot")],
            [_btn("📊 RISK %", "settings_risk"),
             _btn("📦 FIXED LOT", "settings_fixedlot")],
            [_btn(BACK, "settings")],
        )

    def _menu_risk(self):
        current = self.settings.get("risk_percent")
        text = "\n".join([
            "📊 <b>RISK %</b>",
            "",
            f"Current: {current}%",
            "",
            "Percentage of account balance risked per trade.",
        ])
        opts = [0.5, 1.0, 1.5, 2.0]
        row = [_btn(f"{self._mark(current == v)}{v:g}%", f"confirm:risk_percent:{v:g}")
               for v in opts]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:risk_percent")],
                           [_btn(BACK, "settings_lot")])

    def _menu_fixedlot(self):
        current = self.settings.get("fixed_lot")
        text = "\n".join([
            "📦 <b>FIXED LOT</b>",
            "",
            f"Current: {current}",
            "",
            "Validated against the symbol's volume_min / volume_max and",
            "snapped to volume_step before the order is sent.",
        ])
        opts = [0.01, 0.02, 0.05, 0.10]
        row = [_btn(f"{self._mark(current == v)}{v:g}", f"confirm:fixed_lot:{v:g}")
               for v in opts]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:fixed_lot")],
                           [_btn(BACK, "settings_lot")])

    # ---- positions --------------------------------------------------------
    def _menu_positions(self):
        current = self.settings.get("max_open_positions")
        text = "\n".join([
            "📈 <b>MAX POSITIONS</b>",
            "",
            f"Current: {current}",
            "",
            "Applies to NEW trades. Positions already open are never closed",
            "by changing this.",
        ])
        row = [_btn(f"{self._mark(current == v)}{v}", f"confirm:max_open_positions:{v}")
               for v in (1, 2, 3, 4, 5)]
        return text, _rows(row[:3], row[3:],
                           [_btn("✏️ CUSTOM", "custom:max_open_positions")],
                           [_btn(BACK, "settings")])

    # ---- stop loss --------------------------------------------------------
    def _menu_sl(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "🛑 <b>STOP LOSS SETTINGS</b>",
            "",
            f"Mode: {SL_MODE_LABELS[s['sl_mode']]}",
            "",
            f"Minimum: {s['sl_points_min']} pts",
            f"Maximum: {s['sl_points_max']} pts",
            f"Fixed distance: {s['sl_fixed_points']} pts",
            "",
            "STRUCTURAL is the original behaviour: the SL comes from the",
            "signal candle's range, clamped between minimum and maximum.",
            "FIXED always uses the fixed distance.",
        ])
        return text, _rows(
            [_btn(f"{self._mark(s['sl_mode'] == 'structural')}STRUCTURAL",
                  "confirm:sl_mode:structural"),
             _btn(f"{self._mark(s['sl_mode'] == 'fixed')}FIXED",
                  "confirm:sl_mode:fixed")],
            [_btn("SL MIN", "settings_slmin"), _btn("SL MAX", "settings_slmax")],
            [_btn("FIXED SL", "settings_slfixed")],
            [_btn(BACK, "settings")],
        )

    def _menu_slmin(self):
        current = self.settings.get("sl_points_min")
        text = "\n".join([
            "🛑 <b>SL MIN</b>",
            "",
            f"Current: {current} pts",
            f"Must stay below SL max ({self.settings.get('sl_points_max')} pts).",
        ])
        row = [_btn(f"{self._mark(current == v)}{v}", f"confirm:sl_points_min:{v}")
               for v in (300, 400, 500, 600)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:sl_points_min")],
                           [_btn(BACK, "settings_sl")])

    def _menu_slmax(self):
        current = self.settings.get("sl_points_max")
        text = "\n".join([
            "🛑 <b>SL MAX</b>",
            "",
            f"Current: {current} pts",
            f"Must stay above SL min ({self.settings.get('sl_points_min')} pts).",
        ])
        row = [_btn(f"{self._mark(current == v)}{v}", f"confirm:sl_points_max:{v}")
               for v in (600, 800, 1000, 1200)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:sl_points_max")],
                           [_btn(BACK, "settings_sl")])

    def _menu_slfixed(self):
        current = self.settings.get("sl_fixed_points")
        text = "\n".join([
            "🛑 <b>FIXED SL DISTANCE</b>",
            "",
            f"Current: {current} pts",
            "",
            "Only used when SL mode is FIXED.",
        ])
        row = [_btn(f"{self._mark(current == v)}{v}", f"confirm:sl_fixed_points:{v}")
               for v in (300, 400, 500, 600)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:sl_fixed_points")],
                           [_btn(BACK, "settings_sl")])

    # ---- breakeven --------------------------------------------------------
    def _menu_be(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "🔒 <b>BREAKEVEN</b>",
            "",
            f"Status: {'ON' if s['breakeven_enabled'] else 'OFF'}",
            f"Trigger: {s['breakeven_r']}R",
            "",
            "Moves the SL to entry once price travels this multiple of the",
            "original risk. Existing positions keep whatever they already have.",
        ])
        on = s["breakeven_enabled"]
        trig = s["breakeven_r"]
        return text, _rows(
            [_btn(f"{self._mark(on)}ON", "apply:breakeven_enabled:true"),
             _btn(f"{self._mark(not on)}OFF", "apply:breakeven_enabled:false")],
            [_btn(f"{self._mark(trig == v)}{v:g}R", f"apply:breakeven_r:{v:g}")
             for v in (0.25, 0.5, 0.75, 1.0)],
            [_btn(BACK, "settings")],
        )

    # ---- partial close ----------------------------------------------------
    def _menu_partial(self):
        s = self.settings.snapshot()
        text = "\n".join([
            "📉 <b>PARTIAL CLOSE</b>",
            "",
            f"Status: {'ON' if s['partial_close_enabled'] else 'OFF'}",
            f"Trigger: {s['partial_close_r']}R",
            f"Fraction: {s['partial_close_fraction'] * 100:.0f}%",
            "",
            "Closes part of a winning position at the trigger. Uses the",
            "existing close mechanism; open positions are not touched by",
            "changing these values.",
        ])
        on = s["partial_close_enabled"]
        trig = s["partial_close_r"]
        frac = s["partial_close_fraction"]
        return text, _rows(
            [_btn(f"{self._mark(on)}ON", "apply:partial_close_enabled:true"),
             _btn(f"{self._mark(not on)}OFF", "apply:partial_close_enabled:false")],
            [_btn(f"{self._mark(trig == v)}{v:g}R", f"apply:partial_close_r:{v:g}")
             for v in (1.0, 1.5, 2.0)],
            [_btn(f"{self._mark(abs(frac - v) < 1e-9)}{v * 100:.0f}%",
                  f"apply:partial_close_fraction:{v}")
             for v in (0.2, 0.3, 0.5)],
            [_btn(BACK, "settings")],
        )

    # ---- spread -----------------------------------------------------------
    def _menu_spread(self):
        current = self.settings.get("max_spread_points")
        text = "\n".join([
            "📏 <b>MAX SPREAD</b>",
            "",
            f"Current: {current} pts",
            "",
            "Signals are skipped while the spread is wider than this.",
        ])
        row = [_btn(f"{self._mark(current == v)}{v}", f"apply:max_spread_points:{v}")
               for v in (300, 400, 500, 600)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:max_spread_points")],
                           [_btn(BACK, "settings")])

    # ---- pip size ---------------------------------------------------------
    def _menu_pip(self):
        current = self.settings.get("pip_points")
        text = "\n".join([
            "📐 <b>PIP SIZE</b>",
            "",
            f"Current: {'AUTO' if not current else str(current) + ' points'}",
            f"{self._pip_line()}",
            "",
            "AUTO derives the pip from the symbol's digits and point size",
            "(10 points on a 3/5-digit feed, 1 point otherwise).",
            "Pin it here if your broker quotes gold differently.",
        ])
        row = [_btn(f"{self._mark(current == v)}"
                    f"{'AUTO' if v == 0 else str(v) + ' pts'}",
                    f"apply:pip_points:{v}")
               for v in (0, 1, 10, 100)]
        return text, _rows(row[:2], row[2:],
                           [_btn("✏️ CUSTOM", "custom:pip_points")],
                           [_btn(BACK, "settings")])
