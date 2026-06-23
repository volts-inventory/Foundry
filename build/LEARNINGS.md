# LEARNINGS — append-only compounding knowledge

**Read this FIRST before any task.** Every agent appends reusable lessons here.
Keep entries terse, root-caused, and actionable. Periodically consolidated.

## Conventions every agent must follow
- L1. `from __future__ import annotations` at the top of every module; full type hints.
- L2. Run the gate chain locally before reporting done: `uv run ruff check foundry tests`
  → `uv run mypy foundry` → `uv run pytest -q`. Production code (`foundry/`) must pass mypy
  strictly; test modules are exempt from the strict type bar.
- L3. Every agent I/O artifact is a **frozen pydantic v2 model** in the matching
  `foundry/schemas/*` module. Reuse existing base models (`FrozenModel`, records). Do not
  invent a parallel artifact type.
- L4. Artifacts are **content-addressed + versioned, never mutated**. Supersession, not
  in-place edit. Bodies are stored out-of-band as handles; never put bodies in state channels.
- L5. **Agents emit `GateTriage` (advisory), never `GateResult` (verdict).** Verdicts are
  computed + signed by the two-zone gate runner. Writing an unsigned `GateResult` to the
  `gates` channel is dropped by `_merge_gates` (fail-closed) — by design.
- L6. **All model calls go through `foundry/proxy/`** (single egress). Never import/call the
  Anthropic SDK directly from an agent or gate. The proxy observes the served model, stamps
  judgment provenance, and applies the determinism policy.
- L7. Idempotency keys must be **process-stable** — use `content_digest`, never Python's
  builtin `hash()` (seed-randomized per process; defeats outbox dedup on resume).
- L8. Reducers must be pure / commutative / associative / None-safe and **read** global
  cross-fork records (never write them). A LangGraph resume re-folds committed writes, so any
  non-idempotent fold is a bug the property tests will catch.

## Inefficiencies already caught (don't reintroduce — build-log.md)
- I1. No signature re-verification per reducer fold — memoize by content identity; nonce/latch
  reads are the only un-cached (mutable) inputs.
- I2. No intermediate list materialization in reducers (`(a or []) + (b or [])`); iterate both.
- I3. No full-tree re-hash per `tree_sha`; use the blob cache keyed `(path, size, mtime_ns)`.
- I4. Outbox relay drains in batches with one indexed SELECT per pass — never per-row polling.
- I5. Checkpoint carries content-addressed handles only; serde allowlists frozen Foundry models
  (no pickle fallback).

## Bugs the Phase-0 property tests caught (the gate earns its keep)
- B1. `merge_constraints` must be a pure keyed union retaining superseded records as data;
  supersession is a read-time view (`live_constraints()`), else resume re-folds resurrect data.
- B2. Judgment-binding memo cache key = `(content_identity, seq.node_id)` — content_identity
  excludes seq but the binding signs node_id; collision otherwise.
- B3. The shared checkpointer connection's `commit()` is a no-op while `superstep_txn()` is
  open (one COMMIT at superstep exit) — else the checkpoint persists without its outbox rows.

## Schema authoring
- S1. A defaulted mutable field on a `FrozenModel` (`x: dict[...] = {}` / `= []`) trips ruff
  RUF012 even though pydantic v2 deep-copies it safely. Use `Field(default_factory=dict)` /
  `default_factory=tuple`. Required dict fields (no default) don't trip it — that's why most
  schema dicts are fine. (T-M1-02: `E2EReport.scenario_criteria`.)

## Provider-accuracy notes (verified against the live Anthropic surface)
- P1. Tier map: `claude-opus-4-8` (T1) / `claude-sonnet-4-6` (T2) / `claude-haiku-4-5` (T3).
- P2. `temperature` is **removed on opus-4-8** (400 if sent) — the "temp 0 for gates/judges"
  policy applies only to T2/T3; on T1 record `output_config.effort` and note the substitution.
- P3. No `system_fingerprint`; served-model observation = `response.model` + `_request_id`.
- P4. Judgment verdict channel = `output_config.format` (json_schema); stable SOP prefix is
  `cache_control`-cached.

## Test doubles — the seam every later wave injects (T-M1-03)
- D1. To run the graph/CLI with NO live API key, inject `MockTransport`
  (`foundry/proxy/test_doubles.py`) into `ModelProviderProxy(transport=..., signer=ZoneBSigner(key,key_id))`.
  It is a real `ProviderTransport`: deterministic `ProviderResponse`, `served_model`
  defaults to a real `TIER_MODELS` value (`LineageMap` resolves it to `anthropic-claude`),
  stable `request_id` via `content_digest` (L7, never `hash()`), `DEFAULT_USAGE`, `stop_reason="end_turn"`.
- D5. (R1-02) `MockTransport.served_model` now defaults to the **requested tier's own model**
  (mirrors `request["model"]`), so a T2/T3 `complete()` no longer spuriously trips pinned-ID
  drift. Pass the optional `served_model=` ctor kwarg (or `ResponseScript.served_model`) to
  FORCE a drift scenario. `ProviderTransport` is a plain (non-runtime_checkable) Protocol, so
  structural conformance tests assert against a local runtime_checkable mirror of `send`.
- D6. (R1-01/R1-03; narrowed by DT1) On the constrained VERDICT channel the verdict
  queue/`default_verdict` take PRIORITY over keyed scripts, so `judge()` can never crash in
  `json.loads` on free-text that matched a script; scripts/`completions=` still win on every other
  channel — INCLUDING schema-constrained ideation. The verdict channel is now detected by SCHEMA
  STRUCTURE, not by the mere presence of `output_config.format.type == "json_schema"` (DT1/AC3):
  the schema must BE `VERDICT_SCHEMA` (`_is_verdict_schema`). Verdict-queue literal-vs-JSON detection
  is whitespace-tolerant (`json.loads` probe, not `startswith("{")`).
- D2. Scripting API (priority order): `MockTransport(responses=[ResponseScript(text, match)])`
  first-match-wins; `MockTransport.scripted({needle: text})` keys by substring of system SOP/messages;
  `verdicts=[...]` / `completions=[...]` are FIFO queues for the constrained-json-schema channel vs
  free-text channel; `default_verdict=`/`default_text=` are the per-channel fallbacks. Verdict items
  may be `"approve"`/`"request-changes"` or pre-serialized JSON; use `verdict_json(status, findings)`.
  Verdict channel is auto-detected by SCHEMA STRUCTURE — the request schema must BE `VERDICT_SCHEMA`
  (DT1/AC3), NOT merely carry a json_schema format. So a schema-constrained ideation payload rides
  `completions=`/`scripted`/`default_text` (returned verbatim as `text`), not `verdicts=`.
- D3. To run the substrate with NO `claude -p` binary, use `StubAdapter`
  (`foundry/execution/adapters/stub.py`): `supports_turn_snapshots=True`, holds no egress.
  Ctor kwargs: `run_id`, `node_id`, `output_path`, `triage_note`, `self_reported_model`.
  `run(packet, sandbox)` writes a deterministic per-task file into the worktree, returns
  `AdapterResult(diff_uri=cas://..., triage=GateTriage-only (L5), turns_used, snapshot_digest)`.
  Determinism: same task body -> same worktree -> same `snapshot_digest` (timestamp/perm-free).
- D4. judge() requires the injected `ZoneBSigner`; verify bindings with `DevGateWriteVerifier`
  and admit via `merge_gates` under `set_gate_write_verifier(...)` (reset the contextvar token in teardown).

## Agent runtime — the shared seam all four Phase-1 waves build against (T-M1-04a)
- AR1. `from foundry.agents import AgentRuntime, NodeFn` (also `run_id_of`). `AgentRuntime`
  is a `@dataclass`: fields `proxy: ModelProviderProxy`, `epochs: EpochCounter = field(default_factory=...)`,
  `adapter: WorkerAdapter | None = None`. Inject one `rt` per run; thread it into every node factory.
- AR2. `rt.seq(config, node_id, key, local=0) -> Seq` reproduces skeleton.py's `_seq` EXACTLY:
  run_id = `config["configurable"]["thread_id"]`, emission_epoch = `epochs.bump(run_id, f"{node_id}/{key}")`
  (resume-monotonic), superstep from `config["metadata"]["langgraph_step"]`. `key` names the
  artifact/action emitted, so distinct emissions get independent epoch streams.
- AR3. Node-factory convention: `def make_<agent>_node(rt: AgentRuntime, ...) -> NodeFn:` returning a
  closure `(state: FactoryState, config: RunnableConfig) -> dict[str, Any]` of channel writes (never
  mutate state). Drops straight into `StateGraph(FactoryState)`. `NodeFn` = that callable alias.
- AR4. Agents call `rt.proxy.complete(...)`/`rt.proxy.judge(...)` (L6 single egress); implementation
  agents use `rt.adapter`. Agents NEVER import the Anthropic SDK and NEVER write a `GateResult` to
  `gates` — emit `GateTriage` to `gate_triage` instead (L5). `_runtime.py` holds NO agent logic and is
  import-cheap (TYPE_CHECKING-only heavy imports).

## Architecture layer (T-M1-05)
- AC1. `make_principal_architect_node(rt, index=None)` (T1) retrieves spans through the
  `SymbolIndex` (`foundry.context`, impact-analysis contract), NEVER ad-hoc file reads; `index=None`
  is greenfield (empty retrieval), distinct from a retrieval FAILURE (`retrieve_or_need_context`
  raises `NeedContext`). It emits SystemDesign/ADR/InterfaceContract/OwnershipMap as
  content-addressed `Artifact` handles on `artifacts`, and `PinnedConstraint[]` on the SEPARATE
  `pinned_constraints` channel (reducer `merge_constraints`) — constraints are NOT artifacts; they
  flow downstream verbatim into adapter task packets. Each constraint/ADR/interface gets a distinct
  `seq` via `seq._replace(local=i)` off one per-key `Seq` (so records within a key stay distinct).
- AC2. Conditional Layer-2 nodes (`make_domain_specialist_node` / `make_ux_design_node`) gate on
  `should_enter_domain_specialist(state)` / `should_enter_ux_design(state)` — the SAME predicate the
  node's internal guard uses, so the subgraph's conditional edge and the node never disagree. On
  no-match they return `{}` (no writes); downstream tolerates absence.
- AC3. **RESOLVED (Wave-B DT1).** Was: ANY `complete(..., schema=...)` attaches
  `output_config.format.type == "json_schema"`, which MockTransport treated as the verdict channel,
  SHADOWING a scripted structured ideation payload (PRD/SystemDesign/TestPlan/decomposition) under
  the default `approve` verdict — the workaround was to smuggle the payload through the `verdicts=`
  FIFO. **New rule (DT1):** MockTransport routes to the verdict channel only when the json_schema
  format is present AND the schema IS the verdict schema — detected by STRUCTURE
  (`properties.status.enum == ["approve","request-changes"]`, or `required ⊇ {status, findings}`;
  see `test_doubles._is_verdict_schema`, matching `VERDICT_SCHEMA`). `judge()` passes `VERDICT_SCHEMA`
  so it still hits the verdict path; a schema-constrained *ideation* call does NOT — script its
  payload on the natural free-text path (`completions=` FIFO / `scripted` / `default_text`), returned
  verbatim as `text`. The `verdicts=` smuggling workaround is no longer needed and was removed from
  the architecture/product/implementation agent tests.

## Implementation layer (T-M1-06)
- IM1. **Idempotent-retry base snapshot needs a DURABLE store (cost a debug pass).** The Junior
  (`make_junior_engineer_node`) builds a FRESH `Sandbox` per node invocation (arch §2(d): no reuse).
  A `Sandbox`'s default `snapshot_store` is ephemeral and dies with `destroy()`, so the base
  `snapshot()` digest recorded on run 1 CANNOT be `restore()`d by run 2's new sandbox — you get
  `FileNotFoundError: no recorded snapshot`. Fix: thread a `snapshot_store=` path into the factory
  and pass the SAME path on both the first run and the retry. Only then does L4 supersession-by-reset
  work across the destroy/rebuild. The idempotency key itself (`idempotency_key`, content_digest over
  task id + branch + verbatim pinned texts + base_tree_sha; L7) is process-stable regardless.
- IM2. Pinned constraints reach the `TaskPacket` via `pinned_texts(live_constraints(...))` — read the
  LIVE (non-superseded) view, order by `id` (fold-order-independent so the packet is byte-identical
  across resume), drop straight into `TaskPacket.pinned_constraints`; `assemble_prompt` then renders
  them FIRST + VERBATIM ahead of the TASK body. NB (AC3 resolved, DT1): the Senior's `complete(...,
  schema=_DECOMPOSITION_SCHEMA, purpose="ideation")` is NOT the verdict channel anymore — script the
  decomposition JSON on the natural `completions=[json.dumps(payload)]` path (it is returned verbatim
  as `text`), not `verdicts=`.
- IM3. The `tasks` channel reducer (`union_by_id`) keys plain dicts by `id` with NO `.seq` tiebreak
  (dicts aren't models), so FIRST-keyed wins. Senior emits a Task dict and Junior emits a done-marker
  dict for the same id — in one graph run the Senior's wins. That's fine here (nodes tested in
  isolation); a real integration loop must supersede via a fresh id/version, not rely on later-wins.
  Corollary (T-M1-10): in the WIRED subgraph the Senior's plain Task dict therefore survives on
  `tasks`, so the produced `diff_uri`/`base_tree_sha` do NOT appear on the `tasks` channel — the
  FeatureDiff lives on `artifacts` (assert the diff off `artifacts`, not off the done-marker).

## Implementation layer normalization + subgraph (T-M1-10)
- IM4. **Node DATA inputs come from `state`; only infra deps/config are factory args (AR1-AR4).** Both
  implementation factories now follow it: `make_senior_engineer_node(rt, *, resolve, tier="T1")` reads
  the SystemDesign+OwnershipMap off `state["artifacts"]` (found by `Artifact.kind` =
  `DESIGN_KIND`/`OWNERSHIP_KIND`, newest-by-`seq`) and reconstitutes the bodies via the injected
  `resolve` (INFRA: CAS in prod, an in-memory `{uri: body}` map in tests — the architecture layer
  emits HANDLES, not bodies, DS9). `make_junior_engineer_node(rt, *, source_dir, snapshot_store=None,
  turn_budget=…, cost_budget_microusd=…)` dropped its old `task=`/`base_snapshot=` build-time kwargs;
  it reads the ready Task set off `state["tasks"]` (`ready_tasks`: reconstitute each dict — strip the
  done-marker's extra `diff_uri`/`base_tree_sha` keys first since `FrozenModel` is `extra="forbid"` —
  ordered by `seq`) and pinned constraints off `state["pinned_constraints"]`, processing the WHOLE
  ready set in one node (branch-per-task preserved by the per-task `task/<id>` branch + per-task
  fresh `Sandbox` + per-task FeatureDiff). The retry base is the recorded `base_tree_sha` read off the
  re-presented done-marker (not a build-time arg) — supersession via state, not config.
- IM5. **`StateGraph.add_node` won't bind its `_NodeWithConfig` Protocol through a `Callable` alias.**
  A factory closure typed as `NodeFn`/`_Node = Callable[[FactoryState, RunnableConfig], dict]` trips
  `add_node` overload resolution (mypy can't unify the contravariant `NodeInputT` TypeVar through an
  opaque alias). Fix WITHOUT a private import or `cast`: wrap each node in a thin named concrete `def
  _senior(state: FactoryState, config: RunnableConfig) -> dict[str, Any]: return senior(state, config)`
  and add THAT (same trick the architecture subgraph uses; product.py instead `cast`s to `StateNode`).
- IM6. **A nested `snapshot_store` inside `source_dir` corrupts the base snapshot (test gotcha).** If
  the durable store path lives UNDER the cloned source tree, the second sandbox's `copytree(source)`
  pulls the first run's `store/` into the worktree, so `normalized_source_tree` (covers on-disk state,
  `prefer_git=False`) yields a DIFFERENT base digest run-to-run and the idempotency key drifts. Keep
  `snapshot_store` in a SIBLING dir of `source_dir` (e.g. `tmp/src` + `tmp/store`).

## Product layer (T-M1-04)
- PR1. The four Layer-1 factories live in `foundry/agents/product/`:
  `make_stakeholder_proxy_node`→IntentBrief (T2), `make_product_manager_node`→PRD +
  AcceptanceCriterion[] (T2), `make_criteria_completeness_critic_node`→CriteriaCritique +
  spec-gate GateTriage (T1), `make_eng_program_mgr_node`→SprintPlan (T2). All call
  `rt.proxy.complete(..., purpose="ideation", schema=...)` — product output is exploratory
  authoring, NOT a verdict, so even the critic uses `complete` (its CriteriaCritique is an
  artifact + advisory triage), never `proxy.judge()` (that path mints a signed GateResult, L5).
- PR2. **Seq/uri rebinding pattern** (the node-authoring gotcha). The typed product models
  (IntentBrief/PRD/AcceptanceCriterion/CriteriaCritique/SprintPlan) are NOT `Artifact`s and carry
  no `.uri`; the `artifacts` channel is `list[Artifact]` folded by `merge_by_uri`. So a node owns
  the `seq` and any `*_uri`/CAS body handle, NOT the model: drop them from the constrained schema
  (`_common.content_schema`, or omit from a hand-written schema), build the model with the node-minted
  `seq`, mint body handles via `_common.cas_uri(body)`, then wrap the typed body as a
  content-addressed `Artifact` via `_common.emit_artifact(model, kind, produced_by, seq)` whose
  `uri = cas://sha256/<model.content_identity()>` so resume re-emissions coalesce under `merge_by_uri`.
  Multiple records under one key (e.g. AcceptanceCriterion[]) each get `seq._replace(local=i)`.
- PR3. CriteriaCritique requires a `judgment_provenance` field; the MODEL never supplies it — stamp it
  from the proxy profile (`rt.proxy.profile == "self_judge"` → `JudgmentProvenance.SELF`, else
  INDEPENDENT). The critic emits `GateTriage(name="spec", ...)` whose explanation is marked BLOCKING
  iff `critique.spec_gate_blocking()` (any `security_abuse_omissions`); the Spec-gate runner still owns
  the verdict bit (L5). MockTransport gotcha for these schema-bearing `complete` calls is AC3 — feed
  payloads via the `verdicts=` FIFO queue, sized to the number of `complete` calls per test.

## Quality layer (T-M1-07) — the judge/nonce wiring later waves reuse
- Q1. **The verdict_nonce is a RUN INPUT, not agent-minted.** `make_reviewer_critic_node(rt)`
  reads it off `config["configurable"]["verdict_nonce"]` (helper `quality._common.server_issued_nonce`,
  fail-closed: raises `MissingVerdictNonceError` if absent). The orchestrator/runner issues it from
  the cross-fork nonce ledger (`global_records.py`) and threads it onto the run config exactly like
  `thread_id`/`state_sha`/`subject_artifact_uri`. An agent NEVER mints one — a verdict with no nonce
  is dropped (`nonce_consumed_by_other_verdict` fail-closes on empty).
- Q2. **judge() output is the ONLY thing an agent writes to `gates`, and it is admitted only because
  the proxy (not the agent) stamped it.** Wire `seq = rt.seq(config, "<node>", "verdict:...")` with
  `seq.node_id == node_id` (the binding signs seq.node_id; `judge()` raises on mismatch — see B2).
  `rt.proxy.judge(node_id, subject_uri, state_sha, nonce, sop, msgs, seq=seq, gate_name="review")`
  returns a `JudgmentResult.gate_result` already carrying a ZoneBSigner-stamped binding +
  `judgment_provenance`. Write `{"gates": [result.gate_result]}`. `_merge_gates` admits it iff:
  valid binding (signed status+nonce+state_sha+served_family+node_id) AND nonce not consumed by a
  DIFFERENT content_identity AND no negative latch on `(node_id, subject_uri, run_against)`. This is
  how L5 is honored while still touching `gates`: the agent emits a verdict it could never sign.
- Q3. **Negative latch is terminal until released — including for the request-changes verdict itself.**
  Once the runner calls `records.latch_negative_verdict(rc.latch_key())`, re-folding even `rc` returns
  `[]` (the verdict that established the latch lives in committed records, not re-admitted); a
  higher-seq approve on the same `latch_key` is also dropped. Only `release_negative_verdict(key,
  fresh_nonce)` + a fresh re-review re-admits an approve. Test the override-block with
  `merge_gates([approve],None)==[]`, NOT `==[rc]`.
- Q4. **qa_engineer/sdet are pure `rt.proxy.complete` authors — never touch `gates` (L5).** Pass
  `criteria: tuple[AcceptanceCriterion,...]` (resolved from CAS by the orchestrator) as a factory
  kwarg: the CRITERIA SET is the source of truth for coverage, the model only enriches traceability.
  qa fail-closes uncovered criteria to a synthetic `test::<cid>` so the Test gate's coverage check is
  never starved; it binds one NEGATIVE `OracleBinding` per HIGH-RISK criterion (fidelity != coverage).
  Bodies (`TestPlan`/`TestReport`/`OracleBinding`/`E2EReport`) ride `artifacts` as content-addressed
  handles via `quality._common.emit_artifact` (DS9/L4) — NOT inline; they carry no `.uri` so they are
  not `Artifact`s. NB: a typed body's `content_identity()` INCLUDES `seq`, so two emissions under
  different epochs get different handle uris — don't assert cross-epoch handle equality; the resume
  idempotency that `merge_by_uri` gives is re-folding the SAME handles, not re-emitting.

## Orchestration / tooling
- O1. mypy gate is `mypy foundry` (config in pyproject; `ignore_missing_imports=true`).
  Don't add `# type: ignore` unless needed — `warn_unused_ignores` flags dead ignores.
- O2. `make_union_by_key` is a generic *function*; mypy rejects `f[T](...)` type-application on
  functions. Type the key callable via a named function with annotated params, not a lambda.
- O3. pytest collects production classes named `Test_*` only (pyproject) — domain types like
  `TestPlan`/`TestReport` are NOT test cases. Name real test classes `Test_*` or use functions.
- O3a. **The O3 underscore rule SILENTLY EATS plain `class TestX:` test classes (T-FIX).**
  `tests/test_monitors.py` had 7 plain `TestX` classes (38 test methods) that collected to ZERO and
  had NEVER run since written — pytest reports the file as passing because it sees no cases, not green
  cases. Fix: rename `class TestX:` → `class Test_X:`. AUDIT: grep `^class Test[A-Z]` across `tests/`
  after authoring; any hit (no underscore) is a dead suite. Reassuringly, once collected all 38 passed
  against the EVOLVED monitors/governance code — no stale assertions, no genuine bug surfaced (the
  monitor/governance contracts held). Watch for transient `ERROR collecting` during a full `pytest`
  run: a CONCURRENT background agent committing/mutating the tree mid-collection causes it, not the
  suite — re-run in isolation to confirm (it cleared to 962 passed / 1 skipped).
- O4. No CI of any kind (no `.github/workflows`). All verification is local via the gate chain.

