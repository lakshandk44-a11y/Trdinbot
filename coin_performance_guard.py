"""
Coin Performance Auto-Guard (user request).

Automatically tracks each currently-scanned coin's own win/loss record
over a rolling lookback window (COIN_PERF_LOOKBACK_DAYS, default 30 days)
using trade_manager.trade_history - the exact same fee-adjusted
pnl_percent/win definition telegram_control._build_rate_text() already
uses ("win" = pnl_percent > 0). Once a coin has AT LEAST
COIN_PERF_MIN_TRADES closed trades in that window (so one or two early
losses can never trigger anything on their own - user's explicit
sample-size requirement), applies one of two independent auto-disable
tiers, worst-first:

  - PERMANENT: win rate <= COIN_PERF_PERMANENT_WINRATE_THRESHOLD ->
    disabled indefinitely. Only a manual Telegram override (turning the
    coin back ON from the "🪙 Coin List View" menu) clears it - see
    clear_manual_override() below.
  - COOLDOWN: win rate above the permanent threshold but <=
    COIN_PERF_COOLDOWN_WINRATE_THRESHOLD -> disabled for
    COIN_PERF_COOLDOWN_DAYS days, then automatically re-enabled on its
    own - no manual action needed.

DESIGN NOTES (why this is a separate file with its own state file):

  - Own persisted state (COIN_PERFORMANCE_STATE_FILE), completely
    separate from telegram_control.py's SETTINGS_OVERRIDE_FILE. Two
    independent subsystems (this module's background evaluation on the
    main trading thread; a manual override triggered from the Telegram
    polling thread) writing to the SAME file would risk one clobbering
    the other's concurrent change. Keeping them in separate files makes
    that race structurally impossible instead of relying on careful
    timing.
  - bot_core._filter_disabled_coins() is the ONLY place that actually
    blocks scanning/new entries - it checks BOTH telegram_control's
    manual disabled_coins AND this module's auto-disabled set (a coin is
    skipped if EITHER says off), so a coin already in an OPEN trade is -
    exactly like a manually-disabled coin - never dropped from active
    management, only ever blocked from a brand NEW entry. See that
    method's own docstring for exactly why that safety rule exists.
  - A manual override (admin taps a coin back ON in the Coin List View
    while it's auto-disabled) does not just delete the auto-disable
    entry - it also records a reset point (reset_at) so the SAME stale
    trades that triggered it can never immediately re-trigger the exact
    tier that was just overridden. Only genuinely NEW trades (closed
    after the override) count toward the next decision - otherwise a
    manual "give it another chance" would be silently undone on the very
    next evaluation cycle.
  - Fails open everywhere, same convention as every other guard in this
    codebase (Smart Hours Guard, Volatility Guard, etc.): any exception
    here is caught and logged, never allowed to affect scanning/trading
    itself, and the master COIN_PERFORMANCE_GUARD_ENABLED config flag
    (Telegram-toggleable, in the same menu grid as the other guards)
    turns the entire feature off instantly - every coin immediately
    returns to plain manual-only control, zero other behavior change.
  - Zero extra API calls of any kind (Binance or Telegram) - everything
    here reads data trade_manager already holds in memory
    (trade_manager.trade_history) and bot_core already computes
    (bot.last_scanned_coins). Alerts are handed back to the caller as
    plain dicts (pop_pending_alerts) rather than sent directly, so this
    module has no Telegram/network dependency of its own at all.
"""

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger("coin_performance_guard")


