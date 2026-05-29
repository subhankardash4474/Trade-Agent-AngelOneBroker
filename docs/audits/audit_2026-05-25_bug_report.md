# Trading Agent — Read-Only Bug Audit

**Date:** 2026-05-25 (NSE holiday; daemon idle, last live cycle 2026-05-22)
**Auditor:** Senior SWE / Algo-Trader, read-only pass
**Scope:** Whole-repo static + dynamic scan, no source edits
**Test posture during audit:** **1,209 unit + 248 integration = 1,457 tests PASSING** on Python 3.14
**Audit baseline:** `logs/audit/2026-05-23/checkpoint_0616.md` — **GREEN**, daemon healthy when last seen.

---

## Executive Summary

This is a mature, defensively-engineered intraday trading system with an unusually rich self-audit and incident-driven test suite. The freeze-v2.1 contract is real and well-honored — every behavior-affecting change is accounted for in `docs/changes_done_*` and `docs/FREEZE_v2.1.md` bypass-slot ledger. The core P&L math, risk gates, SL/peak-giveback machinery, and order-rollback paths have all been hardened against specific past incidents (the "11 EOD emails" 2026-05-13 dedup, the "naked position after entry-SL failure" 2026-05-15 rollback, the "stale cash on restart" 2026-05-04 atomic-persist). The full test suite is green.

That said, the audit surfaced **one outright-broken operator tool, two latent live-mode time bombs that fire the first time the relevant flag flips, one money-loss vulnerability on the order retry path, and one silent data-integrity issue in the sector-exposure cap**. None of these are tripped by today's paper-mode configuration, which is why the test suite (which doesn't exercise the live HTTPS / live-order / WebSocket paths) is still clean. They become real money / safety problems on the day the operator cuts the live switch, enables WebSocket, or experiences a broker-side timeout mid-`placeOrder`.

The highest-leverage fixes (`stop_daemon.py` SyntaxError, `feed_token` plumbing, holiday calendar, SSL default polarity) are all small, isolated, and zero-bypass-slot — they do not touch `trading_agent.py`, `risk_manager.py`, `strategies/`, or `config.yaml` risk/strategy blocks, so they slot cleanly into the freeze without consuming a bypass. The order-idempotency fix is the only one that touches `core/execution.py` and would warrant the slot it consumes.

The system is **safe to keep running in paper mode** as-is. Pre-conditions for cutting live: fix B-1, B-2, B-3 below.

---

## How findings are prioritized

| Severity | Meaning |
|---|---|
| **Critical (C)** | Direct money loss, regulatory breach, or live trading outage possible on existing code paths. |
| **High (H)** | Becomes Critical on the next flag-flip or environment change (live mode, WebSocket on, fresh deploy). |
| **Medium (M)** | Correctness or observability degradation; no immediate money loss. |
| **Low (L)** | Code hygiene, dead code, future-proofing. |

Each issue carries a one-sentence severity rationale.

---

## Findings — ordered by priority

### B-1 — `stop_daemon.py` raises `SyntaxError` on import (CRITICAL — operational)

- **File:line:** `stop_daemon.py:39`
- **Description:** `from __future__ import annotations` is placed AFTER `sys.path` bootstrap statements (lines 33–37). Python requires future-imports at the top of the file; running `python stop_daemon.py` immediately fails with `SyntaxError: from __future__ imports must occur at the beginning of the file`.
- **Reproduction:**
  ```
  python stop_daemon.py
  # → SyntaxError at line 39
  ```
  Confirmed live during this audit.
- **Root cause hypothesis:** When the Phase-1 `sys.path` shim was prepended by `tools/_phase1_move.py` (per the conftest comment), it was inserted before the existing `from __future__` line rather than after it. The fix is to move the future import to line 1.
- **Impact on safety:** This is the operator's **emergency kill switch** for runaway daemons — created specifically because seven daemons once ran in parallel writing to the same DB. While it's broken, the only way to kill a misbehaving daemon is manual `taskkill /pid …`, which is exactly what the script was designed to prevent. **Pure operational risk; cannot lose money directly but extends MTTR on the day the agent goes haywire.**
- **Suggested fix (1-line patch):**
  ```python
  # First non-comment line of stop_daemon.py:
  from __future__ import annotations
  # Then the sys.path bootstrap, then the other imports.
  ```
