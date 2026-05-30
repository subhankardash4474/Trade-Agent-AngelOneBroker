from strategies.base_strategy import BaseStrategy
from strategies.moving_average_crossover import MovingAverageCrossover
from strategies.rsi_momentum import RSIMomentum
from strategies.mean_reversion import MeanReversion
from strategies.vwap_bounce import VWAPBounce
from strategies.opening_range_breakout import OpeningRangeBreakout
from strategies.supertrend_follow import SupertrendFollow
# v3.0 swing strategies (charter §2). Imported unconditionally — these
# are pure-pandas rules with no native deps, so they always load. v2.1
# variants that don't reference them in ``strategies.active`` are
# unaffected by their presence in the registry.
from strategies.trend_pullback import TrendPullback
from strategies.breakout_20d import Breakout20D

STRATEGY_REGISTRY = {
    "moving_average_crossover": MovingAverageCrossover,
    "rsi_momentum": RSIMomentum,
    "mean_reversion": MeanReversion,
    "vwap_bounce": VWAPBounce,
    "opening_range_breakout": OpeningRangeBreakout,
    "supertrend_follow": SupertrendFollow,
    # v3.0 (per docs/freeze/freeze_v3.0_charter_2026-05-30.md §2)
    "trend_pullback": TrendPullback,
    "breakout_20d": Breakout20D,
}

# ML strategies loaded conditionally to avoid hard dependency on torch/xgboost
try:
    from strategies.xgboost_classifier import XGBoostClassifier
    STRATEGY_REGISTRY["xgboost_classifier"] = XGBoostClassifier
except ImportError:
    pass

try:
    from strategies.lstm_model import LSTMPriceModel
    STRATEGY_REGISTRY["lstm_price_model"] = LSTMPriceModel
except ImportError:
    pass

__all__ = [
    "BaseStrategy",
    "MovingAverageCrossover",
    "RSIMomentum",
    "MeanReversion",
    "VWAPBounce",
    "OpeningRangeBreakout",
    "SupertrendFollow",
    "TrendPullback",
    "Breakout20D",
    "STRATEGY_REGISTRY",
]
