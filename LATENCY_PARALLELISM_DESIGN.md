# Latency Parallelism — Design (D1 / D7 / B2)

Design-first deliverable for the three high-blast latency items. These make the
pipeline **faster**, not cheaper (no token reduction) — they attack the serial
waterfall. Because they touch state reducers, `Seq` ordering, determinism, and
resume, this doc precedes any fan-out code (per the plan + CLAUDE.md gates).

## 0. Scope & non-goals

- **D1** — run the three implementation lanes (frontend/backend/data) concurrently
  instead of the serial conditional chain (`implementation.py:160-186`).
- **D7** — run each of the quality (`reviewer`/`qa`/`sdet`) and assurance
  (`appsec`/`audit`/`supply_chain`) author chains concurrently, converging at the
  gate.
- **B2** — reuse the coding session across fix attempts (coding agent only).
- **Non-goals:** any token reduction (this is wall-clock only); parallelizing the
  gate runners; changing the phase order.

## 1. The shared prerequisite — `Seq` under concurrency

LangGraph runs the parallel branches of a superstep on a thread pool. Two shared,
non-thread-safe seams must be fixed BEFORE any fan-out, or determinism/resume
breaks:

### 1a. `EpochCounter.bump` is an unlocked read-modify-write (`state/seq.py:46-50`)

```
nxt = self._epochs.get((run_id, key), 0) + 1
self._epochs[(run_id, key)] = nxt
```

Two concurrent nodes minting a `Seq` can interleave the get/set and collide or
skip an `emission_epoch`. Since `emission_epoch` is what makes a re-executed node
outrank the stale record it supersedes (resume-monotonic ordering), a corrupted
epoch corrupts resume.

**Fix (foundation, low-risk, behavior-preserving):** guard `bump` with a
`threading.Lock`. Different keys never collide logically; the lock only serializes
the dict mutation. This is correct regardless of whether fan-out ever ships, so it
lands first, on its own, with a concurrency test.

*Note:* keys are `f"{node_id}/{key}"`. Parallel lanes share the internal
`NODE_ID` (`senior_engineer`/`junior_engineer`) but differ by task/artifact key,
so logical collisions are unlikely — but the dict mutation itself still races
without the lock.

### 1b. Channel reducers must be order-independent

Under a parallel superstep, LangGraph merges concurrent channel writes by folding
them through the channel reducer. Each reducer must produce the SAME result
regardless of branch arrival order:

- `union_by_id` (tasks) — keyed union; **first-keyed-wins for dicts** (IM3). Order
  matters only if two branches write the SAME id — which lanes never do (disjoint
  ownership). **OK**, with a test asserting two disjoint-id writes commute.
- `merge_by_uri` (artifacts) — keyed by uri (content/idempotency address). Two
  branches produce disjoint uris (different tasks). **OK**; verify insertion-order
  independence for the fold result the reviewer's `max-by-seq` reads.
- `merge_gates` — admits only attested writes; the author agents write
  `gate_triage`, not `gates`, so no concurrent `gates` contention from D7 authors.
  **OK**.
- `union_journal` (agent_journal) — advisory, append-union. **OK**.

**Action:** add a reducer-commutativity test (feed writes in both orders, assert
equal folds) as part of the fan-out PR; do not assume.

### 1c. The junior "overlay" (shared evolving tree)

The SERIAL junior path lets each task build on predecessors' slices
(`junior_engineer.py` overlay). Parallel lanes lose that cross-lane visibility
mid-flight. **This is moot for D1**: lanes own disjoint files (ownership map), and
the integration boundary owns the authoritative merge — no lane needs to see
another lane's in-flight slice. Verify: no lane reads another lane's un-integrated
output. Within a lane, tasks stay serial (the overlay is intra-lane).

## 2. D7 — author-chain fan-out (lower risk, first real parallelism)

Quality authors (`reviewer`/`qa`/`sdet`) and assurance authors
(`appsec`/`audit`/`supply_chain`) are independent: each reads the same integrated
diff + criteria and writes its own artifact; none consumes another's output.