## Subgraph composition (T-M1-08, the per-layer `build_<layer>_subgraph` waves)
- SG1. **`StateGraph.add_node` rejects the `NodeFn` alias under mypy — cast to the public
  `StateNode`.** A `make_<agent>_node` factory returns `NodeFn` = a bare
  `Callable[[FactoryState, RunnableConfig], dict]` whose params are POSITIONAL-ONLY, so mypy
  can't unify it with `add_node`'s `_NodeWithConfig[NodeInputT]` overload and reports
  `No overload variant ... matches` (an INLINE `def node(state, config)` matches structurally
  and passes — that's why `skeleton.py` is clean but a factory-fed graph is not). Fix without a
  bare `# type: ignore`: `cast("StateNode[FactoryState, Any]", fn)` (the public type `add_node`'s
  own signature declares; `from langgraph.graph.state import StateNode` under TYPE_CHECKING).
  Passing `input_schema=FactoryState` does NOT fix it. NB: `subgraphs/architecture.py` (Wave-B)
  used a local `_Node` alias annotation and still trips this — the cast-to-`StateNode` is the one
  that actually clears mypy.
- SG2. **Builder returns the UNCOMPILED `StateGraph`; channel writes accumulate via FactoryState's
  reducers, not the builder.** Wire `START -> ... -> END` linearly and `return g` (no `.compile`,
  no checkpointer — the caller/supervisor owns that). Across the linear chain each node's deltas
  fold through the per-channel reducers (`artifacts`->`merge_by_uri`, `gate_triage`->`union_triage`,
  `required_capabilities`->`union`), so a standalone `build_product_subgraph(rt).compile().invoke(
  {"request": ...}, {"configurable": {"thread_id": ...}, "metadata": {"langgraph_step": n}})` yields
  the full PRD + AcceptanceCriterion[] + CriteriaCritique set on `artifacts` and the `spec`
  GateTriage — with NO `gates` write (L5; verdicts come from the runner). `thread_id` is mandatory
  in config (Seq/run_id, AR2).

## Subgraph composition (T-M1-08/09)
- SG1. **Conditional fan-out routes on a LIST, keyed by the agents' OWN `should_enter_*` predicates.**
  `build_architecture_subgraph` wires `principal_architect` as the entry, then
  `g.add_conditional_edges(ARCHITECT, _route, {DOMAIN: DOMAIN, UX: UX, END: END})`. The router
  returns a LIST of branch targets (`[DOMAIN, UX]` for a regulated UI project) so LangGraph fans
  out to BOTH conditional nodes in one superstep; the per-channel reducers (`merge_by_uri` /
  `merge_constraints`) merge the fan-in. Empty caps -> router returns `[END]` and the conditional
  nodes are NEVER scheduled (their internal no-op guard is then moot — defense in depth, not the
  primary skip). Branch on the SAME `should_enter_domain_specialist` / `should_enter_ux_design`
  predicates the nodes guard on (AC2) so edge and node never disagree. The path map MUST list every
  legal return value (node ids + `END`) or compile rejects the route.
- SG2. **mypy + `StateGraph.add_node`: a `Callable` ALIAS-typed node fails overload resolution; a
  concrete `def` passes.** `add_node` matches its node arg against the `_NodeWithConfig`
  `Protocol[NodeInputT_contra]`; mypy can solve that contravariant TypeVar against a NAMED `def`'s
  signature (why `skeleton.py`, which defines nodes inline, is clean) but NOT through an opaque
  `Callable[[FactoryState, RunnableConfig], dict[str, Any]]` alias (which `NodeFn` and the factory
  returns are) — you get 4x `No overload variant of "add_node" ... matches [call-overload]`. Fix
  WITHOUT a private import or `cast`: wrap each factory closure in a thin concrete local `def
  _x(state, config) -> dict[str, Any]: return node(state, config)` and add THAT. Full type-checking
  preserved. (Annotating the var as `StateGraph[FactoryState]` vs inferring it is NOT the trigger —
  both pass for a concrete def; the alias is.)
- SG3. **Docstring ASCII-art topology with `\--` trips `SyntaxWarning: invalid escape sequence`** at
  import time (pytest surfaces it). Make the module docstring a raw string (`r"""..."""`).
- SG4. Subgraph builders return the **UNCOMPILED** `StateGraph` (the supervisor owns compile +
  the shared checkpointer; mirrors `skeleton.py`'s split). Tests compile standalone
  (`build_*_subgraph(rt).compile()`), thread `thread_id` in `config["configurable"]`, and script
  the architect's structured design payload on the FREE-TEXT `completions=` FIFO (schema-constrained
  ideation is NOT the verdict channel — AC3/DT1), sized to the number of `complete` calls.

## Quality subgraph + qa/sdet state-reading normalization (T-M1-11)
- QS1. **qa/sdet are now `make_qa_engineer_node(rt, *, resolve=None, run_against, tier)` /
  `make_sdet_node(rt, *, resolve=None, run_against, tier)` — criteria are a DATA input read from
  `state` (AR1-AR4), never a build-time kwarg.** Reviewer/Critic stays `make_reviewer_critic_node(rt)`.
- QS2. **A RAW typed body CANNOT ride the `artifacts` channel through a fan-in fold — `merge_by_uri`
  does `m[x.uri] = x` and `AcceptanceCriterion` (a `FrozenModel`) has NO `.uri`.** So seeding raw
  criteria into `artifacts` and then having ANY node write `artifacts` crashes with
  `AttributeError: 'AcceptanceCriterion' object has no attribute 'uri'` IN A COMPILED GRAPH (it only
  "works" in direct-call unit tests where no reducer runs). The sanctioned `quality._common.
  criteria_from_state` (reads raw instances by `isinstance`) is therefore graph-UNSAFE on its own.
- QS3. **Fix = the Senior Engineer's seam: read content-addressed HANDLES + an injected `resolve`.**
  The product layer emits each criterion as an `Artifact(kind="acceptance_criterion", uri=...)`
  HANDLE (has `.uri`, survives `merge_by_uri`). qa/sdet read criteria via a new
  `qa_engineer.read_criteria(state, resolve)` that UNIONS (deduped+ordered by `id`): raw
  `AcceptanceCriterion` from `criteria_from_state` (for direct-call unit tests) PLUS
  `acceptance_criterion` handles reconstituted through `resolve` (INFRA — CAS in prod, in-memory map
  in tests; `ArtifactResolver = Callable[[Artifact], FrozenModel]`, exported from `qa_engineer`).
  `resolve` is INFRA so it is a legit factory arg; the DATA (WHICH criteria) still comes from state.
  `build_quality_subgraph(rt, *, resolve)` threads it into both author nodes — mirrors
  `build_implementation_subgraph`'s `resolve=`.
- QS6. **(C-R1-01) `resolve` is REQUIRED on `build_quality_subgraph` — NOT `resolve=None`.** In the
  chained factory_graph the product layer emits AcceptanceCriterion ONLY as `acceptance_criterion`
  Artifact HANDLES, so a defaulted `resolve=None` reads ZERO criteria → empty `TestPlan`, no oracles,
  no error → a VACUOUS Test-gate pass. The signature now requires it (mirrors
  `build_implementation_subgraph`). DEFENSE IN DEPTH: `qa_engineer.read_criteria` (used by qa AND
  sdet) is FAIL-CLOSED — if `acceptance_criterion` handles are present on `state["artifacts"]` but
  `resolve is None`, it raises `MissingCriteriaResolverError` rather than silently returning `()`.
- QS7. **(C-R1-04) The dead raw-criteria path was REMOVED.** `quality._common.criteria_from_state`
  (the `isinstance(a, AcceptanceCriterion)` read) only fired for direct-call unit tests; in a folded
  graph it is dead AND widened the surface masking C-R1-01 (raw bodies have no `.uri` and crash
  `merge_by_uri` anyway, QS2). `read_criteria` now reads ONLY handles+`resolve` — production and unit
  tests use the SAME path: seed criteria as `acceptance_criterion` handles + an in-memory resolver.
- QS8. **(C-R1-03) Reviewer subject fallback selects by KIND, newest-by-`seq` — not `artifacts[-1]`.**
  `artifacts` folds by `merge_by_uri` (insertion-order, not seq), so `artifacts[-1]` is not
  deterministically the diff under review. `reviewer_critic._newest_feature_diff_uri` picks the
  `feature_diff` handle with the max `Seq` (mirrors `senior_engineer._first_of_kind`); the
  config-pinned `subject_artifact_uri` stays the PRIMARY source and the node still fails closed on an
  empty subject.
- QS9. **(C-R1-02) product.py + quality.py now use the concrete-`def` wrapper for `add_node`**
  (SG2/IM5), not `cast("StateNode[...]", fn)`. All four layer subgraphs are consistent; the
  `StateNode` TYPE_CHECKING import is gone.
- QS4. **Quality subgraph SOP order (agents.md Layer 4): `START -> reviewer_critic -> qa_engineer ->
  sdet -> END`** (linear). Reviewer is the entry (it judges the diff); qa authors TestPlan +
  per-high-risk NEGATIVE OracleBinding + quarantining TestReport; sdet authors E2EReport.
- QS5. **Nonce/verdict path threading.** The reviewer is the ONLY quality node touching `gates`, and
  only via `rt.proxy.judge` (proxy stamps the binding; agent holds no key, L5/Q2). The single-use
  `verdict_nonce` is a RUN INPUT (Q1): seed it on `config["configurable"]["verdict_nonce"]`
  (alongside `subject_artifact_uri` / `state_sha` / `thread_id`) — agents NEVER mint it; absent it
  the node fails closed (`MissingVerdictNonceError`). The subgraph test installs a `DevGateWriteVerifier`
  via `set_gate_write_verifier(...)` (reset the token in `finally`), seeds a `FeatureDiff` handle as
  the subject + criterion handles + `resolve`, scripts `verdicts=["approve"]` for the reviewer and
  `completions=[QA, SDET]` for the authors, and asserts the reviewer's proxy-stamped `GateResult`
  is admitted to `gates` by the REAL `merge_gates` (valid binding + fresh nonce + no latch).

## Graph-side gate wiring at phase boundaries (T-M1-12)
- GW1. **`materialize_workdir(state, dest, *, resolve, resolve_blob) -> tree_sha` is the boundary's
  trust-seam contract.** It builds the integrated tree the gates run against, ENTIRELY from the
  content-addressed handles on `state["artifacts"]`: writes `criteria.json` from the
  `acceptance_criterion` handles (typed `resolve`) in the EXACT shape SpecGate parses —
  `{id, text, oracle, security_abuse}` (NB: `AcceptanceCriterion.oracle_hint` -> the `oracle` key),
  ordered by `id` so the file (and digest) is byte-stable; and materializes code+test sources from
  the `feature_diff` handles. FAIL-CLOSED: a handle present but unresolvable raises
  (`MaterializationError`/`FeatureDiffBodyError`/`PathContainmentError`) — never run the gates against
  a partial tree (vacuous PASS risk, gov §1b). Returns `tree_sha(dest, prefer_git=False)` (dest is a
  fresh tree, not a git worktree) as the integrated `run_against`.
- GW1a. **(D-R1-01) The FeatureDiff body is a UNIFIED-DIFF blob, NOT a path->content dict.** The real
  adapters (StubAdapter / raw_react / claude_code) store the diff body via `sandbox.put_blob(text)` as
  a unified-diff STRING (`--- /dev/null\n+++ b/<path>\n@@ ...\n+<line>`) and it reads back as bytes via
  `sandbox.get_blob(uri) -> bytes`. The earlier `resolve(diff_uri) -> Mapping[str,str]` contract
  matched NO emitter (tests fabricated a `{uri: dict}` no producer makes) — the real end-to-end path
  would have raised `FeatureDiffBodyError`. Two DISTINCT seams now (D-R1-02): typed `ArtifactResolver
  = Callable[[Artifact], Any]` reconstitutes the typed `FeatureDiff`/`AcceptanceCriterion` from its
  handle; a SEPARATE `BlobResolver = Callable[[str], bytes|str]` keyed on the `cas://` diff_uri
  fetches the raw diff body (prod: `sandbox.get_blob`) — so a kind-aware CAS resolver can't
  mis-dispatch the body as a typed artifact, and we never `model_copy(update={"uri": diff_uri})` a
  handle. A minimal Phase-1 applier (`_parse_addition_diff`) handles pure file ADDITIONS only
  (`--- /dev/null` hunks; it also tolerates the StubAdapter's single-`+`-prefixed-first-line shape)
  and FAILS CLOSED on modifications/deletions — full patch application is a later phase.
- GW1b. **(D-R1-03) Path containment is enforced before any write.** Diff-body paths are
  agent-influenced; `_contained_target` rejects absolute paths, any `..` component, or anything that
  resolves outside `dest` (raises `PathContainmentError`) BEFORE writing, so a body path like `../x`
  or `/etc/x` can't escape the integrated tree.
- GW1c. **Test the GENUINE producer->consumer contract.** The round-trip test drives a REAL
  `StubAdapter.run(packet, sandbox)`, takes the resulting `diff_uri`, and resolves it with a
  `BlobResolver` backed by `sandbox.get_blob` — exercising emit->materialize end to end, NOT a
  hand-fabricated path->content dict. Diffs applied in `Seq` order (later/retry wins, L4).
- GW2. **Verdicts come ONLY from the runner; the wiring NEVER constructs a `GateResult`.**
  `run_spec_gate(state, config, *, rt, runner, resolve, workdir) -> {"gates":[...]}` (Spec at
  product/architecture exit) and `run_build_test_gates(..., min_passed=1) -> {"gates":[build, test]}`
  (Build+Test at implementation/quality exit) materialize the workdir, mint each verdict's `Seq` via
  `rt.seq(config, "<gate>_gate", "verdict:<name>")` (resume-monotonic epoch, AR2), and call
  `runner.run(gate, run_against, seq, workdir)` — the runner attests; the helper just forwards the
  attested record. Build+Test share ONE materialized `tree_sha` so both bind/latch to one integrated
  state. The real `merge_gates` admits them iff attested (an unsigned `GateResult` is dropped — the
  test proves this by constructing one and asserting `merge_gates([unsigned], None) == []`).
- GW3. **`route_after_gates(state, config, *, boundary, records, max_fix_attempts=3) -> str` returns
  `advance` / `fix` / `halt`, fail-closed.** Reads the newest-by-`Seq` verdict per relevant gate
  (`_BOUNDARY_GATES`: spec -> {spec}; build_test -> {build,test}) off the already-trust-filtered
  `gates` channel. Order: (1) a `records.stable_fail_latched(run_against, name)` on ANY relevant
  verdict -> `halt` (terminal, cross-fork, monotonic — a later PASS never auto-clears; routing reads
  the latch REGARDLESS of channel content, so a status-flipped copy can't sneak past); (2) any FAIL
  or ERROR (infra OR flake) — and a MISSING relevant verdict (defense-in-depth: never advance on an
  absent verdict) -> `fix` within the bound, `halt` once `fix_attempts >= max_fix_attempts`
  (`fix_attempts` read from `budget.fix_attempts_since_all_green` else
  `config["configurable"]["fix_attempts"]`); (3) all PASS, nothing latched -> `advance`. ERROR never
  latches (DS7), so it always routes to bounded retry. Flake quarantine (N=2 split Test/E2E ERROR)
  does NOT fail the boundary — it routes to `fix` like any ERROR; `flake_quarantined(state, *,
  boundary)` exposes the REASON (gate family + flake/quarantine finding marker) so the supervisor can
  quarantine-and-retry vs report a failure. The supervisor (next wave) calls these plain helpers at
  boundaries; gate_wiring does NOT edit supervisor.py/factory_graph.py (they don't exist yet).

## Supervisor — phase routing + gate-wiring at boundaries (T-M1-13)
- SUP1. **`build_supervisor_graph(rt, *, runner, resolve, resolve_blob, source_dir, index=None,
  max_fix_attempts=3) -> StateGraph[FactoryState]`** returns the UNCOMPILED graph (SG4; factory_graph
  T-M1-14 compiles + adds the checkpointer + emit node). Phase order:
  `triage -> product -> architecture -> [spec_gate] -> implementation -> quality -> [build_test_gate]
  -> END`. The four layer subgraphs are mounted as nodes via `build_<layer>_subgraph(...).compile()`
  then `.invoke(state, config)` inside a thin concrete node `def` (SG2/IM5 — the `NodeFn` alias trips
  `add_node` overload resolution; wrap, don't `cast`). The subgraph node also stamps the single-writer
  `phase` channel.
- SUP2. **Gate nodes call the gate-wiring helpers; verdicts ONLY from the runner (L5/GW2).** `spec_gate`
  -> `run_spec_gate(...)`; `build_test_gate` -> `run_build_test_gates(...)`, each returning
  `{"gates":[...]}` (forwarded attested records). Each gate node materializes into a FRESH
  `tempfile.mkdtemp()` workdir (the integrated `tree_sha` is a pure function of the content-addressed
  handles, so a per-run temp dir is fine).
- SUP3. **Routing = `add_conditional_edges(gate_node, _route, {"advance": next, "fix": fix_node,
  "halt": END})`** where `_route` calls `route_after_gates(state, config, boundary=..., records=...,
  max_fix_attempts=...)` (GW3). SPEC: advance->implementation, fix->architecture (re-verify), halt->END.
  BUILD_TEST: advance->END, fix->implementation, halt->END. The path map MUST list every legal return
  (`advance`/`fix`/`halt`) or compile rejects it. `route_after_gates` needs the cross-fork
  `GlobalRecordsView`; the supervisor signature carries no `records=`, so read it off the runner.
  (E-R1-02) The supervisor now reads it via the PUBLIC read-only `TwoZoneGateRunner.records`
  property (returns `_records`; read-only — the runner stays the SOLE latch writer via `run`'s
  `latch_stable_fail`), NOT the private `runner._records`. No `# noqa: SLF001` anymore — the runner
  is still the records' owner, but the read goes through its public surface.
- SUP4. **Fix-bound lives on `BudgetLedger.fix_attempts_since_all_green` (single-writer `budget`),
  bumped BY THE GATE NODE, not the fix node.** The gate node, after running the gates, re-evaluates its
  own boundary against the freshly-produced verdicts (`route_after_gates` over `{gates: out["gates"],
  budget: state-budget}`) and, iff the decision is `fix`, writes an incremented ledger onto `budget` in
  the SAME superstep. The conditional edge then reads the POST-FOLD bumped counter, so it converges:
  fix N times then `halt`. The `*_fix` nodes only stamp `phase` + edge back to the responsible layer —
  if they bumped, the edge (same superstep as the gate) would read the stale pre-bump value and never
  converge. No infinite loop. (E-R1-01) The gate node's bump decision now derives from the SAME
  post-fold `gates` view the edge routes on: `_bump_on_non_advance` folds `state["gates"]` with the
  node's `out["gates"]` delta through the REAL `merge_gates` reducer (latest-by-Seq per
  (name,run_against), trust-filtered) — NOT a hand-assembled `{"gates": out["gates"]}` subset. The
  edge reads the full channel AFTER LangGraph folds the SAME delta through `merge_gates`, so the
  in-node bump can never diverge from the edge's post-fold read by an unenforced invariant. Behavior
  is identical for passing/converging cases (the run-scope `set_gate_write_verifier` contextvar is
  active inside the node, so the in-node fold admits the freshly attested records exactly as the
  edge's fold does; with no verifier installed BOTH folds drop them identically — still agreeing).
- SUP5. **A hard FAIL on an integrated digest does NOT exercise the route-back loop — it LATCHES +
  halts AT ONCE (gov §5; latch check is first in `route_after_gates`).** Only an ERROR (infra/flake,
  DS7) or a missing verdict routes to bounded `fix`. So the end-to-end "routes back + terminates
  bounded" test must force an ERROR, NOT a FAIL: a `test_app.py` that IMPORTS a missing module compiles
  (Build PASS via `compileall`) but FAILS pytest COLLECTION (exit 2) -> a signed Test ERROR -> `fix` ->
  bounded -> `halt`. (A syntax error would instead trip `compileall` -> Build FAIL -> latch.)
- SUP6. **Triage seeds `route` + `budget` (both single-writer).** `classify_request(request)` is a
  deterministic substring classifier (bugfix/spike/evolution markers in order; default greenfield);
  `_seed_budget(route)` runs the route's cap through `BudgetController(BudgetCaps(total_microusd=cap))`
  and stamps `BudgetLedger(cap_microusd=...)` (zero spend, zero fix attempts).
- SUP7. **End-to-end test seam = a KIND-dispatching synthetic `resolve` (not a uri-keyed map).** The
  architecture/product agents mint content-addressed HANDLES mid-run whose uri encodes an
  epoch-dependent `content_identity()` (so it can't be pre-keyed) and the body is NEVER persisted in a
  test (`emit_artifact` only makes a handle). So the in-memory typed resolver synthesizes a faithful
  body from `handle.kind` + `handle.seq` (SystemDesign/OwnershipMap for the Senior's decomposition;
  AcceptanceCriterion with `security_abuse=True` for the Spec gate; FeatureDiff -> a fixed `diff_uri`).
  `resolve_blob` returns the raw unified-diff body keyed on that fixed uri (mirrors `sandbox.get_blob`)
  — the test controls whether it carries a passing test (Build+Test PASS) or a collection-erroring one.
  The reviewer's `verdict_nonce`/`subject_artifact_uri`/`state_sha` are RUN INPUTS seeded on
  `config["configurable"]` (Q1). Script per-node payloads with `MockTransport.scripted({SOP-marker:
  payload}, default_verdict="approve")` — substring-keyed + COUNT-INDEPENDENT so the fix-loop can re-run
  the implementation+quality phases without exhausting a FIFO `completions=` queue (an emptied queue ->
  empty `default_text` -> a parse failure on the re-loop). Raise LangGraph's `recursion_limit` on the
  run config for multi-loop fix runs.

## Assurance gate-wiring at the assurance boundary (T-M2-05)
- AW1. **`foundry/graph/assurance_wiring.py` is the SIBLING of `gate_wiring.py` for the Phase-2
  assurance boundary — it REUSES `gate_wiring.materialize_workdir` verbatim, never edits it.**
  `run_assurance_gates(state, config, *, rt, runner, resolve, resolve_blob, workdir, tier=None,
  allow_build_changes=False) -> {"gates":[...]}` materializes the integrated tree ONCE, runs the
  tier-scaled Security/Audit/Supply-chain/E2E family via `runner.run(gate, run_against, seq, workdir)`
  (seq = `rt.seq(config, "<gate>_gate", "verdict:<name>")`, AR2), and folds ONLY the attested verdicts.
  Verdicts come ONLY from the runner (L5) — the helper NEVER constructs/signs a `GateResult`; the
  REAL `merge_gates` drops an unsigned/agent verdict so it can never gate advancement (the test
  proves `merge_gates([unsigned_security], None) == []`).
- AW2. **The assurance gates need TWO grounding inputs the base materializer doesn't write —
  `materialize_assurance_workdir` augments the tree, then RE-HASHES it.** Base `materialize_workdir`
  writes criteria.json + code/test sources (Security scans those; E2E runs them). On top of that this
  module writes `changes.diff` (= the concatenated FeatureDiff unified-diff bodies, in Seq order, via
  the SAME `resolve`+`resolve_blob` seams) so SupplyChainGate (`diff_filename="changes.diff"`) has its
  policy input; the Audit gate's lockfile inventory rides in as a normal source file the diff added
  (`uv.lock`/`requirements*.txt`). Because the tree changed, the returned `tree_sha` is RE-COMPUTED
  over the augmented tree — the integrated `run_against` the verdicts bind/latch against reflects the
  EXACT tree the gates ran over. (`SecurityGate(use_semgrep=False)` pins the dev-grade secret scan so
  the floor needs no external binary, plan §0.3.)
- AW3. **KNOWN-VULNERABLE-CHANGE-CAUGHT (the exit criterion).** Materialize a source file with a
  hardcoded secret (`API_KEY = "...token..."` trips SecurityGate's `hardcoded-password` dev rule) →
  attested `security` FAIL → `route_after_assurance` returns NOT "advance" (it `halt`s: a FAIL on an
  integrated hex digest latches stable-FAIL, terminal). A second proof: a `requirements.txt` line
  `evil @ git+https://...` → attested `supply_chain` FAIL (`new-dep-source`) at STANDARD+. Both are
  runner-attested, so the REAL `merge_gates` admits them and routing blocks — the vulnerable change
  cannot ship.
- AW4. **TIER SCALES DEPTH, NEVER INTEGRITY (gov §1e).** `assurance_gate_set(tier)`: floor = Security
  + Audit ALWAYS (None/LOW = floor only); STANDARD adds supply_chain; HIGH adds e2e. No path removes a
  floor gate — a LOW tier still runs the security floor fail-closed and still catches the secret
  (tested at BOTH tiers). `route_after_assurance(state, config, *, records, tier=None,
  max_fix_attempts=3)` mirrors `route_after_gates`: stable-FAIL latch → halt; FAIL/ERROR or a MISSING
  tier-relevant verdict → bounded fix then halt; all-PASS → advance. It scopes the required verdict
  set by the SAME tier-scaled `assurance_gate_set`, so LOW advances on Security+Audit PASS while HIGH
  also requires supply_chain+e2e. ERROR (e.g. Audit with no lockfile → inventory not rederivable)
  never latches → bounded retry. The assurance subgraph/supervisor call these plain helpers at the
  boundary (mirrors SUP2/SUP3 for the M1 gate nodes).
- AW5. **Gotcha: `gate_wiring.ArtifactResolver`/`BlobResolver` are TYPE_CHECKING-only — don't import
  them at runtime.** Mirror the aliases under your own `if TYPE_CHECKING:` (`= gate_wiring.X`); with
  `from __future__ import annotations` the function-signature annotations are strings so runtime is
  fine and mypy still resolves them. Importing them at module top raises `ImportError` at collection.

## Assurance subgraph + bounded evaluator-optimizer rework loop (T-M2-08)
- AS1. **`build_assurance_subgraph(rt, *, runner, resolve, resolve_blob, source_dir=None, tier=None,
  max_fix_attempts=3) -> StateGraph[FactoryState]` (UNCOMPILED; SG4).** Node order:
  `START -> appsec_engineer -> audit_compliance -> supply_chain_reviewer -> reviewer_critic ->
  assurance_gate -> {advance: END, fix: assurance_fix -> assurance_gate, halt: END}`. The three
  Layer-5 security agents author advisory `GateTriage` only (L5/SEC1-4); the reviewer/critic is the
  EVALUATOR (the only agent touching `gates`, via `rt.proxy.judge`, proxy-stamped; Q1/Q2); the
  `assurance_gate` node forwards ONLY runner-attested verdicts via `run_assurance_gates` (AW1).
  `source_dir` is parity-only (the gates materialize a fresh tree from handles). Wrap every node in a
  concrete `def` for `add_node` (SG1/SG2/IM5 — never the `NodeFn` alias).
- AS2. **The rework loop is bounded by the SUPERVISOR mechanism, re-used verbatim (SUP4).** The
  `assurance_gate` node bumps `budget.fix_attempts_since_all_green` ONCE per non-advancing iteration
  (decision derived from the SAME post-fold `merge_gates(state, out)` view the conditional edge reads
  in the SAME superstep, E-R1-01) and the `assurance_fix` node only marks `phase` + edges back to the
  gate. So the counter increases monotonically toward `max_fix_attempts`, after which the router
  returns `halt` — the loop terminates, never hangs. Raise `recursion_limit` on the run config so the
  bounded loop has room to converge; reaching a terminal state (vs `GraphRecursionError`) is the proof
  it halts.
- AS3. **The subgraph router composes BOTH evaluators.** `_route_assurance` calls
  `route_after_assurance` (the deterministic gates: latch->halt; FAIL/ERROR/missing->bounded fix;
  all-PASS->advance, AW4) AND the reviewer's verdict: a `request-changes` (review FAIL) forces a
  bounded `fix` even when every machine gate PASSed — but never SHORTCUTS the bound (still `halt` once
  spent). A request-changes verdict establishes a NEGATIVE LATCH (Q3) so it drops from the re-folded
  `gates` view on the next iteration; `_review_requests_changes` is fail-closed (missing review ->
  True), so the loop keeps routing `fix` until the bound -> still terminates.
- AS4. **Re-verification scoping = `rework_scoped_gates(state, *, runner, tier)` (gov §1f / RV1-5).**
  On a REWORK iteration (prior verdicts present on `gates`) the gate node re-runs ONLY the scoped
  subset: it builds a `reverification_plan` over the tier's `_assurance_gates` and re-runs a gate iff
  its scope intersects the change OR its newest prior verdict is not a PASS (fail-closed); unaffected
  PASSing gates keep their prior attested verdict (change-proportional, never a blind full re-run).
  The current scanner-config rev is read off a prior attested verdict's `scanner_config_rev` (the
  runner stamps it on every `GateResult`) — NOT through the runner's private `_scanner_config_rev`.
- AS5. **Post-run, a latched FAIL drops from a RE-fold.** After the graph run latches a stable-FAIL
  (security caught a hardcoded secret), `merge_gates(final["gates"], None)` returns `[]` (the latch is
  terminal, Q3) — so assert the attested FAIL is present on `final["gates"]` directly (+ check
  `runner_attestation is not None` / `records.stable_fail_latched(...)`), NOT via a re-fold (that
  passes only BEFORE the latch is set, as in test_assurance_wiring).

## Evaluation — Phase-1.5 central-hypothesis schemas (T-M15-01)
- EV1. **`foundry/evaluation/hypothesis.py` is a PURE schema/enum layer — NO statistics.** Power
  calcs, bootstrap, alpha-spending, scoring live in the later M1.5 modules
  (preregistration.py/decision_table.py/scorecard.py/…). The next 10 tasks build on these frozen
  types; do NOT add stats logic here. `foundry/evaluation/__init__.py` re-exports the types and is
  import-cheap (pydantic + enums only).
- EV2. **API.** `Arm` (FOUNDRY / BASELINE_SINGLE_AGENT / SINGLE_AGENT_PLUS_GATES),
  `DecisionClass` (PROCEED / HARD_KILL / BOUNDED_OVERHEAD_REDESIGN), `Equalizer`
  (EQUAL_USD primary / EQUAL_EFFECTIVE_TASK_TOKEN secondary), `Provenance`
  (DRY_RUN / PILOT / SCORED). `PrimaryMetric` = SWE-EVO resolution rate, `higher_is_better`,
  `oracle_scored` (§4: no LLM judge in primary), `equalizer=EQUAL_USD`, `intent_to_treat` (§2 ITT).
  `PowerRung` = the funded rung (`mde>0`, `0<target_power<1`, optional `n_tasks/k_repeats/
  funded_budget_microusd` frozen off the pilot). `CentralHypothesis` carries h0/h1, primary_metric,
  treatment/control arms (FOUNDRY vs BASELINE at `at_most_equal_cost`), `alpha` (0<α<1), `mde`>0
  (committed pre-run), `funded_power_rung`, `decision_classes`, `provenance`, `dry_run`.
- EV3. **DRY-RUN HONESTY CONVENTION (load-bearing for the whole harness).** Every hypothesis model
  defaults `provenance=Provenance.DRY_RUN` + `dry_run=True`: it is a METHODOLOGY DRY-RUN on synthetic
  fixtures, asserting NOTHING about real Foundry-vs-baseline performance — it only fixes the
  pre-registration SHAPE so it can be hashed into a manifest. Flip to `Provenance.SCORED`/`PILOT`
  (+ `dry_run=False`) for the funded run/pilot. Keep this default and the no-real-evidence docstrings
  on every model the later tasks add.
- EV4. **Validation via `Field(gt=…/lt=…/ge=…)` (not `field_validator`)** — `0<alpha<1`, `mde>0`,
  `0<target_power<1`, positive counts. `FrozenModel` (extra=forbid, frozen) + `content_identity()`
  apply unchanged; `provenance`/`dry_run` are part of identity, so a dry-run and a scored hypothesis
  with otherwise-equal fields have DIFFERENT content identities (don't coalesce a real run with its
  dry-run rehearsal). Monetary fields stay integer micro-USD (base.py convention), never float;
  `mde`/`alpha`/`target_power` are floats (they are rates/effect sizes, not money).

## Evaluation — synthetic task frame + disjoint pilot/scored slices (T-M15-02)
- EV5. **`foundry/evaluation/task_fixtures.py` is the deterministic SYNTHETIC task frame the whole
  M1.5 harness runs on with NO live model/benchmark.** API: `generate_synthetic_frame(*, n_tasks=24,
  seed=0, pilot_fraction=0.4) -> TaskFrame` builds N frozen `TaskFixture`s (each: `task_id`, synthetic
  `spec`, an `OracleDescriptor` oracle — deterministic equality target, NOT an LLM judge per §4 —
  `difficulty`∈{EASY,MEDIUM,HARD}, per-stratum `control_base_rate`, and a `base_resolution_seed` the
  NEXT task's per-arm runner folds with Arm+repeat to draw a deterministic resolved/unresolved
  outcome). Difficulty is ROUND-ROBIN over the 3 strata so a spread is GUARANTEED (the pilot needs
  between-task variance + base rate to estimate). HONESTY: every model defaults `synthetic=True`; the
  frame asserts NOTHING about real Foundry-vs-baseline perf — it only fixes a stable contract to hash.
- EV6. **Pilot/scored slices are DISJOINT + DETERMINISTIC by a seeded prefix-cut of a single
  permutation (evaluation.md §2 — both drawn from ONE frame so base rate + variance transfer).**
  `frame.pilot_slice` / `frame.scored_slice` (cached `@property`) sort task_ids FIRST (partition
  independent of fixture insertion order), `random.Random(seed).shuffle`, then cut at
  `round(n*pilot_fraction)` clamped to `[1, n-1]` (both sides non-empty for n>=2; `n_tasks<2` raises).
  Prefix vs suffix of ONE permutation ⇒ disjoint AND exhaustive (`pilot | scored == all_ids`) by
  construction. Same `(n_tasks, seed, pilot_fraction)` ⇒ identical split.
- EV7. **`eval_set_identity` is `content_digest` (L7, never `hash()`) over the SORTED-BY-ID fixtures +
  seed + pilot_fraction — it binds the alpha look-ledger `(eval_set_identity, decision_id)`
  (`global_records.py`) and the preregistration.** It is stable across reconstruction of an equal
  frame and FLIPS iff the tasks OR the partition (seed/pilot_fraction) change — so forking/retraining
  cannot silently re-draw the alpha budget. Each `TaskSlice.identity` is a further `content_digest`
  over `(eval_set_identity, name, task_ids)`, so a slice identity is bound to its parent frame (a
  like-named slice of a different frame differs even on coincidental membership overlap). The pilot,
  A/B, and preregistration tasks all build on this frame/slice API.
- EV8. **`foundry/evaluation/harness.py` is the deterministic oracle-scored per-arm runner + ITT
  ratio the pilot & A/B runner build on (T-M15-03).** API: `run_arm(arm, slice, fixtures, *,
  k_repeats=1, seed=0, budget_usd_microusd=5_000_000, foundry_runner=default_foundry_runner)
  -> ArmResult` (frozen). It runs every `slice.task_id` (SORTED -> order-independent) `k_repeats`
  times, emits ONE `TaskOutcome` per attempt (`status` ∈ RESOLVED/UNRESOLVED/CRASHED/OVER_BUDGET,
  `resolved`, `cost_microusd`), and oracle-scores by EXACT EQUALITY vs `fixture.oracle.expected_output`
  (`_score`, no LLM judge — §4; a `None` produced output can never match). `ArmResult.resolved_count`
  / `.total_cost_microusd` are derived; `make_run(result) -> Run` wraps it with a content-addressed
  `run_id` (L7). FOUNDRY arm = REAL pipeline: `default_foundry_runner` wraps `foundry.cli.run(spec,
  thread_id=...)` KEYLESS via the doubles (D1-D3; cli import is FUNCTION-LOCAL so harness stays
  import-cheap), resolved iff `passed AND open_pr_idempotency_key is not None`; USD cost is SIMULATED
  (RunResult carries no metered cost). BASELINE / +gates arms are SIMULATED stand-ins (`simulated=True`,
  flagged) — a deterministic Bernoulli draw folded from `(base_resolution_seed, arm, repeat, seed)` via
  `content_digest` (L7, never `hash()`) vs the arm-adjusted `control_base_rate`. All default
  `provenance=DRY_RUN`.
- EV9. **ITT DENOMINATOR RULE (load-bearing — §2): `itt_pass_rate = resolved / attempted` where
  `attempted == len(slice) * k_repeats` (ALL initiated tasks).** Crashes, over-budget, and
  escalation-without-resolution score `resolved=False` and are RETAINED in `outcomes` (NEVER dropped) —
  differential attrition otherwise inflates whichever arm aborts more. TWO guards keep the denominator
  from silently shrinking: `run_arm` asserts `len(outcomes) == attempted`, and `itt_pass_rate` asserts
  `attempted == len(outcomes)` (a hand-built `ArmResult` with a dropped sample raises). A FOUNDRY-arm
  exception is caught -> CRASHED/unresolved (not a drop); an attempt whose nominal cost exceeds
  `budget_usd_microusd` is OVER_BUDGET/unresolved, charged the CAP (equal-USD honesty), not the overrun.
  The crash test injects an always-raising `foundry_runner` and asserts every outcome is CRASHED,
  `resolved_count==0`, and the denominator is unchanged. (Don't condition the rate on completed runs —
  that is the collider bias §2/§6 forbid.)

## Evaluation — variance/base-rate PILOT (T-M15-04)
- EV10. **`foundry/evaluation/pilot.py::run_pilot(frame, *, arms=DEFAULT_PILOT_ARMS,
  k_repeats=2, seed=0, budget_usd_microusd=...) -> PilotResult` (frozen) runs the arms over
  `frame.pilot_slice` ONLY (via `run_arm`) and estimates the read-only summary the LATER power-calc
  module CONSUMES.** `PilotResult` exposes EXACTLY: `control_base_rate` (= control arm's
  `itt_pass_rate`, the §2 strong-baseline rate, NOT the borrowed ~21%), `effect_size_variance`
  (between-task POPULATION variance of the per-task FOUNDRY−CONTROL difference — the cluster effect
  the §2 paired bootstrap is powered on), `control_resolution_variance` (between-task variance of the
  control's per-task resolution), `pilot_n`, `k_repeats`, `consumed_budget_microusd`,
  `eval_set_identity` (the PILOT slice identity, EV7), `provenance=PILOT`, `scored=False`. The TASK is
  the inferential unit (§2): collapse each task's `k_repeats` to ONE per-task rate BEFORE taking
  variance (`n×k` as independent draws overstates effective N). `DEFAULT_PILOT_ARMS=(FOUNDRY,
  BASELINE_SINGLE_AGENT)`; the control arm is REQUIRED (else `ValueError` — base rate undefined).
- EV11. **THREE integrity guards (the pilot is read-only INTO the power calc, never a second scored
  arm).** (1) DISJOINT-SLICE: `run_pilot` asserts `set(pilot.task_ids) & set(scored.task_ids) == {}`
  and raises `ScoredSliceTouchedError` on overlap; it also hands `run_arm` ONLY the pilot fixtures
  (filtered by `pilot_ids`) so a stray lookup can't touch a scored task. (2) NOT A SCORED ARM:
  `provenance=PILOT` + `scored=False` + docstring; there is NO path from `run_pilot` to a scored
  verdict, and `PilotResult` exposes only the read-only summary (no per-task outcomes leak). (3)
  BUDGET RECORDED: `consumed_budget_microusd = Σ arm.total_cost_microusd` (§8 reserves it). RE-RUNNING
  the pilot does NOT re-draw the scored A/B: the partition is a deterministic seed-fixed prefix-cut of
  one frame permutation (EV6), so `frame.scored_slice.identity` is unchanged across pilot runs at any
  seed — the pilot only re-estimates, it never selects scored tasks. Determinism: same `(frame, arms,
  k, seed, budget)` ⇒ identical `PilotResult` (`content_identity()` stable). Tests: 18, all green; the
  FOUNDRY-inclusive ones keep a SMALL frame (FOUNDRY wraps the shipped pipeline keyless via the
  doubles, D1-D3; control-only runs are cheap simulated stand-ins). The overlap-raise test subclasses
  `TaskFrame` to override `pilot_slice` to inject a scored id (frozen model, so `model_copy(update=)`).

## Evaluation — power calc + (n,k) constrained allocation (T-M15-05)
- EV12. **`foundry/evaluation/power.py::compute_power_rung(pilot, *, mde, alpha, target_power,
  budget_usd_microusd, per_task_repeat_cost_microusd, k_max=16) -> FundedPowerRung` (pure stats, NO
  model calls) sizes the §2 funded rung off the read-only `PilotResult`.** FORMULA: the inferential
  unit is the TASK (cluster); each task contributes ONE per-task resolution rate (its `k` repeats
  collapsed — `n×k` as independent overstates effective N), and we power a **two-sample
  difference-in-means z-test** on FOUNDRY vs BASELINE per-task rates:
  `n_per_arm = ceil((z_{1-α/2}+z_{1-β})² · (var_T+var_C) / mde²)`. Per-arm per-task variance =
  `σ²_between + p(1−p)/k` (`_per_arm_task_variance`): the pilot's `control_resolution_variance`
  (between-task, does NOT shrink with k) PLUS the within-task Bernoulli term that AVERAGES DOWN with
  k (so k only pays while within-task dominates). Control base rate = `pilot.control_base_rate`;
  treatment rate = `control+mde` clamped `[0,1]`; treatment between-task var = `max(control_between,
  pilot.effect_size_variance)` (conservative floor). `normal_ppf` is Acklam's probit (no scipy);
  `normal_cdf` via `math.erf`. CLOSED-FORM PIN: base=0.2, treatment=0.3, zero between-var, mde=0.1,
  α=0.05, power=0.8 ⇒ z_{.975}=1.95996, z_{.8}=0.84162 ⇒ **n=291/arm** (test asserts exact).
- EV13. **(n,k) is a CONSTRAINED OPTIMIZATION, not two free numbers.** For each `k∈[1,k_max]` compute
  the budget-unconstrained `required_n` and the equal-USD cost `2·n·k·per_task_repeat_cost_microusd`
  (TWO arms); among candidates that fit budget AND reach `target_power`, pick the CHEAPEST (prefers
  small k once between-task var dominates). INTEGRITY (load-bearing, §1/§2/§8): the rung is computed
  from (pilot var/base-rate + the PRE-COMMITTED mde/alpha/target_power) ONLY — this module NEVER
  chooses or relaxes them. If no `(n,k)` reaches `target_power` within budget, it returns
  UNDERPOWERED at the funded budget (`funded=False`/`underpowered=True`, `achieved_power<target` of
  the BEST budget-affordable allocation) with the COMMITTED `mde`/`alpha` UNCHANGED — it does NOT
  shrink MDE or inflate alpha, and there is NO "re-pick a smaller MDE after data" path. Explicit
  side-effect-free `is_funded(rung, budget) -> bool` (compares frozen `funded_budget_microusd` ≤
  budget; raises if unset). `FundedPowerRung` SUBCLASSES the frozen `PowerRung` (same manifest
  contract) + adds `alpha`, `required_n_tasks`, `achieved_power`, `funded`, `underpowered`. The caller
  freezes the returned rung in the preregistration BEFORE scored runs. Tests: 17, all green; full
  suite unbroken. Monotonicity covered (smaller MDE / higher power / higher variance → larger n);
  k-halving check (zero between-var ⇒ doubling k ≈ halves n); determinism (`content_identity()`
  stable). HONESTY: the pilot is synthetic/simulated, so a rung asserts NOTHING about real perf.

## Evaluation — frozen pre-registration manifest + alpha-ledger binding (T-M15-06)
- EV14. **`foundry/evaluation/preregistration.py::freeze_preregistration(hypothesis, funded_rung,
  scored_eval_set_identity, *, decision_id, ledger, state_sha="preregistration",
  run_id="preregistration") -> PreRegistration` is the integrity keystone: it freezes the BINDING
  manifest BEFORE any scored run AND draws the alpha budget in one fail-closed step.** `PreRegistration`
  is a frozen `FrozenModel` capturing the `CentralHypothesis`, committed `mde`+`alpha` (mirrored off
  the rung), the `FundedPowerRung` (n,k from T-M15-05), the SCORED `scored_eval_set_identity` (EV7), the
  `decision_id`, a `decision_table_ref`, and a provenance note; `registration_identity()` ==
  `content_identity()` is what a SCORED RUN MUST CITE. THREE HARD GUARDS: (1) FUNDED-ONLY — raises
  `UnderpoweredPreRegistrationError` if `rung.underpowered or not rung.funded` (or no concrete (n,k));
  you cannot pre-register an underpowered study as if powered (§2 BLOCKER). (2) NO-RE-DRAW — checks
  `ledger.alpha_looks_spent(eval_set_identity, decision_id) > 0` BEFORE recording and raises
  `AlphaAlreadyDrawnError`, so a failed/rejected freeze never appends a look; a fork/retrain
  re-presenting the SAME pair is rejected (ledger keyed on the PAIR, NOT `state_sha` — `state_sha`/
  `run_id` are audit-only per-look). A different `decision_id` OR `eval_set_identity` draws its own
  budget (allowed). (3) TAMPER-EVIDENT — content-addressed, so editing `mde`/`alpha`/the rung yields a
  different `content_identity`; `assert_unmodified(prereg, *, expected_identity=None)` re-derives the
  identity, fails closed on a cited-identity mismatch AND on a top-level `mde`/`alpha` mirror that
  diverges from the rung's. The A/B runner + decision table (later tasks) CITE this frozen manifest.
- EV15. **Build the funded rung from the REAL power calc, not by hand.** Tests construct
  `FundedPowerRung` via `compute_power_rung(PilotResult(...), mde, alpha, target_power, big_budget,
  per_task_repeat_cost)` (funded) and the same with a 1-microUSD budget (underpowered — can't fund one
  task-repeat/arm). `freeze_preregistration` reads `rung.alpha`/`rung.mde` for the manifest mirror (the
  base `PowerRung` has no `alpha`; the `FundedPowerRung` subclass adds it — EV12). DETERMINISM is
  ledger-independent: the manifest is a pure function of its inputs, so two freezes against SEPARATE
  fresh `GlobalRecords()` (to avoid the no-re-draw rejection) yield an identical `registration_identity`.
  A SCORED-provenance hypothesis does NOT coalesce with its DRY_RUN rehearsal (provenance ∈ identity,
  EV4). Tests: 14, all green; full suite unbroken (526 passed, 1 skipped). HONESTY: synthetic dry-run —
  asserts NOTHING about real perf.
- EV19. **HYPOTHESIS↔RUNG COHERENCE is a FOURTH hard guard (M15-R1-01 fix).** The manifest mirrors
  `mde`/`alpha` off `funded_rung`, but the EMBEDDED `CentralHypothesis` carries its OWN
  `alpha`/`mde`/`funded_power_rung` — content-addressing alone does NOT catch a manifest frozen powered
  for rung A while the bound hypothesis commits a different MDE/alpha/rung (incoherent but well-hashed).
  FIX: `_assert_hypothesis_rung_coherent(hypothesis, funded_rung)` asserts `hypothesis.mde ==
  funded_rung.mde` AND `hypothesis.alpha == funded_rung.alpha` AND `hypothesis.funded_power_rung ==
  funded_rung` (the field is required + pydantic preserves the `FundedPowerRung` subclass, so `==` is
  exact). Wired into `freeze_preregistration` as GUARD 1b — runs AFTER the funded-rung guard but BEFORE
  the alpha draw, so an incoherent pair fails closed (`IncoherentPreRegistrationError`) WITHOUT spending
  alpha — AND re-checked in `assert_unmodified` (against the manifest's own `funded_rung`), so a
  hand-built incoherent manifest is flagged even with a matching mde/alpha mirror + no external
  identity. A COHERENT caller (ab_runner builds `CentralHypothesis(mde, funded_power_rung=rung)` then
  freezes that same rung) is unaffected.
- EV20. **`is_funded` short-circuits on the rung's OWN verdict (M15-R1-02 fix).** A degenerate
  underpowered rung (budget can't fund one task-repeat/arm) carries `funded_budget_microusd == 0`, so
  the naive `cost <= budget` read `0 <= budget` as funded. FIX: `is_funded` returns False immediately
  when `rung.underpowered or not rung.funded` (read via `getattr` defaults so a base `PowerRung` w/o
  those fields still hits the cost check / ValueError path). The rung's own verdict is authoritative.
- EV21. **Alpha-look TOCTOU closed ATOMICALLY (M15-R1-03 decision: atomic, not documented).** Chose the
  atomic-ledger fix over the documented-assumption fallback: it is low-risk (all `GlobalRecords` writes
  already serialize through `self._lock`; no existing test calls `record_alpha_look`/keys on its return)
  and removes the foot-gun entirely rather than relying on a single-threaded harness comment. Added
  `GlobalRecords.try_first_alpha_look(...) -> bool`: an `INSERT ... SELECT ... WHERE NOT EXISTS` at
  `look_index==1` under ONE lock acquisition (the `(eval_set_identity, decision_id, look_index)` PK +
  the WHERE-NOT-EXISTS make it insert-if-absent), returning True iff THIS call recorded the first look.
  `freeze` now keys the `AlphaAlreadyDrawnError` rejection on that boolean — the prior
  `alpha_looks_spent()`-then-`record_alpha_look()` check-then-record (two acquisitions) is gone. Legacy
  `record_alpha_look` (append + return total) is left intact for other callers.
- EV22. **Pilot no-treatment-arm fallback is a BASE-RATE substitution, flagged (M15-R1-04 fix).** When
  no treatment arm is supplied, `run_pilot` sets `effect_size_variance = control_resolution_variance`;
  added a comment that this is a base-rate-for-effect-size substitution VALID ONLY when no treatment arm
  is present, and is NEVER reached on `DEFAULT_PILOT_ARMS` (which includes FOUNDRY, the treatment arm —
  the real per-task FOUNDRY−CONTROL difference variance is computed in the main branch).
- EV23. **M15 integrity-trio fix roll-up: ruff + mypy clean; full suite 548 passed / 1 skipped (incl.
  test_eval_ab_runner.py — a coherent prereg still freezes + validates end-to-end).**

## Evaluation — powered 3-arm A/B runner (T-M15-07)
- EV16. **`foundry/evaluation/ab_runner.py::run_ab(frame, prereg: PreRegistration, *, seed=0) ->
  ABResult` (frozen) is the confirmatory A/B the decision table consumes.** ORDER MATTERS — the
  cite-frozen-prereg gate is FIRST: `run_ab` calls `assert_unmodified(prereg)` (EV14) BEFORE any slice
  check or arm run, so a tampered manifest (mde/alpha mirror divergence, or a cited-identity drift)
  fails closed `TamperedPreRegistrationError` before work begins. It reads the funded rung's
  `(n_tasks, k_repeats)` + committed `alpha`/`mde` FROM the manifest, never the call site. SCORED-SLICE-
  ONLY: runs `frame.scored_slice` via `run_arm` (EV8), asserts it is disjoint from `frame.pilot_slice`
  AND `scored.identity == prereg.scored_eval_set_identity` (binds the run to the registered
  frame/partition — EV7) else `ScoredSliceContaminationError`; fixtures handed to `run_arm` are
  RESTRICTED to scored ids so a stray lookup can't touch a pilot task (mirrors `run_pilot`, EV11).
  `ABResult` API: per-arm `ArmReport` (UNCONDITIONAL `itt_pass_rate` = `itt_pass_rate(result)`,
  `resolved_count`, `attempted == n*k`, `total_cost_microusd`, `simulated`) for all THREE arms
  (`AB_ARMS = (FOUNDRY, BASELINE_SINGLE_AGENT, SINGLE_AGENT_PLUS_GATES)`); a PRIMARY
  `BootstrapComparison`; a Holm-corrected `secondaries` tuple; `provenance` carried (`dry_run =
  provenance != SCORED`). NB ITT is reported on EVERY arm regardless of completion (no collider
  conditioning, §2/§6).
- EV17. **PAIRED CLUSTER BOOTSTRAP resamples TASKS, not runs (§2 — the TASK is the inferential unit).**
  `_paired_diffs(treatment, control)` collapses each task's k repeats to ONE per-task resolution rate
  (`_per_task_rates`) FIRST (so n×k is never mistaken for independent draws), pairs FOUNDRY−BASELINE
  WITHIN each common task, ordered by task_id. `_bootstrap_diff_means` draws `len(diffs)` task-level
  paired diffs WITH REPLACEMENT per resample (cluster == task) and takes the mean; the CI is the
  `[alpha/2, 1-alpha/2]` percentiles (`BootstrapComparison.ci_low/ci_high`), `significant_at_alpha` iff
  the CI EXCLUDES 0, `meets_mde` iff `point_estimate >= mde`. CONSTRUCTED-DATA behavior (tested via
  hand-built `ArmResult`s of known per-task rates): all-tasks FOUNDRY=1/BASELINE=0 ⇒ every diff 1.0 ⇒
  CI entirely > 0 ⇒ significant; identical per-task rates in both arms ⇒ every diff 0.0 ⇒ bootstrap is
  a point mass at 0 ⇒ CI=[0,0] ⇒ NOT significant. DETERMINISM: the RNG is `_seeded_rng(seed, *labels)`
  = `random.Random(int(content_digest({seed, labels})[:16],16))` — NOT `random.Random((seed,str))`
  (Python `hash()` of a str-containing tuple is process-randomized, L7) — so same `(frame, prereg,
  seed)` ⇒ identical CI + identical frozen `ABResult`.
- EV18. **HOLM–BONFERRONI step-down over the SECONDARY family (`_holm_bonferroni(raw_ps, *, alpha)`
  -> (adjusted, significant) in ORIGINAL order).** Sort p ascending; the rank-`i` p is rejected iff it
  AND all smaller clear `alpha/(m-i)` — the FIRST acceptance halts ALL further rejections (true
  step-down: a later tiny p after an early accept is NOT rejected). Adjusted-p is the cumulative-max of
  `min(1,(m-i)*p)` (monotone non-decreasing in rank, the standard reporting form; `holm_adjusted_p >=
  raw_p`). FWER-controlling (conservative on the positively-correlated bootstrap endpoints — §2 notes
  the studentized max-T alternative; Holm is the shipped choice here). Secondary family =
  {FOUNDRY-vs-+gates resolution, FOUNDRY-vs-BASELINE efficiency (resolutions-per-USD, paired by task)};
  each gets its own seeded two-sided bootstrap p (`_bootstrap_two_sided_p` = 2·min tail mass either
  side of 0). Worked-example test pins the classic `[0.005,0.01,0.03,0.04]`@α=.05 ⇒ reject the first
  two, ACCEPT at 0.03, and 0.04 stays non-significant (blocked by the step-down even though
  0.04<0.05). Tests: 17, all green; full suite 543 passed/1 skipped. HONESTY: simulated baseline arms —
  a dry-run `ABResult` asserts NOTHING about real Foundry-vs-baseline perf; `provenance`/`dry_run`
  carried so a consumer can tell a rehearsal from a funded run.

## Evaluation — role-ablation harness (T-M15-09)
- EV24. **`foundry/evaluation/ablation.py::run_ablation(frame, *, roles=DEFAULT_CORE_ROLES, seed=0,
  k_repeats=2, threshold=0.02, contribution=None, cost=None, use_pilot_slice=False) -> AblationResult`
  (frozen) answers roadmap P-F2 "do roles earn their place?".** For each `Role` in the core set
  (PM→ARCHITECT→SENIOR→QA) it runs the eval over `frame.scored_slice` (or pilot) with the FULL core vs
  that role ABLATED, emitting a per-role `RoleAblationDelta` (`full_rate`, `ablated_rate`, `delta =
  full−ablated`, `full/ablated/cost_delta_microusd`, `earns_place_threshold`, `earns_place`).
  `AblationResult.delta_for(role)` / `.non_earning_roles` are derived; `full_rate` is the shared core
  baseline. EARNS_PLACE RULE: a role earns its place iff `delta >= threshold` (removing it MEASURABLY
  hurts the resolution rate); a removal within the threshold flags it not-earning (`earns_place=False`).
- EV25. **SIMULATED ablation, NOT a real bypass — flagged `simulated_ablation=True` (load-bearing
  honesty).** The shipped Phase-1 graph (`build_supervisor_graph`) is a FIXED 4-role pipeline wired with
  hard `add_edge` + NO node-bypass seam, and the FOUNDRY arm resolves on a single `passed AND open_pr`
  binary that doesn't vary per-role — so a real architecture-less/QA-less run is out of scope here (would
  touch the graph package; task scope is ablation.py + its test ONLY). So each role's contribution is
  modeled as an additive resolution-rate LIFT over `control_base_rate` (DRY-RUN table; SENIOR 0.20 /
  ARCHITECT 0.15 / PM 0.10 / QA **0.0** — a deliberately non-earning role) folded into a per-task
  Bernoulli draw via `content_digest` (L7, never `hash()`). Ablating a role removes its lift AND its
  per-attempt cost (so `cost_delta>0`: removal is cheaper). Swap for real ablated-run deltas behind the
  SAME API + flip the flag when a per-role graph bypass exists (later phase). The conclusion is about the
  METHOD, never a real org-design verdict; `provenance=DRY_RUN`.
- EV26. **COMMON-RANDOM-NUMBERS (paired) draw is what makes the earns-place test crisp.** The uniform
  draw folds `(base_resolution_seed, repeat, seed)` ONLY — INDEPENDENT of the role set — so the SAME
  draw is compared against the full-core vs ablated rate target. A zero-contribution role (QA) therefore
  has IDENTICAL full/ablated rates (`delta == 0` EXACTLY, no sampling noise) → reliably flagged
  not-earning; a positive-contribution role's removal can only LOWER the rate. Determinism: same `(frame,
  roles, seed, k, threshold, tables, slice)` ⇒ identical `AblationResult` (`content_identity()` stable).
  Tests: 15, all green; full suite 578 passed/1 skipped (the `decision_table.py` ruff/mypy errors are a
  SEPARATE uncommitted task's WIP, untouched here). HONESTY: synthetic dry-run — asserts NOTHING real.

## Evaluation — Phase-1.5 decision table (T-M15-08)
- EV28. **`foundry/evaluation/decision_table.py::decide(ab: ABResult, prereg: PreRegistration, *,
  token_reading: TokenSecondaryReading | None = None) -> DecisionRecord` is the §2.5 branch point — PURE
  + DETERMINISTIC (no RNG/IO; same inputs ⇒ identical frozen record).** ORDER MATTERS — the
  cite-frozen-prereg gate is FIRST: `decide` calls `assert_unmodified(prereg)` (EV14) BEFORE any verdict,
  so a tampered manifest fails closed `TamperedPreRegistrationError`; the record binds the re-derived
  `registration_identity`. `DecisionRecord` (frozen) carries: `decision: DecisionClass`, `rule` (cites the
  §2.5 row + why), `primary_significant_at_alpha`/`primary_meets_mde`, `observed_effect`
  (=`primary.point_estimate`), `committed_mde`/`committed_alpha` (from the MANIFEST, not the ABResult), a
  `CostComparison` (FOUNDRY vs BASELINE `total_cost_microusd`, `at_most_equal`),
  `token_axis_available`/`token_reading`, `registration_identity`, `provenance`/`dry_run`.
- EV29. **THREE branch predicates (exact).** (1) PROCEED iff `primary.significant_at_alpha AND
  primary.meets_mde AND cost.at_most_equal` (FOUNDRY cost <= BASELINE — the equal-USD frame is PART of
  the predicate, so a SIGNIFICANT + meets-MDE win that costs MORE is NOT a PROCEED; it routes to the null
  branch). Secondary is irrelevant once primary is positive (§2.5 row 1 "(any)"). (2) Else (a USD-null:
  not sig, OR effect<MDE, OR cost-worse) BOUNDED_OVERHEAD_REDESIGN iff a SUPPLIED `token_reading.positive`.
  (3) Else HARD_KILL (terminal).
- EV30. **NO-NULL-TO-PROCEED + NO-SILENT-RE-POWER are STRUCTURAL invariants, not just branch order.**
  `DecisionRecord.model_post_init` recomputes the PROCEED predicate from the recorded bits and raises
  `ValueError` iff `(decision is PROCEED) != predicate` — even a HAND-FORGED record can't claim PROCEED on
  a null. It ALSO rejects a BOUNDED_OVERHEAD_REDESIGN whose `token_reading` is absent/not `positive`.
  HARD_KILL is TERMINAL: the record exposes NO re-test/re-power field (a test asserts no
  `repower`/`re_test`/`retest` field exists).
- EV31. **TOKEN AXIS — HONEST GAP (load-bearing).** §2.5's redesign fork turns on the
  **equal-effective-task-token** secondary, which carries its OWN pre-registered MDE/alpha/power (§2).
  That secondary is NOT in `ABResult` — its `secondaries` are the +gates *resolution* gating-secondary and
  a *resolutions-per-USD efficiency* comparison, both USD-denominated. Inferring "token-positive" from the
  USD efficiency secondary would FABRICATE the exact signal §2.5 says must be its own powered endpoint. So
  `decide` does NOT manufacture a token reading: the redesign branch is gated behind an EXPLICIT optional
  `TokenSecondaryReading` (its own `mde`/`alpha`/`significant_at_alpha`/`meets_mde` → `positive`); ABSENT
  it, a USD-null is HARD_KILL (the conservative §1 default). `token_axis_available` makes the gap
  auditable. Tests: 15, all green; full suite 578 passed / 1 skipped; ruff + mypy clean. HONESTY: synthetic
  dry-run — `provenance`/`dry_run` flow from the cited manifest's hypothesis; the record asserts NOTHING
  about real Foundry-vs-baseline performance.

## Phase-1.5 EXIT integration test — the honest audit map (T-M15-11)
`tests/test_eval_phase1_5_exit.py` (13 tests; full suite 604 passed/1 skipped, ruff+mypy clean) is the
milestone witness for the docs/roadmap.md Phase-1.5 EXIT criteria. It drives the WHOLE decision gate
end-to-end (`generate_synthetic_frame -> run_pilot -> compute_power_rung -> freeze_preregistration ->
run_ab -> decide -> run_ablation -> build_scorecard`) on synthetic fixtures and asserts coherence +
honesty. HONEST MAP of what is GENUINELY end-to-end vs a representative/simulated stand-in:
- EV35. **Criterion->test map + which stages are real vs stand-in.** (1) FULL PIPELINE
  `test_exit_1_*` runs every stage live and asserts a real `DecisionClass` falls out. (2) PILOT-FIRST +
  DISJOINT + READ-ONLY `test_exit_2_*`: pilot/scored slices share no task and exhaust the frame; a
  re-run leaves `scored_slice.identity` UNCHANGED + the `PilotResult` identical (deterministic prefix-cut
  — pilot never re-draws/selects scored tasks). (3) ALPHA-ONCE `test_exit_3_*`: after one
  `freeze_preregistration` the `(scored_eval_set_identity, decision_id)` look is spent
  (`alpha_looks_spent==1`); a second freeze of the SAME pair against the SAME `GlobalRecords` raises
  `AlphaAlreadyDrawnError` (still ==1); a different `decision_id` draws its own. (4) CITE-PREREG
  `test_exit_4_*`: a tamper rebuilt via `prereg.model_construct(mde+=0.05)` (skips validation so the
  diverged mirror survives) makes BOTH `run_ab` and `decide` raise `TamperedPreRegistrationError`; a
  prereg bound to a DIFFERENT frame's scored slice raises `ScoredSliceContaminationError`. (5)
  UNCONDITIONAL ITT `test_exit_5_*`: every `scorecard.arm_score(arm).attempted == n_scored_tasks *
  rung.k_repeats == ArmReport.attempted` and `itt_pass_rate == resolved/attempted` (never
  resolved/completed). (6) BOTH BRANCHES `test_exit_6_*` — see EV36. (7) HONESTY `test_exit_7_*`: the
  DRY-RUN scorecard's `honesty_banner()` contains DRY-RUN + SYNTHETIC + SIMULATED + NOT/REAL and is
  `render()`'s FIRST line; the SCORED-provenance banner is non-empty too (ORACLE-SCORED + UNCONDITIONAL),
  also first; `ablation_simulated is True`.
- EV36. **Reaching BOTH PROCEED and HARD_KILL deterministically — the load-bearing asymmetry.** The
  harness's SIMULATED FOUNDRY arm costs MORE per attempt than the baseline stand-in
  (`_SIMULATED_COST_MICROUSD`: FOUNDRY 1.2M vs BASELINE 0.8M), and the equal-USD cost test
  (`FOUNDRY <= BASELINE`) is PART of the PROCEED predicate — so a naturally-run dry-run frame can NEVER
  PROCEED. HARD_KILL is therefore reached via the GENUINE full pipeline (`_drive_full_gate` -> `decide`).
  PROCEED is reached by driving the gate's REAL branch logic (`decide`) on a hand-built `ABResult` that
  CITES a real frozen prereg (`registration_identity == prereg.registration_identity()` so
  `assert_unmodified` still passes) with significant + meets_mde + FOUNDRY-cost<=BASELINE. Both verdicts
  come from the same `decide`, proving the gate is a real branch (not hard-wired). A third test pins the
  two as distinct classes (PROCEED via cost-ok; HARD_KILL via a cost-WORSE win — same logic).
- EV37. **The pilot is REAL but its rung-sizing inputs are a representative stand-in (`_sizing_pilot`).**
  `run_pilot` genuinely runs end-to-end (its provenance=PILOT/scored=False/disjointness/read-only are all
  asserted), but on a TINY dry-run frame the simulated arms SATURATE (`control_base_rate==1.0`, zero
  between-task variance), which drives `compute_power_rung`'s `required_n` to 0 -> an invalid `n_tasks=0`
  rung (a synthetic-frame artifact, NOT a methodology bug; `power.py` is out of this task's scope). So the
  power calc is fed a NON-DEGENERATE representative `PilotResult` (base_rate 0.2, var 0.01 — the same
  values every shipped power/prereg test uses) carrying the LIVE pilot's bound `eval_set_identity`. The
  sizing ARITHMETIC is genuinely exercised; only the variance/base-rate INPUTS are the representative
  stand-in. The FOUNDRY arm wraps the shipped pipeline keyless (D1-D3), so frames are kept TINY
  (`n_tasks<=12`, `k_repeats=1`) for runtime. HONESTY: synthetic dry-run — asserts NOTHING about real
  Foundry-vs-baseline performance; the dry-run banner is the load-bearing disclosure (criterion 7).

## Evaluation — decision-gate scorecard slice + unconditional ITT reporting (T-M15-10)
- EV32. **`foundry/evaluation/scorecard.py::build_scorecard(ab: ABResult, decision: DecisionRecord, *,
  ablation: AblationResult | None = None, prereg: PreRegistration | None = None) -> Scorecard` (frozen) is
  the PRESENTATION layer over the confirmatory artifacts — it RECOMPUTES NO statistics.** It assembles the
  Phase-1.5 decision-gate slice (docs/evaluation.md §7): per-arm `ArmScore` (UNCONDITIONAL ITT), the ONE
  confirmatory `PrimaryScore` (FOUNDRY vs BASELINE, equal-USD), the `DecisionClass` + `rule` carried
  VERBATIM, and an optional role-ablation summary (`RoleAblationLine` tuple). `Scorecard.arm_score(arm)`,
  `.honesty_banner()`, `.render()` are the API. NO `assert_unmodified` here — the integrity gate already
  fired inside `run_ab`/`decide`; the scorecard only presents their verdict (it carries
  `decision.registration_identity`). Pure/deterministic: same inputs ⇒ identical frozen `Scorecard` +
  identical `render()` string.
- EV33. **UNCONDITIONAL ITT is preserved by COPYING, never recomputing.** Each `ArmScore.itt_pass_rate`
  and `.attempted` are copied STRAIGHT from the `ArmReport` (`itt_pass_rate`, `attempted` = n_tasks×k); the
  scorecard never divides resolved/completed and never shrinks the denominator (the §2/§6 collider bias;
  §7 reports ITT unconditionally). The equal-USD `PrimaryScore` costs + `cost_at_most_equal` are read from
  the authoritative `decision.cost` (the SAME `CostComparison` bit the PROCEED predicate turned on), not
  recomputed from arm costs — so the scorecard and the verdict can never disagree.
- EV34. **Mandatory dry-run/honesty banner, rendered FIRST (load-bearing — §0/§4).**
  `Scorecard.honesty_banner()` is NON-EMPTY for both provenances; for a dry-run it states this is a
  METHODOLOGY DRY-RUN on synthetic fixtures with SIMULATED baseline/ablation arms — NOT real evidence —
  and (per §0/§4) that the primary is oracle-scored so a self-judge seam is NEVER reported as independent
  validation; `render()` prints the banner as its FIRST line. `provenance`/`dry_run` flow from the
  `DecisionRecord` (which carries them from the cited manifest's hypothesis). SCOPE: decision-gate slice
  ONLY — the Holm–Bonferroni secondary FAMILY as a published headline and the optimization Pareto front
  are OUT of scope (Phase-6, not built here). A HARD_KILL scorecard renders the KILL verbatim (no
  proceed-spin). AMBIGUITY RESOLVED: the brief's "§0.2" self-judge rule maps to the §0/§4 principle
  (primary oracle-scored, no LLM/self-judge in the confirmatory verdict — there is no literal §0.2
  heading); and the banner's dry-run-vs-scored text is driven by the DECISION's provenance (from the
  prereg hypothesis), not the ABResult's. Tests: 13, all green; full suite 591 passed / 1 skipped; ruff +
  mypy clean. HONESTY: synthetic dry-run — asserts NOTHING about real Foundry-vs-baseline performance.

## Evaluation — eval safeties: reward-hacking / eval-lie / nondeterminism (T-M6-04)
- EV40. **`foundry/evaluation/safeties.py` is the FAIL-CLOSED defense that protects the SCORECARD from
  being gamed (docs/evaluation.md §10) — PURE/DETERMINISTIC over structured eval signals (no RNG/IO/
  model calls; a flaky safety check could not be trusted to catch a flaky eval).** API: three detectors
  + a combinator, all returning frozen records. `SafetyKind` (REWARD_HACKING / EVAL_LIE /
  NONDETERMINISM); `SafetyFinding(kind, subject, detail)`; `EvalSafetyReport(subject, findings)` whose
  `.clean` is the AND of every check (`clean == not findings`; `.invalidated` is its complement) — ANY
  finding INVALIDATES the result so it can never be published clean. `check_arm_safeties(runs, *,
  self_reports=None, discriminator=None) -> EvalSafetyReport` runs all three; omitted inputs contribute
  NO findings but do NOT fabricate a pass (the present signals still fail closed).
- EV41. **REWARD-HACKING = metric moved without the work — grounded on the ORACLE, never the suite's own
  green bit.** `detect_reward_hacking(report: DiscriminatorReport, *, subject)` REUSES the gamed-suite
  discriminator (`foundry/gates/discriminator.py`): `report.passed` ⇒ no finding; else one finding per
  distinct evidence — SURVIVING held-out mutants (suite green but tested nothing real), a `kill_score <
  required_kill_ratio` with no named survivor, OR violated behavioral invariants. A non-passing report
  with NO itemized reason STILL emits one finding (fail-closed — a non-passing oracle signal never slips
  through clean). A gamed suite passes its OWN tests by construction, so only the held-out-mutant +
  invariant oracle (the agent can't see it coming) separates genuine work from metric-gaming — a lie
  can't fool the oracle.
- EV42. **EVAL-LIE = a SELF-REPORT of success the ORACLE contradicts; the oracle wins, the self-report
  can NEVER set the score.** `detect_eval_lies(outcomes, *, self_reports: dict[(task_id,repeat), bool])`
  re-derives the oracle verdict from ground-truth (`produced_output == expected_output`, a `None`
  produced never matches — mirrors `harness._score`, §4) and flags iff the agent CLAIMED success the
  oracle did NOT grant. ASYMMETRIC BY DESIGN: it guards ONLY the score-inflating direction — a false
  SUCCESS claim is a lie; an under-claim (claimed fail, oracle resolved) is NOT (the oracle still wins
  and sets the score). An absent self-report = no claim. The `self_reports` map IS the untrusted channel;
  the assertion is that there is no path from it to the score.
- EV43. **NONDETERMINISM = differs across SAME-seed/SAME-input re-runs (flaky).** `detect_nondeterminism(
  runs: Sequence[ArmResult])` compares each later run to runs[0] per `(task_id, repeat)` on the
  score-bearing fields (oracle `resolved` bit, terminal `status`, `produced_output`) — any disagreement
  is flaky/untrustworthy (the §7 k-run averaging assumes per-run reproducibility GIVEN a fixed seed). A
  MOVED attempt SET (a missing/extra unit — denominator moved) is itself a violation. <2 runs is
  vacuously clean. GUARD: `_assert_comparable` RAISES `ValueError` unless the runs share `(arm,
  slice_identity, seed, budget_usd_microusd)` — comparing different runs would manufacture a FALSE flake.
- EV44. **CONSUMPTION + scope.** factory_eval (the A/B / pilot harness) and the scorecard consume these:
  the arm's re-runs (the harness's `ArmResult`/`TaskOutcome`, EV8), the discriminator's
  `DiscriminatorReport`, and the (untrusted) self-report map flow into `check_arm_safeties`; an
  invalidated `EvalSafetyReport` blocks that arm result from reaching the scorecard as clean (it is the
  pre-publication integrity gate, complementing the §6 collider/ITT rules the scorecard already honors).
  Tests: 28, all green; full suite green (1 pre-existing skip). DoD ruff/mypy were clean on the two owned
  files (concurrent untracked-WIP files `agent_validity.py`/`maintenance.py`/`test_eval_integrity.py`/
  `test_eval_suites.py` carry their OWN unrelated ruff/mypy errors — NOT this task's). HONESTY: operates
  over the DRY-RUN synthetic signals; asserts NOTHING about real Foundry-vs-baseline perf — it protects
  the eval's INTEGRITY only.

## ArtifactStore — the real content-addressed body store (T-M1-14b, DECISIONS D6)
- AS1. **`foundry/durability/artifact_store.py::ArtifactStore` closes the emit/resolve gap.**
  `emit_artifact` MINTED a `cas://sha256/<content_identity>` handle but DISCARDED the body, so
  every `resolve`/`resolve_blob` consumer (senior, qa, gate_wiring) only worked against a SYNTHETIC
  resolver — a real run resolved NOTHING (fail-closed). API: `put_body(model)->uri` (uri ==
  `cas://sha256/<model.content_identity()>`, IDENTICAL to the handle uri; serialized via the
  allowlist `foundry_serde()`, never pickle — I5); `get_body(uri)->FrozenModel` (FAIL-CLOSED:
  `UnknownArtifactError` on unknown uri AND if the payload didn't round-trip to a `FrozenModel` — a
  non-allowlisted type degrades to a `dict` under the serde, rejected); `put_blob(bytes|str)->uri` /
  `get_blob(uri)->bytes`; and callable VIEWS `resolve(artifact)->Any` (= `get_body(artifact.uri)`) +
  `resolve_blob(uri)->bytes` (= `get_blob`) matching the seams gate_wiring/factory_graph expect.
  In-memory dict-backed (dev/smoke); a durable filesystem/Postgres CAS is a prod ops concern (§0.3).
- AS2. **`put_blob` keys by the RAW `hashlib.sha256` of the bytes — EXACTLY `Sandbox.put_blob`, NOT
  `content_digest`.** `content_digest` rejects `bytes` (canonical-JSON only). So a diff blob mirrored
  sandbox->store keeps the SAME `cas://sha256/` uri the FeatureDiff's `diff_uri` names, and a
  store-backed `resolve_blob(diff.diff_uri)` reads it end to end (resolved the loose "<content_digest>"
  wording in the task in favor of the round-trip-with-sandbox constraint).
- AS3. **`emit_artifact(model, kind, produced_by, seq, *, store=None)` is backward-compatible.**
  `store=None` ⇒ mint-only (EXACTLY the historical behavior — the synthetic-resolver tests are
  untouched; the change is purely additive). `store=rt.cas` ⇒ ALSO `store.put_body(model)`; the handle
  uri MUST equal the stored uri (both are the content identity — asserted). `AgentRuntime.cas:
  ArtifactStore | None = None` carries it; all product/architecture/quality emit sites pass
  `store=rt.cas` (`rt` is in scope in every node closure).
- AS4. **The junior's FeatureDiff handle uri carries the idempotency KEY (`cas://feature_diff/<key>`),
  NOT `content_identity` — so use `store.put_body_at(handle.uri, diff)`, not `put_body`.** Otherwise
  `resolve(feature_diff_handle)` = `get_body(handle.uri)` would miss the body. The junior also mirrors
  the adapter's diff BLOB out of the per-task sandbox into `rt.cas` BEFORE `sandbox.destroy()` (arch
  §2(d): no reuse kills the sandbox blob store) via `cas.put_blob(sandbox.get_blob(result.diff_uri))`
  — same raw sha256, so `mirrored == result.diff_uri` (asserted). Both store writes are guarded on
  `rt.cas is not None` so the synthetic-resolver path is unchanged.
- AS5. **Every typed BODY a node emits must be in `serde.FOUNDRY_MSGPACK_MODULES`** (plus the enums it
  carries: Criticality/TaskStatus/OracleKind) or `get_body` fail-closes (the allowlist IS the trust
  posture). The cli (T-M1-15) / factory_graph wire `rt.cas.resolve` / `rt.cas.resolve_blob` as the REAL
  `resolve`/`resolve_blob` in place of the test synthetic (SUP7).

## Factory graph — top-level compile + assistant-mode emit (T-M1-14)
- FG1. **`build_factory_graph(rt, *, runner, resolve, resolve_blob, source_dir, checkpointer=None,
  index=None, max_fix_attempts=3) -> CompiledStateGraph[FactoryState]`** mounts
  `build_supervisor_graph(...).compile()` as a single subgraph node `factory`, appends an
  assistant-mode `emit` node, then END: `START -> factory -> emit -> END`, and compiles with the
  injected `checkpointer`. A COMPILED subgraph is added to `add_node` DIRECTLY (no mypy seam); only the
  plain `emit` closure needs the SG2/IM5 concrete-`def` — and a thin local `def` already IS concrete,
  so no `cast` (mypy clean as-is). `CompiledStateGraph`/`BaseCheckpointSaver` imports stay
  TYPE_CHECKING-only.
- FG2. **The emit node is the SINGLE Phase-1 external EFFECT, expressed ONLY as an `actions` write
  (skeleton.py mirror) — never a GitHub/network call.** Guard FIRST on terminal gate PASS
  (`terminal_gates_pass`: newest-by-`Seq` per name on the trust-filtered `gates`; Spec AND Build AND
  Test must each be PASS — `review` is advisory, the deterministic gates own the release bit, L5). A
  halted/failed run (latch / ERROR / bound spent => any terminal gate non-PASS or missing) returns
  `{"phase":"halted"}` and emits NOTHING. Reading only `gates` means an unattested status-flipped copy
  can't trick the guard (merge_gates already dropped it).
- FG3. **Idempotency key = `content_digest({"run_id":..., "artifact_uri":...})` (L7 — never `hash()`/wall
  clock), prefixed `open_pr:`.** PR subject = the newest-by-`Seq` `feature_diff` handle (NOT
  `artifacts[-1]`; `merge_by_uri` folds by insertion order, QS8). On kill-then-resume LangGraph re-folds
  the committed `emit` write, the emit node re-derives the SAME key, and `union_actions` (keyed by
  `idempotency_key`) coalesces => exactly one `open_pr` persists across both invokes. The resume test
  re-`invoke`s the SAME `thread_id` against a durable `FoundryCheckpointer().saver` and asserts
  `len(open_pr actions) == 1` with an unchanged key (cli T-M1-15 reuses this compile+emit path).

## CLI smoke entry — the dev-wiring assembly (T-M1-15; exit test T-M1-16 reuses it)
- CLI1. **`foundry/cli.py::run(request, *, thread_id="cli-run", resolve=None, resolve_blob=None,
  diff_body=_PASSING_DIFF, max_fix_attempts=3) -> RunResult`** assembles the FULL dev wiring with the
  test doubles (NO live API key, NO `claude -p`, NO network) and runs `build_factory_graph(...).invoke(
  {"request": request}, config)`. `RunResult` (frozen dataclass) reports `passed`, `phase`,
  `artifact_uri` (the reviewed FeatureDiff handle = PR subject), `open_pr_idempotency_key`,
  `open_pr_payload_uri`, and `gate_statuses` (newest-by-name attested status per gate) — handles/ids
  only, no bodies (L4). `main(argv=None)` joins argv into the request (so `python -m foundry.cli "build
  a ..."` works), prints `RunResult.summary()`, returns exit 0 on clean PASS / 1 halted / 2 usage.
- CLI2. **The wiring (mirror of test_factory_graph/test_supervisor; D1-D6/AS5/GW2/SUP7):** an in-memory
  `ArtifactStore` on `rt.cas`; `AgentRuntime(proxy=ModelProviderProxy(MockTransport.scripted(SCRIPTS,
  default_verdict="approve"), signer=ZoneBSigner(KEY,KEY_ID)), adapter=StubAdapter(), cas=store)`; a
  real `TwoZoneGateRunner(ZoneBSigner, GlobalRecords, scanner_rev, lineage_rev)`; a
  `DevGateWriteVerifier` installed via `set_gate_write_verifier(...)` with its token RESET in `finally`
  (never leak the contextvar); a dev `FoundryCheckpointer().saver`; a `tempfile.TemporaryDirectory`
  `source_dir`. The verdict_nonce/subject_artifact_uri/state_sha are RUN INPUTS on
  `config["configurable"]` (Q1); `recursion_limit` raised (multi-loop fix runs, SUP7).
- CLI3. **The StubAdapter authors a deterministic NO-OP blob, NOT a passing test suite — so a pure
  `rt.cas`-resolved end-to-end run would yield NO Build/Test PASS.** To drive the SPEC core path to a
  clean Spec/Build/Test PASS the CLI defaults `resolve`/`resolve_blob` to the proven SUP7 synthetic
  KIND-dispatching doubles (faithful body per `handle.kind`; `acceptance_criterion` with
  `security_abuse=True` for Spec coverage; FeatureDiff -> a fixed `_DIFF_BODY_URI`; passing unified-diff
  blob). Both doubles FALL THROUGH to the real `store.resolve`/`store.resolve_blob` for any other kind,
  so a future adapter that authors a real passing suite needs no CLI change. `rt.cas` IS the real store
  (AS5) and is written on emit regardless.
- CLI4. **mypy: `CompiledStateGraph.invoke(...)` returns `dict[str, Any]`, NOT the `FactoryState`
  TypedDict — a plain dict is not assignable to a TypedDict, so `cast("FactoryState", final)` (string
  form; the import is TYPE_CHECKING-only). Annotating the assignment target `final: FactoryState =
  app.invoke(...)` fails [assignment]; cast at the `_result(...)` call site instead.** Same `import
  foundry` / `import foundry.cli` smoke-clean as every module.

## Phase-1 EXIT integration test — the honest audit map (T-M1-16)
`tests/test_phase1_exit.py` (8 tests) is the milestone witness for the docs/roadmap.md Phase-1 exit
criteria. It reuses the SHIPPED dev wiring directly — `foundry.cli.run(...)` for the end-to-end
criteria and a thin `_run_factory` (mirror of test_factory_graph) that retains the `ArtifactStore`
so the quality-layer bodies can be resolved. It imports the CLI's private `_SCRIPTS` /
`_synthetic_resolve` / `_KEY` etc. ON PURPOSE (it is the witness FOR that assembly, not a parallel
copy). Lessons / honesty map for the audit:
- EX1. **What is GENUINELY end-to-end (no stand-in beyond mock-model + dev-HMAC roots):** the GATE
  machinery — Spec/Build/Test run by the real `TwoZoneGateRunner`, signed in zone B, admitted ONLY by
  the real `merge_gates` (assert `runner_attestation is not None` on each — distinct gate names, all
  PASS, on the trust-filtered `gates` channel); the single `open_pr` ExternalAction + its
  `content_digest` idempotency key; kill-then-resume coalescing across a durable
  `FoundryCheckpointer` (same key, exactly one PR); the stable-FAIL latch on an integrated digest; the
  flake-quarantine routing predicate; TestPlan criterion traceability.
- EX2. **HIDDEN/held-out test (criterion 2) works via the materialization seam, NOT a real producer.**
  The `diff_body` is a multi-FILE addition unified-diff carrying BOTH the agent's `test_app.py` AND a
  separate `test_hidden.py`; `materialize_workdir` writes the whole tree and the Test gate's pinned
  pytest runs ALL of it (passed count >= min_passed). The honesty: the StubAdapter is a no-op
  codegen stand-in (CLI3), so the hidden file is INJECTED through `diff_body`/`resolve_blob` (SUP7/
  GW1a), not authored by the producing (scripted) agent — this asserts the gate MACHINERY genuinely
  materializes+runs a hidden oracle, NOT that the stub wrote good code. A CONTROL test
  (`test_exit_2_hidden_suite_actually_gates_the_pass`) proves the hidden suite is load-bearing:
  `app.py` with the wrong value → own test passes, HIDDEN test FAILs → Test FAIL → no PR.
- EX3. **Stable-FAIL-no-PR (criterion 6) is forced with a SYNTAX-error diff, not a collection error.**
  `+def broken( :` makes `app.py` fail `compileall` → Build FAIL on the INTEGRATED `tree_sha` (matches
  `_DIGEST_RE`) → `runner` latches stable-FAIL → halt, NO `open_pr`, `records.stable_fail_latched(
  build_digest,"build")` is True. (Contrast SUP5/cli-smoke: a `import missing_module` diff COMPILES
  but errors pytest → Test ERROR → bounded `fix`, never latches — that is the *halted-not-latched*
  path, already covered by the CLI smoke; do NOT conflate the two.)
- EX4. **Criterion 3 (traceability) is asymmetric — be honest.** The QA node FAILS CLOSED, backfilling
  a synthetic `test::<cid>` per uncovered criterion (Q4), so `TestPlan.test_criteria` /
  `covered_criteria()` are genuinely NON-EMPTY against the resolved criterion ids → the authoritative,
  non-vacuous traceability proof. The SDET node does NOT backfill: `sdet._scenario_criteria` keeps only
  proposed criterion_ids that are KNOWN, and the synthetic resolver mints epoch-dependent ids
  (`AC-001-<epoch>`) the scripted SDET payload cannot pre-name, so `E2EReport.scenario_criteria` is
  present + scenario-keyed but its per-scenario coverage is mock-dependent. Assert the FIELD/shape is
  present for E2E (structural exit criterion); per-scenario coverage FIDELITY is the worker-substrate's
  job. (If you want E2E coverage non-empty too, give SDET the same fail-closed backfill as QA.)
- EX5. **Flake-quarantine (criterion 4) is exercised on the genuine `gate_wiring` predicates, not via
  a contrived pytest flake.** Construct a runner-ATTESTED Test ERROR whose findings carry a
  "flake-quarantine ... (N=2 fail-then-pass split)" marker (signed via `ZoneBSigner.with_attestation`),
  fold it with an attested Build PASS through the REAL `merge_gates` (a `DevGateWriteVerifier` MUST be
  installed on the contextvar or both are dropped fail-closed — easy to miss), then assert
  `flake_quarantined(...) is True`, the digest is NOT latched, and `route_after_gates(...) == "fix"`
  (bounded retry, not a failure-halt). This is the same pattern as
  test_gate_wiring::test_flake_quarantine_does_not_fail_boundary.

## Assurance / Gated mode — Phase-2 artifact schemas (T-M2-01)
The Gated-mode assurance layer (next M2 waves: agents, tiering, sign-off, evidence-pack)
builds on these frozen models. APPEND-ONLY additions to the existing modules; reused where
adequate.
- AS1. **security.py — REUSED, not duplicated:** AppSec already has `ThreatModel`
  (per-item oracle bindings + `residual_item_ids`/`has_residual()`) + `SecurityTriage`
  (advisory); Supply-Chain already has `SupplyChainReport` (`trust_root_touched` → HITL);
  Audit already has `SBOMRef` (the SBOM handle). **ADDED `AuditFinding`** — the missing
  Audit/Compliance ADVISORY triage (agents.md L5 emits findings + license_report +
  compliance_map; only the SBOM handle existed): `run_against`/`sbom_uri`/`severity`
  (reuses `Severity`)/`findings` + optional `license_report_uri`/`compliance_map_uri`
  CAS handles, `version`/`supersedes`. Advisory ONLY — the Audit verdict (SBOM/CVE/
  license) is runner-re-derived + signed (gov §1c, L5). No new GateResult/verdict type.
- AS2. **governance.py — `RiskTier = ChangeRiskTier` (ALIAS, not a new enum):** the §1e
  tier (LOW/STANDARD/HIGH) already existed as `ChangeRiskTier`; tiering + triage share ONE
  enum (L3, no parallel type). Tests assert `RiskTier is ChangeRiskTier`.
- AS3. **ADDED `TierDecision`** (gov §1e): `tier`/`rationale`/`run_against` + advisory
  `gate_set` (scoped family) + `two_person_release`/`forced_high` flags + `version`/
  `supersedes`. Carries `relaxes_integrity()` → ALWAYS False (an asserted invariant, not a
  comment): tiering scales assurance DEPTH + HITL only, NEVER admission/integrity. It is a
  TRIAGE record — mis-tiering is caught by deterministic pattern checks + HITL, not by this
  record bypassing a gate. Distinct from `TriageDecision` (route + seeded budget); this is
  the standalone tier record the tiering wave supersedes independently.
- AS4. **ADDED `SignOffRecord`** (gov §1e/§6, class-4): approver-of-record sign-off.
  `subject_artifact_uri`/`state_sha`/`approver`/`role`/`approved`/`server_nonce` (per-approval
  challenge, gov §6)/`presented_evidence_hash` (hash of the EXACT pack shown — what-you-see-
  is-what-you-sign) + `meaning_of_signing`. **`judgment_provenance: JudgmentProvenance`
  (REQUIRED, never defaulted)** is the self-judge stamp for the chosen profile (gov §1e
  solo-lineage) — SELF is recorded, never hidden; the MODEL never supplies it (proxy/runner
  stamps from the active profile, mirrors PR3). It IS part of content_identity, so a SELF vs
  INDEPENDENT sign-off are distinct records. NB this is a SEPARATE, richer record than the
  existing `HumanDecision` (which stays the generic L7 HITL log — kept untouched); SignOffRecord
  adds role/state_sha/presented_evidence_hash/judgment_provenance for the Gated release path.
  Import note: governance.py now imports `JudgmentProvenance` from gates.py — gates.py imports
  neither, so NO cycle (verified).
- AS5. **ADDED `EvidencePack`** (gov §6): the independent non-agent-assembled release pack.
  `pack_uri` (the pack's own CAS handle — what a `SignOffRecord.presented_evidence_hash` binds)
  + `run_against` (integrated tree_sha) + `chain_head` (hash-chain head, tamper-evident) +
  tuple handle sets `gate_result_uris`/`artifact_uris`/`sign_off_uris` (default empty tuple —
  list coerces) + optional `scanner_config_rev`/`provenance_predicate_uri` + `version`/
  `supersedes`. Bodies OUT-OF-BAND (DS9/L4); the record carries only content-addressed handles
  + the chain head.
- AS6. **No `Field(default_factory=...)` needed here (S1):** every mutable default is a `tuple`
  literal `= ()`, which is IMMUTABLE, so RUF012 does not trip (S1 only bites on `= []`/`= {}`).
  Tests live in `tests/test_schemas_assurance.py` (mirrors test_schemas.py's CASES table +
  spot-check style); existing test_schemas.py stays green. Gate chain clean
  (ruff/mypy/pytest full suite).

## FactoryState Phase-2 (Gated mode) channels (T-M2-02)
The tiering/sign-off/supervisor M2 waves write these channels (APPEND-ONLY additions to
`FactoryState`; existing channel semantics UNCHANGED — `factory_state.py`/`reducers.py` are
single-owner, collision-hazardous).
- ST1. **Three channels added.** `mode: str` — single-writer ("assistant" | "gated"), stamped
  by the supervisor/factory_graph at run start (mirrors `phase`/`request`, no reducer →
  last-writer-wins). `risk_tier: TierDecision | None` — single-writer (triage tiering, gov §1e);
  the FULL `TierDecision` record rides it (not the bare `RiskTier` enum) so `forced_high`/
  `two_person_release`/`gate_set` travel with the tier. `sign_offs: Annotated[list[SignOffRecord],
  union_sign_offs]` — keyed-union, append-only.
- ST2. **`union_sign_offs = make_union_by_key(_sign_off_key)` keys by `SignOffRecord.
  content_identity()`** — the record carries NO natural domain key (unlike tasks=id /
  actions=idempotency_key / triage=union_key). `content_identity` is a digest over the WHOLE
  canonical record INCLUDING `seq` and `judgment_provenance` (AS4), so SELF vs INDEPENDENT and
  any field difference are DISTINCT records that coexist; re-folding the SAME record coalesces
  (key is a pure function of the record, per-key payload byte-identical). Pure/commutative/
  associative/None-safe/re-fold-idempotent (L8) — proven in `tests/test_state_phase2_channels.py`
  mirroring `test_reducers_properties.py`'s algebra suite. A sign-off is ALWAYS recorded
  (recorded sign-off, no-op HITL in the self-judge profile); this channel has NO admission/trust
  gate (unlike `gates`/`merge_gates`) — every record survives, none dropped.
- ST3. **No import cycle:** `reducers.py`/`factory_state.py` now import from `schemas/governance.py`,
  which imports only `schemas/base`+`schemas/gates`+`state/seq` (leaf) — no back-edge. Channel
  presence/typing asserted via `get_type_hints(FactoryState, include_extras=True)` (keeps the
  `Annotated` reducer metadata). Gate chain clean (ruff/mypy/pytest full suite, 667 passed).

## Security layer (T-M2-04) — ADVISORY-only Layer-5 agent nodes
- SEC1. **Three node factories in `foundry/agents/security/`, all ADVISORY (L5).**
  `make_appsec_engineer_node(rt)` -> a `ThreatModel` handle (STRIDE, per-item `oracle_bindings`,
  `residual_item_ids` -> Threat-review sub-gate) + an advisory `GateTriage(name="security")`;
  `make_audit_compliance_node(rt)` -> an `AuditFinding` handle (SBOM/license/compliance, with
  optional `license_report_uri`/`compliance_map_uri`) + `GateTriage(name="audit")`;
  `make_supply_chain_reviewer_node(rt)` -> a `SupplyChainReport` handle (CI/IaC/lockfile policy
  diff) + `GateTriage(name="supply_chain")`. Each follows AR1-AR4: `rt.proxy.complete(...,
  purpose="ideation", schema=...)`, node-owned `Seq` via `rt.seq`, bodies content-addressed via
  `cas_uri` + `emit_artifact(..., store=rt.cas)` (DS9/L4). NONE writes `gates`; NONE imports
  `anthropic`. The trusted runner computes+signs the Security/Audit/Supply-chain verdicts later
  in the assurance fan-out (gov §1c) — agents only triage (L5).
- SEC2. **Blocking is a TRIAGE-EXPLANATION marker, not a verdict (mirrors criteria_critic).** AppSec
  flags the advisory BLOCKING iff `severity in {HIGH, CRITICAL}` AND `findings` is non-empty (a
  HIGH severity with zero findings has nothing to block on). Supply-chain flags BLOCKING on any
  `policy_violations` OR `trust_root_touched` (the latter also escalates HITL, gov §1e). Audit
  flags BLOCKING on HIGH/CRITICAL findings. The `severity`/`policy_violations`/`trust_root_touched`
  come from the model schema; the node enums-coerce them. The runner still owns the verdict bit.
- SEC3. **A store-resolvable body MUST be on the serde allowlist (D6/I5).** `rt.cas` (=
  `ArtifactStore`) `put_body`/`get_body` round-trips through `foundry.durability.serde`'s
  `FOUNDRY_MSGPACK_MODULES`; a type NOT listed degrades to `dict` on load and `get_body` rejects
  it fail-closed (`UnknownArtifactError: ... did not deserialize to a frozen model`). So adding any
  NEW typed artifact body (ThreatModel/AuditFinding/SupplyChainReport/SecurityTriage/SBOMRef +
  `Severity`) requires appending it to that allowlist — the ONE file outside `agents/security/`
  this task had to touch (the alternative, a synthetic uri->body resolver like the quality unit
  tests, would not satisfy the "resolvable from the store" DoD). The assurance subgraph waves that
  consume these advisories thread `rt.cas.resolve` as the real materialization seam.
- SEC4. **MockTransport: schema-constrained ideation rides `completions=` (DT1/AC3), NOT `verdicts=`.**
  These `complete(..., schema=...)` calls are NOT the verdict channel (their schema isn't
  `VERDICT_SCHEMA`), so script each node's JSON payload on the FIFO `completions=` queue (returned
  verbatim as `text`), sized to the number of `complete` calls. Graph-compat test: the three nodes
  chain linearly in a `StateGraph(FactoryState)`, bodies fold on `artifacts` (merge_by_uri),
  triages on `gate_triage` (union_triage), and `final.get("gates")` is empty (L5). 8 tests, full
  gate chain clean (ruff/mypy/pytest full suite green).

## HITL sign-off + WORM-backed evidence pack (T-M2-07)
- GOV1. **The sign-off RECORD is ALWAYS made in the self-judge profile — the testable invariant.**
  `foundry/governance/sign_off.py`: `record_sign_off(point, *, subject_artifact_uri, state_sha,
  presented_evidence_hash, server_nonce, approver, role, profile, seq, approved=True, liaison=None,
  ...)` (+ `record_design_sign_off`/`record_release_sign_off` wrappers) ALWAYS returns a
  `SignOffRecord`. In `self_judge` the HumanLiaison path is no-op (`deferred_log`) but the record is
  NEVER gated on delivery; provenance is stamped from the PROFILE (`provenance_for_profile`: SELF in
  self_judge, INDEPENDENT otherwise — mirrors `proxy.judge()`'s stamp source, the model never
  supplies it, gov §1e). The record binds the 4 what-you-sign anchors (subject/state_sha/
  presented_evidence_hash/server_nonce, gov §6).
- GOV2. **Fail-closed release: no record ⇒ not released.** `release_allowed(record, *, <4 anchors>)`
  returns False on `record is None`, `not approved`, OR any anchor mismatch (a sign-off for a
  different artifact/state/evidence/nonce can't advance THIS release). `require_release_sign_off(...)`
  is the raising variant (`SignOffError`) for the path that must refuse, not branch. The record is
  content-addressed (`SignOffRecord.content_identity()`) so any bound-field change (incl. the
  provenance stamp) re-keys identity — tamper-evident, and what the evidence-pack chain folds over.
- GOV3. **Evidence pack REUSES the outbox WORM link function — do not reinvent.**
  `foundry/durability/evidence_pack.py` imports `_chain_hash` + `_GENESIS` from
  `foundry/durability/outbox.py` and folds the ORDERED references (gate-result content-identities,
  then artifact `.uri`s, then sign-off content-identities) via `chain_over(refs)` — each ref chained
  as a canonical `{"index": i, "ref": uri}` event so head depends on content AND position.
  `build_evidence_pack(*, run_against, gate_results, artifacts, sign_offs, seq, ...)` returns an
  `EvidencePack` whose `chain_head` is that fold and `pack_uri = cas://sha256/<pack_evidence_hash>`
  (content hash EXCLUDING pack_uri/seq — stable, re-derivable; this is what a sign-off's
  `presented_evidence_hash` binds to, gov §6).
- GOV4. **`verify_evidence_pack(pack, *, gate_results, artifacts, sign_offs, require_sign_off=True)`
  detects tampering two ways (both fail-closed):** (1) the supplied members must reproduce the
  recorded `*_uris` EXACTLY in order (a swapped/removed/reordered gate result, swapped artifact, or
  missing sign-off mismatches), AND (2) the recomputed `chain_over(refs)` must equal `chain_head`
  (out-of-band edit of head/refs breaks it), AND the recomputed `pack_uri` must match (forged
  pack_uri). `require_sign_off` empty `sign_off_uris` ⇒ False. `assert_evidence_pack(...)` is the
  raising variant (`EvidencePackError`). Determinism (L7): chain_head/pack_uri are pure functions of
  `content_digest`/canonical bytes (never `hash()`). Consumed by the supervisor + Gated emit:
  release admission must check BOTH `require_release_sign_off` AND `verify_evidence_pack` over the
  attested gate set. 28 tests; full gate chain clean (ruff/mypy/pytest full suite green: 667 passed).

## Change-risk tiering classifier (T-M2-03)
- GOV5. **`foundry/governance/tiering.py::classify_tier(source, *, capabilities=None, seq,
  run_against="") -> TierDecision` is the DETERMINISTIC triage tier classifier (gov §1e) — pure
  rule-based substring/keyword matching, NO model call (auditable + resume-stable).** `source` is
  EITHER the raw request `str` OR a state mapping (reads `request` + unions `required_capabilities`);
  the explicit `capabilities=` arg unions with state caps (both lowercased). Rules in order: (1) ANY
  security/trust-root/build-CI-IaC-deps/scanner-config/auth-crypto signal (`_SECURITY_TRUST_SIGNALS`),
  ANY abuse/security-significant signal (`_ABUSE_SIGNALS`), OR ANY regulated/domain-pack capability
  (`_REGULATED_CAPABILITIES` = regulated/fintech/healthcare/ml/data/privacy/pii/phi) → **FORCE HIGH**
  (`forced_high=True`); (2) empty/blank request → STANDARD (NOT LOW — ambiguity never silent-
  downgrades); (3) a trivial/low-blast-radius marker (`_TRIVIAL_SIGNALS`: typo/docstring/rename/…) →
  LOW; (4) default → STANDARD.
- GOV6. **Force-escalation is FAIL-CLOSED: when in doubt or on ANY security/regulated signal, HIGH —
  never silently downgrade.** Security signals are checked BEFORE the trivial branch, so "just a typo
  in the crypto helper" → HIGH (a trivial marker can't downgrade a security touch). `two_person_release`
  is set on HIGH iff a REGULATED capability OR a trust-root/signer/gate-config/oracle/scanner-config
  touch (`_TRUST_ROOT_SIGNALS` subset) — distinct-identity sign-off (gov §1e/domains §4), independent
  of diff size. Signal vocabularies are deliberately BROAD (a false escalation costs one extra review;
  a miss ships an un-reviewed trust-root change).
- GOV7. **Tiering scales assurance DEPTH + HITL only — NEVER integrity (gov §1e).** The advisory
  `gate_set` ALWAYS includes the integrity-critical floor `("build","test","security")` at EVERY tier
  (`_INTEGRITY_GATES`); LOW just drops OPTIONAL depth (spec/review/e2e), HIGH adds supply_chain +
  threat_review. `TierDecision.relaxes_integrity()` is `False` by construction; `_decide` ASSERTS it
  before returning (defense in depth — a future schema edit can't silently emit an integrity-relaxing
  tier). The supervisor wires this at triage (stamps the single-writer `risk_tier` channel). 14 tests;
  full gate chain clean (ruff/mypy/pytest full suite green).

## Dependency-scoped re-verification — change-proportional + reuse-soundness (T-M2-06)
- RV1. **`foundry/graph/reverification.py` is a PURE planner — it decides WHICH gates re-run, runs
  NONE.** Two steps: `changed_scope(old_files, new_files, *, old/new_toolchain_digest,
  old/new_scanner_config_rev, ...) -> ChangedScope` (a frozen delta: `changed_files` = added∪removed∪
  content-modified relative-POSIX paths — content, never mtime, drives it (DS4) — plus
  `lockfile_changed`/`toolchain_changed`/`scanner_config_changed`); and `reverification_plan(
  prior_verdicts, changed, *, gates, runner, new_state_sha, scanner_config_rev, scopes=None) ->
  ReverificationPlan` (one `GateDecision{gate_name, rerun, reason, reuse_key, reused_verdict}` per gate;
  `.rerun_gates`/`.reuse_gates`/`.reused_verdicts` views). The assurance subgraph/supervisor consume
  the plan: re-run `rerun_gates` via the runner, fold `reused_verdicts` (real attested priors) forward.
- RV2. **RE-RUN vs REUSE = scope-disjointness + valid attested prior + scanner-config match.** REUSE iff
  (a) the gate's dependency SCOPE is disjoint from the change set AND (b) a prior attested NON-ERROR
  verdict exists AND (c) — for a SCANNER-BOUND gate — its recorded `scanner_config_rev` equals the
  current runner's. Else RE-RUN (fail-closed). A `GateScope` has `reads_sources`+a name-keyed
  source selector (`_SOURCE_SELECTORS`; DEFAULT = generic code/test files, EXCLUDING the gate-grounding
  files criteria.json/changes.diff/lockfiles) + `reads_lockfile`/`reads_toolchain`/`reads_scanner_config`
  booleans. `DEFAULT_GATE_SCOPES`: spec=criteria-only, build/test/security=generic sources (security+
  test also lockfile/scanner-bound), audit/supply_chain=lockfile+diff.
- RV3. **THE KEY DESIGN CALL: the reuse key is the §1f "unchanged input SUB-HASH", NOT the whole-tree
  `state_sha`.** Keying a config-evaluable gate (Spec) on the whole `tree_sha` would re-run it on EVERY
  unrelated code edit (any file change → new tree_sha → key mismatch), DESTROYING change-proportionality.
  So reuse keys on the gate's SCOPED inputs: scope-disjointness IS the scoped-sub-hash-equality test
  (no scoped file/dep changed → that gate's scoped inputs are byte-identical). `runner.reuse_key(gate,
  sha)` (state_sha for class-1; +mutant_rotation_id+scanner_rule_version for class-2) is folded into the
  informational `reuse_key` but the decision is driven by the scope+scanner components directly.
- RV4. **SOUNDNESS (no stale reuse) is structural: reuse requires scope-disjoint AND a valid prior.**
  Impossible to carry a verdict whose inputs changed — a scoped change → in_scope=True → RE-RUN; an
  absent/ERROR/scanner-mismatched prior → RE-RUN. ERROR is never reusable (DS7 infra, not a verdict).
  The scanner guard is SCOPED to `reads_scanner_config` gates so a scanner bump re-runs only the
  scanner-bound family (proportional), never the whole set. `scanner_config_rev` is a PLAIN PARAM (the
  current runner's rev) — DON'T reach `runner._scanner_config_rev` (private; the runner has no public
  scanner-rev property, only `.records`).
- RV5. **Test the GENUINE contract: feed REAL runner-attested priors.** Materialize a tree, run the
  real `TwoZoneGateRunner` over every gate to get signed `GateResult[]`, pass THOSE as `prior_verdicts`;
  reused decisions then carry a verdict with `runner_attestation is not None` (a cached attested verdict
  is still a real verdict, gov §1f). Cosmetic 1-file change → `rerun_gates < all` (STRICT subset proves
  proportionality); lockfile/toolchain bump → bound gates re-run; identical tree+matching priors → ALL
  reuse. 12 tests; full gate chain clean (ruff/mypy/pytest 703 passed).

## Supervisor — Phase-2 (Gated mode) wiring (T-M2-09)
- SP2-1. **`build_supervisor_graph(rt, *, runner, resolve, resolve_blob, source_dir, index=None,
  max_fix_attempts=3, mode="assistant")` — `mode` is the ONLY new param; default keeps the Phase-1
  topology BYTE-FOR-BYTE (back-compat).** `mode="gated"` selects the Phase-2 flow. No new deps: the
  Gated path REUSES the SAME `runner`/`resolve`/`resolve_blob`/`source_dir` for `build_assurance_subgraph`
  (the assurance subgraph needs exactly those). Gated phase order:
  `triage -> product -> architecture -> [design_sign_off] -> spec_gate -> implementation -> quality ->
  [build_test_gate] -> assurance -> [release_sign_off] -> END`. The spec/build+test integrity boundaries
  are UNCHANGED — the sign-off/assurance boundaries are ADDED, never a relaxation (gov §1e). Exports
  `MODE_ASSISTANT`/`MODE_GATED`.
- SP2-2. **Triage tiers IN-LINE (no extra node): gated triage writes `mode` + `risk_tier` (single-writer
  channels).** `classify_tier(state, capabilities=state['required_capabilities'], seq=rt.seq(config,
  "triage","tier"), run_against=state_sha)` — the tier scales the assurance subgraph's DEPTH at run time
  (the subgraph reads `state['risk_tier']`, AS1's `_state_tier`), NEVER integrity (`relaxes_integrity()`
  is False). Assistant triage stamps `mode="assistant"` only.
- SP2-3. **Design/Release sign-offs are ALWAYS-RECORDED (the testable invariant) — the RECORD is
  unconditional; only ADVANCEMENT is gated.** Each boundary node calls `record_design_sign_off` /
  `record_release_sign_off` (self-judge stamp from `rt.proxy.profile`, default `self_judge` ->
  `JudgmentProvenance.SELF`) and writes `{"sign_offs": [record]}`. Anchors bound: subject =
  newest-by-Seq `system_design` (DESIGN_KIND) artifact uri (mirror QS8, NOT `artifacts[-1]`); `state_sha`
  = a RUN INPUT off `config["configurable"]` (Q1); a PROCESS-STABLE `server_nonce` =
  `content_digest({point,subject,state_sha,thread_id})` (L7, never `hash()` — resume-stable);
  `presented_evidence_hash` = `content_digest` over subject+state+sorted attested-gate ids. The Release
  conditional edge `_route_release` RE-DERIVES the SAME anchors and calls `release_allowed(record, ...)`
  — fail-closed: a missing/!approved/mismatched record routes to END WITHOUT emit (no record => no
  release, gov §1/§5). `sign_offs` folds by `union_sign_offs` (dedup by content identity); the release
  record is found by `role == "release-approver"`.
- SP2-4. **The assurance boundary routes ADVANCE/HALT like an M1 gate — verdict from the subgraph's
  internal convergence + the cross-fork latch.** `assurance_node` invokes the compiled
  `build_assurance_subgraph(...)` (which runs its OWN bounded evaluator-optimizer loop internally), then
  `_route_assurance` calls `route_after_assurance(state, config, records=runner.records,
  tier=state['risk_tier'], max_fix_attempts=...)` and maps anything != ADVANCE to HALT->END. So a
  KNOWN-VULNERABLE change (hardcoded secret -> attested Security FAIL -> stable-FAIL latch inside the
  subgraph) makes the boundary HALT -> there is NO `release_sign_off` path to emit (the release node never
  runs; assert `"release-approver" not in sign_offs`). The M1 build/test gates still PASS on that diff
  (it compiles + the test passes) — the vuln is a SECURITY concern caught ONLY in the assurance phase.
- SP2-5. **Test seam = the Phase-1 supervisor doubles (SUP7) PLUS the 3 Layer-5 security-agent scripts
  (keyed by their `"You are the Security / AppSec Engineer"` / `Audit / Compliance` / `Supply-Chain
  Reviewer` SOP markers, free-text completions; AC3/DT1) AND a `uv.lock` ADDITION in the diff body** (the
  assurance Audit gate reads the lockfile inventory, AW2 — without it Audit ERRORs and the boundary never
  advances). Build the gated graph with `mode=MODE_GATED`; assert mode/risk_tier, both sign-off records
  (provenance SELF), the assurance verdicts present, and `release_allowed` admits the recorded release.
  The vuln test swaps app.py for `API_KEY="..."`. Back-compat test: default mode -> no risk_tier, no
  sign_offs, no security/audit/supply_chain verdicts. 5 tests; full chain clean (ruff/mypy/pytest 715
  passed / 1 skipped).

## Factory graph — Gated-mode emit: the WORM-evidence-carrying release (T-M2-10)
- FG4. **`build_factory_graph(..., mode="assistant")` threads `mode` to `build_supervisor_graph(...,
  mode=mode)` AND selects the emit node** (`gated_emit` when gated, else the Phase-1 `emit`). The
  assistant path is byte-for-byte unchanged (back-compat: bare `open_pr` against the FeatureDiff,
  `gate_bearing=False`, no pack). Same `START -> factory -> emit -> END` topology; only the emit closure
  added to `add_node` differs.
- FG5. **The Gated emit guard = `gated_release_admitted(state, config)` — TWO independent fail-closed
  checks.** (1) EVERY required gate PASS, newest-by-Seq on the trust-filtered `gates` channel: the M1
  terminal gates (spec+build+test) PLUS the tier-scaled assurance family from
  `assurance_gate_set(state["risk_tier"])` (gov §1e — Security+Audit floor always; STANDARD adds
  supply_chain; HIGH adds e2e — so a HIGH release that skipped depth can't be admitted). (2)
  `release_allowed(record, ...)` re-derived against the SAME anchors the supervisor's release boundary
  bound (subject=newest DESIGN_KIND uri, state_sha, evidence_hash, nonce). To AVOID DRIFT, the anchor
  derivations live in ONE shared PUBLIC module **`foundry.governance.release_anchors`**
  (`newest_artifact_uri`/`state_sha`/`sign_off_nonce`/`evidence_hash`/`newest_release_sign_off`) imported
  by BOTH `supervisor.py` (`_route_release`/`design_sign_off`/`release_sign_off`) AND `factory_graph.py`
  (`gated_release_admitted`). (M2-R1-01: was a cross-module PRIVATE import from supervisor — promoted to
  public, single source of truth; pure refactor, byte-identical anchors. No test imported the privates.)
- FG6. **The evidence pack is built ONLY on admission, over the ATTESTED members, and TRAVELS as the PR
  payload.** `build_evidence_pack(run_against=<read off an attested verdict's `run_against` — the ACTUAL
  materialized tree_sha the gates ran over, NOT a config state_sha LABEL; M2-R1-02 honesty fix via
  `_attested_run_against`>, gate_results=<newest attested per required gate, sorted by name>,
  artifacts=(newest FeatureDiff,), sign_offs=(release-approver records, by Seq),
  scanner_config_rev=<read off an attested verdict, AS4 — NOT the runner's private attr>, seq=...)`. The
  `open_pr` action's `payload_uri` is the pack's `pack_uri` (`cas://sha256/...`) and `gate_bearing=True`
  — the release carries its evidence (gov §6), distinct from the assistant PR (payload = the diff uri).
- FG7. **IDEMPOTENCY KEY MUST EXCLUDE the pack_uri (the resume-duplication trap).** The pack_uri folds
  in the gate results' `content_identity()`, whose `Seq.emission_epoch` is BUMPED on a resume
  re-attestation, so a pack_uri-keyed action does NOT coalesce -> DUPLICATE PR. Key on the SAME stable
  inputs the assistant key uses (`content_digest({"run_id", "artifact_uri": subject.uri})`); the pack
  still travels as the payload, the key only needs to be process-stable (L7). With the stable key,
  `union_actions` (keyed by `idempotency_key`, later-by-Seq) coalesces the two emissions to one.
- FG8. **Tamper-evidence is verified end-to-end via `verify_evidence_pack`.** The test rebuilds the
  carried pack from final state (asserting `pack.pack_uri == pr.payload_uri` — the pack is a pure
  function of its members + the attested `run_against` so it reproduces; the rebuild reads `run_against`
  off an attested verdict, mirroring the emit, M2-R1-02), then: a genuine-members verify passes; a
  `model_copy(status=FAIL)` on a gate result re-keys its content-identity -> verify False; dropping the
  sign-off (`sign_offs=()`) -> verify False; an out-of-band `chain_head` mutation -> verify False.
  Fail-closed: the vuln run (hardcoded secret -> attested Security FAIL -> latch -> assurance halt)
  never reaches release_sign_off, so `gated_release_admitted` is False -> NO open_pr, NO pack release.
  5 tests; full chain clean (ruff/mypy/pytest 720 passed / 1 skipped). Touched ONLY factory_graph.py +
  the new test.

## Phase-2 (Gated mode) EXIT integration test — the honest audit map (T-M2-11)
`tests/test_phase2_exit.py` (9 tests; full suite 729 passed / 1 skipped, ruff+mypy clean) is the
milestone witness for the docs/roadmap.md Phase-2 EXIT criteria. It drives the WHOLE Gated pipeline
end-to-end over the SHIPPED dev wiring (mirror of test_factory_graph_gated: real ModelProviderProxy
+ MockTransport, dev ZoneBSigner, real TwoZoneGateRunner admitted by the real merge_gates via a
DevGateWriteVerifier, StubAdapter, in-memory resolve/resolve_blob, ArtifactStore, durable
FoundryCheckpointer). Criterion->test + the genuine-E2E-vs-stand-in map for the audit:
- P2X1. **Criterion->test.** (a) known-vulnerable-CAUGHT = `test_exit_a_known_vulnerable_change_is_
  caught_and_halts` (hardcoded secret -> runner-attested Security FAIL -> stable-FAIL latch -> halt ->
  no PR/pack) + the LOAD-BEARING CONTROL `test_exit_a_clean_control_releases_...` (SAME pipeline+wiring,
  only the diff body differs -> CLEAN diff RELEASES one gate-bearing PR -> the halt is the vuln, not a
  hard-wired fail). (b) Design+Release recorded sign-off = `test_exit_b_clean_release_records_both_*`
  (both records on `sign_offs`, provenance=SELF, design.seq < release.seq, release_allowed admits) +
  `test_exit_b_release_allowed_fail_closes_*` (None/!approved/nonce-mismatch/evidence-mismatch all ->
  False — the pure predicate gated_release_admitted re-derives). (c) change-proportional re-verify =
  `test_exit_c_cosmetic_change_reverifies_a_strict_subset` (rerun ⊂ full; spec/audit REUSE the REAL
  attested prior) + `test_exit_c_lockfile_change_invalidates_the_bound_gates` (audit+security re-run,
  code gates reuse). (d) bounded rework = `test_exit_d_persistently_failing_run_halts_within_caps_no_pr`
  (no-lockfile diff -> Audit ERROR -> bounded fix -> HALT at max_fix_attempts=1; invoke RETURNS a
  terminal state, not GraphRecursionError; no PR). (e) HITL volume measured = `test_exit_e_hitl_volume_
  is_observable_and_countable` (count SignOffRecords on `sign_offs`: clean=2 {design,release}, halted<2;
  run-dependent, not a constant). (6) verifiable evidence pack = `test_exit_6_release_pack_verifies_and_
  tampering_breaks_it` (rebuilt pack.pack_uri == pr.payload_uri; genuine members verify; swap-gate /
  drop-sign-off / mutate-chain-head each break verify_evidence_pack).
- P2X2. **GENUINELY end-to-end (no stand-in beyond mock-model + dev-HMAC roots):** the assurance GATE
  machinery for (a) (runner-attested FAIL, `judgment_binding is None and runner_attestation is not None`,
  stable-FAIL latch, fail-closed emit); the (b) fail-closed RELEASE admission (live `gated_release_
  admitted` = terminal+assurance PASS AND `release_allowed` over the recorded Release sign-off); the (d)
  bounded-rework loop (the SHIPPED supervisor/assurance bound, run live — terminates vs hangs); the (e)
  HITL-volume count off the live `sign_offs` channel; the (6) evidence pack chained over the REAL
  attested verdicts + FeatureDiff + Release sign-off. The (c) reverification_plan is fed REAL
  runner-attested priors from the LIVE runner over a materialized tree (genuine producer->planner, RV5)
  — pure planner, not a graph node, so it's exercised against real priors rather than inside the run.
- P2X3. **DEV-MODE STAND-INS (declared, NOT overclaimed — plan §0.3).** (1) StubAdapter CODEGEN is a
  no-op blob, so the integrated tree the gates run over is INJECTED via resolve/resolve_blob (SUP7
  synthetic doubles), NOT authored by the (scripted) producing agents — (a) proves the gate MACHINERY
  catches an injected vulnerable diff, and the clean-vs-vuln CONTROL is what makes that load-bearing.
  (2) MockTransport + dev-HMAC trust roots (ZoneBSigner/DevGateWriteVerifier): the signing/attestation
  DISCIPLINE is real, only key custody is dev-grade (real KMS/SLSA-L3 = the Phase-4 trust root, an ops
  task). (3) SELF-JUDGE HITL (plan §0.2): sign-offs are RECORDED-but-no-op (provenance=SELF), so the
  (e) "HITL volume" is the count of recorded-but-no-op self-judge sign-offs — the honest measurable in
  this profile (flip to a real-HITL profile -> same channel, blocking delivery). (4) SIMULATED
  single-agent A/B: N/A here (that's the Phase-1.5 decision gate, not the Phase-2 assurance pipeline).
- P2X4. **Test-harness gotcha:** two Gated runs in one test (criterion e) need DISTINCT `tmp_path`
  subdirs created BEFORE `_src` (which mkdir's `<dir>/src`) — a nested mkdir on a non-existent parent
  raises FileNotFoundError. Touched ONLY tests/test_phase2_exit.py + this LEARNINGS note.

## Operability (Phase-3) schema foundation (T-M3-01)
- OPS1. **`foundry/schemas/operations.py` is APPEND-ONLY and single-owned; Phase-3 records were
  ADDED, the four Phase-1/2 records (`Pipeline`/`IaCProposal`/`DeployPlan`/`Release`) PRE-EXISTED
  and were not touched.** The 7 M3 ops modules (run_status, operator, store_resilience, capacity,
  hitl, trusted_component, upgrade) build on these shared records — don't invent parallels (L3).
- OPS2. **Records added (all FrozenModel, extra=forbid, `seq` on every channel record, `*_uri` CAS
  handles, S1 `default_factory` for mutable defaults, integer micro-USD for money):**
  §2 liveness — `RunStatus` enum (RUNNING/PAUSED/WEDGED/HIBERNATED/DONE/FAILED/QUARANTINED;
  **PAUSED vs WEDGED is the load-bearing distinction** — paused = legitimate $0/hr wait, wedged =
  no-heartbeat-past-derived-deadline alert), `PauseReason` enum (store_outage /
  trusted_component_unavailable / hitl_wait / capacity / non_convergence / latched_by_sibling_fork /
  operator), `RunStatusRecord` (status+reason+`awaiting`+`last_progress_seq/ts`+derived
  `expected_next_transition_deadline_ms`+`cost_rate_microusd_per_hr`+`state_footprint_bytes`;
  `.is_alertable()` == WEDGED). §1 operator — `AlertSeverity`, `SLOTarget` (`objective_micro` in
  millionths so ratio SLOs carry no float), `SLO`, `Alert` (maps 1:1 to the deterministic monitors,
  fleet-level when `run_id is None`). §6 capacity/fairness — `TenantQuota` (carries the
  `cost_rate_cap_microusd_per_hr` fleet slice + noisy-neighbor caps), `QueueEntry` (per-`lineage`
  backpressure dim), `FairnessDecision` (admission-side throttle: `admitted=False` queues, never
  kills; carries fleet rate+cap). §7 HITL — `ReviewerQueueEntry` (`decision_class` so drift
  decisions don't serialize behind release sign-offs; `two_person`+`sla_deadline_ms`), `HITLLoad`
  (backlog/age/`available_reviewer_pairs`/capacity-derived `alert_age_ms`/`flake_recovery_count`).
  §3 store — `CheckpointHealth` (clean vs poisoned, `last_clean_checkpoint_uri` fork-to-prior-good,
  `dangling_handle_uris`, `hibernated`). §5/§8 upgrade — `VersionStamp` (NO `seq` — a sub-record
  embedded in checkpoints/`UpgradePlan`; `model_pins` S1 map), `UpgradeAction` enum,
  `UpgradePlan` (from/to VersionStamp + action + `drift_recalibration` + correlated-spike
  `affected_run_ids`).
- OPS3. **`VersionStamp`/`SLOTarget` are seq-LESS sub-records** (they embed inside seq'd records),
  so the parametrized seq-asserting conformance table skips them — they get a separate
  frozen/extra-forbid/content_identity test. Mirror this if a later ops module adds an embedded
  value object: `seq` belongs on the channel-WRITTEN record, not every nested value.
- OPS4. **Gates: ruff+mypy clean on the two owned files; full pytest green (test_schemas_operations.py
  = 19 cases incl. 10 parametrized × 3). Pre-existing unrelated ruff F401s in
  `foundry/monitors/guardian.py` (another owner's in-flight edit) are NOT mine — scope ruff to the
  owned files when verifying.** Touched ONLY operations.py + test_schemas_operations.py.

## Observability plane — telemetry + drift alerts (T-M3-11)
- OBS1. **The observability plane CONSUMES the proxy manifest; it adds NO second egress (L6).**
  `foundry/observability/telemetry.py::RunTelemetry` is an OTel-STYLE in-memory span collector.
  `ingest_proxy(proxy)` DRAINS the proxy's append-only `manifest` read-only view into MODEL_CALL spans
  carrying the determinism fingerprints (node_id/purpose/tier/requested+served model/served_family/
  lineage_map_revision/request_id/usage/sampling/drift — `_FINGERPRINT_FIELDS` lifted verbatim, proxy
  stays source of truth). It depends on a `_ManifestSource` Protocol exposing ONLY `manifest` (no
  `complete`/`judge`), so telemetry structurally CANNOT make a model call. `record_node_span` /
  `record_gate_span` collect the other two span kinds. `ingest_proxy` is idempotent over a growing
  manifest (a `_manifest_drained` cursor → re-drain emits no duplicate spans). No network/IO; a real
  OTel exporter is an ops swap that drains `export()`.
- OBS2. **Drift alerts REUSE `proxy.drift_events` as the AUTHORITATIVE pinned-ID source — never
  re-derived.** `drift_alerts.pinned_id_drift_alerts(proxy)` lifts each proxy drift_event into a
  CRITICAL `PinnedIdDriftAlert` (served != requested → calibration-baseline invalidation, arch §8;
  `alert.context == dict(proxy.drift_events[i])` verbatim). `threshold_alerts(telemetry, policy)` adds
  cost/usage/latency drift OVER the collected spans (`ThresholdPolicy`, integer token counts, float-free
  budget domain). FAIL-LOUD (gov §2): `raise_on_drift(...)` / `raise_if(alerts)` raise `DriftAlertError`
  (carrying the full alert set) the moment any alert exists — never swallowed; `detect_drift(...)` returns
  the set for triage.
- OBS3. **Tested KEYLESS via the MockTransport-backed proxy (D1/D5).** Force a pinned-ID drift by
  driving a T1 `complete()` against `MockTransport(served_model="claude-sonnet-4-6")` (served override
  ≠ pinned opus) — the proxy records the drift_event, the alert is sourced from it and raised. No signer
  needed (telemetry/drift use `complete`, not `judge`). 13 cases; ruff+mypy clean; full suite green.
  Touched ONLY `foundry/observability/` + `tests/test_observability.py`.

## Five-plane policy package (T-M3-10)
- POL1. **`foundry/policy/` makes the five-plane guardrail model INSPECTABLE, not just wired.**
  `FIVE_PLANE_MODEL: FivePlaneModel` (in `planes.py`) is a frozen registry mapping each `Plane` enum
  member (POLICY/EXECUTION/DATA/OBSERVABILITY/ASSURANCE) → a `PlaneSpec(summary, controls, modules)`
  whose `modules` are the real import paths implementing the plane (sandbox / proxy / gates.runner+
  attestation / durability+trust_root / this package). Completeness is ASSERTABLE: `is_complete()` is
  true iff all five planes present AND each has ≥1 control and ≥1 module (`missing_planes()` reports the
  gap). An empty plane is a missing guardrail, not a wired one — the test prunes a plane / empties a
  plane's controls to prove both fail.
- POL2. **The Policy plane evaluator is OPA-style DEFAULT-DENY / fail-closed.** `PolicyEvaluator(rules=
  DENY_RULES).evaluate(subject: Mapping) -> PolicyDecision` allows ONLY when the subject names a known
  `policy` AND no deny rule fires; an unknown/missing policy (`unknown-policy` rule), any deny rule
  (egress-widening / scanner-config-write / signing-outside-zone-b), OR an evaluator EXCEPTION all DENY.
  Rules are pinned at ctor (run-immutable). `DenyRule` is a FROZEN DATACLASS, not a `FrozenModel` — it
  carries a non-serializable `Callable` predicate, so it is pinned policy CODE, not content-addressed
  run state (a `Callable` field on a `FrozenModel` breaks `content_identity()`/dump). Production swaps
  this for OPA/Conftest/Checkov as an ops change; the fail-closed seam is the real part.
- POL3. **Run-immutable scanner config = content-addressed frozen record + a run binding.**
  `ScannerConfigRecord` (FrozenModel: rules+versions, severity_floors, suppression/license allowlists)
  exposes `revision_hash` = `content_digest(model_dump)` — its content ADDRESS. Any mutation (weakened
  floor, new suppression) → different `revision_hash` → detectable. This IS the value gate verdicts
  already bind as `scanner_config_rev` (gates/runner.py) and the SLSA predicate re-checks at admission.
  `PinnedScannerConfig.pin(record)` captures the pinned rev; `.verify()` is fail-closed tamper
  detection (recompute address, raise `ScannerConfigTamperError` on mismatch) — a run cannot mutate its
  own config (only PRODUCE a new revision). `rule_versions()` feeds the class-2 reuse key (§1f).
- POL4. **Segregation of duties is a fail-closed predicate (mirrors the two-zone split).**
  `authorize_segregation_of_duties(DutyRequest) -> AuthorizationDecision`: DENY if approver ∈ authors
  (an agent cannot sign its own verdict), if approver_zone != `Zone.B_ATTESTOR` (producer zone can't
  sign), if a `required_distinct_role` is unmet, or if author/approver identity is empty. ALLOW only
  when no deny rule fires. `PolicyEvaluator.authorize_duties(...)` re-exposes it. 23 tests; ruff+mypy
  clean; full suite green. Touched ONLY `foundry/policy/` + `tests/test_policy.py`.

## Phase-3 monitor set completion + advisory analyzer (T-M3-09)
- M3-09a. **Three NEW deterministic monitors join the load-bearing set (gov §3, arch §8) — each a
  pure function of trusted-infra counts/observations, no LLM, not prompt-injectable.** `PinnedIdDriftMonitor`
  (`foundry/monitors/pinned_id_drift_monitor.py`) `.inspect(drift_events)` trips PINNED_ID_DRIFT on ANY
  proxy-observed drift event (served != requested; consumes `ModelProviderProxy.drift_events` OR raw
  manifest records — a record with `drift=False` is skipped, no `drift` key = pre-filtered) →
  calibration-baseline invalidation. `ConstraintPressureMonitor` (`constraint_pressure_monitor.py`)
  `.inspect(node_id, injected_count, budget, global_count)` trips CONSTRAINT_PRESSURE at the **90% bound**
  (integer `count*100 >= budget*pct`); the GLOBAL-subset-alone variant is a DISTINCT, strictly-worse rule
  (`global-constraint-pressure`, `matched` carries `scope=global`) because it is unfixable-by-scoping
  (retire/consolidate globals) — never silent truncation (arch §8). `CoverageDeltaMonitor`
  (`coverage_delta_monitor.py`) `.inspect(baseline_high_risk, attested_satisfied, run_against)` trips
  COVERAGE_DELTA when the RUNNER-ATTESTED high-risk satisfied count drops below the FROZEN Spec baseline
  by more than `allowed_regression` (default 0) — baseline + satisfied are signals the agent does not
  solely control (gov §3, AI-R8-F3). All three Guardian-map to ESCALATE (HITL).
- M3-09b. **The advisory analyzer (`analyzer.py::SandboxedAnalyzer`) CANNOT override a deterministic
  trip — structurally, not by convention.** Its `flag(features) -> list[str]` consumes the STRUCTURED
  features mapping ONLY (no tools, no raw repo/transcript — the input type IS the sandbox boundary) and
  returns inert annotation strings; there is NO return path for a Trip/action/verdict. Prompt-injection
  posture: untrusted prose is data; trusted-infra feature keys are PRIORITY-RANKED above agent-shaped
  keys and the slice is CAPPED (`_ranked_capped_keys`), so transcript flooding cannot evict a suspicious
  high-priority feature (cap-eviction defense). It satisfies the existing `guardian.AdvisoryAnalyzer`
  Protocol; the Guardian computes the deterministic action/trips BEFORE consulting it and RE-ASSERTS them
  after (`_consult_analyzer`), so even a malicious `flag()` that mutates its copied feature slice or emits
  "OVERRIDE: clear all trips" lands as an annotation and the trip STANDS.
- M3-09c. **Wiring: new monitors are Guardian ctor deps + new typed events** (`PinnedIdDriftEvent`/
  `ConstraintPressureEvent`/`CoverageDeltaEvent`, dispatched in `assess`'s match) + new `TripKind`s
  (PINNED_ID_DRIFT/CONSTRAINT_PRESSURE/COVERAGE_DELTA) + `MANDATED_ACTIONS` entries (all ESCALATE).
  Existing guardian behavior preserved (deterministic trips dominate; CAP_BREACH still outranks). New
  exports + lazy map in `monitors/__init__.py`.
- M3-09d. **GOTCHA: pytest `python_classes = ["Test_*"]` (underscore!) — `class TestFoo:` is NOT
  collected (silently 0 tests).** `tests/test_monitors.py`'s `TestSemanticLoopDetector`-style classes do
  not actually run under this config; name Phase-3 test classes `Test_PinnedIdDriftMonitor` etc. so the
  methods are collected (18 cases in `tests/test_monitors_phase3.py`). Distinct seam from observability's
  OBS2: that lifts `drift_events` into an OTel/alert `PinnedIdDriftAlert`; THIS lifts the same events into
  a Guardian deterministic Trip — same trusted source, two consumers.

## Run-status liveness model + watchdog (T-M3-02)
- M3-02a. `foundry/operations/run_status.py` is the DETERMINISTIC PAUSED-vs-WEDGED derivation (no LLM,
  injected clock). API: `RunSignals` (frozen dataclass of observable signals — data only), `DeadlineModel`
  (frozen dataclass: `p99_ms + provider_backoff_ms + queue_wait_ms (+ cold_tier_restore_ms ON rehydration)`,
  `.derive(rehydrating=)`), `derive_deadline_ms(sig)` (absolute deadline = `last_progress_ts_ms + budget`),
  `derive_status(sig, now_ms) -> RunStatusRecord`, `watchdog(sig, now_ms|callable) -> RunStatusRecord|None`,
  `is_past_deadline(sig, now_ms)`. T-M3-03 operator surface COMPOSES `watchdog`/`derive_status`; it routes on
  `RunStatusRecord.is_alertable()`, never re-derives.
- M3-02b. **Classification ORDER is load-bearing** (operations.md §2): terminal (DONE/FAILED/QUARANTINED)
  reported verbatim → legitimate `pause_reason` set ⇒ PAUSED (alive, `$0/hr`, carries `awaiting`) **even PAST
  the deadline** (the wait is intentional — this half must NOT alert) → else no-heartbeat past DERIVED
  deadline ⇒ WEDGED (alertable) → else RUNNING. The pause check precedes the deadline check, so a paused run
  is never escalated to WEDGED regardless of how long it sits.
- M3-02c. **The pause-reason flag is the ENTIRE PAUSED-vs-WEDGED hinge.** Same heartbeat + same derived
  deadline + same clock: presence of a `PauseReason` ⇒ PAUSED (not alertable), absence ⇒ WEDGED (alertable).
  The boundary is exact via strict `now_ms > deadline` (AT the deadline is still alive; one ms past is wedged).
- M3-02d. `$0/hr` when paused is enforced at the SINGLE construction site (`_record`): a PAUSED record forces
  `cost_rate_microusd_per_hr=0` (ignores the input rate) and `awaiting` is blanked unless status is PAUSED, so
  the invariant "pause_reason set IFF status==PAUSED, paused⇒$0/hr" cannot drift across call sites.
- M3-02e. **Deadline is DERIVED, never a constant.** Cold-tier first-byte-restore term is added ONLY when
  `rehydrating=True` (a cold/archival-evicted hibernation resume, §6) — otherwise a rehydrating run pages a
  false WEDGED before its first byte returns. `last_progress_*` is the slow-but-progressing heartbeat (adapter
  /partial-progress marker advances it without a superstep), so legitimate provider backoff isn't misread.
- M3-02f. `watchdog` returns the record IFF `is_alertable()` (WEDGED only), else `None` — flags exactly the
  stuck case, ignores RUNNING/PAUSED/terminal. Accepts `now_ms` as int OR zero-arg callable (mirrors codebase
  time injection); the callable is invoked exactly once. 13 tests in `tests/test_run_status.py` cover RUNNING,
  PAUSED within+past deadline, WEDGED, both boundary sides, same-clock paused-vs-wedged, watchdog selectivity,
  terminal passthrough, injected-clock callable, and determinism (`content_identity()` equality).

## HITL-load model + reviewer queue/SLA (T-M3-06)
- M3-06a. **`foundry/operations/hitl.py`: `ReviewerQueue(*, profile, max_backlog, available_reviewer_pairs)`
  with `enqueue(entry, *, regulated=False) -> bool`, `assign(*, run_id, decision_class) -> entry|None`,
  `load(*, now_ms, seq, arrival_rate_micro_per_hr=0) -> HITLLoad`, `alerts(*, now_ms, seq) -> list[Alert]`.**
  Pure/deterministic — the clock (`now_ms`) and thresholds (`max_backlog`, per-entry `sla_deadline_ms`) are
  INJECTED, never wall-clock; same inputs ⇒ same `HITLLoad` + same alert list (the EXIT proof is reproducible).
- M3-06b. **BOUNDED + ALERTED is the control (the Phase-3 exit criterion).** `alerts()` returns: a PAGE
  `hitl_backlog_exceeded` (monitor `hitl_backlog`) iff `backlog_depth > max_backlog` (`max_backlog<=0` disables
  the bound), PLUS one WARN `hitl_sla_breach` (monitor `hitl_sla`) per entry with `now_ms > sla_deadline_ms`
  (`sla_deadline_ms==0` ⇒ no SLA, never breaches). Within bound + within SLA ⇒ `[]`. So the backlog can never
  grow unbounded SILENTLY — bound-crossing pages so the documented response (batch windows → delegated tiers →
  admission-throttle new High-tier runs, operations.md §7) kicks in.
- M3-06c. **Profile DORMANT-vs-LIVE = `HITLProfile` input (mirrors `HumanLiaison`/`sign_off`).** Under
  `SELF_JUDGE` a non-regulated `enqueue` is DROPPED (returns False) — recorded-but-no-op sign-offs don't pile
  HITL, queue stays empty, load model dormant, no alerts. A `regulated=True` touchpoint VETOES zero-HITL and is
  always enqueued (the regulated pack force-disables zero-HITL, governance §1e); `REGULATED` profile enqueues
  everything live + measured (`per_class_backlog` keyed by `decision_class` so drift batches don't serialize
  behind release sign-offs, §5/§7).
- M3-06d. **Two-person = safe deadlock, never auto-approve.** `assign` on a `two_person` entry returns None
  when `available_reviewer_pairs<=0` (stays queued — the High-tier release deadlocks safely as paused, §7);
  with a pair it dequeues the OLDEST matching entry (FIFO by `enqueued_ts_ms` then queue order) and decrements
  the pair count.
- M3-06e. **Alert seq de-collision gotcha.** `alerts()` mints the backlog alert at `seq` (local=0) and the SLA
  alerts at `seq._replace(local=1)` + a running offset, so the per-record seqs in one alert list stay DISTINCT
  (a property test asserts `len({a.seq}) == len(alerts)`). `sla_breach_alerts` advances `local` from its own
  base by `len(alerts)` so far, not by enumerate index — the index-based variant collided the first SLA alert
  (local override → 0) with the backlog alert. 16 tests in `tests/test_hitl.py`. NB: sibling M3 ops modules
  (capacity/store_resilience/upgrade) were untracked WIP during this task — their ruff/collection failures are
  not in scope; `ruff/mypy/pytest` on `hitl.py`+`test_hitl.py` are clean+green.
- M3-08. **Upgrade-with-runs-in-flight = per-run DRAIN-or-MIGRATE, else FAIL-CLOSED PAUSE.**
  `foundry/operations/upgrade.py` (pure/deterministic) plans an upgrade so NO in-flight run is corrupted or
  stranded (operations.md §5/§8). API: `compatible(stamp, target)` (TRUE iff `schema_compatible` — same
  `state_schema_ver` — AND no model-pin DRIFT, i.e. no role pinned to a DIFFERENT id on both sides; a graph/prompt
  bump or a target-ADDED pin is NOT an incompatibility because a run resumes on its PINNED graph/prompt, §5);
  `plan_upgrade(current, target, run: InFlightRun, *, migration_uri=None) -> RunUpgradeDecision`; fleet helper
  `plan_fleet_upgrade(...)` maps it over the cohort + sorts by `seq` (order-independent, one decision per run, none
  dropped). Decision order: (1) compatible ⇒ DRAIN (`UpgradeAction.DRAIN_BEFORE_UPGRADE`); (2) ADDITIVE schema bump
  (target schema strictly newer) WITH a `migration_uri` AND a serde-loadable checkpoint ⇒ MIGRATE; (3) breaking
  change that CAN complete on old (and is loadable) ⇒ DRAIN; (4) else (unloadable / downgrade / breaking-no-drain)
  ⇒ PAUSE (`UpgradeAction.ABORT`, `stranded=True`, checkpoint LEFT INTACT on its pinned version, flagged for
  operator) — never a silent lossy upgrade.
- M3-08a. **Honor the serde allowlist as the orphan-checkpoint guard.** `serde_loadable(model_types)` checks every
  frozen-model type the checkpoint carries is in `FOUNDRY_MSGPACK_MODULES` (a type NOT there degrades to `dict` on
  load → store rejects fail-closed, serde.py). `migrate_checkpoint(stamp, target, *, model_types, migration_uri)` is
  the DEV-GRADE seam: returns `target` iff loadable + forward-only + (strict bump ⇒ has `migration_uri`), else raises
  `MigrationError` (planner catches → PAUSE). It PROVES round-trip under the target allowlist; it does NOT run a
  concrete channel-rewriting migration — that's an ops task wiring a deterministic function behind `migration_uri`,
  run by the trusted upgrade tooling, version-stamped per §8 (noted inline). 17 tests in `tests/test_upgrade.py`;
  ruff/mypy/full-pytest clean+green. NB: sibling untracked WIP `tests/test_capacity.py` had pre-existing ruff E501s
  (another agent's file) — not in my scope (touched ONLY upgrade.py + test_upgrade.py).

## Trusted-component availability + per-lineage reservation (T-M3-07)
- TC1. **Outage → PAUSED, never WEDGED (fail-closed, not stuck).** `foundry/operations/trusted_component.py`
  models the trust-critical chokepoints (`TrustedComponent`: model_provider_proxy / zone_b_signer /
  gate_runner). A trusted-component OUTAGE (≠ compromise, §4) degrades the affected run to
  `RunStatusRecord(status=PAUSED, pause_reason=TRUSTED_COMPONENT_UNAVAILABLE, cost_rate=0)` — alive,
  $0/hr, NOT alertable (`is_alertable()==False`). The dependent judgment/gate node does NOT proceed —
  same discipline as `ProxyUnavailable` (proxy fail-closed). API: `AvailabilityModel.component_status`
  / `.all_available(deps)` (the node's go/no-go precondition, fail-closed), `on_unavailable(...) ->
  PAUSED`, `on_recovery(...) -> RUNNING`, `classify_for_dependencies(model, deps, ...)` (the
  end-to-end derivation: any dep down → PAUSED on the first-in-enum-order outage, else RUNNING). EXIT
  round-trip = outage→PAUSED→recovery→RUNNING. Reuses the §2 RunStatus/PauseReason vocab so the
  operator surface reads ONE taxonomy; pure (no clock/IO/LLM).
- TC2. **Per-lineage reservation protects each lineage's slice from starvation (§4).** `CapacityReservation`
  holds a per-lineage `reserved` slice + an optional SHARED `burst` pool. `reserve(lineage, n)` declares
  a slice (preserves in-flight `used` on re-declare — supersession not reset, L4); `admit_for_lineage(
  lineage, amount) -> (new_table, admitted)` draws from the lineage's OWN reserved headroom FIRST then
  spills onto the shared burst — and NEVER reaches into another lineage's reserved quota. So one lineage
  saturating demand exhausts only its own slice + the shared pool; another lineage's reservation stays
  fully intact (the anti-starvation invariant; §4 worst-case-failover-redistribution sizing). On refusal
  the table is returned UNCHANGED (`after is before`) — queue against reserved capacity, never an
  unbounded retry storm or a partial draw. Immutable + insertion-order-independent (per-lineage keyed) →
  deterministic. 14 tests in `tests/test_trusted_component.py`; ruff+mypy clean, full suite green. NB:
  sibling `tests/test_capacity.py` was untracked WIP with its OWN ruff E501s — not in scope.

## Store HA / outage-pause + poisoned-checkpoint recovery (T-M3-04)
- M3-04a. **`foundry/operations/store_resilience.py` is PURE wiring over the EXISTING durability surface
  — it rebuilds nothing.** REUSES `FoundryCheckpointer.superstep_txn` (the one-txn/superstep atomic
  commit, B3), `ArtifactStore.get_body`/`get_blob` (allowlist serde, fail-closed on unknown/non-frozen,
  I5), `OutboxRelay.recover`/`drain` (at-least-once + idempotent, L7), and `verify_audit_chain` (the WORM
  chain). API: `with_store_outage_handling(ckpt, *, run_id, current_phase, now_ms, seq, on_pause) ->`
  ctx-mgr yielding the conn; `on_store_unavailable(...) -> RunStatusRecord`; `resume_after_outage(ckpt, *,
  reconcile_relay) -> bool`; `check_checkpoint_health(*, run_id, checkpoint_uri, seq, resolve_body,
  handle_uris, probe_handle, conn, last_clean_checkpoint_uri, hibernated) -> CheckpointHealth`;
  `recover_to_last_clean(health, *, resolve_body) -> FrozenModel`. Exceptions `StoreUnavailable` /
  `PoisonedCheckpointError`.
- M3-04b. **Outage → pause-not-corrupt is enforced BY CONSTRUCTION, not by a new mechanism.** The seam
  runs the superstep body inside `superstep_txn`; ANY exception rolls the whole txn back (checkpoint +
  ledgers + outbox + WORM all-or-nothing, B3) so NO partial state ever persists. It then CLASSIFIES the
  exception: a store-unavailability signal (`StoreUnavailable` OR `sqlite3.OperationalError` — the
  Postgres connection-loss analogue) is SWALLOWED and turned into a `PAUSED(store_outage)` record via
  `on_pause` ($0/hr, not alertable, §2); ANY OTHER exception is RE-RAISED (a real bug must never be
  masked as a pause — fail-closed both directions). The resume point is the PRIOR committed checkpoint;
  `resume_after_outage` probes the store inside a real txn (returns False, stays paused, if still down —
  never declares recovery against an unreachable store) then calls `OutboxRelay.recover` to resolve the
  intent-window idempotently — no duplicate external action (the relay is the only thing that re-fires,
  and it dedups). NB: `on_store_unavailable` is a PURE fn (clock+seq injected) — deterministic record.
- M3-04c. **Poisoned-checkpoint detection = three corruption classes, recovery = fork-to-prior-good,
  fail-closed.** `check_checkpoint_health` marks `clean=False`+`quarantined=True` on ANY of: (a)
  crash-on-resume — `resolve_body(uri)` raises (unknown uri) or returns a non-`FrozenModel` (a
  non-allowlisted body degrades to `dict` under the serde, I5); (b) dangling handle — a uri in
  `handle_uris` fails `probe_handle` (the integrity sweep; `hibernated=True` surfaces it as a hard alert
  BEFORE wake, §3); (c) broken WORM chain — `verify_audit_chain(conn)` False. `recover_to_last_clean`
  loads `last_clean_checkpoint_uri`, RE-verifies it round-trips to a frozen model, and returns THAT — the
  poisoned checkpoint is NEVER returned as trusted state. It RAISES `PoisonedCheckpointError` when: the
  checkpoint is actually clean (nothing to recover), there is no last-clean pointer (operator override
  needed — never falls back to poisoned), or the last-clean itself won't load (no trusted recovery point).
- M3-04d. **EXIT proof = `tests/test_store_resilience.py`, 12 tests.** Outage side: a clean prior
  superstep commits one outbox row; the next superstep enqueues a row then raises `StoreUnavailable` —
  asserts exactly one `PAUSED(store_outage,$0/hr)` record, the outbox still holds ONLY the prior row
  (`push-B` rolled back), `verify_audit_chain` intact, then resume drains `push-A` EXACTLY once across
  two `drain()` calls (idempotent, no dup external action). Plus: resume returns False while store down
  (and never reconciles), a real `ValueError` is re-raised not masked, determinism of the pause record.
  Poison side: clean reports healthy; bad-serde/unknown-uri, dangling-handle, broken-WORM-chain each
  detected; recover returns the LAST CLEAN body; fail-closed on no-pointer / clean-checkpoint /
  last-clean-also-poisoned / non-frozen body. NB (gotcha): a CLOSED sqlite conn raises
  `ProgrammingError`, NOT `OperationalError` — to simulate Postgres connection-loss in the resume test,
  monkeypatch `conn.execute` to raise `sqlite3.OperationalError`, don't just `.close()`.
- M3-04e. **Sibling M3 ops modules (capacity/upgrade/monitors) were untracked WIP during this task** —
  their ruff/mypy/collection failures (`capacity.py:230 _fair_share_order`, `test_capacity.py` E501,
  `test_upgrade.py` MIGRATE-vs-DRAIN) are NOT in scope. `ruff`+`mypy`+`pytest` on `store_resilience.py` +
  `test_store_resilience.py` are clean+green; the durability deps they ride (`test_outbox`,
  `test_schemas_operations`) stay green.

## Multi-tenancy / capacity / queue / fairness + fleet cost-rate cap + hibernation (T-M3-05)
- M3-05a. **`foundry/operations/capacity.py` = a PURE/DETERMINISTIC weighted-fair admission scheduler
  (operations.md §6); the clock (`now_ms`) and per-run usage are INJECTED, never wall-clock.** API:
  `CapacityScheduler(capacity: FleetCapacity, quotas: Mapping[str, TenantQuota], hibernate_idle_ms=...)`.
  `admit(entry, *, now_ms, seq) -> FairnessDecision` decides ONE queued run; `schedule(queue, *, now_ms,
  seq) -> tuple[FairnessDecision, ...]` runs one fair round over the whole queue; `fleet_cost_rate_ok(
  rate) -> bool` is the cap check; `register_running`/`release`/`active_runs`/`used_slots`/
  `tenant_active_count`/`fleet_cost_rate_microusd_per_hr` are the in-flight bookkeeping. `FleetCapacity`
  + `RunningRun` are local types (FleetCapacity is a FrozenModel; RunningRun a mutable @dataclass — it is
  live in-flight state, not a persisted artifact). Reuses the schema records `TenantQuota`/`QueueEntry`/
  `FairnessDecision` verbatim and mirrors `BudgetController`'s integer-µ$ projected-vs-cap accounting.
- M3-05b. **No-starvation = deficit weighted round-robin with PER-STEP RE-RANKING.** Each round accrues
  credit per tenant weight (`fair_share_micro`; an unset weight still earns 1 so it can't starve forever);
  `schedule` then repeatedly picks the MOST-STARVED entry — ordered `(slots_held ASC, accrued_credit DESC,
  enqueued_ts ASC, run_id)` — and RE-EVALUATES after each admission so a just-admitted tenant's now-held
  slot pushes it down the order. A winner ZEROES its credit (`_spend_credit`); losers keep accruing. THE
  GOTCHA: sorting the queue ONCE up front is NOT enough — a heavy tenant flooding the queue takes BOTH
  slots in round 0 because the held-count only updates as you register. You MUST re-rank greedily mid-round
  (pick-remove-admit-register loop), else `test_heavy_tenant_is_throttled` and the spread assertion fail.
- M3-05c. **Admission-side ONLY, never kill-side.** `admit` checks in order: tenant-quota-exists → global
  slots free (`REASON_NO_SLOTS`) → per-tenant `max_concurrent_runs` noisy-neighbor guard
  (`REASON_TENANT_CONCURRENCY`) → fleet cost-rate cap (`REASON_FLEET_COST_RATE_CAP`). A throttled run stays
  QUEUED (`admitted=False`); nothing is ever killed. `fleet_cost_rate_ok(rate)` = projected (current
  in-flight aggregate + requested) ≤ cap — paused/hibernated runs contribute $0/hr so the aggregate is live
  spend only. The decision carries the aggregate + cap it was made against.
- M3-05d. **Hibernation is DISTINCT from PAUSED and is the slot-freeing lever.** `should_hibernate(run, *,
  now_ms)` = idle ≥ `hibernate_idle_ms` AND not mid-trust-root-attestation (non-preemptible §4/§6) AND not
  already terminal/hibernated. `hibernate(run_id, *, now_ms, seq, incremental_footprint_bytes=0) ->
  RunStatusRecord(status=HIBERNATED, cost_rate=0, ...)` RELEASES the slot (so a contender is admittable) and
  reports the incremental CAS footprint; resume = re-`register_running` on wake. A PAUSED run still holds
  its slot; only hibernation frees it.
- M3-05e. **EXIT proof = `tests/test_capacity.py`, 11 tests (all green; ruff+mypy clean; full suite green).**
  No-starvation: 5 tenants contend for 2 slots over 10 rounds (release-after-admit), EVERY tenant makes
  progress, spread ≤ 2 / max ≤ 6 (a monopolist would be ~10+); heavy-floods-queue still lets the light
  tenant in; concurrency guard blocks monopoly with free slots. Fleet cap: cap=100µ$/hr, two 60µ$/hr runs
  → exactly ONE admitted, aggregate never exceeds cap, `REASON_FLEET_COST_RATE_CAP`. Hibernation: idle run
  hibernates → slot freed → waiting run now admitted → resumable; mid-attestation/terminal never
  hibernate. Determinism: identical inputs → identical decisions + admission order.

## Operator surface — dashboard + SLOs + diagnose-stuck-run (T-M3-03)
- OP1. **`foundry/operations/operator.py` COMPOSES `run_status.derive_status`; it NEVER re-derives the
  PAUSED-vs-WEDGED classification.** `dashboard(records, *, now_ms) -> OperatorView` aggregates already-
  derived `RunStatusRecord`s; `dashboard_from_signals(signals, *, now_ms)` is the convenience that maps
  `derive_status` across the fleet first. `OperatorView` carries zero-filled `counts` by `RunStatus`
  (every tile present), `total`, `healthy` (RUNNING+PAUSED), and the diagnosable `stuck` tuple.
- OP2. **The EXIT deliverable ("operator can diagnose a stuck run") is `StuckRunSummary`**, built ONLY
  for `record.is_alertable()` (WEDGED) runs: run_id + `current_phase` + last-progress heartbeat
  (seq+ts) + DERIVED `deadline_ms` + `overdue_ms` (now − deadline) + live `cost_rate` + a `reason`
  string spelling out *why WEDGED not PAUSED* (no heartbeat past derived deadline, no legitimate pause
  reason). Ordered most-overdue-then-run_id (a total order → deterministic).
- OP3. **SLOs are integer-millionths, no float.** `OperatorView.health_ratio_micro()` EXCLUDES terminal
  runs (DONE/FAILED/QUARANTINED) from the denominator (vacuously 1.0 = `MICRO` when none non-terminal).
  `evaluate_slos(view, slos, *, seq) -> tuple[Alert,...]` recognises 3 built-in SLO names: floor
  `run_health_ratio` (breach→WARN), ceilings `max_wedged_count`/`max_stuck_age_ms` (breach→PAGE — a
  wedge wastes spend + blocks delivery). Within SLO emits nothing; unknown names skipped (forward-
  compatible); alerts are fleet-level (`run_id=None`), carry the target's `runbook_uri`, and each gets a
  distinct `seq.local` (`seq._replace(local=seq.local+i)`) so records are distinct WORM identities.
- OP4. **Gates: ruff+mypy clean on owned files (mypy clean across 129 files); test_operator.py = 17
  cases; full pytest green. Touched ONLY operator.py + test_operator.py.** Pitfall hit: a global `now_ms`
  past the deadline makes a "RUNNING" fixture read WEDGED via derive_status — a progressing run needs a
  RECENT `last_progress_ts_ms` (within the derived budget), mirroring real heartbeat advancement.

## Operator reference doc (T-M3-12)
- DOC1. **`build/RUNBOOKS.md` is THE operator reference** (Day-2): the SLO/alert catalog + one runbook
  per scenario (wedged run, store outage, poisoned checkpoint, trusted-component outage, HITL backlog,
  capacity/cost-rate, upgrade-in-flight, pinned-ID/cost drift, five planes). Each step cites the REAL
  shipped module/function (operations/observability/policy) — keep it in sync when those APIs change.

## Phase-3 (Operability & scale) EXIT integration test — the honesty map (T-M3-13)
- P3X1. **`tests/test_phase3_exit.py` is the Phase-3 EXIT proof; it COMPOSES the shipped M3 ops
  modules, never re-derives their logic.** One test (or two) per roadmap criterion: (a) operator
  diagnoses a stuck run — `dashboard_from_signals` over a mixed RUNNING/PAUSED/WEDGED fleet +
  `watchdog` + `evaluate_slos`; (b) store outage pauses-not-corrupts + recovers, plus poisoned
  checkpoint detect→recover; (c) no-starvation over 10 rounds + fleet cost-rate cap + hibernation;
  (d) HITL backlog bounded+alerted + self-judge dormant. 9 tests, all green; ruff+mypy clean; full
  suite unbroken. Touched ONLY this file + this LEARNINGS note.
- P3X2. **GENUINE vs smoke — the two load-bearing criteria are exercised by reading STATE, not a
  proxy.** (b) no-corrupt is proved by reading the real SQLite `outbox` back after the
  `superstep_txn` rollback (only the prior committed `push-A` remains; `push-B` never landed) and by
  draining a real `OutboxRelay` TWICE to show exactly-once (no duplicate external action) + an intact
  WORM chain. (c) no-starvation is proved by COUNTING per-tenant admissions across rounds
  (`all(count>0)` + a no-monopoly ceiling), not a single-round check. (a) the paused-vs-wedged
  distinction is tested on the NOT-alert side too (a PAUSED run observed 100× past its deadline is
  still not stuck).
- P3X3. **Honesty note: the ONLY dev-mode stand-in is the outage SIGNAL.** The "store outage" is an
  injected `StoreUnavailable` (the documented dev analogue of a Postgres/object-store connection
  loss; `sqlite3.OperationalError` is the other recognised signal, operations.md §3) — the
  rollback/pause/resume CODE under test is the real shipped durability path, and the persisted store
  is read to confirm no partial write. (a)/(c)/(d) are pure deterministic compositions of real module
  code over in-memory fleet/tenant/reviewer-queue FIXTURES; the classification/scheduling/alerting
  logic is the unmodified shipped code, clocks + `Seq` injected for reproducibility.

## Phase-3 review fixes (M3-R1-01..04, batched fixer)
- M3F1. **SLOTarget objective fields are now KIND-TYPED — `_micro` is no longer overloaded.**
  `SLOTarget` carries distinct integer objectives: `objective_micro` (RATIO floor, millionths),
  `objective_count` (COUNT ceiling, e.g. max WEDGED), `objective_ms` (MS ceiling, e.g. oldest stuck
  age). `ratio_floor()` / `ceiling(field=...)` resolve the right one and FAIL CLOSED
  (`SLOConfigError`) if unset — a ceiling SLO falls back to legacy `objective_micro` only if the
  dedicated field is None (back-compat). All three objective fields are now `int | None`.
- M3F2. **`evaluate_slos` NEVER silently skips a configured SLO (M3-R1-03).** An unrecognised SLO
  name OR a recognised-but-mis-configured objective emits a WARN `slo_config_error` Alert
  (`ALERT_SLO_CONFIG`) instead of being dropped — a modeled-but-unevaluable SLO is visible on the
  operator surface, never a dead monitor. Recognised set = `_RECOGNISED_SLO_NAMES`.
- M3F3. **Resume-gate decision (M3-R1-01): ADDED `CapacityScheduler.resume_admit(...)`, not deferred.**
  operations.md §6 is explicit (lines 224-230 / 299-303) that the fleet cost-rate cap must ALSO gate
  IN-FLIGHT spend ("resume is not new-run admission") — cold-tier rehydration retrieval/egress + §7
  in-flight quorum re-runs. `resume_admit` mirrors `admit` on the RATE dimension ONLY (no slot/fan-out
  consume, since a resuming run already holds its lineage): a cap-breaching resume is queued
  (`admitted=False`, `REASON_RESUME_RATE_CAP`), never killed/uncapped. Chose to implement (not just
  document) because it is a small, exact mirror of the existing `fleet_cost_rate_ok` admission check.
- M3F4. **No-starvation now holds under UNEQUAL weights + held slots (M3-R1-02 was a latent gap, not
  just a test gap).** The deficit-RR `_credit` reset-to-0-on-win + accrue-full-weight-per-round meant a
  tiny-weight tenant (weight 1 vs 1_000_000) could be starved forever when slots are HELD across rounds
  (it never wins the `-credit` tiebreak). Fix: added an anti-starvation AGE ledger `_waited_rounds`
  (consecutive contended-but-denied rounds, reset on win) as the DOMINANT fairness key AFTER the
  held-slots anti-monopoly guard, BEFORE credit. `_most_starved` rank is now
  `(held, -waited_rounds, -credit, ts, run_id)`. Credit/weight still orders ties among equally-aged
  tenants; equal-weight behavior is unchanged (existing tests green). New unequal-weight no-release
  multi-round test proves every tenant is admitted.
- M3F5. **store_resilience `check_checkpoint_health` bare-except narrowed (M3-R1-04).** Expected
  blob-loss (`UnknownArtifactError`/`KeyError`) → recorded as dangling silently; ANY OTHER probe
  exception now LOGs at ERROR with `exc_info` (a real bug is diagnosed) while STAYING fail-closed
  (still appended as dangling → poisoned, so we never wake on an unverified checkpoint).
- M3F6. **Gates: ruff + mypy clean (129 files); full suite 976 passed / 1 skipped, incl.
  test_phase3_exit. Touched ONLY operator.py, capacity.py, store_resilience.py, schemas/operations.py
  + their 4 test files + this note. No exit-property behavior changed.**

## Cross-fork digest-disqualification + admission_look (T-M4-02; the fork-divergence defense)
- M4D1. **`global_records.py` now carries a 5th cross-fork record: the digest-disqualification
  latch (gov §5, plan §2.4) — the fork-divergence defense.** `disqualify_digest(digest, reason,
  run_id) -> bool` ATOMICALLY disqualifies a build/tree digest insert-if-absent under the lock
  (`INSERT OR IGNORE` + the `digest` PRIMARY KEY — mirrors `try_first_alpha_look`; rowcount==1 ⇒
  THIS call recorded it). It is KEYED ON THE DIGEST, never the `run_id`, and MONOTONIC: a later
  fork minting a fresh `run_id` and re-presenting the SAME digest reads still-disqualified and
  cannot un-disqualify or overwrite the FIRST reason/run_id (concurrent callers collapse to one
  record, FIRST wins). So a malicious fork cannot launder a stable-FAIL'd / forged-verdict digest
  by forking. `digest_disqualified(digest) -> bool` is the read.
- M4D2. **`admission_look(digest) -> AdmissionState` is the deploy-time admission controller's
  (T-M4-07) one-shot read.** It FOLDS two disqualifying conditions into ONE truthful latch-state:
  an EXPLICIT digest-disqualification OR a stable-FAIL latch on the digest FOR ANY GATE (new private
  `_stable_fail_latched_any(digest)` — the public `stable_fail_latched(digest, gate)` needs a gate
  arg, admission is digest-scoped). Returns `AdmissionState.DISQUALIFIED` if either holds, else
  `ADMISSIBLE`. The query REPORTS state truthfully; the *not-admit-on-unknown* fail-closed policy is
  the ADMISSION CALLER'S, not this query's (gov §5). `AdmissionState` is a 2-value `enum.Enum`
  (ADMISSIBLE/DISQUALIFIED) added to `GlobalRecordsView` alongside the existing read methods. T-M4-07
  consumes `admission_look`; do NOT re-derive admissibility from the live `gates` reducer (latest-by-
  seq) — that legitimately shows PASS after a recovery while admission HOLDS the digest latched.
- M4D3. **Additive only — no existing latch touched.** New table `digest_disqualification`; existing
  stable_fail/negative/nonce/alpha latches and their callers (`gates/runner.py`) unchanged. Gates:
  ruff + mypy clean (129 files); 8 new tests in `tests/test_global_records_admission.py` green; full
  suite 984 passed / 1 skipped. Touched ONLY `global_records.py` + the new test + this note. NB there
  is NO `tests/test_global_records.py` in the tree (the DoD referenced it but the existing latch
  coverage lives in `test_gate_runner.py`/`test_gate_wiring.py` etc. — all still green).

## Trusted-component lifecycle — key/component rotation + HA (T-M4-09)
- M4L1. **`foundry/operations/trusted_lifecycle.py` is the SIBLING of `trusted_component.py`** for the
  §8 lifecycle concerns (rotation, HA), REUSING `trusted_component`'s availability vocabulary verbatim
  (`on_unavailable`/`on_recovery`/`TrustedComponent`) — never editing it. Pure/deterministic, clock
  INJECTED (`now_ms` is always an arg, never read). Dev-grade roots honesty (§0.3): rotation LOGIC is
  real, key CUSTODY is dev-grade (the HMAC keys live in `gates/attestation.py`).
- M4L2. **Rotation WITH OVERLAP, not a hard cut-over.** `RotationState(component, versions, grace_ms)`
  holds the monotonic `KeyVersion` history (last = active signer). `rotate_key(new_key_id, now_ms,
  in_flight_key_ids=())` advances version by one and schedules the prior key to retire at
  `now_ms + grace_ms` (it stays VERIFIABLE for the grace window so in-flight verdicts signed under it
  drain). `verifiable_under(key_id, now_ms)` is the verifier's grace check — fail-closed: a verdict's
  `key_id` (the `"{scheme}:{key_id}:{mac}"` middle field of `attestation.py`) is verifiable iff live OR
  `now_ms < retired_ms` (strict — non-verifiable AT/after retirement); an UNKNOWN key_id is never
  verifiable. Same model rotates `lineage_map_revision`/`scanner_config_rev` (any `TrustedComponent`).
- M4L3. **FAIL-CLOSED on orphan — never silently break trust.** A rotation/`retire(...)` that would
  invalidate an in-flight, still-valid attestation (zero-grace rotation away from a key still in flight,
  or retiring a key with an in-flight verdict) RAISES `OrphanedVerdictError`; the caller calls
  `pause_for_rotation(...)` which mints the SAME PAUSED(`trusted_component_unavailable`) record an outage
  does (reuses `on_unavailable`) — never WEDGED, $0/hr, not alerted. A grace_ms>0 rotation keeps the
  prior key verifiable, so a normal overlapped rotation NEVER orphans. Monotonicity: version only
  advances; rotating to the active key_id or a non-monotonic version history is rejected; the active
  signer can't be retired (there must always be a live signer).
- M4L4. **HA = redundant instances sharing one signing identity (§4).** `HAComponent(component,
  primary, instances)` + `failover() -> (new_component, serving | None)`: a healthy primary keeps
  serving (no needless failover); an unhealthy primary fails over to a healthy instance (it becomes the
  new primary); NO healthy instance returns `(self, None)`. `classify_ha(...)` wraps it: serving
  instance → RUNNING (`on_recovery`); none healthy → PAUSED(`trusted_component_unavailable`), NOT WEDGED
  (`on_unavailable`). So a single-instance outage degrades to PAUSED-then-resumes-on-the-replica, and a
  TOTAL outage stays PAUSED fail-closed. `healthy_instance()` prefers the primary, deterministic order
  otherwise. Gates: ruff + mypy clean (130 files); 24 new tests in `tests/test_trusted_lifecycle.py`
  green; full suite 1038 passed / 1 skipped. Touched ONLY the new module + test + this note.

## Trust root — Phase-4 artifact schemas (T-M4-01)
The M4 runner/builder/admission/deployer waves build on these frozen records (APPEND-ONLY to the
existing modules; reused where adequate). DEV-GRADE-ROOTS HONESTY (plan §0.3): the records, their
input-binding, and the re-verification LOGIC are REAL + CORRECT; only the cryptographic ROOT (builder
hardware/OIDC identity, signing-key custody, hermetic isolation, deployer least-privilege boundary)
is dev-grade — stamped in every new docstring.
- TR1. **security.py — REUSED, not duplicated:** `SBOMRef` (the agent-side SBOM *handle*),
  `ThreatModel`/`SecurityTriage`/`SupplyChainReport` (`trust_root_touched`)/`AuditFinding`/`Severity`
  pre-existed and are UNTOUCHED. `EvidencePack` (governance.py, AS5) already carries
  `provenance_predicate_uri` + `scanner_config_rev` — the Phase-4 predicate is the body that handle
  points at. **ADDED** `Material` (one pinned hash-addressed build input: `uri`+`digest`),
  `SLSAProvenance` (the SLSA/in-toto predicate — `build_type`/`builder_id`/`invocation_uri`/source +
  `tree_sha`/`subject_name`/`subject_digest` + `materials: tuple[Material,...]` + `gate_result_uris`
  (WORM attested gate-state, gov §1c/§4) + `scanner_config_rev`/`trust_store_rev`/`hermetic`; helpers
  `material_digests()`/`binds_subject()`), and the fuller `SBOM` (authoritative, distinct from
  `SBOMRef`: inline `components: tuple[SBOMComponent,...]` of name+version+digest+license,
  `fully_resolved`, `unpinned_components()`) + its `SBOMComponent` sub-record.
- TR2. **operations.py — `DeployPlan` (the plan+rollback BODY the agent authors) is REUSED, untouched.**
  It does NOT cover idempotent rollback-capable EXECUTION, so **ADDED** `DeployResult` (the non-agent
  deployer's execution record: `deploy_id` + the exact admitted `subject_digest` + process-stable
  `idempotency_key` (content_digest, never `hash()` — L7) + `admission_decision_uri` link +
  `rollback_to_digest`/`rolled_back`). Also **ADDED** `BuildRequest`/`BuildResult` (hermetic inputs —
  source/base-image/material digests — → produced `subject_digest` + `provenance_uri` handle) and the
  `AdmissionDecision` (+ `AdmissionVerdict` enum ADMIT/DENY/INDETERMINATE) carrying the EXACT
  `subject_digest`, the verified grounds (`signature_verified`, `all_required_gates_pass` +
  `required_gates`, `gate_state_uri`, `scanner_config_rev` match, `stable_fail_latched`), and `reason`;
  `admitted()` is True ONLY on ADMIT (fail-closed — DENY/INDETERMINATE never ship, gov §5). This is the
  SCHEMA side; T-M4-02's `global_records.admission_look(digest)`/`disqualify_digest` are the cross-fork
  LATCH the admission controller (T-M4-07) reads — the `AdmissionDecision` is the controller's emitted
  record, distinct from that latch query.
- TR3. **The provenance predicate is INPUT-BOUND (gov §4 — the load-bearing property).** Every field
  naming an input (each material's `digest`, `source_digest`, `tree_sha`, `gate_result_uris`,
  `scanner_config_rev`, `subject_digest`) is part of `content_identity()`, so changing ANY material
  digest (or reordering materials — tuple order IS identity) changes the predicate the signature
  attests over and admission re-checks. Tests prove material-digest / subject / gate-state / config
  flips all re-key identity.
- TR4. **No `Field(default_factory=...)` needed (S1):** every mutable default is a `tuple` literal
  `= ()` (immutable → no RUF012); the `Field` import was REMOVED from security.py after switching to
  tuple defaults. Tests in `tests/test_schemas_trust_root.py` (34 tests; mirrors the operations CASES
  table + spot-checks). Gate chain clean: ruff + mypy (130 files) clean; full suite 1038 passed /
  1 skipped. Touched ONLY `schemas/security.py`, `schemas/operations.py`, the new test, and this note.

## Input-bound attestation + provenance binding — the forged-verdict/replay defense (T-M4-05)
- TR5. **AUDIT: the deterministic-gate attestation was ALREADY fully input-bound — no replay gap to
  close.** `_attestation_payload` signs the COMPLETE input tuple `(gate_name, run_against (=tree/
  state_sha), scanner_config_rev, lineage_map_revision, status, results_digest)`. So a verdict signed
  for (tree A, config X, PASS) does NOT verify when re-presented against tree B / config Y / a flipped
  status / a swapped lineage or results digest — `model_copy`-ing any of those fields recomputes a
  DIFFERENT expected HMAC and `verify_attestation` returns False (the `merge_gates` grounding suite
  already proved status-flip; T-M4-05 adds the cross-tree / cross-config / lineage / results-digest
  cases directly against the verifier). The verdict bit cannot be moved off its inputs.
- TR6. **Build-artifact provenance binding mirrors the judgment-binding pattern but the PROVENANCE is
  an ARGUMENT, not a `GateResult` field (you may NOT add fields — single-file ownership).**
  `ZoneBSigner.with_provenance_binding(g, prov)` stores a signature over `_provenance_payload(g, prov)`
  (= the base attestation tuple FOLDED with `prov.content_identity()` + spelled-out `subject_digest`/
  `builder_id`/`tree_sha`) into the EXISTING `runner_attestation` field;
  `DevGateWriteVerifier.verify_provenance_binding(g, prov)` recomputes with the RE-PRESENTED `prov`.
  Re-presenting a TAMPERED provenance (different material/subject/builder digest → different
  `content_identity()`, TR3) fails verification, so a build attestation can't be replayed onto a
  different build/subject — and the base tuple still blocks cross-tree/config/status replay even with
  the original provenance.
- TR7. **A provenance-bound signature is NOT a valid PLAIN attestation (defense in depth).** Because
  the provenance identity is folded in, `verify_attestation(g)` returns False for a provenance-bound
  verdict — a build verdict can't masquerade as a generic deterministic verdict to dodge the provenance
  re-check. Admission (T-M4-07) / builder (T-M4-06) must call `verify_provenance_binding` for build
  artifacts. `canonical_bytes` can't encode `bytes`, so the folded base tuple is `.decode("utf-8")`'d
  (it's already canonical JSON text) before nesting. Memo key = `(g.content_identity(),
  prov.content_identity())` (separate `_provenance_cache`) — no stale-true leak across provenances.
  Touched ONLY `gates/attestation.py` + `tests/test_attestation_provenance.py`; ruff + mypy (130 files)
  clean; target trio + full suite green.

## Two-zone runner hardening — config re-derivation + sealed observation (T-M4-03)
- HR1. **Zone B RE-DERIVES the scanner-config rev; it never trusts the plain string nor an
  agent-supplied config (forged-results defense, gov §1c/§4).** `TwoZoneGateRunner(... , *,
  pinned_config: PinnedScannerConfig | None = None, seal_key: bytes | None = None)` — both ADDITIVE
  kwargs (every existing caller passing only the plain `scanner_config_rev` string is unchanged). When
  `pinned_config` is set, `run` re-derives the bound rev via `pinned_config.verify()` (fail-closed
  content-address tamper check, `policy/scanner_config.py`) and binds THAT into the attestation
  (`scanner_config_rev`), overriding the ctor string. Outcomes: pinned config fails to verify →
  **ERROR** (tamper/infra, not a tree judgment, DS7 — never latches); zone A reports it ran under a
  config rev != the re-derived pin (`obs.structured["ran_scanner_config_rev"]`, key
  `runner.RAN_CONFIG_REV_KEY`) → **FAIL** (a judgment that the tree was scanned under the wrong/weakened
  rules — a forged/weakened config can NEVER produce a PASS; on an integrated digest it LATCHES). The
  mismatch check fires ONLY when `pinned_config` is present — with no pin there is nothing trustworthy
  to compare against, so a zone-A config claim is ignored (legacy back-compat; this is why a plain-string
  runner with a reported ran-rev still PASSes).
- HR2. **Sealed observation channel: zone A stamps an integrity tag, zone B re-derives it.**
  `seal_observation(obs, seal_key)` (also `observe(..., seal_key=...)`) HMAC-SHA256s the WHOLE
  observation core (`_observation_core`: every raw field INCLUDING `results_digest`, excluding the seal
  itself) → `obs.seal = "seal256:<tag>"`. On a sealed channel (`seal_key` on the runner), `run` calls
  `_verify_observation` BEFORE `gate.decide`: an absent or non-matching seal raises `ObservationSealError`
  → **ERROR, never PASS**. So a swapped FAIL→PASS observation between execution and decision is caught
  (the attacker lacks zone A's sealing secret — re-sealing with a wrong key fails the constant-time
  compare); rewriting any body field (or laundering `results_digest`) breaks the tag too. Seal is OPTIONAL
  (empty default field) → unsealed channel is byte-identical to pre-T-M4-03 behavior.
- HR3. **Check ORDER in `_verify_observation` (fail-closed, integrity before judgment):** (1) seal →
  ERROR; (2) config-tamper → ERROR; (3) config-mismatch → FAIL. Implemented via private control-flow
  exceptions (`_ConfigTamperError`/`_ConfigMismatchError`) caught in `run`'s zone-B block alongside
  `ObservationSealError`, so each maps to its intended status while the existing generic-exception arm
  still yields a signed zone-B-decision ERROR. Every emitted verdict (PASS/FAIL/ERROR) is still
  zone-B-attested and admitted by the REAL `merge_gates`. The discriminator T-M4-04 builds on this
  hardened runner. Touched ONLY `gates/runner.py` + `tests/test_runner_hardening.py` (10 tests); ruff +
  mypy (130 files) clean; target quartet + full suite (1061 passed / 1 skipped) green.

## Trust root — the hermetic SLSA-L3 builder (T-M4-06)
- TB1. **`foundry.trust_root.TrustedBuilder(signer, *, builder_id).build(request, *, components,
  gate_result_uris=(), scanner_config_rev=None, trust_store_rev=None, seq=None) -> BuildOutput`.**
  Turns a pinned `BuildRequest` (schemas/operations.py) into a `BuildOutput` dataclass carrying
  `(result: BuildResult, provenance: SLSAProvenance, sbom: SBOM, signed_verdict: GateResult)`. The
  bodies travel on `BuildOutput` (NOT inline on `BuildResult`) so admission T-M4-07 gets the EXACT
  objects to re-present; `BuildResult` carries only the subject digest + `cas://provenance/` +
  `cas://sbom/` handles + `signature` (= `signed_verdict.runner_attestation`).
- TB2. **Hermeticity is STRUCTURAL, not a runtime guard. "No network" = pinned-inputs-only.** The
  module has NO egress seam at all (no http/resolver/clock) — the ONLY way to pull in something
  unnamed is an un-pinned input, and `_pinned_materials` refuses those fail-closed. The single
  predicate `is_pinned(d)` = `^sha256:[0-9a-f]{64}$` (lowercase hex only — uppercase is NOT canonical
  and is rejected). Refused inputs (raise `NonHermeticBuildError` BEFORE any work): un-pinned
  source_digest / base_image_digest / any material_digest, any unpinned SBOM component
  (`c.digest`), or `request.hermetic is False`. Materials = source + base image + declared
  material_digests, all woven into the predicate's `materials` tuple.
- TB3. **Reproducible subject digest is `content_digest` over the canonical sorted tuple of pinned
  inputs** (`build_type, source_digest, tree_sha, base_image_digest, [material digests in order]`),
  prefixed `sha256:`. Same pinned inputs ⇒ byte-identical subject ⇒ identical `SLSAProvenance`
  identity ⇒ identical handles + signature (test asserts the WHOLE BuildOutput is reproducible).
  Flipping ANY material digest re-keys subject AND provenance (security.py INPUT-BINDING).
- TB4. **Sign by binding the provenance INTO the verdict.** `_sign_build` mints a PASS build
  `GateResult` (`run_against=tree_sha`, `results_digest=subject_digest`) then
  `ZoneBSigner.with_provenance_binding(verdict, provenance)`. Verify with
  `DevGateWriteVerifier.verify_provenance_binding(signed_verdict, provenance)` (the attestation
  provenance-binding seam, T-M4-05): True for the genuine provenance, False for any tampered one
  (different material/subject digest → different `content_identity()` → different MAC). T-M4-07
  admission re-verifies exactly this: signature + the predicate's gate-state for the produced digest.
- TB5. **CAS handles are content-identity-keyed (DS9/L4) so re-deriving yields the same uri.**
  provenance handle = `cas://provenance/<prov.content_identity()>`; SBOM handle is computed over the
  SBOM with `sbom_uri=""` blanked (it's filled FROM the handle, so it can't be in its own identity) →
  `cas://sbom/<keyed.content_identity()>`. Dev-grade HMAC root (plan §0.3) — logic real, root local.
  16 tests; ruff (foundry tests) + mypy (133 files) clean; full suite green (1 skipped). Touched ONLY
  `foundry/trust_root/__init__.py`, `foundry/trust_root/trusted_builder.py`,
  `tests/test_trusted_builder.py`.

## Held-out-mutant + behavioral-invariance discriminator — the malicious-test defense (T-M4-04)
- DC1. **`foundry/gates/discriminator.py` is the machinery behind `mutant_rotation_id` — a green
  pytest run is NECESSARY but NOT SUFFICIENT (gov §1c).** `TestSuiteDiscriminator(mutants,
  invariants, verdict_class, mutant_rotation_id, kill_bar).evaluate(workdir) -> DiscriminatorReport`
  is a PURE observation (no signing power). Two controls: (a) **held-out mutants** — each
  `HeldOutMutant(mutant_id, target, find, replace)` is applied to a COPY of the tree and the agent
  suite is re-run under the SAME pinned harness (`pinned_test_env`); the suite KILLS the mutant iff it
  FAILS (pytest rc==1) on the mutated tree. A suite still GREEN on mutated code (rc 0 or 5) means the
  mutant SURVIVED → the suite tested nothing real. `kill_score = killed/total`; (b) **behavioral
  invariance** — each `BehavioralInvariant(module, call, args, expected)` is checked in a child
  interpreter against the REAL tree (imports the unit, asserts the golden output). The report
  `passed = (kill_score >= required_kill_ratio) AND no invariant violated`.
- DC2. **Gate-class-aware (domains.md §1): `required_kill_ratio(verdict_class, bar)` is the class
  knob.** Class-2 (EXECUTION_DETERMINISTIC) demands the full `bar` (default 1.0 — a single surviving
  mutant is fatal); class-1 (CONFIG_EVALUABLE) + class-3 (STATISTICAL) get ratio 0.0 — the held-out-
  mutant assumption is class-2-only, so the discriminator is vacuously satisfied and never rejects a
  statistical/perceptual test. SAME gamed suite + surviving mutant: class-2 FAILs, class-3 passes.
- DC3. **`TestGate(discriminator=...)` consults it: the discriminator runs in ZONE A (executor, no
  key) ONLY when pytest is green** (a red/no-tests run is already FAIL/ERROR by exit code), folds its
  `model_dump()` report onto the sealed observation under `DISCRIMINATOR_KEY` + surfaces `findings`.
  Zone B `decide`: rc/exit-code semantics + `min_passed` adequacy floor UNCHANGED, then `green AND
  adequate AND discriminator.passed` ⇒ PASS, else FAIL. `mutant_rotation_id` mirrors from the wired
  discriminator so the reuse key + the substitution family agree. No discriminator wired → legacy
  (green suffices). T-M4-11 (Phase-4 exit) drives the gamed-green-suite-CAUGHT path through this.
- DC4. **GOTCHA (cost a flaky debug pass): do NOT `copytree` `__pycache__`/`.pytest_cache` into the
  mutant copy.** The agent suite's own `execute()` run leaves a `calc.pyc` recording the ORIGINAL
  source mtime; a same-second `write_text` of the mutated source can leave the mtime unchanged →
  Python imports the STALE (unmutated) bytecode → the mutant spuriously "survives" → NON-DETERMINISTIC
  kill score (flipped run-to-run). Fix: `shutil.copytree(..., ignore=ignore_patterns("__pycache__",
  ".pytest_cache"))` forces a recompile of the mutated source. An inapplicable mutant (`find` absent)
  is fail-closed: counted as survived, never as killed.
- DC5. 14 tests; ruff (foundry tests) + mypy (133 files) clean; full suite green (1091 passed, 1
  skipped). Touched ONLY `foundry/gates/discriminator.py`, `foundry/gates/test.py`,
  `tests/test_discriminator.py`. The new public types are imported directly from
  `foundry.gates.discriminator` (no `__init__.py` re-export needed this wave).

## Trust root — the fail-closed deploy-time admission controller (T-M4-07)
- AD1. **`foundry.trust_root.AdmissionController(*, builder_id, require_hermetic=True)` is the
  fail-closed deploy gate.** `admit(subject_digest, *, provenance: SLSAProvenance, build_verdict:
  GateResult, gate_results: tuple[GateResult,...], required_gates: tuple[str,...], records:
  GlobalRecordsView, verifier: DevGateWriteVerifier, seq, evidence_pack=None, scanner_config_rev=None,
  gate_state_uri=None) -> AdmissionDecision`. Trusted infra (zone B): NO agent code, NO network, NO
  clock — a PURE function of its re-presented evidence + the cross-fork records view + the verifier,
  so the SAME inputs always yield the SAME `AdmissionDecision` (deterministic; tested incl.
  order-independence). ADMIT iff ALL four checks hold for the EXACT digest; ANY miss ⇒ DENY.
- AD2. **The 4 fail-closed checks, in order (short-circuit to DENY on first failure):**
  (1) **Provenance bound (input-binding)** — `prov.binds_subject(subject_digest)` AND
  `prov.builder_id == self.builder_id` AND (if `require_hermetic`) `prov.hermetic`. THIS is how
  WRONG-DIGEST is denied: a verdict/provenance built for a DIFFERENT digest won't bind the requested
  subject (`_R_PROVENANCE_UNBOUND`). Crucially the integrated `tree_sha` the gate-state keys to is
  read FROM `prov.tree_sha`, NEVER a caller arg — so the gate-state check (#3) is bound to the tree the
  provenance attests. (2) **Build signature re-verified** — `verifier.verify_provenance_binding(
  build_verdict, prov)` re-presenting the EXACT prov; a forged/unsigned build sig OR a tampered
  provenance (different `content_identity()`, even if it still binds the same subject) recomputes a
  different MAC ⇒ DENY (`_R_SIG_INVALID`). (3) **Gate-state complete + for THIS tree** — every name in
  `required_gates` must have a verdict that is `run_against == prov.tree_sha` (input-bound;
  `_R_GATE_WRONG_TREE`), `status is PASS` (`_R_GATE_NOT_PASS`), and re-verifies under
  `verify_attestation` (forged/unsigned ⇒ `_R_GATE_SIG_INVALID`); absent ⇒ `_R_GATE_MISSING`.
  (4) **Not disqualified / fork-divergent** — `records.admission_look(subject_digest) is ADMISSIBLE`,
  else DENY (`_R_DISQUALIFIED`, sets `stable_fail_latched=True`). A scanner-config pin (optional
  `scanner_config_rev=`) is checked after #1: `prov.scanner_config_rev` must match
  (`_R_SCANNER_CONFIG_MISMATCH`). FORGED-VERDICT ⇒ #2/#3; MISSING-GATE ⇒ #3; DISQUALIFIED/LAUNDERED-
  FORK ⇒ #4 (cross-fork digest-disqualification OR stable-FAIL latch, T-M4-02 `admission_look`).
- AD3. **Why admission re-reads `admission_look`, not the live `gates` reducer (M4D2).** The live
  reducer is latest-by-`seq` and legitimately shows PASS after a recovery while WORM holds the digest
  stable-FAIL latched; admission consumes the cross-fork latch query so a fork re-presenting a
  disqualified digest under a fresh `run_id` STILL reads DISQUALIFIED. The gate-state check (#3) reads
  the NEWEST-by-`Seq` per gate name (`_latest_by_name`, mirrors the reducer tiebreak) — sound BECAUSE
  #4 is what holds a latched digest down regardless of any later PASS.
- AD4. **Build verdict is a PROVENANCE-BINDING, not a plain attestation (TR7).** The build's
  `signed_verdict` (from `TrustedBuilder`) carries a provenance-bound sig — re-verify it with
  `verify_provenance_binding(g, prov)`, NOT `verify_attestation` (which returns False for it, TR7).
  The REQUIRED gates (spec/test/security/...) carry plain attestations — re-verify with
  `verify_attestation`. Tests build GENUINE evidence: real `TrustedBuilder.build` for the signed
  provenance-bound build verdict + `ZoneBSigner.with_attestation` for each PASS gate; real
  `DevGateWriteVerifier` + real `GlobalRecords` (`disqualify_digest`/`latch_stable_fail` for the
  fork-divergence cases). `AdmissionDecision` records the EXACT digest + `tree_sha` + grounds
  (`signature_verified`/`all_required_gates_pass`/`required_gates`/`stable_fail_latched`) + deny
  `reason`. **The deployer T-M4-08 deploys ONLY an admitted decision (`decision.admitted()`);
  Autonomous mode T-M4-10 uses admission as the SOLE release gate.**
- AD5. 14 tests; ruff (foundry tests) + mypy (134 files) clean; full suite 1105 passed / 1 skipped.
  Touched ONLY `foundry/trust_root/admission.py`, `tests/test_admission.py`, the `AdmissionController`
  re-export in `foundry/trust_root/__init__.py`, and this note.

## Trust root — the non-agent least-privilege deployer (T-M4-08)
- DP1. **`from foundry.trust_root import Deployer, DeployRefused`. The deployer ships ONLY an
  admitted artifact, idempotently, with rollback — and runs NO agent code (gov §4/§5).** It is the
  LAST mile after build (`TrustedBuilder`) -> admit (`AdmissionController`) -> deploy (`Deployer`).
  `Deployer(target="prod")` is a frozen dataclass whose SOLE field is the deploy `target`.
  `deploy(decision, *, admission_decision_uri, seq, deploy_plan_uri=None) -> (DeployResult,
  ExternalAction)`; `rollback(decision, *, from_digest, admission_decision_uri, seq, ...)` ditto.
- DP2. **Admitted-only is fail-closed: `deploy`/`rollback` REFUSE (`raise DeployRefused`) when `not
  decision.admitted()`** (DENY/INDETERMINATE never ship). The deploy is bound to the EXACT
  `decision.subject_digest` the admission verified — read FROM the decision, NEVER a caller-supplied
  digest (so a caller can't smuggle a different digest under an admit). A rollback's prior-good
  target must ITSELF come from an admitted decision.
- DP3. **Idempotency key = `content_digest({op, subject_digest, target, from_digest})`** (L7 — never
  `hash()`/wall clock). So a kill-then-resume re-derives the SAME key and the keyed `union_actions`
  reducer / outbox coalesces (mirrors the `open_pr` ExternalAction). `op` (`deploy`|`rollback`)
  discriminates so a deploy and a rollback of the same digest never collide; `from_digest` keeps two
  rollbacks landing on one digest from different priors distinct; `target` keeps the same digest to
  two destinations as two distinct (non-coalescing) effects. The deploy rides `EMBEDDED_MARKER` +
  `gate_bearing=True` (a deploy is gate-bearing, never path-3 dedup — DS-R9-F4).
- DP4. **Non-agent / least-privilege = the deployer carries NO signer/verifier/records dependency for
  the deploy.** It consumes a FINISHED `AdmissionDecision` (trusts admission's `admitted()` bit),
  re-derives no verdicts, signs nothing, and its SOLE capability is to emit the deploy/rollback
  `ExternalAction` REQUEST — the outbox relay is the only thing touching the world (NO real cloud
  deploy here, §0.3). Tested by asserting `inspect.signature(Deployer).parameters == {"target"}` and
  that neither method's signature takes a signer/verifier/key. Autonomous mode T-M4-10 deploys via this.
- DP5. 18 tests; ruff (foundry tests) + mypy (135 files) clean; full suite 1121 passed / 1 skipped.
  Touched ONLY `foundry/trust_root/deployer.py`, `tests/test_deployer.py`, the `Deployer`/`DeployRefused`
  re-export in `foundry/trust_root/__init__.py`, and this note.

## Autonomous mode — trust-root admission as the SOLE release gate (T-M4-10)
- AU1. **`MODE_AUTONOMOUS = "autonomous"` reuses the GATED supervisor spine VERBATIM.**
  `build_supervisor_graph` now treats `gated = mode in (MODE_GATED, MODE_AUTONOMOUS)`, so the phase
  topology (tiering + the Design sign-off boundary + the assurance subgraph + the convergence bound)
  is BYTE-IDENTICAL to gated — only the triage `mode` channel records the exact mode (`mode if gated
  else MODE_ASSISTANT`). The DIFFERENCE is entirely in the factory_graph emit node: the release gate
  is the TRUST ROOT (build → admit → deploy), NOT a human/recorded sign-off. The supervisor still
  RECORDS a release SignOffRecord (harmless), but autonomous emit IGNORES it — admission is the gate.
- AU2. **Autonomous emit chain = guard → build → admit → deploy, fail-closed at each step.** (1) GUARD
  on every required gate PASS (terminal Spec+Build+Test + tier-scaled assurance, `_required_gate_names`
  / `_gates_all_pass` newest-by-Seq on the trust-filtered `gates` channel) — a halted/failed/latched
  run fails here and emits NOTHING (no build). (2) BUILD the integrated subject hands-off via
  `TrustedBuilder.build(BuildRequest)` keyed to the integrated `tree_sha` → signed provenance + SBOM.
  (3) ADMIT via `AdmissionController.admit(...)` — **the SOLE release gate**; on `not decision.admitted()`
  emit NOTHING (no deploy). (4) DEPLOY only the admitted decision via `Deployer.deploy(...)` → ONE
  deploy `ExternalAction` (kind="deploy", gate_bearing, payload = `cas://admission/<decision id>`).
  Inject `builder/admission/deployer/gate_verifier` as factory args (tests wire doubles); required
  only for autonomous mode (ValueError if any is None). `gate_verifier` shares the runner's zone-B
  KEY/KEY_ID + `runner.records` so it re-verifies the runner-attested gate-state + reads the
  cross-fork disqualification latch.
- AU3. **Admission's gate-state check is INPUT-BOUND to ONE tree, but the gated spine spans TWO.** The
  terminal gates (Spec/Build/Test) and the assurance gates (Security/Audit/...) run on DISTINCT
  materialized `run_against` trees (the assurance materializer RE-HASHES the augmented tree with
  `changes.diff`, AW2). Admission rejects any required gate whose `run_against != provenance.tree_sha`
  (`required_gate_wrong_tree`). FIX: `_admission_tree_and_gates` groups the attested gates by
  `run_against`, picks the LARGEST group (the integrated tree the most gates share; deterministic
  `(len, tree)` tiebreak), and builds+admits against EXACTLY that tree with EXACTLY the gates bound to
  it (`required_gates = sorted(names in group)`). Every gate admission re-verifies is then honestly
  bound to the provenance tree (no cross-tree laundering); the largest group is the assurance group, so
  the always-on Security+Audit FLOOR is always covered.
- AU4. **DENY deploys NOTHING (fail-closed) — the "denied artifact" test.** Force a DENY by
  `records.disqualify_digest(subject_digest, ...)` so `admission_look(subject_digest)` reads
  DISQUALIFIED — admission DENYs even though the live gate-state is all-PASS, and emit deploys nothing.
  To pre-disqualify the EXACT digest, PROBE: run a clean autonomous build, recover its `tree_sha` via
  `_admission_tree_and_gates`, re-derive the subject digest with `TrustedBuilder.build(_autonomous_
  build_request(state, tree_sha, seq))` (subject_digest is a pure function of the PINNED inputs, NOT the
  seq — so any seq works), then disqualify that digest before the real run. The build inputs are CONSTANT
  dev stand-ins (`_AUTONOMOUS_*`), so the subject digest is a pure function of (integrated tree_sha) —
  resume-stable.
- AU5. **Idempotency holds via the deployer's key (DP3), NOT a graph-side key.** `Deployer.deploy`
  derives `content_digest({op, subject_digest, target, from_digest})` — process-stable — so
  kill-then-resume re-invoking the same `thread_id` re-runs the chain, re-derives the SAME subject digest
  (pure-from-pinned-inputs) → SAME deploy key → `union_actions` coalesces: exactly ONE deploy. A
  halted/DENY'd run produced no action to coalesce.
- AU6. **Back-compat: assistant + gated emit paths are UNCHANGED.** Node selection is
  `autonomous_emit if autonomous else (gated_emit if gated else emit)`; the supervisor's gated branch is
  unchanged. 4 new autonomous tests; the existing factory_graph / factory_graph_gated / supervisor /
  supervisor_phase2 suites pass untouched. Touched ONLY `foundry/graph/supervisor.py`,
  `foundry/graph/factory_graph.py`, `tests/test_factory_graph_autonomous.py`. ruff (foundry tests) +
  mypy (135 files) clean; full suite 1125 passed / 1 skipped.

## M4 trust-root review fixes — admission re-verifies ALL gates across BOTH trees (M4-R1-01/02/03)
- M4R1. **Admission now CRYPTOGRAPHICALLY re-verifies EVERY required gate across BOTH materialized
  trees — SUPERSEDES AU3's largest-group-only approach.** AU3 built+admitted against ONLY the largest
  `run_against` group (the assurance tree); the terminal Spec/Build/Test gates ran on a DISTINCT tree
  (AW2 re-hash) and were NOT re-verified by admission — they rested on the upstream emit guard's
  fail-closed `merge_gates` filter. FIX: `AdmissionController.admit` gained an optional
  `expected_gate_trees: Mapping[str, str]` — a per-gate name→tree mapping pinning EACH required gate to
  the `run_against` it is supposed to have run on. The gate-state loop now checks
  `g.run_against != trees.get(name, tree_sha)` (defaults to `provenance.tree_sha` when a gate is absent
  from the mapping — so single-tree callers pass NOTHING and every gate is checked against the prov tree,
  ORIGINAL behavior unchanged + all existing single-tree tests still pass). `_admission_tree_and_gates`
  now returns `(prov_tree, ALL_attested_gates_across_both_trees, expected_gate_trees)`: the build is still
  keyed to the largest group (provenance binds that integrated tree), but admission gets the FULL gate-set
  + the per-gate expected trees (each gate→its actual `run_against`, newest-by-Seq per name to match
  admission's own `_latest_by_name` selection). So a forged / missing / WRONG-TREE TERMINAL gate now ALSO
  fails admission (`required_gate_signature_invalid` / `_missing` / `_wrong_tree`), not just the emit guard.
- M4R2. **Mandatory floor asserted before build/admit (M4-R1-02).** `MANDATORY_ADMISSION_FLOOR =
  TERMINAL_PASS_GATES | {"security","audit"}` (the always-on integrity floor + terminals). The autonomous
  emit fail-closes (`return {"phase":"halted"}`) if `not MANDATORY_ADMISSION_FLOOR.issubset(required_gates)`
  — so admission ALONE enforces a floor, not merely whatever gate-state happened to land. (Use
  `.issubset()` not `floor <= frozenset(...)` — ruff SIM300 flags the latter as a Yoda condition.)
- M4R3. **gate_result_uris labeling fix (M4-R1-03).** The `gate_result_uris` fed to `builder.build` are
  the attested verdicts' `results_digest` content identities, NOT WORM gate-result HANDLE uris (gov §4).
  Left the values as-is (the digests are valid content-addressed build inputs; provenance binding intact)
  but added a comment that the real WORM-anchored handle URIs wire in when the WORM store lands (§0.3).
- M4R4. **New DENY tests for the TERMINAL gate across two trees.** `test_admission.py`: multi-tree clean
  ADMIT + forged / missing / wrong-tree TERMINAL gate DENY (additive — single-tree tests untouched).
  `test_phase4_exit.py`: `test_forged_terminal_gate_across_trees_cannot_reach_prod` +
  `test_wrong_tree_terminal_gate_across_trees_cannot_reach_prod` (terminal gates on the terminal tree,
  assurance on the prov tree; a forged/wrong-tree terminal verdict → admission DENY → deployer refuses →
  no deploy). The four-attack defenses + clean control stay green; clean autonomous run still deploys once.
  Touched ONLY admission.py + factory_graph.py + the 3 test files + LEARNINGS. ruff/mypy clean; full suite
  1146 passed / 1 skipped.

## Phase-4 (Trust root & Autonomous) EXIT integration test — the honesty map (T-M4-11)
`tests/test_phase4_exit.py` proves the four roadmap-named attacks CANNOT reach a deploy, each driven by
the REAL component (non-vacuous) and each paired with a control that DOES deploy (load-bearing). 15 tests.
- P4X1. **Each attack's BLOCKING verdict is produced by the real component, not a hand-flipped status.**
  (1) Malicious-test: the real `TestGate(discriminator=TestSuiteDiscriminator(mutants=[ADD_MUTANT]))` over a
  GAMED_SUITE attests FAIL (mutant SURVIVED); (2) forged-results: the real hardened `TwoZoneGateRunner`
  with a `PinnedScannerConfig` re-derives the pin and attests FAIL on a weakened ran-rev (and ERROR on a
  swapped sealed observation); (3) forged-verdict: the real `AdmissionController.admit` DENYs an unsigned
  gate attestation (`required_gate_signature_invalid`) and a wrong-tree verdict (`required_gate_wrong_tree`);
  (4) fork-divergence: real `GlobalRecords.disqualify_digest` / `latch_stable_fail` → real `admission_look`
  DISQUALIFIED → admission DENYs despite valid signatures + all-PASS gate-state.
- P4X2. **Propagation to "no deploy" reuses the REAL autonomous emit node, extracted via `app.nodes["emit"]`
  off a compiled `build_factory_graph(..., mode=MODE_AUTONOMOUS)`.** Build the graph with the real
  trust-root chain (TrustedBuilder/AdmissionController/Deployer/DevGateWriteVerifier), then `node.invoke(state,
  config)` over a HAND-ASSEMBLED gate-state so the blocking verdict is the real component's output while the
  build→admit→deploy chain runs verbatim. The emit closure reads only `state["gates"]` (admitted through the
  REAL `merge_gates`) + a `feature_diff` subject handle + `risk_tier` (None → required gates
  {spec,build,test,security,audit}). Attacks 1-2 splice the genuine FAIL/ERROR verdict into the gate-state →
  guard halts (`{"phase": "halted"}`, no deploy); attacks 3-4 go through admission directly + assert the
  `Deployer` REFUSES (`DeployRefused`) a non-admitted decision.
- P4X3. **GENUINE-E2E vs DEV-GRADE-ROOT stand-ins (§0.3).** Genuine end-to-end: the discriminator, the
  two-zone runner hardening, `merge_gates` admittance, admission re-verification, the cross-fork
  `GlobalRecords`, the deployer's admitted-only `DeployRefused`, and the FULL compiled autonomous graph
  (the clean control `test_clean_autonomous_run_deploys_exactly_once` reuses the T-M4-10 harness whole →
  exactly ONE deploy). Dev-grade: the symmetric-HMAC `ZoneBSigner`/`DevGateWriteVerifier` (prod: air-gapped
  signer / keyless OIDC), the constant pinned hermetic build inputs, and the SURROUNDING required-PASS
  gate-state in the propagation tests — genuine `ZoneBSigner.with_attestation` records (mirrors
  `test_admission.py`), not full materialized-tree gate runs. Only the BLOCKING verdict needs to be real.
- P4X4. **Outage → PAUSED not WEDGED reuses `trusted_component` + `trusted_lifecycle` verbatim.**
  `classify_for_dependencies` over an `AvailabilityModel` with the zone-B signer down → PAUSED(
  TRUSTED_COMPONENT_UNAVAILABLE), $0/hr, not alertable; `on_recovery` → RUNNING (the round-trip). HA:
  `classify_ha` fails a single-instance outage over to a healthy replica → RUNNING; all-down → STAYS PAUSED.
  Never WEDGED in any branch.
- P4X5. **Cross-test reuse without duplication.** Imports the autonomous harness helpers
  (`_build_autonomous`, `_config`, `_deploy_actions`, `_gates_by_name`, `_src`, `KEY`, `KEY_ID`,
  `BUILDER_ID`, `_CLEAN_DIFF`) from `tests/test_factory_graph_autonomous` rather than re-deriving them.
  Touched ONLY `tests/test_phase4_exit.py` + this note. ruff (foundry tests) + mypy (135 files) clean;
  full suite 1140 passed / 1 skipped.

## Discriminator wired into the COMPILED Test gate via a DiscriminatorFactory (A-M4-01 fix)
- DW1. **The gap A-M4-01 found: the discriminator was proven in `test_phase4_exit` but NOT plumbed into
  the SHIPPING gate node.** `gate_wiring.run_build_test_gates` constructed `TestGate(min_passed=...)`
  WITHOUT a discriminator, so the compiled autonomous/gated spine relied on the adequacy floor ALONE for
  a gamed-green suite — only the unit test exercised the held-out-mutant block. Fix: thread an OPTIONAL
  `DiscriminatorFactory` (`= Callable[[], TestSuiteDiscriminator]`, the alias already defined at the
  bottom of `gates/discriminator.py`) end to end so the COMPILED Test gate consults it.
- DW2. **The thread: `build_factory_graph(..., discriminator_factory=None)` -> `build_supervisor_graph(
  ..., discriminator_factory=...)` -> the `build_test_gate` node -> `run_build_test_gates(...,
  discriminator_factory=...)`.** In `run_build_test_gates`, `discriminator = discriminator_factory() if
  discriminator_factory is not None else None`, then `TestGate(min_passed=min_passed,
  discriminator=discriminator)`. The factory produces a FRESH `TestSuiteDiscriminator` per gate run (the
  curated held-out mutants + behavioral invariants for the subject under test); the gate calls
  `discriminator.evaluate(workdir)` in zone A over the materialized integrated tree (only when pytest is
  green — DC3). So `green AND adequate AND discriminator.passed` ⇒ PASS in the SHIPPING graph; a
  gamed-green suite that clears the floor but does not kill a held-out mutant ⇒ attested Test FAIL ⇒ the
  boundary halts ⇒ the autonomous emit guard deploys NOTHING.
- DW3. **BACK-COMPAT is the `None` default at EVERY hop.** The param is the LAST keyword-only argument on
  all three functions with a `None` default; absent a factory the constructed gate is the legacy
  `TestGate(min_passed=...)` (no discriminator) — byte-for-byte the prior behavior. Every existing
  caller / test (Assistant / Gated / Autonomous, `test_gate_wiring`, `test_supervisor*`,
  `test_factory_graph_*`) is unchanged and stays green. The `DiscriminatorFactory` type is imported under
  `TYPE_CHECKING` in all three modules (no runtime import cycle; the alias lives in `gates/discriminator`).
- DW4. **The END-TO-END proof through the COMPILED graph (`test_phase4_exit`, the A-M4-01 re-audit
  evidence).** Two full autonomous-spine runs with the SAME `_e2e_discriminator_factory` (curated
  `add->sub` held-out mutant on `app.py`) wired via `build_factory_graph(discriminator_factory=...)`:
  (a) a GENUINE suite that kills the mutant -> compiled Test PASS -> build -> admit -> EXACTLY ONE deploy
  (control); (b) a GAMED-GREEN suite that clears the adequacy floor (one passing test) but survives the
  mutant -> the COMPILED Test gate attests FAIL (`runner_attestation is not None`, findings contain
  `SURVIVED`) -> boundary halts -> `_deploy_actions(final) == []`, `phase == "halted"`. This proves the
  malicious-test block is in the SHIPPING path, not just the unit test. GOTCHA: `test_phase4_exit`
  defines its OWN keyless `_runtime()`; the autonomous spine needs the SCRIPTED `MockTransport`, so import
  the harness's runtime/runner as `_auto_runtime` / `_auto_runner` (a bare `_runtime` import is shadowed
  by the local def). The diff hunks mirror the StubAdapter shape (first body line `+`-prefixed, rest
  verbatim) so `gate_wiring`'s Phase-1 applier round-trips them.
- DW5. **HONESTY (§0.3): the held-out mutant SET is the dev-grade CURATED set the factory supplies** — a
  real family-decorrelated mutation engine is heavier (the discriminator's own dev-grade scope, DC1). The
  WIRING + the discriminator logic are real; only the mutant generator is the documented stand-in.
  Touched ONLY `foundry/graph/gate_wiring.py`, `foundry/graph/factory_graph.py`,
  `foundry/graph/supervisor.py`, `tests/test_phase4_exit.py`, this note. ruff (foundry tests) + mypy
  (135 files) clean; full suite 1148 passed / 1 skipped.

## Domains — the DomainPackContract extension seam (T-M5-01)
- DM1. **The seam lives in `foundry/domains/{contract,registry}.py` + `__init__.py` ONLY.** A domain
  pack (regulated/ML/frontend — T-M5-05/06/07) is added with `registry.register(NewPack())` and routes
  by capability — NO edit to contract.py/registry.py/core. That is the testable open/closed invariant:
  the test defines a FRESH `BrandNewPack(BaseDomainPack)` class inline, registers it, and asserts it
  routes + tightens with zero core change. T-M5-08 wires the registry into the graph.
- DM2. **The 7 hooks are typed methods returning MONOTONE policy objects (frozen pydantic, L3), each
  with a SAFE DEFAULT on `BaseDomainPack` (the no-op identity).** A pack overrides only what it tightens:
  (1) `assign_verdict_class -> VerdictClassPolicy` (per-key class 1-5), (2) `state_inputs ->
  StateInputPolicy` (data/feature/weight refs into `state_sha`), (3) `compute_budget -> ComputePolicy`
  (expensive-gates + proxy/confirm + per-fix cap), (4) `mode_tier_policy -> ModeTierPolicy` (vetoed_modes
  + min_tier + force_human/two_person Release), (5) `approver_of_record -> ApproverPolicy` (bound
  artifact classes), (6) `egress_profile -> EgressPolicy` (no third-party model/trace egress / no PHI),
  (7) `perceptual_hitl -> PerceptualHITLPolicy` (HITL-required caps + rendered evidence). All take an
  advisory `request: Mapping` view; hooks are PURE (no I/O / state mutation). `DomainPackContract` is a
  `@runtime_checkable Protocol`, so a pack need not subclass — but `BaseDomainPack` gives the defaults.
- DM3. **Each policy is `tighten`/`relaxes`/`identity` — strictest-wins JOIN + a floor probe.** The
  registry's `compose(request)` routes (intersect caps) then folds each hook through `tighten` (seeded
  with identity, strictest-wins) into a `ComposedPolicy`. Two axis SHAPES: UNION/OR axes (vetoed_modes,
  refs, gates, bound classes, egress flags, HITL caps) — the join only GROWS them, so they can NEVER
  relax through composition (`relaxes` returns False; a relaxing override has NO EFFECT, the floor is
  preserved); DISPLACING axes (min_tier picks stricter, per-fix cap picks lower, per-key verdict class
  picks stricter) — a contribution SPECIFYING a strictly-weaker value than the composed floor is
  REJECTED `TightenError` (`tighten_only`). So "a pack can only tighten" is enforced two ways: no-effect
  on union axes, rejection on displacing axes.
- DM4. **`_fold` is TWO-PASS for order-independence of BOTH the result AND the verdict.** Pass 1 joins
  all contributions (commutative/associative/idempotent → order-free result). Pass 2 checks each
  contribution against the FINAL composed floor (not a running one) — so a STANDARD-tier pack alongside a
  HIGH-tier pack raises whichever registered first. **An identity contribution is a no-opinion no-op:
  skipped without a relax check** (a pack tightening axis A while defaulting axis B must not trip B's
  guard against a floor another pack raised on B). Corollary: the DISPLACING `relaxes` methods treat the
  axis identity (min_tier=LOW, cap=None, empty verdict map) as no-opinion → never relaxes — needed so the
  unit-level `identity().relaxes(strict_floor) is False` holds independent of the fold's skip.
- DM5. **Mode veto = a permissiveness FLOOR, not a deletion.** `ModeTierPolicy.vetoed_modes` +
  `allowed_modes(candidates)` make Autonomous/auto-approve-Assistant UNREACHABLE by union-growing the
  veto set; the regulated pack pins min_tier=HIGH + force_human_release + two_person_release. Verdict
  classes are ranked config<exec<statistical<perceptual<human-witnessed (strictest = a named human must
  attest), so a pack reroutes a criterion to a STRICTER grounding but can never downgrade one.
- DM6. **mypy: the strictest-wins fold needs PEP-695 self-bound generics.** `_fold[T: _Monotone[Any]]`
  with `_Monotone[P](Protocol)` declaring `tighten(other: P) -> P` + `relaxes(floor: P) -> bool` types
  the fold result as the concrete policy T (a plain `_Monotone[_P]` TypeVar made `compose` args
  incompatible). Route input is `Mapping | Iterable[str]`; narrow with `isinstance(x, Mapping)` (a custom
  `_is_mapping` helper does NOT narrow for mypy). Touched ONLY `foundry/domains/__init__.py`,
  `contract.py`, `registry.py`, `tests/test_domains_contract.py`, this note. ruff (foundry tests) + mypy
  (138 files) clean; suite 1172 passed / 1 skipped; 24 new tests.
- DM7. **ML state_sha data/weight binding + the compute budget (T-M5-03, domains.md §2-3).**
  `foundry/domains/state_sha.py`: `ml_state_sha(base_tree_sha, *, data_refs, weight_refs, feature_defs,
  require_binding=True)` folds a pack's content-identity refs onto the source-only `tree_sha` by REUSING
  `foundry.state.tree_sha.state_sha` (base rides as `source_tree`; lockfile/toolchain are already in the
  base so they pass fixed marker constants — the REF SETS are what re-key). A data/weight/feature change
  → a different key → the cached class-3/ML verdict is invalidated (forced re-draw); same refs → same key
  (deterministic; sort+dedup inherited from `state_sha`, so order/dup don't perturb). FAIL-CLOSED: a key
  over ZERO refs IS the source-only tree_sha (reusable across a data change), so minting an *ML* key from
  it raises `UnboundMLStateError` — an ML verdict must bind its data/weight. `ml_state_sha_from_policy(base,
  StateInputPolicy)` keys off the composed `state_inputs` hook; `ml_verdict_rebinds(base, old=, new=)` is
  the re-key-vs-cache-hit predicate (`require_binding=False` — it's a key COMPARISON, either side may be
  empty). The compute-cost budget is the USD `BudgetController` ANALOGUE for an integer dimension DISTINCT
  from micro-USD: `ComputeBudget(cap_units, per_gate_units, default_gate_units)` + `ComputeBudgetController`
  with `authorize(gate)` (boundary decision, no commit — mirrors `authorize_superstep`) and `charge(gate)`
  (books + FAILS CLOSED: raises `ComputeOverspendError` and books NOTHING when spent+charge>cap — the gate
  pauses/escalates, no silent overspend). The ml/data pack (T-M5-06) keys its class-3 gate on `ml_state_sha`
  and runs its expensive proxy/retrain gates under a `ComputeBudgetController`. Touched ONLY
  `foundry/domains/state_sha.py` + `tests/test_domains_state_sha.py` + this note. ruff (foundry tests) clean;
  `mypy foundry/domains/state_sha.py` clean (the `mypy foundry` error in the concurrently-authored sibling
  `verdict_class.py` is not this task's file); 19 new tests green; full suite green (1 skipped).
- DM8. **Verdict-class gate requirements — how a pack's assigned class 1-5 is ENFORCED (T-M5-02,
  domains.md §1).** `foundry/domains/verdict_class.py`: `gate_requirements_for(VerdictClass) ->
  GateRequirement` (pure, fixed per class) turns the composed `VerdictClassPolicy` assignment into the
  gate-class-aware requirement the supervisor/gates enforce. Per class: **1/2 = MACHINE** (existing
  zone-B re-derive / discriminator; pass-through, auto-approvable, in predicate); **3 = ALPHA_STATE_SHA**
  (auto-approvable — runner re-derives, in predicate); **4 = APPROVER_OF_RECORD** (NOT auto-approvable,
  HITL, in predicate); **5 = PERCEPTUAL_HITL** (NOT auto-approvable, HITL, EXCLUDED from the signed
  predicate). `autonomous_reachable(vc)` is False ONLY for 4+5. **Class-3 fail-closed:**
  `assert_class3_binds_ledger(Class3Binding|None, AlphaLedgerView)` REUSES the eval alpha-ledger
  (`GlobalRecords.alpha_looks_spent` over `(eval_set_identity, decision_id)` — NOT state_sha) — raises
  `InvalidClass3BindingError` unless the look is actually DRAWN in the ledger (frozen, not self-asserted)
  AND a non-empty per-look `state_sha` is cited (a data/model change re-draws). I needed a local
  `AlphaLedgerView` Protocol — `alpha_looks_spent` is on `GlobalRecords` but NOT the `GlobalRecordsView`
  Protocol (which is the reducer-facing latch slice), so typing against the full view fails mypy. **Class 4
  + 5** both REUSE `governance.sign_off.release_allowed` (same anchors: subject/state_sha/evidence_hash/
  nonce): `class4_attested(...)` / `class5_hitl_satisfied(...)` are True ONLY with a recorded approved
  sign-off binding the EXACT decision — a missing/None record is a BLOCK, never an auto-approve; a sign-off
  over a different artifact/bundle/nonce never satisfies. **Higher-class-can't-be-satisfied-by-lower:**
  `satisfies(required, gate_class)` is `_VERDICT_CLASS_STRICTNESS[gate] >= [required]` (reuses the
  contract's SAME strictness order: config<execution<statistical<perceptual<human-witnessed) — a class-4
  key is NOT satisfied by a green class-2 machine gate; a stricter gate satisfies a weaker requirement
  (rerouting only tightens). The packs T-M5-05/06/07 ASSIGN classes via the contract; THIS enforces them
  (T-M5-08 wires it at the gate seam). Touched ONLY `verdict_class.py` + `tests/test_domains_verdict_class.py`
  + this note. ruff (foundry tests) clean; `mypy foundry/domains/verdict_class.py` clean; 24 new tests
  green; full suite green (1240 passed / 1 skipped). NB: the pre-existing `mypy foundry` error is in the
  concurrently-authored sibling `governance_hooks.py`, not this task's file.
- DM9. **The 4 core governance-hook ENFORCEMENT helpers — mode/tier veto + approver-of-record + egress +
  perceptual-HITL (T-M5-04, domains.md §4(2-6)).** `foundry/domains/governance_hooks.py` is the CORE
  machinery the composed `ModeTierPolicy`/`ApproverPolicy`/`EgressPolicy`/`PerceptualHITLPolicy` DRIVE
  (the pack DECLARES via the contract + registry; THIS enforces, fail-closed + tighten-only). All four
  are PURE/deterministic and REUSE tiering + sign_off (never rebuild them):
  (d) `resolve_effective_mode_tier(policy, requested_mode, request, ..., regulatory_significant=None)
  -> EffectiveModeTier`: the base tier is `tiering.classify_tier` (reused) then RAISED to the STRICTER of
  it and `policy.min_tier` (`min_tier=LOW` can NEVER lower a HIGH classifier verdict); a vetoed mode is
  removed from `RELEASE_MODES` (autonomous>assistant>gated permissiveness) — a fully-vetoed set degrades
  to `{gated}`, NEVER "anything goes"; the resolver caps the effective mode at `requested_mode` so a Gated
  request can't be widened to Autonomous (tighten-only); the **regulatory-significance axis** (explicit
  override OR a `regulatory_significant`/`regulatory_significance` request-view flag) forces the strict
  path independent of diff size — discards autonomous+assistant (→ `{gated}`), floors the tier at HIGH,
  forces human + two-person.
  (e) `bind_approver_of_record(...)` reuses `sign_off.record_sign_off` to RECORD a NAMED human approver
  for a class in `policy.bound_artifact_classes`; `_is_human_approver` REJECTS the `autonomous`/`agent`/
  `self-judge`/`system`/`""` sentinels (Regulated-F2: agent-identity class-4 write rejected), so an
  Autonomous auto-approve is unreachable; two-person requires a SECOND distinct identity in a DISTINCT
  role. `release_admitted(policy, records, ...)` is the fail-closed admission: no recorded approved HUMAN
  sign-off binding THIS (subject, state_sha) ⇒ `False` (no approver ⇒ no release); an unbound class is a
  no-op `True`.
  (f) `egress_within_profile(policy, EgressRequest) -> EgressVerdict` (+ raising `assert_*`): a request
  egress axis the profile FORBIDS (third-party model/trace egress, real PHI/PAN) is a fail-closed DENY
  with violations enumerated; an identity profile forbids nothing (tighten-only).
  (g) `perceptual_hitl_required(policy, request, capabilities) -> PerceptualHITLRequirement`: a request
  capability ∩ `policy.hitl_required_capabilities` (e.g. `ui`) ⇒ `required=True`, `auto_approvable=False`
  ALWAYS, `require_rendered_evidence=True`. `mark_perceptual_hitl(...)` RAISES `SignOffError` unless a
  RECORDED, approved human sign-off is supplied — a class-5 visual HITL is NEVER auto-approved (UX-F1/F2);
  a non-matching request is a no-op `None`. Touched ONLY `governance_hooks.py` +
  `tests/test_domains_governance_hooks.py` + this note. ruff (foundry tests) + `mypy foundry` clean; 25
  new tests green; full suite green (1240 passed / 1 skipped). T-M5-08 wires these at triage/release.

## ML/data domain pack (T-M5-06, domains.md §5 "ML / data systems")
- ML1. **`foundry/domains/packs/ml_data.py` — `MLDataPack(BaseDomainPack)` engages on
  `{ml, data, model, training, ai}` and tightens THREE hooks + an egress hint; it edits NO core and
  REUSES the existing machinery verbatim (`ml_state_sha`/`ml_state_sha_from_policy`,
  `assert_class3_binds_ledger` + `GlobalRecords` alpha ledger, `ComputeBudgetController`).** Registers
  via `PackRegistry.register(MLDataPack())` and routes by capability — the open/closed seam, no
  contract/registry/graph edit (proven by a fresh `PackRegistry` routing to it in the test).
- ML2. **`assign_verdict_class` marks `MODEL_EVAL_GATE="model_eval"` class 3 (STATISTICAL) ONLY when the
  request actually carries an ML capability** (`request_capabilities(request) & _ML_CAPABILITIES`),
  else returns the identity — so a misrouted/non-ML request is never spuriously forced onto the
  statistical class. `gate_requirements_for(STATISTICAL).grounding is ALPHA_STATE_SHA`
  (`requires_alpha_state_sha=True`): a class-3 verdict is grounded ONLY if it binds a FROZEN alpha look
  (`(eval_set_identity, decision_id)` ledger) AND a `state_sha` — both re-checked fail-closed by
  `assert_class3_binds_ledger` (absent binding / empty `state_sha` / undrawn look all raise
  `InvalidClass3BindingError`).
- ML3. **`state_inputs` reads `data_refs`/`feature_defs`/`weight_refs` off the REQUEST view** (defensive:
  absent/None → empty frozenset) and folds them into `state_sha` via `ml_state_sha_from_policy`, so the
  model-eval verdict is BOUND to its data+weights: a data/feature/weight change RE-KEYS the verdict
  (old key ≠ new key; `ml_verdict_rebinds(...) is True`) — a verdict for D/W can't be reused for D'/W'.
  Same refs → same key (cache hit, `ml_verdict_rebinds is False`). The `StateInputPolicy` join is a
  pure union (tighten-only).
- ML4. **`compute_budget` declares `{model_eval, retrain}` as `expensive_gates` + `require_proxy` +
  `confirm_escalation` + a per-fix compute cap** (compute UNITS, distinct from USD; the `ComputePolicy`
  field is `per_fix_compute_cap_units` — renamed from the misleading `per_fix_compute_cap_microusd`
  per M5-R1-03, since `state_sha.ComputeBudget` meters integer compute units, not micro-USD).
  Mapped onto a `ComputeBudget(cap_units=cap, per_gate_units={...})` the `ComputeBudgetController`
  fail-closes on overspend: `authorize` denies + `charge` raises `ComputeOverspendError` and books
  NOTHING past the cap (the expensive gate pauses/escalates rather than silently overspending).
- ML5. **Alpha-ledger REUSE proof:** `GlobalRecords.try_first_alpha_look(eval, dec, state, run_id)`
  returns True on the first draw and False on a SECOND draw of the SAME `(eval_set_identity,
  decision_id)` (forking/retraining can't re-draw; ledger reused, not rebuilt) — `alpha_looks_spent`
  stays 1; a DISTINCT `decision_id` draws independently. The pack adds `egress_profile` →
  `no_third_party_trace_egress=True` (training-data lineage hint, OR flag). ruff (`ml_data.py` +
  test) + `mypy foundry` clean; 16 new tests green; full suite green (NB: sibling untracked packs
  `frontend.py`/`regulated.py` carry their OWN pre-existing ruff nits — not in scope here).

## The REGULATED domain pack — first first-party pack (T-M5-05)
- DM9. **`RegulatedPack(BaseDomainPack)` (`foundry/domains/packs/regulated.py`) is the regulated/
  safety-critical pack (domains.md §5); it OVERRIDES EXACTLY 4 of the 7 hooks and inherits the
  identity default on the other 3 — registers + routes via `PackRegistry.register(RegulatedPack())`
  with ZERO core edit (open/closed seam).** Serves caps `{regulated, fintech, healthcare, finance,
  phi, pii}` (`REGULATED_CAPABILITIES`). The overrides (each TIGHTENS only — `relaxes(...)` is False):
  (1) `mode_tier_policy` -> `vetoed_modes={autonomous, assistant}` (auto-approve vetoed too),
  `min_tier=HIGH`, `force_human_release=True`, `two_person_release=True`. So
  `resolve_effective_mode_tier(...).effective_mode == MODE_GATED`, `autonomous_reachable is False`,
  `reachable_modes == {gated}` — Autonomous UNREACHABLE for a regulated request regardless of
  requested mode. (2) `approver_of_record` -> binds class-4 classes
  `{design, test_protocol, risk_analysis, validation_report, release}`; `release_admitted(...)` is
  fail-closed (no recorded HUMAN sign-off => False; a non-human/`autonomous`/`agent`/`self-judge`/
  `system` approver is rejected; two-person needs 2 distinct humans in 2 distinct roles). (3)
  `assign_verdict_class` -> maps `{release, sign_off, validation, design_review, risk_analysis}` ->
  `HUMAN_WITNESSED` (class 4): `gate_requirements_for(...)` has `requires_approver`/`requires_hitl`
  True, `auto_approvable` False, and `satisfies(HUMAN_WITNESSED, EXECUTION_DETERMINISTIC) is False`
  (a green machine gate never satisfies a class-4 key — self-judge unreachable). (4) `egress_profile`
  -> all three OR flags True (no 3rd-party model/trace egress, no real PHI/PAN). The hooks are
  UNCONDITIONAL (`del request`) — engagement is by CAPABILITY ROUTING, not a per-request branch (the
  deterministic non-agent trigger lives upstream, Regulated-F4a; an agent can't self-scope out). Test
  pattern: instantiate + register IN the test (`PackRegistry().register(RegulatedPack())`), drive the
  composed policy through the `governance_hooks`/`verdict_class` enforcers. Touched ONLY `regulated.py`
  + `tests/test_pack_regulated.py` + this note. ruff (both files) + `mypy foundry` clean; 18 new tests
  green; full suite green.

- DM10. **`FrontendPack(BaseDomainPack)` (`foundry/domains/packs/frontend.py`) is the FRONTEND/UX
  pack — caps `{ui, frontend, ux, web, mobile, visual}`; overrides ONLY 2 hooks (identity default on
  the other 5), registers + routes via `PackRegistry.register(FrontendPack())`, NO core edit.** (1)
  `perceptual_hitl` -> a class-5 mandatory NON-auto-approvable visual HITL over a zone-B-signed
  RENDERED-EVIDENCE bundle (`hitl_required_capabilities=caps`, `require_rendered_evidence=True`),
  consumed by the EXISTING `governance_hooks.perceptual_hitl_required`/`mark_perceptual_hitl` —
  taste is never delegated to a model, so no machine path clears it; no recorded human sign-off ⇒
  `mark_perceptual_hitl` RAISES `SignOffError` (fail-closed, never auto-approved). The class-5
  `gate_requirements_for(PERCEPTUAL)` already carries `auto_approvable=False`,`requires_hitl=True`,
  `in_signed_predicate=False` — assert those (don't rebuild). (2) `assign_verdict_class` maps THREE
  gate keys: `frontend.perceptual`->class 5; `frontend.visual_regression`->class 3 (STATISTICAL,
  device/breakpoint/locale matrix); `frontend.a11y.wcag`->class 1 (CONFIG_EVALUABLE, deterministic +
  BLOCKING). The AT-user screen-reader step is a published OUT-OF-BAND human marker
  (`AT_USER_OUT_OF_BAND`), not a gate — the WCAG class-1 gate is the machine floor. (3) The
  visual-regression key is a RENDER `state_sha` (`render_state_sha`): matrix cells ride as
  `data_refs`, the blessed baseline as a `feature_def` through the EXISTING `tree_sha.state_sha`
  (sorted+deduped, so cell order can't perturb the key; ANY cell/baseline change re-keys -> re-draw).
  (4) SUB-PIXEL-ONLY flake envelope (`envelope_absorbs(RenderDelta)`): absorbs `sub_pixel_diff` ONLY;
  a `layout_delta`/`affordance_delta`/`locale_delta` is NEVER absorbed -> FAILS the cell (routes to
  class-5 re-baseline) EVEN with a tiny pixel diff (no "perceptual-distance-alone" license, UX-F2a/F4a).
  NB: a `_engaged` capability guard on the two hooks is belt-and-suspenders (the registry only calls a
  pack's hooks when its caps intersect the request); DM9's regulated pack instead leaves hooks
  unconditional. Both are fine. Touched ONLY `frontend.py` + `tests/test_pack_frontend.py` + this note.
  ruff (both files) + `mypy foundry` clean; 23 new tests green; full suite 1297 passed / 1 skipped.

## Domain-pack engagement at triage + composed policy at release — the SINGLE graph-core seam (T-M5-08)
- DM11. **`build_supervisor_graph(..., pack_registry=None)` + `build_factory_graph(..., pack_registry=None)`
  are the ONE graph-core edit that makes the 3 packs take effect end-to-end. `None` (DEFAULT) engages
  NO packs → EXACT prior behavior (back-compat; every Assistant/Gated/Autonomous test unchanged).** The
  registry is the SEAM: this wiring reads the COMPOSED policy GENERICALLY, never pack-by-pack — so a 4th
  pack is `registry.register(pack)` with ZERO wiring edit (proven by a `_StubPack` test the core never saw).
- DM12. **TRIAGE engages the registry (only on the gated spine, `mode in {gated, autonomous}`).** When a
  registry is passed, triage calls `pack_registry.compose(state)` (routes by `state['required_capabilities']`,
  strictest-wins) → `resolve_effective_mode_tier(policy.mode_tier, requested_mode=mode, request=state, ...)`.
  The EFFECTIVE mode (capped by the veto) is stamped on the `mode` single-writer channel; the `risk_tier`
  is `classify_tier_at_floor(...)` (NEW in `tiering.py`: classify then TIGHTEN up to the policy floor +
  OR the two-person flag — tighten-only, `relaxes_integrity()` still False). A regulated request requested
  as Autonomous → `effective_mode == gated`, `autonomous_reachable is False`, tier HIGH, two-person on.
- DM13. **RELEASE/emit RE-DERIVES the composed policy (a pure fn of caps) and enforces it fail-closed — no
  new state channel needed.** `pack_release_admitted(state, config, registry, requested_mode=)` in
  `factory_graph.py`: (1) approver-of-record — `release_admitted(policy.approver, sign_offs, artifact_class=
  "release", ...)` rejects a self-judge/agent/autonomous approver (no recorded HUMAN ⇒ no release); (2)
  perceptual HITL — if `perceptual_hitl_required(policy.perceptual_hitl, request=state)`, require a recorded
  approved HUMAN visual sign-off via `class5_hitl_satisfied` (a self-judge clean run is BLOCKED — never
  auto-approved). All 3 emit nodes (assistant/gated/autonomous) AND-in this guard. The autonomous emit ALSO
  checks `autonomous_pack_reachable(...)`: when the veto makes Autonomous unreachable AND the human-approver
  requirement is unmet, it builds/admits/deploys NOTHING (the regulated hands-off-deploy block, fail-closed).
- DM14. **ML→class-3-state_sha and UI→perceptual-HITL flow through the composed `verdict_class` policy
  GENERICALLY** (the boundary reads `gate_requirements_for(assigned).grounding`): MLDataPack assigns
  `model_eval`→STATISTICAL (`grounding == ALPHA_STATE_SHA`, `requires_alpha_state_sha`); FrontendPack
  assigns `frontend.perceptual`→PERCEPTUAL (`requires_hitl`, `auto_approvable is False`, and
  `satisfies(PERCEPTUAL, EXECUTION_DETERMINISTIC) is False` — a green machine gate can't clear it).
- DM15. **GOTCHA — the supervisor topology is fixed at BUILD time from the REQUESTED `mode`, but the veto
  degrades the mode at RUNTIME (triage).** Since Autonomous reuses the Gated spine, a regulated-Autonomous
  request already builds the gated boundaries (design/release sign-off), so degrading to Gated is consistent;
  the Autonomous-vs-Gated difference lives entirely in the factory_graph EMIT node, which is where the veto
  bites (no hands-off deploy). A pack that vetoes ONLY `autonomous` (not `assistant`) degrades to `assistant`,
  not `gated` — drive packs that veto BOTH (like RegulatedPack) when a Gated effective mode is wanted.
- DM16. Touched ONLY `foundry/graph/supervisor.py` + `foundry/graph/factory_graph.py` +
  `foundry/governance/tiering.py` (added `classify_tier_at_floor` + `_TIER_STRICTNESS`) +
  `tests/test_supervisor_packs.py` (11 new tests). `ruff check foundry tests` clean; `mypy foundry` clean;
  full suite 1308 passed / 1 skipped.

## Phase-5 EXIT integration test (T-M5-09) — what's genuine E2E vs dev-grade
- DM17. **`tests/test_phase5_exit.py` (12 tests) is the binding Phase-5 acceptance test.** It REUSES the
  `test_supervisor_packs.py` / `test_factory_graph_autonomous.py` harness verbatim (the SOP-keyed
  `MockTransport.scripted` scripts, `_synthetic_resolve`/`_resolve_blob`, `_CLEAN_DIFF`, the REAL
  `TwoZoneGateRunner`+`DevGateWriteVerifier`+`ZoneBSigner`, and the REAL trust root for the autonomous case).
  No new core code — the test OWNS only this file (+ this note).
- DM18. **Criterion→mechanism map (each non-vacuous):** (a) Regulated → `autonomous_pack_reachable(...) is
  False` E2E AND the full autonomous factory graph deploys NOTHING (no recorded human approver ⇒ no release,
  `_deploy_actions(final)==[]`, `phase=="halted"`); the non-vacuous twin records a NAMED HUMAN two-person
  approver-of-record via `bind_approver_of_record` over the EXACT release anchors and shows
  `pack_release_admitted` flips False→True (the ONLY delta is the human record). (b) ML → class-3
  `ALPHA_STATE_SHA` grounding fail-closed over the REAL `GlobalRecords` alpha ledger
  (`assert_class3_binds_ledger` rejects an UNDRAWN look, admits after `record_alpha_look`); a data OR weight
  change RE-KEYS via `ml_state_sha`/`ml_verdict_rebinds` (identical refs = cache hit, non-vacuous); compute
  budget fail-closes via `ComputeBudgetController.charge` → `ComputeOverspendError` (books nothing). (c) UI →
  class-5 PERCEPTUAL `auto_approvable is False` + `satisfies(PERCEPTUAL, EXECUTION_DETERMINISTIC) is False`,
  and an E2E self-judge clean run is BLOCKED at release (`pack_release_admitted ... is False`; non-vacuous:
  `registry=None` admits the SAME state). (d) A brand-new `_CryptoPack` defined INSIDE the test, added via
  `registry.register(...)`, engages E2E through `build_supervisor_graph` (caps Autonomous→Gated, raises tier
  to HIGH, blocks self-judge release) AND composes strictest-wins with the 3 first-party packs — proving
  add-a-pack is a contract registration, NOT a core edit.
- DM19. **Honesty (genuine-E2E vs dev-grade stand-in), recorded in the module docstring:** the regulated
  Autonomous-unreachable + no-hands-off-deploy proof, the human-approver admit, and all four pack
  engagements run through the REAL wired graph + reducers + runner + trust root (GENUINE E2E). The class-3
  `state_sha` re-key and the compute-overspend fail-close are proven at the UNIT level against the REAL core
  machinery the MLDataPack composes (NOT yet a graph node in Phase 5) — a dev-grade stand-in, but over the
  SAME code path the graph boundary reads (`pack_release_admitted`/`autonomous_pack_reachable` read exactly
  these facts), so there is no parallel logic. `ruff check foundry tests` clean; `mypy foundry` clean (145
  files); `pytest -q tests/test_phase5_exit.py` 12 passed; full suite unbroken.

## M5 review fixes (M5-R1-01..03) — Phase-5 hardening
- DM20. **M5-R1-01 (substantive) — the autonomous-emit approver/HITL check is now UNCONDITIONAL.** The
  autonomous emit (`factory_graph.autonomous_emit`) previously halted only on the AND-coupling
  `not autonomous_pack_reachable(...) AND not pack_release_admitted(...)`. That was safe for every CURRENT
  pack (every approver-binding pack also vetoes Autonomous), but a FUTURE pack binding a class-4
  approver-of-record WITHOUT vetoing Autonomous would keep `autonomous_pack_reachable is True` and
  SHORT-CIRCUIT the approver check on the ONLY hands-off (no-human) deploy path. FIX: the guard is now an
  OR — `if not pack_release_admitted(...) or not autonomous_pack_reachable(...): halt`. So the pack release
  requirements (recorded human approver-of-record, perceptual HITL) are enforced UNCONDITIONALLY on the
  autonomous path, EXACTLY like the gated/assistant emit paths already call `pack_release_admitted`
  unconditionally. A regulated/class-4/class-5 request can NEVER hands-off deploy without its human
  requirement met, regardless of whether the pack also vetoed Autonomous. The clean control (no pack) still
  deploys once; the regulated request still halts (no human ⇒ no deploy). New test
  `test_m5r101_approver_binding_pack_halts_autonomous_even_when_reachable` (test_phase5_exit.py) drives a
  stub `_ApproverOnlyPack` (binds the `release` class-4 approver but does NOT veto Autonomous) through the
  REAL autonomous factory graph: gates all PASS, `autonomous_pack_reachable is True`,
  `pack_release_admitted is False` → emit HALTS, `_deploy_actions==[]`, `phase=="halted"` — the OLD
  AND-coupling would have deployed.
- DM21. **M5-R1-02 (MINOR) — strictness-ordering comment.** `contract._VERDICT_CLASS_STRICTNESS` ranks
  PERCEPTUAL (class 5) at 3, BELOW HUMAN_WITNESSED (class 4) at 4, so for SATISFACTION a HUMAN_WITNESSED
  sign-off satisfies a PERCEPTUAL-required key (a recorded human attestation is at least as strong as a
  perceptual HITL; both non-auto-approvable, no integrity loss). Added a load-bearing comment that this is
  INTENTIONAL and must NOT be "fixed" into class-5 > class-4 (that would regress the satisfaction lattice).
  The ranking governs only the satisfaction/tighten JOIN; it does NOT make either class auto-approvable —
  class 4 AND class 5 both remain never-auto-approvable (enforced elsewhere; unchanged).
- DM22. **M5-R1-03 (MINOR) — field rename.** `ComputePolicy.per_fix_compute_cap_microusd` →
  `per_fix_compute_cap_units` (it carries integer compute UNITS metered by `state_sha.ComputeBudget`, NOT
  micro-USD). Updated the field, `tighten`, `relaxes` (contract.py), its sole producer (ml_data.py
  `compute_budget`), and the test references (test_pack_ml_data.py, test_domains_contract.py). Behavior is
  byte-identical; only the name + a clarifying inline comment changed.
- DM23. **Gates:** `ruff check foundry tests` clean; `mypy foundry` clean (145 files); full suite
  **1321 passed, 1 skipped** (incl. test_phase5_exit, test_factory_graph_autonomous, test_supervisor_packs,
  test_pack_ml_data, test_domains_contract). Touched ONLY factory_graph.py, contract.py, packs/ml_data.py,
  their test files (+ the new test in test_phase5_exit.py) + this LEARNINGS note.

## Evaluation — per-feature + A/B-scale cost & wall-clock model (T-M6-01)
- EV44. **`foundry/evaluation/cost_model.py::CostModel` makes the §8 "viability" / "<=5% overhead" claims
  FALSIFIABLE — a concrete accounting model, not an assertion.** API: `RunSignals` (frozen inputs) ->
  `CostModel.feature_cost(run_signals) -> CostBreakdown` (per-feature) and
  `ab_scale_cost(arms: Mapping[Arm, RunSignals], n, k) -> CostBreakdown` (the `Σ_arm per-task-cost x n x k`
  roll-up — the decisive run shown affordable, §8 roll-up). PURE/DETERMINISTIC: same signals -> identical
  frozen `CostBreakdown` (no RNG/IO/`time.time()` — wall-clock is an INPUT signal, never a live read).
- EV45. **THE DOMINANT NON-TOKEN TERMS are the whole point (§8 / COST-F5).** The five §8 terms:
  (1) model tokens — the CHEAP part §8 warns the token-only enumeration rests on; (2) trusted-runner
  COMPUTE for the behavioral-invariance discriminator (mutant + perturbation families, ML eval-set
  re-runs — §8a; CPU/GPU units, scales with the §4 adequacy floor, NOT feature size); (3) WALL-CLOCK
  (§7 lead time, operations §6 queue/replica sizing); (4) HUMAN-REVIEW / HITL time (operations §7,
  a BOUNDED per-tier budget, not an unbounded externality); (5) INFRA (replicas/queue/reserved burst).
  `CostBreakdown.non_token_dominates()` surfaces that an arm CHEAPER ON TOKENS can be MORE EXPENSIVE
  end-to-end — so the §7 scorecard / §9 optimization MUST consume `total_microusd`, never the token line
  alone. The dominant-non-token test proves it both ways (a non-token-heavy feature trips the flag; a
  token-heavy one does not) + an inversion test where the cheaper-tokens arm has the larger total.
- EV46. **INTEGER-MONEY DISCIPLINE (the pinned global constraint — base.py / budget_controller.py).** All
  money is integer micro-USD; compute is integer units, time is integer ms — `CostBreakdown` carries the
  RAW ms/units alongside the priced USD (the §7 scorecard reports wall-clock-per-feature as a TIME budget,
  not only its priced dollar). Pricing is `units x $/unit` / `ms x $/ms`, all integer — no float drift
  (tested with awkward primes `n=137, k=11` reconstructed by exact integer arithmetic to the unit; a float
  model would not survive). `token_fraction()` is a RATE (float OK, 0.0 on empty). HONESTY (load-bearing):
  the FORMULA/accounting is REAL, the input NUMBERS are SYNTHETIC (no funded per-node tokens / $/hr / HITL
  minutes at Phase 1.5) — `provenance` defaults DRY_RUN (+ `dry_run` mirror), part of content identity, so
  a dry-run and a SCORED breakdown of the same shape do NOT coalesce.
- EV47. **Gates:** `ruff check foundry tests` clean; `mypy foundry` clean (150 files); cost-model tests
  **10 passed**. Full suite: my slice is green and broke nothing — 7 PRE-EXISTING failures in
  `test_lifecycle_maintenance.py` (a different parallel M6 task's untracked files; fail identically with
  my work stashed). Touched ONLY `cost_model.py`, `test_eval_cost_model.py` (+ `__init__.py` re-exports +
  this note). HONESTY: synthetic dry-run — asserts NOTHING about real cost.

## Evaluation — per-role AGENT-VALIDITY evals, distinct from outcome (T-M6-03)
- EV48. **`foundry/evaluation/agent_validity.py` measures whether each ROLE agent's CONTRIBUTION is valid
  (evaluation.md §4) — the generalist OUTCOME eval (`harness`/`ab_runner`) only asks "did the pipeline
  resolve?". Contribution validity ⊥ outcome: a pipeline can resolve by luck while an agent is invalid,
  and an agent can be valid on a task the pipeline never resolves.** API: `criteria_critic_validity` /
  `reviewer_critic_validity` / `qa_engineer_validity` each `(*, catches=True) -> RoleValidity`, and
  `agent_validity_report(*, catches=None, provenance=DRY_RUN) -> AgentValidityReport` aggregating the three
  (`validity_score` = fraction valid, `all_valid`, `role_validity(role)`). `ValidityRole` enum names the
  three roles with a directly-checkable planted-defect contract (critic/reviewer/qa; architect/senior are
  measured by the §4 handoff/retrieval evals elsewhere).
- EV49. **PLANTED-DEFECT-CAUGHT is the per-role measure — drive the REAL agent over the doubles (D1/D4, no
  key) against a fixture with a KNOWN omission/defect and read validity off the agent's OWN contribution,
  never a pipeline-resolution bit nor a signed gate verdict (L5).** (1) CRITIC: PRD with a known
  security/abuse omission → valid iff its advisory `GateTriage(name="spec")` is BLOCKING
  (`CriteriaCritique.spec_gate_blocking()`, read off `gate_triage`, NOT a `GateResult`). (2) REVIEWER:
  planted diff defect → valid iff its proxy-stamped `GateResult.status is FAIL` (request-changes) — we
  inspect the verdict the reviewer PRODUCED, not whether `merge_gates` admitted it. (3) QA: a HIGH-RISK
  criterion → valid iff it authors a criterion-traceable NEGATIVE `OracleBinding` for it (FIDELITY, not
  coverage — Q4); wire an in-process `ArtifactStore` as `rt.cas` so the emitted oracle BODIES persist and
  `cas.get_body(handle.uri)` can check `criterion_id`+`oracle_kind` — the handle uri is the body's content
  identity (`cas://sha256/...`), NOT the body's `oracle_uri` field, so you MUST resolve the body, not
  string-match the handle uri.
- EV50. **NON-VACUITY is the load-bearing property: the SAME check scores the catcher valid AND the misser
  invalid (else a green validity score is a tautology).** The catch/miss is scripted via `catches`: critic
  serves omission-vs-clean, reviewer serves request-changes-vs-approve, QA swaps the fixture's HIGH-RISK
  criterion to NORMAL (a correct QA then authors NO negative oracle → invalid). MockTransport gotcha: the
  critic/QA `complete(...,schema=)` ideation calls are NOT the verdict channel (AC3/DT1) — script them on
  the free-text path via `MockTransport.scripted({}, default_text=json)`; the reviewer's `judge()` IS the
  verdict channel — script `verdicts=[verdict_json("request-changes"|"approve", [...])]`.
- EV51. **VALIDITY ⊥ OUTCOME is asserted directly, not by construction.** The orthogonality test pairs a
  hand-built FOUNDRY `ArmResult` whose every `TaskOutcome` RESOLVED (`itt_pass_rate == 1.0`, a green
  outcome eval) with `agent_validity_report(catches={REVIEWER: False})` (`all_valid is False`) — a resolved
  pipeline coexisting with an invalid contribution proves the validity eval is a SEPARATE measurement, not
  a re-derivation of the outcome. The mirror (valid contributions + an all-unresolved arm) covers the other
  diagonal. Determinism: same `catches` map ⇒ identical `content_identity()`; the per-role verdict + the
  report `provenance` are part of identity (EV4), so a flipped role or a SCORED-vs-DRY_RUN twin never
  coalesce. Gates: `ruff check foundry tests` clean (my 2 files; 3 untracked parallel-M6 WIP files —
  `test_eval_integrity.py`, `foundry/lifecycle/*` + their tests — carry PRE-EXISTING ruff/mypy/pytest
  failures, NOT mine); `mypy foundry` clean (my file; the 2 `lifecycle/maintenance.py` mypy errors are that
  untracked WIP); `pytest tests/test_eval_agent_validity.py` **19 passed**; full suite green excluding those
  3 untracked WIP files. Touched ONLY `agent_validity.py` + its test + this note. HONESTY: synthetic
  dry-run — hand-authored planted defects; asserts NOTHING about real agent quality, only that the per-role
  check is real + non-vacuous + outcome-independent.

## Evaluation — outcome SUITES + TRAJECTORY + eval-INTEGRITY (fail-closed) (T-M6-02)
- EV52. **Three layered modules over the oracle harness.** `foundry/evaluation/suites.py`
  (`OutcomeSuite`/`make_suite`, `run_suite`→`SuiteResult`): a §3 outcome suite is a NAMED
  (`SuiteKind`: golden / in_house_novel / swe_bench_style / swe_evo_style) group of oracle-scored
  tasks. `run_suite` REUSES `harness.run_arm` verbatim (suite = `TaskSlice` + §3 semantics via
  `OutcomeSuite.as_slice()`, whose identity mirrors the suite so the arm result binds back), then rolls
  the RETAINED per-(task,repeat) ITT outcomes into `resolution_rate` (over attempts) + per-task
  `TaskResolution` + `task_resolution_rate` (over distinct tasks). No LLM judge — oracle equality only.
- EV53. **ITT denominator is the harness denominator, re-asserted not recomputed.** `SuiteResult.attempted
  == len(suite)*k == len(arm_result.outcomes)`; a `resolution_rate` guard re-asserts denom==retained
  outcomes so a crash/over-budget attempt is RETAINED unresolved and the denom can NEVER silently shrink
  (§2/§6 collider-bias forbidden). `suite_itt_pass_rate` delegates to `harness.itt_pass_rate` (one source
  of truth). Tests inject a crashing/never/always `FoundryRunner` to pin the disposition.
- EV54. **`trajectory.py` scores PROCESS, orthogonal to outcome (§5).** `score_trajectory(TrajectorySignals)
  →ProcessScore` over structured §5 signals (rework_loops, gate_fail_then_fix, excess_steps=steps-ideal,
  policy_violations, converged). Score∈[0,1], HIGHER-IS-BETTER; each axis is a BOUNDED + MONOTONE
  saturating penalty (`weight*min(signal,sat)/sat`, weights sum≤1) so (a) a clean first-pass converged
  path scores exactly 1.0, (b) a strictly-higher-churn run can NEVER out-score an otherwise-equal lower-
  churn run (the property the tests pin), (c) saturating churn stays bounded in [0,1]. Deterministic:
  same signals→identical `score`+content-addressed `score_id` (L7). Policy violations are zero-tolerance
  (saturate at 1). NO LLM judge — §5 reserves judges for "no oracle exists"; a process score is a pure fn.
- EV55. **`integrity.py` FAIL-CLOSES the eval — three §6 mechanisms, ANY violation INVALIDATES.**
  `check_eval_integrity(oracles, surfaces, manifests, arm_results)→IntegrityReport` (`.clean` iff zero
  violations); `require_clean` raises `EvalIntegrityError` at the reporting boundary. (1) `assert_held_out_
  unreachable`: a hidden test (`oracle.hidden_test_id`/`expected_output`) intersecting the agent's recorded
  `AgentRuntimeSurface.reachable_refs` ⇒ REACHABLE_HIDDEN_TEST violation; a MISSING surface fail-closes too
  (unreachability UNPROVEN ≠ proven). (2) `assert_itt_unconditional`: denom==retained-outcomes (else
  MISSING_OUTCOME) AND not conditioned-to-resolved-subset (else CONDITIONED_ITT — collider bias). (3)
  `contamination_probe`: oracle `expected_output` (the ANSWER) or a canary token appearing as a SUBSTRING
  of `TaskInputManifest.input_texts` ⇒ CONTAMINATED_TASK.
- EV56. **DESIGN CALL (resolved): the contamination probe does NOT flag the hidden-test ID.** The synthetic
  frame's `spec` legitimately NAMES the hidden test it's scored by (`task_fixtures.generate_synthetic_frame`
  embeds `oracle.hidden_test_id` in the spec). Leaking a test's NAME ≠ leaking its EXPECTED OUTPUT — only
  the answer token + canaries are contamination signals (probing the id flagged every real fixture, a false
  positive). Runtime REACHABILITY of the test (Mechanism 1, against the sandbox read surface — NOT the spec
  input) is the separate, real isolation check and still keys on `hidden_test_id`.
- EV57. **Gates:** `ruff check`/`mypy foundry` clean for my 3 files; the 2 `lifecycle/maintenance.py` mypy
  errors + `test_lifecycle_*` pytest failures are a CONCURRENT M6 agent's untracked WIP (confirmed by
  stash), NOT mine. `pytest test_eval_suites/trajectory/integrity` **39 passed**; full suite green excluding
  those untracked lifecycle files. Touched ONLY the 3 modules + 3 tests + this note. Forward ref: a future
  `factory_eval` aggregates suite resolution + trajectory process-score + the fail-closed integrity gate.

## Lifecycle — generated-code maintenance + released-artifact provenance (T-M6-09)
- LC1. **`foundry/lifecycle/` (maintenance + provenance) REUSES the trust seams verbatim — it builds NO
  parallel re-verify / chain / admission.** `__init__.py` is import-cheap (a doc + empty `__all__`); the two
  modules carry the logic. The feedback loop (T-M6-10) + the exit test consume these.
- LC2. **MAINTENANCE re-verifies CHANGE-PROPORTIONALLY then re-gates + re-admits — NO bypass (fail-closed).**
  `plan_maintenance(prior_verdicts, changed_scope, *, prior_subject_uri, gates, runner, new_state_sha,
  scanner_config_rev) -> MaintenancePlan` wraps `reverification.reverification_plan` (the EXISTING engine —
  a one-file change re-runs only the touched scope, REUSEs the prior attested verdict for the rest; gov §1f)
  + the maintenance-chain link. `maintain(plan, *, gates, runner, workdir, new_subject_digest, provenance,
  build_verdict, admission, records, verifier, seq, ...) -> MaintenanceOutcome` then (a) RE-RUNs the plan's
  rerun gates through the SAME `TwoZoneGateRunner` (zone-B-attested, keyed to `new_state_sha`) + folds the
  REUSED prior verdicts, (b) requires EVERY required gate present + attested PASS, (c) RE-ADMITs via the
  SAME `AdmissionController`. `released` iff all-PASS AND ADMIT; any non-PASS / DENY ⇒ NOT released (`flags`
  name why; `strict=True` raises `MaintenanceRefused`). A maintained change adding a hardcoded secret →
  Security FAIL → not released AND the runner stable-FAIL-latches the integrated digest so admission DENYs
  too. A forged (unsigned) REUSED prior verdict can't launder a release — admission re-verifies every
  required gate's attestation (`required_gate_signature_invalid`).
- LC3. **A REUSED verdict legitimately keeps its OLD `run_against` — so the re-verified gate-state SPANS TWO
  TREES (gov §1f).** `maintain` therefore DERIVES `expected_gate_trees` per gate from each verdict's own
  `run_against` (re-run gates → `new_state_sha`, reused → their recorded old sub-tree) and passes it to
  `admission.admit` (the AW2 multi-tree path). A caller-supplied mapping (a distinct assurance tree) is
  merged on top. WITHOUT this, admission's whole-tree binding spuriously rejects every reused verdict as
  `required_gate_wrong_tree`. The SOUNDNESS still lives in `reverification_plan` (reuse only when the gate's
  scoped sub-hash is unchanged), NOT in admission's tree pin.
- LC4. **PROVENANCE traces every released artifact + is TAMPER-EVIDENT via the EXISTING evidence-pack
  hash-chain.** `ProvenanceRecord` links subject `Artifact` + `run_id` + `EvidencePack` + attested
  `GateResult[]` + `SignOffRecord[]` + `SLSAProvenance` (+ `prior_uri` = the maintenance chain). `trace(rec,
  *, require_sign_off=True, require_slsa=False) -> ProvenanceChain` re-verifies the pack chain over the
  re-presented members (`evidence_pack.verify_evidence_pack` — a swapped/dropped gate verdict or sign-off
  re-keys a content-addressed ref → chain breaks), checks the subject is IN the pack, every gate is an
  attested PASS keyed to THIS tree, and every sign-off bound the pack AS PRESENTED. `complete` iff full +
  intact; any flag ⇒ `trusted_as_released() is False` (fail-closed; `assert_released` raises
  `IncompleteProvenanceError`).
- LC5. **A sign-off's `presented_evidence_hash` binds the pack BUILT WITHOUT SIGN-OFFS (the evidence shown
  BEFORE signing), not the final pack — else it's circular** (the final pack references the sign-off whose
  hash references the final pack). `trace` rebuilds the pre-sign-off pack deterministically (same
  run_against/seq/scanner_rev/version) and compares its `pack_evidence_hash`. Production already splits
  these anchors: factory_graph's `evidence_hash(subject,sha,gates)` ≠ `pack_evidence_hash` — the sign-off
  and the WORM pack are two distinct anchors. Mirror that in tests: build the no-sign-off pack, hash it,
  stamp the sign-off, then build the final pack with the sign-off folded in.
- LC6. **A pydantic FIELD type must be a RUNTIME import, not TYPE_CHECKING-only** (the model below has
  `gate_results: tuple[GateResult,...]`, `evidence_pack: EvidencePack`, `subject_artifact: Artifact`, etc.).
  A `from __future__ import annotations` + TYPE_CHECKING-only import leaves the forward ref unresolved →
  `PydanticUserError: not fully defined; call model_rebuild()`. Import the schema classes at runtime (they
  are import-cheap); keep only INFRA deps (runner/admission/verifier protocols) under TYPE_CHECKING.
  Gates: `ruff check foundry tests` + `mypy foundry` clean; `pytest test_lifecycle_{maintenance,provenance}`
  = 21 passed (9+12); full suite 1438 passed / 1 skipped.

## Lifecycle — feedback loop + steering + trust modes + licensing (T-M6-10)
- LC7. **The product-feedback loop RE-ENTERS via the maintenance re-gate path — it never bypasses the gates.**
  `feedback_to_backlog(ProductSignal) -> BacklogItem` (pure) links the signal to the affected released
  artifact's provenance (`prior_subject_uri` = the maintenance-chain anchor consumed by
  `maintenance.plan_maintenance`; `provenance_uri` = the `ProvenanceRecord` handle) and routes it to the
  SINGLE `MAINTENANCE_ROUTE` with `re_gate_required=True` ALWAYS — a backlog item cannot opt out of the
  re-gate. A `SignalKind.REGRESSION` additionally sets `is_regression` (the canonical "re-prove it" trigger),
  but the re-gate requirement is universal across every kind. `reenters_via_regate()` is the loop-validity
  self-check (route==maintenance AND re_gate_required). Closed loop: signal → backlog → maintain (re-verify
  change-proportional + re-gate + re-admit) → re-gated release. NO new `BacklogItem` schema existed; I defined
  my own frozen models (ambiguity resolved: `schemas/operations.py` "backlog" refers to the HITL reviewer
  queue, unrelated).
- LC8. **Steering is TIGHTEN-ONLY: human directives become PINNED CONSTRAINTS injected verbatim, never
  relaxing the floor.** `steering_constraints(directives, *, existing=(), seq) -> tuple[PinnedConstraint,...]`
  emits one `PinnedConstraint` per ADMITTED directive (`text` VERBATIM per arch §8; `source=f"human-steering:
  {author}"` so taste authorship is legible — taste is a HUMAN input, never model-delegated). A directive is
  REJECTED (no-op, produces no constraint) when it RELAXES: either an explicit relax shape (`_RELAX_VERBS`:
  "remove constraint"/"unpin"/...) OR it `targets` an id already in `existing` (steering may not edit/remove
  an architecture-pinned constraint — those are superseded by the Architect, not relaxed by steering). The
  return is a NEW add-set layered ON TOP of `existing`; `existing` is never mutated/dropped. Conservative
  default: ADMIT (tighten) — a false-tighten costs one extra constraint, a false-relax would silently lower
  the floor.
- LC9. **Mode graduation is EVIDENCE-GATED + fail-closed; one rung at a time; mirrors the supervisor
  constants.** `modes.py` re-exports `MODE_{ASSISTANT,GATED,AUTONOMOUS}` from `graph.supervisor` (NOT a
  parallel mode vocabulary). `can_graduate(DeploymentTrustMode, GraduationEvidence) -> bool` /
  `graduate(...) -> DeploymentTrustMode` move ONE ladder rung up ONLY when the evidence justifies the NEXT
  posture: GATED needs `gates_green AND human_approval_recorded`; AUTONOMOUS needs those PLUS
  `trust_root_green` (two-zone runner + SLSA-L3 + admission + deployer) AND a named `accountable_party` (the
  liability anchor, §5). Every evidence bit defaults to the WEAKEST value, so an empty `GraduationEvidence`
  justifies NO graduation (no-evidence → no graduation). Can't skip a rung (Assistant+full-evidence → GATED,
  not AUTONOMOUS); top rung can't graduate further. `strict=True` raises `GraduationRefused`; `strict=False`
  is a no-op (stays put) — never an un-earned graduation.
- LC10. **Licensing is PROVENANCE-BACKED + fail-closed: no clean license for un-provable code.**
  `license_for(Artifact, ProvenanceRecord | None, *, license_id=DEFAULT_LICENSE, provenance_uri,
  require_slsa=False, strict=False) -> LicenseRecord`. CLEAN iff: provenance present, its `subject_artifact`
  IS the licensed artifact, AND it traces complete via the EXISTING `provenance.trace` (reuse, no parallel
  check). On success: `license_id` set, `accountable_party` = first approved sign-off (the liability anchor),
  `ai_generated=True` (stated honestly). On ANY failure: `clean=False`, `license_id=""`,
  flags=`no_provenance` / `provenance_subject_mismatch` / `provenance:<underlying-flag>` (broken chain flags
  carried through so remediation is legible). `strict=True` raises `UnlicensableArtifactError`.
- LC11. **Left `lifecycle/__init__.py` UNTOUCHED (empty `__all__`).** T-M6-09 deliberately kept it import-cheap
  with no re-exports (LC1); the four new modules export their own `__all__` and are imported by path. No
  contested edit. Gates: `ruff check foundry tests` + `mypy foundry` clean (my 7 files; the
  `tests/test_eval_scorecard_headline.py` E501s are a CONCURRENT agent's untracked WIP, not mine);
  `pytest test_lifecycle_{feedback_loop,steering,modes}` = 37 passed (10+9+18, licensing folded into modes);
  full suite 1511 passed / 1 skipped.

## Evaluation — factory_eval aggregator (fail-closed unified self-eval) (T-M6-05)
- EV58. **`factory_eval.py` is the single ASSEMBLY layer — it COMPOSES the Wave-A layers verbatim, recomputes
  no statistics.** `run_factory_eval(frame, *, arms=…, suites=…, seed=…, …)→FactoryEvalResult` (frozen)
  runs, per arm (iterated in SORTED-arm order for determinism, not dict order): OUTCOME via `suites.run_suite`
  (→`SuiteResult`, the existing `harness.run_arm` over MockTransport/StubAdapter — no live key), TRAJECTORY
  via `trajectory.score_trajectory` (→`ProcessScore`), COST via `cost_model.CostModel.feature_cost`
  (→`CostBreakdown`), SAFETIES via `safeties.check_arm_safeties` (→`EvalSafetyReport`), folded into a per-arm
  `ArmEval`. Factory-wide: AGENT-VALIDITY via `agent_validity.agent_validity_report` (§4, one report),
  EVAL-INTEGRITY via `integrity.check_eval_integrity` over every arm's headline `ArmResult` (§6), and the §8
  A/B-scale roll-up via `CostModel.ab_scale_cost`.
- EV59. **FAIL-CLOSED is the whole point: `valid = integrity.clean AND every arm's safety.clean`, computed
  from the layer verdicts (`_compute_valid`), NEVER asserted separately.** A reachable hidden test /
  contaminated input (integrity) or a reward-hack / eval-lie / nondeterminism finding (safeties) sets
  `valid is False` — the WHOLE result is INVALIDATED. The layers still produce their values (so the failure
  is auditable) but a non-`valid` result is NOT publishable: `publishable` aliases `valid`, and the scorecard
  / confirmatory A/B consume ONLY a valid result. An EMPTY arm set is NOT vacuously valid (fails closed).
- EV60. **The alpha look-ledger is bound READ-ONLY via a `LedgerView` Protocol that exposes ONLY
  `alpha_looks_spent` (§6.5).** Phase 1.5 is the sole confirmatory arbiter; this Phase-6 aggregator is
  estimation/replication only and re-spends NO alpha — it structurally CANNOT reach `record_alpha_look` /
  `try_first_alpha_look` (not on the Protocol). It records the OBSERVED look-count for audit (`None` when no
  ledger is bound — never a fabricated 0). A spy-ledger test asserts the write methods are never called.
- EV61. **Clean-run defaults: an EMPTY `AgentRuntimeSurface` per task = PROVABLY isolated (passes §6); a
  MISSING surface fails closed (EV55).** So `_isolated_surfaces` records an empty-reachable surface for every
  scored task by default; a test injects a surface intersecting a held-out token to flip integrity. The
  default suite is a SWE-EVO-style suite over `frame.scored_slice` (the §2 A/B arena, disjoint from pilot).
  Honesty: `provenance` defaults DRY_RUN + `honesty_banner` states synthetic-fixtures/mock-model/dev-roots AND
  that this is the FACTORY self-eval, explicitly NOT a delivered product's own shipped eval suite.
- EV62. **`__init__.py` re-export SKIPPED (per the task's "if contested, skip").** `factory_eval` imports
  `agent_validity` at module top → pulls in `foundry.agents`/proxy; re-exporting from the package `__init__`
  would make the import-cheap pure-pydantic `foundry.evaluation` package heavy. The existing `__init__` only
  re-exports cost_model + hypothesis (both pure). Left it untouched; import `factory_eval` directly.
- EV63. **Gates:** `ruff check foundry tests` + `mypy foundry` clean (155 source files); `pytest
  test_eval_factory_eval` = 11 passed (clean→valid; reachable-hidden-test + contamination flip integrity;
  reward-hack + eval-lie + nondeterminism flip safeties; determinism via `content_identity`; non-vacuity;
  read-only ledger; default real per-role validity path); full suite green. Touched ONLY
  `foundry/evaluation/factory_eval.py` + `tests/test_eval_factory_eval.py` + this note.
- EV64. **`foundry/evaluation/replication_ab.py::run_replication_ab(frame, prereg, phase15_decision, *,
  seed=0, ledger=None, estimation_core=None) -> ReplicationResult` (frozen) — the Phase-6 confirmatory A/B is
  ESTIMATION/REPLICATION ONLY (§6.5).** Phase 1.5 is the SOLE arbiter that SPENT alpha; Phase 6 re-runs the
  3-arm A/B on the scored slice to report an effect estimate + CI + whether it REPRODUCES the Phase-1.5
  primary direction/decision — it is NOT a second gate and draws NO new alpha look. The cite-frozen-prereg
  gate is FIRST (`assert_unmodified`, EV14), then the estimation core (defaults to `ab_runner.run_ab`,
  reusing the paired-cluster-bootstrap machinery; `ab_runner.py` UNTOUCHED) produces the primary read.
- EV65. **THE ALPHA-NOT-RE-SPENT INVARIANT (load-bearing) is enforced by a READ-ONLY `LedgerView` Protocol +
  a before==after look-count assert.** Same pattern as EV60: `LedgerView` exposes ONLY `alpha_looks_spent`,
  so `record_alpha_look`/`try_first_alpha_look` are STRUCTURALLY unreachable (not on the Protocol).
  `run_replication_ab` reads `alpha_looks_spent((scored_eval_set_identity, decision_id))` (the SAME
  Phase-1.5 ledger key, from the cited prereg) BEFORE and AFTER, and ASSERTS unchanged
  (`AlphaReSpentError` fail-closed). `ReplicationResult` carries `is_estimation_only=True` +
  `alpha_spent_delta=0` (both re-asserted in `model_post_init`); look counts are `None` without a ledger
  (never a fabricated 0). Test: a spy ledger proves the write methods are never called; a ledger with the
  Phase-1.5 look ALREADY drawn (freeze recorded it) stays at 1 across the replication.
- EV66. **`reproduces_phase15(phase15_decision: DecisionRecord) -> bool` is a SELF-CONSISTENCY check vs the
  Phase-1.5 `DecisionRecord`, not a fresh verdict (no alpha spent).** A PROCEED is reproduced iff the
  replication is significant at the committed alpha in the SAME positive direction AND meets the committed
  MDE; a non-PROCEED (HARD_KILL / a primary null) is reproduced iff the replication is likewise a null. A
  Phase-6 DISCONFIRMATION (`reproduces=False`) is RECORDED but the Phase-1.5 decision object + ledger are
  UNTOUCHED — it cannot un-spend the sunk cost of the machinery Phase 1.5 licensed (§6.5); a materially-
  inconsistent read is an investigation trigger, not a back-door second gate. Exposed both as a method and a
  module-level alias `reproduces_phase15(result, decision)`.
- EV67. **Gates:** `ruff check foundry tests` + `mypy foundry` clean (156 source files); `pytest
  test_eval_replication_ab` = 11 passed (winning reproduces PROCEED; null disconfirms PROCEED w/ Phase-1.5
  unchanged; null reproduces KILL; winning disconfirms KILL; before==after look-count; spy-ledger no-write;
  None look-counts; re-spend fails closed; tamper gate; determinism; default real `run_ab` path drives a
  null→KILL reproduction with no alpha drawn); full suite 1460 passed/1 skipped. Touched ONLY
  `foundry/evaluation/replication_ab.py` + `tests/test_eval_replication_ab.py` + this note.
- EV68. **`foundry/evaluation/scorecard.py::build_headline_scorecard(factory_eval, *, replication=None,
  decision=None, secondaries=()) -> HeadlineScorecard` (frozen) is the FULL published headline (§7), built
  IN-PLACE alongside the M1.5 `build_scorecard` slice (backward-compatible — the M1.5 API/tests are
  untouched).** It assembles: the ONE confirmatory PRIMARY (`HeadlinePrimary`) — the Phase-1.5 arbiter read
  [SPENT alpha] (from the `DecisionRecord`, or the replication's `phase15_*` read as fallback) PLUS the
  Phase-6 `ReplicationResult`'s reproduction status [ESTIMATION only, NO alpha — §6.5], each clearly LABELED
  (`alpha_source="phase_1.5_arbiter"` vs `estimation_source="phase_6_replication"`); the SECONDARY family
  under Holm–Bonferroni; the per-feature wall-clock + cost line (`CostLine` off the §8 `CostBreakdown` —
  full breakdown + raw wall-clock ms + `non_token_dominates`, NOT the token line alone); the
  agent-validity/trajectory/integrity/safeties summary; and the UNCONDITIONAL per-arm ITT (`ArmScore`,
  denominator = `outcome.attempted`, never shrunk — the same §2/§6/§7 rule). It PRESENTS — recomputes no
  statistic EXCEPT the Holm adjustment (the one derivation).
- EV69. **Holm keeps secondaries from becoming primaries two ways.** (1) It REUSES the ab_runner Holm helper
  (`from foundry.evaluation.ab_runner import _holm_bonferroni`) over the family of supplied `SecondaryReading`
  raw-p's to derive each `HeadlineSecondary.significant` (FWER-controlled) — the `raw_significant`
  (`raw_p < alpha`) bit is kept DESCRIPTIVE only. A raw-significant-but-Holm-NON-significant endpoint is
  reported NOT significant: a 3-endpoint family `[0.001, 0.04, 0.04]` at alpha=0.05 step-downs to reject only
  rank0 (thr alpha/3) and STOPS at rank1 (0.04 > alpha/2=0.025), so both 0.04 endpoints are raw-sig but
  Holm-killed. NOTE the corner case: with m<=2 the largest endpoint's Holm threshold is exactly alpha, so a
  raw-sig endpoint can NEVER be Holm-killed — need m>=3 to witness it. (2) `HeadlineSecondary.is_primary` is
  STRUCTURALLY False — `model_post_init` raises if anyone tries to construct a secondary as a primary claim.
- EV70. **PUBLISHED + STABLE = a deterministic `stable_digest()` (canonical `content_digest` over the full
  record — L7, never `hash()`) that re-derives IDENTICALLY across two builds from the same inputs (byte-
  identical scorecard).** The digest is surfaced INSIDE `render()` (the published stable bit). Determinism is
  free because every input layer (factory_eval / replication / decision) is itself deterministic and the only
  derivation (Holm) is a pure function of the supplied p-values. FAIL-CLOSED-ON-INVALID: a non-valid
  `FactoryEvalResult` (`valid is False` — reachable hidden test/contamination §6, or reward-hack/eval-lie/
  nondeterminism §10) raises `NotPublishableError` as the FIRST thing `build_headline_scorecard` does — an
  invalidated eval can never reach the headline as evidence. `HeadlineScorecard.model_post_init` re-asserts
  `publishable` (a validated `publishable=False` fails closed), though note pydantic `model_copy(update=)`
  BYPASSES validation, so the structural guarantee lives at construction/`model_validate`, not `model_copy`.
- EV71. **The honesty banner is FIRST + mandatory (non-empty for both provenances).** It states (§0/§4) the
  ONE primary is oracle-scored so a self-judge seam is NEVER reported as independent validation, AND (§6.5)
  the Phase-6 replication is ESTIMATION only — a disconfirmation is an investigation trigger, NOT a second
  gate. Provenance/dry_run flow from the `factory_eval`.
- EV72. **Gates:** `ruff check foundry tests` + `mypy foundry` clean (161 source files incl. tests target);
  `pytest test_eval_scorecard_headline test_eval_scorecard` = 27 passed (14 headline + 13 M1.5 slice: one
  primary labeled 1.5+6; Holm-killed raw-sig secondary; secondary-can't-be-primary; cost/wall-clock line;
  unconditional ITT; validity/trajectory/integrity/safeties summary; stable_digest identical across builds +
  changes-with-inputs; fail-closed on non-valid eval; banner-first; replication-only fallback primary;
  needs-a-1.5-read; provenance carried). Full suite 1511 passed/1 skipped. Touched ONLY
  `foundry/evaluation/scorecard.py` + `tests/test_eval_scorecard_headline.py` + this note.
- EV73. **T-M6-08 — the §9 constrained workflow optimization is PARETO-IMPROVING ONLY (`foundry/evaluation/`
  `optimization.py`).** `MetricVector` bundles the headline metrics with an EXPLICIT per-metric direction (the
  SINGLE `MetricVector._directions()` table — never a blanket "bigger is better"): resolution-rate↑ (the
  objective) + policy-compliance↑ are higher-is-better; escaped-defects↓ + cost↓ + wall-clock↓ are the
  lower-is-better CONSTRAINTS. Money is integer micro-USD + time integer ms (no float drift on the
  constrained metrics — the pinned global rule); rates are floats in [0,1], defects a non-neg int.
  `pareto_improves(baseline, candidate)` = NO regression on ANY metric (each judged in its OWN direction via
  the one table) AND >= 1 STRICT win. `evaluate_optimization(...) -> OptimizationDecision` (frozen) ADOPTS iff
  `pareto_improves`, else REJECTS — naming `regressed_metrics` (the constrained metric(s) that regressed) and
  recording `improved_metrics` (so the audit shows a resolution win was correctly NOT bought with a regression).
- EV74. **Fail-closed: a win is NEVER traded for a constrained-metric regression, and a TIE is NOT a Pareto
  improvement.** The §9 constraint is HARD, not a trade-off: a candidate that improves resolution but regresses
  cost / wall-clock / escaped-defects / policy-compliance is REJECTED with that metric named (`_regressions`
  short-circuits adoption even when `_improvements` is non-empty). A TIE (no strict improvement, no regression)
  is REJECTED too — `regressed_metrics == ()` distinguishes a tie-reject from a regression-reject (a tie makes
  no progress on the objective; only a strict Pareto win adopts). NON-VACUITY proven: the adopt↔reject verdict
  flips on ONE metric (resolution 0.60 ADOPT vs 0.40 REJECT; the SAME win flips to reject the instant
  cost regresses by 1 micro-USD).
- EV75. **Tiering is a VALIDATED OUTPUT of optimization, not an intuition (§9).** `evaluate_tiering_change(...)`
  feeds a proposed change-risk tiering through the SAME fail-closed Pareto gate (reuses `evaluate_optimization`
  with `kind="tiering"` — no parallel laxer path): a retiering is adopted ONLY if its measured headline metrics
  Pareto-improve, and rejected (metric named) if it regresses ANY constrained metric — a retiering can't be
  adopted on "the diff is small" grounds if it costs a headline metric. Defense-in-depth: a `proposed_tiers`
  entry whose `relaxes_integrity()` is True raises BEFORE the Pareto gate (governance.md §1e: tiering scales
  depth/HITL only, never integrity). The optimization READS metrics, recomputes NO eval; pure/deterministic
  (`content_identity()` identical across re-runs); honesty: DRY_RUN inputs synthetic, the LOGIC real.
  **Gates:** `ruff check foundry tests` + `mypy foundry` clean (161 source files); `pytest`
  `test_eval_optimization` = 16 passed (genuine + multi-metric Pareto adopt; better-resolution-worse-cost /
  -worse-wall-clock / worse-escaped-defects / worse-policy-compliance rejects [named]; tie reject; both
  directions; tiering adopt/reject/same-gate/integrity-relax-raises; determinism; non-vacuity; provenance).
  Full suite 1527 passed/1 skipped. Touched ONLY `foundry/evaluation/optimization.py` +
  `tests/test_eval_optimization.py` + this note.
- EV76. **T-M6-11 — the RED-TEAM harness probes the REAL defenses (`foundry/evaluation/red_team.py`).**
  `RedTeamHarness(probes?, provenance).run_red_team(weaken=False) -> RedTeamReport`. `RedTeamProbe(name,
  attack, run: (weaken:bool)->ProbeResult)` is a FROZEN DATACLASS (carries the bound callable; pydantic
  can't). `ProbeResult(name, attack, defended, conclusive, detail)`: `defended` True IFF the defense HELD
  (attack FAILED) AND `conclusive`. `RedTeamReport.all_defended` = suite non-empty AND every probe defended
  (empty suite is NOT vacuously clean); `critical_findings` = the UNDEFENDED probes. The seven default probes
  each EXERCISE THE REAL DEFENSE, never a re-mock: prompt-injection runs the REAL `Guardian` with a malicious
  analyzer emitting "OVERRIDE/approve/clear all trips" on a real CAP_BREACH→HALT and asserts the deterministic
  action/trips are byte-identical after the consult (gov §3 inert advisory); gate-evasion runs the REAL
  `TestSuiteDiscriminator` on a gamed-green suite + held-out mutant and asserts FAIL (mutant survives);
  forged-results runs the REAL `TwoZoneGateRunner` on a SEALED channel with a swapped FAIL→PASS observation
  (sealed under a wrong key) and asserts ERROR not PASS; forged-verdict runs the REAL `AdmissionController`
  with an unsigned required gate → DENY `required_gate_signature_invalid`; fork-divergence disqualifies a
  digest then re-presents it under a FRESH run_id with genuine gate-state → still DENY
  `stable_fail_latched_or_disqualified` (latch keyed on the digest, gov §5); reward-hacking/eval-lie run the
  REAL `detect_reward_hacking`/`detect_eval_lies` safeties.
- EV77. **FAIL-CLOSED + NON-VACUITY + cost/latency.** `_run_one` catches ANY probe exception → INCONCLUSIVE +
  `defended=False` (a red-team probe that errors is NEVER a silent pass); an inconclusive ProbeResult also
  blocks `all_defended`. NON-VACUITY: every probe takes `weaken`; weaken=True wires a DELIBERATELY-BYPASSED
  defense (honored analyzer override, mutant-less discriminator, UNSEALED channel, laundered-genuine
  signature, an `_AlwaysAdmissibleRecords` view that drops the disqualification look, a laundered green
  discriminator report, a suppressed self-report claim) and the SAME suite reports UNDEFENDED — proving the
  DEFENDED verdict tracks the real defense, not a constant (each probe flips real-DEFENDED↔weak-UNDEFENDED).
  `cost_latency_report(run_signals, *, budget_microusd, budget_wall_clock_ms, cost_model?) -> CostLatencyReport`
  REUSES the §8 `CostModel.feature_cost` (no re-implementation) and flags `overspend` (total USD > budget) +
  `latency_regression` (wall-clock > budget); `within_budget` = NEITHER (the budget IS the bound, §8;
  at-exact-bound is within). Integer micro-USD/ms throughout; deterministic (no RNG/clock — wall-clock is an
  input signal). `_AlwaysAdmissibleRecords` explicitly implements `GlobalRecordsView` (delegates every read
  but `admission_look`) so mypy sees the Protocol — a `__getattr__` shim does NOT satisfy a Protocol.
  **Gates:** `ruff check foundry tests` + `mypy foundry` clean (162 source files); `pytest test_eval_red_team`
  = 22 passed; full suite 1549 passed/1 skipped. Touched ONLY `foundry/evaluation/red_team.py` +
  `tests/test_eval_red_team.py` + this note.
