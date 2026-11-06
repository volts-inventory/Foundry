"""Shared per-task build helpers for the implementation layer (agents.md Layer 3).

The ticket scheduler (``tickets.py``) and the build/test :func:`make_fixer_node`
run the swappable coding substrate (``rt.adapter``) inside a scoped
:class:`~foundry.execution.sandbox.Sandbox` and emit a content-addressed
:class:`~foundry.schemas.implementation.FeatureDiff` through the helpers here.

What these helpers are careful about (LEARNINGS L4/L5/L7, arch §2/§2b):

* **Pinned constraints VERBATIM + FIRST.** The architecture-pinned constraint
  texts are pulled from the live (non-superseded) view and injected into the
  :class:`TaskPacket` byte-for-byte, ahead of the task body.
* **Content-addressed keys (L7).** The diff/idempotency key is derived with
  ``content_digest``, never builtin ``hash()``, so the SAME inputs produce the
  SAME key across a kill-then-resume in a fresh process.
* **Lineage is proxy-observed, never self-reported (arch §2b).** The adapter's
  ``adapter_self_reported_model`` is IGNORED for lineage.
* **Agents never mint verdicts (L5).** The adapter returns advisory
  ``GateTriage`` only; it is forwarded to ``gate_triage`` and NEVER written as a
  ``GateResult`` to ``gates``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry.agents._common import journal_entry
from foundry.agents._runtime import AgentRuntime, NodeFn
from foundry.canonical import content_digest
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.sandbox import Sandbox
from foundry.schemas.architecture import PinnedConstraint
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.implementation import FeatureDiff, Task, TaskStatus
from foundry.state.reducers import DETERMINISTIC_GATES, live_constraints

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from langchain_core.runnables import RunnableConfig

    from foundry.graph.gate_wiring import BlobResolver, _DiffOps
    from foundry.state.factory_state import FactoryState

    # Reconstitutes a typed body from its content-addressed handle (the same INFRA
    # seam the gates take; CAS in prod, an in-memory map in tests).
    ArtifactResolver = Callable[[Artifact], Any]

__all__ = [
    "idempotency_key",
    "make_fixer_node",
    "pinned_texts",
    "ready_tasks",
]

NODE_ID = "junior_engineer"
FIXER_NODE_ID = "fixer"

_FIXER_PERSONA = (
    "You are fixing an integrated build that failed verification. The worktree holds "
    "the whole project as built so far. Make the smallest changes that make the failing "
    "gates below pass; do not rewrite working code or add features."
)
_FIXER_RULES = (
    "The failures below are the tail of each gate's output: re-run the failing "
    "command yourself (see `verify`) to read the whole output before you edit, unless "
    "the summary says it cannot run here.",
    "After each fix re-run the commands in `verify` (install only once its package "
    "manifest exists; then build and typecheck; then the tests) and keep going until "
    "they all pass or you run out of turns. If one cannot run here (missing tool, no "
    "network), say so in a TRIAGE line.",
    "Acceptance scenarios live only in the gate-owned features/<ticket>.feature files: "
    "never write, copy or regenerate a .feature file (any you write is discarded). A "
    "scenario that \"was not executed\" has no step module bound to that exact file: "
    "point the step module's binding at it (pytest-bdd: scenarios(\"<path relative to "
    "the module>\")) and implement its steps. Fix one feature file group at a time.",
)

# Claude Code needs several turns to read, write and verify; 8 left contract stubs empty.
_DEFAULT_TURN_BUDGET = 25
# A local model spends ~10 of a fix pass's turns re-reading files before its first edit.
_LOCAL_FIX_TURN_BUDGET = 40
# The files a failure names are inlined in the fix packet, so they cost no read turns.
_INLINE_FILES = 3
_INLINE_CHARS = 8_000
_DEFAULT_COST_BUDGET_MICROUSD = 1_000_000  # integer micro-USD, never floats


def pinned_texts(constraints: Sequence[PinnedConstraint]) -> tuple[str, ...]:
    """The constraint TEXTS to inject, in a deterministic order.

    Reads the live (non-superseded) view (``live_constraints``) so a retired
    constraint is not injected, then orders by ``(id)`` for a stable, fold-order-
    independent sequence — the packet must be byte-identical across a resume so
    its idempotency key is stable (L7)."""
    live = live_constraints(list(constraints))
    return tuple(c.text for c in sorted(live, key=lambda c: c.id))


def idempotency_key(task: Task, constraints: Sequence[PinnedConstraint], base_tree_sha: str) -> str:
    """Process-stable idempotency key for this task's diff (LEARNINGS L7).

    Derived with ``content_digest`` over the task identity + the verbatim
    pinned-constraint texts + the base tree — NEVER builtin ``hash()`` (seed-
    randomized per process; would defeat outbox dedup on resume). A re-run with
    identical inputs re-derives the identical key, so the keyed-union channel
    coalesces the supersession instead of duplicating it."""
    return content_digest(
        {
            "task_id": task.id,
            "branch": task.branch,
            "pinned_constraints": list(pinned_texts(constraints)),
            "base_tree_sha": base_tree_sha,
        }
    )


def ready_tasks(state: FactoryState) -> tuple[Task, ...]:
    """The Task(s) on ``state['tasks']`` (AR1), typed and ordered by ``Seq``.

    The ``tasks`` channel carries plain ``dict``s (keyed by id, ``union_by_id``);
    reconstitute the typed :class:`Task` from each and order by ``Seq`` so the
    order is deterministic and recoverable from the records alone."""
    raw = state.get("tasks") or ()
    fields = set(Task.model_fields)
    out: list[Task] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # A done-marker carries extra keys (diff_uri / base_tree_sha) the frozen
        # Task model forbids; keep only the Task fields to reconstitute it.
        out.append(Task.model_validate({k: v for k, v in entry.items() if k in fields}))
    return tuple(sorted(out, key=lambda t: t.seq))


def _prior_failures(state: FactoryState) -> tuple[str, ...]:
    """Findings from the latest pass's FAILED/ERRORed gates — fed back to the
    engineer (via the TaskPacket) so a fix attempt targets the SPECIFIC failure
    instead of re-rolling blind. Empty on the first pass (no gates yet).

    The ``gates`` channel keeps one record per (gate, tree), so superseded trees'
    failures stay in state. Only the latest verdict per gate counts, and a
    deterministic gate counts only if it ran against the newest tree: a gate skipped
    on this pass (tests after a build failure) must not resurface last pass's FAIL."""
    out: list[str] = []
    for g in _failed_gates(state):
        out.append(f"[{g.name} gate FAILED]")
        out.extend(g.findings)
    return tuple(out)


