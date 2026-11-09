"""Failure clusters, the next pass's plan, and ticket restarts / resets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from foundry.graph.gate_wiring.diffs import _diff_ops
from foundry.graph.gate_wiring.ledger import (
    _STAGES,
    RESTARTED_TRIAGE,
    _newest_per_name,
    window_passes,
)
from foundry.graph.gate_wiring.materialize import (
    FEATURE_DIFF_KIND,
    RESTART_PREFIX,
    _of_kind,
    _resolve_typed,
    contract_paths,
    integrated_files,
)
from foundry.graph.gate_wiring.routing import (
    _BOUNDARY_GATES,
    PIP_MANIFESTS,
    _failure_signature,
    failing_ids,
    failure_files,
    is_collection_error,
)
from foundry.schemas.base import Artifact
from foundry.schemas.gates import FixPass, GateResult, GateStatus
from foundry.schemas.implementation import (
    CONTRACTS_TICKET_ID,
    SKELETON_TASK_ID,
    FeatureDiff,
    Ticket,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from foundry.graph.gate_wiring.diffs import BlobResolver
    from foundry.graph.gate_wiring.materialize import ArtifactResolver
    from foundry.state.factory_state import FactoryState


# ---- one failure cluster per fix pass, and ticket restarts --------------------
#
# Each fixer pass is aimed at ONE cluster of the failures (Ralph's "one item per
# loop"): the build before the tests, then the cluster with the most failures. A
# cluster tried twice without the tree improving gives way to the next. Repairing
# the same code again decays fast (arXiv 2506.18403): a cluster that maps to one or
# two tickets is, once, rebuilt from scratch instead — the tickets' files reset to
# the tree before them, each ticket re-run on its own tier with a memo of what the
# repairs tried. The gate node plans each pass on the newest ledger entry
# (``target``, ``restart_tickets``), so the fixer, the engineering node and a
# resumed run all read the same plan.

_ROTATE_AFTER = 2  # passes on a cluster without the tree improving
_RESTART_TICKETS = 2  # the most tickets one restart rebuilds
_BDD_LINE = re.compile(r"^BDD criterion (\S+): scenario (\S+)")


@dataclass(frozen=True)
class FailureCluster:
    """Failures one fix pass repairs together. ``key`` is ``build`` / a gate name,
    ``ticket:<ids>`` (the built tickets owning the failing scenarios or test
    modules), or ``feature:<path>`` / ``file:<path>`` when no ticket owns them;
    ``items`` are the ledger's ``gate:ident`` failures; ``tickets`` the task ids
    the cluster maps to. ``in_tests``: the failure is in a test module itself (it
    fails to collect, or a step cannot bind), so its tests are what to rebuild."""

    key: str
    stage: int
    items: tuple[str, ...]
    tickets: tuple[str, ...] = ()
    in_tests: bool = False


def _owners(tickets: Iterable[Ticket], built: set[str], path: str) -> list[str]:
    from foundry.agents.implementation.tickets import in_write_scope, task_id_for_ticket

    return sorted(
        {
            task_id_for_ticket(t.id)
            for t in tickets
            if task_id_for_ticket(t.id) in built and t.files and in_write_scope(path, t.files)
        }
    )


def failure_clusters(
    gates: Iterable[GateResult],
    tickets: Sequence[Ticket],
    built: Iterable[str],
) -> tuple[FailureCluster, ...]:
    """The FAILed gates' failures in clusters, highest priority first: earlier stages
    (build, test, smoke) first; within a stage a test module that fails to collect
    (it hides every test in it), then the most failures. Empty when a gate ERRORed
    (an infra error is no failure to aim a pass at)."""
    from foundry.agents.implementation.tickets import task_id_for_ticket

    gates = [g for g in gates if g.status in (GateStatus.FAIL, GateStatus.ERROR)]
    if any(g.status is GateStatus.ERROR for g in gates):
        return ()
    built = set(built)
    carriers: dict[str, set[str]] = {}
    for t in tickets:
        if task_id_for_ticket(t.id) in built:
            for cid in t.scenario_ids:
                carriers.setdefault(cid, set()).add(task_id_for_ticket(t.id))
    stage = {name: i for i, name in enumerate(_STAGES)}
    # key -> (stage, collection error?, items, owners, in a test module?)
    groups: dict[str, list[Any]] = {}

    def add(
        key: str,
        rank: int,
        item: str,
        owners: Iterable[str],
        *,
        collect: bool = False,
        in_tests: bool = False,
    ) -> None:
        group = groups.setdefault(key, [rank, False, [], set(), False])
        group[1] = group[1] or collect
        if item not in group[2]:
            group[2].append(item)
        group[3].update(owners)
        group[4] = group[4] or in_tests

    for g in gates:
        rank = stage.get(g.name, len(_STAGES))
        ids = failing_ids(g.findings)
        if g.name == "test" and ids:
            criteria = {
                m.group(2): m.group(1) for line in g.findings if (m := _BDD_LINE.match(line))
            }
            unbound = set(failure_files([g]))
            for ident in ids:
                path = ident.split("::", 1)[0]
                owners = (
                    sorted(carriers.get(criteria[ident], ()))
                    if ident in criteria
                    else _owners(tickets, built, path)
                )
                kind = "feature" if ident in criteria else "file"
                key = "ticket:" + "+".join(owners) if owners else f"{kind}:{path}"
                collect = is_collection_error(ident)
                add(
                    key,
                    rank,
                    f"{g.name}:{ident}",
                    owners,
                    collect=collect,
                    in_tests=collect or path in unbound,
                )
            continue
        signature = _failure_signature([g]) or frozenset()
        named = failure_files([g]) if g.name == "build" else ()
        owners = sorted({o for f in named for o in _owners(tickets, built, f)})
        for _, ident in sorted(signature):
            add(g.name, rank, f"{g.name}:{ident}", owners)
        if not signature:
            add(g.name, rank, f"{g.name}:", owners)
    return tuple(
        FailureCluster(
            key=key, stage=rank, items=tuple(items), tickets=tuple(sorted(own)), in_tests=in_tests
        )
        for key, (rank, _collect, items, own, in_tests) in sorted(
            groups.items(), key=lambda kv: (kv[1][0], not kv[1][1], -len(kv[1][2]), kv[0])
        )
    )


def _stuck(passes: Sequence[FixPass], key: str) -> int:
    """How many passes aimed at ``key`` in a row, newest back, left the tree no
    better than the window's best."""
    n = 0
    for p in reversed(passes):
        if p.cluster != key or p.outcome in ("initial", "restored"):
            continue
        if p.outcome == "improved":
            break
        n += 1
    return n


