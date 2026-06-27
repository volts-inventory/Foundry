# Foundry

A graph-based, multi-agent software factory. Give it a one-line request; it runs
a full product → architecture → engineering → QA → security → gates pipeline and
writes a GitHub-ready repo (code + docs). Use it two ways:

- **CLI** — `python -m foundry.cli "<request>"` → a repo on disk.
- **Workshop UI** — a localhost web app that drives the same factory as a game.

Defaults are keyless and deterministic (`mock` backend), so everything below runs
with no API key until you opt into a real model.

---

## Requirements

- Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/).
- Docker (only for `make up` / compose).
- A real backend (optional): an Anthropic API key, the `claude` CLI on a Max
  plan, the `codex` CLI, or a local Ollama.

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
# 1. Simplest possible run (mock backend, assistant mode) → repo in ./out
uv run python -m foundry.cli "build a tiny URL-shortener service" --out ./out

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

A backend is the model egress. Default `mock`. Set it three ways — last one wins:

```bash
# 1. Per command — the --backend flag (overrides everything)
uv run python -m foundry.cli "build a todo API" --backend anthropic --out ./out

# 2. For the shell session — the FOUNDRY_MODEL_BACKEND env var
export FOUNDRY_MODEL_BACKEND=anthropic
uv run python -m foundry.cli "build a todo API" --out ./out      # uses anthropic

# 3. Persistently — put it in .env (read by make + docker compose)
make env                                  # scaffold .env if missing
echo "FOUNDRY_MODEL_BACKEND=anthropic" >> .env
make run REQUEST="build a todo API"       # uses anthropic
```

### Backend settings

| Backend | What it is | Required setting(s) | How to get it |
|---|---|---|---|
| `mock` | Keyless, deterministic stub (default) | none | — |
| `anthropic` | Claude API | `ANTHROPIC_API_KEY` | console.anthropic.com |
| `claude-cli` | `claude -p` on a Max plan | `CLAUDE_CODE_OAUTH_TOKEN` | `make token` |
| `codex` | codex CLI | `OPENAI_API_KEY` | platform.openai.com |
| `ollama-gemma` | Local Ollama endpoint | `FOUNDRY_OLLAMA_URL` | run Ollama locally |

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
# 1. mock — keyless, deterministic (default; demos + CI). No setup.
uv run python -m foundry.cli "build a todo API" --backend mock --out ./out

# 2. anthropic — Claude API. Set ANTHROPIC_API_KEY.
export ANTHROPIC_API_KEY=sk-ant-...
uv run python -m foundry.cli "build a todo API" --backend anthropic --out ./out

# 3. claude-cli — `claude -p` on a Max subscription. Mint a token, add to .env.
make token                                # logs in; prints CLAUDE_CODE_OAUTH_TOKEN
echo "CLAUDE_CODE_OAUTH_TOKEN=<token>" >> .env
uv run python -m foundry.cli "build a todo API" --backend claude-cli --out ./out

# 4. codex — codex CLI. Set OPENAI_API_KEY.
export OPENAI_API_KEY=sk-...
uv run python -m foundry.cli "build a todo API" --backend codex --out ./out

# 5. ollama-gemma — local Ollama. Point at the endpoint.
export FOUNDRY_OLLAMA_URL=http://localhost:11434
uv run python -m foundry.cli "build a todo API" --backend ollama-gemma --out ./out
```

`mock` writes no real code (the `stub` adapter no-ops). For real code generation,
pair a real backend with a real adapter — see §5. In the Workshop UI, the backend
is a dropdown; `FOUNDRY_MODEL_BACKEND` only sets which option is preselected (§9).

---

## 5. Choosing a coding substrate (`--adapter`)

The adapter is what actually writes the code. Default `stub`.

```bash
# 1. stub — deterministic no-op; structure only (smoke/demo, default)
uv run python -m foundry.cli "build a dice roller" --adapter stub --out ./out

# 2. claude-code — real `claude -p` writes the files (needs the claude binary)
uv run python -m foundry.cli "build a dice roller" \
  --adapter claude-code --backend claude-cli --out ./out

# 3. raw-react — real code generated via whichever --backend you chose
uv run python -m foundry.cli "build a dice roller" \
  --adapter raw-react --backend anthropic --out ./out
```

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
# 1. Run locally (no Docker) on http://localhost:8765
uv run python -m webapp.server

# 2. Expose it to your LAN
WORKSHOP_HOST=0.0.0.0 uv run python -m webapp.server

# 3. Default to a real backend in the UI picker
FOUNDRY_MODEL_BACKEND=claude-cli uv run python -m webapp.server

# 4. Require a shared token for any mutating action
FOUNDRY_WORKSHOP_TOKEN=secret uv run python -m webapp.server
#    then open  http://localhost:8765/?token=secret
```

### Docker / compose

```bash
# 1. Scaffold a .env (backend + secrets; gitignored)
make env && $EDITOR .env

# 2. Build + serve in Docker → http://localhost:8765
make up

# 3. Stop the stack
make down
```

---

## 10. Develop

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
| `FOUNDRY_MODEL_BACKEND` | Default backend | `mock` |
| `FOUNDRY_CODING_ADAPTER` | Default adapter | `stub` |
| `ANTHROPIC_API_KEY` | `anthropic` backend key | — |
| `CLAUDE_CODE_OAUTH_TOKEN` | `claude-cli` backend (via `make token`) | — |
| `OPENAI_API_KEY` | `codex` backend key | — |
| `FOUNDRY_OLLAMA_URL` | `ollama-gemma` endpoint | — |
| `FOUNDRY_MODEL_T1/T2/T3` | Pin exact model id per tier | curated map |
| `FOUNDRY_MAX_SLOTS` | Parallelism ceiling | auto |
| `FOUNDRY_COST_CAP_MICROUSD` | Global spend envelope | — |
| `FOUNDRY_STATE_DIR` | Durable state dir | in-memory |
| `WORKSHOP_HOST` / `WORKSHOP_PORT` | UI bind address / port | `127.0.0.1` / `8765` |
| `WORKSHOP_OUTPUT_DIR` | UI build output root | `.run/web` |
| `FOUNDRY_WORKSHOP_TOKEN` | Shared token for UI mutations | off (localhost-only) |

> Don't set both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` — the key
> outranks the token and bills the API.

### Cheat sheet

| Flag | Values | Default |
|---|---|---|
| `--mode` | `assistant` · `gated` · `autonomous` | `assistant` |
| `--backend` | `mock` · `anthropic` · `claude-cli` · `codex` · `ollama-gemma` | `mock` |
| `--adapter` | `stub` · `claude-code` · `raw-react` | `stub` |
| `--spec FILE\|DIR` | spec input (repeatable) | — |
| `--packs a,b` | domain packs | — |
| `--checkpoints` | `all` · `product,design,release` | off |
| `--max-fix-attempts N` | gate fix-loop bound | `3` |
| `--max-slots N` | parallelism ceiling | auto |
| `--cost-cap-microusd N` | spend envelope | — |
| `--out DIR` | output repo | — |
| `--state-dir DIR` / `--thread-id NAME` | durable resume | in-memory / `cli-run` |

Exit codes: `0` passed · `1` halted/failed · `2` usage error.
