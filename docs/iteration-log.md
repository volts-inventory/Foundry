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

## Round 11 — specialist panel re-review (3 findings: 0 BLOCKER, 1 MAJOR, 2 MINOR)

**Two of three lenses clean** (AI-agents NO MATERIAL; DS only MINOR). The security
lens found the next seam in the execution-gate grounding.

| # | Sev | Problem | Resolution |
|---|---|---|---|
| SEC-F1 | MAJOR | The execution-gate discriminator is still agent-influenceable: an agent-authored test can **fingerprint the substituted mutant by *identity*** (file hash, presence of the fix) rather than behavior, passing zone B's digest-flip while testing nothing | Zone B draws mutants from a **rotating held-out set unknown to the test-authoring family** **and** enforces a **behavioral-invariance check** (PASS invariant under behavior-preserving perturbation; FAIL across a mutant family) — a fingerprinting test is rejected. [gov §1c](./governance.md), [eval §4](./evaluation.md) |
| DS-F1 | MINOR | The admission "all gates PASS in WORM" policy was silent on a coexisting stable FAIL + later flaky PASS for one digest | Latch made **monotonic + WORM-history-scoped**: a stable FAIL disqualifies until override; a later PASS never auto-clears (live-channel vs admission disagreement is intentional). [gov §5](./governance.md) |
| DS-F2 | MINOR | Override scope unspecified → a digest-level override could clear a co-resident stable FAIL | Overrides are **record-scoped** to a specific transient `(gate, tree_sha, results_digest)`; admission re-evaluates the full set; co-resident stable FAILs stay latched. [gov §5](./governance.md) |

**Round 11 verdict (solutioner):** all 3 addressed. Re-submitted to a fresh
specialist panel for Round 12.

## Round 12 — specialist panel: **ALL THREE LENSES `NO MATERIAL FINDINGS`** (loop converged)

The fresh panel independently verified the package at the implementation-readiness
bar and each lens declared convergence:

- **Security:** verified the held-out-mutant + behavioral-invariance fix grounds
  the execution-dependent gate verdict end-to-end (observation *and* discriminator
  both outside agent control; verdict input-bound and admission-rechecked). The
  one residual — a behaviorally-weak-but-non-vacuous test surviving conservative
  perturbations — is the *already-declared* "necessary, not sufficient" limit of
  mutation-based fidelity, not a new exploit. **NO MATERIAL FINDINGS.**
- **Distributed-systems:** verified the monotonic WORM-history-scoped latch and
  record-scoped override are correct and consistent with the reducer/seq/outbox
  core; confirmed FAIL records cannot be agent-suppressed (status is inside the
  signed attestation; agents write only `GateTriage`); confirmed a latched digest
  never wedges the pipeline (a fix yields a new `tree_sha`). **NO MATERIAL FINDINGS.**
- **AI-agents:** third consecutive clean pass; adversarially probed held-out-mutant
  depletion, coverage-delta denominator gaming, the threat-residual escape hatch,
  and the A/B equal-USD asymmetry (found to be conservative *against* Foundry, not
  a rig). Every seam resolved to a specified mechanism with a non-circular backstop.
  **NO MATERIAL FINDINGS.**

**The implementation-readiness loop converged here:** the three technical lenses
(security, distributed-systems, AI-agents) found no defect. But convergence at one
bar is not fitness-for-purpose — so Round 13 deliberately changed the lenses.

## Round 13 — FRESH LENSES (fitness-for-purpose bar): 17 findings (0 BLOCKER, 14 MAJOR, 3 MINOR)

The technical panel had converged, so Round 13 brought in three perspectives it
structurally could not provide — **SRE/operability**, **cost/scale (FinOps)**, and
a **premise-skeptic** — to test the *shape and viability* of the design, not its
internal correctness. They immediately surfaced 17 material findings, validating
that "internally sound" ≠ "the right, runnable, adoptable thing." *(This itself
resolves P-F6: the Round-12 convergence claim is now explicitly bounded to internal
soundness.)*