def _failed_gates(state: FactoryState) -> list[GateResult]:
    """The latest pass's FAILED/ERRORed gates with findings, oldest first (see
    :func:`_prior_failures`)."""
    gates = list(state.get("gates") or ())
    latest: dict[str, GateResult] = {}
    for g in gates:
        if g.name not in latest or g.seq > latest[g.name].seq:
            latest[g.name] = g
    tree_gates = [g for g in gates if g.name in DETERMINISTIC_GATES]
    current_tree = max(tree_gates, key=lambda g: g.seq).run_against if tree_gates else None
    return [
        g for g in sorted(latest.values(), key=lambda g: g.seq)
        if not (g.name in DETERMINISTIC_GATES and g.run_against != current_tree)
        and g.status in (GateStatus.FAIL, GateStatus.ERROR) and g.findings
    ]


def _fix_summary(
    state: FactoryState,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    *,
    attempt: int,
    tree: Mapping[str, str | None] | None = None,
) -> tuple[str, ...]:
    """The head of a fixer pass's input: the failing gates and the tests/scenarios
    they name, the files to edit first (with their content from ``tree``, the
    integrated tree), then what the previous pass of this fix loop touched and
    reported.
    Fresh attempts from a summary of earlier rollouts beat one ever-growing repair
    context (arXiv 2602.18998, 2604.16529). Read off state; no model call."""
    from foundry.graph.gate_wiring import (
        FEATURE_DIFF_KIND,
        MaterializationError,
        _diff_ops,
        _of_kind,
        _resolve_typed,
        failing_ids,
        pip_manifest_failure,
    )

    failed = _failed_gates(state)
    ids = [i for g in failed for i in failing_ids(g.findings)]
    named = _named_files(failed, tree)
    out = [
        "[fix summary]",
        "failed gates: " + (", ".join(g.name for g in failed) or "none recorded"),
        "failing tests/scenarios: " + (", ".join(ids) or "none named; see the gate output"),
    ]
    if named:
        out.append("Edit these files first: " + ", ".join(named)
                   + " (the failure names them)")
    if pip_manifest_failure(failed):
        out.append("pip could not install a dependency the manifest declares. The install "
                   "cannot be re-run in this sandbox (no network), so do not try: edit the "
                   "manifest line the gate output names (a package that does not exist on "
                   "PyPI, e.g. a standard-library module, or a version that was never "
                   "released) and remove or correct it.")
    # attempt counts the failing checks of this fix loop: from 2 on, the newest fix
    # diff is this loop's previous pass, not an earlier wave's.
    fixes = [a for a in _of_kind(state, FEATURE_DIFF_KIND)
             if a.task_id and a.task_id.startswith("fix-")]
    if attempt >= 2 and fixes:
        prev = max(fixes, key=lambda a: a.seq)
        try:
            ops = _diff_ops(_resolve_typed(prev, resolve, FeatureDiff, FEATURE_DIFF_KIND),
                            resolve_blob)
            files = touched_files(ops)
            touched: set[str] | None = {*ops.writes, *ops.deletes, *ops.patches}
        except MaterializationError:
            files, touched = "an unreadable diff", None
        out.append(f"previous fix pass ({prev.task_id}) touched {files} and left the "
                   "gates above failing: try a different approach")
        if named and touched is not None and not touched & set(named):
            out.append(f"previous fix pass never edited {', '.join(named)}, the file(s) the "
                       "failure names: edit them before anything else")
        out.extend(f"previous fix pass reported: {t.explanation[:300]}"
                   for t in sorted(state.get("gate_triage") or (), key=lambda t: t.seq)
                   if t.run_against == f"task/{prev.task_id}")
    out.extend(_inline(named, tree))
    out.append("gate output (tail):")
    return tuple(out)


