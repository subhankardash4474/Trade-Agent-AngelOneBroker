"""
Market Regime Detector
Maps current market conditions into a coarse regime label that can be used to
filter strategies and apply regime-specific learned weights.

Regime labels:
    - bull_low_vol   : Nifty above 200 EMA, VIX low (< 16)
    - bull_high_vol  : Nifty above 200 EMA, VIX elevated (16-25)
    - bear_low_vol   : Nifty below 200 EMA, VIX low
    - bear_high_vol  : Nifty below 200 EMA, VIX elevated
    - sideways       : Used when trend is ambiguous (fallback)
    - unknown        : Insufficient data

Regimes are computed cheaply from two pieces of context: India VIX and Nifty's
trend vs its 200 EMA (provided by trading_agent._market_context).
"""

from typing import Dict, Optional


# Coarse regime → which strategy categories do well in it.
# Values are multipliers (1.0 = normal, 0.0 = skip, >1.0 = prefer).
#
# These are the *symmetric* multipliers — applied when no direction context
# is available (e.g. trading_agent's pre-evaluation skip filter, where we
# don't yet know if the strategy will emit BUY or SELL).
STRATEGY_REGIME_PREF = {
    # Trend-following strategies love trending regimes
    "moving_average_crossover": {
        "bull_low_vol": 1.2, "bull_high_vol": 1.0,
        "bear_low_vol": 0.6, "bear_high_vol": 0.4,
        "sideways": 0.5, "unknown": 1.0,
    },
    "supertrend_follow": {
        "bull_low_vol": 1.3, "bull_high_vol": 1.1,
        "bear_low_vol": 0.5, "bear_high_vol": 0.3,
        "sideways": 0.4, "unknown": 1.0,
    },
    "rsi_momentum": {
        "bull_low_vol": 1.1, "bull_high_vol": 0.9,
        "bear_low_vol": 0.7, "bear_high_vol": 0.5,
        "sideways": 0.8, "unknown": 1.0,
    },
    "opening_range_breakout": {
        # ORB works best in directional opens — both bulls and trending bears.
        "bull_low_vol": 1.1, "bull_high_vol": 1.2,
        "bear_low_vol": 0.8, "bear_high_vol": 0.9,
        "sideways": 0.5, "unknown": 1.0,
    },
    # Mean reversion is the opposite — it shines in range-bound markets.
    "mean_reversion": {
        "bull_low_vol": 0.8, "bull_high_vol": 0.6,
        "bear_low_vol": 0.7, "bear_high_vol": 0.4,
        "sideways": 1.4, "unknown": 1.0,
    },
    "vwap_bounce": {
        "bull_low_vol": 1.0, "bull_high_vol": 0.8,
        "bear_low_vol": 0.9, "bear_high_vol": 0.6,
        "sideways": 1.2, "unknown": 1.0,
    },
    # ML models are trained on mixed data; trust them more in calm regimes.
    "xgboost_classifier": {
        "bull_low_vol": 1.1, "bull_high_vol": 0.9,
        "bear_low_vol": 0.9, "bear_high_vol": 0.7,
        "sideways": 1.0, "unknown": 1.0,
    },
    "lstm_price_model": {
        "bull_low_vol": 1.1, "bull_high_vol": 0.9,
        "bear_low_vol": 0.9, "bear_high_vol": 0.7,
        "sideways": 1.0, "unknown": 1.0,
    },
}