- **Effort:** 5 minutes.
- **Confidence:** High — reproduced live.
- **Bypass-slot impact:** None (tool only, not `trading_agent.py`).

---

### B-2 — Order-retry has no idempotency key → potential double-fill (CRITICAL — money)

- **File:line:** `packages/core/execution.py:469-571` (`_live_order_with_retry`), called from `place_order` with `retry_attempts: 3` per `config.yaml:636`.
- **Description:** The live order path retries `self._api.placeOrder(order_params)` up to `retry_attempts` times on any exception. The order params dict contains no client-side idempotency key (no `userref`, no client-generated `order_tag` echoed back by the broker). If a network timeout fires *after* AngelOne has already accepted the order, the retry creates a second order. With `retry_attempts: 3` and a transient API hiccup, the agent can open **3× the intended notional at potentially worse prices**.
- **Reproduction (mock harness, no live cred required):**
  ```python
  # tests/unit/test_order_idempotency_hypothesis.py
  from unittest.mock import MagicMock
  api = MagicMock()
  # First call: broker accepts (returns order id) but client gets a Timeout.
  api.placeOrder.side_effect = [
      __import__("requests").exceptions.Timeout("read timeout"),
      "AO_ORDER_42",  # second call: server-side de-dup absent, returns NEW id
      "AO_ORDER_43",
  ]
  from core.execution import ExecutionEngine
  ee = ExecutionEngine({"broker": {"mode": "live"}, "execution": {"retry_attempts": 3}}, smart_api=api)
  ee._live_order_with_retry("RELIANCE", "2885", "BUY", 5, 2840.0, "LIMIT", None, None, "")
  assert api.placeOrder.call_count == 2          # passes today
  # Real bug: the SECOND call submitted a NEW order; nothing checked
  # the broker's order book first to see if the first one actually filled.
  ```
- **Root cause:** AngelOne SmartAPI's `placeOrder` is not idempotent on the server side. The retry assumes "no response = no order placed" which is wrong for any post-commit timeout. There is also no `orderBook()` pre-check before retry.
- **Impact on P&L:** A network blip during a 5-lot RELIANCE order at ₹2,840 can produce 10 lots at ₹2,840 + slippage. On the worst-case third retry, **3× over-fill in a single bad burst**. Position-sizing gates (`max_position_size_pct: 15%`, `max_open_positions: 12`) are computed before the order goes out, so the duplicate fill bypasses them entirely. With paper mode, this is invisible because `_paper_order` is single-shot.
- **Suggested fix (sketch):**
  ```python
  # 1) Include a stable client tag in order params:
  client_ref = f"agent-{uuid.uuid4().hex[:12]}"
  order_params["ordertag"] = client_ref     # AngelOne supports ordertag/correlationid
  # 2) Before retry, query orderBook() and skip if any row matches our client_ref:
  if attempt > 1 and self._client_ref_already_placed(client_ref):
      logger.warning(f"Skipping retry; broker already shows {client_ref} in orderBook")
      return self._build_result_from_order_book(client_ref, ...)
  ```
- **Effort:** ~1 day (incl. unit + integration tests).
- **Confidence:** High — reasoning is mechanical from the code; not directly reproducible without a live cred + simulated timeout.
- **Bypass-slot impact:** **Consumes one slot** (touches `core/execution.py`, which is on the slot-consuming list per `docs/FREEZE_v2.1.md`).

---

### B-3 — SSL verification disabled by default; cloud VMs ship insecure unless env-flag set (CRITICAL — security)

- **File:line:**
  - `main.py:30-46`, `run_daemon.py:47-62`: `_ssl_bypass = os.environ.get("TRADER_DISABLE_SSL_VERIFY", "true")` — **default is `"true"`** (bypass enabled).
  - `packages/core/stock_scanner.py:99-104`: hard-coded `verify=False` in `requests.get`.
  - `packages/core/data_handler.py:18-22`: hard-coded `urllib3.disable_warnings(...)` at module import.
  - `packages/core/data_handler.py:201`: `self._session.verify = False`.
