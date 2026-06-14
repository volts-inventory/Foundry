# Build log — Foundry

Status of the actual build against [implementation-plan.md](./implementation-plan.md).
Build order followed: §8 (state → schemas → durability → trust seams → skeleton graph),
then the trust & execution core, gates, monitors, governance, and context layers.

**Suite: 247 passing, 1 skipped, ruff clean. Python 3.12.**

## Build wave 2 — trust core, gates, monitors, context (this iteration)

| Component | Module(s) | Plan ref | Status |
|---|---|---|---|
| Full agent-artifact schemas (product/impl/quality/security/ops/governance) | `foundry/schemas/*` | agents.md | ✅ tested |
| Model-provider proxy (single egress, lineage observation, judgment stamping, self-judge provenance) | `foundry/proxy/` | arch §2b, plan §3.3 | ✅ tested |
| Curated lineage-equivalence map (revisioned, fail-closed on unknown model) | `foundry/proxy/lineage.py` | arch §2b/§4 | ✅ tested |
| Sandbox + adapter contract + adapters (`claude -p`, raw-react) | `foundry/execution/` | arch §2, plan §3.2/§3.4 | ✅ tested |
| Two-zone gate runner + 8 deterministic gates (spec/build/test/e2e/security/audit/supply_chain) | `foundry/gates/` | gov §1c, plan §3.1/§5.1 | ✅ tested |
| Deterministic monitors + Guardian + advisory-analyzer seam | `foundry/monitors/` | gov §3, arch §7 | ✅ tested |
| Budget controller (superstep-boundary caps, convergence bound) + HITL liaison | `foundry/governance/` | arch §6, plan §0.2 | ✅ tested |
| Context assembly (pinned constraints, retriever, determinism policy) | `foundry/context/` | arch §8 | ✅ tested |

**Provider-accuracy notes (caught against the live Anthropic API surface, not memory):**
the tier map is `claude-opus-4-8`/`claude-sonnet-4-6`/`claude-haiku-4-5`; `temperature` is
**removed on opus-4-8** (400 if sent), so the "temperature 0 for gates/judges" policy
applies only to T2/T3 — on T1 the proxy/determinism layer records `output_config.effort`
instead and notes the substitution in the manifest. There is no `system_fingerprint`;
served-model observation is `response.model` + `_request_id`. The judgment verdict channel
is `output_config.format` (json_schema), and the stable SOP prefix is `cache_control`-cached.

**Resume note:** wave 2 ran as 6 parallel agents; 3 hit the session limit mid-task but
their files had already landed and passed. The gates agent stopped before `audit.py`,
`supply_chain.py`, and `tests/test_gate_runner.py` — these were completed by hand in the
same style (zone-A/zone-B split, fail-closed ERROR, cross-fork stable-FAIL latch), and a
cross-layer smoke test confirms a runner-signed verdict and a proxy-stamped judgment are
both admitted by the same `merge_gates` under one verifier.

---

## Build wave 1 — Phase 0: the state backbone

## Built (this iteration)

| Component | Module(s) | Plan ref | Status |
|---|---|---|---|
| Canonical serialization + content addressing | `foundry/canonical.py` | arch §3–4 | ✅ tested |
| `Seq` total order + epoch counter | `foundry/state/seq.py` | arch §3 | ✅ tested |
| Channel reducers (commutative, None-safe, fail-closed) | `foundry/state/reducers.py` | arch §3 | ✅ property-tested |
| `FactoryState` typed channels | `foundry/state/factory_state.py` | arch §3 | ✅ |
| Global cross-fork records (nonce ledger, latches, alpha ledger) | `foundry/state/global_records.py` | arch §3/§5, gov §5 | ✅ tested |
| `tree_sha` / `state_sha` / execution reuse key | `foundry/state/tree_sha.py` | arch §4 | ✅ tested |
| Schemas: base records, gates, pinned constraints | `foundry/schemas/` | agents.md, gov §1c | ✅ |
| Dev-grade zone-B attestation + judgment binding | `foundry/gates/attestation.py` | gov §1c, plan §0.3 | ✅ tested |
| Transactional outbox + WORM audit chain + relay | `foundry/durability/outbox.py` | arch §5 | ✅ tested |
| Shared-connection checkpointer (one txn/superstep) | `foundry/durability/checkpointer.py`, `serde.py` | arch §5 | ✅ tested |
| Phase 0 skeleton graph (intake → work → emit) | `foundry/graph/skeleton.py` | plan §7 Phase 0 | ✅ tested |

**Phase 0 exit criteria** (plan §7): a trivial task runs end-to-end and is
checkpointed ✅; kill-then-resume produces no duplicate external action ✅;
reducer-purity + idempotency property tests pass ✅.

