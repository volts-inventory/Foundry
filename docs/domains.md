# Domains — verdict classes, the pack contract, and per-domain specs

A general software factory must confront a hard truth the domain-expert review
surfaced: **"correctness" is not one shape.** Foundry's core was built around a
*deterministic behavioral oracle* (a test that fails against a known-bad mutant,
PASS invariant under behavior-preserving perturbation — [governance.md §1c](./governance.md)).
That is exactly right for backend/logic code and **wrong, or insufficient, as the
*primary* signal for whole classes of software**:

- **Regulated/safety-critical** — correctness is traceable evidence + human-witnessed validation + per-record human attribution.
- **ML/data** — correctness is a *thresholded random variable* (metric + confidence interval) over *versioned data*.
- **Frontend/UX** — correctness is perceptual/usability judgment that a "behavior-preserving refactor" deliberately *changes*.

A domain pack cannot fix this by adding gates, because it cannot change what a
**verdict** *means*, cannot widen the verification key, and cannot relax the
runner's discriminator. So the core is generalized here, and the pack contract is
expanded to reach the new core hooks.

> **Honest restatement of thesis idea #3.** "A deterministic decision rule at the
> seams" holds **where a deterministic oracle exists** (backend/logic). It is
> **one of several verdict classes**, not the universal one. Other domains ground
> verdicts via the statistical, human-witnessed, and perceptual classes below.

## 1. The verdict-class taxonomy (core)

`GateResult` carries a `verdict_class`. Each class defines what the verdict *is*,
how it is *grounded*, who *signs* it, whether it enters the **signed SLSA
predicate**, and how **admission latching** ([governance.md §5](./governance.md))
treats it. The behavioral-invariance discriminator and the `results_digest`-stable
assumption apply **only to class 2** — they are now *gate-class-aware*, so they no
longer reject statistical or perceptual tests.

| # | Class | Verdict is… | Grounded by | Signed by | In predicate? | Latch semantics |
|---|---|---|---|---|---|---|
| 1 | **Config-evaluable** | pass/fail | zone B **re-derives** from tree+pinned data (SBOM/CVE/license/IaC/lockfile) | trusted runner | yes | digest-stable |
| 2 | **Execution-deterministic** | pass/fail | pinned trusted infra **observes** behavioral test; held-out-mutant + behavioral-invariance | trusted runner | yes | digest-stable |
| 3 | **Statistical / tolerance-band** *(new)* | thresholded random variable | runner **re-derives** the metric from a **pinned eval set + seed (`n`)**, applies the decision rule | trusted runner | yes | over the **decision** (estimate vs threshold ± CI), tolerating CI-width noise — *not* digest-identity |
| 4 | **Human-witnessed / attested** *(new)* | a signed human record | a **named, credentialed human** executes/approves a protocol (IQ/OQ/PQ, design-control review, per-artifact approver-of-record); signature grounded **out-of-band against the federated IdP / PKI** and bound via the **nonce + evidence-pack-hash binding** ([governance.md §1c](./governance.md) defines the mechanics) | **the human** (out-of-band IdP/PKI identity; an **agent-identity class-4 write is rejected**) | yes | a missing/withdrawn attestation disqualifies |
| 5 | **Perceptual / judgment** *(new; generalizes the Threat-review residual)* | honestly **not** deterministic | independent **cross-family LLM critique + MANDATORY HITL** over a **rendered/evidence bundle** | n/a (advisory model + human) | **no** — excluded from the signed predicate, but **still first-class and gated** (not optional/advisory) | blocked until human sign-off; never auto-approved |

Class 3 signs `(metric_family, subgroup_metrics, calibration_estimand,
eval_dataset_hash, label_version, eval_seed, n, point_estimate, ci, decision_rule,
multiple_testing_correction, look_ledger, remaining_alpha, heldout_look_count,
artifact_eval_decision, status)` and re-derives under the pinned seed+data, so a
metric that crosses threshold on resample does *not* produce an unrecoverable false
latch (it latches on the *decision*, not a noisy digest). **`label_version` is a
distinct signed input from the feature bytes** (a relabel pass is a semantic version
change that dirties the eval gate *and* re-derives the power calc; see §5 ML). The
**population-level train-variance acceptance is signed and WORM-bound at gate time
(over a pre-registered retrain-attempt budget `N`, with retrains-discarded counted +
enumerated) but is *not* in the admission-re-derivable set** (see the train-variance
bullet below). Class 5 reuses the **exact pattern the Security gate already
uses** for its "judgment residual" — extended from one security carve-out to a
first-class class with a required evidence bundle.

