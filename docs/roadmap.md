# Roadmap — phased delivery, sequenced to learn cheaply

Each phase has explicit exit criteria. The sequencing is deliberately reordered
around two principles the premise review surfaced: **learn whether the core bet
is true before building the expensive machinery**, and **earn autonomy (and its
trust-root cost) incrementally** rather than front-loading it.

## Guiding principles (why this order)

- **Falsify early.** The central bet — org-structure beats a strong single agent
  on long-horizon work — is tested by a *decision-grade* A/B at **Phase 1.5**,
  with a real **kill/redesign branch**, *before* the most expensive subsystems
  (trusted runner, held-out-mutant discriminators, supply-chain trust root,
  specialist registry) are built. The full Phase-6 A/B is confirmation, not first
  contact. *(P-F1)*
- **Roles earn their place.** Start with the smallest role set (PM → Architect →
  Senior → QA) and add each further role only when an **ablation** shows it
  improves the scorecard. The 16-role org is a *ceiling to grow into*, not a given
  — the cited evidence (MetaGPT/ChatDev) validates ~5 roles, not 16. *(P-F2)*
- **Earn autonomy.** Ship in **Assistant mode** first (Foundry opens a PR a human
  merges; ordinary CI), graduate to **Gated**, and build the **trust root**
  (two-zone runner, SLSA-L3, admission, deployer) only for **Autonomous mode**,
  which most adopters won't initially want ([lifecycle.md §4](./lifecycle.md)).
  This defers the hardest, most novel engineering until autonomy is demanded.
  *(P-F4)*
- **Effort is estimated, not assumed.** Each phase carries a rough team/quarter
  estimate. These are planning figures, not commitments. *(P-F3)*

## Smallest version that delivers value

**Phases 0–1 in Assistant mode** (PM→Architect→Senior→QA producing a reviewed PR
behind Spec/Build/Test gates, no trusted-runner attestation, no specialist system,
single model family). That is the minimum that produces useful software a human
merges — and the substrate for the Phase-1.5 A/B. Everything after is earned.

**Adopter→mode→phase mapping (made explicit).** The first adopter is **not** a
regulated/audit-bound team: regulated work is Gated-or-higher by construction and
only gets real teeth at the Phase-4 trust root ([lifecycle.md §4](./lifecycle.md)),
so it cannot draw value from the thin-differentiation Assistant mode that ships
first. The honest early wedge is a **non-regulated team that values
criterion-linked structured PRs + review/test discipline** — value lands in
**Assistant (Phases 0–1)** and deepens at **early Gated (Phase 2)** via distinct
gates, risk-tiering, and recorded Design/Release sign-off. Regulated pilots are
deliberately **later** (Gated from Phase 2, full guarantees at Phase 4), not served
by deferred modes dressed up as an early wedge — consistent with the
incremental-autonomy sequencing and [PLAN.md §9](./PLAN.md)/[PLAN.md §10](./PLAN.md).

---

## Phase 0 — Foundations  · ~1 team-quarter
**Goal:** one agent runs a task safely, observably, resumably, idempotently.
- Reducer-annotated `FactoryState` (`seq`/`run_id`, `None`-safe reducers); LangGraph skeleton + checkpointer; **transactional outbox + external-action ledger**; sandbox runner; OpenTelemetry; policy-plane stub; **basic operator run-status (§ operations.md §2)**.
**Exit:** trivial task runs end-to-end, traced, checkpointed; **kill-then-resume produces no duplicate commit/PR**.

## Phase 1 — Core vertical slice (Assistant mode)  · ~1–2 team-quarters
**Goal:** the smallest real org that produces a useful, human-merged PR.
- PM → Architect → Senior Eng → QA subgraphs; Spec/Build/Test gates (distinct), criterion-traceable tests, flake quarantine; content-addressed artifacts; **Assistant mode** (PR + human merge, ordinary CI — no trusted runner/attestation yet).
**Exit:** from a one-line request, produces a small working app passing its own + hidden tests, opened as a PR a human merges with minimal edits.

