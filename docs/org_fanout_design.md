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
| Area of a domain | *(new)* `OwnershipMap.area_of()` / `leads_by_area()` — **derived**, not stored | added in Wave A; pure methods, see note below |
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

- **Wave A — Area classification (data only, no behavior change). ✅ DONE.**
  **Decision (deviates from the original "add a field" plan):** areas are
  **derived on demand**, NOT stored. A stored `areas: dict[str,str]` field — even
  defaulting to `{}` — changes `model_dump`, so any *populated* map would churn
  the `ownership_map` `content_identity()` and every downstream `state_sha`. A
  derived method changes no serialization → **zero digest churn** → true
  back-compat. Since classification is a pure function of `owners` (itself frozen
  + content-addressed), the derived value is *implicitly* content-addressed —
  a resume recomputes the identical result. The one thing a stored field would
  buy (pinning the label so a future `_AREA_RULES` change can't retroactively
  re-label historical runs) belongs in **Wave B**, recorded on the `Task` it
  first influences, not on the OwnershipMap schema.
  Implemented in `foundry/schemas/architecture.py`: `OwnershipMap.leads()` /
  `units_of()` / `area_of()` / `areas_present()` / `leads_by_area()`, plus
  module-level `classify_area()` / `classify_areas()`. Classification reuses the
  domain-pack capability vocabulary; **`ml` folds into the `data` area** (the org
  has 5 areas — `frontend, backend, data, infra, security` — not the 6-label
  vocabulary). Matching is **whole-token only** (no substring matching: it
  false-fires on `tokenizer`/`interview`/`metadata`), erring toward precision
  with a `backend` fallback. Ships dark — nothing consumes it yet.

- **Wave B — Tech-Lead per-domain decomposition. ✅ DONE.**
  **Decisions (deviate from the original plan, both deliberate):**
  (1) *In-node loop, not LangGraph `Send`.* There is no `Send` precedent in this
  codebase (it uses `add_conditional_edges`), and `Send` interacts with the
  resumable checkpointer / `interrupt()` boundaries in ways that aren't worth the
  risk for *decomposition* — which is cheap reasoning the `work_pool` design
  itself keeps "in-process." So the existing `senior_engineer` node loops over
  `OwnershipMap.leads()`, one focused `proxy.complete` per lead. NODE_ID stays
  `"senior_engineer"` (it's embedded in `Seq`; renaming would churn every task
  seq). The real parallelism lives in **Wave C's `WorkPool`** (threads), which
  needs no `Send`. (2) *No `area` tag stored on the `Task`.* Consistent with
  Wave A: the Task record stays byte-identical, and lineage is **derived in
  Wave C** by recomputing `_task_id(unit)` over the OwnershipMap and reading
  `area_of(owner_of(unit))` — the owners map is frozen + content-addressed, so the
  derived lineage is implicitly content-addressed too.
  **Cost:** decomposition is now N model calls (one per owner) instead of one
  global call — an intentional N× multiplier that buys per-domain focus and that
  Wave D's budget tree meters. (§5.5's "fan-out multiplies callers, not egresses"
  holds at the egress; the call *count* does rise.)
  Implemented in `foundry/agents/implementation/senior_engineer.py` + per-lead
  advisory journal entries. Identity **proven** by the critic: single-owner →
  byte-identical (ids, branches, spec_uris, seq order, criteria, one call);
  multi-owner → same ids + same `merge_order`, per-domain criteria only.

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
