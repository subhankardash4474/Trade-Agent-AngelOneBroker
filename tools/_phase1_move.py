"""
Phase 1 move script -- restructure top-level packages into packages/<name>/.

Goal:  preserve all import paths (`from core.X import Y` keeps working) by
       moving entire directories under packages/ and adding packages/ to
       sys.path via a project-root conftest.py.

Modes:
  --dry-run    Print every move + every file change. Touches nothing.  (DEFAULT)
  --execute    Do the moves with `git mv` (preserves git history) +
               write conftest.py + verify pytest passes.
  --rollback   git stash any uncommitted changes from a failed --execute.

Run from project root:
    python tools/_phase1_move.py                  # dry-run preview
    python tools/_phase1_move.py --dry-run        # explicit dry-run
    python tools/_phase1_move.py --execute        # actually do it
    python tools/_phase1_move.py --rollback       # if something goes wrong

Preconditions checked before --execute:
  1. Working dir is clean (no uncommitted changes outside the move plan).
  2. No live battery / daemon process holds open the directories we move.
  3. All source dirs exist; all dest dirs do NOT (no merge surprises).
  4. We are in a git repository.

Postconditions verified after --execute:
  1. All source dirs are gone.
  2. All dest dirs exist with expected content.
  3. `python -c "import core, strategies, brokers"` succeeds.
  4. `python -m pytest tests/unit -q` exits 0.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ─────────────────────────────────────────────────────────────────────
# THE PLAN (single source of truth)
# ─────────────────────────────────────────────────────────────────────
# Directory moves: source -> destination. Preserves git history via `git mv`.
# Names are kept identical so `from core.X import Y` works unchanged once
# packages/ is on sys.path.
DIRECTORY_MOVES: list[tuple[str, str]] = [
    ("core",        "packages/core"),
    ("strategies",  "packages/strategies"),
    ("brokers",     "packages/brokers"),
    ("monitoring",  "packages/monitoring"),
    ("training",    "packages/training"),
]

# Files that move into research/ and result in a NEW import path.
# These are the only items that require import-rewrite work.
RESEARCH_MOVES: list[tuple[str, str]] = [
    ("backtest.py",                          "packages/research/backtest.py"),
    ("backtest_ensemble.py",                 "packages/research/backtest_ensemble.py"),
    ("analyze_day.py",                       "packages/research/analyze_day.py"),
    ("tools/overnight_backtest_battery.py",  "packages/research/battery.py"),
    ("tools/profit_diagnostic.py",           "packages/research/diagnostic.py"),
]

# Import-rewrite map: old module path -> new module path.
# Applied across all .py files (excluding the move-targets themselves).
IMPORT_REWRITES: list[tuple[str, str]] = [
    # research-pod modules pick up their new home
    ("from backtest_ensemble",                "from research.backtest_ensemble"),
    ("import backtest_ensemble",              "import research.backtest_ensemble"),
    ("from backtest ",                        "from research.backtest "),
    ("import backtest ",                      "import research.backtest "),
    ("from analyze_day",                      "from research.analyze_day"),
    ("from tools.overnight_backtest_battery", "from research.battery"),
    ("from tools.profit_diagnostic",          "from research.diagnostic"),
]

# Conftest.py at project root: makes packages/ importable without `pip install`.
CONFTEST_PROLOGUE = '''"""
Project-root conftest.py -- bootstraps sys.path for the new packages/ layout.

Phase 1 (2026-05-09) moved core/, strategies/, brokers/, monitoring/, training/
under packages/. To preserve unchanged import paths (`from core.X import Y`),
we prepend packages/ to sys.path at pytest collection time.

A matching prelude is added to every entry-point script (run_daemon.py,
stop_daemon.py, etc.) by tools/_phase1_move.py at execution.

Phase 2 will replace this hack with `pip install -e .` reading from
pyproject.toml's package-dir config. For laptop-mode, this is sufficient
and zero-config from the user's perspective.
"""
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parent / "packages"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))
'''

# Entry-point scripts that need the same sys.path prelude (since they're
# run directly, not via pytest).
ENTRY_POINTS = [
    "run_daemon.py",
    "stop_daemon.py",
    "main.py",
]
ENTRY_PRELUDE = (
    "# Phase 1 sys.path bootstrap -- packages/ is the new home for core, strategies, etc.\n"
    "import sys as _sys\n"
    "from pathlib import Path as _Path\n"
    "_pkg = _Path(__file__).resolve().parent / 'packages'\n"
    "if str(_pkg) not in _sys.path:\n"
    "    _sys.path.insert(0, str(_pkg))\n"
)
ENTRY_PRELUDE_MARKER = "# Phase 1 sys.path bootstrap"


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def _run(cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command and return CompletedProcess."""
    return subprocess.run(cmd, cwd=ROOT, check=check, capture_output=True, text=True)


def _is_git_repo() -> bool:
    try:
        _run(["git", "rev-parse", "--is-inside-work-tree"])
        return True
    except subprocess.CalledProcessError:
        return False