**Mechanism:** replace the serial `add_edge` chain with a fan-out from `START` to
all authors and a fan-in to the gate — exactly the pattern
`architecture.py:64-80,122-134` already uses for `domain_specialist` + `ux_design`
(the one existing intra-subgraph parallel superstep). LangGraph runs them in one
superstep; the reducers merge their `artifacts`/`gate_triage` writes.

**Why lower risk than D1:** no file mutation, no worktrees, no overlay — only
concurrent model calls writing to disjoint artifact kinds. Given §1a + §1b, this
is safe. **Pair with pacing** (§4).

**Caveat:** the quality `reviewer` writes to `gates` (its verdict) while running
alongside qa/sdet. `merge_gates` handles concurrent gate writes (attested-only),
and qa/sdet don't write `gates`, so no contention — but the fan-in must complete
all three before the gate boundary reads the review verdict (the barrier is the
gate node itself). Confirm the gate node is the single fan-in point.

## 3. D1 — lane fan-out (highest risk)

Convert `{fe,be,da}_lead` and `{fe,be,da}_engineer` from the serial
conditional chain to concurrent fan-out over lanes-with-work.

**Concerns beyond §1:**
- Each lane engineer runs the coding substrate (sandbox + `claude-code`). Parallel
  sandboxes are already supported (the junior pool runs tasks concurrently WITHIN
  a lane via `LocalWorkPool`); D1 extends that to ACROSS lanes at the graph level.
- `rt.seq`/`rt.cas` finalize must stay single-threaded per the existing pool
  design — the graph-level fan-out gives each lane node its own invocation, and
  each lane's finalize is already sequential within the node. Cross-lane, the
  `EpochCounter` lock (§1a) covers the shared counter.
- Resume: a mid-fan-out kill must resume deterministically. LangGraph checkpoints
  the superstep; each lane node is idempotent (content-addressed task ids). Verify
  a kill-then-resume mid-fan-out reproduces identical artifacts.

**Rollout:** land §1a (EpochCounter lock) + §1b tests, then D7, then D1. Get a
second opinion on D1 specifically before merging (concurrency + resume).

## 4. Pacing (mandatory with any fan-out)

Fan-out bursts concurrent requests → trips 429s faster on a subscription. Turn on
the proxy's existing client-side pacing (`_RateLimiter`,
`model_provider_proxy.py:570-609`): set `FOUNDRY_MAX_CONCURRENT_CALLS` to the
fan-out width (≈3 for lanes/authors) and `FOUNDRY_MAX_RPM` under the plan ceiling.
Without this, the wall-clock win becomes a throttle stall. **This is a pairing
requirement, not optional.**

## 5. B2 — cross-fix session continuity (coding agent only)

Reuse the coding session on a fix attempt instead of re-priming.

- **Mechanism:** `--resume <session_id>` (preferred over live bidirectional
  stream-json — a live process can't survive the graph's checkpoint pauses /
  hours-long rate-limit waits). `session_id` is already in the envelope
  (`cli_transports.py:366`).
- **Hard problem:** the sandbox is destroyed + reset-to-base per task, so the
  worktree + `session_id` must be kept alive across the fix loop and stored
  durably for graph resume. This conflicts with the idempotent-retry contract.
- **Scope:** coding agent ONLY. Never judges/reviewers (breaks verdict
  independence + temp-0 reproducibility) or single-shot agents.
- **Value:** latency + some token saving on fix re-priming; NOT a primary lever.
  Lowest priority of the three.

## 6. Rollout order & gates

1. **§1a EpochCounter lock** — foundation, behavior-preserving, ships alone with a
   concurrency test. *(Implementable now, low-risk.)*
2. **§1b reducer-commutativity tests** — assert the folds commute.
3. **D7 author fan-out** — first real parallelism, lower risk.
4. **D1 lane fan-out** — highest risk; second opinion before merge.
5. **B2** — last, separate; needs its own worktree/session-lifecycle design.

Every fan-out step ships WITH pacing (§4) enabled by default in the parallel
config, and WITH a kill-then-resume determinism test.