def _named_files(
    failed: Sequence[GateResult], tree: Mapping[str, str | None] | None
) -> tuple[str, ...]:
    """The files the failure names, kept to those in ``tree`` when one is given
    (both pip manifests are candidates; a project usually has one)."""
    from foundry.graph.gate_wiring import failure_files

    files = failure_files(failed)
    if tree is None:
        return files
    return tuple(f for f in files if tree.get(f) is not None)


def _inline(named: Sequence[str], tree: Mapping[str, str | None] | None) -> list[str]:
    """The current content of the first few named files, within a size cap; a file
    that does not fit is left for the agent to read."""
    out: list[str] = []
    room = _INLINE_CHARS
    for rel in named[:_INLINE_FILES]:
        content = (tree or {}).get(rel)
        if content is None or len(content) > room:
            continue
        room -= len(content)
        out.append(f"current content of {rel}:\n{content}")
    return out


def touched_files(ops: _DiffOps) -> str:
    """The paths one diff writes, deletes or patches, for a model-facing summary."""
    return ", ".join(sorted({*ops.writes, *ops.deletes, *ops.patches})) or "no files"


def _build_packet(
    task: Task,
    constraints: Sequence[PinnedConstraint],
    sandbox: Sandbox,
    *,
    turn_budget: int,
    cost_budget_microusd: int,
    prior_failures: tuple[str, ...] = (),
    persona: str = "",
    retrieved_spans: tuple[str, ...] = (),
    rules: tuple[str, ...] = (),
    verify: Mapping[str, str] | None = None,
) -> TaskPacket:
    """Assemble the immutable packet: pinned constraints VERBATIM + FIRST.

    The constraint texts are placed in ``pinned_constraints`` byte-for-byte (no
    paraphrase, no reorder behind the task body); ``assemble_prompt`` renders
    them first. ``retrieved_spans`` grounds the call in the criteria/design the
    task traces to (ROADMAP Phase 4). The proxy endpoint is intentionally NOT
    handed to the adapter here (egress stays None) — the StubAdapter holds no
    egress and the dev sandbox's network denial is "nothing to talk to"."""
    body: dict[str, Any] = {
        "id": task.id,
        "title": task.id,
        "spec_uri": task.spec_uri,
        "branch": task.branch,
        "criterion_ids": list(task.criterion_ids),
    }
    if rules:
        body["rules"] = list(rules)
    if verify:
        body["verify"] = dict(verify)
    return TaskPacket(
        task=body,
        persona=persona,  # area-expert role preamble (empty == unchanged prompt)
        pinned_constraints=pinned_texts(constraints),  # VERBATIM + FIRST (arch §2)
        retrieved_spans=retrieved_spans,  # spec grounding (empty == unchanged prompt)
        prior_failures=prior_failures,
        worktree_path=str(sandbox.worktree),
        turn_budget=turn_budget,
        cost_budget_microusd=cost_budget_microusd,
        proxy_endpoint=None,
    )


