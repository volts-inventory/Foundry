# Org fan-out: domains, leads, conditional areas, and a parallel IC pool

Status: **design — not yet implemented.** Captured before building, per the
project's "design doc → critic-gated waves" discipline. Grounded in the current
code (file:line cited); nothing here weakens the trust invariant — **agents
propose, signed gates decide** — and every change stays fail-closed.

---

## 1. Why

Today the "company" has a real planning manager and a real domain decomposition,
but the implementation layer is flat and serial:

- **Engineering/Program Manager** (`foundry/agents/product/eng_program_mgr.py`) —
  owns milestones, budget envelope, risk register, definition-of-done.
- **Principal Architect** (`foundry/agents/architecture/principal_architect.py:205`)
  — carves the system into domains: emits the `OwnershipMap` (`owners: path/seam
  → owner`) plus `InterfaceContract`s that govern the seams *between* owners.
- **Senior Engineer** (`foundry/agents/implementation/senior_engineer.py:186`) —
  turns the OwnershipMap into one `Task` per ownership unit, in the deterministic
  `merge_order()` (`schemas/architecture.py:112`).
- **Junior Engineer** — the single IC that runs **all** tasks **sequentially** in
  one node (`agents/implementation/junior_engineer.py:340`), each in its own
  sandbox.

So the *work* is already divided into reproducible, assignable units — but one
Senior decomposes everything and one Junior drains the queue serially. There is
no per-domain lead, no area/department tier, and no parallel execution. The
roster is hardcoded in the implementation subgraph
(`graph/subgraphs/implementation.py:118`); adding engineers does nothing today.

## 2. Target org

```
                       Program Manager (1, singular)
                       · global budget envelope + go/no-go + checkpoints
                       · sub-allocates a budget envelope per AREA / lead
                                   │
              ┌────────────────────┼─────────────────────┐   (areas materialize
          [area: backend]     [area: frontend]       [area: data]   ONLY if the
          Area Manager        Area Manager           (1 small domain  project has
          · sub-budget         · sub-budget           → no manager;    that work)
          · x-domain deps      · x-domain deps        reports to PM)
              │                     │                     │
        ┌─────┴─────┐               │                     │
   Tech-Lead     Tech-Lead     Tech-Lead             Tech-Lead
   (domain A)    (domain B)    (domain C)            (domain D)
   · decompose its slice of the OwnershipMap into Tasks
   · owns its per-domain budget; reconciles its InterfaceContracts
              │
        IC pool (WorkPool, max_slots = N)  ── tasks run in PARALLEL
        · one ExecutionTask per Task, fanned out via LangGraph Send
        · each in its own Sandbox; results merge branch-per-task
```

**Rules (locked with the CTO):**

1. **Program Manager is singular** — one global budget/scope/go-no-go source of
   truth (the fail-closed `QuotaPolicy` needs one), and the CTO's single
   checkpoint counterpart.
2. **Areas are derived & conditional** — canonical vocabulary
   (`frontend`, `backend`, `data`, `infra`, `ml`, `security`) used as *labels*;
   an area + its manager is materialized **only** when the project actually has
   domains classified into it. A tiny area (1 small domain) skips the manager and
   the lead reports straight to the PM. No idle managers, ever.
3. **One Tech-Lead per domain** (per OwnershipMap owner) — this is the fan-out.
4. **IC pool executes in parallel** via the existing `WorkPool` seam.
5. **Budget flows down the tree** and is metered fail-closed at every admission.

## 3. Mapping onto existing seams (this is mostly wiring, not new machinery)

| Org concept | Existing seam it rides on | Change needed |
|---|---|---|
| Domains | `OwnershipMap.owners` (unit→owner) | none — owners *are* the domains |
| Area of a domain | *(new)* `OwnershipMap.areas: owner→area` | additive schema field, default `{}` → flat org (back-compat) |
| Tech-Lead per domain | Senior decomposition `senior_engineer.py:218` | fan out the Senior **per owner** (LangGraph `Send`), each emits its slice's Tasks |
| Conditional Area Manager | *(new node)* | one node per non-empty area; sub-allocates budget, checks `InterfaceContract` consumers crossing its boundary |
| Parallel ICs | `LocalWorkPool.submit` + `Send` (`work_pool.py:16`, `:323`) | replace the Junior's sequential loop with a `Send`-per-task fan-out into the pool |
| **Budget tree** | `QuotaPolicy.lineage_quota_microusd` (`work_pool.py:204`) | **lineage = area (or domain)**; the per-lineage quota already meters each bucket separately and fail-closed (`would_exceed`, `:208`) |

