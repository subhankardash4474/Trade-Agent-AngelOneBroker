# Strategy Research Backlog

A curated, opinionated reading list for **next-strategy candidates**. Single
source of truth so ideas surfaced in chat or code reviews don't decay into
forgotten threads. Each entry follows the same structure: **claim → evidence →
implementation cost → fit with our framework**.

> **Discipline rule** before adding any item: a strategy/feature only gets
> implemented after (a) a hypothesis is articulated in plain English, (b)
> there is at least one out-of-sample reference (academic paper or repo with
> public stats), and (c) it fits our existing `strategies/*` interface (or
> we explicitly accept the cost of a new abstraction). No retail-blog
> "I made 50% with this Pine Script" content -- those are marketing, not edge.

---

## 1. Academic / peer-reviewed sources (highest signal)

These have multi-decade out-of-sample evidence. Most won't transplant verbatim
to NSE 5m intraday, but the *components* (factors, regime detectors, sizing
heuristics) absolutely transfer.

### 1.1 Jegadeesh & Titman 1993 -- Cross-Sectional Momentum
- **Claim**: Stocks that outperform over the past 3-12 months continue to
  outperform over the next 3-12 months. Effect is robust across markets,
  decades, and cap tiers.
- **Implementation cost (us)**: ~1 day. We already have momentum-style
  features (RSI, MACD); cross-sectional ranking + long/short top-vs-bottom
  decile fits cleanly into the ensemble vote.
- **Fit**: Daily/weekly horizon, NOT 5m intraday. Useful as a *regime gate*
  (only buy intraday momentum signals when the stock is also in the top
  cross-sectional momentum decile).
- **Caveats**: Original paper used monthly data. Indian-market replications
  (e.g., Sehgal & Balakrishnan 2002) confirm the effect on NSE.

### 1.2 Asness, Moskowitz & Pedersen 2013 -- "Value & Momentum Everywhere"
- **Claim**: Combining value and momentum across asset classes produces
  Sharpe ~1.0+ with low correlation to either alone. The two factors are
  *negatively* correlated, so combining them is genuinely a free-lunch
  diversification.
- **Implementation cost**: Medium. Value features (P/E, P/B, EV/EBITDA) need
  fundamental data which we don't currently fetch. NSE has free corporate
  filings via screener.in / Tijori; AngelOne SmartAPI exposes fundamentals
  as a separate endpoint.
- **Fit**: Adds a **portfolio-level** filter that we currently lack. Today
  the ensemble votes per-symbol per-bar; this would tilt position-sizing
  toward stocks that are *both* technically attractive (intraday signal)
  and structurally cheap.

### 1.3 Avellaneda & Lee 2010 -- "Statistical Arbitrage in the US Equities Market"
- **Claim**: Cointegrated pair residuals mean-revert with predictable
  half-lives. Provides explicit z-score entry/exit thresholds and Bayesian
  half-life estimation.
- **Implementation cost**: ~2 days. Engle-Granger cointegration test is
  ~30 lines of statsmodels; pair-selection scan over NIFTY-50 is the bulk
  of the work; the trading logic itself maps cleanly to our existing
  `BaseStrategy` interface (just produces signals on a *spread*, not a
  single symbol).
- **Fit**: **Market-neutral**, so doesn't fight our directional momentum
  strategies for capital. This is the **first new-strategy candidate** I
  would queue after v2 results land. Indian banks/PSUs have notoriously
  stable cointegration (HDFCBANK/ICICIBANK, RELIANCE/ONGC, SBIN/PNB).
- **Risk**: cointegration relationships break during regime shifts (e.g.,
  RBI rate-cut surprises). Need rolling re-test + circuit breaker.

### 1.4 Lo 2002 -- "The Statistics of Sharpe Ratios" + Adaptive Markets
- **Claim**: Sharpe ratio estimation has wide confidence intervals on small
  samples; many "edges" are statistical illusions until ~5 years of data.
- **Implementation cost**: 0 (we just need to internalize this).
- **Fit**: Validation discipline. This is the academic backing for the
  walk-forward design we just shipped (`--train-window-days` / 
  `--holdout-window-days` flags in `battery.py`).

