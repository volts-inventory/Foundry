# Foundry — Master Plan

> A graph-based, multi-agent pipeline that reproduces every layer of a modern
> software engineering organization and drives a request from intent to
> released, audited software.

**Status:** Plan (no application code yet)
**Orchestration target:** LangGraph (Python)
**Scope:** General-purpose software factory with a plugin system for domain specialists

---

## 1. Problem statement

Single-agent "write me an app" tools fail on real software the same way a single
engineer fails on a real org's work: no separation of concerns, no review, no
testing discipline, no security or compliance, no durable plan, and no way to
recover when something goes wrong halfway through. Benchmarks confirm the gap —
frontier models score ~65–75% on isolated SWE-bench Verified issues but only
~21% on long-horizon, multi-commit evolution tasks (SWE-EVO). Real software is
long-horizon.

**Goal:** build the *organization*, not just the coder. Encode the human
workflows that have shipped software for decades — product, architecture,
senior/junior engineering, QA, security, audit, and operations — as a graph of
specialized agents with explicit hand-offs, quality gates, and human oversight.

## 2. Thesis

Three ideas, combined:

1. **Code = SOP(Team)** (MetaGPT). Quality emerges from well-defined Standard
   Operating Procedures executed by specialized roles that communicate through
   *structured artifacts* (PRDs, ADRs, interfaces, test reports, diffs), not
   free-form chat.
2. **The org is a graph** (LangGraph). Roles are nodes; hand-offs are edges;
   progress is gated by *conditional* edges; rework is *loops*; oversight is
   *interrupts*. State is typed, durable, and replayable.
3. **Determinism at the seams** (Anthropic patterns + enterprise guardrails).
   LLMs do judgment work *inside* nodes; the transitions *between* nodes are
   governed by deterministic checks (compile, lint, type, test, scan, attest)
   wherever a deterministic check exists. LLM-as-judge is used only where no
   deterministic oracle exists, and always with auditing.

## 3. The organization, as agents

Seven layers plus a governance/meta layer. Full catalog with SOPs and I/O
contracts in [docs/agents.md](./docs/agents.md).

| Layer | Roles (agents) | Primary artifacts produced |
|---|---|---|
| **1. Product & Planning** | Stakeholder/Client Proxy, Product Manager, Engineering/Program Manager | Intent brief, PRD + user stories, acceptance criteria, sprint plan, budget & risk register |
| **2. Architecture & Design** | Principal Architect / Eng Lead, Domain Specialist (pluggable), UX/Design | System design, ADRs, interface/API contracts, data model, UX spec |
| **3. Implementation** | Senior Engineer(s), Junior Engineer(s) | Task breakdown, scaffolding, feature diffs, PRs, inline code review of juniors |
| **4. Quality & Verification** | QA Engineer, SDET, Reviewer/Critic | Test plan, unit/integration/e2e tests, exploratory findings, PR review verdicts |
| **5. Security & Compliance** | Security/AppSec Engineer, Audit/Compliance | Threat model, SAST/DAST/secret/dep findings, SBOM, license & regulatory mapping, audit trail |
| **6. Operations** | DevOps/SRE, Release Manager | CI/CD pipeline, IaC, observability config, deploy/rollback plan, versioned release + changelog |
| **7. Governance / Meta** | Orchestrator/Supervisor, Guardian, Budget Controller, Human Liaison | Routing decisions, policy enforcement, cost ledger, escalations |

Why both **senior and junior** engineers? It is not cosmetic. Seniors do
decomposition, scaffolding, the hard parts, and *review* — juniors implement
well-specified tasks cheaply. This maps directly onto **model tiering** (§6):
expensive models on architecture/review, cheaper models on well-scoped
implementation, cheapest on routing. The senior→junior review relationship *is*
the evaluator-optimizer pattern.

