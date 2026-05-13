# Ampere capacity watcher -- one-time setup

This guide walks through the ~15 min of one-time setup required before
`tools/cloud/ampere_capacity_watcher.sh` can poll OCI for free-tier
Ampere A1 capacity on your behalf.

Background: see `docs/backtester_vm_runbook.md` for why we want a
dedicated VM at all and what we'll do with it once it provisions.

---

## Step 1 -- install `oci-cli`

**On macOS/Linux/Windows-WSL (recommended):**
```bash
pip install --user oci-cli
oci --version    # confirm install
```

**On Windows native PowerShell:**
```powershell
pip install --user oci-cli
# Add the Scripts dir to PATH if `oci --version` says "not found":
$env:PATH += ";$env:APPDATA\Python\Python311\Scripts"
oci --version
```

**On the trader VM (recommended host for the watcher -- always-on):**
```bash
ssh ubuntu@80.225.251.79
sudo apt update && sudo apt install -y python3-pip
pip3 install --user oci-cli
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
oci --version
```

---

## Step 2 -- run `oci setup config`

```bash
oci setup config
```

Interactive prompts:

1. **Location for your config (~/.oci/config)** -- accept default.
2. **User OCID** -- find at: OCI Console > top-right Avatar > User settings >
   OCID > Copy. Looks like `ocid1.user.oc1..aaa...`.
3. **Tenancy OCID** -- OCI Console > top-right Avatar > Tenancy: ... >
   OCID > Copy. Looks like `ocid1.tenancy.oc1..aaa...`.
4. **Region** -- whichever region your trader is in (`ap-mumbai-1` for
   the current setup).
5. **Generate API key pair?** -- **Yes**. The tool writes them to
   `~/.oci/oci_api_key.pem` and `~/.oci/oci_api_key_public.pem`.
6. **Passphrase** -- leave empty (the watcher needs unattended access).

When done, the tool prints the **public key fingerprint** and the public
key contents.

---

## Step 3 -- upload the public API key to OCI

1. OCI Console > Avatar > **User settings** > **Resources** sidebar >
   **API Keys** > **Add API Key**.
2. Choose **Paste a Public Key**.
3. Paste the contents of `~/.oci/oci_api_key_public.pem` (the entire
   `-----BEGIN PUBLIC KEY-----` to `-----END PUBLIC KEY-----` block).
4. Click **Add**.

OCI Console will show the fingerprint; it should match what `oci setup
config` printed.

---

## Step 4 -- verify CLI auth works

```bash
oci iam region list --query 'data[*].name' --output table
```

If you see a list of region names (`ap-mumbai-1`, `ap-tokyo-1`, ...) the
auth is working. If you see `NotAuthenticated` or `InvalidPrivateKey`,
re-check Step 3.

---

## Step 5 -- discover OCIDs the watcher needs

The watcher needs 6 values. Run each command, copy the relevant ID
into a worksheet, then write the final config file in Step 6.

**a. AVAILABILITY_DOMAIN**
```bash
oci iam availability-domain list --query 'data[*].name' --output table
```
Expected for Mumbai: one entry like `Anye:AP-MUMBAI-1-AD-1`.

**b. COMPARTMENT_OCID**

For a fresh personal account you usually want the root compartment, which
has the same OCID as your tenancy. Confirm:
```bash
oci iam compartment list --all \
    --query 'data[*].{name:name,id:id}' --output table
```
Pick the row where `name == "root"` (or wherever you keep your VMs).
Note: list excludes the root entry by default; use the tenancy OCID as
`COMPARTMENT_OCID` if you want the root.

**c. SUBNET_OCID**

Reuse the trader VM's subnet (so both VMs are on the same VCN and can
talk to each other for `rclone` if needed later):
```bash
oci compute instance list-vnics \
    --instance-id <trader-instance-ocid> \
    --query 'data[0]."subnet-id"' --raw-output
```
You can find the trader instance OCID in the OCI Console > Compute >
Instances > click the trader > OCID.

**d. IMAGE_OCID**

