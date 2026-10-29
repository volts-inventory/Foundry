# Foundry

A graph-based, multi-agent software factory. Give it a one-line request — "a
stardew-style farming game about colonizing the moon", "a file-encryption
vault", "a terraform stack for a three-tier app" — and it runs a full
plan → engineering → review → gates pipeline and writes the repo. Use it two
ways:

- **CLI** — `python -m foundry.cli "<request>"` → a repo on disk.
- **Workshop UI** — a localhost web app that drives the same factory as a game.

**What makes it a factory, not a script:**

- **Agentic coding substrates.** Each implementation ticket runs a real
  multi-turn coding agent inside an isolated sandbox: `claude-code` (the
  `claude` CLI edits files and runs builds/tests itself — your Max plan, no API
  billing, or an API key) or `proxy-agent` (the same agency through the
  mediating proxy, any backend, credentials never enter the sandbox).
- **Gates that mean it works.** Verification profiles pin stack-appropriate
  gate commands — pytest/compileall for Python, npm build/test for web, a
  runtime SMOKE gate for games ("boots and survives N ticks"), KAT/property
  suites for crypto, fmt/validate for Terraform. Verdicts are deterministic,
  signed, and fail-closed; a failing gate fixes forward, never ships.
- **Hands-off and durable.** Builds run unattended start to finish; with a
  state dir they survive restarts and resume from the last checkpoint.
- **A sandbox that holds no secrets.** Only model-auth credentials can ever
  enter the build sandbox — cloud/deploy keys structurally cannot.

Pick a model backend — Claude Code (`claude -p`, a Max plan) or the Anthropic /
OpenAI APIs — and it writes real code. Examples below use `claude-cli`.

---

## Quick start — run the Workshop locally

```bash
python -m venv .venv && source .venv/bin/activate    # 1. install the project (once)
pip install -e .
cp foundry.mac.env.example .env          # 2. Mac preset (Linux: foundry.linux.env.example)

# 3. Auth for claude-cli — pick ONE (skip if you're already logged in here):
#   a) Log in on THIS machine — session cached on disk, auto-refreshes:
# claude /login
#   b) Or mint a portable token and store it in .env — for headless boxes, CI, a Pi:
# claude setup-token                              # prints CLAUDE_CODE_OAUTH_TOKEN
# echo "CLAUDE_CODE_OAUTH_TOKEN=<token>" >> .env

python -m webapp.server                  # 4. start it → http://localhost:8765
```

The server loads `.env` on startup. Prefer the metered API? Set
`FOUNDRY_MODEL_BACKEND=anthropic` and an `ANTHROPIC_API_KEY` in `.env`.

---

## How it works

```
"<one-line request>"
        │
        ▼
  planner ──▶ engineering waves ──▶ reviewer ──▶ gates (spec · build · test · smoke)
        │                                          │
        └──── fix-forward on a red gate ◀──────────┘
                                                   │
                                                   ▼
                              ./out  (code + BUILD-MANIFEST.json/.md)
```

The planner emits a PRD, acceptance criteria, a design and a ticket DAG; per-ticket
workers build in dependency waves; the reviewer and the signed gates decide what
ships. `BUILD-MANIFEST.json` / `.md` record the gates, artifacts and observed model
usage (calls, tokens in/out, cache reads/writes, coding-session tokens).

Two seams are deliberately independent:

- **`--backend`** — the model egress ("the brain"): *where model calls go*, all
  routed through the single-egress proxy.
- **`--adapter`** — the coding substrate ("the hands"): *what writes the code*.
  Usually omitted — it's **derived from the backend**: `claude-cli` →
  `claude-code` (agentic), other real backends → `raw-react` (one completion →
  one diff). `stub` is a no-op; `proxy-agent` is the agentic loop through the
  proxy for any backend.

---

## CLI

