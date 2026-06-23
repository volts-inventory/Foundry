# Foundry — An AI Software Factory

Foundry is a **graph-based, multi-agent pipeline that mimics a full software
engineering organization**. A directed graph of role-specialized agents (product,
architecture, engineering, QA, security, audit, DevOps, and pluggable domain
specialists) collaborates through standard operating procedures (SOPs),
deterministic quality gates, durable state, and human-in-the-loop checkpoints to
take a request from intent to released, audited software.

This repository is **both the design and a working build**. The design lives in
[PLAN.md](./PLAN.md) + [docs/](./docs/); the build lives in `foundry/` + `tests/`.
The system runs end-to-end today from a one-line request — behind real,
adversarially-tested gate/trust/state machinery — on **dev-grade roots** (see the
honesty section below). Run it:

```sh
uv sync --dev
uv run python -m foundry.cli "build a tiny URL-shortener service"
```

## What is built (mapped to the phases)

All phases (0–6) are implemented in `foundry/` with the suite in `tests/`. At a
glance:

| Capability | Where | Phase |
|---|---|---|
| Typed `FactoryState` + `None`-safe reducers, `Seq` total order, `tree_sha`, cross-fork `GlobalRecords`, transactional outbox + WORM audit, durable checkpointer, resumable skeleton graph | `foundry/state/`, `foundry/durability/` | 0 |
| Role subgraphs (PM → Architect → Senior Eng → QA), Spec/Build/Test gates, criterion-traceable tests, content-addressed artifacts, **Assistant mode** (emits a PR-equivalent `open_pr` action) | `foundry/agents/`, `foundry/graph/`, `foundry/gates/` | 1 |
| Eval harness: pre-registered A/B runner, role ablation, power/pilot, scorecard | `foundry/evaluation/` | 1.5, 6 |
| Reviewer/evaluator loop, Security/Audit gates, risk-tiering, dependency-scoped re-verification, HITL Design/Release sign-off, WORM evidence pack, **Gated mode** | `foundry/gates/`, `foundry/governance/`, `foundry/graph/` | 2 |
| Operability models (run-status, recovery, multi-tenancy, HITL-load) | `foundry/` ops modules, `foundry/lifecycle/` | 3 |
| Two-zone trusted gate runner, hermetic build, deploy-time admission, least-privilege deployer, held-out-mutant/behavioral discriminator, **Autonomous mode** | `foundry/gates/runner.py`, `foundry/trust_root/`, `foundry/gates/discriminator.py` | 4 |
| Domain-pack registry + capability routing + strictest-wins composition; **regulated**, **ML/data**, **frontend** packs | `foundry/domains/`, `foundry/domains/packs/` | 5 |
| Lifecycle: maintenance, steering, feedback loop, provenance, licensing | `foundry/lifecycle/` | 6 |

**The three release modes** (`foundry/graph/factory_graph.py`,
`foundry/graph/supervisor.py`):

- **Assistant** (default) — runs the role spine, and on a clean Spec+Build+Test
  PASS emits a bare `open_pr` external action a human merges. This is what the
  CLI runs.
- **Gated** — adds risk-tiering, tier-scaled assurance gates (Security/Audit/
  supply-chain/E2E), recorded Design/Release sign-off, and a tamper-evident WORM
  evidence pack the release travels with. Fail-closed.
- **Autonomous** — the same assurance spine, but the release boundary is the
  trust root (build → admit → deploy, hands-off); admission is the sole release
  gate. Requires the trust-root components be injected.

Gated and Autonomous modes (and domain packs) are reached programmatically via
`build_factory_graph(..., mode=..., pack_registry=...)`; the CLI exercises only
Assistant mode. See [docs/usage.md](./docs/usage.md) for how to wire them.

## Quickstart

