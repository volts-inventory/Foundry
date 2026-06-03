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
- **One re-verification funnel, dependency-scoped, with a convergence bound.**
  Every fix routes through `FIX`; multi-task fixes re-merge through `INTEG` (new
  tree → new `tree_sha`), single-task fixes go straight to `IBUILD`. A changed
  tree invalidates the integrated verdicts the change can affect; re-verification
  re-runs **only those** gates and **reuses cached runner-attested verdicts** for
  unaffected inputs (keyed on the unchanged input sub-hash), so the final attested
  gate-state still covers the whole tree at change-proportional cost
  ([governance.md §1f](./governance.md)). A **global convergence bound** (§7) stops
  the cross-gate "whack-a-mole" cascade that per-loop counters cannot (the
  loop-detector keys on a semantic signature, not the diff bytes — see
  [governance.md §3](./governance.md)).
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

> **The per-node inner loop is a pluggable adapter, not a core dependency.**
> `implementation_subgraph` (and, optionally, QA/Review/Security-triage nodes) run
> their plan→edit→test→iterate loop through a **coding-agent substrate contract**:
> **in** — a task packet (`pinned_constraints` + retrieved spans + a scoped
> sandboxed worktree + least-privilege tools + a turn/cost budget); **out** — a
> diff + test **sources** + `GateTriage` + the `served_family`/`served_model_id`
> the adapter actually drove (recorded per node in the run manifest). Adapters:
> Claude Code (`claude -p`), OpenAI Codex CLI (`codex exec`), OpenHands/SWE-agent,
> or a raw multi-provider ReAct loop. Because the substrate is a swappable seam, a
> high-risk review/QA node can be required to run a **different served family** than
> the implementer it checks — preserving the judge decorrelation the model tiering
> depends on ([PLAN §6](../PLAN.md)).
>
> **Two verdict kinds — adapters never sign a deterministic-gate verdict, but
> judgment nodes do emit a judgment verdict.** For deterministic gates the adapter
> emits only a diff + test **sources** + `GateTriage`; the *signed
> deterministic-gate verdict* is computed and signed solely by the trusted
> two-zone runner (the forged-results-stream defense — [governance §1c](./governance.md)).
> But LLM-judgment seams (Review approve/request-changes, Threat-review residual,
> perceptual class-5) legitimately emit a **judgment verdict** that routes the
> evaluator-optimizer loop — there is no deterministic oracle to sign one. The
> *only* integrity controls on a judgment verdict are **cross-family decorrelation**
> (below) **+ HITL**; the blanket "never a verdict" does not apply to these nodes.
>
> **Decorrelation binds served lineage, not adapter brand.** OpenHands, the raw
> loop, *and* Claude Code can each drive any model, so the brand carries no lineage
> guarantee — every adapter must **declare** its `served_family`/`served_model_id`.
> A deterministic, admission-/CI-checked invariant fails closed on high-risk
> judgment criteria when `reviewer.served_family == implementer.served_family`,
> independent of brand. Cross-family failover (§8) is **decorrelation-aware**: if
> the only available family on a high-risk judgment node would violate the
> invariant, the node **pauses-and-escalates** (consistent with "T1 never
> auto-downgrades") — it never silently fails over into a correlated configuration.
>
> **The adapter boundary is ENFORCED by the sandbox, not asserted** — the adapter
> runs arbitrary bash, so "writes worktree only / no external side-effect" must be
> a property of the cage, not a promise: (a) the **only** writable mount is the
> task worktree; (b) the **only** network route is a **mediating model-provider
> proxy** that holds the provider credential, injects the system prompt/SOP/tools,
> content-scans + schema-validates egress bodies against the task data profile, and
> meters tokens/USD/entropy — no git remote, no registry except the hash-pinned
> mirror reached *through* the proxy; (c) outbox, result-channel, and credential
> endpoints live in namespaces the adapter process tree cannot address (separate
> netns, no FD/socket inheritance, no shared mount); (d) the sandbox is destroyed
> and rebuilt between the untrusted-execution phase and any credentialed phase — no
> reuse.
>
> **Adapter vs superstep atomicity, budget, and cancellation.** The adapter runs a
> long multi-turn loop *inside one superstep*, so superstep-boundary budget
> enforcement and the cooperative-cancellation token (§6) — which a third-party CLI
> does not poll — do **not** bind it, and a mid-loop crash would discard all
> uncommitted work. The contract therefore requires: (a) **incremental resumable
> progress** — each adapter turn persists to the worktree with a resumable marker,
> the node is re-enterable mid-task, and budget/cancellation are enforced at
> **adapter-turn boundaries** (a sub-superstep meter), not only at LangGraph
> superstep boundaries; (b) the adapter is **metered and hard-killed from outside**
> (proxy token/USD accounting + cgroup CPU/wall-clock + `SIGKILL` at cap) —
> adapter-reported budget is **advisory only**; (c) an adapter that cannot meet the
> incremental-progress + external-meter contract is **ineligible** for the
> substrate. *Which adapters can meet it is an open integration question.*

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

Seq = tuple[str, int, str, int]    # (run_id, superstep_index, node_id, local) — see "Seq" below

class Artifact(BaseModel):
    kind: str
    uri: str                       # content-addressed store ref — GLOBALLY UNIQUE identity
    produced_by: str
    task_id: str | None = None
    version: int = 1
    supersedes: str | None = None
    seq: Seq                       # TOTAL causal order (no ties) — see "Seq"

class GateResult(BaseModel):
    name: str
    status: str                    # "pass" | "fail" | "error"  (error = infra failure, not a verdict)
    decision_deterministic: bool
    execution_deterministic: bool
    # For deterministic gates the verdict is WRITTEN ONLY by the trusted attestor (zone B,
    # governance §1c); the signature binds the verdict to its inputs so it can't be replayed:
    verdict_class: str             # config-evaluable | execution-deterministic | statistical | human-witnessed | perceptual (domains.md §1)
    runner_attestation: str | None # sig over (name, state_sha, scanner_config_rev, status, results_digest); human sig for class "human-witnessed"
    results_digest: str | None     # class 1/2: hash of raw zone-A results; class 3: (metric, dataset_hash, seed, n, estimate, ci, decision)
    findings: list[str] = []
    retries: int = 0
    run_against: str               # per-branch: "task/<id>"; integrated: state_sha (tree_sha + data/weight refs, see §4 + domains.md §2)
    seq: Seq                       # TOTAL causal order across runs AND forks; never iteration order

class GateTriage(BaseModel):       # agents write THIS, never GateResult, for deterministic gates
    name: str
    run_against: str
    explanation: str               # triage/remediation notes; advisory, never a verdict
    produced_by: str
    seq: Seq

class LogRef(BaseModel):           # handle to out-of-band data (history/large artifacts)
    uri: str                       # append-only store / object store
    count: int                     # number of records, for cheap reads
    head_seq: int

class FactoryState(TypedDict):
    request: str                                              # single-writer
    route: str | None                                         # single-writer (triage)
    phase: str                                                # single-writer (supervisor)
    required_capabilities: Annotated[list[str], _union]       # fan-in, deduped, commutative
    pinned_constraints: Annotated[list[dict], _merge_constraints]  # fan-in; relevance-scoped, supersedable (§8)
    artifacts: Annotated[list[Artifact], _merge_by_uri]       # fan-in, set-union by uri (commutative)
    gates: Annotated[list[GateResult], _merge_gates]          # fan-in, max-by-seq per (name, run_against); deterministic-gate writes require valid attestation
    gate_triage: Annotated[list[GateTriage], _union_by_key]   # fan-in; agent triage notes (never verdicts)
    tasks: Annotated[list[dict], _union_by_id]                # fan-in, keyed (commutative)
    budget: BudgetLedger                                      # single-writer (Budget Ctrl); meters token AND compute (GPU/external-job) cost — domains.md §3
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
        # Deterministic-gate verdicts are accepted ONLY with a valid runner attestation,
        # so an unsigned agent write can never supersede the trusted runner's verdict:
        if g.name in DETERMINISTIC_GATES and not verify_attestation(g):
            continue
        k = (g.name, g.run_against)
        if k not in m or g.seq > m[k].seq:     # TOTAL tuple order — ties are impossible
            m[k] = g
    return list(m.values())
```

**Seq — a total, race-free causal order *within* a run; globally unique across
runs.** `seq` is **not** a shared mutable counter (parallel branches share one
immutable input snapshot and could not read-increment one without a race). It is
the lexicographic tuple `(run_id, superstep_index, node_id, local)`:

- `run_id` — a **globally-unique** run/fork identifier (e.g. a ULID allocated at
  run/fork creation), recorded in the WORM record. It is **not** monotonic across
  concurrent forks, and that is fine: **cross-fork verdict resolution does not
  depend on `seq` order at all** — `_merge_gates` runs per-run (it never sees two
  forks' channels), and *across* runs the admission controller is **fail-closed**
  ([governance.md §5](./governance.md)), not seq-ordered. `run_id` is present
  purely so two forks' records for the same `tree_sha` are *distinct identities*
  in the WORM/predicate stores, never colliding. *(DS-R9-F1, DS-R10-F3)*
- `superstep_index` — read from LangGraph **run config metadata at emit time**
  (`config["metadata"]["langgraph_step"]`); monotonic across the run, **constant
  across all branches of one `Send` fan-out** (hence the tie-breakers below), and
  strictly greater in any later superstep (so retries/rebases outrank prior writes).
- `node_id` — deterministic id of the emitting node, separating distinct siblings
  in one fan-out. For *same-node-id* branches (branch-per-task), separation comes
  from the channel **key** (`run_against`), not `seq`. For `Send` fan-out
  emissions to a **non-keyed** channel (same `node_id`, same `superstep_index`),
  the per-item **branch key** is folded into `seq` — otherwise siblings collide;
  alternatively every fan-out-written channel must be **key-deduped/commutative and
  never `seq`-ordered** (the reducer-invariant test asserts whichever discipline
  the channel adopts).
- `local` — per-node monotonic counter for multiple emissions by one node. It must
  be a deterministic function of the emission's node-internal **logical position**,
  **not wall-clock emission order** — OR all channel writes must be content/key
  idempotent so a replayed write coalesces. Otherwise LLM-nondeterministic
  re-execution assigns a different `seq`/key to a logically-identical write,
  duplicating or mis-ordering gate records on replay. **This replay-determinism
  requirement is mandatory.**

Within a run `run_id` is constant, so `(superstep_index, node_id, local)` gives a
**total** order — no equal-`seq` fallthrough to fold order — which is all
`_merge_gates` needs (it only ever folds writes of one run). A reducer invariant
test asserts order-independence per key. *(DS1, DS2, DS8, DS-R9-F1, DS-R9-F3, DS-R10-F3)*

Why each fix matters:

- **`gates` is max-by-`seq` over a total order *and* attestation-gated**, so a
  real PASS can't be masked by a stale FAIL regardless of fold order, **and an
  unsigned agent write can never supersede the trusted runner's deterministic-gate
  verdict in the live channel** — agents write only `GateTriage`
  ([governance.md §1c](./governance.md)). In-graph routing and the HITL evidence
  pack read the attested record. *(DS1, DS2, DS8, DS-R9-F2)*
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
  verdicts no longer match the current key and are re-run.
- **Execution-dependent verdicts depend on inputs *not* in `tree_sha`.** Cached
  runner-attested Test/E2E/SAST verdicts also depend on the rotating held-out
  **mutant family** and the **scanner rule-set version** — neither is in the source
  tree. The **mutant-rotation-id + scanner-rule-version** are therefore bound into
  the **attestation tuple** *and* the **dependency-scoped reuse key**, so a cached
  class-2 (execution-dependent) verdict is invalidated across a rotation /
  rule-version boundary and is **not** reused across it. *(ties to §SEC1)*
- **`state_sha` generalization (data/ML domains).** For software whose behavior
  depends on data or model weights, `tree_sha` is the source-only special case of
  `state_sha = H(source ⧺ lockfile ⧺ toolchain ⧺ data_refs ⧺ feature_defs ⧺
  weight_refs)`, where data/weight **content identities** (manifest hashes) enter
  the key while the bytes stay in their external versioned stores. This makes
  dependency-scoped re-verification *sound* for ML — a dataset/label/weight change
  dirties the data-dependent gates instead of reusing a stale cached PASS
  ([domains.md §2](./domains.md)). A pack declares which refs are data vs weights. Per-branch verdicts
  key on the stable `task/<id>` and are invalidated when that branch's base moves
  (rebase resets the branch; its verdicts are re-emitted with a higher `seq`).

## 5. Durability, atomicity, and external side effects

Resume restores *internal* state; it does not undo external effects. The dual
-write between the checkpoint DB and the outside world (git, GitHub, deploys,
WORM audit) is handled by a **transactional outbox**, not by hope.

- **The outbox is the *only* durable-effect surface.** Every external effect is
  Foundry's explicit outbox enqueue, derived from the **validated worktree diff**.
  Because the sandbox denies the adapter all network/git egress (§2(b,c)), the
  adapter **cannot** produce an external effect directly — which is precisely what
  makes "writes worktree only" an *enforced* property rather than an assertion.

- **One Postgres transaction** commits, per superstep: the LangGraph checkpoint,
  the `budget` ledger, the `actions` ledger update, **and** an **outbox** row for
  every external effect and every audit event. The checkpoint and the ledgers
  can never diverge. *(DS3)*
- **A separate relay** drains the outbox to external systems (git/PR/tag/deploy)
  and to the **append-only WORM audit store**, at-least-once, each carrying its
  `idempotency_key`. *(DS3)*
- **Effectful tools are idempotent at the external boundary**, via one of three
  paths in priority order: *(DS2)*
  1. **External client key** — the `idempotency_key` is transmitted to and
     stored by the external system (deploy idempotency token; GitHub client
     token); resume **re-queries by key**.
  2. **Embedded marker** — where no client key exists but the effect is
     queryable, the key is embedded (branch/tag name, PR marker commit) and
     resume re-derives existence.
  3. **Foundry-side dedup record** — for effects whose system supports *neither*
     (fire-and-forget webhooks, notifications, registries with no by-key lookup),
     the relay writes a **local dedup row in the same outbox transaction** and
     consults it before emitting, giving effectively-once delivery (true
     exactly-once being impossible). **Path 3 is reserved for effects whose
     duplicate-or-omission is tolerable (notifications) — never for anything a
     gate, release, or deploy depends on** — because a crash between *act* and
     *confirm-log* on a non-queryable effect resolves to **fail-to-human**, not
     autonomous dedup. *(DS-R9-F4)*

  The protocol is **intent-log → act → confirm-log**; if none of the three can
  establish state on resume, the node **fails to the human** (never assumes). *(DS2, DS3-F3)*
- **All idempotency / dedup / marker records are GLOBAL** (run-independent): they
  survive checkpoint forks, and fork-recovery replay re-queries them exactly as an
  ordinary resume does. So a poisoned-checkpoint fork **never re-fires** a path-2 or
  path-3 effect — the global record already reflects the prior emission regardless
  of which run/fork produced it.
- **Checkpointer:** `PostgresSaver` (prod) / `SqliteSaver` (dev).
- **Time-travel** rewinds *internal state only*; forking creates a **new run ID**,
  never mutates the original WORM record, and replaying past an effectful node
  hits the external idempotency check and skips.
- **Cross-fork stable-FAIL latch is queryable at the E2E/Release gate, not only at
  deploy-admission.** The per-run `gates` reducer (§3) folds one run's channels and
  cannot see a *sibling fork's* stable-FAIL latch on the same digest — so without
  this a fork could consume the full human Release sign-off before deploy-admission
  bounces it. The stable-FAIL latch on a digest is therefore surfaced from
  **cross-fork WORM latch state at the E2E/Release gate**, so a latched digest
  blocks **before** consuming human sign-off; the run is marked with run-status
  reason **`latched_by_sibling_fork`** ([operations.md](./operations.md) owns the
  status enum). This is the only cross-fork read in the gate path; live verdict
  resolution stays per-run and admission stays fail-closed (§3).
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

- **Pinned, never-summarized — but bounded and scoped.** ADR decisions, rejected
  alternatives, invariants, high-risk acceptance criteria, and ownership
  constraints live in `pinned_constraints` and are injected **verbatim** (never
  summarized) — but the set is **not** an unbounded monotonic union (which would
  overflow context on exactly the long-horizon runs it protects). Four controls
  keep it bounded *without* silently dropping a relevant constraint: *(AI2, AI-R8-F2, AI-R9-F1)*
  - **Global (non-scopable) subclass — always injected.** Cross-cutting
    invariants ("monetary values in integer cents," "no PII in logs"),
    security/abuse constraints, and trust-root constraints are flagged
    `scope: global` and injected **verbatim into every node regardless of
    relevance scoring** — these have the weakest call/def-graph signal, so they
    are never subject to a scoping miss. **The classification is validated, not
    trusted to the Architect:** a deterministic check flags any constraint whose
    text matches cross-cutting/security patterns (money, PII, logging, auth,
    crypto) that is *not* marked `global`, routing the mismatch to HITL the same
    way criteria/config edits trip. This bounds the blast radius of any scoping
    error to *narrowly-local* constraints (module-specific ownership limits, local
    rejected-alternatives) whose omission is low-stakes — and even those are
    backstopped by the local-scoper recall floor. *(AI-R10-F1)*
  - **Relevance scoping (local subclass only):** local constraints are scoped to a
    node's active task/module (via `ownership_map` + retrieval graph) under a
    **per-node injection token budget**. Because scoping is itself a retrieval
    problem, the scoper has its **own measured recall floor** over a labeled
    `(task, relevant-constraint)` set ([evaluation.md §4](./evaluation.md)) — gated,
    not assumed.
  - **Supersession:** `_merge_constraints` retires a constraint by id when a later
    ADR reverses it (`supersedes`), so the set reflects current truth.
  - **Cardinality monitor:** escalates to HITL when the injected set approaches the
    budget — **never silently truncated**.
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
- **Model families vs tiers, pinning, failover.** Tiers T1/T2/T3 are *capability
  levels within the primary family* (Claude Opus/Sonnet/Haiku) — they are **not**
  different families. Because decorrelating judge blind spots requires a genuinely
  **different family** (shared training lineage shares blind spots), the stack
  provisions a **second model family from a second provider**, used specifically
  for (a) **high-risk review/QA judging** and cross-family disagreement triggers
  ([evaluation.md §4](./evaluation.md)) and (b) **cross-family failover** of T1
  nodes on a primary-provider outage. The serving family+model is recorded per
  node in the manifest, and **each judge model is calibrated independently**.
  Pinned IDs; **T1 judgment nodes never auto-downgrade in tier** (breach
  escalates); intra-family there is no real same-weights alternate, so a
  single-provider outage without the second family is retry/backoff then
  **pause-and-escalate**. **Failover is decorrelation-aware:** on a high-risk
  judgment node, if the only available family would make `reviewer.served_family ==
  implementer.served_family` (violating the §2 invariant), the node
  **pauses-and-escalates** rather than silently failing over into a correlated
  configuration — same discipline as the no-auto-downgrade rule. *(AI-R8-F1, AI3, AI9)*
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
