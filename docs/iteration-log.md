# Iteration log — hardening the plan against today's state of the art

The request was to review the plans in a loop — **critic (findings) → solutioner
→ full rewrite of the affected doc → back to critic, until all findings are
solved**. This document records that loop honestly: the *real* findings raised by
an independent adversarial critic agent, how each was resolved (with the concrete
mechanism, not just a citation), the conformance mapping to current practice, and
an honest maturity assessment with the gaps that genuinely remain open.

> This replaces an earlier, self-congratulatory version of this doc whose
> "critique passes" were strawmen (a naive linear chat chain no serious 2025
> design would start from). A reviewer correctly flagged that as
> buzzword-heavy/mechanism-light marketing of the plan's own rigor. The record
> below is the actual review.

## Round 1 — independent critic: 16 findings (2 BLOCKER, 9 MAJOR, 5 MINOR)

| # | Sev | Doc | Problem (abridged) | Resolution (mechanism) |
|---|---|---|---|---|
| 1 | BLOCKER | architecture | Plain `list` state channels crash/drop on parallel writes (no reducer) | State is now `TypedDict` with `Annotated[..., reducer]` on every fan-in channel; each channel labeled single-writer vs fan-in; custom dedupe-merge for artifacts. [architecture §3](./architecture.md) |
| 2 | BLOCKER | architecture | Shared git tree across parallel agents undefined; no merge/conflict model | Branch-/worktree-per-task isolation; per-branch gates; explicit deterministic **integration node** + conflict-resolution step; integrated gate authoritative; idempotent retry resets branch to base. [architecture §4](./architecture.md) |
| 3 | MAJOR | governance | "Deterministic" gates conflate decision rule with non-deterministic execution; mutation testing too costly per-gate | Separated *decision rule* from *execution* in `GateResult`; flake quarantine + seed pinning + retry cap; mutation testing moved off the hot path; retry-until-green is a monitored signal. [governance §1/§1a](./governance.md) |
| 4 | MAJOR | governance | QA LLM both writes and blesses tests (circular trust) | Test authorship separated from implementation (different role + model instance); per-criterion traceability + **criterion-coverage** gate; property/human-seeded oracles for high-risk criteria; hidden tests in golden suite. [governance §1b](./governance.md), [evaluation §2](./evaluation.md) |
| 5 | MAJOR | architecture | Ambiguous parallel-gate fan-in/loops; release-reject re-decomposes everything | Explicit **assurance join**; **re-verification rule** (code change ⇒ re-run Build+Test before downstream); release rejection routes to targeted fix; single-entry/single-exit subgraphs. [architecture §1](./architecture.md) |
| 6 | MAJOR | PLAN/agents | Context-window/long-horizon state, repo retrieval, model pinning, and silent T1 downgrade unaddressed | Per-node **context-assembly contract** + history compaction; concrete retrieval (symbol index + embeddings + rerank); pinned model IDs + same-tier failover; **T1 never auto-downgrades** (escalates). [architecture §6](./architecture.md), [governance §7](./governance.md) |
| 7 | MAJOR | agents | Guardian = vague mid-tier LLM doing open-ended "reward-hacking" detection | Split into **deterministic monitors** (loop=state-signature, cost=ledger, policy=OPA denies, retry-until-green) and a **sandboxed, toolless analyzer** with named features + its own evals; doesn't share acting agents' injection surface. [governance §3](./governance.md) |
| 8 | MAJOR | governance | SLSA/signing produced inside agent-controlled sandbox = theater; key mgmt absent | Attestation/signing moved to a **trusted hermetic CI builder** outside agent control; **keyless OIDC/sigstore**; keys air-gapped from sandboxes; SLSA L3 target + provenance predicate; concrete SBOM pass criteria. [governance §4](./governance.md) |
| 9 | MAJOR | architecture/agents | Budget = single LLM-estimated cap, no per-loop limits, no mid-flight semantics, monitor overhead ignored | Per-phase/per-loop sub-budgets w/ default percentages; **graceful checkpoint-and-pause** mid-flight; monitor overhead metered (≤5%); prompt-caching strategy. [governance §7](./governance.md) |
| 10 | MAJOR | PLAN/eval | Over-claim: asserts L4 / "structure helps long-horizon" with no baseline, direction-only targets | Added **controlled A/B vs strong single-agent baseline** on identical SWE-EVO subset (Phase-1 smoke + Phase-5 decisive exit criterion); **concrete numeric thresholds** per metric. [evaluation §1/§4](./evaluation.md) |
| 11 | MAJOR | architecture | Resume claimed but external side effects not idempotent (dup PRs/tags/deploys) | **External-action ledger** + idempotency keys; pure vs effectful node split; check-then-act; time-travel rewinds internal state only (forks get new run IDs). [architecture §5](./architecture.md) |
| 12 | MINOR | architecture | Bugfix route bypasses Spec gate → Test gate has no oracle | Bugfix first produces a **repro + expected-behavior brief** that *is* its acceptance criterion, through the Spec gate. [architecture §1](./architecture.md) |
| 13 | MINOR | agents/governance | Gate ownership inconsistent across docs (Build vs Test vs combined) | One decomposition everywhere: distinct **Build** (CI runner) and **Test** (QA) gates, each run per-branch + integrated. [governance §1](./governance.md), [agents](./agents.md) |
| 14 | MINOR | agents | Optional roles (UX, Specialist) not modeled as conditional in control flow | Modeled as **conditional edges** keyed on `required_capabilities`/project type; consumers tolerate absent optional artifacts. [architecture §7](./architecture.md) |
| 15 | MINOR | iteration-log | Self-congratulatory strawman "critique passes"; citations without mechanism | This doc rewritten to the real critic loop; each cited technique now states its mechanism + cost. |
| 16 | MINOR | governance | In-state `history` called "immutable audit log" but it's mutable/rewindable | Audit events persisted to a **separate append-only WORM/hash-chained store** distinct from the checkpoint; forks don't mutate the original record. [governance §4](./governance.md) |

