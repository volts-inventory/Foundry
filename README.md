# Foundry — an AI software factory

> A graph of role-specialized AI agents that takes a request from **intent → released, audited software** — behind deterministic quality gates, a cryptographic trust root, durable state, and human-in-the-loop checkpoints.

Foundry models a *whole software organization* as a directed graph. A **Product** layer turns a request into a PRD and testable acceptance criteria; an **Architecture** layer designs the system; **Engineering** implements it; **QA** authors the test oracles; **Security / Audit / Supply-chain** review it; and a **governance** layer gates, signs, and (optionally) deploys it. Agents only ever **propose** — the **signed gates are the sole authority on whether anything ships.**

```text
        request ──▶  Product ──▶ Architecture ──▶ [Spec gate] ──▶ Implementation
                       │             │                                  │
                  PRD + criteria   design + ADRs                    feature diff
                                                                        │
   release  ◀── emit ◀── [Build+Test gate] ◀── Quality ◀───────────────┘
   (PR · evidence pack · deploy)       │            (tests · oracles · review)
                                Security · Audit · Supply-chain   (Gated / Autonomous)
```

This repository is **both the design and a working build**: the design lives in [`PLAN.md`](./PLAN.md) + [`docs/`](./docs/); the implementation is `foundry/` (179 modules) with **1748 passing tests**. It runs end-to-end today from a one-line request on **dev-grade trust roots** (HMAC keys, in-memory stores) — honestly labeled below, behind the same interfaces a production deployment swaps for real ones.

> 📖 **New here? The [Full Documentation Guide](./docs/guide.md)** is the complete how-to — install, concepts, every CLI command & flag, modes, packs, backends, checkpoints, evolution, deployment, the programmatic API, and troubleshooting.

---

## Quickstart

```sh
uv sync --dev

# Run the whole pipeline end-to-end (deterministic; no API key, no network):
uv run python -m foundry.cli "build a tiny URL-shortener service"

# …and write the generated code, a company-style doc set, and the build manifest to disk:
uv run python -m foundry.cli --out ./build --docs ./build-docs "build a URL shortener"
```

The bundled CLI is a **deterministic smoke harness**: every model call goes through a mock transport and every external effect is a queued action, so a full run touches no network and needs no credentials. Point it at a real backend (below) to do real work.

