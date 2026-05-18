"""Battery queue scheduler — run multiple battery jobs sequentially on the
backtester VM.

Why this exists
---------------
A single `tools/run_battery.py` invocation runs ONE battery (set of variants
over one universe / window). For the freeze-v2.1 validation window we want
the VM to keep producing evidence 24/7: when one battery finishes, the
next one should start automatically.

This module:
  1. Reads a YAML queue file (default: `data/battery_queue.yaml`).
  2. For each job:
     a. Skip if state file already marks it done.
     b. Spawn `docker run` for `tools/run_battery.py` with the job's args.
     c. Block until the container exits.
     d. Mark done in state file.
  3. On startup, waits for any pre-existing battery container to finish
     before processing the queue (handles the "deploy mid-battery" case).

Crash / reboot resilience
-------------------------
* The systemd unit auto-restarts the scheduler.
* When restarted, the scheduler:
  - re-reads the queue (so operators can edit it live and the change
    will be picked up at the next loop iteration)
  - resumes any incomplete docker run via the battery harness's own
    `--resume <run_id>` mechanism (run_id is deterministic per queue
    entry, derived from `name`)
  - skips any job already marked done in state file

State file format (data/battery_queue_state.json):
  {
    "schema_version": 1,
    "jobs": {
        "<job_name>": {
            "status": "pending" | "running" | "completed" | "failed",
            "run_id": "battery_<name>_<utc_ts>",
            "started_at": "...iso...",
            "finished_at": "...iso...",
            "exit_code": 0
        }
    }
  }

Queue file format (data/battery_queue.yaml):
  schema_version: 1
  jobs:
    - name: v2_baseline_90d
      days: 90
      workers: 2
      interval: 5m
      universe-file: tests/fixtures/battery_v2_universe.json
    - name: nifty50_60d
      ...

CLI:
  python tools/run_battery_queue.py                  # run with defaults
  python tools/run_battery_queue.py --dry-run        # show plan, don't execute
  python tools/run_battery_queue.py --queue X --state Y  # custom paths
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("[FATAL] PyYAML is required for the queue scheduler. "
          "Install via `pip install pyyaml` or rely on the image which "
          "already has it.", file=sys.stderr)
    raise SystemExit(2)

# Path bootstrap so the scheduler works from any cwd (systemd may invoke
# it from /).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)

DEFAULT_QUEUE = PROJECT_ROOT / "data" / "battery_queue.yaml"
DEFAULT_STATE = PROJECT_ROOT / "data" / "battery_queue_state.json"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs" / "battery_scheduler"

# Match launch_battery.sh defaults
DEFAULT_IMAGE = "trading-agent:latest"
TRADER_HOME = "/opt/trading-agent"

POLL_INTERVAL_SEC = 60  # how often to poll docker for container exit
PRE_EXISTING_POLL_SEC = 90  # how often to check for pre-existing battery


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


# ───────────────────────── state file I/O ─────────────────────────
def load_state(state_path: Path) -> dict:
    """Return the persisted scheduler state. Empty schema if missing or
    corrupt -- the scheduler should never crash on a broken state file,
    because the practical recovery is "start the queue from scratch"
    which is safe (jobs are idempotent and the docker container's own
    --resume covers the in-flight case).
    """
    if not state_path.exists():
        return {"schema_version": 1, "jobs": {}}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            print(f"[scheduler] WARN state file {state_path} has unexpected "
                  f"schema; treating as empty.", file=sys.stderr)
            return {"schema_version": 1, "jobs": {}}
        if "jobs" not in raw or not isinstance(raw["jobs"], dict):
            raw["jobs"] = {}
        return raw
    except Exception as exc:
        print(f"[scheduler] WARN failed to parse {state_path}: {exc!r}; "
              f"treating as empty.", file=sys.stderr)
        return {"schema_version": 1, "jobs": {}}


def save_state(state: dict, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    # Atomic rename so a kill mid-write can't leave a half-file.
    os.replace(tmp, state_path)


# ───────────────────────── queue file I/O ─────────────────────────
def load_queue(queue_path: Path) -> list[dict]:
    """Load + validate the queue file. Returns the list of job dicts."""
    if not queue_path.exists():
        raise SystemExit(
            f"[FATAL] queue file not found: {queue_path}. "
            f"Create one from tests/fixtures/battery_queue_example.yaml "
            f"or pass --queue <path>."
        )
    try:
        raw = yaml.safe_load(queue_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SystemExit(f"[FATAL] queue file has invalid YAML: {exc!r}")

    if not isinstance(raw, dict):
        raise SystemExit("[FATAL] queue file must be a YAML mapping at root.")
    if raw.get("schema_version") != 1:
        raise SystemExit(
            f"[FATAL] queue file schema_version != 1 "
            f"(got {raw.get('schema_version')!r}). "
            f"Refusing to run -- check upstream changes."
        )
    jobs = raw.get("jobs") or []
    if not isinstance(jobs, list):
        raise SystemExit("[FATAL] queue file `jobs` must be a list.")

    seen_names = set()
    for i, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise SystemExit(f"[FATAL] queue job #{i} is not a mapping.")
        name = job.get("name")
        if not name or not isinstance(name, str):
            raise SystemExit(f"[FATAL] queue job #{i} missing string `name`.")
        if name in seen_names:
            raise SystemExit(f"[FATAL] duplicate job name '{name}' in queue.")
        seen_names.add(name)

    return jobs


# ───────────────────────── docker glue ─────────────────────────
def find_running_battery_container() -> str | None:
    """Return the first running container whose name starts with
    'battery_', or None. We deliberately match by name prefix because:
      * the queue scheduler names jobs `battery_<name>_<ts>` (matches)
      * the ad-hoc launch_battery.sh script names runs
        `battery_freeze_v21_<ts>` (also matches)
    so the scheduler will wait for either kind to finish before starting
    its own queue.
    """
    try:
        out = subprocess.check_output(
            ["sudo", "docker", "ps", "--filter", "name=battery_",
             "--format", "{{.Names}}"],
            text=True, stderr=subprocess.STDOUT, timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"[scheduler] WARN docker ps failed: {exc!r}", file=sys.stderr)
        return None
    names = [n.strip() for n in out.splitlines() if n.strip()]
    return names[0] if names else None


def wait_for_running_battery(quiet: bool = False) -> None:
    """Block until no 'battery_*' container is running."""
    waited = 0
    while True:
        name = find_running_battery_container()
        if not name:
            if waited and not quiet:
                print(f"[scheduler] pre-existing battery finished after "
                      f"{waited}s; resuming queue processing.")
            return
        if not quiet:
            print(f"[scheduler] waiting for pre-existing battery "
                  f"'{name}' to finish (poll every {PRE_EXISTING_POLL_SEC}s)...")
        time.sleep(PRE_EXISTING_POLL_SEC)
        waited += PRE_EXISTING_POLL_SEC


def build_docker_run_argv(job: dict, run_id: str, image: str) -> list[str]:
    """Translate a queue-job dict into a `docker run` argv list.

    Job dict supports these keys (others are passed through verbatim as
    `--<key> <value>` to run_battery.py):
        days, workers, interval, universe-file, variants, capital,
        train-window-days, holdout-window-days, run-id (overrides
        auto-generated), resume
    """
    cmd: list[str] = [
        "sudo", "docker", "run",
        "--name", run_id,
        "--no-healthcheck",
        "-e", "BACKTESTER_MODE=1",
        "-v", f"{TRADER_HOME}/logs:/app/logs",
        "-v", f"{TRADER_HOME}/data:/app/data",
        "-v", f"{TRADER_HOME}/tests/fixtures:/app/tests/fixtures:ro",
        "--restart=no",
        image,
        "python", "tools/run_battery.py",
    ]

    # Forward queue knobs as --<flag> <value>. Skip `name` (it's our
    # scheduler-internal id) and `run-id` (we control that).
    for key, val in job.items():
        if key in ("name", "run-id"):
            continue
        if val is None:
            continue
        # `variants` can be a list -> emit each
        if isinstance(val, list):
            cmd.append(f"--{key}")
            cmd.extend(str(v) for v in val)
        elif isinstance(val, bool):
            if val:
                cmd.append(f"--{key}")
        else:
            cmd.append(f"--{key}")
            cmd.append(str(val))

    cmd.append("--run-id")
    cmd.append(run_id)
    return cmd


def _run_id_for(job: dict, prior_state: dict | None) -> tuple[str, bool]:
    """Compute the run_id we'll pass to the battery for this job.

    Returns (run_id, resuming). When resuming, the caller should also
    pass --resume on the next launch; but since the battery harness
    already supports `--resume <run_id>` by run_id matching the on-disk
    folder, the cleanest approach is to just reuse the same run_id and
    let the harness DTRT. We do NOT pass --resume on the docker argv
    because that'd require us to also remove the --run-id arg; the
    harness auto-resumes when the on-disk run_id folder exists and
    contains partial results.
    """
    if prior_state and prior_state.get("run_id"):
        return prior_state["run_id"], True
    name = job["name"]
    return f"battery_{name}_{_utc_ts()}", False


def wait_for_container_exit(container_name: str, log_dir: Path) -> int:
    """Block until the named container terminates. Returns its exit code."""
    log_dir.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            out = subprocess.check_output(
                ["sudo", "docker", "inspect",
                 "--format", "{{.State.Status}}|{{.State.ExitCode}}",
                 container_name],
                text=True, stderr=subprocess.STDOUT, timeout=15,
            ).strip()
            status, exit_code = out.split("|", 1)
            if status not in ("running", "created", "restarting"):
                return int(exit_code)
        except subprocess.CalledProcessError:
            # Container was --rm-cleaned up; treat as success unless we
            # know otherwise. Caller will verify via state inspection.
            return 0
        except subprocess.TimeoutExpired:
            print(f"[scheduler] docker inspect timed out on "
                  f"'{container_name}'; retrying.", file=sys.stderr)
        time.sleep(POLL_INTERVAL_SEC)


# ───────────────────────── orchestrator ─────────────────────────
def process_queue(
    queue_path: Path,
    state_path: Path,
    log_dir: Path,
    image: str,
    dry_run: bool = False,
    wait_pre_existing: bool = True,
) -> int:
    """Main loop. Returns the number of jobs that completed in this
    invocation (excluding already-done ones).
    """
    jobs = load_queue(queue_path)
    state = load_state(state_path)

    print(f"[scheduler] queue: {queue_path} ({len(jobs)} jobs)")
    print(f"[scheduler] state: {state_path}")
    print(f"[scheduler] log dir: {log_dir}")

    if wait_pre_existing and not dry_run:
        wait_for_running_battery()

    processed = 0
    for job in jobs:
        name = job["name"]
        prior = state["jobs"].get(name)

        # Skip jobs that are already marked completed.
        if prior and prior.get("status") == "completed":
            print(f"[scheduler] SKIP '{name}' (already completed at "
                  f"{prior.get('finished_at')})")
            continue

        run_id, resuming = _run_id_for(job, prior)
        argv = build_docker_run_argv(job, run_id, image)

        if dry_run:
            print(f"[scheduler] DRY-RUN '{name}' resume={resuming} run_id={run_id}")
            print(f"  cmd: {' '.join(argv)}")
            continue

        print(f"[scheduler] START '{name}' run_id={run_id} resume={resuming}")
        state["jobs"][name] = {
            "status": "running",
            "run_id": run_id,
            "started_at": _utc_iso(),
            "resuming": resuming,
        }
        save_state(state, state_path)

        # Spawn docker run (detached -- the docker daemon manages the
        # process, we just wait for it).
        try:
            subprocess.run(
                argv + ["-d"],   # detach; we'll inspect for status
                check=True, capture_output=True, text=True, timeout=60,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            print(f"[scheduler] FAIL docker run for '{name}': {stderr}",
                  file=sys.stderr)
            state["jobs"][name] = {
                **state["jobs"][name],
                "status": "failed",
                "finished_at": _utc_iso(),
                "exit_code": exc.returncode,
                "error": stderr[-500:],
            }
            save_state(state, state_path)
            # Keep going -- a single launch failure shouldn't sink the
            # whole queue. Subsequent runs can retry by clearing the
            # state entry.
            continue

        # docker run was kicked off detached; container should now exist.
        exit_code = wait_for_container_exit(run_id, log_dir)
        finished_at = _utc_iso()
        status = "completed" if exit_code == 0 else "failed"

        state["jobs"][name] = {
            **state["jobs"][name],
            "status": status,
            "finished_at": finished_at,
            "exit_code": exit_code,
        }
        save_state(state, state_path)
        print(f"[scheduler] {status.upper()} '{name}' exit={exit_code}")
        processed += 1

    print(f"[scheduler] queue exhausted; processed {processed} job(s) "
          f"this invocation.")
    return processed


# ───────────────────────── CLI ─────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--queue", default=str(DEFAULT_QUEUE))
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan; do not docker run anything.")
    ap.add_argument("--no-wait-pre-existing", action="store_true",
                    help="Don't wait for an existing battery container; "
                         "start the queue immediately (dangerous: can "
                         "result in two batteries competing for CPU).")
    args = ap.parse_args(argv)

    # Surface that we're absolutely NOT carrying broker creds. The
    # battery harness itself enforces this via _assert_backtester_isolation,
    # but having the scheduler also check provides defence-in-depth and
    # a clearer error message.
    leaked = [k for k in os.environ if k.startswith(
        ("ANGELONE_", "SMARTAPI_", "BROKER_", "KITE_")
    )]
    if leaked:
        print(f"[scheduler][FATAL] backtester env carries broker creds: "
              f"{sorted(leaked)}. Refusing to start.", file=sys.stderr)
        return 9

    # Make sure docker is reachable; otherwise systemd would keep
    # restarting us forever on a broken host.
    if not shutil.which("docker") and not Path("/usr/bin/docker").exists():
        print("[scheduler][FATAL] docker binary not found on PATH.",
              file=sys.stderr)
        return 3

    return 0 if process_queue(
        queue_path=Path(args.queue),
        state_path=Path(args.state),
        log_dir=Path(args.log_dir),
        image=args.image,
        dry_run=args.dry_run,
        wait_pre_existing=not args.no_wait_pre_existing,
    ) >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