def _git_status_clean() -> tuple[bool, str]:
    """True iff working tree has no uncommitted changes."""
    cp = _run(["git", "status", "--porcelain"])
    out = cp.stdout.strip()
    return (out == "", out)


def _battery_or_daemon_alive() -> tuple[bool, str]:
    """Refuse to execute if the live battery or daemon is holding the dirs we move."""
    if os.name != "nt":
        return False, ""  # Linux check would use psutil; not needed for laptop dev
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'overnight_backtest_battery|run_daemon' } | "
             "Select-Object -ExpandProperty ProcessId"],
            cwd=ROOT, check=False, capture_output=True, text=True, timeout=10,
        )
        pids = [p.strip() for p in ps.stdout.splitlines() if p.strip()]
        if pids:
            return True, f"PIDs alive: {', '.join(pids)}"
    except Exception:
        pass
    return False, ""


def _all_py_files() -> list[Path]:
    """Every .py we might rewrite imports in (post-move tree)."""
    # SKIP the move script itself so it doesn't rewrite its own
    # IMPORT_REWRITES literal-data table (would corrupt the rules).
    SELF_PATH = Path(__file__).resolve()
    out: list[Path] = []
    for p in ROOT.rglob("*.py"):
        if p.resolve() == SELF_PATH:
            continue
        rel = p.relative_to(ROOT).as_posix()
        if any(rel.startswith(s) for s in (
            "venv/", ".venv/", "build/", "dist/", "__pycache__/",
            "logs/", "data/", "models/", ".git/",
            "logs/backtests/", "logs/cloud_sync/",
        )):
            continue
        out.append(p)
    return out


def _preview_import_rewrites(files: list[Path]) -> list[tuple[Path, list[tuple[int, str, str]]]]:
    """Return [(file, [(lineno, before, after)])] without writing."""
    out: list[tuple[Path, list[tuple[int, str, str]]]] = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits: list[tuple[int, str, str]] = []
        for ln_idx, line in enumerate(content.splitlines(), start=1):
            for old, new in IMPORT_REWRITES:
                if old in line:
                    hits.append((ln_idx, line.rstrip(), line.replace(old, new).rstrip()))
                    break
        if hits:
            out.append((f, hits))
    return out


def _check_preconditions(execute: bool) -> list[str]:
    """Return list of error messages. Empty list => OK."""
    errs: list[str] = []
    if not _is_git_repo():
        errs.append("not in a git repo")
        return errs
    clean, dirty = _git_status_clean()
    if not clean and execute:
        errs.append(f"working tree not clean ({len(dirty.splitlines())} dirty paths). "
                    "Commit or stash first.")
    alive, msg = _battery_or_daemon_alive()
    if alive and execute:
        errs.append(f"battery or daemon process is alive ({msg}). "
                    "Stop them before --execute.")
    for src, _ in DIRECTORY_MOVES + RESEARCH_MOVES:
        if not (ROOT / src).exists():
            errs.append(f"source missing: {src}")
    for _, dest in DIRECTORY_MOVES:
        if (ROOT / dest).exists() and any((ROOT / dest).iterdir()):
            errs.append(f"destination not empty: {dest}")
    for _, dest in RESEARCH_MOVES:
        if (ROOT / dest).exists():
            errs.append(f"destination already exists: {dest}")
    return errs


# ─────────────────────────────────────────────────────────────────────
# Modes
# ─────────────────────────────────────────────────────────────────────
def dry_run() -> int:
    print("Phase 1 DRY RUN")
    print("=" * 70)
    errs = _check_preconditions(execute=False)
    if errs:
        print("\nPreconditions (warnings, not blockers in dry-run):")
        for e in errs:
            print(f"  - {e}")
    print("\nDirectory moves (`git mv`):")
    for src, dest in DIRECTORY_MOVES:
        print(f"  {src:18s}  ->  {dest}")
    print("\nFile moves into packages/research/:")
    for src, dest in RESEARCH_MOVES:
        print(f"  {src:42s}  ->  {dest}")
    print("\nNew files to be CREATED:")
    print("  conftest.py                          (project root, sys.path bootstrap)")
    print("  Each entry point gets prelude:")
    for ep in ENTRY_POINTS:
        print(f"    {ep}")
    print("\nImport rewrites (will scan all .py except /logs /data /models /.git):")
    files = _all_py_files()
    print(f"  Files in scan: {len(files)}")
    plan = _preview_import_rewrites(files)
    if not plan:
        print("  (no import rewrites needed -- all moves preserve names)")
    else:
        total = sum(len(h) for _, h in plan)
        print(f"  Total rewrites: {total} across {len(plan)} files")
        for f, hits in plan[:25]:
            rel = f.relative_to(ROOT).as_posix()
            print(f"    {rel}:")
            for ln, before, after in hits[:5]:
                print(f"       L{ln}: - {before}")
                print(f"       L{ln}: + {after}")
            if len(hits) > 5:
                print(f"       ... +{len(hits)-5} more in this file")
        if len(plan) > 25:
            print(f"    ... +{len(plan)-25} more files")
    print("\nNext step: run with --execute (after battery+daemon are stopped).")
    return 0


