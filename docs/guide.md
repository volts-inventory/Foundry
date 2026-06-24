# Foundry — Full Documentation Guide

A complete user & operator guide to running, configuring, extending, and deploying Foundry. For the design rationale see the [`docs/`](./) spec set; for a fast overview see the [root README](../README.md). This guide is the **how-to**.

## Contents

1. [What Foundry is (mental model)](#1-what-foundry-is)
2. [Installation](#2-installation)
3. [Core concepts](#3-core-concepts)
4. [Your first build (walkthrough)](#4-your-first-build)
5. [CLI reference](#5-cli-reference)
6. [Deployment-trust modes](#6-modes)
7. [Domain packs](#7-domain-packs)
8. [Model backends](#8-model-backends)
9. [Spec ingestion & ideation](#9-spec--ideation)
10. [Checkpoints (human-in-the-loop)](#10-checkpoints)
11. [The agent work-journal & interview](#11-journal)
12. [The evolution route](#12-evolution)
13. [Outputs: code, docs, manifest, evidence](#13-outputs)
14. [Configuration reference (env vars)](#14-configuration)
15. [Durability & deployment](#15-deployment)
16. [Programmatic API](#16-programmatic-api)
17. [Development & quality gates](#17-development)
18. [Troubleshooting & FAQ](#18-troubleshooting)
19. [Glossary](#19-glossary)

---

## 1. What Foundry is

Foundry is a **graph of role-specialized AI agents** that takes a request from intent to released, audited software. It models a software organization: Product → Architecture → Implementation → Quality → (Security/Audit/Supply-chain) → governance/release.

The one idea to internalize:

> **Agents propose; the signed gates decide.** Nothing — not a model, not a human checkpoint, not an evolved change — can release software except by passing the gates. Everything else sits *on top* of the gates and can only *halt*, never *force*, a release.

Keep that in mind and the rest of the system is just elaboration of *how* that invariant is made unbreakable.

---

## 2. Installation

**Prerequisites:** Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
git clone <repo> && cd Coders
uv sync --dev          # install runtime + dev tooling
```

Verify:

```sh
uv run python -m foundry.cli "build a tiny URL-shortener service"   # exit 0 on success
uv run pytest -q -n auto                                            # 1748 passing
```

No API key or network is required for any of the above — the bundled CLI runs on a deterministic mock transport (see [§8](#8-model-backends) to use a real model).

---

## 3. Core concepts

| Concept | What it is |
|---|---|
| **Agent / node** | A role (Product Manager, Architect, QA Engineer, Reviewer, …) implemented as a graph node that reads state, calls the model proxy, and emits artifacts. Agents emit *advisory* `GateTriage`, never signed verdicts. |
| **Gate** | A deterministic check (Spec, Build, Test, Security, Audit, …) whose verdict is computed and **signed** by the trusted runner. Gates are the only release authority. |
| **Two-zone runner** | *Zone A* runs untrusted agent/build code with no key; *Zone B* re-derives config, observes over a sealed channel, and signs the verdict. `merge_gates` admits only attested verdicts (fail-closed). |
| **Model proxy** | The single egress for all model calls and sole credential holder. Records the *served* model off the wire ("observed, not trusted") and resolves lineage; detects pinned-ID drift. |
| **Artifact** | A content-addressed handle `cas://sha256/…`; bodies live in the `ArtifactStore`, state carries only handles. |
| **`Seq`** | A total causal-order tuple `(run_id, emission_epoch, superstep, node, local)` — race-free, resume-monotonic. |
| **`GlobalRecords`** | Cross-fork ledgers: consumed verdict nonces, the stable-FAIL latch, the statistical alpha-look ledger. |
| **Mode** | The release posture: Assistant, Gated, or Autonomous ([§6](#6-modes)). |
| **Domain pack** | A pluggable policy bundle (regulated / ML / frontend) that tightens requirements and can veto a mode ([§7](#7-domain-packs)). |
| **Trust root** | (Autonomous) hermetic builder + input-bound provenance + admission controller + non-agent deployer. |

---

## 4. Your first build

```sh
uv run python -m foundry.cli --out ./build --docs ./build-docs "build a URL shortener with click analytics"
```

What happens, stage by stage:

1. **Intake → Product** — the request (and any `--spec`) becomes an `IntentBrief`, a `PRD`, and testable `AcceptanceCriteria`. A Criteria-Completeness Critic checks for missing security/abuse criteria.
2. **Architecture** — a `SystemDesign`, `ADR`s, `InterfaceContract`s, and an `OwnershipMap`.
3. **[Spec gate]** — verifies the criteria are complete and security-covered. A critic-flagged omission **fails** it.
4. **Implementation** — the engineering layer produces a content-addressed feature diff.
5. **Quality** — QA authors oracles (tests) with criterion traceability; the Reviewer judges the diff.
6. **[Build + Test gates]** — the runner materializes the integrated tree and runs build + tests in the sandbox; verdicts are signed.
7. **Emit** — on all-PASS: Assistant emits a PR-equivalent action; Gated assembles an evidence pack behind sign-off; Autonomous builds/admits/deploys.

Inspect the result:

```sh
ls ./build                    # the generated source tree (exactly what the gates verified)
ls ./build-docs               # PRD.md, architecture/, qa/, RUNBOOK.md, BUILD-MANIFEST.md, …
cat ./build-docs/BUILD-MANIFEST.md
```

The CLI prints a summary: the mode, gate statuses, the reviewed artifact handle, and the emitted action (or where it halted).

---

## 5. CLI reference

### `python -m foundry.cli [OPTIONS] "<request>"`

Runs a build. Options:

| Flag | Default | Meaning |
|---|---|---|
| `--mode {assistant\|gated\|autonomous}` | `assistant` | Release posture ([§6](#6-modes)). |
| `--packs name1,name2` | none | Engage domain packs ([§7](#7-domain-packs)); a pack may veto the mode (fail-closed). |
| `--spec FILE\|DIR` (repeatable) | none | Seed a design-spec doc into the build ([§9](#9-spec--ideation)). |
| `--out DIR` | none | On a passing build, write the generated source tree here. |
| `--docs DIR` | none | Write the rendered doc set + `BUILD-MANIFEST` here. |
| `--checkpoints [all\|b1,b2]` | off | Pause at stage boundaries for human verification ([§10](#10-checkpoints)). |

Exit codes: `0` clean pass (release action emitted), `1` halted/failed, `2` usage error (incl. a pack veto).

### `python -m foundry.cli evolve <PRIOR_DIR> "<change>" --apply OVERLAY_DIR [--guardian-approve]`

Evolve a previously-built product ([§12](#12-evolution)). `PRIOR_DIR` is a tree written by `--out`; `OVERLAY_DIR` holds the edited/added files; `--guardian-approve` records the HITL decision for a protected-path edit.

### `python -m foundry.ideation [--mock] [--rounds N] "<rough idea>"`

Co-generate a spec document, printed to stdout ([§9](#9-spec--ideation)). `--mock` runs deterministically with no backend; `--rounds N` sets the refinement-loop cap.

---

## 6. Modes

```sh
--mode assistant    # default
--mode gated
--mode autonomous
```

| Mode | Trust machinery | Release boundary | Reviewer verdict |
|---|---|---|---|
| **Assistant** | none beyond the gates | emits an `open_pr` action a human merges | advisory |
| **Gated** | risk-tiering, tier-scaled assurance gates (Security/Audit/Supply-chain/E2E), Design + Release **human sign-off**, WORM evidence pack | release behind recorded sign-off, fail-closed | **gating** |
| **Autonomous** | the full **trust root**: hermetic build → fail-closed admission → non-agent deploy | admission is the sole release gate; hands-off deploy | **gating** |

**Earned autonomy:** a product should graduate Assistant → Gated → Autonomous only when evidence justifies it (`foundry/lifecycle/modes.py`). All three are reachable from the CLI; Gated/Autonomous construct the (dev-grade) trust-root components automatically.

---

## 7. Domain packs

```sh
--packs regulated
--packs ml_data,frontend
```

Packs compose extra policy onto a build (governance.md §9, domains.md §5):

- **`regulated`** — forces HITL + approver-of-record, class-4 (IdP/PKI) human attestation, data-egress rules; **vetoes Autonomous** (and Assistant) — only Gated is reachable.
- **`ml_data`** — statistical (class-3) verdict with the alpha-look ledger, train/eval separation, compute budget.
- **`frontend`** — rendered-artifact capture, perceptual HITL, visual-regression flake envelope.

**Strictest-wins, fail-closed:** if a pack vetoes the requested mode (e.g. `--mode autonomous --packs regulated`), the CLI **refuses** with a clear error (exit 2) rather than silently downgrading. The veto is derived from the composed policy, not an agent-settable value, and is re-enforced at the emit boundary as defense-in-depth.

---

## 8. Model backends

The bundled CLI uses a **deterministic mock transport** (no key/network) — ideal for demos, tests, and CI-less local gates. To do real work, configure a backend on the single-egress proxy.

Backends (`foundry/proxy/cli_transports.py`), selected by name:

| Backend | Engine | Notes |
|---|---|---|
| `anthropic` | Anthropic SDK (default) | needs `ANTHROPIC_API_KEY` |
| `claude-cli` | `claude -p` (Claude Code CLI) | needs the `claude` binary on PATH |
| `codex` | `codex exec` (OpenAI Codex CLI) | OpenAI lineage — enables real cross-vendor decorrelation |
| `ollama-gemma` | local Ollama server (Gemma) | offline-capable; keeps native JSON-schema grammar |

```python
from foundry.proxy import build_backend, backend_from_env

proxy = build_backend("ollama-gemma")          # explicit
proxy = backend_from_env()                      # reads FOUNDRY_MODEL_BACKEND (default "anthropic")
```

Every backend runs the **identical factory flow** — only the egress changes. Lineage stays proxy-observed, determinism is pinned per backend (reasoning models get an effort knob, not a rejected temperature), the verdict channel stays grammar-constrained where the backend supports it (degrades fail-closed otherwise), and an unknown served model fails closed. The trust caveat: with a CLI/local backend the *credential* lives in that tool's own auth, so the proxy is the sole *egress path* but no longer the sole *secret holder*.

> The bundled `foundry.cli` smoke harness always uses the mock transport; wiring a real backend into a full run is a programmatic assembly ([§16](#16-programmatic-api)).

---

## 9. Spec & ideation

**Bring your own spec.** A `--spec FILE|DIR` (`.md`/`.txt`, dir read recursively; repeatable) seeds the design document into the Product/Architecture intake so the PRD and criteria are grounded in it. The spec is **input data only** — it never bypasses the criteria derivation or any gate. `--spec ""` is treated as no spec (never ingests the working directory).

**Co-generate a spec.** `python -m foundry.ideation` runs a bounded, iterative loop (clarify → propose capabilities/criteria/architecture → refine) and prints a spec-markdown document you can feed to `--spec`:

```sh
uv run python -m foundry.ideation --mock --rounds 3 "a link shortener with abuse limits" > spec.md
uv run python -m foundry.cli --spec spec.md "build it"
```

Ideation is advisory — a front-of-funnel authoring aid that shapes the spec; it writes no channel and overrides no gate.

---

## 10. Checkpoints

```sh
--checkpoints                 # pause at all stage boundaries
--checkpoints product,design  # pause at specific boundaries
```

Checkpoint mode pauses the build at stage boundaries (resumable via the durable checkpointer), surfaces a **read-only inspection payload** — that stage's artifacts plus the agent work-journal — and resumes on a human decision (approve / reject / steer). From the shell the CLI uses an approve-all responder; a programmatic run supplies `run(checkpoint_responder=...)`.

**The guarantee:** a checkpoint *approve* only lets the run proceed to the gates — it can **never** turn a failing gate into a release. The decision lives on an isolated `checkpoint_decisions` channel that no gate/admission path reads; the only release effect it can have is *subtractive* (a reject halts). A reject halts with no PR; a malformed/missing decision fails closed to reject.

---

## 11. Journal

Every agent records a structured, **strictly-advisory** rationale entry (`JournalEntry`) on its own `agent_journal` channel — what it considered, assumptions, concerns. At a checkpoint you can **interview** an agent: `foundry.agents.interview.interview(...)` re-instantiates its role + journal through the proxy to answer "why this design? what worries you?".

This is pure **observability** — the journal is read by nothing in the trust path (gates, admission, routing), `interview()` has no channel-write surface, and entries cost zero extra model calls by default. It mimics the human layer of a real team without becoming a trust input.

---

## 12. Evolution

Refine a shipped product through the same fail-closed factory:

```sh
uv run python -m foundry.cli --out ./v1 "build a URL shortener"          # ship v1
# edit/add files under ./changes, then:
uv run python -m foundry.cli evolve ./v1 "add per-IP rate limiting" --apply ./changes
```

The change re-enters as a **maintained change**:

- **change-proportional re-verification** — only gates whose scope the change touches re-run; the rest reuse the prior *attested* verdict (never a stale pass for touched scope);
- **re-gate + re-admit, fail-closed** — a change that doesn't re-pass is **not** released (no PR/deploy);
- **provenance chain** — the new version links to the prior (`prior_uri`);
- **regression safety** — prior acceptance criteria + oracles carry forward; a regression fails the re-gate;
- **Guardian** — an edit to an existing criterion/oracle/gate/scanner path is a deterministic CRITICAL-DIFF → HITL, blocked unless `--guardian-approve` is recorded. `--guardian-approve` clears only the HITL block; it can never launder a gate FAIL.

Production telemetry/tickets feed this same route via the feedback loop (`foundry/lifecycle/feedback_loop.py`).

---

## 13. Outputs

| Output | Flag / where | Contents |
|---|---|---|
| **Source tree** | `--out DIR` | the integrated tree the gates verified (path-escape-guarded) |
| **Doc set** | `--docs DIR` | `PRD.md`, `REQUIREMENTS.md`, `architecture/{DESIGN,ADRS,INTERFACES,OWNERSHIP}.md`, `qa/{TEST-PLAN,TEST-REPORT,E2E-REPORT}.md`, `ops/RUNBOOK.md`, `KNOWN-ISSUES.md`, `UPGRADE.md`, `PERF-COST.md`, `README.md` |
| **Build manifest** | `--docs DIR` → `BUILD-MANIFEST.{md,json}` | every artifact (kind/producer/uri), every gate verdict + **attested?**, provenance/SBOM digests, observed model usage (calls/tokens/families/drift), cost |
| **Evidence pack** | Gated mode | WORM, hash-chained: gate verdicts + artifacts + human sign-offs |
| **Release action** | always (in state) | `open_pr` (Assistant/Gated) or `deploy` (Autonomous) as a queued `ExternalAction` — the outbox is the only thing that touches the outside world |

The doc set mirrors what a real company ships; the upstream docs (PRD/design/ADRs) inline the agents' actual narrative prose, and the four ops docs (runbook/known-issues/upgrade/perf-cost) are authored by a Technical Writer through the proxy. Cost numbers are grounded in the proxy's observed manifest, not model-claimed.

---

## 14. Configuration

| Env var | Values | Effect |
|---|---|---|
| `FOUNDRY_STATE_DIR` | a directory path | When set, the checkpointer, outbox/WORM audit, trust ledgers, and CAS become **file/volume-backed** (durable across restart). Unset → in-memory ([§15](#15-deployment)). |
| `FOUNDRY_CHECKPOINTER` | `sqlite` (default) \| `postgres` | Checkpoint backend. `postgres` is a documented swap point (fails closed if selected — not yet built). |
| `FOUNDRY_MODEL_BACKEND` | `anthropic` (default) \| `claude-cli` \| `codex` \| `ollama-gemma` | The proxy backend `backend_from_env()` builds ([§8](#8-model-backends)). |
| `ANTHROPIC_API_KEY` | a key | Used by the `anthropic` backend (lazy; only on real calls). |

---

## 15. Deployment

| Tier | How | State |
|---|---|---|
| **Dev / smoke** | `python -m foundry.cli …` | in-memory (`:memory:` SQLite + in-dict CAS) — nothing persists |
| **Single host** | docker-compose ([`deploy-compose.md`](./deploy-compose.md)) + `FOUNDRY_STATE_DIR=/data` (a mounted volume) | durable filesystem CAS + file-backed checkpoints/outbox/ledgers; **survives restart** |
| **Fleet / cloud** | Kubernetes/EKS (documented path) | same images/interfaces: CAS→S3, SQLite→Postgres (`FOUNDRY_CHECKPOINTER`), secrets→K8s/IRSA, scale→replicas+HPA, execution→broker-backed worker pool |

**Single-host durability** is one env var:

```sh
FOUNDRY_STATE_DIR=/data uv run python -m foundry.cli "..."
```

This file-backs the stable-FAIL latch, consumed nonces, alpha-look ledger, checkpoints, outbox/audit, and artifact bodies. The one caveat (documented): checkpoint + ledger writes aren't *cross-store atomic across a crash* on single-host — that atomicity is the Postgres/multi-host swap. The compose profile is **EKS-portable by construction**: moving to Kubernetes is a backend swap, not a rewrite, and physically realizes the two-zone/proxy/builder isolation that's logical-only in dev.

---

## 16. Programmatic API

### Run a build (dev harness — deterministic mock transport)

```python
from foundry.cli import run

result = run(
    "build a URL shortener",
    mode="gated",                 # "assistant" | "gated" | "autonomous"
    packs=["regulated"],          # domain packs (may veto the mode)
    spec="./design.md",           # path | list[path] | literal text | None
    output_dir="./build",         # write the source tree
    docs_dir="./build-docs",      # write docs + manifest
    checkpoints=True,             # or ["product", "design"]
    checkpoint_responder=lambda payload: "approve",   # programmatic HITL
)
result.passed        # bool
result.gate_statuses # {"spec": PASS, "build": PASS, "test": PASS, ...}
result.output_root   # where the tree was written
```

`RunResult` also carries `mode`, `packs`, `phase`, `artifact_uri`, `open_pr_*`, `doc_root`, `checkpoints`, and `paused_at`.

### Real model backend / durable state (production assembly)

```python
from foundry.proxy import build_backend, backend_from_env
from foundry.durability.persistence import durable_backends, state_dir_from_env

proxy = build_backend("ollama-gemma")            # or backend_from_env()
state_dir = state_dir_from_env()                 # FOUNDRY_STATE_DIR or None
if state_dir:
    backends = durable_backends(state_dir)       # (checkpointer, global_records, cas)
```

A production run composes `foundry.graph.factory_graph.build_factory_graph(rt, mode=..., ...)` with an `AgentRuntime` whose `proxy` is the real backend and whose `cas`/checkpointer come from `durable_backends`. See [`deploy-compose.md`](./deploy-compose.md) and `foundry/cli.py::run` for the canonical wiring.

### Evolve / ideate

```python
from foundry.lifecycle.evolve import evolve
out = evolve("./v1", "add rate limiting",
             changes={"app.py": NEW_SRC, "test_app.py": NEW_TEST},  # relpath -> content (None deletes)
             guardian_approval=False, strict=False)
out.released         # bool (fail-closed re-gate); out.prior_uri chains to the prior version

from foundry.agents.interview import interview     # advisory; needs an AgentRuntime
from foundry.ideation import ideate                 # ideate(rt, "idea", rounds=3) -> IdeationResult
```

---

## 17. Development

```sh
uv run pytest -q -n auto            # full suite (1748 passing) — parallel
uv run pytest tests/test_proxy.py   # one module
uv run ruff check foundry tests     # lint
uv run mypy foundry                 # type-check (strict on foundry/)
```

**Quality bars (enforced as the local gate chain — there is no hosted CI by design):** ruff clean · mypy clean · suite green · mutation testing ≥70% on the trust-critical reducer (`merge_gates` is at ~85%). Production classes named `Test*` (e.g. `TestPlan`) are domain types, not tests — pytest only collects `Test_*`.

**Conventions** (LEARNINGS): integer micro-USD (never floats) for money; `content_digest`, never builtin `hash()`, for stable ids; all model egress through `rt.proxy`; agents emit advisory triage, never signed verdicts; new frozen-model channel records get allowlisted in `foundry/durability/serde.py`.

---

## 18. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `error: ... VETOED ...` (exit 2) | A `--packs` pack vetoes the `--mode` (e.g. regulated + autonomous). Choose a reachable mode (regulated → gated). Working as intended (fail-closed). |
| Run halts, no PR | A gate FAILed (read `gate_statuses` / the summary). Assistant/Gated emit only on all-PASS. A stable FAIL latches and won't auto-clear. |
| Build wrote nothing to `--out` | `--out` writes only on a passing build (`result.passed`). A halted run produces no accepted tree. |
| Nothing persists across restart | Expected without `FOUNDRY_STATE_DIR`. Set it to a (volume) path for single-host durability. |
| `claude`/`codex` backend errors | Those backends need the real binary on PATH + that tool's own auth. Use `anthropic`, or the mock CLI for keyless runs. |
| `FOUNDRY_CHECKPOINTER=postgres` raises | Documented-but-unbuilt multi-host swap point; use `sqlite` (default) on a single host. |
| Empty/odd CLI completions | The bundled CLI uses a mock transport — completions are scripted/deterministic, not a real model. Configure a backend ([§8](#8-model-backends)). |

---

## 19. Glossary

- **Attested verdict** — a `GateResult` signed by Zone B; the only kind `merge_gates` admits.
- **Stable-FAIL latch** — once a gate FAILs on an integrated tree, that result is latched in `GlobalRecords` and cannot auto-clear.
- **Observed, not trusted** — the proxy records the model the provider *actually served*, never the adapter's self-report.
- **CAS handle** — `cas://sha256/<digest>`, a content-addressed pointer; the body lives in the `ArtifactStore`.
- **Evidence pack** — the WORM, hash-chained Gated-mode release bundle (verdicts + artifacts + sign-offs).
- **Maintained change** — an evolution-route change re-verified/re-gated/re-admitted against the prior version.
- **Verdict class** — the 1–5 taxonomy (deterministic … perceptual) that sets a gate's HITL/decorrelation requirements (domains.md).
- **Dev-grade root** — a deliberately-labeled stand-in (HMAC keys, in-memory store) behind a production-swappable interface (implementation-plan §0.3).

---

### Where to go next

- **Design & rationale:** [`architecture.md`](./architecture.md) · [`agents.md`](./agents.md) · [`governance.md`](./governance.md) · [`domains.md`](./domains.md) · [`lifecycle.md`](./lifecycle.md) · [`evaluation.md`](./evaluation.md) · [`operations.md`](./operations.md).
- **The honest dev-grade boundary:** [`implementation-plan.md` §0.3](./implementation-plan.md).
- **Deploying:** [`deploy-compose.md`](./deploy-compose.md).
