# Cloud MVC Runbook — Trader Pod on Oracle Cloud Free Tier

**Audience**: operator (you) running the cloud migration in a 5-day sprint.
**Goal**: move the trader pod off the corporate VPN and onto a stable-IP
cloud VM so the AngelOne whitelist works reliably and live trading can resume.

**Locked decisions** (see `docs/cloud_pod_architecture.md` for the why):

| Decision | Value |
|---|---|
| Cloud provider | Oracle Cloud Infrastructure (OCI) free tier — fallback DigitalOcean Bangalore $4 droplet |
| Region | `ap-mumbai-1` (best NSE latency) |
| OS | Oracle Linux 9 (ARM64 on Ampere A1) |
| Compute | VM.Standard.A1.Flex, 1 OCPU + 6 GB RAM (free forever) |
| Storage | 50 GB boot volume (free up to 200 GB) |
| Networking | 1 reserved public IP (free, attached to instance) |
| Secrets | `.env` file, chmod 600, owned by trader user |
| Container | Docker + docker-compose plugin, `restart=unless-stopped` |
| Backup | Nightly `data/*.db` → object storage (deferred to Day 6) |

---

## Day-by-day plan

| Day | Phase | Status |
|---|---|---|
| **Day 1 (today)** | Local prep — build Dockerfile, compose, scripts, runbook | this commit |
| Day 2 | OCI signup → instance allocation (Ampere A1 capacity hunt) | pending |
| Day 3 | Bootstrap VM → deploy paper daemon → verify health | pending |
| Day 4 morning | Whitelist OCI public IP in AngelOne portal | pending |
| Day 4 afternoon | Re-run Stage 0 / Stage 1 / Stage 2 from cloud | pending |
| Day 5 | Stage 2.1 from cloud (first real fill) | pending |
| Days 6-7 | Harden: cron healthcheck, nightly DB backup, alerting | pending |

---

## Day 1 — Local prep (this commit)

What this commit ships:

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-arch (amd64+arm64) container image. Python 3.11-slim, non-root, tini PID 1, integrated healthcheck. |
| `.dockerignore` | Keep secrets/logs/models/data out of the build context. |
| `docker-compose.yml` | Volume-mount data/logs/models/config; restart=unless-stopped; mem caps. |
| `.env.production.example` | Cloud-flavoured env template (forces SSL verification on, optional force-paper). |
| `tools/cloud/oci_bootstrap.sh` | One-shot installer for fresh OCI / Ubuntu / AL2023 VM. |
| `tools/cloud/trader.service` | Optional systemd wrapper for boot-time start. |
| `tools/cloud/deploy.sh` | SSH-push: git pull on the box, rebuild image, restart container. |
| `tools/cloud/healthcheck_cron.sh` | Dead-man's switch — emails via Resend if heartbeat goes stale. |
| `docs/cloud_mvc_runbook.md` | This file. |

Local smoke test before committing:

```bash
# From the repo root on your Windows laptop (Docker Desktop installed):
docker compose build trader
docker compose up trader
# Watch ~3 min, you should see paper-mode heartbeats and "Sleeping outside
# market window" lines. Ctrl+C, then:
docker compose down
```

If `docker compose build` fails on Windows with a permissions error on
`/app/data`, it's a Windows path-translation issue — the volume mount works
fine on Linux (which is what the cloud VM will be). Local smoke is just for
catching Dockerfile syntax / missing deps, not full fidelity.

Commit + push when smoke is green.

---

## Day 2 — OCI signup and instance allocation

### 2.1 Create OCI account

1. Go to <https://signup.oraclecloud.com/>
2. Use a personal email (not work). Credit card required for verification —
   **OCI will NOT charge** for always-free resources, but a card-on-file
   prevents account abuse.
3. **CRITICAL**: when prompted for "Home Region", pick **Mumbai
   (`ap-mumbai-1`)**. This cannot be changed later for free-tier resources.
4. Wait 5-30 min for account verification email.

### 2.2 Generate an SSH key (on your Windows laptop)

```powershell
# In PowerShell, from anywhere:
ssh-keygen -t ed25519 -f $HOME\.ssh\oci_trader_key -C "trader-oci"
# Press enter twice (no passphrase, OR set one and add to Windows ssh-agent)
# Public key to paste into OCI:
type $HOME\.ssh\oci_trader_key.pub
```

### 2.3 Allocate an Ampere A1 instance

Navigate: **Console → Compute → Instances → Create Instance**