- **Description:** The repository's default posture is to silently trust any TLS certificate. The motivation (corporate-network MITM proxies) is documented and reasonable for a laptop, but on the OCI / cloud VM the operator has to remember to set `TRADER_DISABLE_SSL_VERIFY=false` in the deployment `.env`. **The .env.production.example does flip this — but if an operator copies `.env.example` instead, the cloud VM ships insecure.** A MITM (or compromised upstream CA) can rewrite the AngelOne API responses — including order confirmations and fund balances — without detection.
- **Reproduction:** Inspect any HTTPS call on the daemon and observe no cert validation logs / no `ssl.SSLCertVerificationError` on a tampered cert.
- **Root cause:** Polarity inversion. Secure-by-default would set the default to `false` (verify) and require `TRADER_DISABLE_SSL_VERIFY=true` to opt out for corp networks. The code does the opposite.
- **Impact:** A compromised proxy can silently swap order destinations, falsify "filled @ 2840" responses, or harvest the AngelOne JWT. Bandit also flags `packages/core/stock_scanner.py:102` as **B501 High/High**.
- **Suggested fix:**
  1. Flip the default in `main.py:30` and `run_daemon.py:47` to `"false"`.
  2. Replace hard-coded `verify=False` in `stock_scanner.py:102` with a config-driven flag, defaulting to `True`.
  3. Move the urllib3 warning suppression behind the same flag.
  4. Update `.env.example` to explicitly call out the polarity change.
- **Effort:** ~30 min (low surface) + careful CI confirmation that paper-mode startup still works on a corp laptop.
- **Confidence:** High — code is unambiguous.
- **Bypass-slot impact:** None (entry-point shims + scanner only).

---

### B-4 — `feed_token` captured but never written into `config["broker"]["feed_token"]` → WebSocket auth fails the day someone flips `use_websocket: true` in live mode (HIGH — latent)

- **File:line:**
  - `main.py:67`: `feed_token = api.getfeedToken()` — assigned to local, then discarded. (Ruff `F841` flags this as an unused local.)
  - `packages/core/websocket_client.py:158`: `feed_token = broker_cfg.get("feed_token", "")` — reads from config, which `main.connect_angelone` never updates.
  - `config.yaml:11`: `feed_token: null  # populated at runtime`.