### Premise-skeptic (6: shape, sequencing, lifecycle)
| # | Sev | Problem | Resolution |
|---|---|---|---|
| P-F1 | MAJOR | The falsifying A/B was sequenced *last* — full build cost sunk before the bet is decided | **Phase-1.5 decision gate**: powered A/B with a real kill/redesign branch before the expensive machinery. [roadmap](./roadmap.md) |
| P-F2 | MAJOR | 16-role pantheon not evidence-grounded (cited work validates ~5) | Role-count is an **early ablation**; start with the 4-role core, earn each role. [roadmap](./roadmap.md), [eval §9](./evaluation.md) |
| P-F3 | MAJOR | No effort/team/timeline or maintainability realism | Per-phase team-quarter estimates + a stated **smallest-useful milestone**. [roadmap](./roadmap.md) |
| P-F4 | MAJOR | Trust-root machinery front-loaded for an autonomy not yet earned | **Deployment-trust modes** (Assistant→Gated→Autonomous); trust root **deferred** to Phase 4. [lifecycle §4](./lifecycle.md), [roadmap](./roadmap.md) |
| P-F5 | MAJOR | Missing lifecycle: maintenance, human steering/taste, feedback loop, AI-code licensing/liability | New **[lifecycle.md](./lifecycle.md)** + generated-code provenance in the assurance plane. |
| P-F6 | MINOR | "Converged" risked being read as fitness-for-purpose | Claim explicitly bounded to internal soundness ([PLAN §13](../PLAN.md)). |

### Cost/scale (3: viability)
| # | Sev | Problem | Resolution |
|---|---|---|---|
| C-F1 | MAJOR | Unconditional **total** re-verification → a cost *multiplier* | **Dependency-scoped re-verification** + verdict caching on unchanged sub-hashes; cost reported converged *and* worst-case. [gov §1f](./governance.md) |
| C-F2 | MAJOR | No pipeline risk-tiering; **unbounded HITL volume** | **Change-risk tiering** (Low/Standard/High) scaling assurance + HITL; **human-decisions-per-feature** scorecard metric. [gov §1e](./governance.md), [eval §7](./evaluation.md) |
| C-F3 | MINOR | A/B de-scope authority unstated; trusted-runner/builder serialization; checkpoint txn budget | De-scope **ladder with authority note**; replicas + queue model; superstep/txn budget. [eval §8](./evaluation.md), [operations §4/§6](./operations.md) |

### SRE/operability (8: Day-2 — entirely new dimension)
| # | Sev | Problem | Resolution |
|---|---|---|---|
| S-F1 | MAJOR | No operator role, runbooks, SLOs, alerting, dashboards | [operations §1](./operations.md) |
| S-F2 | MAJOR | Can't distinguish *paused* from *wedged* | First-class **run-status model + watchdog**. [operations §2](./operations.md) |
| S-F3 | MAJOR | No store HA/backup/restore or poisoned-checkpoint recovery | RPO/RTO + block-and-pause + fork-to-good + integrity sweep. [operations §3](./operations.md) |
| S-F4 | MAJOR | Trusted-component *outage* blast radius unanalyzed (mandatory chokepoints) | Degrade-to-paused + HA replicas + queue-and-drain. [operations §4](./operations.md) |
| S-F5 | MAJOR | No upgrade/migration with runs in flight (schema/graph/prompt/model drift) | Version-stamped checkpoints + migration/drain policy + drift response. [operations §5](./operations.md) |
| S-F6 | MAJOR | No multi-tenancy/capacity/queueing/fairness | Run-admission layer + fleet cost-rate cap + noisy-neighbor guard. [operations §6](./operations.md) |
| S-F7 | MAJOR | Cumulative HITL load never tallied/bounded | HITL-load model + reviewer queue/SLA + release-compute-release + staffing fallback. [operations §7](./operations.md) |
| S-F8 | MINOR | Trusted-component lifecycle/rotation + bootstrap-trust unspecified | Rotation cadence/procedure + named ownership + bootstrap posture (humans build the trust root). [operations §8](./operations.md) |

