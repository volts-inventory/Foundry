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
at equal cost. This is tested by a pre-registered A/B (§2), run as a
**decision-grade gate at Phase 1.5 — before the expensive machinery is built —
with a real kill/redesign branch**, and again as a confirmatory Phase-6 exit
criterion ([roadmap.md](./roadmap.md)). If structure doesn't help, that forces a
redesign at the cost of Phases 0–1, not 0–5. The bet is falsifiable *and*
sequenced to be falsified cheaply.

## 2. A/B pre-registration (frozen before any Phase-5 run, hashed into the manifest)

A vague A/B rigs itself; this one is specified so it can actually fail. *(AI1)*

- **Arms (three, to isolate what does the work):**
  1. **Foundry** — the full org.
  2. **Strong single-agent baseline** — a *named, strong agentic loop*
     (SWE-agent/OpenHands-class) with **identical tool, retrieval, sandbox, and
     model-tier access**, differing *only* in the absence of role-decomposition,
     gates, and hand-offs, with unlimited self-iteration until the budget. **Not**
     a single no-tools Opus call. **Parity is a CI-checked invariant:** the
     baseline's tool/retrieval/sandbox substrate is **re-pinned to Foundry's at
     every scored run and hashed into the same manifest**, so the comparison
     cannot silently rot in Foundry's favor as its substrate evolves. *(AI-R8-F7)*
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
  Self-preference is a first-class scorecard metric. High-risk criteria require a
  judge from the **second model family** (a genuinely different provider/lineage,
  provisioned in the stack — [architecture.md §8](./architecture.md); Claude
  tiers are *not* different families), or cross-family disagreement triggers. *(AI3, AI-R8-F1)*
- **Criterion fidelity (necessary, not sufficient):** for each high-risk
  criterion, the bound test is run against a **family of independently-authored,
  non-trivial known-bad variants** (tied to the mutation-testing apparatus,
  authored **cross-family** from the test author, and drawn from a **rotating
  held-out set kept secret from the test-authoring family** until after
  attestation). A variant counts only if it **passes the build and the criterion's
  happy-path test but fails the intent assertion**. The discriminator also
  enforces **behavioral invariance** — PASS must survive behavior-preserving
  perturbations of the real tree (renames/path/hash changes) and FAIL across the
  mutant family — so a test that keys on mutant *identity* rather than *behavior* is
  rejected ([governance.md §1c](./governance.md)). This is necessary evidence of
  intent fidelity, **not proof** — a single mutant never proves intent capture. *(AI6, SEC8, AI-R8-F5, SEC-R11-F1)*
- **Handoff fidelity:** inject known requirements at intent and measure how many
  **survive** as faithful tests at the Test gate — a direct measure of the
  telephone-game loss the artifact-passing architecture is meant to prevent. *(AI6)*
- **Retrieval recall (split by ground-truth obtainability):** (a) a **mechanical**
  recall target on statically-derivable call/def + dependency coupling edges
  (objective, cheap — a concrete floor is set); (b) a smaller **human-labeled**
  task-relevance set with a stated labeling protocol and inter-annotator
  agreement, reported with its own floor. The retriever is gated on both. *(AI2, AI-R8-F6)*
- **Constraint-scoping recall:** because relevance-scoping `pinned_constraints` is
  itself a retrieval problem, the *local-constraint scoper* has its own recall
  floor over a labeled `(task, relevant-constraint)` set with **cross-cutting
  invariants over-represented**. (Global/security/trust-root constraints are
  non-scopable and always injected, so a scoping miss can only affect low-stakes
  local constraints — [architecture.md §8](./architecture.md).) *(AI-R9-F1)*
