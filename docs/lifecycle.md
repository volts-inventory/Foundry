# Product lifecycle — beyond the one-shot pipeline

A pipeline that produces a release is not the same as a system that *owns software
over its life*. This document covers the product-lifecycle concerns a real adopter
asks about first, which the pipeline-centric plan otherwise omits: maintenance of
generated code, human steering and taste, the product-feedback loop, and the
licensing/liability of AI-generated code. (Raised by the premise review, P-F5.)

## 1. Maintenance of generated code over time

Software is owned for years, not shipped once.

- **The codebase is the durable artifact, not the run.** Foundry's evolution route
  handles change requests against an existing repo, but the repo must remain
  **legible to human engineers who did not write it**: enforced docs/ADRs,
  consistent conventions, and the `pinned_constraints`/ADR set kept coherent across
  many evolution cycles (with **supersession**, [architecture.md §8](./architecture.md),
  so old decisions retire rather than accrete).
- **Constraint/ADR coherence is itself maintained:** a periodic
  architecture-review pass (an Architect agent + HITL) consolidates and prunes the
  pinned set so it doesn't drift into contradiction over a long-lived project.
- **Frontend rot is its own maintainability dimension.** ADR coherence does not
  catch UI decay, so the periodic architecture-review pass also inspects
  **component duplication/complexity**, **styling-system/design-token adherence
  drift**, and **markup/a11y-semantics quality** across evolution cycles — the
  ways a generated frontend degrades even while its decision record stays clean.
- **Human takeover must always be possible:** the output is ordinary source in
  ordinary VCS; a human team can fork, read, and modify it without Foundry. No
  lock-in to Foundry-only representations.

## 2. Human steering and taste — not just gatekeeping

In the pipeline, humans appear only as gate-approvers and escalation targets. Real
software needs humans *steering*, not just vetoing.