**Round 13 verdict (solutioner):** all 17 addressed via two new docs
(operations.md, lifecycle.md), a reworked roadmap, change-risk tiering +
dependency-scoped re-verification in governance, and threaded updates to
PLAN/evaluation/architecture/README.

## Round 14 — fresh-lens verification: **ALL THREE LENSES `NO MATERIAL FINDINGS`** (fitness-for-purpose converged)

The same three fresh lenses re-reviewed the Round-13 reshaping and each confirmed
it sound, with only editorial-consistency nits (now fixed):

- **SRE/operability:** verified all eight operability areas are specified at the
  right altitude and consistent with the existing models — confirmed `RunStatus`
  doesn't collide with `phase`/`GateResult.status`, and **degrade-to-paused is
  orthogonal to fail-closed admission** ("an outage cannot manufacture a green
  verdict"). **NO MATERIAL FINDINGS.**
- **Cost/scale:** verified risk-tiering preserves the security guarantees
  (admission still fail-closed; Low-tier is safe-by-construction since any
  security-relevant path forces High), cached-verdict reuse is sound, and the
  cost model is honestly bounded. **NO MATERIAL FINDINGS** (1 MINOR doc-consistency
  nit — a stale "re-runs all integrated gates" lead-in — fixed).
- **Premise-skeptic:** verified the Phase-1.5 kill branch is decision-grade, role
  growth is genuinely ablation-gated, deployment-trust modes make the trust root
  truly deferrable, the lifecycle story is mechanisms not gestures, and the
  convergence claim is honestly bounded. **NO MATERIAL FINDINGS.**

**Editorial nits fixed in Round 14:** `seq` tuple naming reconciled to `run_id` in
PLAN.md; the governance §1 re-verification lead-in pointed to §1f; evaluation.md
Phase-5 references updated to the reordered Phase-1.5 (decision A/B) / Phase-6
(optimization).

**Both bars have now converged:** the technical panel at Round 12, the
fitness-for-purpose panel at Round 14.

## Round 15 — DOMAIN-EXPERT lenses (type-of-software bar): 18 findings (0 BLOCKER, 15 MAJOR, 3 MINOR)

A user prompt — *"type of software can be key"* — motivated a third fresh axis:
three domain experts (**regulated/safety-critical**, **ML/data**, **frontend/UX**)
asked whether *specific kinds of software* break core assumptions the pluggable
"domain pack" mechanism can't reach. They **independently converged on one deep
finding**: Foundry's core defines correctness as a *deterministic behavioral
oracle*, but the **primary correctness signal of three major domains isn't that
shape** — and a pack can't fix it because it can't change what a *verdict* means.

**Unifying resolution (core, not pack):** a **verdict-class taxonomy**
([domains.md §1](./domains.md)) generalizing the existing "deterministic oracle +
judgment residual" into five classes (config-evaluable, execution-deterministic,
**statistical**, **human-witnessed**, **perceptual**), with the behavioral-
invariance discriminator made **gate-class-aware**; a **`state_sha`** that
includes data/weight refs; a **compute-cost budget** dimension; and an **expanded
pack contract** with real core hooks. Per-finding:

| Lens | # | Sev | Problem | Resolution (core unless noted) |
|---|---|---|---|---|
| Regulated | F1 | MAJOR | Autonomous/auto-approve is often *unlawful*; risk ≠ diff size | Pack **mode/tier veto** (force human approver, make Autonomous unreachable); **regulatory-significance** tiering axis. [gov §1e](./governance.md), [domains §4](./domains.md) |
| Regulated | F2 | MAJOR | Records must be attributable to a *named qualified human*, not an agent | **Per-artifact approver-of-record** (class 4) bound into WORM/predicate. [domains §1/§5](./domains.md) |
| Regulated | F3 | MAJOR | Regulated verification is traceability + human-witnessed protocols, not an oracle | **Human-witnessed verdict class**; requirement→design→risk→evidence **traceability matrix** as a predicate artifact. [domains §1/§5](./domains.md) |
| Regulated | F4 | MAJOR | Redaction isn't a PHI/PAN control; second model family may be unavailable | **No-third-party-egress data profile** + judge-decorrelation fallback; **no-real-PHI/PAN** (synthetic data). [gov §2](./governance.md) |
| Regulated | F5/F6 | MAJOR/MINOR | Reg-significance tiering; identity-bound WORM + retention | Tiering axis; **authenticated approver identity** + **retention floor** in WORM. [domains §5](./domains.md) |
| ML/data | F1 | MAJOR | Correctness is a thresholded random variable, not a digest-flip | **Statistical verdict class** (metric + CI + pinned eval set/seed); latch on the *decision*. [domains §1](./domains.md) |
| ML/data | F2 | MAJOR | `tree_sha` excludes data/weights → unsound cache reuse + attestation | **`state_sha`** (source ⧺ lockfile ⧺ toolchain ⧺ data/feature/weight refs). [arch §4](./architecture.md), [domains §2](./domains.md) |
| ML/data | F3 | MAJOR | Budget/re-verify assume cheap gates; a retrain costs >1 feature | **Compute-cost budget** + expensive-gate proxy + retrain-confirm escalation. [arch §3](./architecture.md), [domains §3](./domains.md) |
| ML/data | F4/F5 | MAJOR/MINOR | "Factory eval" vs "product eval" conflated; model-vs-code rollback | Product eval/monitoring a **shipped artifact**; drift/decay triggers; **state-based rollback**. [eval](./evaluation.md), [lifecycle §3](./lifecycle.md) |
| Frontend | F1 | MAJOR | Look/feel is behavior-preserving-*variant* — the discriminator rejects visual tests | **Perceptual verdict class** (excluded from signed predicate, still gated); discriminator gate-class-aware. [domains §1/§5](./domains.md) |
| Frontend | F2 | MAJOR | "Taste not delegated to a model" was unimplemented — no human sees a rendered screen | **Mandatory non-auto-approvable visual HITL** + **rendered-artifact evidence bundle** for `ui`. [domains §5](./domains.md) |
| Frontend | F3 | MAJOR | No real user pre-release; LLM Proxy isn't a substitute | Stated honestly: **pre-release UX is bounded**; usability-test HITL artifact. [lifecycle §3](./lifecycle.md) |
| Frontend | F4 | MAJOR | Visual flakiness ≠ seed/clock; binary verdict has no tolerance band | **Tolerance-band / device-matrix statistical gate** + perceptual flake carve-out. [domains §5](./domains.md) |
| Frontend | F5/F6 | MINOR | A11y/CWV/design-system/i18n unmodeled (but fit cleanly) | Pack: deterministic a11y + judgment a11y sub-gate; CWV/bundle/token/i18n runner gates; Figma input + handoff-fidelity. [domains §5](./domains.md) |

**Round 15 verdict (solutioner):** all 18 addressed via a new **[domains.md](./domains.md)**
(verdict-class taxonomy + pack contract + three domain specs) and threaded core
changes to governance/architecture/PLAN/evaluation/lifecycle/roadmap. The biggest
lesson yet: the design's *thesis* ("a deterministic decision rule at the seams")
was **domain-specific** — true for backend/logic, false as a universal — and only a
*domain-expert* lens could see it. **"Type of software is key"** was exactly right.

## Round 16 — FULL-DOMAIN expert panel (8 fresh lenses, vs the codebase): 62 findings (10 BLOCKER, 33 MAJOR, 19 MINOR)