```bash
python -m foundry.cli [OPTIONS] "<one-line request>"

  --backend <claude-cli|anthropic|openai|codex>  # WHERE model calls go (default: mock)
  --adapter <stub|raw-react|claude-code|proxy-agent>          # WHAT writes the code (default: derived)
  --profile <auto|pygame|node-web|…|none>  # stack → pinned gate commands (default: auto)
  --scope <auto|demo|standard>             # caps what the agents propose     (default: auto)
  --spec <FILE|DIR>          # ground the build (or run from a spec alone); repeatable
  --out <DIR>                # write the repo + BUILD-MANIFEST here (a held build writes a DRAFT)
  --state-dir <DIR>          # durable, restart-survivable state (FOUNDRY_STATE_DIR)
  --thread-id <NAME>         # run identity; seeds idempotency keys + resume (default: cli-run)
  --max-fix-attempts <N>     # fix-forward retries per failing gate        (default: 3)
  --max-slots <N>            # parallelism ceiling                         (default: auto)
  --workers frontend=2,backend=2,data=2,test=2   # tickets in flight per specialty
  --ticket-tiers trivial=T3,standard=T2,complex=T1
  --cost-cap-microusd <N>    # spend envelope for metered backends
  --token-cap <N>            # stop cleanly after N tokens (exit 3); with --state-dir,
                             #   rerun the same command to resume

# Exit codes: 0 passed · 1 halted · 2 usage error · 3 token cap reached
# Run with no arguments to print the full usage text.
```

Examples:

```bash
python -m foundry.cli "build a tiny URL-shortener service" --backend claude-cli --out ./out
python -m foundry.cli --spec ./spec.md --backend claude-cli --out ./out
python -m foundry.cli "a stardew-style farming game about colonizing the moon" \
  --backend claude-cli --profile pygame --state-dir ~/foundry/state --out ~/moon-colony
```

`--profile pygame` pins build = compileall, test = pytest, smoke = `python smoke.py`
(the game must boot headless and survive 120 frames). Omit it to let the planner
pick a stack from the installed toolchains.

---

## Backends

| Backend | What it is | Required setting(s) |
|---|---|---|
| `claude-cli` | `claude -p` on a Max plan (no API billing) | machine login (`claude /login`) **or** `CLAUDE_CODE_OAUTH_TOKEN` |
| `anthropic` | Claude API (SDK) | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI API (SDK `chat.completions`) | `OPENAI_API_KEY` |
| `codex` | codex CLI (`codex exec`) | `OPENAI_API_KEY` |

Set it per command (`--backend`), per shell (`FOUNDRY_MODEL_BACKEND`), or in `.env`.
The direct-SDK backends keep the grammar-constrained verdict channel; the CLIs
degrade it to fail-closed JSON parsing. Pin exact model ids per tier with
`FOUNDRY_MODEL_T1/T2/T3` when your egress serves one the default map doesn't name.

> Don't set both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` — the key
> outranks the token and bills the API.

---

## Workshop UI

A localhost web app that runs the real factory and visualizes it: type a
request, watch the animated floor build it (Planner, per-ticket workers, Reviewer,
Gate, Vault) with the live log and Tickets tab, then download the repo as a
**.zip**. Each build is a floor; the elevator reopens earlier builds and their
logs, and REPO browses a build's files. The backend comes from
`FOUNDRY_MODEL_BACKEND` (as in the CLI); the New-build dialog's collapsed
Settings hold the per-specialty worker limits and a token cap.

```bash
python -m webapp.server                                  # http://localhost:8765
WORKSHOP_HOST=0.0.0.0 python -m webapp.server            # expose to your LAN
FOUNDRY_WORKSHOP_TOKEN=secret python -m webapp.server    # token for mutating actions
```

Sign-in is on by default (`FOUNDRY_AUTH=0` disables it for a local smoke test).