| Field | Value |
|---|---|
| Name | `trader-mumbai-01` |
| Compartment | Default (or whichever you prefer) |
| Image | **Oracle Linux 9** (ARM64 variant) |
| Shape | **VM.Standard.A1.Flex**, 1 OCPU + 6 GB RAM |
| Boot volume | 50 GB (default 47 GB also fine; 50 leaves headroom) |
| Network | Auto-create VCN + public subnet |
| Public IPv4 | Assign automatically (we'll swap to reserved IP next) |
| SSH key | Paste `oci_trader_key.pub` content |

Click **Create**.

#### Capacity blocker (likely)

You may get `Out of capacity in this availability domain for shape
VM.Standard.A1.Flex`. This is the well-known OCI Mumbai issue. Two tactics:

- **Manual retry**: hit Create every 15-30 min for up to 24 hrs. Often
  releases happen during off-peak Indian hours (early AM IST).
- **Auto-retry script** (advanced — only after you've tried manually for a
  few hours):

```bash
while true; do
    oci compute instance launch --from-json file://launch_config.json && break
    sleep 600  # try every 10 min
done
```

#### Plan B (if Ampere blocked >24h): always-free x86 micro

- Shape: **VM.Standard.E2.1.Micro** (1/8 OCPU, 1 GB RAM)
- Image: **Oracle Linux 9** (x86_64)
- Free forever, instantly available.
- Caveat: 1 GB RAM is tight for our daemon. Reduce paper capital from
  ₹100k to ₹50k and disable XGBoost strategy temporarily to bring memory
  under 700 MB.

#### Plan C (if both OCI options fail): DigitalOcean

- <https://cloud.digitalocean.com/registrations/new>
- Region: Bangalore (BLR1)
- Plan: Basic Regular, $4/mo (1 vCPU, 512 MB RAM) or $6/mo (1 vCPU, 1 GB RAM, recommended)
- Image: Ubuntu 22.04 LTS
- Add your SSH key during create.

The `oci_bootstrap.sh` script works on Ubuntu 22.04 + DO out of the box.

### 2.4 Reserve a static public IP

Once instance is **Running**:

1. **Console → Compute → IP Management → Reserved IPs → Reserve**
2. Region: `ap-mumbai-1`, named `trader-eip-01`
3. **Compute → Instances → trader-mumbai-01 → Attached VNICs → primary VNIC**
4. **IP Addresses → Public IP → Edit → Use reserved public IP → Select `trader-eip-01`**
5. Wait ~30s for the IP swap. SSH session will drop; reconnect to the new IP.

Note the reserved IP — this is what AngelOne will whitelist.

---

## Day 3 — Bootstrap and first paper-mode boot

### 3.1 SSH in for the first time

```powershell
# Replace <NEW_IP> with the reserved IP from Day 2.4
ssh -i $HOME\.ssh\oci_trader_key opc@<NEW_IP>
```

(Username is `opc` on Oracle Linux 9, `ubuntu` on Ubuntu, `ec2-user` on
Amazon Linux.)

### 3.2 Run the bootstrap

On the VM (as `opc` / `ubuntu`):

```bash
# Pull the bootstrap script directly from GitHub
curl -fsSL https://raw.githubusercontent.com/<your-org>/<your-repo>/main/tools/cloud/oci_bootstrap.sh \
    -o /tmp/bootstrap.sh
chmod +x /tmp/bootstrap.sh

# Run it (replace with your actual GitHub URL)
/tmp/bootstrap.sh https://github.com/<your-org>/<your-repo>.git main
```

Expected runtime: 3-5 min. It installs Docker, creates the `trader` user,
clones the repo into `/opt/trading-agent`, opens firewall for SSH only.

### 3.3 Place secrets and the XGBoost model

From your laptop (in a SECOND PowerShell window — don't lose your SSH session):

```powershell
# Push .env. Edit a copy locally first with your real keys.
cp .env.production.example .env.cloud
notepad .env.cloud   # fill in real ANGELONE_*, RESEND_API_KEY, ALERT_RECIPIENT
scp -i $HOME\.ssh\oci_trader_key .env.cloud opc@<NEW_IP>:/tmp/.env

# Push the trained XGBoost model (it's gitignored, must be copied separately)
scp -i $HOME\.ssh\oci_trader_key models\xgboost_model.pkl opc@<NEW_IP>:/tmp/
```

Back on the VM:

```bash
# Move .env and model into place with correct ownership/permissions
sudo install -o trader -g trader -m 600 /tmp/.env /opt/trading-agent/.env
sudo install -o 1001  -g 1001  -m 644 /tmp/xgboost_model.pkl \
        /opt/trading-agent/models/xgboost_model.pkl
sudo rm /tmp/.env /tmp/xgboost_model.pkl

# Sanity check
sudo cat /opt/trading-agent/.env | head -3   # should print first 3 lines
ls -la /opt/trading-agent/.env                # -rw------- trader trader
ls -la /opt/trading-agent/models/
```

### 3.4 Build and start the container

```bash
sudo -u trader -i
cd /opt/trading-agent

# First build is slow (~4-7 min on ARM Mumbai region, includes wheel
# download for pandas/xgboost/scikit-learn). Subsequent rebuilds reuse the
# layer cache and finish in ~30s.
docker compose build

# Start detached
docker compose up -d

# Tail the logs to verify boot
docker compose logs -f trader
```

You should see lines like:

```
TRADING AGENT DAEMON STARTED
  Config: config.yaml
  Mode: PAPER
  Poll: 60s
  Market hours only: True
```

If market is open (Mon-Fri 09:15-15:30 IST), it will start polling
immediately. Otherwise it logs `Outside market hours — sleeping...`.

### 3.5 Verify health from host

```bash
# Heartbeat status (run after a few cycles, ~2 min after compose up)
python3 /opt/trading-agent/tools/health_check.py

# Expected:
# [OK] daemon up: pid=1 age=58s cycle=3 positions=0 cash=Rs 100,000 pnl=Rs +0 trades=0 mode=paper
```

If you get `[FAIL] no health file at ...`, give the daemon another minute
and re-run. If still failing after 3 min, check `docker compose logs trader`
for tracebacks.

### 3.6 Enable systemd unit (optional but recommended)

```bash
# Back in your sudo-able session (exit the trader shell first)
exit
sudo cp /opt/trading-agent/tools/cloud/trader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable trader.service
sudo systemctl status  trader.service   # should be "active (exited)"
```

This guarantees the container comes up at boot even if `docker` itself
gets restarted or the VM is rebooted.

---

## Day 4 morning — AngelOne IP whitelist + Stage 0

### 4.1 Capture the reserved IP

```bash
# On the VM:
curl -s https://checkip.amazonaws.com
# Should print the OCI reserved IP from Day 2.4. Copy it.
```

### 4.2 Whitelist in AngelOne SmartAPI portal

1. Browser → <https://smartapi.angelbroking.com/>
2. Login → **My Apps** → select your app → **Edit**
3. Find **Primary Static IP** → paste the OCI reserved IP
4. Save. Propagation is instant (verified in Stage 1 on 2026-05-11).
5. Tip: also note the IP somewhere in your password manager so a future
   you remembers where it's whitelisted.

### 4.3 Re-run Stage 0 from the cloud

```bash
sudo -u trader -i
cd /opt/trading-agent

# Stage 0 = read-only auth + funds smoke
docker compose exec trader python tools/test_angelone_auth.py
```

Expected: **8/8 PASS**, `available_cash=Rs 1,000.00`.

If you get `AG7002 Unregistered IP`, the whitelist didn't propagate yet.
Wait 60s and retry.

### 4.4 Re-run Stage 1 from the cloud

```bash
# Stage 1 = place + cancel lifecycle (no real exposure)
# Wait until market is open (09:15-15:30 IST)
docker compose exec trader python tools/test_amo_lifecycle.py --confirm
```

Expected: **Stage 1 PASS**, wall-clock ~30s, Rs 0 cost.

---

## Day 4 afternoon — Stage 2 patient + Day 5 Stage 2.1 aggressive

```bash
# Stage 2 = single live BUY+SELL round-trip, patient pricing (no-fill expected)
docker compose exec trader python tools/test_live_single_trade.py --confirm
# Expected: Stage 2 PASS, no-exposure clean exit

# Stage 2.1 = aggressive pricing, guaranteed fill (~Rs 0.13 cost)
docker compose exec trader python tools/test_live_single_trade.py --confirm --aggressive
# Expected: BUY fills in <5s, SELL fills in <5s, total ~30s, ~Rs 0.13 spent
```

After Stage 2.1 PASS: **the cloud trader can place real orders against
AngelOne**. The MVC is functionally complete.

---

## Days 6-7 — Hardening

### 6.1 Cron healthcheck (dead-man's switch)

```bash
# As user trader:
sudo -u trader crontab -e
# Add:
#   */5 * * * * /opt/trading-agent/tools/cloud/healthcheck_cron.sh \
#                 >> /opt/trading-agent/logs/cron_healthcheck.log 2>&1
```

Test by stopping the container and waiting 5 min — you should receive
an email titled `[trader] container DOWN on <hostname>`.

### 6.1.b Pulling cloud logs to the laptop (stop-gap)

Until §6.2 nightly rclone lands, the daily post-close ritual to make the
cloud daemon's artefacts available to Cursor / the `trading-audit` skill
is a single PowerShell command from the laptop:

```powershell
# one-time
$env:TRADER_VM_HOST = "<oci-mumbai-ip>"
# daily, after 16:05 IST (i.e. once the Profit Diagnostic email lands)
.\tools\cloud\pull_logs.ps1
```

It SCP's today's audit checkpoints, signal-audit CSV, trades CSV, daemon
log, post-mortem, EOD diagnostic, and `health.json` into the local repo.
All targets are gitignored. Pass `-Date YYYY-MM-DD` for a historical day,
`-IncludeDb` if you also need the SQLite DB, and `-DryRun` to inspect the
exact commands without executing.

### 6.2 Nightly DB backup (OCI Object Storage)

```bash
# Create a bucket in OCI Object Storage: trader-backups-mumbai
# Generate an API key for the trader user
# Configure rclone:
sudo -u trader -i
rclone config   # interactive: select OCI / paste keys / bucket name

# Add to crontab:
#   30 19 * * 1-5  rclone copy /opt/trading-agent/data/ oci:trader-backups-mumbai/ --update
```

(Detailed steps deferred to Day 6 commit — not blocking for live trading.)

### 6.3 Flip from paper to live (manual gate)

Once Stage 2.1 has passed and you've watched the cloud daemon paper-trade
for at least 2 full sessions without anomalies:

```bash
# 1. SSH in as trader, edit config.yaml:
sudo -u trader nano /opt/trading-agent/config.yaml
# Change:
#   broker.mode: paper   ->   broker.mode: live
#   capital.initial_balance: 100000  ->  1000  (start small!)

# 2. Restart the container so it re-reads config.yaml
docker compose down
docker compose up -d

# 3. Verify the first cycle's logs explicitly say "MODE: LIVE"
docker compose logs -f trader | grep -i "mode:"
```

---

## Common failure modes & fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `docker compose build` hangs on `pip install pandas` | pip cache miss + slow ARM wheel download | Wait. First build is ~5 min. |
| `AG7002 Unregistered IP` | Whitelist not propagated, or wrong IP whitelisted | Re-check IP via `curl https://checkip.amazonaws.com` on the VM; re-paste in AngelOne portal. |
| `[FAIL] no health file` after 5 min | Daemon crashed during boot | `docker compose logs trader` — read traceback. Usually a missing `.env` field or unreadable model file. |
| Container restarts every 60s | OOM kill (mem cap too low) or import error | `docker inspect trader \| grep OOM`. If true, raise memory cap in compose; else read logs. |
| `pip wheel build for xgboost` fails on Ampere A1 | Trying to compile from source | Make sure base image is `python:3.11-slim` (matches available ARM wheels). |
| EOD email never arrives | Resend API key wrong or sender domain not verified | `docker exec trader python -c "import resend; resend.api_key='re_..'; print(resend.Emails.send(...))"` |
| Heartbeat stale but container "running" | Python GIL deadlock or blocking I/O | `docker compose restart trader`. If reproducible, file an issue and capture `py-spy dump`. |
| Reserved IP gets reassigned | Instance terminated → IP released | Always reserve the IP **before** terminating an instance; or re-attach after recreating. |

---

## Cost expectations

| Resource | OCI free tier | Monthly cost after free tier expires |
|---|---|---|
| VM.Standard.A1.Flex (1 OCPU, 6 GB RAM) | **free forever** | n/a |
| Boot volume 50 GB | **free up to 200 GB always** | n/a |
| Reserved Public IP (attached) | **free** | n/a |
| Egress 10 TB/mo | **free** | n/a |
| Object Storage 10 GB | **free up to 20 GB** | $0.0255/GB after |
| **Total** | **₹0/month** | **~₹0-200/mo even at scale** |

Compare: AWS Mumbai ~₹1700/mo, DigitalOcean Bangalore ₹350/mo.

---

## Roll-back plan

If something goes catastrophically wrong on the cloud:

1. **Stop the cloud trader** immediately: `ssh opc@<vm> sudo systemctl stop trader.service`
2. **Pull the .env off** (you'll need it again): `scp opc@<vm>:/opt/trading-agent/.env ./env.backup`
3. **Pull the DB off** (audit/postmortem): `scp opc@<vm>:/opt/trading-agent/data/trading_agent.db ./db.backup`
4. **Resume local paper daemon** on laptop: same `.env`, run `python run_daemon.py --paper`. Zero data loss.
5. **Terminate OCI instance** if needed (Console → Instances → Terminate). Reserved IP can be kept for re-use.

The local paper daemon never depends on the cloud — they coexist independently.

---

## Open items / future improvements

- [ ] Push image to GHCR / Docker Hub instead of build-on-box (faster deploys)
- [ ] GitHub Actions: build + push on every commit to main, deploy on tagged release
- [ ] OCI Object Storage for nightly DB snapshots (Day 6)
- [ ] Multi-pod: split research and UI into their own OCI VMs once the trader is stable for 2+ weeks
- [ ] Migrate `core/secrets.py` to optional OCI Vault backend (for when we have >5 secrets)
- [ ] Add `make` targets: `make deploy`, `make logs`, `make status`, `make stage21`

---

*Last updated: 2026-05-11 (Day 1 commit). Each phase update lands as a
follow-up commit citing the day completed.*
