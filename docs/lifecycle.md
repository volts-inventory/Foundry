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
- **Taste is not delegated to a model.** The Stakeholder/Client Proxy is an *LLM
  stand-in* used to keep runs moving; it does **not** replace the real product
  owner. High-ambiguity or high-taste decisions route to a human, and the Proxy's
  assumptions are surfaced for correction.
- **Collaborative, not fire-and-forget:** the default operating mode (§4) is a
  human partner reviewing and steering, with autonomy earned incrementally.

## 3. Product-feedback loop

The pipeline currently ends at "Released." Real products iterate on user reality.

- **Close the loop from production telemetry:** shipped software emits usage/error
  telemetry that feeds *new* evolution-route requests (regressions, unused
  features, performance hotspots) — Foundry's input is not only human tickets but
  observed product behavior.
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
