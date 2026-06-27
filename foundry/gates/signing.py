"""Pluggable signing schemes for the zone-B trust root.

The dev-grade root is symmetric **HMAC-SHA256** under a locally-held key: the
signer and the verifier share the same secret, so anyone who can verify can also
forge. That is fine for a hermetic smoke run, but it is NOT a production trust
root — the whole point of "the gates are the sole authority" is that the party
which *verifies* a verdict (``merge_gates`` / admission / the deployer) must not
be able to *mint* one.

This module makes the scheme a swap point. ``HmacScheme`` is the unchanged dev
default (byte-identical wire format ``hmac256:<key_id>:<mac>``). ``Ed25519Scheme``
is a real **asymmetric** root: the signer holds the private key; the verifier
holds only the PUBLIC key and can verify but never sign. The wire format carries
the scheme name (``ed25519:<key_id>:<sig_hex>``) so a verifier dispatches on it,
which also makes scheme/key rotation a verifier-side concern, not a format change.

Production wiring (env, read by :func:`signing_schemes_from_env`):

* ``FOUNDRY_SIGNING_SCHEME=ed25519`` (default ``hmac256``).
* ``FOUNDRY_SIGNING_PRIVATE_KEY`` — raw 32-byte Ed25519 seed as hex, or a path to
  a file containing it (signer side only; the verifier never needs it).
* ``FOUNDRY_SIGNING_PUBLIC_KEY`` — raw 32-byte Ed25519 public key as hex or a path
  (verifier side; derived from the private key if only the private key is given).
* ``FOUNDRY_SIGNING_KEY_ID`` — the key identifier stamped into the wire format.

Ed25519 itself is still a software key here — a hardware HSM / KMS / keyless-OIDC
(Sigstore) root is the next swap, and slots in behind this same ``SigningScheme``
interface (replace ``tag``/``verify_tag`` with a KMS sign/verify call). The
asymmetric split — verifier cannot forge — is the load-bearing production property
this delivers today.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

HMAC_SCHEME = "hmac256"
ED25519_SCHEME = "ed25519"


def hmac_tag(key: bytes, payload: bytes) -> str:
    """HMAC-SHA256 hex — the dev-grade symmetric tag."""
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


@runtime_checkable
class SigningScheme(Protocol):
    """A verdict-signing primitive. ``tag`` produces the signature hex; a
    verify-only scheme (the asymmetric verifier) raises from ``tag``."""

    name: str
    key_id: str

    def tag(self, payload: bytes) -> str: ...
    def verify_tag(self, payload: bytes, tag: str) -> bool: ...


class HmacScheme:
    """Symmetric HMAC-SHA256 (dev default). Sign and verify both need the key."""

    name = HMAC_SCHEME

    def __init__(self, key: bytes, key_id: str) -> None:
        self._key = key
        self.key_id = key_id

    def tag(self, payload: bytes) -> str:
        return hmac_tag(self._key, payload)

    def verify_tag(self, payload: bytes, tag: str) -> bool:
        expected = hmac_tag(self._key, payload)
        return hmac.compare_digest(expected.encode(), tag.encode())


class Ed25519Scheme:
    """Asymmetric Ed25519. The signer holds the private key; the verifier holds
    only the public key (``tag`` then raises — a verifier can never forge)."""

    name = ED25519_SCHEME

    def __init__(
        self,
        key_id: str,
        *,
        private_key: object | None = None,
        public_key: object | None = None,
    ) -> None:
        # Imported lazily so the dev/default HMAC path never needs `cryptography`.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        self.key_id = key_id
        self._priv = private_key
        if public_key is not None:
            self._pub: object | None = public_key
        elif private_key is not None:
            self._pub = private_key.public_key()  # type: ignore[attr-defined]
        else:
            self._pub = None
        # Type assertion: a present public key is the right type.
        if self._pub is not None and not isinstance(self._pub, Ed25519PublicKey):
            raise TypeError("public_key must be an Ed25519PublicKey")

    def tag(self, payload: bytes) -> str:
        if self._priv is None:
            raise RuntimeError(
                "Ed25519Scheme has no private key — this is a VERIFY-ONLY scheme "
                "(the verifier must not be able to sign)"
            )
        return self._priv.sign(payload).hex()  # type: ignore[attr-defined]

    def verify_tag(self, payload: bytes, tag: str) -> bool:
        from cryptography.exceptions import InvalidSignature

        if self._pub is None:
            return False
        try:
            self._pub.verify(bytes.fromhex(tag), payload)  # type: ignore[attr-defined]
            return True
        except (InvalidSignature, ValueError):
            return False


def parse_signature(sig: object) -> tuple[str, str, str] | None:
    """Split a wire signature ``<scheme>:<key_id>:<tag>`` into its parts, or None
    if malformed. The key_id may itself contain no ``:`` (schemes/key-ids don't)."""
    if not isinstance(sig, str) or not sig:
        return None
    parts = sig.split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return (parts[0], parts[1], parts[2])


# --- production wiring -------------------------------------------------------- #


def _load_key_material(value: str) -> bytes:
    """A key given as hex, or as a path to a file containing hex/raw bytes."""
    p = Path(value)
    if p.is_file():
        raw = p.read_bytes().strip()
        try:
            return bytes.fromhex(raw.decode("ascii").strip())
        except (ValueError, UnicodeDecodeError):
            return raw  # raw 32 bytes on disk
    return bytes.fromhex(value)


def signing_schemes_from_env(
    *,
    dev_key: bytes,
    dev_key_id: str,
    env: dict[str, str] | None = None,
) -> tuple[SigningScheme, SigningScheme]:
    """Return ``(signer_scheme, verifier_scheme)`` from the environment.

    Default (no env) is the dev HMAC scheme on both sides — byte-identical to the
    historical behavior. ``FOUNDRY_SIGNING_SCHEME=ed25519`` selects the asymmetric
    production root: the signer scheme carries the private key, the verifier scheme
    carries ONLY the public key, so the two are genuinely different objects and the
    verifier cannot mint a verdict. Fails closed (raises) on a misconfigured
    asymmetric setup rather than silently falling back to a forgeable scheme.
    """
    env = env if env is not None else dict(os.environ)
    scheme = env.get("FOUNDRY_SIGNING_SCHEME", HMAC_SCHEME).strip().lower()
    if scheme in ("", HMAC_SCHEME):
        shared = HmacScheme(dev_key, dev_key_id)
        return (shared, shared)
    if scheme == ED25519_SCHEME:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )

        key_id = env.get("FOUNDRY_SIGNING_KEY_ID", dev_key_id)
        priv_env = env.get("FOUNDRY_SIGNING_PRIVATE_KEY")
        pub_env = env.get("FOUNDRY_SIGNING_PUBLIC_KEY")
        if not priv_env and not pub_env:
            raise ValueError(
                "FOUNDRY_SIGNING_SCHEME=ed25519 requires FOUNDRY_SIGNING_PRIVATE_KEY "
                "(signer) and/or FOUNDRY_SIGNING_PUBLIC_KEY (verifier); refusing to "
                "fall back to a forgeable symmetric key"
            )
        priv = (
            Ed25519PrivateKey.from_private_bytes(_load_key_material(priv_env))
            if priv_env
            else None
        )
        pub = (
            Ed25519PublicKey.from_public_bytes(_load_key_material(pub_env))
            if pub_env
            else None
        )
        signer = Ed25519Scheme(key_id, private_key=priv, public_key=pub if priv is None else None)
        # The verifier gets ONLY the public key (derived from priv if needed).
        verifier_pub = pub
        if verifier_pub is None and priv is not None:
            verifier_pub = priv.public_key()
        verifier = Ed25519Scheme(key_id, public_key=verifier_pub)
        return (signer, verifier)
    raise ValueError(
        f"unknown FOUNDRY_SIGNING_SCHEME {scheme!r} (expected 'hmac256' or 'ed25519')"
    )
