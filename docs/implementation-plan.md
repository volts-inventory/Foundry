# Implementation Plan — Building Foundry

This is the **build-ready implementation plan** for the system designed across
[PLAN.md](../PLAN.md) and [docs/](.). It turns the architecture/design docs into a
concrete, sequenced engineering program: a pinned stack, a repository layout, the
typed schemas that are the backbone of the build, per-component interfaces, the
phase-by-phase delivery spine with a task backlog, and a topological build order.

It is a **plan, not code** — nothing here is built yet. Every component traces back
to a section of the design docs (cross-referenced inline), so this plan stays
faithful to the converged design and does not introduce new architecture.

> **Scope of this plan.** It targets the *whole* system in the docs (Phases 0–6,
> through Autonomous mode + domain packs). The PLAN's own estimate is **~8–11
> team-quarters** for the full program and **~2–3 quarters** for the smallest-useful
> Assistant-mode milestone (Phases 0–1). This plan is sequenced so value ships early
> and the expensive trust root is deferred until demanded.

---

## 0. Stack, conventions, and honesty about the environment

### 0.1 Pinned stack

| Concern | Choice | Notes |
|---|---|---|
| Language | **Python 3.12** | type hints everywhere; `from __future__ import annotations` |
| Orchestration | **LangGraph** `StateGraph` | nodes, conditional edges, durable checkpointers, interrupts ([architecture.md §1](./architecture.md), [§9](./architecture.md)) |
| Model provider | **Anthropic** (single lineage) | Claude Opus/Sonnet/Haiku tiers; the **solo-lineage self-judge profile** of [governance §1e](./governance.md) |
| Worker substrate | pluggable adapter; default **Claude Code (`claude -p`)** | adapter contract is provider-agnostic ([architecture.md §2b](./architecture.md)) |
| Typed artifacts | **pydantic v2** | every agent I/O is a validated, versioned model |
| Durable state | **Postgres** | LangGraph checkpoints + budget ledger + outbox + WORM audit, one txn/superstep ([architecture.md §5](./architecture.md)) |
| Object/artifact store | **content-addressed store (CAS)** | large artifact bodies + worktree snapshots out-of-band |
| Policy engine | **OPA / Conftest / Checkov** | five-plane policy + IaC plan gating ([governance.md §2](./governance.md)) |
| Scanners | **Semgrep (SAST), Syft/Grype (SBOM/CVE), license scanner** | rules in a run-immutable external config repo |
| Observability | **OpenTelemetry + Langfuse** | per-node temp/seed/model-fingerprint, drift alerts |
| Tests | **pytest** + property tests (`hypothesis`) | reducer-purity + idempotency suites are load-bearing |
| Supply chain | **Sigstore/SLSA** (Phase 4) | keyless OIDC signing; see §0.3 caveat |

### 0.2 Single-lineage self-judge configuration (the chosen operating profile)

This build is configured for **one provider (Anthropic), zero HITL, fully
autonomous** per the profile in [governance §1e](./governance.md). Concretely, that
threads through the implementation as:

- The **model-provider proxy** (§3.3) resolves a single Anthropic lineage; tier mix
  (Opus/Sonnet/Haiku) is a *cost* choice, not a decorrelation choice.
- On the **oracle-irreducible judgment seams** (subjective Review residual,
  threat-model residual, perceptual class-5), a same-lineage judge issues the verdict
  and the proxy stamps `judgment_provenance = "self"` into the judgment binding and
  WORM record. The scorecard MUST NOT claim independent validation for these.
- **All machine-grounded gates remain fail-closed and unchanged** — the two-zone
  trusted runner, deterministic gate re-derivation, and admission still apply. The
  self-judge profile relaxes *judgment*, never *machine verification*.
- The **regulated domain pack force-disables this profile** ([domains.md §5](./domains.md),
  [governance §1e](./governance.md)): where law requires a named approver, HITL is
  re-enabled, fail-closed.

