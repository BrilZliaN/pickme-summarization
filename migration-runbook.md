# Server Migration Runbook — pickme-summarization Telegram Bot

Old server (fully configured) → NEW server (bare, Chunkserve by Datapasa).
SQLite + Docker Compose + long polling. Ubuntu 24.04 both ends.

**Plan only. Copy-paste commands in order. Stop on first red check.**

### Conventions

On your **Windows 10/11 workstation (PowerShell + built-in OpenSSH)** define once
per session (note the `${OLD}:` form — PowerShell needs the braces before a colon
after a variable, plain `"$OLD:"` does not interpolate):

```powershell
$OLD = "root@OLD_IP"      # e.g. "root@203.0.113.10"
$NEW = "root@NEW_IP"      # e.g. "root@198.51.100.20"
$Mig  = "$env:USERPROFILE\pickme-migration"
New-Item -ItemType Directory -Force -Path $Mig | Out-Null
```

> Tip: keep these three lines in a **local, gitignored** file (e.g.
> `~\pickme-migration\targets.ps1`) and `. targets.ps1` at the start of each
> session, so IPs live only on your workstation.

On servers (Ubuntu 24.04, `bash` as `root`):

- `REPO` = repo checkout path. **Do not assume.** Discover it in Phase 2.
  Examples use `/root/pickme-summarization` — replace with the real discovered path.
- Keep the new server's repo path identical to the old one to make commands trivial.
- Workstation transfer dir: `~\pickme-migration\`.

Verified source of truth:

- `docker-compose.yml`: single service `bot`, `build: .`, `restart: unless-stopped`,
  `env_file: .env`, `./data:/app/data`, memory 512M.
- `Dockerfile`: `python:3.12-slim`, non-root user `app` (uid 1000),
  `CMD ["python","-m","pickme.main"]`.
- `README.md`: `mkdir -p data && chown -R 1000:1000 data` required; re-chown after
  scp'ing as root, or the container crash-loops with
  `sqlite3.OperationalError: unable to open database file`.
- `.env.example`: all config in `.env` (TELEGRAM_BOT_TOKEN, HETZNER_*,
  OPENCODE_*, DATA_DIR=/app/data). No webhook/TLS/DNS to migrate.

**Golden rule: only one poller at a time.** Starting the new bot while the old bot
runs = `409 Conflict` for both. Order is always:
**pre-build new (don't start) → stop old → copy DB → start new.**

**Recommended transfer path: two-hop via workstation** (`old → workstation → new`
with `scp -r`). Justification:

1. DB is single-digit MB — double transfer adds seconds.
2. No `sshpass` on Windows; interactive password prompts work fine for two short
   scp's. Server-to-server would require typing the old root password on the new
   server (lands in remote shell history) or pre-sharing keys between servers
   (unwanted trust between a soon-decommissioned host and an unverified network).
3. No firewall dependency between servers — old→new direct SSH may be blocked by
   either provider.
4. Workstation automatically retains an insurance copy for rollback/audit.

---

## Step 0 — SSH keys (optional, strongly recommended; ~5 min)

Keeps the runbook workable with passwords, but keys remove ~10 password prompts
during cutover, where a typo costs downtime. **Mandatory if you want an LLM agent
to execute this runbook** — see "Credentials & LLM-assisted execution" below.

On workstation **PowerShell**:

```powershell
# 1. Generate key if missing (press Enter for the default file, empty passphrase)
if (-not (Test-Path "$env:USERPROFILE\.ssh\id_ed25519")) {
  ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\id_ed25519" -N '""'
}

# 2. Install on OLD (ssh-copy-id equivalent; ssh-copy-id is not native on Windows)
Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub" | ssh $OLD "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && echo OK"

# 3. Install on NEW
Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub" | ssh $NEW "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && echo OK"