Latest Oracle Linux 9 ARM image:
```bash
oci compute image list \
    --compartment-id "$COMPARTMENT_OCID" \
    --operating-system "Oracle Linux" \
    --operating-system-version "9" \
    --shape VM.Standard.A1.Flex \
    --query 'data[0:3].{name:"display-name",id:id,date:"time-created"}' \
    --output table
```
Pick the most recent. Copy its OCID.

**e. SSH_PUBLIC_KEY_PATH**

Reuse the trader key. On the laptop:
```
C:\Users\subhanda\.ssh\oci_trader_key.pub
```
On the trader VM:
```
~/.ssh/oci_trader_key.pub        # if you copied it; otherwise generate / scp
```

**f. DISPLAY_NAME**

Any string. Recommended: `backtester` (so it's clearly distinct from
`trader` in the Console).

---

## Step 6 -- write `~/.ampere_watcher.env`

```bash
cat > ~/.ampere_watcher.env <<'EOF'
# Filled in from Step 5. Quotes are recommended for OCIDs.
AVAILABILITY_DOMAIN="Anye:AP-MUMBAI-1-AD-1"
COMPARTMENT_OCID="ocid1.tenancy.oc1..<your-tenancy>"
SUBNET_OCID="ocid1.subnet.oc1.ap-mumbai-1.<your-subnet>"
IMAGE_OCID="ocid1.image.oc1.ap-mumbai-1.<latest-ol9-arm>"
SSH_PUBLIC_KEY_PATH="$HOME/.ssh/oci_trader_key.pub"
DISPLAY_NAME="backtester"
# Shape config. Default 2 OCPU + 12 GB leaves half your Ampere quota
# for future use. Bump to 4/24 if you want max performance and don't
# need a 3rd VM later.
OCPUS=2
MEMORY_GB=12
EOF
chmod 600 ~/.ampere_watcher.env
```

---

## Step 7 -- dry-run validation

Before kicking off a 48 h poll loop, sanity-check the config:
```bash
bash tools/cloud/ampere_capacity_watcher.sh --dry-run
```

Expected output: a single "DRY-RUN: config validated" line with all the
fields echoed back. If any required var is missing, the script tells you
which one.

---

## Step 8 -- run it

**On the laptop (sleeps -- only good for short windows):**
```bash
bash tools/cloud/ampere_capacity_watcher.sh --interval 10 --max-hours 4
```

**On the trader VM under tmux (always-on -- recommended):**
```bash
ssh ubuntu@80.225.251.79
tmux new -s ampere
bash tools/cloud/ampere_capacity_watcher.sh --interval 10 --max-hours 48
# Ctrl-b d  to detach the tmux session. Re-attach later with:
#   tmux attach -t ampere
```

The watcher logs every attempt to `~/ampere_watcher.log` (override with
`LOG_FILE=...`). When capacity lands it prints the new VM's OCID and
public IP, then exits clean (rc=0). If you set OCI Console
notifications on the new instance creation event, you'll also get an
email -- handy if the watcher is on the trader VM and you're away from
the laptop.

When the watcher exits successfully, follow `docs/backtester_vm_runbook.md`
Stage 1 (bootstrap) to install Docker + clone the repo + build the
image.

---

## Common errors and fixes

| Error | Cause | Fix |
|---|---|---|
| `NotAuthenticated` | Public key not uploaded, or wrong fingerprint | Re-run Step 3, double-check the key file you pasted |
| `InvalidPrivateKey` | `~/.oci/oci_api_key.pem` corrupted or wrong path | Re-run `oci setup config` |
| `LimitExceeded` (not capacity!) | You already have 4 OCPU of Ampere allocated | Free the existing instance(s) before retrying |
| `InvalidParameter: image-id` | Image OCID doesn't match the AD's region | Re-run Step 5d with the AD's region |
| watcher logs only "no capacity" for >24 h | Mumbai genuinely full | Try `ap-hyderabad-1` -- re-run Steps 5a, 5c, 5d for the new region |
