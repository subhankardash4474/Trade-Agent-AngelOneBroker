"""
Market Safety Checks

Pre-trade guardrails specific to Indian equity markets:

  - **Circuit-limit detection**: NSE applies daily price bands of 2%, 5%, 10%,
    or 20% per stock. Orders placed at upper/lower circuit will be rejected
    (or stuck pending), and "frozen" stocks typically can't be exited
    intraday. Avoid entering stocks that have already moved close to their
    daily band — the upside is capped and the downside is a one-way trap.

  - **Data quality checks**: reject OHLCV dataframes with NaNs, zero volume,
    stale timestamps, or implausible price spikes (likely a corporate
    action / data glitch).

  - **Price sanity**: reject LTPs that are too far from the last candle close
    (stale quote or bad tick).

All checks are pure functions — easy to unit-test, no side effects.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")


# ──────────────────────────────────────────────────────────────────────
# Circuit-limit detection
# ──────────────────────────────────────────────────────────────────────

# Thresholds at which we get uncomfortable placing a new BUY. We stay well
# inside the actual circuit band (which might be 5/10/20%) because near-circuit
# stocks often freeze or gap aggressively.
CIRCUIT_PROXIMITY_PCT = 8.0   # >8% intraday move → circuit-hit risk
CIRCUIT_HARD_LIMIT_PCT = 19.0  # >19% → almost certainly at circuit


def check_circuit_risk(
    current_price: float,
    previous_close: Optional[float],
    day_high: Optional[float] = None,
    day_low: Optional[float] = None,
) -> Tuple[bool, str]:
    """
    Returns (is_safe, reason). `is_safe=False` means don't enter.

    Computes intraday % move vs previous close and checks the day's range.
    A stock that has already run +9% today is either at upper circuit or
    near it — adding exposure here is asymmetric: limited upside, full
    downside if trend reverses.
    """
    if not current_price or current_price <= 0:
        return False, "invalid_current_price"

    if not previous_close or previous_close <= 0:
        return True, "no_prev_close_available"  # can't evaluate — let caller decide

    day_change_pct = (current_price - previous_close) / previous_close * 100

    if abs(day_change_pct) >= CIRCUIT_HARD_LIMIT_PCT:
        return False, f"hard_circuit_band_hit ({day_change_pct:+.1f}% vs prev close)"

    if abs(day_change_pct) >= CIRCUIT_PROXIMITY_PCT:
        return False, f"near_circuit_band ({day_change_pct:+.1f}% vs prev close)"

    # Day-range proximity: if the current price is at the day's high AND the
    # day's range is already >7%, upside is exhausted.
    if day_high is not None and day_low is not None and day_low > 0:
        day_range_pct = (day_high - day_low) / day_low * 100
        at_high = abs(current_price - day_high) / day_high * 100 < 0.1
        if day_range_pct >= 7.0 and at_high:
            return False, f"at_day_high after {day_range_pct:.1f}% range (exhausted)"

    return True, "ok"


# ──────────────────────────────────────────────────────────────────────
# OHLCV quality checks
# ──────────────────────────────────────────────────────────────────────


def check_data_quality(
    df: pd.DataFrame,
    max_staleness_minutes: int = 30,
    min_volume: float = 0.0,
) -> Tuple[bool, str]:
    """
    Returns (is_safe, reason). `is_safe=False` means skip this symbol.

    Validates:
      - DataFrame is non-empty with the required OHLCV columns.
      - No NaNs in the last 3 rows (slight tolerance for legitimate gaps earlier).
      - Last candle timestamp is fresh.
      - Last candle volume is above `min_volume` (dead stock check).
      - No suspicious single-bar price spikes (>20% move — likely a split or
        bad tick that would wreck indicators).
    """
    if df is None or df.empty:
        return False, "empty_dataframe"

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        return False, f"missing_columns:{missing}"

    if len(df) < 3:
        return False, "too_few_bars"

    # Last 3 rows shouldn't have NaNs in price fields
    recent = df.iloc[-3:]
    if recent[["open", "high", "low", "close"]].isna().any().any():
        return False, "nan_in_recent_ohlc"

    last = df.iloc[-1]

    # Volume sanity
    if float(last["volume"]) < min_volume:
        return False, f"zero_or_low_volume ({last['volume']})"

    # Staleness: last bar should be within max_staleness_minutes of "now".
    # yfinance returns UTC-naive timestamps (epoch seconds in UTC), so a tz-naive
    # index is treated as UTC — NOT as IST. Before the fix, localizing as IST
    # inflated every bar's age by 5h30m (the UTC-IST offset) and silently
    # killed signal generation via the "stale_data" gate.
    try:
        last_ts = df.index[-1]
        if hasattr(last_ts, "to_pydatetime"):
            last_ts = last_ts.to_pydatetime()
        if isinstance(last_ts, datetime):
            if last_ts.tzinfo is None:
                last_ts = pytz.utc.localize(last_ts)
            age = datetime.now(IST) - last_ts
            # During market hours we expect freshness; allow >24h for after-hours.
            if age > timedelta(minutes=max_staleness_minutes) and age < timedelta(hours=12):
                return False, f"stale_data (age={age})"
    except Exception:
        pass  # index isn't a datetime — skip staleness check

    # Price spike detection: last close vs prev close > 20% is almost certainly
    # a corporate action (split/bonus) or a bad tick. Either way, indicators
    # computed on unadjusted data will be wildly wrong.
    if len(df) >= 2:
        try:
            last_close = float(last["close"])
            prev_close = float(df.iloc[-2]["close"])
            if prev_close > 0:
                move = abs(last_close - prev_close) / prev_close * 100
                if move > 20.0:
                    return False, f"price_spike ({move:.1f}% bar-over-bar — possible split)"
        except Exception:
            pass

    return True, "ok"


# ──────────────────────────────────────────────────────────────────────
# Sector exposure limits
# ──────────────────────────────────────────────────────────────────────


# Lightweight sector map for common NSE tickers. Not exhaustive, but covers
# the liquid names the scanner typically picks. "UNKNOWN" maps to a default
# bucket so we still apply a limit even for un-classified symbols.
NSE_SECTOR_MAP: Dict[str, str] = {
    # Banks / Financial
    "HDFCBANK": "Banks", "ICICIBANK": "Banks", "SBIN": "Banks",
    "AXISBANK": "Banks", "KOTAKBANK": "Banks", "INDUSINDBK": "Banks",
    "PNB": "Banks", "BANKBARODA": "Banks", "CANBK": "Banks",
    "FEDERALBNK": "Banks", "IDFCFIRSTB": "Banks", "CUB": "Banks",
    "BAJFINANCE": "NBFC", "BAJAJFINSV": "NBFC", "HDFCLIFE": "Insurance",
    "SBILIFE": "Insurance", "ICICIGI": "Insurance", "ICICIPRULI": "Insurance",
    "SHRIRAMFIN": "NBFC", "CHOLAFIN": "NBFC", "MUTHOOTFIN": "NBFC",
    "LICHSGFIN": "NBFC", "HUDCO": "NBFC", "PFC": "NBFC", "RECLTD": "NBFC",
    "NAM-INDIA": "AMC",
    # Energy / Power / Oil
    "RELIANCE": "Energy", "ONGC": "Energy", "IOC": "Energy", "BPCL": "Energy",
    "HPCL": "Energy", "GAIL": "Energy", "ADANIENSOL": "Power", "ATGL": "Energy",
    "TATAPOWER": "Power", "NTPC": "Power", "POWERGRID": "Power",
    "TORNTPOWER": "Power", "NHPC": "Power", "JSWENERGY": "Power",
    "ADANIPOWER": "Power", "ADANIGREEN": "Power",
    # IT
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "TECHM": "IT", "LTIM": "IT", "MPHASIS": "IT", "COFORGE": "IT",
    "PERSISTENT": "IT", "LTTS": "IT",
    # Pharma
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "DIVISLAB": "Pharma", "LUPIN": "Pharma", "AUROPHARMA": "Pharma",
    "BIOCON": "Pharma", "PPLPHARMA": "Pharma", "ALKEM": "Pharma",
    "TORNTPHARM": "Pharma", "ZYDUSLIFE": "Pharma",
    # Auto
    "MARUTI": "Auto", "M&M": "Auto", "TATAMOTORS": "Auto",
    "BAJAJ-AUTO": "Auto", "EICHERMOT": "Auto", "HEROMOTOCO": "Auto",
    "TVSMOTOR": "Auto", "ASHOKLEY": "Auto", "MRF": "Auto",
    # Metals / Cement
    "TATASTEEL": "Metals", "JSWSTEEL": "Metals", "HINDALCO": "Metals",
    "VEDL": "Metals", "SAIL": "Metals", "COALINDIA": "Metals",
    "NMDC": "Metals", "JINDALSTEL": "Metals",
    "ULTRACEMCO": "Cement", "SHREECEM": "Cement", "GRASIM": "Cement",
    "AMBUJACEM": "Cement", "ACC": "Cement", "INDIACEM": "Cement",
    # FMCG
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "DABUR": "FMCG", "BRITANNIA": "FMCG", "MARICO": "FMCG",
    "GODREJCP": "FMCG", "VBL": "FMCG", "UBL": "FMCG",
    # Consumer / Retail
    "TITAN": "Consumer", "TRENT": "Consumer", "DMART": "Consumer",
    "AVENUE": "Consumer",
    # Infra / Construction
    "LT": "Infra", "ADANIPORTS": "Infra", "GMRINFRA": "Infra",
    "IRB": "Infra", "HCC": "Infra", "NBCC": "Infra",
    # Chemicals
    "AARTIIND": "Chemicals", "PIDILITIND": "Chemicals", "SRF": "Chemicals",
    "DEEPAKNTR": "Chemicals", "COHANCE": "Chemicals",
    # Shipping / Defence
    "COCHINSHIP": "Defence", "HAL": "Defence", "BEL": "Defence",
    "BEML": "Defence", "MAZDOCK": "Defence",
    # Misc
    "PINELABS": "FinTech", "PAYTM": "FinTech",
    "ELECON": "Capital Goods", "ABB": "Capital Goods",
    "SIEMENS": "Capital Goods", "HSCL": "Capital Goods",
    "HAVELLS": "Capital Goods", "CUMMINSIND": "Capital Goods",
    "TARIL": "Capital Goods", "ONESOURCE": "Specialty",
    "ARE&M": "Capital Goods", "AXISTECHE": "Specialty", "GMDCLTD": "Mining",
    # ── 2026-04-30 expansion: mid-caps the scanner frequently picks ──
    # Public-sector and mid-cap banks
    "UNIONBANK": "Banks", "INDIANB": "Banks", "BANDHANBNK": "Banks",
    "RBLBANK": "Banks", "YESBANK": "Banks", "IDBI": "Banks",
    "IOB": "Banks", "UCOBANK": "Banks", "BANKINDIA": "Banks",
    "CENTRALBK": "Banks", "MAHABANK": "Banks", "PSB": "Banks",
    "AUBANK": "Banks", "KARURVYSYA": "Banks", "SOUTHBANK": "Banks",
    "J&KBANK": "Banks", "DCBBANK": "Banks",
    # Power / renewables / utilities
    "RPOWER": "Power", "SUZLON": "Power", "NLCINDIA": "Power",
    "INOXWIND": "Power", "IEX": "Power", "SJVN": "Power",
    "KPIGREEN": "Power", "ORIENTGREEN": "Power", "RTNINDIA": "Power",
    "BHEL": "Power", "RAYMOND": "Power",
    # Telecom
    "TTML": "Telecom", "IDEA": "Telecom", "BHARTIARTL": "Telecom",
    "MTNL": "Telecom", "TATACOMM": "Telecom",
    # Chemicals / specialty
    "TATACHEM": "Chemicals", "GNFC": "Chemicals", "RCF": "Chemicals",
    "FACT": "Chemicals", "CHAMBLFERT": "Chemicals", "UPL": "Chemicals",
    "BALRAMCHIN": "Sugar", "DHAMPURSUG": "Sugar", "BAJAJHIND": "Sugar",
    # Consumer durables / retail / discretionary
    "CROMPTON": "Consumer Durables", "VOLTAS": "Consumer Durables",
    "WHIRLPOOL": "Consumer Durables", "BLUESTARCO": "Consumer Durables",
    "ORIENTELEC": "Consumer Durables", "SYMPHONY": "Consumer Durables",
    "DELTACORP": "Consumer", "JUBLFOOD": "Consumer",
    "DEVYANI": "Consumer", "SAPPHIRE": "Consumer", "WESTLIFE": "Consumer",
    "LUXIND": "Consumer", "RELAXO": "Consumer", "BATAINDIA": "Consumer",
    "PAGEIND": "Consumer", "ABFRL": "Consumer", "FASHION": "Consumer",
    "VEDANTFASH": "Consumer", "CHOICEIN": "Consumer",
    # Metals / mining / materials
    "HINDCOPPER": "Metals", "MOIL": "Metals", "NATIONALUM": "Metals",
    "JSL": "Metals", "WELCORP": "Metals", "APLAPOLLO": "Metals",
    "JINDWORLD": "Metals", "JPPOWER": "Metals",
    # Realty / construction
    "DLF": "Realty", "GODREJPROP": "Realty", "OBEROIRLTY": "Realty",
    "PRESTIGE": "Realty", "SOBHA": "Realty", "LODHA": "Realty",
    "MAHLIFE": "Realty", "BRIGADE": "Realty", "PHOENIXLTD": "Realty",
    "IRCON": "Infra", "RVNL": "Infra", "RITES": "Infra",
    "NCC": "Infra", "KEC": "Infra", "PNCINFRA": "Infra",
    "PFC": "NBFC", "IREDA": "NBFC", "IRFC": "NBFC",
    # Media / entertainment
    "ZEEL": "Media", "SUNTV": "Media", "PVR": "Media", "INOXLEISUR": "Media",
    "DISHTV": "Media", "TV18BRDCST": "Media", "SAREGAMA": "Media",
    # IT mid-caps
    "KPITTECH": "IT", "BIRLASOFT": "IT", "ZENSARTECH": "IT",
    "HEXAWARE": "IT", "RATEGAIN": "IT", "CYIENT": "IT", "TATAELXSI": "IT",
    "INTELLECT": "IT", "NEWGEN": "IT", "MASTEK": "IT", "NAZARA": "IT",
    "POLICYBZR": "IT", "NYKAA": "IT", "EASEMYTRIP": "IT",
    "MAPMYINDIA": "IT", "ZOMATO": "IT", "IRCTC": "IT",
    # Pharma mid-caps
    "NATCOPHARM": "Pharma", "GLAND": "Pharma", "IPCALAB": "Pharma",
    "ABBOTINDIA": "Pharma", "SANOFI": "Pharma", "PFIZER": "Pharma",
    "GSK": "Pharma", "GLENMARK": "Pharma", "GRANULES": "Pharma",
    "LAURUSLABS": "Pharma", "AJANTPHARM": "Pharma", "PIRAMALPHL": "Pharma",
    "MANKIND": "Pharma", "CAPLIPOINT": "Pharma",
    "JBCHEPHARM": "Pharma", "SYNGENE": "Pharma",
    # Auto ancillary / tyres
    "BOSCHLTD": "Auto", "MOTHERSON": "Auto", "EXIDEIND": "Auto",
    "AMARARAJA": "Auto", "MINDAIND": "Auto", "SUNDRMFAST": "Auto",
    "BALKRISIND": "Auto", "CEAT": "Auto", "APOLLOTYRE": "Auto",
    "JKTYRE": "Auto", "ESCORTS": "Auto",
    # Capital goods / industrials extras
    "GRINDWELL": "Capital Goods", "TIMKEN": "Capital Goods",
    "THERMAX": "Capital Goods", "AIAENG": "Capital Goods",
    "BHARATFORG": "Capital Goods", "SKFINDIA": "Capital Goods",
    "ABBPOWER": "Capital Goods", "KIRLOSBROS": "Capital Goods",
    # Logistics / shipping
    "BLUEDART": "Logistics", "MAHLOG": "Logistics",
    "CONCOR": "Logistics", "TCI": "Logistics", "GATI": "Logistics",
    "ADANIWILMAR": "FMCG", "SWIGGY": "Consumer",
    # Hospitality / travel
    "INDHOTEL": "Hospitality", "CHALET": "Hospitality",
    "LEMONTREE": "Hospitality", "SPICEJET": "Aviation",
    "INDIGO": "Aviation", "JETAIRWAYS": "Aviation",
    # Energy / oil marketing mid-caps
    "MGL": "Energy", "IGL": "Energy", "GSPL": "Energy",
    "PETRONET": "Energy", "GUJGASLTD": "Energy", "OIL": "Energy",
    "HINDPETRO": "Energy",
    # Defence / aerospace mid-caps
    "BDL": "Defence", "GRSE": "Defence", "MIDHANI": "Defence",
    "ASTRAMICRO": "Defence", "SOLARINDS": "Defence",
    # Agri / fertilizers
    "COROMANDEL": "Agri", "GNFC": "Agri", "DEEPAKFERT": "Agri",
    "BAYERCROP": "Agri", "DHANUKA": "Agri", "PIINDIA": "Agri",
    "RALLIS": "Agri",
    # Diversified
    "ADANIENT": "Conglomerate", "TATAINVEST": "Conglomerate",
    "BAJAJHLDNG": "Conglomerate", "GODREJIND": "Conglomerate",
    # ── 2026-05-15 expansion: symbols that traded unmapped on 2026-05-14 ──
    # Live evidence (2026-05-14): TATACAP (NBFC) + 3 other financial shorts
    # totalled 41.3% of equity but the supersector cap saw only 31.1% because
    # TATACAP fell into UNKNOWN. Adding the gap-closers below; classifications
    # reflect underlying business + how the 200-EMA / rate-cycle factor moves
    # them, not necessarily NSE's industry tag.
    "TATACAP": "NBFC",          # Tata Capital — diversified NBFC parent
    "BSOFT": "IT",              # Birlasoft (current NSE symbol; legacy BIRLASOFT kept above)
    "AEGISLOG": "Logistics",    # Aegis Logistics — oil & gas distribution
    "KFINTECH": "FinTech",      # KFin Technologies — RTA / fund accounting
    "PCBL": "Chemicals",        # Phillips Carbon Black
    # Common financial-services scanner picks (proactive coverage)
    "ABCAPITAL": "NBFC",        # Aditya Birla Capital — NBFC/insurance holdco
    "M&MFIN": "NBFC", "POONAWALLA": "NBFC",
    "IIFL": "NBFC", "MFSL": "NBFC", "SBICARD": "NBFC",
    "360ONE": "NBFC", "EDELWEISS": "NBFC", "MANAPPURAM": "NBFC",
    "PEL": "NBFC",              # Piramal Enterprises
    # Capital markets / depository / brokers — FinTech bucket so they roll
    # up to Financials via supersector (correlated to broader market vol)
    "CDSL": "FinTech", "BSE": "FinTech", "MCX": "FinTech",
    "ANGELONE": "FinTech", "CAMS": "FinTech", "IEXLTD": "FinTech",
    # Shipping (its own concentration risk — global trade cycle)
    "GESHIP": "Shipping", "SCI": "Shipping",
    # Specialty chemicals / industrial intermediates scanner often picks
    "CLEAN": "Chemicals", "FLUOROCHEM": "Chemicals", "NAVINFLUOR": "Chemicals",
    "PIIND": "Chemicals", "ATUL": "Chemicals", "LINDEINDIA": "Chemicals",
    # Industrial electrodes / graphite / refractories
    "GRAPHITE": "Capital Goods", "HEG": "Capital Goods",
    "CARBORUNIV": "Capital Goods",
}


def get_sector(symbol: str) -> str:
    """Return the sector bucket for an NSE symbol, or 'UNKNOWN'."""
    return NSE_SECTOR_MAP.get(symbol.upper(), "UNKNOWN")


# Supersector roll-up (2026-05-14). Fine-grained sectors that share a
# single underlying risk factor are collapsed into one bucket for
# concentration math. Live evidence (2026-05-14): a SHORT book held
# CENTRALBK (Banks) + FEDERALBNK (Banks) + TATACAP (NBFC) + CHOLAFIN (NBFC)
# simultaneously -- effectively 67% of the book in a single rate-cycle
# factor. The flat per-sector 40% cap allowed it because each sub-bucket
# (Banks, NBFC) was independently under 40%.
#
# When a sector isn't listed here, it stays in its own bucket (no roll-up).
# Override at the config level via `risk.use_supersectors: false` to
# disable entirely and revert to the legacy fine-grained behaviour.
SUPERSECTOR_MAP: Dict[str, str] = {
    "Banks": "Financials",
    "NBFC": "Financials",
    "Insurance": "Financials",
    "AMC": "Financials",
    "FinTech": "Financials",
    "Energy": "EnergyPower",
    "Power": "EnergyPower",
    "Metals": "MetalsMining",
    "Mining": "MetalsMining",
    "Cement": "MetalsMining",
    "Auto": "AutoAuxiliary",
    # Note: cement is grouped with metals because both are commodity-cycle
    # plays driven by infra capex and global steel/iron-ore prices.
}


def get_supersector(symbol: str) -> str:
    """Coarser bucket: collapses Banks/NBFC/Insurance/AMC/FinTech into
    ``Financials`` etc. Falls back to the fine-grained sector when no
    roll-up exists, so behaviour is strictly broader-or-same."""
    sector = get_sector(symbol)
    return SUPERSECTOR_MAP.get(sector, sector)


def _bucket_for(
    symbol: str,
    unknown_per_symbol: bool,
    use_supersectors: bool = False,
) -> str:
    """Sector bucket used for concentration math.

    When `unknown_per_symbol=True`, unmapped symbols get a private bucket
    (``UNKNOWN:<SYMBOL>``) so they don't artificially combine with other
    unmapped names. This prevents one open position in an unclassified
    mid-cap from blocking every other unclassified mid-cap — a real
    2026-04-30 pathology where 28 distinct signals were rejected against
    a single ``UNKNOWN`` bucket.

    When ``use_supersectors=True`` (default once wired through config),
    fine sectors are rolled up via ``SUPERSECTOR_MAP`` so e.g. a Banks +
    NBFC + Insurance triple is correctly recognised as one Financials
    concentration. UNKNOWN handling is unchanged.
    """
    sector = get_supersector(symbol) if use_supersectors else get_sector(symbol)
    if sector == "UNKNOWN" and unknown_per_symbol:
        return f"UNKNOWN:{symbol.upper()}"
    return sector


def check_sector_exposure(
    symbol: str,
    current_positions_by_symbol: Dict[str, float],
    additional_cost: float,
    total_equity: float,
    max_sector_exposure_pct: float = 40.0,
    unknown_per_symbol: bool = False,
    use_supersectors: bool = False,
) -> Tuple[bool, str]:
    """
    Returns (is_safe, reason). Rejects a new position if it would push the
    sector's share of portfolio equity above `max_sector_exposure_pct`.

    Args:
        symbol: New symbol being opened.
        current_positions_by_symbol: {existing_symbol: notional_value}.
        additional_cost: Notional value of the proposed new position.
        total_equity: Current total portfolio equity.
        max_sector_exposure_pct: Hard cap per sector (0-100).
        unknown_per_symbol: When True, unclassified symbols each get their
            own bucket (recommended — prevents the "UNKNOWN" logjam).
        use_supersectors: When True, sectors are rolled up via
            SUPERSECTOR_MAP so e.g. Banks/NBFC/Insurance share a single
            Financials cap. Default off for backward compatibility; the
            agent overlay flips it on at the config level.
    """
    # P1 #6 (2026-05-17) — LIVE-MODE SAFETY: fail-closed when equity is
    # non-positive. The old code returned `(True, "no_equity_check")` here,
    # i.e. the sector cap was BYPASSED precisely in the MTM-distorted regime
    # where concentration matters most (e.g. a transient quote spike that
    # makes unrealised P&L look like a wipe-out). Without this gate, a
    # cascade of new entries can fire during the worst part of the day.
    #
    # Fallback denominator: gross notional of existing positions plus the
    # additional cost. When that is also zero we have nothing meaningful to
    # measure against — refuse the trade explicitly. This is much rarer in
    # practice than the equity-glitch case so the strict failure mode is OK.
    bucket = _bucket_for(symbol, unknown_per_symbol, use_supersectors)
    existing_sector_value = sum(
        v for s, v in current_positions_by_symbol.items()
        if _bucket_for(s, unknown_per_symbol, use_supersectors) == bucket
    )
    new_sector_value = existing_sector_value + additional_cost
    display_bucket = bucket.replace("UNKNOWN:", "UNKNOWN/")

    if total_equity <= 0:
        gross_exposure = (
            sum(current_positions_by_symbol.values()) + additional_cost
        )
        if gross_exposure <= 0:
            return False, (
                "sector_concentration: equity<=0 and gross_exposure<=0; "
                "refusing new entry until portfolio state is meaningful"
            )
        sector_pct_fallback = new_sector_value / gross_exposure * 100
        if sector_pct_fallback > max_sector_exposure_pct:
            return False, (
                f"sector_concentration_fallback: {display_bucket} would be "
                f"{sector_pct_fallback:.1f}% of GROSS (equity\u22640; "
                f"cap {max_sector_exposure_pct}%, "
                f"existing Rs {existing_sector_value:,.0f})"
            )
        return True, (
            f"ok_fallback (sector {display_bucket} @ "
            f"{sector_pct_fallback:.1f}% of gross; equity<=0)"
        )

    sector_pct = new_sector_value / total_equity * 100

    if sector_pct > max_sector_exposure_pct:
        return False, (
            f"sector_concentration: {display_bucket} would be {sector_pct:.1f}% "
            f"(cap {max_sector_exposure_pct}%, existing Rs {existing_sector_value:,.0f})"
        )
    return True, f"ok (sector {display_bucket} @ {sector_pct:.1f}%)"
