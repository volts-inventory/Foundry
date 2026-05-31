# Governance — gates, guardrails, security & compliance

Foundry's reliability comes from putting **a deterministic decision rule at the
seams**: LLMs do judgment *inside* nodes, but the PASS/FAIL *rule* between nodes
is a deterministic function of observed outputs wherever an oracle exists. This
document defines the quality gates (and an honest account of where execution is
*not* deterministic), the five-plane guardrail model, the monitor design, the
supply-chain trust root, the budget controls, and the domain-pack mechanism.

## 1. Quality gates

A gate is a node that returns `pass | fail | escalate` and writes a `GateResult`
recording **both** whether the *decision rule* was deterministic and whether the
underlying *execution* was reproducible (see [architecture.md §3](./architecture.md)).
Failures route back to the responsible role with a bounded retry counter;
exceeding the cap escalates to the Human Liaison. Per the re-verification rule,
any code change re-enters the Build and Test gates before any downstream gate.

| Gate | Owner | Runs against | Decision rule | Pass condition |
|---|---|---|---|---|
| **Spec** | Product Manager | PRD | deterministic (structural) + LLM check | Every story has testable Given/When/Then criteria (bugfixes: a repro + expected behavior) |
| **Design** | Architect (+HITL) | design artifacts | LLM + human | ADRs present; interfaces & data model defined; human sign-off for significant designs |
| **Build (per-branch + integrated)** | CI runner | task branch, then merged tree | deterministic | Compiles, lints, type-checks: zero errors |
| **Test (per-branch + integrated)** | QA | task branch, then merged tree | deterministic decision over possibly-flaky execution | All tests green; **every acceptance criterion exercised** (criterion-coverage); line coverage ≥ threshold; no quarantined test covering a high-risk `criterion_id`, and any other quarantine has recorded QA acceptance + rationale |
| **Security** | AppSec | merged tree | deterministic scans + LLM triage | No unresolved high/critical (SAST, secret, SCA); threat-model items resolved |
| **Audit** | Compliance | merged tree | deterministic | SBOM has no disallowed-license or critical-CVE deps; license compatibility confirmed |
| **E2E** | SDET | integrated tree | deterministic decision over possibly-flaky execution | End-to-end scenarios green (flake policy of §1a applies) |
| **Release** | Release Manager (HITL) | **already-attested artifact** | human | All prior gates green; SLSA provenance + signature present (produced by the trusted builder *before* this gate); changelog + version + rollback plan present |

Note the split into distinct **Build** and **Test** gates (reconciling the
earlier inconsistency), each run **twice**: once per task branch (fast,
attributable) and once on the integrated tree (authoritative for release).

### 1a. Execution is not always deterministic — and that's handled

"Deterministic gate" means the **decision rule** is a deterministic function of
outputs. The **outputs** (real builds/tests in sandboxes) often are not:

- **Flake policy:** tests run with pinned seeds, frozen clock, and no live
  network (mocked). A test that fails then passes within a retry budget of *N=2*
  is marked **flaky → quarantined** (not silently re-run to green). A quarantined
  test covering a **high-risk `criterion_id` blocks the gate** until QA fixes it;
  any other quarantine requires **recorded QA acceptance with rationale** to pass
  (identical rule to the §1 Test-gate pass condition).
- **Anti-"retry-until-green":** the Guardian (§3) keys on repeated identical
  gate invocations with differing results and flags it as a reward-hacking
  signal; flake retries are metered against the loop budget.
- **Mutation testing is off the hot path:** it runs on a schedule / at the
  integrated gate only (not on every per-branch build), because it is expensive
  and itself timeout-sensitive. It validates test *sensitivity*, not criterion
  coverage.

### 1b. Test-authoring trust (breaking the LLM-judges-itself loop)

Letting one LLM both write and bless the tests is circular. Controls:

- **Separation of authorship:** the agent/model that **authors acceptance
  tests is different** from the one that implements the code (different role,
  and a different model instance/tier), reducing collusion.
- **Criterion traceability:** every acceptance criterion maps to ≥1 test
  (`criterion_id` tag); the Test gate enforces **criterion-coverage** (all
  criteria exercised) in addition to line coverage. Untraceable tests don't
  count toward the gate.
- **Stronger oracles for high-stakes criteria:** property-based tests and/or
  human-seeded golden oracles for criteria the PRD marks high-risk.
- **Review of test diffs:** the Reviewer inspects test changes for weakened
  assertions; mutation testing (§1a) backs this up.

## 2. The five-plane guardrail model

