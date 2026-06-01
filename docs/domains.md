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
| 4 | **Human-witnessed / attested** *(new)* | a signed human record | a **named, credentialed human** executes/approves a protocol (IQ/OQ/PQ, design-control review, per-artifact approver-of-record) | **the human** (identity + credential, authenticated) | yes | a missing/withdrawn attestation disqualifies |
| 5 | **Perceptual / judgment** *(new; generalizes the Threat-review residual)* | honestly **not** deterministic | independent **cross-family LLM critique + MANDATORY HITL** over a **rendered/evidence bundle** | n/a (advisory model + human) | **no** — excluded from the signed predicate, but **still first-class and gated** (not optional/advisory) | blocked until human sign-off; never auto-approved |

Class 3 signs `(metric_name, eval_dataset_hash, eval_seed, n, point_estimate, ci,
decision_rule, status)` and re-derives under the pinned seed+data, so a metric
that crosses threshold on resample does *not* produce an unrecoverable false latch
(it latches on the *decision*, not a noisy digest). Class 5 reuses the **exact
pattern the Security gate already uses** for its "judgment residual" — extended
from one security carve-out to a first-class class with a required evidence bundle.

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
- **Per-artifact approver-of-record (class 4):** design, test protocol, risk analysis, and validation report each require a **credentialed human** attestation bound into WORM + predicate — agents *draft*, humans *own*. *(Regulated-F2)*
- **Verification = evidence:** a **requirement → design → risk → verification-evidence traceability matrix** is a required predicate artifact; **human-witnessed validation (IQ/OQ/PQ)** is a class-4 verdict on equal footing with runner oracles, not "advisory." *(Regulated-F3)*
- **Data boundary:** **no third-party model/trace egress** profile; **no real PHI/PAN** enters the pipeline (synthetic data); judge-decorrelation falls back to separated-context same-family + stronger oracle + human review. *(Regulated-F4)*
- **Regulatory-significance tiering axis**; **identity-bound WORM** + **retention floor** (years/decades) with referenced artifacts pinned for the horizon. *(Regulated-F5, F6)*

### ML / data systems
- **Statistical verdict class (3)** for model-eval (metric + CI + pinned eval set/seed); admission latches on the *decision*, not digest noise. *(ML-F1)*
- **`state_sha`** includes data/feature/weight refs; data change dirties data-dependent gates. *(ML-F2)*
- **Compute-cost budget + expensive-gate handling** (proxy eval + retrain-confirm escalation). *(ML-F3)*
- **Product eval-suite + monitoring/drift detectors are required *shipped* artifacts** (distinct from Foundry's *own* eval — see §6); **drift/metric-decay/retrain-trigger** are first-class evolution-route inputs ([lifecycle.md §3](./lifecycle.md)). *(ML-F4)*
- **Model-vs-code rollback:** a weight ref is a deployable/rollback-able unit; "rollback" = prior attested *state*, not prior code digest. *(ML-F5)*

### Frontend / UX / design-heavy
- **Perceptual/judgment verdict class (5)** for look/feel/interaction/copy, with a **mandatory, non-auto-approvable visual/UX HITL** over a **rendered-artifact bundle** (key screens × breakpoints × states, captured by trusted infra) — this is how "taste is not delegated to a model" ([lifecycle.md §2](./lifecycle.md)) becomes *implemented*, not just stated. *(UX-F1, F2)*
- **Tolerance-band / device-matrix statistical gate (3)** for visual regression: a **perceptual-distance** verdict against a signed baseline across a **browser/device matrix** (a first-class dimension of the gate key), with a **flake carve-out** so font-hinting/sub-pixel noise doesn't latch a deploy. *(UX-F4)*
- **A11y dual-gate:** a runner-signed **deterministic** WCAG gate (axe/Lighthouse rules from the run-immutable external config — reuses the SAST grounding) **plus** an **a11y-judgment** sub-gate (screen-reader flow + AT-user, HITL). *(UX-F5)*
- **CWV/bundle-budget, design-system/token conformance, i18n/RTL** runner gates (thresholds in run-immutable config); **Figma design-source** as an input artifact with a **design→build handoff-fidelity** eval. *(UX-F6)*
- **Honesty:** *pre-release* UX correctness is **bounded** to heuristic/automatable checks + human design review; genuine usability validation requires a **real-user step Foundry schedules and incorporates** (a usability-test HITL artifact) but **cannot internally generate** — the LLM Client Proxy is not a substitute. *(UX-F3)*

## 6. "Factory eval" vs "product eval" (disambiguation)

[evaluation.md](./evaluation.md) is Foundry's eval **of itself** (does the factory
beat a single agent). It is **not** the eval suite a product must ship. For ML (and
increasingly for any product), the **product's own eval/monitoring is a required
delivered artifact** produced *by* the pipeline, with its own acceptance criteria —
never conflated with the factory's self-eval. Packs make the product eval/monitoring
a required output.
