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

## Round 7 — HIGHER BAR: three specialist critics in parallel (30 findings: 7 BLOCKER, 16 MAJOR, 7 MINOR)

Rounds 1–6 used generalist design-doc critics. Round 7 raised the bar to
**implementation-readiness**, running three specialists concurrently —
distributed-systems/correctness, offensive-security/AI-safety, and
AI-agents/LLM-systems — each empowered to challenge core assumptions. They found
deep, real defects the generalist rounds structurally could not.

### Distributed-systems (10): correctness/concurrency/atomicity

| # | Sev | Problem | Resolution |
|---|---|---|---|
| DS1 | BLOCKER | `merge_gates` "latest-wins" relied on dict-iteration order, not causality | Added monotonic `seq`; reducer is **max-by-`seq`** per `(name, run_against)`. [arch §3](./architecture.md) |
| DS2 | BLOCKER | Checkpoint↔external dual-write; idempotency key not stored by external system → can't disambiguate on resume | Key **stored by the external system**; protocol intent-log→act→confirm-log; resume **re-queries by key**, fail-to-human if unknown. [arch §5](./architecture.md) |
| DS3 | MAJOR | Checkpoint / ledger / WORM could diverge on crash | **Transactional outbox**: checkpoint+ledgers+outbox in one txn; relay drains to externals + WORM. [arch §5](./architecture.md) |
| DS4 | BLOCKER | `tree_sha` undefined → collisions (stale PASS) or spurious diffs (cascade) | Precise definition: normalized tracked source + lockfile hash + toolchain digest; build artifacts/timestamps excluded; cross-machine invariant test. [arch §4](./architecture.md) |
| DS5 | MAJOR | Cross-gate fix cascade never converges; loop-detector keyed on ever-changing diff hash | **Global `fix_attempts_since_all_green` bound** (escalates as *non-convergence*) + **semantic loop signature**. [arch §7](./architecture.md) |
| DS6 | MAJOR | "Pause in-flight branches mid-superstep" is impossible in LangGraph | Budget enforced at **superstep boundaries**; cooperative-cancellation token; `Send` per-item commit. [arch §6](./architecture.md) |
| DS7 | MAJOR | Assurance join undefined under partial failure / hang | `GateResult.status="error"`; `Send` independent branches; per-branch deadline; bounded retry → escalate. [arch §5](./architecture.md) |
| DS8 | MINOR | `operator.add` reducers not commutative under fan-in | All channels keyed set-unions; order read from `seq`. [arch §3](./architecture.md) |
| DS9 | MINOR | Checkpoint write amplification on long runs | `history`/large artifacts **out-of-band by handle**; bounded checkpoint. [arch §3](./architecture.md) |
| DS10 | MINOR | Loop signature defeated by cosmetic diff changes | Signature keyed on `(criterion_id, gate, failure_signature)`. [gov §3](./governance.md) |

### Offensive-security / AI-safety (10): the agent-authored trust surface

