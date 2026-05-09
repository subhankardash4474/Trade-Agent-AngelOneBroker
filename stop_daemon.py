"""
Stop-Daemon — safely kill all trading-agent processes.

Motivation
──────────
`Stop-Process -Id <pid>` (PowerShell) or `kill <pid>` (POSIX) only kills the
process whose PID you know. The agent is launched via `run_daemon.py` which
has auto-restart logic that can spawn child Python processes, and repeated
restarts during development can leave orphan daemons running in parallel —
each one reading/writing the same SQLite DB, each one sending its own email
alerts. During the 2026-04-29 session we ended up with **seven** daemons
running at once because of this footgun.

What this script does
─────────────────────
1. Enumerate every `python.exe` process whose command line contains
   `run_daemon.py`, `trading_agent.py`, or `main.py trade`.
2. Print a summary (PID, start-time, command-line).
3. Terminate each one, confirming they're gone.
4. If `--keep <pid>` is given, skip that PID (useful for "kill all except
   the current").

Usage
─────
    python stop_daemon.py                # kill every agent process
    python stop_daemon.py --keep 30044   # keep PID 30044 alive
    python stop_daemon.py --dry-run      # just list, don't kill

Portable — works on Windows, Linux, macOS.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import List, Optional

try:
    import psutil
except ImportError:
    print("ERROR: psutil not installed. Run: pip install psutil", file=sys.stderr)
    sys.exit(1)


AGENT_CMDLINE_MARKERS = ("run_daemon.py", "trading_agent.py")
# main.py is also an entry point but only when invoked with the trade subcommand
MAIN_PY_MARKER = "main.py"
MAIN_PY_REQUIRED_ARG = "trade"


def is_agent_process(proc: "psutil.Process") -> bool:
    """True if this process looks like a trading-agent instance."""
    try:
        if not proc.name().lower().startswith("python"):
            return False
        cmdline = " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if any(m in cmdline for m in AGENT_CMDLINE_MARKERS):
        return True
    if MAIN_PY_MARKER in cmdline and MAIN_PY_REQUIRED_ARG in cmdline.split():
        return True
    return False


def find_agent_processes() -> List["psutil.Process"]:
    found = []
    for proc in psutil.process_iter(attrs=["pid", "name"]):
        if is_agent_process(proc):
            found.append(proc)
    return found


def fmt(proc: "psutil.Process") -> str:
    try:
        age = time.time() - proc.create_time()
        age_min = age / 60
        cmdline = " ".join(proc.cmdline())
        # Truncate long command lines for readability
        if len(cmdline) > 80:
            cmdline = cmdline[:77] + "..."
        return f"  PID {proc.pid:>6}  age={age_min:5.1f}m  {cmdline}"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return f"  PID {proc.pid:>6}  <access denied>"


def terminate(proc: "psutil.Process", graceful_timeout: float = 3.0) -> bool:
    """Try SIGTERM first, then SIGKILL. Returns True on success."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=graceful_timeout)
            return True
        except psutil.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
                return True
            except psutil.TimeoutExpired:
                return False
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--keep", type=int, action="append", default=[],
                   help="PID to skip (can be used multiple times)")
    p.add_argument("--dry-run", action="store_true",
                   help="List matching processes but do not kill them")
    args = p.parse_args(argv)

    procs = find_agent_processes()
    if not procs:
        print("No trading-agent processes found.")
        return 0

    print(f"Found {len(procs)} agent process(es):")
    for proc in procs:
        marker = "  [KEEP]" if proc.pid in args.keep else ""
        print(fmt(proc) + marker)

    if args.dry_run:
        print("\n(dry-run: no processes killed)")
        return 0

    targets = [p for p in procs if p.pid not in args.keep]
    if not targets:
        print("\nAll matches are protected by --keep; nothing to do.")
        return 0

    print(f"\nKilling {len(targets)} process(es)...")
    killed = 0
    failed: List[int] = []
    for proc in targets:
        pid = proc.pid
        if terminate(proc):
            print(f"  killed PID {pid}")
            killed += 1
        else:
            print(f"  FAILED to kill PID {pid}")
            failed.append(pid)

    # Final verification
    time.sleep(1)
    remaining = [p for p in find_agent_processes() if p.pid not in args.keep]
    if remaining:
        print(f"\nWARNING: {len(remaining)} process(es) still running:")
        for proc in remaining:
            print(fmt(proc))
        return 1

    print(f"\nDone — {killed} killed, {len(args.keep)} kept.")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
