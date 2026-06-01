# Evaluation — proving the factory works

A multi-agent factory is only credible if we can measure it, **set thresholds it
can fail**, test its central claim against a **fair, powered baseline**, and
guard the eval itself against the standard ways agent evals silently lie
(contamination, observable "hidden" tests, gameable coverage, correlated judges,
nondeterminism). This document specifies the experiments, their pre-registration,
and the cost model.

## 1. The central hypothesis

**Hypothesis:** the org/graph structure (planning, decomposition, gates,
checkpoints, HITL) improves **long-horizon** outcomes over a single strong agent
at equal cost. This is tested by a pre-registered A/B (§2), run as a Phase-1
smoke test and a Phase-5 decisive exit criterion. If structure doesn't help, that
is a finding that forces a redesign — the bet is falsifiable.

## 2. A/B pre-registration (frozen before any Phase-5 run, hashed into the manifest)

A vague A/B rigs itself; this one is specified so it can actually fail. *(AI1)*

- **Arms (three, to isolate what does the work):**
  1. **Foundry** — the full org.
  2. **Strong single-agent baseline** — a *named, strong agentic loop*
     (SWE-agent/OpenHands-class) with **identical tool, retrieval, sandbox, and
     model-tier access**, differing *only* in the absence of role-decomposition,
     gates, and hand-offs, with unlimited self-iteration until the budget. **Not**
     a single no-tools Opus call.
  3. **Single agent + same gates, no role split** — isolates whether the gain
     (if any) comes from *roles* or merely from *verification/gates*.
- **Equalizer:** **equal USD** is the primary, economically-honest equalizer
  (reported headline); equal tokens is secondary. The arms have the *same tier
  mix available* so Foundry can't win merely by using a stronger model than the
  baseline.
- **Power:** stated sample size `n` on the task pool, `k` repeated runs per task
  (to average LLM nondeterminism — §7), a **paired** test (bootstrap), the
  **minimum detectable effect**, and the target power (e.g., 0.8). The exit
  threshold (Foundry > baseline by ≥ a pre-registered margin at ≤ equal cost) is
  only meaningful with `n`/`k` sized to detect it at the ~21% base rate.
- **Pre-registration:** baseline config, tier mix, equalizer, `n`, `k`, the
  statistical test, seeds, and the stopping rule are **frozen and content-hashed
  into the run manifest** before any scored run.

## 3. Outcome evals

- **Golden task suite** — curated greenfield/feature/refactor/bugfix requests
  with **hidden** known-good acceptance tests (isolation in §6).
- **In-house novel suite (headline)** — tasks authored **after** the pinned
  model's training cutoff, rotated and regenerated, used for the headline
  generalization claims because public benchmarks are contamination-suspect (§6).
- **SWE-bench-Verified-style** — public, used only for *relative* baselining;
  absolute numbers treated as contamination-inflated.
- **SWE-EVO-style long-horizon** — multi-commit evolution; the honest bar and the
  arena for the §2 A/B.

## 4. Agent-specific validity evals (the things generalist evals miss)

- **Judge decorrelation:** measure **error correlation** between each judge
  (Reviewer/QA/Security-triage) and the implementer on a labeled defect set, and a
  **self-preference probe** (does a judge favor diffs from its own model family?).
  Self-preference is a first-class scorecard metric, not a footnote. High-risk
  criteria require a **different model family** judge or cross-family disagreement
  triggers. *(AI3)*
- **Handoff fidelity:** inject known requirements at intent and measure how many
  **survive** as faithful tests at the Test gate — a direct measure of the
  telephone-game loss the artifact-passing architecture is meant to prevent. *(AI6)*
- **Criterion fidelity:** for each high-risk criterion, the bound test must
  **fail against a known-bad reference implementation** (per-criterion negative
  oracle), proving the test captures intent, not just the tag. *(AI6, SEC8)*
- **Retrieval recall:** the symbol+graph retriever is scored against labeled
  known-relevant spans (incl. cross-file coupling); a recall floor gates it. *(AI2)*
- **Analyzer evals:** precision/recall of the Guardian analyzer's advisory flags
  against labeled reward-hacking / scope-creep trajectories **and** against
  **prompt-injection-of-the-monitor** cases. *(SEC3, AI10)*
- **Per-node decision variance:** `k` repeated runs quantify run-to-run variance
  per node; feeds both the A/B power calc and the optimization search. *(AI5)*

## 5. Trajectory evals

Scored from OpenTelemetry traces; deterministic tool mocks in CI make the
*evaluation* reproducible. LLM-as-judge only where no oracle exists, recorded
with the judged content and calibrated **per judge model** against human labels.

