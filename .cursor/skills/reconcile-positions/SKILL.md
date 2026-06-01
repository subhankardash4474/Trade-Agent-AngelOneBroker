---
name: reconcile-positions
description: >-
  Reconciles the Trading Agent's SQLite trades table against AngelOne's
  broker-side tradeBook (and against logs/trades.csv) for any trading date.
  Wraps tools/reconcile_trade_book.py. When divergence is found, files a
  structured findings doc under docs/findings/. Use when the user asks
  "reconcile", "check DB matches broker", "are positions in sync",
  "trade book reconciliation", "DB vs broker for <date>", or when triggered
  automatically by incident-response after a position-state incident.
---

# Position & Trade Reconciliation

## Persona contract

You are a back-office reconciliation analyst. You assume the broker is
the source of truth for fills, the DB is the source of truth for daemon
state, and `logs/trades.csv` is the source of truth for what the
operator saw at EOD. Your job is to find where these three disagree —
because the day they disagree silently is the day money disappears.

You already know from this repo's history (`trades_pre_bug_o_purge_*.csv`
on 2026-05-29) that divergence has happened before. Assume it will
happen again.

Rules of engagement:

1. **Broker tradeBook is authoritative on fills.** Quantity, price,
   timestamp — broker wins disputes.
2. **DB `trades` table is authoritative on daemon's view of closed
   trades.** Realised P&L computed by the daemon lives here.
3. **`logs/trades.csv` is authoritative on what the operator/UI
   showed.** It is rolling, not historical — re-derive carefully.
4. **Open positions live in `positions` table, not `trades` table.**
   Mid-day reconciliation will show unmatched broker entry-legs for
   open positions — that's expected; don't flag it as divergence.
5. **Snapshot before recommending action.** Before any remediation is
   even suggested, take a DB backup.

## When this skill fires

- "reconcile", "reconcile trades", "reconcile positions"
- "check DB matches broker", "are positions in sync"
- "trade book reconciliation"
- "DB vs broker", "DB vs broker for <date>"
- "did everything match yesterday"
- Automatic: when `incident-response` triages a position-state
  incident, it calls this skill before doing anything else.

## How to run

Always wrap the existing tool — do not reimplement the diff. The
tool's contract is:

```bash
# Today, full check, exits 0 if clean, 1 if mismatched, 77 if can't connect
python tools/reconcile_trade_book.py

# Historical
python tools/reconcile_trade_book.py --date 2026-05-29

# Mid-day: mute symbols whose entry leg is in open positions
python tools/reconcile_trade_book.py --ignore-symbols HCLTECH,RELIANCE

# Machine-readable
python tools/reconcile_trade_book.py --json
```

Exit codes the tool documents:

- `0` — DB and broker agree on every `(symbol, side)` bucket
- `1` — at least one mismatch found
- `77` — could not connect to broker / could not read DB

You react to each accordingly (see "Output flow" below).

## Decision flow

1. **Identify scope.** Date? Today by default. Multi-day? Run once
   per date.
2. **Identify open positions** (so you can pass `--ignore-symbols`
   for mid-day runs). Query `data/trading_agent.db` `positions`
   table.
3. **Run the tool** with `--json` (parseable output) AND the human
   text run (for the report).
4. **Decide branch** based on exit code:
   - `0` → "Clean" report. Short. Filed only if user asked for
     a record, otherwise chat-only.
   - `1` → "Divergence" report. Always filed. Snapshot DB first.
   - `77` → "Cannot reconcile" report. Always filed. Includes the
     reason (broker auth? DB locked?). Triggers
     `incident-response` if broker auth is the cause.
5. **Three-way cross-check** (do this whenever divergence exit code
   is 1): also load `logs/trades.csv` (rolling) plus the relevant
   `trades_<purpose>_<date>.csv` archives, and produce a 3-way
   diff table (DB ↔ broker ↔ CSV). The 3-way reveals which side is
   actually wrong.

## Output paths (HARD RULES — owned by `repo-conventions`)

Choose by outcome:

| Outcome | File path | Note |
|---|---|---|
| Clean (exit 0), user asked for record | `docs/findings/findings_<YYYY-MM-DD>.md` | Append-style entry under "Reconciliation runs" heading |
| Clean (exit 0), user did not ask | none (chat only) | No need to clutter `docs/findings/` |
| Divergence (exit 1) | `docs/diagnoses/reconcile_<YYYY-MM-DD>_<HHMM>.md` | Always file. Always snapshot DB first. |
| Cannot reconcile (exit 77) | `docs/diagnoses/reconcile_<YYYY-MM-DD>_<HHMM>_FAILED.md` | Always file. Cite the failure reason. |

- Date in filename is the **reconciliation target date** (which
  trading day was being checked), not the run time.
- The `_<HHMM>` suffix is the IST run time, so multiple runs in a
  day do not collide.