| # | Sev | Problem | Resolution |
|---|---|---|---|
| SEC1 | BLOCKER | Trusted builder executes **agent-authored** build/CI/IaC config → signs a backdoor | Build config = high-privilege artifact behind a **Supply-chain gate**; strongly-hermetic **no-egress** builder; no build hooks; SAST over build/IaC. [gov §4](./governance.md) |
| SEC2 | BLOCKER | Agents can pass the Security gate by editing scanner config/suppressions | Scanner config **run-immutable, repo-external**; any suppression/floor edit = first-class finding + deterministic monitor. [gov §2](./governance.md) |
| SEC3 | MAJOR | Analyzer injectable via untrusted trace content | Load-bearing detections **deterministic**; analyzer toolless, event-triggered, fed **structured features** (prose as data); injection in its eval set. [gov §3](./governance.md) |
| SEC4 | MAJOR | Exfil over **allowlisted** egress; install/build not mocked | Credentialed phase separated from untrusted-code exec; internal hash-pinned dep mirror; anomaly-detecting egress proxies. [gov §2](./governance.md) |
| SEC5 | MAJOR | Agent-authored IaC escalates the deployed system's privileges | Policy-as-code on IaC `plan`; OIDC/trust changes → HITL; **non-agent deployer**. [gov §2/§5](./governance.md) |
| SEC6 | MAJOR | HITL is a rubber stamp over agent-curated framing | Independent non-agent **evidence pack**; two-person sign-off on trust-root. [gov §6](./governance.md) |
| SEC7 | MAJOR | Signature ≠ "passed gates"; no deploy-time admission | **Gate-state bound into the SLSA predicate** + **deploy-time admission controller**. [gov §4/§5](./governance.md) |
| SEC8 | MAJOR | Criterion-coverage gameable via weak/omitted criteria | **Independent criteria-completeness critic** (security/abuse required; omission fails Spec). [gov §1b](./governance.md) |
| SEC9 | MINOR | WORM written by agents → tamper risk | **Non-agent appender**; external chain anchoring; no agent can truncate/rotate. [gov §8](./governance.md) |
| SEC10 | MINOR | PII/PHI egress to model/trace providers unspecified | Redaction **before provider egress** (fail-closed); zero-retention terms; PHI/PCI never leaves boundary. [gov §2](./governance.md) |

### AI-agents / LLM-systems (10): the core bet and eval validity

| # | Sev | Problem | Resolution |
|---|---|---|---|
| AI1 | BLOCKER | The decisive A/B was under-specified and rigged toward Foundry | **Pre-registration**: 3 arms (incl. strong agentic baseline + gates-only arm), USD equalizer, `n`/`k`/power/test frozen & hashed. [eval §2](./evaluation.md) |
| AI2 | BLOCKER | Context-assembly silently drops plan-critical state | Never-summarized **`pinned_constraints`**; call/def-graph retrieval with a **recall floor**; **`need-context` escape**. [arch §8](./architecture.md) |
| AI3 | MAJOR | Judges correlate with implementer; self-preference bias | Measured error-correlation + self-preference evals; **different model *family*** on high-risk review/QA. [eval §4](./evaluation.md) |
| AI4 | MAJOR | "Monitor overhead ≤5%" + cost/latency unvalidated | **Per-feature cost+wall-clock model**; **event-triggered** analyzer with input cap. [eval §8](./evaluation.md) |
| AI5 | MAJOR | LLM nondeterminism contradicts the audit/replay claim | Temp-0 for gates/judges; record seeds/model-fingerprint; **pinned-ID-drift alert**; audit = record, not bitwise replay. [arch §8](./architecture.md) |
| AI6 | MAJOR | Telephone-game intent loss; coverage ≠ fidelity | **Criterion fidelity** (negative oracle per high-risk criterion) + **handoff-fidelity eval**. [eval §4](./evaluation.md) |
| AI7 | MAJOR | Tiering by intuition (impl < review is questionable) | Tiering = **validated optimization output**; high-risk impl + oracle-authoring default **T1**; escaped-defects tracked **by producing tier**. [agents](./agents.md), [eval §9](./evaluation.md) |
| AI8 | MAJOR | "Hidden" tests reachable at runtime; benchmark contamination | Hidden tests in an **unreachable scoring harness**; **post-cutoff in-house novel suite** + contamination probes. [eval §6](./evaluation.md) |
| AI9 | MINOR | Same-tier failover illusory within one family | Stated honestly: intra-family = retry/pause-escalate; cross-family carries per-model calibration. [arch §8](./architecture.md) |
| AI10 | MINOR | Analyzer-as-injectable-judge (≈SEC3) | Oracle/gate/criteria edits are **deterministic** trips; analyzer advisory only. [gov §3](./governance.md) |

**Round 7 verdict (solutioner):** all 30 addressed via full rewrites of
architecture.md, governance.md, evaluation.md and targeted updates to agents.md /
PLAN.md. Re-submitted to a fresh specialist panel for Round 8.

## Round 8 — specialist panel re-review (13 findings: 1 BLOCKER, 7 MAJOR, 5 MINOR)

