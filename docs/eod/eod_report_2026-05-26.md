# EOD Report — Tuesday, 2026-05-26 (IST)

_Operator narrative compiled from `logs/trading_agent_2026-05-26.log`, `logs/daemon_2026-05-26.log`, `logs/diagnostics/eod_2026-05-26.md` (auto-gen), `logs/postmortem/2026-05-26.md` (auto-gen). Compiled 16:05 IST after market close._

---

## TL;DR (one line)

**-Rs 453 (3 stop-loss trades from xgboost_classifier, all "late entry / late exit"). Daily kill switch tripped at 11:35:34 IST — no further trades for the rest of the session. Daemon healthy at EOD with `allow_shorts: false` LIVE and Bug-I rebuild stable.**

---

## 1. Trading session — what happened

### Market context

| Time (IST) | Nifty | EMA200 | VIX | Regime |
|---|---:|---:|---:|---|
| 08:02 (preopen) | 24,032 | 24,744 | 16.7 | bear_high_vol |
| 09:48 (1st entry) | ~24,015 | 24,737 | 16.7 | (transient sideways/bear_low_vol — see §4) |
| 11:35 (kill switch) | 24,038 | 24,737 | 16.7 | bear_high_vol |
| 14:41 (rebuild) | 23,898 | 24,736 | 16.7 | bear_high_vol |
| 15:30 (close) | 23,911 | 24,736 | 16.26 | bear_high_vol |

