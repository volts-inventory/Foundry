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
- D6. (R1-01/R1-03) On the constrained verdict channel
  (`output_config.format.type == "json_schema"`) the verdict queue/`default_verdict` take
  PRIORITY over keyed scripts, so `judge()` can never crash in `json.loads` on free-text that
  matched a script; scripts still win on every other channel. Verdict-queue literal-vs-JSON
  detection is whitespace-tolerant (`json.loads` probe, not `startswith("{")`).
- D2. Scripting API (priority order): `MockTransport(responses=[ResponseScript(text, match)])`
  first-match-wins; `MockTransport.scripted({needle: text})` keys by substring of system SOP/messages;
  `verdicts=[...]` / `completions=[...]` are FIFO queues for the constrained-json-schema channel vs
  free-text channel; `default_verdict=`/`default_text=` are the per-channel fallbacks. Verdict items
  may be `"approve"`/`"request-changes"` or pre-serialized JSON; use `verdict_json(status, findings)`.
  Constrained verdict channel is auto-detected via `output_config.format.type == "json_schema"`.
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

## Orchestration / tooling
- O1. mypy gate is `mypy foundry` (config in pyproject; `ignore_missing_imports=true`).
  Don't add `# type: ignore` unless needed — `warn_unused_ignores` flags dead ignores.
- O2. `make_union_by_key` is a generic *function*; mypy rejects `f[T](...)` type-application on
  functions. Type the key callable via a named function with annotated params, not a lambda.
- O3. pytest collects production classes named `Test_*` only (pyproject) — domain types like
  `TestPlan`/`TestReport` are NOT test cases. Name real test classes `Test_*` or use functions.
- O4. No CI of any kind (no `.github/workflows`). All verification is local via the gate chain.
