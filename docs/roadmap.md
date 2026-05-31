# Roadmap — phased delivery with exit criteria

Each phase ends with explicit, testable exit criteria. We do not start a phase
until the previous phase's criteria are met. Every phase ships behind the gates
and observability it introduces.

## Phase 0 — Foundations
**Goal:** a single agent can run a task safely, observably, resumably, and
without corrupting the outside world on retry.

- Repo scaffold; **reducer-annotated `FactoryState`** (TypedDict) schema; LangGraph skeleton with a checkpointer (SQLite dev).
- One worker node (engineer) with sandboxed code execution on an isolated branch.
- **External-action ledger** with idempotency keys for git/PR side effects.
- OpenTelemetry tracing of every tool call (tokens, USD, latency).
- Policy plane stub (least-privilege tool allowlist).

**Exit criteria:** a trivial task runs end-to-end, produces a traced run,
persists checkpoints, **and a kill-then-resume produces no duplicate commit/PR**
(idempotency proven).

## Phase 1 — Core vertical slice
**Goal:** the smallest real org that builds a real (small) app — and a first
read on the central hypothesis.

- Product Manager → Principal Architect → Senior Engineer → QA, wired as single-entry/single-exit subgraphs.
- Spec, **Build, and Test** gates (distinct) with deterministic decision rules; criterion-traceable tests; flake quarantine.
- Content-addressed, versioned artifact store as the communication medium.
- **First single-agent A/B smoke test** on a handful of golden tasks.

**Exit criteria:** from a one-line request, the pipeline produces a small working
app that passes its own generated tests **and hidden tests** behind all gates,
with no human edits to code; the A/B harness runs and reports (even if Foundry
doesn't yet win — that's data).

## Phase 2 — Review & assurance
**Goal:** quality and safety, not just "it runs."

- Reviewer/Critic + evaluator-optimizer loop with bounded retries; **separated test authorship**.
- Security/AppSec and Audit/Compliance agents; Security + Audit gates.
- **Trusted CI builder** producing SBOM + SLSA provenance + keyless signature (keys air-gapped from sandboxes).
- HITL interrupts at Design and Release gates via the Human Liaison; **WORM audit store**.

**Exit criteria:** a known-vulnerable change is caught and force-fixed by the
Security gate; a design and a release each require and record human sign-off in
the WORM store; the re-verification rule is enforced (a security fix re-runs
Build+Test before re-entering downstream gates); rework loops terminate within
the retry cap.

## Phase 3 — Operations & scale
**Goal:** parallelism with correct integration, deployment, and the full
guardrail stack.

- **Branch-per-task parallelism + deterministic integration node** with conflict resolution; per-branch and integrated gates.
- DevOps/SRE + Release Manager: CI/CD, IaC, observability config, idempotent deploy + rollback.
- **Deterministic monitors** (loop/cost/policy/retry-until-green) + **sandboxed analyzer**; all five guardrail planes operational; per-phase/per-loop budgets with graceful checkpoint-and-pause.

**Exit criteria:** a multi-task feature is implemented in parallel on isolated
branches, integrated (including a resolved merge conflict), deployed to a sandbox
environment with a working rollback, under enforced sub-budgets, with the
deterministic monitor able to halt a deliberately looping run and the analyzer
flagging an injected scope-creep trajectory.

## Phase 4 — Specialist plugin system
**Goal:** general core, expert on demand.

- Specialist interface (`capabilities`, `tools`, `prompts`, `gates`, `evals`) + registry; conditional-edge routing.
- First two domain packs (e.g., ML/Data and Fintech) with their extra gates.

**Exit criteria:** the same core, given a domain request, auto-activates the
right specialist and its extra gates without core changes; a non-domain request
ignores them and downstream gates tolerate the absent domain artifacts.

## Phase 5 — Eval & optimization
**Goal:** measured, tuned, hardened — and the central bet decided.

- Full eval harness: golden suite (hidden tests) + SWE-bench-Verified-style + SWE-EVO-style long-horizon, with deterministic mocks in CI.
- Headline scorecard with **concrete thresholds** tracked over time; regressions block changes.
- **Decisive single-agent A/B** on a held-out SWE-EVO subset at equal budget.
- Constrained workflow optimization (search space + objective + method per [evaluation.md §6](./evaluation.md)); red-team pass; cost/latency hardening.

**Exit criteria:** the scorecard is published and stable across reruns;
**Foundry beats the single-agent baseline by the pre-registered margin at ≤ equal
cost** (or the design is revised); optimization demonstrably Pareto-improves at
least one headline metric with no regression in escaped defects, cost, or policy
compliance.

## Sequencing notes

- **Reducers, the side-effect ledger, observability, and the policy plane are
  Phase 0 foundations**, not bolted on — every later phase depends on correct
  concurrency, idempotent resume, traces, and least-privilege tools.
- **Gates are added with the role that owns them**, so the pipeline is always
  shippable, never a big-bang integration.
- **The A/B is run early (Phase 1) and decisively (Phase 5)** so the central
  hypothesis is continuously tested, not asserted at the end.