# 4. Test (should not prompt for a password)
ssh -o PasswordAuthentication=no $OLD  "echo OLD_OK; whoami; head -1 /etc/os-release"
ssh -o PasswordAuthentication=no $NEW  "echo NEW_OK; whoami; head -1 /etc/os-release"
```

If the test fails, continue with passwords — every `ssh`/`scp` below will just
prompt. Do not block the migration on this (unless an agent will drive it).

---

## Phase 0 — Prep new server (~15-25 min, zero downtime)

Goal: bare Ubuntu 24.04 → Docker + Compose + git + hardened SSH baseline.
Old bot untouched and still serving.

```bash
# On NEW:
ssh $NEW    # (from PowerShell)

cat /etc/os-release   # must say Ubuntu 24.04 (noble)
whoami                # must say root
timedatectl           # need: "System clock synchronized: yes".
                      # If not synced, TLS + Telegram will fail — fix before proceeding.

export DEBIAN_FRONTEND=noninteractive
apt-get update && apt-get upgrade -y
# Reboot only if required:
[ -f /var/run/reboot-required ] && reboot
# (reconnect after reboot)
```

Install Docker Engine + Compose plugin via the official Docker apt repo (noble):

```bash
apt-get install -y ca-certificates curl gnupg git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
docker --version
docker compose version
systemctl enable --now docker
systemctl is-active docker    # must say active
```

Optional UFW baseline (recommended, safe for a polling bot):

```bash
ufw allow OpenSSH
ufw --force enable
ufw status verbose
```

> Docker bypasses UFW via iptables; this baseline only hardens SSH. The bot needs
> **zero inbound ports** (long polling = outbound HTTPS only). Do not open 80/443.

Phase 0 done when: `docker compose version` prints, `systemctl is-active docker` =
`active`, UFW allows OpenSSH.

---

## Phase 1 — Feasibility gate on NEW (~5-10 min) — DO NOT SKIP

Goal: prove the unknown Chunkserve/Datapasa network can reach Telegram, Docker Hub,
Hetzner, OpenCode **before** touching the old server. Old bot still serving.

```bash
# On NEW:
echo "=== 1. DNS ==="
getent hosts api.telegram.org || nslookup api.telegram.org
getent hosts download.docker.com || true
getent hosts inference.hetzner.com || true
getent hosts opencode.ai || true

echo "=== 2. Telegram egress (any HTTP response = PASS) ==="
curl -sS -m 15 -o /dev/null -w "telegram root: HTTP %{http_code} in %{time_total}s\n" https://api.telegram.org/ || echo "TELEGRAM_FAIL rc=$?"
# Expect HTTP 404 (or 200) — that IS success; it proves TLS + egress.
# FAIL looks like: Could not resolve host / Connection timed out / SSL handshake timeout.

echo "=== 3. Docker Hub pull path ==="
docker pull hello-world && docker run --rm hello-world | head -5

echo "=== 4. Hetzner Inference ==="
curl -sS -m 15 -o /dev/null -w "hetzner: HTTP %{http_code} in %{time_total}s\n" https://inference.hetzner.com/api/v1/models || echo "HETZNER_FAIL rc=$?"
# Expect HTTP 401/403 = reachable (rejected dummy/no key). 404 also proves egress.

echo "=== 5. OpenCode Zen + Go ==="
curl -sS -m 15 -o /dev/null -w "zen: HTTP %{http_code} in %{time_total}s\n" https://opencode.ai/zen/v1/models || echo "ZEN_FAIL rc=$?"
curl -sS -m 15 -o /dev/null -w "go:   HTTP %{http_code} in %{time_total}s\n" https://opencode.ai/zen/go/v1/models || echo "GO_FAIL rc=$?"
```

### Verdict criteria

- **MUST PASS: Telegram + Docker Hub.** If either fails → **ABORT. Do not proceed
  to Phase 2/3. Do not stop the old bot.**
- **SHOULD PASS: Hetzner + OpenCode.** If they fail but Telegram passes, the bot
  will poll but LLM features (`/summarize`, `/ask`, `/evaluate`, memory merges)
  will be degraded to whichever tier is reachable. Proceed only if you explicitly
  accept degraded mode for now.

### Abort path (Telegram unreachable)

1. Leave the old server alone — it is still serving.
2. On NEW, collect evidence for a provider ticket:
   `curl -v -m 15 https://api.telegram.org/ 2>&1 | head -40`,
   `traceroute api.telegram.org` (or `mtr -rwzb api.telegram.org`),
   `iptables -L -n; ufw status verbose`.
