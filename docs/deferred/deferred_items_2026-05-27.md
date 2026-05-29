# Deferred Items — 2026-05-27 Audit

Companion to `docs/findings/findings_2026-05-27.md` and
`docs/changes/changes_done_2026-05-27.md`.

These are findings raised during the 2026-05-27 audit that were NOT
fixed in this sweep, with the rationale and a required user decision
for each. The intent is that a future operator-driven session can pick
up any of these without re-doing the analysis.

Each deferred item is also documented inline in source near the
relevant code with a `(deferred D-F-NN)` tag pointing here.

---

## D-F-01 — Battery backtester hardcodes `regime="unknown"`

**Original finding:** F-25
**File:** `packages/research/backtest_ensemble.py:454-464`

The live agent classifies regime per cycle from `Nifty trend + India
VIX` (`classify_regime(self._market_context)` → bull_low_vol /
bear_high_vol / etc.) and the ensemble + risk manager use that for
weight scaling. The battery backtester hardcodes `regime="unknown"`
because the per-bar Nifty+VIX context is not currently in the
`market_data.pkl` feed.

**Net effect.** Backtester evaluates every strategy with its full
GLOBAL learned weight on every bar; live agent suppresses several
strategies in bear_high_vol. Direction of any "shorts have negative
edge" finding from the battery is preserved, but magnitude in
production will differ.

**Why deferred.** Requires (a) adding Nifty/VIX daily bars to
`market_data.pkl`, (b) computing regime per-bar from a rolling
Nifty/VIX window, (c) plumbing that regime into `ensemble.aggregate`
+ the sizing call. This is a multi-day infrastructure change, not a
patch. Documented as a follow-up in `docs/findings/findings_log_2026-05-25.md`
§11.

**Required user decision.** Confirm the trade-off: do we (a) accept the
documented backtest/live magnitude gap as a known caveat and continue
reading battery output qualitatively (today's policy), or (b) invest
1-2 weeks in the data pipeline upgrade to close the gap quantitatively?

---

## D-F-02 — `pickle.load` on battery market_data cache without integrity check

**Original finding:** F-101
**File:** `packages/research/battery.py` (cache loader)

The battery harness loads `market_data.pkl` via `pickle.load`, which
is arbitrary-code-on-load. The cache file lives under operator-owned
`data/cache/` and is produced by the same operator-controlled
pipeline, so the threat model is bounded — but a tampered file would
execute attacker code under the battery worker's privileges.

**Why deferred.** Hashing + signing requires a key-distribution policy
(where does the signing key live? rotation cadence? CI integration?).
The fix is small but the operational change is not.

**Required user decision.** Choose a policy:
1. **Trust-on-first-use sha256 manifest** alongside the .pkl
   (operator regenerates the manifest after each cache rebuild).
2. **HMAC** with a shared secret in `.env`.
3. **Migrate to a non-executable format** (parquet / arrow) so the
   pickle attack surface goes away entirely. Largest change, best
   long-term posture.
4. **Accept the current threat model** (cache under operator
   control, no external write path) and add a comment noting the
   acceptance. Smallest change.

---

## D-F-03 — Battery `--run-id` reuse without `--resume` mixes prior-run artifacts

**Original finding:** F-102
**File:** `packages/research/battery.py` (run-id setup)

If an operator reuses a `--run-id` from a previous battery invocation
without passing `--resume`, the harness writes new variant artifacts
into the SAME directory as the previous run's leftover artifacts. The
comparison.md aggregator then mixes results from two different runs.

**Why deferred.** Two valid policies:
- **Auto-resume on `--run-id` collision** — assume the operator
  meant to continue; UX matches `docker run --name`.
- **Reject on `--run-id` collision unless `--force`** — assume the
  operator made a mistake; safer; matches `git branch` semantics.

Neither is strictly wrong. The fix is one-line either way once chosen.

**Required user decision.** Pick a policy. Recommendation: reject
on collision unless `--force` (errors are cheap to recover from;
silent contamination is not).

---

## D-F-04 — Model deserialization has no integrity / allowlist enforcement

**Original finding:** F-105
**Files:** `packages/strategies/lstm_model.py`,
`packages/strategies/xgboost_classifier.py`

`pickle.load` (XGBoost) and `torch.load(weights_only=False)` (LSTM)
both deserialize arbitrary code at load time. B-19 (audit 2026-05-25)
mitigated by logging the absolute path of every load so a security
audit can verify provenance, but did NOT migrate to safe load.

**Why deferred.** Migration path:
1. Re-export every checked-in artifact in `state_dict()` form.
2. Reconstruct the model architecture at load time in code.
3. Switch `torch.load(..., weights_only=True)`.
4. For XGBoost, switch to `model.save_model()` (JSON) instead of
   pickle.
5. Add a one-time migration script + regression test that the
   re-loaded model produces identical predictions on a fixed test
   batch.

Multi-week. Touches strategy code (frozen surface) and the training
pipeline, plus a coordinated artifact roll-forward. Slot-blocked
under FREEZE_v2.1.

**Required user decision.** Schedule a post-freeze sprint to do the
migration. Tracked separately as `post_freeze_v4_proposal.md` §model-
loading-hardening (to be added once the freeze is lifted).

---

## D-F-05 — Telegram alert path referenced in audit scope but not implemented

**Original finding:** F-106
**File:** `packages/monitoring/alerts.py` (would be `_send_telegram`)

The audit checklist mentions Telegram as a fallback alert channel
(parallel to email + Resend). The codebase has no Telegram client and
no `_send_telegram` method.

**Why deferred.** This is a FEATURE REQUEST, not a bug. The current
two-channel setup (SMTP + Resend HTTPS) has documented retry,
spool-and-replay, and dedup. Telegram would add a third independent
channel for the "what if SMTP+Resend are both down" scenario, which
has not yet occurred in production.

**Required user decision.** Either:
1. Confirm Telegram is NOT in scope and remove from the audit
   checklist (one-line edit). Simplest.
2. Confirm Telegram IS in scope, in which case it becomes a post-
   freeze feature (out of scope for any bug-fix sweep).

---

## Status

| ID       | Owner-decision needed | Estimated effort once decided |
| -------- | --------------------- | ----------------------------- |
| D-F-01   | Trade-off accept vs invest | 1-2 weeks (option b) |
| D-F-02   | Pick a policy (4 options) | 1 day (options 1/2), 1 week (option 3), zero (option 4) |
| D-F-03   | Pick a policy (2 options) | <1 hour either way |
| D-F-04   | Schedule sprint       | 1-2 weeks |
| D-F-05   | Confirm scope         | <1 hour (option 1), out of scope (option 2) |

None of these are blockers for the freeze tail. All are unblock-able
on operator decision. Re-raise at the post-freeze planning session
(scheduled for the day after 2026-06-08 freeze-lift).