A user prompt — *"can this leverage `claude -p`"* — added a **pluggable
coding-agent worker-substrate** (PLAN §6, [arch §2](./architecture.md)): the
Implementation/QA inner loop became an adapter (Claude Code `claude -p`, Codex CLI
`codex exec`, OpenHands, or a raw loop) behind a defined contract. The user then
asked for a fresh **critic → fix → validate loop, run against the codebase** across
**all domains it touches** — eight expert lenses in parallel (distributed systems,
LLM/multi-agent orchestration, evaluation methodology, offensive-security/supply-
chain, SRE/operability/cost, regulated-compliance, ML/data, frontend/UX). The new
substrate drew the heaviest fire, and the deeper domain lenses re-opened the
verdict-class and assurance machinery the prior rounds had signed off on.

| Lens | # | Headline findings (sev) | Resolution locus |
|---|---|---|---|
| Distributed systems | 8 (1B,4M,3m) | Adapter inner loop runs inside one superstep → superstep-boundary budget/cancellation/checkpoint don't bind it **(B)**; `seq`/`local` replay-determinism; reuse staleness across mutant rotation; cross-fork latch invisible to routing | [arch §2/§3/§4/§5/§6](./architecture.md) |
| Security / supply-chain | 6 (2B,3M,1m) | Agent-authored test reads the tree at runtime to defeat the held-out-mutant discriminator **(B)**; "worktree-only/no-verdict" unenforced for a bash-running adapter **(B)**; provider egress as a high-entropy exfil channel; analyzer feature-flooding | [gov §1c/§2/§3](./governance.md) |
| Regulated / compliance | 9 (2B,5M,2m) | Class-4 human attestation forgeable by the pipeline **(B)**; no model-risk governance (SR 11-7/IEC 62304) **(B)**; audit content agent-authored; traceability cells unbound to runner verdicts; SoD unenforced; Assistant mode bypasses all controls | [gov §1c/§1e/§4/§8](./governance.md), [domains §1/§5](./domains.md), [lifecycle §4](./lifecycle.md) |
| ML / data | 9 (2B,5M,2m) | No multiple-comparisons/adaptive-overfitting control **(B)**; leakage *detection* mechanism absent **(B)**; train- vs eval-variance; proxy-gate soundness; eval staleness; no criterion-fidelity analog; label-noise | [domains §1/§5](./domains.md), [gov §1c/§5](./governance.md) |
| Evaluation methodology | 7 (2B,4M,1m) | Post-hoc power-rung selection defeats falsifiability **(B)**; no single primary endpoint / multiple-comparisons control **(B)**; equal-USD handicap; adaptive suite authoring; judge self-preference in the A/B; collider bias in conditional hidden-test rate | [evaluation §2/§3/§4/§6/§7/§8](./evaluation.md), [roadmap Phase 1.5](./roadmap.md) |
| SRE / operability / cost | 10 (0B,5M,5m) | Cost model token-only — omits mutant×perturbation re-execution compute, adapter inner-loop turns, second-family capacity, paused-run storage; two-person reviewer-pool queueing; fleet-wide drift HITL spike; fleet cost-cap enforcement | [evaluation §8](./evaluation.md), [operations §2/§4/§5/§6/§7](./operations.md), [arch §8](./architecture.md) |
| LLM / multi-agent | 6 (0B,3M,3m) | **Adapter ≠ model family** — decorrelation binds the *served lineage*, not the CLI brand; "never a verdict" overstated (judgment seams emit verdicts); held-out keying + failover must be decorrelation-aware | [arch §2/§8](./architecture.md), [PLAN §6](../PLAN.md) |
| Frontend / UX | 7 (1B,4M,2m) | Visual-regression **baseline lifecycle** undefined → false-positive treadmill or loose tolerance **(B)**; perceptual-HITL load unbounded; a11y AT-user dependency unframed; no generative taste loop; product wedge unargued | [domains §5](./domains.md), [lifecycle §1/§2](./lifecycle.md), [PLAN §9/§10](../PLAN.md), [roadmap](./roadmap.md) |

