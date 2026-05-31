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
- **Gate produced:** Spec gate.

### Engineering / Program Manager — T2
- **Purpose:** planning, sequencing, budget, and risk.
- **SOP:** decompose into milestones → estimate → **refine the absolute `usd_cap`** seeded at Triage; per-phase/per-loop sub-budgets apply ([governance.md §7](./governance.md)) → build risk register → define done-for-this-iteration.
- **In:** `prd`. **Out:** `sprint_plan`, `risk_register`, budget seed.

---

## Layer 2 — Architecture & Design

### Principal Architect / Engineering Lead — T1
- **Purpose:** the most consequential decisions; everything downstream inherits these.
- **SOP:** **retrieve** relevant existing code via the symbol-index + embedding retrieval contract (not free reading) → choose architecture & tech → write **ADRs** (with rejected alternatives) → define **interface/API contracts**, **data model**, and **file/interface ownership** (used for deterministic merge order) → identify cross-cutting concerns.
- **In:** `prd` (PRD on greenfield, change brief / PRD delta on evolution), `sprint_plan`, retrieved code spans. **Out:** `system_design`, `adrs[]`, `interfaces`, `ownership_map`.
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

### QA Engineer — T2 · *distinct model instance from the implementer*
- **Purpose:** does it meet the acceptance criteria? Authors the oracle.
- **SOP:** derive tests from `acceptance_criteria` with **`criterion_id` traceability** (every criterion → ≥1 test) → add property-based/golden oracles for high-risk criteria → run on the branch then on the integrated tree → exploratory probing → report. **Authorship is separated from implementation** ([governance.md §1b](./governance.md)).
- **In:** `acceptance_criteria`, build. **Out:** `test_plan`, `test_report` (with criterion-coverage).
- **Gate produced:** **Test gate** (green + criterion-coverage + line-coverage threshold).

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
- **SOP:** threat model (STRIDE) → run SAST + secret scan + dependency/SCA audit → manual review of auth/crypto/input handling → file findings with severity.
- **In:** integrated tree, design. **Out:** `threat_model`, `security_findings`.
- **Gate produced:** Security gate (no unresolved high/critical; threat-model items addressed).

### Audit / Compliance — T2
- **SOP:** generate **SBOM** → check license compatibility + critical-CVE floor → map to applicable controls (domain-pack dependent) → write audit events to the **append-only WORM/hash-chained store** ([governance.md §4 and §below](./governance.md)). **Signing/SLSA attestation is performed by the trusted CI builder, not this agent.**
- **Out:** `sbom`, `license_report`, `compliance_map`.
- **Gate produced:** Audit gate.

---

## Layer 6 — Operations

### DevOps / SRE — T2
- **SOP:** author/verify CI/CD → IaC → observability (logs/metrics/traces) → deploy plan + rollback + health checks. Deploy actions go through the **external-action ledger** (idempotent) ([architecture.md §5](./architecture.md)).
- **Out:** `pipeline`, `iac`, `deploy_plan`.

### Release Manager — T2
- **SOP:** version (semver) → changelog → verify all gates green and that the **trusted builder has already produced the SLSA provenance + signature** (attestation precedes the Release gate) → request human sign-off on the attested artifact → tag & release (idempotent via ledger; never double-tags on resume).
- **Gate produced:** Release gate (HITL).

---

## Layer 7 — Governance / Meta

### Orchestrator / Supervisor (incl. Triage) — T2
- **Purpose:** routing and scheduling across subgraphs; owns top-level transitions and the single-entry/single-exit + re-verification rules.
- **Triage SOP:** classify the request (greenfield | evolution | bugfix | spike) → **seed a default per-route budget cap** (refined later by the Eng/Program Manager on routes that visit PLAN) → route. Greenfield/evolution → Product Manager; bugfix → MINSPEC; spike → timeboxed Architect research (throwaway, not released).

### Guardian — *deterministic monitors (no LLM) + sandboxed analyzer (T2, no tools)*
- **Deterministic monitors:** loop detection (state-signature hash), cost thresholds, OPA deny-count, retry-until-green oscillation — these need no model and cannot be prompt-injected.
- **Analyzer:** read-only over redacted traces in its **own toolless sandbox**; flags semantic concerns (scope change, edits to test oracles/gate logic, mid-task capability acquisition) as **advisory** to the Human Liaison. Cannot approve work. Has its own evals; its tokens are metered as `monitor_overhead_usd`.

### Budget Controller — T3 (mostly deterministic accounting)
- **SOP:** meter tokens/USD/wallclock per node and per loop → enforce per-phase/per-loop caps → on T2/T3 pressure may downgrade tier; on **T1 pressure escalates** → on hard breach gracefully checkpoint-and-pause ([governance.md §7](./governance.md)).

### Human Liaison — interface
- **SOP:** package escalations and HITL decisions (design approval, release sign-off, retry-cap breaches, monitor flags) into a clear question → record the decision in `human_decisions` and the WORM audit store → resume from checkpoint without re-firing effectful actions.

---

## I/O contract summary

| Agent | Input | Output artifact | Tier | Produces gate |
|---|---|---|---|---|
| Client Proxy | request | intent_brief | T2 | — |
| Product Manager | intent_brief | prd, acceptance_criteria | T2 | Spec |
| Eng/Program Mgr | prd | sprint_plan, risk_register, budget | T2 | — |
| Principal Architect | prd, retrieved code | system_design, adrs, interfaces, ownership_map | T1 | Design |
| Domain Specialist *(cond.)* | system_design | domain addendum, extra gates | T1/T2 | domain-specific |
| UX/Design *(cond.)* | prd | ux_spec | T2 | — |
| Senior Engineer | design, interfaces, ownership_map | tasks, scaffolding, reviews, conflict resolutions | T1/T2 | — |
| Junior Engineer | task | diff/PR (own branch) | T2/T3 | — |
| Reviewer/Critic | diff | verdict | T1 | — |
| CI runner | branch / integrated tree | build result | n/a | **Build** |
| QA Engineer | acceptance_criteria | test_plan, test_report | T2 | **Test** |
| SDET | integrated tree | e2e_suite | T2 | E2E |
| Security/AppSec | integrated tree | threat_model, security_findings | T1 | Security |
| Audit/Compliance | integrated tree | sbom, license_report, compliance_map | T2 | Audit |
| Trusted CI builder | approved tree | SBOM, SLSA provenance, signature | n/a (not an agent) | — |
| DevOps/SRE | release | pipeline, iac, deploy_plan | T2 | — |
| Release Manager | attested build | release, changelog | T2 | Release |
| Supervisor | state | routing decision | T2 | — |
| Guardian (monitors) | trajectory | halt/pause | n/a (deterministic) | — |
| Guardian (analyzer) | redacted traces | advisory flags | T2 | — |
| Budget Controller | usage ledger | throttle (T2/T3) / escalate (T1) | T3 | — |