3. Ask Chunkserve/Datapasa support: "Is outbound 443 to api.telegram.org
   filtered/blocked?" If yes and unfixable → destroy the NEW VM, pick a different
   provider/region. No data touched, no downtime incurred.

---

## Phase 2 — Pre-stage on NEW while OLD still serves (~10-15 min)

Goal: minimize downtime by building everything except the DB.
**Explicitly forbidden in this phase: `docker compose up` on NEW** — it would
dual-poll with OLD and both get `409 Conflict`.

### 2.1 Discover source of truth on OLD

```bash
# On OLD:
ssh $OLD     # (from PowerShell)
pwd; ls -la
# Find the checkout — do not assume:
ls -d ~/pickme-summarization /root/pickme-summarization /opt/pickme* 2>/dev/null
find /root -maxdepth 3 -name docker-compose.yml 2>/dev/null

cd /root/pickme-summarization          # <-- replace with the real path (=$REPO_OLD)
git remote -v                           # RECORD origin URL
git branch --show-current; git rev-parse HEAD; git status --short --branch
ls -lh .env Dockerfile docker-compose.yml
ls -lh data/; du -sh data/; du -h data/pickme.db*
docker compose ps; docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
exit
```

Record: `$REPO_OLD`, remote URL, branch, HEAD hash, `data/` size.

### 2.2 Clone same code on NEW

```bash
# On NEW:
git ls-remote <REMOTE_URL> HEAD     # reachability test first
git clone <REMOTE_URL> pickme-summarization
cd ~/pickme-summarization
git rev-parse HEAD; git branch --show-current   # MUST match OLD's HEAD; if not:
#   git fetch origin && git checkout <same-branch-or-commit-as-old>
cat docker-compose.yml              # must show bot + ./data:/app/data + env_file: .env
```

> If `git ls-remote` fails from NEW (private repo without a key, or blocked host):
> fallback is a workstation hop — on OLD
> `tar --exclude=data --exclude=.git -czf /tmp/code.tgz -C $REPO_OLD .`, scp down
> then up, unpack on NEW. Prefer fixing clone (deploy key / HTTPS + PAT) instead.
> Do not proceed with mismatched code.

### 2.3 Copy `.env` securely (out-of-band — it is gitignored; clone does NOT bring it)

Workstation **PowerShell**:

```powershell
scp "${OLD}:/root/pickme-summarization/.env" "$Mig\.env.from-old"
# Validate WITHOUT printing secrets:
Get-Item "$Mig\.env.from-old" | Select-Object Length, LastWriteTime
Select-String -Path "$Mig\.env.from-old" -Pattern '^(TELEGRAM_BOT_TOKEN|HETZNER_API_KEY|OPENCODE_API_KEY)=' |
  ForEach-Object { $_ -replace '=.*','=***' }
# Must show all three keys present. Any missing → stop, fix OLD .env first.

scp "$Mig\.env.from-old" "${NEW}:/root/pickme-summarization/.env"
```

```bash
# On NEW:
cd ~/pickme-summarization
chmod 600 .env
grep -E '^(TELEGRAM_BOT_TOKEN|HETZNER_API_KEY|OPENCODE_API_KEY)=' .env | sed 's/=.*/=<set>/'
# Must list 3 redacted lines. Do NOT cat .env to terminal/logs.
docker compose config > /dev/null && echo "compose config OK"

# Authenticated Telegram probe (does NOT start polling — safe while OLD serves):
set -a; source .env; set +a
curl -sS -m 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"; echo
unset TELEGRAM_BOT_TOKEN HETZNER_API_KEY OPENCODE_API_KEY
# Expect {"ok":true,...}. 401 Unauthorized => wrong token copied — fix before cutover.
```

### 2.4 Data dir + pre-build (no start)

