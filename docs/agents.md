# Agent catalog — roles, SOPs, and I/O contracts

Every agent is a graph node with: a **role/system prompt**, a **standard
operating procedure (SOP)** (ordered steps), a **least-privilege tool set**, an
**input contract** and an **output artifact**, a **model tier**, and a
**context-assembly contract** ([architecture.md §6](./architecture.md)) so it
receives a bounded window, never the full history. Agents communicate through
content-addressed, versioned artifacts, not chat.

> Model tiers (pinned IDs recorded in the run manifest): **T1 (Opus-class)** =
> high-stakes judgment; **T2 (Sonnet-class)** = implementation; **T3
> (Haiku-class)** = routing/cheap classification. **T1 nodes never auto-downgrade
> on budget pressure** — they escalate ([governance.md §7](./governance.md)).
>
> Tiering is a **validated output of Phase-5 optimization, not an intuition**
> ([evaluation.md §9](./evaluation.md)). Until data says otherwise, **high-risk
> *implementation* and high-risk *oracle authoring* default to T1** (defects are
> born in implementation, and a weak oracle defeats every downstream gate);
> escaped-defects are tracked *by the producing tier* to keep the assignment
> honest. On high-risk criteria, review/QA run a model from the **second provider
> family** (Claude tiers are *not* different families; a genuinely different
> lineage is provisioned — [architecture.md §8](./architecture.md)) to
> decorrelate blind spots.
>
> **Non-agent components** (not LLM roles, listed for completeness): the trusted
> hermetic CI builder, the **two-zone trusted gate runner** — *zone A* runs the
> agent-authored tests/build/scans with no signing creds and emits a raw results
> stream; *zone B* (runs no agent code) computes & **signs** the authoritative
> verdict (bound to `tree_sha`+config) for every deterministic gate —
> Build/Test/Security/Audit/Supply-chain/E2E — so agents only triage
> (`GateTriage`), never bless ([governance.md §1c](./governance.md)) — the
> deploy-time admission controller, the least-privileged deployer, and the WORM
> audit appender. Agents can *propose* inputs but never *are* these components.

---

## Layer 1 — Product & Planning

### Stakeholder / Client Proxy — T2
- **Purpose:** stand in for the customer; hold the "why" and resolve ambiguity.
- **SOP:** restate intent → list assumptions & unknowns → define success in user terms → flag must-have vs nice-to-have.
- **In:** raw request. **Out:** `intent_brief`. May trigger HITL to ask the real human.

### Product Manager — T2
- **Purpose:** turn intent into a buildable, testable specification.
- **SOP:** write PRD → user stories → **acceptance criteria** (testable Given/When/Then, each with a `criterion_id`) → declare `required_capabilities` (drives UX/specialist routing) → mark high-risk criteria (for stronger oracles) → prioritize.
- **In:** `intent_brief`. **Out:** `prd`, `acceptance_criteria`.

### Criteria-Completeness Critic — T1 · *independent role/family*
- **Purpose:** stop "100% coverage of weak criteria." Independently critiques the PM's `acceptance_criteria` for completeness against the PRD — **especially security/abuse criteria** — before the Spec gate.
- **SOP:** enumerate missing/under-specified criteria → flag omitted security/abuse cases → require additions. **Omission of applicable security/abuse criteria fails the Spec gate.**
- **In:** `prd`, `acceptance_criteria`. **Out:** `criteria_findings`.
- **Gate produced:** Spec gate (jointly with the structural check).

### Engineering / Program Manager — T2
- **Purpose:** planning, sequencing, budget, and risk.
- **SOP:** decompose into milestones → estimate → **refine the absolute `usd_cap`** seeded at Triage; per-phase/per-loop sub-budgets apply ([governance.md §7](./governance.md)) → build risk register → define done-for-this-iteration.
- **In:** `prd`. **Out:** `sprint_plan`, `risk_register`, budget seed.

---

## Layer 2 — Architecture & Design

