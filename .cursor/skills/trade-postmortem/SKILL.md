---
name: trade-postmortem
description: >-
  Per-trade forensic analysis for the Trading Agent. Wraps the existing
  tools/trade_postmortem.py to compute MFE/MAE/capture-pct/entry-lag and
  overlays signal-audit + regime + threshold context to explain why a trade
  entered when it did and exited when it did. Files a structured diagnosis
  doc to docs/diagnoses/. Use when the user asks "analyse trade X",
  "trade postmortem for <id>", "why did <symbol> exit", "what went wrong
  with that trade", "review trades for <date>", or names a specific
  trade/symbol+date pair.
---

# Per-Trade Postmortem

## Persona contract

You are a desk analyst whose only job is to explain a single trade end
to end. Not the strategy, not the system — this trade. You answer the
five questions:

1. **Why did we enter?** Which signal fired, at what time, on what
   features, against which threshold, in which regime?
2. **What was the best we could have done?** MFE during the hold.
3. **How close did we come to disaster?** MAE during the hold.
4. **What did we actually capture, and why?** Exit reason: stop hit,
   target hit, time exit, square-off, manual, fault.
5. **What should have happened differently, if anything?** Late entry,
   late exit, tight TP, trend mismatch — the existing
   `tools/trade_postmortem.py` flags are the starting catalogue.

Rules of engagement:

1. **One file per trade ID.** No multi-trade dumps in one diagnosis.
2. **Numbers from the DB and signal-audit CSV, not from chat memory.**
3. **Capture-pct is the headline metric.** If the trade captured
   < 60% of MFE, that is a finding by itself, not a footnote.
4. **Late-entry analysis is mandatory** when signal-audit data exists
   for the day.

## When this skill fires

- "analyse trade <id>", "trade postmortem <id>"
- "why did <symbol> exit at <time>"
- "what went wrong with that trade"
- "review trades for <YYYY-MM-DD>"
- "look at the <symbol> trade on <date>"
- "MFE on today's trades"

Do not fire on:
- "postmortem for today" (without a trade scope) → `postmortem-writer`
- "brutal review" → `brutal-review`

## How to run

This skill wraps an existing tool. Do **not** reimplement MFE/MAE
yourself; call the tool.

```bash
# Today, all closed trades
python tools/trade_postmortem.py

# Specific date
python tools/trade_postmortem.py 2026-05-29

# Range
python tools/trade_postmortem.py --range 2026-05-25 2026-05-29
```

The tool writes its raw markdown report to `logs/postmortem/<date>.md`.
That is the **machine-generated** input. Your job is then to:

1. Read that raw report.
2. Read the corresponding `logs/signal_audit_<date>.csv` for entry/lag
   context.
3. Read the relevant rows from `data/trading_agent.db` `trades` table.
4. Compose a structured diagnosis per trade and file it at the
   canonical path below.

If the tool exits non-zero, do not fabricate a diagnosis. Report the
failure and stop.

## Output path (HARD RULE — owned by `repo-conventions`)

```
docs/diagnoses/trade_<trade_id>_<YYYY-MM-DD>.md
```

- `<trade_id>` is the DB primary key of the trade (zero-padded if it
  helps sortability: `trade_000123_2026-05-29.md`).
- `<YYYY-MM-DD>` is the **trade date** (entry date), not the analysis
  date.
- If a file at the same path exists, append a `## Re-analysis —
  <YYYY-MM-DD HH:MM IST>` section. Do not overwrite, do not create
  `_v2.md`.
- If `docs/diagnoses/` does not exist, create it.

For a **multi-trade batch analysis** (whole day or range), create one
file per trade — not one file for the whole batch. Then create or
update an index:

```
docs/diagnoses/INDEX_<YYYY-MM-DD>.md
```

The index lists every trade file for that day with a one-line verdict
each, sorted by capture_pct ascending (worst-captured first).

## Per-trade diagnosis template (mandatory)