```bash
# On NEW:
cd ~/pickme-summarization
mkdir -p data && chown -R 1000:1000 data
ls -ldn data                              # must show 1000 1000
docker compose build
echo "PRE-STAGE DONE — DO NOT run 'docker compose up' yet (would 409 with OLD)"
```

Phase 2 done when: code HEAD matches OLD, `.env` present with 3 keys,
`compose config OK`, `getMe` → `ok:true`, image built, `docker ps` on NEW shows
**no** bot.

---

## Phase 3 — Cutover / downtime window (~10-20 min total, ~3-8 min downtime)

Start only when you can spend 20 uninterrupted minutes and test the bot live
from Telegram.

### 3.1 Stop OLD (downtime clock starts)

```bash
# On OLD:
cd /root/pickme-summarization            # $REPO_OLD
docker compose down
docker ps | cat                           # must NOT show the bot
ls -l --full-iso data/                    # RECORD sizes + timestamps
exit
```

**Why `down` before copy is mandatory (WAL safety):** SQLite in WAL mode =
`pickme.db` + `pickme.db-wal` (uncheckpointed transactions) + `pickme.db-shm`.
Copying while the container runs risks (a) copying `db` without latest `wal`
frames → silent data loss, (b) copying `wal` mid-write → torn snapshot,
(c) `shm` mismatch → `database is locked` / `file is not a database` on the new
host. `down` closes the SQLite connection cleanly (checkpoints WAL into the main
db) and prevents `restart: unless-stopped` from resurrecting a dual poller
mid-copy.

### 3.2 Copy DB two-hop

Workstation **PowerShell**:

```powershell
if (Test-Path "$Mig\data") { Remove-Item -Recurse -Force "$Mig\data" }

# OLD -> workstation (whole dir; -wal/-shm may or may not exist — both OK)
scp -r "${OLD}:/root/pickme-summarization/data" "$Mig\data"
Get-ChildItem "$Mig\data" | Format-Table Name, Length, LastWriteTime

# workstation -> NEW
scp -r "$Mig\data\*" "${NEW}:/root/pickme-summarization/data/"
```

Fallback (direct server-to-server, only if the workstation hop is impossible):

```bash
# On NEW (you will type OLD's root password ON the new server — prefer two-hop):
scp -r root@OLD_IP:/root/pickme-summarization/data/* ./data/
```

### 3.3 Integrity + ownership on NEW, then start

```bash
# On NEW:
cd ~/pickme-summarization
ls -lh data/; du -sh data/        # must roughly match OLD's record; else STOP, re-copy

chown -R 1000:1000 data           # FIRST — else crash-loop
find data/ ! -user 1000 -ls       # must print nothing

# Integrity check without installing sqlite3 (host python3 always exists):
python3 -c "import sqlite3; con=sqlite3.connect('file:data/pickme.db?mode=ro', uri=True); print('integrity:', con.execute('PRAGMA integrity_check;').fetchone()); print('tables:', con.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1\").fetchall())"
# Must print integrity: ('ok',). If not => DO NOT START. Re-copy from OLD or Rollback.

stat -c '%y %n %s' data/pickme.db* ; date -u    # baseline for ingest proof

docker compose up -d
sleep 10
docker compose ps
docker compose logs --tail=100 bot
```

Log triage (first 60 s):

- GOOD: polling started / first `getUpdates` success, no traceback.
- `Conflict: terminated by other getUpdates` / 409 → dual poller! `docker compose
  down` on NEW, verify OLD is really down, then `up -d` on NEW again.
- `sqlite3.OperationalError: unable to open database file` → ownership: `down`,
  `chown -R 1000:1000 data`, `up -d`.
- `Unauthorized` → bad TELEGRAM_BOT_TOKEN: `down`, re-copy `.env`, `up -d`.
- Repeated 429/timeouts to inference.hetzner.com / opencode.ai → network or
  quota; polling still works, LLM degraded (Phase 1 verdict applies).

### 3.4 Live verification checklist (OLD stays stopped)

All must pass before Phase 4:

