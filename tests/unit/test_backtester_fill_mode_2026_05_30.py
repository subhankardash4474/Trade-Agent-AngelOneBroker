"""Regression tests for A2-1 — ``BacktestConfig.fill_mode``.

Lands as part of v3.0 charter Phase A2 (2026-05-30). The
gap-analysis output (`docs/diagnoses/v3_backtester_gap_analysis_2026-05-30.md`
§4 + §10 A2-1) identified next-day-open entry fills as the single
real engine change for v3 swing variants.

Three properties pinned here:

1. **Legacy mode (``close_plus_slippage``) is byte-identical to pre-A2-1.**
   A signal at bar N's close fills at bar N's close + slippage. Existing
   v2.1 variants set ``fill_mode`` to its default (or omit the key) and
   must produce the same trade record they always did. This is the
   default-preserves-behaviour smoke.

2. **next_bar_open mode fills at bar N+1's open + slippage.** A signal at
   bar N's close on a daily series fills at the NEXT trading day's open
   plus slippage. Hand-computed expectation for a 3-bar synthetic with a
   gap-up open: entry price = next_bar.open * (1 + slippage_pct/100).

3. **Signal on the FINAL bar drops silently.** When fill_mode is
   ``next_bar_open`` AND the signal arrives on the symbol's last bar,
   no trade fires and ``GateStats.no_next_bar`` increments by exactly 1
   per such event. Counter is visible in ``result.gate_stats.as_dict()``
   so a 180-day swing battery can detect end-of-window edge effects.

We construct a deterministic 3-bar daily fixture and inject a stub
strategy via subclass override of ``EnsembleBacktester._build_strategies``
to keep the test independent of strategy-registry state and of the
ensemble weight table. The stub emits BUY on a configurable bar and
HOLD elsewhere. This isolates the fill-pricing logic from any
strategy-specific behaviour.

Cross-references:
* `docs/freeze/freeze_v3.0_charter_2026-05-30.md` §6 (Phase A2 plan).
* `docs/diagnoses/v3_backtester_gap_analysis_2026-05-30.md` §4 (gap), §10 (A2-1 plan).
* `packages/research/backtest_ensemble.py` (BacktestConfig.fill_mode, GateStats.no_next_bar).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import pytz

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from research.backtest_ensemble import (  # noqa: E402
    BacktestConfig,
    EnsembleBacktester,
)
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal  # noqa: E402


IST = pytz.timezone("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


def _make_daily_ohlcv(
    n_bars: int,
    *,
    base_price: float = 100.0,
    gap_open: float | None = None,
    seed: int = 7,
) -> pd.DataFrame:
    """Build a deterministic daily OHLCV frame with an IST DatetimeIndex.

    ``gap_open`` (when set) sets bar 1's open to a specific value so
    the test can hand-compute the next-bar-open fill price. The other
    bars are tame random walks around ``base_price`` so SL/TP don't
    fire intra-bar (we want to isolate the entry-fill code path).
    """
    rng = np.random.default_rng(seed)
    closes = [base_price + i * 0.1 for i in range(n_bars)]
    opens = [c + rng.uniform(-0.2, 0.2) for c in closes]
    if gap_open is not None and n_bars >= 2:
        opens[1] = gap_open
    highs = [max(o, c) + 0.5 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.5 for o, c in zip(opens, closes)]
    volumes = [50_000.0 + i * 100 for i in range(n_bars)]
    # ATR column required by EnsembleBacktester._latest_atr;
    # _bump_equity also reads close. Other feature columns are unused
    # by the entry path under test.
    atr = [1.5] * n_bars
    idx = pd.date_range(
        "2026-01-05 09:15:00", periods=n_bars, freq="D", tz="Asia/Kolkata"
    )
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "atr": atr,
        },
        index=idx,
    )


class _StubBuyOnBarStrategy(BaseStrategy):
    """Emits BUY on a configured bar, HOLD elsewhere. Used to drive
    deterministic single-trade simulations against the entry-fill path.
    """

    name = "stub_buy_on_bar"

    def __init__(self, params: dict):
        super().__init__(name=self.name, params=params or {})
        self._buy_bar_idx: int = int(params.get("buy_bar_idx", 0))

    @property
    def required_history_bars(self) -> int:
        return 1

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        # The slice the engine hands us starts at the per-event window
        # cap (max 300 bars) and ends INCLUSIVE of the current bar. The
        # last row is always the bar being evaluated. We want to fire
        # BUY on the bar whose original-index matches buy_bar_idx; the
        # cleanest signal-time identifier is the bar's index timestamp.
        last = data.iloc[-1]
        ts = last.name
        return TradeSignal(
            signal=(
                Signal.BUY
                if ts == data.index[-1] and self._is_target_bar(last)
                else Signal.HOLD
            ),
            symbol=symbol,
            price=float(last["close"]),
            timestamp=ts,
            strategy_name=self.name,
            confidence=0.99,  # well above the default 0.55 ensemble threshold
        )

    def _is_target_bar(self, last: pd.Series) -> bool:
        # Marker-based: the engine passes a slice that grows by one row
        # per event. The slice's length is the (window-capped) bar
        # number+1. For this test we use a hand-tagged ``volume``
        # marker so the stub matches exactly the bar we want. Setting
        # volume to a sentinel value at fixture-build time is far more
        # robust than re-deriving the bar index from a time-modulo
        # check, which would make the test brittle to DataFrame
        # construction details.
        return abs(float(last["volume"]) - _BUY_BAR_MARKER) < 0.5


# Sentinel volume marker used by the stub strategy to recognise the
# bar on which it should emit BUY. Avoids any reliance on positional
# indexing of the slice (which is window-capped, so bar 0 of the
# original df may not be slice[0]).
_BUY_BAR_MARKER = 999_999.0


class _BuyOnBarBacktester(EnsembleBacktester):
    """Subclass that injects ``_StubBuyOnBarStrategy`` instead of going
    through STRATEGY_REGISTRY. Keeps the tests independent of registry
    state, ensemble weights, and the runtime DEFAULT_WEIGHTS dict.
    """

    def _build_strategies(self, names):  # type: ignore[override]
        return [_StubBuyOnBarStrategy(params={})]


def _stub_ensemble_with_marker(target_bar_idx: int) -> dict:
    """Build a config that lets the ensemble pass our stub's BUY
    through. The default weights table doesn't include
    ``stub_buy_on_bar``; we add it explicitly so the aggregator's
    confidence math reaches the 0.55 default.
    """
    return {
        "ensemble": {
            "confidence_threshold": 0.55,
            "weights": {"stub_buy_on_bar": 2.0},
            "min_strategies_agree": 1,  # single-strategy signal must pass
        },
        "strategies": {
            "active": ["stub_buy_on_bar"],
        },
        "risk": {
            "min_profit_to_charges_ratio": 0.0,  # disable charges gate
            "min_absolute_reward_rs": 0.0,
        },
        "robustness": {
            "min_entry_atr_pct": 0.0,
        },
    }


def _mark_buy_bar(df: pd.DataFrame, idx: int) -> pd.DataFrame:
    """Stamp the chosen bar with the sentinel volume marker so the
    stub strategy fires there and only there.
    """
    df = df.copy()
    df.iloc[idx, df.columns.get_loc("volume")] = _BUY_BAR_MARKER
    return df


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────


class TestFillModeLegacyCloseSlippage:
    """Default fill_mode preserves v2.1 byte-identical behaviour."""

    def test_default_fill_mode_string(self):
        """Defensive: the default must be ``close_plus_slippage`` so
        every legacy variant gets v2.1 behaviour without explicit
        opt-in. Documented contract; flipping the default would silently
        change every legacy battery result."""
        assert BacktestConfig().fill_mode == "close_plus_slippage"

    def test_legacy_entry_price_is_close_plus_slippage(self):
        """Stub fires BUY on bar 0; entry should fill at bar 0's close
        + slippage (deterministic when paper_seed is None — the apply
        slippage path uses the static slippage_pct for paper_seed=None).
        """
        df = _mark_buy_bar(_make_daily_ohlcv(n_bars=4), idx=0)
        market_data = {"AAA": df}

        cfg = _stub_ensemble_with_marker(target_bar_idx=0)
        bt_cfg = BacktestConfig(
            initial_capital=100_000.0,
            slippage_pct=1.0,  # 1% — large so the test signal is unambiguous
            confidence_threshold=0.55,
            apply_dead_hour=False,
            apply_expected_profit_gate=False,
            min_entry_atr_pct=0.0,
            max_positions=1,
            max_losses_per_stock=99,
            paper_seed=None,  # deterministic mean-slippage path
            # fill_mode left at default (close_plus_slippage)
        )
        bt = _BuyOnBarBacktester(cfg, bt_cfg)
        result = bt.run(symbols=["AAA"], market_data=market_data)

        # Exactly one trade fired. Entry at bar 0 close + 1% slippage
        # (BUY entries pay slippage above the close).
        assert result.gate_stats.executed == 1
        assert len(result.trades) == 1
        trade = result.trades[0]
        bar0_close = float(df["close"].iloc[0])
        expected_entry = bar0_close * 1.01
        assert trade["entry_price"] == pytest.approx(expected_entry, abs=1e-6), (
            f"legacy fill mode should fill at bar0.close * 1.01 = "
            f"{expected_entry:.4f}; got {trade['entry_price']:.4f}"
        )
        # Entry timestamp matches the signal bar (no next-bar shift).
        assert trade["entry_time"] is not None
        entry_dt = pd.Timestamp(trade["entry_time"])
        assert entry_dt.normalize() == df.index[0].normalize(), (
            f"legacy mode entry_time must be the signal bar's "
            f"timestamp; got {entry_dt} expected {df.index[0]}"
        )

    def test_legacy_no_next_bar_counter_stays_zero(self):
        """Sanity: the new ``no_next_bar`` gate stat must NEVER increment
        in legacy mode (the next-bar lookup is dead code there)."""
        df = _mark_buy_bar(_make_daily_ohlcv(n_bars=3), idx=2)  # signal on FINAL bar
        market_data = {"AAA": df}

        cfg = _stub_ensemble_with_marker(target_bar_idx=2)
        bt_cfg = BacktestConfig(
            initial_capital=100_000.0,
            slippage_pct=0.0,
            confidence_threshold=0.55,
            apply_dead_hour=False,
            apply_expected_profit_gate=False,
            min_entry_atr_pct=0.0,
            max_positions=1,
            max_losses_per_stock=99,
            # fill_mode default
        )
        bt = _BuyOnBarBacktester(cfg, bt_cfg)
        result = bt.run(symbols=["AAA"], market_data=market_data)

        assert result.gate_stats.no_next_bar == 0, (
            "legacy fill mode must never touch no_next_bar; got "
            f"{result.gate_stats.no_next_bar}"
        )
        # Trade DOES fire on the final bar in legacy mode (close-fill).
        assert result.gate_stats.executed == 1


class TestFillModeNextBarOpen:
    """``next_bar_open`` mode fills at bar N+1's open + slippage."""

    def test_next_bar_open_uses_next_bar_open_price(self):
        """Hand-computed expectation: bar 0 close = 100.0, bar 1 open
        is forced to 102.0 via the gap_open fixture knob. Entry should
        fill at 102.0 * 1.01 = 103.02 (BUY pays slippage upward), NOT
        at 100.0 * 1.01.
        """
        df = _mark_buy_bar(
            _make_daily_ohlcv(n_bars=4, gap_open=102.0),
            idx=0,
        )
        market_data = {"AAA": df}

        cfg = _stub_ensemble_with_marker(target_bar_idx=0)
        bt_cfg = BacktestConfig(
            initial_capital=100_000.0,
            slippage_pct=1.0,
            confidence_threshold=0.55,
            apply_dead_hour=False,
            apply_expected_profit_gate=False,
            min_entry_atr_pct=0.0,
            max_positions=1,
            max_losses_per_stock=99,
            paper_seed=None,
            fill_mode="next_bar_open",
        )
        bt = _BuyOnBarBacktester(cfg, bt_cfg)
        result = bt.run(symbols=["AAA"], market_data=market_data)

        assert result.gate_stats.executed == 1
        assert len(result.trades) == 1
        trade = result.trades[0]
        expected_entry = 102.0 * 1.01  # next bar's open + 1% slippage
        assert trade["entry_price"] == pytest.approx(expected_entry, abs=1e-6), (
            f"next_bar_open should fill at bar1.open * 1.01 = "
            f"{expected_entry:.4f}; got {trade['entry_price']:.4f}. "
            f"If this fails the engine is filling at the SIGNAL bar's "
            f"close, which is the v2.1 close_plus_slippage behaviour "
            f"this mode is designed to replace."
        )

    def test_next_bar_open_uses_next_bar_timestamp(self):
        """Entry timestamp must be bar N+1's index, not bar N's. Swing
        trade ``holding_minutes`` / ``holding_days`` accounting depends
        on this.
        """
        df = _mark_buy_bar(
            _make_daily_ohlcv(n_bars=4, gap_open=102.0),
            idx=0,
        )
        market_data = {"AAA": df}

        cfg = _stub_ensemble_with_marker(target_bar_idx=0)
        bt_cfg = BacktestConfig(
            initial_capital=100_000.0,
            slippage_pct=0.0,
            confidence_threshold=0.55,
            apply_dead_hour=False,
            apply_expected_profit_gate=False,
            min_entry_atr_pct=0.0,
            max_positions=1,
            max_losses_per_stock=99,
            fill_mode="next_bar_open",
        )
        bt = _BuyOnBarBacktester(cfg, bt_cfg)
        result = bt.run(symbols=["AAA"], market_data=market_data)

        trade = result.trades[0]
        assert trade["entry_time"] is not None
        entry_dt = pd.Timestamp(trade["entry_time"])
        # Bar 1's date, not bar 0's.
        assert entry_dt.normalize() == df.index[1].normalize(), (
            f"next_bar_open mode must record entry_time as bar1's "
            f"timestamp ({df.index[1]}), got {entry_dt}"
        )

    def test_signal_on_final_bar_drops_silently(self):
        """A signal on the symbol's last bar has no next bar to fill on;
        the engine must drop the signal AND increment no_next_bar by 1.
        """
        df = _mark_buy_bar(_make_daily_ohlcv(n_bars=3), idx=2)  # final bar
        market_data = {"AAA": df}

        cfg = _stub_ensemble_with_marker(target_bar_idx=2)
        bt_cfg = BacktestConfig(
            initial_capital=100_000.0,
            slippage_pct=0.0,
            confidence_threshold=0.55,
            apply_dead_hour=False,
            apply_expected_profit_gate=False,
            min_entry_atr_pct=0.0,
            max_positions=1,
            max_losses_per_stock=99,
            fill_mode="next_bar_open",
        )
        bt = _BuyOnBarBacktester(cfg, bt_cfg)
        result = bt.run(symbols=["AAA"], market_data=market_data)

        assert result.gate_stats.no_next_bar == 1, (
            f"final-bar signal under next_bar_open must increment "
            f"no_next_bar by 1; got {result.gate_stats.no_next_bar}"
        )
        assert result.gate_stats.executed == 0
        assert len(result.trades) == 0

    def test_nan_open_in_next_bar_is_absorbed_under_no_next_bar(self):
        """Defensive: data-quality glitches (NaN / zero open) must not
        feed through to the slippage path. The engine absorbs them
        under no_next_bar with the same drop-silently semantics as a
        truly missing next bar.
        """
        df = _make_daily_ohlcv(n_bars=4, gap_open=102.0)
        df.iloc[1, df.columns.get_loc("open")] = float("nan")
        df = _mark_buy_bar(df, idx=0)
        market_data = {"AAA": df}

        cfg = _stub_ensemble_with_marker(target_bar_idx=0)
        bt_cfg = BacktestConfig(
            initial_capital=100_000.0,
            slippage_pct=0.0,
            confidence_threshold=0.55,
            apply_dead_hour=False,
            apply_expected_profit_gate=False,
            min_entry_atr_pct=0.0,
            max_positions=1,
            max_losses_per_stock=99,
            fill_mode="next_bar_open",
        )
        bt = _BuyOnBarBacktester(cfg, bt_cfg)
        result = bt.run(symbols=["AAA"], market_data=market_data)

        assert result.gate_stats.no_next_bar == 1
        assert result.gate_stats.executed == 0