# Direction-aware overrides. Some strategies have asymmetric risk profiles
# in trending markets — most importantly, mean-reversion BUYs in a bearish
# tape are catching falling knives, while mean-reversion SELLs in the same
# tape are fading rallies into supply (a much higher-probability setup).
#
# The flat STRATEGY_REGIME_PREF above can't capture this asymmetry — it
# weights mean_reversion symmetrically at 0.4 in bear_high_vol, which means
# both BUY-the-dip and SELL-the-rally get the same penalty. That's the bug
# we shipped on 2026-04-30: 3 straight mean_reversion BUYs in a bear tape,
# all stopped out for -Rs 136 in 9 minutes.
#
# When `direction` is provided to `regime_multiplier`, this map takes
# precedence over the symmetric one. Strategies not listed here continue
# to use the symmetric multiplier for both directions.
#
# 2026-05-04 update: the original assumption that trend-following strategies
# (MA-X, supertrend, ORB) needed only symmetric weighting because "they emit
# signals only when the trend agrees" turned out to be incomplete — a moving-
# average BUY signal in a bearish tape is *still* the strategy emitting a
# trade, and the asymmetry between trend-aligned and counter-trend signals
# from the same strategy is large in trending regimes. We now apply
# direction-aware multipliers to all strategies that have a meaningful
# directional bias in trending regimes.
STRATEGY_REGIME_PREF_DIRECTIONAL: Dict[str, Dict[str, Dict[str, float]]] = {
    "mean_reversion": {
        # Bull regimes: BUY-the-dip is the primary edge, SELL-the-rally
        # fights the prevailing trend.
        "bull_low_vol":  {"BUY": 1.0, "SELL": 0.4},
        "bull_high_vol": {"BUY": 0.7, "SELL": 0.3},
        # Bear regimes: SELL-the-rally is the primary edge, BUY-the-dip
        # is suicidal (catching falling knives).
        "bear_low_vol":  {"BUY": 0.3, "SELL": 0.8},
        "bear_high_vol": {"BUY": 0.1, "SELL": 0.7},
        # Sideways: both directions work — that's the natural habitat for MR.
        "sideways":      {"BUY": 1.4, "SELL": 1.4},
        "unknown":       {"BUY": 1.0, "SELL": 1.0},
    },
    # VWAP-bounce has a similar (but milder) asymmetry: bouncing off
    # support in an uptrend is much higher-probability than fading
    # resistance in the same uptrend.
    "vwap_bounce": {
        "bull_low_vol":  {"BUY": 1.1, "SELL": 0.6},
        "bull_high_vol": {"BUY": 0.9, "SELL": 0.5},
        "bear_low_vol":  {"BUY": 0.5, "SELL": 1.0},
        "bear_high_vol": {"BUY": 0.3, "SELL": 0.8},
        "sideways":      {"BUY": 1.2, "SELL": 1.2},
        "unknown":       {"BUY": 1.0, "SELL": 1.0},
    },
    # Trend-following strategies are the most asymmetric of all in trending
    # regimes — a moving-average BUY in a bear tape is fighting the trend
    # (low edge), while a moving-average SELL in a bear tape rides with it
    # (high edge). Same logic for supertrend (a pure trend strategy).
    #
    # Pre-2026-05-04 these used the symmetric STRATEGY_REGIME_PREF table,
    # which suppressed BOTH directions equally in bear regimes. Today's run
    # exposed the cost: PRESTIGE had mean_reversion SELL (Z=2.16) +
    # rsi_momentum SELL (RSI 65 reversal) converging in bear_high_vol but
    # was rejected by the ensemble because rsi_momentum's SELL multiplier
    # was only 0.5 (symmetric), pulling the weighted confidence below 0.55.
    # With direction-aware weighting, the trend-aligned shorting setup
    # passes — exactly the multi-strategy high-conviction trade the
    # ensemble was designed to surface.
    "moving_average_crossover": {
        "bull_low_vol":  {"BUY": 1.3, "SELL": 0.6},
        "bull_high_vol": {"BUY": 1.1, "SELL": 0.5},
        "bear_low_vol":  {"BUY": 0.5, "SELL": 1.2},
        "bear_high_vol": {"BUY": 0.4, "SELL": 1.0},
        "sideways":      {"BUY": 0.5, "SELL": 0.5},
        "unknown":       {"BUY": 1.0, "SELL": 1.0},
    },
    "supertrend_follow": {
        "bull_low_vol":  {"BUY": 1.4, "SELL": 0.5},
        "bull_high_vol": {"BUY": 1.2, "SELL": 0.4},
        "bear_low_vol":  {"BUY": 0.4, "SELL": 1.3},
        "bear_high_vol": {"BUY": 0.3, "SELL": 1.1},
        "sideways":      {"BUY": 0.4, "SELL": 0.4},
        "unknown":       {"BUY": 1.0, "SELL": 1.0},
    },
    # RSI momentum: in bull regimes a BUY (bounce off oversold) rides the
    # trend; in bear regimes a SELL (rejection at overbought) rides the
    # trend. The contrarian side of each is suppressed (catching falling
    # knives in bear, fading rallies in bull) but less aggressively than
    # for mean_reversion since RSI extremes are slower / cleaner signals.
    "rsi_momentum": {
        "bull_low_vol":  {"BUY": 1.2, "SELL": 0.7},
        "bull_high_vol": {"BUY": 1.0, "SELL": 0.6},
        "bear_low_vol":  {"BUY": 0.6, "SELL": 1.0},
        "bear_high_vol": {"BUY": 0.5, "SELL": 0.9},
        "sideways":      {"BUY": 0.8, "SELL": 0.8},
        "unknown":       {"BUY": 1.0, "SELL": 1.0},
    },
    # Opening Range Breakout: the *direction* of the breakout is itself
    # information (long ORB = upside break, short ORB = downside break),
    # so directional weighting is mostly about whether that break aligns
    # with the prevailing trend. Bear-regime downside breaks tend to
    # follow through; upside breaks more often reverse.
    "opening_range_breakout": {
        "bull_low_vol":  {"BUY": 1.2, "SELL": 0.7},
        "bull_high_vol": {"BUY": 1.3, "SELL": 0.6},
        "bear_low_vol":  {"BUY": 0.7, "SELL": 1.2},
        "bear_high_vol": {"BUY": 0.6, "SELL": 1.3},
        "sideways":      {"BUY": 0.5, "SELL": 0.5},
        "unknown":       {"BUY": 1.0, "SELL": 1.0},
    },
}


