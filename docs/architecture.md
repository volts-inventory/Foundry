# Architecture — Graph topology, state, and orchestration

This document specifies *how* the Foundry pipeline is wired as a LangGraph
`StateGraph`: the node/subgraph layout, the typed state **and its concurrency
semantics**, the source-control isolation model, durability and side-effect
handling (including the dual-write / atomicity protocol), the context-assembly
contract, model-determinism policy, and the control flow (gates, loops, joins,
interrupts).

## 1. Top-level graph

```mermaid
flowchart TD
    START([Intent / Request]) --> TRIAGE{Triage & Route · seeds default budget cap}
    TRIAGE -->|greenfield / evolution| PROD[Product Manager: PRD or change brief + acceptance criteria]
    TRIAGE -->|bugfix| MINSPEC[Repro + expected-behavior brief]
    TRIAGE -->|spike| SPIKE[Architect: timeboxed research]
    SPIKE --> SPIKEOUT([Spike report · not released])

    PROD --> CRITERIA[Criteria-completeness critique · independent role/family]
    CRITERIA --> SPECGATE{{Spec gate}}
    MINSPEC --> SPECGATE
    SPECGATE -->|greenfield/evolution: fail| PROD
    SPECGATE -->|bugfix: fail| MINSPEC
    SPECGATE -->|greenfield/evolution: pass| PLAN[Eng/Program Mgr: sprint plan + budget + risk]

    PLAN --> ARCH
    ARCH[Principal Architect: system design + ADRs + interfaces + ownership_map + pinned constraints]
    ARCH -->|capability match| SPECIALIST[Domain Specialist]
    ARCH -->|UI project| UX[UX/Design]
    ARCH --> DESIGNGATE{{Design gate + HITL}}
    SPECIALIST --> DESIGNGATE
    UX --> DESIGNGATE
    DESIGNGATE -->|changes requested| ARCH
    DESIGNGATE -->|approved| DECOMP[Senior Eng: task breakdown → branch-per-task]
    SPECGATE -->|bugfix: pass| DECOMP

    DECOMP --> IMPL[Eng implements task on its own branch]
    IMPL --> PBUILD{{Per-branch Build gate}}
    PBUILD -->|fail| IMPL
    PBUILD -->|pass| REVIEW[Reviewer reviews diff · QA authors/runs tests · cross-family on high-risk]
    REVIEW -->|changes requested| IMPL
    REVIEW -->|approve| PTEST{{Per-branch Test gate · criterion fidelity}}
    PTEST -->|fail| IMPL
    PTEST -->|tasks > 1: all branches pass| INTEG[Integration node: merge branches, resolve conflicts]
    PTEST -->|tasks == 1: single branch| IBUILD

    INTEG --> IBUILD{{Integrated Build+Test gate}}
    IBUILD -->|fail / merge conflict| FIX[Targeted fix on branch]
    IBUILD -->|integration reveals structural design defect| DECOMP

    IBUILD -->|pass| PAR{{Assurance fan-out · Send, per-branch deadline}}
    PAR --> SEC[Security/AppSec: threat model + SAST/DAST/deps]
    PAR --> AUD[Audit/Compliance: SBOM + license scan]
    PAR --> SUPPLY[Supply-chain gate: build/CI/IaC/scanner-config policy]
    SEC --> AJOIN[Assurance join · tolerates per-branch error]
    AUD --> AJOIN
    SUPPLY --> AJOIN
    AJOIN -->|any high/critical finding| FIX
    AJOIN -->|branch error / timeout| RETRYB[Bounded per-branch retry → else escalate]
    AJOIN -->|all clean| E2E[SDET: integration + e2e]
    E2E --> E2EGATE{{E2E gate}}
    E2EGATE -->|fail / error| FIX
    E2EGATE -->|pass| ATTEST[[Trusted hermetic CI builder · no egress · SBOM + SLSA + sign · gate-state in predicate]]
    ATTEST --> RELGATE{{Release gate · HITL on attested artifact + evidence pack}}
    RELGATE -->|rejected w/ reason| FIX
    RELGATE -->|approved| ADMIT[[Deploy-time admission controller · verify signature + gate-state for this digest]]
    ADMIT -->|verify fail| FIX
    ADMIT -->|verified| OPS[Non-agent deployer: deploy + changelog + rollback]
    OPS --> DONE([Released])

    FIX -->|tasks > 1: re-merge| INTEG
    FIX -->|tasks == 1| IBUILD

    MON[[Guardian: deterministic monitors + event-triggered sandboxed analyzer + convergence bound]] -. halt/pause/escalate .- TRIAGE
```