> The decorrelation machinery (`served_family` as a *set*, set-disjointness checks,
> second-family judging) is still **built** — it is how the default profile and the
> regulated fallback work — but in the chosen profile the set has one element and the
> disjointness check is replaced by the recorded self-judge stamp. Building it keeps
> the system honest and lets an operator flip back to the decorrelated/HITL default.

### 0.3 What is faithfully buildable here vs. honest scaffold

| Layer | In this plan | Reality |
|---|---|---|
| Graph orchestration, agents, typed state, reducers, gates (Spec/Build/Test/E2E/SAST/SBOM/CVE/IaC), monitors, context assembly, durability/outbox, eval harness, ops models | **Fully buildable & runnable** | real code, real tests |
| Two-zone trusted runner, **SLSA-L3 hermetic build, keyless hardware-rooted signing**, deploy-time admission, least-privilege deployer | **Buildable as faithful software**, but the *hardware/CI root of trust* (air-gapped keys, real OIDC federation, isolated build infra) **cannot be made genuinely production-grade in an ephemeral container** | implemented as correct logic + interfaces with a clearly-labelled local/dev trust root; production hardening is an ops deployment task |
| Real multi-tenant fleet infra, true HA Postgres/CAS, real provider burst capacity | **Modeled + interfaces built** | exercised against local/dev backends, not a production fleet |

This split is stated up front so "implemented" never overclaims. The trust-root code
is *real and correct*; its *roots* (keys, isolated builders) are deployment concerns.

### 0.4 Conventions

- **Artifacts are content-addressed and versioned, never mutated** ([architecture.md §10](./architecture.md)): supersession, not in-place edit.
- **Agents write `GateTriage` (advisory), never `GateResult` (verdict)** ([governance §1c](./governance.md)). Verdicts are computed/signed by trusted infra.
- **Determinism policy** ([architecture.md §8](./architecture.md)): temperature 0 for gates/routing/judges; higher only for ideation. Every node records model-id + fingerprint + seed.
- **Fail-closed everywhere** for admission and machine gates; **fail-to-human** only where a human is in the loop (disabled in the chosen profile except regulated).

---

## 1. Repository layout

A single harmonized tree (reconciling the per-doc module proposals):

