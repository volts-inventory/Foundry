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

ACCEPTANCE (M1.5 integrity trio): Round-2 fresh verification critic CLEAN — M15-R1-01..04 all RESOLVED,
keystone holds (coherence + funded guards fail-closed strictly BEFORE the atomic single-draw alpha look;
no incoherent manifest; no alpha on a rejected freeze), atomic try_first_alpha_look verified, no Phase-0
regression (nonce/latches untouched). Gates: ruff/mypy clean, 548 pass/1 skip. **Trio (pilot/power/prereg) ACCEPTED.**
(INFO: legacy record_alpha_look now callerless — retained for future multi-look spending, not dead code.)

## M1.5 harness/A-B modules (T-M15-01/02/03/07/08/09) — Round 1 (1 fresh E5 critic)
Tool gates green: ruff/mypy clean (98 files), full suite green. Verdict: **CLEAN** — 3 MINOR notes, 0 BLOCKER/MAJOR.
Per-area: harness-ITT SOUND (denominator double-guarded; crash/over-budget retained, never shrunk);
bootstrap SOUND (resamples TASKS/clusters, k collapsed to per-task rate, paired within task, content_digest
seed L7); Holm SOUND (monotone step-down, halts at first acceptance, FWER ok); decision SOUND
(no-null-to-PROCEED enforced in model_post_init, cost-worse win -> null, assert_unmodified first); honesty
SOUND (simulated baselines/ablation + dry-run inescapable; FOUNDRY arm genuinely wraps cli.run).
- M15-R2-01 MINOR | ab_runner.py:266-279 _bootstrap_two_sided_p counts the m==0 boundary in BOTH tails ->
  for asymmetric dists touching 0 the smaller tail is inflated (biases p UP = conservative/safe, anti-power);
  achievable min p = 2/n (resample-grid floor). | FIX (cheap, on M1.5 close): strict wrong-side tail + half
  boundary; document the grid floor.
- M15-R2-02 MINOR | ab_runner.py:449 | significant_at_alpha two-sided (CI excludes 0) vs meets_mde one-sided
  -> a significant-but-negative effect reports significant=True but correctly cannot PROCEED (meets_mde fails).
  Defensible as-is (matches §2 two-sided alpha). | accept.
- M15-R2-03 MINOR | ab_runner.py:482-493 | secondary point/raw_p stored by positional index vs Holm original
  order — VERIFIED correct (no off-by-one). | accept (no action).
ACCEPTANCE: all-MINOR, conservative/safe direction, no validity break -> CLEAN ROUND by cross-check.
M15-R2-01 boundary-count to be fixed on M1.5 close (batched small edit). **01/02/03/07/08/09 reviewed CLEAN.**

UPDATE: M15-R2-01 RESOLVED (commit pending) — _bootstrap_two_sided_p now splits the m==0 boundary
evenly between tails (no double-count); grid-floor documented. ab_runner tests + full suite green.

## M2 assurance/trust chain (T-M2-01..10) — Round 1 (1 fresh E3/E1/E2 critic)
Tool gates green: ruff/mypy clean, 81 M2 tests pass, full suite green. Verdict: **overall CLEAN, 3 MINOR, 0 BLOCKER/MAJOR**.
Per-question ALL SOUND: (1) known-vulnerable CAUGHT genuine (attested Security/Supply-chain FAIL + clean control;
unsigned dropped; FAIL latches->halt; cannot reach gated open_pr); (2) sign-off ALWAYS recorded (provenance=SELF,
model never supplies) + release_allowed fail-closes on missing/!approved/anchor-mismatch (+ emit independently
re-checks); (3) evidence pack tamper-evident (WORM hash-chain over content identities; swap/drop/mutate break verify);
(4) tiering relaxes_integrity()==False every tier, LOW catches the secret, force-escalation; (5) re-verification reuse
only on exact scope/key + attested non-ERROR prior (fail-closed); (6) attested-only advancement + bounded rework loops
terminate within caps; (7) coupling deliberate, no fail-OPEN, Assistant back-compat byte-identical.