def plan_next_pass(
    passes: Sequence[FixPass],
    clusters: Sequence[FailureCluster],
    *,
    restart_after: int,
    restartable: Callable[[FailureCluster], tuple[str, ...]],
    dead_end: bool = False,
    unimproved_only: bool = False,
) -> tuple[str | None, tuple[str, ...]]:
    """``(cluster key, tickets to restart)`` for the pass after ``passes`` (one
    window, in pass order): the first cluster by priority not yet tried
    :data:`_ROTATE_AFTER` times in a row without progress — restarted instead once
    it has been tried ``restart_after`` times and its tickets can be. With every
    cluster spent, the least-tried one. ``dead_end`` (the loop would stop here)
    restarts the first cluster that still can be. ``unimproved_only`` restarts only
    a cluster no pass of the window has improved."""
    if not clusters:
        return None, ()
    target: FailureCluster | None = None
    for c in clusters:
        stuck = _stuck(passes, c.key)
        fresh = not unimproved_only or not any(
            p.cluster == c.key and p.outcome == "improved" for p in passes
        )
        if stuck >= restart_after and fresh and (tickets := restartable(c)):
            return c.key, tickets
        if stuck < _ROTATE_AFTER:
            target = c
            break
    if dead_end:
        for c in clusters:
            if tickets := restartable(c):
                return c.key, tickets
    if target is None:
        target = min(clusters, key=lambda c: _stuck(passes, c.key))
    return target.key, ()


def restarted_task_ids(state: FactoryState) -> set[str]:
    """Tickets that spent their one restart this run (whatever its outcome)."""
    return {
        t.run_against.removeprefix("task/")
        for t in state.get("gate_triage") or ()
        if t.name == RESTARTED_TRIAGE
    }


