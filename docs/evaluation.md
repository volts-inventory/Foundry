# Evaluation — proving the factory works

A multi-agent factory is only credible if we can measure it, **set thresholds it
can fail**, test its central claim against a **fair, powered baseline**, and
guard the eval itself against the standard ways agent evals silently lie
(contamination, observable "hidden" tests, gameable coverage, correlated judges,
nondeterminism). This document specifies the experiments, their pre-registration,
and the cost model.

> **This is "factory eval" — Foundry's eval of *itself*, not the *product's* eval.**
> A delivered product (especially ML) needs its **own** eval/monitoring suite as a
> *shipped artifact* with its own acceptance criteria, produced by the pipeline —
> never conflated with the factory self-eval here ([domains.md §6](./domains.md)).

## 1. The central hypothesis

**Hypothesis:** the org/graph structure (planning, decomposition, gates,
checkpoints, HITL) improves **long-horizon** outcomes over a single strong agent
at equal cost. This is tested by a pre-registered A/B (§2), run as a
**decision-grade gate at Phase 1.5 — before the expensive machinery is built —
with a real kill/redesign branch**, and again as a confirmatory Phase-6 exit
criterion ([roadmap.md](./roadmap.md)). If structure doesn't help, that forces a
redesign at the cost of Phases 0–1, not 0–5. The bet is falsifiable *and*
sequenced to be falsified cheaply. The **funded power rung is pre-registered with
budget committed before the Phase-1.5 run (§2); a null at the funded MDE is a real
KILL trigger**, not a cue to slide down the de-scoping ladder (§8).

## 2. A/B pre-registration (frozen before the Phase-1.5 decision run, hashed into the manifest)

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
- **Equalizer:** **equal USD** is the primary, economically-honest equalizer and
  the **headline viability metric** (reported headline); equal tokens is
  secondary. Because equal-USD *handicaps* Foundry — it spends part of the budget
  on coordination + monitor overhead the baseline pours into the task — a null
  there cannot distinguish "no structural signal" from "signal swamped by
  overhead." So a pre-registered **equal-effective-task-token** secondary
  (overhead-excluded: planning/gate/monitor tokens subtracted, only
  implementation-directed tokens counted) is reported alongside, **specifically so
  the kill/redesign branch (§1) can separate those two cases.** The arms have the
  *same tier mix available* so Foundry can't win merely by using a stronger model
  than the baseline. *(COST-F4)*
- **Single confirmatory primary endpoint:** **SWE-EVO resolution vs baseline
  (§7)** is the *sole* confirmatory primary, with its own pre-registered alpha.
  Every other scorecard threshold (§7) is **secondary/descriptive** and cannot, on
  its own, block the A/B verdict. Any secondary that *can* gate a release is placed
  in a stated **hierarchical gatekeeping** order with **Holm–Bonferroni**
  correction over the gating family; the full hierarchy (primary → ordered
  secondaries → descriptive) is pre-registered in the manifest. The prior
  implicit-OR over ~12 thresholds (no alpha control) inflated both false-positive
  and false-negative rates and is retired. *(AI-R9-F2)*
- **Power:** stated sample size `n` on the task pool, `k` repeated runs per task
  (to average LLM nondeterminism — §7), a **paired cluster-bootstrap that resamples
  *tasks* (the inferential unit), carrying each task's `k` runs together**, the
  **minimum detectable effect**, and the target power (e.g., 0.8). Power is based
  on **task-level effect-size variance, not run-level** — treating `n×k` as
  independent draws overstates effective N. The exit threshold (Foundry > baseline
  by ≥ a pre-registered margin at ≤ equal cost) is only meaningful with `n`/`k`
  sized to detect it at the ~21% base rate. *(AI-R9-F3)*
- **Pre-registered funded power rung (BLOCKER, before any scored run):** the
  de-scoping ladder (§8) must **not** be walked post-hoc to whichever rung happens
  to "pass" once cost is known. Before the first scored run we **pre-register the
  funded rung — its MDE and power — with the budget committed**, and forbid rung
  selection after cost is observed. A **null at the funded MDE is a real KILL
  trigger**, not an invitation to slide to a cheaper rung. If the funded MDE is
  *larger* than the margin that would justify the program, we **state that plainly
  as the test's authority** — the run can only license claims at or above the
  funded MDE. *(AI-R9-F4)*