Load-bearing control rules:

- **Single-entry / single-exit subgraphs; gate failures return to the route's
  producer** (Spec fail → `PROD` for greenfield/evolution, `MINSPEC` for bugfix).
- **Integration is task-count-based** (`tasks > 1` → `INTEG`; `tasks == 1` →
  `IBUILD`), so bugfix *and* single-task greenfield/evolution are both covered.
- **One re-verification funnel with a convergence bound.** Every fix routes
  through `FIX`; multi-task fixes re-merge through `INTEG` (new tree → new
  `tree_sha`), single-task fixes go straight to `IBUILD`. A changed tree
  invalidates *all* integrated verdicts (§4), so re-verification re-runs
  Build+Test **and** Security/Audit/Supply-chain/E2E. A **global convergence
  bound** (§7) stops the cross-gate "whack-a-mole" cascade that per-loop counters
  cannot (the loop-detector keys on a semantic signature, not the diff bytes —
  see [governance.md §3](./governance.md)).
- **Assurance fan-out uses `Send` with per-branch deadlines and an `error`
  status**, so one crashed/timed-out branch neither voids its siblings' committed
  results nor wedges the join (§5, [governance.md §3](./governance.md)).
- **Supply-chain gate** runs in the assurance fan-out: build/CI/IaC/Dockerfile/
  lockfile/scanner-config changes are high-privilege artifacts reviewed
  separately from application code ([governance.md §4](./governance.md)).
- **Attest-then-release-then-admit.** The trusted hermetic builder (no egress)
  produces the signature + SLSA provenance **with the gate-state bound into the
  predicate** *before* the Release gate; after human sign-off, a **deploy-time
  admission controller** re-verifies the signature *and* that all required gates
  PASS for this exact digest before a **non-agent deployer** ships
  ([governance.md §5](./governance.md)).
- **Independent criteria critique.** Greenfield/evolution criteria pass an
  independent completeness critique (esp. security/abuse criteria) before the
  Spec gate, so "100% criterion-coverage" can't be satisfied by weak criteria
  ([governance.md §6](./governance.md)).

## 2. Subgraphs

- `product_subgraph(intent) -> {PRD, acceptance_criteria}` (+ criteria critique)
- `architecture_subgraph(PRD) -> Design` (conditionally invokes UX / Domain Specialist; emits `pinned_constraints`)
- `implementation_subgraph(Design, task) -> branch` (per-task; evaluator-optimizer loop + per-branch Build/Test gates)
- `integration_node(branches[]) -> IntegratedTree` (runs only when `tasks > 1`)
- `assurance_subgraph(IntegratedTree) -> {SecurityReport, AuditReport, SupplyChainReport}` (`Send` fan-out + join)
- `release_subgraph(attested tree) -> Release` (admission controller + non-agent deployer)

## 3. Typed state and concurrency semantics

LangGraph merges updates **per channel** at each superstep, folding the pending
writes as an **unordered set**. Reducers must therefore be **associative *and*
commutative**, **`None`-safe**, and must establish ordering from data (a
monotonic field) rather than iteration order. Large/append-only data is held
**out-of-band by content-addressed handle** to bound checkpoint size.

