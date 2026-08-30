"""
Telegram control panel.

Runs a python-telegram-bot Application on its own thread with its own asyncio
event loop, so it never blocks - and is never blocked by - the MT5 trading
loop. Every button performs a real operation against the live TradingEngine.

Security: only chat ids listed in TELEGRAM_CHAT_ID / TELEGRAM_ALLOWED_CHAT_IDS
may query or control the bot. Everybody else gets "Unauthorized." Secrets are
never sent to Telegram.
"""

import asyncio
import threading
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, InvalidToken
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config as cfg
from telegram_settings import SettingsPanel

UNAUTHORIZED = "Unauthorized."
# Seconds to wait before re-polling after a Telegram network failure
RETRY_SECONDS = 30


def _now():
    return datetime.now().strftime("%H:%M:%S")


def _num(value, digits=2):
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _money(value):
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


class TelegramController:
    def __init__(self, engine, csv_logger, settings=None):
        self.engine = engine
        self.csv = csv_logger
        self.panel = SettingsPanel(engine, settings, csv_logger)

        self._app = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._shutdown = threading.Event()
        self._lock = threading.Lock()

        self.authorized = list(cfg.AUTHORIZED_CHAT_IDS)
        self.primary_chat = self.authorized[0] if self.authorized else None

    # ================================================================ THREAD
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return  # never run two pollers
            self._ready.clear()
            self._shutdown.clear()
            self._thread = threading.Thread(
                target=self._thread_main, name="telegram", daemon=True
            )
            self._thread.start()
        # Wait briefly so startup errors surface to the caller's log
        self._ready.wait(timeout=15)

    def _thread_main(self):
        """
        One poller, forever. A network failure retries in place on this same
        thread, so Telegram can never spawn a second poller - and a dead
        Telegram connection can never stop MT5 trading.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            while not self._shutdown.is_set():
                try:
                    app = Application.builder().token(cfg.TELEGRAM_BOT_TOKEN).build()
                    self._register(app)
                    self._app = app
                    self._ready.set()
                    # stop_signals=None is required outside the main thread
                    app.run_polling(
                        stop_signals=None,
                        close_loop=False,
                        drop_pending_updates=True,
                    )
                    if self._shutdown.is_set():
                        break
                    self._log_error(
                        f"Telegram polling ended; reconnecting in {RETRY_SECONDS}s"
                    )
                except InvalidToken as exc:
                    self._log_error(
                        f"Invalid TELEGRAM_BOT_TOKEN ({exc}) - remote control "
                        "disabled. Trading continues."
                    )
                    break
                except Exception as exc:
                    self._log_error(
                        f"Telegram error: {exc} - reconnecting in {RETRY_SECONDS}s"
                    )
                finally:
                    self._app = None
                    self._ready.set()
                self._shutdown.wait(RETRY_SECONDS)
        finally:
            self._ready.set()
            self._app = None
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    def stop(self):
        self._shutdown.set()
        app, loop = self._app, self._loop
        if app and loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(app.stop_running)
            except Exception as exc:
                self._log_error(f"Telegram stop failed: {exc}")
        if self._thread:
            self._thread.join(timeout=10)

    def _register(self, app):
        app.add_handler(CommandHandler(["start", "panel", "menu"], self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("account", self.cmd_account))
        app.add_handler(CommandHandler("positions", self.cmd_positions))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("stopbot", self.cmd_stop))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CommandHandler("ladder", self.cmd_ladder))
        app.add_handler(CallbackQueryHandler(self.on_button))
        # typed CUSTOM values for the settings panel
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                       self.on_text))
        app.add_error_handler(self.on_error)

    # ========================================================= NOTIFICATIONS
    def notify(self, text):
        """Fire-and-forget push. Called from the trading thread; never blocks."""
        loop = self._loop
        app = self._app
        if not (loop and app and loop.is_running()):
            return
        if not cfg.TELEGRAM_NOTIFICATIONS:
            return
        targets = [self.primary_chat] if self.primary_chat else self.authorized
        for chat_id in targets:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._send(chat_id, text), loop
                )
                future.add_done_callback(self._swallow)
            except Exception as exc:
                self._log_error(f"notify failed: {exc}")

    async def _send(self, chat_id, text):
        await self._app.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
        )

    def _swallow(self, future):
        try:
            future.result()
        except Exception as exc:
            self._log_error(f"Telegram send failed: {exc}")

    def _log_error(self, message):
        print(f"[{_now()}] ⚠ {message}", flush=True)
        try:
            self.csv.log_event("ERROR", message, status="TELEGRAM")
        except Exception:
            pass

    # ================================================================= AUTH
    def is_authorized(self, chat_id):
        return chat_id in self.authorized

    async def _deny(self, update):
        chat = update.effective_chat
        user = update.effective_user
        self.csv.log_event(
            "UNAUTHORIZED",
            f"chat_id={chat.id if chat else '?'} user={user.id if user else '?'}",
            status="DENIED",
        )
        if update.callback_query:
            await update.callback_query.answer(UNAUTHORIZED, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(UNAUTHORIZED)

    async def _guard(self, update):
        chat = update.effective_chat
        if chat and self.is_authorized(chat.id):
            return True
        await self._deny(update)
        return False

    # =============================================================== KEYBOARD
    @staticmethod
    def keyboard():
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🟢 START TRADING", callback_data="start"),
                InlineKeyboardButton("⏸ PAUSE TRADING", callback_data="pause"),
            ],
            [
                InlineKeyboardButton("▶️ RESUME TRADING", callback_data="resume"),
                InlineKeyboardButton("🛑 STOP BOT", callback_data="stop"),
            ],
            [
                InlineKeyboardButton("📊 STATUS", callback_data="status"),
                InlineKeyboardButton("💰 ACCOUNT", callback_data="account"),
            ],
            [
                InlineKeyboardButton("📈 POSITIONS", callback_data="positions"),
                InlineKeyboardButton("📋 TODAY'S STATS", callback_data="stats"),
            ],
            [
                InlineKeyboardButton("🪜 LADDER", callback_data="ladder"),
                InlineKeyboardButton("⚙️ SETTINGS", callback_data="settings"),
            ],
            [
                InlineKeyboardButton("🔄 REFRESH", callback_data="refresh"),
            ],
        ])

    # ================================================================= VIEWS
    async def _status_text(self):
        s = await asyncio.to_thread(self.engine.status)
        account = s.get("account")
        d = 2
        price = (f"{s['bid']:.{d}f} / {s['ask']:.{d}f}"
                 if s.get("bid") and s.get("ask") else "n/a")
        spread = f"{s['spread']:.{d}f}" if s.get("spread") is not None else "n/a"
        updated = s.get("last_update")

        momentum = s.get("momentum_score", 0.0)
        momentum_word = ("STRONG" if momentum >= 0.66 else
                         "WEAK" if momentum <= 0.33 else "NEUTRAL")
        reversal = s.get("reversal_score", 0.0)
        reversal_word = "DETECTED" if s.get("state") == "REVERSAL_DETECTED" or \
            reversal >= 0.5 else "NOT DETECTED"
        market_state = (s.get("state") or "").replace("_", " ")

        lines = [
            "🪜 <b>ROLLING LADDER SCALPER</b>", "",
            f"Bot: {s['icon']} {s['lifecycle']}   [{s.get('mode', '?')}]",
            f"Symbol: {s['symbol']}",
            f"Timeframe: {s.get('timeframe', '')}",
            f"MT5: {'Connected' if s['mt5_connected'] else 'Disconnected'}",
            f"Price: {price}   Spread: {spread}",
            "",
            f"Ladder spacing: {s.get('spacing')}",
            f"TP: {_num(s.get('tp_distance'))}   Lot: {s.get('lot')}",
            "",
            f"Cycle: #{s.get('cycle_id', 0)}",
            f"BUY triggers: {s.get('buy_triggers', 0)}",
            f"SELL triggers: {s.get('sell_triggers', 0)}",
            f"Last direction: {s.get('last_side') or '-'}",
            f"Directional imbalance: {s.get('imbalance', 0):.2f}x",
            f"Direction changes: {s.get('direction_changes', 0)}",
            "",
            f"Open positions: {s.get('positions', 0)}",
            f"Pending orders: {s.get('orders', 0)}",
            f"Ladder depth used: {s.get('ladder_depth_used', 0)}",
            "",
            f"Basket P/L: {_money(s.get('cycle_profit', 0))}",
            f"Basket drawdown: {_money(s.get('basket_drawdown', 0))}",
            f"Daily P/L: {_money(s.get('daily_profit', 0))}",
            "",
            f"Momentum: {momentum_word} ({momentum:.2f})",
            f"Reversal: {reversal_word} ({reversal:.2f})",
            f"Exhaustion: {s.get('exhaustion_score', 0):.2f}",
            f"Exit score: {s.get('exit_score', 0):.0f}"
            f" / {s.get('settings', {}).get('exit_threshold_exit', 70):.0f}",
            f"Decision: {s.get('decision', '-')}",
            f"State: {market_state or '-'}",
        ]
        if s.get("reason"):
            lines.append(f"Why: {s['reason']}")
        lines += ["",
                  f"Last ladder update: "
                  f"{updated.strftime('%H:%M:%S') if updated else 'n/a'}"]
        if account:
            lines.append(f"Balance: {_money(account.balance)} | "
                         f"Equity: {_money(account.equity)}")
        if s.get("spread_blocked"):
            lines.append("\n⚠️ Spread too wide - new entries paused")
        if s.get("block_reason"):
            lines.append(f"\n⛔ Risk block: {s['block_reason']}")
        if s.get("error"):
            lines.append(f"\n⚠ Last error: {s['error']}")
        lines.append(f"\n<i>Updated {_now()}</i>")
        return "\n".join(lines)

    async def _ladder_text(self):
        """Live ladder: pending levels above and below, plus open positions."""
        s = await asyncio.to_thread(self.engine.status)
        orders = await asyncio.to_thread(self.engine.orders)
        positions = await asyncio.to_thread(self.engine.positions)
        d = 2
        buys = sorted([o for o in orders if o.side == "BUY_STOP"],
                      key=lambda o: o.price, reverse=True)
        sells = sorted([o for o in orders if o.side == "SELL_STOP"],
                       key=lambda o: o.price, reverse=True)

        lines = ["🪜 <b>ROLLING LADDER</b>", "",
                 f"Cycle #{s.get('cycle_id', 0)}   "
                 f"anchor {_num(s.get('anchor'))}   spacing {s.get('spacing')}",
                 f"State: {s.get('engine_state', '')}", ""]
        if buys:
            lines.append("<b>BUY STOPS</b>")
            lines += [f"  {o.price:.{d}f}  →  TP {o.tp:.{d}f}  ({o.volume})"
                      for o in buys]
        if s.get("bid"):
            lines.append(f"— price {s['bid']:.{d}f} / {s['ask']:.{d}f} —")
        if sells:
            lines.append("<b>SELL STOPS</b>")
            lines += [f"  {o.price:.{d}f}  →  TP {o.tp:.{d}f}  ({o.volume})"
                      for o in sells]
        if not orders:
            lines.append("No pending levels right now.")
        if positions:
            lines.append("")
            lines.append("<b>OPEN</b>")
            lines += [f"  {p.side} {p.volume} @ {p.price_open:.{d}f} "
                      f"→ TP {p.tp:.{d}f}  {_money(p.profit)}" for p in positions]
        lines.append(f"\n<i>Updated {_now()}</i>")
        return "\n".join(lines)

    async def _account_text(self):
        account = await asyncio.to_thread(self.engine.account)
        if not account:
            return ("💰 <b>ACCOUNT</b>\n\nAccount information is unavailable "
                    f"(terminal not connected).\n\n<i>Updated {_now()}</i>")
        return "\n".join([
            "💰 <b>ACCOUNT</b>", "",
            f"Login: {getattr(account, 'login', '')}",
            f"Server: {getattr(account, 'server', 'n/a')}",
            f"Currency: {getattr(account, 'currency', '')}",
            "",
            f"Balance: {_money(account.balance)}",
            f"Equity: {_money(account.equity)}",
            f"Margin: {_money(getattr(account, 'margin', 0))}",
            f"Free Margin: {_money(getattr(account, 'margin_free', 0))}",
            f"Margin Level: {getattr(account, 'margin_level', 0):.2f}%",
            f"Floating P/L: {_money(getattr(account, 'profit', 0))}",
            "",
            f"<i>Updated {_now()}</i>",
        ])

    async def _positions_text(self):
        positions = await asyncio.to_thread(self.engine.positions)
        orders = await asyncio.to_thread(self.engine.orders)
        if not positions and not orders:
            return ("📈 <b>OPEN POSITIONS</b>\n\nNo open positions."
                    f"\n\n<i>Updated {_now()}</i>")

        blocks = ["📈 <b>OPEN POSITIONS</b>", ""]
        total = 0.0
        for p in positions:
            total += p.profit
            blocks += [
                f"Symbol: {p.symbol}",
                f"Direction: {p.side}",
                f"Volume: {p.volume}",
                f"Entry: {p.price_open}",
                f"SL: {p.sl if p.sl else '-'}",
                f"TP: {p.tp if p.tp else '-'}",
                f"Profit: {_money(p.profit)}",
                f"Ticket: {p.ticket}",
                "",
            ]
        if not positions:
            blocks.append("No open positions.")
            blocks.append("")
        blocks.append(f"Total floating P/L: {_money(total)}")
        blocks.append(f"Pending ladder orders: {len(orders)}")
        blocks.append(f"\n<i>Updated {_now()}</i>")
        return "\n".join(blocks)

    async def _stats_text(self):
        stats = await asyncio.to_thread(self.engine.today_stats)
        if not stats.get("available"):
            return ("📋 <b>TODAY'S STATS</b>\n\nLadder history is unavailable: "
                    f"{stats.get('error', 'unknown error')}."
                    f"\n\n<i>Updated {_now()}</i>")

        lines = [
            "📋 <b>TODAY'S STATS</b>", "",
            f"Date: {stats['day']}",
            f"Orders placed: {stats.get('orders_placed', 0)}",
            f"Levels triggered: {stats.get('entries', 0)}",
            f"TP hits: {stats.get('tp_hits', 0)}",
            f"SL hits: {stats.get('sl_hits', 0)}",
            f"Cycles completed: {stats.get('cycles_completed', 0)}",
            f"Realized P/L: {_money(stats.get('realized', 0))}",
        ]
        if stats.get("closed"):
            lines += ["",
                      f"Closed trades: {stats['closed']}",
                      f"Wins: {stats.get('wins', 0)}  Losses: {stats.get('losses', 0)}"]
            if stats.get("win_rate") is not None:
                lines.append(f"Win rate: {stats['win_rate']:.1f}%")
        if not stats.get("entries") and not stats.get("orders_placed"):
            lines.append("\nNo ladder activity recorded yet today.")
        lines.append(f"\n<i>Updated {_now()}</i>")
        return "\n".join(lines)

    async def _panel_text(self):
        s = await asyncio.to_thread(self.engine.status)
        price = f"{s['bid']:.2f}" if s.get("bid") else "n/a"
        return "\n".join([
            "🤖 <b>XAUUSD ROLLING LADDER</b>", "",
            f"State: {s['icon']} {s['state']}   [{s.get('mode', '?')}]",
            f"Symbol: {s['symbol']} {s.get('timeframe', '')}   Price: {price}",
            f"MT5: {'Connected' if s['mt5_connected'] else 'Disconnected'}",
            "",
            f"Cycle #{s.get('cycle_id', 0)}  ·  "
            f"{s.get('buy_triggers', 0)}B/{s.get('sell_triggers', 0)}S  ·  "
            f"P/L {_money(s.get('cycle_profit', 0))}  ·  "
            f"exit {s.get('exit_score', 0):.0f}",
            f"Positions: {s.get('positions', 0)}   "
            f"Pending: {s.get('orders', 0)}",
            f"Spacing {s.get('spacing')} · TP {_num(s.get('tp_distance'))} · "
            f"Lot {s.get('lot')}",
            "",
            "Use the buttons below to control the bot.",
            f"\n<i>Updated {_now()}</i>",
        ])

    # =============================================================== COMMANDS
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            await self._panel_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=self.keyboard(),
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            "\n".join([
                "<b>Commands</b>",
                "/start - control panel",
                "/status - runtime status",
                "/account - live account info",
                "/positions - open positions",
                "/stats - today's statistics",
                "/pause - stop opening new trades",
                "/resume - allow new trades again",
                "/stopbot - stop the trading loop (positions stay open)",
                "/ladder - live ladder levels",
                "/settings - TP, lot, ladder and risk settings",
            ]),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_status(self, update, context):
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            await self._status_text(), parse_mode=ParseMode.HTML,
            reply_markup=self.keyboard())

    async def cmd_account(self, update, context):
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            await self._account_text(), parse_mode=ParseMode.HTML,
            reply_markup=self.keyboard())

    async def cmd_positions(self, update, context):
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            await self._positions_text(), parse_mode=ParseMode.HTML,
            reply_markup=self.keyboard())

    async def cmd_stats(self, update, context):
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            await self._stats_text(), parse_mode=ParseMode.HTML,
            reply_markup=self.keyboard())

    async def cmd_ladder(self, update, context):
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            await self._ladder_text(), parse_mode=ParseMode.HTML,
            reply_markup=self.keyboard())

    async def cmd_settings(self, update, context):
        if not await self._guard(update):
            return
        text, markup = await asyncio.to_thread(
            self.panel.render, "settings", update.effective_chat.id
        )
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Free-text CUSTOM values. Plain chatter is ignored, never answered."""
        chat = update.effective_chat
        message = update.effective_message
        if not (chat and message and message.text):
            return
        if not self.is_authorized(chat.id):
            return  # silence, not a reply - unauthorized chats get nothing
        if not self.panel.awaiting_input(chat.id):
            return
        try:
            result = await asyncio.to_thread(
                self.panel.handle_text, chat.id, message.text
            )
        except Exception as exc:
            self._log_error(f"Custom value failed: {exc}")
            await message.reply_text(f"⚠ Could not apply that value: {exc}")
            return
        if result is None:
            return
        text, markup = result
        await message.reply_text(text, parse_mode=ParseMode.HTML,
                                 reply_markup=markup)

    async def cmd_pause(self, update, context):
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            await self._action_text("pause"), parse_mode=ParseMode.HTML,
            reply_markup=self.keyboard())

    async def cmd_resume(self, update, context):
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            await self._action_text("resume"), parse_mode=ParseMode.HTML,
            reply_markup=self.keyboard())

    async def cmd_stop(self, update, context):
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            await self._action_text("stop"), parse_mode=ParseMode.HTML,
            reply_markup=self.keyboard())

    # ================================================================ BUTTONS
    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        chat = update.effective_chat
        if not (chat and self.is_authorized(chat.id)):
            await self._deny(update)
            return

        action = query.data
        await query.answer()
        markup = None

        try:
            if self.panel.handles(action):
                # settings menus render (and persist) off the event loop
                text, markup = await asyncio.to_thread(
                    self.panel.render, action, chat.id
                )
            elif action in ("start", "pause", "resume", "stop"):
                text = await self._action_text(action)
            elif action == "status":
                text = await self._status_text()
            elif action == "account":
                text = await self._account_text()
            elif action == "positions":
                text = await self._positions_text()
            elif action == "stats":
                text = await self._stats_text()
            elif action == "ladder":
                text = await self._ladder_text()
            else:  # refresh / panel / unknown
                text = await self._panel_text()
        except Exception as exc:
            self._log_error(f"Button '{action}' failed: {exc}")
            text = f"⚠ Command failed: {exc}\n\n<i>Updated {_now()}</i>"

        await self._edit(query, text, markup)

    async def _edit(self, query, text, markup=None):
        """Prefer editing the existing panel message over spamming new ones."""
        markup = markup or self.keyboard()
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            try:
                await query.message.reply_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=markup
                )
            except Exception as inner:
                self._log_error(f"Telegram edit/reply failed: {inner}")
        except Exception as exc:
            self._log_error(f"Telegram edit failed: {exc}")

    # ================================================================ ACTIONS
    async def _action_text(self, action):
        """Run a lifecycle action off the event loop and format the reply."""
        if action == "start":
            ok, msg = await asyncio.to_thread(self.engine.start)
            s = await asyncio.to_thread(self.engine.status)
            head = "🟢 <b>Trading Started</b>" if ok else "ℹ️ <b>Start</b>"
            return "\n".join([
                head,
                "",
                msg,
                f"Symbol: {s['symbol']}",
                f"Status: {s['state']}",
                f"MT5: {'Connected' if s['mt5_connected'] else 'Disconnected'}",
                f"\n<i>Updated {_now()}</i>",
            ])

        if action == "pause":
            ok, msg = await asyncio.to_thread(self.engine.pause)
            head = "⏸ <b>Trading Paused</b>" if ok else "ℹ️ <b>Pause</b>"
            return "\n".join([
                head,
                "",
                msg,
                "New ladder levels: DISABLED (pending orders cancelled)",
                "Existing positions: STILL MANAGED",
                f"\n<i>Updated {_now()}</i>",
            ])

        if action == "resume":
            ok, msg = await asyncio.to_thread(self.engine.resume)
            head = "▶️ <b>Trading Resumed</b>" if ok else "ℹ️ <b>Resume</b>"
            return "\n".join([
                head,
                "",
                msg,
                "New ladder levels: ENABLED",
                f"\n<i>Updated {_now()}</i>",
            ])

        if action == "stop":
            ok, msg = await asyncio.to_thread(self.engine.stop)
            s = await asyncio.to_thread(self.engine.status)
            head = "🛑 <b>Bot Stopped</b>" if ok else "ℹ️ <b>Stop</b>"
            return "\n".join([
                head,
                "",
                msg,
                "New trades: DISABLED",
                f"Open positions left untouched: {s.get('positions', 0)}",
                "Pending ladder orders were cancelled.",
                "Press 🟢 START TRADING to run again.",
                f"\n<i>Updated {_now()}</i>",
            ])

        return await self._panel_text()

    # ================================================================= ERRORS
    async def on_error(self, update, context):
        """Telegram errors are logged, never propagated into the engine."""
        self._log_error(f"Telegram error: {context.error}")
