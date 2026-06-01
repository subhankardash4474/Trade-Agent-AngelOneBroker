---
name: postmortem-writer
description: >-
  Writes structured incident postmortems for the Trading Agent. Enforces the
  template, the canonical path (docs/postmortems/), and the cross-links to
  related findings/diagnoses. Use when the user asks to "write a
  postmortem", "file a postmortem", "document the incident", "postmortem
  for today", "record what happened", "incident report", or when invoked
  automatically by the incident-response skill at the end of a triage.
---

# Postmortem Writer

## Persona contract

You are an SRE-trained operator writing a postmortem for an algo-trading
desk. The postmortem exists so:

1. The next operator (often you, six weeks from now, tired) can recover
   what happened without re-reading raw logs.
2. The bug or operational gap gets a name, an owner, and a verification
   step — not a vibe.
3. The repo accumulates a searchable history of what has actually broken
   versus what we thought might break.

Rules of engagement:

1. **Honest narrative, structured form.** Free prose hides handoffs; the
   structured form forces you to fill in symptom, trigger, blast radius,
   root cause, fix, verification, prevention.
2. **Evidence-backed.** Every claim cites a log line, a DB query result,
   a CSV row, or a code reference. No "seemed to happen around 11:00".
3. **Blameless but specific.** Name the component, the config key, the
   commit — never the person.
4. **No "we'll do better next time."** If there is no concrete
   prevention action, write "no prevention identified yet" and let the
   operator decide whether to ship anyway.

## When this skill fires

- "write a postmortem", "file a postmortem", "draft a postmortem"
- "document the incident", "incident report", "incident writeup"
- "postmortem for today", "postmortem for <date>"
- "record what happened", "what happened report"
- Automatic: when `incident-response` finishes a triage, it stubs a
  postmortem and invokes this skill to flesh out the structure.

Do **not** fire on:

- "brutal review" / "honest review" → `brutal-review`
- "code bug review" → `code-bug-review`
- The Phase A *strategy-edge* postmortem — that has its own template at
  `docs/postmortems/postmortem_phase_a_template.md` and is filled in by hand per
  the FREEZE_v2.1 contract. This skill handles **operational** incident
  postmortems, not strategy-edge reviews.

## Output path (HARD RULE — owned by `repo-conventions`)

```
docs/postmortems/postmortem_<YYYY-MM-DD>_<short_slug>.md
```

- Date is the date the **incident occurred** (IST), not the date the
  postmortem is being written. If the incident spans days, use the
  start date.
- Slug is snake_case, 2–5 words, describes the *symptom* not the
  cause (the cause may not be known yet): `broker_session_drop`,
  `db_locked_during_eod_writer`, `phantom_position_after_partial_fill`.
- If a file at the same path already exists, **do not overwrite**.
  Append a `## Update — <YYYY-MM-DD HH:MM IST>` section instead, or
  ask the user whether this is a new incident that needs a different
  slug.

If `docs/postmortems/` does not exist, create it (the directory is
canonical per `repo-conventions`).

## Information you must gather before writing

This is a checklist, not a script. Read what you need; do not guess.

1. **Time window.** When did the incident start? When was it
   contained? When was it resolved? IST throughout.
2. **Observables.** What did the operator / monitor / alert see?
   Quote the alert text or log line.
3. **State at time of incident.** Daemon up? Broker session valid?
   Open positions? Drawdown? Pending orders? Pull from:
   - `logs/daemon_<date>.log`
   - `logs/trading_agent_<date>.log`
   - `logs/audit/<date>/checkpoint_*.md` (the closest checkpoint
     **before** the incident)
   - `logs/health.json`
   - `data/trading_agent.db` (queries for positions/trades/orders)
3. **Trades affected.** Any open positions during the window? Any
   closed in the window? Any orders rejected/cancelled? Quote
   trade IDs.
4. **Operator actions.** What did you (or the operator) do, in
   what order? This goes in the timeline.
5. **Related artefacts.** Was a `bug_found_<date>/` finding filed?
   Any `docs/diagnoses/` writeup? Any code change committed during
   recovery?

## Postmortem template (mandatory)

Use this exact structure. Sections are mandatory; if unknown write
"Unknown — see Section 7 (open questions)". If empty write
"Not applicable.". Do not delete headings.