class TestFillModeBatteryPlumbing:
    """The battery harness must read ``backtest.fill_mode`` from config
    and pass it through to BacktestConfig. Without this, v3 swing
    variants would silently get the v2.1 default."""

    def test_battery_bt_config_reads_fill_mode_from_config(self):
        """Direct call into the battery's _bt_config helper to confirm
        it picks up ``backtest.fill_mode`` from the variant cfg dict.
        """
        from research.battery import _bt_config

        cfg_default = {}  # no backtest section
        bt_default = _bt_config(cfg_default)
        assert bt_default.fill_mode == "close_plus_slippage", (
            "battery harness default must preserve v2.1 behaviour"
        )

        cfg_swing = {"backtest": {"fill_mode": "next_bar_open"}}
        bt_swing = _bt_config(cfg_swing)
        assert bt_swing.fill_mode == "next_bar_open", (
            "battery harness must plumb backtest.fill_mode through to "
            "BacktestConfig.fill_mode; if this fails, V20+ swing variants "
            "will silently run with the v2.1 close_plus_slippage default."
        )


class TestGateStatsNoNextBarSurfaced:
    """``GateStats.no_next_bar`` must appear in ``as_dict()`` so battery
    comparison.md and the per-variant JSON include it. Otherwise an
    operator reading the gate table can't tell why a 180d swing run
    has fewer trades than expected."""

    def test_gate_stats_includes_no_next_bar(self):
        from research.backtest_ensemble import GateStats

        gs = GateStats()
        d = gs.as_dict()
        assert "no_next_bar" in d
        assert d["no_next_bar"] == 0
        gs.no_next_bar = 3
        assert gs.as_dict()["no_next_bar"] == 3


