# Evaluation — proving the factory works

A multi-agent factory is only credible if we can measure it. We evaluate both
the **trajectory** (did the agents behave well along the way?) and the
**outcome** (did the software actually get built and work?), and we run
regression evals in CI on every change to prompts, graph, or tools.

## 1. Outcome evals

- **Golden task suite** — a curated set of representative build requests
  (greenfield small app, bugfix, feature addition, refactor) with known-good
  acceptance tests. The factory's output must pass *its own* generated tests
  **and** the golden hidden tests.
- **SWE-bench Verified-style** — isolated, single-issue resolution tasks to
  baseline raw coding capability against published numbers (~65–75% frontier).
- **SWE-EVO-style long-horizon** — multi-commit software *evolution* tasks
  spanning planning across changes. This is the honest test: frontier systems
  score ~21% here, so it is where the org structure (planning, decomposition,
  checkpoints, HITL) must earn its keep.

## 2. Trajectory evals

Scored from OpenTelemetry traces (deterministic tool mocks in CI; LLM-as-judge
only with auditing and human-calibrated thresholds):

- Tool-choice correctness and argument validity
- Step count / loop count (efficiency; detect thrashing)
- Gate pass-rate and **first-pass yield** (passed without rework)
- Rework count per feature
- Policy-compliance rate (zero unauthorized tool calls)
- Cost (USD, tokens) and wall-clock per feature

## 3. Headline metrics (the scorecard)

| Metric | What it tells us | Target direction |
|---|---|---|
| Task resolution rate | Did it produce working software? | ↑ |
| First-pass yield | Quality of initial work (less rework) | ↑ |
| Escaped-defect rate | Bugs that slipped past all gates | ↓ |
| Rework loops / feature | Evaluator-optimizer efficiency | ↓ |
| Cost per feature (USD) | Economic viability | ↓ |
| Wall-clock per feature | Throughput | ↓ |
| Human-intervention rate | Autonomy vs. babysitting | ↓ (without raising escaped defects) |
| Gate pass-rates | Where the pipeline stalls | diagnostic |

Cost-per-feature and human-intervention-rate are tracked *together with*
escaped-defect-rate so the system can't "win" by cutting corners.

## 4. CI integration

- Every PR that changes prompts, the graph, tools, or gate logic runs the
  golden suite with **deterministic tool mocks** (fast, reproducible).
- LLM-as-judge scores are recorded with the judged content for audit; thresholds
  are calibrated against a human-labeled sample and re-checked periodically.
- Regressions on any headline metric block the change.

## 5. Optimization loop (later phase)

Once the eval harness is trustworthy, treat the workflow itself as searchable
(à la MetaGPT's AFlow, which automates agentic-workflow generation): tune
prompts, role assignments, model tiers, and graph topology against the
scorecard — but only ever promote a change that improves outcomes *without*
regressing cost, escaped defects, or policy compliance.

## 6. What we explicitly watch for

- **Reward hacking** — passing gates by weakening oracles. Caught by immutable
  acceptance tests, test-diff review, and mutation testing.
- **Judge drift** — LLM-judge scores diverging from human judgment. Caught by
  periodic recalibration.
- **Benchmark overfitting** — gaming the golden suite. Mitigated by a rotating
  hidden hold-out set.