**Class-3 statistical validity is not a single threshold crossing.** A bare
single-metric verdict is gameable: repeated retrain-and-re-eval against the pinned
set consumes it as a *selection signal* (adaptive overfitting), and a cheap proxy
can hide tail/subgroup regressions. So class 3 is grounded by these core
requirements, all **runner-re-derived from manifests** (not self-reported):
- **Multiple-comparisons control.** The verdict declares either a **metric family
  with explicit FWER/FDR correction** *or* a **single pre-registered composite
  acceptance criterion** — never an uncorrected pick-the-best over many metrics. The
  family **must include declared subgroup/slice metrics** (each with its own
  threshold and **per-slice powered MDE**, with the multiple-testing correction
  **spanning the slices**) **and a calibration estimand** (a **proper scoring rule
  with a bootstrap CI**, or a defined calibration test with a valid p-value that
  composes into the FWER/FDR procedure — **not raw ECE**, which is binning-dependent,
  finite-sample-biased, and has no CI/p-value that composes, so it would leave the
  corrected family ill-defined). A global headline metric alone is insufficient.
  **A-priori under-powered slices are an explicit verdict, never silent
  under-powering.** Because per-slice powered MDE + slice-spanning correction +
  post-noise detectability compound, the eval-`n`-within-smallest-slice can be
  a-priori INFEASIBLE for a rare protected subgroup; this is **not** `inconclusive`
  (a transient CI-straddle). Such a slice **routes** to one of: a **HITL waiver with a
  documented residual**, a **slice-merge to a powered grouping**, or a **block** —
  pre-declared, never a silently low-powered PASS. (The regulated pack force-declares
  protected attributes + disparity thresholds; see §5 Regulated.)