The **Guardian** agent (a "guardian agent" in Gartner's sense) monitors the
other agents' trajectories for policy violations, loops, runaway cost, and
reward-hacking, and can halt or escalate.

## 4. How work flows through the graph

The pipeline is a LangGraph `StateGraph`. Each layer is a **subgraph**; a
**supervisor** routes between them; **conditional edges** gate progress on
passing checks; **loops** handle rework with bounded retries; **interrupts**
pause for human sign-off. Full topology and diagram in
[docs/architecture.md](./docs/architecture.md).

```
Intent ─▶ Triage/Route ─▶ Product ─▶ Architecture ─▶ Planning ─▶
   ┌──────────────────── Implementation ⇄ Review/QA (evaluator-optimizer) ───────────────────┐
   │                                                                                          │
   └─▶ Security & Audit (parallel) ─▶ Integration/E2E ─▶ Release gate (HITL) ─▶ Deploy ─▶ Done
```

**Composable patterns applied** (Anthropic, "Building Effective Agents"):

- **Routing** — triage the request and pick the path (bugfix vs. greenfield vs. evolution vs. spike).
- **Orchestrator-workers** — the supervisor decomposes and delegates to role pods.
- **Evaluator-optimizer** — engineer ⇄ reviewer/QA loops until the work passes.
- **Parallelization** — independent tasks implemented concurrently; security, QA, and performance reviews run in parallel (scatter-gather).
- **Prompt chaining** — the ordered steps *inside* each role's SOP.
- **Reflection** — agents critique their own output before hand-off.
- **Human-in-the-loop** — interrupts at design approval and release.

## 5. State, memory, and artifacts

- **Typed shared state** — a Pydantic `FactoryState` (request, plan, artifacts, gate results, budget, history) flows through the graph.
- **Durable checkpoints** — a Postgres (or SQLite for dev) checkpointer persists every step, enabling crash recovery, time-travel debugging, and resumable long-horizon runs.
- **Artifacts as the communication medium** — PRD, ADRs, interface contracts, test reports, and diffs live in a versioned artifact store and the Git repo, not in chat history (MetaGPT's structured-communication principle, which reduces error propagation).
- **Long-term memory** — a vector store (pgvector) over the codebase and a decision log for RAG, so agents ground changes in existing code and past decisions.
- **Per-agent scratchpads** — short-term reasoning that does *not* pollute shared state.

## 6. Technology stack

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.12+ | Ecosystem fit for LangGraph/LangChain and agent tooling |
| Orchestration | **LangGraph** (`StateGraph`, checkpointers, interrupts) | Graph-native; durable state; HITL; production-proven |
| State schema | Pydantic v2 | Typed, validated state and artifact contracts |
| Models | Claude, **tiered by role** — Opus for architecture/review, Sonnet for implementation, Haiku for routing/cheap tasks | Cost/quality match; high-stakes judgment gets the strongest model |
| Tools | MCP servers for filesystem, git, test runners, CI, package audit | Standard tool interface; least-privilege per role |
| Code execution | Sandboxed runners (containers; Firecracker/gVisor for stronger isolation) | Never run agent-written code on the host |
| Source of truth | Git + GitHub (PRs as the review surface) | Real review workflow; diffable artifacts |
| Persistence | Postgres (checkpoints + ledger) + pgvector (memory) + object store (artifacts) | Durable, queryable, recoverable |
| Observability | OpenTelemetry traces + Langfuse/LangSmith | Trace every tool call, token, and cost; required for evals |
| Policy | OPA / policy-as-code | Deterministic guardrails on what each agent may do |

Model tiering is a first-class design decision, not an afterthought: it is the
primary lever for keeping cost-per-feature bounded (§ Budget Controller).

## 7. Quality gates (the seams)

Hard gates sit *between* phases. A gate is deterministic wherever an oracle
exists; failures route back to the responsible role with bounded retries, then
escalate to a human. Full definitions in [docs/governance.md](./docs/governance.md).

1. **Spec gate** — acceptance criteria exist and are testable.
2. **Design gate** — ADR approved, interfaces/data model defined (HITL optional).
3. **Build gate** — compiles, lints, type-checks.
4. **Test gate** — unit + integration green; coverage ≥ threshold; no flaky-skip.
5. **Security gate** — SAST + secret scan + dependency audit clean; threat-model items addressed.
6. **Audit gate** — SBOM generated; licenses compatible; SLSA provenance attested.
7. **Release gate** — human sign-off; changelog + version + rollback plan present.

## 8. Guardrails & governance (five planes)

The enterprise-standard five-plane model, applied to a coding factory
([docs/governance.md](./docs/governance.md)):

- **Policy plane** — OPA/permission checks on every tool call; least privilege per role.
- **Execution plane** — all generated code runs in sandboxes (containers / microVMs); no host access.
- **Data plane** — secret and PII handling, redaction, scoped credentials.
- **Observability plane** — OpenTelemetry traces of every step (tool, args, tokens, cost, latency).
- **Assurance plane** — SBOM, SLSA attestations, artifact signing, red-teaming, and evals run in CI.

## 9. Pluggable domain specialists

A **Specialist** is a graph node conforming to a stable interface
(`capabilities`, `tools`, `prompts/SOP`, `gates`, `evals`). A **registry** maps
required capabilities (declared by the PM/Architect) to available specialists,
and the supervisor routes accordingly. Specialists ship as **domain packs**:

- *Fintech pack* → adds a Compliance specialist + PCI/SOX checks to the audit gate.
- *Healthcare pack* → adds HIPAA mapping + PHI data-plane rules.
- *ML/Data pack* → adds an ML engineer + data-validation and model-eval gates.
- *Mobile / Embedded / Frontend packs* → domain-specific build/test gates.

This keeps the core factory general while letting it get genuinely expert in a
domain by plugging in nodes — no core changes required.

## 10. Evaluation — does the factory work?

We measure both *trajectory* and *outcome* ([docs/evaluation.md](./docs/evaluation.md)):

- **Trajectory evals** — tool-choice correctness, argument validity, step count, gate pass-rate, policy compliance, cost, latency. Deterministic tool mocks in CI; LLM-as-judge only with auditing.
- **Outcome evals** — build the thing, run *its* tests, and score against a golden task suite plus SWE-bench-Verified-style issues and SWE-EVO-style long-horizon evolution tasks.
- **Headline metrics** — task resolution rate, rework count per feature, escaped-defect rate, cost-per-feature, wall-clock per feature, human-intervention rate.
- **Regression** — golden tasks run in CI on every change to prompts, graph, or tools.

## 11. Roadmap (phased)

Each phase has explicit exit criteria ([docs/roadmap.md](./docs/roadmap.md)):

- **Phase 0 — Foundations:** repo, `FactoryState` schema, supervisor + single agent loop, sandbox runner, tracing.
- **Phase 1 — Core vertical slice:** PM → Architect → Senior Eng → QA builds a small *real* app behind spec/build/test gates.
- **Phase 2 — Review & assurance:** Reviewer, Security, Audit agents; evaluator-optimizer loops; HITL interrupts.
- **Phase 3 — Operations & scale:** parallelization, DevOps/SRE, Release Manager, full gate set, five guardrail planes.
- **Phase 4 — Specialist plugin system:** registry, capability routing, first two domain packs.
- **Phase 5 — Eval & optimization:** full eval harness + benchmark; AFlow-style workflow tuning; cost controls; hardening.

## 12. Known limitations & risks (eyes open)

- **Long-horizon reliability** — even SOTA struggles on multi-commit evolution (SWE-EVO ~21%). Mitigation: durable checkpoints, bounded retries, aggressive HITL on large changes, smaller task decomposition.
- **Reward hacking / spec-gaming** — agents "passing the gate" by gaming it (e.g., weakening tests). Mitigation: Guardian agent, immutable acceptance tests, review of test *changes*, mutation testing.
- **LLM-judge unreliability** — overuse of LLM-as-judge. Mitigation: prefer deterministic oracles; audit judge outputs; calibrate against human labels.
- **Cost & latency blow-ups** — multi-agent loops are expensive. Mitigation: model tiering, Budget Controller with hard caps, parallelization, caching.
- **Hallucinated APIs / dependencies** — Mitigation: RAG over real codebase + docs, dependency-existence checks, sandboxed build gate.
- **Security of the factory itself** — prompt injection via fetched content/issues. Mitigation: policy plane, untrusted-content handling, least-privilege tools, sandboxed execution.

## 13. How this plan was hardened

This plan was deliberately red-teamed across multiple critique passes against the
practices used in industry today (MetaGPT/ChatDev SOP agents, OpenHands/SWE-agent
execution, Anthropic's workflow patterns, and the enterprise five-plane guardrail
model). The critique passes, the gap fixes, the conformance mapping, and a
maturity rubric are recorded in [docs/iteration-log.md](./docs/iteration-log.md).

---

### Sources

- Anthropic — *Building Effective AI Agents*: https://www.anthropic.com/research/building-effective-agents
- MetaGPT (FoundationAgents): https://github.com/FoundationAgents/MetaGPT — paper: https://arxiv.org/abs/2308.00352
- ChatDev (IBM overview): https://www.ibm.com/think/topics/chatdev
- LangGraph multi-agent orchestration (2025 guide): https://latenode.com/blog/ai-frameworks-technical-infrastructure/langgraph-multi-agent-orchestration/langgraph-multi-agent-orchestration-complete-framework-guide-architecture-analysis-2025
- SWE-bench results viewer: https://www.swebench.com/viewer.html
- SWE-EVO (long-horizon evolution benchmark): https://arxiv.org/html/2512.18470v5
- OpenHands coding-agent evaluation: https://openhands.dev/blog/evaluation-of-llms-as-coding-agents-on-swe-bench-at-30x-speed
- Enterprise agent guardrails (five-plane model): https://skywork.ai/blog/agentic-ai-safety-best-practices-2025-enterprise/