def restart_set(
    state: FactoryState,
    resolve: ArtifactResolver,
    task_ids: Iterable[str],
    *,
    tests_ok: bool = False,
) -> tuple[str, ...]:
    """The tickets of ``task_ids`` that can be restarted together, else ``()``: one
    or two built planned tickets with declared files, neither restarted before nor
    the contracts ticket or the skeleton (every other ticket builds against those),
    whose files no other built ticket's scope shares (a rebuild rewrites its whole
    scope). A ticket that writes only tests is left out unless ``tests_ok`` (the
    failure is in the test module itself): rebuilding the tests of failing code
    rewrites the checks, not the code under them."""
    from foundry.agents.implementation.interface_map import _is_test
    from foundry.agents.implementation.tickets import (
        _scopes_overlap,
        built_task_ids,
        read_tickets,
        task_id_for_ticket,
    )

    planned = {task_id_for_ticket(t.id): t for t in read_tickets(state, resolve)}
    ids = tuple(
        sorted(
            tid
            for tid in set(task_ids)
            if tests_ok
            or tid not in planned
            or not planned[tid].files
            or not all(map(_is_test, planned[tid].files))
        )
    )
    if not 1 <= len(ids) <= _RESTART_TICKETS:
        return ()
    spent = restarted_task_ids(state)
    built = built_task_ids(state)
    picks = [planned[tid] for tid in ids if tid in planned]
    if (
        len(picks) != len(ids)
        or any(not t.files for t in picks)
        or any(
            tid in spent or tid not in built or tid in (CONTRACTS_TICKET_ID, SKELETON_TASK_ID)
            for tid in ids
        )
    ):
        return ()
    others = [t for tid, t in planned.items() if tid in built and tid not in ids]

    def own(t: Ticket) -> tuple[str, ...]:
        # A shared manifest doesn't block a restart: the reset never touches one, and
        # the rebuilt session edits it as the tree now has it.
        return tuple(f for f in t.files if not _is_manifest(f))

    if any(_scopes_overlap(own(p), own(o)) for p in picks for o in others):
        return ()
    if len(picks) == 2 and _scopes_overlap(own(picks[0]), own(picks[1])):
        return ()
    return ids


def pending_restarts(state: FactoryState, resolve: ArtifactResolver) -> tuple[str, ...]:
    """The tickets the newest check of the current fix loop planned to restart, not
    yet restarted."""
    passes = window_passes(state, resolve)
    if not passes:
        return ()
    spent = restarted_task_ids(state)
    return tuple(t for t in passes[-1].restart_tickets if t not in spent)


def restart_cluster(
    state: FactoryState,
    resolve: ArtifactResolver,
    gates: Iterable[GateResult],
    boundary: str,
    tickets: tuple[str, ...],
) -> str:
    """The key of the failure cluster a restart of ``tickets`` that stands in for an
    escalation rebuilds: the first of ``gates``' clusters that maps to one of them,
    else a ``ticket:`` key of their own."""
    from foundry.agents.implementation.tickets import built_task_ids, read_tickets

    names = _BOUNDARY_GATES[boundary]
    verdicts = _newest_per_name(g for g in gates if g.name in names)
    clusters = failure_clusters(
        verdicts.values(), read_tickets(state, resolve), built_task_ids(state)
    )
    return next(
        (c.key for c in clusters if set(c.tickets) & set(tickets)), "ticket:" + "+".join(tickets)
    )


def fizzled_restart(state: FactoryState, resolve: ArtifactResolver) -> bool:
    """True iff the current fix loop's newest planned restart was spent without a
    rebuild landing (every attempt failed) and no pass landed since: the tree it
    re-checks is unchanged because nothing ran on it, which is no verdict on a
    pass — the legacy no-progress halt waits for the next pass."""
    passes = window_passes(state, resolve)
    planned = next((i for i in range(len(passes) - 1, -1, -1) if passes[i].restart_tickets), None)
    if planned is None or planned == len(passes) - 1:
        return False
    tickets = passes[planned].restart_tickets
    if any(p.fix_task is not None for p in passes[planned + 1 :]) or not set(
        tickets
    ) <= restarted_task_ids(state):
        return False
    rebuilt = {a.task_id for a in _of_kind(state, FEATURE_DIFF_KIND)}
    return not any(f"{RESTART_PREFIX}{t}" in rebuilt for t in tickets)


