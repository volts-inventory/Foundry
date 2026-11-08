"""Pinned constraints (the PC-* stack table), ownership, scope and triage records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents.planner.prompt import _forced_standard
from foundry.agents.planner.readers import _enum, _objs, _section, _str, _strs, _uniq
from foundry.agents.planner.schema import (
    _DEFAULT_CAP_MICROUSD,
    _ROUTE_CAP_MICROUSD,
    _SPEC_GATE,
    NODE_ID,
)
from foundry.schemas.architecture import ConstraintScope, OwnershipMap, PinnedConstraint
from foundry.schemas.gates import GateTriage
from foundry.schemas.governance import ScopeDecision, ScopeTier
from foundry.schemas.implementation import StackDecision, Ticket
from foundry.schemas.product import CriteriaCritique
from foundry.state.reducers import live_constraints
from foundry.telemetry import count

if TYPE_CHECKING:
    from foundry.state.factory_state import FactoryState
    from foundry.state.seq import Seq


def _build_constraints(raw: Any, seq: Seq) -> list[PinnedConstraint]:
    return [
        PinnedConstraint(
            id=_str(item.get("id"), f"PC-{i:03d}"),
            text=_str(item.get("text")),
            # Unknown scope is injected as global: the fail-safe direction.
            scope=_enum(ConstraintScope, item.get("scope"), ConstraintScope.GLOBAL),
            source=_str(item.get("source"), NODE_ID),
            seq=seq._replace(local=i),
        )
        for i, item in enumerate(_objs(raw))
    ]


_SQLITE_MEMORY_CONSTRAINT = {
    "id": "PC-python-sqlite-memory",
    "text": (
        "Python SQLite: every sqlite3.connect(':memory:') opens a NEW, empty database, "
        "so an in-memory store must open ONE connection and reuse it for its whole "
        "life (schema, writes and reads), never connect per call. That database then "
        "outlives each test, and pytest runs every test module in one process: a "
        "conftest.py autouse fixture must give each test an empty database (drop and "
        "re-create the schema, or delete every row), or one test's rows break the next."
    ),
    "scope": "global",
    "source": NODE_ID,
}


_SQLALCHEMY_TYPED_CONSTRAINT = {
    "id": "PC-python-sqlalchemy-typed",
    "text": (
        "Python SQLAlchemy: declare ORM models in the SQLAlchemy 2.0 typed style — a "
        "`class Base(DeclarativeBase)` and columns as `id: Mapped[int] = "
        "mapped_column(primary_key=True)` — never a bare `id = Column(Integer)`: the "
        "type checker reads a bare Column attribute as Column[int], not int, so every "
        "caller that passes it on as a plain value fails the typecheck. Pin "
        "SQLAlchemy>=2.0."
    ),
    "scope": "global",
    "source": NODE_ID,
}


_SQLALCHEMY_RELATIONSHIP_CONSTRAINT = {
    "id": "PC-python-sqlalchemy-relationships",
    "text": (
        "Python SQLAlchemy relationships: write `back_populates=` or `backref=` only "
        "when BOTH mapped classes are in this ticket's files, or the other class "
        "already exists (written by a ticket this one depends on) in a file this "
        "ticket also lists in its files and so can add the matching side. Otherwise "
        "declare a one-sided `relationship()` with neither: SQLAlchemy configures "
        "every mapper together on first use, so a back_populates naming an attribute "
        "the other class lacks fails every model, every query and every test."
    ),
    "scope": "global",
    "source": NODE_ID,
}


_SQLALCHEMY_SQLITE_CONSTRAINT = {
    "id": "PC-python-sqlalchemy-sqlite",
    "text": (
        "Python SQLAlchemy over SQLite: every module reaches the database through "
        "SQLAlchemy, never through sqlite3.connect(). An in-memory database is ONE "
        "shared engine, `create_engine('sqlite://', poolclass=StaticPool, "
        "connect_args={'check_same_thread': False})`; `Base.metadata.create_all`, "
        "`drop_all` and `Session` take that engine, never a sqlite3 connection, which "
        "has none of the methods they call (AttributeError: 'sqlite3.Connection' "
        "object has no attribute '_run_ddl_visitor')."
    ),
    "scope": "global",
    "source": NODE_ID,
}


def _stack_constraints(
    stack: StackDecision | None, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Pinned constraints the planner adds itself for a known stack footgun."""
    if stack is None or stack.language != "python":
        return []
    text = str(payload).lower()
    added = [
        c
        for c, words in (
            (_SQLITE_MEMORY_CONSTRAINT, ("sqlite",)),
            (_SQLALCHEMY_TYPED_CONSTRAINT, ("sqlalchemy",)),
            (_SQLALCHEMY_RELATIONSHIP_CONSTRAINT, ("sqlalchemy",)),
            (_SQLALCHEMY_SQLITE_CONSTRAINT, ("sqlite", "sqlalchemy")),
        )
        if all(w in text for w in words)
    ]
    for c in added:
        count(f"planner.{c['id']}")
    return added


