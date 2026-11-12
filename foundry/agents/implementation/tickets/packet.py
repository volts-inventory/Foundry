"""Packet assembly: a ticket's context, scenarios, contracts and check rules."""

from __future__ import annotations

import json
import re
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
from foundry.gates.bdd_scaffold import is_helper_module, step_module, step_scaffold
from foundry.gates.profiles import property_testing, pytest_bdd_binds
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateTriage
from foundry.schemas.implementation import (
    CONTRACTS_TICKET_ID,
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
    tickets: Sequence[Ticket] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(spans, feature files): the ticket's scenarios and its contracts, verbatim,
    then the signature of each contract a dependency ticket implements that it does
    not name (sessions otherwise opened turns reading the dependency's files, or a
    contracts module that never existed, to learn them)."""
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
    named = {*ticket.implements, *ticket.consumes}
    by_id = {t.id: t for t in tickets}
    for dep in ticket.depends_on:
        for name in by_id[dep].implements if dep in by_id else ():
            contract = contracts.get(name)
            if name in named or contract is None:
                continue
            named.add(name)
            body = contract.signature or _contract_body(rt, contract)
            spans.append(
                f"INTERFACE CONTRACT {name} (ticket {dep}, which this ticket depends on, "
                f"implements it — call it exactly as signed):\n{body}"
            )
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


ENTRYPOINT_RULE = (
    "ENTRYPOINT: you wire the app together. Import the modules the tickets you depend "
    "on built and register or call them (their routes, blueprints, services, models, "
    "CLI commands); never re-implement their routes or logic here. A second copy "
    "type-checks, while the tested modules ship as dead code."
)


PRODUCTION_WIRING_RULE = (
    "TEST THROUGH PRODUCTION WIRING: step and test setup builds the app through its "
    "real factory or entrypoint (e.g. create_app() with a test config, the CLI's "
    "main(), the server's app object), never a hand-assembled copy of its routes, "
    "services or store: a test that wires its own app passes while the shipped "
    "entrypoint is broken."
)


def ticket_properties(
    ticket: Ticket, tickets: Sequence[Ticket], contracts: Mapping[str, InterfaceContract]
) -> tuple[str, ...]:
    """ "<contract>: PRn: <statement>" for each contract property ``ticket`` tests:
    those of the contracts it implements when it writes a test file, else (a test
    ticket) those of the tickets it depends on that write none."""

    def props(t: Ticket) -> list[str]:
        return [
            f"{name}: {p}"
            for name in t.implements
            if (c := contracts.get(name)) is not None
            for p in c.properties
        ]

    if any(_is_test_path(f) for f in ticket.files):
        out = props(ticket)
        if ticket.specialty is Specialty.TEST:
            by_id = {t.id: t for t in tickets}
            for d in ticket.depends_on:
                dep = by_id.get(d)
                if dep is not None and not any(_is_test_path(f) for f in dep.files):
                    out += props(dep)
        return tuple(dict.fromkeys(out))
    return ()


def property_rule(properties: Sequence[str], stack: StackDecision | None) -> str | None:
    """The rule a ticket implementing contracts with ``properties`` follows: one
    property-based test per property, in the stack language's library (None when
    there are none, or the language has no library)."""
    lib = property_testing(stack.language if stack is not None else "python")
    if not properties or lib is None:
        return None
    return (
        f"PROPERTY TESTS (checked: the Test gate FAILs a property no test names): write "
        f"ONE property-based test per contract property below with {lib.library}: "
        f"{lib.usage}. Put the property id (PR1, ...) in the test's name. Each test "
        "generates its inputs across the whole domain the property states (every "
        "feature of the input and their combinations, never a fixed example list) and "
        "calls the real function you implement, never a copy or a mock of it. Import "
        f"{lib.library} unconditionally: never behind a try/except ImportError, an "
        "optional or dynamic import, or a skip when it is missing (the gate counts a "
        "guarded test as no test)"
        + (f"; declare {lib.dependency} if no ticket has yet" if lib.dependency else "")
        + ". A failing property is a bug in the code: fix the code, never weaken the "
        "property. Properties: " + "; ".join(properties)
    )


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        name.startswith("test_")
        or name.rsplit(".", 1)[0].endswith(("_test", ".test", ".spec"))
        or any(p in ("tests", "test", "step_definitions", "__tests__") for p in parts[:-1])
    )


def _inline_rule(spans: Sequence[str], blocked: Sequence[str] = ()) -> str:
    """Where the packet already carries what a session would otherwise open turns
    re-reading: its scenarios, contracts and dependency notes."""
    has = [
        what
        for what, head in (("scenarios", "SCENARIO "), ("interface contracts", "INTERFACE "))
        if any(s.startswith(head) for s in spans)
    ]
    carried = (
        f"Your {' and '.join(has)} are inline in this packet, verbatim: do not read "
        f"{FEATURE_DIR}/*.feature to find them"
        if has
        else f"This ticket owns no scenario: do not read {FEATURE_DIR}/*.feature"
    )
    tests = " or test/step module" if any(not p.endswith(".feature") for p in blocked) else ""
    return (
        f"{carried}, nor another ticket's feature file{tests} (not your spec). Do not re-read "
        "requirements.txt or another manifest unless it is one of the paths you write "
        "(the check installs what it declares). Read a file only for content this "
        "packet does not carry, then write."
    )


def _foreign_feature(path: str, own: Sequence[str]) -> bool:
    return path.startswith(f"{FEATURE_DIR}/") and path.endswith(".feature") and path not in own


def blocked_reads(
    ticket: Ticket, tickets: Sequence[Ticket], feature_files: Sequence[str]
) -> tuple[str, ...]:
    """The files a ticket session may not read: other tickets' feature files and
    their test and step modules — not its spec, and a local session opened each
    ticket with reads of them. Shared helpers (conftest.py, fixtures, utils) and the
    contracts and BDD harness tickets' files stay readable; the harness ticket
    itself reads anything."""
    if ticket.id == HARNESS_TICKET_ID:
        return ()
    own = set(ticket.files)
    out: list[str] = []
    for t in tickets:
        if t.id in (ticket.id, CONTRACTS_TICKET_ID, HARNESS_TICKET_ID):
            continue
        if _foreign_feature(path := feature_path(t.id), feature_files):
            out.append(path)
        out += [
            f
            for f in t.files
            if f not in own
            and not f.endswith("/")
            and _is_test_path(f)
            and f.rsplit("/", 1)[-1] not in ("conftest.py", "__init__.py")
            and not (f.endswith(".py") and is_helper_module(f))
        ]
    return tuple(dict.fromkeys(out))


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
    properties: Sequence[str] = (),
    tickets: Sequence[Ticket] = (),
) -> TaskPacket:
    scenarios = scenarios or {}
    blocked = blocked_reads(ticket, tickets, feature_files)
    rules = [
        "You can read the whole project in the working directory: it holds everything "
        "the earlier tickets built.",
        f"Never edit {FEATURE_DIR}/*.feature files: they are the executable spec.",
        _inline_rule(spans, blocked),
        *_check_rules(task, ticket, stack, contract_files, scenarios),
    ]
    if ticket.files:
        rules.insert(
            1,
            "Write ONLY these paths; changes anywhere else are discarded: "
            + ", ".join(ticket.files)
            + ".",
        )
    if ticket.entrypoint:
        rules.append(ENTRYPOINT_RULE)
    if ticket.specialty is Specialty.TEST or any(_is_test_path(f) for f in ticket.files):
        rules.append(PRODUCTION_WIRING_RULE)
    if rule := property_rule(properties, stack):
        rules.append(rule)
    extra: dict[str, Any] = {"blocked_reads": list(blocked)} if blocked else {}
    return TaskPacket(
        task={
            "id": task.id,
            "ticket_id": ticket.id,
            "title": ticket.title,
            "description": ticket.description,
            "specialty": ticket.specialty.value,
            "difficulty": ticket.difficulty.value,
            "entrypoint": ticket.entrypoint,
            "tier": task.tier,
            "spec_uri": task.spec_uri,
            "branch": task.branch,
            "criterion_ids": list(task.criterion_ids),
            "depends_on": list(task.depends_on),
            "files": list(ticket.files),
            "reads": [
                r
                for r in ticket.reads
                if not _foreign_feature(r, feature_files) and r not in blocked
            ],
            "feature_files": list(feature_files),
            "rules": rules,
            "verify": {
                **verify_commands(stack),
                **scenario_verify(ticket.files, scenarios, stack, ticket.id),
            },
            **extra,
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


def upstream_defect_context(
    state: FactoryState, tickets: Sequence[Ticket]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(failure lines, files) a fixer pass gets for the upstream defects ticket
    sessions reported: each report under a header, and every file of each ticket
    owning a file a report names, for the pass's write fence. A session may not
    write another ticket's file, so the fixer is where such a bug gets fixed;
    ``((), ())`` when none was reported."""
    from foundry.manifest import upstream_defects

    reports = upstream_defects(state)
    if not reports:
        return (), ()
    files: list[str] = []
    for t in tickets:
        if any(_names(f, r) for f in t.files for r in reports):
            files.extend(t.files)
    lines = (
        "[upstream-defect] a dependent ticket's session found these bugs in a file it "
        "may not write, and kept its test as a real client calls it: confirm each still "
        "reproduces, then fix it in the named file (not by changing the caller's test)",
        *reports,
    )
    return lines, tuple(dict.fromkeys(files))


def _names(path: str, text: str) -> bool:
    """Whether ``text`` names ``path`` as a whole path (``app.py`` is not in
    ``webapp.py``)."""
    return re.search(rf"(?<![\w/.-]){re.escape(path.rstrip('/'))}(?![\w-])", text) is not None


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
