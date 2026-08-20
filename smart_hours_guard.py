"""
HackerAI Trading Bot — Smart Hours Guard  (v2)
================================================
Learns from the bot's OWN recent trade history which hours of the day
have a consistently high loss rate, and automatically blocks new entries
during those hours.

WHAT MAKES v2 MORE RELIABLE THAN A SIMPLE HOURLY AVERAGE
----------------------------------------------------------
Three problems plagued a naive "count losses per hour" approach:

  1. THIN DATA / COINCIDENCE
     Three losses at 03:xx all on ONE unlucky Tuesday looks like a
     pattern but isn't.  v2 requires losses to be spread across at
     least MIN_SPREAD_DAYS different calendar days before an hour is
     marked bad — one bad day can't block an hour by itself.

  2. STALE DATA
     A loss from 28 days ago should matter far less than one from
     yesterday.  v2 applies a linear recency weight so recent trades
     count at full value (1.0) while older trades decay toward 0.1.
     The WEIGHTED loss rate — not the raw count — decides blocking.

  3. NOT ENOUGH HISTORY
     7 days of data at 5 trades/day = 35 trades total. Split across
     24 hours that's ~1.5 per hour — statistically meaningless.  v2
     uses a 30-day lookback and requires at least 20 total trades
     and 4 trades per hour before any blocking decision is made.

ALGORITHM SUMMARY
-----------------
Every REANALYZE_DAYS days (and at startup):

  for each hour h in 0..23:
      collect all trades from the last LOOKBACK_DAYS days entered at hour h
      if count < MIN_SAMPLES → skip (not enough data)

      compute recency weight for each trade:
          weight_i = max(0.10, 1.0 – days_ago_i / LOOKBACK_DAYS)

      weighted_loss_rate = Σ weight_i[loss] / Σ weight_i[all]

      loss_dates = set of calendar dates on which losses occurred
      if len(loss_dates) < MIN_SPREAD_DAYS → skip (pattern on too few days)

      if weighted_loss_rate >= LOSS_RATE_THRESHOLD → mark h as BAD

  apply sanity cap (never block > 12 hours)
  persist bad_hours; send Telegram alert if the set changed

DESIGN PRINCIPLES
-----------------
  Zero coupling    : no imports from bot_core / analysis_engine / pattern_engine
  Thread-safe      : all state mutations under a single internal lock;
                     history access is a snapshot taken under trade_manager's lock
  Restart-safe     : full state persisted in trade_state.json; analysis
                     always re-runs at startup to pick up new trades
  Fail-safe        : import error or any exception → guard = None → old behaviour
  Sanity-capped    : never blocks more than 12 hours regardless of data shape
"""

import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_MAX_BLOCKED_HOURS = 12          # absolute cap — never silence more than half the day


