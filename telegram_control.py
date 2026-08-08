"""
Telegram remote-control panel for the trading bot (user request).

Lets you flip a small set of SAFE settings and pause/resume the bot
straight from Telegram, using tappable buttons - no need to SSH into the
server for these. Deliberately does NOT expose anything that could leak
credentials or require a restart: API keys and testnet/real-account
switching are NOT here on purpose (see the toggles list below for why).

Uses Telegram's "long polling" (getUpdates) - no public webhook/domain/SSL
needed, works from behind any EC2 security group exactly like the existing
one-way notifications in telegram_notify.py already do.

Setup:
  Reuses the SAME TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID environment
  variables telegram_notify.py already uses - nothing new to configure
  there. Only your own chat can issue commands (see TELEGRAM_ADMIN_CHAT_ID
  in config.py) - any other chat's messages/button-taps are silently
  ignored.

This is intentionally fail-safe: any error here (network blip, bad
response, etc.) is caught and logged - it can NEVER crash or block the
actual trading loop, which runs completely independently on its own thread.
"""

import json
import logging
import os
import threading
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger("telegram_control")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
POLL_TIMEOUT_SECONDS = 25  # Telegram long-poll wait
REQUEST_TIMEOUT_SECONDS = 30  # HTTP timeout, must exceed POLL_TIMEOUT_SECONDS

# ----------------------------------------------------------------------
# Only these config keys are togglable from Telegram - deliberately a
# small, hand-picked allowlist of settings that are safe to flip live
# (no restart needed, no credentials, no exchange-switching).
# ----------------------------------------------------------------------
TOGGLE_DEFINITIONS = [
    {"code": "TP1",   "config_key": "TP1_REANALYSIS_ENABLED",       "label": "TP1/TP2/TP3 Reanalysis"},
    {"code": "HOURS", "config_key": "TRADING_HOURS_FILTER_ENABLED", "label": "Trading Hours Filter"},
    {"code": "SMT",   "config_key": "SMT_DIVERGENCE_ENABLED",       "label": "SMT Divergence"},
    {"code": "PATTERN", "config_key": "PATTERN_ENGINE_ENABLED",     "label": "Pattern Engine (Phase 1)"},
    # ADDED (user request): ON = Isolated margin, OFF = Cross margin.
    # Bot reads this exact config key (bot_core._execute_trade) right
    # before opening every new trade and sets the symbol's margin mode on
    # Binance to match, whichever is toggled ON here at that moment.
    # Nothing else about how/when a trade opens changes either way.
    {"code": "ISOMGN", "config_key": "USE_ISOLATED_MARGIN",         "label": "Isolated Margin (OFF = Cross)"},
]