A passing run produces:
- the **generated source tree** (`--out`) — the exact tree the gates verified;
- a **company-shaped doc set** (`--docs`) — PRD, design + ADRs, interfaces, test plan/report, threat model, runbook, known-issues, upgrade guide, perf/cost — plus a **`BUILD-MANIFEST`** indexing every artifact, every gate verdict (and whether it's *attested*), provenance digests, and the proxy's *observed* model usage;
- a single `open_pr` action (Assistant), a WORM **evidence pack** (Gated), or an attested **deploy** (Autonomous).

---

## The core idea: agents propose, gates decide

Everything is built around one invariant:

> **The signed gates are the only thing that can release software.** Every other mechanism — model backends, human checkpoints, the agent journal, ideation, evolution — sits *on top* of the gates. It can *halt* a release; it can **never** turn a failing gate into a passing one.

This is enforced structurally, not by convention:

- **Two-zone gate runner.** Untrusted agent code runs in *Zone A* (no signing key). *Zone B* re-derives the config, observes the run over a sealed channel, and **signs the verdict**. The state reducer `merge_gates` admits **only attested verdicts** and is fail-closed by default (`_RejectAll`) — an unsigned or forged `GateResult` is dropped.
- **Single-egress model proxy.** Every model call goes through one mediating proxy, the sole credential holder. It records the *actually-served* model off the wire and resolves its lineage through a curated map — it never trusts what an adapter *claims* ("observed, not trusted"). Drift from the pinned model is detected and can fail closed.
- **Content-addressed artifacts.** Bodies live in a store behind `cas://sha256/…` handles; state carries only handles. Identical work coalesces; nothing is silently mutated.
- **Total causal order + cross-fork ledgers.** A `Seq` tuple gives a race-free order; `GlobalRecords` holds consumed nonces, the stable-FAIL latch, and the statistical alpha-look ledger, so a failed gate stays failed and a nonce can't be replayed.
- **Trust root (Autonomous).** A hermetic SLSA-style builder + input-bound provenance + a fail-closed admission controller + a non-agent least-privilege deployer + a held-out-mutant discriminator together block the four canonical attacks: malicious test, forged results, forged verdict, fork divergence.

The adversarial suite *proves* these: forged verdicts are dropped, a stable FAIL never auto-clears, a vacuous test suite fails its gate, a checkpoint "approve" can't release a failing build, and an evolved change can't ride its predecessor's pass.

---

## What it can do

A tour of the capabilities — each runnable today.

### Deployment-trust modes — earned autonomy
```sh
uv run python -m foundry.cli --mode assistant  "..."   # default — emits a PR-equivalent action
uv run python -m foundry.cli --mode gated      "..."   # full gate set + human Design/Release sign-off + WORM evidence pack
uv run python -m foundry.cli --mode autonomous "..."   # builds, attests, and deploys behind the full trust root
```
A product graduates Assistant → Gated → Autonomous only when the evidence justifies it. In Assistant the reviewer verdict is advisory; in Gated/Autonomous it gates.

### Domain packs — pluggable, strictest-wins, fail-closed vetoes
```sh
uv run python -m foundry.cli --packs regulated "..."        # forces HITL + approver-of-record + egress rules; VETOES autonomous
uv run python -m foundry.cli --packs ml_data,frontend "..."
```
Packs (`regulated`, `ml_data`, `frontend`) compose extra requirements and can **veto an unsafe mode** — a regulated build can't run autonomous, and the CLI refuses it fail-closed rather than silently downgrading.

### Swappable model backends — same flow, different engine
The proxy egress is config-swappable (`foundry/proxy/cli_transports.py`): the **Anthropic SDK** (default), **`claude -p`** (Claude Code CLI), **`codex exec`** (OpenAI Codex CLI), or a local **Ollama / Gemma** server — selected via `build_backend(...)` / `FOUNDRY_MODEL_BACKEND`. The whole factory runs identically; only the egress changes. Lineage stays proxy-observed, determinism is pinned per backend (temperature vs. reasoning-effort), and an unknown served model fails closed.

### Spec ingestion & LLM ideation — start from a real spec
```sh
# Bring your own design doc:
uv run python -m foundry.cli --spec ./design.md "build it"

# …or co-generate one interactively first, then build from it:
uv run python -m foundry.ideation --mock "a link shortener with abuse limits" > spec.md
uv run python -m foundry.cli --spec spec.md "build it"
```
Ideation is an advisory front-of-funnel aid — it shapes the spec, it never overrides the spec gate.

### Human checkpoints — verify mid-build
```sh
uv run python -m foundry.cli --checkpoints "..."             # pause at every stage boundary
uv run python -m foundry.cli --checkpoints product,design "..."
```
The build pauses at stage boundaries (resumable via the checkpointer), surfaces that stage's artifacts **and the agents' work-journal** for inspection, and resumes on a human decision. An approve only lets the run reach the gates — it can never release a failing build.

### The agent work-journal — interview your team
Each agent records a structured, **strictly-advisory** rationale entry on its own channel; at a checkpoint you can "interview" an agent — re-instantiate its role + journal through the proxy and ask *"why this design? what are you worried about?"* It's observability, never a trust input.

### The evolution route — refine a shipped product
```sh
uv run python -m foundry.cli --out ./v1 "build a URL shortener"      # ship v1
uv run python -m foundry.cli evolve ./v1 "add rate limiting" --apply ./changes   # ./changes = overlay dir of edited/added files
```
A later change re-enters the **same factory** as a *maintained change*: it re-verifies only the touched scope (reusing prior attested verdicts elsewhere), **re-gates and re-admits fail-closed**, chains provenance to the prior version, and carries the prior oracles forward as a regression suite. It is **not** trusted just because v1 passed.

### Durable, restart-surviving state (single host)
```sh
FOUNDRY_STATE_DIR=/data uv run python -m foundry.cli "..."
```
Set one env var and the checkpointer, outbox + WORM audit, the trust ledgers (stable-FAIL latch / nonces / alpha-look), and the CAS become file/volume-backed — a single-host deployment survives a restart. Unset, it's the in-memory default (byte-identical).

---

## The product lifecycle

Foundry owns software *over its life*, not just at first ship:

- **Build** — intent → released artifact (above).
- **Steer** — a human injects direction mid-run (a checkpoint), recorded as *authorship* for segregation-of-duties.
- **Evolve** — change requests re-enter through the fail-closed maintenance re-gate; ADRs/constraints *supersede* rather than accrete.
- **Feedback loop** — production signals (defects, regressions, feature requests) become backlog items that re-enter the evolution route.

---

## Deployment

| Tier | How | What you get |
|---|---|---|
| **Dev / smoke** | `uv run python -m foundry.cli …` | deterministic, no network, in-memory state |
| **Single host** | **docker-compose** ([`docs/deploy-compose.md`](./docs/deploy-compose.md)) + `FOUNDRY_STATE_DIR` | durable filesystem CAS + file-backed checkpoints/ledgers; isolated proxy/signer/builder containers; real scanner services |
| **Fleet / cloud** | EKS (documented path) | the *same* images + interfaces — CAS→S3, SQLite→Postgres, secrets→K8s/IRSA, scale→replicas+HPA, execution→broker-backed worker pool |

The compose profile is written **12-factor / EKS-portable by construction** — moving to Kubernetes is a backend swap, not a rewrite, and it physically realizes the two-zone / single-egress isolation that's logical-only in dev.

---

## Architecture & repository map

```text
foundry/
├── schemas/      frozen pydantic records (artifacts · gates · governance · docs · …)
├── state/        FactoryState, None-safe reducers, Seq total order, tree_sha, GlobalRecords
├── graph/        the StateGraph: supervisor · role subgraphs · gate-wiring · factory_graph
├── agents/       role agents (product · architecture · implementation · quality · security) + work-journal + interview
├── gates/        two-zone runner · attestation/signing · Spec/Build/Test/Security/Audit gates · discriminator
├── proxy/        single-egress model proxy · lineage map · swappable backends (SDK · claude-cli · codex · ollama)
├── trust_root/   hermetic builder · input-bound provenance · admission controller · deployer (Autonomous)
├── governance/   sign-off · evidence pack · verdict classes · mode/tier policy
├── domains/      DomainPackContract · registry · regulated / ml_data / frontend packs
├── durability/   content-addressed ArtifactStore (+ filesystem backend) · checkpointer · outbox · WORM audit · persistence
├── execution/    WorkerAdapter contract · Sandbox · adapters · scalable work-pool seam
├── lifecycle/    maintenance · evolve · feedback loop · steering · provenance · modes
├── evaluation/   pre-registered A/B · power/pilot · role ablation · scorecard · eval safeties
├── operations/ · observability/ · monitors/ · policy/ · context/   (operability layer)
├── docs/         per-build doc-set assembler + BUILD-MANIFEST + technical writer
└── ideation/     interactive spec co-generation front-end
```

**Design docs** (the authoritative spec): [`PLAN.md`](./PLAN.md) · [`architecture.md`](./docs/architecture.md) · [`agents.md`](./docs/agents.md) · [`governance.md`](./docs/governance.md) · [`domains.md`](./docs/domains.md) · [`lifecycle.md`](./docs/lifecycle.md) · [`evaluation.md`](./docs/evaluation.md) · [`operations.md`](./docs/operations.md) · [`roadmap.md`](./docs/roadmap.md) · [`implementation-plan.md`](./docs/implementation-plan.md) (**§0.3 = the dev-grade honesty split**).

---

## Development

```sh
uv run pytest -q -n auto         # full suite (1748 passing) in parallel
uv run ruff check foundry tests  # lint
uv run mypy foundry              # type-check (strict on foundry/)
```

Quality bars are enforced as gates: ruff clean, mypy clean, suite green, and mutation testing on the trust-critical reducer (`merge_gates`, ≥85% kill). There is **no hosted CI by design** — the gate chain is local. The build's own delivery is tracked in [`build/EXECUTION-PLAN.md`](./build/EXECUTION-PLAN.md): every feature landed via an isolated worktree → a fresh-eyes (often adversarial) critic → fix → merge-green loop.

---

## Honesty: what's real vs. dev-grade

Foundry's *logic* — the gates, the trust model, fail-closed admission, the statistical decision machinery — is real, adversarially tested, and load-bearing. What is deliberately **dev-grade** (per implementation-plan §0.3), all behind production-swappable interfaces:

- **Crypto roots** are HMAC keys, not KMS / keyless-OIDC / Sigstore.
- **Worker substrate** in the CLI is a `StubAdapter` (deterministic no-op); the real `claude -p` / Codex / Ollama adapters need a live backend.
- **Persistence** defaults to in-memory; durable filesystem/SQLite exists today (one env var); HA Postgres + S3 is the documented multi-host swap.
- **The two zones / proxy / builder** run in one process in dev; the compose profile gives them real container isolation.
- **Scanners** (OPA/Semgrep/Syft) and the **container/K8s worker pool** are documented seams pending real infra.
- The default profile is single-lineage **self-judge** (`judgment_provenance="self"`, recorded openly); the decorrelation machinery is built and engaged by a cross-vendor backend or the regulated pack.

Nothing dev-grade is hidden — it's labeled in code, in [`build/STATUS.md`](./build/STATUS.md), and here. The path to production is *swapping backends behind stable interfaces*, not rewriting the system.

---

## License & liability

See [`docs/lifecycle.md` §5](./docs/lifecycle.md) for the licensing & liability posture of AI-generated code.
