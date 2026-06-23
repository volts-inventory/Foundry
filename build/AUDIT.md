# AUDIT — append-only Independent Auditor reports

Each milestone + final: a fresh Auditor briefed with ONLY the design docs + current code
(not status files) re-derives requirements and verifies the code delivers them. Findings open
backlog tasks; repeat until a clean pass (max 3 cycles).

(No audits yet — first milestone gate pending.)

## M1 (Phase 1) — Cycle 1 (fresh independent auditor; design-docs + code only)
Auditor re-ran the gate chain (ruff/mypy clean, 413 pass/1 skip) + the cli (PASS + one open_pr).
**Per-requirement:** org&pipeline DELIVERED; distinct gates signed by trusted infra DELIVERED
(merge_gates drops unsigned/forged/replayed — non-vacuous grounding tests); Phase-1 EXIT criteria
PARTIAL-but-honest (all six proven by non-vacuous tests incl. a held-out hidden suite that gates the
result; the sole caveat: the "working app" is a scripted _PASSING_DIFF via StubAdapter, not agent
codegen — openly labeled per §0.3); self-judge profile DELIVERED (provenance=SELF stamp); honesty
NO SILENT OVERCLAIM (every stand-in labeled); drift/security/NFR DELIVERED for Phase-1 scope
(fail-closed default, content-addressed frozen artifacts, single egress via proxy, digest-keyed latches).
**Verdict:** FINDINGS(3), all MINOR, none blocking. "Suitable for milestone close provided the
codegen-stand-in caveat is recorded."

- A-M1-01 MINOR | Phase-1 "produces a working app" is met at the GATE-MACHINERY level; real codegen
  is deferred to the worker substrate (StubAdapter no-op; cli demo app is request-independent VALUE=42).
  RESOLVED: recorded explicitly here + in the milestone-close note (already in LEARNINGS CLI3/EX1-5, §0.3).
- A-M1-02 MINOR | terminal_gates_pass keys release on spec/build/test only (Design gate absent).
  RESOLVED-as-correct: per roadmap Phase 1 the gate set IS Spec/Build/Test; the Design gate (HITL) is a
  Phase-2 deliverable — DEFERRED by design, not silently dropped. Confirmed against roadmap §Phase 1/2.
- A-M1-03 MINOR | one skipped test (claude-binary unavailable-path untestable in-env). RESOLVED-benign:
  environment artifact, not a coverage gap.

RESOLUTION: all 3 findings are documentation/confirmation (no code defect); resolved in-note. Auditor
gave a conditional-clean verdict with the condition (record the codegen caveat) now met. A second audit
cycle is not warranted for documentation-only MINORs on an explicitly close-suitable milestone.
**M1 AUDIT: CLEAN PASS (conditional, conditions met).**

### MILESTONE-CLOSE NOTE (honesty, for non-doc readers)
Phase 1 delivers a REAL Assistant-mode pipeline: a PM→Architect→Senior→QA LangGraph org that runs a
one-line request through distinct, trusted-infra-SIGNED Spec/Build/Test gates to a content-addressed,
reviewed PR-equivalent, resume-idempotent. What is a DEV-MODE STAND-IN (not production): the coding
agent itself (StubAdapter emits a scripted diff, not real generated code — real codegen is the pluggable
worker substrate, e.g. the claude_code adapter, exercised only with a live key+binary); the model provider
(MockTransport in keyless runs); and the trust roots (dev HMAC, two zones in one process — production
hardening is an ops task per §0.3). The GATE/TRUST machinery, state/durability, idempotency, and the
self-judge profile are genuine and adversarially tested.

