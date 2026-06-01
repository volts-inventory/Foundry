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
| **1. Product & Planning** | Stakeholder/Client Proxy, Product Manager, Criteria-Completeness Critic *(independent)*, Engineering/Program Manager | Intent brief, PRD + user stories, acceptance criteria (+ completeness critique), sprint plan, budget & risk register |
| **2. Architecture & Design** | Principal Architect / Eng Lead, Domain Specialist *(conditional)*, UX/Design *(conditional)* | System design, ADRs, interfaces, data model, ownership map, `pinned_constraints`, UX spec |
| **3. Implementation** | Senior Engineer(s), Junior Engineer(s) | Task breakdown, branch-per-task scaffolding, feature diffs, PRs, code review, conflict resolutions |
| **4. Quality & Verification** | QA Engineer (cross-family on high-risk), SDET, Reviewer/Critic (+ CI runner) | Test plan, criterion-*fidelity* tests, e2e suite, build/test results, review verdicts |
| **5. Security & Compliance** | Security/AppSec Engineer, Audit/Compliance, Supply-Chain Reviewer | Threat model, SAST (incl. build/IaC) findings, SBOM, license & control mapping, supply-chain report |
| **6. Operations** | DevOps/SRE, Release Manager *(+ non-agent: trusted builder, admission controller, deployer)* | CI/CD pipeline, IaC *proposal*, observability config, deploy/rollback, SLSA-attested signed release (gate-state bound) |
| **7. Governance / Meta** | Orchestrator/Supervisor, Guardian (deterministic monitors + event-triggered analyzer), Budget Controller, Human Liaison | Routing, policy enforcement, cost ledger, convergence bound, escalations |

> **Non-agent components** (trusted builder, **trusted gate runner** that computes
> & signs deterministic-gate verdicts, deploy-time admission controller,
> least-privileged deployer, WORM audit appender) are deliberately *outside* agent
> control — agents propose inputs and triage findings but never are these
> components, and never bless their own verdicts.

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
- **Durable checkpoints + transactional outbox** — one Postgres transaction
  commits the checkpoint, budget/action ledgers, **and** an outbox per superstep,
  so internal state, governance records, and audit never diverge; a relay drains
  the outbox to git/PR/deploy and the WORM store. Idempotency keys are **stored
  by the external system**, and resume **re-queries by key** (fail-to-human if
  unknown) — so replay never double-commits, double-tags, or re-deploys
  ([docs/architecture.md §5](./docs/architecture.md)).
- **Bounded checkpoints** — `history` and large artifact bodies live out-of-band
  by content-addressed handle, so checkpoint size doesn't grow with trajectory
  length (no quadratic write amplification on long-horizon runs).
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
| Models | Claude, **pinned IDs, tiered by role** — T1 (Opus-class) architecture/review/security, T2 (Sonnet-class) implementation, T3 (Haiku-class) routing — **plus a second provider family** for high-risk judging and cross-family failover | Cost/quality match; **T1 never auto-downgrades**; Claude tiers aren't different *families*, so judge decorrelation needs a genuinely different lineage |
| Model resilience | Same-tier retry/backoff; **cross-family failover** of T1 on a primary-provider outage (per-model calibration) | No silent cross-tier-down on high-stakes nodes; real redundancy needs a second family |
| Gate verdicts | **Trusted gate runner (non-agent)** computes & signs deterministic-gate verdicts | Agents triage; they cannot forge a PASS into the predicate ([docs/governance.md §1c](./docs/governance.md)) |
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