- Tool-choice correctness, argument validity
- Step/loop count; first-pass yield; rework loops/feature
- Policy-compliance rate (zero unauthorized tool calls; zero un-reviewed
  suppression/IaC-privilege edits)
- Cost (USD, tokens, **incl. monitor overhead**) and wall-clock per feature
- Side-effect idempotency (replay/resume → no duplicate PR/tag/deploy)
- Escaped-defect rate **cut by the tier that produced the defect** (so the
  tiering hypothesis is empirical, not intuition — feeds §8). *(AI7)*

## 6. Eval-integrity mechanisms

- **Hidden tests are unreachable at runtime:** they live in a **scoring harness
  the agent sandbox cannot read or reach** (no shared FS, no returned logs),
  applied **only after the run terminates**. The policy plane asserts this
  isolation. "Passes its own tests AND hidden tests" therefore measures
  generalization, not overfitting. *(AI8)*
- **Contamination probes:** canary/quote-completion checks plus reliance on the
  post-cutoff in-house novel suite; public-benchmark absolutes are flagged
  contamination-suspect. *(AI8)*
- **Coverage is not fidelity:** criterion→test tags are sample-audited; high-risk
  criteria use negative oracles (§4). *(AI6, SEC8)*

## 7. Headline scorecard — concrete thresholds

Direction-only targets can't fail, so each metric has a release threshold
(initial; recalibrated in Phase 5). A change regressing any threshold **blocks**.

| Metric | Tells us | Initial threshold |
|---|---|---|
| Novel-suite resolution | Working software, uncontaminated | ≥ 85% small, ≥ 65% feature/refactor |
| Hidden-test pass rate (of self-passing runs) | Real oracles, not overfit | ≥ 95% |
| SWE-EVO resolution vs baseline (USD-equal) | Does structure help long-horizon? | **> baseline by ≥ pre-registered margin at ≤ equal cost** |
| Judge↔implementer error correlation | Are LLM gates independent? | below a set ceiling; self-preference within calibration |
| Handoff fidelity | Intent survives hand-offs | ≥ 90% of injected requirements survive to tests |
| First-pass yield | Initial quality | ≥ 60% |
| Escaped-defect rate (by producing tier) | Bugs past all gates | ≤ 1 per 10 features |
| Cost per feature (USD) + wall-clock | Economic viability | within the §8 envelope; monitor overhead ≤ modeled bound |
| Criterion fidelity | Tests capture intent | 100% high-risk criteria fail vs known-bad ref |

Cost, wall-clock, human-intervention, and escaped-defects are read **together**
so the system can't "win" by cutting corners.

## 8. Per-feature cost & wall-clock model (so "viability" and "≤5%" are falsifiable)

A concrete model — not an assertion — for one representative feature: enumerate
`nodes × expected loop iterations × tokens/node (assembled-context input +
output) × tier price`, **plus** the analyzer's **event-triggered** invocation
count × per-call input cap, **plus** prompt-cache hit assumptions (high on actor
SOP prefixes, low on the analyzer's changing trajectory). This yields (a) the
cost-per-feature envelope, (b) the realistic monitor-overhead %, and (c) a
**wall-clock budget per feature** placed on the scorecard. If the modeled
overhead exceeds 5%, the target is raised honestly rather than throttling the
analyzer blind. *(AI4)*

## 9. Optimization (Phase 5) — search space, objective, method

- **Search space:** prompt/SOP variants per role, **model-tier assignment per
  node (tiering is an *output*, validated, not an intuition — incl. defaulting
  high-risk implementation and oracle-authoring to T1 until data says otherwise)**,
  graph-topology toggles, retry/convergence caps. *(AI7)*
- **Objective:** maximize novel-suite + SWE-EVO resolution **subject to** no
  regression in escaped defects, cost, wall-clock, or policy compliance.
- **Method:** evaluate candidates on novel + SWE-EVO held-out rotations; promote
  only Pareto-improving configs; record the winner in the manifest. The
  optimization is itself budgeted and run offline.

## 10. What we explicitly watch for

- **Reward hacking** — weakened oracles/scanner config/criteria → caught by
  separated+cross-family authorship, criterion fidelity, deterministic
  oracle/config-edit monitors, mutation testing.
- **Correlated-judge failure** — caught by the §4 correlation/self-preference evals.
- **Eval lies** — contamination probes, runtime-unreachable hidden tests,
  per-node variance, and the relative (not absolute) A/B framing.
- **Nondeterminism** — `k`-run averaging, recorded seeds/model-fingerprints,
  pinned-ID-drift alerts.