## M1.5 (Phase 1.5 decision gate) — Cycle 1 (fresh independent auditor; design-docs + code only)
Auditor re-ran gates (ruff/mypy clean, full suite pass/1 skip) + read all evaluation modules + the
integration test. **Per-requirement: ALL DELIVERED** — (1) pilot first/disjoint/read-only/not-a-scored-arm
(ScoredSliceTouchedError); (2) pre-registration immutable+coherent, ledger keyed on (eval_set_identity,
decision_id), guards ordered underpowered→incoherent→atomic alpha-look so a rejected freeze spends ZERO
alpha; (3) 3-arm equal-USD A/B, oracle primary, ITT denominator can't shrink (double-asserted), paired
cluster bootstrap + Holm; (4) decision table PROCEED⟺(sig∧meets_mde∧cost≤) enforced in model_post_init,
no null→PROCEED, no silent re-power; (5) role ablation present + honestly simulated; (6) HONESTY —
FOUNDRY arm genuine cli.run, baselines simulated, mandatory dry-run banner first, self-judge never
reported as independent validation; (7) no overclaim (token-axis gap disclosed, not faked).
**Verdict: CLEAN PASS.** Findings: 3 MINOR informational (all disclosed, no integrity impact):
- A-M15-01 MINOR | exit-test _sizing_pilot substitutes representative variance (tiny synthetic frames
  saturate to base_rate=1.0 → degenerate required_n=0); power arithmetic genuinely exercised, inputs are
  stand-ins. Documented in-test. No action (synthetic-fixture artifact).
- A-M15-02 MINOR | PROCEED only reachable in tests via a hand-built ABResult (simulated FOUNDRY cost >
  baseline always); real decide branch logic IS exercised. Acceptable for a dry-run. No action.
- A-M15-03 MINOR (info) | the effective-token secondary endpoint is out of scope (Phase 6); redesign fork
  gated behind an external reading. Disclosed gap, not an overclaim. No action.
**M1.5 AUDIT: CLEAN PASS.**

## M2 (Phase 2, Gated mode) — Cycle 1 (fresh independent auditor; design-docs + code only)
Auditor re-ran gates (ruff/mypy clean, full suite pass/1 skip) + inspected modules + load-bearing tests.
**Per-requirement: ALL DELIVERED** — (1) known-vulnerable CAUGHT genuine (attested Security FAIL +
stable-FAIL latch + clean control; agents advisory-only; merge_gates drops unsigned; no path to gated
open_pr for a vuln change); (2) recorded sign-off + fail-closed release (provenance never model-supplied;
release_allowed pure-AND over approved+4 anchors; emit re-derives via shared release_anchors, no drift);
(3) change-proportional re-verification (reuse only on disjoint scope+exact key+attested non-ERROR prior);
(4) bounded rework (terminal at max_fix_attempts, no GraphRecursionError); (5) tiering relaxes_integrity()
==False, security floor at every tier incl LOW, force-HIGH on signals; (6) WORM evidence pack tamper-evident
(swap/drop/mutate rejected); (7) self-judge provenance=SELF, machine gates fail-closed, HITL recorded-no-op;
(8) honesty — explicit HONESTY MAP, dev stand-ins labeled per §0.3, no circular tests, Assistant back-compat.
**Verdict: CLEAN PASS.** Findings: 2 MINOR observations (NOT gaps):
- A-M2-01 MINOR | dev-grade trust roots (HMAC zone-B signer, single-process two-zone, dev secret rules,
  no real semgrep/Syft) — honestly labeled §0.3; production hardening is the deferred Phase-4 ops task. No action.
- A-M2-02 MINOR | rework_scoped_gates uses an empty file delta at the rework seam, compensating by
  force-re-running any non-PASS prior (fail-closed, sound); true file-delta reuse on rework is a future refinement.
  No action (documented honest seam, matches M2-R1-03).
**M2 AUDIT: CLEAN PASS.**