- **Pre-registration:** baseline config, tier mix, equalizer (USD primary +
  effective-task-token secondary), the funded rung's `n`/`k`/MDE/power/alpha, the
  endpoint hierarchy + correction, the cluster-bootstrap, seeds, and the stopping
  rule are **frozen and content-hashed into the run manifest** before any scored
  run.

## 3. Outcome evals

- **Golden task suite** — curated greenfield/feature/refactor/bugfix requests
  with **hidden** known-good acceptance tests (isolation in §6).
- **In-house novel suite (headline)** — tasks authored **after** the pinned
  model's training cutoff, rotated and regenerated, used for the headline
  generalization claims because public benchmarks are contamination-suspect (§6).
  To close the adaptive-overfitting / eval-on-train channel that "rotated and
  regenerated" otherwise opens: each scored rotation is **version-locked into the
  manifest hash** (no live regeneration of a scored suite); suite authors are
  **firewalled (blind) from Foundry trajectory/failure data** so suites cannot
  drift toward Foundry's observed weak spots; **task-selection criteria + the
  target difficulty distribution are pre-committed before any Foundry run on that
  rotation**; and successive rotations are **audited for difficulty drift against
  the baseline** so a "harder/easier next suite" can't masquerade as a Foundry
  delta. *(AI-R9-F5)*
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
- **The A/B primary endpoint is not scored by LLM judges.** Arms 1 and 3 pass
  through Foundry's gates; if a gate judge has measured self-preference toward the
  in-family implementer, both gated arms gain a scoring tailwind over the ungated
  baseline (Arm 2), manufacturing a "structure helps" result out of judge bias.
  So the **§2 confirmatory primary (SWE-EVO resolution) is scored on oracle /
  hidden-test signals *only*** — no LLM-judge component enters the primary verdict.
  Where an oracle is genuinely unavailable, the primary runs on a **cross-family
  judge** *and* the **sensitivity of the A/B verdict to the measured
  self-preference residual is reported** (would the verdict flip within the
  residual's confidence band?). *(AI-R9-F6)*
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
  intent fidelity, **not proof** — a single mutant never proves intent capture.
  Because "100% of mutants fail" is trivially satisfiable by a thin, easy-to-kill
  family, each criterion's mutant family must clear an **adequacy/diversity floor**:
  a **minimum number of distinct fault classes** (off-by-one, dropped guard,
  inverted predicate, swapped operand, silent-catch, etc.), **or** a **sampled
  second independent mutant family** held for audit — so the threshold measures
  discrimination, not a soft target. *(AI6, SEC8, AI-R8-F5, SEC-R11-F1, AI-R9-F8)*
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
- **Hidden-test pass rate is reported unconditionally, per attempt.** Conditioning
  pass rate on "self-passing runs" is **collider bias** and **differs by arm** —
  Foundry filters its own runs harder, so the conditional rate flatters whichever
  arm self-gates most aggressively. The comparison metric is therefore the
  **intent-to-treat rate: hidden-test passes ÷ all initiated tasks** (every
  attempt counts, self-passing or not). The conditional "of self-passing runs"
  figure is kept as **secondary/descriptive only.** *(AI-R9-F7)*
- **Contamination probes:** canary/quote-completion checks plus reliance on the
  post-cutoff in-house novel suite; public-benchmark absolutes are flagged
  contamination-suspect. *(AI8)*
- **Coverage is not fidelity:** criterion→test tags are sample-audited; high-risk
  criteria use negative oracles (§4). *(AI6, SEC8)*

## 7. Headline scorecard — concrete thresholds

Direction-only targets can't fail, so each metric has a release threshold
(initial; recalibrated in Phase 6). **Exactly one row is the confirmatory primary
(SWE-EVO resolution, with its own alpha); the rest are secondary/descriptive and
carry the §2 Holm–Bonferroni / gatekeeping correction if they gate a release** —
they no longer block via implicit OR. A change regressing a gating threshold
**blocks** under that hierarchy.

| Metric | Tells us | Initial threshold |
|---|---|---|
| **SWE-EVO resolution vs baseline (USD-equal) — CONFIRMATORY PRIMARY** | Does structure help long-horizon? | **> baseline by ≥ pre-registered (funded-rung) margin at ≤ equal cost, at the pre-registered alpha; oracle-scored (§4)** |
| Novel-suite resolution *(secondary)* | Working software, uncontaminated | **≥ measured baseline + margin** on the *same* version-locked novel suite (anchored, not an absolute; e.g. small-task ≈70–85% tracking frontier, recalibrated Phase 6) |
| Hidden-test pass rate — unconditional, per attempt (ITT) *(secondary; conditional version descriptive only)* | Real oracles, not overfit | ≥ 95% |
| Judge↔implementer error correlation | Are LLM gates independent? | below a set ceiling; self-preference within calibration |
| Handoff fidelity | Intent survives hand-offs | ≥ 90% of injected requirements survive to tests |
| First-pass yield | Initial quality | ≥ 60% |
| Escaped-defect rate (by producing tier) | Bugs past all gates | ≤ 1 per 10 features |
| Cost per feature (USD) + wall-clock | Economic viability | within the §8 envelope; monitor overhead ≤ modeled bound |
| Criterion fidelity *(secondary)* | Necessary evidence tests capture intent | 100% high-risk criteria fail a *family* of non-trivial mutants **that clears the §4 adequacy/diversity floor** (necessary, not sufficient) |
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

The token-only enumeration above **understates** the costs the design itself
mandates; the model adds these **dominant non-token terms** (so a "≤5%" or
"viability" claim is not quietly resting on the cheap part): *(COST-F5)*

- **(a) Trusted-runner compute for the behavioral-invariance discriminator (§4):**
  `Σ high-risk-criteria × (|mutant family| + |perturbation family|) ×
  test-suite-wall-clock × $/hr × expected re-verification iterations`, **plus** the
  analogous **ML eval-set re-run** term (eval-set size × per-run compute ×
  re-verification iterations). This is runner CPU/GPU time, not model tokens, and
  it scales with the mutant + perturbation families — not with feature size.
- **(b) Adapter inner-loop term:** `nodes × adapter-internal-turns × tokens/turn`
  using the **measured turns-per-node distribution** (the CLI adapter spends turns
  the orchestrator never sees), **plus per-node CLI cold-start / sandbox-provision
  wall-clock**. Inner-loop turns are a separate, larger multiplier than the
  orchestrator's loop-iteration count.
- **(c) Second-family judging is mandatory on every high-risk criterion (§4):**
  cost it as a standing line item (cross-family judge invocations × per-call cap),
  **plus reserved burst-failover capacity** for the second family — it is not a
  rare add-on.
- **(d) Realized prompt-cache fraction *per node-type*, not a uniform assumption:**
  the Anthropic-style SOP-prefix cache hit does **not** generalize across families
  or inside CLI adapters, so model the realized cache fraction per node-type and
  add **cache-TTL-eviction-on-pause re-warm cost** (HITL/queue pauses evict the
  prefix; the next turn re-pays full input). Assuming uniform high cache hits is
  the optimistic error this term removes.

Together these make A/B-scale capacity sizing **derivable** rather than asserted —
they are exactly the inputs [operations.md §6](./operations.md) needs to size
replicas, queue depth, and reserved burst capacity.

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
authority** — so reviewers know exactly what claim survives. **Rung selection
happens *before* the run, when the budget is committed (§1/§2): the funded rung and
its MDE are pre-registered, and walking the ladder *after* cost is observed — to
whichever rung the result happens to clear — is forbidden.** A null at the funded
MDE is a KILL, not a prompt to descend. Under-powering silently would make the
falsifiability theatre. The trusted gate runner and
builder are **replicable, non-agent** components, so A/B throughput is a sizing
problem (replicas + queue), not a redesign — see [operations.md §4/§6](./operations.md). *(AI-R8-F4, COST-F3)*

## 9. Optimization (Phase 6) — search space, objective, method

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