The budget tree is the part worth emphasizing: `ExecutionTask.lineage`
(`work_pool.py:153`) + `QuotaPolicy`'s per-lineage quota were **purpose-built**
for exactly this — "one lineage's fan-out cannot starve the others." We set
`lineage = <area>/<domain>` and the existing admission check enforces the tree
with no new budget code.

## 4. Concrete changes (staged)

Each wave: worktree-isolated implementer → fresh-eyes critic → fix → full suite
green → merge. No wave lands unless the trust/determinism invariants below hold.

- **Wave A — Area classification (data only, no behavior change).**
  Add `OwnershipMap.areas: dict[str, str]` (owner→area), default `{}`. Extend the
  Principal Architect to classify each owner into the canonical vocabulary
  (reusing the same capability signal that drives the `frontend`/`ml_data` domain
  packs). Add `OwnershipMap.areas_present()` / `leads_by_area()` helpers, both
  pure functions of the mapping (so resume re-derives identically). Ships dark:
  nothing consumes it yet. *Critic focus: schema back-compat + determinism of the
  classification.*

- **Wave B — Tech-Lead fan-out (decomposition parallelism).**
  Replace the single Senior node with a `Send`-per-owner fan-out: each Tech-Lead
  decomposes only its owner's units into Tasks, tagging each Task with its
  `area`/`domain` (for lineage). Fan-in merges on the existing keyed `tasks`
  channel (union by id — unchanged). Behavior-equivalent to today when there's one
  owner. *Critic focus: the merged task set is identical (same ids, same
  merge_order) to the pre-fan-out output — proven by a tree-sha identity test.*

- **Wave C — Parallel IC pool (execution parallelism).**
  Swap the Junior's sequential `for task in ready` loop for a `Send`-per-task
  fan-out that submits each `ExecutionTask` to a `LocalWorkPool(max_slots=N)`
  with `lineage = area/domain`. `execution_task_for()`
  (`junior_engineer.py:176`, already written for this) builds the durable record.
  Results read back via `pool.result()` and merge branch-per-task as today.
  *Critic focus: idempotent/no-double-run on resume; budget rejection still
  fail-closed; results identical to serial modulo order.*

- **Wave D — Conditional Area Managers + budget tree.**
  Materialize one Area-Manager node per non-empty area (skip singletons → report
  to PM). The PM sub-allocates the global envelope into per-area
  `lineage_quota_microusd`; each Area Manager sub-allocates to its domains. Pure
  budget-tree wiring on `QuotaPolicy`. *Critic focus: Σ(area quotas) ≤ global cap;
  an over-budget domain is rejected at admit, never run.*

- **Wave E — Surface it.** Telemetry/inspection payload gains
  `org: {areas, leads, ics, budget_tree}`; the checkpoint inspection shows the
  tree. The workshop game renders areas as wings, leads as desks, ICs as the pool
  (this is what makes "5 engineers in parallel" visible).

`max_slots` (N) is the only knob the operator sets; **physical** worker count
stays infra's job (`docker compose up --scale worker=N`) — the app never spawns a
container (`work_pool.py:18-22`).

## 5. Invariants preserved (must hold every wave)

1. **Gates decide.** Areas/leads/ICs only *propose* artifacts and diffs; the
   signed two-zone gate runner is untouched. More agents can only add proposals,
   never force a release.
2. **Determinism / reproducibility.** Area classification, lead assignment, and
   merge order are **pure functions of the OwnershipMap** — a LangGraph resume
   re-derives the identical org and the identical task set. No reliance on dict
   insertion order, wall-clock, or process state.
3. **Fail-closed budget.** Every admission (global, per-area, per-domain) is
   metered before a worker runs; an over-budget task is `rejected` at admit and
   never executed (`QuotaPolicy.would_exceed`).
4. **No double-run on resume.** Content-addressed `task_id` + idempotent claim
   coalesce duplicate submits (`work_pool.py:323`).
5. **Single egress preserved.** Every model call still goes through the
   `ModelProviderProxy` — fan-out multiplies callers, not egresses.

## 6. Explicitly out of scope (for now)

- Real `ContainerWorkPool` / `K8sJobPool` (still scaffolding — `work_pool.py:283`).
- A fixed Director→EM→Lead two-tier hierarchy (rejected: staffs empty teams).
- Dynamic *area* re-grouping mid-build (areas are fixed once the Architect emits
  the OwnershipMap for that run).