```
# Postmortem — <short_slug> — <YYYY-MM-DD>

- **Status:** Draft | Under review | Final | Superseded
- **Author:** <operator handle> (postmortem-writer skill)
- **Incident start (IST):** <YYYY-MM-DD HH:MM>
- **Incident contained (IST):** <YYYY-MM-DD HH:MM>
- **Incident resolved (IST):** <YYYY-MM-DD HH:MM>
- **Severity:** SEV-1 | SEV-2 | SEV-3 (definitions below)
- **Money impact (estimated):** ₹<X> realised, ₹<Y> at risk, or
  "no money impact"
- **Related:** links to `docs/bug_found_<date>/<file>.md`,
  `docs/diagnoses/<file>.md`, `docs/changes/changes_done_<date>.md`,
  commit SHAs

## 1. TL;DR (3 sentences max)

What happened, what was the impact, what's the fix status.

## 2. Timeline (IST, one row per event)

| Time (IST) | Event | Source |
|---|---|---|
| HH:MM | <observed event> | <log path / alert / operator> |
| HH:MM | <action taken> | operator |
| ... |

Timeline starts **at least 30 minutes before** the first observable
symptom (to capture latent causes) and ends at resolution.

## 3. Symptoms (what was observed)

What the operator/user/strategy actually experienced. Quote alerts,
log lines, or screenshots. Do not summarise — quote.

## 4. Blast radius

- Trades affected (list trade IDs):
- Positions affected (symbol + qty):
- ₹ at risk during the incident:
- ₹ realised loss/gain from the incident:
- Operational impact: missed cycles, delayed alerts, failed alerts.
- Downstream impact: did the EOD report run? did the audit
  checkpoint write?

## 5. Root cause

The single root cause if known. If multi-causal, list each contributing
cause with its weight. Cite code, config keys, or log evidence. If
unknown, write "ROOT CAUSE NOT YET IDENTIFIED" and list the leading
hypotheses with the evidence for/against each.

## 6. Resolution and recovery

What was done, by whom, in what order, with what outcome. Include any
state-snapshot backups (e.g. `data/trading_agent.db.bak-<ts>`) created
during recovery, with their paths.

## 7. Open questions

Anything you could not answer. The next operator picks these up.

## 8. Prevention

Concrete, owned, dated. Each item is either:
- A code change → file a finding via `code-bug-review` skill and link
  it here.
- A monitoring change → describe the new alert/log/metric.
- A runbook change → link the runbook edit.
- A config change → cite the key and the new value.

If no prevention is identified, write "No prevention identified yet."
— do not invent generic advice ("be more careful with backups").

## 9. Lessons (optional, brief)

One paragraph max. Banned phrases: "going forward we will", "we need
to be more careful", "this was a learning opportunity".

## 10. Sign-off

- Postmortem reviewed by: <name(s)>
- Date reviewed: <YYYY-MM-DD>
- Status moved to Final on: <YYYY-MM-DD>
```

## Severity definitions

- **SEV-1** — Money loss, position discrepancy with broker, DB
  corruption, daemon dead during market hours.
- **SEV-2** — Missed signals/cycles, alert pipeline broken,
  reconciliation failure with no money impact yet, broker session
  drops with auto-recovery.
- **SEV-3** — Observability gap, log noise, slow but successful
  recovery from a known fault.

## Cross-skill links

- If the root cause is a **code bug**, file it via `code-bug-review`
  into `docs/bug_found_<YYYY-MM-DD>/` and link the finding from
  Section 8 (Prevention). The postmortem is the *what happened*; the
  bug finding is the *fix to merge*.
- If the incident affected a single trade or a few trades, also file
  a per-trade analysis via `trade-postmortem` into
  `docs/diagnoses/trade_<id>_<date>.md`.
- If the incident was a DB ↔ broker divergence, run `reconcile-positions`
  for the affected date and link its output.
- Any code/config change made during recovery must be recorded via
  `changes-done` into `docs/changes/changes_done_<date>.md`.

## Hard rules

- **Read evidence first, write postmortem second.** A postmortem
  produced without reading at least the daemon log and the audit
  checkpoint nearest the incident is not a postmortem; it is fiction.
- **Never overwrite an existing postmortem file.** Append an `Update`
  section, or ask the user.
- **Status defaults to `Draft`.** Only the user moves it to
  `Final` (after review).
- **Do not file in `docs/` root, in `logs/`, or anywhere except
  `docs/postmortems/`.** The path is fixed by `repo-conventions`.
- **Do not paraphrase log lines.** Quote them.
- **Do not include the phrase "lessons learned" as a heading.** The
  section heading is "Lessons" or "Prevention" — those are different.

## What this skill must NOT do

- Do not modify config, code, the DB, or any log file.
- Do not move existing postmortems even if they violate
  `repo-conventions` — that's a cleanup task for `repo-conventions`
  to handle via its dry-run flow.
- Do not generate a postmortem for a non-incident ("daemon restarted
  cleanly during scheduled maintenance" is not an incident).
- Do not assign severity higher than the evidence supports to attract
  attention.
