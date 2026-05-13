# Backtester VM Runbook

**Purpose**: stand up a dedicated OCI free-tier VM to run multi-day backtests
(battery-v2: 200 stocks x 90 days x 18 variants, estimated 8-35 h depending
on shape) without disturbing the live trader pod on
`80.225.251.79`.

**Status**: plan only. Bootstrap scripts and result-pull helpers will be
shipped in the session immediately after the Ampere VM provisions. This
document is the snapshot of architectural decisions so the next session
has zero reconstruction cost.

---

## Decision matrix

| Concern | Same-VM swap (rejected) | Dedicated backtester VM (chosen) |
|---|---|---|
| Trader during a 30 h batch | OFF | **Stays up** -- no missed market days |
| Failure isolation | Backtester OOM could kill trader | **Separate failure domains** |
| Mode-switch complexity | Lock file + market-hours guard + swap | **None** -- just SSH the other host |
| AngelOne IP whitelist | Both modes share one IP | Only trader IP whitelisted; backtester has no broker creds |
| Resource ceiling | 1 GB RAM, 1/8 OCPU | Up to 24 GB RAM, 4 OCPU (Ampere quota) |
| Marginal cost | $0 | **$0** (within Always-Free) |

The dedicated-VM path is strictly simpler than the same-VM swap was going
to be. We are NOT building the compose `batch` profile, switch script,
lock file, or 2 GB swapfile machinery on the trader VM.

---

## Target shape: Ampere A1 Flex, 2 OCPU + 12 GB RAM

OCI Always-Free includes **4 OCPU and 24 GB RAM** of Ampere A1 to be
split across 1-4 instances. The backtester carves out half:

| Spec | Value | Why |
|---|---|---|
| Shape | `VM.Standard.A1.Flex` | Ampere ARM64, free tier |
| OCPU | 2 | Lets `tools/run_battery.py --workers 2` (or 4 with hyperthreading) |
| RAM  | 12 GB | Battery `market_data.pkl` ~340 MB raw -> ~1 GB in pandas; xgboost + features push to 3-4 GB; comfortable headroom |
| OS   | Oracle Linux 9 ARM64 | Same as trader, same `oci_bootstrap.sh` flow |
| Region | `ap-mumbai-1` | Match trader VM; capacity comes and goes here -- Hyderabad is fallback |
| Storage | 50 GB boot | Default; result tarballs are small (<1 GB per battery run) |
| Public IP | Reserved free IP | For SSH from laptop |
| SSH key | `oci_trader_key.pub` (reused) | Same key as trader -- no new key management |

**Capacity blocker**: at the time of writing the user has hit "Out of host
capacity" on Ampere repeatedly. The community workaround is a polling
script that retries `oci compute instance launch` every 5-10 minutes
until OCI yields a slot. To be added as `tools/cloud/ampere_capacity_watcher.sh`
in the next session if the user wants it (currently they will retry
manually from the OCI console).

**Fallback shape if Ampere stays unavailable**: 2nd `VM.Standard.E2.1.Micro`
(AMD, 1 OCPU, 1 GB RAM). Has been a working environment for the trader, but
for battery-v2 it would need:
- 2 GB swapfile to cover RAM overruns during the data-cache load.
- Sub-batching the 200-stock universe into 4 x 50-stock runs.
- `--workers 1` (no parallelism).
- Wall time roughly **35-45 hours per full battery run** vs ~8-12 h on
  the Ampere 2-OCPU shape.

---

## Architecture (post-Ampere-provision)

```
  +--------------------+      no network coupling      +-----------------------+
  | trader VM           |                              | backtester VM         |
  | 80.225.251.79       |                              | <to be provisioned>   |
  | E2.1.Micro (AMD)    |                              | A1.Flex 2/12 (Ampere) |
  |                     |                              |                       |
  | docker compose      |                              | docker run --rm       |
  |   trader (live)     |                              |   battery_<runid>     |
  |   running 24/7      |                              |   one-shot batch      |
  |                     |                              |                       |
  | logs/* (live)       |                              | logs/backtests/* (run results) |
  | data/trading_agent.db (live)                       | no broker DB           |
  +--------------------+                              +-----------------------+
            \                                                   /
             \--- laptop pulls from both via SSH+SCP/rclone ---/
                  (pull_logs.ps1 already covers trader; new
                   pull_battery_results.ps1 to be added)
```

Both VMs run the **same** `trading-agent:latest` image, built from the
same `Dockerfile`. The only difference at runtime is the CMD:
- trader: `python run_daemon.py --paper --interval 60`
- backtester: `python tools/run_battery.py --days 90 --workers 2 --run-id <id>`

This means a future code change auto-applies to both pods when each one
rebuilds; no schema drift possible.

---

## Step-by-step: when Ampere capacity lands

**Stage 0 -- provision the VM (manual, OCI console or oci-cli)**

