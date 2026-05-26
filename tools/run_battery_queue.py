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
        validate_job_args(job, queue_path)

    return jobs


# ──────────────── pre-flight validation ────────────────
# Recognised intervals match the BacktestConfig._INTERVAL_ALIASES set on
# the engine side. Keeping this as an explicit allow-list keeps a typo
# ('5min ' with trailing space, '5M', 'hourly') from making it past the
# scheduler and only surfacing inside the docker container 60s later.
_VALID_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "1d",
                    "1min", "5min", "15min", "30min"}


def validate_job_args(job: dict, queue_path: Path) -> None:
    """Reject obvious mistakes before we burn a docker run on them.

    Each check is a high-signal failure mode we've actually hit:
      * unresolvable universe-file -> battery exits with code 3 inside
        the container; the scheduler then marks the job 'failed' and
        moves on, having wasted ~30s of docker-startup. Rejecting here
        saves the round-trip and gives the operator a clear pointer.
      * non-positive `days` -> the harness would download zero bars and
        the EnsembleBacktester would emit `[BATTERY-PROGRESS] 0/0` then
        write an empty result. Equally invisible to the operator.
      * non-integer / negative `workers` -> docker run argv would carry
        --workers -1 which battery.py refuses; clearer to fail here.
      * unknown interval -> passed straight through to yfinance, which
        accepts then silently returns nothing for some strings.
    """
    name = job["name"]

    # universe-file: relative to queue file's project root. We resolve
    # the same way run_battery.py will (PROJECT_ROOT-relative).
    uf = job.get("universe-file")
    if uf is not None:
        uf_path = Path(uf)
        if not uf_path.is_absolute():
            uf_path = PROJECT_ROOT / uf_path
        if not uf_path.exists():
            raise SystemExit(
                f"[FATAL] queue job '{name}': universe-file '{uf}' does not "
                f"exist (resolved: {uf_path}). Fix the path in {queue_path} "
                f"or remove the key to use the default symbols list."
            )

    # days: expect a positive int. Floats / strings get rejected.
    days = job.get("days")
    if days is not None:
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise SystemExit(
                f"[FATAL] queue job '{name}': `days` must be a positive "
                f"integer, got {days!r}."
            )

    # workers: 'auto' is permitted (the harness resolves it) or any
    # positive int.
    workers = job.get("workers")
    if workers is not None:
        if isinstance(workers, str):
            if workers.strip().lower() != "auto":
                raise SystemExit(
                    f"[FATAL] queue job '{name}': `workers` string must be "
                    f"'auto', got {workers!r}."
                )
        elif isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise SystemExit(
                f"[FATAL] queue job '{name}': `workers` must be a positive "
                f"integer or 'auto', got {workers!r}."
            )

    # interval: not a hard requirement, but if specified it must be a
    # known shape so the engine doesn't silently fall back.
    interval = job.get("interval")
    if interval is not None:
        if not isinstance(interval, str) or interval not in _VALID_INTERVALS:
            raise SystemExit(
                f"[FATAL] queue job '{name}': `interval` must be one of "
                f"{sorted(_VALID_INTERVALS)}, got {interval!r}."
            )


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