**Round 1 verdict (from solutioner after rewrites):** all 16 addressed across
PLAN.md, architecture.md, governance.md, agents.md, evaluation.md, roadmap.md.
Re-submitted to a fresh critic for Round 2.

## Round 2 — fresh critic on the revised package: 10 findings (1 BLOCKER, 5 MAJOR, 4 MINOR)

Round 2 mostly caught bugs the Round-1 *fixes* introduced — exactly what a
second pass is for.

| # | Sev | Doc | Problem (abridged) | Resolution (mechanism) |
|---|---|---|---|---|
| 1 | BLOCKER | architecture | `merge_artifacts` keyed on `(kind, produced_by, version)` → parallel same-kind branch artifacts collide and are dropped | Re-keyed the reducer on the **content-addressed `uri`** (true set-union by identity); added `task_id` to `Artifact`. [architecture §3](./architecture.md) |
| 2 | MAJOR | architecture | Custom reducer crashes on `None` left operand (first write) | All custom reducers now `a = a or []`-guarded; documented the requirement. [architecture §3](./architecture.md) |
| 3 | MAJOR | architecture | Diagram had both `PROD→PLAN` and `PROD→SPECGATE→PLAN` (Spec gate bypassable) | Removed the direct edge; PROD → Spec gate → PLAN only. [architecture §1](./architecture.md) |
| 4 | MAJOR | architecture | Bugfix path lacked the `ownership_map` the integration node needs for deterministic merge | Bugfix is single-branch and **bypasses integration**; documented lexical-`task_id` fallback for any other missing-`ownership_map` case. [architecture §4](./architecture.md) |
| 5 | MAJOR | architecture/agents/governance | SDET e2e sat before Release with **no gate and no fail edge** | Added an explicit **E2E gate** (owner SDET) with `fail → FIX`; added to the gate table and SDET's contract. [architecture §1](./architecture.md), [governance §1](./governance.md) |
| 6 | MAJOR | architecture | `gates` channel append-only with no "current verdict" semantics → supervisor could read a stale PASS | Keyed reducer `merge_gates`: **latest verdict per `(name, run_against)` wins**; verdict history retained in `history`. [architecture §3](./architecture.md) |
| 7 | MINOR | governance | "zero criticals quarantined" (§1) contradicted "block until fixed or accepted" (§1a) | Unified to one rule in both places: high-risk-criterion quarantine blocks; others need recorded QA acceptance. [governance §1/§1a](./governance.md) |
| 8 | MINOR | governance | Budget summed to 100% with no contingency, so designed-in rework always breaches | Carved a **10% Rework reserve** (Impl 45→35); per-loop caps draw from the reserve, not the phase. [governance §7](./governance.md) |
| 9 | MINOR | agents/architecture | Release gate required a signature that `ATTEST` only produced *after* it | Reordered: **ATTEST precedes RELGATE**; human signs off on an already-attested artifact. [architecture §1](./architecture.md), [agents Release Mgr](./agents.md) |
| 10 | MINOR | architecture | Integrated-gate failure routed to full `DECOMP` (re-decompose), contradicting the targeted-fix rule | `IBUILD fail → FIX`; `→ DECOMP` reserved only for structural design defects (edge relabeled). [architecture §1](./architecture.md) |

