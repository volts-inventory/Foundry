"""The typed skeleton: stubbed contract files and the rules that keep them."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from foundry.agents._runtime import AgentRuntime
from foundry.agents.implementation.stubs import python_skeleton, stub_signatures
from foundry.agents.implementation.tickets.constants import (
    _FEATURE_DIFF_KIND,
    NODE_ID,
    SKELETON_TRIAGE,
)
from foundry.agents.implementation.tickets.contracts import contract_members
from foundry.canonical import content_digest
from foundry.execution.adapter_contract import AdapterResult
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateTriage
from foundry.schemas.implementation import SKELETON_TASK_ID, FeatureDiff, Ticket
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.graph.gate_wiring import BlobResolver
    from foundry.state.factory_state import FactoryState

    ArtifactResolver = Callable[[Artifact], Any]


def skeleton_files(
    state: FactoryState, resolve: ArtifactResolver, resolve_blob: BlobResolver | None
) -> dict[str, str]:
    """The typed stub files the skeleton diff wrote (path -> stub text)."""
    from foundry.graph.gate_wiring import MaterializationError, _diff_ops, _resolve_typed

    handles = [
        a
        for a in state.get("artifacts") or ()
        if a.kind == _FEATURE_DIFF_KIND and a.task_id == SKELETON_TASK_ID
    ]
    if not handles or resolve_blob is None:
        return {}
    try:
        diff = _resolve_typed(
            max(handles, key=lambda a: a.seq), resolve, FeatureDiff, _FEATURE_DIFF_KIND
        )
        return dict(_diff_ops(diff, resolve_blob).writes)
    except MaterializationError:
        return {}


def revised_stubs(
    tickets: Sequence[Ticket],
    contracts: Mapping[str, InterfaceContract],
    contract_texts: Mapping[str, str],
) -> dict[str, str]:
    """The typed stubs of ``tickets`` regenerated from the newest ``contracts``: a
    ticket whose interface the planner revised is held to the revised signatures,
    not the skeleton's."""
    if not contract_texts:
        return {}
    sk = python_skeleton(list(contracts.values()), tickets, contract_texts)
    owned = {f for t in tickets for f in t.files}
    return {p: text for p, text in sk.files.items() if p in owned}


def restub_revised(
    wave: Sequence[Ticket],
    revised: Collection[str],
    restarts: Collection[str],
    skeleton: dict[str, str],
    contracts: Mapping[str, InterfaceContract],
    contract_texts: Mapping[str, str],
    reset: tuple[dict[str, str], tuple[str, ...]],
) -> tuple[dict[str, dict[str, str]], tuple[dict[str, str], tuple[str, ...]]]:
    """Hold every build of a ticket whose interface the planner revised to the
    revised stubs: they replace its plan-time stubs in ``skeleton`` (the guard's
    reference), and a restart's reset puts them back instead of the plan-time ones
    the tree before the ticket holds. Returns the revised stubs per task id and the
    reset."""
    from foundry.agents.implementation.tickets.tasks import task_id_for_ticket

    restubbed: dict[str, dict[str, str]] = {}
    if not skeleton:
        return restubbed, reset
    for t in wave:
        tid = task_id_for_ticket(t.id)
        if tid not in revised:
            continue
        fresh = revised_stubs([t], contracts, contract_texts)
        restubbed[tid] = {p: text for p, text in fresh.items() if p in skeleton}
        skeleton.update(restubbed[tid])
    back = {
        p: text for tid, files in restubbed.items() if tid in restarts for p, text in files.items()
    }
    if back:
        reset = ({**reset[0], **back}, tuple(p for p in reset[1] if p not in back))
    return restubbed, reset


def stub_rules(
    ticket: Ticket, skeleton: Mapping[str, str], contracts: Mapping[str, InterfaceContract]
) -> list[str]:
    """The packet rules for a ticket that owns typed stubs or calls a contract the
    skeleton implements: fill the bodies, keep the signatures, construct the
    ``<Class>Impl`` by its name."""
    rules: list[str] = []
    own = [p for p in ticket.files if skeleton.get(p)]
    if own:
        rules.append(
            f"TYPED STUBS: {', '.join(own)} already exist as typed stubs generated from "
            "the planned contracts: each planned function and method with its exact "
            "signature and a `raise NotImplementedError` body (a contract class is "
            "implemented by the concrete subclass of it the stub declares). Fill in "
            "every body and add an `__init__`, private helpers or extra methods as you "
            "need; never rename or remove a stubbed class, function or method, and "
            "never change its parameters or return annotation (checked: a changed "
            "public signature fails the attempt)."
        )
    impls: dict[str, str] = {}  # contract class -> "XImpl in path"
    for path, text in skeleton.items():
        if path in ticket.files:
            continue
        for name, sig in (stub_signatures(text) or {}).items():
            base = sig.removeprefix("class ").strip()
            if sig.startswith("class ") and base.isidentifier():
                impls[base] = f"{name} in {path}"
    wanted = _consumed_names(ticket, contracts)
    named = [
        f"{cls} is implemented by {where}" for cls, where in sorted(impls.items()) if cls in wanted
    ]
    if named:
        rules.append(
            "IMPLEMENTATIONS: " + "; ".join(named) + ". Construct those classes "
            "by these names and annotate with the contract types."
        )
    return rules