def ticket_reset(
    state: FactoryState,
    tickets: Sequence[Ticket],
    *,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """``(writes, deletes)`` that put ``tickets``' files (their write scope and every
    file their diffs touched) back as they were before the first of them was built:
    the integrated tree of the diffs that came before it. Only content the tickets
    and the repairs of the restarted cluster produced is reset: a path any later
    diff of another ticket touched, or a fix (or the revert of one) aimed at another
    cluster, stays as it is — so the reset never deletes or rewrites another ticket's
    work. Contract files, gate inputs and dependency manifests (and their lockfiles,
    which every ticket's install shares) stay as they are too. Raises
    :class:`MaterializationError` like the gates."""
    from foundry.agents.implementation.tickets import (
        _is_gate_input,
        in_write_scope,
        task_id_for_ticket,
    )

    ids = {task_id_for_ticket(t.id) for t in tickets}
    diffs = _of_kind(state, FEATURE_DIFF_KIND)
    own = [a for a in diffs if a.task_id in ids]
    if not own:
        return {}, ()
    first = min(a.seq for a in own)
    earlier: FactoryState = {
        **state,
        "artifacts": [
            a for a in state.get("artifacts") or () if a.kind != FEATURE_DIFF_KIND or a.seq < first
        ],
    }
    before = integrated_files(earlier, resolve=resolve, resolve_blob=resolve_blob)
    current = integrated_files(state, resolve=resolve, resolve_blob=resolve_blob)
    ours = _cluster_repairs(state, resolve)

    def paths(art: Artifact) -> set[str]:
        ops = _diff_ops(_resolve_typed(art, resolve, FeatureDiff, FEATURE_DIFF_KIND), resolve_blob)
        return {*ops.writes, *ops.deletes, *ops.patches}

    touched: set[str] = set()
    foreign: set[str] = set()
    for art in diffs:
        if art.task_id in ids:
            touched |= paths(art)
        elif art.seq > first and art.task_id not in ours:
            foreign |= paths(art)
    declared = [f for t in tickets for f in t.files]
    frozen = set(contract_paths(state, resolve))

    def owned(path: str) -> bool:
        return (
            path not in frozen
            and path not in foreign
            and not _is_gate_input(path)
            and not _is_manifest(path)
            and (path in touched or (bool(declared) and in_write_scope(path, declared)))
        )

    writes = {p: c for p, c in before.items() if owned(p) and current.get(p) != c}
    deleted = tuple(sorted(p for p in current if owned(p) and p not in before))
    return writes, deleted


def narrow_reset(
    state: FactoryState,
    reset: tuple[dict[str, str], tuple[str, ...]],
    kept: Sequence[Ticket],
    failed: Sequence[Ticket],
    *,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """``reset`` (the :func:`ticket_reset` of ``kept`` and ``failed`` together) cut
    to what the ``kept`` rebuilds replace: a path in a failed rebuild's write scope,
    or one its earlier diffs touched, keeps its current content — the earlier build
    of that ticket stays integrated. A path in a kept ticket's scope is reset
    whatever touched it: its rebuild was written on the reset content."""
    from foundry.agents.implementation.tickets import in_write_scope, task_id_for_ticket

    ids = {task_id_for_ticket(t.id) for t in failed}
    theirs: set[str] = set()
    for art in _of_kind(state, FEATURE_DIFF_KIND):
        if art.task_id in ids:
            ops = _diff_ops(
                _resolve_typed(art, resolve, FeatureDiff, FEATURE_DIFF_KIND), resolve_blob
            )
            theirs |= {*ops.writes, *ops.deletes, *ops.patches}
    mine = [f for t in kept for f in t.files]
    held = [f for t in failed for f in t.files]

    def stays(path: str) -> bool:
        if mine and in_write_scope(path, mine):
            return False
        return path in theirs or (bool(held) and in_write_scope(path, held))

    return (
        {p: c for p, c in reset[0].items() if not stays(p)},
        tuple(p for p in reset[1] if not stays(p)),
    )


def _cluster_repairs(state: FactoryState, resolve: ArtifactResolver) -> set[str]:
    """The fix and revert diffs of the current fix loop aimed at the cluster its
    newest check plans to restart: a revert undoes the pass the check before it
    judged, so it counts for that pass's cluster."""
    passes = window_passes(state, resolve)
    if not passes or passes[-1].target is None:
        return set()
    target = passes[-1].target
    out: set[str] = set()
    for i, p in enumerate(passes):
        if p.fix_task is None:
            continue
        aimed = passes[i - 1].cluster if p.outcome == "restored" and i else p.cluster
        if aimed == target:
            out.add(p.fix_task)
    return out


def _is_manifest(path: str) -> bool:
    from foundry.agents.implementation.tickets import _LOCKFILES

    name = path.rpartition("/")[2]
    return (
        name in PIP_MANIFESTS
        or name in _LOCKFILES
        or any(name in locks for locks in _LOCKFILES.values())
        or (name.startswith("requirements") and name.endswith(".txt"))
    )
