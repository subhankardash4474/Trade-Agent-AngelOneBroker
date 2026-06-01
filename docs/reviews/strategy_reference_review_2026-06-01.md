# Strategy reference review — 2026-06-01

**Status:** Adversarial read of four institutional-quant "proofs" the
operator surfaced for discussion on 2026-06-01 ~11:10 IST, the week of
the 2026-06-05 wind-down verdict. Reference document; intended to be
quoted in the verdict-meeting packet and pinned for future re-reads
when the operator re-encounters the same headline statistics in
trader-blog / Twitter prose.

**Persona contract:** same as `brutal-review/SKILL.md` — evidence or
silence, no participation trophies, rank by relevance to *this*
operator's *this* capital base. Strategy folklore that does not
survive contact with retail-scale arithmetic is called out as
folklore.

**Companion docs:**

* [`brutal_review_2026-06-01.md`](brutal_review_2026-06-01.md) — this
  morning's adversarial review of the trading agent itself. §1 of
  that doc (the uncommitted Zerodha→AngelOne charges rewrite) is
  reinforced, not weakened, by the evidence below.
* [`freeze_v3.0_charter_2026-05-30.md`](../freeze/freeze_v3.0_charter_2026-05-30.md)
  — §1 finding #4 ("5-min MIS commission drag dominates P&L at
  retail sizes... v3 changes the cost regime") is the thesis under
  test.

---

## The four claims, as presented

1. **Virtu Financial** — S-1 disclosed 1 losing day out of 1,278 (2009–2014),
   "99.9% daily win rate purely through market-making algorithms".
2. **Renaissance Medallion** — ~66% gross / ~39% net annualised since 1988,
   "advanced math to exploit short-term statistical mispricings across
   thousands of highly correlated financial instruments".
3. **Trend-following CTAs** (Man AHL, Dunn Capital, SG Trend Index) —
   "multi-decade profitable track records; double-digit gains in 2008
   and 2022 while broader stocks collapsed".
4. **Practical retail caveats** — infrastructure / leverage / data
   pipeline disadvantage paragraph.

The claims are **factually approximately correct** but selectively
framed in three of the four cases. The retail-applicable lesson from
each is materially different from what the headline suggests.

---

## 1. Virtu — "1 losing day out of 1,278"

### Factual corrections

* **Window was 2009-01-01 → 2013-12-31** (5 calendar years), not "2009-2014".
  Source: S-1 filed March 2014.
* The **single losing day was attributed to human error**, not a market loss.
* The IPO was **delayed** by the Michael Lewis *Flash Boys* controversy
  in March 2014 and didn't complete until **April 2015**.

### Material omissions

* "Winning" is at the **firm-wide daily P&L level**, not per-trade.
  Virtu loses on individual fills constantly. The day-level
  distribution is tight-around-positive by the central limit theorem
  applied to millions of micro-edges.
* The 99.9% daily WR is a **structural property of the business
  model**, not of a strategy that can be copied. A registered market
  maker with rebate tiers, co-location, and obligated two-sided quoting
  will, by construction, have day-level P&L that converges to a tight
  positive distribution.
* Most of Virtu's gross income is **exchange maker rebates** (sometimes
  >50% on certain venues) and, post-KCG-acquisition, **payment for
  order flow**. The "bid-ask spread capture" framing understates this.
  Strip the rebate stream and a chunk of the daily edge disappears.
* Net income margin in the cited years was ~30-40%. The gross edge per
  trade is in tenths of a basis point. "Trading is risk-free" collapses
  the moment a venue changes its fee schedule.

### Retail relevance

**Zero.** Registered market-maker status requires exchange membership,
regulatory capital, prime-broker inventory financing, and infrastructure
that costs ₹10-50 Cr/year minimum. Anyone selling a retail trader on
"do what Virtu does at home" is selling something. The **transferable
lesson is the opposite of the headline**: you only achieve a 99.9% daily
WR by having a tiny per-trade edge and millions of trades per day to
invoke the law of large numbers. This agent runs ~15-30 trades on a
*good* day. The law of large numbers will not help it; trade-level
edge has to be visible without averaging.

---

## 2. Renaissance Medallion — "66% gross / 39% net since 1988"

### Factual corrections

* Numbers are approximately right through **2018**. Post-2018 is
  unaudited; reconstructed from employee-LP disclosures and Greg
  Zuckerman's *The Man Who Solved the Market* (2019) reporting.
* Medallion's **modern era starts ~1990**, not 1988. The 1988-1989
  Axcom-system years returned ~9% then **-4%**. The legendary streak
  begins after Robert Mercer / Peter Brown rebuilt the mathematics.

### Material omissions — and this is the one that matters

* **Medallion has been capped at $10B AUM forever.** RenTech actively
  returns capital to employees rather than let the fund grow. The
  strategy is **non-scalable by design**.
* RenTech's funds open to outside investors — RIEF, RIDA, RIDGE — have
  performed **roughly in line with or below the S&P 500** over the
  long run. **Same firm, same researchers, same data — different
  capacity envelope, different result.** The Medallion edge does not
  transfer to anyone except the people who built it, and even within
  the firm it cannot be productised at scale.
