"""Domain packs — the extension seam (domains.md §4).

"Correctness is not one shape." A **domain pack** teaches the general factory how
a domain (regulated / ML / frontend) grounds correctness, WITHOUT editing core. A
pack implements the :class:`DomainPackContract` — seven typed hooks each with a
safe no-op default (:class:`BaseDomainPack`) — and is added via
``PackRegistry.register(pack)``. The registry routes a request to the engaged
packs by capability and composes their hook effects **strictest-wins**
(``tighten_only``: a pack may make policy stricter, never relax integrity).

This is the OPEN/CLOSED seam: adding a pack is one ``register(...)`` call — no edit
to :mod:`foundry.domains.contract`, :mod:`foundry.domains.registry`, or the graph.
The three packs (T-M5-05/06/07) implement this contract; T-M5-08 wires the
registry into the graph.
"""

from foundry.domains.contract import (
    ApproverPolicy,
    BaseDomainPack,
    ComputePolicy,
    DomainPackContract,
    EgressPolicy,
    ModeTierPolicy,
    PerceptualHITLPolicy,
    StateInputPolicy,
    TightenError,
    VerdictClassPolicy,
    request_capabilities,
)
from foundry.domains.registry import ComposedPolicy, PackRegistry

__all__ = [
    "ApproverPolicy",
    "BaseDomainPack",
    "ComposedPolicy",
    "ComputePolicy",
    "DomainPackContract",
    "EgressPolicy",
    "ModeTierPolicy",
    "PackRegistry",
    "PerceptualHITLPolicy",
    "StateInputPolicy",
    "TightenError",
    "VerdictClassPolicy",
    "request_capabilities",
]