class SmartHoursGuard:
    """
    History-driven hourly entry gate.

    Parameters
    ----------
    config : dict
        Live bot config dict — read on every call so Telegram toggles
        take effect without a restart.
    trade_history_fn : () -> List[Dict]
        Callable that returns a thread-safe SNAPSHOT of closed trades.
        Should be trade_manager._get_trade_history_snapshot().
    alert_callback : (str) -> None  |  None
        Called with a Markdown message when the bad-hours set changes.
    """

    def __init__(
        self,
        config: Dict,
        trade_history_fn: Callable[[], List[Dict]],
        alert_callback: Optional[Callable[[str], None]] = None,
    ):
        self.config           = config
        self._get_history     = trade_history_fn
        self.alert_callback   = alert_callback
        self._lock            = threading.Lock()

        # --- persisted state ---
        self._bad_hours:    Set[int]          = set()
        self._hourly_stats: Dict[int, Dict]   = {}   # hour → {raw_wins, raw_losses, w_loss_rate, spread_days}
        self._last_analysis:   Optional[datetime] = None
        self._next_analysis:   Optional[datetime] = None
        self._trades_analyzed: int = 0

        # --- runtime only ---
        self._analyzing: bool = False

    # ------------------------------------------------------------------ #
    # Public interface
    # ------------------------------------------------------------------ #

    def is_blocked_now(self) -> bool:
        """
        Returns True when the current hour is historically bad.
        Also triggers a scheduled re-analysis if one is due.
        """
        if not self.config.get("SMART_HOURS_GUARD_ENABLED", True):
            return False
        self._maybe_analyze()
        with self._lock:
            return datetime.now().hour in self._bad_hours

    def get_status_text(self) -> str:
        """One-line summary for the Telegram /status panel."""
        try:
            if not self.config.get("SMART_HOURS_GUARD_ENABLED", True):
                return "🔕 Disabled"

            with self._lock:
                ch   = datetime.now().hour
                bad  = sorted(self._bad_hours)

                if not bad:
                    state = "✅ Clear"
                elif ch in self._bad_hours:
                    state = f"🔴 BLOCKED ({ch:02d}:xx)"
                else:
                    state = f"✅ Clear now"

                bad_str = ", ".join(f"{h:02d}:xx" for h in bad) if bad else "none"

                if self._next_analysis:
                    secs = max(0, (self._next_analysis - datetime.now()).total_seconds())
                    if secs < 3600:
                        next_str = f"in {int(secs//60)}m"
                    else:
                        next_str = f"in {int(secs//86400)}d"
                else:
                    next_str = "pending"

                note = (f" | {self._trades_analyzed} trades / 30d"
                        if self._trades_analyzed else "")

            return f"{state} | Bad: [{bad_str}] | Next analysis {next_str}{note}"

        except Exception:
            return "⚠️ status unavailable"

    # ------------------------------------------------------------------ #
    # Persistence  (called by trade_manager._save_state / _load_state)
    # ------------------------------------------------------------------ #

    def get_state(self) -> Dict:
        with self._lock:
            return {
                "bad_hours":       sorted(self._bad_hours),
                "hourly_stats":    {str(k): v for k, v in self._hourly_stats.items()},
                "last_analysis":   self._last_analysis.isoformat() if self._last_analysis else None,
                "next_analysis":   self._next_analysis.isoformat() if self._next_analysis else None,
                "trades_analyzed": self._trades_analyzed,
            }

    def restore_state(self, state: Dict) -> None:
        """
        Load state from trade_state.json.
        Always forces a fresh analysis on the next is_blocked_now() call
        so the guard immediately reflects any trades that happened while
        the bot was down.
        """
        if not state:
            return

        with self._lock:
            try:
                self._bad_hours = set(int(h) for h in state.get("bad_hours", []))
            except (TypeError, ValueError):
                self._bad_hours = set()

            try:
                raw = state.get("hourly_stats", {})
                self._hourly_stats = {int(k): v for k, v in raw.items()}
            except Exception:
                self._hourly_stats = {}

            for jkey, attr in [("last_analysis", "_last_analysis"),
                                ("next_analysis", "_next_analysis")]:
                v = state.get(jkey)
                setattr(self, attr, datetime.fromisoformat(v) if v else None)

            self._trades_analyzed = int(state.get("trades_analyzed", 0))

            # Force immediate re-analysis on first call after startup
            self._next_analysis = datetime.now()

        logger.info(
            f"♻️ SmartHoursGuard restored: bad_hours={sorted(self._bad_hours)} "
            f"→ re-analysis on first entry check."
        )

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _maybe_analyze(self) -> None:
        with self._lock:
            if self._analyzing:
                return
            if self._next_analysis and datetime.now() < self._next_analysis:
                return
            self._analyzing = True
        try:
            self._run_analysis()
        except Exception as exc:
            logger.error(f"SmartHoursGuard: analysis error: {exc}", exc_info=True)
        finally:
            with self._lock:
                self._analyzing = False

    def _run_analysis(self) -> None:
        # --- read config fresh so live changes take effect ---
        lookback_days   = max(1,   int(self.config.get("SMART_HOURS_LOOKBACK_DAYS",       30)))
        reanalyze_days  = max(1,   int(self.config.get("SMART_HOURS_REANALYZE_DAYS",       3)))
        min_total       = max(1,   int(self.config.get("SMART_HOURS_MIN_TOTAL_TRADES",     20)))
        min_samples     = max(1,   int(self.config.get("SMART_HOURS_MIN_SAMPLES",           4)))
        min_spread_days = max(1,   int(self.config.get("SMART_HOURS_MIN_SPREAD_DAYS",       2)))
        threshold       = max(0.0, min(1.0, float(
                              self.config.get("SMART_HOURS_LOSS_RATE_THRESHOLD",         0.70))))

        # --- thread-safe history snapshot ---
        try:
            all_trades: List[Dict] = self._get_history()
        except Exception as exc:
            logger.warning(f"SmartHoursGuard: history read failed: {exc}")
            with self._lock:
                self._next_analysis = datetime.now() + timedelta(hours=1)
            return

        now    = datetime.now()
        cutoff = now - timedelta(days=lookback_days)

        # --- filter: closed trades with valid entry_time and pnl ---
        recent: List[tuple] = []          # (entry_datetime, pnl_float)
        for t in all_trades:
            et = t.get("entry_time")
            if isinstance(et, str):
                try:
                    et = datetime.fromisoformat(et)
                except (ValueError, TypeError):
                    continue
            if not isinstance(et, datetime) or et < cutoff:
                continue
            pnl = t.get("pnl_percent")
            if pnl is None:
                continue
            try:
                pnl = float(pnl)
            except (TypeError, ValueError):
                continue
            recent.append((et, pnl))

        total_trades = len(recent)
        next_analysis = now + timedelta(days=reanalyze_days)

        # --- not enough data → lift all blocks ---
        if total_trades < min_total:
            with self._lock:
                old_bad = set(self._bad_hours)
                self._bad_hours.clear()
                self._hourly_stats    = {}
                self._last_analysis   = now
                self._next_analysis   = next_analysis
                self._trades_analyzed = total_trades

            logger.info(
                f"📊 SmartHoursGuard: {total_trades}/{min_total} trades in "
                f"last {lookback_days}d — insufficient data, no blocks."
            )
            if old_bad:
                self._send_alert(
                    f"📊 *Smart Hours Guard — Updated*\n"
                    f"Not enough recent history ({total_trades} trades; "
                    f"need {min_total}).\n✅ All hours unblocked.\n"
                    f"Next re-analysis in {reanalyze_days} days."
                )
            return

        # --- group by entry hour with recency weighting ---
        #
        # weight_i = max(0.10, 1.0 – days_ago / lookback_days)
        #   today   → 1.00   (full weight)
        #   day 15  → 0.50   (half weight)
        #   day 29  → 0.10   (minimum weight, still counted)
        #
        # For spread check we also track which CALENDAR DATES had losses
        # at each hour so we can require losses across 2+ different days.

        hour_data: Dict[int, Dict] = defaultdict(lambda: {
            "w_loss": 0.0, "w_win": 0.0,
            "raw_loss": 0, "raw_win": 0,
            "loss_dates": set(),           # dates (date objects) with losses
        })

        for entry_dt, pnl in recent:
            days_ago = max(0.0, (now - entry_dt).total_seconds() / 86400.0)
            weight   = max(0.10, 1.0 - days_ago / lookback_days)
            h        = entry_dt.hour

            if pnl > 0:
                hour_data[h]["w_win"]   += weight
                hour_data[h]["raw_win"] += 1
            else:
                hour_data[h]["w_loss"]        += weight
                hour_data[h]["raw_loss"]       += 1
                hour_data[h]["loss_dates"].add(entry_dt.date())

        # --- identify bad hours ---
        bad_hours:   Set[int]        = set()
        hourly_stats: Dict[int, Dict] = {}

        for h, d in hour_data.items():
            raw_total = d["raw_win"] + d["raw_loss"]
            w_total   = d["w_win"]   + d["w_loss"]

            if raw_total < min_samples or w_total == 0:
                hourly_stats[h] = {
                    "raw_wins":    d["raw_win"],
                    "raw_losses":  d["raw_loss"],
                    "w_loss_rate": None,
                    "spread_days": len(d["loss_dates"]),
                    "blocked":     False,
                    "skip_reason": f"only {raw_total} trades (need {min_samples})",
                }
                continue

            w_loss_rate = d["w_loss"] / w_total
            spread_days = len(d["loss_dates"])

            reason = None
            if w_loss_rate < threshold:
                reason = f"loss rate {w_loss_rate*100:.0f}% < {threshold*100:.0f}%"
            elif spread_days < min_spread_days:
                reason = f"losses on {spread_days} day(s) only (need {min_spread_days})"

            blocked = reason is None
            if blocked:
                bad_hours.add(h)

            hourly_stats[h] = {
                "raw_wins":    d["raw_win"],
                "raw_losses":  d["raw_loss"],
                "w_loss_rate": round(w_loss_rate, 4),
                "spread_days": spread_days,
                "blocked":     blocked,
                "skip_reason": reason,
            }

        # --- sanity cap: never block more than half the day ---
        if len(bad_hours) > _MAX_BLOCKED_HOURS:
            ranked = sorted(
                bad_hours,
                key=lambda h: hour_data[h]["w_loss"] /
                              max(0.0001, hour_data[h]["w_win"] + hour_data[h]["w_loss"]),
                reverse=True,
            )
            removed = set(ranked[_MAX_BLOCKED_HOURS:])
            bad_hours = set(ranked[:_MAX_BLOCKED_HOURS])
            for h in removed:
                hourly_stats[h]["blocked"]     = False
                hourly_stats[h]["skip_reason"] = "sanity cap (max 12 blocked hours)"
            logger.warning(
                f"SmartHoursGuard: sanity cap — kept {len(bad_hours)} worst hours."
            )

        # --- commit ---
        with self._lock:
            old_bad               = set(self._bad_hours)
            self._bad_hours       = bad_hours
            self._hourly_stats    = hourly_stats
            self._last_analysis   = now
            self._next_analysis   = next_analysis
            self._trades_analyzed = total_trades

        changed = old_bad != bad_hours

        # --- detailed log ---
        for h in sorted(hourly_stats):
            s = hourly_stats[h]
            if s["w_loss_rate"] is not None:
                tag = "🔴 BAD" if s["blocked"] else "✅ ok "
                logger.info(
                    f"  SmartHoursGuard {h:02d}:xx {tag} | "
                    f"{s['raw_losses']}L/{s['raw_wins']}W raw | "
                    f"w_loss={s['w_loss_rate']*100:.0f}% | "
                    f"spread={s['spread_days']}d"
                    + (f" | skip: {s['skip_reason']}" if s['skip_reason'] else "")
                )

        logger.info(
            f"📊 SmartHoursGuard complete: {total_trades} trades/{lookback_days}d | "
            f"bad={sorted(bad_hours)} | changed={changed} | "
            f"next in {reanalyze_days}d"
        )

        if changed:
            self._send_analysis_alert(
                total_trades, lookback_days, hourly_stats,
                bad_hours, old_bad, min_samples, min_spread_days,
                threshold, reanalyze_days,
            )

    def _send_analysis_alert(
        self, total_trades, lookback_days, stats,
        bad_hours, old_bad, min_samples, min_spread_days,
        threshold, reanalyze_days,
    ) -> None:

        added   = bad_hours - old_bad
        removed = old_bad   - bad_hours

        lines = []
        for h in sorted(bad_hours):
            s = stats.get(h, {})
            w  = s.get("raw_wins",   0)
            l  = s.get("raw_losses", 0)
            lr = (s.get("w_loss_rate") or 0) * 100
            sp = s.get("spread_days", 0)
            tag = " 🆕" if h in added else ""
            lines.append(
                f"  • `{h:02d}:xx`{tag} → {w}W/{l}L  "
                f"({lr:.0f}% wt-loss, {sp} different days)"
            )

        change_parts = []
        if added:
            change_parts.append(
                "🆕 *Newly blocked*: "
                + ", ".join(f"`{h:02d}:xx`" for h in sorted(added))
            )
        if removed:
            change_parts.append(
                "✅ *Unblocked*: "
                + ", ".join(f"`{h:02d}:xx`" for h in sorted(removed))
            )

        if bad_hours:
            body = (
                f"📊 *Smart Hours Guard — Updated*\n\n"
                f"Analysed *{total_trades}* trades · last *{lookback_days}* days\n"
                f"Criteria: ≥{threshold*100:.0f}% weighted loss · "
                f"≥{min_samples} trades · losses on ≥{min_spread_days} different days\n\n"
                f"🔴 *Hours blocked (new entries skipped):*\n"
                + ("\n".join(lines) if lines else "  (none)")
                + ("\n\n" + "\n".join(change_parts) if change_parts else "")
                + f"\n\nNext re-analysis in *{reanalyze_days}* days."
            )
        else:
            body = (
                f"📊 *Smart Hours Guard — Updated*\n\n"
                f"Analysed *{total_trades}* trades · last *{lookback_days}* days.\n"
                f"✅ No consistently bad hours found — *all hours open*.\n"
                + ("\n".join(change_parts) + "\n" if change_parts else "")
                + f"Next re-analysis in *{reanalyze_days}* days."
            )

        self._send_alert(body)

    def _send_alert(self, message: str) -> None:
        if not self.alert_callback:
            return
        try:
            self.alert_callback(message)
        except Exception as exc:
            logger.warning(f"SmartHoursGuard: alert failed: {exc}")