- Nifty closed **~3.4 % below 200 EMA** — confirmed bear regime end-to-end.
- VIX flat at 16.7 (slight rise off yesterday's 16.26 close).

### Trades (3, all from `xgboost_classifier`, all stop-loss)

| # | Symbol | Side | Entry (IST) | Exit (IST) | Held | PnL | Entry lag | MFE capture |
|---|---|---|---|---|---:|---:|---:|---:|
| 1 | TATAINVEST | BUY | 09:48:55 @ 697.57 | 10:55:27 @ 697.63 | 67 min | **-Rs 14.96** (-0.10 %) | 22.2 min | 0.7 % |
| 2 | HFCL | BUY | 09:52:40 @ 166.69 | 10:03:28 @ 164.38 | 11 min | **-Rs 228.65** (-1.49 %) | 31.5 min | -259.6 % |
| 3 | TATACHEM | BUY | 10:48:46 @ 800.50 | 11:35:34 @ 790.32 | 47 min | **-Rs 209.43** (-1.38 %) | 86.7 min | -135.7 % |

**Totals:** 3 trades, 3 losses, **-Rs 453.04** realised, **+Rs 409.84** MFE theoretical → **Rs +814 left on the table** (mean MFE-capture -131.5 %).

> Every entry was flagged `[LATE-ENTRY]` (entry lag well above 5-min threshold), and every exit was `[LATE-EXIT]` (price moved against us before the SL fired). Translation: the model saw the move early, we entered after the momentum already faded, then the stop tripped on the noise reversal.

### Kill-switch chain

```
11:35:34  WARNING  [STRATEGY-BREAKER] xgboost_classifier suspended for the day
                   (3 consecutive losses, day_pnl=Rs -453.03)
11:35:35  WARNING  Trading blocked: Consecutive losses: 3 (limit: 3)
```

Both breakers fired correctly — first the per-strategy breaker, then the global consecutive-loss gate one second later. **No further entries after 11:35 IST.**  Cash settled at **Rs 120,990** (start Rs 121,443 → loss Rs 453, the math balances).

---

## 2. Why we lost (root cause)

The system behaved exactly as designed — but the design has a known asymmetry that today's data exposes cleanly:

1. **The Nifty was in a confirmed bear regime all day** (3.4 % below 200 EMA, falling all afternoon).
2. **`xgboost_classifier` is the only strategy currently firing long signals** in this market. Today it generated **BUY** on dozens of names (DABUR, HDFCBANK, ICICIBANK, KOTAKBANK, DLF, ITC, JINDALSTEL, TECHM, BAJAJFINSV, …).
3. The **`[LONG-REGIME]` gate** correctly blocked most of them — only `['bear_low_vol', 'bull_high_vol', 'bull_low_vol', 'sideways']` are allowed for longs, and `bear_high_vol` was the dominant regime.
4. **3 names slipped through** during the 09:45–10:50 window when the regime classifier briefly dropped to `sideways` / `bear_low_vol`. The XGB model immediately fired BUY → entry → stop loss as the regime snapped back to `bear_high_vol` and the price faded.
5. The daily-loss kill switch then did its job and saved capital.

**Net read:** the model is mechanically wrong-way in this market (bullish long-only signals into a downtrend). The regime filter is the only thing standing between us and a much larger drawdown. The 3 "leaks" cost Rs 453 — which is the *floor*, not a worst case.

---

## 3. Operational events (post-market)

After the kill switch, the rest of the day was operator activity:

| Time (IST) | Event | Outcome |
|---|---|---|
| 14:41:04 | Initial deploy attempt (Bug G + risk-flag pull) | Aborted — local `git pull` rejected due to **Bug I** (5 uncommitted hot-fixes on trader VM, ~2 weeks old). |
| 14:41:08 | Daemon restarted on existing code | XGB-HEALTH OK, risk-state recovered from DB (ConsecLoss=3 honored, DD 1.6 %). |
| 14:42:27 | Restart confirmed `Trading blocked` still active | DB-persisted breaker survived the restart — design works. |
| ~15:00 | Manual VM rebuild by you | Hot-fixes committed → `origin/main` pulled → merged → resolved. Trader code now at `73c26bf`. |
| 15:08–15:18 | 3 docker restarts during config flip / image rebuild | Each came back `(healthy)`. |
| 15:19:55 | Final restart with `risk.allow_shorts: false` LIVE | `allow_shorts = False` confirmed inside container. Consecutive-loss block still in effect. |
| 16:01:03 | Auto post-mortem + EOD diagnostic emails dispatched | (post-mortem suppressed as duplicate of 15:32 send) |

### Bypass-ledger / freeze-v2.1 status at EOD

- **Slot 1 — `risk.allow_shorts: false`** — **LIVE** as of 15:19:55 IST. Trader VM `/app/config.yaml` confirmed via `yaml.safe_load`. *3 / 4 emergency slots remaining.*
- **Audit-only (no slot consumed):** Bug G-1.A + G-3.A code parity, Bug H (models bind-mount on backtester), Bug I (trader VM drift — now reconciled).

---

## 4. Items the auto-EOD already flagged (worth reading)

- **Rolling 5-day PF = 0.09** — Phase-A verdict **FAIL** (floor is 1.00). `logs/diagnostics/eod_2026-05-26.md` recommends running `profit_diagnostic.py --days 10` for a full postmortem before any further changes.
- **Portfolio Kelly = -2.227** → mathematically "do not bet" at current WR (28.6 %) and R:R (1:0.28).
- **`xgboost_classifier` per-strategy stats:** 3 trades, **0 wins, PF 0.00, Rs -453**, all stop-losses, all longs in a bear regime. The auto-EOD marks it `INSUFFICIENT_DATA` (N=3, threshold is 10), but the direction is unambiguous.
- **`rsi_momentum`** is the only short-side strategy (4 trades window-wide, all shorts, -Rs 169, PF 0.32). With `allow_shorts: false` now live, **rsi_momentum will not enter new positions tomorrow** — by design, this consumes one of the four bypass slots in exchange for cutting the largest source of bleed.

---

## 5. State going into 2026-05-27

| Component | State at EOD | Notes |
|---|---|---|
| Trader daemon | **Up (healthy)** — code `73c26bf` (origin/main), uptime 41 min at 16:01 | XGB-HEALTH OK, model 31 features / threshold 0.65 |
| Cash | **Rs 120,990** | Down Rs 453 on the day; peak Rs 122,993; DD 1.6 % |
| Open positions | **0** | All flat by 11:35 IST |
| Cooldowns | HFCL, TATAINVEST, TATACHEM | Daemon will skip these tomorrow morning |
| Risk state | `cash` | Within DD limits |
| Trading blocked | **YES** (ConsecLoss=3, DB-persisted) | Resets on next trading day's `Daily trackers reset` (around 08:00 IST) |
| `risk.allow_shorts` | **false** (LIVE) | No new shorts will enter until reverted |
| Alert pipeline | OK | EOD diagnostic + post-mortem both dispatched (1 duplicate suppressed) |

**Expected tomorrow morning (2026-05-27, ~08:00 IST):**
- Daily trackers reset → `Trading blocked: Consecutive losses` clears.
- Cooldowns (HFCL/TATAINVEST/TATACHEM) auto-expire at next day boundary.
- `rsi_momentum` blocked from opening new shorts (allow_shorts=false).
- `xgboost_classifier` will resume firing — and will again be gated by `[LONG-REGIME]` for any name in `bear_high_vol`.
- **If Nifty stays below 200 EMA**, expected behaviour: very few trades fire; whatever does fire is the same 3-leak pattern. **Plan B if losses repeat tomorrow**: kill `xgboost_classifier` from `config.yaml` (does not consume a bypass slot, it's an audit-only change since the strategy is bleeding by data, not by policy).

---

## 6. Friday review (2026-05-29) — what we need from the backtester

The Friday review depends on **at least V1–V3 completing on the backtester VM with the XGBoost model loaded** (Bug H was that the backtester containers had no model — fixed today). Status:

- Backtester running `nifty50_60d` queue with audit fixes (G-1.A, G-3.A) + Bug H bind-mount.
- Validation re-run starts from V1; previous "V1/V2 without xgboost" results were archived this afternoon.
- First variant ETA: **late tonight → early Wed AM IST**. I'll spot-check `XGB-HEALTH` on the first completion and report.

---

## 7. Open follow-ups (for Wednesday)

1. **Monitor first backtester variant completion** (~22–31 h buffer to Friday).
2. Once V3 (`bear_regime_no_longs`) completes, **compare its drawdown vs today's live -Rs 453** to validate that disabling longs in `bear_high_vol` would have prevented this exact loss pattern.
3. If V3 confirms the hypothesis, draft the policy change for slot-2 (`block_longs_in_bear_high_vol`) and put it through the bypass ledger.
4. **No new bypass slots consumed without explicit approval.** 3 / 4 remain.

---

_Compiled by trading-agent operator audit pass. Cross-references: auto profit-diagnostic at `logs/diagnostics/eod_2026-05-26.md`, trade-by-trade post-mortem at `logs/postmortem/2026-05-26.md`, findings log §16-17 in `docs/findings_log_2026-05-25.md`, change history §17 in `docs/changes_done_2026-05-25.md`, freeze policy slot ledger in `docs/FREEZE_v2.1.md`._
