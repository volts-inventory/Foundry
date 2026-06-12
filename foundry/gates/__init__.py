"""Gate trust seam — signing/attestation + the write verifier. (gov §1c, arch §3)

Dev-grade trust root (implementation-plan.md §0.3): real, correct logic with
HMAC keys standing in for the hardware/OIDC production roots.
"""

from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner

__all__ = ["DevGateWriteVerifier", "ZoneBSigner"]
