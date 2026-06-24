"""Durable, filesystem-backed content-addressed store — a drop-in for ArtifactStore.

Same interface as :class:`foundry.durability.artifact_store.ArtifactStore`
(``put_body`` / ``put_body_at`` / ``get_body`` / ``put_blob`` / ``get_blob`` /
``resolve`` / ``resolve_blob``), so it can back :attr:`AgentRuntime.cas`
unchanged. Where the in-memory store keeps two ``dict`` maps, this keeps two
directories under a ``root``: typed bodies and raw blobs (distinct trees so a
kind-aware lookup can never mis-dispatch a raw diff blob as a typed body — the
SAME GW1a / D-R1-02 discipline as the in-memory store).

DURABILITY POSTURE (implementation-plan §0.3): the in-memory ``dict``-backed
``ArtifactStore`` is dev-grade. This is the PRODUCTION drop-in for the
docker-compose / EKS profile — bodies/blobs live on a mounted volume that
survives a process restart. The interface is identical so swapping to an S3 /
object-store backend later is again a drop-in (write the same two seams against
a bucket instead of a directory). See ``docs/deploy-compose.md``.

Serialization seams (IDENTICAL trust posture to the in-memory store):

* **Typed bodies** are (de)serialized through the allowlist serde
  (``dumps_typed`` -> ``(type_tag, payload)``), NEVER pickle (LEARNINGS I5). The
  ``type_tag`` is persisted alongside the payload so ``loads_typed`` can
  reconstitute the exact frozen model. A body that does not round-trip back to a
  :class:`FrozenModel` (a non-allowlisted type degrades to a ``dict``) is
  REJECTED fail-closed, exactly like the in-memory store.
* **Raw blobs** are written as raw bytes, keyed by the RAW ``hashlib.sha256`` of
  the bytes — EXACTLY ``Sandbox.put_blob`` / the in-memory store — so a diff
  blob mirrored from the sandbox keeps the same ``cas://sha256/`` uri.

Content-addressed on-disk layout (the uri is the address, fanned out 2 chars to
avoid one giant directory):

    <root>/bodies/<aa>/<rest-of-key>.body   # framed: type_tag + b"\\n" + payload
    <root>/blobs/<aa>/<rest-of-key>.blob    # raw bytes

The ``<key>`` is a stable, filesystem-safe hash of the uri (the uri itself can
carry ``cas://feature_diff/<key>`` for ``put_body_at``, whose slashes must NOT
become path separators here). Writes are atomic (write a temp file in the same
directory, then ``os.replace``) so a concurrent reader never observes a
half-written body.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.durability.artifact_store import _CAS_SHA256_PREFIX, UnknownArtifactError
from foundry.durability.serde import foundry_serde
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.schemas.base import Artifact

__all__ = ["FilesystemArtifactStore"]

# Frame a typed body on disk as: <type_tag-utf8> b"\n" <payload-bytes>. The
# type_tag is a short ascii token ("msgpack"/"json") with no embedded newline,
# so the FIRST newline unambiguously splits tag from payload.
_FRAME_SEP = b"\n"


def _key_for_uri(uri: str) -> str:
    """A stable, filesystem-safe, collision-free key for a cas uri.

    The uri is already content-addressed (or carries an explicit idempotency
    key); we only need a reversible-enough, path-safe encoding. We hash the
    uri's utf-8 bytes — unambiguous, no path-separator hazard, fixed alphabet —
    rather than trusting the uri's own characters (``put_body_at`` uris carry
    ``/`` which must NOT become a directory boundary).
    """
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()


class FilesystemArtifactStore:
    """Durable filesystem content-addressed store for typed bodies AND raw blobs.

    Drop-in for :class:`foundry.durability.artifact_store.ArtifactStore`: carry it
    on :attr:`foundry.agents._runtime.AgentRuntime.cas` and ``emit_artifact`` /
    the ``resolve`` / ``resolve_blob`` seams use it unchanged.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._bodies_dir = self._root / "bodies"
        self._blobs_dir = self._root / "blobs"
        self._bodies_dir.mkdir(parents=True, exist_ok=True)
        self._blobs_dir.mkdir(parents=True, exist_ok=True)
        self._serde = foundry_serde()

    # -- on-disk path helpers -------------------------------------------------

    def _body_path(self, uri: str) -> Path:
        key = _key_for_uri(uri)
        return self._bodies_dir / key[:2] / f"{key}.body"

    def _blob_path(self, uri: str) -> Path:
        key = _key_for_uri(uri)
        return self._blobs_dir / key[:2] / f"{key}.blob"

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """Write ``data`` to ``path`` atomically (temp file + ``os.replace``).

        Content-addressed writes are idempotent; the atomic swap means a
        concurrent reader sees either the previous body or the full new one,
        never a truncated frame.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    # -- typed bodies ---------------------------------------------------------

    def put_body(self, model: FrozenModel) -> str:
        """Persist a typed frozen body; return its ``cas://sha256/<identity>`` uri.

        The uri is the body's ``content_identity()`` — IDENTICAL to what
        ``emit_artifact`` mints and to the in-memory store — so the handle
        resolves. Serialized via the allowlist serde (never pickle, I5).
        """
        uri = f"{_CAS_SHA256_PREFIX}{model.content_identity()}"
        self._write_body(uri, model)
        return uri

    def put_body_at(self, uri: str, model: FrozenModel) -> str:
        """Persist a typed body under an EXPLICIT, caller-chosen ``uri``.

        For the junior's FeatureDiff handle, whose uri carries the diff
        idempotency KEY (``cas://feature_diff/<key>``) rather than the body's
        content identity. Returns ``uri`` unchanged.
        """
        self._write_body(uri, model)
        return uri

    def _write_body(self, uri: str, model: FrozenModel) -> None:
        type_tag, payload = self._serde.dumps_typed(model)
        frame = type_tag.encode("utf-8") + _FRAME_SEP + payload
        self._atomic_write(self._body_path(uri), frame)

    def get_body(self, uri: str) -> FrozenModel:
        """Deserialize the typed body at ``uri``; FAIL CLOSED on an unknown uri.

        Raises :class:`UnknownArtifactError` if the uri is absent OR if the
        stored payload did not round-trip back to a :class:`FrozenModel` (a
        non-allowlisted body degrades to a ``dict`` under the serde — we reject
        it rather than hand back an untyped body), matching the in-memory store.
        """
        path = self._body_path(uri)
        try:
            frame = path.read_bytes()
        except FileNotFoundError as exc:
            raise UnknownArtifactError(f"no body stored for cas uri {uri!r}") from exc
        type_tag, _, payload = frame.partition(_FRAME_SEP)
        body = self._serde.loads_typed((type_tag.decode("utf-8"), payload))
        if not isinstance(body, FrozenModel):
            raise UnknownArtifactError(
                f"body at {uri!r} did not deserialize to a frozen model "
                f"(got {type(body).__name__}) — type not in the serde allowlist?"
            )
        return body

    # -- raw blobs ------------------------------------------------------------

    def put_blob(self, data: bytes | str) -> str:
        """Persist raw bytes; return a ``cas://sha256/<raw-sha256-hex>`` uri.

        Uses the RAW ``hashlib.sha256`` of the bytes — EXACTLY ``Sandbox.put_blob``
        and the in-memory store — so a diff blob mirrored from the sandbox keeps
        the uri the FeatureDiff's ``diff_uri`` already names.
        """
        raw = data.encode("utf-8") if isinstance(data, str) else data
        digest = hashlib.sha256(raw).hexdigest()
        uri = f"{_CAS_SHA256_PREFIX}{digest}"
        self._atomic_write(self._blob_path(uri), raw)
        return uri

    def get_blob(self, uri: str) -> bytes:
        """The raw blob bytes at ``uri``; FAIL CLOSED on an unknown uri."""
        try:
            return self._blob_path(uri).read_bytes()
        except FileNotFoundError as exc:
            raise UnknownArtifactError(f"no blob stored for cas uri {uri!r}") from exc

    # -- callable views (the seams gate_wiring / factory_graph wire) ----------

    def resolve(self, artifact: Artifact) -> Any:
        """``ArtifactResolver`` view: reconstitute the typed body of a handle."""
        return self.get_body(artifact.uri)

    def resolve_blob(self, uri: str) -> bytes:
        """``BlobResolver`` view: fetch a raw blob keyed on its ``cas://`` uri."""
        return self.get_blob(uri)