- [ ] `docker inspect -f '{{.RestartCount}} {{.State.Status}}' $(docker compose ps -q bot)`
      → `0 running` (never a `restarting` loop)
- [ ] No `409`/`Conflict` in `docker compose logs --since=5m bot`
- [ ] From a group where the bot is a member: send a plain test message → then
      `/status` → expect a reply (proves inbound + outbound + `.env` loaded)
- [ ] Ingest proof: send another plain message, wait 15 s, re-run
      `stat -c '%y %n %s' data/pickme.db*` — mtime/size must have advanced
- [ ] LLM path: `/summarize 5` or `/ask migration test` → expect a model answer
      (proves LLM egress). Failure with polling OK = degraded mode — accept or
      Rollback per Phase 1 verdict.
- [ ] Soak: normal traffic 15-30 min before decommissioning OLD.

If any check fails persistently (>5 min) → **Rollback. Do NOT wipe OLD.**
Downtime clock stops at the first successful `/status` reply.

---

## Phase 4 — Decommission OLD (~10 min + provider click)

Only after the Phase 3 checklist is fully green for 15-30 min.

### 4.1 Final insurance tarball (on OLD, before wipe)

```bash
# On OLD:
cd /root/pickme-summarization
docker compose ps    # confirm still down
tar -czf ~/pickme-final-$(date -u +%Y%m%dT%H%M%SZ).tgz -C /root/pickme-summarization data .env docker-compose.yml
ls -lh ~/pickme-final-*.tgz; sha256sum ~/pickme-final-*.tgz
```

```powershell
# Workstation:
scp "${OLD}:~/pickme-final-*.tgz" "$Mig\"
Get-ChildItem "$Mig\pickme-final-*.tgz" | Format-Table Name, Length
# Keep OFFLINE 7-30 days (contains secrets), then securely delete.
```

### 4.2 Wipe OLD + provider destroy (both)

```bash
# On OLD:
cd /root/pickme-summarization
docker compose down -v
rm -rf /root/pickme-summarization
rm -f ~/pickme-final-*.tgz          # only AFTER confirming the workstation copy
docker system prune -af
history -c; history -w; rm -f ~/.bash_history
```

Then **provider-level destroy (YES):** file deletion on SSD is not secure-erase
and does not stop billing. In the old provider's panel: destroy the VM, delete
snapshots/backups/firewall rules that may contain `.env`/DB. That is the real
decommission; `rm -rf` only prevents an accidental restart before destroy.

Optional second insurance copy on NEW:

```bash
# On NEW:
tar -czf ~/pickme-migrated-$(date -u +%Y%m%dT%H%M%SZ).tgz -C ~/pickme-summarization data .env docker-compose.yml
```

---

## Rollback plan (use BEFORE old is wiped)

Trigger: any Phase 3 verification failure on NEW you cannot fix in ~5 min.

```bash
# 1. Stop NEW immediately (ends 409 risk):
#    on NEW: cd ~/pickme-summarization && docker compose down && docker ps | cat
# 2. Restart OLD (its DB was never written after 3.1 — copy was read-only FROM it):
#    on OLD: cd /root/pickme-summarization && docker compose up -d && sleep 10 && docker compose logs --tail=50 bot
```

Verify: `/status` + a plain test message in the group → bot replies; `stat
data/pickme.db*` on OLD advances.

**Lost-data ceiling:** messages sent during the failed window were ingested by
neither bot (Telegram usually redelivers pending long-poll updates on the next
`getUpdates`, but don't rely on it). Anything NEW partially ingested lives only
in NEW's divergent `data/` — **discard it; never copy NEW→OLD** (that forks the
SQLite history / split-brain). Note the window timestamps (`date -u` at stop and
restart) to audit any gap. After rollback, debug NEW offline and reschedule.

---

## Gotchas

1. **409 dual-polling:** exactly one `getUpdates` consumer allowed. Never `up`
   NEW while OLD is up — not even "for 10 seconds to test".