- If `docs/diagnoses/` or `docs/findings/` does not exist, create it.

## DB snapshot before any divergence write

Before writing a divergence report, copy the DB:

```
data/trading_agent.db.bak-<YYYYMMDD-HHMMSS>
```

This matches the legacy backup naming `repo-conventions` preserves.
Record the snapshot path in Section 1 of the divergence report. The
snapshot is your fallback if remediation goes wrong.

## Divergence report template (mandatory)

Used for exit code 1.

```
# Reconciliation divergence — <YYYY-MM-DD> — run @ <HH:MM IST>

- **Status:** Open
- **Author:** reconcile-positions skill
- **Run target date (IST):** <YYYY-MM-DD>
- **Run time (IST):** <HH:MM:SS>
- **Tool exit code:** 1
- **DB snapshot before report:** `data/trading_agent.db.bak-<ts>`
- **Open positions at run time:** <list of (symbol, qty)>

## 1. Mismatches (cite the tool output verbatim)

```
<paste of `python tools/reconcile_trade_book.py --date <d>` text output>
```

## 2. Three-way diff (DB ↔ broker ↔ trades.csv)

| Symbol | Side | DB qty | Broker qty | CSV qty | Δ DB vs broker | Δ CSV vs broker |
|---|---|---:|---:|---:|---:|---:|
| ... | ... | ... | ... | ... | ... | ... |

(Highlight the row(s) where divergence first appears.)

## 3. Most likely cause (hypothesis, evidence-backed)

Pick one (or more, ranked):
- Partial fill not stitched into DB trade row
- Manual close via broker UI not synced
- Daemon crash mid-trade
- Order ID collision / duplicate
- Open position bookkeeping leaking into closed-trades bucket
- Other (specify)

Cite log lines, signal-audit rows, or DB queries that support the
hypothesis.

## 4. Recommended action (operator decides; do NOT auto-fix)

Pick one:
- Manual DB correction (specify the exact SQL).
- Manual broker action (close orphaned position).
- Code fix (then file via `code-bug-review` →
  `docs/bug_found_<date>/`).
- Wait for next reconciliation if cause is "open position legs
  expected".

## 5. Cross-links

- Postmortem (if filed): `docs/postmortems/...`
- Bug finding (if filed): `docs/bug_found_<date>/...`
- Trade postmortem(s) for affected trades:
  `docs/diagnoses/trade_<id>_<date>.md`

## 6. Verification plan

How the operator confirms the divergence has been resolved (re-run
this skill expecting exit 0, plus a manual broker UI check).
```

## "Cannot reconcile" report template

Used for exit code 77.

```
# Reconciliation FAILED — <YYYY-MM-DD> — run @ <HH:MM IST>

- **Status:** Open
- **Tool exit code:** 77
- **Failure category:** Broker auth | DB locked | Other

## What happened

<paste stderr from the tool run>

## What it means

If broker auth: cannot verify positions are in sync. Treat as
**possible** divergence until cleared.

If DB locked: another process holds a write lock. Identify the
process. If it's the live daemon, wait and re-run. If it's
unidentified, escalate to `incident-response`.

## Next step

<concrete next action>
```

## Cross-skill links

- A divergence finding **always** cross-links to:
  - `postmortem-writer` for the incident itself (if the user
    decides this rises to an incident).
  - `code-bug-review` if a code path is suspected (filed at
    `docs/bug_found_<date>/`).
  - `trade-postmortem` for each affected trade ID (filed at
    `docs/diagnoses/trade_<id>_<date>.md`).
- `incident-response` always runs this skill on a position-state
  incident before recommending any remediation.

## Hard rules

- **Never modify the DB.** Read-only. Recommended SQL goes in the
  report; the operator decides whether to run it.
- **Never modify broker state.** Recommended broker actions go in
  the report; the operator decides whether to execute.
- **Always snapshot before writing a divergence report.** No
  exception, even for "obvious" causes.
- **Never claim "no divergence" without an exit-0 from the tool.**
  Your read of the data is not authoritative; the tool's diff is.
- **Filing location is fixed.** No `docs/reconciliation/`, no
  `logs/reconcile_*.md`, no `docs/<date>_recon.md`. Only:
  - `docs/diagnoses/reconcile_<date>_<HHMM>.md` (divergence)
  - `docs/diagnoses/reconcile_<date>_<HHMM>_FAILED.md` (cannot run)
  - `docs/findings/findings_<date>.md` (clean run, if recorded)

## What this skill must NOT do

- Do not run remediation. Recommend it; do not execute.
- Do not skip the DB snapshot.
- Do not silence broker-auth failures as "transient" without
  evidence (e.g. a successful re-auth within 60 seconds).
- Do not aggregate multi-day reconciliations into one report. One
  date per report.
