"""The zone-B signing scheme — the symmetric HMAC root.

The property that matters: ``merge_gates`` / admission only admit a verdict
carrying a valid tag, and the wire format is dispatched on the scheme name.
"""

from __future__ import annotations

from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.gates.signing import (
    HmacScheme,
    parse_signature,
    signing_schemes_from_env,
)
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq


def _det_gate(name: str = "build", tree: str = "a" * 40) -> GateResult:
    return GateResult(
        name=name, status=GateStatus.PASS, decision_deterministic=True,
        execution_deterministic=True, verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        run_against=tree, seq=Seq("r1", 1, 0, "build_gate", 0),
    )


# --- HMAC scheme wire format --------------------------------------------------- #

def test_hmac_wire_format() -> None:
    s = HmacScheme(b"k", "kid")
    tag = s.tag(b"payload")
    assert s.verify_tag(b"payload", tag) is True
    assert s.verify_tag(b"other", tag) is False
    assert s.name == "hmac256"


def test_signer_verifier_hmac_roundtrip_via_default_ctor() -> None:
    records = GlobalRecords()
    signer = ZoneBSigner(b"shared-key", "kid")
    verifier = DevGateWriteVerifier(b"shared-key", "kid", records)
    signed = signer.with_attestation(_det_gate())
    assert verifier.verify_attestation(signed) is True
    # Wrong key cannot verify.
    assert DevGateWriteVerifier(b"other-key", "kid", records).verify_attestation(signed) is False


def test_signer_verifier_via_from_scheme() -> None:
    records = GlobalRecords()
    signer = ZoneBSigner.from_scheme(HmacScheme(b"shared", "prod-kid"))
    verifier = DevGateWriteVerifier.from_scheme(HmacScheme(b"shared", "prod-kid"), records)
    signed = signer.with_attestation(_det_gate())
    assert signed.runner_attestation.startswith("hmac256:prod-kid:")
    assert verifier.verify_attestation(signed) is True
    # Wrong key_id → reject.
    wrong = DevGateWriteVerifier.from_scheme(HmacScheme(b"shared", "other-kid"), records)
    assert wrong.verify_attestation(signed) is False


def test_parse_signature() -> None:
    assert parse_signature("hmac256:kid:abcd") == ("hmac256", "kid", "abcd")
    assert parse_signature("malformed") is None
    assert parse_signature("") is None
    assert parse_signature(None) is None


def test_signing_schemes_from_env_shared_hmac() -> None:
    signer, verifier = signing_schemes_from_env(dev_key=b"dev", dev_key_id="dev")
    assert signer.name == "hmac256" and verifier.name == "hmac256"
    assert signer is verifier  # symmetric: same object both sides
