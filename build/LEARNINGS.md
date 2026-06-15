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