class TelegramController:
    """
    Polls Telegram for commands/button-taps from the admin chat only, and
    applies SAFE, in-memory + disk-persisted setting changes to the
    already-running bot. Runs on its own daemon thread - completely
    independent of the trading/monitoring loops.
    """

    def __init__(self, bot_instance, config: Dict):
        self.bot = bot_instance          # HackerAIBot instance (reads/writes .paused, .config)
        self.config = config
        self.admin_chat_id = str(config.get("TELEGRAM_ADMIN_CHAT_ID", TELEGRAM_CHAT_ID))
        self.override_file = config.get("SETTINGS_OVERRIDE_FILE", "settings_override.json")
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self._offset = 0

        if not TELEGRAM_BOT_TOKEN or not self.admin_chat_id:
            logger.warning("⚠️ Telegram control panel disabled - "
                            "TELEGRAM_BOT_TOKEN or admin chat id not set.")
            self.enabled = False
        else:
            self.enabled = True
            self._load_and_apply_overrides()

    # ------------------------------------------------------------------
    # Persistence: overrides survive a bot/VPS restart, same pattern as
    # trade_manager's trade_state.json.
    # ------------------------------------------------------------------
    def _load_and_apply_overrides(self):
        try:
            if os.path.exists(self.override_file):
                with open(self.override_file, "r") as f:
                    overrides = json.load(f)
                for tog in TOGGLE_DEFINITIONS:
                    if tog["config_key"] in overrides:
                        self.config[tog["config_key"]] = overrides[tog["config_key"]]
                if "PAUSED" in overrides:
                    self.bot.paused = bool(overrides["PAUSED"])
                logger.info(f"🎛️ Telegram control: restored saved settings from {self.override_file}")
        except Exception as e:
            logger.warning(f"Telegram control: could not load {self.override_file}: {e}")

    def _save_overrides(self):
        try:
            data = {tog["config_key"]: bool(self.config.get(tog["config_key"], False))
                    for tog in TOGGLE_DEFINITIONS}
            data["PAUSED"] = bool(self.bot.paused)
            with open(self.override_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Telegram control: could not save {self.override_file}: {e}")

    # ------------------------------------------------------------------
    # Telegram HTTP helpers
    # ------------------------------------------------------------------
    def _api_call(self, method: str, payload: Dict) -> Optional[Dict]:
        try:
            resp = requests.post(f"{API_BASE}/{method}", json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            data = resp.json()
            if not data.get("ok"):
                logger.warning(f"Telegram control: {method} failed: {data}")
                return None
            return data.get("result")
        except Exception as e:
            logger.warning(f"Telegram control: {method} request failed: {e}")
            return None

    def _get_updates(self):
        try:
            resp = requests.get(
                f"{API_BASE}/getUpdates",
                params={"offset": self._offset, "timeout": POLL_TIMEOUT_SECONDS},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            data = resp.json()
            if not data.get("ok"):
                return []
            return data.get("result", [])
        except Exception as e:
            logger.debug(f"Telegram control: getUpdates failed (will retry): {e}")
            return []

    # ------------------------------------------------------------------
    # Menu / status rendering
    # ------------------------------------------------------------------
    def _toggle_button_text(self, tog: Dict) -> str:
        on = bool(self.config.get(tog["config_key"], False))
        return f"{'✅' if on else '❌'} {tog['label']}"

    def _pause_button_text(self) -> str:
        return "▶️ Resume Bot" if self.bot.paused else "⏸️ Pause Bot"

    def _build_menu_keyboard(self) -> Dict:
        rows = [[{"text": self._toggle_button_text(tog), "callback_data": f"t:{tog['code']}"}]
                for tog in TOGGLE_DEFINITIONS]
        rows.append([{"text": self._pause_button_text(), "callback_data": "t:PAUSE"}])
        rows.append([{"text": "📊 Status", "callback_data": "status"}])
        return {"inline_keyboard": rows}

    def _send_menu(self, chat_id: str, text: str = "🎛️ *Bot Control Panel*\nTap to toggle:"):
        self._api_call("sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": "Markdown",
            "reply_markup": self._build_menu_keyboard(),
        })

    def _edit_menu(self, chat_id: str, message_id: int, text: str):
        self._api_call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "Markdown", "reply_markup": self._build_menu_keyboard(),
        })

    def _build_status_text(self) -> str:
        state = "⏸️ PAUSED (not opening new trades)" if self.bot.paused else "▶️ RUNNING"
        balance = getattr(self.bot, "balance", None)
        balance_line = f"Balance: {balance:.2f} USDT\n" if balance is not None else ""

        open_trades = []
        try:
            open_trades = list(self.bot.trade_manager.open_trades.values())
        except Exception:
            pass
        if open_trades:
            trade_lines = "\n".join(
                f"  • {t.get('symbol')} ({t.get('side')}) entry {t.get('entry_price'):.6f}, "
                f"TP{t.get('tp_stage', 1)}"
                for t in open_trades
            )
        else:
            trade_lines = "  (none)"

        toggles_text = "\n".join(f"  {self._toggle_button_text(tog)}" for tog in TOGGLE_DEFINITIONS)

        return (
            f"🎛️ *Bot Status*\n"
            f"Status: {state}\n"
            f"{balance_line}"
            f"Open trades ({len(open_trades)}):\n{trade_lines}\n\n"
            f"Settings:\n{toggles_text}"
        )

    def _build_rate_text(self) -> str:
        """
        FIX (user request, /rate command): win/loss rate of every trade
        closed so far, from trade_manager's own trade_history (the same
        list get_trade_history() reads from). "Win"/"Loss" uses the exact
        same rule trade_manager already uses at close time (pnl_percent >
        0, which is the fee-adjusted NET result, not the raw price move -
        see the "Real win-rate bug" fix in trade_manager._close_trade),
        so this always agrees with what's logged/Telegram'd at close.
        """
        history = []
        try:
            history = list(self.bot.trade_manager.trade_history)
        except Exception:
            pass

        total = len(history)
        if total == 0:
            return "📊 *Trade Rate*\n\nNo closed trades yet."

        wins = sum(1 for t in history if t.get("pnl_percent", 0) > 0)
        losses = total - wins
        win_pct = (wins / total) * 100
        loss_pct = (losses / total) * 100

        return (
            f"📊 *Trade Rate* (all closed trades so far)\n\n"
            f"Total Trades: {total}\n"
            f"🟢 Win: {wins}  ({win_pct:.1f}%)\n"
            f"🔴 Loss: {losses}  ({loss_pct:.1f}%)"
        )

    # ------------------------------------------------------------------
    # Command menu registration (Telegram's native "/" command list)
    # ------------------------------------------------------------------
    def _register_bot_commands(self):
        """
        FIX (user request): register every slash command with Telegram's
        setMyCommands API so they all show up in the native "/" command
        menu next to the message box - nothing to remember/forget.
        Fire-and-forget like every other Telegram call here (_api_call
        already catches/logs errors) - harmless if it fails, just falls
        back to typing commands manually as before.
        """
        commands = [
            {"command": "start", "description": "Show control panel buttons"},
            {"command": "menu", "description": "Show control panel buttons"},
            {"command": "status", "description": "Bot status + open trades"},
            {"command": "rate", "description": "Win/loss rate of closed trades"},
            {"command": "help", "description": "List all commands"},
        ]
        self._api_call("setMyCommands", {"commands": commands})

    # ------------------------------------------------------------------
    # Update handling
    # ------------------------------------------------------------------
    def _handle_toggle(self, code: str):
        if code == "PAUSE":
            self.bot.paused = not self.bot.paused
            logger.info(f"🎛️ Telegram control: bot {'PAUSED' if self.bot.paused else 'RESUMED'} "
                        f"(new trades only - open trades keep being managed normally)")
        else:
            tog = next((t for t in TOGGLE_DEFINITIONS if t["code"] == code), None)
            if tog:
                current = bool(self.config.get(tog["config_key"], False))
                self.config[tog["config_key"]] = not current
                logger.info(f"🎛️ Telegram control: {tog['config_key']} -> {not current}")
        self._save_overrides()

    def _handle_update(self, update: Dict):
        if "callback_query" in update:
            cq = update["callback_query"]
            chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
            message_id = cq.get("message", {}).get("message_id")
            data = cq.get("data", "")
            self._api_call("answerCallbackQuery", {"callback_query_id": cq["id"]})

            if chat_id != self.admin_chat_id:
                return  # silently ignore anyone else

            if data == "status":
                self._edit_menu(chat_id, message_id, self._build_status_text())
            elif data.startswith("t:"):
                self._handle_toggle(data[2:])
                self._edit_menu(chat_id, message_id, "🎛️ *Bot Control Panel*\nTap to toggle:")
            return

        if "message" in update:
            msg = update["message"]
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip().lower()
            if chat_id != self.admin_chat_id:
                return  # silently ignore anyone else

            if text in ("/start", "/menu"):
                self._send_menu(chat_id)
            elif text == "/status":
                self._send_menu(chat_id, self._build_status_text())
            elif text == "/rate":
                self._api_call("sendMessage", {
                    "chat_id": chat_id, "text": self._build_rate_text(), "parse_mode": "Markdown",
                })
            elif text == "/help":
                self._api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": ("Commands:\n"
                              "/menu - show control panel buttons\n"
                              "/status - show bot status + open trades\n"
                              "/rate - win/loss rate of closed trades\n"
                              "/help - this message"),
                })

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------
    def _poll_loop(self):
        logger.info("🎛️ Telegram control panel listening (admin chat only)...")
        while self.running:
            updates = self._get_updates()
            for update in updates:
                self._offset = update["update_id"] + 1
                try:
                    self._handle_update(update)
                except Exception as e:
                    logger.warning(f"Telegram control: error handling update: {e}")
            if not updates:
                time.sleep(1)  # avoid a tight loop if getUpdates ever returns instantly empty

    def start(self):
        if not self.enabled:
            return
        self._register_bot_commands()
        self.running = True
        self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