def _consumed_names(ticket: Ticket, contracts: Mapping[str, InterfaceContract]) -> set[str]:
    """Every name a ticket's consumed contracts declare (a method contract's class too)."""
    return {
        n
        for c in ticket.consumes
        for n in (
            c,
            c.rpartition(".")[0],
            *(contract_members(contracts[c].signature) if c in contracts else ()),
        )
    }


def shared_stub_names(
    ticket: Ticket,
    tickets: Sequence[Ticket],
    stubs: Mapping[str, str],
    contracts: Mapping[str, InterfaceContract],
    tree: Mapping[str, str | None],
) -> frozenset[str]:
    """The top-level names of ``ticket``'s typed ``stubs`` some other ticket depends
    on: a contract it consumes declares the name (or the base a stubbed class
    implements, which its IMPLEMENTATIONS rule names), or its plan text or its files
    in ``tree`` mention it."""
    declared: dict[str, set[str]] = {}  # top-level name -> its bases (a class)
    for text in stubs.values():
        for name, sig in (stub_signatures(text) or {}).items():
            if "." not in name:
                parts = sig.removeprefix("class ").split(",") if sig.startswith("class ") else []
                declared[name] = {b.strip() for b in parts if b.strip()}
    out: set[str] = set()
    for other in tickets:
        if other.id == ticket.id:
            continue
        wanted = _consumed_names(other, contracts)
        text = "\n".join(
            [
                other.title,
                other.description,
                *(t for f in other.files if f not in stubs and isinstance(t := tree.get(f), str)),
            ]
        )
        for name, bases in declared.items():
            if name in wanted or bases & wanted or re.search(rf"\b{re.escape(name)}\b", text):
                out.add(name)
    return frozenset(out)


def _skeleton_diff(
    rt: AgentRuntime,
    config: RunnableConfig,
    tickets: Sequence[Ticket],
    contracts: Mapping[str, InterfaceContract],
    contract_files: Sequence[str],
    overlay: Mapping[str, str | None],
    built: Sequence[AdapterResult],
    blobs: BlobResolver,
    base: str,
) -> tuple[dict[str, list[Any]], tuple[str, ...]]:
    """The skeleton FeatureDiff on top of ``overlay`` plus this wave's ``built``
    diffs (the contracts file as just built), with the generator's concerns."""
    from foundry.graph.gate_wiring import MaterializationError, _apply_hunks, _diff_ops

    tree = dict(overlay)
    try:
        for got in built:
            probe = FeatureDiff(
                diff_uri=got.diff_uri,
                task_id="probe",
                branch="task/probe",
                base_tree_sha="",
                seq=Seq("scope", 0, 0, NODE_ID, 0),
            )
            ops = _diff_ops(probe, blobs)
            tree.update(ops.writes)
            tree.update(dict.fromkeys(ops.deletes))
            for rel, hunks in ops.patches.items():
                if isinstance(old := tree.get(rel), str):
                    tree[rel] = _apply_hunks(old, hunks, diff_uri=got.diff_uri, rel=rel)
    except MaterializationError as exc:
        return {}, (f"No typed skeleton: the contracts could not be read ({exc}).",)
    texts = {f: t for f in contract_files if isinstance(t := tree.get(f), str)}
    sk = python_skeleton(
        list(contracts.values()),
        tickets,
        texts,
        exists=(p for p, t in tree.items() if t is not None),
    )
    if not sk.files or rt.cas is None:
        return {}, tuple(sk.concerns)
    blob = json.dumps(
        {"kind": "full-file-snapshot", "files": sk.files}, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    key = content_digest({"skeleton": sk.files})
    diff = FeatureDiff(
        diff_uri=rt.cas.put_blob(blob),
        task_id=SKELETON_TASK_ID,
        branch=f"task/{SKELETON_TASK_ID}",
        base_tree_sha=base,
        seq=rt.seq(config, NODE_ID, f"diff:{SKELETON_TASK_ID}"),
    )
    artifact = Artifact(
        kind=_FEATURE_DIFF_KIND,
        uri=f"cas://feature_diff/{SKELETON_TASK_ID}-{key}",
        produced_by=NODE_ID,
        task_id=SKELETON_TASK_ID,
        seq=rt.seq(config, NODE_ID, f"artifact:{SKELETON_TASK_ID}"),
    )
    rt.cas.put_body_at(artifact.uri, diff)
    note = GateTriage(
        name=SKELETON_TRIAGE,
        run_against=f"task/{SKELETON_TASK_ID}",
        explanation=(
            "typed stubs: "
            + ", ".join(f"{p} ({sk.owners[p]})" for p in sorted(sk.files))
            + "".join(f"; {c}" for c in sk.concerns)
        )[:2000],
        produced_by=NODE_ID,
        seq=rt.seq(config, NODE_ID, SKELETON_TRIAGE),
    )
    return {"artifacts": [artifact], "gate_triage": [note]}, tuple(sk.concerns)
