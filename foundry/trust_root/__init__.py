"""Supply-chain trust root: the hermetic SLSA-L3 trusted builder. (gov §4)

The :class:`TrustedBuilder` performs a HERMETIC build of a subject from a pinned
:class:`~foundry.schemas.operations.BuildRequest`: pinned-inputs-only (every
material content-addressed, un-pinned inputs refused fail-closed), no network
(only digest-addressed inputs enter — there is no egress seam), and
reproducible (the subject digest is a pure function of the pinned inputs). It
produces the authoritative SBOM, the input-bound SLSA provenance predicate, and
a build verdict whose attestation is bound into that provenance — the evidence
the deploy-time admission controller (T-M4-07) re-verifies.

DEV-GRADE ROOT (implementation-plan.md §0.3): the logic + interfaces are real;
the cryptographic root (builder identity, HMAC signer, hermetic isolation) is a
local-dev stand-in for a real hardware/OIDC root + air-gapped builder.
"""

from foundry.trust_root.trusted_builder import (
    BuildOutput,
    NonHermeticBuildError,
    TrustedBuilder,
    is_pinned,
)

__all__ = [
    "BuildOutput",
    "NonHermeticBuildError",
    "TrustedBuilder",
    "is_pinned",
]
