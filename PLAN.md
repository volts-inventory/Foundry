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
3. **The right verdict class at each seam.** LLMs do judgment *inside* nodes; the
   verdict *between* nodes is grounded by the appropriate **verdict class**, not
   one universal rule. A deterministic oracle is the right class for backend/logic
   code (and we handle its execution flakiness explicitly). But **correctness is
   not one shape**: ML correctness is a *thresholded random variable* on versioned
   data (statistical class), regulated correctness is *traceable evidence +
   human-witnessed validation* (human-witnessed class), and UX correctness is
   *perceptual/usability judgment* (perceptual class, mandatory HITL). The core
   carries a verdict-class taxonomy and a domain-pack contract that reaches it
   ([docs/domains.md](./docs/domains.md)) — so the factory is genuinely general,
   not silently backend-only.

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

> **This 16-role org is a ceiling to grow into, not a starting assumption.** The
> cited evidence (MetaGPT/ChatDev) validates ~5 roles; whether more roles beat
> fewer is an open question. So Foundry **starts** with the PM→Architect→Senior→QA
> core and **earns each additional role by ablation** — a role is added only when
> it measurably improves the scorecard ([roadmap.md Phase 1.5](./docs/roadmap.md),
> [evaluation.md §9](./docs/evaluation.md)). Role multiplicity is coordination,
> context-handoff, and cost overhead; it must pay for itself.

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
| Code execution | Sandboxed runners (containers; Firecracker/gVisor) | Never run agent-written code on the host. The substrate must be **sandbox-*enforced*** to writes-worktree-only + mediated provider egress (not merely contractual), and **externally metered/hard-killed** — its long inner loop runs inside one superstep, so superstep-boundary budget can't bound it ([docs/architecture.md §2/§6](./docs/architecture.md), [docs/governance.md §2](./docs/governance.md)) |
| Coding-agent substrate | **Pluggable worker contract** (the ACI); adapters: Claude Code (`claude -p`), OpenAI Codex CLI (`codex exec`), OpenHands/SWE-agent, or a raw multi-provider loop | The Implementation/QA inner loop is an *adapter, not a core dependency* — keeps the factory **provider-agnostic**. Adapters are harnesses that can drive any model, so the `out` contract records `served_family`/`served_model_id` — **trusted-grounded** (stamped by the mediating model-provider proxy that observes the actual served model, **not** self-declared by the adapter); **decorrelation binds served model lineage, not adapter brand** — `served_family` is a curated **lineage-equivalence class** (keyed on base-model lineage, not provider brand or model-id string), so the deterministic, admission-/CI-checked `reviewer.served_family != implementer.served_family` on high-risk criteria compares lineages, not CLI brands or opaque strings. Every adapter runs in-sandbox, emits diff + test **sources** + `GateTriage` (**never a signed deterministic-gate verdict** — the trusted runner signs; LLM-judgment seams may still emit a judgment verdict bound only by decorrelation + HITL), writes worktree-only (side-effects via the outbox) ([docs/architecture.md §2/§8](./docs/architecture.md), [docs/governance.md §1c](./docs/governance.md)) |
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
5. **Security** — SAST (incl. build scripts + IaC) + secret + dependency audit clean; each threat-model item's **bound deterministic oracle** passes (irreducible residual → a separate Threat-review judgment sub-gate, not the signed verdict).
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

## 8a. Earned autonomy & change-risk tiering

Adoption does not begin at "AI autonomously deploys to prod," and not every change
warrants the full assurance tax:

- **Deployment-trust modes** ([docs/lifecycle.md §4](./docs/lifecycle.md)):
  **Assistant** (Foundry opens a PR a human merges — ordinary CI, *no* trust-root
  machinery), **Gated** (full gate set, human approves), **Autonomous** (hands-off
  build/attest/deploy — requires the full two-zone runner, SLSA-L3, admission,
  deployer). Most adopters start in Assistant; the expensive trust root is **built
  only for Autonomous** and therefore **deferred** in the roadmap.
- **Change-risk tiering** ([docs/governance.md §1e](./docs/governance.md)): Triage
  emits a validated risk tier so a one-line fix doesn't pay a major-feature's
  9-gate + human-Release tax. Low-risk (no security/trust-root path) → reduced
  gates + auto-approve (admission still fail-closed); High-risk (build/IaC/deps/
  auth/trust-root) → full chain + two-person sign-off. Tiering scales *assurance
  depth and human load*, never the integrity guarantees.
