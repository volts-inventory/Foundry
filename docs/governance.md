# Governance — gates, guardrails, security & compliance

Foundry's reliability comes from putting **determinism at the seams**: LLMs do
judgment *inside* nodes, but the transitions *between* nodes are governed by
checks that are deterministic wherever an oracle exists. This document defines
the quality gates, the five-plane guardrail model, and the domain-pack
extension mechanism.

## 1. Quality gates

A gate is a node that returns `pass | fail | escalate` and writes a
`GateResult`. Failures route back to the responsible role with a bounded retry
counter; exceeding the cap escalates to the Human Liaison.

| Gate | Owner | Checks (oracle type) | Pass condition |
|---|---|---|---|
| **Spec** | Product Manager | Acceptance criteria present & each is testable (structural + LLM check) | All stories have Given/When/Then criteria |
| **Design** | Architect (+HITL) | ADRs present; interfaces & data model defined; cross-cutting concerns covered | Approved (human sign-off for significant designs) |
| **Build** | Senior Eng / CI | Compiles; lints; type-checks (deterministic) | Zero errors |
| **Test** | QA | Unit + integration green; coverage ≥ threshold; no skipped/quarantined criticals; mutation score ≥ threshold (deterministic) | All green + thresholds met |
| **Security** | AppSec | SAST + secret scan + dependency/SCA audit; threat-model items resolved (deterministic scans + LLM triage) | No unresolved high/critical |
| **Audit** | Compliance | SBOM generated; license compatibility; SLSA provenance attested (deterministic) | Clean SBOM + compatible licenses + attestation |
| **Release** | Release Manager (HITL) | All prior gates green; changelog + version + rollback plan present | Human sign-off |

**Anti-gaming controls** (because agents will optimize to pass the gate):

- Acceptance tests derived from criteria are **immutable** to the implementing agent; only QA may change them, and changes are reviewed.
- The Reviewer explicitly inspects **test diffs** to catch weakened assertions.
- **Mutation testing** verifies the tests actually constrain behavior.
- The **Guardian** flags trajectories that touch gate logic or test oracles.

## 2. The five-plane guardrail model

The enterprise-standard architecture for agentic safety, applied to a coding
factory.

### Policy plane
- Policy-as-code (OPA) authorizes every tool call: *which* role may call *which*
  tool with *which* arguments. Least privilege per role (a Junior Engineer's
  git scope ≠ a Release Manager's).
- Denials are logged and surfaced to the Guardian.

### Execution plane
- **All agent-written code runs in sandboxes** — containers by default,
  Firecracker/gVisor microVMs for stronger isolation. Never on the host.
- Network egress from sandboxes is default-deny with an allowlist.
- Sandboxes are ephemeral and reset between tasks.

### Data plane
- Secret management (scoped, short-lived credentials injected per task; never in
  prompts or state).
- PII/secret redaction on inputs and traces.
- The artifact store enforces access scoping by role.

### Observability plane
- **OpenTelemetry** traces every super-step: node, tool, arguments, tokens,
  USD, latency, and gate results.
- Traces feed (a) the Guardian and Budget Controller in real time and (b) the
  eval harness offline.
- Every run is fully replayable from the checkpointer.

### Assurance plane
- **SBOM** per build; **SLSA** provenance attestation; artifact **signing**.
- **Red-teaming** of the factory (prompt injection, tool abuse, sandbox escape).
- **Evals run in CI** on every change to prompts, graph, or tools (see
  [evaluation.md](./evaluation.md)).

## 3. Untrusted-content handling

Requests, issue text, fetched web pages, and dependency READMEs are **untrusted
input** and a prompt-injection vector. Rules:

- Untrusted content is clearly delimited and never elevated to instructions.
- Tools that act on the outside world require policy-plane authorization and,
  for high-impact actions, HITL.
- The Guardian watches for trajectory hijacking (sudden goal/scope changes).

## 4. Compliance & audit trail

- Every decision, gate result, and human sign-off is appended to an immutable
  audit log (the `history` + `human_decisions` in state, persisted).
- The Audit/Compliance agent maps changes to applicable controls; domain packs
  add specific frameworks.

## 5. Domain packs

A **domain pack** extends governance without touching the core:

| Pack | Adds specialist | Adds gates / controls |
|---|---|---|
| Fintech | Compliance specialist | PCI-DSS / SOX checks in Audit gate; stricter crypto review |
| Healthcare | Privacy specialist | HIPAA mapping; PHI data-plane rules |
| ML / Data | ML engineer | Data-validation + model-eval gate; dataset license checks |
| Mobile / Embedded | Platform specialist | Platform build/sign gates; resource budgets |

A pack is registered by declaring its `capabilities`; the supervisor routes to
it when the PM/Architect's `required_capabilities` intersect the registry.

## 6. Failure & escalation policy

1. Gate fails → route to responsible role, increment retry counter.
2. Retry cap reached → Human Liaison escalation with full context.
3. Guardian trip (loop / policy / cost / reward-hacking) → halt + escalate.
4. Budget cap breach → downgrade tier or pause + escalate.
5. All escalations are resumable from the last checkpoint after a human
   decision is recorded.