**Round 2 verdict (from solutioner after rewrites):** all 10 addressed. Re-submitted to a fresh critic for Round 3.

## Round 3 — fresh critic: 5 findings (0 BLOCKER, 2 MAJOR, 3 MINOR)

Convergence is visible: no blockers, and the issues are diagram/consistency
residue from the Round-2 edits.

| # | Sev | Doc | Problem (abridged) | Resolution (mechanism) |
|---|---|---|---|---|
| 1 | MAJOR | architecture | `SPECIALIST` and `UX` were dead-end nodes (no outbound edge) | Added `SPECIALIST → DESIGNGATE` and `UX → DESIGNGATE` (fan-in to the Design gate). [architecture §1](./architecture.md) |
| 2 | MAJOR | architecture | Bugfix routed `SPECGATE → IMPL`, skipping `DECOMP` — the node that provisions the branch/`task_id` keys everything depends on | Bugfix now routes `SPECGATE → DECOMP`, which emits exactly one task/branch; still bypasses integration. [architecture §1/§4](./architecture.md) |
| 3 | MINOR | architecture | Unlabeled `REVIEW → PTEST` edge; Reviewer + QA conflated in one node, blurring §1b separation | Edge labeled `approve`; node relabeled "Reviewer reviews diff · QA authors/runs tests · separate models". [architecture §1](./architecture.md) |
| 4 | MINOR | architecture | A `FIX` re-running only Build+Test left **stale** integrated `Security/Audit/E2E` PASS verdicts (keyed on constant `"integrated"`) | Integrated gates now key on the **integrated-tree content hash (`tree_sha`)**, so a changed tree invalidates prior verdicts and forces re-run of all integrated gates. [architecture §3/§4](./architecture.md) |
| 5 | MINOR | PLAN | PLAN §4 ASCII flow still showed Release-gate-before-builder (pre-fix ordering); PLAN §7 omitted the E2E gate | Fixed ASCII to attest-then-release; added E2E as gate #7 so all docs list the same 8 gates. [PLAN §4/§7](../PLAN.md) |

**Round 3 verdict (from solutioner after edits):** all 5 addressed. Re-submitted to a fresh critic for Round 4.

## Round 4 — fresh critic: 3 findings (0 BLOCKER, 3 MAJOR)

A tight cluster on the two routes not yet hardened (evolution/spike) and the
multi-task fix re-merge.

