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
