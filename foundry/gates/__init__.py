"""Gate trust seam + deterministic gate implementations. (gov §1/§1c, arch §3)

Dev-grade trust root (implementation-plan.md §0.3): real, correct logic with
HMAC keys standing in for the hardware/OIDC production roots. The gate impls
carry real machine grounding (subprocess exits, pure-python scans, config
re-derivation); where an external scanner is absent a labeled dev-grade
fallback runs, fail-closed (missing grounding ⇒ ERROR, never PASS).
"""

from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.gates.audit import AuditGate
from foundry.gates.build import BuildGate
from foundry.gates.e2e import E2EGate
from foundry.gates.runner import (
    GateImpl,
    GroundingUnavailable,
    TwoZoneGateRunner,
    ZoneAObservation,
    observe,
)
from foundry.gates.security import SecurityGate
from foundry.gates.spec import SpecGate
from foundry.gates.supply_chain import SupplyChainGate
from foundry.gates.test import TestGate

__all__ = [
    "AuditGate",
    "BuildGate",
    "DevGateWriteVerifier",
    "E2EGate",
    "GateImpl",
    "GroundingUnavailable",
    "SecurityGate",
    "SpecGate",
    "SupplyChainGate",
    "TestGate",
    "TwoZoneGateRunner",
    "ZoneAObservation",
    "ZoneBSigner",
    "observe",
]