| # | Sev | Doc | Problem (abridged) | Resolution (mechanism) |
|---|---|---|---|---|
| 1 | MAJOR | architecture | Evolution/spike routed `TRIAGE → ARCH`, bypassing PROD/Spec gate/PLAN → no `acceptance_criteria` (Test gate has no oracle); the `SPECGATE→PLAN` "evolution" label was unreachable | Evolution now routes through **PROD (change brief) → Spec gate → PLAN → ARCH** like greenfield; **spike** gets its own throwaway terminal node (not released). [architecture §1](./architecture.md) |
| 2 | MAJOR | agents/architecture | On the evolution path the Architect (T1) ran with `prd`/`sprint_plan` absent and **no budget seeded** — the whole budget plane is relative to a cap that was never set | **Triage seeds a default per-route budget cap** (refined by PLAN); evolution now visits PROD+PLAN so the Architect's inputs exist. [architecture §1](./architecture.md), [agents Triage/Eng-Mgr](./agents.md), [governance §7](./governance.md) |
| 3 | MAJOR | architecture | Multi-task `FIX → IBUILD` skipped `integration_node`, so the integrated tree/`tree_sha` never changed — fix "verified" against a stale tree, defeating the Round-3 `tree_sha` invalidation | `FIX → INTEG` (re-merge) for multi-task runs produces a new tree/`tree_sha`; `FIX → IBUILD` only for the single-branch bugfix case. [architecture §1/§4](./architecture.md) |

**Round 4 verdict (from solutioner after edits):** all 3 addressed. Re-submitted to a fresh critic for Round 5.

## Round 5 — fresh critic: 2 findings (0 BLOCKER, 1 MAJOR, 1 MINOR)

| # | Sev | Doc | Problem (abridged) | Resolution (mechanism) |
|---|---|---|---|---|
| 1 | MAJOR | architecture | Post-per-branch routing was **route-name-based** (`multi-task` vs `bugfix`), stranding a **single-task greenfield/evolution** run with no matching PTEST/FIX edge | Predicate is now **task-count-based** everywhere: `tasks > 1 → INTEG`, `tasks == 1 → IBUILD` (bugfix is just the canonical `tasks == 1` case). [architecture §1](./architecture.md) |
| 2 | MINOR | architecture | Prose still framed single-branch/integration-bypass as bugfix-specific | Generalized §1/§2/§4 wording to "any single-task run"; integration "runs when `tasks > 1`". [architecture §2/§4](./architecture.md) |

**Round 5 verdict (from solutioner after edits):** both addressed. Re-submitted to a fresh critic for Round 6.

## Round 6 — fresh critic: VERDICT **NO MATERIAL FINDINGS** (loop terminates)

The critic confirmed all of the following are solid: task-count routing
(`tasks > 1` vs `tasks == 1`) consistent across diagram/prose/subgraphs; all 8
gates (Spec, Design, Build, Test, Security, Audit, E2E, Release) consistent
across PLAN §7, governance §1, and the agents I/O table; all 4 routes
(greenfield/evolution/bugfix/spike) specced + budgeted + oracle'd + terminating
cleanly; reducer code None-safe and identity-keyed with `tree_sha` invalidation;
idempotency/side-effect claims coherent; no dead-end or unreachable nodes.

One **MINOR** was raised and fixed for completeness:

| # | Sev | Doc | Problem | Resolution |
|---|---|---|---|---|
| 1 | MINOR | architecture | Spec-gate `fail` edge returned unconditionally to `PROD`, but bugfix enters Spec from `MINSPEC` | Fail edge is now route-discriminated: `greenfield/evolution: fail → PROD`, `bugfix: fail → MINSPEC`; added a prose rule that gate failures return to the route's producer. [architecture §1](./architecture.md) |

## Loop summary