- **Description:** The README's planned cutover path is `data_pipeline.use_websocket: true` once OCI demonstrates a stable WS session. On that day, `WebSocketClient._run_angelone` will instantiate `SmartWebSocketV2(auth_token, api_key, client_id, feed_token="")` and the subscribe call will fail with an auth-token error. The proper broker wrapper (`packages/brokers/angelone.py:136`) does the plumbing right but is currently bypassed by the live `connect_angelone` path in `main.py`.
- **Reproduction:** Set `broker.mode: live`, `data_pipeline.use_websocket: true`; on next startup the WS thread will log an AngelOne auth error and silently die (per the P1 #13 reconnect path the daemon will keep retrying with the same empty token).
- **Suggested fix:**
  ```python
  # main.py, inside connect_angelone, after the existing success branch:
  if session.get("status"):
      feed_token = api.getfeedToken()
      # Surface the runtime token to the config so the WebSocketClient
      # (and any other consumer of broker.feed_token) picks it up.
      config.setdefault("broker", {})["feed_token"] = feed_token
      logger.info(f"AngelOne session established for {broker['client_id']}")
      return api
  ```
- **Effort:** 10 min + a unit test that asserts `config["broker"]["feed_token"]` is populated post-connect.
- **Confidence:** High.
- **Bypass-slot impact:** None (`main.py` is not on the slot-consuming list).

---

### B-5 — `GNFC` mapped to two different sectors in same dict literal; the second value silently wins (HIGH — risk-management)

- **File:line:** `packages/core/market_safety.py:246` (`"GNFC": "Chemicals"`) overwritten by `:311` (`"GNFC": "Agri"`).
- **Description:** Ruff `F601` flags both `PFC` (duplicated as NBFC→NBFC, harmless) and `GNFC` (Chemicals→Agri, harmful). Python dict literals silently keep the LAST value. The 40% sector-exposure cap therefore treats GNFC as Agri-sector exposure. Buying GNFC alongside `COROMANDEL` and `DEEPAKFERT` correctly trips the cap; buying GNFC alongside `TATACHEM`, `RCF`, `FACT` (Chemicals) does NOT count toward Chemicals concentration even though all three names share the same chemicals-cycle exposure.
- **Reproduction:**
  ```python
  >>> from core.market_safety import NSE_SECTOR_MAP
  >>> NSE_SECTOR_MAP["GNFC"]
  'Agri'   # ← the Chemicals entry on line 246 was silently dropped
  ```
- **Root cause:** Manual sector-map edits, no de-duplication test.
- **Impact:** Real-money — sector-concentration risk control mis-fires for GNFC. A correlated chemicals down-day with GNFC + TATACHEM + RCF in the book exceeds the intended diversification. The supersector rollup (`use_supersectors: true`) does not include Chemicals + Agri so the rollup doesn't catch it either.
- **Suggested fix:** Pick the right sector (Chemicals — GNFC is Gujarat Narmada Valley Fertilizers and Chemicals, but its scanner volatility tracks the fertilizer cycle; the operator should decide), keep one entry, add a unit test:
  ```python
  def test_sector_map_no_dup_keys():
      from core import market_safety
      src = open(market_safety.__file__).read()
      # Light-touch check; rebuild the dict from the source repr instead
      # of trusting the runtime dict (which silently dedups).
      import ast, collections
      tree = ast.parse(src)
      for node in ast.walk(tree):
          if isinstance(node, ast.Dict):
              keys = [k.s for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
              dups = [k for k, c in collections.Counter(keys).items() if c > 1]
              assert not dups, f"duplicate sector-map keys: {dups}"
  ```
- **Effort:** 15 min + the unit test.
- **Confidence:** High.
- **Bypass-slot impact:** Touches `packages/core/market_safety.py` (not in the explicit slot-consuming list, but is "core" — recommend treating as **slot-consuming** to be conservative).

---

### B-6 — NSE hardcoded holiday calendar ends 2026-12-25; trading days in 2027 will be treated as live sessions (HIGH — operational)

- **File:line:** `packages/core/data_handler.py:27-38`.
- **Description:** Hardcoded set of holiday dates for 2025 + 2026. On 2027-01-01 the holiday gate fails OPEN — the agent will try to trade on Republic Day 2027 (Tue 2027-01-26 etc.). Today, 2026-05-25 (Buddha Purnima), is correctly listed; that's why the latest checkpoint is from Saturday 2026-05-23.
- **Reproduction:** Set system clock forward to 2027-01-26 and observe the daemon enter market-window and place orders.
- **Suggested fix:** Either (a) auto-fetch from NSE archives once a year (the same archives CSV used by `_fetch_nse_archive_csv`), or (b) at minimum add a startup assertion:
  ```python
  if max(d for d in NSE_HOLIDAYS) < (datetime.now(IST).date() + timedelta(days=180)).isoformat():
      logger.critical("Holiday calendar is stale — please update NSE_HOLIDAYS for the next 12 months.")
  ```
- **Effort:** 1 day for auto-fetch, 10 min for the assertion.
- **Confidence:** High.
- **Bypass-slot impact:** None for the assertion (observability); the auto-fetch path is `packages/core/data_handler.py` and is conservative-slot.

---

### B-7 — `_paper_order` uses unseeded global `random` → backtests are not bitwise reproducible (HIGH — research)

- **File:line:** `packages/core/execution.py:362-385`.
- **Description:** Slippage (`random.uniform(0.0, self.slippage_tolerance)`) and partial-fill simulation (`random.random()`) draw from the global `random` instance with no seed. Two runs of the same backtest produce different P&L tables, which polluted the V2==V3 forensic on 2026-05-25 (the team had to SHA-256 the trade ledgers to prove the bug elsewhere — see `docs/findings_log_2026-05-25.md` §2.3). It also makes the freeze-v2.1 battery comparisons noisy at the per-trade level.
- **Reproduction:**
  ```bash
  python main.py backtest --symbols RELIANCE TCS INFY --interval 5min --export
  python main.py backtest --symbols RELIANCE TCS INFY --interval 5min --export
  diff backtest_run_*/results_RELIANCE.csv   # non-empty
  ```
- **Suggested fix:** Inject a deterministic per-run RNG into `ExecutionEngine`:
  ```python
  def __init__(self, config, smart_api=None, database=None, rng=None):
      import random as _random
      self._rng = rng or _random.Random(config.get("execution", {}).get("rng_seed", 1729))
  # then replace random.uniform → self._rng.uniform, random.random → self._rng.random
  ```
  Pass the seed through `BacktestConfig` so battery harness can record it.
- **Effort:** 1 hour + a reproducibility test that runs the same backtest twice and asserts identical CSVs.
- **Confidence:** High.
- **Bypass-slot impact:** `core/execution.py` is on the slot-consuming list → **one slot** for the runtime change, but the battery-harness propagation is `packages/research/*` which is audit-only.

---

### B-8 — Alert dedup state file is process-unsafe → 2026-05-13 EOD-email storm could recur during overlapping Docker restarts (HIGH — operational)

- **File:line:** `packages/monitoring/alerts.py:321-373` (`_load_dedup_state` / `_record_send`).
- **Description:** Read-modify-write pattern on `logs/.alert_dedup_state.json` with no OS-level file lock. Single-process within one daemon is fine (each `send_alert` call is sequential). But two daemons running simultaneously (the exact scenario the dedup was designed for — Docker `restart: unless-stopped` spawning a new container before the old one fully exited on SIGTERM) can both load state, both miss the dedup, and both send the email. The atomic `os.replace` only protects state-file integrity, not dedup semantics.
- **Reproduction:**
  ```python
  # tests/unit/test_alert_dedup_two_process.py
  from multiprocessing import Process
  def fire():
      from monitoring.alerts import AlertManager
      AlertManager(cfg).send_alert("EOD Summary", "body", level="info")
  p1, p2 = Process(target=fire), Process(target=fire)
  p1.start(); p2.start(); p1.join(); p2.join()
  # Inspect logs/.alert_dedup_state.json + the mocked email backend: expect 1 send, observe 2.
  ```
- **Suggested fix:** Add an OS file lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows; or use `portalocker` which is a small cross-platform dep) around the load→modify→save block. Or move the dedup state from a file to a SQLite UPSERT with `UNIQUE(fingerprint) ON CONFLICT IGNORE` — single SQL statement is atomic.
- **Effort:** 2 hours.
- **Confidence:** High — race is mechanical.
- **Bypass-slot impact:** None (`monitoring/` is not on the slot list).

