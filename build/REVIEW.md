# REVIEW — append-only critic ledger

Each finding: `[round] severity | location | description | resolution (fixed @commit /
dismissed-with-rationale)`. Critics are fresh agents who did not implement the work.

## Wave A (Phase 1: schemas + test doubles) — Round 1 (2 fresh critics)
Tool gates green before critics: ruff ✓, mypy foundry ✓, pytest green, cov 83%.
Critic verdicts: code-review FINDINGS(4, all MINOR); domain/trust FINDINGS(3, all MINOR).
Trust path CONFIRMED SOUND: doubles do not mint pre-signed verdicts — proxy extracts the
verdict from the constrained json_schema channel, the real ZoneBSigner stamps the binding,
and the real `merge_gates` admits it (nonce + negative-latch exercised). No BLOCKER/MAJOR.

- R1-01 MINOR | proxy/test_doubles.py | verdict-vs-JSON detection via `v.startswith("{")`
  mis-classifies leading-whitespace JSON → would crash `judge()`. (both critics) | FIX: json.loads-attempt / lstrip.
- R1-02 MINOR | proxy/test_doubles.py | `served_model` defaults to a constant TIER_MODELS["T1"]
  regardless of requested tier → docstring mismatch + silently exercises the drift branch on
  T2/T3 calls. | FIX: echo the requested tier's model id (no spurious drift) + assert no-drift.
- R1-03 MINOR | proxy/test_doubles.py | a matching ResponseScript outranks channel
  auto-detection → free-text script on the verdict channel crashes `judge()`. | FIX: document the
  constraint and/or make scripts respect verdict-channel detection.
- R1-04 MINOR | tests/test_test_doubles.py | distinct-tasks test has dead choreography (pointless
  `restore()`, asymmetric sandbox handling). | FIX: drop restore; symmetric two-sandbox compare.
- R1-05 MINOR | tests/test_test_doubles.py | no `isinstance(double, Protocol)` conformance assert
  (runtime_checkable) — structural drift (renamed run/send) would slip past. | FIX: add 1-line asserts.
- R1-06 MINOR | tests/test_test_doubles.py | "process-reproducible" test only compares within one
  process (PYTHONHASHSEED constant) — can't catch an L7 hash() regression. | FIX: note limit or subprocess-compare.

Resolution: all FIXABLE (high-leverage — doubles are the shared seam). Fresh batched fixer
(commit 84ac849) resolved all six 1:1; gates re-verified green (ruff/mypy clean, 286 pass/1 skip).
- R1-01 RESOLVED (whitespace-tolerant json.loads probe). R1-02 RESOLVED (served_model echoes
  requested tier + override kwarg). R1-03 RESOLVED (verdict channel outranks scripts; documented
  D6). R1-04 RESOLVED (symmetric two-sandbox test). R1-05 RESOLVED (Protocol-conformance asserts;
  ProviderTransport via local runtime_checkable mirror since proxy file is out-of-wave-scope).
  R1-06 RESOLVED (renamed in-process stability + L7 cross-process note).
ACCEPTANCE: all-MINOR round, fixes deterministically gate-verified and cross-checked 1:1 against
this ledger → CLEAN ROUND by orchestrator cross-check (mandatory fresh re-review is reserved for
BLOCKER/MAJOR). **Wave A ACCEPTED.**

## Wave B (Phase 1: four agent layers) — Round 1 (2 fresh critics)
Tool gates green before critics: ruff ✓, mypy foundry ✓ (77 files), full suite green, cov 85%.
Verdicts: code-review FINDINGS(5, MINOR); domain/trust FINDINGS(3, MINOR). 0 BLOCKER/MAJOR.
**Trust integrity + pinned-constraint fidelity CONFIRMED HOLD**: reviewer_critic only relays the
proxy.judge-stamped, nonce-bound, latch-checked GateResult (never signs its own); verdict_nonce
read fail-closed from config/ledger; junior_engineer injects pinned constraints verbatim + first.

- B-R1-01 (DT1) MINOR | proxy/test_doubles.py | verdict-channel detection treats ANY json_schema
  complete() as the verdict channel → structured ideation collides with verdict FIFO (the AC3/
  IM2/PR3 footgun). | FIX (central, high-leverage): narrow detection to the ACTUAL verdict schema
  (status-enum approve/request-changes), so general structured output rides the completions/script
  path; only judge() hits the verdict queue. Update all callers/tests that worked around it.
- B-R1-02 (CR4) MINOR | agents/architecture/principal_architect.py:226,253 | ADR/SystemDesign
  artifact uri = body digest, not content_identity() → two identical-body ADRs with different
  id/title collide under merge_by_uri (metadata dropped). | FIX: mint handle off content_identity()
  like product/quality layers (consistent, collision-safe).
- B-R1-03 (CR1) MINOR | tests/test_agents_product.py:204,288 | PRD/SprintPlan tests assert on a
  hand-rebuilt object, not node output; clamp logic never exercised. | FIX: assert off node writes.
- B-R1-04 (CR2) MINOR | tests/test_agents_quality.py:264,314 | TestReport/E2EReport tests assert on
  hand-built objects, not node output. | FIX: assert off emitted records.
- B-R1-05 (CR5) MINOR | agents/{product,quality}/_common.py + architecture | emit_artifact/_as_text/
  index-by-id duplicated across layers. | FIX: hoist shared helpers into one agents-level module.
- B-R1-06 (CR3) MINOR | agents/quality/qa_engineer.py:179 | TestReport counts (passed=N,failed=0)
  are fictional — qa authors but does not execute. | RESOLVE: document as authoring-only (execution
  feeds counts when gate-wiring lands, T-M1-12).
- B-R1-07 (DT2) MINOR | agents/quality/reviewer_critic.py:67 | subject_artifact_uri falls back to
  ""/artifacts[-1]; empty subject could share latch scope. | RESOLVE: note for supervisor/gate-wiring
  (T-M1-13) to pin subject on config; add a defensive guard if cheap.

Resolution: dispatched ONE coherent fresh fixer (findings interlock: DT1 ripples into the test
rewrites). New round verifies. (open → pending fix)