```
foundry/
├── state/                     # the backbone — typed state + merge semantics
│   ├── factory_state.py       # FactoryState TypedDict + channel annotations   [arch §3]
│   ├── reducers.py            # _merge_by_uri, _merge_constraints, _merge_gates, _union_by_key
│   ├── seq.py                 # Seq total order (emission_epoch ahead of superstep)
│   ├── tree_sha.py            # tree_sha / state_sha + execution-dependent cache key  [arch §4]
│   └── global_records.py      # cross-fork latches, nonce ledger, alpha ledger   [arch §3/§5, gov §5]
├── schemas/                   # all pydantic artifact models (the I/O contracts)
│   ├── product.py             # IntentBrief, PRD, AcceptanceCriterion, …         [agents.md]
│   ├── architecture.py        # SystemDesign, ADR, InterfaceContract, PinnedConstraint
│   ├── implementation.py      # Task, FeatureDiff, ReviewVerdict, ConflictResolution
│   ├── quality.py             # TestPlan, OracleBinding, TestReport, E2EReport
│   ├── security.py            # ThreatModel, SecurityTriage, SupplyChainReport, SBOM
│   ├── operations.py          # Pipeline, IaCProposal, DeployPlan, Release
│   ├── gates.py               # GateResult, GateTriage, VerdictAttestation, JudgmentBinding
│   └── governance.py          # TriageDecision, GuardianDecision, BudgetDecision, HumanDecision
├── durability/
│   ├── outbox.py              # OutboxTransaction (one txn/superstep), OutboxRelay  [arch §5]
│   └── checkpointer.py        # Postgres LangGraph checkpointer + turn-snapshot handles
├── proxy/
│   └── model_provider_proxy.py# single egress, lineage observation, judgment binding  [arch §2b]
├── execution/
│   ├── sandbox.py             # worktree-only writes, netns, cgroup hard-kill        [arch §2, PLAN §6]
│   ├── adapter_contract.py    # pluggable substrate contract (in/out)
│   └── adapters/              # claude_code.py, openhands.py, codex.py, raw_react.py
├── context/
│   ├── pinned_constraints.py  # global-always + local-scoped + supersession + cardinality  [arch §8]
│   ├── retriever.py           # symbol-index + call/def graph; measured recall floor
│   └── determinism.py         # temp/seed policy + run-manifest record
├── agents/                    # LLM role nodes (SOP + I/O contract)                 [agents.md]
│   ├── product/               # stakeholder_proxy, product_manager, criteria_critic, eng_program_mgr
│   ├── architecture/          # principal_architect, domain_specialist, ux_design
│   ├── implementation/        # senior_engineer, junior_engineer
│   ├── quality/               # reviewer_critic, qa_engineer, sdet
│   ├── security/              # appsec_engineer, audit_compliance, supply_chain_reviewer
│   ├── operations/            # devops_sre, release_manager
│   └── governance/            # orchestrator (triage), guardian_analyzer
├── gates/                     # deterministic verdict computation (trusted infra)   [gov §1c]
│   ├── runner.py              # two-zone gate runner (zone A exec / zone B sign)
│   ├── spec.py build.py test.py e2e.py
│   ├── security.py audit.py supply_chain.py
│   └── attestation.py         # verify_runner_attestation, verify_judgment_binding
├── monitors/                  # deterministic monitors + advisory analyzer          [gov §3]
│   ├── loop_detector.py cost_monitor.py oracle_monitor.py policy_monitor.py
│   └── guardian.py            # orchestrates monitors; halt/pause/escalate
├── graph/                     # StateGraph assembly
│   ├── subgraphs/             # product, architecture, implementation, integration, assurance, release
│   ├── supervisor.py          # phase routing (orchestrator-workers)
│   └── factory_graph.py       # build_factory_graph() -> Compiled[FactoryState]      [arch §1]
├── governance/
│   ├── budget_controller.py   # per-phase/loop caps, convergence bound              [arch §6]
│   └── human_liaison.py       # HITL packaging (no-op path in self-judge profile)
├── trust_root/                # Phase 4 (real code; dev-grade roots — see §0.3)     [gov §4/§5]
│   ├── trusted_builder.py     # hermetic build, SBOM, SLSA provenance, keyless sign
│   ├── admission.py           # fail-closed re-verify of signature + gate-state
│   └── deployer.py            # non-agent, idempotent, rollback
├── domains/                   # verdict-class taxonomy + pluggable packs            [domains.md]
│   ├── contract.py            # DomainPackContract (the hooks)
│   ├── verdict_class.py       # classes 1–5
│   ├── state_sha.py           # data/weight refs for ML
│   └── packs/                 # regulated.py, ml_data.py, frontend.py
├── evaluation/                # the eval/A-B harness                                [evaluation.md]
│   ├── hypothesis.py preregistration.py decision_table.py
│   ├── suites.py agent_validity.py trajectory.py integrity.py
│   ├── scorecard.py cost_model.py optimization.py safeties.py factory_eval.py
├── operations/                # Day-2                                               [operations.md]
│   ├── operator.py run_status.py store_resilience.py trusted_component.py
│   ├── upgrade.py capacity.py hitl.py trusted_lifecycle.py
├── lifecycle/                 # product lifecycle                                   [lifecycle.md]
│   ├── maintenance.py steering.py feedback_loop.py modes.py licensing.py
├── policy/                    # five-plane wiring + segregation-of-duties           [gov §2]
├── observability/             # OTel + Langfuse + drift alerts
└── cli.py                     # run a request end-to-end
tests/                         # unit + property (reducer purity, idempotency) + eval-integrity
```

