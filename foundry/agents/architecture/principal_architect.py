"""Principal Architect node (agents.md Layer 2 — T1).

The most consequential role: everything downstream inherits its decisions. The
node factory follows the shared convention (``_runtime.py`` AR3):

    make_principal_architect_node(rt) -> NodeFn

SOP (agents.md Layer 2):

1. **Retrieve** relevant existing code through the context retriever
   (``foundry.context.SymbolIndex``) — symbol index + one-hop call/def-graph
   neighbours (impact analysis), NOT ad-hoc file reads. An empty retrieval is a
   typed ``NeedContext`` escape rather than proceeding over a partial view.
2. Call ``rt.proxy.complete(..., purpose="ideation", schema=...)`` (the single
   model egress, LEARNINGS L6) to produce the architecture: a SystemDesign, the
   ADRs it rests on (each WITH rejected alternatives), the interface contracts,
   the ownership map, and the pinned constraints.
3. Emit the SystemDesign / ADRs / InterfaceContracts / OwnershipMap as
   content-addressed :class:`Artifact` handles on the ``artifacts`` channel.
4. Emit the :class:`PinnedConstraint` records on the ``pinned_constraints``
   channel — these flow downstream VERBATIM into adapter task packets
   (architecture.md §8); the ``merge_constraints`` reducer keeps them by id +
   supersession.

This node emits NO ``GateResult`` (LEARNINGS L5: agents triage, never bless) and
imports NO Anthropic SDK (LEARNINGS L6: all egress via ``rt.proxy``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.canonical import content_digest
from foundry.context import SymbolIndex
from foundry.schemas.architecture import (
    ADR,
    ConstraintScope,
    InterfaceContract,
    OwnershipMap,
    PinnedConstraint,
    SystemDesign,
)
from foundry.schemas.base import Artifact

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.context.retriever import Span
    from foundry.state.factory_state import FactoryState
    from foundry.state.seq import Seq

NODE_ID = "principal_architect"
TIER = "T1"  # most consequential judgment never auto-downgrades (agents.md)

# Default symbols the architect probes when state carries none yet — broad,
# recall-oriented; the retriever's call/def neighbours expand the impact set.
_DEFAULT_QUERY_SYMBOLS: tuple[str, ...] = ()
_RETRIEVAL_K = 24

# The structured-output channel the proxy constrains the model to. The model
# returns the *narratives* (titles, decisions, constraint text); the node mints
# the content-addressed body handles + Seqs so identity is process-stable (L7).
_DESIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "design_body": {"type": "string"},
        "components": {"type": "array", "items": {"type": "string"}},
        "adrs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "decision": {"type": "string"},
                    "body": {"type": "string"},
                    "rejected_alternatives": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["id", "title", "decision", "body"],
                "additionalProperties": False,
            },
        },
        "interfaces": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "spec": {"type": "string"},
                    "consumers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "spec"],
                "additionalProperties": False,
            },
        },
        "ownership": {"type": "object", "additionalProperties": {"type": "string"}},
        "pinned_constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "scope": {"type": "string", "enum": ["global", "local"]},
                    "source": {"type": "string"},
                },
                "required": ["id", "text", "scope", "source"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "design_body", "adrs", "interfaces", "ownership"],
    "additionalProperties": False,
}

_SYSTEM_SOP = (
    "You are the Principal Architect (Layer 2). From the PRD and the retrieved "
    "code spans, choose the architecture and component decomposition; write ADRs "
    "WITH rejected alternatives; define interface/API contracts, the data model, "
    "and file/interface ownership (used for deterministic merge order); identify "
    "cross-cutting concerns and emit them as pinned constraints injected verbatim "
    "downstream. Respond in the constrained JSON schema."
)


def _cas(prefix: str, body: str) -> str:
    """Content-addressed handle for an out-of-band body (DS9 / LEARNINGS L4).

    Bodies never live in state channels; only this handle does. The digest is
    process-stable (LEARNINGS L7: ``content_digest``, never builtin ``hash()``).
    """
    return f"cas://{prefix}/{content_digest(body)}"


def _query_symbols(state: FactoryState) -> tuple[str, ...]:
    """Symbols to retrieve impact for. ``required_capabilities`` seed the probe
    (recall-oriented); the retriever expands to one-hop call/def neighbours."""
    caps = tuple(state.get("required_capabilities") or ())
    return caps or _DEFAULT_QUERY_SYMBOLS


def _retrieve_spans(index: SymbolIndex | None, state: FactoryState) -> tuple[Span, ...]:
    """Retrieve code spans through the context retriever (agents.md Layer 2 SOP).

    The architect retrieves through the symbol-index + call/def-graph contract,
    never by reading files ad hoc. No index (greenfield, nothing to retrieve)
    and no query symbols both yield an empty span set — a legitimate greenfield
    view, distinct from a retrieval FAILURE over a populated index, which the
    retriever surfaces as ``NeedContext`` via ``retrieve_or_need_context``.
    """
    symbols = _query_symbols(state)
    if index is None or not symbols:
        return ()
    return tuple(index.retrieve_or_need_context(symbols, _RETRIEVAL_K))


def _seqs(rt: AgentRuntime, config: RunnableConfig) -> dict[str, Seq]:
    """One independent epoch stream per emitted artifact key (AR2)."""
    return {
        "design": rt.seq(config, NODE_ID, "artifact:system_design"),
        "adr": rt.seq(config, NODE_ID, "artifact:adrs"),
        "interface": rt.seq(config, NODE_ID, "artifact:interfaces"),
        "ownership": rt.seq(config, NODE_ID, "artifact:ownership_map"),
        "constraint": rt.seq(config, NODE_ID, "pinned_constraints"),
    }


def make_principal_architect_node(
    rt: AgentRuntime, index: SymbolIndex | None = None
) -> NodeFn:
    """Build the Principal Architect node (T1).

    ``index`` is the context retriever the architect probes for existing code
    spans; ``None`` (greenfield, no source tree yet) yields an empty retrieval.
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        spans = _retrieve_spans(index, state)
        retrieved = [
            {"file": s.file, "lines": [s.line, s.end_line], "symbol": s.symbol}
            for s in spans
        ]
        user_content = {
            "prd": state.get("request", ""),
            "required_capabilities": list(state.get("required_capabilities") or ()),
            "retrieved_spans": retrieved,
        }
        completion = rt.proxy.complete(
            NODE_ID,
            TIER,
            _SYSTEM_SOP,
            [{"role": "user", "content": _as_text(user_content)}],
            purpose="ideation",
            schema=_DESIGN_SCHEMA,
        )
        payload = completion.parsed or {}
        seqs = _seqs(rt, config)

        adrs = _build_adrs(payload.get("adrs", []), seqs["adr"])
        interfaces = _build_interfaces(payload.get("interfaces", []), seqs["interface"])
        ownership = OwnershipMap(
            owners={str(k): str(v) for k, v in (payload.get("ownership") or {}).items()},
            seq=seqs["ownership"],
        )
        design = SystemDesign(
            title=str(payload.get("title", "")),
            design_uri=_cas("design", str(payload.get("design_body", ""))),
            components=tuple(str(c) for c in payload.get("components", ())),
            adr_ids=tuple(a.id for a in adrs),
            interface_ids=tuple(i.name for i in interfaces),
            seq=seqs["design"],
        )
        constraints = _build_constraints(
            payload.get("pinned_constraints", []), seqs["constraint"]
        )

        ownership_uri = _cas("ownership", _as_text(ownership.owners))
        artifacts = [
            _artifact("system_design", design.design_uri, seqs["design"]),
            *(_artifact("adr", a.body_uri, a.seq) for a in adrs),
            *(_artifact("interface_contract", i.spec_uri, i.seq) for i in interfaces),
            _artifact("ownership_map", ownership_uri, seqs["ownership"]),
        ]

        # pinned_constraints flow to their own channel (merge_constraints reducer:
        # union by id, latest by seq) so they reach downstream task packets
        # VERBATIM (architecture.md §8). Artifacts ride the artifacts channel.
        return {
            "artifacts": artifacts,
            "pinned_constraints": constraints,
        }

    return _node


