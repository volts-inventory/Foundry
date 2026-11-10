"""Plan assembly: build, repair, then check the tickets of one frame."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from foundry.agents.plan_contracts import CompileResult
from foundry.agents.planner.build import _build_tickets, _Epic
from foundry.agents.planner.checks import (
    _builtin_method_findings,
    _contract_findings,
    _coverage_findings,
    _offline_scenario_findings,
    _redeclared_findings,
    _size_findings,
    _unused_contracts,
    _wiring_findings,
)
from foundry.agents.planner.folds import (
    _add_contract_deps,
    _add_read_deps,
    _drop_orphan_scenarios,
    _drop_self_consumes,
    _fix_covers,
    _fold_test_bindings,
    _merge_sole_dependents,
    _one_step_module_per_ticket,
    _own_scenario_bindings,
    _own_stack_files,
    _wire_entrypoint,
    stack_files,
)
from foundry.agents.planner.sizing import ALL_CLOUD, Ceilings
from foundry.agents.planner.stack import _attach_generated, _python_deps_ticket
from foundry.base_tree import BaseView
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.implementation import StackDecision, Ticket
from foundry.schemas.product import AcceptanceCriterion, IntentBrief

if TYPE_CHECKING:
    from foundry.state.seq import Seq


@dataclass(frozen=True)
class _Frame:
    """Everything turning raw ticket items into the checked plan needs besides
    the items themselves; fixed for one call-1 plan."""

    names: set[str]  # contract names
    epics: Sequence[_Epic]  # empty for an inline plan
    cap: int
    generated: Sequence[Ticket]
    stack: StackDecision | None
    brief: IntentBrief | None
    run: str | None  # None: the run check is skipped
    declared: frozenset[str]  # contracts the contracts ticket writes in full
    interfaces: Sequence[InterfaceContract]
    language: str  # "" skips the signature checks (unchanged contracts)
    compiled: CompileResult
    tickets_seq: Seq
    criteria_seq: Seq
    base: BaseView | None = None  # a change build's base tree
    stack_findings: tuple[str, ...] = ()  # of a stack chosen this pass
    ceilings: Ceilings = field(default_factory=lambda: ALL_CLOUD)  # per-ticket size, by tier


@dataclass(frozen=True)
class _Fit:
    """The plan's tickets after every mechanical repair, and what the checks left."""

    tickets: list[Ticket]
    criteria: list[AcceptanceCriterion]
    concerns: list[str]
    contract_findings: tuple[str, ...]
    coverage_findings: tuple[str, ...]
    overflow: tuple[str, ...]  # work dropped at a cap: a concern, fed to a revision
    folds: tuple[str, ...] = ()  # the repairs that changed something, for telemetry

    @property
    def blocking(self) -> tuple[str, ...]:
        return (*self.contract_findings, *self.coverage_findings)


def _assemble(raw: Any, frame: _Frame, overflow: Sequence[str] = ()) -> _Fit:
    """Raw ticket items -> the repaired, checked plan. Repairs run first (each one
    mechanical, leaving what it cannot fix to the checks); only the checks' leftovers
    are findings. ``overflow`` names work already dropped at the epic caps. The fit
    names each fold that fired; the planner counts them only for the plan it emits."""
    fired: list[str] = []

    def fold(name: str, more: Sequence[str]) -> list[str]:
        if more:
            fired.append(name.lstrip("_"))
        return list(more)

    over = list(overflow)
    base = frame.base
    tickets, criteria, concerns = _build_tickets(
        raw,
        frame.tickets_seq,
        frame.criteria_seq,
        frame.names,
        cap=frame.cap,
        epics=frame.epics,
        overflow=over,
        reserved=base.feature_names if base is not None else frozenset(),
    )
    generated = (
        *frame.generated,
        *_python_deps_ticket(tickets, frame.generated, frame.stack, base, frame.tickets_seq),
    )
    tickets, more = _attach_generated(tickets, generated, frame.tickets_seq)
    concerns += more
    for repair in (_drop_self_consumes, _add_contract_deps):
        tickets, more = repair(tickets)
        concerns += fold(repair.__name__, more)
    tickets, more, stack_findings = _own_stack_files(
        tickets, frame.stack, frozenset(base.files) if base is not None else frozenset()
    )
    concerns += fold(_own_stack_files.__name__, more)
    tickets, more = _add_read_deps(tickets)
    concerns += fold(_add_read_deps.__name__, more)
    planned = {(t.id, d) for t in tickets for d in t.depends_on}
    tickets, more = _wire_entrypoint(tickets)
    concerns += fold(_wire_entrypoint.__name__, more)
    wired = frozenset((t.id, d) for t in tickets for d in t.depends_on) - planned
    # After the order repairs: the fold reads the final order to judge a scenario
    # no earlier than before.
    tickets, more = _fold_test_bindings(tickets, frame.stack)
    concerns += fold(_fold_test_bindings.__name__, more)
    # After the binding fold, which drops paired test tickets and so leaves a feature
    # ticket's real dependents; before the step-module folds, so the merged ticket
    # binds every scenario it now carries from ONE module.
    tickets, criteria, more = _merge_sole_dependents(
        tickets, criteria, frame.stack, frame.ceilings, wired
    )
    concerns += fold(_merge_sole_dependents.__name__, more)
    for bind in (_own_scenario_bindings, _one_step_module_per_ticket):
        tickets, more = bind(tickets, frame.stack)
        concerns += fold(bind.__name__, more)
    criteria, more = _drop_orphan_scenarios(tickets, criteria)
    concerns += fold(_drop_orphan_scenarios.__name__, more)
    criteria, more = _fix_covers(criteria, frame.brief)
    concerns += fold(_fix_covers.__name__, more)
    contract = (
        *_contract_findings(tickets, criteria, frame.declared),
        *(_redeclared_findings(frame.interfaces, base) if base is not None else ()),
        *_wiring_findings(tickets, frame.interfaces),
        *frame.compiled.findings,
        # mypy, when it ran, is the authority on signatures; else the heuristic.
        *(() if frame.compiled.ran else _builtin_method_findings(frame.interfaces, frame.language)),
    )
    concerns += [*_unused_contracts(tickets, criteria), *frame.compiled.concerns, *over]
    coverage = (
        *stack_findings,
        *_coverage_findings(tickets, criteria, frame.brief, run=frame.run, change=base is not None),
        *_offline_scenario_findings(criteria),
        *_size_findings(
            tickets, frame.ceilings, set(stack_files(frame.stack)) if frame.stack else set()
        ),
        *frame.stack_findings,
    )
    return _Fit(tickets, criteria, concerns, contract, coverage, tuple(over), tuple(fired))