---

## 2. The state backbone (build this first)

Everything depends on the typed state and its merge semantics. From [architecture.md §3–4](./architecture.md):

### 2.1 `Seq` total order
`Seq = (run_id, emission_epoch, superstep_index, node_id, local)`, lexicographic.
**Invariant:** `emission_epoch` (a durable per-`(run,key)` resume-generation counter,
incremented in the committing txn) is ordered **ahead of** `superstep_index`, because
`superstep_index` is not monotonic across resume. Total order within a run; cross-fork
resolution is *not* seq-ordered (it is fail-closed admission).

### 2.2 `FactoryState` channels and reducers
`FactoryState` is a `TypedDict` with channel-specific commutative, none-safe reducers:

| Channel | Reducer | Semantics |
|---|---|---|
| `artifacts` | `_merge_by_uri` | set-union by content address |
| `pinned_constraints` | `_merge_constraints` | supersession + cardinality bound → HITL at 90% (never silent truncation) |
| `gates` | `_merge_gates` | latest-by-seq per `(name, run_against)`, **attestation-gated** |
| `gate_triage`, `tasks`, `actions` | `_union_by_key` | keyed set-union |
| `request`, `route`, `phase`, `budget` | single-writer | no merge |

**Load-bearing invariant on `_merge_gates`:** deterministic-gate records are accepted
**only if runner-attested** (unsigned agent writes dropped); judgment verdicts only if
the judgment binding validates, the nonce is unconsumed, and no negative-verdict latch
is set. The negative latch makes the reducer **not pure max-by-seq** — a
`request-changes` cannot be superseded by re-emitting an older `approve`. The latch is
**read** from the global cross-fork record, never written by the reducer.

### 2.3 `tree_sha` / `state_sha`
`tree_sha = H(normalized_source ⧺ resolved_lockfile ⧺ toolchain_digest [⧺ data_refs ⧺ feature_defs])`.
Excludes build outputs/timestamps; includes lockfile + toolchain so a dependency or
builder bump invalidates cached verdicts. Execution-dependent verdicts (Test/E2E/SAST)
additionally bind `mutant_rotation_id + scanner_rule_version` into the attestation and
the dependency-scoped reuse key.

### 2.4 Global cross-fork records
Survive forking (new `run_id`) and persist across runs ([architecture.md §3/§5](./architecture.md), [governance §5](./governance.md)):
stable-FAIL latch (deterministic gates, monotonic), negative-verdict latch (judgment),
nonce-consumption ledger, and the **class-3 alpha look-ledger keyed on
`(eval_set_identity, decision_id)`** — not `state_sha` — so forking/retraining can't
re-draw the budget.

---

## 3. Trust & execution core

### 3.1 Two-zone gate runner ([governance §1c](./governance.md))
**Zone A** runs agent code (sandbox, no signing creds); **Zone B** computes and signs
verdicts, runs no agent code. Verdict attestation signs the tuple
`(gate_name, state_sha, scanner_config_rev, lineage_map_revision, status, results_digest)`.

### 3.2 Sandbox ([architecture.md §2](./architecture.md), [PLAN §6](../PLAN.md))
Worktree-only writes; network only to the proxy via loopback/UDS; no credential
inheritance; cgroup CPU/wall-clock cap with external SIGKILL on budget breach;
destroyed and rebuilt between untrusted-execution and credentialed phases.