1. OCI Console > Compute > Instances > Create Instance.
2. Image: Oracle Linux 9 (ARM).
3. Shape: VM.Standard.A1.Flex; 2 OCPU; 12 GB memory.
4. Networking: existing VCN; assign public IPv4.
5. SSH key: paste `oci_trader_key.pub` contents (same key as trader).
6. Boot volume: 50 GB.
7. Click Create. Note the public IP -- this becomes `BACKTESTER_IP`.

**Stage 1 -- bootstrap (script to be written, ~30 min in next session)**

`tools/cloud/bootstrap_backtester.sh` will, from the user's laptop:
1. `ssh ubuntu@$BACKTESTER_IP` via `oci_trader_key`.
2. Install Docker + docker-compose plugin (same as `oci_bootstrap.sh`).
3. Create the `trader` host user (UID 998 if free, else auto-allocate
   and write into `.env` via the `TRADER_UID` machinery that already
   lives in `Dockerfile` and `_deploy_inline.sh`).
4. Allocate a 2 GB swapfile (`/swapfile`, 0644, fstab entry) -- harmless
   on Ampere with 12 GB RAM, essential if the fallback Micro path is
   used later.
5. `git clone` the repo into `/opt/trading-agent`.
6. `docker compose build trader` (this becomes the cached `trading-agent:latest`
   image used for batteries too).
7. Smoke-test: `docker run --rm trading-agent:latest python tools/run_battery.py --help`.

**Stage 2 -- launch a battery (script to be written)**

`tools/cloud/launch_battery.sh <args>` will:
1. SSH to backtester VM.
2. `docker run -d --rm --name battery_<timestamp> \
     -v /opt/trading-agent/logs:/app/logs \
     -v /opt/trading-agent/data:/app/data \
     trading-agent:latest \
     python tools/run_battery.py <args>`
3. Print container ID + tail-log command.

Typical invocation for battery-v2:
```
tools/cloud/launch_battery.sh \
   --days 90 \
   --workers 2 \
   --symbols-file tests/fixtures/battery_v2_universe.json \
   --run-id battery_v2_$(date +%Y%m%dT%H%M%S)
```

**Stage 3 -- monitor (no script, just SSH)**

`ssh ubuntu@$BACKTESTER_IP docker logs -f battery_<id>` from laptop. The
harness already writes mid-run `comparison.md` after every successful
variant; tail that for human-readable progress.

**Stage 4 -- pull results (script to be written)**

`tools/cloud/pull_battery_results.ps1 <run_id>` will SCP
`logs/backtests/<run_id>/` from backtester VM to laptop. Same
SSH-key + same Windows pattern as `pull_logs.ps1`.

---

## Resume semantics (free safety net)

The battery harness (`packages/research/battery.py`) already supports
`--resume <run_id>` and `--resume auto`. This means:

* Killing the container mid-run (`docker stop battery_<id>` or kernel OOM)
  is **safe** -- per-variant JSON files persist and the cached
  `market_data.pkl` survives.
* Restarting with `--resume` skips completed variants and reuses the
  cached market data -- no expensive yfinance refetch.
* If a Micro VM is provisioned first and Ampere lands mid-run, the
  resume mechanism makes mid-batch migration trivial:
  ```
  # On Micro:
  docker stop battery_<id>
  rclone copy /opt/trading-agent/logs/backtests/<id>/ \
      backtester:/opt/trading-agent/logs/backtests/<id>/

  # On Ampere:
  tools/cloud/launch_battery.sh --resume <id> --workers 2
  ```

---

## Risks / open questions

1. **Ampere capacity**: blocker right now. Two mitigations: (a) manual
   retry via console, (b) optional `ampere_capacity_watcher.sh` to be
   added in next session if user wants automation.
2. **xgboost ARM64 wheel availability**: confirmed. xgboost ships
   `manylinux_2_28_aarch64` wheels for the version pinned in
   `requirements.txt`. No source-build needed.
3. **Historical data source**: battery harness fetches via yfinance.
   Confirm fetch limits aren't hit on first run (the cache means
   subsequent variants reuse data; only the cold start matters).
4. **No broker credentials on backtester**: explicitly intentional.
   The harness must NEVER load `.env` with `BROKER_*` keys. Need to
   add a startup assertion to that effect when we wire the launcher --
   tracked as a follow-up in the next session.

---

## Deferred from this session

Code artefacts -- to be written when an Ampere VM provisions:
- `tools/cloud/bootstrap_backtester.sh`
- `tools/cloud/launch_battery.sh`
- `tools/cloud/pull_battery_results.ps1`
- `tools/cloud/ampere_capacity_watcher.sh` (optional, offered separately)
- Startup assertion that backtester image cannot load broker creds

Research artefacts -- separate session, not deploy-related:
- battery-v2 variant slate definition (currently `battery.py` has the
  2026-05-08 V1..V15 slate; battery-v2 wants 18 variants tuned to the
  open-questions from the 2026-05-13 morning audit)
- Universe freeze: run `tools/_freeze_battery_v2_universe.py` and commit
  `tests/fixtures/battery_v2_universe.json`
