"""Interface revision: the planner's answer to a ticket left to a re-plan.

The engineering node leaves a ticket to a re-plan once the stub-signature guard
rejects its attempts :data:`REPLAN_AFTER` times: the signatures its planned
contracts give it are likely wrong. One small call revises only the contracts that
ticket implements; the revised records supersede the old ones by Seq, and an
``interface-revised`` note, minted above every earlier epoch of the run, supersedes
the ticket's skip / failure / re-plan notes so the next wave builds it again
(against stubs regenerated from the revised contracts). A reply that revises
nothing usable records ``interface-declined``. Either note answers the ticket for
the rest of the run.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from foundry.agents._common import as_text, emit_artifact, journal_entry, persist_prose
from foundry.agents._runtime import run_id_of
from foundry.agents.implementation.stubs import _top_names
from foundry.agents.implementation.tickets.constants import (
    INTERFACE_DECLINED_TRIAGE,
    INTERFACE_REVISED_TRIAGE,
    REPLAN_TRIAGE,
)
from foundry.agents.implementation.tickets.constants import NODE_ID as ENGINEERING_NODE_ID
from foundry.agents.plan_contracts import _py_normalize, check_contracts
from foundry.agents.planner.build import _contract_text
from foundry.agents.planner.readers import _newest_by_id, _objs, _prose, _str
from foundry.agents.planner.schema import CODE_CONTRACT_KINDS, NODE_ID, ROLE, TICKET_KIND
from foundry.proxy.model_provider_proxy import ProxyUnavailable, StructuredOutputError
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.gates import GateTriage
from foundry.schemas.implementation import StackDecision, Ticket
from foundry.state.seq import max_epoch

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime
    from foundry.state.factory_state import FactoryState

INTERFACE_SOP = (
    "You are the Interface Reviser. The implementation sessions of one ticket kept "
    "changing the public signatures its planned interface contracts give it, and the "
    "stub-signature guard rejected each attempt: the planned interface is likely "
    "wrong. Revise ONLY the contracts listed below so the ticket can implement them "
    "and the tickets that consume them can still call them: keep each contract's "
    "name, write its corrected signature in the plan's language, and keep every part "
    "the guard's findings do not show to be wrong. Return each contract you change "
    "under its name (none when the planned interface is right) and say why."
)

INTERFACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "contracts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "signature": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "signature", "description"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["contracts", "rationale"],
    "additionalProperties": False,
}

Call = Callable[[str, dict[str, Any], str], dict[str, Any]]


def revise_interfaces(
    rt: AgentRuntime,
    state: FactoryState,
    config: RunnableConfig,
    task_ids: Sequence[str],
    call: Call,
) -> dict[str, Any]:
    """One revision call per ticket of ``task_ids``: the revised contracts, and the
    note that answers each ticket."""
    tickets = _newest_by_id(state, rt, TICKET_KIND, Ticket)
    contracts = _newest_by_id(state, rt, "interface_contract", InterfaceContract)
    why = {
        t.run_against.removeprefix("task/"): t.explanation
        for t in state.get("gate_triage") or ()
        if t.name == REPLAN_TRIAGE and t.produced_by == ENGINEERING_NODE_ID
    }
    stack = state.get("stack")
    declared = _contracts_file_names(rt, state)
    # The revised note must outrank every note of the ticket so far (each note key
    # has its own epoch stream), and everything after it must outrank the note.
    run_id = run_id_of(config)
    rt.epochs.raise_floor(run_id, max_epoch(dict(state), run_id))
    marks = {tid: rt.seq(config, NODE_ID, f"interface:{tid}") for tid in task_ids}
    rt.epochs.raise_floor(run_id, max(m.emission_epoch for m in marks.values()))

    artifacts: list[Any] = []
    notes: list[GateTriage] = []
    lines: list[str] = []
    for tid in task_ids:
        ticket = tickets.get(tid)
        own = [contracts[n] for n in ticket.implements if n in contracts] if ticket else []
        revised: list[InterfaceContract] = []
        reason = "it implements no planned contract"
        if ticket is not None and own:
            names = {c.name for c in own}
            consumers = [
                {"id": t.id, "title": t.title, "consumes": [n for n in t.consumes if n in names]}
                for t in tickets.values()
                if t.id != tid and names.intersection(t.consumes)
            ]
            reply = _ask(rt, call, state, ticket, own, consumers, why.get(tid, ""))
            reason = _str(reply.get("rationale")).strip() if reply else "its reply did not parse"
            revised, problems = _revised(rt, config, own, reply, contracts, stack, declared)
            if reply and not revised:
                reason = "; ".join(problems) or "no contract changed: " + (
                    reason or "no reason given"
                )
        artifacts += [
            emit_artifact(c, "interface_contract", NODE_ID, c.seq, store=rt.cas) for c in revised
        ]
        name = INTERFACE_REVISED_TRIAGE if revised else INTERFACE_DECLINED_TRIAGE
        what = (
            "revised " + ", ".join(c.name for c in revised) + f": {reason}"
            if revised
            else f"left as planned ({reason}); the ticket stays unbuilt and unrestarted"
        )
        notes.append(
            GateTriage(
                name=name,
                run_against=f"task/{tid}",
                explanation=f"interface of {tid} {what}"[:2000],
                produced_by=NODE_ID,
                seq=marks[tid],
            )
        )
        lines.append(f"{tid}: {what}")
    return {
        "artifacts": artifacts,
        "gate_triage": notes,
        "agent_journal": [
            journal_entry(
                node_id=NODE_ID,
                role=ROLE,
                seq=rt.seq(config, NODE_ID, "journal:interface"),
                summary="Interface re-plan after the stub-signature guard rejected a "
                "ticket's attempts. " + " ".join(lines),
            )
        ],
    }


def _ask(
    rt: AgentRuntime,
    call: Call,
    state: FactoryState,
    ticket: Ticket,
    own: Sequence[InterfaceContract],
    consumers: Sequence[dict[str, Any]],
    findings: str,
) -> dict[str, Any] | None:
    brief = {
        "id": ticket.id,
        "title": ticket.title,
        "description": ticket.description,
        "files": list(ticket.files),
    }
    content = (
        f"REQUEST:\n{state.get('request', '')}\n\n"
        f"TICKET:\n{as_text(brief)}\n\n"
        f"GUARD FINDINGS:\n{findings or '(none recorded)'}\n\n"
        "ITS CONTRACTS (revise only these):\n"
        + "\n".join(
            f"- {c.name} [{c.kind or 'unspecified'}]: {c.signature}\n  {_prose(rt, c.spec_uri)}"
            for c in own
        )
        + "\n\nTICKETS CONSUMING THEM:\n"
        + (as_text(list(consumers)) if consumers else "(none)")
    )
    try:
        return call(INTERFACE_SOP, INTERFACE_SCHEMA, content)
    except ProxyUnavailable as exc:
        # Only an unparseable reply is an answer: a transport outage stays one.
        if not isinstance(exc.__cause__, StructuredOutputError):
            raise
        return None


def _revised(
    rt: AgentRuntime,
    config: RunnableConfig,
    own: Sequence[InterfaceContract],
    reply: dict[str, Any] | None,
    contracts: Mapping[str, InterfaceContract],
    stack: StackDecision | None,
    declared: set[str],
) -> tuple[list[InterfaceContract], list[str]]:
    """The contracts of ``own`` the reply re-signs (a new, non-empty signature), and
    why any it re-signs is refused. All are refused when the revised set adds a
    compile error the planned one did not have."""
    python = stack is None or stack.language == "python"
    by_name = {c.name: c for c in own}
    out: list[InterfaceContract] = []
    problems: list[str] = []
    for item in _objs((reply or {}).get("contracts")):
        old = by_name.pop(_str(item.get("name")).strip(), None)
        signature = _str(item.get("signature")).strip()
        if old is None or not signature or signature == old.signature.strip():
            continue
        # The skeleton subclasses a class the contracts file declares: a re-signed
        # method would conflict with the unchanged base.
        owner = old.name.rpartition(".")[0] or (old.name if old.kind == "class" else "")
        if owner and owner in declared:
            problems.append(
                f"{old.name}: its class {owner} is declared in the built contracts file, "
                "which a revision cannot change"
            )
            continue
        if old.name in declared:
            problems.append(
                f"{old.name} is declared in the built contracts file, which a revision "
                "cannot change"
            )
            continue
        if python and old.kind in CODE_CONTRACT_KINDS:
            try:
                ast.parse(_py_normalize(signature))
            except SyntaxError as exc:
                problems.append(f"{old.name}: its revised signature does not parse ({exc.msg})")
                continue
        # The planned prose (inputs, outputs, errors) stands unless the reply restates it.
        planned = _prose(rt, old.spec_uri).partition("\n")[2]
        restated = _str(item.get("description")).strip()
        text = _contract_text(
            {"kind": old.kind, "signature": signature, "description": restated or planned},
            # The planned prose already lists them.
            old.properties if restated else (),
        )
        out.append(
            old.model_copy(
                update={
                    "signature": signature,
                    "spec_uri": persist_prose(text, store=rt.cas),
                    "version": old.version + 1,
                    "seq": rt.seq(config, NODE_ID, f"artifact:interface:{old.name}"),
                }
            )
        )
    if new := _new_compile_findings(contracts, out, stack):
        return [], [*problems, "the revised contracts do not compile: " + " | ".join(new)]
    return out, problems


def _new_compile_findings(
    contracts: Mapping[str, InterfaceContract],
    revised: Sequence[InterfaceContract],
    stack: StackDecision | None,
) -> list[str]:
    """Compile findings of the newest contract set with ``revised`` substituted that
    the planned set does not have."""
    if not any(c.kind in CODE_CONTRACT_KINDS for c in revised):
        return []
    language = stack.language if stack is not None else "python"
    strict = stack is not None and "--strict" in stack.typecheck.split()
    swapped = {**contracts, **{c.name: c for c in revised}}

    def code(cs: Mapping[str, InterfaceContract]) -> list[tuple[str, str]]:
        return [(c.name, c.signature) for c in cs.values() if c.kind in CODE_CONTRACT_KINDS]

    before = set(check_contracts(code(contracts), language, strict=strict).findings)
    after = check_contracts(code(swapped), language, strict=strict).findings
    return [f for f in after if f not in before]


def _contracts_file_names(rt: AgentRuntime, state: FactoryState) -> set[str]:
    """The top-level names of the built contracts file(s), empty before it is built."""
    if rt.cas is None:
        return set()
    from foundry.graph.gate_wiring import MaterializationError, contract_paths, integrated_files

    try:
        paths = [p for p in contract_paths(state, rt.cas.resolve) if p.endswith(".py")]
        if not paths:
            return set()
        tree = integrated_files(state, resolve=rt.cas.resolve, resolve_blob=rt.cas.resolve_blob)
    except (MaterializationError, KeyError, ValueError):
        return set()
    return {n for p in paths if isinstance(t := tree.get(p), str) for n in _top_names(t)[0]}
