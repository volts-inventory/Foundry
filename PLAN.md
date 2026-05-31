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
testing discipline, no security or compliance, no durable plan, and no recovery
when something breaks halfway through. Benchmarks confirm the gap — frontier
models score ~65–75% on isolated SWE-bench Verified issues but only ~21% on
long-horizon, multi-commit evolution tasks (SWE-EVO). Real software is
long-horizon.

**Goal:** build the *organization*, not just the coder. Encode the human
workflows that have shipped software for decades — product, architecture,
senior/junior engineering, QA, security, audit, and operations — as a graph of
specialized agents with explicit hand-offs, quality gates, and human oversight.

**This is a falsifiable bet, not an assumption.** That the org structure beats a
strong single agent on long-horizon work is an empirical claim, so the plan
includes a controlled A/B against a single-agent baseline as an exit criterion
([docs/evaluation.md §1](./docs/evaluation.md)). If structure doesn't help, the
design must change.

## 2. Thesis

Three ideas, combined:

1. **Code = SOP(Team)** (MetaGPT). Quality emerges from well-defined Standard
   Operating Procedures executed by specialized roles that communicate through
   *structured, versioned artifacts* (PRDs, ADRs, interfaces, test reports,
   diffs), not free-form chat.
2. **The org is a graph** (LangGraph). Roles are nodes; hand-offs are edges;
   progress is gated by *conditional* edges; rework is *bounded loops*; oversight
   is *interrupts*. State is typed, durable, and replayable — with explicit
   concurrency semantics (reducers) and side-effect idempotency.
3. **A deterministic decision rule at the seams.** LLMs do judgment *inside*
   nodes; the PASS/FAIL *rule* between nodes is a deterministic function of
   observed outputs wherever an oracle exists. We are honest that the *outputs*
   (real builds/tests) are often non-deterministic, and handle flakiness
   explicitly. LLM-as-judge is used only where no oracle exists, always audited.

## 3. The organization, as agents

Seven layers plus a governance/meta layer. Full catalog with SOPs and I/O
contracts in [docs/agents.md](./docs/agents.md).

| Layer | Roles (agents) | Primary artifacts produced |
|---|---|---|
| **1. Product & Planning** | Stakeholder/Client Proxy, Product Manager, Engineering/Program Manager | Intent brief, PRD + user stories, acceptance criteria, sprint plan, budget & risk register |
| **2. Architecture & Design** | Principal Architect / Eng Lead, Domain Specialist *(conditional)*, UX/Design *(conditional)* | System design, ADRs, interface/API contracts, data model, ownership map, UX spec |
| **3. Implementation** | Senior Engineer(s), Junior Engineer(s) | Task breakdown, branch-per-task scaffolding, feature diffs, PRs, code review, conflict resolutions |
| **4. Quality & Verification** | QA Engineer, SDET, Reviewer/Critic (+ CI runner) | Test plan, criterion-traceable tests, e2e suite, build/test results, review verdicts |
| **5. Security & Compliance** | Security/AppSec Engineer, Audit/Compliance | Threat model, SAST/DAST/secret/dep findings, SBOM, license & control mapping |
| **6. Operations** | DevOps/SRE, Release Manager (+ trusted CI builder) | CI/CD pipeline, IaC, observability config, deploy/rollback, SLSA-attested signed release |
| **7. Governance / Meta** | Orchestrator/Supervisor, Guardian (monitors + analyzer), Budget Controller, Human Liaison | Routing, policy enforcement, cost ledger, escalations |

Why both **senior and junior** engineers? Seniors do decomposition, scaffolding,
the hard parts, *review*, and *integration/conflict resolution* — juniors
implement well-specified tasks cheaply. This maps onto **model tiering** (§6):
expensive models on architecture/review, cheaper on well-scoped implementation,
cheapest on routing. The senior→junior review relationship *is* the
evaluator-optimizer pattern.

The **Guardian** is *not* one vague LLM: deterministic monitors handle loops,
cost, and policy (no LLM, no injection surface), and a separately-sandboxed,
toolless analyzer raises *advisory* flags for semantic concerns
([docs/governance.md §3](./docs/governance.md)).

## 4. How work flows through the graph

The pipeline is a LangGraph `StateGraph` with single-entry/single-exit
subgraphs, a supervisor for routing, conditional edges that gate progress,
bounded rework loops, an explicit assurance join, and interrupts for human
sign-off. Full topology, diagram, and the concurrency/merge/side-effect model in
[docs/architecture.md](./docs/architecture.md).

```
Intent ─▶ Triage ─▶ (Product ─▶ Architecture ─▶ Planning) ─▶ Decompose (branch per task)
   ┌─ per task: Implement ⇄ Review/QA, per-branch Build+Test ─┐
   └─▶ Integrate (merge+conflicts) ─▶ Integrated Build+Test ─▶ Security ‖ Audit ─▶ join
        ─▶ E2E gate ─▶ Trusted builder (SBOM/SLSA/sign) ─▶ Release gate (HITL) ─▶ Deploy ─▶ Done
```