---

### B-9 — `DataHandler._cache` is unbounded; long-lived daemon leaks RAM across trading days (MEDIUM — performance)

- **File:line:** `packages/core/data_handler.py:323` (`self._cache: Dict[str, pd.DataFrame] = {}`); inserts at `:375`.
- **Description:** Cache key is `f"{symbol}_{interval}_{start_date.date()}_{end_date.date()}"`. Each new trading day produces new entries with no eviction. A daemon running for a quarter accumulates ~60 × 300 symbols × N intervals of cached DataFrames. With ~15 KB per DataFrame this is ~270 MB after 90 days. On the 1 GB OCI Ampere VM this matters.
- **Reproduction:** Run the agent for a week, monitor `psutil.Process(pid).memory_info().rss` via `tools/audit_checkpoint`; observe monotonic growth.
- **Suggested fix:** Use `functools.lru_cache(maxsize=512)` semantics via `cachetools.LRUCache` or a hand-rolled deque-based eviction:
  ```python
  from collections import OrderedDict
  self._cache: OrderedDict[str, pd.DataFrame] = OrderedDict()
  self._cache_max = 512
  # In the insert path:
  self._cache[cache_key] = df
  self._cache.move_to_end(cache_key)
  while len(self._cache) > self._cache_max:
      self._cache.popitem(last=False)
  ```
- **Effort:** 30 min + a unit test for the eviction.
- **Confidence:** High.
- **Bypass-slot impact:** None (DataHandler is `core/` but cache eviction is observability-equivalent).

---

### B-10 — Live-mode SMTP login uses display-string sender as username → SMTP path is broken if anyone switches `provider: smtp` (MEDIUM)

- **File:line:** `packages/monitoring/alerts.py:497` — `server.login(self._email_cfg["sender"], self._email_cfg["password"])`.
- **Description:** Config ships with `sender: "Trading Agent <onboarding@resend.dev>"` (RFC 5322 display format). SMTP servers (Gmail, Outlook365, AWS SES) require the plain email address as the auth username; passing the display-formatted string yields a 535 auth failure. The default `provider: resend` masks this — the day an operator falls back to SMTP because Resend is rate-limiting them, the EOD email stops working without an alert (since alerts ARE email).
- **Suggested fix:** Extract the addr-spec via `email.utils.parseaddr` before login:
  ```python
  from email.utils import parseaddr
  smtp_user = parseaddr(self._email_cfg["sender"])[1] or self._email_cfg["sender"]
  server.login(smtp_user, self._email_cfg["password"])
  ```