### 3.3 Model-provider proxy ([architecture.md §2b](./architecture.md))
The **single egress** and **single credential holder**. Observes the actually-served
model (not the adapter's self-report), resolves it through a versioned
lineage-equivalence map to a `served_family` **set**, extracts judgment verdicts from a
grammar-constrained channel, and stamps the judgment binding **outside** the
implementing adapter. In the self-judge profile it also stamps `judgment_provenance`.
Acknowledged trust SPOF → on unavailability, judgment nodes pause-and-escalate
(fail-closed).

### 3.4 Adapter contract ([architecture.md §2b](./architecture.md))
In: task packet + pinned constraints (verbatim) + retrieved spans + scoped worktree +
least-privilege tools + turn/cost budget. Out: diff + test sources + `GateTriage`
(advisory) + proxy-observed lineage (adapter self-report ignored). Default adapter
`claude -p`; long-running adapters must support turn snapshots, external metering,
trusted-infra-observed progress signals, and turn-granularity loop detection.

### 3.5 Durability ([architecture.md §5](./architecture.md))
One Postgres transaction per superstep commits: LangGraph checkpoint + budget ledger +
audit (WORM, hash-chained) + external-action outbox + turn-snapshot handle. The relay
drains the outbox **at-least-once** with three idempotency paths (external client key →
embedded marker → Foundry dedup); unknown key ⇒ fail-to-human.

---

## 4. Agent catalog (the org)

Seven layers, each agent a node with an SOP and pydantic I/O contract ([agents.md](./agents.md)).
**Tiers** map to Anthropic models (T1≈Opus, T2≈Sonnet, T3≈Haiku) — validated in Phase 6,
not assumed. In the self-judge profile, every "different-family" rule becomes a recorded
self-judge stamp.

| Layer | Agents (tier) | Produces |
|---|---|---|
| Product | Stakeholder Proxy (T2), Product Manager (T2), **Criteria-Completeness Critic (T1)**, Eng/Program Mgr (T2) | IntentBrief, PRD, AcceptanceCriteria, SprintPlan/Budget → **Spec gate** |
| Architecture | Principal Architect (T1), Domain Specialist (T1/T2, cond.), UX/Design (T2, cond.) | SystemDesign, ADRs, Interfaces, OwnershipMap, PinnedConstraints → **Design gate (HITL)** |
| Implementation | Senior Engineer (T1/T2), Junior Engineer (T2/T3) | Tasks, FeatureDiffs (branch-per-task), conflict resolutions → **Build/Test (per-branch)** |
| Quality | Reviewer/Critic (T1), QA Engineer (T2/T1), SDET (T2) | ReviewVerdict, TestPlan/Report (negative+property+golden oracles), E2E → **Test/E2E gates** |
| Security | AppSec (T1), Audit/Compliance (T2), Supply-Chain Reviewer (T1) | ThreatModel (oracle-bound), SBOM/license, supply-chain policy → **Security/Audit/Supply-chain** |
| Operations | DevOps/SRE (T2), Release Manager (T2) | Pipeline, IaC proposal (plan-only), DeployPlan, Release evidence pack → **Release gate** |
| Governance | Orchestrator/Triage (T2), Guardian analyzer (T2), Budget Controller (T3), Human Liaison | routing, advisory flags, budget decisions, HITL packaging |

Each agent's full SOP + schema is specified in `schemas/` (see the I/O contracts already
drafted per role). The **Criteria-Completeness Critic** and **Reviewer** are the seams
where the self-judge stamp applies in the chosen profile.

---

## 5. Gates, monitors, and the five planes

### 5.1 Quality gates ([governance §1](./governance.md), [domains.md §1](./domains.md))
Each gate is keyed by verdict class:

| Gate | Verdict class | Grounding |
|---|---|---|
| Spec | 1 (config-evaluable) | criteria-completeness; security/abuse omission fails |
| Design | 1 + HITL | artifact completeness; threat-oracle bindings frozen; residual → Threat-review |
| Build | 2 (execution) | zone-B-observed compiler/lint/type exit codes |
| Test | 2 | trusted harness + **held-out mutants + behavioral invariance + criterion fidelity**; adequacy floor |
| Security | 2 | pinned SAST/secret/SCA from run-immutable config |
| Threat-review | 5 (judgment) | self-judge stamp (profile) or independent + HITL (default/regulated) — **not** in signed predicate |
| Audit | 1 | zone-B-rederived SBOM; pinned CVE snapshot; license rules |
| Supply-chain | 1 | OPA over build/CI/IaC diffs vs baseline |
| E2E | 2 | trusted harness, pinned seeds/clock/network, N=2 flake quarantine |
| Release | 4 (human-attested) | evidence-pack + nonce binding; two-person if trust-root (regulated) |

