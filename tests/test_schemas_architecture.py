"""Conformance tests for the Layer 2 (Architecture & Design) artifact schemas.

Contract under test (docs/agents.md Layer 2; conventions from
foundry/schemas/base.py): every model is a FrozenModel (frozen, extra="forbid"),
carries `seq: Seq`, has a content_identity() stable across construction order
and sensitive to field changes. Large bodies are `*_uri` CAS handles (DS9).
Versioned models supersede, never mutate. OwnershipMap yields a deterministic,
resume-stable merge order.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.schemas.architecture import (
    ADR,
    InterfaceContract,
    OwnershipMap,
    SystemDesign,
)
from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=3, node_id="architect", local=0)

# (model, valid kwargs, field to change, changed value) — one row per model.
CASES: list[tuple[type[FrozenModel], dict, str, object]] = [
    (
        SystemDesign,
        {
            "title": "Todo service architecture",
            "design_uri": "cas://design/1",
            "components": ("api", "store"),
            "adr_ids": ("ADR-1",),
            "interface_ids": ("TodoStore.add",),
            "seq": SEQ,
        },
        "title",
        "Notes service architecture",
    ),
    (
        ADR,
        {
            "id": "ADR-1",
            "title": "Use Postgres for persistence",
            "decision": "Postgres over SQLite for concurrency",
            "body_uri": "cas://adr/1",
            "rejected_alternatives": ("SQLite", "in-memory dict"),
            "status": "accepted",
            "seq": SEQ,
        },
        "decision",
        "SQLite for simplicity",
    ),
    (
        InterfaceContract,
        {
            "name": "TodoStore.add",
            "spec_uri": "cas://iface/1",
            "consumers": ("api",),
            "seq": SEQ,
        },
        "name",
        "TodoStore.remove",
    ),
    (
        OwnershipMap,
        {
            "owners": {"src/api.py": "T-1", "src/store.py": "T-2"},
            "seq": SEQ,
        },
        "version",
        2,
    ),
]

IDS = [cls.__name__ for cls, *_ in CASES]


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_constructs_and_is_frozen(cls, kwargs, field, changed):
    m = cls(**kwargs)
    assert m.seq == SEQ
    with pytest.raises(ValidationError):
        setattr(m, field, changed)


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_extra_fields_rejected(cls, kwargs, field, changed):
    with pytest.raises(ValidationError):
        cls(**kwargs, not_a_declared_field="boom")


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_content_identity_stable_and_field_sensitive(cls, kwargs, field, changed):
    a = cls(**kwargs)
    b = cls(**dict(reversed(list(kwargs.items()))))  # construction order irrelevant
    assert a.content_identity() == b.content_identity()
    assert a.content_identity() == a.content_identity()  # deterministic
    mutated = a.model_copy(update={field: changed})
    assert mutated.content_identity() != a.content_identity()


# ---------------------------------------------------------------------------
# Spot checks on key invariants
# ---------------------------------------------------------------------------


def test_adr_carries_rejected_alternatives():
    """The Architect SOP writes ADRs WITH rejected alternatives (Layer 2)."""
    adr = ADR(
        id="ADR-2",
        title="Event sourcing",
        decision="adopt event sourcing",
        body_uri="cas://adr/2",
        rejected_alternatives=("CRUD", "CQRS-without-ES"),
        seq=SEQ,
    )
    assert adr.rejected_alternatives == ("CRUD", "CQRS-without-ES")
    assert adr.status == "accepted"  # default


def test_versioned_supersession_round_trip():
    """A model supersedes a prior version; identity changes, lineage recorded."""
    v1 = SystemDesign(title="v1", design_uri="cas://design/1", seq=SEQ)
    v2 = SystemDesign(
        title="v2",
        design_uri="cas://design/2",
        version=2,
        supersedes=v1.design_uri,
        seq=SEQ,
    )
    assert v2.version == 2
    assert v2.supersedes == "cas://design/1"
    assert v2.content_identity() != v1.content_identity()
    # An ADR supersession round-trips the same way.
    a1 = ADR(id="ADR-1", title="t", decision="d", body_uri="cas://adr/1", seq=SEQ)
    a2 = a1.model_copy(
        update={"version": 2, "supersedes": a1.body_uri, "body_uri": "cas://adr/2"}
    )
    assert a2.supersedes == "cas://adr/1" and a2.version == 2
    assert a2.content_identity() != a1.content_identity()


def test_ownership_map_merge_order_is_deterministic():
    """Merge order is a pure function of the mapping — insertion order and
    dict key order do not affect it (resume re-derives the same order)."""
    a = OwnershipMap(
        owners={"src/z.py": "T-2", "src/a.py": "T-1", "src/b.py": "T-1"}, seq=SEQ
    )
    b = OwnershipMap(
        owners={"src/b.py": "T-1", "src/z.py": "T-2", "src/a.py": "T-1"}, seq=SEQ
    )
    # ordered by (owner, unit): T-1 units first (a, b), then T-2 (z)
    expected = ("src/a.py", "src/b.py", "src/z.py")
    assert a.merge_order() == expected
    assert b.merge_order() == expected  # independent of construction order
    # content_identity also order-independent (canonical dump)
    assert a.content_identity() == b.content_identity()


def test_ownership_map_owner_lookup():
    om = OwnershipMap(owners={"src/api.py": "T-1", "TodoStore.add": "T-2"}, seq=SEQ)
    assert om.owner_of("src/api.py") == "T-1"
    assert om.owner_of("TodoStore.add") == "T-2"
    assert om.owner_of("missing.py") is None


def test_interface_contract_supersession():
    v1 = InterfaceContract(name="TodoStore.add", spec_uri="cas://iface/1", seq=SEQ)
    v2 = v1.model_copy(
        update={"version": 2, "supersedes": v1.spec_uri, "spec_uri": "cas://iface/2"}
    )
    assert v2.version == 2 and v2.supersedes == "cas://iface/1"
    assert v2.content_identity() != v1.content_identity()