def classify_regime(market_context: Optional[Dict]) -> str:
    """
    Map current market context to a regime label.

    Args:
        market_context: Dict with keys `nifty_trend` (1 above 200EMA, -1 below)
                        and `india_vix` (latest VIX).
    """
    if not market_context:
        return "unknown"

    trend = market_context.get("nifty_trend")
    vix = market_context.get("india_vix")

    if trend is None or vix is None:
        return "unknown"

    high_vol = vix >= 16.0

    if trend == 1:
        return "bull_high_vol" if high_vol else "bull_low_vol"
    if trend == -1:
        return "bear_high_vol" if high_vol else "bear_low_vol"
    return "sideways"


def classify_intraday_regime(market_context: Optional[Dict]) -> str:
    """
    Classify the *current* intraday market regime overlay.

    The daily-scale `classify_regime()` is computed from Nifty's position
    vs its 200-EMA and VIX, both slow-moving signals. A 2026-05-14 audit
    surfaced the gap: the agent treated a full bear_high_vol session
    identically even when intraday Nifty momentum had clearly flipped
    relief-rally, leaving the SHORT book defenseless against squeeze
    risk in the afternoon.

    The intraday overlay reads two extra fields from `market_context`:

    * ``nifty_intraday_pct`` -- Nifty 50 percentage change over the last
      ~60 min. Positive = up move, negative = down move.
    * ``vix_intraday_delta`` -- absolute VIX change vs morning opening
      print. Positive = vol expanding (risk-off proxy).

    Mapping (asymmetric on purpose -- panic moves trigger faster than rally
    moves because the cost of being long during a flash crash dwarfs the
    cost of missing some upside in a relief bounce):

    * `risk_off`  : nifty_intraday_pct <= -0.5%   OR   vix_intraday_delta >= +1.5
    * `risk_on`   : nifty_intraday_pct >=  0.5%   AND  vix_intraday_delta <= +0.5
    * `neutral`   : everything else
    * `unknown`   : missing fields (legacy/permissive)

    Designed so callers can apply a *tightening* on top of `classify_regime`
    without inverting it -- a daily `bull_low_vol` + intraday `risk_off`
    means "cooled bull session, be careful with new longs", not "bear".
    """
    if not market_context:
        return "unknown"
    nifty_intraday = market_context.get("nifty_intraday_pct")
    vix_delta = market_context.get("vix_intraday_delta")
    if nifty_intraday is None and vix_delta is None:
        return "unknown"

    nifty_intraday = float(nifty_intraday or 0.0)
    vix_delta = float(vix_delta or 0.0)

    if nifty_intraday <= -0.5 or vix_delta >= 1.5:
        return "risk_off"
    if nifty_intraday >= 0.5 and vix_delta <= 0.5:
        return "risk_on"
    return "neutral"


def regime_multiplier(
    strategy_name: str,
    regime: str,
    direction: Optional[str] = None,
) -> float:
    """
    Return multiplier (0-1.5) for a strategy in a given regime.

    Args:
        strategy_name: Name of the strategy (e.g. "mean_reversion").
        regime: Regime label produced by `classify_regime`.
        direction: Optional "BUY" or "SELL". When provided, directional
                   overrides take precedence (see STRATEGY_REGIME_PREF_DIRECTIONAL).
                   When omitted (or "HOLD"), the symmetric multiplier is used.

    Notes:
        - Direction-aware mode lets us correctly down-weight mean-reversion
          BUYs in bear regimes (catching falling knives) while keeping
          mean-reversion SELLs (fading rallies) at a healthy weight.
        - If a strategy is not listed in either map, returns 1.0 (no
          regime preference — neutral).
    """
    if direction in ("BUY", "SELL"):
        directional = STRATEGY_REGIME_PREF_DIRECTIONAL.get(strategy_name)
        if directional is not None:
            regime_map = directional.get(regime)
            if regime_map is not None:
                value = regime_map.get(direction)
                if value is not None:
                    return value
            # Strategy has directional overrides but not for this regime —
            # fall through to symmetric.
    pref = STRATEGY_REGIME_PREF.get(strategy_name)
    if pref is None:
        return 1.0
    return pref.get(regime, 1.0)