def _apply_overlay(worktree: Path, overlay: Mapping[str, str | None]) -> None:
    """Write predecessor slices onto a fresh clone (content ⇒ write, None ⇒
    delete). Paths are containment-checked defensively even though they come
    from our own CAS (same posture as the integration boundary, D-R1-03)."""
    root = worktree.resolve()
    for rel, content in sorted(overlay.items()):
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"overlay path {rel!r} escapes the worktree")
        target = (root / pure).resolve()
        if not str(target).startswith(str(root)):
            raise ValueError(f"overlay path {rel!r} escapes the worktree")
        if content is None:
            if target.is_file():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _sandbox_for(
    rt: AgentRuntime, source_dir: str, snapshot_store: str | None, tier: str | None = None
) -> Sandbox:
    """Build the per-task Sandbox honoring the adapter's DECLARED model-auth needs.

    An agentic CLI substrate (claude-code) declares ``credential_grant`` /
    ``home_passthrough`` on the adapter; the Sandbox validates the grant
    fail-closed against ``GRANTABLE_MODEL_AUTH`` (ROADMAP Phase 1b — model-auth
    only, target-system credentials can never be granted). Adapters that declare
    nothing (stub, raw-react) get the fully-scrubbed cage, byte-identical to
    before. A per-tier adapter (model modes) answers for the substrate that will
    run ``tier``, so a local session's cage never gets the cloud credential."""
    adapter: Any = rt.adapter
    for_tier = getattr(adapter, "for_tier", None)
    if callable(for_tier):
        adapter = for_tier(tier)
    return Sandbox(
        source_dir,
        snapshot_store=snapshot_store,
        credential_grant=tuple(getattr(adapter, "credential_grant", ()) or ()),
        home_passthrough=bool(getattr(adapter, "home_passthrough", False)),
        toolchain_env=bool(getattr(adapter, "toolchain_env", False)),
    )