The three specialists re-reviewed the Round-7 rewrites, hunting for defects the
fixes *introduced*. Convergence is visible (30 → 13; blockers 7 → 1). Several
findings are bugs the Round-7 fixes themselves created — the value of re-review.

| # | Sev | Problem | Resolution |
|---|---|---|---|
| DS-F1 | BLOCKER | `seq` *assignment* was racy/unspecified (parallel branches share one snapshot; a single-writer `seq_clock` can't be read-incremented concurrently) → equal `seq` falls back to iteration order, reintroducing DS1 | `seq` is now a **total lexicographic tuple** `(superstep_index, node_id, local)`; reducer compares tuples, ties impossible, invariant test. [arch §3](./architecture.md) |
| DS-F2 | MAJOR | Retry/rebase re-emit could carry equal `seq` → lost update (stale `error`/PASS survives) | Total order from DS-F1: a later-superstep re-emit always outranks the prior write to the same key. [arch §3/§4](./architecture.md) |
| DS-F3 | MINOR | Idempotency protocol assumed external client-key/requery support universally | Added a **Foundry-side dedup record** (written in the outbox txn) for systems supporting neither. [arch §5](./architecture.md) |
| SEC-F1 | MAJOR | Deterministic-gate **verdicts** were agent-*claimed* → predicate/admission chain circular against a forged PASS | Verdicts **computed & signed by a trusted gate runner** (agents triage only); WORM/builder accept only attested verdicts; admission verifies runner identity. [gov §1c/§4/§5](./governance.md) |
| SEC-F2 | MAJOR | Dependency-mirror **vetting authority** unspecified → backdoor via new dependency + dependency-confusion | **Non-agent/HITL mirror-admission** (provenance, namespace reservation, source-build diff); new-dep = HITL change class. [gov §2](./governance.md) |
| SEC-F3 | MINOR | External scanner-config repo's own change-authority unstated | Separate trust boundary, no agent write/PR path, two-person sign-off, **revision hash in the predicate**. [gov §2/§4](./governance.md) |
| AI-F1 | MAJOR | Claude-only stack contradicts the "different *family*" judge requirement (tiers ≠ families) | Stack provisions a **second provider family** for high-risk judging + cross-family failover; all docs reconciled. [arch §8](./architecture.md), [PLAN §6](../PLAN.md) |
| AI-F2 | MAJOR | `pinned_constraints` (never-summarized, pure union) grows unboundedly → overflows the long-horizon runs it protects | **Relevance-scoped + supersedable + cardinality-monitored** (escalate, never silently truncate); per-node injection budget. [arch §8](./architecture.md) |
| AI-F3 | MAJOR | Event-triggered analyzer can't see under-threshold scope-creep that touches no protected path (yet is eval'd on scope-creep) | Added **sampled passes** + a deterministic **criterion-coverage-delta** trigger; eval now includes an **untriggered held-out set**. [gov §3](./governance.md), [eval §4](./evaluation.md) |
| AI-F4 | MAJOR | A/B feasibility shown per-feature but never **costed at experiment scale** | Added a full **A/B-scale cost roll-up** (3 arms × n × k × SWE-EVO cost) + honest de-scope rule. [eval §8](./evaluation.md) |
| AI-F5 | MINOR | "Single known-bad ref proves intent" — overclaim + strawman risk | Criterion fidelity = a **family of non-trivial mutants** (cross-family authored, must pass happy-path); reframed **necessary, not sufficient**. [eval §4](./evaluation.md) |
| AI-F6 | MINOR | Retrieval-recall ground truth uneven / floor unstated | Split into **mechanical** (static coupling edges) + **human-labeled** (task-relevance, IAA), each with a floor. [eval §4](./evaluation.md) |
| AI-F7 | MINOR | Baseline parity is a maintenance obligation; the ≥85% threshold was unanchored | Parity is a **CI-checked, manifest-hashed invariant**; thresholds **anchored to measured baseline + margin**. [eval §2/§7](./evaluation.md) |

**Round 8 verdict (solutioner):** all 13 addressed. Re-submitted to a fresh
specialist panel for Round 9.

## Round 9 — specialist panel re-review (9 findings: 0 BLOCKER, 3 MAJOR, 6 MINOR)