def build_docker_run_argv(job: dict, run_id: str, image: str,
                          resuming: bool = False) -> list[str]:
    """Translate a queue-job dict into a `docker run` argv list.

    Job dict supports these keys (others are passed through verbatim as
    `--<key> <value>` to run_battery.py):
        days, workers, interval, universe-file, variants, capital,
        train-window-days, holdout-window-days, run-id (overrides
        auto-generated), resume

    `resuming`: when True, pass `--resume <run_id>` to the harness instead
    of `--run-id <run_id>`. The two flags are mutually exclusive on the
    harness side. The harness ONLY skips completed variants and reuses
    cached market_data when `--resume` is passed; `--run-id` alone tells
    it to start fresh in the named folder (latent bug fixed 2026-05-25
    after a queue restart re-ran V1 from scratch in the
    battery_nifty50_60d_20260522T085929 run, almost overwriting V1-V8
    results).
    """
    cmd: list[str] = [
        "sudo", "docker", "run",
        "-d",                       # detached -- docker daemon owns the process
        # 2026-05-25 Bug G-5: --rm so the docker daemon removes the
        # container on exit. Without this, the next launch with the
        # same run_id (the resume path always reuses the run_id) hits
        # a name conflict and the scheduler permanently marks the job
        # "failed". `tools/cloud/launch_battery.sh` already passes
        # --rm; the queue path was the parity gap. Container exit
        # logs survive on the docker daemon side for ~docker-events
        # retention, and the harness writes its own logs to a host
        # bind-mount so --rm doesn't cost us anything for diagnosis.
        "--rm",
        "--name", run_id,
        "--no-healthcheck",
        "-e", "BACKTESTER_MODE=1",
        "-v", f"{TRADER_HOME}/logs:/app/logs",
        "-v", f"{TRADER_HOME}/data:/app/data",
        "-v", f"{TRADER_HOME}/tests/fixtures:/app/tests/fixtures:ro",
        # 2026-05-25 read-only packages mount. Without this, the running
        # battery container holds the version of packages/research/battery.py
        # baked into trading-agent:latest at image-build time. Production
        # urgency: the 2026-05-25 throughput-degradation bug fix needs to
        # take effect on the next scheduler-spawned container without a
        # full image rebuild. Read-only so a stray edit during a battery
        # run can't crash a worker mid-stream.
        "-v", f"{TRADER_HOME}/packages:/app/packages:ro",
        # 2026-05-26 read-only models mount. The trading-agent:latest
        # image was built (2026-05-22) without `models/xgboost_model.pkl`
        # baked in -- discovered during the nifty500_v4_long_only_60d
        # validation run when every variant logged
        # `[XGB-HEALTH] XGBoost model not found at models/xgboost_model.pkl.
        # Strategy will return HOLD`. The result was a 60-day validation
        # where xgboost_classifier silently returned HOLD on every cycle,
        # so V1 = "shipped MINUS xgboost" rather than the actual shipped
        # baseline. Bind-mount fixes the gap without an image rebuild:
        # the trader VM has a known-good copy of the production model
        # (sha256 fc17fcb5efce..., mtime 2026-05-14) which is staged on
        # this host at /opt/trading-agent/models/. Read-only so a
        # mid-run worker can never write/corrupt the model file.
        "-v", f"{TRADER_HOME}/models:/app/models:ro",
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

    # 2026-05-25 fix: resume mode uses --resume (which sets resuming=True
    # in the harness and triggers completed-variant skip + cached
    # market_data reuse). Fresh runs use --run-id (which pins the
    # folder name but starts from scratch).
    if resuming:
        cmd.append("--resume")
    else:
        cmd.append("--run-id")
    cmd.append(run_id)
    return cmd


def _run_id_for(job: dict, prior_state: dict | None) -> tuple[str, bool]:
    """Compute the run_id we'll pass to the battery for this job.

    Returns (run_id, resuming).

    2026-05-25 correctness fix: previously this function claimed the
    harness "auto-resumes when the on-disk run_id folder exists" and
    only ever passed `--run-id`. That claim was WRONG -- the harness
    only sets resuming=True when `--resume <run_id>` is explicitly
    passed; `--run-id` alone tells it to use the folder name but start
    from scratch. The result was a near-miss data loss when the
    battery-nifty50-60d-20260522 run was restarted mid-flight and the
    harness began re-running V1 (which would have overwritten the V1
    results JSON on completion). The `resuming` flag returned here is
    now CONSUMED by `build_docker_run_argv` to switch the docker argv
    between `--resume` (when prior state exists) and `--run-id`
    (fresh).
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
        argv = build_docker_run_argv(job, run_id, image, resuming=resuming)

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
        # process, we just wait for it). The -d flag is already in argv
        # (placed by build_docker_run_argv right after `docker run`); do
        # NOT append it here -- python argparse on the inner script
        # would reject an unrecognized -d at the tail.
        #
        # 2026-05-25 Bug G-5: launch failures fall into two categories:
        #   (a) NAME CONFLICT: a stale container with the same run_id
        #       lingers from a prior launch (pre-G-5 launches didn't
        #       use --rm; even with --rm, manual `docker stop` without
        #       `docker rm` leaves a zombie). The container is dead
        #       but the name is taken. Recoverable: `docker rm -f`
        #       the zombie and retry once.
        #   (b) REAL FAILURE: image missing, daemon down, bad mount,
        #       etc. Mark the job failed and move on.
        # Without (a), the exact incident from 2026-05-25 happens: the
        # nifty50_60d resume can't launch because the dead container
        # from the original failed run holds the name, the scheduler
        # marks the job permanently failed, the queue grinds to a halt.
        def _try_launch() -> tuple[int, str]:
            """Run argv; return (returncode, stderr) without raising."""
            try:
                subprocess.run(
                    argv,
                    check=True, capture_output=True, text=True, timeout=60,
                )
                return 0, ""
            except subprocess.CalledProcessError as e:
                return e.returncode, (e.stderr or "").strip()

        rc, stderr = _try_launch()
        if rc != 0 and "is already in use by container" in stderr:
            # Zombie container blocking the name. Force-remove and
            # retry exactly once.
            print(f"[scheduler] '{name}' launch hit name conflict; "
                  f"removing zombie container '{run_id}' and retrying...")
            try:
                subprocess.run(
                    ["sudo", "docker", "rm", "-f", run_id],
                    capture_output=True, text=True, timeout=30,
                )
            except subprocess.SubprocessError as rm_err:
                print(f"[scheduler] docker rm -f failed: {rm_err}",
                      file=sys.stderr)
                # Fall through to mark failed below.
            else:
                rc, stderr = _try_launch()
                if rc == 0:
                    print(f"[scheduler] '{name}' launch retry succeeded "
                          f"after zombie cleanup")

        if rc != 0:
            print(f"[scheduler] FAIL docker run for '{name}': {stderr}",
                  file=sys.stderr)
            state["jobs"][name] = {
                **state["jobs"][name],
                "status": "failed",
                "finished_at": _utc_iso(),
                "exit_code": rc,
                "error": stderr[-500:],
                # Distinguish launch failure from run failure so an
                # operator can see the difference at a glance.
                "failure_phase": "launch",
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
            # 2026-05-25 Bug G-5: distinguish run-time failure (the
            # container started but the harness exited non-zero) from
            # launch-time failure (the docker run command itself
            # failed). Operators triaging a "failed" job should know
            # at a glance whether to look at workers/<name>.log or at
            # the docker daemon.
            **({"failure_phase": "run"} if status == "failed" else {}),
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