* Leverage is reportedly **~12:1**. A 39% net on 12x leverage implies
  an **unlevered edge of ~3-4%** — compounded brutally, but in raw
  terms not magical.
* Strategy is closer to **microstructure prediction at intraday-to-hours
  horizons** than classical stat-arb. Holding periods are reportedly
  minutes to hours.
* Medallion has **not had a down year since 1990**. +98% in 2008, +76%
  in 2020. The strategy does *better* in chaotic markets — opposite of
  what most retail systems do.

### Retail relevance

**Zero.** The actionable lesson from Medallion is uncomfortable: **the
only quant strategy in history with a verified decade-long high-Sharpe
edge is one that (a) cannot be scaled, (b) cannot be productised, (c)
cannot be transferred to other researchers, (d) cannot be opened to
outside investors.** Every fund that claimed to "do what Medallion
does" at institutional scale has underperformed. The Medallion story
is **evidence that high-Sharpe retail quant trading is structurally
hard, not that it is possible**.

---

## 3. Trend-following CTAs (Man AHL, Dunn Capital, SG Trend Index)

This is the only one of the four that is **plausibly retail-transferable**,
but the user's framing is selective.

### Factual corrections

* 2008 SG Trend Index: ~**+20%** vs S&P -37%. ✓
* 2022 SG Trend Index: ~**+27%** vs S&P -18%. ✓
* **Long-term CAGR of the SG Trend Index since 2000 is ~3-7% net**
  depending on the window — **less than the S&P 500's ~7-9% CAGR over
  the same period.** Trend-following has **underperformed buy-and-hold
  equities on a CAGR basis since 2000**. The crisis-alpha years (2008,
  2022) are real, but the basket has not been a long-run alpha generator
  vs equities.

### Material omissions

* **2011-2019: ~8 years of flat-to-negative real returns for trend
  followers.** Anyone who invested at the 2011 SG Trend peak was
  underwater for ~8 years until 2020-2022's volatility regime returned.
  The pattern is **decade-long flat stretches punctuated by 12-24 month
  bursts of crisis alpha**.
* Win rate on individual trades is ~**30-45% with positive skew** —
  many small losses, occasional huge winners. Expectancy is positive
  only because of the tail. **A retail trader who cuts winners short
  or doesn't ride 20% trends will destroy the edge.**
* Capacity is real for the institutional names (Man AHL is ~$50B+);
  running daily-frame trend at that AUM is itself an art. At retail
  size capacity is a non-issue, which is the one structural advantage
  retail has in this strategy class.
* Net-of-fees institutional returns (1-and-15 or 2-and-20) are far
  below the gross. Retail can avoid the fee drag, which slightly
  shrinks the institutional-vs-retail gap.

### Retail relevance

**Plausible, with hard caveats.** Daily/weekly trend following on a
diversified basket of **20-40 liquid futures or ETFs** (equity indices,
bonds, commodities, FX) is the one institutional quant strategy that
*does* roughly survive retail-scale implementation — but **only** if
the operator accepts:

1. **3-7% CAGR, not 30%.**
2. Multi-year flat stretches between crisis-alpha events.
3. ~35% win rate with positive skew (most trades small losses,
   occasional huge winners).
4. **The diversification across 20+ instruments IS the strategy**, not
   a nice-to-have. Trend-following on 5 NSE stocks is not trend
   following — it is concentration-risk-with-a-trend-label.

---

## 4. The "practical reality" paragraph

Correct as far as it goes; it understates two harder retail constraints:

### Survivorship bias

Every quant-success statistic selects on firms that **survived**. We
never see the 10,000 stat-arb shops that quietly died in 2007-2011.
The base rate of "quant fund actually delivers the promised edge over
a 10-year window" is **well below 20%**. The four examples in the
user's text are all selection-biased — we cite the survivors.

### Edge decay

Every published edge erodes once it's published.

* 1990s pairs trading (cointegrated equity baskets) → dead by ~2010.
* 2000s factor rotation (value, momentum) → smart-beta-ETF'd into
  oblivion by ~2015.
* 2010s vol-risk-premium harvesting → mostly compressed by 2020.

**By the time a strategy is well-known enough to be in a popular
article, the alpha is mostly gone.** This is the most-violated rule in
retail quant.

### Decision latency

Even with zero infrastructure cost, a retail trader making manual
decisions is bounded to **minutes-to-days holding periods**. Virtu
operates at microseconds; Medallion at minutes; Man AHL at days-to-weeks.
Latency is not a 1:1 proxy for edge but it strongly bounds **which
strategy families are available to which actors**.

---

## What this means for **this** trading agent

This is the part that should drive Friday's verdict-meeting framing.

### 1. The agent is in the trend-following family, not the stat-arb / market-making family

