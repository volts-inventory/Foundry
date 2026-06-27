"""The pluggable signing schemes — the dev HMAC root and the production Ed25519
(asymmetric) root. The load-bearing production property: the VERIFIER cannot forge
a signature the way it can under shared-secret HMAC.
"""

from __future__ import annotations

import pytest

from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.gates.signing import (
    Ed25519Scheme,
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


# --- HMAC scheme stays byte-identical to the historical wire format ----------- #

def test_hmac_wire_format_unchanged() -> None:
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


# --- Ed25519: the asymmetric production root ---------------------------------- #

def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def test_ed25519_sign_then_verify_with_public_key_only() -> None:
    priv, pub = _keypair()
    signer_scheme = Ed25519Scheme("kid", private_key=priv)
    verifier_scheme = Ed25519Scheme("kid", public_key=pub)   # NO private key
    tag = signer_scheme.tag(b"msg")
    assert verifier_scheme.verify_tag(b"msg", tag) is True
    assert verifier_scheme.verify_tag(b"tampered", tag) is False


def test_ed25519_verifier_cannot_forge() -> None:
    """The whole point vs HMAC: a verify-only scheme (public key) CANNOT sign."""
    _priv, pub = _keypair()
    verifier_scheme = Ed25519Scheme("kid", public_key=pub)
    with pytest.raises(RuntimeError, match="VERIFY-ONLY"):
        verifier_scheme.tag(b"msg")


def test_ed25519_end_to_end_through_signer_and_verifier() -> None:
    records = GlobalRecords()
    priv, pub = _keypair()
    signer = ZoneBSigner.from_scheme(Ed25519Scheme("prod-kid", private_key=priv))
    verifier = DevGateWriteVerifier.from_scheme(Ed25519Scheme("prod-kid", public_key=pub), records)
    signed = signer.with_attestation(_det_gate())
    assert signed.runner_attestation.startswith("ed25519:prod-kid:")
    assert verifier.verify_attestation(signed) is True
    # A verifier holding a DIFFERENT public key rejects it (no shared secret to leak).
    _, other_pub = _keypair()
    other_scheme = Ed25519Scheme("prod-kid", public_key=other_pub)
    other = DevGateWriteVerifier.from_scheme(other_scheme, records)
    assert other.verify_attestation(signed) is False


def test_verifier_rejects_cross_scheme_or_wrong_keyid() -> None:
    records = GlobalRecords()
    priv, pub = _keypair()
    ed_signer = ZoneBSigner.from_scheme(Ed25519Scheme("kid", private_key=priv))
    signed = ed_signer.with_attestation(_det_gate())
    # An HMAC verifier must reject an ed25519 signature (scheme-name mismatch).
    assert DevGateWriteVerifier(b"k", "kid", records).verify_attestation(signed) is False
    # Right scheme, wrong key_id → reject.
    wrong_id = DevGateWriteVerifier.from_scheme(Ed25519Scheme("other-kid", public_key=pub), records)
    assert wrong_id.verify_attestation(signed) is False


def test_parse_signature() -> None:
    assert parse_signature("hmac256:kid:abcd") == ("hmac256", "kid", "abcd")
    assert parse_signature("ed25519:k:deadbeef") == ("ed25519", "k", "deadbeef")
    assert parse_signature("malformed") is None
    assert parse_signature("") is None
    assert parse_signature(None) is None


# --- env-driven production wiring --------------------------------------------- #

def test_env_default_is_dev_hmac_shared() -> None:
    signer, verifier = signing_schemes_from_env(dev_key=b"dev", dev_key_id="dev", env={})
    assert signer.name == "hmac256" and verifier.name == "hmac256"
    assert signer is verifier  # symmetric: same object both sides (dev)


def test_env_ed25519_splits_signer_and_verifier() -> None:
    priv, _pub = _keypair()
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    seed = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    signer, verifier = signing_schemes_from_env(
        dev_key=b"dev", dev_key_id="dev",
        env={"FOUNDRY_SIGNING_SCHEME": "ed25519", "FOUNDRY_SIGNING_PRIVATE_KEY": seed,
             "FOUNDRY_SIGNING_KEY_ID": "prod"},
    )
    assert signer.name == "ed25519" and verifier.name == "ed25519"
    # The signer can sign; the verifier provably CANNOT (verify-only).
    tag = signer.tag(b"x")
    assert verifier.verify_tag(b"x", tag) is True
    with pytest.raises(RuntimeError):
        verifier.tag(b"x")


def test_env_ed25519_without_keys_fails_closed() -> None:
    with pytest.raises(ValueError, match="requires"):
        signing_schemes_from_env(
            dev_key=b"dev", dev_key_id="dev", env={"FOUNDRY_SIGNING_SCHEME": "ed25519"}
        )