- **Dependency-scoped re-verification** ([docs/governance.md §1f](./docs/governance.md)):
  a change re-runs only the gates it can affect and reuses cached runner-attested
  verdicts for unaffected inputs — bounding the re-verification cost multiplier.

## 9. Pluggable domain specialists

A **Specialist** is a conditional graph node implementing `{capabilities, tools,
prompts/SOP, gates, evals}`. A **registry** maps required capabilities (declared
by PM/Architect) to specialists; the supervisor routes via a conditional edge,
and downstream consumers tolerate absent optional artifacts
([docs/architecture.md §7](./docs/architecture.md)).

But a domain pack is **more than "a specialist + Audit checks"**: some software
*breaks core assumptions* a plugin can't reach (an LLM can't change what a verdict
*means*). So the core exposes a **pack contract**
([docs/domains.md](./docs/domains.md)) — verdict-class assignment (statistical /
human-witnessed / perceptual), mode/tier veto (force a human accountable approver;
make autonomous deploy unreachable), a regulatory-significance tiering axis,
per-artifact human approver-of-record, a `state_sha` that includes data/weights, a
compute-cost budget for expensive gates, a no-third-party-egress data profile, and
mandatory perceptual HITL with rendered evidence. Domain packs (regulated fintech/
healthcare, ML/data, frontend/UX) are specified against these hooks, keeping the
core genuinely general while it becomes expert — and *honest* about each domain's
real correctness signal.

**Wedge / who adopts, and why.** First/early adopter: a **non-regulated
engineering team** that values *criterion-linked structured PRs* + review/test
discipline. What Foundry's *Assistant-mode* output does that a single strong
agent + standard CI (or Chromatic/Percy/axe-CI/Storybook + ordinary CI) does
**not**: it ships a PR with *structured, criterion-linked* artifacts — PRD →
criteria → fidelity tests → threat-model items → review mapping — wired to a
verdict-class taxonomy, so a reviewer sees *why* each gate passed, not just a
green check. Honestly: in Assistant mode the differentiated value is thin — most
of it is convenience over assembling point tools — so value lands in **Assistant
(Phases 0–1)** and deepens at **early Gated (Phase 2)** via distinct gates,
risk-tiering, and recorded Design/Release sign-off. A **regulated or audit-bound
team** (fintech/healthcare) is an explicitly **later** deepening adopter: its
relevant value (decorrelated judging, two-zone signing, gate-state-bound
provenance, deploy-time admission, attested control-mapping) concentrates in
**Gated/Autonomous** modes and only gets real teeth at the **Phase-4** trust
root — regulated work is Gated+ by construction, served later, never dressed up
as an early wedge. This is exactly why the trust root is deferred and sequenced
behind the Phase-1.5 decision gate, consistent with §12 and
[docs/roadmap.md](./docs/roadmap.md) (§8a, §11).

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

Sequenced to **falsify the core bet early** and **earn autonomy incrementally**
([docs/roadmap.md](./docs/roadmap.md)):

- **Phase 0 — Foundations:** reducer-annotated `FactoryState`, transactional outbox + ledger, sandbox, tracing, run-status, policy-plane stub.
- **Phase 1 — Core vertical slice (Assistant mode):** PM → Architect → Senior → QA produces a useful, human-merged PR behind Spec/Build/Test gates. *(smallest version that delivers value)*
- **Phase 1.5 — DECISION GATE:** a powered, pre-registered A/B + role ablation **with a real kill/redesign branch**, *before* the expensive machinery. If structure doesn't beat a strong single agent, redesign at the cost of Phases 0–1.
- **Phase 2 — Review & assurance (Gated mode):** Reviewer, Security, Audit; evaluator-optimizer loops; **risk-tiering**; dependency-scoped re-verification; HITL.
- **Phase 3 — Operability & scale:** operator dashboard/SLOs/runbooks, run-status liveness, store HA + recovery, multi-tenancy/queueing, HITL-load model ([docs/operations.md](./docs/operations.md)).
- **Phase 4 — Trust root & Autonomous mode:** two-zone runner, SLSA-L3, admission, deployer — *built only because Phases 1.5–3 proved value and demand.*
- **Phase 5 — Specialist plugin system:** registry, capability routing, first two domain packs.
- **Phase 6 — Eval, optimization & lifecycle:** full harness + confirmatory A/B; workflow optimization; product-feedback loop + generated-code maintenance ([docs/lifecycle.md](./docs/lifecycle.md)).