- **A first-class steering channel:** a human can inject direction mid-run ("this
  is technically passing but not what I meant," "prefer approach B," "descope X")
  that updates the PRD/`pinned_constraints` and re-plans — distinct from a binary
  gate approval.
- **Steering is authorship, not approval.** A human who steers a run via the
  steering channel makes a substantive content contribution and is therefore
  recorded in the **author set** for that artifact, for segregation-of-duties
  purposes. By the enforced "author != approver" predicate they **cannot also serve
  as a release/admission approver** for the same artifact (preparer/validator
  independence) — the distinctness check rejects an approver identity that
  intersects the author set ([domains.md §5](./domains.md),
  [governance.md §1e](./governance.md)).
- **Taste is not delegated to a model.** The Stakeholder/Client Proxy is an *LLM
  stand-in* used to keep runs moving; it does **not** replace the real product
  owner. High-ambiguity or high-taste decisions route to a human, and the Proxy's
  assumptions are surfaced for correction.
- **Design-iteration sub-loop (a first-class collaborative mode).** Coarse
  text→re-plan steering is too blunt for UX, where taste is iterative and visual.
  For user-facing work Foundry presents **multiple rendered variants** and lets the
  human **select, annotate, and diff at the rendering level** (not at the
  spec/plan level). Captured preferences become **durable design-system/taste
  constraints** that **persist across evolution cycles** as part of the
  `pinned_constraints`/design-token set — so they shape future builds rather than
  being re-litigated as a one-off re-plan. This is a steering channel in its own
  right. **Taste authorship persists across runs.** Because a blessed taste
  constraint encodes a human's substantive steering contribution, its
  **authorship is carried into the author set** of any *later* artifact whose
  approval is grounded in that constraint — so the constraint's author **cannot
  serve as approver-of-record** on that later artifact (preparer/validator
  independence across runs, not just within one run), enforced by the same
  "approver ∉ author set" distinctness check ([domains.md §5](./domains.md),
  [governance.md §1e](./governance.md)). Encoding settled taste **lowers the material-threshold / improves
  first-pass yield** (fewer iteration rounds per blessing), but it does **not**
  eliminate the terminal human blessing: perceptual verdicts are class-5 —
  mandatory, non-auto-approvable, never auto-approved
  ([domains.md §5](./domains.md)). So the gain is fewer rounds to reach a blessing,
  **not** a raw HITL-count reduction that would skip it. (The lone exception is a
  narrow, audited class of changes fully covered by an already-blessed taste
  constraint, which may auto-skip *only* with that constraint reference recorded as
  the approval grounding — the human blessing is inherited from the constraint, not
  waived.) This "fully-covered-by-a-blessed-constraint" predicate and the
  domains.md **UX-F2a** auto-skip (perceptual-distance floor **AND**
  semantic-affordance classifier) are **two distinct predicates over the same
  mandatory class-5 verdict**: an auto-skip requires satisfying **BOTH** (the
  strictest AND), so neither weak path alone authorizes skipping the perceptual
  verdict ([domains.md §5](./domains.md)).
- **Collaborative, not fire-and-forget:** the default operating mode (§4) is a
  human partner reviewing and steering, with autonomy earned incrementally.

## 3. Product-feedback loop

The pipeline currently ends at "Released." Real products iterate on user reality.

- **Close the loop from production telemetry:** shipped software emits usage/error
  telemetry that feeds *new* evolution-route requests (regressions, unused
  features, performance hotspots) — Foundry's input is not only human tickets but
  observed product behavior. For **ML products** the trigger taxonomy includes
  **data drift, metric decay, and retraining triggers** as first-class evolution
  inputs, and **rollback can be model-vs-code** (revert a *weight ref* behind an
  unchanged code digest — "rollback" means a prior attested *state*, not a prior
  code digest; [domains.md §5](./domains.md)).
- **Pre-release UX correctness is bounded.** For user-facing work, the LLM
  Stakeholder Proxy is **not** a substitute for real users; pre-release signals are
  limited to heuristic/automatable checks + human design review. Genuine usability
  validation requires a **real-user step Foundry schedules and incorporates** (a
  usability-test HITL artifact) but cannot internally generate — stated honestly,
  not assumed away ([domains.md §5](./domains.md)).
- **Product-level evaluation** (distinct from pipeline evals): did the change move
  the user/business metric? This is the outer loop around the inner build loop and
  is owned by humans, with Foundry as the executor.

## 4. Deployment-trust modes (earned autonomy)

Adoption does **not** start at "AI autonomously signs and deploys to prod." The
plan defines explicit modes, and the elaborate trust-root machinery is required
only at the highest:

| Mode | What Foundry does | Human role | Trust-root machinery needed |
|---|---|---|---|
| **Assistant** | Produces a PR | Reviews & merges (normal CI) | None beyond ordinary CI |
| **Gated** | Produces a release candidate behind the full gate set | Approves Design + Release | Gates + evidence pack; signing optional |
| **Autonomous** | Builds, attests, and deploys hands-off | Spot-audit + exception | Full two-zone runner, SLSA-L3, admission, deployer |

Most early adopters run **Assistant** (a human merges, so the attest→admit chain
is moot) and graduate to **Gated**/**Autonomous** only as evidence accrues. This
is why the trust-root build is **deferred** in the roadmap
([roadmap.md](./roadmap.md)) — it defends an autonomy that must be *earned*, not
front-loaded.

- **Regulated work is Gated-or-higher by construction.** Assistant mode bypasses
  the entire compliance apparatus — approver-of-record, WORM attestation, SLSA
  predicate, admission — because a human merges via normal CI. That is acceptable
  only for *unregulated* artifacts. The regulated guarantees of
  [domains.md §5](./domains.md) are therefore **scoped to Gated and above**: when
  a domain pack flags a regulated capability (the **mode/tier veto +
  regulatory-significance axis**), it forces a **minimum of Gated mode** — gates +
  per-artifact approver-of-record + WORM — and **forbids Assistant-mode merge of
  regulated artifacts**. Assistant mode is **non-compliant for regulated work**;
  the "safe by construction" claim holds only at Gated+, where the controls are
  actually engaged.

## 5. Licensing & liability of AI-generated code

The Audit gate checks *dependency* licenses/SBOM; the *generated code itself*
needs its own provenance and liability posture.

- **Generated-code provenance:** scan for verbatim/near-verbatim regurgitation of
  training data and copyleft-snippet contamination (a deterministic similarity
  check in the Security/Audit class, runner-computed); flag matches to HITL.
- **License hygiene of outputs:** the produced repo carries clear licensing; AI
  contribution is recorded in provenance/SLSA metadata.
- **Liability posture:** an explicit statement that autonomously-shipped code
  carries organizational accountability — the Release/Admission human sign-off is
  also the **accountability anchor** for Autonomous mode, and regulated domains
  (fintech/healthcare packs) may *require* a human accountable party regardless of
  mode. This is named as a policy decision for the adopting org, not silently
  assumed away.

*(All of §1–§5 address premise finding P-F5; §4 also addresses P-F4.)*