### 5.2 Monitors ([governance §3](./governance.md))
**Deterministic (load-bearing, no LLM):** semantic loop signature, cost thresholds, OPA
deny-count, retry oscillation, and the **critical-diff trip** (any diff to test oracles,
gate logic, acceptance criteria, or scanner config → deterministic HITL trip, hard
rule). **Analyzer (advisory):** toolless sandbox, structured features only, cannot
approve or override a deterministic trip; precision/recall measured incl. prompt-injection.

### 5.3 Five planes ([governance §2](./governance.md))
Policy (OPA, run-immutable scanner config), Execution (sandboxes), Data (scoped creds,
egress redaction), Observability (OTel + fingerprints + drift alerts), Assurance (SBOM,
hermetic build, SLSA, admission). Wired in `policy/` + `observability/`.

### 5.4 Change-risk tiering ([governance §1e](./governance.md))
Triage emits a validated tier (Low/Standard/High) scaling assurance depth and HITL —
never the integrity guarantees (admission always fail-closed). Self-judge tier-split is
configurable here.

---

## 6. Trust root, domains, eval, ops (the later layers)

- **Trust root** (`trust_root/`, [governance §4–5](./governance.md)): hermetic SLSA-L3
  builder, fail-closed admission re-verifying signature + gate-state for the exact
  digest, non-agent deployer. Real code; dev-grade roots (§0.3).
- **Domain packs** (`domains/`, [domains.md](./domains.md)): `DomainPackContract` hooks
  (verdict-class assignment, `state_sha` components, compute budget, mode/tier veto,
  approver-of-record, egress profile, mandatory perceptual HITL). Packs: **regulated**
  (forces Gated+, disables self-judge, two-person), **ml/data** (`state_sha` on
  data/weights, statistical class-3 gate + alpha ledger, product-eval + monitoring
  artifacts), **frontend** (class-5 perceptual HITL, zone-B-signed rendered bundle,
  visual-regression class-3 with sub-pixel-only envelopes, dual a11y gate).
- **Eval harness** (`evaluation/`, [evaluation.md](./evaluation.md)): the **central
  hypothesis**, **A/B pre-registration** frozen into the manifest, the **decision
  table** (Proceed / Hard KILL / Bounded Redesign), outcome+agent-validity+trajectory
  evals, eval-integrity (runtime-unreachable hidden tests, contamination probes), the
  **headline scorecard** (one confirmatory primary: SWE-EVO resolution vs baseline at
  equal USD, oracle-scored; secondaries under Holm–Bonferroni — see thresholds in
  [evaluation §7](./evaluation.md)), the per-feature + A/B cost model, and Phase-6
  optimization (Pareto-improving only).
- **Operations** (`operations/`, [operations.md](./operations.md)): operator
  role/SLOs/alerts/runbooks, run-status (paused vs wedged + watchdog), store
  HA/poisoned-checkpoint recovery, trusted-component availability + per-lineage
  capacity reservation, upgrade/migration with runs in flight, multi-tenancy/capacity/
  hibernation, and the HITL-load model (mostly dormant in the zero-HITL profile, live
  for the regulated fallback).
- **Lifecycle** (`lifecycle/`, [lifecycle.md](./lifecycle.md)): generated-code
  maintenance, human steering/taste constraints, product-feedback loop, deployment-trust
  modes, AI-code licensing/liability.

---