Rough total: ~8–11 team-quarters to Autonomous + specialists; the smallest-useful
milestone (Assistant mode) is ~2–3 quarters.

## 12. Known limitations & risks (eyes open)

- **Long-horizon reliability** — unsolved industry-wide (~21% on SWE-EVO). Mitigation: durable checkpoints, bounded retries, decomposition, HITL — *and the pre-registered, fairly-baselined A/B that tells us whether structure actually helps* ([docs/evaluation.md §2](./docs/evaluation.md)).
- **Context loss across hand-offs (the real long-horizon killer)** — Mitigation: a never-summarized `pinned_constraints` channel, retrieval over the call/def graph (not similarity alone) with a measured recall floor, a `need-context` escape, and a handoff-fidelity eval.
- **Reward hacking / gate-gaming** — Mitigation: separated + cross-family test authorship, **criterion *fidelity* (negative oracles)**, **run-immutable repo-external scanner config**, deterministic oracle/config-edit monitors, mutation testing, semantic loop signature.
- **Correlated LLM judges / self-preference** — Mitigation: measured judge↔implementer error correlation + self-preference probes; different model *family* on high-risk review/QA; deterministic oracles where possible.
- **Judge decorrelation silently evaporates** — adapter-brand confusion (a "different" harness driving the *same* served family) or an outage failover that quietly re-points the judge to the implementer's family. Mitigation: decorrelation binds *served lineage* via a **trusted-grounded** `served_family` — proxy-observed, a lineage-equivalence class, **not** a self-declared field — and a deterministic `reviewer.served_family != implementer.served_family` check spanning **all judge↔subject seams** (reviewer/test-author/criteria-critic), enforced at admission/CI; decorrelation now binds the **set** of served lineages (set-disjointness, not a single field) with the lineage-map revision pinned into the attestation + judgment binding, and the judgment binding includes the verdict `status` (no same-tree approve-flip) ([docs/architecture.md §2/§3/§8](./docs/architecture.md)).
- **Per-task Review bound by decorrelation alone** — there is *no* HITL on the per-task reviewer↔implementer seam, so the trusted-grounded served-family is load-bearing for that gate. Mitigation: decorrelation is the sole grounding and must be proxy-stamped, not self-declared ([docs/architecture.md §2/§9](./docs/architecture.md)).
- **Adapter-substrate trust boundary** — Mitigation: substrate is **sandbox-*enforced*** to writes-worktree-only + mediated provider egress (not contractual), **externally metered/hard-killed** (inner loop unbounded by superstep budget), and decorrelation binds served lineage, not adapter brand ([docs/architecture.md §2/§6](./docs/architecture.md), [docs/governance.md §2](./docs/governance.md)).
- **Concurrency correctness** — Mitigation: commutative, `None`-safe reducers with a **total `seq` order** `(run_id, superstep_index, node_id, local)` — total within a run, globally unique across forks (cross-fork resolution is fail-closed, not seq-ordered) — so gate verdicts resolve by causality, never fold order; precise `tree_sha`; `Send` fan-out with per-branch `error` status + deadlines; superstep-boundary budget enforcement.
- **Forged gate verdicts / agent-forged results stream** — Mitigation: deterministic-gate verdicts computed & signed by a **two-zone trusted runner** (zone A runs agent code with no signing creds; zone B signs, runs no agent code). Because a malicious test/scanner could *forge the results stream*, the verdict is grounded by class: **config-evaluable gates (SBOM/CVE/license/IaC) are re-derived by zone B** from the tree; **execution-dependent gates (Test/E2E/SAST) are observed by pinned trusted infra** over a sealed channel (agents supply test *sources*, not the harness), with zone B independently verifying pass-on-real/fail-on-known-bad. Input-bound signatures; agents write only `GateTriage`; admission verifies per-record and is **fail-closed on stable FAILs** (with a bounded HITL recovery for flakes); the **Security "threat-model addressed" residual** that can't be reduced to an oracle is a judgment sub-gate, not in the signed verdict.
- **Context channel overflow** — Mitigation: `pinned_constraints` is relevance-scoped + supersedable + cardinality-monitored (escalate, never silently truncate), not an unbounded union.
- **Single-provider fragility / correlated judges** — Mitigation: a **second provider family** for high-risk judging and real failover.
- **A/B feasibility** — Mitigation: the decisive A/B is **costed at experiment scale** (3 arms × n × k × SWE-EVO cost) and honestly de-scoped if the powered run is unaffordable, never silently under-powered.
- **Dual-write / idempotency on resume** — Mitigation: transactional outbox (checkpoint+ledger+audit in one txn); idempotency keys **stored by the external system**; re-query-by-key on resume, fail-to-human if unknown.
- **Supply-chain (agent-authored build) & deploy** — Mitigation: build config as a high-privilege artifact behind the Supply-chain gate; strongly-hermetic no-egress builder; gate-state bound in the SLSA predicate; deploy-time admission + non-agent deployer; IaC policy-as-code.
- **HITL rubber-stamp** — Mitigation: independent, non-agent evidence pack; two-person sign-off on trust-root changes.
- **Non-convergent cross-gate cascade** — Mitigation: global `fix_attempts_since_all_green` bound escalating as *non-convergence*, not a late budget breach.
- **Cost/latency realism** — Mitigation: a concrete per-feature cost+wall-clock model; event-triggered analyzer; honest monitor-overhead target.
- **LLM nondeterminism vs audit** — Mitigation: temp-0 for gates/judges, recorded seeds/model-fingerprints, pinned-ID-drift alerts; audit framed as immutable *record*, not bitwise replay.
- **Audit-store & data-boundary integrity** — Mitigation: WORM written by a non-agent appender with external anchoring; redaction before provider egress; PHI/PCI never leaves the boundary. The provider-egress proxy **bounds but does not eliminate** the high-entropy exfil channel, and the provider is now a **trusted component / SPOF** ([docs/governance.md §2](./docs/governance.md), [docs/architecture.md §2](./docs/architecture.md)).
- **Prompt injection (incl. of the monitor)** — Mitigation: load-bearing detections are deterministic; the analyzer is toolless, event-triggered, fed structured features with untrusted prose as data, and evaluated against injection.
- **Over-engineering / mis-sequencing** — the iterative hardening *added* complexity; risk that the artifact is too complex to build or is built in the wrong order. Mitigation: the falsifying A/B + role ablation move to a **Phase-1.5 decision gate** with a kill branch; the trust root is **deferred** to Autonomous mode; a **smallest-useful milestone** (Assistant mode) ships first; effort is estimated per phase.
- **Operability / Day-2** — Mitigation: a dedicated [operations.md](./docs/operations.md): run-status liveness (paused vs wedged), store HA + poisoned-checkpoint recovery, trusted-component availability/blast-radius, upgrade/migration with runs in flight, multi-tenancy/capacity/queueing, trusted-component lifecycle.
- **Cost/latency at scale & HITL volume** — Mitigation: **change-risk tiering** + **dependency-scoped re-verification**; cost reported converged *and* worst-case *and* weighted over the change-size distribution; **human-decisions-per-feature** is a scorecard threshold, not an unbounded externality. The dominant cost drivers — trusted-runner mutant/perturbation re-execution compute, adapter inner-loop turns, and second-family judging — are now explicitly modeled ([docs/evaluation.md §8](./docs/evaluation.md), [docs/operations.md §6](./docs/operations.md)).
- **Product lifecycle** — Mitigation: [lifecycle.md](./docs/lifecycle.md) covers maintenance of generated code, human **steering/taste** (not just gatekeeping), a **product-feedback loop**, deployment-trust modes, and **AI-code licensing/liability**.
- **Domain mismatch (correctness is not one shape)** — the deterministic-oracle verdict model fits backend/logic but breaks for regulated (evidence + human-witnessed validation), ML/data (statistical metrics + versioned data), and UX (perceptual judgment) software. Mitigation: a core **verdict-class taxonomy** + an **expanded domain-pack contract** ([docs/domains.md](./docs/domains.md)) — statistical/human-witnessed/perceptual verdict classes, `state_sha` for data/weights, compute-cost budgeting, mode/tier veto, and mandatory perceptual HITL — with the honest caveat that *pre-release UX correctness is bounded* (real users aren't in the pre-release loop) and *autonomous deploy is often unavailable* in regulated domains. Further caveats: the **statistical (class-3) verdict is scoped to batch/offline supervised eval** — online/RL/generative correctness need separate handling, and the class-3 **alpha look-ledger is global/cross-fork** so forking can't re-draw the budget — **keyed on eval-set identity (`(eval_set_identity, decision_id)`), not `state_sha`**, so adaptive retrain/feature iteration against the same eval set debits ONE shared budget and can't re-draw it either ([docs/domains.md §1/§5](./docs/domains.md), [docs/governance.md §5](./docs/governance.md)); and **regulated controls have no teeth in Assistant mode** — without the trust root, evidence is advisory and a human can merge past it; teeth arrive only in Gated/Autonomous, so the **regulated wedge adopter is served only by the deferred Gated+ modes** and near-term value is **backend-first** ([docs/roadmap.md](./docs/roadmap.md), §9, [docs/lifecycle.md §4](./docs/lifecycle.md), [docs/domains.md §5](./docs/domains.md)).

## 13. How this plan was hardened

This plan was red-teamed in a **critic → solutioner → full-rewrite loop across
twenty-three rounds at four bars** (plus a Round-24 fix pass): a generalist
*design-doc* bar (Rounds 1–6), an *implementation-readiness* bar (three-lens
technical panel — distributed-systems, offensive-security, AI-agents — Rounds
7–12, converged), a *fitness-for-purpose* bar (SRE/operability, cost/scale,
premise-skeptic — Rounds 13–14, converged), and a *type-of-software* bar
(regulated, ML/data, frontend/UX domain experts — Round 15). **Round 16** ran a
fresh full-domain expert panel raising **62 findings (10 BLOCKER, 33 MAJOR, 19
MINOR)**; **Round 17** ran another fresh full-domain validation panel at the
interaction seams of the Round-16 fixes, raising **57 findings (9 BLOCKER, 32
MAJOR, 16 MINOR)**, which **Round 18** resolved. **Round 19** ran a fresh full-domain validation
panel returning **29 findings (0 BLOCKER, 25 MAJOR, 4 MINOR)** — the BLOCKER
count fell to **zero** and the finding count roughly halved (62→57→29), with the
remainder being localized refinements at the new mechanisms' seams, which
**Round 20** resolved. **Round 21** ran a fresh full-domain validation panel
returning **10 findings (2 BLOCKER, 7 MAJOR, 1 MINOR)** — and honestly, these
were almost entirely **consistency/reconciliation** bugs introduced by the
multi-fixer method across rounds (the look-ledger key, the §9-vs-§12 adopter
drift, "second family" vs ≥3 lineages, an unwired verdict-nonce), **not** new
design flaws; **Round 22** resolved them with verbatim cross-doc definitions to
stop the drift. **Round 23** ran a fresh full-domain validation panel returning
**9 findings (1 BLOCKER, 7 MAJOR, 1 MINOR)** — with the regulated panel returning
the loop's **first NO-MATERIAL-FINDINGS** sweep on the live codebase. Honestly,
these were again dominated by **fix-hygiene of Round 22's own edits**, not new
architecture: the look-ledger key change was never propagated to architecture.md (a
dispatch omission), `decision_id` was left undefined, and the Round-22 verdict-nonce/latch
was unsoundly placed inside the pure `_merge_gates` reducer and left per-run when it
must be global/cross-fork like the stable-FAIL latch. **Round 24** resolved all 9 with
verbatim cross-doc definitions. **302 findings have now been raised and resolved** (293
prior + 9 this round).

The defining lesson is the progression: **each genuinely new axis of scrutiny
re-opened a problem the prior axes had signed off on** — and each new round
re-opens prior sign-offs at the *seams its own fixes created*. The trajectory is
now **converging** (findings 62→57→29→10→9; BLOCKERs 10→9→0→2→1): no new
*architectural* defect has surfaced since Round 19, one domain now returns clean,
and the residual is the multi-agent editing process **policing its own
consistency/completeness**, not whole-system design risk — an absolute zero-finding
sweep across all eight lenses is a receding horizon dominated by edit-hygiene rather
than design. **Convergence is
not proof of correctness** — a genuinely new axis of scrutiny could still re-open
the design. The fitness-for-purpose
lenses surfaced 17 findings about sequencing/operability/cost the technical panel
couldn't see; the domain experts then found that the plan's own thesis — "a
deterministic decision rule at the seams" — was *domain-specific* (true for
backend/logic, false as a universal). "Converged" means an independent panel found
no further defects *at the bar it was reviewing*; it does **not** mean the system
is proven correct, complete, viable, or the right thing to build. The genuine open
questions — does structure beat a strong single agent (Phase-1.5 A/B), does it
pencil out at scale, will it be adopted, do the domain packs work in practice —
are settled by evidence and real use, not by this loop. The full per-finding record, the
conformance mapping, and a maturity assessment are in
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
