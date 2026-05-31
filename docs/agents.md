# Agent catalog — roles, SOPs, and I/O contracts

Every agent is a graph node with: a **role/system prompt**, a **standard
operating procedure (SOP)** (ordered steps), a **least-privilege tool set**, an
**input contract** and an **output artifact**, and a **model tier**. Agents
communicate through typed artifacts, not chat.

> Model tiers: **T1 (Opus-class)** = high-stakes judgment; **T2 (Sonnet-class)**
> = implementation; **T3 (Haiku-class)** = routing/cheap classification.

---

## Layer 1 — Product & Planning

### Stakeholder / Client Proxy — T2
- **Purpose:** stand in for the customer; hold the "why" and resolve ambiguity.
- **SOP:** restate intent → list assumptions & unknowns → define success in user terms → flag must-have vs nice-to-have.
- **In:** raw request. **Out:** `intent_brief`.
- **Tools:** none (reasoning only). May trigger HITL to ask the real human.

### Product Manager — T2
- **Purpose:** turn intent into a buildable, testable specification.
- **SOP:** write PRD → user stories → **acceptance criteria (testable, Given/When/Then)** → declare `required_capabilities` (drives specialist routing) → prioritize.
- **In:** `intent_brief`. **Out:** `prd`, `acceptance_criteria`.
- **Gate produced:** Spec gate (criteria must be present and testable).

### Engineering / Program Manager — T2
- **Purpose:** planning, sequencing, budget, and risk.
- **SOP:** decompose into milestones → estimate → set `Budget.usd_cap` → build risk register → define done-for-this-iteration.
- **In:** `prd`. **Out:** `sprint_plan`, `risk_register`, budget.

---

## Layer 2 — Architecture & Design

### Principal Architect / Engineering Lead — T1
- **Purpose:** the most consequential decisions; everything downstream inherits these.
- **SOP:** survey existing code (RAG) → choose architecture & tech → write **ADRs** (with rejected alternatives) → define **interface/API contracts** and **data model** → identify cross-cutting concerns (auth, observability, errors).
- **In:** `prd`, `sprint_plan`, codebase memory. **Out:** `system_design`, `adrs[]`, `interfaces`, `data_model`.
- **Gate produced:** Design gate (+ HITL approval for significant designs).

