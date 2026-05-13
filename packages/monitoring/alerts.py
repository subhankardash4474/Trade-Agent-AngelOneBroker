"""
Alert Manager Module.
Sends notifications via email for trade executions, risk breaches,
drawdown warnings, and daily performance summaries.

Network resilience:
  Email sends retry on transient network errors (DNS / connection / 5xx)
  with short exponential backoff. If retries exhaust we spool the alert
  to `logs/failed_alerts/<ts>_<title>.json` so the daemon (or a manual
  CLI) can replay it later. This is what saves us when a VPN flake
  takes down DNS at EOD.
"""

import hashlib
import json
import os
import re
import smtplib
import time
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import pytz
import requests
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")

# Persistent dedup state file. Survives daemon restarts so a crash-loop
# during EOD (the 2026-05-13 incident where 11 identical EOD Summary
# emails went out in 30 minutes because the daemon kept getting
# SIGTERM'd by Docker's healthcheck) cannot resend the same alert.
# The file is a flat newline-separated list of "<unix_ts>:<hash>:<subject>".
_DEDUP_STATE_FILE = Path("logs") / ".alert_dedup_state.json"

# Default TTL window. Anything within this many minutes of a previous
# send with the same content hash is dropped. 60 minutes is generous
# enough to absorb a full Docker healthcheck crash-loop (180s grace +
# restart * a few cycles) while still letting genuinely repeated alerts
# (e.g. "trade rejected" multiple times in a day for different reasons,
# which hash differently because the body changes) flow through. Tuned
# down in unit tests via the config knob.
_DEDUP_DEFAULT_TTL_MINUTES = 60

# Network errors we should retry. Anything else (e.g. 401 invalid api key)
# is a permanent failure and skipping retries saves time.
_RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Backoff schedule in seconds. 4 attempts total: 0s, 2s, 8s, 24s.
_BACKOFF_DELAYS = [0, 2, 8, 24]

_FAILED_ALERTS_DIR = Path("logs") / "failed_alerts"