1. **Spec** — acceptance criteria exist, are testable, carry `criterion_id`s, and pass an **independent completeness critique** (security/abuse criteria required; omission fails) (bugfixes: a repro + expected-behavior brief).
2. **Design** — ADRs approved; interfaces, data model, ownership map, and `pinned_constraints` defined (HITL for significant designs).
3. **Build** *(per-branch + integrated)* — compiles, lints, type-checks (CI runner).
4. **Test** *(per-branch + integrated)* — green; **criterion *fidelity*** (each high-risk criterion's test fails against a known-bad reference), not just coverage; threshold met; flakes quarantined (QA, authorship separated + cross-family on high-risk).
5. **Security** — SAST (incl. build scripts + IaC) + secret + dependency audit clean; threat-model items addressed.
6. **Audit** — SBOM clean (no critical CVEs / disallowed licenses); mapped to controls.
7. **Supply-chain** — agent-authored build/CI/IaC/lockfile/scanner-config changes conform to policy (no build-time network/hooks, no privilege/egress widening, no signing/OIDC-trust changes without HITL).
8. **E2E** — end-to-end scenarios green on the integrated tree (SDET; flake policy applies).
9. **Release** — evidence-backed human sign-off (two-person on trust-root changes) on an artifact already attested with **gate-state bound into the SLSA predicate**.

Plus a non-graph **deploy-time admission** control: a non-agent controller
re-verifies the signature *and* that all gates PASS for the exact digest before a
non-agent deployer ships. Integrated gates re-run on every `tree_sha` change.

## 8. Guardrails & governance (five planes)

The enterprise-standard five-plane model, applied to a coding factory
([docs/governance.md §2](./docs/governance.md)): **policy** (OPA on tool calls
*and on IaC `plan` outputs*; scanner config run-immutable and repo-external;
signing reachable only by the trusted builder), **execution** (sandboxes/microVMs;
credentialed phase separated from untrusted-code execution; egress through
anomaly-detecting proxies and an internal hash-pinned dependency mirror, since
allowlisting alone doesn't stop exfil), **data** (scoped short-lived creds, keys
air-gapped, **redaction before provider egress**, zero-retention/no-train terms,
PHI/PCI never leaves the boundary), **observability** (OpenTelemetry traces +
serving-model fingerprints feeding monitors and evals), and **assurance** (SBOM,
**strongly-hermetic** build with no egress, SLSA L3 with gate-state in the
predicate, deploy-time admission, red-teaming incl. build-hook injection and
exfil-over-allowlist, evals in CI).

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

- **Long-horizon reliability** — unsolved industry-wide (~21% on SWE-EVO). Mitigation: durable checkpoints, bounded retries, decomposition, HITL — *and the pre-registered, fairly-baselined A/B that tells us whether structure actually helps* ([docs/evaluation.md §2](./docs/evaluation.md)).
- **Context loss across hand-offs (the real long-horizon killer)** — Mitigation: a never-summarized `pinned_constraints` channel, retrieval over the call/def graph (not similarity alone) with a measured recall floor, a `need-context` escape, and a handoff-fidelity eval.
- **Reward hacking / gate-gaming** — Mitigation: separated + cross-family test authorship, **criterion *fidelity* (negative oracles)**, **run-immutable repo-external scanner config**, deterministic oracle/config-edit monitors, mutation testing, semantic loop signature.
- **Correlated LLM judges / self-preference** — Mitigation: measured judge↔implementer error correlation + self-preference probes; different model *family* on high-risk review/QA; deterministic oracles where possible.
- **Concurrency correctness** — Mitigation: commutative, `None`-safe reducers with a **total `seq` order** `(superstep_index, node_id, local)` so gate verdicts resolve by causality, never fold order; precise `tree_sha`; `Send` fan-out with per-branch `error` status + deadlines; superstep-boundary budget enforcement.
- **Forged gate verdicts / agent-authored trust inputs** — Mitigation: deterministic-gate **verdicts computed & signed by a trusted runner** (agents triage only) and verified at admission; dependency-mirror **admission vetting** (namespace reservation vs dependency-confusion); external scanner-config repo under a **separate trust boundary** with its revision hash in the predicate.
- **Context channel overflow** — Mitigation: `pinned_constraints` is relevance-scoped + supersedable + cardinality-monitored (escalate, never silently truncate), not an unbounded union.
- **Single-provider fragility / correlated judges** — Mitigation: a **second provider family** for high-risk judging and real failover.
- **A/B feasibility** — Mitigation: the decisive A/B is **costed at experiment scale** (3 arms × n × k × SWE-EVO cost) and honestly de-scoped if the powered run is unaffordable, never silently under-powered.
- **Dual-write / idempotency on resume** — Mitigation: transactional outbox (checkpoint+ledger+audit in one txn); idempotency keys **stored by the external system**; re-query-by-key on resume, fail-to-human if unknown.
- **Supply-chain (agent-authored build) & deploy** — Mitigation: build config as a high-privilege artifact behind the Supply-chain gate; strongly-hermetic no-egress builder; gate-state bound in the SLSA predicate; deploy-time admission + non-agent deployer; IaC policy-as-code.
- **HITL rubber-stamp** — Mitigation: independent, non-agent evidence pack; two-person sign-off on trust-root changes.
- **Non-convergent cross-gate cascade** — Mitigation: global `fix_attempts_since_all_green` bound escalating as *non-convergence*, not a late budget breach.
- **Cost/latency realism** — Mitigation: a concrete per-feature cost+wall-clock model; event-triggered analyzer; honest monitor-overhead target.
- **LLM nondeterminism vs audit** — Mitigation: temp-0 for gates/judges, recorded seeds/model-fingerprints, pinned-ID-drift alerts; audit framed as immutable *record*, not bitwise replay.
- **Audit-store & data-boundary integrity** — Mitigation: WORM written by a non-agent appender with external anchoring; redaction before provider egress; PHI/PCI never leaves the boundary.
- **Prompt injection (incl. of the monitor)** — Mitigation: load-bearing detections are deterministic; the analyzer is toolless, event-triggered, fed structured features with untrusted prose as data, and evaluated against injection.

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