class TestHoldingDaysFieldOnTrades:
    """A2-2: trade dicts in result.trades must carry ``holding_days``
    so swing-trade analyses can read calendar-day holds without
    re-deriving from holding_minutes / entry_time / exit_time.
    """

    def test_holding_days_present_on_trade_dict(self):
        """Run a 4-bar daily backtest in next_bar_open mode where the
        BUY fires on bar 0 (fills at bar 1 open) and the position is
        flushed at the end-of-backtest on bar 3. Expected holding_days
        = 3 - 1 = 2 calendar days (bar 1 to bar 3 inclusive of weekends
        if any; here it's 4 consecutive D-frequency bars, so 2 days).
        """
        df = _mark_buy_bar(
            _make_daily_ohlcv(n_bars=4, gap_open=102.0),
            idx=0,
        )
        market_data = {"AAA": df}

        cfg = _stub_ensemble_with_marker(target_bar_idx=0)
        bt_cfg = BacktestConfig(
            initial_capital=100_000.0,
            slippage_pct=0.0,
            confidence_threshold=0.55,
            apply_dead_hour=False,
            apply_expected_profit_gate=False,
            min_entry_atr_pct=0.0,
            max_positions=1,
            max_losses_per_stock=99,
            fill_mode="next_bar_open",
        )
        bt = _BuyOnBarBacktester(cfg, bt_cfg)
        result = bt.run(symbols=["AAA"], market_data=market_data)

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert "holding_days" in trade, (
            "trade dict must include holding_days for swing readability"
        )
        # Bar 1 → Bar 3 = 2 calendar days under D-frequency fixture.
        assert trade["holding_days"] == 2, (
            f"expected 2 calendar days hold (bar 1 entry → bar 3 exit), "
            f"got holding_days={trade['holding_days']}; entry_time="
            f"{trade['entry_time']} exit_time={trade['exit_time']}"
        )

    def test_holding_days_legacy_mode_same_day_close(self):
        """Legacy fill mode + flushing on the same bar = entry_time
        equals the signal bar's index, exit_time is the END-OF-BACKTEST
        flush at the symbol's final bar. Two-bar fixture means
        holding_days = 1 (bar 0 → bar 1).
        """
        df = _mark_buy_bar(_make_daily_ohlcv(n_bars=2), idx=0)
        market_data = {"AAA": df}

        cfg = _stub_ensemble_with_marker(target_bar_idx=0)
        bt_cfg = BacktestConfig(
            initial_capital=100_000.0,
            slippage_pct=0.0,
            confidence_threshold=0.55,
            apply_dead_hour=False,
            apply_expected_profit_gate=False,
            min_entry_atr_pct=0.0,
            max_positions=1,
            max_losses_per_stock=99,
            # fill_mode left at default
        )
        bt = _BuyOnBarBacktester(cfg, bt_cfg)
        result = bt.run(symbols=["AAA"], market_data=market_data)

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade["holding_days"] == 1, (
            f"expected 1 calendar day hold (bar 0 → bar 1 final-flush), "
            f"got {trade['holding_days']}"
        )