def _artifact(kind: str, uri: str, seq: Seq) -> Artifact:
    return Artifact(kind=kind, uri=uri, produced_by=NODE_ID, seq=seq)


def _build_adrs(raw: Any, seq: Seq) -> list[ADR]:
    out: list[ADR] = []
    for i, item in enumerate(raw or ()):
        out.append(
            ADR(
                id=str(item.get("id", f"ADR-{i:03d}")),
                title=str(item.get("title", "")),
                decision=str(item.get("decision", "")),
                body_uri=_cas("adr", str(item.get("body", ""))),
                rejected_alternatives=tuple(
                    str(a) for a in item.get("rejected_alternatives", ())
                ),
                seq=seq._replace(local=i),
            )
        )
    return out


def _build_interfaces(raw: Any, seq: Seq) -> list[InterfaceContract]:
    out: list[InterfaceContract] = []
    for i, item in enumerate(raw or ()):
        out.append(
            InterfaceContract(
                name=str(item.get("name", f"iface-{i}")),
                spec_uri=_cas("interface", str(item.get("spec", ""))),
                consumers=tuple(str(c) for c in item.get("consumers", ())),
                seq=seq._replace(local=i),
            )
        )
    return out


def _build_constraints(raw: Any, seq: Seq) -> list[PinnedConstraint]:
    out: list[PinnedConstraint] = []
    for i, item in enumerate(raw or ()):
        scope = ConstraintScope(str(item.get("scope", "local")))
        out.append(
            PinnedConstraint(
                id=str(item.get("id", f"PC-{i:03d}")),
                text=str(item.get("text", "")),
                scope=scope,
                source=str(item.get("source", NODE_ID)),
                seq=seq._replace(local=i),
            )
        )
    return out


def _as_text(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
