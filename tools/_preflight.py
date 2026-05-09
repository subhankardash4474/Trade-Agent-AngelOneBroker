"""Pre-flight check: scans config, DB, logs, files for daemon startup readiness.

Single-shot diagnostic. Exit 0 = green to launch, 1 = blocking issue found.
Used at the start of each trading day to confirm nothing's stale, locked, or
mis-configured before the daemon takes over.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import yaml

IST = pytz.timezone("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "trading_agent.db"
CONFIG_PATH = ROOT / "config.yaml"


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def section_emergency_stop() -> int:
    banner("EMERGENCY_STOP file")
    if (ROOT / "EMERGENCY_STOP").exists():
        print("BLOCKING: EMERGENCY_STOP file exists -- daemon will refuse to start.")
        return 1
    print("OK: no EMERGENCY_STOP file")
    return 0


def section_config() -> int:
    banner("Config sanity")
    rc = 0
    try:
        c = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"BLOCKING: cannot parse config.yaml: {type(e).__name__}: {e}")
        return 1
    # Mode is set via CLI flag (`--paper` / `--live`) on `run_daemon.py`,
    # not in YAML, so we just note that.
    print("mode               : (set via --paper/--live on run_daemon.py CLI)")
    print(f"initial_capital    : Rs {c.get('backtest', {}).get('initial_capital', '?')}")
    risk = c.get("risk", {})
    print(f"max_drawdown_pct   : {risk.get('max_drawdown_pct', '?')}")
    print(f"daily_loss_limit   : {risk.get('daily_loss_limit_pct', '?')}")
    # Email config lives under monitoring.alerts.email (not alerts.email).
    email = c.get("monitoring", {}).get("alerts", {}).get("email", {})
    print(f"email.enabled      : {email.get('enabled', '?')}")
    print(f"email.recipient    : {email.get('recipient', '?')}")
    print(f"email.provider     : {email.get('provider', '?')}")
    print(f"eod_summary_time   : {c.get('robustness', {}).get('eod_summary_time', '?')}")
    actives = c.get("strategies", {}).get("active", [])
    print(f"active strategies  : {actives}")
    if not actives:
        print("BLOCKING: no active strategies configured")
        rc = 1
    if email.get("enabled") and not (
        os.getenv("RESEND_API_KEY") or os.getenv("SMTP_PASSWORD")
    ):
        # Many users keep keys in .env which `python-dotenv` loads at runtime.
        # Check if .env exists; if so it's almost certainly fine.
        if (ROOT / ".env").exists():
            print("note: email enabled, key not in shell env -- .env present, "
                  "daemon will load it on startup")
        else:
            print("WARNING: email enabled but no RESEND_API_KEY / SMTP_PASSWORD "
                  "env var set AND no .env file (alerts will fail silently)")
    return rc


def section_db() -> int:
    banner("Database state")
    if not DB_PATH.exists():
        print("BLOCKING: data/trading_agent.db does not exist")
        return 1
    try:
        c = sqlite3.connect(DB_PATH)
    except Exception as e:
        print(f"BLOCKING: cannot open DB: {type(e).__name__}: {e}")
        return 1
    try:
        # 1. Open positions
        rows = list(c.execute(
            "SELECT symbol, side, quantity, entry_price, stop_loss, take_profit, "
            "strategy, entry_time FROM open_positions"
        ))
        print(f"Open positions    : {len(rows)}")
        invested = 0.0
        for sym, side, qty, ep, sl, tp, strat, et in rows:
            invested += (ep or 0) * (qty or 0)
            print(f"  {sym:<12} {side:<5} {qty:>4} @ {ep:>8.2f}  "
                  f"SL {sl:>8.2f}  TP {tp:>8.2f}  [{strat}]  opened {et[:19]}")
        if rows:
            print(f"  Total invested    : Rs {invested:,.2f}")

        # 2. Latest equity snapshot
        cols = [r[1] for r in c.execute("PRAGMA table_info(equity_curve)")]
        last = c.execute(
            "SELECT * FROM equity_curve ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if last:
            d = dict(zip(cols, last))
            ts = d.get("timestamp", "?")
            cash = d.get("cash", 0)
            equity = d.get("equity", 0)
            pos_count = d.get("positions", 0)
            print(f"Last equity snap  : {ts}")
            print(f"  equity={equity:,.2f}  cash={cash:,.2f}  positions={pos_count}")
            # Stale check: snapshot older than 18h is suspicious for a daily run
            try:
                snap_dt = datetime.fromisoformat(ts)
                if snap_dt.tzinfo is None:
                    snap_dt = IST.localize(snap_dt)
                age_h = (datetime.now(IST) - snap_dt).total_seconds() / 3600
                if age_h > 18:
                    print(f"  NOTE: snapshot is {age_h:.1f}h old (last close was yesterday)")
            except Exception:
                pass
            if pos_count != len(rows):
                print(f"  WARNING: snapshot says {pos_count} positions, "
                      f"DB has {len(rows)} -- self-heal will reconcile on startup")

        # 3. Recent trades (last 24h)
        cutoff = (datetime.now(IST) - timedelta(hours=24)).isoformat()
        n_recent = c.execute(
            "SELECT COUNT(*) FROM trades WHERE exit_time >= ?", (cutoff,)
        ).fetchone()[0]
        print(f"Trades last 24h   : {n_recent}")

        # 4. DB integrity
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"PRAGMA integrity  : {ok}")
        if ok != "ok":
            print("BLOCKING: DB integrity check failed")
            return 1

        # 5. WAL files (open daemon would lock these)
        wal = DB_PATH.with_suffix(".db-wal")
        shm = DB_PATH.with_suffix(".db-shm")
        if wal.exists() or shm.exists():
            print(f"NOTE: WAL files present (-wal: {wal.exists()}, -shm: {shm.exists()})")
            print("      Harmless if no daemon was running. If daemon WAS running, "
                  "they'd be locked.")

    finally:
        c.close()
    return 0


def section_recent_errors() -> int:
    banner("Recent log errors (last log file)")
    log_files = sorted(ROOT.glob("logs/trading_agent_*.log"))
    if not log_files:
        print("(no agent logs found)")
        return 0
    latest = log_files[-1]
    print(f"Latest log file: {latest.name}")
    err_count = 0
    crit_count = 0
    tb_count = 0
    err_samples = []
    try:
        with latest.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if "| ERROR" in line:
                    err_count += 1
                    if len(err_samples) < 5:
                        err_samples.append(line.rstrip())
                elif "| CRITICAL" in line:
                    crit_count += 1
                if "Traceback (most recent call last)" in line:
                    tb_count += 1
    except Exception as e:
        print(f"WARNING: cannot read log: {type(e).__name__}: {e}")
        return 0
    print(f"errors      : {err_count}")
    print(f"criticals   : {crit_count}")
    print(f"tracebacks  : {tb_count}")
    if err_samples:
        print("first error samples:")
        for s in err_samples:
            # Strip the timestamp + level prefix for readability
            # "2026-05-06 10:32:43 | ERROR    | <body>"
            parts = s.split(" | ", 2)
            body = parts[2] if len(parts) >= 3 else s
            print(f"  - {body[:140]}")
    if crit_count > 0 or tb_count > 5:
        print("WARNING: high traceback / critical count -- review before launch")
    return 0


def section_files() -> int:
    banner("Disk / file health")
    rc = 0
    # 1. Stale backtest tmp dirs
    tmp = list((ROOT / "logs").glob("_bt_tmp_*"))
    if tmp:
        print(f"WARNING: {len(tmp)} stale backtest tmp dirs in logs/ -- "
              f"safe to delete")
        for t in tmp[:5]:
            print(f"  {t}")
    else:
        print("logs/ tmp dirs   : clean")

    # 2. DB locked check (try a write transaction)
    try:
        c = sqlite3.connect(DB_PATH, timeout=2.0)
        c.execute("BEGIN IMMEDIATE")
        c.execute("ROLLBACK")
        c.close()
        print("DB lock check    : OK (no lock held)")
    except sqlite3.OperationalError as e:
        print(f"BLOCKING: DB is locked: {e}")
        rc = 1
    except Exception as e:
        print(f"WARNING: DB write probe failed: {type(e).__name__}: {e}")

    # 3. Models present
    models = list((ROOT / "models").glob("*.pkl")) if (ROOT / "models").exists() else []
    print(f"models/*.pkl     : {len(models)} files")
    if not models:
        print("WARNING: no models found in models/ -- xgboost_classifier will be muted")

    # 4. Disk space (rough check)
    try:
        import shutil
        total, used, free = shutil.disk_usage(str(ROOT))
        free_gb = free / 1024 ** 3
        print(f"free disk space  : {free_gb:.1f} GB")
        if free_gb < 1.0:
            print("BLOCKING: less than 1 GB free disk -- DB writes may fail")
            rc = 1
    except Exception:
        pass

    return rc


def section_python_processes() -> int:
    banner("Python processes alive")
    try:
        import psutil
        my_pid = os.getpid()
        candidates = []
        for p in psutil.process_iter(attrs=["pid", "name", "cmdline", "cpu_times"]):
            try:
                if p.info["pid"] == my_pid:
                    continue
                name = (p.info.get("name") or "").lower()
                if "python" not in name:
                    continue
                cmd = " ".join(str(x) for x in (p.info.get("cmdline") or []))
                if "run_daemon.py" in cmd or "trading_agent" in cmd.lower():
                    cpu = sum(p.info.get("cpu_times") or (0, 0))
                    candidates.append((p.info["pid"], cmd[:100], cpu))
            except Exception:
                continue
        if candidates:
            print(f"WARNING: {len(candidates)} daemon-like python(s) already alive:")
            for pid, cmd, cpu in candidates:
                print(f"  PID {pid}  CPU {cpu:.0f}s  {cmd}")
            print("  (Stop these before starting a fresh daemon to avoid double-trading)")
            return 1
        print("no daemon-like python running")
        return 0
    except ImportError:
        print("(psutil unavailable -- skipping)")
        return 0


def main() -> int:
    print(f"Pre-flight @ {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST")
    rc = 0
    rc |= section_emergency_stop()
    rc |= section_config()
    rc |= section_db()
    rc |= section_recent_errors()
    rc |= section_files()
    rc |= section_python_processes()
    print()
    if rc == 0:
        print("=== PRE-FLIGHT: GREEN -- safe to launch ===")
    else:
        print("=== PRE-FLIGHT: REVIEW BLOCKERS ABOVE BEFORE LAUNCH ===")
    return rc


if __name__ == "__main__":
    sys.exit(main())