Cross-panel consensus (raised independently by ≥2 lenses, highest confidence):
**adapter ≠ served model family** (decorrelation must bind the served lineage, not
the adapter brand; failover must preserve it); **"never emits a verdict" was
overstated** (split signed-deterministic-gate verdicts from LLM-judgment verdicts);
**the adapter's worktree-only/no-side-effect property must be sandbox-*enforced*,
not contractual** (it runs arbitrary bash); and **the cost model was token-only**,
omitting the trusted-runner re-execution compute and adapter inner-loop turns that
actually dominate.

**Round 16 verdict (solutioner):** all 62 resolved via file-partitioned expert
fixers across PLAN/architecture/governance/evaluation/operations/domains/lifecycle/
roadmap, using a shared canonical-resolution set so independent edits stayed
consistent. Re-submitted to a fresh full-domain panel for **Round 17**.

## Round 17 — FULL-DOMAIN validation panel (8 fresh lenses, same domains): 57 findings (9 BLOCKER, 32 MAJOR, 16 MINOR)

A second fresh panel of the same eight domains validated the Round-16 fixes and
hunted regressions. It did **not** converge — and characteristically, the findings
clustered at the **interaction seams of the Round-16 mechanisms themselves**: the
fixes worked, but each introduced new surface a fresh expert could attack. The
sharpest were self-inflicted by Round 16:

- **`served_family` was self-declared by the adapter** — the decorrelation fix
  relocated trust to a forgeable field instead of grounding it (BLOCKER ×, raised by
  LLM-orchestration *and* security). Resolution: proxy-observed served lineage, a
  curated lineage-equivalence class, invariant spanning all judge↔subject seams.
- **"decorrelation + HITL" was false for per-task Review** (no HITL on that seam) —
  the most frequent judgment verdict was bound by decorrelation alone (BLOCKER).
- **Incremental progress reintroduced the worktree dual-write divergence** §5 had
  closed (BLOCKER) → content-addressed turn-snapshot in the outbox transaction.
- **`superstep_index` is not monotonic across resume** → stale verdict outranks a
  correction → added `emission_epoch` ahead of it in `seq`.
- ML: **train-variance CI vs re-score-at-admission was internally inconsistent**;
  **look-budget bypassable** via reuse + `inconclusive`→add-n alpha leak (BLOCKERs).
- Eval: **power needs a pilot** (borrowed 21% ≠ this baseline); **equal-token
  equalizer asymmetric**; **KILL vs redesign rule self-contradictory** (BLOCKERs).
- Regulated: **PHI scanner unvalidated**; **model-risk lacked ongoing monitoring**;
  **IdP/PKI trust store unpinned** (BLOCKERs).
- Security: the provider-egress proxy **bounds but does not eliminate** the
  high-entropy exfil channel; **response path unguarded**; **value-pinned vacuous
  tests survive** the discriminator (adapter authors both impl and test).

**Round 17 verdict (solutioner):** all 57 resolved in **Round 18** via the same
file-partitioned-fixer method. Re-submitted to a fresh panel for **Round 19**. The
finding count is decreasing only slowly (62 → 57): each fix round adds mechanism
and the next panel finds new seams in it — the same "every new axis re-opens the
problem" dynamic, now operating *within* a fixed axis set as the new substrate
machinery stabilizes.

## Round 19 — FULL-DOMAIN validation panel (8 fresh lenses, same domains): 29 findings (0 BLOCKER, 25 MAJOR, 4 MINOR)

A third fresh panel of the same eight domains validated the Round-18 fixes. The
**convergence signal arrived**: BLOCKERs fell to **zero** and the count nearly
halved again (62 → 57 → 29). Every remaining finding was a *localized refinement*
at the seams of the Round-18 mechanisms, not a fundamental flaw — and several were
genuinely sharp:

