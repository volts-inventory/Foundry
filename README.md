# Foundry — An AI Software Factory

Foundry is a design for a **graph-based, multi-agent pipeline that mimics a full
software engineering organization**. A directed graph of role-specialized agents
(product, architecture, engineering, QA, security, audit, DevOps, and pluggable
domain specialists) collaborates through standard operating procedures (SOPs),
deterministic quality gates, durable state, and human-in-the-loop checkpoints to
take a request from intent to released, audited software.

This repository currently contains **the plan only** — no application code yet.

## Read in this order

1. **[PLAN.md](./PLAN.md)** — the master plan (problem, thesis, architecture, stack, roadmap).
2. **[docs/architecture.md](./docs/architecture.md)** — graph topology, state model, orchestration patterns.
3. **[docs/agents.md](./docs/agents.md)** — the full catalog of stakeholder agents, their SOPs and I/O contracts.
4. **[docs/governance.md](./docs/governance.md)** — quality gates, the 5-plane guardrail model, security & compliance.
5. **[docs/evaluation.md](./docs/evaluation.md)** — how we measure whether the factory actually works.
6. **[docs/roadmap.md](./docs/roadmap.md)** — phased delivery plan with exit criteria.
7. **[docs/iteration-log.md](./docs/iteration-log.md)** — the critique passes and conformance check against today's state of the art.

## Design foundations (state of the art, 2024–2026)

- **SOP-driven role agents** — MetaGPT's "Code = SOP(Team)" and ChatDev's communicative agents.
- **Graph orchestration** — LangGraph `StateGraph`: nodes, conditional edges, durable checkpoints, interrupts.
- **Composable workflow patterns** — Anthropic's orchestrator-workers, evaluator-optimizer, routing, parallelization, reflection, human-in-the-loop.
- **Coding-agent execution** — lessons from SWE-agent (agent-computer interface) and OpenHands (multi-agent platform), benchmarked on SWE-bench Verified and long-horizon SWE-EVO-style tasks.
- **Enterprise guardrails** — the five-plane model (policy / execution / data / observability / assurance) with SBOM, SLSA, and evals shipped in CI.