## Phase 1.5 — DECISION GATE: powered A/B + role ablation  · ~1 team-quarter
**Goal:** decide the central bet **before** expensive machinery. *(P-F1, P-F2)*
- Run a **decision-grade, pre-registered A/B** (Foundry-slice vs strong agentic single-agent baseline vs single-agent+gates, equal-USD, sized `n`/`k`) on a SWE-EVO-style subset; run a **role ablation** (does adding each role past PM→Architect→Senior→QA help?).
- **A separate variance/base-rate PILOT runs first** (on **disjoint tasks**, before the scored A/B): it establishes this study's control-arm resolution rate and task-level effect-size variance to **feed the power calc**, since neither is known a priori. The pilot **consumes part of the funded budget** and is read-only into the power calc — it is *not* a second scored arm or a rung to descend to ([evaluation.md §1](./evaluation.md), [evaluation.md §2](./evaluation.md)).
- **Pre-registration is binding at the *funded* power rung.** The A/B is pre-registered with its **MDE committed before any scored run**, sized to the power rung the program is actually willing to fund — no post-hoc rung selection, no reaching for a smaller MDE after seeing data. A **null at the funded MDE is a real kill trigger**, not an invitation to re-power. (Consistent with [evaluation.md §2](./evaluation.md) and [evaluation.md §8](./evaluation.md).)
**Exit (a real branch point):**
- If structure **beats** baseline by the pre-registered margin at ≤ cost → proceed, carrying only the roles the ablation justified.
- If it **does not** (a null at the funded MDE) → **stop and redesign** (smaller role set, or gates-only, or abandon the org thesis). Sunk cost is Phases 0–1, not 0–5.

## Phase 2 — Review & assurance (Gated mode)  · ~1–2 team-quarters
**Goal:** quality/safety for a human-approved release candidate.
- Reviewer + evaluator-optimizer loop; Security/Audit agents + gates; **risk-tiering** so trivial changes don't pay full freight ([governance.md §1e](./governance.md)); dependency-scoped re-verification ([governance.md §1f](./governance.md)); HITL at Design/Release; WORM audit; **Gated mode** (full gates, human approves; signing still optional).
**Exit:** a known-vulnerable change is caught; Design/Release require recorded sign-off; re-verification is change-proportional; rework loops terminate within caps and HITL volume is measured.

## Phase 3 — Operability & scale  · ~1–2 team-quarters
**Goal:** runnable in production by an operator, across concurrent runs.
- Operator dashboard + SLOs + alerting + runbooks; **run-status liveness (paused vs wedged)** + watchdog; **store HA/backup + poisoned-checkpoint recovery**; **multi-tenancy/capacity/queue/fairness** + fleet cost-rate cap; **HITL-load model + reviewer queue/SLA**; deterministic monitors + sandboxed analyzer; five guardrail planes (sans full trust root). See [operations.md](./operations.md).
**Exit:** an operator can diagnose a stuck run; a store outage pauses (not corrupts) in-flight runs and recovers; concurrent runs don't starve each other; HITL backlog is bounded and alerted.

## Phase 4 — Trust root & Autonomous mode  · ~2 team-quarters (research-grade)
**Goal:** earned hands-off deployment — built only because Phases 1.5–3 proved value and demand.
- **Two-zone trusted gate runner** (config re-derivation; sealed-channel execution observation; held-out-mutant + behavioral-invariance discriminator); strongly-hermetic **SLSA-L3** build; **deploy-time admission** + non-agent deployer; input-bound attestations; trusted-component HA + lifecycle/rotation ([operations.md §4/§8](./operations.md)).
**Exit:** a malicious-test / forged-results / forged-verdict / fork-divergence attempt cannot reach production; trusted-component outage degrades to *paused*, not *wedged*.