Trust posture per plan §0.3: attestation/binding logic is real and correct;
the signing root is dev-grade HMAC (labelled as such in the module), not a
hardware/OIDC root — that is an ops deployment task by design.

## Inefficiencies caught (design-doc pseudocode → implementation)

The user-facing mandate for this build was to catch inefficiencies. Found and
fixed, with the semantics preserved exactly:

1. **Signature re-verification on every reducer fold.** The `_merge_gates`
   pseudocode in architecture.md §3 calls `verify_attestation` /
   `verify_judgment_binding` on every record at every fold — O(records ×
   supersteps) HMAC/signature checks over *immutable* records. Signature
   validity is pure in (record content, key, map revision), so the verifier
   memoizes by `content_identity()` in a bounded cache. The **nonce-consumed
   and negative-latch reads are deliberately NOT cached** — they are the
   mutable committed inputs that make the reducer latch-aware
   (`gates/attestation.py` documents the split; the grounding test asserts it).
2. **Intermediate list materialization in reducers.** The pseudocode's
   `(a or []) + (b or [])` allocates a concatenated copy per fold per channel;
   reducers iterate both sides directly (`reducers._iter`).
3. **Full-tree re-hash on every `tree_sha`.** Hashing every byte of a large,
   mostly-unchanged tree per verdict emission is the hot path of
   dependency-scoped re-verification. `tree_sha.py` takes an optional blob
   cache keyed on `(path, size, mtime_ns)` — a *pure* optimization: cold and
   warm caches produce identical digests, and the cache key never leaks into
   the digest (tested both ways).
4. **Seed-randomized `hash()` for idempotency keys.** The first skeleton draft
   derived an artifact URI from Python's `hash()`, which is randomized per
   process — a kill-then-resume in a fresh process would mint a *different*
   idempotency key and defeat outbox dedup (the exact dual-write bug class
   arch §5 exists to prevent). Replaced with `content_digest`; a regression
   test pins process-stability.
5. **Outbox polling.** The relay drains in batches with one indexed SELECT
   per drain pass (status index), not per-row polling.
6. **Checkpoint bloat.** All channel records are content-addressed *handles*
   (DS9); bodies never enter state. The serde allowlists exactly Foundry's
   frozen models instead of pickle-fallback.

## Bugs found *by* the Phase 0 gate (property tests doing their job)

The plan calls the reducer property tests "load-bearing"; they earned it on
day one:

1. **`merge_constraints` was not associative / re-fold idempotent.** The
   architecture pseudocode's "merge by id, then drop superseded" shape is
   destructive at intermediate folds: a record retired in one fold takes its
   `supersedes` edge with it, so a later fold can *resurrect* what it had
   retired — and a LangGraph resume re-fold could change committed state.
   Hypothesis produced the minimal counterexample (a 3-link supersession
   chain folded in different groupings). Fix: the reducer is now a **pure
   keyed union that retains superseded records as data**, and supersession
   is a deterministic **read-time view** (`live_constraints()`), restoring
   the full algebra (38 property tests pass).
2. **Judgment-binding cache key was unsound as spec'd.** `content_identity()`
   excludes `seq`, but the binding signs `seq.node_id` — two content-identical
   records under different node ids would collide in the verifier's memo
   cache with different true validity. The binding cache is keyed on
   `(content_identity, seq.node_id)`.
3. **`SqliteSaver.put()` self-commits**, which would have committed the
   checkpoint *without* its outbox rows mid-superstep — precisely the DS3
   dual-write divergence. The shared connection's `commit()` is a no-op
   while `superstep_txn()` is open (rollback is never suppressed); one real
   COMMIT happens at superstep exit. A test forces an exception between the
   checkpoint put and the enqueue and asserts neither persists.

## Deviations from the plan (recorded, not hidden)

- `PinnedConstraint` lives in `schemas/architecture.py` per the plan's layout,
  but the rest of the architecture-layer models (SystemDesign, ADR, …) are
  deferred to Phase 1 with the agents that produce them.
- `ExternalAction` lives in `schemas/base.py` (the state channel needs it);
  the plan's layout had no explicit home for it.
- Reducer↔verifier wiring uses a `ContextVar` set per superstep rather than
  module-level globals, with a **fail-closed default**: no configured verifier
  ⇒ every deterministic-gate/judgment write is dropped, never trusted.
- Python 3.12 (plan pins 3.12; container default was 3.11 — uv-managed 3.12
  venv).

## Next (build order §8)

4. `proxy/` — model-provider proxy (single egress, lineage observation,
   judgment-binding stamping, self-judge provenance stamp).
5. `execution/` — sandbox + adapter contract (+ `claude -p` adapter).
6. `gates/runner.py` — two-zone runner wiring attestation to real gate execs.
7. `governance/budget_controller.py` + deterministic monitors.
