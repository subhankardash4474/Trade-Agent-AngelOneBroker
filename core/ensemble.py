"""
Ensemble Meta-Model
Aggregates signals from multiple rule-based and ML strategies using
weighted voting. Only emits a final signal when ensemble confidence
reaches the configurable threshold (default 0.7).
"""

from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from core.regime import regime_multiplier
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


# Default weights reflecting trust hierarchy:
#   ML models get higher base weight; rule-based act as confirmation.
DEFAULT_WEIGHTS = {
    "xgboost_classifier": 2.0,
    "lstm_price_model": 1.8,
    "supertrend_follow": 1.5,
    "opening_range_breakout": 1.3,
    "vwap_bounce": 1.2,
    "moving_average_crossover": 1.0,
    "rsi_momentum": 1.0,
    "mean_reversion": 0.8,
}


class EnsembleModel:
    """
    Weighted-voting ensemble that combines signals from all active strategies.

    For each instrument:
      1. Collect signals from every strategy.
      2. Multiply each signal's confidence by its strategy weight.
      3. Sum weighted BUY and SELL scores.
      4. Normalize to [0, 1] confidence.
      5. Only emit BUY/SELL if confidence >= threshold.

    Attributes:
        confidence_threshold: Minimum confidence to act (default 0.7).
        weights: Dict mapping strategy name → weight multiplier.
    """

    def __init__(self, config: dict):
        ens_cfg = config.get("ensemble", {})
        self._base_confidence_threshold: float = ens_cfg.get("confidence_threshold", 0.7)
        self.confidence_threshold: float = self._base_confidence_threshold
        self._min_dynamic_threshold: float = ens_cfg.get("min_dynamic_threshold", 0.45)
        self._max_dynamic_threshold: float = ens_cfg.get("max_dynamic_threshold", 0.75)
        self._base_weights: Dict[str, float] = {**DEFAULT_WEIGHTS, **ens_cfg.get("weights", {})}
        self.weights: Dict[str, float] = dict(self._base_weights)
        # Global learned weights (regime-agnostic)
        self._global_learned_weights: Dict[str, float] = {}
        # Cached: regime -> {strategy: weight}
        self._regime_learned_weights: Dict[str, Dict[str, float]] = {}
        self.min_strategies_agree: int = ens_cfg.get("min_strategies_agree", 2)

    def update_weights(self, learned_weights: Dict[str, float]):
        """
        Replace current weights with learned weights from TradeAnalyzer.
        Only overrides strategies present in learned_weights; others keep base values.
        """
        # Cast to plain float — TradeAnalyzer computes weights via numpy and
        # the np.float64 type leaks into the dict, polluting log output as
        # "np.float64(4.599)" instead of "4.599" (live evidence: 2026-05-05
        # log line 15322: 'supertrend_follow': np.float64(4.599)). Plain
        # float renders cleanly and keeps downstream arithmetic identical.
        self._global_learned_weights = {k: float(v) for k, v in learned_weights.items()}
        self.weights = dict(self._base_weights)
        for strategy, weight in self._global_learned_weights.items():
            self.weights[strategy] = weight
        # Pretty render: 2 decimals, sorted by weight desc so the dominant
        # strategies are visible at a glance.
        rendered = ", ".join(
            f"{k}={v:.2f}" for k, v in sorted(
                self.weights.items(), key=lambda kv: kv[1], reverse=True
            )
        )
        logger.info(f"[ENSEMBLE] Weights updated from learning: {rendered}")

    def update_regime_weights(self, regime: str, weights: Dict[str, float]):
        """Store learned weights for a specific regime."""
        self._regime_learned_weights[regime] = {k: float(v) for k, v in weights.items()}
        logger.debug(f"[ENSEMBLE] Regime weights for {regime}: {self._regime_learned_weights[regime]}")

    def set_runtime_threshold(self, new_threshold: float):
        """Dynamically adjust the confidence threshold within configured bounds."""
        clamped = max(self._min_dynamic_threshold, min(self._max_dynamic_threshold, new_threshold))
        if abs(clamped - self.confidence_threshold) > 0.001:
            logger.info(
                f"[ENSEMBLE] confidence_threshold {self.confidence_threshold:.2f} -> {clamped:.2f}"
            )
            self.confidence_threshold = clamped

    def _unsuppressed_weight(self, strategy: str, regime: Optional[str] = None) -> float:
        """Strategy weight WITHOUT the rule-based regime preference multiplier.

        Returned value reflects only learning + base trust (regime-learned >
        global-learned > base). This is the denominator for confidence
        normalization in `aggregate` — using it (instead of the multiplied
        weight) ensures the regime/direction multipliers actually dampen final
        confidence rather than cancelling out via numerator/denominator.

        Without this, a solo `mean_reversion` BUY in bear_high_vol with
        multiplier=0.1 would still produce ensemble confidence equal to the
        raw signal confidence (because both buy_score and active_weight would
        carry the 0.1 factor, which then divides away).
        """
        weight: Optional[float] = None
        if regime and regime in self._regime_learned_weights:
            weight = self._regime_learned_weights[regime].get(strategy)
        if weight is None:
            weight = self._global_learned_weights.get(strategy)
        if weight is None:
            weight = self._base_weights.get(strategy, 1.0)
        return max(0.0, weight)

    def effective_weight(
        self,
        strategy: str,
        regime: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> float:
        """
        Compute the effective weight for a strategy in a given regime.
        Order of precedence:
            1. regime-specific learned weight (if available for this regime)
            2. global learned weight
            3. base weight
        Then multiplied by the rule-based regime preference multiplier (which
        is direction-aware when `direction` is provided — see core/regime.py).
        """
        base = self._unsuppressed_weight(strategy, regime)
        if regime:
            base *= regime_multiplier(strategy, regime, direction=direction)
        return max(0.0, base)

    def aggregate(
        self,
        signals: List[TradeSignal],
        symbol: str,
        current_price: float,
        regime: Optional[str] = None,
    ) -> Optional[TradeSignal]:
        """
        Aggregate multiple strategy signals into a single ensemble decision.

        Args:
            signals: List of TradeSignal from individual strategies.
            symbol: The instrument being evaluated.
            current_price: Latest price for the instrument.
            regime: Current market regime label (optional) - used to select
                    regime-specific learned weights and apply strategy-regime
                    preference multipliers.

        Returns:
            A single TradeSignal (BUY/SELL) if consensus is strong enough, else None.
        """
        if not signals:
            return None

        # Snapshot effective weights per *signal* (strategy + direction) so
        # the aggregator can apply direction-aware regime multipliers.
        # Each signal indexes into `effective_per_signal` by id(signal) — we
        # can't key on strategy name alone because the same strategy could
        # in principle emit BUY in one cycle and SELL in another.
        #
        # We also snapshot the *unsuppressed* weight per signal — the weight
        # WITHOUT the regime/direction multiplier. This is the denominator
        # for confidence normalization, which is the fix for the 2026-05-04
        # bug where regime suppression had no effect on solo-strategy votes:
        # since the multiplier appeared in both numerator (buy_score) and
        # denominator (active_weight), it cancelled out and a `mean_reversion`
        # BUY at conf=0.886 in bear_high_vol with multiplier=0.1 came out of
        # the ensemble at 0.886 instead of the intended ~0.089.
        effective_per_signal: Dict[int, float] = {}
        unsuppressed_per_signal: Dict[int, float] = {}
        for s in signals:
            direction: Optional[str] = None
            if s.signal == Signal.BUY:
                direction = "BUY"
            elif s.signal == Signal.SELL:
                direction = "SELL"
            effective_per_signal[id(s)] = self.effective_weight(
                s.strategy_name, regime, direction=direction
            )
            unsuppressed_per_signal[id(s)] = self._unsuppressed_weight(
                s.strategy_name, regime
            )

        buy_score = 0.0
        sell_score = 0.0
        hold_count = 0
        buy_signals: List[TradeSignal] = []
        sell_signals: List[TradeSignal] = []

        for sig in signals:
            weight = effective_per_signal.get(id(sig), 1.0)
            weighted_conf = sig.confidence * weight

            if sig.signal == Signal.BUY:
                buy_score += weighted_conf
                buy_signals.append(sig)
            elif sig.signal == Signal.SELL:
                sell_score += weighted_conf
                sell_signals.append(sig)
            else:
                hold_count += 1

        # Only count strategies that expressed an opinion (BUY or SELL), not HOLD.
        # Normalize by the *unsuppressed* weight so regime multipliers actually
        # dampen final confidence — see comment block above.
        opinionated = [s for s in signals if s.signal in (Signal.BUY, Signal.SELL)]
        norm_weight = sum(unsuppressed_per_signal.get(id(s), 1.0) for s in opinionated)
        if norm_weight == 0:
            return None

        # Normalize scores to [0, 1]. In regimes where contributing strategies
        # have a directional multiplier > 1 (e.g. supertrend SELL in bear =
        # 1.1) the raw quotient can exceed 1.0 — verified live on 2026-05-04
        # when SAILIFE supertrend SELL logged conf=1.100. Confidence is a
        # probability/score and should never exceed 1.0; downstream code
        # (ATR-relaxation tiers, threshold checks, audit CSVs) expects values
        # in [0, 1]. Clamp to be safe — anything above 1 is already in
        # "exceptionally strong" territory and passes every threshold.
        buy_confidence = min(buy_score / norm_weight, 1.0)
        sell_confidence = min(sell_score / norm_weight, 1.0)

        metadata = {
            "buy_score": round(buy_score, 3),
            "sell_score": round(sell_score, 3),
            "buy_confidence": round(buy_confidence, 3),
            "sell_confidence": round(sell_confidence, 3),
            "buy_strategies": [s.strategy_name for s in buy_signals],
            "sell_strategies": [s.strategy_name for s in sell_signals],
            "hold_count": hold_count,
            "total_strategies": len(signals),
        }

        # BUY consensus
        if (buy_confidence >= self.confidence_threshold
                and buy_confidence > sell_confidence
                and len(buy_signals) >= self.min_strategies_agree):

            # Use the best stop-loss and take-profit from contributing signals
            stop_loss = self._best_stop_loss(buy_signals, current_price, side="BUY")
            take_profit = self._best_take_profit(buy_signals, current_price, side="BUY")
            contributions = self._build_contributions(buy_signals, regime=regime)
            metadata["regime"] = regime or "unknown"
            metadata["contributions"] = contributions

            logger.info(
                f"[ENSEMBLE] BUY {symbol} | conf={buy_confidence:.3f} | "
                f"strategies={[s.strategy_name for s in buy_signals]}"
            )
            return TradeSignal(
                signal=Signal.BUY,
                symbol=symbol,
                price=current_price,
                timestamp=signals[0].timestamp,
                strategy_name="ensemble",
                confidence=buy_confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata=metadata,
                contributing_strategies=contributions,
            )

        # SELL consensus
        if (sell_confidence >= self.confidence_threshold
                and sell_confidence > buy_confidence
                and len(sell_signals) >= self.min_strategies_agree):

            stop_loss = self._best_stop_loss(sell_signals, current_price, side="SELL")
            take_profit = self._best_take_profit(sell_signals, current_price, side="SELL")
            contributions = self._build_contributions(sell_signals, regime=regime)
            metadata["regime"] = regime or "unknown"
            metadata["contributions"] = contributions

            logger.info(
                f"[ENSEMBLE] SELL {symbol} | conf={sell_confidence:.3f} | "
                f"strategies={[s.strategy_name for s in sell_signals]}"
            )
            return TradeSignal(
                signal=Signal.SELL,
                symbol=symbol,
                price=current_price,
                timestamp=signals[0].timestamp,
                strategy_name="ensemble",
                confidence=sell_confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata=metadata,
                contributing_strategies=contributions,
            )

        logger.debug(
            f"[ENSEMBLE] HOLD {symbol} | buy_conf={buy_confidence:.3f} "
            f"sell_conf={sell_confidence:.3f} (threshold={self.confidence_threshold})"
        )
        return None

    def _build_contributions(
        self, signals: List[TradeSignal], regime: Optional[str] = None,
    ) -> Dict[str, float]:
        """
        Build a per-strategy credit share for the trade. Each strategy's share is
        (its_effective_weight * its_confidence) / total, so that when the trade
        closes the learner can attribute PnL proportionally to the strategies
        that voted.

        2026-05-06: defensively cast every value to plain `float`. ML strategies
        (XGBoost in particular) emit numpy.float32 confidences which propagate
        here and break JSON serialization downstream — Portfolio.open_position
        stores this dict in the DB. Live failure: UNITDSPR + CGPOWER both
        rejected on solo XGBoost votes. Fix at source AND here (defense in
        depth).
        """
        if not signals:
            return {}
        raw: Dict[str, float] = {}
        for s in signals:
            direction: Optional[str] = None
            if s.signal == Signal.BUY:
                direction = "BUY"
            elif s.signal == Signal.SELL:
                direction = "SELL"
            w = self.effective_weight(s.strategy_name, regime, direction=direction)
            raw[s.strategy_name] = raw.get(s.strategy_name, 0.0) + float(w) * float(max(s.confidence, 0.01))
        total = sum(raw.values())
        if total <= 0:
            return {k: float(1.0 / len(raw)) for k in raw}
        return {k: float(round(v / total, 4)) for k, v in raw.items()}

    @staticmethod
    def _best_stop_loss(signals: List[TradeSignal], price: float, side: str) -> float:
        """Select the tightest (most conservative) stop-loss from contributing signals."""
        sl_values = [s.stop_loss for s in signals if s.stop_loss is not None]
        if not sl_values:
            return price * (0.985 if side == "BUY" else 1.015)
        if side == "BUY":
            return max(sl_values)  # tightest = highest SL below price
        return min(sl_values)

    @staticmethod
    def _best_take_profit(signals: List[TradeSignal], price: float, side: str) -> float:
        """Select the most conservative take-profit from contributing signals."""
        tp_values = [s.take_profit for s in signals if s.take_profit is not None]
        if not tp_values:
            return price * (1.03 if side == "BUY" else 0.97)
        if side == "BUY":
            return min(tp_values)  # conservative = lowest TP above price
        return max(tp_values)
