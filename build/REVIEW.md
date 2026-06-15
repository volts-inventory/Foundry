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