## Phase 5 — Specialist plugin system + domain core hooks  · ~1–2 team-quarters
**Goal:** general core, expert on demand — *honest about each domain's correctness signal.*
- Specialist interface + registry + capability routing; **the core pack hooks** ([domains.md §4](./domains.md)): the **verdict-class taxonomy** (statistical / human-witnessed / perceptual, with the discriminator made gate-class-aware), **`state_sha`** (data/weight refs), the **compute-cost budget** dimension, **mode/tier veto + regulatory-significance axis**, **per-artifact approver-of-record**, **data-egress profiles**, and **mandatory perceptual HITL + rendered evidence**.
- First domain packs against those hooks: **regulated** (fintech/healthcare), **ML/data**, **frontend/UX**.
**Exit:** a regulated request makes Autonomous/auto-approve unreachable and binds human approver-of-record records; an ML request keys verdicts on `state_sha` (data) with a statistical model-eval gate and compute budget; a UI request forces a visual HITL over a rendered bundle — all without core changes *to add the next pack*.

**Wedge note (frontend/UX lands late — name it honestly).** The frontend/UX pack
arrives here, in Phase 5 (~8–11 quarters in), while the smallest-useful milestone
(Phases 0–1) is a **backend PR generator**. Rather than implicitly claiming
"general-purpose" from day one, the near-term product is **honestly re-scoped as
backend-logic-first, with UX as an explicit later bet** — consistent with earning
autonomy (and pack breadth) incrementally rather than front-loading it. The
adoption/wedge and competitive analysis behind this framing — and the option to
pull a minimal UX-validation slice forward if the "general-purpose" claim needs
earlier evidence — lives in [PLAN.md §9](./PLAN.md) and [PLAN.md §10](./PLAN.md).
*(Note: the verdict-class taxonomy and `state_sha` are **core** generalizations; the statistical/human-witnessed/perceptual classes are designed in alongside Phase 2's gates and Phase 4's trust root, not bolted on here.)*

## Phase 6 — Eval, optimization & lifecycle  · ongoing
**Goal:** measured, tuned, and owning software over time.
- Full eval harness + the **decisive (confirmatory) A/B**; constrained workflow optimization (incl. tiering as a validated output); product-feedback loop + generated-code maintenance/provenance ([lifecycle.md](./lifecycle.md)); red-team; cost/latency hardening against the instantiated per-feature **and** A/B-scale cost model ([evaluation.md §8](./evaluation.md)).
- **Confirmatory roles are asymmetric (not two independent gates).** Exactly **one**
  A/B run is the designated confirmatory arbiter, spending its own pre-registered
  alpha; the other is **estimation/replication only** — it informs but is **not** a
  second gate ([evaluation.md §1](./evaluation.md), [evaluation.md §2](./evaluation.md)).
  Phase 1.5 is the kill-cheap arbiter that gates Phases 2–5; Phase 6 is the
  confirmatory arbiter for the **Autonomous** claim. A **Phase-6 disconfirmation
  cannot un-spend the sunk Phases 2–5** (that cost is already committed), but it
  **blocks the Phase-6 exit / Autonomous claim**.
**Exit:** scorecard published and stable; confirmatory A/B reproduces the Phase-1.5 result; optimization Pareto-improves ≥1 headline metric with no regression in escaped-defects, cost, wall-clock, or policy compliance.

## Sequencing notes

- **Reducers, outbox, observability, run-status, and the policy plane are Phase
  0–1 foundations** — concurrency, idempotent resume, and operability underpin
  everything later.
- **The decision gate (1.5) precedes the expensive machinery**, so the program can
  learn "structure doesn't pay" cheaply and redesign.
- **The trust root (Phase 4) is deferred** until Autonomous mode is demanded; most
  value (Assistant/Gated) ships without it.
- These phase estimates total roughly **8–11 team-quarters** for a small senior
  team to reach Autonomous + specialists — a multi-year program. The honest
  smallest-useful milestone (Assistant mode, Phases 0–1) is ~2–3 quarters.
