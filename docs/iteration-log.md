# Iteration log — hardening the plan against today's state of the art

The request was to "reiterate over the plan until it passes what is used in
today's world." This document records the critique passes the plan was put
through, the gaps each pass exposed, how they were fixed, a conformance mapping
to current industry practice, and an honest maturity assessment.

## Pass 1 — Naive draft → critique

**Draft:** a linear chain of role agents (PM → Architect → Engineer → QA → done),
each prompting the next in chat.

**Critique (what today's systems would reject):**
- Chat-as-communication propagates errors; MetaGPT showed structured artifacts are better. → *Fixed:* artifacts are the system of record ([architecture.md §6](./architecture.md)).
- No durability — a crash loses everything; long-horizon runs are impossible. → *Fixed:* LangGraph checkpointer, resumable runs ([architecture.md §4](./architecture.md)).
- No verification between steps; "it printed code" ≠ "it works." → *Fixed:* deterministic quality gates at the seams ([governance.md §1](./governance.md)).
- No security, no compliance, no audit trail. → *Fixed:* Security/Audit agents + Security/Audit gates + SBOM/SLSA.

## Pass 2 — Add structure → critique

**State after Pass 1 fixes:** graph with gates, artifacts, security/audit.

**Critique:**
- Gates judged by an LLM are gameable and non-reproducible. → *Fixed:* determinism-at-the-seams; LLM-judge only where no oracle exists, always audited; anti-gaming controls (immutable acceptance tests, test-diff review, mutation testing) ([governance.md §1](./governance.md)).
- No oversight of the agents themselves; runaway loops and cost. → *Fixed:* Guardian agent + Budget Controller + bounded-retry escalation ([agents.md Layer 7](./agents.md)).
- Generated code executed unsafely. → *Fixed:* execution plane (sandboxes/microVMs, default-deny egress) ([governance.md §2](./governance.md)).
- No human control points. → *Fixed:* HITL interrupts at Design and Release gates ([architecture.md §4](./architecture.md)).
- No way to know if any of it works. → *Fixed:* trajectory + outcome eval harness with a scorecard ([evaluation.md](./evaluation.md)).

## Pass 3 — Production-readiness → critique

**State after Pass 2 fixes:** gated graph, guardian, sandboxing, HITL, evals.

**Critique:**
- One model for everything is wasteful and/or under-powered. → *Fixed:* model tiering by role (T1/T2/T3) as the primary cost lever ([PLAN.md §6](../PLAN.md), [agents.md](./agents.md)).
- Monolith can't specialize per domain. → *Fixed:* Specialist plugin interface + registry + domain packs ([governance.md §5](./governance.md)).
- Prompt injection via untrusted request/issue/web content. → *Fixed:* untrusted-content handling rules ([governance.md §3](./governance.md)).
- "Passes SWE-bench" overstates real capability. → *Fixed:* explicit SWE-EVO-style long-horizon evals and an honest limitations section ([PLAN.md §12](../PLAN.md), [evaluation.md §1](./evaluation.md)).
- Observability/policy bolted on late. → *Fixed:* both are Phase 0 foundations ([roadmap.md](./roadmap.md)).

## Conformance mapping — plan vs. industry practice (2024–2026)

| Industry practice | Source | Where Foundry adopts it |
|---|---|---|
| SOP-driven specialized roles; structured-artifact communication | MetaGPT ("Code = SOP(Team)") | [agents.md](./agents.md), [architecture.md §6](./architecture.md) |
| Communicative multi-agent dev with role hand-offs | ChatDev | Layered subgraphs, evaluator-optimizer loop |
| Graph orchestration: nodes, conditional edges, durable state, interrupts | LangGraph | [architecture.md](./architecture.md) |
| Orchestrator-workers, evaluator-optimizer, routing, parallelization, reflection, HITL | Anthropic, *Building Effective Agents* | [architecture.md §5](./architecture.md) |
| Agent-computer interface; sandboxed execution | SWE-agent / OpenHands | Execution plane ([governance.md §2](./governance.md)) |
| Long-horizon evolution is the real bar | SWE-EVO | [evaluation.md §1](./evaluation.md), risk register |
| Five-plane guardrails; SBOM/SLSA; evals in CI; guardian agents | Enterprise agent-safety practice | [governance.md §2](./governance.md), [evaluation.md §4](./evaluation.md) |
| Workflow auto-optimization | MetaGPT AFlow (ICLR 2025) | [evaluation.md §5](./evaluation.md) |

## Maturity rubric

| Level | Description | Foundry phase that reaches it |
|---|---|---|
| L0 | Single agent writes code, no verification | pre-Phase 0 baseline |
| L1 | Role agents + structured artifacts, manual checks | Phase 1 |
| L2 | Automated gates + review/security/audit + HITL | Phase 2 |
| L3 | Parallelism, full guardrails, deploy/rollback, guardian | Phase 3 |
| L4 | Pluggable specialists + measured, optimized, hardened | Phases 4–5 |

The plan targets **L4**. Most public multi-agent coding systems today sit
between L1 and L3; the differentiators that push to L4 are the guardrail planes,
the eval harness with long-horizon benchmarks, and the specialist plugin system.

## Residual gaps (honestly held open)

- **Long-horizon reliability is unsolved industry-wide** (~21% on SWE-EVO). The
  org structure mitigates but does not eliminate this; aggressive decomposition
  + HITL on large changes is the current best lever.
- **Cost at L3+** (many agents, many loops) is real; the Budget Controller caps
  it but tuning the cost/quality frontier is ongoing work (Phase 5).
- **Eval trustworthiness** depends on resisting overfitting and judge drift;
  hidden hold-outs and recalibration are mitigations, not guarantees.

These are not plan defects — they are the genuine frontier. The plan's value is
that it confronts them explicitly rather than assuming them away.