## 7. Delivery phases & task backlog

Sequenced per [roadmap.md](./roadmap.md). Each phase lists deliverables → owning
modules, exit criteria, and effort.

### Phase 0 — Foundations (~1 quarter)
**Deliver:** reducer-annotated `FactoryState` + `seq` + none-safe reducers
(`state/`); LangGraph skeleton (`graph/`); transactional outbox + checkpointer
(`durability/`); sandbox runner (`execution/`); OTel (`observability/`); policy-plane
stub + deterministic-monitor placeholders (`policy/`, `monitors/`); basic run-status
(`operations/run_status.py`).
**Exit:** a trivial task runs end-to-end, traced, checkpointed; kill-then-resume
produces no duplicate commit/PR. *(property tests for reducer purity + idempotency are
the gate here.)*

### Phase 1 — Core vertical slice, Assistant mode (~1–2 quarters)
**Deliver:** PM → Architect → Senior → QA subgraphs (`agents/`, `graph/subgraphs/`);
distinct Spec/Build/Test gates (`gates/`); criterion-traceable tests; flake quarantine;
content-addressed artifacts; Assistant mode (open a PR a human merges; ordinary CI, no
trust root). The proxy + adapter (`claude -p`) land here.
**Exit:** from a one-line request, produces a small working app passing its own + hidden
tests and opens a PR mergeable with minimal edits.

### Phase 1.5 — Decision gate: powered A/B + role ablation (~1 quarter)
**Deliver:** variance/base-rate pilot (blocker, runs first); frozen pre-registration +
decision table (`evaluation/`); powered A/B on SWE-EVO-style tasks (3 arms, oracle-scored
primary, ITT); role ablation (keep only roles that beat the PM→Architect→Senior→QA core).
**Exit (real branch):** structure beats baseline by the pre-registered margin at ≤ cost
→ **Proceed**; null at funded MDE → **Hard KILL / redesign**; {USD-null, token-positive}
→ **bounded overhead-redesign** (single, scope-frozen re-test). *This gate precedes the
expensive machinery on purpose.*

### Phase 2 — Review & assurance, Gated mode (~1–2 quarters)
**Deliver:** Reviewer + evaluator-optimizer loop; Security/Audit/Supply-chain agents +
gates; change-risk tiering; dependency-scoped re-verification; HITL at Design/Release
(self-judge stamp in the chosen profile); WORM audit; Gated mode + evidence pack.
**Exit:** a known-vulnerable change is caught; re-verification is change-proportional;
rework loops terminate within caps.

### Phase 3 — Operability & scale (~1–2 quarters)
**Deliver:** operator dashboard/SLOs/alerts/runbooks; run-status liveness + watchdog;
store HA/backup + poisoned-checkpoint recovery; multi-tenancy/capacity/queue/fairness +
hibernation; HITL-load model + reviewer queue; full deterministic monitors + sandboxed
analyzer; five planes (sans full trust root).
**Exit:** operator can diagnose a stuck run; store outage pauses (not corrupts) and
recovers; concurrent runs don't starve each other.

### Phase 4 — Trust root & Autonomous mode (~2 quarters, research-grade)
**Deliver:** two-zone trusted gate runner; hermetic SLSA-L3 build; deploy-time
admission; input-bound attestations; trusted-component HA + rotation (`trust_root/`,
`operations/trusted_lifecycle.py`). *(Dev-grade roots here; production hardening is an
ops task — §0.3.)*
**Exit:** malicious-test / forged-results / forged-verdict / fork-divergence cannot
reach production; trusted-component outage degrades to *paused*, not *wedged*.

### Phase 5 — Specialist plugins + domain hooks (~1–2 quarters)
**Deliver:** specialist interface + registry + capability routing; core pack hooks;
first packs (regulated, ml/data, frontend).
**Exit:** a regulated request makes Autonomous/auto-approve unreachable and binds an
approver-of-record; an ML request keys verdicts on `state_sha` with a class-3 gate; a UI
request forces visual HITL — all without core changes.

