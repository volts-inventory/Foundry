# ARCHITECTURE (orchestration layer) — stack, bars, contracts

Authoritative architecture: `docs/architecture.md` + `docs/implementation-plan.md`
(repo layout §1, build order §8). This file pins the QUALITY BARS and the stable
interface contracts parallel agents build against. Respect the stack already implied
and implemented — do not re-open it.

## Pinned stack (implementation-plan §0.1) — already in use
- Python 3.12 (`from __future__ import annotations`), type hints everywhere.
- LangGraph `StateGraph` for orchestration; durable checkpointer.
- Anthropic single lineage (Opus/Sonnet/Haiku tiers); pluggable worker substrate
  (default `claude -p` adapter, contract is provider-agnostic).
- pydantic v2 for every agent I/O artifact (validated, versioned, frozen).
- Durable state: SQLite checkpointer in dev (plan names Postgres for prod; dev-grade
  per §0.3). Content-addressed artifact handles; bodies never enter state.
- Tests: pytest + hypothesis property tests (load-bearing).

## Directory structure (target — implementation-plan §1)
Implemented: `state/ schemas/ durability/ proxy/ execution/ gates/ monitors/
governance/ context/ graph/(skeleton only) canonical.py`.
Remaining to build: `agents/ graph/subgraphs+supervisor+factory_graph trust_root/
domains/ evaluation/ operations/ lifecycle/ policy/ observability/ cli.py`.

## Build order (topological — implementation-plan §8)
1–8 DONE (state, schemas, durability, proxy, execution, gates, governance+monitors,
context). Remaining: 9 agents → 10 graph subgraphs+supervisor+factory_graph →
11 trust_root → 12 domains → 13 evaluation/operations/lifecycle/policy/observability →
cli.py (ties it together at Phase 1).

## QUALITY BARS (enforced via the gates below)
- **Format/lint:** `uv run ruff check foundry tests` — zero findings (config in pyproject).
- **Type-check:** `uv run mypy foundry` — zero errors. (Production code strictly typed;
  test modules are exempt from the strict bar — pydantic/hypothesis dynamic usage.)
- **Build/import:** `uv run python -c "import foundry"` and per-package import smoke.
- **Tests + coverage:** `uv run pytest` green; **line+branch coverage ≥ 80%** overall
  (`--cov=foundry --cov-fail-under=80` at milestone gates; changed-scope per wave). Current
  baseline 82%. New code SHOULD land at ≥ 85% local coverage.
- **Mutation (milestone gate, scoped to changed code):** `mutmut` — **mutation score ≥ 70%**
  on changed modules; survivors killed or justified in REVIEW.md.
- **Security scan:** dependency/static security review per wave (Security Critic + any
  available scanner). No known-vulnerable patterns in shipped code.
- **Determinism:** every model-touching node records model-id + served-model + seed/effort.

## GATE ORDER (cheap → expensive, short-circuit on first failure)
ruff → mypy → import-smoke → pytest(changed-scope; FULL at milestone) + coverage →
regression → (milestone) mutation → (milestone) runtime smoke → model critics.

## INTERFACE CONTRACTS (stable seams parallel agents build against)
- **State channels + reducers** (`foundry/state/`): `FactoryState` TypedDict; channels
  `request route phase budget artifacts pinned_constraints gates gate_triage tasks actions`.
  Agents WRITE artifacts/gate_triage/tasks/actions; never write attested `gates` directly.
- **Artifact schemas** (`foundry/schemas/`): every agent I/O is a frozen pydantic model in the
  matching schema module (product/architecture/implementation/quality/security/operations/
  gates/governance). New agent outputs extend the existing schema modules.
- **Worker substrate** (`foundry/execution/adapter_contract.py`): In = task packet + pinned
  constraints (verbatim) + retrieved spans + scoped worktree + tools + budget. Out = diff +
  test sources + `GateTriage` (advisory) + proxy-observed lineage. Adapter self-report ignored.
- **Model egress** (`foundry/proxy/`): ALL model calls go through the proxy (single egress,
  lineage observation, judgment-binding stamp, self-judge provenance). Agents never call the
  Anthropic SDK directly.
- **Gates** (`foundry/gates/`): agents emit `GateTriage` (advisory); verdicts (`GateResult`)
  are computed + signed by the two-zone runner (zone A exec / zone B sign). `_merge_gates`
  admits a deterministic-gate write ONLY if runner-attested; a judgment verdict only if the
  binding validates, nonce unconsumed, no negative latch.
- **Graph assembly** (`foundry/graph/`): subgraphs per layer → `supervisor.py` phase routing →
  `factory_graph.build_factory_graph() -> Compiled[FactoryState]`. Nodes are pure-ish:
  read state, call proxy/substrate, return channel writes.

## CROSS-CUTTING COLLISION HAZARDS (serialize edits; never parallel_safe)
- `pyproject.toml` (deps/config) — single owner per wave.
- `foundry/schemas/*` shared modules — append-only additions; serialize edits to a given module.
- `foundry/graph/factory_graph.py` + `supervisor.py` (node/edge registry) — single owner.
- `foundry/state/factory_state.py` (channel registry) — single owner; append-only channels.
- `build/*` state files — orchestrator-owned.