2. **WAL copy consistency:** live `db + -wal + -shm` is not a consistent
   snapshot; `down` first. If `-wal`/`-shm` are absent after `down`, that's
   normal (fully checkpointed) — copying just `pickme.db` is correct.
3. **uid 1000 chown:** root-scp'd `data/` → crash-loop. Always
   `chown -R 1000:1000 data` after any scp as root; verify `ls -ln`.
4. **`.env` out-of-band:** gitignored, never in clone. Validate with redacted
   grep, never paste tokens into logs/tickets.
5. **Git remote reachability from NEW:** `git ls-remote` before assuming clone
   works; private repos need a deploy key/PAT on NEW.
6. **Docker Hub rate limits on fresh IPs:** unauthenticated ~100/6h per IP; a
   shared Chunkserve egress IP may already be throttled → `toomanyrequests`.
   Mitigate: `docker login` with a free Docker ID, or retry after the window.
7. **Clock skew mimics network block:** unsynced clock → TLS cert errors against
   Telegram/Hetzner. Check `timedatectl` in Phase 0. Keep `DATA_DIR=/app/data`
   in `.env` as-is (host path is the compose bind mount, not `.env`).

---

## Time estimates

| Phase | Wall time | Downtime |
|---|---|---|
| Step 0 SSH keys | 5 min | 0 |
| Phase 0 prep NEW | 15-25 min | 0 |
| Phase 1 feasibility gate | 5-10 min | 0 |
| Phase 2 pre-stage | 10-15 min | 0 |
| Phase 3 cutover | 10-20 min | **3-8 min** |
| Soak | 15-30 min | 0 |
| Phase 4 backup + wipe + destroy | 10 min | 0 |
| Rollback (if needed) | ~5 min | +~5 min window |
| **Total (happy path)** | **~70-110 min** | **~3-8 min** |

---

## Credentials & LLM-assisted execution

### Recommended: SSH keys — required if you want an LLM agent to run this runbook

Password prompts are interactive; an AI agent (opencode etc.) driving `ssh`/`scp`
from your workstation **cannot type passwords**. Keys make every command here
non-interactive:

1. You run **Step 0** yourself once (the only place passwords are typed — three
   prompts total).
2. From then on `ssh $OLD` / `ssh $NEW` authenticate silently; the agent executes
   any phase through its shell tool.

### What the model gets vs. never gets

| Item | How to hand it over | Why |
|---|---|---|
| This runbook | In-repo `migration-runbook.md`; the agent reads the file | No secrets inside |
| Server IPs | In chat, or local gitignored `targets.ps1` (better) | Chat logs persist; IPs are mildly sensitive |
| Root passwords | **Never** paste into any model chat/ticket/file the model reads | Conversation logs are retained elsewhere; passwords become a long-lived liability |
| `~/.ssh/id_ed25519` private key | Never leaves the workstation; ssh uses it implicitly | The agent's shell authenticates without ever seeing key material |
| Bot `.env` | Moves server→workstation→server via scp; agent sees filenames + redacted checks only | Tokens are secrets |

### Division of labor

- **You (human):** Step 0 key install; Phase 1 verdict if LLM tiers fail
  (accept degraded or abort); Phase 3 live Telegram test messages; Phase 4
  provider-panel destroy; approval before each destructive step.
- **Agent (LLM):** Phase 0 prep, Phase 1 probes, Phase 2 pre-stage, Phase 3
  stop/copy/integrity/start commands — pausing for your approval before
  anything destructive or irreversible (`compose down` on OLD, first `up -d` on
  NEW, any wipe).

### Post-migration hardening (after Phase 4)

- On NEW, disable password SSH now that keys work (test from a second terminal
  before closing the first):
  `sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && systemctl restart ssh`
- Destroying the OLD server (Phase 4) retires its root password.
- Delete the final tarball (contains `.env`) after 7-30 days.

### If you insist on passwords (no keys)

The agent cannot drive password-based ssh non-interactively, and Windows lacks
`sshpass`. Your options (both worse than keys): run those few commands yourself
as the agent prints them; or WSL + `sshpass -p '…'` — which puts the root
password into shell history and process lists. Use keys.
