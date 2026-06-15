# Foundry — An AI Software Factory

Foundry is a design for a **graph-based, multi-agent pipeline that mimics a full
software engineering organization**. A directed graph of role-specialized agents
(product, architecture, engineering, QA, security, audit, DevOps, and pluggable
domain specialists) collaborates through standard operating procedures (SOPs),
deterministic quality gates, durable state, and human-in-the-loop checkpoints to
take a request from intent to released, audited software.

The build is underway: the **Phase 0 state backbone** is implemented and tested
(`foundry/` + `tests/`) — typed state + reducers, `tree_sha`, global cross-fork
records, dev-grade attestation, the transactional outbox/WORM audit, and a
checkpointed skeleton graph whose kill-then-resume produces no duplicate
external effect. See [docs/build-log.md](./docs/build-log.md) for what is built,
the inefficiencies caught along the way, and recorded deviations.

```sh
uv sync --dev && uv run pytest   # Python 3.12; property tests are the Phase 0 gate
```

## Read in this order

1. **[PLAN.md](./PLAN.md)** — the master plan (problem, thesis, architecture, stack, roadmap).
2. **[docs/architecture.md](./docs/architecture.md)** — graph topology, state model, orchestration patterns.
3. **[docs/agents.md](./docs/agents.md)** — the full catalog of stakeholder agents, their SOPs and I/O contracts.
4. **[docs/governance.md](./docs/governance.md)** — quality gates, the 5-plane guardrail model, security & compliance.
5. **[docs/evaluation.md](./docs/evaluation.md)** — how we measure whether the factory actually works.
6. **[docs/roadmap.md](./docs/roadmap.md)** — phased delivery, sequenced to falsify the core bet early and earn autonomy incrementally.
7. **[docs/operations.md](./docs/operations.md)** — Day-2 operability: run-status, store recovery, trusted-component availability, multi-tenancy, HITL-load, upgrades.
8. **[docs/lifecycle.md](./docs/lifecycle.md)** — product lifecycle: maintenance of generated code, human steering, feedback loop, deployment-trust modes, AI-code licensing/liability.
9. **[docs/domains.md](./docs/domains.md)** — the verdict-class taxonomy, the domain-pack contract, and per-domain specs (regulated, ML/data, frontend) — because *correctness is not one shape*.
10. **[docs/iteration-log.md](./docs/iteration-log.md)** — the full multi-round critique record and conformance check against today's state of the art.
11. **[docs/implementation-plan.md](./docs/implementation-plan.md)** — the build-ready implementation plan: pinned stack (Python + LangGraph + Anthropic), repo layout, schemas, per-component interfaces, and the phase-by-phase task backlog that turns the design into code.
12. **[docs/build-log.md](./docs/build-log.md)** — the live build status against that plan: what is implemented, the inefficiency catches, deviations, and what's next.

## Design foundations (state of the art, 2024–2026)

- **SOP-driven role agents** — MetaGPT's "Code = SOP(Team)" and ChatDev's communicative agents.
- **Graph orchestration** — LangGraph `StateGraph`: nodes, conditional edges, durable checkpoints, interrupts.
- **Composable workflow patterns** — Anthropic's orchestrator-workers, evaluator-optimizer, routing, parallelization, reflection, human-in-the-loop.
- **Coding-agent execution** — a *pluggable worker-substrate contract* (the agent-computer interface) with provider adapters — Claude Code (`claude -p`), OpenAI Codex CLI (`codex exec`), OpenHands/SWE-agent, or a raw multi-provider loop — so the inner coding loop stays provider-agnostic and a *different-family* adapter can review the implementer's work. Benchmarked on SWE-bench Verified and long-horizon SWE-EVO-style tasks.
- **Enterprise guardrails** — the five-plane model (policy / execution / data / observability / assurance) with SBOM, SLSA, and evals shipped in CI.