### Domain Specialist (pluggable) — T1/T2
- **Purpose:** deep expertise for a domain (ML, data, mobile, embedded, fintech, healthcare…).
- **SOP:** domain-specific; conforms to the Specialist interface (`capabilities`, `tools`, `prompts`, `gates`, `evals`).
- **In:** `system_design` + domain context. **Out:** domain design addendum + extra gates.
- **Routing:** activated when `required_capabilities` intersect the registry. See [governance.md](./governance.md#domain-packs).

### UX / Design — T2 (UI projects only)
- **SOP:** user flows → wireframe/spec → accessibility checklist.
- **Out:** `ux_spec`.

---

## Layer 3 — Implementation

### Senior Engineer — T1/T2
- **Purpose:** decomposition, scaffolding, the hard parts, and reviewing juniors.
- **SOP:** break design into well-specified tasks → scaffold modules & interfaces → implement high-risk pieces → **review junior diffs** (this is the evaluator side of the evaluator-optimizer loop).
- **In:** `system_design`, `interfaces`. **Out:** `tasks[]`, scaffolding diff, review verdicts.
- **Tools:** filesystem, git, build, test runner (sandboxed).

### Junior Engineer — T2/T3
- **Purpose:** implement well-specified tasks cheaply.
- **SOP:** read task + interface contract → write code + local tests → self-check against acceptance criteria → open PR for review.
- **In:** a single `task`. **Out:** feature `diff` / PR.
- **Tools:** filesystem, git, build, test runner (sandboxed), scoped to the task.

---

## Layer 4 — Quality & Verification

### Reviewer / Critic — T1
- **Purpose:** the optimizer's evaluator — gatekeep code quality.
- **SOP:** review diff for correctness, design adherence, readability, and **test changes** (guard against weakening tests) → verdict `approve | request-changes` with specific findings.
- **In:** `diff`, `interfaces`, `acceptance_criteria`. **Out:** review verdict.

### QA Engineer — T2
- **Purpose:** does it meet the acceptance criteria?
- **SOP:** derive test plan from acceptance criteria → write unit/integration tests → run → exploratory/edge-case probing → report.
- **In:** `acceptance_criteria`, build. **Out:** `test_plan`, `test_report`.
- **Gate produced:** Test gate (green + coverage threshold).

### SDET — T2
- **Purpose:** integration and end-to-end automation.
- **SOP:** define e2e scenarios → automate → wire into CI → report.
- **Out:** `e2e_suite`, integration report.

---

## Layer 5 — Security & Compliance

### Security / AppSec Engineer — T1
- **Purpose:** find and force-fix vulnerabilities before release.
- **SOP:** threat model (STRIDE) → run SAST + secret scan + dependency/SCA audit → manual review of auth/crypto/input handling → file findings with severity.
- **In:** diff, design. **Out:** `threat_model`, `security_findings`.
- **Gate produced:** Security gate (no unresolved high/critical; threat-model items addressed).

### Audit / Compliance — T2
- **Purpose:** provenance, licensing, regulatory mapping, immutable trail.
- **SOP:** generate **SBOM** → check license compatibility → map to applicable controls (domain-pack dependent) → attest **SLSA** provenance → record audit trail.
- **Out:** `sbom`, `license_report`, `compliance_map`, `attestation`.
- **Gate produced:** Audit gate.

---

## Layer 6 — Operations

### DevOps / SRE — T2
- **SOP:** author/verify CI/CD → IaC → observability (logs/metrics/traces) → deploy plan + rollback + health checks.
- **Out:** `pipeline`, `iac`, `deploy_plan`.

### Release Manager — T2
- **SOP:** version (semver) → changelog → verify all gates green → request human sign-off → tag & release.
- **Gate produced:** Release gate (HITL).

---

## Layer 7 — Governance / Meta

### Orchestrator / Supervisor — T2
- **Purpose:** routing and scheduling across subgraphs; owns the top-level graph transitions.
- **SOP:** read state → pick next node / parallelize → enforce gate routing.

### Guardian — T2 (always-on monitor)
- **Purpose:** a "guardian agent" that watches the others.
- **SOP:** stream the trajectory → detect loops, policy violations, reward-hacking, runaway cost → **halt or escalate** to the Human Liaison.
- **Tools:** read-only access to traces + the policy plane.

### Budget Controller — T3
- **Purpose:** keep cost/latency bounded.
- **SOP:** meter tokens/USD/wallclock per node → enforce `Budget.usd_cap` → downgrade model tier or pause and escalate on breach.

### Human Liaison — interface
- **Purpose:** the single, structured channel to the human.
- **SOP:** package escalations and HITL decisions (design approval, release sign-off, retry-cap breaches) into a clear question → record the decision in `human_decisions` → resume from checkpoint.

---

## I/O contract summary

| Agent | Input | Output artifact | Tier | Produces gate |
|---|---|---|---|---|
| Client Proxy | request | intent_brief | T2 | — |
| Product Manager | intent_brief | prd, acceptance_criteria | T2 | Spec |
| Eng/Program Mgr | prd | sprint_plan, risk_register, budget | T2 | — |
| Principal Architect | prd, memory | system_design, adrs, interfaces | T1 | Design |
| Domain Specialist | system_design | domain addendum, extra gates | T1/T2 | domain-specific |
| Senior Engineer | design, interfaces | tasks, scaffolding, reviews | T1/T2 | — |
| Junior Engineer | task | diff/PR | T2/T3 | — |
| Reviewer/Critic | diff | verdict | T1 | — |
| QA Engineer | acceptance_criteria | test_plan, test_report | T2 | Build+Test |
| SDET | build | e2e_suite | T2 | — |
| Security/AppSec | diff, design | threat_model, security_findings | T1 | Security |
| Audit/Compliance | diff | sbom, license_report, attestation | T2 | Audit |
| DevOps/SRE | release | pipeline, iac, deploy_plan | T2 | — |
| Release Manager | green build | release, changelog | T2 | Release |
| Supervisor | state | routing decision | T2 | — |
| Guardian | trajectory | halt/escalate | T2 | — |
| Budget Controller | usage | throttle/escalate | T3 | — |