def _sanitize_filename(s: str) -> str:
    """Make a string safe for filenames across OSes."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")
    return s[:80] or "alert"


def _spool_failed_alert(payload: dict, reason: str) -> Optional[Path]:
    """Persist a failed alert payload to disk so it can be replayed later.
    Returns the path written, or None if spool itself failed.
    """
    try:
        _FAILED_ALERTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(IST).strftime("%Y-%m-%dT%H%M%S")
        title_slug = _sanitize_filename(payload.get("subject", "alert"))
        path = _FAILED_ALERTS_DIR / f"{ts}_{title_slug}_{uuid.uuid4().hex[:6]}.json"
        path.write_text(
            json.dumps(
                {**payload, "spooled_at": ts, "reason": reason},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        logger.warning(f"[ALERT-SPOOL] {payload.get('subject')} -> {path.name} ({reason})")
        return path
    except Exception as e:
        logger.error(f"[ALERT-SPOOL] failed to write spool file: {e}")
        return None


class AlertManager:
    """
    Alert manager.
    Supports: Email (Resend or SMTP).
    """

    def __init__(self, config: dict):
        mon_cfg = config.get("monitoring", {}).get("alerts", {})
        self.enabled = mon_cfg.get("enabled", False)

        self._email_cfg = mon_cfg.get("email", {})
        self._email_enabled = self._email_cfg.get("enabled", False) and self.enabled

        # Persistent dedup window. Disabled by setting ttl_minutes <= 0 in
        # config -- useful for tests / for the rare operator who actually
        # WANTS every duplicate sent (e.g. live debugging a flapping
        # service).
        dedup_cfg = mon_cfg.get("dedup", {}) or {}
        self._dedup_ttl_minutes: int = int(
            dedup_cfg.get("ttl_minutes", _DEDUP_DEFAULT_TTL_MINUTES)
        )
        # Override state-file location (mostly for tests; production reads
        # the module-level default which sits next to the daemon's other
        # ephemeral state under ``logs/``).
        state_path = dedup_cfg.get("state_path")
        self._dedup_state_path: Path = Path(state_path) if state_path else _DEDUP_STATE_FILE

    # ── Persistent dedup helpers ─────────────────────────────────
    @staticmethod
    def _alert_fingerprint(title: str, body: str) -> str:
        """Stable hash of the alert payload. SHA1 hex (truncated) is plenty
        for dedup -- collisions across distinct messages would have to be
        deliberate, and our keyspace is tiny (~50 alert types in a day)."""
        h = hashlib.sha1()
        h.update(title.encode("utf-8", errors="replace"))
        h.update(b"\x1f")  # unit separator -- can't appear in real text
        h.update(body.encode("utf-8", errors="replace"))
        return h.hexdigest()[:16]

    def _load_dedup_state(self) -> dict:
        """Return ``{fingerprint: unix_ts}`` of the most recent send for
        each known alert. Missing/corrupt state file -> empty dict (we
        prefer "send anyway" over "permanently swallow alerts" when the
        state is unreadable, on the principle that an unexpected
        duplicate is safer than a missed alert)."""
        try:
            if not self._dedup_state_path.exists():
                return {}
            raw = json.loads(self._dedup_state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            return {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}
        except Exception as e:
            logger.debug(f"[DEDUP] state load failed (treating as empty): {e}")
            return {}

    def _save_dedup_state(self, state: dict) -> None:
        try:
            self._dedup_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._dedup_state_path.with_suffix(self._dedup_state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, self._dedup_state_path)
        except Exception as e:
            logger.warning(f"[DEDUP] state save failed (alerts may re-fire on next restart): {e}")

    def _is_recent_duplicate(self, fingerprint: str) -> Optional[int]:
        """If this fingerprint was sent within the TTL window, return
        the age in seconds. Otherwise return None.
        """
        if self._dedup_ttl_minutes <= 0:
            return None
        state = self._load_dedup_state()
        prev_ts = state.get(fingerprint)
        if prev_ts is None:
            return None
        age = int(time.time()) - int(prev_ts)
        if age <= self._dedup_ttl_minutes * 60:
            return age
        return None

    def _record_send(self, fingerprint: str, title: str) -> None:
        """Stamp this fingerprint with `now` and GC expired entries.
        We don't keep an unbounded log -- entries past 24h are dropped to
        cap the file size (a busy day might emit 200 alerts)."""
        if self._dedup_ttl_minutes <= 0:
            return
        state = self._load_dedup_state()
        now = int(time.time())
        cutoff = now - max(24 * 3600, self._dedup_ttl_minutes * 60 * 2)
        state = {fp: ts for fp, ts in state.items() if ts >= cutoff}
        state[fingerprint] = now
        self._save_dedup_state(state)

    def send_alert(self, title: str, message: str, level: str = "info"):
        """Send an alert to configured channels.

        Persistent dedup: if the same (title, message) pair was already
        sent within ``monitoring.alerts.dedup.ttl_minutes`` (default 60),
        the duplicate is logged as a SUPPRESS and silently dropped. This
        survives daemon restarts via ``logs/.alert_dedup_state.json``,
        which is what prevents EOD-time crash loops from spraying the
        operator with 11 identical emails (see 2026-05-13 incident).
        """
        timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        full_message = f"[{timestamp}] [{level.upper()}] {title}\n{message}"

        log_fn = {"info": logger.info, "warning": logger.warning, "error": logger.error}.get(level, logger.info)
        log_fn(f"ALERT: {title} - {message}")

        if not self.enabled:
            return

        # Dedup on (title, message) -- intentionally NOT including the
        # leading timestamp from full_message, otherwise every alert
        # would have a unique fingerprint and the dedup would never fire.
        fingerprint = self._alert_fingerprint(title, message)
        recent_age = self._is_recent_duplicate(fingerprint)
        if recent_age is not None:
            logger.info(
                f"[ALERT-SUPPRESSED] '{title}' is a duplicate of one sent "
                f"{recent_age}s ago (ttl {self._dedup_ttl_minutes} min). "
                f"Skipping email send."
            )
            return

        if self._email_enabled:
            self._send_email(title, full_message, level)

        self._record_send(fingerprint, title)

    def send_trade_alert(
        self,
        trade_type: str,
        symbol: str,
        quantity: int,
        price: float,
        strategy: str,
        pnl: Optional[float] = None,
    ):
        """Specialized alert for trade execution."""
        emoji = {"BUY": "BUY", "SELL": "SELL"}.get(trade_type, "INFO")
        msg = f"{emoji} {trade_type} {quantity} x {symbol} @ INR {price:.2f}\nStrategy: {strategy}"
        if pnl is not None:
            pnl_flag = "PROFIT" if pnl >= 0 else "LOSS"
            msg += f"\nP&L: {pnl_flag} INR {pnl:+.2f}"
        self.send_alert(f"Trade: {trade_type} {symbol}", msg, level="info")

    def send_risk_alert(self, reason: str, details: str):
        """Specialized alert for risk breaches."""
        self.send_alert(f"Risk Alert: {reason}", details, level="warning")

    def send_daily_report(self, portfolio_summary: dict, risk_summary: dict):
        """Send end-of-day performance report."""
        timestamp = datetime.now(IST).strftime("%Y-%m-%d")
        metrics = portfolio_summary.get("metrics", {})

        lines = [
            f"Daily Report - {timestamp}",
            "-" * 30,
            f"Portfolio:  INR {portfolio_summary.get('total_value', 0):,.2f}",
            f"Cash:       INR {portfolio_summary.get('cash', 0):,.2f}",
            f"Day P&L:    INR {risk_summary.get('daily_pnl', 0):+,.2f}",
            f"Week P&L:   INR {risk_summary.get('weekly_pnl', 0):+,.2f}",
            f"Trades:     {risk_summary.get('daily_trades', 0)}",
            f"Drawdown:   {risk_summary.get('drawdown_pct', 0):.2f}%",
            "",
            f"Win Rate:   {metrics.get('win_rate', 0):.1f}%",
            f"Sharpe:     {metrics.get('sharpe_ratio', 0):.2f}",
            f"Profit Factor: {metrics.get('profit_factor', 0):.2f}",
            f"Total P&L:  INR {metrics.get('total_pnl', 0):+,.2f}",
        ]
        report = "\n".join(lines)
        self.send_alert("Daily Report", report, level="info")

    def _send_email(self, subject: str, body: str, level: str):
        # Prefix the IST date to every subject so alerts sort/filter by day
        # in the inbox. Format: "[Trading Agent 2026-04-29] Daily Report".
        today = datetime.now(IST).strftime("%Y-%m-%d")
        dated_subject = f"{today} | {subject}"
        provider = self._email_cfg.get("provider", "smtp").lower()
        if provider == "resend":
            self._send_email_resend(dated_subject, body)
            return
        self._send_email_smtp(dated_subject, body, level)

    def _send_email_smtp(self, subject: str, body: str, level: str, *, spool_on_fail: bool = True) -> bool:
        """SMTP send with retry + disk-spool fallback (same semantics as Resend path)."""
        spool_payload = {
            "provider": "smtp",
            "subject": subject,
            "body": body,
            "level": level,
        }
        last_reason = "unknown"
        for attempt, delay in enumerate(_BACKOFF_DELAYS, start=1):
            if delay:
                time.sleep(delay)
            try:
                msg = MIMEMultipart()
                msg["From"] = self._email_cfg["sender"]
                msg["To"] = self._email_cfg["recipient"]
                msg["Subject"] = f"[Trading Agent] {subject}"

                color = {"info": "#2196F3", "warning": "#FF9800", "error": "#F44336"}.get(level, "#333")
                html = f"""
                <html><body>
                <div style="border-left: 4px solid {color}; padding: 12px; font-family: monospace;">
                <pre>{body}</pre>
                </div>
                </body></html>
                """
                msg.attach(MIMEText(html, "html"))

                with smtplib.SMTP(self._email_cfg["smtp_server"], self._email_cfg["smtp_port"], timeout=15) as server:
                    server.starttls()
                    server.login(self._email_cfg["sender"], self._email_cfg["password"])
                    server.send_message(msg)
                if attempt > 1:
                    logger.info(f"Email alert sent on attempt {attempt}: {subject}")
                else:
                    logger.debug(f"Email alert sent: {subject}")
                return True
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError) as e:
                last_reason = f"network: {type(e).__name__}: {e!s:.150}"
                logger.warning(
                    f"SMTP transient error, attempt {attempt}/{len(_BACKOFF_DELAYS)}: {type(e).__name__}"
                )
            except Exception as e:
                last_reason = f"exception: {type(e).__name__}: {e!s:.150}"
                logger.error(f"Email alert failed: {e}")
                break

        if spool_on_fail:
            _spool_failed_alert(spool_payload, last_reason)
        return False

    # ── Failed-alert spool management ────────────────────────
    def drain_failed_alerts(self, *, max_per_run: int = 50) -> dict:
        """Replay any spooled alerts (e.g. ones that failed during a VPN/DNS
        outage). Successful replays delete the spool file; failures leave it
        in place to be retried next run. Safe to call at daemon boot.

        Returns: {"sent": N, "failed": N, "skipped": N}.
        """
        if not _FAILED_ALERTS_DIR.exists():
            return {"sent": 0, "failed": 0, "skipped": 0}

        sent = failed = skipped = 0
        for path in sorted(_FAILED_ALERTS_DIR.glob("*.json"))[:max_per_run]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[ALERT-SPOOL] cannot read {path.name}: {e}")
                skipped += 1
                continue

            subject = payload.get("subject", "")
            body = payload.get("body", "")
            provider = payload.get("provider", "resend")
            level = payload.get("level", "info")

            ok = False
            try:
                if provider == "resend":
                    ok = self._send_email_resend(subject, body, spool_on_fail=False)
                else:
                    ok = self._send_email_smtp(subject, body, level, spool_on_fail=False)
            except Exception as e:
                logger.error(f"[ALERT-SPOOL] replay raised for {path.name}: {e}")
                ok = False

            if ok:
                try:
                    path.unlink()
                except Exception:
                    pass
                sent += 1
                logger.info(f"[ALERT-SPOOL] replayed and removed: {path.name}")
            else:
                failed += 1
                logger.warning(f"[ALERT-SPOOL] replay still failing, kept on disk: {path.name}")

        if sent or failed:
            logger.info(f"[ALERT-SPOOL] drain summary: sent={sent} failed={failed} skipped={skipped}")
        return {"sent": sent, "failed": failed, "skipped": skipped}

    def _send_email_resend(self, subject: str, body: str, *, spool_on_fail: bool = True) -> bool:
        """Send email through Resend API with retry-on-network-error and
        disk-spool fallback. Returns True on success, False on terminal
        failure (which also writes a JSON spool file under logs/failed_alerts/
        unless spool_on_fail=False, used by the drain path).
        """
        api_key = self._email_cfg.get("resend_api_key", "")
        sender = self._email_cfg.get("sender", "")
        recipient = self._email_cfg.get("recipient", "")
        if not api_key or not sender or not recipient:
            logger.warning("Resend email not configured: missing api_key/sender/recipient")
            return False

        full_subject = f"[Trading Agent] {subject}"
        payload = {
            "from": sender,
            "to": [recipient],
            "subject": full_subject,
            "html": f"<pre>{body}</pre>",
        }
        spool_payload = {
            "provider": "resend",
            "subject": subject,
            "body": body,
        }

        last_reason = "unknown"
        for attempt, delay in enumerate(_BACKOFF_DELAYS, start=1):
            if delay:
                time.sleep(delay)
            try:
                # verify=False to avoid corporate proxy self-signed cert retries.
                resp = requests.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    data=json.dumps(payload),
                    timeout=10,
                    verify=False,
                )
                if resp.status_code in (200, 201):
                    if attempt > 1:
                        logger.info(
                            f"Resend email alert sent on attempt {attempt}: {subject}"
                        )
                    else:
                        logger.debug(f"Resend email alert sent: {subject}")
                    return True
                # Non-retryable HTTP status (auth/validation) — bail immediately
                if resp.status_code not in _RETRYABLE_STATUS:
                    last_reason = f"http_{resp.status_code}: {resp.text[:200]}"
                    logger.error(f"Resend email failed (non-retryable {resp.status_code}): {resp.text}")
                    break
                last_reason = f"http_{resp.status_code}_retryable"
                logger.warning(
                    f"Resend email transient {resp.status_code}, attempt {attempt}/{len(_BACKOFF_DELAYS)}"
                )
            except _RETRYABLE_EXCEPTIONS as e:
                last_reason = f"network: {type(e).__name__}: {e!s:.150}"
                logger.warning(
                    f"Resend email network error, attempt {attempt}/{len(_BACKOFF_DELAYS)}: {type(e).__name__}"
                )
            except Exception as e:
                last_reason = f"exception: {type(e).__name__}: {e!s:.150}"
                logger.error(f"Resend email alert exception: {e}")
                break

        if spool_on_fail:
            _spool_failed_alert(spool_payload, last_reason)
        return False
