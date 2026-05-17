"""
Risk Manager Module (Enhanced)
Comprehensive risk management with ATR-based stops, trailing stop-loss,
VIX-based regime filtering, consecutive loss protection, drawdown tiers,
weekly loss limits, and market regime awareness.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pytz
from loguru import logger

from core.charges import compute_round_trip

IST = pytz.timezone("Asia/Kolkata")


@dataclass
class RiskState:
    """Tracks the current risk state across multiple dimensions."""
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    daily_trades: int = 0
    peak_balance: float = 0.0
    current_balance: float = 0.0
    open_positions: int = 0
    consecutive_losses: int = 0
    daily_date: Optional[date] = None
    week_start: Optional[date] = None
    is_circuit_breaker_active: bool = False
    breaker_reason: str = ""
    recent_trade_results: deque = field(default_factory=lambda: deque(maxlen=20))

    def reset_daily(self, today: date):
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_date = today
        self.is_circuit_breaker_active = False
        self.breaker_reason = ""
        self.consecutive_losses = 0

    def reset_weekly(self, week_start: date):
        self.weekly_pnl = 0.0
        self.week_start = week_start


class TrailingStop:
    """Manages a trailing stop-loss for an open position.

    Two complementary protections:

      1. Classic trailing SL (price-based, 0.3% from peak).
         Activates only after 1R favorable move. Locks in profit on
         small reversals once we're already in the green.

      2. Peak-giveback exit (P&L-based, default 35% of peak R).
         Tracks peak unrealized R; arms after `peak_arm_rr` R and
         triggers an exit when current_R falls back by `peak_giveback_pct`
         of peak_R. This handles the case where price drifts back from
         a strong MFE without ever crossing the trailing stop level
         (e.g. today's MEESHO: peak +Rs 276 -> exit on signal at +Rs 71,
         a 74% giveback the trailing logic missed).

         Caller checks `should_peak_giveback_exit()` separately and
         closes the position if True. The classic trail and peak-giveback
         are independent — whichever fires first wins.
    """

    def __init__(
        self,
        entry_price: float,
        initial_sl: float,
        side: str = "BUY",
        trail_activation_rr: float = 1.0,
        trail_step_pct: float = 0.3,
        peak_arm_rr: float = 1.5,
        peak_giveback_pct: float = 35.0,
        peak_giveback_enabled: bool = True,
        breakeven_arm_rr: float = 0.5,
        breakeven_buffer_pct: float = 0.10,
        breakeven_enabled: bool = True,
        symbol: str = "",
    ):
        self.entry_price = entry_price
        self.current_sl = initial_sl
        self.side = side
        # 2026-05-15 Symbol kept for observability logs on arm transitions.
        # Default "" preserves the legacy API; RiskManager.create_trailing_stop
        # plumbs the real symbol through.
        self.symbol = symbol
        self.trail_activation_rr = trail_activation_rr
        self.trail_step_pct = trail_step_pct
        self.highest_since_entry = entry_price
        self.lowest_since_entry = entry_price
        self.trailing_active = False
        self._initial_risk = abs(entry_price - initial_sl)

        # Peak-giveback state
        self.peak_arm_rr = peak_arm_rr
        self.peak_giveback_pct = peak_giveback_pct
        self.peak_giveback_enabled = peak_giveback_enabled
        self.peak_unrealized_r: float = 0.0
        self.peak_giveback_armed: bool = False
        self.last_unrealized_r: float = 0.0

        # 2026-05-14 Breakeven-stop guard ---------------------------------
        # Plugs the "MFE between 0.5R and trail_activation_rr (1.0R) where
        # the position drifts back to a loss" hole. The classic trail only
        # arms at 1.0R; below that, a position that ran +0.8R then reversed
        # would lose the entire initial-SL distance. Breakeven moves the SL
        # to entry-plus-buffer once MFE crosses `breakeven_arm_rr`, so the
        # worst case once we've reached half-R favorable is a scratch trade
        # (small win on the buffer or small loss on the buffer overshooting
        # back through entry). The buffer is in percent of entry to cover
        # round-trip charges (~6 bps) plus a small slippage cushion.
        self.breakeven_arm_rr = breakeven_arm_rr
        self.breakeven_buffer_pct = breakeven_buffer_pct
        self.breakeven_enabled = breakeven_enabled
        self.breakeven_armed: bool = False

    def _current_unrealized_r(self, current_price: float) -> float:
        if self._initial_risk <= 0:
            return 0.0
        if self.side == "BUY":
            return (current_price - self.entry_price) / self._initial_risk
        return (self.entry_price - current_price) / self._initial_risk

    def update(self, current_price: float) -> float:
        """Update trailing stop based on latest price. Returns current SL.

        Also tracks peak-unrealized-R for the giveback exit. Caller must
        invoke `should_peak_giveback_exit()` separately to act on it.
        """
        unrealized_r = self._current_unrealized_r(current_price)
        self.last_unrealized_r = unrealized_r
        self.peak_unrealized_r = max(self.peak_unrealized_r, unrealized_r)
        # 2026-05-15 Observability: log the FIRST time peak-giveback arms so
        # operators can audit which positions ever reached +peak_arm_rr R.
        # Without this, the only evidence of arming was an eventual exit
        # tagged `peak_giveback` — silent if the position later closed via
        # signal/trailing/SL/intraday-exit instead.
        if not self.peak_giveback_armed and self.peak_unrealized_r >= self.peak_arm_rr:
            self.peak_giveback_armed = True
            logger.info(
                f"[PEAK-GIVEBACK-ARMED] {self.symbol or '?'} {self.side} "
                f"peak_R={self.peak_unrealized_r:.2f} "
                f"(arm_rr={self.peak_arm_rr:.2f}, giveback_pct={self.peak_giveback_pct:.0f}%)"
            )

        # Breakeven arm (2026-05-14) -- monotonic, never disarms.
        if (
            self.breakeven_enabled
            and not self.breakeven_armed
            and self.peak_unrealized_r >= self.breakeven_arm_rr
        ):
            self.breakeven_armed = True
            # 2026-05-15 Observability: log on the False->True transition so
            # operators can audit which positions had the breakeven SL lift
            # engaged. Yesterday's 5 stop-outs at -1.5% are a likely cohort
            # this would have saved (or proven not to apply, if MFE never
            # crossed 0.5R favorable).
            be_sign = "+" if self.side == "BUY" else "-"
            be_pct = self.breakeven_buffer_pct
            logger.info(
                f"[BREAKEVEN-ARMED] {self.symbol or '?'} {self.side} "
                f"peak_R={self.peak_unrealized_r:.2f} entry={self.entry_price:.2f} "
                f"(SL will lift to entry {be_sign}{be_pct:.2f}%)"
            )

        if self.side == "BUY":
            self.highest_since_entry = max(self.highest_since_entry, current_price)
            # Breakeven first (lower bar than trail). Sets a floor at
            # entry + buffer; cannot move BELOW the existing SL (so a real
            # initial-SL stop-out still wins).
            if self.breakeven_armed:
                be_sl = self.entry_price * (1 + self.breakeven_buffer_pct / 100)
                self.current_sl = max(self.current_sl, be_sl)
            if unrealized_r >= self.trail_activation_rr:
                self.trailing_active = True
                new_sl = self.highest_since_entry * (1 - self.trail_step_pct / 100)
                self.current_sl = max(self.current_sl, new_sl)
        else:
            self.lowest_since_entry = min(self.lowest_since_entry, current_price)
            if self.breakeven_armed:
                # SHORT: breakeven SL sits BELOW entry by buffer (price has
                # to rise above entry-buffer to stop us out).
                be_sl = self.entry_price * (1 - self.breakeven_buffer_pct / 100)
                self.current_sl = min(self.current_sl, be_sl)
            if unrealized_r >= self.trail_activation_rr:
                self.trailing_active = True
                new_sl = self.lowest_since_entry * (1 + self.trail_step_pct / 100)
                self.current_sl = min(self.current_sl, new_sl)

        return self.current_sl

    def should_peak_giveback_exit(self) -> bool:
        """True iff peak-giveback condition met. Call AFTER update().

        Conditions:
          - Feature enabled.
          - Peak unrealized R reached `peak_arm_rr` at least once.
          - Current unrealized R has fallen by `peak_giveback_pct` of peak.
        Never triggers while still hitting new highs (giveback is 0).
        """
        if not self.peak_giveback_enabled or not self.peak_giveback_armed:
            return False
        if self.peak_unrealized_r <= 0:
            return False
        giveback_r = self.peak_unrealized_r - self.last_unrealized_r
        giveback_frac_pct = (giveback_r / self.peak_unrealized_r) * 100.0
        return giveback_frac_pct >= self.peak_giveback_pct


class RiskManager:
    """
    Production-grade risk management engine for ₹10,000 capital.

    Key rules (from spec):
      - Max risk per trade: 1% of capital (₹100)
      - Max position size: 20% of capital (₹2,000)
      - Hard stop-loss on EVERY trade, ATR-based: 1.5 x ATR(14)
      - Trailing stop: activated at 1:1 R:R
      - Time-based exit: all intraday by 3:15 PM
      - Max daily loss: 3% of capital
      - Max trades per day: 5
      - Max open positions: 2
      - Stop trading after 2 consecutive losses
      - Drawdown tiers: 15% → reduce size, 30% → HALT
      - Weekly max loss: 5%
      - Skip if India VIX > 25 or Nifty below 200 EMA
    """

    def __init__(self, config: dict, initial_balance: float,
                 peak_balance: Optional[float] = None,
                 absolute_daily_loss_floor_rs: Optional[float] = None):
        """
        Args:
            initial_balance: Starting balance. If the portfolio was seeded from
                the DB snapshot, pass the same value here so risk state stays
                in sync with portfolio state.
            peak_balance: Historical peak equity from DB. If provided, drawdown
                is measured against the real peak (not today's balance).
            absolute_daily_loss_floor_rs: Optional hard rupee floor on the
                day's realised P&L. Independent from the existing
                ``daily_loss_limit_pct`` (which is a percentage of capital).
                When both are set, whichever is *tighter* fires first.
                Set via ``run_daemon.py --max-loss-rs N`` for the e2e
                Stage 3 live basket runs, where the percentage limit on
                a Rs 1L config is too lax (Rs 3,000) for a Rs 5k basket
                experiment.
        """
        risk_cfg = config.get("risk", {})

        # Position sizing
        self.max_position_size_pct: float = risk_cfg.get("max_position_size_pct", 20.0)
        self.max_risk_per_trade_pct: float = risk_cfg.get("max_risk_per_trade_pct", 1.0)

        # Stop-loss / take-profit
        self.atr_stop_multiplier: float = risk_cfg.get("atr_stop_multiplier", 1.5)
        self.default_stop_loss_pct: float = risk_cfg.get("stop_loss_pct", 1.5)
        self.default_take_profit_pct: float = risk_cfg.get("take_profit_pct", 3.0)
        self.trailing_activation_rr: float = risk_cfg.get("trailing_activation_rr", 1.0)
        self.trailing_step_pct: float = risk_cfg.get("trailing_step_pct", 0.3)
        # Peak-giveback exit (2026-05-07): independent of price-trail.
        # Tracks peak unrealized R; once `peak_arm_rr` reached, an exit
        # fires the moment current_R has dropped by `peak_giveback_pct`
        # of peak_R. Designed for cases like today's MEESHO where price
        # drifted back from MFE without ever hitting the price-trail.
        self.peak_arm_rr: float = risk_cfg.get("peak_giveback_arm_rr", 1.5)
        self.peak_giveback_pct: float = risk_cfg.get("peak_giveback_pct", 35.0)
        self.peak_giveback_enabled: bool = risk_cfg.get("peak_giveback_enabled", True)

        # 2026-05-14 Breakeven-stop guard: lifts SL to entry+buffer once MFE
        # crosses `breakeven_arm_rr`. Plugs the dead zone between 0.5R and
        # 1.0R (trail_activation_rr) where a +0.8R MFE could still finish at
        # the initial SL. Buffer is in % of entry to cover round-trip charges
        # (~6 bps) plus a small slippage cushion. Set enabled=False to revert.
        self.breakeven_arm_rr: float = risk_cfg.get("breakeven_arm_rr", 0.5)
        self.breakeven_buffer_pct: float = risk_cfg.get("breakeven_buffer_pct", 0.10)
        self.breakeven_enabled: bool = risk_cfg.get("breakeven_enabled", True)

        # Daily limits
        self.daily_loss_limit_pct: float = risk_cfg.get("daily_loss_limit_pct", 3.0)
        self.max_trades_per_day: int = risk_cfg.get("max_trades_per_day", 5)
        self.max_open_positions: int = risk_cfg.get("max_open_positions", 2)
        self.max_consecutive_losses: int = risk_cfg.get("max_consecutive_losses", 2)

        # Optional absolute-rupee daily-loss floor (Stage 3 e2e safety net).
        # Distinct from the percentage-based limit: set both, whichever fires
        # first wins. None = disabled. Constructor kwarg takes precedence over
        # the config-file fallback so CLI overrides (--max-loss-rs) always win.
        cfg_abs_floor = risk_cfg.get("absolute_daily_loss_floor_rs", None)
        self.absolute_daily_loss_floor_rs: Optional[float] = (
            float(absolute_daily_loss_floor_rs)
            if absolute_daily_loss_floor_rs is not None
            else (float(cfg_abs_floor) if cfg_abs_floor is not None else None)
        )

        # Drawdown tiers
        self.drawdown_reduce_pct: float = risk_cfg.get("drawdown_reduce_pct", 15.0)
        self.drawdown_halt_pct: float = risk_cfg.get("drawdown_halt_pct", 30.0)
        self.max_drawdown_pct: float = risk_cfg.get("max_drawdown_pct", 10.0)

        # Regime-aware position-size multipliers (2026-05-13). Sized risk
        # already shrinks in DRAWDOWN tier; this adds REGIME-level scaling
        # on top, so we under-trade in environments where our backtested
        # edge is weakest. Empty dict / missing keys = no scaling (= 1.0).
        #
        # Default tuning is conservative -- cut in bear high-vol where we
        # have empirically seen the worst losses (2026-05-13 HCLTECH
        # supertrend short -Rs 148 in bear_high_vol; 2026-05-12 GODREJCP
        # short -Rs 6 also bear_high_vol). Expand in bull_low_vol where
        # long-side trend-followers historically perform best.
        regime_size_cfg = risk_cfg.get("regime_size_multipliers") or {}
        self.regime_size_multipliers: Dict[str, float] = {
            "bull_low_vol":  float(regime_size_cfg.get("bull_low_vol", 1.20)),
            "bull_high_vol": float(regime_size_cfg.get("bull_high_vol", 1.00)),
            "sideways":      float(regime_size_cfg.get("sideways", 0.85)),
            "bear_low_vol":  float(regime_size_cfg.get("bear_low_vol", 0.85)),
            "bear_high_vol": float(regime_size_cfg.get("bear_high_vol", 0.70)),
            "unknown":       float(regime_size_cfg.get("unknown", 1.00)),
        }

        # Weekly limit
        self.weekly_loss_limit_pct: float = risk_cfg.get("weekly_loss_limit_pct", 5.0)

        # Market regime
        self.max_vix: float = risk_cfg.get("max_vix", 25.0)
        self.require_nifty_above_200ema: bool = risk_cfg.get("require_nifty_above_200ema", True)

        # Intraday time exit
        self.intraday_exit_time: str = risk_cfg.get("intraday_exit_time", "15:15")

        # Expected-profit gate: reject trades where reward doesn't cover charges
        # with a safety multiplier (e.g. 2x). Protects against microscopic edges
        # being destroyed by STT + brokerage + GST + stamp duty + slippage.
        self.min_profit_to_charges_ratio: float = risk_cfg.get("min_profit_to_charges_ratio", 2.0)
        self.min_absolute_reward_rs: float = risk_cfg.get("min_absolute_reward_rs", 15.0)

        # Minimum SL distance (% of entry) — ATR-only stops on quiet stocks
        # come out < 1 %, which is inside normal intraday noise. Floor stops
        # to this value so we survive first-bar shakeouts.
        # 2026-04-30 audit: INDIANB hit 0.99 % SL in 4 minutes (noise, not signal).
        self.min_stop_loss_pct: float = float(risk_cfg.get("min_stop_loss_pct", 0.0))

        # Take-profit ceilings. Both are 0 = disabled; when set, we clamp TP
        # so it remains reachable intraday.
        self.max_tp_to_sl_multiple: float = float(risk_cfg.get("max_tp_to_sl_multiple", 0.0))
        self.max_tp_pct: float = float(risk_cfg.get("max_tp_pct", 0.0))

        # Per-strategy RR floors used by `is_trade_worth_taking`. Default
        # fallback is 1.2x — but mean_reversion's edge is win-rate, not RR,
        # so we accept lower RR for high-WR strategies and demand more for
        # trend-followers.
        raw_rr = risk_cfg.get("min_rr_by_strategy") or {}
        self.min_rr_by_strategy: Dict[str, float] = {
            str(k): float(v) for k, v in raw_rr.items()
        }
        self.default_min_rr: float = 1.2

        # Preserve historical peak across restarts so drawdown is measured
        # against the true lifetime high, not just today's seed balance.
        # Safeguard: if the DB peak implies a drawdown that already exceeds the
        # halt threshold, the DB is almost certainly polluted (test runs, old
        # seed, manual reset). We clamp the peak to the current balance so the
        # circuit breaker doesn't trip on startup for spurious historical data.
        effective_peak = max(initial_balance, peak_balance or 0.0)
        if peak_balance and peak_balance > initial_balance:
            implied_dd = (peak_balance - initial_balance) / peak_balance * 100
            if implied_dd >= self.drawdown_halt_pct:
                logger.warning(
                    f"DB historical peak Rs {peak_balance:,.2f} implies "
                    f"{implied_dd:.1f}% drawdown vs current Rs {initial_balance:,.2f} "
                    f"(halt threshold {self.drawdown_halt_pct}%). "
                    f"Treating as stale data and resetting peak to current balance. "
                    f"Use --reset-balance to formally reset."
                )
                effective_peak = initial_balance
            else:
                logger.info(
                    f"Risk state seeded with historical peak Rs {peak_balance:,.2f} "
                    f"(current balance Rs {initial_balance:,.2f}, "
                    f"implied drawdown {implied_dd:.1f}%)"
                )

        self.state = RiskState(
            current_balance=initial_balance,
            peak_balance=effective_peak,
            daily_date=datetime.now(IST).date(),
            week_start=self._get_week_start(datetime.now(IST).date()),
        )
        self._initial_balance = initial_balance

        # Trailing stops for each position
        self._trailing_stops: Dict[str, TrailingStop] = {}

    @staticmethod
    def _get_week_start(d: date) -> date:
        return d - timedelta(days=d.weekday())

    def _ensure_resets(self):
        today = datetime.now(IST).date()
        if self.state.daily_date != today:
            logger.info("New trading day — resetting daily risk counters")
            self.state.reset_daily(today)

        week_start = self._get_week_start(today)
        if self.state.week_start != week_start:
            logger.info("New trading week — resetting weekly P&L")
            self.state.reset_weekly(week_start)

    # ── Core Risk Checks ─────────────────────────────────────

    def can_trade(self, market_context: Optional[dict] = None) -> Tuple[bool, str]:
        """
        Check all risk gates before allowing a new trade.
        Returns (allowed, reason).
        """
        self._ensure_resets()

        if self.state.is_circuit_breaker_active:
            return False, f"Circuit breaker: {self.state.breaker_reason}"

        # Daily loss limit
        daily_limit = self._initial_balance * (self.daily_loss_limit_pct / 100)
        if self.state.daily_pnl <= -daily_limit:
            self._activate_breaker(f"Daily loss limit hit: ₹{self.state.daily_pnl:.2f} (limit: -₹{daily_limit:.2f})")
            return False, self.state.breaker_reason

        # Absolute-rupee daily-loss floor (e2e Stage 3 / --max-loss-rs flag).
        # This is a hard floor independent of the percentage limit above. Used
        # by the e2e basket runs where the percentage limit on a Rs 1L config
        # is too lax for a small live experiment.
        if (self.absolute_daily_loss_floor_rs is not None
                and self.state.daily_pnl <= -self.absolute_daily_loss_floor_rs):
            self._activate_breaker(
                f"Absolute daily loss floor breached: "
                f"₹{self.state.daily_pnl:.2f} <= -₹{self.absolute_daily_loss_floor_rs:.2f}"
            )
            return False, self.state.breaker_reason

        # Weekly loss limit
        weekly_limit = self._initial_balance * (self.weekly_loss_limit_pct / 100)
        if self.state.weekly_pnl <= -weekly_limit:
            self._activate_breaker(f"Weekly loss limit hit: ₹{self.state.weekly_pnl:.2f}")
            return False, self.state.breaker_reason

        # Drawdown halt tier (hard stop)
        drawdown_pct = self._current_drawdown_pct()
        if drawdown_pct >= self.drawdown_halt_pct:
            self._activate_breaker(f"CRITICAL drawdown: {drawdown_pct:.1f}% — manual restart required")
            return False, self.state.breaker_reason

        # Max-drawdown soft ceiling. This was a documented risk guard but was
        # previously loaded into config yet never enforced — audit 2026-04-28.
        # We treat it as a warning-level halt: no new trades, but existing
        # positions are managed to exit (unlike the "halt" tier which is hard).
        if drawdown_pct >= self.max_drawdown_pct:
            return False, (
                f"Max drawdown limit breached: {drawdown_pct:.1f}% "
                f">= {self.max_drawdown_pct}% — no new entries until recovery"
            )

        # Consecutive losses
        if self.state.consecutive_losses >= self.max_consecutive_losses:
            return False, f"Consecutive losses: {self.state.consecutive_losses} (limit: {self.max_consecutive_losses})"

        # Max open positions
        if self.state.open_positions >= self.max_open_positions:
            return False, f"Max positions: {self.state.open_positions}/{self.max_open_positions}"

        # Max daily trades
        if self.state.daily_trades >= self.max_trades_per_day:
            return False, f"Max daily trades: {self.state.daily_trades}/{self.max_trades_per_day}"

        # Intraday time gate
        now = datetime.now(IST)
        exit_h, exit_m = map(int, self.intraday_exit_time.split(":"))
        if now.hour > exit_h or (now.hour == exit_h and now.minute >= exit_m):
            return False, f"Past intraday exit time ({self.intraday_exit_time})"

        # Market regime filters
        # 2026-05-15 LIVE-MODE SAFETY (P0 #5): defensively coerce values that
        # arrive from external feeds. If the upstream context has the key with
        # a None/NaN/string value, a raw comparison `None > self.max_vix`
        # raises TypeError, the trading cycle aborts, and after 5 consecutive
        # failures the daemon halts. Same pattern for nifty_trend (KeyError
        # safety + non-int values from cached snapshots).
        if market_context:
            vix_raw = market_context.get("india_vix", 0)
            try:
                vix = float(vix_raw) if vix_raw is not None else 0.0
            except (TypeError, ValueError):
                logger.warning(
                    f"can_trade: india_vix has non-numeric value {vix_raw!r}; "
                    "treating as 0 (regime filter bypass for this cycle)"
                )
                vix = 0.0
            if vix > self.max_vix:
                return False, f"India VIX too high: {vix:.1f} (max: {self.max_vix})"

            if self.require_nifty_above_200ema:
                trend_raw = market_context.get("nifty_trend", 1)
                try:
                    nifty_trend = int(trend_raw) if trend_raw is not None else 1
                except (TypeError, ValueError):
                    logger.warning(
                        f"can_trade: nifty_trend has non-numeric value {trend_raw!r}; "
                        "treating as 1 (neutral, allows entry)"
                    )
                    nifty_trend = 1
                if nifty_trend == -1:
                    return False, "Nifty below 200-day EMA — bearish regime"

        return True, "OK"

    def _activate_breaker(self, reason: str):
        self.state.is_circuit_breaker_active = True
        self.state.breaker_reason = reason
        logger.warning(f"CIRCUIT BREAKER: {reason}")

    def _current_drawdown_pct(self) -> float:
        if self.state.peak_balance <= 0:
            return 0.0
        return (self.state.peak_balance - self.state.current_balance) / self.state.peak_balance * 100

    # ── Position Sizing ──────────────────────────────────────

    def regime_size_multiplier(self, regime: Optional[str]) -> float:
        """Return the position-size multiplier configured for ``regime``.

        Defaults to 1.0 if regime is None or unmapped. Looked up from the
        ``risk.regime_size_multipliers`` config block. Empirical baseline
        (2026-05-13):
            bull_low_vol  1.20  -- expand in our strongest regime
            bull_high_vol 1.00
            sideways      0.85
            bear_low_vol  0.85
            bear_high_vol 0.70  -- shrink in our worst regime
            unknown       1.00  -- pre-first-refresh default

        These layer ON TOP of drawdown-tier reduction, the per-trade risk
        budget, and the max_position_size cap -- the regime multiplier
        affects ``risk_amount`` and ``max_position_value`` proportionally
        so all downstream gates fire correctly.
        """
        if not regime:
            return 1.0
        return self.regime_size_multipliers.get(regime, 1.0)

    def calculate_position_size(self, price: float, stop_loss_price: Optional[float] = None,
                                atr: Optional[float] = None, side: str = "BUY",
                                regime: Optional[str] = None) -> int:
        """
        Fixed-fractional position sizing (symmetric for long/short).
          risk_amount = balance * max_risk_per_trade_pct / 100
          shares = risk_amount / risk_per_share
        Applies drawdown tier reduction if active, then regime multiplier
        if a regime is provided.

        Args:
            regime: Optional market regime label (`bull_low_vol`,
                `bear_high_vol`, etc.). When provided, scales BOTH the
                risk budget and the max-position-value cap by
                `regime_size_multiplier(regime)`. Passing None preserves
                pre-2026-05-13 behaviour.
        """
        if price <= 0:
            return 0

        balance = self.state.current_balance
        risk_pct = self.max_risk_per_trade_pct

        # Drawdown tier: reduce risk if in warning zone
        dd = self._current_drawdown_pct()
        if dd >= self.drawdown_reduce_pct:
            risk_pct *= 0.5
            logger.info(f"Drawdown tier active ({dd:.1f}%): risk reduced to {risk_pct}%")

        # Regime-aware scaling. Multiply BOTH the risk budget and the
        # max-position-value cap so the regime knob affects "how much
        # capital we're willing to deploy" *and* "how much risk we're
        # willing to take", proportionally.
        regime_mult = self.regime_size_multiplier(regime)
        if regime_mult != 1.0:
            logger.info(
                f"[REGIME-SIZING] regime={regime} multiplier={regime_mult:.2f} "
                f"(risk {risk_pct:.2f}% -> {risk_pct * regime_mult:.2f}%)"
            )
            risk_pct *= regime_mult

        risk_amount = balance * (risk_pct / 100)
        max_position_value = balance * (self.max_position_size_pct / 100) * regime_mult
        max_shares_by_value = int(max_position_value / price)

        # ATR-based stop-loss if no explicit SL
        if stop_loss_price is None and atr is not None and atr > 0:
            if side == "BUY":
                stop_loss_price = price - self.atr_stop_multiplier * atr
            else:
                stop_loss_price = price + self.atr_stop_multiplier * atr

        risk_per_share = abs(price - stop_loss_price) if stop_loss_price is not None else 0
        if risk_per_share > 0:
            shares_by_risk = int(risk_amount / risk_per_share)
            return max(1, min(shares_by_risk, max_shares_by_value))

        return max(1, max_shares_by_value)

    # ── ATR-Based Stop-Loss ──────────────────────────────────

    def get_atr_stop_loss(self, entry_price: float, atr: float, side: str = "BUY") -> float:
        """Calculate ATR-based stop-loss: 1.5 x ATR(14) from entry."""
        distance = self.atr_stop_multiplier * atr
        if side == "BUY":
            return round(entry_price - distance, 2)
        return round(entry_price + distance, 2)

    def enforce_sl_floor(
        self,
        entry_price: float,
        proposed_sl: float,
        side: str = "BUY",
    ) -> float:
        """Widen ``proposed_sl`` outward if it sits inside ``min_stop_loss_pct``.

        This is the canonical floor enforcer. ``get_stop_loss`` uses it
        for its own ATR/percentage stops; callers that source the SL
        elsewhere (e.g. strategy-provided ``signal.stop_loss``) MUST also
        route through this helper before sizing the position.

        Why this exists as a separate method
        ------------------------------------
        Before 2026-05-13 the floor lived inline at the bottom of
        ``get_stop_loss``. A strategy that returned its own SL (e.g.
        supertrend_follow at price ± 3 × ATR) bypassed the floor entirely
        because trading_agent did ``signal.stop_loss or get_stop_loss()``.
        On 2026-05-13 09:37, HCLTECH was sold short at 1142.35 with a
        supertrend SL of ~1150.93 (0.75 %) -- inside the 1.2 % noise
        floor -- and was stopped out at 1150.40 for -Rs 148.24 within
        30 min. The floor-as-method fixes that class of bug.

        Returns the floored SL (always rounded to 2 dp).
        """
        if self.min_stop_loss_pct <= 0 or entry_price <= 0:
            return round(proposed_sl, 2)

        min_distance = entry_price * self.min_stop_loss_pct / 100
        if side == "BUY":
            floor_sl = entry_price - min_distance
            if proposed_sl > floor_sl:
                return round(floor_sl, 2)
        else:
            floor_sl = entry_price + min_distance
            if proposed_sl < floor_sl:
                return round(floor_sl, 2)
        return round(proposed_sl, 2)

    def get_stop_loss(self, entry_price: float, side: str = "BUY",
                      atr: Optional[float] = None) -> float:
        """Compute SL, enforcing a minimum distance floor when configured.

        The ATR- or %-based stop is first computed, then widened outward if
        it sits inside `min_stop_loss_pct` of entry. This protects against
        sub-1 % stops on quiet mid-caps that get knocked out by noise before
        the thesis has a chance to play out.
        """
        if atr is not None and atr > 0:
            sl = self.get_atr_stop_loss(entry_price, atr, side)
        else:
            if side == "BUY":
                sl = round(entry_price * (1 - self.default_stop_loss_pct / 100), 2)
            else:
                sl = round(entry_price * (1 + self.default_stop_loss_pct / 100), 2)

        return self.enforce_sl_floor(entry_price, sl, side)

    def get_take_profit(
        self,
        entry_price: float,
        side: str = "BUY",
        atr: Optional[float] = None,
        regime: Optional[str] = None,
        trend_continuation: bool = False,
    ) -> float:
        """
        Compute the take-profit level.

        Regime-aware reward multiplier (applied on top of ATR-based base):
            * bull_low_vol     -> 3.0 x ATR-stop (let winners run)
            * bull_high_vol    -> 2.5 x ATR-stop
            * sideways         -> 1.5 x ATR-stop (scalping)
            * bear_*           -> 2.0 x ATR-stop (default)
            * trend_continuation -> push to 4.0 x ATR-stop and rely on trailing.

        These are the *fixed* TP levels that trigger an exit. The trailing
        stop activates at 1:1 R:R independent of the TP, so even if the TP
        is wide, the trailing stop will lock in profit on a reversal.
        """
        if regime is None:
            tp_mult = 2.0
        elif trend_continuation:
            tp_mult = 4.0
        elif regime == "bull_low_vol":
            tp_mult = 3.0
        elif regime == "bull_high_vol":
            tp_mult = 2.5
        elif regime == "sideways":
            tp_mult = 1.5
        elif regime in ("bear_low_vol", "bear_high_vol"):
            tp_mult = 2.0
        else:
            tp_mult = 2.0

        if atr is not None and atr > 0:
            distance = tp_mult * self.atr_stop_multiplier * atr
            tp = round(entry_price + distance, 2) if side == "BUY" else round(entry_price - distance, 2)
        else:
            # Percentage fallback (also scaled)
            pct = self.default_take_profit_pct * (tp_mult / 2.0)
            if side == "BUY":
                tp = round(entry_price * (1 + pct / 100), 2)
            else:
                tp = round(entry_price * (1 - pct / 100), 2)

        return self.clamp_take_profit(entry_price, tp, side, atr=atr)

    def clamp_take_profit(
        self,
        entry_price: float,
        take_profit: float,
        side: str = "BUY",
        atr: Optional[float] = None,
    ) -> float:
        """Bound the TP so it stays reachable intraday.

        Caps applied (most restrictive wins):
          * max_tp_to_sl_multiple × SL distance from entry
          * max_tp_pct % of entry price

        Both caps are configurable; 0 disables each one. This never *widens*
        a TP — it only pulls an unrealistic TP back in. Trailing-stop logic
        still lets real winners run because TS activates on its own at
        1:1 R:R regardless of the fixed TP.
        """
        if entry_price <= 0:
            return take_profit

        # Compute the initial SL distance the same way get_stop_loss would
        # (needed to apply `max_tp_to_sl_multiple`).
        sl_distance: Optional[float] = None
        if atr is not None and atr > 0:
            sl_distance = self.atr_stop_multiplier * atr
        else:
            sl_distance = entry_price * self.default_stop_loss_pct / 100
        if self.min_stop_loss_pct > 0:
            sl_distance = max(sl_distance, entry_price * self.min_stop_loss_pct / 100)

        # Absolute TP distance candidates (all in rupees)
        tp_distance = abs(take_profit - entry_price)
        caps: List[float] = []
        if self.max_tp_to_sl_multiple > 0 and sl_distance and sl_distance > 0:
            caps.append(self.max_tp_to_sl_multiple * sl_distance)
        if self.max_tp_pct > 0:
            caps.append(entry_price * self.max_tp_pct / 100)

        if not caps:
            return take_profit

        capped_distance = min(tp_distance, min(caps))
        if capped_distance >= tp_distance:
            return take_profit  # already inside caps

        if side == "BUY":
            return round(entry_price + capped_distance, 2)
        return round(entry_price - capped_distance, 2)

    # ── Trailing Stops ───────────────────────────────────────

    def create_trailing_stop(self, symbol: str, entry_price: float,
                              initial_sl: float, side: str = "BUY") -> TrailingStop:
        ts = TrailingStop(
            breakeven_arm_rr=self.breakeven_arm_rr,
            breakeven_buffer_pct=self.breakeven_buffer_pct,
            breakeven_enabled=self.breakeven_enabled,
            entry_price=entry_price,
            initial_sl=initial_sl,
            side=side,
            peak_arm_rr=self.peak_arm_rr,
            peak_giveback_pct=self.peak_giveback_pct,
            peak_giveback_enabled=self.peak_giveback_enabled,
            trail_activation_rr=self.trailing_activation_rr,
            trail_step_pct=self.trailing_step_pct,
            # 2026-05-15 Plumb symbol into TrailingStop so the new
            # [BREAKEVEN-ARMED] / [PEAK-GIVEBACK-ARMED] logs are auditable.
            symbol=symbol,
        )
        self._trailing_stops[symbol] = ts
        return ts

    def update_trailing_stop(self, symbol: str, current_price: float) -> Optional[float]:
        ts = self._trailing_stops.get(symbol)
        if ts:
            return ts.update(current_price)
        return None

    def remove_trailing_stop(self, symbol: str):
        self._trailing_stops.pop(symbol, None)

    def get_trailing_stop(self, symbol: str) -> Optional[TrailingStop]:
        return self._trailing_stops.get(symbol)

    # ── SL/TP Trigger Check ──────────────────────────────────

    def check_stop_loss_take_profit(
        self, entry_price: float, current_price: float, side: str,
        stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
    ) -> Optional[str]:
        sl = stop_loss or self.get_stop_loss(entry_price, side)
        tp = take_profit or self.get_take_profit(entry_price, side)

        if side == "BUY":
            if current_price <= sl:
                return "stop_loss"
            if current_price >= tp:
                return "take_profit"
        else:
            if current_price >= sl:
                return "stop_loss"
            if current_price <= tp:
                return "take_profit"
        return None

    # ----- Expected-profit gate ----------------------------------------

    def min_rr_for(self, strategy: Optional[str]) -> float:
        """Return the required reward-to-risk multiple for a given strategy.

        Falls back to `default_min_rr` (1.2) when the strategy isn't listed.
        """
        if strategy and strategy in self.min_rr_by_strategy:
            return self.min_rr_by_strategy[strategy]
        return self.default_min_rr

    def is_trade_worth_taking(
        self,
        entry_price: float,
        take_profit: float,
        stop_loss: float,
        quantity: int,
        side: str = "BUY",
        product: str = "INTRADAY",
        strategy: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Reject setups where the expected reward doesn't cover trading charges by
        a safe margin.

        Rules:
            1. Reward must be >= `min_profit_to_charges_ratio` * round-trip charges.
            2. Reward must be >= `min_absolute_reward_rs` (no sub-Rs 15 trades).
            3. Risk-to-reward must be at least `min_rr_by_strategy[strategy]`
               (defaults to 1.2x when strategy is not listed).

        The per-strategy RR floor lets mean-reversion (high WR, modest RR)
        coexist with trend-following (lower WR, high RR) without a single
        flat threshold mis-sizing both.
        """
        if quantity <= 0 or entry_price <= 0:
            return False, "invalid_inputs"

        if side == "BUY":
            reward_per_share = max(0.0, take_profit - entry_price)
            risk_per_share = max(0.0, entry_price - stop_loss)
        else:
            reward_per_share = max(0.0, entry_price - take_profit)
            risk_per_share = max(0.0, stop_loss - entry_price)

        if reward_per_share <= 0 or risk_per_share <= 0:
            return False, "zero_reward_or_risk"

        reward_rs = reward_per_share * quantity
        if reward_rs < self.min_absolute_reward_rs:
            return False, f"reward_too_small(Rs{reward_rs:.2f}<{self.min_absolute_reward_rs:.0f})"

        # Estimate round-trip charges assuming the trade exits at TP
        try:
            charges = compute_round_trip(
                buy_price=min(entry_price, take_profit) if side == "BUY" else max(entry_price, take_profit),
                sell_price=max(entry_price, take_profit) if side == "BUY" else min(entry_price, take_profit),
                quantity=quantity,
                product=product,
            )
            total_charges = charges.total
        except Exception:
            total_charges = entry_price * quantity * 0.001  # 0.1% fallback

        if total_charges > 0 and reward_rs < self.min_profit_to_charges_ratio * total_charges:
            return False, (
                f"reward_vs_charges(Rs{reward_rs:.2f} < "
                f"{self.min_profit_to_charges_ratio}x Rs{total_charges:.2f})"
            )

        required_rr = self.min_rr_for(strategy)
        if reward_per_share < required_rr * risk_per_share:
            return False, (
                f"poor_rr({reward_per_share:.2f} < {required_rr:.2f}x risk "
                f"{risk_per_share:.2f}, strategy={strategy or 'default'})"
            )

        return True, "OK"

    def should_time_exit(self) -> bool:
        """Check if it's past the intraday exit time (3:15 PM by default)."""
        now = datetime.now(IST)
        exit_h, exit_m = map(int, self.intraday_exit_time.split(":"))
        return now.hour > exit_h or (now.hour == exit_h and now.minute >= exit_m)

    # ── Trade Recording ──────────────────────────────────────

    def record_trade(self, pnl: float):
        self._ensure_resets()
        self.state.daily_pnl += pnl
        self.state.weekly_pnl += pnl
        self.state.daily_trades += 1
        self.state.current_balance += pnl

        if pnl > 0:
            self.state.consecutive_losses = 0
        elif pnl < 0:
            self.state.consecutive_losses += 1
        # breakeven (pnl == 0) doesn't reset or increment

        self.state.recent_trade_results.append(pnl)

        if self.state.current_balance > self.state.peak_balance:
            self.state.peak_balance = self.state.current_balance

        logger.debug(
            f"Trade: PnL=₹{pnl:.2f} | Day=₹{self.state.daily_pnl:.2f} | "
            f"Balance=₹{self.state.current_balance:.2f} | "
            f"Consec losses={self.state.consecutive_losses}"
        )

    def update_open_positions(self, count: int):
        self.state.open_positions = count

    # ── Boot Rehydration ─────────────────────────────────────

    def rehydrate_daily_state(self, todays_trades: List[dict]) -> None:
        """
        Rebuild today's risk counters from already-persisted trades.

        Called once at boot so a daemon restart mid-session does NOT zero out
        daily_pnl/daily_trades/consecutive_losses. Without this, the EOD
        report on a restarted daemon shows "Day PnL: Rs +0.00" even when
        earlier daemons (same calendar day) closed real trades — a bug
        observed live on 2026-05-04 where 4 closed trades summing to Rs -2.25
        showed as Rs +0.00 because the final daemon (PID 3352) booted after
        all closes had landed.

        Why this is safe to call after __init__:
          - current_balance and peak_balance are already correct (resolved
            from the equity_curve snapshot which reflects post-trade cash).
            We DO NOT re-add pnl to current_balance — that would double-count.
          - daily_pnl/weekly_pnl/daily_trades are zero-initialised counters
            that ONLY track in-session activity, so replaying today's trades
            into them is the right thing to do.
          - consecutive_losses and recent_trade_results are also session
            counters; replaying preserves the streak the user actually has.
        """
        if not todays_trades:
            return

        for trade in todays_trades:
            pnl = float(trade.get("pnl") or 0.0)
            self.state.daily_pnl += pnl
            self.state.weekly_pnl += pnl
            self.state.daily_trades += 1

            if pnl > 0:
                self.state.consecutive_losses = 0
            elif pnl < 0:
                self.state.consecutive_losses += 1

            self.state.recent_trade_results.append(pnl)

        logger.info(
            f"[RISK-REHYDRATE] Replayed {len(todays_trades)} trade(s) from today: "
            f"daily_pnl=Rs {self.state.daily_pnl:+.2f}, "
            f"daily_trades={self.state.daily_trades}, "
            f"consec_losses={self.state.consecutive_losses}"
        )

    # ── Summary ──────────────────────────────────────────────

    def get_risk_summary(self) -> dict:
        dd = self._current_drawdown_pct()
        return {
            "current_balance": round(self.state.current_balance, 2),
            "peak_balance": round(self.state.peak_balance, 2),
            "daily_pnl": round(self.state.daily_pnl, 2),
            "weekly_pnl": round(self.state.weekly_pnl, 2),
            "daily_trades": self.state.daily_trades,
            "drawdown_pct": round(dd, 2),
            "consecutive_losses": self.state.consecutive_losses,
            "open_positions": self.state.open_positions,
            "circuit_breaker": self.state.is_circuit_breaker_active,
            "breaker_reason": self.state.breaker_reason,
            "drawdown_tier": "HALT" if dd >= self.drawdown_halt_pct else ("REDUCED" if dd >= self.drawdown_reduce_pct else "NORMAL"),
            "trailing_stops_active": len([ts for ts in self._trailing_stops.values() if ts.trailing_active]),
        }