- **Analyzer evals:** precision/recall of the Guardian analyzer's advisory flags
  against labeled reward-hacking / scope-creep trajectories, against
  **prompt-injection-of-the-monitor** cases, **and crucially on an *untriggered*
  held-out set** (trajectories that cross no deterministic trigger and touch no
  protected path) — so the sampled-pass coverage ([governance.md §3](./governance.md))
  is measured, not assumed. *(SEC3, AI10, AI-R8-F3)*
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
| Novel-suite resolution | Working software, uncontaminated | **≥ measured baseline + margin** on the *same* novel suite (anchored, not an absolute; e.g. small-task ≈70–85% tracking frontier, recalibrated Phase 5) |
| Hidden-test pass rate (of self-passing runs) | Real oracles, not overfit | ≥ 95% |
| SWE-EVO resolution vs baseline (USD-equal) | Does structure help long-horizon? | **> baseline by ≥ pre-registered margin at ≤ equal cost** |
| Judge↔implementer error correlation | Are LLM gates independent? | below a set ceiling; self-preference within calibration |
| Handoff fidelity | Intent survives hand-offs | ≥ 90% of injected requirements survive to tests |
| First-pass yield | Initial quality | ≥ 60% |
| Escaped-defect rate (by producing tier) | Bugs past all gates | ≤ 1 per 10 features |
| Cost per feature (USD) + wall-clock | Economic viability | within the §8 envelope; monitor overhead ≤ modeled bound |
| Criterion fidelity | Necessary evidence tests capture intent | 100% high-risk criteria fail a *family* of non-trivial mutants (necessary, not sufficient) |
| **Human-decisions per feature** | HITL volume / throughput risk | within a per-tier budget; trending down via risk-tiering — **not** an unbounded externality ([operations.md §7](./operations.md)) |
| **Wall-clock per feature (by tier)** | lead-time competitiveness | within the §8 envelope; Low-tier changes not gated on human availability |

Cost, wall-clock, human-intervention, and escaped-defects are read **together**
so the system can't "win" by cutting corners.

## 8. Per-feature cost & wall-clock model (so "viability" and "≤5%" are falsifiable)

A concrete model — not an assertion — for one representative feature: enumerate
`nodes × expected loop iterations × tokens/node (assembled-context input +
output) × tier price`, **plus** the analyzer's (sampled + event-triggered)
invocation count × per-call input cap, **plus** prompt-cache hit assumptions
(high on actor SOP prefixes, low on the analyzer's changing trajectory). This
yields (a) the cost-per-feature envelope, (b) the realistic monitor-overhead %,
and (c) a **wall-clock budget per feature** on the scorecard. If modeled overhead
exceeds 5%, the target is raised honestly rather than throttling detection. *(AI4)*

The per-feature figure is reported **two ways**: a **converged cost** (no rework)
and a **worst-case-cascade cost** (re-verification cascades to the convergence
bound), since dependency-scoped re-verification ([governance.md §1f](./governance.md))
bounds but does not eliminate cascade cost. It is also reported **weighted over a
realistic change-size distribution** (most changes are small) against the
risk-tiered pipeline ([governance.md §1e](./governance.md)) — a single
"representative feature" understates fleet cost when a fixed assurance floor meets
many small changes. *(COST-F1, COST-F2)*

**A/B-scale roll-up (the decisive run must be shown affordable, not just the
feature).** The per-feature model is multiplied through the pre-registered design:
`3 arms × n tasks × k repeats × per-task cost of a long-horizon SWE-EVO run`
(Foundry's per-task cost is *higher* — it pays coordination + monitor overhead).
`n` follows from the MDE + ~21% base rate + target power; `k` from §4 variance.
The roll-up reports total USD/wall-clock **and the budget that funds it**. If the
powered run is unaffordable, the design is **honestly de-scoped along a stated
ladder** (smaller detectable margin → sequential/stratified test → fractional
pilot), **with the explicit note that each rung trades away statistical
authority** — so reviewers know exactly what claim survives. Under-powering
silently would make the falsifiability theatre. The trusted gate runner and
builder are **replicable, non-agent** components, so A/B throughput is a sizing
problem (replicas + queue), not a redesign — see [operations.md §4/§6](./operations.md). *(AI-R8-F4, COST-F3)*

## 9. Optimization (Phase 5) — search space, objective, method

- **Search space:** prompt/SOP variants per role, **model-tier assignment per
  node (tiering is an *output*, validated, not an intuition — incl. defaulting
  high-risk implementation and oracle-authoring to T1 until data says otherwise)**,
  graph-topology toggles, retry/convergence caps, **and the role set itself** —
  role-count is an explicit *ablation variable*, not a given: each role past the
  PM→Architect→Senior→QA core must show it improves the scorecard before it is
  added (the Phase-1.5 ablation, [roadmap.md](./roadmap.md)). *(AI7, P-F2)*
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