### Phase 6 — Eval, optimization & lifecycle (ongoing)
**Deliver:** full eval harness; confirmatory A/B (estimation/replication only — Phase 1.5
is the sole arbiter that spent alpha); constrained workflow optimization (Pareto-improving);
product-feedback loop + generated-code maintenance/provenance; red-team; cost/latency
hardening.
**Exit:** scorecard published and stable; Phase-6 A/B reproduces the Phase-1.5 result;
optimization Pareto-improves ≥1 headline metric with no regression.

---

## 8. Build order (topological)

Within the phases, components have hard dependencies. Build order:

1. `state/` (factory_state, reducers, seq, tree_sha, global_records) — **foundational, no deps**
2. `schemas/` — depends on 1
3. `durability/` (outbox, checkpointer) — depends on 1
4. `proxy/` — depends on 1
5. `execution/` (sandbox, adapter_contract, adapters) — depends on 4
6. `gates/` (runner, attestation, gate impls) — depends on 1, 4
7. `governance/budget_controller` + `monitors/` (loop, cost, oracle, policy, guardian) — depends on 1, 3
8. `context/` (pinned_constraints, retriever, determinism) — depends on 1
9. `agents/` per layer — depends on 2, 4, 8
10. `graph/subgraphs/` then `graph/supervisor` + `graph/factory_graph` — depends on 6–9
11. `trust_root/` (builder, admission, deployer) — depends on 1, 6
12. `domains/` (contract, verdict_class, state_sha, packs) — depends on 1, 6
13. `evaluation/`, `operations/`, `lifecycle/`, `policy/`, `observability/` — cross-cutting, layered in by phase

`cli.py` ties it together once 10 exists (Assistant mode at Phase 1).

---

## 9. Testing & verification strategy

- **Property tests** (load-bearing): reducer commutativity/idempotency/none-safety;
  outbox at-least-once + idempotency under simulated crash/resume; `tree_sha` stability
  under cosmetic change and sensitivity to lockfile/toolchain.
- **Gate-grounding tests:** unsigned agent `GateResult` is rejected by `_merge_gates`;
  forged judgment binding/replayed nonce rejected; negative-verdict latch survives an
  older `approve`.
- **Eval-integrity tests** ([evaluation §6](./evaluation.md)): hidden tests provably
  runtime-unreachable; ITT pass-rate reported unconditionally; contamination probes.
- **Adversarial (Phase 4):** malicious-test, forged-results, forged-verdict,
  fork-divergence, build-hook injection, exfil-over-allowlist — must not reach prod.
- **Self-judge audit:** assert every class-4/5 judgment verdict in the chosen profile
  carries `judgment_provenance = "self"` and the scorecard never reports it as
  independently validated.

---

## 10. Cross-reference index

| Plan section | Design source |
|---|---|
| §2 state backbone | [architecture.md §3–5](./architecture.md) |
| §3 trust & execution core | [architecture.md §2, §2b, §5](./architecture.md), [governance §1c](./governance.md) |
| §4 agent catalog | [agents.md](./agents.md) |
| §5 gates/monitors/planes | [governance §1–3](./governance.md), [domains.md §1](./domains.md) |
| §6 trust root / domains / eval / ops / lifecycle | [governance §4–5](./governance.md), [domains.md](./domains.md), [evaluation.md](./evaluation.md), [operations.md](./operations.md), [lifecycle.md](./lifecycle.md) |
| §7 phases | [roadmap.md](./roadmap.md), [PLAN.md §10](../PLAN.md) |
| §0.2 self-judge profile | [governance §1e](./governance.md) |

---

*This plan is the implementation-specification layer over the converged design. It does
not re-open architecture; where a choice was genuinely open (stack, operating profile)
it records the decision and cites the design section that governs it.*