- The class-3 alpha look-ledger was **per-run, not cross-fork** — forking before a
  look could re-draw the budget (optional-stopping laundering). → global ledger
  keyed on `(state_sha, decision-id, eval-set)`.
- The judgment-verdict binding **omitted `status`** — a same-tree request-changes→
  approve flip passed the binding check on the no-HITL Review seam. → bind `status`
  + a `verdict_nonce`.
- `served_family` as a scalar `!=` couldn't express **ensembles** (one shared
  component lineage passed). → a lineage *set* with disjointness; pin
  `lineage_map_revision` into the attestation + admission re-check.
- Eval: pilot/scored **distributional transfer** unguaranteed; the decision-grade
  token-secondary had **no pre-registered MDE**; max-T needed **studentization**;
  the token classifier was **turn-granular** (should be sub-turn).
- Regulated: model-risk monitoring **named no trip/escalation**; product-model
  post-deployment revalidation had **no firing component**; PHI-scanner recall floor
  was **weaker than the class it borrows**.
- ML: train-variance estimand (a) was a **best-of-unbounded-N** selection statistic;
  rare-subgroup slice-MDE could be **a-priori infeasible** with no defined verdict.
- Frontend: the auto-skip semantic-affordance classifier had **no recall floor**;
  reference-cell tolerance envelopes silently **demoted most matrix cells** from
  class-5 blessing.

**Round 19 verdict (solutioner):** all 29 resolved in **Round 20** (a smaller,
surgical fix pass). Re-submitted to a fresh panel for **Round 21**. The trajectory
(BLOCKERs 10→9→0; findings 62→57→29) is the design **stabilizing** — the loop is
approaching the convergence the earlier bars reached (R6, R12, R14).

## Loop summary

## Round 21 — FULL-DOMAIN validation panel (8 fresh lenses): 10 findings (2 BLOCKER, 7 MAJOR, 1 MINOR)

The count fell again (62 → 57 → 29 → 10), but two BLOCKERs reappeared — and the
defining observation of this round is that **nearly every finding was a
consistency/reconciliation bug introduced by the parallel-fixer method itself, not
a new design flaw**: the Round-20 global look-ledger was keyed on `state_sha`
(which churns every retrain, defeating its anti-overfitting job — domains §1 vs
governance §5 contradiction); PLAN §9 still named a *regulated* first adopter after
the roadmap was reconciled to *non-regulated* (so PLAN contradicted its own §12);
operations §4 still said "second family" after architecture moved to ≥3 lineages;
the `verdict_nonce` was named but its single-use consumption left unwired (a genuine
`approve` could be replayed over a later `request-changes`). Several were *my own*
canonical-decision errors, not fixer drift.

**Round 22 verdict (solutioner):** all 10 resolved — and crucially with **verbatim
cross-doc definitions** (the look-ledger key, the nonce/latch) handed to the
relevant fixers so the fix could not re-introduce drift; consistency re-verified by
grep across files. Re-submitted to a fresh panel for **Round 23**. The honest read:
the design has **converged in substance** (no new design defects since Round 19;
blockers now arise from edit-consistency), and the residual loop is policing the
multi-agent editing process, not the architecture.