### Principal Architect / Engineering Lead — T1
- **Purpose:** the most consequential decisions; everything downstream inherits these.
- **SOP:** **retrieve** relevant existing code via the symbol-index + embedding retrieval contract (not free reading) → choose architecture & tech → write **ADRs** (with rejected alternatives) → define **interface/API contracts**, **data model**, and **file/interface ownership** (used for deterministic merge order) → identify cross-cutting concerns.
- **In:** `prd` (PRD on greenfield, change brief / PRD delta on evolution), `sprint_plan`, retrieved code spans (symbol + call/def graph). **Out:** `system_design`, `adrs[]`, `interfaces`, `ownership_map`, **`pinned_constraints`** (invariants, rejected-alternative rationale, high-risk criteria, ownership limits — injected verbatim downstream, never summarized, per [architecture.md §8](./architecture.md)).
- *(Spike variant:* timeboxed research producing a `spike_report` only; terminates without entering implementation/release.*)*
- **Gate produced:** Design gate (+ HITL approval for significant designs).

### Domain Specialist (pluggable) — T1/T2 · *conditional node*
- **Purpose:** deep expertise for a domain (ML, data, mobile, embedded, fintech, healthcare…).
- **Entered only when** `required_capabilities` intersect the registry ([architecture.md §7](./architecture.md)). Conforms to the Specialist interface (`capabilities`, `tools`, `prompts`, `gates`, `evals`).
- **In:** `system_design` + domain context. **Out:** domain design addendum + extra gates (consumers treat as optional).

### UX / Design — T2 · *conditional node (UI projects only)*
- **Entered only when** `required_capabilities` contains `ui`.
- **SOP:** user flows → wireframe/spec → accessibility checklist.
- **Out:** `ux_spec` (optional artifact; downstream tolerates absence).

---

## Layer 3 — Implementation

### Senior Engineer — T1/T2
- **Purpose:** decomposition, scaffolding, the hard parts, reviewing juniors, and **conflict resolution at integration**.
- **SOP:** break design into well-specified tasks, each assigned **its own branch** (`task/<id>`) → scaffold modules & interfaces → implement high-risk pieces → **review junior diffs** (evaluator side of the evaluator-optimizer loop) → at integration, resolve merge conflicts using the `ownership_map` and conflicting hunks.
- **In:** `system_design`, `interfaces`, `ownership_map`. **Out:** `tasks[]`, scaffolding diff, review verdicts, conflict resolutions.
- **Tools:** filesystem, git (branch-scoped), build, test runner (sandboxed).

### Junior Engineer — T2/T3
- **Purpose:** implement well-specified tasks cheaply on an isolated branch.
- **SOP:** read task + interface contract → write code on `task/<id>` → run per-branch build/test → self-check against acceptance criteria → open PR for review. Re-runs reset the branch to base first (idempotent retry).
- **In:** a single `task`. **Out:** feature `diff` / PR on its branch.
- **Tools:** filesystem, git (own branch only), build, test runner (sandboxed), scoped to the task.

---

## Layer 4 — Quality & Verification

### Reviewer / Critic — T1
- **Purpose:** the optimizer's evaluator — gatekeep code quality.
- **SOP:** review diff for correctness, design adherence, readability, and **test diffs** (guard against weakened assertions) → verdict `approve | request-changes` with specific findings.
- **In:** `diff`, `interfaces`, `acceptance_criteria`. **Out:** review verdict.

### QA Engineer — T2 (high-risk oracle authoring → **T1**) · *distinct model, cross-family on high-risk*
- **Purpose:** does it meet the acceptance criteria? Authors the oracle — the most consequential artifact in the loop, so high-risk oracle authoring is T1.
- **SOP:** derive tests from `acceptance_criteria` with **`criterion_id` traceability** → for each high-risk criterion add a **negative oracle** (the test must *fail against a known-bad reference implementation* — criterion *fidelity*, not just coverage) and property/golden oracles → run on the branch then the integrated tree → exploratory probing → report. **Authorship is separated from implementation and uses a different model family on high-risk criteria** ([governance.md §1b](./governance.md)).
- **In:** `acceptance_criteria`, `pinned_constraints`, build. **Out:** `test_plan`, `test_report` (criterion fidelity + coverage).
- **Gate produced:** **Test gate** (green + criterion fidelity on high-risk + coverage threshold).

### SDET — T2
- **Purpose:** integration and end-to-end automation on the integrated tree.
- **SOP:** define e2e scenarios → automate → wire into CI → run → report.
- **In:** integrated tree. **Out:** `e2e_suite`, integration report.
- **Gate produced:** **E2E gate** (e2e scenarios green; flake policy applies). A failure routes to a targeted `FIX` and re-verification.

