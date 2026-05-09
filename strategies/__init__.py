from strategies.base_strategy import BaseStrategy
from strategies.moving_average_crossover import MovingAverageCrossover
from strategies.rsi_momentum import RSIMomentum
from strategies.mean_reversion import MeanReversion
from strategies.vwap_bounce import VWAPBounce
from strategies.opening_range_breakout import OpeningRangeBreakout
from strategies.supertrend_follow import SupertrendFollow

STRATEGY_REGISTRY = {
    "moving_average_crossover": MovingAverageCrossover,
    "rsi_momentum": RSIMomentum,
    "mean_reversion": MeanReversion,
    "vwap_bounce": VWAPBounce,
    "opening_range_breakout": OpeningRangeBreakout,
    "supertrend_follow": SupertrendFollow,
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
    "STRATEGY_REGISTRY",
]
