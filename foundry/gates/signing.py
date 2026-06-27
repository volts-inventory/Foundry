"""The zone-B signing scheme for the trust root.

The root is symmetric **HMAC-SHA256** under a locally-held key: the signer and
the verifier share the same secret. For a single-user LAN deployment that is the
right grain — the operator IS the trust boundary, and the property that matters
is structural (``merge_gates`` / admission only ever admit a verdict carrying a
valid tag the untrusted *agent* could not have produced).

``SigningScheme`` is kept as a small interface (``tag`` / ``verify_tag``) so the
attestation/verifier wiring dispatches on the scheme name in the wire format
``hmac256:<key_id>:<mac>`` — leaving room to slot a different primitive behind it
later without touching the call sites.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Protocol, runtime_checkable

HMAC_SCHEME = "hmac256"


def hmac_tag(key: bytes, payload: bytes) -> str:
    """HMAC-SHA256 hex — the symmetric tag."""
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


@runtime_checkable
class SigningScheme(Protocol):
    """A verdict-signing primitive. ``tag`` produces the signature hex."""

    name: str
    key_id: str

    def tag(self, payload: bytes) -> str: ...
    def verify_tag(self, payload: bytes, tag: str) -> bool: ...


class HmacScheme:
    """Symmetric HMAC-SHA256. Sign and verify both need the key."""

    name = HMAC_SCHEME

    def __init__(self, key: bytes, key_id: str) -> None:
        self._key = key
        self.key_id = key_id

    def tag(self, payload: bytes) -> str:
        return hmac_tag(self._key, payload)

    def verify_tag(self, payload: bytes, tag: str) -> bool:
        expected = hmac_tag(self._key, payload)
        return hmac.compare_digest(expected.encode(), tag.encode())


def parse_signature(sig: object) -> tuple[str, str, str] | None:
    """Split a wire signature ``<scheme>:<key_id>:<tag>`` into its parts, or None
    if malformed. The key_id may itself contain no ``:`` (schemes/key-ids don't)."""
    if not isinstance(sig, str) or not sig:
        return None
    parts = sig.split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return (parts[0], parts[1], parts[2])


def signing_schemes_from_env(
    *, dev_key: bytes, dev_key_id: str
) -> tuple[SigningScheme, SigningScheme]:
    """The zone-B ``(signer, verifier)`` schemes. Symmetric HMAC under the locally
    held key, so the two share one secret (returned as the same object)."""
    shared = HmacScheme(dev_key, dev_key_id)
    return (shared, shared)