**Sandboxed gates.** Generated tests, BDD scenarios, builds and smoke runs execute
inside an unprivileged Linux namespace sandbox (`unshare`: own netns with loopback
only, read-only filesystem except the workdir, private `/tmp`, host sockets and
credential stores masked, env allowlist). The dependency-install step gets egress
only through a gate-owned HTTPS `CONNECT` relay that refuses loopback, metadata and
private destinations. If the host can't create user namespaces the gates fail
closed; opt out to the sys.path-fenced in-process harness with
`FOUNDRY_GATE_SANDBOX=inprocess` (required on macOS), which still refuses the
install step unless `FOUNDRY_ALLOW_UNSANDBOXED_INSTALL=1`.

### Long-lived server

With a durable state dir the server resumes an in-flight build after a restart,
and the event log replays in full on reconnect. Keep it running with tmux or
nohup (`nohup python -m webapp.server > ~/foundry.log 2>&1 &`; kill the PID to
stop). For long runs set a token cap, size `WORKSHOP_MAX_CONCURRENT_BUILDS`
to the host (Pi `1`, M-series Mac `3–4`), and `FOUNDRY_MAX_RPM` if your plan needs
a throttle. The host presets (`foundry.mac.env.example`,
`foundry.linux.env.example`) are annotated with per-host tuning.

---

## Develop

```bash
uv sync                                                   # one-time
uv run ruff check .                                       # lint
uv run mypy foundry webapp                                # typecheck (strict)
uv run pytest -n auto -q -m "not slow"                    # fast inner loop
uv run pytest -n auto -q                                  # full suite (slow e2e included)
```

---

## Configuration reference

| Variable | Purpose | Default |
|---|---|---|
| `FOUNDRY_MODEL_BACKEND` | Default backend | `mock` |
| `FOUNDRY_CODING_ADAPTER` | Default adapter | derived from backend |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | API keys | — |
| `CLAUDE_CODE_OAUTH_TOKEN` | `claude-cli` token (via `claude setup-token`) | — |
| `FOUNDRY_MODEL_T1/T2/T3` | Pin exact model id per tier | curated map |
| `FOUNDRY_MAX_SLOTS` | Parallelism ceiling | auto |
| `FOUNDRY_COST_CAP_MICROUSD` / `FOUNDRY_TOKEN_CAP` | Spend / token envelope | — |
| `FOUNDRY_STATE_DIR` | Durable state dir | in-memory |
| `FOUNDRY_GATE_SANDBOX` | `inprocess` opts out of the namespace sandbox | sandbox |
| `FOUNDRY_ALLOW_UNSANDBOXED_INSTALL` | `1` lets the networked install step run under the `inprocess` opt-out (refused otherwise) | off |
| `WORKSHOP_HOST` / `WORKSHOP_PORT` | UI bind address / port | `127.0.0.1` / `8765` |
| `WORKSHOP_OUTPUT_DIR` | UI output root | `.run/web` |
| `WORKSHOP_STATE_DIR` | UI durable state dir (resume) | `WORKSHOP_OUTPUT_DIR/state` |
| `FOUNDRY_WORKSHOP_TOKEN` | Shared token for UI mutations | off |
| `FOUNDRY_AUTH` | Workshop sign-in (`0`/`off` disables) | on |
| `WORKSHOP_MAX_CONCURRENT_BUILDS` | Builds running at once; extras queue | `2` |
| `FOUNDRY_MAX_RPM` / `FOUNDRY_MAX_CONCURRENT_CALLS` | Client-side throttles (`0` = off) | `0` |
| `FOUNDRY_RATE_LIMIT_RETRIES` | Waits to ride out a 429/529 burst | `6` |
| `FOUNDRY_CLIENT_TTL_S` | Rebuild SDK client every N s (`0` = off) | `0` |
| `FOUNDRY_MANIFEST_MAX` | Cap proxy per-call manifest (`0` = unbounded) | `100000` |
| `WORKSHOP_LOG_TAIL` / `WORKSHOP_LOG_GRACE_S` / `WORKSHOP_LOG_SWEEP_S` | Finished-build log trim | `64` · `90` · `120` |