```python
import operator
from typing import Annotated, TypedDict
from pydantic import BaseModel
from enum import Enum

class Artifact(BaseModel):
    kind: str
    uri: str                       # content-addressed store ref — GLOBALLY UNIQUE identity
    produced_by: str
    task_id: str | None = None
    version: int = 1
    supersedes: str | None = None
    seq: int                       # monotonic logical clock (see below)

class GateResult(BaseModel):
    name: str
    status: str                    # "pass" | "fail" | "error"  (error = infra failure, not a verdict)
    decision_deterministic: bool
    execution_deterministic: bool
    findings: list[str] = []
    retries: int = 0
    run_against: str               # per-branch: "task/<id>"; integrated: tree_sha (see §4)
    seq: int                       # monotonic; resolves latest-wins by CAUSALITY, not iteration order

class LogRef(BaseModel):           # handle to out-of-band data (history/large artifacts)
    uri: str                       # append-only store / object store
    count: int                     # number of records, for cheap reads
    head_seq: int

class FactoryState(TypedDict):
    request: str                                              # single-writer
    route: str | None                                         # single-writer (triage)
    phase: str                                                # single-writer (supervisor)
    seq_clock: int                                            # single-writer; monotonic source for seq
    required_capabilities: Annotated[list[str], _union]       # fan-in, deduped, commutative
    pinned_constraints: Annotated[list[dict], _union_by_id]   # fan-in; NEVER summarized (see §8)
    artifacts: Annotated[list[Artifact], _merge_by_uri]       # fan-in, set-union by uri (commutative)
    gates: Annotated[list[GateResult], _merge_gates]          # fan-in, max-by-seq per (name, run_against)
    tasks: Annotated[list[dict], _union_by_id]                # fan-in, keyed (commutative)
    budget: BudgetLedger                                      # single-writer (Budget Ctrl)
    actions: Annotated[list[ExternalAction], _union_by_key]   # fan-in, keyed by idempotency_key
    history_ref: LogRef                                       # handle; raw events live out-of-band
```

Reducer rules (all `None`-safe, commutative, identity-keyed):

```python
def _merge_by_uri(a, b):                      # artifacts: set-union by content address
    m = {x.uri: x for x in (a or [])}
    for x in (b or []): m[x.uri] = x
    return list(m.values())

def _merge_gates(a, b):                        # gates: LATEST BY seq per (name, run_against)
    m = {}
    for g in (a or []) + (b or []):
        k = (g.name, g.run_against)
        if k not in m or g.seq > m[k].seq:     # causal latest, NOT iteration order
            m[k] = g
    return list(m.values())
```

Why each fix matters (maps to Round-7 findings):

- **`gates` is max-by-`seq`, not last-iterated** — concurrent or batched gate
  writes resolve to the causally-newest verdict, so a real PASS can't be masked
  by a stale FAIL (or vice-versa) due to fold order. `seq` is drawn from the
  single-writer `seq_clock`. *(DS1, DS8)*
- **`status` distinguishes `error` from `fail`** — an infra crash is not a
  verdict; the join handles it separately (§5). *(DS7)*
- **All list channels are keyed set-unions** (`tasks` by id, `actions` by
  idempotency key), so no channel relies on `operator.add` concatenation order
  for meaning; anything order-sensitive reads by `seq` and sorts. *(DS8)*
- **`history` and large artifact bodies live out-of-band** (`history_ref` +
  object store); the checkpoint holds bounded handles, so checkpoint size doesn't
  grow with trajectory length and Postgres write-amplification stays linear in
  *new* data, not total. *(DS9)*
- **`pinned_constraints`** is a first-class, never-summarized channel (§8). *(AI2)*

## 4. `tree_sha` — precise, behavior-stable definition

The integrated-gate keying and invalidation correctness depend on `tree_sha`
being a deterministic hash of *logical state that affects behavior* and nothing
volatile. It is defined as:

```
tree_sha = H( normalized_source_tree   # git tree of TRACKED SOURCE only,
                                        #   build outputs excluded via .gitattributes/.gitignore,
                                        #   canonical path ordering, no embedded timestamps
            ⧺ resolved_lockfile_hash    # exact dependency versions (all ecosystems)
            ⧺ toolchain_digest          # pinned builder image + tool versions
          )
```