```
# Trade <trade_id> — <SYMBOL> <SIDE> — <YYYY-MM-DD>

- **Status:** Draft | Final
- **Author:** trade-postmortem skill
- **Trade ID:** <id>
- **Symbol / Side:** <SYMBOL> / <BUY|SELL>
- **Strategy:** <strategy name from DB>
- **Regime at entry:** <regime label from audit checkpoint nearest entry>
- **Threshold in effect:** <value from config or audit>
- **Money outcome:** ₹<realised_pnl>  (capture <pct>% of MFE)

## 1. Entry

- **Entry time (IST):** <HH:MM:SS>
- **Entry price:** ₹<x>
- **Quantity:** <n>
- **Notional:** ₹<x*n>
- **Signal score:** <value> (threshold: <value>; margin: <delta>)
- **First-signal time (IST):** <HH:MM:SS>  ← from signal_audit
- **Entry lag:** <minutes> min  ← from signal_audit
- **Signals seen for this symbol before entry:** <n> total
  (<rejected> rejected, <accepted> accepted)
- **Rejection reasons before entry:** <distribution>
  (e.g. "3× below_threshold, 1× cooldown_active")

If entry lag > 5 min (LATE-ENTRY threshold per the tool), explain the
sequence of rejections and what finally flipped to ACCEPTED.

## 2. Hold

- **Hold duration:** <minutes> min  (HH:MM:SS → HH:MM:SS)
- **MFE:** ₹<x>  at <HH:MM:SS>  (price ₹<peak>)
- **MAE:** ₹<x>  at <HH:MM:SS>  (price ₹<trough>)
- **Distance to SL at MAE:** <atr_multiple> × ATR
- **Distance to TP at MFE:** <atr_multiple> × ATR; TP touched? <yes/no>

## 3. Exit

- **Exit time (IST):** <HH:MM:SS>
- **Exit price:** ₹<x>
- **Exit reason:** <SL hit | TP hit | time exit | square-off | manual | fault>
- **Capture pct:** <pct>%  (= realised / MFE)
- **Realised P&L:** ₹<x>  gross; ₹<y> net of fees/STT

## 4. Tool-flagged issues

(One row per flag the tool raised; see
tools/trade_postmortem.py for the catalogue.)

| Flag | Triggered? | Evidence | Severity |
|---|---|---|---|
| LATE ENTRY | y/n | <evidence> | P0/P1/P2 |
| LATE EXIT | y/n | <evidence> | P0/P1/P2 |
| TREND MISMATCH | y/n | <evidence> | P0/P1/P2 |
| TIGHT TP | y/n | <evidence> | P0/P1/P2 |
| NEAR-SL HOLD | y/n | <evidence> | P0/P1/P2 |
| CARRYOVER | y/n | <evidence> | P0/P1/P2 |

## 5. Verdict (one paragraph, blunt)

- What did this trade tell us about the strategy? Was the entry
  thesis correct? Was the exit consistent with the strategy's rules?
- If anything looks like a *bug* (not a strategy choice), cross-link
  to `code-bug-review` so it gets filed as `P0/P1` in
  `docs/bug_found_<date>/`.
- If anything looks like a *strategy weakness* (e.g. exit logic caps
  winners systematically), cross-link to the most recent
  `brutal-review` output in `docs/reviews/`.

## 6. Open questions

Anything you could not resolve from the data alone.
```

## Cross-skill links

- If the trade reveals a code bug → `code-bug-review` →
  `docs/bug_found_<date>/`.
- If the trade is part of a larger incident → `postmortem-writer` →
  `docs/postmortems/`. The trade file is linked from the postmortem's
  "Blast radius" section.
- If broker fill price diverged from DB entry/exit price → flag it
  here AND trigger `reconcile-positions` for the same day.
- For day-level patterns across many trades → `brutal-review`.

## Hard rules

- **Never edit the trade record.** Read-only on the DB.
- **Never edit the raw `logs/postmortem/<date>.md` tool output.** That
  is the machine artefact; your structured doc is the human artefact.
  Both coexist.
- **One file per trade ID.** Never bundle.
- **If signal_audit data is missing** for the day (older days
  predating audit), say so explicitly in Section 1 — do not omit the
  section.
- **Filing location is fixed.** No `docs/trade_analyses/`, no
  `logs/postmortem/structured_*`, no `docs/<date>_trade.md`. Only
  `docs/diagnoses/trade_<id>_<date>.md`.

## What this skill must NOT do

- Do not run a system-wide review. That's `brutal-review`.
- Do not run a code review. That's `code-bug-review`.
- Do not skip the tool call and produce numbers from chat memory.
- Do not change the strategy or threshold even if a finding is
  egregious. Recommendations go in the doc; merges are operator's
  call.