def _run_one_task(
    rt: AgentRuntime,
    task: Task,
    constraints: Sequence[PinnedConstraint],
    config: RunnableConfig,
    *,
    source_dir: str,
    snapshot_store: str | None,
    turn_budget: int,
    cost_budget_microusd: int,
    prior_failures: tuple[str, ...] = (),
    persona: str = "",
    retrieved_spans: tuple[str, ...] = (),
    overlay: Mapping[str, str | None] | None = None,
    scope: tuple[str, ...] = (),
    rules: tuple[str, ...] = (),
    verify: Mapping[str, str] | None = None,
    review: Callable[[AdapterResult, Sandbox], AdapterResult] | None = None,
) -> dict[str, Any]:
    """Run the adapter for ONE task in a fresh sandbox; return its channel writes.
    A non-empty ``scope`` cuts the diff back to those paths (a noted trim);
    ``review`` then annotates the kept diff while the sandbox is alive.

    A FRESH ``Sandbox`` per task (arch §2(d): no reuse), then the diff keyed by
    ``content_digest`` (L7) so a re-run re-derives the same key and the
    keyed-union channels coalesce the supersession.

    ``overlay`` (the integrated tree, for the fixer) is written onto the fresh
    clone BEFORE the base snapshot, so the recorded base — and the idempotency
    key — reflect what the task actually built against."""
    assert rt.adapter is not None  # guaranteed by the factory's guard
    sandbox = _sandbox_for(rt, source_dir, snapshot_store)
    try:
        if overlay:
            _apply_overlay(sandbox.worktree, overlay)
        base = sandbox.snapshot()
        packet = _build_packet(
            task,
            constraints,
            sandbox,
            turn_budget=turn_budget,
            cost_budget_microusd=cost_budget_microusd,
            prior_failures=prior_failures,
            persona=persona,
            retrieved_spans=retrieved_spans,
            rules=rules,
            verify=verify,
        )
        result: AdapterResult = rt.adapter.run(packet, sandbox)
        from foundry.agents.implementation.tickets import trim_to_write_scope

        # An empty scope still drops gate inputs (feature files, criteria).
        result = trim_to_write_scope(result, sandbox, scope, task.id)
        if review is not None:
            result = review(result, sandbox)
        _mirror_diff_blob(rt, sandbox, result)  # while the sandbox is alive
    finally:
        sandbox.destroy()

    return _finalize_diff(rt, task, constraints, base, result, config)


def _mirror_diff_blob(rt: AgentRuntime, sandbox: Sandbox, result: AdapterResult) -> None:
    """Mirror the adapter's diff BLOB out of the per-task sandbox (destroyed after
    — arch §2(d): no reuse) into the run-scoped CAS so a store-backed resolve_blob
    reads it end to end. Both stores key by the RAW sha256, so the mirrored uri ==
    ``result.diff_uri`` (asserted). No-op when ``rt.cas`` is None (synthetic
    resolver). This is the ONLY shared-state write a pool worker makes — and it is
    safe to call concurrently: ``put_blob`` is content-addressed and idempotent
    (in-memory store = a single GIL-atomic dict setitem; filesystem store =
    thread-unique temp + ``os.replace``), so two workers mirroring the same digest
    converge on identical bytes."""
    if rt.cas is None:
        return
    mirrored = rt.cas.put_blob(sandbox.get_blob(result.diff_uri))
    assert mirrored == result.diff_uri, (
        f"mirrored diff uri {mirrored!r} != adapter uri {result.diff_uri!r}"
    )


def _finalize_diff(
    rt: AgentRuntime,
    task: Task,
    constraints: Sequence[PinnedConstraint],
    base: str,
    result: AdapterResult,
    config: RunnableConfig,
) -> dict[str, Any]:
    """The SEQUENTIAL post-processing for one task's adapter result: key the diff
    (``content_digest``, L7), mint the FeatureDiff + Artifact (``rt.seq``), persist
    the typed body, and emit the done-marker + advisory triage. Shared by the fixer
    and the ticket pool path — ALWAYS called single-threaded from the node so
    ``rt.seq`` (a non-thread-safe epoch counter) and ``rt.cas`` writes never race."""
    key = idempotency_key(task, constraints, base)
    diff = FeatureDiff(
        diff_uri=result.diff_uri,  # cas:// handle from the adapter (DS9)
        task_id=task.id,
        branch=task.branch,
        base_tree_sha=base,
        seq=rt.seq(config, NODE_ID, f"diff:{key}"),
    )
    artifact = Artifact(
        kind="feature_diff",
        uri=f"cas://feature_diff/{key}",  # uri carries the idempotency key
        produced_by=NODE_ID,
        task_id=task.id,
        seq=rt.seq(config, NODE_ID, f"artifact:{key}"),
    )
    # Store the TYPED FeatureDiff body keyed by the handle's OWN uri (which
    # carries the idempotency key, not content_identity), so gate_wiring's
    # resolve(feature_diff_handle) == get_body(artifact.uri) reconstitutes it.
    if rt.cas is not None:
        rt.cas.put_body_at(artifact.uri, diff)
    writes: dict[str, Any] = {
        "artifacts": [artifact],
        "tasks": [_task_done_marker(task, diff)],
    }
    if result.triage:
        writes["gate_triage"] = list(result.triage)
    return writes


