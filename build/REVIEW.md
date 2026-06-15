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

ACCEPTANCE (Wave B): all 7 MINOR resolved 1:1 (commit 20a6189), gates re-verified (ruff/mypy
clean, 349 pass/1 skip), narrowed-detection logic spot-verified correct, latent NameError in
principal_architect fixed. CLEAN ROUND by orchestrator cross-check + spot-verification.
**Wave B ACCEPTED.** (4 agent layers done; coverage 85%.)

## Wave C (Phase 1: four subgraphs + node normalization) — Round 1 (2 fresh critics)
Tool gates green before critics: ruff ✓, mypy foundry ✓ (83 files), full suite green, cov 85%.
Verdicts: composition FINDINGS(3); domain/integration FINDINGS(2). 1 MAJOR + 4 MINOR (1 overlap).
**CONFIRMED HOLD:** channel-record safety (no .uri-less record on artifacts), verdict integrity
(reviewer relays only proxy.judge-stamped GateResult; real merge_gates admission, no bypass),
cross-layer composability (each layer reads exactly the artifact kinds the prior writes).

- C-R1-01 (CC) **MAJOR** | graph/subgraphs/quality.py:76 | build_quality_subgraph `resolve=None`
  default → in the chained factory_graph the product layer emits criteria as HANDLES only, so
  read_criteria skips the handle branch → empty TestPlan, zero oracles, NO error: a vacuous Test
  gate pass. | FIX: make `resolve` REQUIRED (mirror build_implementation_subgraph) AND fail-closed/
  log if criteria handles are present on artifacts but resolve is None.
- C-R1-02 (CC) MINOR | subgraphs/product.py + quality.py | SG1 inconsistency: product+quality use
  cast() for add_node; architecture+implementation use the concrete-def wrapper (the recommended
  one per IM5/SG2). | FIX: standardize all four on the concrete-def wrapper; drop cast + StateNode import.
- C-R1-03 (DI) MINOR | agents/quality/reviewer_critic.py:71 | subject fallback = artifacts[-1]
  (insertion-order via merge_by_uri, NOT seq-order) → not deterministically the feature_diff under
  review (masked today because the runner pins config subject). | FIX: select feature_diff by kind,
  newest-by-seq (mirror senior_engineer._first_of_kind).
- C-R1-04 (CC+DI) MINOR | agents/quality/{qa_engineer,_common}.py | dead raw-AcceptanceCriterion
  path (isinstance branch) only fires in direct-call unit tests; widens the surface masking C-R1-01.
  | FIX: drop the production raw path (have unit tests use handles+resolve) OR clearly mark test-only.

Resolution: 1 MAJOR present → dispatch one coherent fresh fixer, then a MANDATORY fresh re-review
round (cross-check acceptance reserved for all-MINOR). (open → pending fix)

ACCEPTANCE (Wave C): Round-2 fresh verification critic: C-R1-01..04 all RESOLVED, no regression,
verdict integrity intact. One MINOR stale-docstring nit fixed directly by orchestrator (qa/sdet
resolve docstrings now describe handles-only + fail-closed). Gates: ruff/mypy clean, 372 pass/1 skip.
**Wave C ACCEPTED.** (4 subgraphs + node normalization done; coverage 85%.)

## Wave D (Phase 1: gate-wiring T-M1-12) — Round 1 (1 fresh trust critic)
Tool gates green before critic: ruff ✓, mypy ✓ (84 files), full suite green.
Verdict: FINDINGS(3): 1 MAJOR + 2 MINOR. CONFIRMED CLEAN: (a) attested-only verdicts (gate_wiring
never constructs a GateResult; unsigned-drop test runs the REAL DevGateWriteVerifier + merge_gates);
(b) fail-closed materialization (missing/unresolvable handles RAISE; vacuous suite FAILs).

- D-R1-01 **MAJOR** | graph/gate_wiring.py:166-185 (_diff_files) ↔ execution/adapters/*.py |
  diff-body contract MISMATCH: gate_wiring expects resolve(diff_uri)->Mapping[str,str], but
  StubAdapter/raw_react/claude_code all store the FeatureDiff body as a UNIFIED-DIFF STRING via
  sandbox.put_blob (read back as bytes). No resolver bridges the shapes → the end-to-end
  factory_graph/CLI smoke path raises FeatureDiffBodyError or materializes an empty tree. The
  green gate_wiring tests fabricate a {uri: files_dict} resolver no emitter produces (vacuous).
  | FIX: make _diff_files parse the adapter's actual unified-diff/bytes body into path->content
  (Phase-1 diffs are /dev/null additions — minimal applier; fail-closed on unsupported hunks),
  and seed the test from a REAL StubAdapter.run().diff_uri + sandbox.get_blob resolver.
- D-R1-02 MINOR | gate_wiring.py:177 | resolves the diff body by mutating the typed FeatureDiff
  handle's .uri — assumes a uri-only resolver; a kind-aware CAS resolver would mis-dispatch. | FIX:
  use a dedicated blob-resolve seam keyed on the uri, not model_copy of the typed handle.
- D-R1-03 MINOR | gate_wiring.py:188-201 (_write_sources) | no path containment — a diff body key
  like `../x` or `/etc/x` writes outside dest (agent-influenced content → traversal). | FIX: reject
  non-relative / `..` POSIX paths fail-closed before write.

Resolution: 1 MAJOR → fresh fixer, then MANDATORY fresh re-review. (open → pending fix)