Prerequisites: **Python 3.12** and [**uv**](https://docs.astral.sh/uv/).

```sh
uv sync --dev                          # install deps + dev tooling

# the local quality-gate chain (there is NO CI by design — gates are local):
uv run ruff check foundry tests        # lint
uv run mypy foundry                    # type check (the static-analysis bar)
uv run pytest -q                       # property + idempotency + adversarial suites

# end-to-end smoke run (no API key, no network, no claude binary required):
uv run python -m foundry.cli "build a tiny URL-shortener service"
```

The CLI run assembles the full dev wiring with test doubles, compiles the factory
graph, drives the SPEC core path (intake → product → architecture → [spec gate] →
implementation → quality → [build+test gates] → emit), and prints the reviewed
artifact handle, the single `open_pr` action (its idempotency key + payload uri),
and the attested gate statuses. Exit code 0 on a clean Spec/Build/Test PASS.

## Honesty: dev-grade roots

This build draws a deliberate line (see
[docs/implementation-plan.md §0.3](./docs/implementation-plan.md)) between what is
genuinely production-faithful and what is a clearly-labelled dev-grade stand-in.
**The gate/trust/state machinery is real and adversarially tested.** Its *roots*
and *substrate* are dev-grade — production hardening is an ops deployment task:

- **Worker substrate** — the CLI/test runs use `StubAdapter`, a placeholder that
  authors a deterministic no-op blob (not real code). The real
  `ClaudeCodeAdapter` (`claude -p` inside the sandbox) is implemented but needs a
  live Anthropic key + the `claude` binary present.
- **Model provider** — Anthropic via the single-egress `ModelProviderProxy`
  (`AnthropicTransport`, lazy SDK client). Keyless runs use `MockTransport`
  (deterministic scripted completions, no network).
- **Trust roots** — a dev **HMAC** signer (`ZoneBSigner`) stands in for real
  KMS/OIDC/hardware-rooted signing; the two-zone runner is single-process; build
  inputs are constant content-addressed stand-ins. No real KMS, OIDC federation,
  hermetic build infra, or HA Postgres/CAS.
- **Scanners/observability** — gate logic is real; real OPA/Semgrep/Syft scanners
  and a real OpenTelemetry exporter are deployment wiring.
- **No CI by design** — quality gates are the local `ruff`/`mypy`/`pytest` chain,
  not a hosted pipeline.

The trust-root *code* is real and correct; its *roots* (keys, isolated builders,
HA stores) are deployment concerns. "Implemented" never claims more than this.

## Setup, run, and deploy

See **[docs/usage.md](./docs/usage.md)** for the operator-facing guide: setup +
the gate chain, running the three modes (incl. wiring a real Anthropic key + the
`claude_code` adapter, and pointing a domain pack via the registry), and the
exact production-hardening boundary an operator must cross to leave dev-grade.

The orchestration audit trail (build state, decisions, learnings, review) lives in
[`build/`](./build/) — `PROGRESS.md`, `DECISIONS.md`, `LEARNINGS.md`, `AUDIT.md`.

## Read in this order (design docs)

1. **[PLAN.md](./PLAN.md)** — the master plan (problem, thesis, architecture, stack, roadmap).
2. **[docs/architecture.md](./docs/architecture.md)** — graph topology, state model, orchestration patterns.
3. **[docs/agents.md](./docs/agents.md)** — the full catalog of stakeholder agents, their SOPs and I/O contracts.
4. **[docs/governance.md](./docs/governance.md)** — quality gates, the 5-plane guardrail model, security & compliance.
5. **[docs/evaluation.md](./docs/evaluation.md)** — how we measure whether the factory actually works.
6. **[docs/roadmap.md](./docs/roadmap.md)** — phased delivery, sequenced to falsify the core bet early and earn autonomy incrementally.
7. **[docs/operations.md](./docs/operations.md)** — Day-2 operability: run-status, store recovery, trusted-component availability, multi-tenancy, HITL-load, upgrades.
8. **[docs/lifecycle.md](./docs/lifecycle.md)** — product lifecycle: maintenance of generated code, human steering, feedback loop, deployment-trust modes, AI-code licensing/liability.
9. **[docs/domains.md](./docs/domains.md)** — the verdict-class taxonomy, the domain-pack contract, and per-domain specs (regulated, ML/data, frontend).
10. **[docs/iteration-log.md](./docs/iteration-log.md)** — the full multi-round critique record and conformance check against the state of the art.
11. **[docs/implementation-plan.md](./docs/implementation-plan.md)** — the build-ready plan: pinned stack, repo layout, schemas, per-component interfaces, phase backlog. **§0.3 is the dev-grade-roots honesty split.**
12. **[docs/build-log.md](./docs/build-log.md)** — the live build status against that plan.

## Design foundations (state of the art, 2024–2026)

- **SOP-driven role agents** — MetaGPT's "Code = SOP(Team)" and ChatDev's communicative agents.
- **Graph orchestration** — LangGraph `StateGraph`: nodes, conditional edges, durable checkpoints, interrupts.
- **Composable workflow patterns** — Anthropic's orchestrator-workers, evaluator-optimizer, routing, parallelization, reflection, human-in-the-loop.
- **Coding-agent execution** — a *pluggable worker-substrate contract* (the agent-computer interface) with provider adapters (Claude Code `claude -p`, raw multi-provider loop) so the inner coding loop stays provider-agnostic.
- **Enterprise guardrails** — the five-plane model (policy / execution / data / observability / assurance) with SBOM, SLSA, and evals.