- **Effort:** 10 min.
- **Confidence:** Medium-High (assumption: standard SMTP behaviour; some servers do tolerate display strings).
- **Bypass-slot impact:** None.

---

### B-11 — `run_daemon.py._write_idle_heartbeat` reports `cash: 0` always (MEDIUM — observability)

- **File:line:** `run_daemon.py:119` — `cash = float(config.get("initial_capital", 0.0))`.
- **Description:** The actual config key is `capital.initial_balance` (per `config.yaml:32` and the README). `config.get("initial_capital")` is always `None` → `cash` always 0. `health.json` is written every minute during off-hours with `cash: 0.0`, which is then read by `tools/health_check.py` and the watchdog. False signal.
- **Suggested fix:** `cash = float((config.get("capital") or {}).get("initial_balance", 0.0))`.
- **Effort:** 2 min.
- **Confidence:** High.
- **Bypass-slot impact:** None.

---

### B-12 — `secrets.apply_env_to_config` overwrites real YAML values with `.env.example` placeholders that don't look like placeholders (MEDIUM)

- **File:line:** `packages/core/secrets.py:106-113` + `.env.example:15`.
- **Description:** `.env.example` ships with `RESEND_API_KEY=re_xxxxxxxxxxxxxxxxxx`. If an operator copies it to `.env` without editing, `load_dotenv` sets the env var; `_is_placeholder("re_xxxxxxxxxxxxxxxxxx")` returns `False` (doesn't start with `YOUR_`); the env-override branch unconditionally writes the bogus value into `config["monitoring"]["alerts"]["email"]["resend_api_key"]`. The resulting alert flow hits Resend with `re_xxxxxxxxxxxxxxxxxx`, gets 401, and spools the alert to `logs/failed_alerts/…` — the operator sees no email and no obvious cause.
- **Suggested fix:** Extend `_is_placeholder` to recognize the `re_xxx`/`re_xx…` pattern, AND ship `.env.example` with the key empty (matching the other AngelOne keys which are empty):
  ```python
  def _is_placeholder(v):
      if v is None or v == "":
          return True
      if isinstance(v, str):
          if v.startswith("YOUR_") or v in {"null", "None"}:
              return True
          if v.startswith("re_") and set(v[3:]) <= {"x", "X"}:  # re_xxxx pattern
              return True
      return False
  ```
- **Effort:** 10 min + 1 unit test.
- **Confidence:** High.
- **Bypass-slot impact:** None.

---

### B-13 — `_periodic_cleanup` directly mutates `tick_aggregator._history` (MEDIUM — encapsulation)

- **File:line:** `trading_agent.py:4810-4813`.
- **Description:** Reaches into a private attribute and re-binds the value. Works today, but a refactor of `TickAggregator` that changes its internal storage will break the cleanup path silently (no exception, just an unreferenced rebind on a stale dict).
- **Suggested fix:** Add a `TickAggregator.cap_history(max_per_symbol: int)` public method and call it from the cleanup path.
- **Effort:** 30 min.
- **Bypass-slot impact:** Touches `trading_agent.py` → **slot-consuming**. Defer.

---

### B-14 — `Portfolio._maybe_persist_trade` opens its own raw `sqlite3` connection (MEDIUM — architecture)

- **File:line:** `packages/core/portfolio.py:615-628`.
- **Description:** Bypasses the `Database` class to do an existence-check INSERT. Today this is safe because `Database._conn()` itself opens a per-call connection. But if `Database` ever moves to a pooled / WAL-checkpoint-aware connection model the bypass diverges silently. The check is also racy under concurrent writers (the `SELECT` and `store_trade` are in two separate connections).
- **Suggested fix:** Add `Database.store_trade_if_absent(record_dict)` that does the existence check + insert in one `WITH` block, then have `_maybe_persist_trade` call it.
- **Effort:** 1 hour + tests.
- **Bypass-slot impact:** None.

---

### B-15 — `profit_factor = float("inf")` when `gross_loss == 0` is not JSON-safe (MEDIUM — serialization)

- **File:line:** `packages/core/portfolio.py:756`.
- **Description:** Strict JSON encoders reject `Infinity`. The team already fixed numpy types via `_json_default` in `database.py`, but the `inf` case flows into the alerts pipeline (`AlertManager.send_daily_report` reads `metrics["profit_factor"]`) and into anything that JSON-serializes `get_performance_metrics()` output for cloud-sync.
- **Reproduction:**
  ```python
  import json
  json.dumps({"pf": float("inf")})   # raises ValueError in strict mode? No, Python's json emits "Infinity" — invalid JSON per RFC 8259
  json.loads('{"pf": Infinity}')      # invalid JSON
  ```
- **Suggested fix:** Cap to a sentinel (e.g. 999.99) or to `None` and surface "infinite — no losing trades" in the rendering layer:
  ```python
  "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
  ```
- **Effort:** 15 min.
- **Bypass-slot impact:** None (Portfolio metrics not on the slot list).

---

### B-16 — `RiskManager.require_nifty_above_200ema` default in code is `True`, config ships `False` (MEDIUM — drift)

- **File:line:** `packages/core/risk_manager.py:357` (`self.require_nifty_above_200ema: bool = risk_cfg.get(..., True)`); `config.yaml:417` (`require_nifty_above_200ema: false`).
- **Description:** Defaults disagree. If a fresh deploy ships without the full config.yaml (e.g. a Docker image with a stripped config), the agent silently blocks all longs. Production sees this only because `config.yaml` overrides it.
- **Suggested fix:** Pick one canonical default (per recent batteries, longs are valuable — default should be `False`) and align both code and config:
  ```python
  self.require_nifty_above_200ema: bool = risk_cfg.get("require_nifty_above_200ema", False)
  ```
- **Effort:** 5 min.
- **Bypass-slot impact:** Touches `risk_manager.py` → **slot-consuming**. Defer to next freeze window; meanwhile pin in operator deploy checklist.

---

### B-17 — `F821 Undefined name Position` in forward type-hint (LOW — cosmetic)

- **File:line:** `trading_agent.py:3840` (`pos: "Position"`).
- **Description:** The string-annotation is purely cosmetic; Python does not import `Position`. `mypy --strict` will fail. No runtime impact.
- **Suggested fix:** Add `from core.portfolio import Position` at top of `trading_agent.py` (or `TYPE_CHECKING` guard) and drop the quotes, OR change the annotation to `pos: "core.portfolio.Position"`.
- **Effort:** 2 min.

---

### B-18 — `urllib.request.urlopen` on AB1050 diagnosis path uses default SSL context (LOW — security)

- **File:line:** `packages/brokers/angelone.py:285-287`.
- **Description:** Bandit B310. Only runs on the IP-whitelist error path; uses urllib instead of `requests`. With the SSL global monkey-patch from `main.py` it will silently succeed against any cert. Minor.
- **Suggested fix:** Replace with `requests.get("https://api.ipify.org?format=json", timeout=5)` so the (fixed) SSL-verify config is honored.
- **Effort:** 5 min.

---

### B-19 — Pickle / torch.load with `weights_only=False` allow arbitrary code execution if model files are tampered with (LOW — security)

- **File:lines:** `packages/strategies/xgboost_classifier.py:152`, `packages/strategies/lstm_model.py:63, 74`, `packages/research/battery.py:527`.
- **Description:** Bandit B301 / B614. Local files only — requires write access to `models/` for any exploitation. On the cloud VM, anyone with shell access can already run anything; on the laptop, the threat is a malicious training pipeline that smuggles a backdoored .pkl.
- **Suggested fix:** Where possible switch to safetensors / xgboost native JSON (`Booster.save_model("...json")`); for the pickle paths add a SHA-256 manifest in the repo and verify on load.
- **Effort:** Half day per artefact format.

---

### B-20 — `B904` lost exception context in `run_battery_queue.py:81, :153` (LOW)

- **File:line:** `tools/run_battery_queue.py:81, :153`.
- **Description:** `raise X` inside `except` clause without `from err`. Debuggability nit.
- **Suggested fix:** Mechanical: `raise X(...) from err`.
- **Effort:** 2 min.

---

### B-21 — Code-hygiene noise (LOW)

- 119 `F401` unused imports, 18 `F841` unused locals, 60 `E402` imports-not-at-top, 47 `SIM105` suppressible-exception patterns, 10 `E741` ambiguous variable names (most in `tools/audit_checkpoint.py`).
- Full ruff statistics in `logs/ruff_report.txt`.
- **Suggested fix:** `ruff check --fix --select F401,F541,UP,I,SIM118` is mostly safe; gate via PR review.

---

## Tests to add (concrete assertions)

| ID | Test name | Asserts |
|---|---|---|
| T-1 | `tests/unit/test_stop_daemon_imports.py` | `importlib.import_module("stop_daemon")` does not raise; `python -c "import stop_daemon"` exits 0. |
| T-2 | `tests/integration/test_order_retry_idempotency.py` | When `_live_order_with_retry` is given a broker that returns a Timeout on the first call and a real order id on the second, the agent does NOT submit a second `placeOrder`; it consults the orderBook for the client-ref instead. |
| T-3 | `tests/unit/test_ssl_default_secure.py` | `TRADER_DISABLE_SSL_VERIFY` unset ⇒ `ssl.create_default_context().verify_mode == ssl.CERT_REQUIRED`. |
| T-4 | `tests/unit/test_feed_token_plumbed_to_config.py` | After `connect_angelone(cfg)` with a mocked SmartAPI returning a feed token, `cfg["broker"]["feed_token"]` equals the mock return value. |
| T-5 | `tests/unit/test_sector_map_no_duplicate_keys.py` | Parse `market_safety.py` AST; assert no duplicate keys in any string-keyed dict literal (catches GNFC/PFC class of bug). |
| T-6 | `tests/unit/test_holiday_calendar_freshness.py` | Assert `max(NSE_HOLIDAYS)` is ≥ `date.today() + 180 days`; emit a clear "update the calendar" message if not. |
| T-7 | `tests/unit/test_paper_order_seeded_random.py` | Run two `ExecutionEngine._paper_order(...)` with same input + same seed; assert identical filled_price and filled_quantity. |
| T-8 | `tests/integration/test_alert_dedup_multiprocess.py` | Spawn two child processes that each call `send_alert("EOD","body")`. Mock the underlying `requests.post`; assert it is called exactly once. |
| T-9 | `tests/unit/test_data_handler_cache_bounded.py` | Insert >`_cache_max + 1` distinct keys; assert `len(handler._cache) <= _cache_max`. |
| T-10 | `tests/unit/test_idle_heartbeat_cash_value.py` | Construct a config with `capital.initial_balance: 100000`; call `_write_idle_heartbeat`; read `logs/health.json`; assert `cash == 100000.0`. |
| T-11 | `tests/unit/test_secrets_resend_xxx_placeholder.py` | `_is_placeholder("re_xxxxxxxxxxxxxxxxxx")` returns True. |
| T-12 | `tests/unit/test_profit_factor_json_safe.py` | `json.dumps(Portfolio(...).get_performance_metrics())` does not raise; with zero losses, `profit_factor` is None or a finite number. |

---

## Quick wins (in order of risk-reduction-per-minute)

1. **Fix `stop_daemon.py` SyntaxError** (B-1) — 5 min, restores the kill switch.
2. **Plumb `feed_token` into config in `main.connect_angelone`** (B-4) — 10 min, defuses the WebSocket time-bomb.
3. **Fix `_write_idle_heartbeat` cash key** (B-11) — 2 min, restores accurate healthcheck.
4. **De-duplicate `GNFC` in sector map** (B-5) — 15 min + test, restores sector-cap correctness.
5. **Flip SSL-verify default to `false`-means-bypass (i.e. `verify`-by-default)** (B-3) — 30 min, fixes the largest security regression.

All five together: under 90 minutes of work, zero bypass slots consumed, and the freeze contract is untouched.

---

## Appendix — raw artefacts produced during this audit

- `logs/ruff_report.txt` — full ruff statistics.
- `logs/ruff_critical.txt` — filtered to bug-prone rules (F821, F601, B904, SIM115, F404, B009, E741, F841, B007).
- `logs/bandit_report.txt` — bandit `-ll` (medium+).
- `logs/pytest_unit_report.txt` — 1,209 unit tests passing (23.5 s).
- `logs/pytest_integration_report.txt` — 248 integration tests passing (39.0 s).

Test environment: Python 3.14.0 (Windows). All test suites green.

---

*Read-only audit. No source files modified during this pass; only `logs/*.txt` and `docs/audit_2026-05-25/*` were written. Recommend a follow-up "fix-it" PR scoped to the **Quick wins** list above for the next freeze-compatible commit.*
