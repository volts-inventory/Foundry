# Foundry — Usage Guide

Foundry turns a one-line request into reviewed, gated software. This guide is **how to use it** — exact commands, exact output. For design/rationale see the other files in [`docs/`](./).

> **The one rule:** agents propose, the **signed gates decide**. Modes, packs, checkpoints, and evolution can *block* a release but can never force a failing build through.

---

## Install

```sh
uv sync --dev
uv run python -m foundry.cli "build a URL shortener"   # should print "result: PASS", exit 0
```

No API key or network needed — the CLI runs on a deterministic mock model. (To use a real model, see [Real models](#real-models).)

---

## Run a build

```sh
uv run python -m foundry.cli "build a URL shortener"
```

Output:

```
request:   'build a URL shortener'
mode:      assistant
phase:     done
result:    PASS
gates:
  - build: pass
  - review: pass
  - spec: pass
  - test: pass
reviewed artifact (PR subject):
  cas://feature_diff/72b84e4c...
open_pr action:
  idempotency_key: open_pr:d240bf5c...
  payload_uri:     cas://feature_diff/72b84e4c...
```

Exit code: **`0`** = clean pass (a release action was emitted); **`1`** = halted (a gate failed → no release); **`2`** = bad usage (e.g. a pack veto).

A failing gate halts the run with no PR — that's the system working. Read the `gates:` block to see which one.

---

## Get the code and docs out

```sh
uv run python -m foundry.cli --out ./build --docs ./build-docs "build a URL shortener"
```

`--out ./build` → the generated, gate-verified source tree:

```
build/
  app.py
  test_app.py
```

`--docs ./build-docs` → the release doc set + manifest:

```
build-docs/
  README.md                     index
  BUILD-MANIFEST.md / .json     every artifact, gate verdict (+ attested?), model usage, cost
  docs/
    PRD.md  REQUIREMENTS.md
    architecture/DESIGN.md  architecture/OWNERSHIP.md
    qa/TEST-PLAN.md  qa/TEST-REPORT.md  qa/E2E-REPORT.md
    ops/RUNBOOK.md
    KNOWN-ISSUES.md  UPGRADE.md  PERF-COST.md
```

(`--out` writes only on a passing build; a halted run writes nothing.)

---

## Recipes

### Choose a release mode
```sh
uv run python -m foundry.cli --mode assistant  "..."   # default: emits a PR for a human to merge
uv run python -m foundry.cli --mode gated      "..."   # full assurance gates + human sign-off + evidence pack
uv run python -m foundry.cli --mode autonomous "..."   # builds, attests, deploys behind the trust root
```
Assistant's reviewer verdict is advisory; Gated/Autonomous make it gating.

### Engage a domain pack
```sh
uv run python -m foundry.cli --packs regulated "..."          # forces HITL + approver-of-record
uv run python -m foundry.cli --packs ml_data,frontend "..."
```
A pack can **veto** an unsafe mode — and the CLI refuses rather than downgrade:
```
$ foundry --mode autonomous --packs regulated "..."
error: mode 'autonomous' is VETOED by the engaged pack(s) regulated;
       the strictest reachable mode is 'gated' (re-run with --mode gated)
```

### Generate a spec, then build from it
```sh
uv run python -m foundry.ideation --mock --rounds 2 "a link shortener with abuse limits" > spec.md
uv run python -m foundry.cli --spec spec.md "build it"
```
`ideation` prints a spec-markdown doc (Summary / Capabilities / Acceptance Criteria / Architecture / Open Questions). `--mock` runs without a model; drop it to use the configured backend.

### Bring your own spec
```sh
uv run python -m foundry.cli --spec ./design.md "build it"          # one file
uv run python -m foundry.cli --spec ./specs/ "build it"             # a dir of .md/.txt (recursive)
uv run python -m foundry.cli --spec a.md --spec b.md "build it"     # repeat
```
The spec grounds the PRD/criteria; it never bypasses a gate.

### Pause for human review mid-build
```sh
uv run python -m foundry.cli --checkpoints "..."             # pause at every stage boundary
uv run python -m foundry.cli --checkpoints product,design "..."
```
The build pauses at stage boundaries, surfaces that stage's artifacts + the agents' notes, and resumes on approve/reject. From the shell it auto-approves (a programmatic run supplies a responder — see [Library use](#library-use)). An approve only lets the run reach the gates; it can't release a failing build.

### Evolve a product you already built
```sh
uv run python -m foundry.cli --out ./v1 "build a URL shortener"     # ship v1
mkdir changes && printf '...' > changes/app.py                      # the edited/added files
uv run python -m foundry.cli evolve ./v1 "add rate limiting" --apply ./changes
```
The change re-runs only the gates it touches, re-gates fail-closed, and links provenance to v1. Editing an existing test/criterion needs `--guardian-approve` (a recorded human decision) — and that still can't launder a gate failure.

### Make state survive a restart (single host)
```sh
FOUNDRY_STATE_DIR=/data uv run python -m foundry.cli "..."
```
With `FOUNDRY_STATE_DIR` set, checkpoints, the audit log, the trust ledgers, and artifacts are written under that dir (a mounted volume in compose). Unset = in-memory (nothing persists).

### <a id="real-models"></a>Use a real model backend
The bundled CLI always uses the mock model. To do real work you assemble the proxy in code:
```python
from foundry.proxy import build_backend, backend_from_env
proxy = build_backend("ollama-gemma")   # "anthropic" | "claude-cli" | "codex" | "ollama-gemma"
proxy = backend_from_env()               # reads FOUNDRY_MODEL_BACKEND (default "anthropic")
```
`anthropic` needs `ANTHROPIC_API_KEY`; `claude-cli`/`codex` need that CLI on PATH; `ollama-gemma` needs a local Ollama server. Then wire `proxy` into `build_factory_graph` (see [Library use](#library-use)).

---

## CLI cheat-sheet

**`python -m foundry.cli [FLAGS] "<request>"`**

| Flag | Effect |
|---|---|
| `--mode {assistant\|gated\|autonomous}` | release posture (default `assistant`) |
| `--packs a,b` | engage domain packs (`regulated`, `ml_data`, `frontend`); may veto the mode |
| `--spec FILE\|DIR` | seed a design doc (repeatable) |
| `--out DIR` | write the source tree (on pass) |
| `--docs DIR` | write the doc set + `BUILD-MANIFEST` |
| `--checkpoints [all\|b1,b2]` | pause at stage boundaries |

**`python -m foundry.cli evolve <PRIOR_DIR> "<change>" --apply <OVERLAY_DIR> [--guardian-approve]`**

**`python -m foundry.ideation [--mock] [--rounds N] "<idea>"`** → prints spec markdown

| Env var | Effect |
|---|---|
| `FOUNDRY_STATE_DIR=<dir>` | durable file/volume-backed state (unset = in-memory) |
| `FOUNDRY_MODEL_BACKEND=<name>` | backend for `backend_from_env()` |
| `FOUNDRY_CHECKPOINTER=sqlite\|postgres` | checkpoint store (`postgres` reserved, not built) |
| `ANTHROPIC_API_KEY=<key>` | for the `anthropic` backend |

---

## Library use

Run a build from Python (still the deterministic harness — for real models, swap the proxy as above and compose `build_factory_graph` yourself):

```python
from foundry.cli import run

result = run(
    "build a URL shortener",
    mode="gated",                                  # assistant | gated | autonomous
    packs=["regulated"],                           # may raise ValueError if it vetoes the mode
    spec="./design.md",                            # path | [paths] | text | None
    output_dir="./build",
    docs_dir="./build-docs",
    checkpoints=["product", "design"],             # or True for all
    checkpoint_responder=lambda payload: "approve" # your HITL decision per pause
)

result.passed          # True/False
result.gate_statuses   # {"spec": <PASS>, "build": <PASS>, "test": <PASS>, ...}
result.output_root     # "./build" (or None)
result.mode, result.packs, result.phase, result.paused_at
```

Evolve / ideate:

```python
from foundry.lifecycle.evolve import evolve
out = evolve("./v1", "add rate limiting",
             changes={"app.py": NEW_SOURCE, "test_app.py": NEW_TEST})  # relpath -> content (None = delete)
out.released, out.prior_uri        # fail-closed re-gate result + link to the prior version

from foundry.ideation import ideate   # ideate(runtime, "idea", rounds=3) -> .spec_markdown
```

Durable backends for a deployment:

```python
from foundry.durability.persistence import durable_backends, state_dir_from_env
sd = state_dir_from_env()                 # FOUNDRY_STATE_DIR or None
backends = durable_backends(sd) if sd else None   # (checkpointer, global_records, cas)
```

---

## Deploy

| Where | Command | Persistence |
|---|---|---|
| **Local / dev** | `python -m foundry.cli …` | in-memory (nothing persists) |
| **One host** | docker-compose ([`deploy-compose.md`](./deploy-compose.md)) + `FOUNDRY_STATE_DIR=/data` | durable across restart |
| **Cloud / EKS** | documented path | same images; CAS→S3, SQLite→Postgres, scale→replicas |

---

## Troubleshooting

| You see | Why / fix |
|---|---|
| `result: HALTED`, exit 1, no PR | a gate FAILed — check the `gates:` block. Working as intended; only all-PASS emits a release. |
| `... is VETOED by the engaged pack(s) ...`, exit 2 | a `--packs` pack forbids that `--mode`. Use the suggested mode. |
| `--out` produced nothing | `--out` writes only on a passing build. |
| `Deserializing unregistered type ... from checkpoint` (with `--checkpoints`) | harmless dev-checkpointer warnings to stderr; the run is unaffected. |
| Nothing persists between runs | expected without `FOUNDRY_STATE_DIR`; set it to a path/volume. |
| `claude`/`codex` backend errors | those need the real binary + that tool's auth; use the default mock CLI for keyless runs. |
| Empty/canned model output | the bundled CLI uses the mock model; configure a real [backend](#real-models). |

---

## How it stays safe (30-second version)

- **Gates are signed** by a trusted runner; the state reducer admits only signed verdicts (forged/unsigned ones are dropped).
- A failed gate **latches** — it can't silently flip to pass.
- Checkpoints, the agent journal, ideation, and evolution live on **separate channels** the gate/release path never reads — they can halt, never release.
- An evolved change **re-passes the gates** from scratch (only re-running what it touched); it isn't trusted because v1 passed.

Full design: [`architecture.md`](./architecture.md) · [`governance.md`](./governance.md) · [`agents.md`](./agents.md) · [`lifecycle.md`](./lifecycle.md) · [`domains.md`](./domains.md). Honest dev-grade boundary: [`implementation-plan.md` §0.3](./implementation-plan.md).
