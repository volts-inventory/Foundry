# SPEC — Foundry (orchestration layer over the design docs)

The authoritative product/architecture specification is the **design docs** already in
the repo. This file is the orchestrator's thin pointer + the testable acceptance
criteria + the smoke-test core path. It does NOT re-open or duplicate the design.

## Authoritative design sources (read these for any detail)
- `PLAN.md` — master plan (problem, thesis, architecture, stack, roadmap).
- `docs/architecture.md` — graph topology, state model, orchestration patterns.
- `docs/agents.md` — the catalog of role agents, SOPs, I/O contracts.
- `docs/governance.md` — quality gates, the 5-plane guardrail model, security/compliance.
- `docs/evaluation.md` — how the factory's effectiveness is measured (A/B, scorecard).
- `docs/roadmap.md` — phased delivery + **per-phase exit criteria** (the acceptance criteria).
- `docs/operations.md` — Day-2 operability.
- `docs/lifecycle.md` — product lifecycle of generated code.
- `docs/domains.md` — verdict-class taxonomy + domain-pack contract + packs.
- `docs/implementation-plan.md` — **build-ready plan**: pinned stack, repo layout, schemas,
  per-component interfaces, phase task backlog, topological build order (§8).
- `docs/iteration-log.md`, `docs/build-log.md` — review record + live build status.

## What "the whole application" means here
The build target is the **entire system in the docs, Phases 0–6** (implementation-plan §7),
implemented as **faithful, tested, runnable Python software** with **dev-grade trust
roots** where production hardening is explicitly an ops deployment task (implementation-plan
§0.3). "Implemented" never means "production-deployed"; it means real code + real tests that
exercise the logic against local/dev backends.

## Chosen operating profile (implementation-plan §0.2)
**Single-lineage (Anthropic) self-judge, zero-HITL, fully autonomous.** Machine-grounded
gates stay fail-closed and unchanged; only *judgment* seams (subjective Review residual,
threat residual, perceptual class-5) get a same-lineage judge that stamps
`judgment_provenance = "self"`. The decorrelation/HITL machinery is still BUILT (so an
operator can flip back, and the regulated pack can force HITL on).

## SMOKE-TEST CORE PATH (primary user flow)
From a one-line natural-language request, the compiled factory graph runs
**intake → product (PRD + acceptance criteria) → architecture (design + pinned
constraints) → implementation (FeatureDiff via the worker-substrate adapter) →
quality (tests + review) → gates (Spec/Build/Test) → emit (content-addressed artifact +
PR-equivalent external action via the outbox)**, checkpointed so a kill-then-resume
produces no duplicate external effect. Phase-1 smoke: `foundry/cli.py` takes a request,
runs the graph end-to-end with a mock/test model provider + a stub worker substrate, and
the SPEC core path completes producing a reviewed artifact behind passing gates.

## Acceptance criteria = the per-phase EXIT criteria (docs/roadmap.md)
Each milestone's acceptance criteria are the roadmap Exit criteria for that phase,
made testable in BACKLOG.json. Summary (see roadmap.md for the binding text):

- **Phase 0 (DONE):** trivial task runs end-to-end, traced, checkpointed; kill-then-resume
  produces no duplicate commit/PR; reducer-purity + idempotency property tests pass.
- **Phase 1 (Assistant mode):** from a one-line request, produce a small working app passing
  its own + hidden tests, emitted as a PR-equivalent a human would merge with minimal edits;
  distinct Spec/Build/Test gates; criterion-traceable tests; flake quarantine.
- **Phase 1.5 (Decision gate):** variance/base-rate pilot; frozen pre-registration + decision
  table; powered A/B harness (3 arms, oracle-scored primary, ITT) + role ablation — built and
  exercised against local/synthetic task fixtures (not a funded live study).
- **Phase 2 (Gated mode):** a known-vulnerable change is caught; Design/Release require recorded
  sign-off; re-verification is change-proportional; rework loops terminate within caps.
- **Phase 3 (Operability):** operator can diagnose a stuck run; store outage pauses (not
  corrupts) and recovers; concurrent runs don't starve each other; HITL backlog bounded/alerted.
- **Phase 4 (Trust root / Autonomous):** malicious-test / forged-results / forged-verdict /
  fork-divergence cannot reach prod; trusted-component outage degrades to *paused*, not *wedged*.
- **Phase 5 (Specialists + domain packs):** a regulated request makes Autonomous/auto-approve
  unreachable + binds approver-of-record; an ML request keys verdicts on `state_sha` with a
  class-3 gate; a UI request forces visual HITL — all without core changes to add a pack.
- **Phase 6 (Eval/optimization/lifecycle):** scorecard published + stable; confirmatory A/B
  reproduces the Phase-1.5 result (estimation/replication only); optimization Pareto-improves
  ≥1 headline metric with no regression; product-feedback + maintenance/provenance loop.

## Non-functional / cross-cutting (from governance.md, operations.md)
- Fail-closed everywhere for admission + machine gates; fail-to-human only where a human is in
  the loop (disabled in the chosen profile except the regulated pack).
- Artifacts content-addressed + versioned, never mutated (supersession, not in-place edit).
- Determinism policy: temperature 0 for gates/routing/judges (note opus-4-8 has no temperature
  knob — record `effort` instead, per build-log); every node records model-id + fingerprint + seed.
- Five planes: policy / execution / data / observability / assurance.
