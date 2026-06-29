# Deploying the Foundry Workshop as a long-running build server

This kit runs the Workshop (`webapp/server.py`) as an always-on service that can
drive **complex, multi-day builds** and **survive restarts** — on either a
**Raspberry Pi** (systemd) or an **Apple-silicon Mac** (launchd). The two paths
share one config file; only the service wrapper and a little host tuning differ.

## Why it survives a long run

The engine is already built for this — it just needs a durable home and something
to keep it alive:

- **Durable, resumable state.** With `FOUNDRY_STATE_DIR` on storage that survives a
  reboot, the checkpointer + content store let a build **resume from its last
  checkpoint** after a crash or restart. The server resumes in-flight builds
  automatically on startup.
- **Restart supervision.** systemd `Restart=always` / launchd `KeepAlive` relaunch
  the server if it dies; the build picks up where it left off.
- **Host safety.** A concurrency cap (`WORKSHOP_MAX_CONCURRENT_BUILDS`) queues extra
  builds instead of running them all at once and exhausting RAM.
- **Provider robustness over days.** A client-side rate limiter + 429/Retry-After
  backoff ride out throttling; the SDK client rebuilds on auth failure / TTL so a
  rotated or expired credential doesn't end the run.

The host **orchestrates**; the model runs **remotely** via a hosted API backend
(`anthropic` / `openai` / `claude-cli` / `codex`). The host's main local cost is
running the generated project's **gate test suites** — so install those toolchains
and keep parallelism modest.

## Shared config

Copy `foundry.env.example` → `foundry.env` and edit it once. Key values:

| Setting | Pi (8 GB) | Mac (M-series, 24 GB) |
|---|---|---|
| `WORKSHOP_MAX_CONCURRENT_BUILDS` | `1` | `3`–`4` |
| `FOUNDRY_MAX_SLOTS` | `2` | `4`–`6` |
| `TMPDIR` | `/var/foundry-tmp` (tmpfs) | unset (SSD is fine) |
| `FOUNDRY_STATE_DIR` / `WORKSHOP_OUTPUT_DIR` | on the SD/SSD | on the internal SSD |
| `FOUNDRY_MODEL_BACKEND` | hosted API (not local ollama) | hosted API (or local ollama for small models) |
| `FOUNDRY_CLIENT_TTL_S` | `3600` | `3600` |
| `FOUNDRY_COST_CAP_MICROUSD` | set a ceiling | set a ceiling |

Set `FOUNDRY_WORKSHOP_TOKEN` whenever you bind beyond localhost (`WORKSHOP_HOST=0.0.0.0`).

---

## Raspberry Pi (systemd)

```bash
# 1. config
sudo mkdir -p /etc/foundry /var/lib/foundry
sudo cp deploy/foundry.env.example /etc/foundry/foundry.env
sudo nano /etc/foundry/foundry.env          # token, API key, paths, Pi values above

# 2. tmpfs build scratch (protects the SD card)
sudo tee -a /etc/fstab < deploy/pi/tmpfs.fstab.snippet
sudo mkdir -p /var/foundry-tmp && sudo mount /var/foundry-tmp

# 3. zram swap, and stop swapping to the card
sudo apt install -y zram-tools
sudo cp deploy/pi/zramswap.conf /etc/default/zramswap && sudo systemctl restart zramswap
sudo dphys-swapfile swapoff && sudo systemctl disable dphys-swapfile

# 4. the service  (edit ExecStart's uv path first: `which uv`)
sudo cp deploy/pi/foundry-workshop.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now foundry-workshop
journalctl -u foundry-workshop -f

# 5. hourly state backup (so a dead card = restore-and-resume)
sudo install -m755 deploy/pi/backup.sh /usr/local/bin/foundry-backup
echo 'FOUNDRY_BACKUP_DST=/mnt/backup/foundry' | sudo tee -a /etc/foundry/foundry.env
sudo cp deploy/pi/foundry-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now foundry-backup.timer
```

**Using the SD card** (instead of a USB SSD) is fine if you do the above: the
tmpfs keeps the heavy churn off the card, leaving only small (KB–MB) durable
writes. Add a **high-endurance card**, a **UPS / clean-shutdown hat** (power loss
mid-write is the #1 SD killer), and the **backup timer**, and it holds up.

Hardware checklist: high-endurance SD (or USB SSD), UPS/shutdown hat, and the
ARM64 gate toolchains your builds need (`node`, `python`, etc.).

---

## Apple-silicon Mac (launchd)

```bash
# 1. config
mkdir -p ~/.config/foundry
cp deploy/foundry.env.example ~/.config/foundry/foundry.env
$EDITOR ~/.config/foundry/foundry.env       # token, API key; Mac values above (no TMPDIR needed)

# 2. wrapper + agent  (edit the two CHANGEME paths in the plist to your checkout)
chmod +x deploy/mac/run-workshop.sh
cp deploy/mac/com.foundry.workshop.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.foundry.workshop.plist
tail -f /tmp/foundry-workshop.{out,err}
```

The wrapper runs the server under **`caffeinate -is`** so the Mac won't sleep
mid-build. For a multi-day run on a laptop, **keep the lid open** (closing it
sleeps the machine regardless of `caffeinate`), or run clamshell on AC with an
external display. Back up `FOUNDRY_STATE_DIR` with Time Machine or `rsync`.

The Mac is the more capable host — more RAM/cores, fast durable SSD, and you can
develop and run on the same box. Its only gotchas are **don't let it sleep** and
**launchd instead of systemd**.

---

## Verifying

Run the acceptance harness on the target host **before** trusting a real multi-day
job — it drives the full server process on the offline `mock` backend (no API key):

```bash
python scripts/soak_resume_check.py            # soak (RAM plateau) + resume checks
python scripts/soak_resume_check.py --mode soak --builds 30
```

- **soak** runs N builds back-to-back and samples the server's RSS; it should
  **plateau**, proving the log-sweeper bounds memory build-over-build (fails on
  >50% growth — a leak).
- **resume** starts a paused build, `kill -9`s the server, restarts it against the
  same state dir, and asserts the build **survives and resumes** with its event
  history intact.

Manual smoke:

- `curl -s localhost:8765/ -o /dev/null -w '%{http_code}\n'` → `200`.
- Start a build from the UI; reboot the host mid-build; confirm it resumes from the
  last checkpoint (the quest log shows a "Resuming after a restart…" line).
- Watch memory (`htop`) during gates and lower `WORKSHOP_MAX_CONCURRENT_BUILDS` /
  `FOUNDRY_MAX_SLOTS` if it gets tight.
