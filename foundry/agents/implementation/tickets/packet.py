"""Packet assembly: a ticket's context, scenarios, contracts and check rules."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from foundry.agents._runtime import AgentRuntime
from foundry.agents.implementation.interface_map import signature_map
from foundry.agents.implementation.junior_engineer import pinned_texts
from foundry.agents.implementation.personas import persona_for
from foundry.agents.implementation.tickets.constants import (
    FAIL_TRIAGE,
    MAX_TICKET_ATTEMPTS,
    NODE_ID,
    SKIP_TRIAGE,
)
from foundry.agents.implementation.tickets.rules import (
    check_rewrites,
    scenario_rules,
    scenario_verify,
    verify_commands,
    verify_rules,
)
from foundry.execution.adapter_contract import TaskPacket
from foundry.gates.bdd_scaffold import step_module, step_scaffold
from foundry.gates.profiles import pytest_bdd_binds
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateTriage
from foundry.schemas.implementation import (
    FEATURE_DIR,
    HARNESS_TICKET_ID,
    Specialty,
    StackDecision,
    Task,
    Ticket,
    feature_path,
    scenario_ref,
    ticket_effort,
)
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.gates.self_check import CheckBase, CheckSpec
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.schemas.architecture import PinnedConstraint
    from foundry.state.factory_state import FactoryState

    ArtifactResolver = Callable[[Artifact], Any]


def _integrated_overlay(
    state: FactoryState, resolve: ArtifactResolver, resolve_blob: BlobResolver
) -> tuple[dict[str, str | None], str | None]:
    """The integrated tree after the previous waves (feature files included, other
    gate inputs excluded), as an overlay for each ticket's fresh sandbox — plus the
    materialization error when the tree could not be built (overlay then empty)."""
    from foundry.graph.gate_wiring import MaterializationError, integrated_files

    try:
        return dict(integrated_files(state, resolve=resolve, resolve_blob=resolve_blob)), None
    except MaterializationError as exc:
        return {}, str(exc)


def _newest_by(
    state: FactoryState, resolve: ArtifactResolver, kind: str, typ: type[Any], key: str
) -> dict[str, Any]:
    newest: dict[str, tuple[Seq, Any]] = {}
    for handle in (a for a in (state.get("artifacts") or ()) if a.kind == kind):
        body = resolve(handle)
        if isinstance(body, typ):
            k = getattr(body, key)
            if k not in newest or handle.seq > newest[k][0]:
                newest[k] = (handle.seq, body)
    return {k: v[1] for k, v in newest.items()}


def _contract_body(rt: AgentRuntime, contract: InterfaceContract) -> str:
    """The contract's authored body (persisted prose), else its record as JSON."""
    if rt.cas is not None:
        from foundry.schemas.docs import ProseBody

        try:
            body = rt.cas.get_body(contract.spec_uri)
        except Exception:
            body = None
        if isinstance(body, ProseBody):
            return body.text
    return json.dumps(
        {
            "name": contract.name,
            "spec_uri": contract.spec_uri,
            "consumers": list(contract.consumers),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _scenario_text(c: AcceptanceCriterion, owner: str) -> str:
    head = f"SCENARIO {c.id} [{scenario_ref(owner, c.id)}]"
    if not (c.given or c.when or c.then):
        return f"{head}: {c.text}"
    lines = [head + ":"]
    for word, steps in (("Given", c.given), ("When", c.when), ("Then", c.then)):
        for i, step in enumerate(steps):
            lines.append(f"  {word if i == 0 else 'And'} {step}")
    return "\n".join(lines)


def _ticket_scenarios(
    ticket: Ticket, criteria: Mapping[str, AcceptanceCriterion]
) -> dict[str, list[str]]:
    """feature path -> the scenario names (criterion ids) of ``ticket`` in it."""
    out: dict[str, list[str]] = {}
    for cid in ticket.scenario_ids:
        c = criteria.get(cid)
        if c is not None:
            out.setdefault(feature_path(c.ticket_id or ticket.id), []).append(cid)
    return dict(sorted(out.items()))


def _ticket_context(
    rt: AgentRuntime,
    ticket: Ticket,
    criteria: Mapping[str, AcceptanceCriterion],
    contracts: Mapping[str, InterfaceContract],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(spans, feature files): the ticket's scenarios and its contracts, verbatim."""
    spans: list[str] = []
    features: set[str] = set()
    for cid in ticket.scenario_ids:
        c = criteria.get(cid)
        if c is None:
            continue
        owner = c.ticket_id or ticket.id
        features.add(feature_path(owner))
        spans.append(_scenario_text(c, owner))
    for role, names in (
        ("this ticket IMPLEMENTS it — match it exactly", ticket.implements),
        ("this ticket CALLS it — code against it exactly", ticket.consumes),
    ):
        for name in names:
            contract = contracts.get(name)
            body = _contract_body(rt, contract) if contract else "(not in the plan)"
            spans.append(f"INTERFACE CONTRACT {name} ({role}):\n{body}")
    return tuple(spans), tuple(sorted(features))


def ticket_signature_map(
    ticket: Ticket,
    tickets: Sequence[Ticket],
    tree: Mapping[str, str | None],
    contract_files: Sequence[str],
) -> str:
    """The signature map of the integrated ``tree`` a ticket builds on: its contracts
    files, its ``reads``, then its dependency tickets' files (see
    :func:`~foundry.agents.implementation.interface_map.signature_map`)."""
    by_id = {t.id: t for t in tickets}
    deps = [f for d in ticket.depends_on if (t := by_id.get(d)) is not None for f in t.files]
    mention = "\n".join(
        [
            ticket.title,
            ticket.description,
            *ticket.files,
            *ticket.reads,
            *ticket.implements,
            *ticket.consumes,
            *(text for f in ticket.files if isinstance(text := tree.get(f), str)),
        ]
    )
    return signature_map(
        tree,
        contract_files=contract_files,
        reads=ticket.reads,
        dependency_files=deps,
        exclude=ticket.files,
        mention_text=mention,
    )


def _feature_texts(
    paths: Iterable[str],
    tree: Mapping[str, str | None],
    criteria: Mapping[str, AcceptanceCriterion],
) -> dict[str, str]:
    """feature path -> Gherkin: the integrated tree's copy, else rendered from the
    criteria exactly as the gate writes it."""
    from foundry.graph.gate_wiring import render_feature

    out: dict[str, str] = {}
    for p in paths:
        text = tree.get(p)
        if not isinstance(text, str):
            owned = [c for c in criteria.values() if c.ticket_id and feature_path(c.ticket_id) == p]
            text = render_feature(owned[0].ticket_id or "", owned) if owned else None
        if text:
            out[p] = text
    return out


def bdd_scaffold_spans(
    files: Sequence[str],
    features: Mapping[str, str],
    stack: StackDecision | None,
    ticket_id: str = "",
) -> tuple[str, ...]:
    """A span for the pytest-bdd step module of ``files`` (:func:`step_module`): the
    exact step scaffold that binds ``features`` (path -> Gherkin text) — a local
    model that wrote the steps freehand bound the wrong path, defined a step text
    twice and passed values by ``return``."""
    binder = step_module(files, ticket_id)
    tests = (binder,) if binder is not None else ()
    if not features or not tests or not pytest_bdd_binds(stack):
        return ()
    return tuple(
        f"PYTEST-BDD STEP SCAFFOLD for {t} — the exact skeleton of this step module: "
        f"the scenarios() binding and one function per distinct step text of "
        f"{', '.join(features)}. Start from it: fill in the bodies, keep every decorator "
        "and its text exactly as written (each matches the feature's step lines), pass "
        "values between steps through `context` (or target_fixture), and add no other "
        f"step definitions.\n```python\n{step_scaffold(t, features)}```"
        for t in tests
    )


def _check_rules(
    task: Task,
    ticket: Ticket,
    stack: StackDecision | None,
    contract_files: Sequence[str],
    scenarios: Mapping[str, Sequence[str]],
    *,
    check: bool = False,
    scenarios_checked: bool = True,
) -> list[str]:
    """A ticket packet's scenario and verify rules (``check``: for a session that
    has the ``check`` tool; ``scenarios_checked``: that runs its scenarios)."""
    return [
        *scenario_rules(
            ticket.files,
            scenarios,
            stack,
            test=ticket.specialty is Specialty.TEST,
            ticket_id=ticket.id,
            check=check and scenarios_checked,
        ),
        *verify_rules(stack, ticket_id=task.id, contract_files=contract_files, check=check),
    ]


def _ticket_check(
    base: CheckBase,
    state: FactoryState,
    resolve: ArtifactResolver,
    task: Task,
    ticket: Ticket,
    criteria: Mapping[str, AcceptanceCriterion],
    contract_files: Sequence[str],
    scenarios: Mapping[str, Sequence[str]],
) -> CheckSpec:
    """A ticket session's gate check: the scenarios whose carriers are built once
    this ticket is, its own among them (those it carries or owns), its files."""
    from foundry.gates.self_check import CheckSpec
    from foundry.graph.gate_wiring import wave_scenarios

    stack = state.get("stack")
    own = frozenset(
        scenario_ref(c.ticket_id or ticket.id, c.id)
        for c in criteria.values()
        if c.id in ticket.scenario_ids or c.ticket_id == ticket.id
    )
    scope = wave_scenarios(state, resolve, also=(task.id,))
    plain = _check_rules(task, ticket, stack, contract_files, scenarios)
    # A scenario another unbuilt ticket also carries (a paired ticket in this wave)
    # is out of the check's scope: its rule keeps "run them yourself".
    checked = _check_rules(
        task, ticket, stack, contract_files, scenarios, check=True, scenarios_checked=own <= scope
    )
    return CheckSpec(
        base=base,
        scope=scope,
        own_refs=own,
        own_files=tuple(ticket.files),
        rules=check_rewrites(plain, checked),
    )


def _ticket_packet(
    task: Task,
    ticket: Ticket,
    constraints: Sequence[PinnedConstraint],
    *,
    spans: tuple[str, ...],
    feature_files: tuple[str, ...],
    prior_failures: tuple[str, ...],
    turn_budget: int,
    cost_budget_microusd: int,
    stack: StackDecision | None = None,
    contract_files: Sequence[str] = (),
    scenarios: Mapping[str, Sequence[str]] | None = None,
) -> TaskPacket:
    scenarios = scenarios or {}
    rules = [
        "You can read the whole project in the working directory: it holds everything "
        "the earlier tickets built.",
        f"Never edit {FEATURE_DIR}/*.feature files: they are the executable spec.",
        *_check_rules(task, ticket, stack, contract_files, scenarios),
    ]
    if ticket.files:
        rules.insert(
            1,
            "Write ONLY these paths; changes anywhere else are discarded: "
            + ", ".join(ticket.files)
            + ".",
        )
    return TaskPacket(
        task={
            "id": task.id,
            "ticket_id": ticket.id,
            "title": ticket.title,
            "description": ticket.description,
            "specialty": ticket.specialty.value,
            "difficulty": ticket.difficulty.value,
            "tier": task.tier,
            "spec_uri": task.spec_uri,
            "branch": task.branch,
            "criterion_ids": list(task.criterion_ids),
            "depends_on": list(task.depends_on),
            "files": list(ticket.files),
            "reads": list(ticket.reads),
            "feature_files": list(feature_files),
            "rules": rules,
            "verify": {
                **verify_commands(stack),
                **scenario_verify(ticket.files, scenarios, stack, ticket.id),
            },
        },
        persona=persona_for(ticket.specialty.value, stack, harness=ticket.id == HARNESS_TICKET_ID),
        pinned_constraints=pinned_texts(constraints),  # VERBATIM + FIRST (arch §2)
        retrieved_spans=spans,
        prior_failures=prior_failures,
        effort=ticket_effort(ticket.difficulty),
        worktree_path=task.branch,  # placeholder; adapters use sandbox.worktree
        turn_budget=turn_budget,
        cost_budget_microusd=cost_budget_microusd,
        proxy_endpoint=None,
    )


def _failure_notes(
    rt: AgentRuntime, config: RunnableConfig, tid: str, attempt: int, exc: Exception
) -> list[GateTriage]:
    """The triage for a ticket's ``attempt``-th failure, plus the skip marker once
    :data:`MAX_TICKET_ATTEMPTS` is spent (so the wave loop terminates)."""
    notes = [
        GateTriage(
            name=f"{FAIL_TRIAGE}:{attempt}",
            run_against=f"task/{tid}",
            explanation=f"attempt {attempt} failed: {exc}"[:2000],
            produced_by=NODE_ID,
            seq=rt.seq(config, NODE_ID, f"fail:{tid}"),
        )
    ]
    if attempt >= MAX_TICKET_ATTEMPTS:
        notes.append(
            GateTriage(
                name=SKIP_TRIAGE,
                run_against=f"task/{tid}",
                explanation=f"not built: failed {attempt} attempt(s); last error: {exc}"[:2000],
                produced_by=NODE_ID,
                seq=rt.seq(config, NODE_ID, f"skip:{tid}"),
            )
        )
    return notes
