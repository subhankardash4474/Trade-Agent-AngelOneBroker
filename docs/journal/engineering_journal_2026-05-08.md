# Engineering Journal — 2026-05-08

## Morning session (08:30 - 09:35 IST)

### AngelOne SmartAPI integration — Phase 1 complete

The user supplied AngelOne API credentials this morning and asked for a clean
broker abstraction. Built `brokers/` as a modular layer (additive only —
the running daemon at PID 8532 was not touched).

#### New module: brokers/

| File              | Purpose                                                      |
|-------------------|--------------------------------------------------------------|
| `base.py`       | Abstract Broker interface + canonical funds/profile shapes.  |
| `angelone.py`   | SmartAPI implementation: TOTP login, token-age tracking,     |
|                   | funded-balance preflight, IP-whitelist diagnosis hints.      |
| `paper.py`      | No-op paper broker; live methods raise loudly to catch       |
|                   | wiring errors instead of silently no-op'ing.                 |
| `__init__.py`   | `get_broker(config)` factory.                              |

#### Verification
- `tools/angelone_smoke.py` — full read-only end-to-end test against
  real AngelOne servers. All 6 probes pass (login, profile, funds,
  positions, orders, LTP).
- LTP probe returned SBIN-EQ = Rs 1089.00 (live market data flowing).
- `tests/test_brokers.py` — 19 new unit tests, all passing.
- Full suite: 639 tests passed.

#### Account state on AngelOne
- Profile: SUBHANKAR DASH / AACH543759 / NSE Cash + BSE Cash.
- **available_cash = Rs 0** — must be funded before May 11 cutover.
- F&O segment: NOT enabled (optional, defer).

### Kite reference cleanup (per user request)

Removed redundant Kite code now that AngelOne is the canonical broker:

| Action                                           | File                       |
|--------------------------------------------------|----------------------------|
| Deleted (unused, standalone)                      | `kite_login.py`          |
| ENV_MAP: removed KITE_*                           | `core/secrets.py`        |
| Removed KITE_* lines (canonical broker only)      | `.env.example`           |
| Removed dead KITE_* entries                       | `.env` (user-owned)      |
| Updated broker block: name=angelone, AngelOne docs| `config.yaml`            |
| `get_broker()` rejects `name: kite` loudly    | `brokers/__init__.py`    |
| Test re-pointed to ANGELONE_API_KEY               | `tests/test_audit_fixes.py`|

NOT touched today (deferred to weekend):
- `main.py` and `run_daemon.py` still have a duplicated
  `connect_angelone()` helper. Will be replaced with
  `brokers.get_broker()` during the Phase A modular refactor on
  Saturday/Sunday so the running daemon doesn't need a mid-week restart.

### Cloud-readiness architecture decision

The user proposed splitting broker / data / DB / email into separate pods.
Pushed back — for an intraday agent the latency tax of cross-pod IPC
(50µs - 5ms per call × ~1,200 decisions/cycle = 1.2s overhead/cycle) wipes
out the benefits. Recommended **modular monolith** instead: clean module
boundaries inside one process, with externalized state (RDS/Secrets
Manager/SNS) running on a single VM. Same cloud-deployable benefits
without paying for IPC on every signal.

Phase A on the weekend will:
1. Formalize `brokers/`, `data/`, `persistence/` module boundaries.
2. Refactor `main.py`/`run_daemon.py`/`core/execution.py` to use the
   `Broker` interface.
3. Dockerize.
4. Add health probe endpoints.

NOT doing:
- Kubernetes / pods / service mesh.
- Polyglot services.

### Live-run observations (first 22 minutes)

C2+C6 winners from overnight battery are visibly filtering signals:
- `[OPENING-LOCKOUT]` blocked NUVOCO SELL (conf 0.747) at 09:16:26.
- `[xgboost_classifier] SELL blocked` for CEMPRO, APTUS, JAINREC due to
  trend filter (>5% above 50d SMA).
- `[mean_reversion] SELL blocked` for APTUS, JAINREC same reason.
- 6 high-confidence shorts filtered in the first minute alone.

Yesterday's MEESHO scenario (against-trend SHORT at 09:15) would now be
blocked by both the opening lockout AND the trend filter — exactly the
defense layers the battery told us to add.

### Files status (end of session)

- New:    `brokers/{base,angelone,paper,__init__}.py`,
          `tools/angelone_smoke.py`,
          `tests/test_brokers.py`,
          `logs/engineering_journal_2026-05-08.md` (this file).
- Edited: `.env`, `.env.example`, `core/secrets.py`, `config.yaml`,
          `tests/test_audit_fixes.py`.
- Deleted: `kite_login.py`.
- Tests: 639 passed.
- Daemon: PID 8532 healthy, 71 min uptime, no restart required.