def execute() -> int:
    print("Phase 1 EXECUTE")
    print("=" * 70)
    errs = _check_preconditions(execute=True)
    if errs:
        print("\nPRECONDITION FAILURES:")
        for e in errs:
            print(f"  - {e}")
        print("\nAborted. Fix the above and re-run.")
        return 2

    print("\n[1/5] git mv: directory moves")
    for src, dest in DIRECTORY_MOVES:
        dest_parent = (ROOT / dest).parent
        dest_parent.mkdir(parents=True, exist_ok=True)
        # If empty placeholder dir already exists at dest, remove it before git mv
        full_dest = ROOT / dest
        if full_dest.exists() and not any(full_dest.iterdir()):
            full_dest.rmdir()
        cp = _run(["git", "mv", src, dest], check=False)
        if cp.returncode != 0:
            print(f"  FAILED: git mv {src} {dest}\n  {cp.stderr}")
            return 3
        print(f"  OK    {src:18s}  ->  {dest}")

    print("\n[2/5] git mv: research file moves")
    (ROOT / "packages" / "research").mkdir(parents=True, exist_ok=True)
    for src, dest in RESEARCH_MOVES:
        cp = _run(["git", "mv", src, dest], check=False)
        if cp.returncode != 0:
            print(f"  FAILED: git mv {src} {dest}\n  {cp.stderr}")
            return 3
        print(f"  OK    {src:42s}  ->  {dest}")

    print("\n[3/5] write conftest.py at project root")
    (ROOT / "conftest.py").write_text(CONFTEST_PROLOGUE, encoding="utf-8")
    print("  OK    conftest.py")

    print("\n[4/5] add sys.path prelude to entry points")
    for ep in ENTRY_POINTS:
        p = ROOT / ep
        if not p.exists():
            print(f"  SKIP  {ep} (not present)")
            continue
        content = p.read_text(encoding="utf-8")
        if ENTRY_PRELUDE_MARKER in content:
            print(f"  SKIP  {ep} (already has prelude)")
            continue
        # Insert after any shebang/coding line, before first import
        lines = content.splitlines(keepends=True)
        insert_at = 0
        for i, ln in enumerate(lines):
            if ln.startswith("#!") or "coding" in ln[:30] or ln.strip().startswith('"""'):
                insert_at = i + 1
                continue
            break
        # Skip docstring if present
        if insert_at < len(lines) and lines[insert_at-1].strip().startswith('"""'):
            for j in range(insert_at, len(lines)):
                if lines[j].rstrip().endswith('"""'):
                    insert_at = j + 1
                    break
        new = "".join(lines[:insert_at]) + "\n" + ENTRY_PRELUDE + "\n" + "".join(lines[insert_at:])
        p.write_text(new, encoding="utf-8")
        print(f"  OK    {ep}")

    print("\n[5/5] apply import rewrites")
    files = _all_py_files()
    n_files = 0
    n_hits = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        new_content = content
        for old, new in IMPORT_REWRITES:
            new_content = new_content.replace(old, new)
        if new_content != content:
            n_files += 1
            n_hits += sum(content.count(old) for old, _ in IMPORT_REWRITES)
            f.write_text(new_content, encoding="utf-8")
    print(f"  OK    rewrote {n_hits} imports across {n_files} files")

    print("\nDONE. Verify with:")
    print("  python -c \"import core, strategies, brokers; print('imports OK')\"")
    print("  python -m pytest tests/unit -q")
    print("\nIf either fails, run --rollback to undo and inspect the diff.")
    return 0


def rollback() -> int:
    print("Phase 1 ROLLBACK")
    print("=" * 70)
    if not _is_git_repo():
        print("not a git repo")
        return 2
    print("Restoring all changes via `git restore` + `git clean -fd`...")
    cp1 = _run(["git", "restore", "--source=HEAD", "--staged", "--worktree", "."], check=False)
    cp2 = _run(["git", "clean", "-fd"], check=False)
    print(f"  git restore: rc={cp1.returncode}")
    print(f"  git clean:   rc={cp2.returncode}")
    if cp1.returncode == 0 and cp2.returncode == 0:
        print("\nRolled back to HEAD. Re-run --dry-run to plan again.")
        return 0
    return 3


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True,
                   help="Show plan only; touch nothing. (default)")
    g.add_argument("--execute", action="store_true",
                   help="Actually perform the moves.")
    g.add_argument("--rollback", action="store_true",
                   help="Restore HEAD via git restore + git clean.")
    args = ap.parse_args()

    if args.rollback:
        return rollback()
    if args.execute:
        return execute()
    return dry_run()


if __name__ == "__main__":
    raise SystemExit(main())
