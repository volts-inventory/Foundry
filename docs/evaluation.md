# Evaluation — proving the factory works

A multi-agent factory is only credible if we can measure it, **set thresholds it
can fail**, and test its central claim against a baseline. We evaluate the
**outcome** and the **trajectory**, run regression evals in CI, and — crucially —
run a **controlled comparison against a strong single-agent baseline**, because
"build the org, not the coder" is an empirical claim the industry has not
settled.

## 1. The central hypothesis and its test (the A/B)

**Hypothesis:** the org/graph structure (planning, decomposition, gates,
checkpoints, HITL) improves **long-horizon** outcomes over a single strong agent
given the same model budget.

This is not assumed — it is measured:

- **Controlled comparison:** Foundry vs. a strong single-agent baseline (same
  underlying model, comparable total token budget) on an **identical held-out
  subset** of SWE-EVO-style long-horizon tasks and the golden suite.
- **Pre-registered success criterion:** Foundry must beat the baseline on
  resolution rate by a pre-registered delta **at equal or lower cost**, or the
  structure is not earning its keep and the design must change. This A/B is a
  **Phase-1 smoke test and a Phase-5 exit criterion** (see [roadmap.md](./roadmap.md)).

If the org structure does *not* help, that is a finding, not something to paper
over — the plan is falsifiable by design.

## 2. Outcome evals

- **Golden task suite** — curated build requests (greenfield small app, bugfix,
  feature addition, refactor) with **hidden** known-good acceptance tests. Output
  must pass *its own* generated tests **and** the hidden tests (guards against
  the test-authoring agent writing weak oracles — [governance.md §1b](./governance.md)).
- **SWE-bench Verified-style** — isolated single-issue tasks to baseline raw
  coding capability against published numbers (~65–75% frontier).
- **SWE-EVO-style long-horizon** — multi-commit *evolution* tasks; the honest
  bar (~21% frontier today). This is where the A/B in §1 is decided.

## 3. Trajectory evals

Scored from OpenTelemetry traces. **Deterministic tool mocks in CI** make the
*evaluation* reproducible even though real runs are not; LLM-as-judge is used
only where no oracle exists, always recorded with the judged content and
calibrated against a human-labeled sample.

- Tool-choice correctness and argument validity
- Step/loop count (detect thrashing and retry-until-green)
- Gate pass-rate and **first-pass yield**
- Rework loops per feature
- Policy-compliance rate (zero unauthorized tool calls)
- Cost (USD, tokens, **including monitor overhead**) and wall-clock per feature
- **Side-effect idempotency:** replay/resume produces no duplicate PRs/tags/deploys

The **Guardian analyzer** has its own evals: precision/recall of its advisory
flags against a labeled set of injected reward-hacking / scope-creep trajectories.

## 4. Headline scorecard — with concrete thresholds

Direction-only targets can never fail, so each metric has a **release threshold**
(initial values; recalibrated in Phase 5). A change that regresses any threshold
**blocks**.

| Metric | Tells us | Initial threshold |
|---|---|---|
| Golden-suite resolution rate | Does it build working software? | ≥ 90% greenfield-small, ≥ 70% feature/refactor |
| Hidden-test pass rate (of self-passing runs) | Are generated oracles real? | ≥ 95% |
| SWE-EVO subset resolution (vs baseline) | Does structure help long-horizon? | **> single-agent baseline by ≥ 10 pts at ≤ equal cost** |
| First-pass yield | Initial quality | ≥ 60% |
| Escaped-defect rate | Bugs past all gates | ≤ 1 per 10 features (golden hidden tests) |
| Rework loops / feature | Loop efficiency | ≤ 3 median |
| Cost per feature (USD) | Economic viability | within Phase-5 budget envelope; monitor overhead ≤ 5% |
| Human-intervention rate | Autonomy | tracked; may not rise while escaped-defects fall |
| Criterion-coverage | Tests trace to spec | 100% of acceptance criteria exercised |

Cost-per-feature, human-intervention-rate, and escaped-defect-rate are evaluated
**together** so the system can't "win" by cutting corners.

## 5. CI integration

- Every PR that changes prompts, the graph, tools, or gate logic runs the golden
  suite with deterministic tool mocks (fast, reproducible).
- LLM-judge scores are stored with the judged content; thresholds are calibrated
  against human labels and re-checked periodically (judge-drift guard).
- Regression on any §4 threshold blocks the change.

## 6. Workflow optimization (Phase 5) — concrete, not hand-wavy

Treating the workflow as searchable (à la MetaGPT's AFlow) requires a defined
search space, objective, and method — not just "tune it":

- **Search space:** prompt/SOP variants per role, model-tier assignment per node,
  graph-topology toggles (e.g., extra review pass on/off, parallel width), retry
  caps.
- **Objective:** the §4 scorecard as a constrained objective — maximize
  resolution and first-pass yield **subject to** no regression in escaped
  defects, cost, or policy compliance.
- **Method:** evaluate candidates on the golden + SWE-EVO subsets with held-out
  rotation; promote only Pareto-improving configurations; record the winning
  config in the run manifest.
- **Cost of search:** the optimization itself is budgeted and run offline, not on
  production traffic.

## 7. What we explicitly watch for

- **Reward hacking** — passing gates by weakening oracles. Caught by separated
  test authorship, criterion-coverage, test-diff review, mutation testing, and
  the retry-until-green deterministic monitor.
- **Judge drift** — caught by periodic recalibration against human labels.
- **Benchmark overfitting** — caught by a rotating hidden hold-out set and the
  baseline A/B (overfitting the suite would not improve the relative delta).