| Round | Findings | BLOCKER | MAJOR | MINOR | Verdict |
|---|---|---|---|---|---|
| 1 | 16 | 2 | 9 | 5 | material findings remain |
| 2 | 10 | 1 | 5 | 4 | material findings remain |
| 3 | 5 | 0 | 2 | 3 | material findings remain |
| 4 | 3 | 0 | 3 | 0 | material findings remain |
| 5 | 2 | 0 | 1 | 1 | material findings remain |
| 6 | 1 | 0 | 0 | 1 | **NO MATERIAL FINDINGS** |

Total: **37 findings raised and resolved** over six critic→solutioner→rewrite
rounds. The loop terminated when an independent critic, calibrated to a
design-doc standard, returned no BLOCKER or MAJOR findings; the single residual
MINOR was also fixed. This log is the honest record the process produced — not a
marketing artifact.

## Conformance mapping — plan vs. industry practice (2024–2026)

For each, the *mechanism* the plan adopts (not just the citation):

| Practice | Source | Mechanism in Foundry |
|---|---|---|
| SOP-driven roles; structured-artifact comms | MetaGPT | Content-addressed versioned artifacts; role nodes with explicit SOPs ([agents](./agents.md)) |
| Communicative multi-agent dev with hand-offs | ChatDev | Evaluator-optimizer review loop; typed hand-offs |
| Graph orchestration: nodes, conditional edges, durable state, interrupts, **reducers** | LangGraph | `StateGraph` + reducer channels + checkpointer + `interrupt()` ([architecture](./architecture.md)) |
| Orchestrator-workers, evaluator-optimizer, routing, parallelization, reflection, HITL | Anthropic, *Building Effective Agents* | Mapped to concrete nodes ([architecture §8](./architecture.md)) |
| Agent-computer interface; sandboxed execution | SWE-agent / OpenHands | Execution plane: containers/microVMs, default-deny egress ([governance §2](./governance.md)) |
| Long-horizon evolution is the real bar | SWE-EVO | The decisive A/B is run on a SWE-EVO subset ([evaluation §1](./evaluation.md)) |
| Five-plane guardrails; SBOM/SLSA; evals in CI; supply-chain trust root | Enterprise agent-safety practice; SLSA | Trusted hermetic builder + keyless signing + SLSA L3; evals gate every change ([governance §2/§4](./governance.md)) |
| Workflow auto-optimization | MetaGPT AFlow | Defined search space + constrained objective + Pareto promotion ([evaluation §6](./evaluation.md)) |

## Maturity rubric

| Level | Description | Phase that reaches it |
|---|---|---|
| L0 | Single agent writes code, no verification | pre-Phase 0 baseline |
| L1 | Role agents + structured artifacts, manual checks | Phase 1 |
| L2 | Automated gates + review/security/audit + HITL + trusted-builder signing | Phase 2 |
| L3 | Correct parallel integration, full guardrails, deploy/rollback, deterministic monitors | Phase 3 |
| L4 | Pluggable specialists + measured, A/B-validated, optimized, hardened | Phases 4–5 |

The plan **targets L4 but does not claim to have achieved it** — L4 is gated on
the Phase-5 A/B actually showing the structure beats a single agent. Most public
multi-agent coding systems today sit L1–L3.

## Residual gaps (honestly held open)

- **Long-horizon reliability is unsolved industry-wide** (~21% SWE-EVO). The org
  structure is a *hypothesis*; the A/B may show it doesn't help enough, which
  would force a redesign. That risk is real and not assumed away.
- **Cost at L3+** is significant; sub-budgets and caching bound it but the
  cost/quality frontier needs the Phase-5 tuning to be acceptable.
- **Eval trustworthiness** depends on resisting overfitting and judge drift;
  hidden hold-outs, the baseline A/B, and recalibration are mitigations, not
  guarantees.
- **Analyzer detection of semantic reward-hacking** is inherently imperfect; the
  deterministic monitors are the reliable floor, the analyzer is advisory.