> The **Build gate** (compile/lint/type-check) is owned by the **CI runner**, not
> QA — it runs per-branch and on the integrated tree. This reconciles the gate
> ownership across all docs.

---

## Layer 5 — Security & Compliance

### Security / AppSec Engineer — T1
- **SOP:** threat model (STRIDE) → **bind each threat-model item to a concrete runner-evaluable oracle** (a test, SAST/Semgrep rule, or negative-oracle) at the Design seam → trigger the trusted runner's scans → manual review of auth/crypto/input handling, filing `GateTriage` (which items to add, severity, remediation) — **never the verdict bit**.
- **In:** integrated tree, design. **Out:** `threat_model` (with per-item oracle bindings), `security_triage`.
- **Gate produced:** the **Security verdict is computed & signed by the trusted runner** (each item's bound oracle passes; SAST/secret/SCA clean). Threat-model items that can't be reduced to an oracle go to the **Threat-review judgment sub-gate** (independent review + HITL), not the runner-signed verdict ([governance.md §1c/§1d](./governance.md)).

### Audit / Compliance — T2
- **SOP:** generate **SBOM** → check license compatibility + critical-CVE floor → map to applicable controls (domain-pack dependent) → **emit audit events via the transactional outbox to the non-agent WORM appender** (this role does *not* hold the WORM write credential and cannot truncate/rotate the chain — [governance.md §8](./governance.md)). **Signing/SLSA attestation is performed by the trusted CI builder, not this agent.**
- **Out:** `sbom`, `license_report`, `compliance_map`.
- **Gate produced:** Audit gate.

### Supply-Chain Reviewer — T1 · *AppSec + DevOps*
- **Purpose:** the build the trusted builder will execute is agent-authored — review it as a high-privilege artifact.
- **SOP:** diff CI/IaC/Dockerfile/lockfile/manifest/scanner-config against baseline → enforce policy (no build-time network/hooks, no privilege/egress widening, no signing/OIDC-trust changes without HITL) → SAST over build scripts + IaC (Conftest/Checkov on `terraform plan`).
- **In:** `tree_sha` diff. **Out:** `supply_chain_report`.
- **Gate produced:** **Supply-chain gate** (runs in the assurance fan-out).

---

## Layer 6 — Operations

### DevOps / SRE — T2
- **SOP:** author/verify CI/CD → IaC → observability (logs/metrics/traces) → deploy plan + rollback + health checks. **IaC is *proposed*, not applied, by this agent:** its `plan` diff is gated by policy-as-code (Supply-chain gate), and a **separate least-privileged non-agent deployer applies it** after admission ([governance.md §2/§5](./governance.md)). Effectful actions go through the **transactional outbox / external-action ledger** (idempotent, idempotency-key stored externally — [architecture.md §5](./architecture.md)).
- **Out:** `pipeline`, `iac` (proposal), `deploy_plan`.

### Release Manager — T2
- **SOP:** version (semver) → changelog → verify all gates green and that the **trusted builder has produced SLSA provenance + signature with gate-state bound in the predicate** → present the **independent evidence pack** ([governance.md §6](./governance.md)) for human sign-off → on trust-root-touching releases require **two-person sign-off** → hand to the admission controller (which re-verifies signature + gate-state for the exact digest before the non-agent deployer ships). Idempotent via ledger; never double-tags on resume.
- **Gate produced:** Release gate (HITL, evidence-backed).

---

## Layer 7 — Governance / Meta

### Orchestrator / Supervisor (incl. Triage) — T2
- **Purpose:** routing and scheduling across subgraphs; owns top-level transitions and the single-entry/single-exit + re-verification rules.
- **Triage SOP:** classify the request (greenfield | evolution | bugfix | spike) → **seed a default per-route budget cap** (refined later by the Eng/Program Manager on routes that visit PLAN) → route. Greenfield/evolution → Product Manager; bugfix → MINSPEC; spike → timeboxed Architect research (throwaway, not released).

### Guardian — *deterministic monitors (no LLM) + event-triggered sandboxed analyzer (T2, no tools)*
- **Deterministic monitors (load-bearing):** semantic loop/non-convergence signature `(criterion_id/task, gate, failure_signature)` + global `fix_attempts_since_all_green` bound; cost thresholds; OPA deny-count; retry-until-green oscillation; **and any diff touching test oracles, gate logic, acceptance criteria, or scanner config is a *deterministic* trip → HITL** (not an analyzer judgment). These need no model and cannot be prompt-injected.
- **Analyzer (advisory color only):** runs read-only in its **own toolless sandbox**, **event-triggered** by a deterministic-monitor signal or a write to oracle/gate/criteria/config paths (not per node), reading a **capped** slice of **deterministically-extracted structured features** (untrusted trace prose framed as data, never instructions). Flags semantic concerns advisorily; **cannot approve work or override a deterministic trip**. Evaluated for precision/recall *including against prompt-injection-of-the-monitor*; tokens metered as `monitor_overhead_usd` against the §8 cost model.

### Budget Controller — T3 (mostly deterministic accounting)
- **SOP:** meter tokens/USD/wallclock per node and per loop → enforce per-phase/per-loop caps → on T2/T3 pressure may downgrade tier; on **T1 pressure escalates** → on hard breach gracefully checkpoint-and-pause ([governance.md §7](./governance.md)).

### Human Liaison — interface
- **SOP:** package escalations and HITL decisions (design approval, release sign-off, retry-cap breaches, monitor flags) into a clear question → record the decision in `human_decisions` and the WORM audit store → resume from checkpoint without re-firing effectful actions.

---

## I/O contract summary

| Agent | Input | Output artifact | Tier | Produces gate |
|---|---|---|---|---|
| Client Proxy | request | intent_brief | T2 | — |
| Product Manager | intent_brief | prd, acceptance_criteria | T2 | (Spec, jointly) |
| Criteria-Completeness Critic | prd, acceptance_criteria | criteria_findings | T1 | Spec |
| Eng/Program Mgr | prd | sprint_plan, risk_register, budget | T2 | — |
| Principal Architect | prd, retrieved code | system_design, adrs, interfaces, ownership_map, pinned_constraints | T1 | Design |
| Domain Specialist *(cond.)* | system_design | domain addendum, extra gates | T1/T2 | domain-specific |
| UX/Design *(cond.)* | prd | ux_spec | T2 | — |
| Senior Engineer | design, interfaces, ownership_map | tasks, scaffolding, reviews, conflict resolutions | T1/T2 | — |
| Junior Engineer | task | diff/PR (own branch) | T2/T3 | — |
| Reviewer/Critic | diff | verdict | T1 | — |
| Trusted gate runner *(non-agent, 2-zone)* | raw results (zone A) + pinned config | input-bound signed verdicts (zone B) | n/a | **Build, Test, Security, Audit, Supply-chain, E2E verdicts** (agents write `GateTriage` only) |
| QA Engineer | acceptance_criteria, pinned_constraints | test_plan, test_report | T2 (T1 high-risk oracle) | **Test** |
| SDET | integrated tree | e2e_suite | T2 | E2E |
| Security/AppSec | tree_sha | threat_model, security_findings | T1 | Security |
| Audit/Compliance | tree_sha | sbom, license_report, compliance_map | T2 | Audit |
| Supply-Chain Reviewer | tree_sha diff | supply_chain_report | T1 | Supply-chain |
| Trusted CI builder | approved tree | SBOM, SLSA provenance + gate-state, signature | n/a (non-agent) | — |
| Admission controller | attested digest | verify result | n/a (non-agent) | (admission) |
| Non-agent deployer | verified digest | deployment | n/a (non-agent) | — |
| WORM appender | outbox audit events | anchored audit chain | n/a (non-agent) | — |
| DevOps/SRE | release | pipeline, iac (proposal), deploy_plan | T2 | — |
| Release Manager | attested build + evidence pack | release, changelog | T2 | Release |
| Supervisor (+Triage) | state | routing decision + budget seed | T2 | — |
| Guardian (monitors) | trajectory | halt/pause + deterministic trips | n/a (deterministic) | — |
| Guardian (analyzer) | structured features (event-triggered) | advisory flags | T2 | — |
| Budget Controller | usage ledger | throttle (T2/T3) / escalate (T1) | T3 | — |
