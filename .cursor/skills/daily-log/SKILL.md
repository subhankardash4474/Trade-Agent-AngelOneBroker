---
name: daily-log
description: >-
  Appends a structured entry to the engineering journal at
  docs/journal/engineering_journal_<YYYY-MM-DD>.md. Enforces a consistent
  template (what I did / in flight / blockers / decisions / followups) so
  the journal is greppable and the operator does not stare at a blank
  markdown file. Use when the user says "log it", "journal this", "add to
  journal", "engineering journal entry", "EOD journal", "log this
  decision", or similar.
---

# Engineering Journal — Daily Log

## Why this skill exists

`docs/journal/` currently has two engineering-journal files from
2026-05-07 and 2026-05-08 and then nothing. The pattern died because
the activation energy of writing a blank journal is too high. This
skill provides the structure so each entry is a 30-second append, not
a 10-minute essay.

## When this skill fires

- "log it", "log this", "log this decision"
- "journal this", "add to journal", "journal entry"
- "engineering journal", "EOD journal"
- "record this for the journal"

Do not fire on:
- "record this change" → `changes-done`
- "write a postmortem" → `postmortem-writer`
- "brutal review" → `brutal-review`

## Output path (HARD RULE — owned by `repo-conventions`)

```
docs/journal/engineering_journal_<YYYY-MM-DD>.md
```

- Date is **IST today** at the time of writing.
- One file per day. If the file does not exist, create it (header +
  first entry). If it exists, **append** a new timestamped entry
  block — do not overwrite.
- If `docs/journal/` does not exist, create it.

## File header (only when the file is first created today)

```
# Engineering Journal — <YYYY-MM-DD> (IST)

**Author(s):** <handle>
```

After the header, each entry is appended below, separated by `---`.

## Entry block template (every append)

```
---

## Entry @ <HH:MM IST>

**Context:** <1 sentence: what I'm in the middle of>

### What I did

- <bullet, past tense>
- <bullet, past tense>

### In flight

- <bullet, present tense — what's running / waiting / open PR>

### Blockers

- <bullet, or "None.">

### Decisions made

- <bullet — be specific: "decided X over Y because Z">

### Followups (for next entry / tomorrow)

- <bullet, or "None.">

### Cross-links (optional)

- Postmortem: `docs/postmortems/...`
- Bug finding: `docs/bug_found_<date>/...`
- Trade diagnosis: `docs/diagnoses/trade_<id>_<date>.md`
- Change record: `docs/changes/changes_done_<date>.md`
- Commit(s): <SHA>, <SHA>
```

## How to elicit content when the user only said "log it"

When the user provides no content (just the trigger phrase), do not
invent content. Ask one consolidated question with three or four
slots, then write the entry from their reply:

> "What I did today / What's in flight / Any blockers / Any decisions
> worth recording? Skip any of these you don't have."

Never make up bullets to fill empty sections — write "Nothing to log
here." instead.

## Hard rules

- **Append, never overwrite.** Same-day re-runs add a new
  `## Entry @ HH:MM IST` block at the bottom.
- **No `_v2`, `_evening`, `_morning` siblings.** One file per day.
- **No sections deleted.** If empty, write "Nothing to log here." or
  "None."; do not remove the heading.
- **No prose paragraphs.** The journal is bullets so it stays
  greppable.
- **Filing location is fixed.** No `docs/log/`, no `journal/` at
  the repo root, no `logs/journal/`. Only
  `docs/journal/engineering_journal_<date>.md`.

## What this skill must NOT do

- Do not summarise or interpret what the user did. Record verbatim
  bullets.
- Do not add "AI-generated" disclaimers in the entry.
- Do not file the same content elsewhere (the journal is the
  journal; if it's also a change record, use `changes-done` for that
  copy with a cross-link).