class CoinPerformanceGuard:
    """
    Owned by HackerAIBot (self.coin_performance_guard). bot_core calls
    evaluate_and_apply() once per scan cycle; telegram_control calls
    get_status()/clear_manual_override() when the admin manually toggles
    a coin from the Coin List View menu.
    """

    def __init__(self, bot_instance, config: Dict):
        self.bot = bot_instance  # gives access to bot.trade_manager / bot.last_scanned_coins
        self.config = config
        self.state_file = config.get(
            "COIN_PERFORMANCE_STATE_FILE",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "coin_performance_state.json"),
        )
        self.lock = threading.Lock()

        # symbol -> {"tier": "permanent"/"cooldown", "since": iso,
        #            "until": iso or None, "win_rate": float,
        #            "trades": int, "reason": str}
        self.auto_disabled: Dict[str, Dict] = {}
        # symbol -> iso timestamp of the last manual-override reset, so a
        # manually re-enabled coin is judged only on genuinely NEW trades
        # (see _recent_trades below) instead of the exact same stale
        # history that got it disabled in the first place.
        self.reset_at: Dict[str, str] = {}
        # Alerts collected during evaluate_and_apply(), for bot_core to
        # actually send via Telegram and then clear with
        # pop_pending_alerts() - only ever touched from the main trading
        # thread (evaluate_and_apply/pop_pending_alerts are both only
        # ever called from bot_core's main loop), so this list needs no
        # lock of its own.
        self.pending_alerts: List[Dict] = []

        self._load_state()

    # ------------------------------------------------------------------
    # Persistence - own file, own lock, never touches settings_override.json
    # ------------------------------------------------------------------
    def _load_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                auto_disabled = data.get("auto_disabled", {})
                reset_at = data.get("reset_at", {})
                # Defensive: ignore anything malformed rather than crash
                # on a hand-edited or older-version state file.
                self.auto_disabled = {
                    s: v for s, v in auto_disabled.items()
                    if isinstance(s, str) and isinstance(v, dict)
                }
                self.reset_at = {s: v for s, v in reset_at.items() if isinstance(s, str) and isinstance(v, str)}
                logger.info(f"🤖 Coin Performance Guard: restored {len(self.auto_disabled)} "
                            f"auto-disabled coin(s) from {self.state_file}")
        except Exception as e:
            logger.warning(f"Coin Performance Guard: could not load {self.state_file}: {e}")

    def _save_state(self):
        try:
            with self.lock:
                data = {"auto_disabled": dict(self.auto_disabled), "reset_at": dict(self.reset_at)}
            with open(self.state_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Coin Performance Guard: could not save {self.state_file}: {e}")

    # ------------------------------------------------------------------
    # Public read API (bot_core._filter_disabled_coins, telegram_control's
    # Coin List View rendering)
    # ------------------------------------------------------------------
    def is_disabled(self, symbol: str) -> bool:
        with self.lock:
            return symbol in self.auto_disabled

    def get_status(self, symbol: str) -> Optional[Dict]:
        with self.lock:
            entry = self.auto_disabled.get(symbol)
            return dict(entry) if entry else None

    def disabled_symbols(self) -> List[str]:
        with self.lock:
            return list(self.auto_disabled.keys())

    # ------------------------------------------------------------------
    # Manual-override integration - called from telegram_control.
    # _handle_toggle_coin() when the admin manually turns an
    # auto-disabled coin back ON.
    # ------------------------------------------------------------------
    def clear_manual_override(self, symbol: str) -> Optional[Dict]:
        """
        Removes any auto-disable flag for `symbol` and records a reset
        point so THIS SAME stale trade history can't immediately
        re-trigger the exact tier that was just manually overridden -
        only trades that close AFTER this moment count toward the next
        decision. Returns the entry that was cleared (or None if the
        coin wasn't auto-disabled to begin with), so the caller can
        mention what was overridden in its own Telegram alert.
        """
        with self.lock:
            cleared = self.auto_disabled.pop(symbol, None)
            self.reset_at[symbol] = datetime.now().isoformat()
        self._save_state()
        if cleared:
            logger.info(f"🤖 Coin Performance Guard: {symbol} auto-disable manually overridden "
                        f"(was {cleared.get('tier')}: {cleared.get('reason')}) - tracking reset.")
        return cleared

    # ------------------------------------------------------------------
    # Core evaluation - safe to call every scan cycle (purely local/
    # in-memory, zero API calls of any kind).
    # ------------------------------------------------------------------
    def evaluate_and_apply(self):
        if not self.config.get("COIN_PERFORMANCE_GUARD_ENABLED", True):
            return
        try:
            self._check_cooldown_expiries()
            self._check_new_disables()
        except Exception as e:
            logger.warning(f"Coin Performance Guard: evaluate_and_apply failed: {e}")

    def pop_pending_alerts(self) -> List[Dict]:
        alerts, self.pending_alerts = self.pending_alerts, []
        return alerts

    def _recent_trades(self, symbol: str) -> List[Dict]:
        """Closed trades for `symbol` within the lookback window, ignoring
        anything before the last manual-override reset (if any)."""
        lookback_days = self.config.get("COIN_PERF_LOOKBACK_DAYS", 30)
        cutoff = datetime.now() - timedelta(days=lookback_days)
        reset_dt = None
        reset_iso = self.reset_at.get(symbol)
        if reset_iso:
            try:
                reset_dt = datetime.fromisoformat(reset_iso)
            except Exception:
                reset_dt = None

        try:
            history = list(self.bot.trade_manager.trade_history)
        except Exception:
            return []

        out = []
        for t in history:
            if t.get("symbol") != symbol:
                continue
            close_time = t.get("close_time")
            if not isinstance(close_time, datetime):
                continue
            if close_time < cutoff:
                continue
            if reset_dt and close_time < reset_dt:
                continue
            out.append(t)
        return out

    def _check_cooldown_expiries(self):
        """Auto re-enables any coin whose COIN_PERF_COOLDOWN_DAYS timer is
        up - purely time-based, independent of any new trades."""
        now = datetime.now()
        expired = []
        with self.lock:
            for symbol, info in self.auto_disabled.items():
                if info.get("tier") != "cooldown":
                    continue
                until_iso = info.get("until")
                if not until_iso:
                    continue
                try:
                    until_dt = datetime.fromisoformat(until_iso)
                except Exception:
                    continue
                if now >= until_dt:
                    expired.append(symbol)

        for symbol in expired:
            with self.lock:
                self.auto_disabled.pop(symbol, None)
                self.reset_at[symbol] = now.isoformat()
            self.pending_alerts.append({
                "symbol": symbol, "action": "ON", "source": "auto",
                "reason": f"{self.config.get('COIN_PERF_COOLDOWN_DAYS', 30)}-day cooldown expired.",
            })
            logger.info(f"🤖 Coin Performance Guard: {symbol} cooldown expired - "
                        f"auto-ON, scanning resumed.")

        if expired:
            self._save_state()

    def _check_new_disables(self):
        """Evaluates only the coins actually being scanned right now
        (bot.last_scanned_coins) - never a symbol just because it happens
        to still be sitting in older trade_history but has since dropped
        out of the live top-N universe."""
        min_trades = self.config.get("COIN_PERF_MIN_TRADES", 12)
        permanent_threshold = self.config.get("COIN_PERF_PERMANENT_WINRATE_THRESHOLD", 25.0)
        cooldown_threshold = self.config.get("COIN_PERF_COOLDOWN_WINRATE_THRESHOLD", 40.0)
        cooldown_days = self.config.get("COIN_PERF_COOLDOWN_DAYS", 30)

        symbols = list(getattr(self.bot, "last_scanned_coins", None) or [])
        changed = False

        for symbol in symbols:
            with self.lock:
                already = self.auto_disabled.get(symbol)
            if already:
                # Permanent stays permanent until a manual override;
                # cooldown is handled purely by _check_cooldown_expiries
                # above - never re-evaluated (and potentially
                # re-triggered) mid-cooldown here.
                continue

            trades = self._recent_trades(symbol)
            if len(trades) < min_trades:
                continue

            wins = sum(1 for t in trades if t.get("pnl_percent", 0) > 0)
            win_rate = (wins / len(trades)) * 100

            if win_rate <= permanent_threshold:
                tier = "permanent"
                until = None
                reason = (f"Win rate {win_rate:.1f}% over last {len(trades)} trades "
                          f"(<= {permanent_threshold:.0f}% permanent threshold).")
            elif win_rate <= cooldown_threshold:
                tier = "cooldown"
                until = (datetime.now() + timedelta(days=cooldown_days)).isoformat()
                reason = (f"Win rate {win_rate:.1f}% over last {len(trades)} trades "
                          f"(<= {cooldown_threshold:.0f}% cooldown threshold).")
            else:
                continue  # performance is fine - nothing to do

            with self.lock:
                self.auto_disabled[symbol] = {
                    "tier": tier, "since": datetime.now().isoformat(), "until": until,
                    "win_rate": round(win_rate, 1), "trades": len(trades), "reason": reason,
                }
            changed = True
            self.pending_alerts.append({
                "symbol": symbol, "action": "OFF", "source": "auto",
                "tier": tier, "reason": reason,
            })
            logger.warning(f"🤖 Coin Performance Guard: {symbol} auto-disabled "
                            f"({tier.upper()}) - {reason}")

        if changed:
            self._save_state()