Blockers eliminated; the panel acknowledged most Round-8 mechanisms as sound. The
three MAJORs all stem from the trust/ordering machinery the prior rounds added —
the value of repeated specialist review.

| # | Sev | Problem | Resolution |
|---|---|---|---|
| DS-F1 | MAJOR | `seq` total within a run but **not across forks**; WORM/admission key on `tree_sha` (not run id), so two forks with the same digest + opposite verdicts collide with equal `seq` | Added **`run_epoch`** to the `seq` tuple (total across forks) **and** made admission **fail-closed**: any digest FAIL disqualifies regardless of a later PASS. [arch §3](./architecture.md), [gov §5](./governance.md) |
| DS-F2 | MAJOR | Max-by-`seq` reducer trusted ordinal position, not attestation → a triaging agent (later superstep) could supersede the runner's signed verdict in the *live* channel that in-graph routing + the evidence pack read | Agents write `GateTriage`, **never** `GateResult`, for deterministic gates; reducer **rejects unsigned deterministic-gate writes**. [arch §3](./architecture.md), [gov §1c](./governance.md) |
| DS-F3 | MINOR | `superstep_index` exposure/semantics stated loosely | Specified: read from run-config metadata at emit time; constant across a `Send` fan-out; same-node-id siblings separated by the channel key. [arch §3](./architecture.md) |
| DS-F4 | MINOR | Path-3 dedup's "effectively-once" overstated for crash-in-window | Path-3 **reserved for tolerable (notification) effects**, never gate/release/deploy-load-bearing; crash-window → fail-to-human. [arch §5](./architecture.md) |
| SEC-F1 | MAJOR | The gate runner executes **agent-authored** test/build code in the same process holding its **signing identity** → RCE-to-signature | **Two-zone runner**: zone A (untrusted executor, no creds) emits a raw results stream; zone B (no agent code) signs the verdict from that stream against pinned config; added to red-team. [gov §1c](./governance.md) |
| SEC-F2 | MINOR | Human couldn't realistically adjudicate a new-dependency mirror admission | Source-build-and-diff **mandatory** absent a trusted upstream attestation; concrete evidence-pack surface for new-dep change class. [gov §2](./governance.md) |
| SEC-F3 | MINOR | `runner_attestation` bound only runner identity, not the verdict's inputs → replay/substitution seam | Attestation now signs `(gate, tree_sha, config_rev, status, results_digest)`; admission re-checks all per record. [arch §3](./architecture.md), [gov §1c/§5](./governance.md) |
| AI-F1 | MINOR | Relevance-scoping `pinned_constraints` is itself a retrieval problem → a relevant (esp. cross-cutting) constraint could be silently dropped | **Global/non-scopable subclass** (cross-cutting invariants, security/abuse, trust-root) always injected; local-constraint scoper gets its **own measured recall floor**. [arch §8](./architecture.md), [eval §4](./evaluation.md) |
| AI-F2 | MINOR | Criterion-coverage-delta trigger's measurand undefined and reducible to an agent-controlled tag count | Anchored to the **Spec-gate-frozen criterion set** + **runner-attested negative-oracle count** (not test tags); baseline + threshold specified. [gov §3](./governance.md) |

**Round 9 verdict (solutioner):** all 9 addressed. Re-submitted to a fresh
specialist panel for Round 10.

## Round 10 — specialist panel re-review (8 findings: 1 BLOCKER, 2 MAJOR, 5 MINOR)

The AI-agents specialist returned **NO MATERIAL**; the security and DS specialists
probed deeper into the just-added two-zone runner and fail-closed model and found
that closing one trust seam exposed the next one in.