The v3 swing charter — two-rule trend-pullback + 20-day breakout on
largecap Nifty CNC delivery — is structurally what AHL / Dunn / SG
Trend constituents do at far larger scale. **The honest reference
point for "v3 success" is 3-7% CAGR with multi-year flat stretches and
~35% WR with positive skew** — not the 39% Medallion delivered, not
the 99.9% daily WR Virtu delivered.

The v3 charter §4 already projects ₹250-700/month at ₹25k seed
(~12-34% annualised), which is **honest by historical-trend-following
standards IF AND ONLY IF the strategy has measurable edge**. Saturday's
V25 result (PF 0.23, MaxDD 37%) says it doesn't, at least on the
long-only 7-11% of the natural signal set.

### 2. Virtu and Medallion are irrelevant references for this project

Both depend on structural advantages this operator will never have
(registered market-maker status, prime-broker leverage, capacity
discipline, decades of accumulated data). **Citing them as
proof-of-concept that algo trading "works" is a category error** —
they are proof that *very specific kinds of algo trading work for
very specific entities*. The transferable lesson from these two is
the opposite of what the operator's text implied: **even with the
best math in history, the only honest verified retail-accessible
edge in this list is a ~5% CAGR trend-following return**.

### 3. The "edge requires structural advantage" pattern reinforces the brutal-review §1 (charges) finding

The Virtu / Medallion / AHL evidence all agree on one thing: **the
firms that win at quant do not win on strategy alone. They win on a
cost / leverage / infrastructure / capacity-discipline advantage
stacked on top of a strategy.** This is the explicit thesis of v3
charter §1 finding #4 ("commission drag dominates P&L at retail
sizes... v3 changes the cost regime"). The uncommitted AngelOne
charges rewrite is therefore the single most consequential file in
the repo right now, because **it tells you whether the v3
cost-regime thesis is even directionally true at retail**.

At Zerodha rates (the rates every v3 backtest was computed at), the
v3 cost-regime thesis stands and the seed-phase income projection is
₹250-700/month. At AngelOne rates (the rates the live broker
actually charges), preliminary analysis from this morning's brutal
review suggests **₹0-500/month at seed capital**. That moves v3 from
"proof of concept, positive expected income" to "**proof of concept,
break-even at best**". The verdict meeting needs to know which cost
regime the charter's numbers were computed under.

### 4. One concrete reframe for Friday

If the verdict meeting is partly about "is algo trading worth
pursuing at retail at all", the honest answer from this evidence is:

> **Yes, but only at trend-following economics.** ~5% CAGR, multi-year
> flat stretches, positive-skew distribution, on a 20-40 instrument
> basket, with **₹3-5L of capital minimum** to make the monthly return
> meaningfully exceed broker fees and infrastructure costs.

The Medallion / Virtu numbers should **not be in the conversation**.
If they are, someone is being sold a fantasy, and the wind-down
decision is the right call **regardless** of what V26 produces.

---

## What the operator MAY cite at the verdict meeting

* SG Trend Index 2000-2026 CAGR (~3-7% net) as the honest benchmark
  for *what a successful retail-implementable trend strategy looks
  like*.
* The 2008 / 2022 trend-following crisis-alpha episodes as evidence
  that **systematic strategies can be additive to a portfolio even
  when their standalone CAGR is unimpressive**.
* The Medallion *capacity-cap* fact ($10B forever, employee-only) as
  evidence that **high-Sharpe quant edges DO exist but are
  structurally tiny and non-scalable** — which is *also* evidence
  that the operator's project should not expect Medallion-class
  returns at any capital level.

## What the operator MUST NOT cite at the verdict meeting

* Virtu's 99.9% daily WR as evidence that "algo trading works".
  It is evidence that **registered market makers with rebate income
  have tight day-level P&L distributions** — a fact that does not
  generalise.
* Medallion's 39% net as a benchmark or aspiration. The retail-accessible
  version of this return does not exist.
* SG Trend Index's 2008 / 2022 numbers in isolation. Always quote
  alongside the **2011-2019 flat stretch** so the listener understands
  the path-dependency of the "crisis alpha" return profile.
* The v3 charter §1 finding #4 ("5-15% commission drag") **at all**
  until the charges-rewrite question (this morning's brutal review §1)
  is resolved one way or the other. The current charter number was
  computed at Zerodha rates and the live broker is AngelOne.

---

## Cross-references

* `.cursor/skills/brutal-review/SKILL.md` — persona contract.
* `brutal_review_2026-06-01.md` §1 (charges rewrite) — the finding
  this doc most directly reinforces.
* `../freeze/freeze_v3.0_charter_2026-05-30.md` §1, §4 — the charter
  whose economic case is under test.
* `../freeze/wind_down_criteria_2026-06-05.md` — the verdict-meeting
  sheet this reference doc supports.

**Source notes:** Virtu S-1 (March 2014, SEC EDGAR);
Zuckerman, *The Man Who Solved the Market* (2019); SG Trend Index
factsheets (Société Générale Prime Services); public Man AHL and
Dunn Capital performance disclosures. Specific numerical claims
above are stated to 2-significant-figure precision because the
underlying public disclosures themselves are 2-sig-fig.