- **Look budget.** A **look = ANY re-derivation of the class-3 decision against an
  eval set** — debited even when the recompute is cache-served, so
  touch-feature/re-eval/revert cycles cannot launder uncounted looks via §1f
  input-hash reuse. The class-3 alpha look-ledger is keyed on
  `(eval_set_identity, decision_id)` — NOT on `state_sha`. `decision_id` is the
  pre-registered, content-derived identity of the *acceptance question* —
  `H(eval_set_lineage ⧺ threshold ⧺ metric_family ⧺ decision_rule)` — explicitly
  INVARIANT to `weight_refs`/`feature_defs`/`label_version`, so a new `decision_id`
  requires a genuinely new pre-registered objective, not a new candidate; `decision_id`
  minting is a deterministic, runner-/HITL-gated, WORM-logged event, never
  agent-self-issued. An eval-set rotation does NOT reset alpha: remaining alpha carries
  forward into the rotated set under a linked `decision_id` lineage, and the rotation is
  itself an alpha-spending, runner-/HITL-gated, WORM-logged event. The ledger is
  GLOBAL/cross-fork (survives `run_id` change); `state_sha` is recorded per-look for
  audit only. Repeated re-eval is bounded by a **pre-registered
  group-sequential / alpha-spending schedule with a hard cap**; an
  `inconclusive`→**collect-more-n** transition is an **explicit alpha-spending
  event** against that boundary, **never an auto-PASS**, and **reaching the cap
  routes to terminal HITL / redesign** (no further looks). The **look ledger +
  remaining alpha are bound into the signed tuple**; the convergence bound
  ([§3](#3-compute-as-a-gate-cost-model-core)) **counts looks**, and governance §5
  **re-checks the ledger + remaining alpha at admission**.
- **Held-out final-acceptance set.** A **final-acceptance eval set distinct from
  the dev/iteration set**; Release/admission reads the held-out set, never the set
  iteration has touched. Running it per-candidate / per-rework-cycle would turn it
  into a second iteration set, so it carries **its own strict look budget (≈1
  decision per pre-registered candidate, hard cap, HITL beyond)**, reaching the
  held-out gate is a **pre-committed event**, the **held-out look-count is bound into
  the signed tuple**, and the held-out set is subject to the **same
  eval-freshness/PSI staleness gate + rotation** as the iteration set.
- **Train-variance vs eval-variance (two separated estimands).** *Re-scoring* a
  pinned `weight_ref` is deterministic; *re-deriving* weights is **not**
  (GPU/dataloader/all-reduce nondeterminism). A training-variance CI built from `k`
  retrains can therefore **never be re-derived by admission**, which re-scores a
  single frozen `weight_ref`. So the verdict separates two estimands, **both signed**:
  **(a) a population-level train-variance ACCEPTANCE made at gate time** — accept only
  if a **pre-registered lower-confidence-bound / fraction over a PRE-REGISTERED `N`
  retrains** clears threshold. Because (a) is a best-of-N selection statistic, the
  **retrain-attempt budget `N` is pre-registered** and the acceptance is computed over
  those `N` (not best-of-unbounded-N): **every retrain attempt is counted and the
  retrains-discarded are WORM-enumerated** (the look ledger counts only eval
  re-derivations, so retrain attempts need their own count), making it an **unbiased
  train-variance estimand**. **WORM-bound with the `N` `weight_ref`s + the attempt
  count enumerated** and **explicitly NOT admission-re-derivable**; **`N` + the
  attempt-count bind into the gate-time WORM attestation**. And **(b) an artifact-level eval-only
  decision on the shipped `weight_ref` under an eval-only CI** — this is what the
  admission **quorum-of-re-runs ([governance.md §5](./governance.md)) RE-SCORES
  (never retrains)**. **Admission grounds only (b)**; **(a) stands as a gate-time
  attestation.**
- **Leakage gate (class 1, not staleness).** `state_sha` invalidation says data
  *changed*, not that train/eval are *disjoint*. The leakage gate is a
  **class-1 (runner-re-derivable) check over the split/feature manifests**:
  train/eval ID disjointness, declared grouping key with **group-disjoint**
  verification, temporal-cutoff enforcement, and **feature point-in-time
  correctness** (each feature's as-of time ≤ label time). Re-derived from
  manifests, never trusted as self-reported.
- **Discriminating power (criterion-fidelity analog).** The statistical class is
  exempt from the deterministic fail-on-known-bad, so it carries its own analog: the
  eval gate must **fail a family of known-bad reference models** and **detect a known
  regression of size δ with stated power** — runner-re-derived, mirroring the held-out
  mutant family of the deterministic class ([governance.md §1c](./governance.md)). A
  fixed coarse-global triple (degraded / label-shuffled / stale-feature, all known to
  the model author) is too thin, so the family carries the **same adequacy/diversity
  floor + held-out rotation as the deterministic mutant family**: a **minimum number
  of DISTINCT regression archetypes** — subgroup-localized degradation, calibration
  drift, single-feature staleness, near-threshold δ-regression, distribution-shift
  injection — with a **rotating held-out subset kept secret from the model-authoring
  family until after attestation** and **stated power per archetype**, not a fixed
  triple.
- **Label-quality floor (a POWER constraint).** A declared **label-quality budget**
  (estimated label error / inter-annotator agreement); the acceptance threshold must
  sit **meaningfully above the label-noise floor**, and **eval `n` must be large
  enough that the powered MDE δ remains detectable after noise attenuation** at the
  declared error rate. A **`label_version` change RE-DERIVES the power calc** (not
  just dirties the gate).
- **Eval-freshness (concept drift, not folklore PSI).** Univariate PSI with folklore
  cutoffs (and labels treated as optional) is inadequate. Freshness therefore adds a
  **concept-drift estimand** (label/outcome divergence with **explicit label-lag
  handling** — delayed-label backfill or a proxy-label monitor), **replaces folklore
  PSI cutoffs with a freshness criterion TIED TO THE POWERED MDE** (admit only if the
  **induced metric bias < the δ the gate must detect**). Measuring that bias needs
  current-distribution labels — which the same label-lag concession says are not
  available — so the bias figure is itself an estimate: the gate **allocates an
  explicit ERROR BUDGET for the bias-estimate relative to δ** (the proxy/delayed-label
  bias estimate has unquantified error), and **under label-lag the freshness verdict
  accounts for the estimator's own uncertainty** rather than treating "bias < δ" as if
  bias were directly observable. It also adds a
  **multivariate/joint shift detector** (a classifier-two-sample test), not
  marginal-only PSI.
- **Scope: batch / offline supervised, frozen weights.** The class-3 signed predicate
  models **only batch/offline-supervised frozen-weight** eval; online/streaming, RL,
  and generative models have no representable verdict here (and an LLM-judge-scored
  generative metric would smuggle correlated judgment into the signed predicate). The
  class is **honestly scoped to batch/offline supervised**, with out-of-scope
  follow-on classes named: an **off-policy-evaluation verdict (bias bounds)** for RL;
  a **continual-eval / monitored-BOUND verdict** for online models; and
  **generative-eval routes through the perceptual class (class 5 — mandatory HITL +
  cross-family)**, with **LLM-judge-scored generative metrics FORBIDDEN from the
  signed class-3 predicate**.

## 2. The verification key: `state_sha` (core)

`tree_sha = H(source ⧺ lockfile ⧺ toolchain)` is **source-only** and silently
wrong for software whose behavior depends on data/weights. Generalize:

```
state_sha = H( source ⧺ lockfile ⧺ toolchain
             ⧺ data_refs        # dataset/snapshot content identities (manifest hash)
             ⧺ feature_defs      # feature-store definitions
             ⧺ weight_refs )     # model-weight content identities
```

Data/weight bytes stay in their external versioned stores (DVC/LakeFS/Delta/feature
store / object store); **only their verifiable content identity enters the hash**.
The existing dependency-scoped re-verification engine ([governance.md §1f](./governance.md))
then works correctly: a dataset/label/weight change **dirties exactly the
data-dependent gates** (model-eval, leakage) instead of silently reusing a cached
PASS — closing an attestation-laundered staleness bug. `tree_sha` is the
source-only special case of `state_sha`; a pack declares which refs are data vs
weights. The artifact model gains a **"large external versioned artifact"** kind
(identity + external locator + integrity hash), distinct from in-store blobs.

## 3. Compute as a gate: cost model (core)

`BudgetLedger` gains a **`compute_usd` / `gpu_hours`** dimension distinct from
token cost; the per-feature cost model ([evaluation.md §8](./evaluation.md)) sums
both. **Expensive gates** (a retrain, a full device-matrix visual run) are **not
auto-re-run on every fix**: they are gated behind a **cheap proxy** (a small-slice
sanity eval) with the full run behind an explicit **"this change requires an
expensive re-run — confirm budget"** escalation, and the convergence bound counts
expensive-gate-triggering fixes specially. Without this, the re-verification rule
would retrain on every rework loop and blow the budget the controls exist to bound.

**A proxy gates ITERATION only — never Release/admission.** A cheap small-slice
proxy can let a model ship with subgroup/tail regressions hidden, so for any
**behavior-affecting** change the **full statistical gate** ([§1](#1-the-verdict-class-taxonomy-core),
class 3) must run before Release/admission. The proxy is **calibrated with a
measured false-negative rate vs the full gate**, and **"all-green" convergence on
proxy-only evidence is forbidden** — the convergence bound cannot be satisfied by
proxy passes alone.

## 4. The expanded domain-pack contract (core hooks)

A **pack** is no longer "a specialist + some Audit checks." It implements
`{capabilities, tools, prompts/SOP, gates, evals}` **and** may use these **core
hooks** (each enforced in core, fail-closed — a pack can only *tighten*):

1. **Verdict-class assignment** per criterion/gate (incl. statistical / human-witnessed / perceptual).
2. **Mode/tier veto** — make **Autonomous** and **Low-tier auto-approve-Release** *unreachable*; **force-on a named human accountable approver**. Core-enforced; auto-approve cannot override.
3. **Regulatory-significance axis** contributed to change-risk tiering ([governance.md §1e](./governance.md)) that can **force-escalate** tier and force human Release **independent of diff size** (a one-line APR/dosing change is *not* Low).
4. **Per-artifact approver-of-record** — declared artifact classes require a class-4 human attestation bound into WORM + predicate.
5. **Data-egress profile** — e.g. **"no third-party model/trace egress"** (in-boundary/self-hosted models + trace backend) with a **defined judge-decorrelation fallback** when only one approved family exists; and **"no real PHI/PAN in pipeline"** (synthetic/de-identified test data enforced *upstream*, not egress-redacted).
6. **Rendered-artifact evidence bundle + mandatory perceptual HITL** for declared capabilities (e.g. `ui`).
7. **State inputs** beyond source (datasets, weights, feature defs) that enter `state_sha`.
8. **Expensive-gate declaration** (compute budget + proxy + confirm-escalation).
9. **Retention + identity policy** on WORM/object-store (floor + authenticated approver identity).

This means a regulated pack can make the factory **safe by construction** for its
domain — not by a prose caveat, but by removing unsafe modes from the reachable
set.

## 5. Domain pack specs

### Regulated / safety-critical (fintech PCI/SOX · healthcare HIPAA/FDA 21 CFR Part 11 / IEC 62304)
- **Mode/tier veto:** Autonomous and Low-tier auto-approve are **unreachable**; a **named qualified human approver** is mandatory at Release (segregation of duties: author ≠ approver). *(Regulated-F1, F5)*
- **Per-artifact approver-of-record (class 4):** design, test protocol, risk analysis, and validation report each require a **credentialed human** attestation, signed **out-of-band against the federated IdP / PKI** binding `(artifact_uri, state_sha, approver_identity, role, credential_ref, timestamp, meaning)` via the **nonce + evidence-pack-hash binding** ([governance.md §1c](./governance.md) owns the mechanics) and bound into WORM + predicate — **agent-identity class-4 writes are rejected**; agents *draft*, humans *own*. *(Regulated-F2)*
- **Verification = evidence:** a **requirement → risk → verification traceability matrix** is a required predicate artifact, and **each matrix cell must reference a specific runner-attested `GateResult`/`results_digest` keyed to the released `state_sha`** — an **unbound cell fails the predicate**. **Completeness is deterministic over frozen inputs:** the **requirement set AND the risk/hazard set are immutable, runner-counted inputs**, and the predicate requires **EVERY frozen requirement and EVERY frozen hazard to have a bound cell** — an **orphaned requirement or unmitigated hazard fails the predicate**, not merely unbound cells; **risk-analysis edits are tied to the deterministic oracle/criteria-edit monitor** ([governance.md §1c](./governance.md)). The **evidence pack the human signs is the non-agent-assembled set** of those attestations. **Human-witnessed validation (IQ/OQ/PQ)** is a class-4 verdict on equal footing with runner oracles, not "advisory." *(Regulated-F3)*
- **Segregation of duties (recorded pack requirement):** regulated forces **two-person sign-off generally** (not only on trust-root changes); the **author set includes human steering-channel contributors** ([lifecycle.md §2](./lifecycle.md)) **AND the authors of any durable taste/blessed constraint that GROUNDS the artifact's approval** — a steering contribution can persist across runs as a durable taste constraint, and a later run's perceptual approval may be grounded by a constraint a human authored, so folding that constraint's authorship into the later artifact's author set stops a **constraint-author from being approver-of-record on an artifact whose approval is grounded in that constraint** (the SoD check must not intersect only the CURRENT artifact's author set; [lifecycle.md §2](./lifecycle.md) mirrors this), **approver ∉ author set**, and the **two approvers must be distinct from EACH OTHER's functional role** (preparer/validator independence), not merely distinct identities both ≠ agent — enforced by the predicate at [governance.md §1e](./governance.md). *(Regulated-F3a)*
- **Model-risk governance (SR 11-7 / IEC 62304 tool-validation analog):** a **versioned model inventory** with **intended-use/limitations per role**; **independent validation evidence** (cross-family decorrelation results recorded *as* validation artifacts); a **model-version swap = change-controlled re-validation event bound into WORM**; a **named model-risk owner**. Governance is **not front-loaded only** — it adds **ongoing monitoring with thresholds + outcomes-analysis / back-testing**, a **periodic revalidation cadence** (not just swap-triggered), and a **pinned-ID-drift → revalidation trigger**; scope **explicitly covers FACTORY models AND delivered PRODUCT models** (the latter required for a regulated ML product). **Monitoring/back-testing NAMES a firing component** (a monitoring program that detects but does not act is a report, not a control — SR 11-7 §V/§VI): a **monitoring-threshold BREACH or revalidation-cadence LAPSE writes a disqualifying/flagging WORM record** for the affected model, **trips revalidation**, and **raises an escalation** — on equal footing with credential-revocation ([governance.md §5](./governance.md)) and external-store retention (Regulated-F6), which already fire. For a **delivered PRODUCT model**, that periodic product-model revalidation has its own firing component because a shipped model drifting in the customer environment **generates no new `state_sha`**: a **named scheduler/monitor** (analogous to the periodic credential sweep, [governance.md §5](./governance.md)) **fires revalidation on concept/data drift of an UNCHANGED deployed `weight_ref`**, **distinct from the pinned-ID-drift (swap) trigger** that keys off a new `state_sha`. *(Regulated-F3b)*
- **Data boundary:** **no third-party model/trace egress** profile; **no real PHI/PAN** enters the pipeline (synthetic / de-identified data meeting **HIPAA Safe Harbor §164.514(b)(2)** or documented **Expert Determination**). The inbound PHI/PAN/PII boundary scanner is a **CORE, UNCONDITIONAL, fail-closed boundary control that runs on ALL pipeline ingest for EVERY tenant regardless of pack engagement or classification status** — it is **not** a pack-gated control, because a not-yet-flagged but actually-regulated tenant would otherwise have no scanner running to produce the very hit that engages the pack, letting real PHI reach the immutable WORM store unquarantined. It **quarantines on any real-identifier hit**, sits **strictly upstream of any WORM/trace write in core** (a record reaching WORM before quarantine cannot be deleted), and **IS the engagement mechanism** (see Regulated-F4a) rather than a consequence of it. The scanner is **not trusted unvalidated** — it carries a **class-3-style validated detection floor** that matches — not weakens — the discriminating-power standard it borrows: measured **recall against a HELD-OUT, ROTATED labeled PHI/PAN reference family kept secret from whoever tunes the scanner** (a family merely "measured against anticipated archetypes" overfits to those archetypes), incl. **free-text and tokenized/encoded** cases, with **stated power PER ARCHETYPE** (mirroring the discriminating-power family of §1 ML) — since the residual here (a real identifier landing in WORM) is **irreversible**. Its **validated per-archetype recall floor and config sit under the run-immutable / separate-trust-boundary regime and are re-checked at admission**. Plus a **documented residual-risk acceptance**, **fail-closed on unparseable/oversize fields**, and a **documented PCI CDE scope** (in or out, and why); judge-decorrelation falls back to separated-context same-family + stronger oracle + human review. *(Regulated-F4)*
- **Deterministic pack engagement (not agent-self-scoped):** regulated-pack engagement + mode veto is **force-engaged by a DETERMINISTIC non-agent trigger independent of agent capability declaration** — an **inbound PHI/PAN/PII scanner hit**, a **repo/data-classification label**, or a **tenant-level regulated flag**; an agent cannot self-scope out. The scanner-hit trigger is **genuinely independent, NOT contingent on the pack already being engaged**: because the boundary scanner is a **core, unconditional control running on every tenant** (Regulated-F4), a hit can fire on an unflagged-but-actually-regulated tenant and *engage* the pack — it does not require the pack to already be on to exist. **CDE in/out scoping is a human/owner determination bound into WORM**, not an agent artifact. *(Regulated-F4a)*
- **Regulatory-significance tiering axis**; **identity-bound WORM** + **retention floor** (years/decades) with referenced artifacts pinned for the horizon. **External stores backing referenced artifacts** (datasets/weights/evidence bundles) honor the **same retention floor + legal-hold + WORM-immutability** as the audit chain, with a **periodic resolvability/integrity check of referenced locators recorded in WORM**. The periodic check **asserts the store's retention-lock / legal-hold / immutability CONFIGURATION** (not just resolvability + hash) and **alarms on any retention-window / lock-policy regression BEFORE expiry**. *(Regulated-F5, F6)*
- **Attestation withdrawal / credential lapse:** withdrawal or credential expiry **writes a disqualifying WORM record** for the affected `state_sha`/digest, **trips admission re-evaluation**, and **raises a recall/notification escalation** — a prior attestation is not timelessly valid. *(Regulated-F7)*

### ML / data systems
- **Statistical verdict class (3)** for model-eval (metric + CI + pinned eval set/seed), **scoped to batch/offline-supervised frozen weights** — online/RL/generative route to the named out-of-scope sub-classes ([§1](#1-the-verdict-class-taxonomy-core); generative→class 5, LLM-judge-scored metrics forbidden from the signed predicate). Admission latches on the *decision*, not digest noise. Carries the full class-3 grounding: **multiple-testing correction or a pre-registered composite criterion** (with **declared subgroup/slice metrics — per-slice thresholds + powered MDE, correction spanning slices, an a-priori-under-powered-slice route (HITL-waiver-with-residual / slice-merge / block, not silent under-powering) — and a calibration estimand entering the correction via a proper scoring rule with bootstrap CI or a valid-p-value calibration test, not raw ECE**), a **debited-per-re-derivation look budget** (cache-served looks still count; the **look-ledger is keyed on `(eval_set_identity, decision_id)` — NOT `state_sha`**, where `decision_id` is the pre-registered, content-derived identity of the *acceptance question* (`H(eval_set_lineage ⧺ threshold ⧺ metric_family ⧺ decision_rule)`), INVARIANT to `weight_refs`/`feature_defs`/`label_version` and minted only via a deterministic runner-/HITL-gated WORM-logged event, so every retrain/feature/label variant scored against the same eval set debits ONE shared alpha budget; an eval-set rotation does NOT reset alpha (remaining alpha carries forward under a linked `decision_id` lineage; the rotation is itself an alpha-spending, runner-/HITL-gated, WORM-logged event); the ledger is GLOBAL/cross-fork (survives `run_id` change), `state_sha` recorded per-look for audit only; `inconclusive`→collect-n is alpha-spending against a group-sequential boundary + hard cap → terminal HITL, never auto-PASS; ledger + remaining alpha bound into the tuple and re-checked at admission [governance.md §5](./governance.md)), a **single-use held-out final-acceptance set** (own ≈1-decision-per-candidate budget, pre-committed gate event, look-count bound into the tuple, same freshness/rotation), a **two-estimand train-variance split** (gate-time population acceptance over a **pre-registered retrain-attempt budget `N`** with **retrains-discarded counted + WORM-enumerated** so it is unbiased, not best-of-unbounded-N; `N` + attempt-count WORM-bound + NOT admission-re-derivable; artifact-level eval-only decision on the shipped `weight_ref` is what admission RE-SCORES, never retrains), and **discriminating power** via a rotated, held-out, multi-archetype known-bad family (not a fixed triple). *(ML-F1)*
- **`state_sha`** includes data/feature/weight refs *and* a distinct **`label_version`**; a data, feature, or relabel change dirties data-dependent gates (a relabel is a semantic version change). A declared **label-quality budget** keeps the acceptance threshold above the label-noise floor **as a power constraint** — eval `n` must keep the powered MDE δ detectable after noise attenuation, and a **`label_version` change RE-DERIVES the power calc** (not merely dirties the gate). The regulated pack **force-declares protected attributes + disparity thresholds** into the subgroup metric family. *(ML-F2)*
- **Leakage gate (class 1):** a **runner-re-derivable** check over the split/feature manifests — train/eval ID disjointness, group-disjoint verification on the declared grouping key, temporal-cutoff enforcement, and feature point-in-time correctness — *not* inferred from `state_sha` staleness. *(ML-F2a)*
- **Compute-cost budget + expensive-gate handling** (proxy eval + retrain-confirm escalation); the **proxy gates iteration only**, is **calibrated with a measured false-negative rate**, and the **full statistical gate runs before Release/admission** for any behavior-affecting change. *(ML-F3)*
- **Eval-freshness / non-stationarity check (gate INPUT, not just monitoring):** pre-release shift detection on inputs, a **bounded eval-set age**, and a **documented covariate-shift justification** when divergence exceeds threshold — but **not** univariate folklore-cutoff PSI alone. It adds a **concept-drift estimand** (label/outcome divergence with **explicit label-lag handling** — delayed-label backfill or proxy-label monitor), a **freshness criterion TIED TO THE POWERED MDE** (induced metric bias < the δ the gate must detect, replacing folklore cutoffs) with an **explicit error budget for the bias-estimate itself relative to δ** (under label-lag the verdict accounts for the estimator's own uncertainty, since "bias < δ" is estimated, not observed), and a **multivariate/joint shift detector** (classifier-two-sample test), not marginal-only PSI. The **held-out final-acceptance set is subject to the same freshness/rotation gate** as the iteration set. *(ML-F3a)*
- **Product eval-suite + monitoring/drift detectors are required *shipped* artifacts** (distinct from Foundry's *own* eval — see §6); **drift/metric-decay/retrain-trigger** are first-class evolution-route inputs ([lifecycle.md §3](./lifecycle.md)). *(ML-F4)*
- **Model-vs-code rollback:** a weight ref is a deployable/rollback-able unit; "rollback" = prior attested *state*, not prior code digest. A rollback **re-checks admission freshness** (eval staleness + feature/schema compatibility vs current serving contracts) rather than trusting the prior attestation as timelessly valid, and **binds the compatible feature/schema version into the rollback unit**. Because a known-prior-good target can itself read `inconclusive` under current freshness (deadlocking the emergency rollback), a **break-glass distinguishes roll-back-to-good (liveness-critical) from promote-new (safety-critical):** a known-prior-good state **may admit under two-person override even when its freshness/CI is inconclusive**, **WORM-logged**, with a **forced follow-up re-eval**. *(ML-F5)*

### Frontend / UX / design-heavy
- **Perceptual/judgment verdict class (5)** for look/feel/interaction/copy, with a **mandatory, non-auto-approvable visual/UX HITL** over a **rendered-artifact bundle** (key screens × breakpoints × states, captured by trusted infra) — this is how "taste is not delegated to a model" ([lifecycle.md §2](./lifecycle.md)) becomes *implemented*, not just stated. This class also **owns the visual baseline** (it blesses a rendering as correct; see UX-F4a) and is **scoped to the affected screens** (UX-F2a). *(UX-F1, F2)*
- **Static matrix ≠ interaction/motion.** A static screenshot matrix **cannot capture interaction / animation / motion / focus-order / transitions**, so the class is honest about it: **static-visual + copy + rule-based a11y is what the screenshot bundle covers**, and **interaction / animation / motion / focus-order are named out-of-band human-review-only** (like usability, [UX-F3](#frontend--ux--design-heavy)) — **UNLESS** an **interaction-evidence artifact** is supplied (**recorded interaction traces / videos / DOM-state sequences**), which then carries **its own perceptual HITL** and a **defined "baseline" for time-based behavior**. *(UX-F2c)*
- **Scoped perceptual HITL.** Mandatory sign-off is **scoped to the screens/states the diff actually affects** (render-dependency analysis, reusing dependency-scoped re-verification [governance.md §1f](./governance.md)); HITL is reserved for changes the diff flags as **material**. **Auto-skip is gated on BOTH a perceptual-distance floor AND a render-dependency / semantic-affordance classifier** (interactive-element identity, tap-target geometry, contrast, copy-on-controls) — **perceptual distance ALONE never authorizes auto-skip**, since small-pixel/high-semantic regressions would otherwise slip through "looks the same" ≠ "is correct." Because that classifier *authorizes* a skip, it is held to the same standard as every other skip/quarantine decider (PHI scanner, render-reachability scoping, iteration proxy): it carries a **validated recall floor against a known-bad affordance-regression family** and **fails closed on low confidence** (an unvalidated affordance classifier cannot license skipping the perceptual verdict). **The two auto-skip paths for this mandatory class-5 gate are reconciled to the STRICTEST:** lifecycle §2 auto-skips on **blessed-constraint coverage** and UX-F2a auto-skips on **perceptual-distance floor AND semantic-affordance classifier** — a skip requires satisfying **BOTH paths (AND)**, so neither weak path alone authorizes skipping the perceptual verdict ([lifecycle.md §2](./lifecycle.md) mirrors this). Per-change **UX-HITL load is published in the cost model** ([§3](#3-compute-as-a-gate-cost-model-core)). *(UX-F2a)*
- **Render-dependency scoping is conservative (fail-wide).** Frontend scoping is unsound under global tokens / shared base components / theme / runtime-conditional rendering, so it requires a **conservative OVER-approximation with a measured render-reachability recall floor** (mirroring [governance.md §1f](./governance.md)): a change to **shared tokens / base components / global config / theme → "affects ALL dependent screens"** (fail-wide), and the **runtime-conditional state-space is excluded from scoping unless an explicit enumerated state manifest exists**. *(UX-F2b)*
- **Tolerance-band / device-matrix statistical gate (3)** for visual regression: a **perceptual-distance** verdict against the **blessed baseline** across a **browser/device matrix** (a first-class dimension of the gate key), with a **flake carve-out** so font-hinting/sub-pixel noise doesn't latch a deploy. **Baseline granularity vs the matrix is explicit:** either **per-cell blessing** (representative + diff workflow) **OR a designated reference cell with per-cell tolerance envelopes** — not undefined. **Which cells get which is bounded so the envelope never becomes a class-3 tolerance dial** (contradicting UX-F4a "not a tolerance dial"): a cell **requires true class-5 blessing** whenever it can differ from the reference cell in **layout, affordance, or locale** (RTL / text expansion / locale-conditional rendering — see UX-F6a), and a cell **may ride a tolerance envelope ONLY** when its sole admissible deviation from the reference is **sub-pixel / font-hinting flake**; **envelopes are bounded to that flake and NEVER absorb layout / affordance / locale deltas** (those route to perceptual HITL re-baseline, UX-F4a). **Envelope-eligibility is NOT an asserted static given gated on perceptual distance alone** — that would re-import the distance-alone failure UX-F2a forbids, because a cell-specific (device/browser/runtime-conditional) regression can introduce a small-pixel/high-semantic affordance change that the envelope absorbs and the reference cell never surfaces. Eligibility is a **decision of the SAME validated, fail-closed semantic-affordance / render-dependency analysis as [UX-F2a](#frontend--ux--design-heavy)**, and is **RE-VERIFIED on every change to the cell's render graph**: a change that CAN alter layout/affordance/locale in that cell (**including a cell-specific / runtime-conditional path**) demotes it from envelope to required class-5 blessing, **fail-wide on uncertainty** (consistent with [UX-F2b](#frontend--ux--design-heavy)). A flake envelope may carry a cell only while its render graph is **proven (per change) identical-modulo-flake to a class-5-gated cell**; any cell-specific divergence routes to perceptual HITL. And the **matrix cardinality (screens × breakpoints × states × browsers × devices × locales) is folded into the published UX-HITL load model** ([§3](#3-compute-as-a-gate-cost-model-core)). The class-3 gate **guards only unintended drift** — it does not establish or move the baseline. *(UX-F4)*
- **Baseline lifecycle (class 5 owns it).** A visual baseline is established/updated **only via class-5 perceptual HITL** — a human blesses "this rendering is correct." An **intended-change PR carries an explicit "expected visual delta" approval** that re-baselines the affected screens; **re-baseline authority is the perceptual approver, not a tolerance dial.** The class-3 gate ([UX-F4](#frontend--ux--design-heavy)) then guards unintended drift from the blessed baseline. *(UX-F4a)*
- **A11y dual-gate (honest split):** a runner-signed **deterministic, BLOCKING** WCAG gate (axe/Lighthouse rules from the run-immutable external config — reuses the SAST grounding) is the enforced floor; remaining checks are **ADVISORY**. The **screen-reader / AT-user step is a scheduled external-human dependency** (same honest framing as usability testing, [UX-F3](#frontend--ux--design-heavy)), not internally generable. Stated guarantee per trust mode: **Autonomous = deterministic rules + structural heuristics only; full WCAG conformance requires the out-of-band AT step.** *(UX-F5)*
- **CWV/bundle-budget, design-system/token conformance** runner gates (thresholds in run-immutable config); **Figma design-source** as an input artifact with a **design→build handoff-fidelity** eval that is **structural/semantic (layout-tree / token correspondence), not pixel-exact**.
- **Localization is perceptual / state-explosion, not a threshold gate.** Locale is a **declared dimension of the visual matrix + baseline key** (**pseudo-localization + key RTL / expansion locales**), and **locale-conditional rendering routes through scoped perceptual HITL** ([UX-F2a](#frontend--ux--design-heavy)) — not a univariate i18n/RTL threshold. *(UX-F6a)*
- Perceptual **render-capture uses the zone-A-renders / zone-B-captures-and-signs-over-sealed-channel pattern** ([governance.md §1c](./governance.md)): the untrusted sandbox renders, trusted infra captures and signs, so perceptual evidence inherits the existing anti-forgery grounding. *(UX-F6)*
- **Honesty:** *pre-release* UX correctness is **bounded** to heuristic/automatable checks + human design review; genuine usability validation requires a **real-user step Foundry schedules and incorporates** (a usability-test HITL artifact) but **cannot internally generate** — the LLM Client Proxy is not a substitute. *(UX-F3)*

## 6. "Factory eval" vs "product eval" (disambiguation)

[evaluation.md](./evaluation.md) is Foundry's eval **of itself** (does the factory
beat a single agent). It is **not** the eval suite a product must ship. For ML (and
increasingly for any product), the **product's own eval/monitoring is a required
delivered artifact** produced *by* the pipeline, with its own acceptance criteria —
never conflated with the factory's self-eval. Packs make the product eval/monitoring
a required output.