| # | Sev | Problem | Resolution |
|---|---|---|---|
| SEC-F1 | BLOCKER | Two-zone runner closed RCE-to-signature but **not results-stream forgery** — a malicious test/scanner in zone A self-reports "all passed" and zone B faithfully signs it (trust relocated, not grounded) | **Gate-class split:** config-evaluable gates (SBOM/CVE/license/IaC) are **re-derived by zone B**; execution-dependent gates (Test/E2E/SAST) are **observed by pinned trusted infra** over a sealed channel (agents supply test *sources*, not the harness), with zone B independently verifying pass-on-real/fail-on-known-bad. [gov §1c](./governance.md) |
| DS-F1 | MAJOR | Unconditional fail-closed **permanently bricks a correct digest** when a flake exceeds the N=2 quarantine (a flaky FAIL then a real PASS on the same `tree_sha`) | Fail-closed latches only **stable, input-relevant FAILs**; **transient-execution divergence** gets a bounded recovery (quorum of trusted re-runs all PASS + two-person WORM-logged override); `error` never latches. [gov §5](./governance.md) |
| DS-F2 | MAJOR | Security gate's "threat-model items addressed" isn't a deterministic function, yet was classified as a fully runner-computed gate — agent judgment leaks into the signed verdict | **§1d:** each threat-model item is **bound to a concrete runner-evaluable oracle**; irreducible residual → a **Threat-review judgment sub-gate** (critic+HITL), **not** in the signed verdict. [gov §1c/§1d](./governance.md) |
| DS-F3 | MINOR | `run_epoch = parent_epoch+1` collides among concurrent sibling forks; "total across forks" was unachievable *and* not load-bearing | `seq` uses a **globally-unique `run_id`** for cross-run identity; "total across forks" claim dropped (cross-fork resolution is **fail-closed**, not seq-ordered). [arch §3](./architecture.md) |
| SEC-F2 | MINOR | Mandatory mirror source-diff doesn't bound **volume** → rubber-stamp at scale | Reachability-weighted prioritization, diff-against-prior-vetted, per-feature new-dep budget, reject obfuscated artifacts lacking buildable source. [gov §2](./governance.md) |
| AI-F1 | MINOR | Global-vs-local constraint classification is an **unvalidated Architect judgment** → a mislabeled cross-cutting invariant is droppable | Deterministic pattern check (money/PII/logging/auth/crypto) forces `global` or trips HITL; local-scoper recall floor backstops. [arch §8](./architecture.md) |
| AI-F2 | MINOR | Cardinality monitor wording inconsistent (governance "relevant" vs architecture "injected"); global subset could overflow unwatched | Reconciled to **total injected set**; global-only overflow escalates to retire/consolidate globals. [gov §3](./governance.md) |
| AI-F3 | MINOR | High-risk flag freezing implied but not stated → denominator re-labeling risk | Stated: high-risk flag is an **immutable attribute of the frozen criterion**. [gov §3](./governance.md) |

**Round 10 verdict (solutioner):** all 8 addressed. Re-submitted to a fresh
specialist panel for Round 11.

## Loop summary

| Round | Bar | Findings | BLOCKER | MAJOR | MINOR | Verdict |
|---|---|---|---|---|---|---|
| 1 | design-doc | 16 | 2 | 9 | 5 | material remain |
| 2 | design-doc | 10 | 1 | 5 | 4 | material remain |
| 3 | design-doc | 5 | 0 | 2 | 3 | material remain |
| 4 | design-doc | 3 | 0 | 3 | 0 | material remain |
| 5 | design-doc | 2 | 0 | 1 | 1 | material remain |
| 6 | design-doc | 1 | 0 | 0 | 1 | **no material** |
| 7 | **implementation-ready (3 specialists)** | 30 | 7 | 16 | 7 | material remain |
| 8 | implementation-ready (3 specialists) | 13 | 1 | 7 | 5 | material remain |
| 9 | implementation-ready (3 specialists) | 9 | 0 | 3 | 6 | material remain |
| 10 | implementation-ready (3 specialists) | 8 | 1 | 2 | 5 | material remain |
| 11 | implementation-ready (3 specialists) | _running_ | | | | _pending_ |

Total so far: **97 findings raised and resolved** across two bars. The deeper the
specialists probe the trust root, the more each fix exposes the next seam in
(RCE→signature → results-stream forgery → …) — characteristic of hardening a hard
problem. One specialist returned NO MATERIAL this round. This log is the honest
record the process produced, not a marketing artifact.

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
