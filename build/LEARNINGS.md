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