def _adapter_name(rt: AgentRuntime) -> str:
    """Stable substrate id for the ExecutionTask (only an identity component)."""
    return str(getattr(rt.adapter, "name", type(rt.adapter).__name__))


def _task_done_marker(task: Task, diff: FeatureDiff) -> dict[str, Any]:
    """The task channel entry, updated with the produced diff handle.

    Keyed by ``id`` (the tasks reducer is a keyed union). NOTE (LEARNINGS IM3): the
    reducer is FIRST-keyed-wins for dicts, so an earlier plain Task dict survives and
    THIS marker is dropped on the `tasks` channel — the produced
    `diff_uri`/`base_tree_sha` are read off the **`artifacts`** channel (the
    FeatureDiff). Do NOT assume a downstream node reads them off the reduced `tasks`
    channel."""
    d = task.model_dump(mode="json")
    d["status"] = TaskStatus.DONE.value
    d["diff_uri"] = diff.diff_uri
    d["base_tree_sha"] = diff.base_tree_sha
    return d


def _fix_focus(state: FactoryState, resolve: ArtifactResolver) -> tuple[str, ...]:
    """The built tickets' files: what the fixer is repairing. Its writes elsewhere
    are kept but noted. Empty = no focus (a built ticket declared no scope)."""
    from foundry.agents.implementation.tickets import (
        built_task_ids,
        read_tickets,
        task_id_for_ticket,
    )

    built = built_task_ids(state)
    files: list[str] = []
    for t in read_tickets(state, resolve):
        if task_id_for_ticket(t.id) not in built:
            continue
        if not t.files:
            return ()
        files.extend(f for f in t.files if f not in files)
    return tuple(files)