def ticket_owner(ticket: Ticket) -> str:
    """Owner name for a ticket's files: the specialty leads it, so area
    classification (whole-token) lands frontend/data tickets in their lane."""
    return f"{ticket.specialty.value}-{ticket.id}"


def _ownership_from_tickets(tickets: list[Ticket], seq: Seq) -> OwnershipMap:
    """Compatibility view for the ownership-driven scheduler: every ticket file
    is a unit. A file shared by tickets goes to the later ticket and carries the
    union of their scenario ids."""
    owners: dict[str, str] = {}
    unit_criteria: dict[str, tuple[str, ...]] = {}
    for t in tickets:
        for path in t.files:
            owners[path] = ticket_owner(t)
            merged = _uniq((*unit_criteria.get(path, ()), *t.scenario_ids))
            if merged:
                unit_criteria[path] = merged
    return OwnershipMap(owners=owners, unit_criteria=unit_criteria, seq=seq)


def _retired_constraints(
    state: FactoryState, emitted: list[PinnedConstraint], seq: Seq
) -> list[PinnedConstraint]:
    """Tombstones for this node's prior live constraints a re-plan dropped.

    A record whose ``supersedes`` names its own id retires that id in
    ``live_constraints``; the newer Seq makes it the id's latest record."""
    kept = {c.id for c in emitted}
    stale = sorted(
        (
            c
            for c in live_constraints(list(state.get("pinned_constraints") or ()))
            if c.seq.node_id == NODE_ID and c.id not in kept
        ),
        key=lambda c: c.id,
    )
    return [
        c.model_copy(update={"supersedes": c.id, "seq": seq._replace(local=len(emitted) + i)})
        for i, c in enumerate(stale)
    ]


def _route_cap(state: FactoryState) -> int:
    return _ROUTE_CAP_MICROUSD.get(str(state.get("route")), _DEFAULT_CAP_MICROUSD)


def _scope_decision(state: FactoryState, payload: dict[str, Any], seq: Seq) -> ScopeDecision:
    if _forced_standard(state):
        return ScopeDecision(
            tier=ScopeTier.STANDARD,
            rationale="Standard scope: a design spec was supplied.",
            seq=seq,
        )
    scope = _section(payload, "scope")
    # Anything that is not exactly "demo" is STANDARD: an absent or garbled
    # answer must never silently cap the build.
    raw_tier = _str(scope.get("tier")).strip().lower()
    tier = ScopeTier.DEMO if raw_tier == ScopeTier.DEMO.value else ScopeTier.STANDARD
    return ScopeDecision(
        tier=tier,
        rationale=_str(scope.get("rationale")),
        signals=_strs(scope.get("signals")),
        seq=seq,
    )


def _triage(critique: CriteriaCritique, prd_uri: str, seq: Seq) -> GateTriage:
    """The advisory the Spec gate's operators read; the gate re-derives the FAIL
    from the materialized omissions itself (L5)."""
    if critique.coverage_findings or critique.contract_findings:
        explanation = (
            "BLOCKING: the "
            + " and ".join(
                f"{what} check failed — {'; '.join(found)}"
                for what, found in (
                    ("contract fit", critique.contract_findings),
                    ("coverage", critique.coverage_findings),
                )
                if found
            )
            + (
                f" Security/abuse omissions: {'; '.join(critique.security_abuse_omissions)}."
                if critique.security_abuse_omissions
                else ""
            )
            + " Any of these fails the Spec gate."
        )
    elif critique.spec_gate_blocking():
        explanation = (
            "BLOCKING: applicable security/abuse acceptance criteria are omitted — "
            f"{'; '.join(critique.security_abuse_omissions)}. "
            "Omission of applicable security/abuse criteria fails the Spec gate."
        )
    elif critique.missing:
        explanation = (
            f"Non-blocking: missing or under-specified criteria — {'; '.join(critique.missing)}."
        )
    else:
        explanation = "No criteria-completeness omissions found."
    return GateTriage(
        name=_SPEC_GATE,
        run_against=prd_uri,
        explanation=explanation,
        produced_by=NODE_ID,
        seq=seq,
    )
