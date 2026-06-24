"""Content-addressed ArtifactStore — the real body store behind cas:// handles.

(DECISIONS D6, architecture.md §3 / DS9, implementation-plan §0.3)

``emit_artifact`` mints a content-addressed :class:`~foundry.schemas.base.Artifact`
*handle* whose ``uri = cas://sha256/<body.content_identity()>`` (DS9 / LEARNINGS
L4) — but historically it DISCARDED the body, so every downstream
``resolve``/``resolve_blob`` consumer (senior reads SystemDesign/OwnershipMap, qa
reads AcceptanceCriterion, gate_wiring reads the FeatureDiff handle + its diff
blob) only ever worked against a SYNTHETIC resolver in tests; a real cli run
would resolve NOTHING (fail-closed). This store closes that gap: when carried by
``AgentRuntime.cas`` it is written ON emit, and its :meth:`resolve` /
:meth:`resolve_blob` views are the REAL resolvers the cli / factory_graph wire.

Two body kinds, two seams (mirrors gate_wiring's two distinct resolve seams,
LEARNINGS GW1a / D-R1-02):

* **Typed bodies** — frozen pydantic records (AcceptanceCriterion, SystemDesign,
  FeatureDiff, ...). :meth:`put_body` serializes via the allowlist serde (the
  SAME trust posture as the checkpoint serde — never pickle, LEARNINGS I5) and
  keys it by ``cas://sha256/<model.content_identity()>`` — IDENTICAL to the uri
  ``emit_artifact`` mints, so the handle resolves. :meth:`get_body` deserializes
  through the allowlist and FAILS CLOSED on an unknown uri (or a body that did
  not round-trip back to a frozen model — a non-allowlisted type silently
  degrades to a ``dict`` under the serde, which we reject).

* **Raw blobs** — the unified-diff body a FeatureDiff's ``diff_uri`` points at
  (a UNIFIED-DIFF string the adapter wrote via ``sandbox.put_blob``).
  :meth:`put_blob` keys it by the RAW ``sha256`` of the bytes (``hashlib.sha256``,
  EXACTLY what :meth:`foundry.execution.sandbox.Sandbox.put_blob` uses) so a
  blob mirrored from the sandbox into the store keeps the SAME ``cas://sha256/``
  uri — and a store-backed :meth:`resolve_blob` reads the diff body end to end.
  (NB: raw bytes have no canonical-JSON form, so ``content_digest`` — which
  rejects ``bytes`` — is NOT usable here; the raw digest is what round-trips.)

DEV-GRADE (implementation-plan §0.3): in-memory ``dict``-backed, for dev / smoke
/ tests. A DURABLE content-addressed store (filesystem / Postgres large-object /
S3) is a PRODUCTION ops concern; this carries the SAME interface so the durable
backend is a drop-in swap behind ``AgentRuntime.cas``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from foundry.durability.serde import foundry_serde
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.schemas.base import Artifact

__all__ = ["ArtifactStore", "UnknownArtifactError"]

_CAS_SHA256_PREFIX = "cas://sha256/"


class UnknownArtifactError(KeyError):
    """A ``cas://`` uri was not found in the store (fail-closed, governance §1b).

    Raised by :meth:`ArtifactStore.get_body` / :meth:`ArtifactStore.get_blob`
    rather than returning ``None`` so a boundary never silently runs against a
    partial tree / mints a vacuous PASS on an unresolvable handle.
    """


class ArtifactStore:
    """In-memory content-addressed store for typed bodies AND raw blobs.

    Carried by :attr:`foundry.agents._runtime.AgentRuntime.cas`; written on
    ``emit_artifact(..., store=self)`` and on the junior's FeatureDiff path,
    read through the :meth:`resolve` / :meth:`resolve_blob` views the
    cli / factory_graph inject as the real materialization seams.
    """

    def __init__(self) -> None:
        # Typed-body store: cas uri -> serde (type_tag, bytes). Raw-blob store:
        # cas uri -> bytes. Distinct maps so a kind-aware lookup can never
        # mis-dispatch a raw diff blob as a typed body (GW1a / D-R1-02).
        self._bodies: dict[str, tuple[str, bytes]] = {}
        self._blobs: dict[str, bytes] = {}
        self._serde = foundry_serde()

    # -- typed bodies ---------------------------------------------------------

    def put_body(self, model: FrozenModel) -> str:
        """Store a typed frozen body; return its ``cas://sha256/<identity>`` uri.

        The uri is the body's ``content_identity()`` — IDENTICAL to what
        ``emit_artifact`` mints — so the returned handle resolves. Serialized via
        the allowlist serde (never pickle, I5); idempotent (re-putting the same
        body overwrites the identical bytes under the same key).
        """
        uri = f"{_CAS_SHA256_PREFIX}{model.content_identity()}"
        self._bodies[uri] = self._serde.dumps_typed(model)
        return uri

    def put_body_at(self, uri: str, model: FrozenModel) -> str:
        """Store a typed body under an EXPLICIT, caller-chosen ``uri``.

        For the junior's FeatureDiff, whose handle ``uri`` carries the diff
        idempotency KEY (``cas://feature_diff/<key>``), not the body's content
        identity — so ``resolve(feature_diff_handle)`` (``get_body(handle.uri)``)
        must find the body keyed on that handle uri. Returns ``uri`` unchanged.
        """
        self._bodies[uri] = self._serde.dumps_typed(model)
        return uri

    def get_body(self, uri: str) -> FrozenModel:
        """Deserialize the typed body at ``uri``; FAIL CLOSED on an unknown uri.

        Raises :class:`UnknownArtifactError` if the uri is absent, and
        :class:`UnknownArtifactError` if the stored payload did not round-trip
        back to a :class:`FrozenModel` (a non-allowlisted body type degrades to a
        ``dict`` under the serde — we reject it rather than hand back an untyped
        body that downstream ``isinstance`` checks would fail-closed on anyway).
        """
        try:
            payload = self._bodies[uri]
        except KeyError as exc:
            raise UnknownArtifactError(f"no body stored for cas uri {uri!r}") from exc
        body = self._serde.loads_typed(payload)
        if not isinstance(body, FrozenModel):
            raise UnknownArtifactError(
                f"body at {uri!r} did not deserialize to a frozen model "
                f"(got {type(body).__name__}) — type not in the serde allowlist?"
            )
        return body

    # -- raw blobs ------------------------------------------------------------

    def put_blob(self, data: bytes | str) -> str:
        """Store raw bytes; return a ``cas://sha256/<raw-sha256-hex>`` uri.

        Uses the RAW ``hashlib.sha256`` of the bytes — EXACTLY
        ``Sandbox.put_blob`` — so a diff blob mirrored from the sandbox into the
        store keeps the same uri the FeatureDiff's ``diff_uri`` already names, and
        :meth:`resolve_blob` resolves it end to end.
        """
        raw = data.encode("utf-8") if isinstance(data, str) else data
        digest = hashlib.sha256(raw).hexdigest()
        uri = f"{_CAS_SHA256_PREFIX}{digest}"
        self._blobs[uri] = raw
        return uri

    def get_blob(self, uri: str) -> bytes:
        """The raw blob bytes at ``uri``; FAIL CLOSED on an unknown uri."""
        try:
            return self._blobs[uri]
        except KeyError as exc:
            raise UnknownArtifactError(f"no blob stored for cas uri {uri!r}") from exc

    # -- callable views (the seams gate_wiring / factory_graph wire) ----------

    def resolve(self, artifact: Artifact) -> Any:
        """``ArtifactResolver`` view: reconstitute the typed body of a handle.

        The real resolver the cli / factory_graph inject in place of the test
        synthetic — ``get_body`` keyed on ``artifact.uri`` (LEARNINGS QS3 / GW1).
        """
        return self.get_body(artifact.uri)

    def resolve_blob(self, uri: str) -> bytes:
        """``BlobResolver`` view: fetch a raw blob keyed on its ``cas://`` uri.

        The real ``resolve_blob`` (mirrors ``sandbox.get_blob``) the
        cli / factory_graph inject for the FeatureDiff diff body (GW1a /
        D-R1-02) — keyed on the uri, NOT the typed handle, so it can never
        mis-dispatch a raw blob as a typed artifact.
        """
        return self.get_blob(uri)