def make_fixer_node(
    rt: AgentRuntime,
    *,
    source_dir: str,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    snapshot_store: str | None = None,
    turn_budget: int = _DEFAULT_TURN_BUDGET,
    cost_budget_microusd: int = _DEFAULT_COST_BUDGET_MICROUSD,
) -> NodeFn:
    """One coding session that repairs the INTEGRATED tree after a failed gate.

    Re-running every task from scratch on each fix iteration rebuilt the whole
    project (one fresh agent session per task) to repair what is usually a local
    failure. The fixer instead seeds a single sandbox with the integrated tree, hands
    the adapter the latest failures behind a :func:`_fix_summary`, and emits one
    FeatureDiff whose seq is lifted past every existing diff, so integration applies
    it on top (later-by-Seq wins)."""
    if rt.adapter is None or rt.cas is None:
        raise ValueError("fixer requires rt.adapter and rt.cas (it rebuilds the integrated tree)")

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        import tempfile

        from foundry.graph.gate_wiring import (
            FEATURE_DIFF_KIND,
            GATE_INPUT_FILES,
            MaterializationError,
            _fix_attempts,
            _of_kind,
            materialize_workdir,
        )

        attempt = _fix_attempts(state, config)
        budget = turn_budget
        # The fixer's packet carries no tier, so it runs on the default level.
        is_local = getattr(rt.adapter, "is_local", None)
        if callable(is_local) and is_local(None):
            budget = max(turn_budget, _LOCAL_FIX_TURN_BUDGET)
        try:
            with tempfile.TemporaryDirectory(prefix="foundry-fix-") as tmp:
                materialize_workdir(state, Path(tmp), resolve=resolve,
                                    resolve_blob=resolve_blob, scaffold_manifest=False)
                # Gate inputs (criteria.json) stay out of the agent's worktree: an agent
                # told to "make the gates pass" must not be handed the oracle to edit.
                overlay: dict[str, str | None] = {}
                for f in sorted(Path(tmp).rglob("*")):
                    rel = f.relative_to(tmp).as_posix()
                    if not f.is_file() or rel in GATE_INPUT_FILES:
                        continue
                    try:
                        overlay[rel] = f.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        continue  # a base tree's binary asset: the sandbox clone has it

        except MaterializationError as exc:
            # The gate already recorded this tree as ERROR; returning no diff lets the
            # bounded fix loop halt in order instead of crashing the run.
            return {"agent_journal": [journal_entry(
                node_id=FIXER_NODE_ID, role="Backend Engineer",
                seq=rt.seq(config, FIXER_NODE_ID, f"journal:fix-{attempt}"),
                summary="Could not rebuild the integrated tree to fix it",
                concerns=(str(exc)[:400],),
            )]}
        from foundry.agents.implementation.tickets import (
            CONTRACT_DUP_TRIAGE,
            contract_duplicates,
            verify_commands,
        )
        from foundry.graph.gate_wiring import contract_paths

        # Contract-type copies type-check alone and then misfit their callers; hand
        # any still in the integrated tree to the fixer with the gate failures.
        duplicates = contract_duplicates(overlay, contract_paths(state, resolve))
        from foundry.agents.implementation.tickets import group_failures_by_ticket, read_tickets

        failures = (
            *_fix_summary(state, resolve, resolve_blob, attempt=attempt, tree=overlay),
            *group_failures_by_ticket(_prior_failures(state), read_tickets(state, resolve)),
        )
        if duplicates:
            failures = (*failures, f"[{CONTRACT_DUP_TRIAGE}]", *duplicates)
        from foundry.agents.implementation.tickets import note_fix_writes, pending_wave

        focus = _fix_focus(state, resolve)
        contracts = contract_paths(state, resolve)
        persona = _FIXER_PERSONA
        if focus and pending_wave(state, resolve):
            persona += (" Later waves have not been built yet: fix the files built so far ("
                        + ", ".join(focus) + "), and never build a later ticket's files. "
                        "A change anywhere else is kept but flagged for review.")
        if contracts:
            persona += (" Every ticket builds against the contract files ("
                        + ", ".join(contracts) + "): change them only when the failure is "
                        "in the contract itself; each change is recorded for review.")
        # Numbered by the fixes so far, not the attempt counter: that resets per wave
        # and on a resume, and a reused id would collide with the earlier fix's notes.
        fixes = {a.task_id for a in _of_kind(state, FEATURE_DIFF_KIND)
                 if a.task_id and a.task_id.startswith("fix-")}
        task_id = f"fix-{len(fixes) + 1}"
        task = Task(
            id=task_id,
            spec_uri=f"fix://{attempt}",
            criterion_ids=(),
            branch=f"task/{task_id}",
            seq=rt.seq(config, FIXER_NODE_ID, f"task:{task_id}"),
        )
        writes = _run_one_task(
            rt,
            task,
            state.get("pinned_constraints") or (),
            config,
            source_dir=source_dir,
            snapshot_store=snapshot_store,
            turn_budget=budget,
            cost_budget_microusd=cost_budget_microusd,
            prior_failures=failures,
            persona=persona,
            overlay=overlay,
            rules=_FIXER_RULES,
            verify=verify_commands(state.get("stack")),
            review=lambda result, sandbox: note_fix_writes(
                result, sandbox, task_id, focus=focus, contracts=contracts),
        )
        # Integration orders diffs by Seq. A fresh diff key starts at epoch 1, tying
        # with (and possibly sorting under) the diffs it repairs, so lift its epoch
        # past every existing diff. With every ticket failed there is none yet.
        top = max((a.seq.emission_epoch for a in _of_kind(state, FEATURE_DIFF_KIND)),
                  default=0)
        writes["artifacts"] = [
            a.model_copy(update={"seq": a.seq._replace(emission_epoch=top + 1)})
            for a in writes["artifacts"]
        ]
        writes.setdefault("agent_journal", []).append(journal_entry(
            node_id=FIXER_NODE_ID, role="Backend Engineer",
            seq=rt.seq(config, FIXER_NODE_ID, f"journal:{task_id}"),
            summary=f"Fixed the failing gates (attempt {attempt})",
        ))
        return writes

    return _node