### Policy plane
- Policy-as-code (OPA) authorizes every tool call: *which* role may call *which*
  tool with *which* arguments. Least privilege per role (a Junior Engineer's git
  scope ≠ a Release Manager's; only the trusted CI builder can reach signing).
- Denials are logged and counted; the count is a deterministic Guardian signal.

### Execution plane
- **All agent-written code runs in sandboxes** — containers by default,
  Firecracker/gVisor microVMs for stronger isolation. Never on the host.
- Egress is default-deny with an allowlist; tests run with network mocked.
- Sandboxes are ephemeral and reset between tasks and between fix retries.

### Data plane
- Scoped, short-lived credentials injected per task; **never in prompts or
  state**. Signing keys are *not* reachable from any agent sandbox (§4).
- PII/secret redaction on inputs and traces.
- The content-addressed artifact store enforces access scoping by role.

### Observability plane
- **OpenTelemetry** traces every super-step: node, tool, arguments, tokens, USD,
  latency, gate results, and policy denials.
- Traces feed (a) the deterministic monitors and analyzer in real time and
  (b) the eval harness offline. Every run is replayable from the checkpointer.

### Assurance plane
- **SBOM** per build; **SLSA** provenance; artifact **signing** — all produced
  by a *trusted builder*, not the agents (§4).
- **Red-teaming** of the factory (prompt injection, tool abuse, sandbox escape).
- **Evals run in CI** on every change to prompts, graph, or tools (see
  [evaluation.md](./evaluation.md)).

## 3. Monitor design (deterministic monitors + sandboxed analyzer)

"Guardian" is not one vague LLM. It is split:

- **Deterministic monitors (no LLM, cannot be prompt-injected):**
  - *Loop detection* — a repeated **state signature** (hash of phase + open
    task + gate name + diff hash) over a window ⇒ loop ⇒ pause + escalate.
  - *Cost* — `BudgetLedger` threshold crossings (§7) ⇒ pause/escalate.
  - *Policy* — OPA deny-count threshold ⇒ halt.
  - *Retry-until-green* — same gate, same input signature, oscillating result.
- **Sandboxed LLM analyzer (semantic concerns only):** runs read-only over
  redacted traces in its **own sandbox with no tools**, so it does not share the
  acting agents' injection/tool surface. It keys on explicit features
  (unexpected scope change, edits to test oracles or gate logic, acquiring new
  capabilities mid-task) and emits *advisory* flags to the Human Liaison; it
  **cannot** itself approve work. The analyzer has its own evals
  ([evaluation.md](./evaluation.md)) and its token cost is metered as
  `monitor_overhead_usd`.

This removes the false assurance of a single mid-tier LLM "watching for
reward-hacking": the cheap, reliable detections are deterministic; the LLM does
only what requires judgment, in isolation.

## 4. Supply-chain trust root (signing & provenance)

An attestation produced inside an agent-controlled sandbox attests nothing.
Therefore:

- **Release builds are produced by a trusted, hermetic CI builder** outside any
  agent's control (the `ATTEST` node in [architecture.md §1](./architecture.md)),
  on a pinned base image with locked dependencies — not by the implementing
  agent's sandbox.
- **Keyless signing (OIDC / sigstore):** no long-lived signing keys exist for an
  agent to exfiltrate; identity comes from the CI workload's OIDC token. If
  long-lived keys are unavoidable, they live in an HSM/KMS the policy plane
  forbids any agent sandbox from reaching.
- **Provenance predicate:** SLSA provenance records the source commit, the
  builder identity, the hermetic build inputs, and the materials. Target:
  **SLSA Build L3** (hardened, non-falsifiable provenance).
- **SBOM pass criteria (concrete):** no dependency with a known critical/high
  CVE (configurable severity floor), no dependency under a disallowed license,
  full dependency resolution (no unpinned/unknown components).

## 5. Domain packs

A **domain pack** extends governance without touching the core. A Specialist is
a graph node implementing the interface `{capabilities, tools, prompts/SOP,
gates, evals}`. A **registry** maps required capabilities to specialists; the
supervisor routes via the conditional edge in [architecture.md §7](./architecture.md).

| Pack | Adds specialist | Adds gates / controls |
|---|---|---|
| Fintech | Compliance specialist | PCI-DSS / SOX checks in Audit gate; stricter crypto review |
| Healthcare | Privacy specialist | HIPAA mapping; PHI data-plane rules |
| ML / Data | ML engineer | Data-validation + model-eval gate; dataset license checks |
| Mobile / Embedded | Platform specialist | Platform build/sign gates; resource budgets |

## 6. Failure & escalation policy

1. Gate fails → route to responsible role, increment retry counter.
2. Code changed during a fix → re-enter Build + Test gates before any downstream
   gate (re-verification rule).
3. Retry cap reached → Human Liaison escalation with full context.
4. Monitor trip (loop / policy / cost / retry-until-green) → pause + escalate;
   analyzer flags are advisory to the human.
5. Budget cap breach → see §7 (T1 escalates, never downgrades).
6. All escalations resume from the last checkpoint after a human decision is
   recorded; effectful actions are not re-fired (external-action ledger).

## 7. Budget controls (concrete)

A single whole-run cap is not a control. The `BudgetLedger`
([architecture.md §3](./architecture.md)) carries:

- **Per-phase sub-budgets** with defaults (illustrative, tuned in Phase 5):
  Product 5% (Triage/Planning are negligible T2/T3 calls counted here),
  Architecture 15%, Implementation 35%, Verification 15%, Assurance 10%, Release
  5%, **Rework reserve 10%**, monitor overhead ≤ 5% — summing to 100% *with* a
  contingency line. The Eng/Program Manager's estimate sets the absolute cap; the
  percentages bound distribution. **Triage seeds a default per-route cap** so
  every route is bounded even before PLAN runs; PLAN refines it on the routes it
  visits (greenfield/evolution).
- **Per-loop caps:** each evaluator-optimizer loop and each fix loop has a max
  spend/iteration count and **draws from the Rework reserve**, not the owning
  phase's budget — so the designed-in rework path does not automatically breach a
  phase cap. Exhausting the reserve escalates rather than looping.
- **Mid-flight behavior:** on cap approach the controller **gracefully
  checkpoints and pauses** in-flight branches (sandbox jobs are signaled to
  checkpoint, not hard-killed mid-write); on hard breach it pauses the whole run
  and escalates. Partial work is preserved on branches.
- **No tier downgrade on T1 nodes:** architecture, security, and review never
  auto-downgrade to save money; a breach there escalates to a human decision.
  Downgrade is permitted only on T2/T3 implementation/routing nodes.
- **Monitor overhead is metered** as its own ledger line so the cost of
  observability is visible and bounded (≤ 5% target).
- **Caching:** stable system prompts/SOPs and pinned reference artifacts use
  prompt-cache breakpoints; expected reuse across a run's many node calls is the
  primary token-cost reduction lever.
