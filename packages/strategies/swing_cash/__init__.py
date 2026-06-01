"""V4 Mode A swing-cash strategies (charter §1).

Net-new in v4. Strategy modules in this package export either a
``CrossAssetTrendV27``-style BaseStrategy class (the live-runtime path)
or a ``SPEC: research.swing_backtester.StrategySpec`` constant (the
multi-strategy backtest engine path).

Multi-strategy roster (charter v4 §3, Phase 13 2026-06-01):

    V35_donchian55_20            cross-asset Donchian-55/20 trend-follow
                                 (engine sanity baseline reproducing V32)
    V36_mean_reversion_swing     RSI(14)<25 in 200-SMA uptrend
    V37_pullback_to_sma50        50-SMA bounce + up-day confirm in 200-SMA uptrend
    V38_weekly_breakout          weekly Donchian-20/10 + 40-week regime
    V39_macd_swing               MACD(12,26,9) bullish cross in 200-SMA uptrend
    V40_dual_momentum_relstrength top-quintile 12-mo momentum + abs > 0 + > NIFTYBEES

The original ``cross_asset_trend_v27`` BaseStrategy class is preserved
for the live agent's strategy registry path. Engine B (the
``swing_backtester`` multi-strategy engine) uses the SPEC-style modules
instead — see ``donchian_55_20_spec`` for the V27 equivalent wrapped
as a SPEC.
"""
