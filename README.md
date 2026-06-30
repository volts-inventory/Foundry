# Foundry

A graph-based, multi-agent software factory. Give it a one-line request; it runs
a full product → architecture → engineering → QA → security → gates pipeline and
writes a GitHub-ready repo (code + docs). Use it two ways:

- **CLI** — `python -m foundry.cli "<request>"` → a repo on disk.
- **Workshop UI** — a localhost web app that drives the same factory as a game.

Pick a model backend — Claude Code (`claude -p`, a Max plan) or the Anthropic /
OpenAI APIs — and it writes real code. Examples below use `claude-cli`.

---

## Quick start — run the Workshop locally

Open **Terminal**, go to this folder, and run (Mac shown; Pi uses
`foundry.pi.env.example`):

```bash
cd ~/Coders                                            # wherever you cloned it
cp deploy/foundry.mac.env.example deploy/foundry.env   # 1. Mac preset — Claude Code, localhost
make token                                             # 2. mint a Claude Code token (Max plan)
open -e deploy/foundry.env                             #    …paste it as CLAUDE_CODE_OAUTH_TOKEN
./deploy/run.sh                                        # 3. start it → http://localhost:8765
```

The Mac preset runs on **Claude Code** (`claude -p`, your Max plan — no API billing).
Prefer the metered API instead? Set `FOUNDRY_MODEL_BACKEND=anthropic` and an
`ANTHROPIC_API_KEY` in the env file (see §4).

