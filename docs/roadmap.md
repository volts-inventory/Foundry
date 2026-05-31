# Roadmap — phased delivery with exit criteria

Each phase ends with explicit, testable exit criteria. We do not start a phase
until the previous phase's criteria are met. Every phase ships behind the gates
and observability it introduces.

## Phase 0 — Foundations
**Goal:** a single agent can run a task safely, observably, and resumably.

- Repo scaffold; `FactoryState` (Pydantic) schema; LangGraph skeleton with a checkpointer (SQLite dev).
- One worker node (engineer) with sandboxed code execution.
- OpenTelemetry tracing of every tool call (tokens, USD, latency).
- Policy plane stub (least-privilege tool allowlist).

**Exit criteria:** a trivial task ("write function + test") runs end-to-end,
produces a traced run, persists checkpoints, and is resumable after a kill.

## Phase 1 — Core vertical slice
**Goal:** the smallest real org that builds a real (small) app.

- Product Manager → Principal Architect → Senior Engineer → QA, wired as subgraphs.
- Spec, Build, and Test gates with deterministic oracles.
- Artifact store (PRD, ADR, interfaces, test report, diff) as the communication medium.

**Exit criteria:** from a one-line request, the pipeline produces a small
working app that passes its own generated tests behind all three gates, with no
human edits to code.

## Phase 2 — Review & assurance
**Goal:** quality and safety, not just "it runs."

- Reviewer/Critic + evaluator-optimizer loop (engineer ⇄ review) with bounded retries.
- Security/AppSec and Audit/Compliance agents; Security + Audit gates; SBOM + SLSA attestation.
- HITL interrupts at Design and Release gates via the Human Liaison.

**Exit criteria:** a known-vulnerable change is caught and force-fixed by the
Security gate; a design and a release each require and record human sign-off;
rework loops terminate within the retry cap.

## Phase 3 — Operations & scale
**Goal:** parallelism, deployment, and the full guardrail stack.

- Parallelization: concurrent independent tasks; parallel security/audit fan-out.
- DevOps/SRE + Release Manager: CI/CD, IaC, observability config, deploy + rollback.
- Guardian + Budget Controller online; all five guardrail planes operational.

**Exit criteria:** a multi-task feature is implemented partly in parallel,
deployed to a sandbox environment with a working rollback, under an enforced
budget cap, with the Guardian able to halt a deliberately looping run.

## Phase 4 — Specialist plugin system
**Goal:** general core, expert on demand.

- Specialist interface (`capabilities`, `tools`, `prompts`, `gates`, `evals`) + registry.
- Capability routing from `required_capabilities`.
- First two domain packs (e.g., ML/Data and Fintech) with their extra gates.

**Exit criteria:** the same core, given a domain request, auto-activates the
right specialist and its extra gates without core changes; a non-domain request
ignores them.

## Phase 5 — Eval & optimization
**Goal:** measured, tuned, and hardened.

- Full eval harness: golden suite + SWE-bench-Verified-style + SWE-EVO-style long-horizon, with deterministic mocks in CI.
- Headline scorecard tracked over time; regressions block changes.
- AFlow-style workflow optimization (prompts, tiers, topology) gated on the scorecard.
- Red-team pass; cost/latency hardening.

**Exit criteria:** the scorecard is published and stable across reruns;
optimization demonstrably improves at least one headline metric with no
regression in escaped-defects, cost, or policy compliance.

## Sequencing notes

- **Observability and the policy plane are built in Phase 0**, not bolted on —
  every later phase depends on traces and least-privilege tools.
- **Gates are added with the role that owns them**, so the pipeline is always
  shippable, never a big-bang integration.
- **Evals (Phase 5) consume the traces from Phase 0**, so trajectory metrics are
  available the whole way, even before the formal harness exists.
