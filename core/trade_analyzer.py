"""
Trade Analyzer - Self-Learning Module (v3)

Three learning layers that analyze every completed trade and automatically
adjust strategy weights and behavior to maximize profit over time.

Layer 1 - Strategy Scorecard          per-strategy performance tracking
Layer 2 - Adaptive Weight Tuner       auto-adjust ensemble weights based on results
Layer 3 - Pattern Memory              remember good/bad entry setups and act on them
Layer 4 - Regime-Aware Tuner          separate weights per market regime
Layer 5 - Daily Journal               human-readable EOD reflection

v3 upgrades over v2:
    * Per-strategy attribution: ensemble trades are split across contributing
      strategies using `contributing_strategies` shares, so each strategy gets
      fractional PnL credit instead of everything being attributed to "ensemble".
    * Regime-aware weights: strategy weights are learned separately for each
      market regime (bull/bear x low/high vol), selected at decision time.
    * Enriched pattern memory: exit_reason, holding_minutes, pnl_bucket, regime.
    * Learning freeze guardrail: skip weight updates during drawdown regimes
      so we don't amplify losing strategies in bad markets.
    * Daily journal: writes a plain-English summary to `logs/learning_journal.md`.
"""

import math
import os
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger


class TradeAnalyzer:
    """
    Learns from every closed trade to improve future decisions.

    Wired into the trading agent: called after every trade close and before
    every new trade entry. Persists all learned state to SQLite.
    """

    def __init__(self, config: dict, database):
        self._db = database
        lcfg = config.get("learning", {})
        self.enabled: bool = lcfg.get("enabled", True)
        self.min_trades: int = lcfg.get("min_trades_to_learn", 10)
        self.update_interval: int = lcfg.get("weight_update_interval", 10)
        self.decay_factor: float = lcfg.get("decay_factor", 0.95)
        self.pattern_lookback: int = lcfg.get("pattern_lookback", 50)
        self.max_weight: float = lcfg.get("max_weight", 5.0)
        self.min_weight: float = lcfg.get("min_weight", 0.1)
        self.pattern_similarity_threshold: float = lcfg.get("pattern_similarity_threshold", 0.75)

        # Learning-freeze guardrail: pause weight escalation when we're in a
        # losing streak combined with a high-VIX regime.
        self.freeze_rolling_window: int = lcfg.get("freeze_rolling_window", 10)
        self.freeze_if_drawdown_pnl_below: float = lcfg.get("freeze_if_drawdown_pnl_below", -100.0)
        self.freeze_if_vix_above: float = lcfg.get("freeze_if_vix_above", 22.0)

        # Kelly-lite sizing multiplier bounds
        self.kelly_min: float = lcfg.get("kelly_min_multiplier", 0.5)
        self.kelly_max: float = lcfg.get("kelly_max_multiplier", 1.5)

        # Auto-suppress losing strategies (2026-05-06). When a strategy's
        # profit_factor stays below auto_suppress_pf_threshold for at least
        # auto_suppress_min_trades closed trades, set its weight to 0 in the
        # ensemble — fully removing it from voting until it recovers. This is
        # checked on every weight recalculation, so recovery is automatic.
        self.auto_suppress_enabled: bool = lcfg.get("auto_suppress_enabled", True)
        self.auto_suppress_min_trades: int = lcfg.get("auto_suppress_min_trades", 8)
        self.auto_suppress_pf_threshold: float = lcfg.get(
            "auto_suppress_pf_threshold", 0.7
        )

        # Journal path
        log_dir = config.get("logging", {}).get("log_dir", "logs")
        self.journal_path = os.path.join(log_dir, "learning_journal.md")

        self._base_weights: Dict[str, float] = config.get("ensemble", {}).get("weights", {})
        self._trade_count_since_update = 0

        self._strategy_stats: Dict[str, dict] = {}
        self._learned_weights: Dict[str, float] = {}
        # Regime-specific stats: {(strategy, regime): stats_dict}
        self._regime_stats: Dict[Tuple[str, str], dict] = defaultdict(self._empty_stats_copy)
        # Recent trade PnLs (any strategy) for freeze guardrail
        self._recent_global_pnl: List[float] = []

        self._load_state()

        if self.enabled:
            logger.info(
                f"TradeAnalyzer v3 enabled | min_trades={self.min_trades} "
                f"update_interval={self.update_interval} decay={self.decay_factor}"
            )
        else:
            logger.info("TradeAnalyzer disabled by config")

    # ----------------------------------------------------------
    # Initialization
    # ----------------------------------------------------------

    def _load_state(self):
        """Restore learned state from the database."""
        try:
            self._strategy_stats = self._db.load_strategy_scores()
            self._learned_weights = self._db.load_learned_weights()
            if self._learned_weights:
                logger.info(
                    "Restored learned weights: "
                    + ", ".join(f"{k}={v:.2f}" for k, v in self._learned_weights.items())
                )
            try:
                regime_map = self._db.load_regime_weights()
                for (strategy, regime), row in regime_map.items():
                    self._regime_stats[(strategy, regime)] = {
                        "total_trades": row.get("trades", 0),
                        "wins": row.get("wins", 0),
                        "losses": max(0, row.get("trades", 0) - row.get("wins", 0)),
                        "total_pnl": row.get("total_pnl", 0.0),
                        "sharpe": row.get("sharpe", 0.0),
                        "learned_weight": row.get("weight", 1.0),
                        "win_rate": (row.get("wins", 0) / row["trades"]) if row.get("trades") else 0.0,
                        "profit_factor": 0.0,
                        "avg_pnl": 0.0,
                        "_gross_wins": 0.0,
                        "_gross_losses": 0.0,
                        "_pnl_list": [],
                    }
            except Exception as e:
                logger.debug(f"Regime stats not loaded: {e}")
        except Exception as e:
            logger.error(f"Failed to load learning state: {e}")

    # ----------------------------------------------------------
    # Layer 1 + 4: Record trade (per-strategy + per-regime)
    # ----------------------------------------------------------

    def record_trade(self, trade_record, market_context: Optional[dict] = None) -> dict:
        """
        Called after every trade close. Updates per-strategy and per-regime
        statistics, stores the entry pattern, and triggers weight recalculation
        when enough trades have accumulated.

        Returns the aggregate dict of stats updated by this trade.
        """
        if not self.enabled:
            return {}

        pnl = trade_record.pnl
        regime = getattr(trade_record, "regime", None) or "unknown"

        raw_contrib = getattr(trade_record, "contributing_strategies", None)
        # Guard against MagicMocks / non-dict attributes in tests
        if not isinstance(raw_contrib, dict) or not raw_contrib:
            strategy = trade_record.strategy or "unknown"
            contributions: Dict[str, float] = {strategy: 1.0}
        else:
            contributions = raw_contrib

        aggregate_stats: dict = {}

        for strategy, share in contributions.items():
            if share <= 0:
                continue
            attributed_pnl = pnl * share

            # ----- per-strategy scorecard -----
            stats = self._strategy_stats.get(strategy, self._empty_stats())
            stats["total_trades"] = stats.get("total_trades", 0) + 1
            if attributed_pnl > 0:
                stats["wins"] = stats.get("wins", 0) + 1
            else:
                stats["losses"] = stats.get("losses", 0) + 1
            stats["total_pnl"] = stats.get("total_pnl", 0) + attributed_pnl
            stats["avg_pnl"] = stats["total_pnl"] / stats["total_trades"]
            if stats["total_trades"] > 0:
                stats["win_rate"] = stats["wins"] / stats["total_trades"]
            stats = self._update_profit_factor(stats, attributed_pnl)
            stats = self._update_sharpe(stats, attributed_pnl)
            self._strategy_stats[strategy] = stats
            aggregate_stats = stats  # last one wins as the return value
            try:
                self._db.save_strategy_score(strategy, stats)
            except Exception as e:
                logger.error(f"Failed to save strategy score for {strategy}: {e}")

            # ----- per-strategy per-regime scorecard -----
            r_stats = self._regime_stats.get((strategy, regime)) or self._empty_stats()
            r_stats["total_trades"] = r_stats.get("total_trades", 0) + 1
            if attributed_pnl > 0:
                r_stats["wins"] = r_stats.get("wins", 0) + 1
            else:
                r_stats["losses"] = r_stats.get("losses", 0) + 1
            r_stats["total_pnl"] = r_stats.get("total_pnl", 0) + attributed_pnl
            r_stats["avg_pnl"] = r_stats["total_pnl"] / r_stats["total_trades"]
            if r_stats["total_trades"] > 0:
                r_stats["win_rate"] = r_stats["wins"] / r_stats["total_trades"]
            r_stats = self._update_profit_factor(r_stats, attributed_pnl)
            r_stats = self._update_sharpe(r_stats, attributed_pnl)
            self._regime_stats[(strategy, regime)] = r_stats

        # Pattern snapshot (Layer 3) - credited to the leading contributor
        self._record_pattern(trade_record, contributions, regime)

        # Rolling global PnL for freeze guardrail
        self._recent_global_pnl.append(pnl)
        if len(self._recent_global_pnl) > self.freeze_rolling_window:
            self._recent_global_pnl.pop(0)

        # Trigger weight recalculation
        self._trade_count_since_update += 1
        total_trades_all = sum(s.get("total_trades", 0) for s in self._strategy_stats.values())

        if (total_trades_all >= self.min_trades
                and self._trade_count_since_update >= self.update_interval):
            self._recalculate_weights(market_context=market_context)
            self._recalculate_regime_weights()
            self._trade_count_since_update = 0

        self._log_scorecard_row(contributions, pnl)
        return aggregate_stats

    def get_scorecard(self) -> Dict[str, dict]:
        """Return current per-strategy stats."""
        return dict(self._strategy_stats)

    def get_regime_scorecard(self) -> Dict[Tuple[str, str], dict]:
        return dict(self._regime_stats)

    # ----------------------------------------------------------
    # Layer 2: Adaptive Weight Tuner (global)
    # ----------------------------------------------------------

    def _should_auto_suppress(self, stats: dict) -> bool:
        """Return True if this strategy has accumulated enough negative
        evidence to be fully removed from ensemble voting.

        The min_weight floor (default 0.1) is a soft demote — bleeding
        strategies can still tip marginal votes. Auto-suppress is the
        hard kill: weight drops to 0 until the strategy proves itself
        again on the next batch of trades.
        """
        if not self.auto_suppress_enabled:
            return False
        if stats.get("total_trades", 0) < self.auto_suppress_min_trades:
            return False
        pf = stats.get("profit_factor", 1.0)
        return pf < self.auto_suppress_pf_threshold

    def _recalculate_weights(self, market_context: Optional[dict] = None):
        """
        Recalculate ensemble weights based on recent strategy performance.
        Formula: new_weight = base_weight * (1 + performance_score)
        Clamped to [min_weight, max_weight]. Skipped entirely if the learning
        freeze guardrail is active.

        2026-05-06: any strategy that satisfies `_should_auto_suppress` has
        its weight forced to 0.0 (overriding the [min_weight, max_weight]
        clamp). The suppression check runs on every recalc so a strategy
        that recovers will be re-promoted automatically.
        """
        if self._is_learning_frozen(market_context):
            logger.warning("[LEARNING-FROZEN] Skipping weight recalc (bad regime)")
            return

        logger.info("=== Recalculating ensemble weights ===")
        new_weights: Dict[str, float] = {}
        suppressed_now: List[str] = []

        for strategy, stats in self._strategy_stats.items():
            if stats.get("total_trades", 0) < 3:
                continue
            base = self._base_weights.get(strategy, 1.0)
            sharpe = stats.get("sharpe", 0.0)
            win_rate = stats.get("win_rate", 0.5)
            profit_factor = stats.get("profit_factor", 1.0)

            # Combined score: sharpe is primary, win_rate and profit_factor secondary
            performance_score = (
                sharpe + 0.3 * (win_rate - 0.5)
                + 0.2 * math.log1p(max(profit_factor - 1, 0))
            )

            raw_weight = base * (1.0 + performance_score)
            clamped = max(self.min_weight, min(self.max_weight, raw_weight))

            # Auto-suppress overrides the min_weight floor. Use a single
            # flag per strategy so we can log it distinctly from a normal
            # demotion (a 0.1-floored weight looks similar in the dict
            # but means something very different to the operator).
            if self._should_auto_suppress(stats):
                clamped = 0.0
                suppressed_now.append(strategy)

            new_weights[strategy] = round(clamped, 3)

            tag = " [SUPPRESSED]" if clamped == 0.0 else ""
            logger.info(
                f"  {strategy}: base={base:.1f} sharpe={sharpe:.2f} "
                f"wr={win_rate:.1%} pf={profit_factor:.2f} -> weight={clamped:.3f}{tag}"
            )

        if suppressed_now:
            logger.warning(
                f"[AUTO-SUPPRESS] {len(suppressed_now)} strategy/strategies "
                f"removed from ensemble voting: {suppressed_now} "
                f"(pf < {self.auto_suppress_pf_threshold} over "
                f">= {self.auto_suppress_min_trades} trades). "
                f"Will be re-evaluated automatically as more trades close."
            )

        self._learned_weights = new_weights
        for strategy, stats in self._strategy_stats.items():
            stats["learned_weight"] = new_weights.get(strategy, stats.get("learned_weight", 1.0))
            try:
                self._db.save_strategy_score(strategy, stats)
            except Exception as e:
                logger.error(f"Failed to persist weight for {strategy}: {e}")

        logger.info(f"Updated weights: {new_weights}")

    def _recalculate_regime_weights(self):
        """Same algorithm as global weights, but per (strategy, regime).

        2026-05-06: per-regime auto-suppress. A strategy that loses money
        in a specific regime (e.g. mean_reversion in bear_high_vol) gets
        weight 0 *only in that regime*, while keeping its global weight
        intact for other regimes where it works. This is more surgical
        than the global suppress and addresses the strategy-monoculture
        leak directly.
        """
        logger.info("=== Recalculating regime-specific weights ===")
        suppressed_pairs: List[Tuple[str, str]] = []
        for (strategy, regime), stats in list(self._regime_stats.items()):
            if stats.get("total_trades", 0) < 3:
                continue
            base = self._base_weights.get(strategy, 1.0)
            sharpe = stats.get("sharpe", 0.0)
            win_rate = stats.get("win_rate", 0.5)
            pf = stats.get("profit_factor", 1.0)

            performance_score = (
                sharpe + 0.3 * (win_rate - 0.5)
                + 0.2 * math.log1p(max(pf - 1, 0))
            )
            raw = base * (1.0 + performance_score)
            clamped = max(self.min_weight, min(self.max_weight, raw))

            if self._should_auto_suppress(stats):
                clamped = 0.0
                suppressed_pairs.append((strategy, regime))

            stats["learned_weight"] = round(clamped, 3)

            try:
                self._db.save_regime_weight(
                    strategy=strategy, regime=regime,
                    weight=clamped, trades=stats.get("total_trades", 0),
                    wins=stats.get("wins", 0), total_pnl=stats.get("total_pnl", 0.0),
                    sharpe=stats.get("sharpe", 0.0),
                )
            except Exception as e:
                logger.error(f"Failed to persist regime weight for {strategy}/{regime}: {e}")
            tag = " [SUPPRESSED]" if clamped == 0.0 else ""
            logger.debug(
                f"  [REGIME] {strategy}/{regime}: wr={win_rate:.1%} "
                f"sharpe={sharpe:.2f} -> weight={clamped:.3f}{tag}"
            )

        if suppressed_pairs:
            logger.warning(
                f"[AUTO-SUPPRESS-REGIME] {len(suppressed_pairs)} "
                f"strategy/regime pair(s) suppressed: {suppressed_pairs}"
            )

    def get_learned_weights(self) -> Dict[str, float]:
        """Return the current learned weights (empty if learning hasn't kicked in)."""
        return dict(self._learned_weights)

    def get_regime_weights(self, regime: str) -> Dict[str, float]:
        """Return {strategy: weight} for a specific regime."""
        out: Dict[str, float] = {}
        for (strategy, r), stats in self._regime_stats.items():
            if r != regime:
                continue
            if stats.get("total_trades", 0) < 3:
                continue
            out[strategy] = stats.get("learned_weight", 1.0)
        return out

    def has_enough_data(self) -> bool:
        """True if enough trades have been recorded to start adjusting weights."""
        total = sum(s.get("total_trades", 0) for s in self._strategy_stats.values())
        return total >= self.min_trades

    def kelly_multiplier(self, strategy: str) -> float:
        """
        Compute a Kelly-lite position-size multiplier in [kelly_min, kelly_max].

        Edge estimate:   E = wr*avg_win + (1-wr)*avg_loss  (avg_loss negative)
        Kelly fraction:  f* = E / variance_proxy

        We apply a 0.25 safety factor on top (quarter-Kelly) and clamp.
        Strategies with < 5 trades fall back to 1.0 (no adjustment).
        """
        stats = self._strategy_stats.get(strategy)
        if not stats or stats.get("total_trades", 0) < 5:
            return 1.0
        pnl_list = stats.get("_pnl_list") or []
        if len(pnl_list) < 5:
            return 1.0
        arr = np.array(pnl_list, dtype=float)
        mean = float(arr.mean())
        var = float(arr.var()) or 1.0
        kelly = mean / var
        mult = 1.0 + 0.25 * kelly  # quarter-Kelly around 1.0
        return round(max(self.kelly_min, min(self.kelly_max, mult)), 3)

    # ----------------------------------------------------------
    # Learning Freeze Guardrail
    # ----------------------------------------------------------

    def _is_learning_frozen(self, market_context: Optional[dict]) -> bool:
        """
        Freeze learning if recent PnL is deeply negative AND volatility is high.
        This prevents us from amplifying weights of strategies that look "good"
        only because everything is chaotic.
        """
        if len(self._recent_global_pnl) < self.freeze_rolling_window:
            return False
        rolling = sum(self._recent_global_pnl)
        if rolling > self.freeze_if_drawdown_pnl_below:
            return False
        # Drawdown condition met; now check vol
        if market_context is None:
            return False
        vix = market_context.get("india_vix", 0) or 0
        return vix >= self.freeze_if_vix_above

    # ----------------------------------------------------------
    # Layer 3: Pattern Memory
    # ----------------------------------------------------------

    def _record_pattern(self, trade_record, contributions: Dict[str, float], regime: str):
        """Snapshot key indicators at entry time and label the outcome."""
        try:
            entry_time = trade_record.entry_time
            if isinstance(entry_time, str):
                entry_time = datetime.fromisoformat(entry_time)

            leading = max(contributions.items(), key=lambda kv: kv[1])[0] if contributions else "unknown"

            pnl_val = trade_record.pnl
            if abs(pnl_val) < 20:
                bucket = "small"
            elif abs(pnl_val) < 100:
                bucket = "medium"
            else:
                bucket = "large"

            def _num(v):
                """Coerce to float or None, safely handling MagicMock / None."""
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            def _int(v):
                if v is None:
                    return None
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None

            def _str(v):
                if v is None:
                    return ""
                if isinstance(v, str):
                    return v
                try:
                    return str(v)
                except Exception:
                    return ""

            pattern = {
                "strategy": leading,
                "symbol": _str(trade_record.symbol),
                "entry_time": entry_time.isoformat() if hasattr(entry_time, "isoformat") else str(entry_time),
                "rsi": _num(getattr(trade_record, "rsi", None)),
                "atr_pct": _num(getattr(trade_record, "atr_pct", None)),
                "volume_ratio": _num(getattr(trade_record, "volume_ratio", None)),
                "hour_of_day": _int(entry_time.hour) if hasattr(entry_time, "hour") else None,
                "day_of_week": _int(entry_time.weekday()) if hasattr(entry_time, "weekday") else None,
                "market_trend": _int(getattr(trade_record, "market_trend", None)),
                "pnl": _num(trade_record.pnl) or 0.0,
                "pnl_pct": _num(trade_record.pnl_pct) or 0.0,
                "outcome": "GOOD" if (trade_record.pnl or 0) > 0 else "BAD",
                "exit_reason": _str(getattr(trade_record, "exit_reason", "")),
                "holding_minutes": _num(getattr(trade_record, "holding_minutes", 0.0)) or 0.0,
                "pnl_bucket": bucket,
                "regime": _str(regime),
            }
            self._db.save_trade_pattern(pattern)
        except Exception as e:
            logger.error(f"Failed to record trade pattern: {e}")

    def evaluate_setup(
        self,
        strategy: str,
        hour_of_day: int = None,
        day_of_week: int = None,
        rsi: float = None,
        atr_pct: float = None,
        volume_ratio: float = None,
        market_trend: int = None,
        regime: Optional[str] = None,
    ) -> Tuple[float, str]:
        """
        Check if the current setup matches known GOOD or BAD patterns.

        Returns:
            (confidence_adjustment, reason)
            * Positive adjustment: the setup looks good historically.
            * Negative adjustment: the setup looks bad.
            * Zero: not enough data to judge.
        """
        if not self.enabled:
            return 0.0, "learning_disabled"

        try:
            patterns = self._db.load_trade_patterns(limit=self.pattern_lookback)
        except Exception:
            return 0.0, "db_error"

        if len(patterns) < self.min_trades:
            return 0.0, "insufficient_data"

        strat_patterns = [p for p in patterns if p.get("strategy") == strategy]
        if len(strat_patterns) < 3:
            return 0.0, "insufficient_strategy_data"

        # If regime info is present on patterns and we know current regime,
        # further filter to same regime for higher relevance.
        if regime:
            same_regime = [p for p in strat_patterns if (p.get("regime") or "unknown") == regime]
            if len(same_regime) >= 3:
                strat_patterns = same_regime

        good_matches = 0
        bad_matches = 0
        total_checked = 0

        for p in strat_patterns:
            sim = self._compute_similarity(
                p, hour_of_day, day_of_week, rsi, atr_pct, volume_ratio, market_trend,
            )
            if sim >= self.pattern_similarity_threshold:
                total_checked += 1
                if p.get("outcome") == "GOOD":
                    good_matches += 1
                else:
                    bad_matches += 1

        if total_checked == 0:
            return 0.0, "no_similar_patterns"

        good_ratio = good_matches / total_checked
        bad_ratio = bad_matches / total_checked

        if good_ratio > 0.6:
            adjustment = 0.1 * good_ratio
            return round(adjustment, 3), f"good_pattern_match({good_matches}/{total_checked})"
        elif bad_ratio > 0.6:
            adjustment = -0.15 * bad_ratio
            return round(adjustment, 3), f"bad_pattern_match({bad_matches}/{total_checked})"

        return 0.0, "mixed_patterns"

    @staticmethod
    def _compute_similarity(
        pattern: dict,
        hour_of_day: int = None,
        day_of_week: int = None,
        rsi: float = None,
        atr_pct: float = None,
        volume_ratio: float = None,
        market_trend: int = None,
    ) -> float:
        """Similarity in [0,1] between current setup and a historical pattern."""
        matches = 0.0
        total = 0

        if hour_of_day is not None and pattern.get("hour_of_day") is not None:
            total += 1
            diff = abs(hour_of_day - pattern["hour_of_day"])
            if diff == 0:
                matches += 1.0
            elif diff == 1:
                matches += 0.5

        if day_of_week is not None and pattern.get("day_of_week") is not None:
            total += 1
            if day_of_week == pattern["day_of_week"]:
                matches += 1.0

        if rsi is not None and pattern.get("rsi") is not None:
            total += 1
            diff = abs(rsi - pattern["rsi"])
            if diff <= 10:
                matches += 1.0 - (diff / 10.0)

        if atr_pct is not None and pattern.get("atr_pct") is not None:
            total += 1
            diff = abs(atr_pct - pattern["atr_pct"])
            if diff <= 1.0:
                matches += 1.0 - diff

        if volume_ratio is not None and pattern.get("volume_ratio") is not None:
            total += 1
            diff = abs(volume_ratio - pattern["volume_ratio"])
            if diff <= 0.5:
                matches += 1.0 - (diff / 0.5)

        if market_trend is not None and pattern.get("market_trend") is not None:
            total += 1
            if market_trend == pattern["market_trend"]:
                matches += 1.0

        if total == 0:
            return 0.0
        return matches / total

    # ----------------------------------------------------------
    # Layer 5: Daily Reflection Journal
    # ----------------------------------------------------------

    def write_daily_journal(self, day_iso: str, market_summary: Optional[dict] = None):
        """
        Write a human-readable EOD summary to logs/learning_journal.md. Pulls the
        day's closed trades from the DB and breaks down performance by strategy,
        regime, and exit-reason.
        """
        if not self.enabled:
            return

        try:
            os.makedirs(os.path.dirname(self.journal_path) or ".", exist_ok=True)
            trades = self._db.load_trades_for_day(day_iso)
            if not trades:
                return

            pnls = [t.get("pnl", 0) or 0 for t in trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            total = sum(pnls)
            wr = len(wins) / len(pnls) if pnls else 0

            by_strategy = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
            by_exit = defaultdict(lambda: {"n": 0, "pnl": 0.0})
            by_regime = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})

            for t in trades:
                s = t.get("strategy", "unknown")
                by_strategy[s]["n"] += 1
                by_strategy[s]["pnl"] += t.get("pnl", 0) or 0
                if (t.get("pnl", 0) or 0) > 0:
                    by_strategy[s]["wins"] += 1

                er = t.get("exit_reason", "unknown")
                by_exit[er]["n"] += 1
                by_exit[er]["pnl"] += t.get("pnl", 0) or 0

                rg = t.get("regime", "unknown") or "unknown"
                by_regime[rg]["n"] += 1
                by_regime[rg]["pnl"] += t.get("pnl", 0) or 0
                if (t.get("pnl", 0) or 0) > 0:
                    by_regime[rg]["wins"] += 1

            lines: List[str] = []
            lines.append(f"## {day_iso}\n")
            lines.append(f"- **Trades:** {len(trades)}  |  **Wins:** {len(wins)}  |  **Losses:** {len(losses)}  |  **WR:** {wr:.0%}")
            lines.append(f"- **Net PnL:** Rs {total:+.2f}")
            if wins:
                lines.append(f"- **Avg win:** Rs {sum(wins)/len(wins):+.2f}   **Max win:** Rs {max(wins):+.2f}")
            if losses:
                lines.append(f"- **Avg loss:** Rs {sum(losses)/len(losses):+.2f}   **Max loss:** Rs {min(losses):+.2f}")

            if market_summary:
                vix = market_summary.get("india_vix")
                trend = market_summary.get("nifty_trend")
                trend_word = "ABOVE" if trend == 1 else ("BELOW" if trend == -1 else "FLAT")
                lines.append(f"- **Regime:** Nifty {trend_word} 200EMA  |  VIX {vix}")

            lines.append("\n**By strategy**")
            for s, v in sorted(by_strategy.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
                swr = v["wins"] / v["n"] if v["n"] else 0
                lines.append(f"- {s}: {v['n']} trades  WR {swr:.0%}  PnL Rs {v['pnl']:+.2f}")

            lines.append("\n**By exit reason**")
            for er, v in sorted(by_exit.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
                lines.append(f"- {er}: {v['n']} trades  PnL Rs {v['pnl']:+.2f}")

            lines.append("\n**By regime**")
            for rg, v in sorted(by_regime.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
                swr = v["wins"] / v["n"] if v["n"] else 0
                lines.append(f"- {rg}: {v['n']} trades  WR {swr:.0%}  PnL Rs {v['pnl']:+.2f}")

            # Narrative: pick the worst thing that happened today so we learn
            worst_strategy = min(by_strategy.items(), key=lambda kv: kv[1]["pnl"], default=None)
            best_strategy = max(by_strategy.items(), key=lambda kv: kv[1]["pnl"], default=None)
            notes: List[str] = []
            if worst_strategy and worst_strategy[1]["pnl"] < 0:
                notes.append(
                    f"- Weakest strategy today: **{worst_strategy[0]}** "
                    f"({worst_strategy[1]['n']} trades, PnL Rs {worst_strategy[1]['pnl']:+.2f}). "
                    f"Consider reducing its weight or disabling in this regime."
                )
            if best_strategy and best_strategy[1]["pnl"] > 0:
                notes.append(
                    f"- Strongest strategy today: **{best_strategy[0]}** "
                    f"(PnL Rs {best_strategy[1]['pnl']:+.2f})."
                )
            if by_exit.get("stop_loss", {}).get("n", 0) > by_exit.get("take_profit", {}).get("n", 0) * 2:
                notes.append(
                    "- Stop-losses materially outnumbered take-profits today. "
                    "Entries may be too early; consider tightening confidence threshold."
                )
            if notes:
                lines.append("\n**Notes**")
                lines.extend(notes)

            lines.append("\n---\n")

            mode = "a" if os.path.exists(self.journal_path) else "w"
            with open(self.journal_path, mode, encoding="utf-8") as f:
                if mode == "w":
                    f.write("# Trading Agent - Learning Journal\n\n")
                f.write("\n".join(lines))
            logger.info(f"[JOURNAL] Wrote daily reflection to {self.journal_path}")
        except Exception as e:
            logger.error(f"Failed to write daily journal: {e}")

    # ----------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------

    @staticmethod
    def _empty_stats() -> dict:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "learned_weight": 1.0,
            "_gross_wins": 0.0,
            "_gross_losses": 0.0,
            "_pnl_list": [],
        }

    def _empty_stats_copy(self) -> dict:
        return self._empty_stats()

    @staticmethod
    def _update_profit_factor(stats: dict, pnl: float) -> dict:
        gw = stats.get("_gross_wins", 0.0)
        gl = stats.get("_gross_losses", 0.0)
        if pnl > 0:
            gw += pnl
        else:
            gl += abs(pnl)
        stats["_gross_wins"] = gw
        stats["_gross_losses"] = gl
        stats["profit_factor"] = round(gw / gl, 3) if gl > 0 else float("inf") if gw > 0 else 0.0
        return stats

    def _update_sharpe(self, stats: dict, pnl: float) -> dict:
        """Compute rolling Sharpe ratio with exponential decay on older trades."""
        pnl_list = stats.get("_pnl_list", [])
        pnl_list.append(pnl)
        n = len(pnl_list)
        if n < 2:
            stats["_pnl_list"] = pnl_list
            stats["sharpe"] = 0.0
            return stats

        weights = np.array([self.decay_factor ** (n - 1 - i) for i in range(n)])
        pnl_arr = np.array(pnl_list)
        weighted_mean = np.average(pnl_arr, weights=weights)
        weighted_var = np.average((pnl_arr - weighted_mean) ** 2, weights=weights)
        weighted_std = math.sqrt(weighted_var) if weighted_var > 0 else 0

        sharpe = (weighted_mean / weighted_std * math.sqrt(250)) if weighted_std > 0 else 0.0
        stats["sharpe"] = round(sharpe, 3)
        stats["_pnl_list"] = pnl_list
        return stats

    @staticmethod
    def _log_scorecard_row(contributions: Dict[str, float], pnl: float):
        if not contributions:
            return
        parts = ", ".join(f"{s}:{w:.2f}" for s, w in contributions.items())
        logger.info(f"[SCORECARD] trade PnL Rs {pnl:+.2f} credited -> {parts}")
