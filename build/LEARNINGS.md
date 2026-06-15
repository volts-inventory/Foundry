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
- GW1. **`materialize_workdir(state, dest, *, resolve) -> tree_sha` is the boundary's trust-seam
  contract.** It builds the integrated tree the deterministic gates run against, ENTIRELY from the
  content-addressed handles on `state["artifacts"]` via the SAME `ArtifactResolver` seam
  senior_engineer/qa use (NOT a parallel one): writes `criteria.json` from the `acceptance_criterion`
  handles in the EXACT shape SpecGate parses — `{id, text, oracle, security_abuse}` (NB:
  `AcceptanceCriterion.oracle_hint` maps to the `oracle` key SpecGate reads), ordered by `id` so the
  file (and thus the digest) is byte-stable; and materializes code+test sources from the
  `feature_diff` handles. The diff BODY contract: `resolve(feature_diff_handle)` -> the typed
  `FeatureDiff`, then `resolve(handle.model_copy(update={"uri": diff.diff_uri}))` -> a
  `Mapping[str,str]` of relative-POSIX-path -> file content. Diffs applied in `Seq` order
  (later/retry wins, L4). FAIL-CLOSED: a handle present but unresolvable to its shape raises
  (`MaterializationError`/`FeatureDiffBodyError`) — never run the gates against a partial tree (that
  risks a vacuous PASS, gov §1b). Returns `tree_sha(dest, prefer_git=False)` (dest is a fresh tree,
  not a git worktree) as the integrated `run_against`. The `ArtifactResolver` return is `Any` (it
  yields both FrozenModel bodies AND raw path->content dicts), mirroring senior_engineer — NOT the
  quality layer's `-> FrozenModel` narrowing.
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