- **Excludes** build artifacts, caches, and any codegen-embedded timestamps, so
  the *same logical state hashes identically across clean checkouts on different
  machines* (a CI invariant test enforces this). This kills spurious
  invalidation → the cascade in §7. *(DS4)*
- **Includes** the resolved lockfile and toolchain digest, so a dependency or
  toolchain change *does* change the hash — two different logical states cannot
  collide into a stale "already verified" PASS. *(DS4, ties to supply-chain §SEC1)*
- Integrated `GateResult.run_against = tree_sha`. A `FIX` (re-merged for
  multi-task) yields a new `tree_sha`, so prior Security/Audit/Supply-chain/E2E
  verdicts no longer match the current key and are re-run. Per-branch verdicts
  key on the stable `task/<id>` and are invalidated when that branch's base moves
  (rebase resets the branch; its verdicts are re-emitted with a higher `seq`).

## 5. Durability, atomicity, and external side effects

Resume restores *internal* state; it does not undo external effects. The dual
-write between the checkpoint DB and the outside world (git, GitHub, deploys,
WORM audit) is handled by a **transactional outbox**, not by hope.

- **One Postgres transaction** commits, per superstep: the LangGraph checkpoint,
  the `budget` ledger, the `actions` ledger update, **and** an **outbox** row for
  every external effect and every audit event. The checkpoint and the ledgers
  can never diverge. *(DS3)*
- **A separate relay** drains the outbox to external systems (git/PR/tag/deploy)
  and to the **append-only WORM audit store**, at-least-once, each carrying its
  `idempotency_key`. *(DS3)*
- **Effectful tools are idempotent at the external boundary.** The
  `idempotency_key` is **transmitted to and stored by the external system**
  (GitHub: embedded in branch/tag name, PR marker, or client idempotency token;
  deploys: server-side idempotency token). The protocol is **intent-log → act →
  confirm-log**. On resume the node **re-queries the external system *by
  idempotency key***; if the system cannot answer authoritatively, the node
  **fails to the human** (never assumes done or not-done). This closes the
  two-generals window to a single, human-mediated boundary. *(DS2)*
- **Checkpointer:** `PostgresSaver` (prod) / `SqliteSaver` (dev).
- **Time-travel** rewinds *internal state only*; forking creates a **new run ID**,
  never mutates the original WORM record, and replaying past an effectful node
  hits the external idempotency check and skips.
- **Partial-failure of a parallel branch:** assurance/impl branches are
  dispatched with `Send` so each commits independently; a branch that raises
  records a `GateResult{status:"error"}` and is retried *alone* (bounded) rather
  than failing the whole superstep and discarding sibling work. A per-branch
  **deadline** prevents a hung branch from wedging the join → escalate on timeout.
  *(DS6, DS7)*

## 6. Budget enforcement under the execution model

LangGraph supersteps are atomic: a node cannot be checkpointed mid-execution.
Budget control is therefore defined against that reality:

- **Enforcement at superstep boundaries:** the Budget Controller checks caps
  **before dispatching the next superstep**, not inside a running node. *(DS6)*
- **Cooperative cancellation for long node bodies:** a node polls a
  cancellation token and persists its own partial progress to git (external
  store) plus a resumable marker to state *before yielding*; there is no
  mid-write kill. *(DS6)*
- **`Send`/map-reduce per-item commit:** parallel work uses independent branches
  so an aborted branch fails only itself and siblings' committed work survives;
  re-executed branches are idempotent. *(DS6)*

## 7. Convergence — stopping the cross-gate cascade

Per-loop retry caps bound a *single* gate's rework, but a fix can satisfy one
gate while breaking another across a *new* `tree_sha`, and the loop-detector's
diff-hash signature never repeats across such progressing-but-non-converging
fixes. Two additional, finding-driven controls:

- **Global convergence bound:** a `fix_attempts_since_all_green` counter
  (distinct `tree_sha`s produced without reaching all-integrated-green). Crossing
  it escalates as a **non-convergence** event — *not* a budget breach — so
  operators get the right diagnosis before maximum spend. *(DS5)*
- **Semantic loop signature:** the Guardian loop-detector keys on
  `(criterion_id/task, gate, failure_signature)` — failing test ids / error
  classes — *not* on diff bytes, so cosmetically-different regenerations of the
  same stuck attempt are detected; a separate normalized-diff *similarity* check
  is a secondary signal. *(DS5, DS10)*

## 8. Context assembly, retrieval, and model determinism

Long-horizon success lives or dies here, so the contract is mechanistic, not
"summarize history + embed code":

- **Pinned, never-summarized state.** ADR decisions, rejected alternatives,
  invariants/constraints, high-risk acceptance criteria, and ownership
  constraints live in the `pinned_constraints` channel and are injected
  **verbatim** into every node that could violate them. Only narrative/event
  history is summarized. *(AI2)*
- **Retrieval is a measured contract, not vibes.** Symbol index (tree-sitter/LSP)
  **plus the call/def + dependency graph** (impact analysis) — not embedding
  similarity alone — so cross-file/implicit coupling is surfaced. A
  **retrieval-recall eval** against labeled known-relevant spans gates the
  retriever ([evaluation.md §5](./evaluation.md)). *(AI2)*
- **Explicit "insufficient context" escape.** A node may return
  `need-context(query)` to re-retrieve or escalate rather than hallucinate over a
  partial view; retrieval failures are detected, not silently tolerated. *(AI2)*
- **History compaction** summarizes only events, with raw events retained
  out-of-band (`history_ref`); summaries are themselves checkpointed.
- **Sampling & determinism policy.** Temperature 0 (low top-p) for gates,
  routing, and all judge/review nodes; higher only where exploration helps
  (architecture ideation). The run manifest **and** WORM store record, per node,
  the temperature/seed, the pinned model ID, **and the provider's
  model-version/system-fingerprint**; a change in that fingerprint raises a
  **pinned-ID-drift** alert (it invalidates prior calibration/eval baselines).
  Audit is honestly framed as an **immutable record of actual decisions and
  inputs**, and replay reproduces *internal control state*, not bitwise-identical
  LLM outputs. *(AI5)*
- **Model pinning, tiers, failover.** Tiers T1/T2/T3 with pinned IDs;
  **T1 judgment nodes never auto-downgrade** (breach escalates). Failover is
  stated honestly: **intra-family** outage handling is retry/backoff/queue then
  **pause-and-escalate** (a same-weights "alternate" is not real redundancy);
  **cross-family** failover, where used, carries **per-model judge calibration**
  and records the serving model per node in the manifest. *(AI9)*
- **Prompt caching** of stable SOP/system prefixes and pinned reference
  artifacts is the primary token-cost lever (low cache benefit for the analyzer,
  which is therefore event-triggered — [governance.md §3](./governance.md)).

## 9. Orchestration patterns → where they appear

| Pattern (Anthropic) | Location |
|---|---|
| Routing | `Triage & Route` |
| Orchestrator-workers | Supervisor → layer subgraphs |
| Evaluator-optimizer | Per-task Implementation ⇄ Review/QA |
| Parallelization (scatter-gather) | Branch-per-task impl; `Send` assurance fan-out + join |
| Prompt chaining | SOP steps inside each role node |
| Reflection | Role self-critique before hand-off |
| Human-in-the-loop | Design gate, Release gate (with evidence pack), error/non-convergence escalations |

## 10. Why structured artifacts (not chat)

Agents communicate via content-addressed, versioned artifacts (MetaGPT's
error-reducing structured communication). Structured ≠ lossless, so intent
fidelity across hand-offs is independently checked
([evaluation.md §6](./evaluation.md)); chat is only for in-loop critique.