- M2-R1-01 MINOR | factory_graph.py:78-92 | imports 5 private supervisor _-helpers (anchor derivation) to avoid
  release-anchor drift. | FIX: promote to PUBLIC anchor-derivation fns in a shared module (governance/sign_off.py)
  so supervisor + factory_graph share one public source of truth (encapsulation without drift risk).
- M2-R1-02 MINOR | factory_graph.py:~421 | EvidencePack.run_against = config "integrated-sha" label, not the actual
  materialized tree_sha the gates ran against (integrity intact — chain folds over content identities — but the
  label can diverge). | FIX: bind run_against to an attested verdict's run_against for honesty.
- M2-R1-03 MINOR | reverification.py:239 (rework_scoped_gates) | passes empty file delta at the rework seam;
  soundness preserved by the fail-closed "no passing prior -> re-run" fallback, so no stale reuse. | FIX: feed real
  old/new file maps when available (comment is already honest).
ACCEPTANCE: all-MINOR, trust SOUND -> CLEAN ROUND by cross-check; M2-R1-01/02 to be fixed on M2 close (batched after
T-M2-11); M2-R1-03 left as a documented honest seam. **M2 assurance chain reviewed CLEAN.**

UPDATE (M2 close): M2-R1-01 RESOLVED (public foundry/governance/release_anchors.py — supervisor +
factory_graph import the public anchor fns; private cross-module import gone) + M2-R1-02 RESOLVED
(EvidencePack.run_against bound to an attested verdict's real tree_sha). Behavior unchanged; gated
tests pass; ruff/mypy clean, 729 pass/1 skip, coverage 89.8%. **M2 review CLEAN-resolved.**

## M3 operability modules (T-M3-02..11) — Round 1 (1 fresh E2/E1/E3 critic)
Tool gates green: ruff/mypy clean (129 files), 154 phase-3 tests pass, full suite green. Verdict: FINDINGS(4), all MINOR, 0 BLOCKER/MAJOR.
ALL 10 per-property verdicts SOUND (non-vacuous tests): stuck-run diagnosis, store-outage-pause-not-corrupt
(real superstep_txn rollback, outbox holds only prior committed row), no-starvation (10-round, re-rank mid-round),
HITL bounded+alerted, trusted-outage paused-not-wedged + per-lineage reservation, upgrade drain/migrate/fail-closed,
monitors+analyzer (override-proof structural, malicious-analyzer test), five-plane+scanner-config+SoD, observability
no-second-egress (read-only manifest Protocol) + drift fail-loud.
- M3-R1-01 MINOR | operations/capacity.py | fleet_cost_rate_ok admission-side only; operations.md §6 wants the
  cap to also gate in-flight resume/rehydration. | FIX: document the deferral (or add a resume-admit gate).
- M3-R1-02 MINOR | operations/capacity.py:152 | _most_starved ranks held-slots-first; unequal-weight long-lived
  multi-round starvation unproven. | FIX: add an unequal-weight no-release multi-round starvation test.
- M3-R1-03 MINOR | operations/operator.py:265 | evaluate_slos reuses SLOTarget.objective_micro as a raw count/ms
  for max_wedged_count/max_stuck_age_ms (overloads a _micro field) AND silently skips unknown SLO names (several
  operations.md SLOs modeled but can't alert — also the runbooks gap). | FIX: distinct integer count/ms objective
  field; don't silently skip a configured SLO (warn/raise on unrecognized).
- M3-R1-04 MINOR | operations/store_resilience.py:306 | bare except folds a genuine bug into "dangling handle"
  (fail-closed direction, but masks diagnostics). | FIX: narrow/log the non-(Unknown/KeyError) branch.
Resolution: batch-fix after T-M3-13 lands (shares operator.py/capacity.py); then milestone sweep + audit. (open → pending)

ACCEPTANCE (M3-R1-01..04, fresh batched fixer): all four MINOR RESOLVED 1:1, no exit-property regression.
- M3-R1-03 RESOLVED: `SLOTarget` gained DISTINCT kind-typed objectives (`objective_count`/`objective_ms`
  ceilings + `objective_micro` ratio floor; `ratio_floor()`/`ceiling(field=...)` fail-closed via
  `SLOConfigError`) — `_micro` no longer overloaded. `evaluate_slos` now emits a WARN `slo_config_error`
  Alert for an unrecognised/mis-configured SLO instead of silently skipping (never a dead monitor).
- M3-R1-01 RESOLVED: ADDED `CapacityScheduler.resume_admit(...)` (not deferred) — the fleet cost-rate cap
  now gates in-flight resume/rehydration on the RATE dimension (queues a cap-breaching resume, never kills),
  per operations.md §6 lines 224-230/299-303. Chosen over documenting because it's a small exact mirror of
  the admission `fleet_cost_rate_ok` check.
- M3-R1-02 RESOLVED: unequal-weight no-release multi-round starvation test added — and it surfaced a LATENT
  starvation (reset-to-0 credit + full-weight accrual starves a weight-1 tenant under held slots). Fixed
  with an anti-starvation wait-age ledger (`_waited_rounds`) as the dominant fairness key after the
  held-slots guard; equal-weight behavior unchanged.
- M3-R1-04 RESOLVED: `check_checkpoint_health` bare-except narrowed — expected `UnknownArtifactError`/
  `KeyError` recorded as dangling; any other probe exception LOGs at ERROR (`exc_info`) while staying
  fail-closed (still poisoned). No genuine bug masked as a benign dangle.
Gates: ruff/mypy clean (129 files), 976 pass/1 skip (incl. test_phase3_exit). Touched ONLY the 4 modules +
their 4 test files + LEARNINGS. **M3-R1 review CLEAN-resolved.**

## M4 trust-root chain (T-M4-01..11) — Round 1 (1 fresh E3 trust critic)
Tool gates green: ruff/mypy clean (135 files), 130 phase-4 tests pass, full suite green, cov 94%. Verdict:
FINDINGS(3), all MINOR, 0 BLOCKER/MAJOR. ALL 7 per-question SOUND: malicious-test (discriminator real held-out
mutant on copytree → Test FAIL), forged-results (zone-B config re-derive + sealed obs → ERROR/FAIL never PASS),
forged-verdict (admission re-verify sig+gate-state for exact tree + input-bound attestation), fork-divergence
(digest-disqualification monotonic/atomic, admission_look folds it → DENY despite valid sigs), autonomous
(admission sole gate; DENY/halt deploys nothing; idempotent; deployer non-agent), outage→paused-not-wedged,
honesty (dev-grade roots labeled; clean control proves blocks load-bearing). The 4 attacks GENUINELY can't deploy.
- M4-R1-01 MINOR (substantive) | factory_graph.py autonomous emit | admission cryptographically re-verifies only
  the largest run_against group (assurance-tree gates); terminal Spec/Build/Test run on a DISTINCT tree (AW2
  re-hash) so admission does NOT re-verify them — they rest on the fail-closed merge_gates filter at the emit
  guard (load-bearing, confirmed). | FIX: have admission re-verify ALL required gates across BOTH run_against trees
  (group-aware required set), so the trust-root release decision cryptographically covers the terminal gates too.
- M4-R1-02 MINOR | factory_graph.py _admission_tree_and_gates | required_gates is data-derived from the chosen
  group; emit guard enforces the mandatory floor first (not exploitable in-path) but admission alone wouldn't.
  | FIX: assert the Security+Audit (+terminal) floor ⊆ the admitted set before building.
- M4-R1-03 MINOR | factory_graph.py:641 | gate_result_uris fed to builder = results_digest values, not WORM
  gate-result handle URIs (semantically mislabeled vs gov §4). | FIX: feed actual handle uris or rename + document.
Resolution: fix M4-R1-01/02 (strengthen the trust root to re-verify all required gates) + M4-R1-03 label; then
re-verify + the independent audit. (open → pending fix)