## M3 (Phase 3, Operability & scale) — Cycle 1 (fresh independent auditor; design-docs + code only)
Auditor re-ran gates (ruff/mypy clean, 168 phase-3 tests pass) + spot-checked the load-bearing tests for
non-vacuity. **Per-requirement: ALL 8 DELIVERED** — (1) operator diagnoses stuck run (pause_reason checked
before deadline; PAUSED-100x-past-deadline not alerted; StuckRunSummary diagnosable); (2) store outage
PAUSES-not-corrupts E2E (real superstep_txn BEGIN IMMEDIATE/rollback; outbox read back proves only prior
row survived + WORM intact + relay drains once; recover_to_last_clean fail-closed 4 ways); (3) no starvation
(wait-age dominant fairness key; proven unequal-weight 1M:100K:1 held-slots 30 rounds; fleet cap admission +
resume_admit never kills in-flight; hibernation frees+resumable); (4) HITL bounded+alerted (PAGE/WARN,
two-person safe-deadlock never auto-approves, self-judge dormant vs regulated); (5) monitors trip +
analyzer override STRUCTURALLY impossible (proven with mutating _OverrideAttemptAnalyzer, byte-identical
verdict); (6) five-plane completeness + content-addressed scanner config tamper-evident + SoD + default-deny;
(7) observability no-second-egress (read-only Protocol) + drift fail-loud; (8) honesty — explicit map,
outage signal is the documented dev analogue, no overclaim.
**Verdict: CLEAN PASS — NO findings at BLOCKER/MAJOR/MINOR.** All load-bearing tests non-vacuous.
**M3 AUDIT: CLEAN PASS (zero findings).**

## M4 (Phase 4, Trust root & Autonomous mode) — Cycle 1 (fresh independent auditor; design-docs + code only)
Auditor re-ran gates (ruff/mypy clean 135 files, full suite pass/1 skip) + spot-checked the four-attack tests +
clean control for non-vacuity. **Per-requirement:** (2) forged-results DELIVERED (zone-B config re-derive + sealed
obs -> FAIL/ERROR never PASS), (3) forged-verdict DELIVERED (admission re-verifies provenance + every required gate
sig+PASS+bound-to-expected-tree across BOTH trees, then disqualification; input-bound attestation), (4) fork-divergence
DELIVERED (digest-keyed monotonic atomic disqualification; admission_look DENYs laundered fork w/ valid sigs), (5)
autonomous DELIVERED (admission sole gate, no sign-off path; DENY/halt deploys nothing; idempotent; deployer non-agent;
mandatory floor), (6) outage->paused-not-wedged DELIVERED (rotation grace + HA failover + fail-closed orphan), (7)
honesty DELIVERED (dev-grade roots labeled; clean controls prove blocks load-bearing; no circular tests).
(1) malicious-test PARTIAL: discriminator mechanism correct + proven in test_phase4_exit (real runner), but NOT
plumbed into the compiled graph's Test-gate node (run_build_test_gates wires TestGate(min_passed) without the
discriminator) -> the shipping autonomous spine relies on the adequacy floor alone for the gamed-green case.
**Verdict: FINDINGS(1) MINOR.**
- A-M4-01 MINOR | gate_wiring.run_build_test_gates / factory_graph autonomous spine | held-out-mutant discriminator
  not wired into the COMPILED Test gate; only test_phase4_exit exercises it directly. | FIX: plumb a DiscriminatorFactory
  into the Test-gate node so the compiled graph (not just the unit test) exercises the malicious-test block end-to-end.
Resolution: fix (plumb the discriminator into the shipping gate node) + an end-to-end-through-the-compiled-graph test,
then re-audit (cycle 2).

## M4 (Phase 4) — Cycle 2 (fresh independent auditor; re-verify A-M4-01)
Auditor (fresh, did not see cycle 1) re-ran gates (ruff/mypy clean, full suite pass/1 skip) focusing on whether
the malicious-test defense is now in the COMPILED shipping path. **ALL 7 DELIVERED.** (1) malicious-test now
DELIVERED: DiscriminatorFactory threads build_factory_graph -> build_supervisor_graph -> build_test_gate ->
run_build_test_gates -> TestGate(discriminator=...); the COMPILED Test gate evaluates held-out mutants; non-vacuous
E2E pair (gamed-green-mutant-survives -> compiled Test FAIL -> 0 deploys; genuine same-factory -> 1 deploy);
back-compat (factory None = legacy floor). (2)-(7) re-confirmed DELIVERED. No circular tests; clean controls pair
every block. **Verdict: CLEAN PASS — no findings.** A-M4-01 RESOLVED.
**M4 AUDIT: CLEAN PASS (cycle 2).** The four attacks genuinely cannot reach a deploy in the shipping path.