### 1.5 Sehgal & Balakrishnan 2002 -- Indian-market momentum replication
- **Claim**: Momentum effect (Jegadeesh-Titman) holds on Indian equities
  with comparable magnitude (~1% / month).
- **Implementation cost**: 0 (just confirms the effect exists on NSE).
- **Fit**: Citation when arguing "yes, momentum works in India too."

---

## 2. Open-source frameworks (real backtests, not blog claims)

Use these as **idea sources and reference implementations**, not as
drop-in replacements for our framework.

### 2.1 [microsoft/qlib](https://github.com/microsoft/qlib)
- **What**: Microsoft Research's open quant platform. Production-quality
  factor-modeling library with reference Alpha158/Alpha360 factor sets.
- **What to mine**: The factor library (`qlib/contrib/data/dataset.py`).
  Many alphas there (price momentum, volume momentum, microstructure
  imbalance) are directly portable as new features for our XGBoost
  classifier.
- **Cost**: ~1 day to lift 5-10 promising alphas into our `core/features.py`.
- **Fit**: Pure feature additions, no architectural change.

### 2.2 [lean-engine/Lean](https://github.com/QuantConnect/Lean)
- **What**: QuantConnect's C# backtest engine + their Algorithm Lab.
- **What to mine**: The community algorithm library has thousands of
  user-submitted strategies with public performance stats. Use as
  *idea source*, not code source (it's C#).
- **Cost**: 30 min to skim top-10 algorithms by Sharpe and copy their
  hypothesis to this doc.

### 2.3 [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade)
- **What**: Python crypto trading bot. Crypto-focused, but the strategy
  patterns (entry filters, exit logic, hyperopt) transfer.
- **What to mine**: Their `populate_indicators` / `populate_entry_trend` /
  `populate_exit_trend` pattern is cleaner than our current per-strategy
  generate_signal() approach.
- **Cost**: 0 -- not a copy candidate, just a pattern reference.

### 2.4 [stefan-jansen/machine-learning-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading)
- **What**: Companion code for "Machine Learning for Algorithmic Trading"
  (2nd ed). Notebook-style, dense.
- **What to mine**: Chapter 4 (alpha factor research workflow), Chapter 6
  (LightGBM for return prediction -- we use XGBoost; LightGBM is often
  faster + better-calibrated on this kind of tabular data), Chapter 19
  (deep learning sequence models -- probably premature for us).
- **Cost**: ~2 hours to read Chapter 4 + lift the alpha-factor evaluation
  protocol.

---

## 3. NSE-specific edges (the asymmetric opportunity)

**These are where the asymmetric opportunity lives.** Most online quant
content is US-centric -- the same strategies exist on NSE but with less
crowding, weaker liquidity in mid-caps, and a few uniquely-Indian data
streams that retail almost never uses.

### 3.1 FII / DII daily flow data (the headline NSE edge)
- **Claim**: Daily FII (Foreign Institutional Investor) and DII (Domestic
  Institutional Investor) cash-market activity is **strongly predictive
  of next-day Nifty constituent direction**. FII outflow + DII outflow on
  the same day is a near-deterministic next-day-down signal historically.
- **Source**: NSE publishes this free at
  https://www.nseindia.com/reports/fii-dii (also via NSEpy library).
- **Implementation cost**: ~1 day. Daily fetcher + 3-5 derived features
  (5d-EMA flow, sign-flip detection, sector skew) added to XGBoost
  feature set.
- **Why it's underused**: Retail tools focus on price-action; institutional
  tools have it but won't share. Genuinely uncommon in open-source quant
  code.
- **Caveat**: Effect strongest at INDEX level; weaker for individual
  mid-caps. Use as a regime/multiplier, not a direct signal.

### 3.2 F&O participant-wise Open Interest (Client / Pro / FII / DII)
- **Claim**: NSE bhav-copy splits daily Open Interest by participant
  category. **Smart-money flow shows up here days before price**: when
  FII OI grows long while Client OI grows short, FII tends to be right.
- **Source**: NSE F&O bhav-copy archive (free,
  https://www.nseindia.com/all-reports-derivatives).
- **Implementation cost**: ~0.5 day on top of the FII/DII pipeline (same
  fetcher pattern, different file format).
- **Fit**: Same XGBoost classifier, additional features. Pairs naturally
  with FII/DII cash data.

### 3.3 Pre-market gap behavior (5-min gap > 2% at 9:15)
- **Claim**: Indian intraday opens with **structurally different** gap
  characteristics from US (no real ECN pre-market, only the 9:00-9:08
  call-auction). Gaps > 2% have documented mean-reversion within 30 min;
  gaps > 5% often *continue* (news-driven).
- **Source**: Various NSE-focused dissertations + our own data (we have
  90 days of 5m bars in `market_data.pkl`).
- **Implementation cost**: 1 day for a Jupyter study quantifying the
  edge on our data; ~1 more day to codify as a strategy if the edge
  survives.
- **Fit**: New strategy class (`strategies/gap_reversion.py`), distinct
  from `opening_range_breakout`. ORB triggers on the *first 30 min*;
  this would trigger on the *gap itself*.

### 3.4 F&O expiry-day effects (Thursday weekly + Last-Thursday monthly)
- **Claim**: Indian F&O expires Thursday (weekly index, monthly individual
  stocks). Last 30 min of Thursday has elevated volatility from
  unwinding; price often pins to a max-pain strike.
- **Source**: Well-documented anecdotally; harder to find rigorous
  academic citation. NISM textbooks discuss it.
- **Implementation cost**: 0.25 day for a feature flag (is_expiry_thursday,
  minutes_to_expiry).
- **Fit**: Regime feature, not a strategy on its own. Would *gate* other
  strategies (e.g., "don't take new positions in last 30 min of Thursday").

### 3.5 RBI / SEBI announcement windows (mostly avoidance, not opportunity)
- **Claim**: Pre-defined volatility shocks (rate decisions, policy
  announcements) blow up directional intraday strategies.
- **Source**: RBI calendar, SEBI press releases.
- **Implementation cost**: ~0.25 day. Calendar-driven feature (is_rbi_day,
  hours_to_announcement).
- **Fit**: Risk gate, not signal. Suppress new entries 60 min before / 30
  min after announcements.

---

## 4. Things deliberately NOT on this list

| Idea | Why excluded |
|---|---|
| L2 / order-book microstructure features | Needs L2 data feed; AngelOne has L1 only on standard plans. Cost > value for retail-tier. |
| News sentiment / NLP on Bloomberg | Requires paid feed (Bloomberg / RavenPack) + NLP pipeline. Out of scope without a separate budget commitment. |
| GPT-generated strategies | Same problem as retail-blog content: no out-of-sample validation. We can use LLMs to *summarize* papers, NOT to *invent* edge. |
| HFT / sub-second strategies | Latency budget on retail broker API is 100-300 ms; HFT requires colo + L2 + custom hardware. Wrong league. |
| Crypto strategies | Different market structure (24/7, no circuit breakers, different costs). Off-strategy for this agent. |

---

## 5. Decision rubric for "should we implement X next?"

When picking the next thing to build, score it 1-5 on each axis:

| Axis | 1 (avoid) | 5 (must do) |
|---|---|---|
| **Evidence quality** | retail blog | peer-reviewed paper with replications |
| **Implementation cost** | weeks | < 1 day |
| **Orthogonality to existing strategies** | does the same thing | market-neutral / different timeframe |
| **NSE-specific advantage** | US-centric, crowded | uses public NSE data nobody else mines |
| **Live-trading risk** | needs new infra | drop-in to current daemon |

Sum > 18: queue immediately. 14-18: queue after current sprint. < 14: leave
in the backlog.

---

## 6. Currently top-of-queue (post v2-redo)

In priority order, gated on next-week's battery results:

1. **Pairs trading on cointegrated NIFTY-50** (1.3 above) -- score ~22.
   Market-neutral, strong academic basis, ~2 day build.
2. **FII/DII flow features** (3.1 above) -- score ~21. Pure feature
   addition, ~1 day, NSE-unique.
3. **F&O participant OI features** (3.2 above) -- score ~19. Pairs with
   #2's data pipeline.
4. **Pre-market gap study** (3.3 above) -- score ~17. Needs a study first
   before commitment.
5. **AngelOne historical fetcher** (separate TODO, infra) -- prerequisite
   for #1-4 because they all need cleaner / longer historical data than
   yfinance can serve.

---

*Maintained by the trading-agent project. Last updated 2026-05-10.
Append new entries with the same structure.*
