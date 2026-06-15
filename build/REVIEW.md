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

ACCEPTANCE (Wave D): Round-2 fresh verification critic: D-R1-01/02/03 all RESOLVED, no new issues,
applier round-trips byte-exact + fail-closed on unsupported hunks, path-containment enforced,
BlobResolver composable for factory_graph. Gates: ruff/mypy clean, 382 pass/1 skip.
**Wave D ACCEPTED.** (gate-wiring done.)

## Wave E / T-M1-13 supervisor — Round 1 (1 fresh critic)
Tool gates green: ruff ✓, mypy ✓ (85 files), full suite green (386/1). Verdict: FINDINGS(3), all MINOR.
**CONFIRMED SOUND:** (a) convergence/no-infinite-loop (fix counter bumped once per non-advancing
boundary re-eval onto single-writer budget, read post-fold by the edge; halt at max_fix_attempts;
forced-fail test terminates without hitting recursion_limit); (b) attested-only advancement (advance
requires runner-attested PASS on every relevant gate; missing/unsigned → fix, never advance).
End-to-end test runs REAL runner + real Spec/Build/Test (real compileall/pytest) + real merge_gates.

- E-R1-01 MINOR | supervisor.py:250-273 (_bump_on_non_advance) | the in-node bump reads a
  hand-assembled `post` while the conditional edge reads the full post-fold gates channel; they agree
  only by an unenforced invariant (epoch-newest + no intervening gates write). | FIX: have the edge
  re-derive from the same merged-gates view (fold state+out gates) so node and edge can't diverge.
- E-R1-02 MINOR | supervisor.py:264,326,334 | `runner._records` private-attr reach-through. | FIX:
  add a public read-only `records` property on TwoZoneGateRunner; use it.
- E-R1-03 MINOR | gate_wiring.py:528-540 (_is_flake_quarantine) | flake/quarantine classified by
  substring sniff over free-text findings; agent-influenced output could mis-route a real infra ERROR
  as flake (bounded fix, never advance — NOT a trust break). | DEFER to Phase 2 (when the held-out-
  mutant/flake machinery lands): have the runner stamp a STRUCTURED flake flag instead of free text.

Resolution: E-R1-01/02 fixed in this wave's fix cycle (batched after factory_graph); E-R1-03 deferred
to Phase 2 with rationale (bounded-safe, not a trust break). (open → pending fix)

## Wave E / T-M1-14 factory_graph — Round 1 (1 fresh critic)
Tool gates green: ruff/mypy clean (86 files), full suite green. Verdict: FINDINGS(3), all MINOR, 0 BLOCKER/MAJOR.
**CONFIRMED SOUND:** (a) a HALTED/FAILED/ERROR/missing-gate run can NEVER open a PR (terminal_gates_pass
fail-closes on Spec∧Build∧Test attested PASS, reads only the trust-filtered gates channel, review advisory);
(b) kill-then-resume idempotency holds (content_digest key + union_actions coalesce to exactly one PR).
No direct GitHub/network call (actions-channel/outbox only). Tests run the REAL supervisor+gates+merge_gates.

- E-R1-04 (FG-M1) MINOR | factory_graph.py:222 / factory_state.py:22 | emit writes single-writer `phase`
  channel (declared owner: supervisor) — works (distinct supersteps) but contradicts the documented
  invariant. | FIX: document emit as the terminal phase writer.
- E-R1-05 (FG-M2) MINOR | tests/test_factory_graph.py | halted-no-PR test covers only the ERROR path;
  the stable-FAIL latch path to terminal_gates_pass=False is not covered E2E. | FIX: roll a stable-FAIL
  (syntax-error diff → Build FAIL + latch → no PR) assertion into the T-M1-16 exit integration test.
- E-R1-06 (FG-M3) MINOR | factory_graph.py:81-93 | `_latest_by_name` collapses by name only vs
  merge_gates' (name, run_against); deliberate cross-tree newest-wins release semantics. | FIX: one-line
  clarifying comment.

Resolution: FG-M1/M3 applied directly (doc/comment); FG-M2 folded into T-M1-16 exit test. (open → pending)

ACCEPTANCE (Wave E supervisor+factory_graph): supervisor E-R1-01/02 fixed (unified gates-view bump;
public runner.records); factory_graph E-R1-04 (emit terminal-phase-writer doc) + E-R1-06 (_latest_by_name
cross-run_against comment) applied directly; E-R1-05 (stable-FAIL no-PR) folded into T-M1-16 exit test;
E-R1-03 (flake structured flag) deferred to Phase 2. Gates: ruff/mypy clean, 389 pass/1 skip. Convergence
+ attested-only advancement + emit trust-guard + resume-idempotency all SOUND. **supervisor + factory_graph ACCEPTED.**

## M1.5 integrity trio (pilot/power/preregistration, T-M15-04/05/06) — Round 1 (1 fresh E5 critic)
Tool gates green: ruff/mypy clean, 526 pass/1 skip. Verdict: FINDINGS(4) — 1 MAJOR + 3 MINOR.
Per-question: pilot SOUND; power SOUND; **pre-registration FLAW (keystone)**; honesty SOUND; tests SOUND
(rejection paths real). The alpha-ledger no-re-draw, disjoint pilot, task-as-unit, no-post-hoc-rung,
tamper-evidence, dry-run-non-coalescence are all confirmed genuine.

- M15-R1-01 **MAJOR** | preregistration.py:198-238 | freeze_preregistration mirrors mde/alpha from
  funded_rung but NEVER reconciles them against the EMBEDDED CentralHypothesis (which carries its own
  alpha/mde/PowerRung). A caller can freeze a manifest powered for rung A while the bound hypothesis
  commits a different/smaller MDE or a non-funded rung — content-addressed (deterministic) but internally
  incoherent, and assert_unmodified does NOT catch it (uncovered by tests). | FIX: in freeze assert
  hypothesis.mde==funded_rung.mde and hypothesis.alpha==funded_rung.alpha (and hypothesis.funded_power_rung
  ==funded_rung); add the same coherence check to assert_unmodified; add a mismatch test.
- M15-R1-02 MINOR | power.py is_funded | on a degenerate-underpowered rung funded_budget_microusd=0 so
  is_funded(rung, any>=0)→True despite funded=False/underpowered=True (freeze GUARD-1 catches it first, but
  the public helper contradicts its own record). | FIX: is_funded short-circuits False when underpowered/
  not funded.
- M15-R1-03 MINOR | preregistration.py:220-229 | check-then-record TOCTOU on the alpha look (separate
  lock acquisitions). In-process/GIL-bounded + DB PK has look_index so no lost write, but "at most one look"
  isn't atomic. | FIX: atomic insert-if-absent in record_alpha_look (or document the single-threaded-harness
  limitation if the core-ledger change is out of scope).
- M15-R1-04 MINOR | pilot.py:276-279 | no-treatment-arm fallback sets effect_size_variance=
  control_resolution_variance (silent base-rate-for-effect-size substitution; harmless given DEFAULT arms
  include FOUNDRY). | FIX: comment/guard as a latent foot-gun.

Resolution: 1 MAJOR (keystone) → fresh fixer after T-M15-07 commits (avoid racing its preregistration import),
then MANDATORY fresh re-review. (open → pending fix)