Leave the Terminal window open while it runs. To keep it running after closing the
window, or to set it up as an auto-restarting service, see
[§10](#10-run-it-as-a-long-lived-server) and [`deploy/README.md`](deploy/README.md).

---

## CLI at a glance

Every flag, with what it does, inline. Defaults are shown in each comment. Set a
`--backend` (the model egress) so it writes real code.

```bash
python -m foundry.cli [OPTIONS] "<one-line request>"   # build a GitHub-ready repo from one sentence

# ── Release posture ───────────────────────────────────────────────────────────
  --mode <assistant|gated|autonomous>  # trust posture: advisory PR · full gates+sign-off · hands-off deploy   (default: assistant)

# ── Model egress & code generation (orthogonal seams) ─────────────────────────
  --backend <claude-cli|anthropic|openai|codex|ollama-gemma>  # WHERE model calls go (the "brain")   (set one; e.g. claude-cli)
  --adapter <stub|raw-react>                                # WHAT writes the code (the "hands"); usually OMIT — it's DERIVED from --backend
                                                            #   omitted ⇒ any backend→raw-react (real code); stub is no-op
                                                            #   raw-react writes code via `claude -p` (claude-cli) or the Anthropic API (anthropic)

# ── Inputs ────────────────────────────────────────────────────────────────────
  --spec <FILE|DIR>          # ground the build with a spec beyond the one-liner; REPEATABLE   (default: none)
  --packs <name1,name2>      # engage domain packs: regulated|ml_data|frontend; a pack may VETO a mode   (default: none)
  --docs <DIR>               # write the generated doc set (PRD / design / ops) here           (default: none)

# ── Output & durable state ────────────────────────────────────────────────────
  --out <DIR>                # write the generated repo here                                   (default: none — report only)
  --state-dir <DIR>          # durable, restart-survivable state (overrides FOUNDRY_STATE_DIR) (default: in-memory)
  --thread-id <NAME>         # run identity; seeds idempotency keys + resume                   (default: cli-run)

# ── Budgets & flow control ────────────────────────────────────────────────────
  --max-fix-attempts <N>     # auto-fix retries per FAILING gate (N ≥ 1)                        (default: 3)
  --max-slots <N>            # parallelism CEILING; pool usually auto-sizes (N ≥ 1)             (default: auto)
  --cost-cap-microusd <N>    # global spend envelope in micro-USD, for metered backends         (default: none)
  --checkpoints [all|product,design,release]  # pause at stage boundaries (CLI auto-approves)   (default: off)

# ── Subcommand: evolve an already-released repo (re-gate a change) ─────────────
python -m foundry.cli evolve <prior-repo> "<change>" [OPTIONS]
  --apply <DIR>              # overlay these changed files onto the prior tree
  --guardian-approve         # authorize a protected criterion / oracle / gate edit (else blocked)
  --strict                   # exit non-zero if the re-gate REFUSES to release (for CI)

# Exit codes:  0 = passed (PR-ready)  ·  1 = halted / failed  ·  2 = usage error
# Run with no arguments to print the full usage text:  python -m foundry.cli
```

---

## Contents

1. [Requirements](#requirements)
2. [Setup](#1-setup)
3. [Quickstart (CLI)](#2-quickstart-cli)
4. [Picking a mode](#3-picking-a-mode)
5. [Setting the backend (real models)](#4-setting-the-backend-real-models)
6. [Choosing a coding substrate (`--adapter`)](#5-choosing-a-coding-substrate---adapter)
7. [Feeding a spec](#6-feeding-a-spec)
8. [Common flags](#7-common-flags)
9. [Evolve an existing repo](#8-evolve-an-existing-repo)
10. [Workshop UI](#9-workshop-ui)
11. [Run it as a long-lived server](#10-run-it-as-a-long-lived-server)
12. [Develop](#11-develop)
13. [Configuration reference](#configuration-reference)

---

## How it works

```
"<one-line request>"
        │
        ▼
  product ──▶ architecture ──▶ engineering ──▶ QA ──▶ security ──▶ gates
        │            │              │            │         │          │
        └──────── one graph; agents share state, parallelize, and re-loop ───────┘
                                                                       │
                                                                       ▼
                                                          ./out  (code + docs + report)
```

Two seams are deliberately independent, and worth internalizing before you start:

- **`--backend`** — the model egress ("the brain"). It decides *where model calls
  go* — Claude Code (`claude -p`) or the Anthropic/OpenAI APIs — all routed through
  the single-egress proxy.
- **`--adapter`** — the coding substrate ("the hands"). It decides *what actually
  writes the code*. You rarely set this: when omitted it is **derived from the
  backend** — any backend → `raw-react` (real code) — so choosing a brain writes real
  code on its own. (`stub` is a no-op substrate.) `raw-react` is the universal
  completion substrate: it writes code via `claude -p` under `claude-cli`, and via
  the Anthropic API under `anthropic`.

> **You usually only set `--backend`.** Pick a real backend and you get real code,
> because the adapter follows it. Reach for `--adapter` only to override — e.g.
> `--adapter stub` to exercise the proxy/lineage path *without* generating code.

---

## Requirements

- Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/).
- A real backend (optional): an Anthropic API key, an OpenAI API key, the `claude`
  CLI on a Max plan, the `codex` CLI, or a local Ollama.

---

## 1. Setup

```bash
# 1. Install deps + the project into a local venv
make setup            # == uv sync

# 2. Verify the toolchain (lint + typecheck + tests)
make check
```

---

## 2. Quickstart (CLI)

Each command writes a complete repo to `--out` and prints a pass/fail report.

```bash
# 1. Simplest possible run (assistant mode) → repo in ./out
uv run python -m foundry.cli "build a tiny URL-shortener service" --backend claude-cli --out ./out

# 2. Same thing via make (REQUEST/OUT are overridable vars)
make run REQUEST="build a CSV-to-JSON converter CLI" OUT=./out

# 3. Inspect the result
ls ./out                 # source tree at the root
cat ./out/README.md      # generated README
ls ./out/docs            # PRD, design, ops docs

# 4. Exit code tells you what happened
echo $?                  # 0 = passed (PR-ready) · 1 = halted · 2 = usage error
```

---

## 3. Picking a mode

`--mode` sets the deployment-trust posture (default `assistant`).

```bash
# 1. assistant — advisory review; emits a PR for a human to merge (default)
uv run python -m foundry.cli "build a markdown linter" --mode assistant --out ./out

# 2. gated — full gate set + recorded human sign-off + WORM evidence pack
uv run python -m foundry.cli "build a payments webhook handler" --mode gated --out ./out

# 3. autonomous — hands-off build → admit → deploy; admission is the only gate
uv run python -m foundry.cli "build a status-page service" --mode autonomous --out ./out
```

---

## 4. Setting the backend (real models)

A backend is the model egress. Set it three ways — last one wins:

```bash
# 1. Per command — the --backend flag (overrides everything)
uv run python -m foundry.cli "build a todo API" --backend anthropic --out ./out

# 2. For the shell session — the FOUNDRY_MODEL_BACKEND env var
export FOUNDRY_MODEL_BACKEND=anthropic
uv run python -m foundry.cli "build a todo API" --out ./out      # uses anthropic

# 3. Persistently — put it in .env (read by make)
make env                                  # scaffold .env if missing
echo "FOUNDRY_MODEL_BACKEND=anthropic" >> .env
make run REQUEST="build a todo API"       # uses anthropic
```

### Backend settings

| Backend | What it is | Required setting(s) | How to get it |
|---|---|---|---|
| `claude-cli` | `claude -p` on a Max plan (no API billing) | `CLAUDE_CODE_OAUTH_TOKEN` | `make token` |
| `anthropic` | Claude API (SDK, direct) | `ANTHROPIC_API_KEY` | console.anthropic.com |
| `openai` | OpenAI API (SDK `chat.completions`, direct) | `OPENAI_API_KEY` | platform.openai.com |
| `codex` | codex CLI (`codex exec`) | `OPENAI_API_KEY` | platform.openai.com |
| `ollama-gemma` | Local Ollama endpoint | `FOUNDRY_OLLAMA_URL` | run Ollama locally |

Each provider is reachable two ways — a **direct SDK** backend and a **CLI** backend:
Anthropic via `anthropic` (API) or `claude-cli` (`claude -p`); OpenAI via `openai`
(API) or `codex` (`codex exec`). The direct-SDK backends keep the grammar-constrained
verdict channel (native JSON-schema); the CLIs degrade it to fail-closed JSON parsing.

Optional, any real backend — pin the exact model id per tier when your egress
serves one the default map doesn't name (else the fail-closed drift policy
rejects the call):

| Setting | Tier | Example |
|---|---|---|
| `FOUNDRY_MODEL_T1` | heaviest reasoning | `claude-opus-4-8` |
| `FOUNDRY_MODEL_T2` | mid | `claude-sonnet-4-6` |
| `FOUNDRY_MODEL_T3` | light / cheap | `claude-haiku-4-5` |

> Don't set both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` — the key
> outranks the token and bills the API.

### Per-backend setup (credentials)

```bash
# 1. claude-cli — `claude -p` on a Max subscription. Mint a token, add to .env.
make token                                # logs in; prints CLAUDE_CODE_OAUTH_TOKEN
echo "CLAUDE_CODE_OAUTH_TOKEN=<token>" >> .env
uv run python -m foundry.cli "build a todo API" --backend claude-cli --out ./out

# 2. anthropic — Claude API (direct SDK). Set ANTHROPIC_API_KEY.
export ANTHROPIC_API_KEY=sk-ant-...
uv run python -m foundry.cli "build a todo API" --backend anthropic --out ./out

# 3. openai — OpenAI API (direct SDK chat.completions). Set OPENAI_API_KEY.
export OPENAI_API_KEY=sk-...
uv run python -m foundry.cli "build a todo API" --backend openai --out ./out

# 4. codex — codex CLI (codex exec). Set OPENAI_API_KEY.
export OPENAI_API_KEY=sk-...
uv run python -m foundry.cli "build a todo API" --backend codex --out ./out

# 5. ollama-gemma — local Ollama. Point at the endpoint.
export FOUNDRY_OLLAMA_URL=http://localhost:11434
uv run python -m foundry.cli "build a todo API" --backend ollama-gemma --out ./out
```

Any backend writes real code automatically — the adapter is derived as `raw-react`,
so you don't need a second flag (override only when you want something else; see §5).
In the Workshop UI, the backend is a dropdown; `FOUNDRY_MODEL_BACKEND` only sets
which option is preselected (§9).

---

## 5. Choosing a coding substrate (`--adapter`)

The adapter is what actually writes the code. **You normally omit it** — it's
derived from `--backend` (any backend → `raw-react`), so picking a backend already
writes real code. Set `--adapter` only to override that default.

```bash
# 1. Derived (recommended) — omit --adapter; a real backend ⇒ raw-react writes code
uv run python -m foundry.cli "build a dice roller" --backend anthropic --out ./out

# 2. Override: stub — force the deterministic no-op even on a real backend
#    (exercises the proxy / lineage / determinism path WITHOUT generating code)
uv run python -m foundry.cli "build a dice roller" \
  --backend anthropic --adapter stub --out ./out

# 3. Override: raw-react explicitly — the universal completion substrate over ANY
#    backend. `--backend claude-cli` here writes code via `claude -p` (completion only).
uv run python -m foundry.cli "build a dice roller" \
  --backend claude-cli --adapter raw-react --out ./out
```

**The two substrates:**

| Adapter | Kind | Writes code? | Notes |
|---|---|---|---|
| `stub` | no-op | no | structure only (smoke/CI; exercises the proxy without generating code) |
| `raw-react` | raw completion, **any** backend | yes | the derived default; writes code via `claude -p` (`claude-cli`) or the Anthropic API (`anthropic`) |

---

## 6. Feeding a spec

Ground the build with detail beyond the one-liner. `--spec` accepts a file or
directory and may repeat.

```bash
# 1. Co-generate a spec from your request, then edit it
make spec REQUEST="build a feature-flag service"   # → writes spec.md
$EDITOR spec.md

# 2. Build from that spec
uv run python -m foundry.cli "build a feature-flag service" --spec spec.md --out ./out

# 3. Ingest multiple spec files / a docs folder
uv run python -m foundry.cli "port the billing module" \
  --spec requirements.md --spec ./design-docs --out ./out
```

---

## 7. Common flags

```bash
# 1. Domain packs — engage extra guidance/governance (a pack can veto a mode)
uv run python -m foundry.cli "build a clinical intake form" \
  --packs regulated,ml_data --out ./out

# 2. Auto-fix budget — retries per failing gate (default 3)
uv run python -m foundry.cli "build a parser" --max-fix-attempts 5 --out ./out

# 3. Cost ceiling — global spend envelope in micro-USD (metered backends)
uv run python -m foundry.cli "build a parser" \
  --backend anthropic --cost-cap-microusd 500000 --out ./out      # $0.50

# 4. Parallelism ceiling — usually auto; lower it on a constrained box
uv run python -m foundry.cli "build a parser" --max-slots 2 --out ./out

# 5. Checkpoints — pause at stage boundaries (CLI auto-approves all)
uv run python -m foundry.cli "build a parser" --checkpoints all --out ./out
uv run python -m foundry.cli "build a parser" --checkpoints product,release --out ./out

# 6. Durable state — survive restarts / resume by thread id
uv run python -m foundry.cli "build a parser" \
  --state-dir ./.state --thread-id parser-v1 --out ./out

# 7. See every flag
uv run python -m foundry.cli        # prints full usage
```

---

## 8. Evolve an existing repo

Re-enter a released codebase as a maintained change: re-verify, re-gate, re-admit
(fail-closed). The change ships only if it re-passes.

```bash
# 1. Apply a change overlaid on the prior tree
uv run python -m foundry.cli evolve ./out "add per-IP rate limiting" --apply ./changes

# 2. Approve a protected criterion/oracle/gate edit (otherwise blocked)
uv run python -m foundry.cli evolve ./out "loosen the auth gate" \
  --apply ./changes --guardian-approve

# 3. Hard-fail in CI when the re-gate refuses to release
uv run python -m foundry.cli evolve ./out "add caching" --apply ./changes --strict
echo $?                              # non-zero if NOT released
```

---

## 9. Workshop UI

A localhost web app that runs the real factory and visualizes it. Floors are
projects; iterate to add releases; browse output and release history; approve,
request changes, or reject at checkpoints.

```bash
# 1. Serve it on http://localhost:8765  (or: make serve)
uv run python -m webapp.server

# 2. Expose it to your LAN
WORKSHOP_HOST=0.0.0.0 uv run python -m webapp.server

# 3. Default to a real backend in the UI picker
FOUNDRY_MODEL_BACKEND=claude-cli uv run python -m webapp.server

# 4. Require a shared token for any mutating action
FOUNDRY_WORKSHOP_TOKEN=secret uv run python -m webapp.server
#    then open  http://localhost:8765/?token=secret
```

**Sandboxed test gate (untrusted-code isolation).** Generated tests run inside a
**namespace sandbox** by default — an unprivileged Linux `unshare` (user + net +
mount + pid + ipc + uts) — so model code runs with **no network** (can't exfiltrate),
isolated mounts/processes, and resource caps, while the pinned harness fences our
source off `sys.path` (a generated module can't import or shadow `foundry.*`). No
Docker daemon, no image — just the kernel. If the host can't create unprivileged
user namespaces the gate **fails closed (ERROR)** rather than run untrusted code
unsandboxed; opt out to the in-process-only harness (still sys.path-fenced) with:

```bash
export FOUNDRY_GATE_SANDBOX=inprocess
```

---

## 10. Run it as a long-lived server

The Workshop is built to run unattended for long, complex builds — including
**multi-day runs** — and to **survive restarts**: with a durable state dir it
resumes an in-flight build from its last checkpoint, and the SSE event log replays
in full on reconnect. The [`deploy/`](deploy/) kit ships one shared env file and two
ways to run it — **on demand** (launch by hand) or as a **service** (auto-restart on
crash/reboot) on a **Raspberry Pi** (systemd) or **Apple-silicon Mac** (launchd).

```bash
# 1. Copy the preset for your host (values pre-filled); paste your API key
cp deploy/foundry.mac.env.example deploy/foundry.env   # Mac   (Pi: foundry.pi.env.example)

# 2a. On demand — no service. Foreground (Ctrl-C to stop); use tmux/nohup to detach.
./deploy/run.sh

# 2b. …or as a managed service (see deploy/README.md for the walkthrough)
#     Pi  → deploy/pi/foundry-workshop.service     (systemd, Restart=always)
#     Mac → deploy/mac/com.foundry.workshop.plist  (launchd, KeepAlive + caffeinate)

# 3. Validate the host BEFORE a real multi-day job — offline, no key needed:
python scripts/soak_resume_check.py   # RAM-plateau soak + kill/restart resume check
```

> **On demand vs a service:** a service only adds **auto-restart on crash/reboot**.
> On demand keeps everything else — and since the server **resumes from its durable
> checkpoint on the next start**, you just re-run `./deploy/run.sh` after a crash and
> it picks up where it left off. Services suit truly unattended runs; on demand suits
> "start it when I want, stop it when I'm done."

For a long run, use a **hosted** backend with a **cost cap**, set
`WORKSHOP_MAX_CONCURRENT_BUILDS` to your host's size (Pi `1`, M-series Mac `3–4`),
and — if your plan needs it — the provider throttle (`FOUNDRY_MAX_RPM`). The server
**bounds its own memory** over time (finished builds' in-memory logs are trimmed; the
proxy manifest is capped) and the **SDK client auto-refreshes** so a rotated/expired
credential doesn't end the run.

> **macOS:** the gate sandbox is Linux-only (`unshare`), so on a Mac the test gate
> **fails closed** unless you set `FOUNDRY_GATE_SANDBOX=inprocess` — which runs
> generated tests `sys.path`-fenced but **without** the network/namespace isolation
> a Linux host gets. Prefer the Pi (or any Linux host) when running untrusted builds.

See **[deploy/README.md](deploy/README.md)** for storage durability (SD vs SSD),
tmpfs/zram tuning, backups, and the full per-host checklist.

---

## 11. Develop

```bash
make lint            # ruff
make fix             # ruff autofix + format
make typecheck       # mypy (strict)
make test            # full suite, parallel
make test-fast       # skips slow end-to-end factory passes
make check           # lint + typecheck + test (the CI gate)
make clean           # remove caches + ./.run output
```

---

## Configuration reference

| Variable | Purpose | Default |
|---|---|---|
| `FOUNDRY_MODEL_BACKEND` | Default backend | set one (e.g. `claude-cli`) |
| `FOUNDRY_CODING_ADAPTER` | Default adapter | derived from backend |
| `ANTHROPIC_API_KEY` | `anthropic` backend key | — |
| `CLAUDE_CODE_OAUTH_TOKEN` | `claude-cli` backend (via `make token`) | — |
| `OPENAI_API_KEY` | `openai` + `codex` backend key | — |
| `FOUNDRY_OLLAMA_URL` | `ollama-gemma` endpoint | — |
| `FOUNDRY_MODEL_T1/T2/T3` | Pin exact model id per tier | curated map |
| `FOUNDRY_MAX_SLOTS` | Parallelism ceiling | auto |
| `FOUNDRY_COST_CAP_MICROUSD` | Global spend envelope | — |
| `FOUNDRY_STATE_DIR` | Durable state dir | in-memory |
| `FOUNDRY_GATE_SANDBOX` | Run generated tests in a namespace sandbox (default) or `inprocess` to opt out | sandbox |
| `WORKSHOP_HOST` / `WORKSHOP_PORT` | UI bind address / port | `127.0.0.1` / `8765` |
| `WORKSHOP_OUTPUT_DIR` | UI build output root | `.run/web` |
| `WORKSHOP_STATE_DIR` | UI durable state dir (resume) | `WORKSHOP_OUTPUT_DIR/state` |
| `FOUNDRY_WORKSHOP_TOKEN` | Shared token for UI mutations | off (localhost-only) |

**Long-lived server** (see [deploy/](deploy/) — for a Pi/Mac running for days):

| Variable | Purpose | Default |
|---|---|---|
| `WORKSHOP_MAX_CONCURRENT_BUILDS` | Cap builds running at once; extras queue | `2` (Pi `1`, Mac `3–4`) |
| `FOUNDRY_MAX_RPM` | Client-side requests/min throttle (`0` = off) | `0` |
| `FOUNDRY_MAX_CONCURRENT_CALLS` | Cap simultaneous model calls (`0` = off) | `0` |
| `FOUNDRY_RATE_LIMIT_RETRIES` | Waits to ride out a 429/529 burst | `6` |
| `FOUNDRY_CLIENT_TTL_S` | Rebuild SDK client every N s — credential refresh (`0` = off) | `0` |
| `FOUNDRY_MANIFEST_MAX` | Cap proxy per-call manifest (`0` = unbounded) | `100000` |
| `WORKSHOP_LOG_TAIL` / `WORKSHOP_LOG_GRACE_S` / `WORKSHOP_LOG_SWEEP_S` | Finished-build log trim: tail kept · grace before trim · sweep interval (`0` = off) | `64` · `90` · `120` |

> Don't set both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` — the key
> outranks the token and bills the API.

### Cheat sheet

| Flag | Values | Default |
|---|---|---|
| `--mode` | `assistant` · `gated` · `autonomous` | `assistant` |
| `--backend` | `claude-cli` · `anthropic` · `openai` · `codex` · `ollama-gemma` | set one (e.g. `claude-cli`) |
| `--adapter` | `stub` · `raw-react` | derived from `--backend` (any backend → `raw-react`) |
| `--spec FILE\|DIR` | spec input (repeatable) | — |
| `--packs a,b` | domain packs (`regulated` · `ml_data` · `frontend`) | — |
| `--docs DIR` | generated doc-set output | — |
| `--checkpoints` | `all` · `product,design,release` | off |
| `--max-fix-attempts N` | gate fix-loop bound (N ≥ 1) | `3` |
| `--max-slots N` | parallelism ceiling (N ≥ 1) | auto |
| `--cost-cap-microusd N` | spend envelope | — |
| `--out DIR` | output repo | — |
| `--state-dir DIR` / `--thread-id NAME` | durable resume | in-memory / `cli-run` |

**`evolve` subcommand:** `--apply DIR` · `--guardian-approve` · `--strict`

Exit codes: `0` passed · `1` halted/failed · `2` usage error.