> Rounds 16–22 are the post-`claude -p` "live codebase" loop: even rounds (16
> critique, 18, 20, 22) bundle the solutioner fix; odd rounds (17, 19, 21, 23…) are
> fresh full-domain validation panels. The table lists the findings-bearing panels.

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
| 11 | implementation-ready (3 specialists) | 3 | 0 | 1 | 2 | material remain |
| 12 | implementation-ready (3 specialists) | 0 | 0 | 0 | 0 | **no material — internal soundness converged** |
| 13 | **fitness-for-purpose (3 FRESH lenses)** | 17 | 0 | 14 | 3 | material remain |
| 14 | fitness-for-purpose (3 fresh lenses) | 0* | 0 | 0 | 0 | **no material — converged** |
| 15 | **type-of-software (3 DOMAIN experts)** | 18 | 0 | 15 | 3 | material remain |
| 16 | **live-codebase (8 FULL-DOMAIN lenses)** | 62 | 10 | 33 | 19 | material remain |
| 17 | live-codebase (8 full-domain lenses) | 57 | 9 | 32 | 16 | material remain |
| 19 | live-codebase (8 full-domain lenses) | 29 | 0 | 25 | 4 | material remain — **BLOCKERs→0** |
| 21 | live-codebase (8 full-domain lenses) | 10 | 2 | 7 | 1 | material remain — **mostly edit-consistency bugs** |
| 23 | live-codebase (8 full-domain lenses) | 9 | 1 | 7 | 1 | material remain — **regulated lens CLEAN; rest fix-hygiene** |

\* plus a few editorial-consistency nits fixed in passing (no material findings).

Total: **302 findings raised and resolved** across **five** review bars over
**twenty-three rounds** (with five interleaved solutioner fix passes). The
progression is the real lesson: **design-doc** → no findings (R6);
**implementation-readiness** (3 technical lenses) → no findings (R12);
**fitness-for-purpose** (operability/cost/premise) → no findings (R14);
**type-of-software** (regulated/ML/UX domain experts) → 18 findings (R15); and the
**live-codebase** bar (8 full-domain lenses, after the `claude -p` worker-substrate
was added) → 62 → 57 → 29 → 10 → 9 (R16–R23). *Each genuinely new axis of scrutiny
re-opens the problem* — a clean verdict only ever bounds the axes that were looked
at, and the domain lens revealed that a thesis the three technical specialists had
signed off on was, in fact, domain-specific. This log is the honest record the
process produced, not a marketing artifact.

## Live-codebase loop (Rounds 16–24): stopped at substantive convergence

The post-`claude -p` loop was run **critic → fix → fresh-critic → repeat** at the
operator's request. It was **stopped by decision at Round 24**, on an explicit
convergence judgment rather than a zero-finding sweep — for reasons the trajectory
makes plain:

- **BLOCKERs 10 → 9 → 0 → 2 → 1; findings 62 → 57 → 29 → 10 → 9.** Volume collapsed
  and one lens (regulated) returned its first clean sweep at R23.
- **No new *architectural* defects after Round 19.** The R21/R23 findings were
  **fix-hygiene** — cross-doc drift (a ledger-key change not propagated to one doc),
  an undefined term (`decision_id`), a fix placed in the wrong layer (a nonce/latch
  put inside the pure reducer) — i.e. the **parallel-fixer method policing its own
  edits**, several of them orchestrator dispatch errors, not flaws in the design.
- Consistent with this log's standing lesson, an **absolute zero across eight
  adversarial lenses is a receding horizon**: a doc this dense will almost always
  yield one more refinement, and each fix round adds a thin new seam. "Stopped at
  substantive convergence" means the *design* is stable and internally consistent at
  the live-codebase bar; it does **not** mean proven correct — the genuine open
  questions (does structure beat a strong single agent; does it pencil out; will it
  be adopted; do the domain packs work) are still settled by evidence and real use,
  exactly as the master plan states.

The key lesson of Round 13: **convergence at one bar is not convergence at
another.** The technical panel (security/DS/AI) genuinely converged to zero at the
implementation-readiness bar — and then three *fresh* lenses (SRE/operability,
cost/scale, premise) immediately found 14 MAJORs about *shape, sequencing,
viability, and lifecycle* that the technical lenses structurally could not see.
"Internally sound" ≠ "the right, runnable, adoptable thing." Each new lens is a new
axis of scrutiny; a clean verdict only ever bounds the axes that were actually
looked at. This log is the honest record the process produced, not a marketing
artifact.

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