**Load-bearing control rules** (see architecture for detail):

- **Re-verification:** any code change re-enters Build+Test before any
  downstream gate — no path skips verification after a fix.
- **Assurance join:** parallel Security/Audit rejoin at one node; failures route
  to a *targeted* fix, not a restart.
- **Release rejection is targeted:** a human "no" carries a reason and routes to
  a fix, never a full re-decomposition.

**Composable patterns applied** (Anthropic, "Building Effective Agents"):
routing, orchestrator-workers, evaluator-optimizer, parallelization
(scatter-gather), prompt chaining, reflection, and human-in-the-loop —
mapped to locations in [docs/architecture.md §8](./docs/architecture.md).

## 5. State, memory, artifacts, and source control

- **Typed shared state with reducers** — a `TypedDict` `FactoryState` whose
  concurrently-written channels (`artifacts`, `gates`, `tasks`, `history`,
  `actions`, …) declare explicit merge reducers so parallel branches don't
  clobber each other ([docs/architecture.md §3](./docs/architecture.md)).
- **Content-addressed, versioned artifacts** — PRD, ADRs, interfaces, test
  reports, diffs are immutable and versioned; a "rewrite" supersedes rather than
  mutates (MetaGPT's structured-communication principle).
- **Source-control isolation** — branch-/worktree-per-task; per-branch gates,
  then a deterministic **integration node** with conflict resolution, then an
  authoritative integrated Build+Test gate
  ([docs/architecture.md §4](./docs/architecture.md)).
- **Durable checkpoints + side-effect ledger** — Postgres checkpointer for
  resume/time-travel of *internal* state; an **external-action ledger** with
  idempotency keys so resume/replay never double-commits, double-PRs,
  double-tags, or re-deploys ([docs/architecture.md §5](./docs/architecture.md)).
- **Bounded context, not the full history** — each node has a context-assembly
  contract (task + relevant interfaces + retrieved code spans + *summarized*
  history); repo grounding uses a symbol index + embeddings, not raw reading
  ([docs/architecture.md §6](./docs/architecture.md)).
- **Append-only audit store** — compliance events go to a separate WORM /
  hash-chained store, distinct from the mutable, rewindable checkpoint
  ([docs/governance.md §4](./docs/governance.md)).

## 6. Technology stack

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.12+ | Ecosystem fit for LangGraph/LangChain |
| Orchestration | **LangGraph** (`StateGraph`, reducer channels, checkpointers, interrupts) | Graph-native; durable state; HITL; production-proven |
| State schema | `TypedDict` + reducers for the graph; Pydantic v2 for artifact values | Reducers compose with LangGraph's channel model; values stay validated |
| Models | Claude, **pinned IDs, tiered by role** — T1 (Opus-class) architecture/review/security, T2 (Sonnet-class) implementation, T3 (Haiku-class) routing | Cost/quality match; **T1 never auto-downgrades** — a budget breach there escalates |
| Model resilience | Same-tier failover on rate-limit/unavailability; backoff | No silent cross-tier-down on high-stakes nodes |
| Tools | MCP servers for filesystem, git, test runners, CI, package audit | Standard interface; least-privilege per role via OPA |
| Code execution | Sandboxed runners (containers; Firecracker/gVisor) | Never run agent-written code on the host |
| Supply chain | **Trusted hermetic CI builder** (outside agent control) + keyless OIDC/sigstore signing + SLSA L3 provenance | Attestation from an agent-controlled sandbox is worthless; keys air-gapped from sandboxes |
| Source of truth | Git + GitHub (branch-per-task, PRs) | Real review workflow; diffable, versioned |
| Persistence | Postgres (checkpoints + budget ledger), pgvector + symbol index (memory/retrieval), content-addressed object store (artifacts), WORM store (audit) | Durable, recoverable, tamper-evident where it must be |
| Observability | OpenTelemetry traces + Langfuse/LangSmith | Trace every tool call, token, USD; feeds monitors and evals |
| Cost control | Per-phase/per-loop sub-budgets, prompt caching of SOPs, metered monitor overhead | Multi-agent loops are where money leaks ([docs/governance.md §7](./docs/governance.md)) |

## 7. Quality gates (the seams)

Hard gates sit *between* phases; failures route back with bounded retries, then
escalate. Each gate records whether its **decision rule** and its **execution**
were deterministic. Full definitions, flake handling, and anti-gaming in
[docs/governance.md §1](./docs/governance.md).

1. **Spec** — acceptance criteria exist, are testable, and carry `criterion_id`s (bugfixes: a repro + expected-behavior brief).
2. **Design** — ADRs approved; interfaces, data model, ownership map defined (HITL for significant designs).
3. **Build** *(per-branch + integrated)* — compiles, lints, type-checks (CI runner).
4. **Test** *(per-branch + integrated)* — green; **every acceptance criterion exercised** (criterion-coverage); coverage ≥ threshold; flakes quarantined, not retried-to-green (QA, with authorship separated from implementation).
5. **Security** — SAST + secret scan + dependency audit clean; threat-model items addressed.
6. **Audit** — SBOM clean (no critical CVEs / disallowed licenses); SBOM mapped to controls (signing & SLSA provenance are produced later by the trusted builder).
7. **E2E** — end-to-end scenarios green on the integrated tree (SDET; flake policy applies).
8. **Release** — human sign-off on an already-attested artifact; signature + provenance + changelog + version + rollback plan present.

## 8. Guardrails & governance (five planes)

The enterprise-standard five-plane model, applied to a coding factory
([docs/governance.md §2](./docs/governance.md)): **policy** (OPA, least
privilege, signing reachable only by the trusted builder), **execution**
(sandboxes/microVMs, default-deny egress, mocked network in tests), **data**
(scoped short-lived creds, keys air-gapped from sandboxes, PII redaction),
**observability** (OpenTelemetry traces feeding monitors and evals), and
**assurance** (SBOM, SLSA L3, signing, red-teaming, evals in CI).

## 9. Pluggable domain specialists

A **Specialist** is a conditional graph node implementing `{capabilities, tools,
prompts/SOP, gates, evals}`. A **registry** maps required capabilities (declared
by PM/Architect) to specialists; the supervisor routes via a conditional edge,
and downstream consumers tolerate absent optional artifacts
([docs/architecture.md §7](./docs/architecture.md)). Specialists ship as **domain
packs** (fintech → PCI/SOX; healthcare → HIPAA/PHI; ML/data → data-validation +
model-eval gates; mobile/embedded → platform gates), keeping the core general
while it becomes genuinely expert on demand.

## 10. Evaluation — does the factory work?

We measure *trajectory* and *outcome*, set thresholds the system **can fail**,
and run the single-agent A/B that tests the central hypothesis
([docs/evaluation.md](./docs/evaluation.md)):

- **A/B vs strong single-agent baseline** on identical SWE-EVO-style tasks at
  equal budget — Foundry must win by a pre-registered margin at ≤ equal cost.
- **Outcome:** golden suite with *hidden* tests + SWE-bench-Verified-style +
  SWE-EVO-style long-horizon.
- **Trajectory:** tool-choice, loop count, first-pass yield, policy compliance,
  cost (incl. monitor overhead), side-effect idempotency.
- **Scorecard with concrete thresholds**; regressions block changes in CI.

## 11. Roadmap (phased)

Each phase has explicit exit criteria ([docs/roadmap.md](./docs/roadmap.md)):

- **Phase 0 — Foundations:** repo, reducer-annotated `FactoryState`, supervisor + single agent loop, sandbox runner, side-effect ledger, tracing, policy-plane stub.
- **Phase 1 — Core vertical slice:** PM → Architect → Senior Eng → QA builds a small *real* app behind Spec/Build/Test gates; first single-agent A/B smoke test.
- **Phase 2 — Review & assurance:** Reviewer, Security, Audit; evaluator-optimizer loops; HITL interrupts; trusted-builder signing/SLSA.
- **Phase 3 — Operations & scale:** branch-per-task parallelism + integration, DevOps/SRE, Release Manager, full gate set, five guardrail planes, deterministic monitors + analyzer.
- **Phase 4 — Specialist plugin system:** registry, capability routing, first two domain packs.
- **Phase 5 — Eval & optimization:** full eval harness + the decisive A/B; constrained workflow optimization; cost/latency hardening; red-team.

## 12. Known limitations & risks (eyes open)

- **Long-horizon reliability** — unsolved industry-wide (~21% on SWE-EVO). Mitigation: durable checkpoints, bounded retries, decomposition, HITL on large changes — *and the A/B that tells us whether our structure actually helps.*
- **Reward hacking / spec-gaming** — Mitigation: separated test authorship, immutable+criterion-traceable acceptance tests, test-diff review, mutation testing, and the deterministic retry-until-green monitor.
- **LLM-judge unreliability** — Mitigation: prefer deterministic oracles; audit and human-calibrate judges; the Guardian analyzer has its own precision/recall evals.
- **Cost & latency blow-ups** — Mitigation: per-phase/per-loop sub-budgets with graceful checkpoint-and-pause, model tiering, prompt caching, metered monitor overhead; **no T1 downgrade**.
- **Parallel git conflicts / non-reproducible builds** — Mitigation: branch isolation + deterministic integration + flake quarantine; integrated gate is authoritative.
- **Non-idempotent side effects on resume** — Mitigation: external-action ledger + idempotency keys; time-travel rewinds internal state only.
- **Supply-chain theater** — Mitigation: attestation/signing by a trusted hermetic builder, not agents; keys air-gapped.
- **Prompt injection via untrusted content** — Mitigation: policy plane, untrusted-content handling, least-privilege tools, sandboxed (toolless) analyzer that doesn't share the acting agents' surface.

## 13. How this plan was hardened

This plan was red-teamed by an independent critic agent in a
critic → solutioner → full-rewrite loop, run until no BLOCKER/MAJOR findings
remained. The findings, their resolutions, the conformance mapping to current
industry practice, and a maturity assessment are recorded in
[docs/iteration-log.md](./docs/iteration-log.md).

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
