"""DEV-GRADE trust root for gate writes. (implementation-plan.md §0.3)

The *logic* here is real and correct — attestation/binding tuples, canonical
encoding, constant-time verification, fail-closed nonce handling — but the
*root of trust* is dev-grade: HMAC-SHA256 under locally-held symmetric keys
instead of hardware-backed / keyless-OIDC signing. Production hardening swaps
the key custody (air-gapped signer, real OIDC federation), not this code.

Trust topology (governance §1c, architecture.md §3):

- ``ZoneBSigner`` is held ONLY by trusted infrastructure (zone B — the
  deterministic-gate runner) and by the proxy that stamps judgment bindings
  OUTSIDE the implementing adapter. Agent code never holds the key, so an
  agent-emitted ``GateResult`` can never carry a valid signature.
- ``DevGateWriteVerifier`` is what ``merge_gates`` consults at fold time
  (via the ``GateWriteVerifier`` protocol in foundry/state/reducers.py).

Caching split (project efficiency requirement — the architecture-doc
pseudocode re-verifies every signature on every fold, O(records × supersteps)
HMACs over immutable records):

- ``verify_attestation`` / ``verify_judgment_binding`` /
  ``verify_provenance_binding`` are PURE in record content + the verifier's key:
  ``GateResult`` is frozen, and the verdict's ``content_identity()`` covers every
  signed field *including* the signature strings themselves. They are therefore
  memoized in a bounded FIFO cache (the provenance check keys on
  ``(verdict identity, provenance identity)`` — the provenance is an argument,
  not a record field, so it joins the memo key).
  (The judgment-binding tuple also signs ``seq.node_id``, which
  ``content_identity()`` deliberately excludes — so the binding cache keys on
  ``(content_identity, seq.node_id)`` to stay sound.)
- ``nonce_consumed_by_other_verdict`` / ``negative_verdict_latched`` read
  MUTABLE, transactionally-committed global state (the cross-fork nonce
  ledger and negative-verdict latch). Their answers legitimately change
  between folds and are NEVER cached — every fold consults the records view.
"""

from __future__ import annotations

import hashlib
import hmac
from collections import OrderedDict

from foundry.canonical import canonical_bytes
from foundry.gates.signing import (
    HmacScheme,
    SigningScheme,
    parse_signature,
)
from foundry.schemas.gates import GateResult
from foundry.schemas.security import SLSAProvenance
from foundry.state.global_records import GlobalRecordsView

_SCHEME = "hmac256"  # the dev-default scheme name (see foundry.gates.signing)
_DEFAULT_CACHE_SIZE = 4096


def _mac(key: bytes, payload: bytes) -> str:
    """The single HMAC entry point (HMAC-SHA256 hex). Module-level so tests
    can observe (re)computation."""
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _attestation_payload(g: GateResult) -> bytes:
    """Canonical bytes of the deterministic-gate attestation tuple.

    Binds the verdict to its inputs (run_against / config / lineage revisions
    / results digest) so it cannot be replayed across trees, and binds STATUS
    so a flipped verdict fails verification. (arch §3)
    """
    return canonical_bytes(
        {
            "gate_name": g.name,
            "run_against": g.run_against,
            "scanner_config_rev": g.scanner_config_rev,
            "lineage_map_revision": g.lineage_map_revision,
            "status": str(g.status),
            "results_digest": g.results_digest,
        }
    )


def _judgment_payload(g: GateResult) -> bytes:
    """Canonical bytes of the judgment-verdict binding tuple.

    state_sha (= run_against) keying blocks cross-tree/fork replay; binding
    STATUS + the single-use nonce blocks a same-tree verdict-FLIP replay.
    served_family is the proxy-OBSERVED set, sorted for canonical form.
    """
    return canonical_bytes(
        {
            "node_id": g.seq.node_id,
            "served_family": sorted(g.served_family),
            "status": str(g.status),
            "verdict_nonce": g.verdict_nonce,
            "lineage_map_revision": g.lineage_map_revision,
            "state_sha": g.run_against,
            "subject_artifact_uri": g.subject_artifact_uri,
        }
    )


def _provenance_payload(g: GateResult, prov: SLSAProvenance) -> bytes:
    """Canonical bytes of a build-artifact attestation bound to its provenance.

    Folds the deterministic-gate attestation tuple together with the SLSA
    provenance predicate's IDENTITY (builder + materials + subject digest +
    source/tree, via ``content_identity()``), plus the load-bearing
    ``subject_digest`` / ``builder_id`` spelled out so a reviewer sees exactly
    what is bound. Because the predicate's identity covers every input it names
    (material digests, ``source_digest``, ``tree_sha``, ``scanner_config_rev``,
    ``gate_result_uris`` — security.py ``SLSAProvenance`` INPUT-BINDING note),
    a build attestation signed for provenance P cannot verify against a DIFFERENT
    provenance P' (any tampered material/subject digest yields a different
    ``content_identity()``), nor be replayed onto a different tree/config/status
    (the base attestation tuple already binds those). (governance.md §4/§5)
    """
    return canonical_bytes(
        {
            "attestation": _attestation_payload(g).decode("utf-8"),
            "provenance_identity": prov.content_identity(),
            "subject_name": prov.subject_name,
            "subject_digest": prov.subject_digest,
            "builder_id": prov.builder_id,
            "tree_sha": prov.tree_sha,
        }
    )


class ZoneBSigner:
    """Signs gate verdicts. Held ONLY by trusted infra (zone B / the proxy);
    agent code never sees the key.

    The signing scheme is pluggable (:mod:`foundry.gates.signing`). The
    ``(key, key_id)`` constructor is the dev-grade symmetric HMAC root (unchanged);
    :meth:`from_scheme` takes an asymmetric Ed25519 (or future KMS) scheme so the
    verifier never holds the signing secret."""

    def __init__(self, key: bytes, key_id: str) -> None:
        self._scheme: SigningScheme = HmacScheme(key, key_id)

    @classmethod
    def from_scheme(cls, scheme: SigningScheme) -> ZoneBSigner:
        self = cls.__new__(cls)
        self._scheme = scheme
        return self

    def _sign(self, payload: bytes) -> str:
        return f"{self._scheme.name}:{self._scheme.key_id}:{self._scheme.tag(payload)}"

    def attest_gate(self, g: GateResult) -> str:
        """Zone-B runner attestation for a deterministic-gate verdict."""
        return self._sign(_attestation_payload(g))

    def stamp_judgment(self, g: GateResult) -> str:
        """Judgment-verdict binding — stamped by the proxy OUTSIDE the
        implementing adapter (the adapter never holds this key)."""
        return self._sign(_judgment_payload(g))

    def attest_provenance(self, g: GateResult, prov: SLSAProvenance) -> str:
        """Zone-B attestation for a BUILD-artifact verdict, INPUT-BOUND to its
        SLSA provenance predicate (builder + materials + subject digest)."""
        return self._sign(_provenance_payload(g, prov))

    # GateResult is frozen — attach signatures via model_copy supersession.

    def with_attestation(self, g: GateResult) -> GateResult:
        return g.model_copy(update={"runner_attestation": self.attest_gate(g)})

    def with_judgment_binding(self, g: GateResult) -> GateResult:
        return g.model_copy(update={"judgment_binding": self.stamp_judgment(g)})

    def with_provenance_binding(
        self, g: GateResult, prov: SLSAProvenance
    ) -> GateResult:
        """Attach a provenance-bound attestation to a build verdict.

        Mirrors ``with_attestation`` but the signature is over the attestation
        tuple AND the provenance identity, so the verdict is bound to its EXACT
        build (subject/builder/materials). Verify with
        ``DevGateWriteVerifier.verify_provenance_binding(g, prov)``; the same
        ``prov`` must be re-presented or verification fails."""
        return g.model_copy(
            update={"runner_attestation": self.attest_provenance(g, prov)}
        )


class _BoundedCache:
    """Tiny FIFO-evicting memo table (records are immutable; dev-grade)."""

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._d: OrderedDict[object, bool] = OrderedDict()

    def get(self, key: object) -> bool | None:
        return self._d.get(key)

    def put(self, key: object, value: bool) -> None:
        self._d[key] = value
        while len(self._d) > self._maxsize:
            self._d.popitem(last=False)


class DevGateWriteVerifier:
    """`GateWriteVerifier` implementation for `merge_gates` (reducers.py).

    Signature checks are memoized by content identity (immutable inputs);
    nonce-consumption and latch reads hit the `GlobalRecordsView` on EVERY
    fold — they are the mutable committed inputs. See module docstring.
    """

    def __init__(
        self,
        key: bytes,
        key_id: str,
        records: GlobalRecordsView,
        *,
        cache_size: int = _DEFAULT_CACHE_SIZE,
    ) -> None:
        self._scheme: SigningScheme = HmacScheme(key, key_id)
        self._records = records
        self._attestation_cache = _BoundedCache(cache_size)
        self._binding_cache = _BoundedCache(cache_size)
        self._provenance_cache = _BoundedCache(cache_size)

    @classmethod
    def from_scheme(
        cls,
        scheme: SigningScheme,
        records: GlobalRecordsView,
        *,
        cache_size: int = _DEFAULT_CACHE_SIZE,
    ) -> DevGateWriteVerifier:
        self = cls.__new__(cls)
        self._scheme = scheme
        self._records = records
        self._attestation_cache = _BoundedCache(cache_size)
        self._binding_cache = _BoundedCache(cache_size)
        self._provenance_cache = _BoundedCache(cache_size)
        return self

    # ---- immutable, memoizable signature checks -----------------------------

    def _check(self, provided: object, payload: bytes) -> bool:
        """Verify a wire signature against ``payload``. False on missing/malformed
        or a scheme/key-id mismatch. Only the secret tag is constant-time-compared
        (the scheme name + key_id are public identifiers)."""
        parsed = parse_signature(provided)
        if parsed is None:
            return False
        scheme_name, key_id, tag = parsed
        if scheme_name != self._scheme.name or key_id != self._scheme.key_id:
            return False
        return self._scheme.verify_tag(payload, tag)

    def verify_attestation(self, g: GateResult) -> bool:
        # Sound memo key: content_identity() covers every field of the
        # attestation tuple AND the runner_attestation string itself.
        key = g.content_identity()
        hit = self._attestation_cache.get(key)
        if hit is not None:
            return hit
        ok = self._check(g.runner_attestation, _attestation_payload(g))
        self._attestation_cache.put(key, ok)
        return ok

    def verify_judgment_binding(self, g: GateResult) -> bool:
        # content_identity() excludes `seq`, but the binding tuple signs
        # seq.node_id — join it into the memo key so identical-content records
        # emitted under different node ids never share a cache entry.
        key = (g.content_identity(), g.seq.node_id)
        hit = self._binding_cache.get(key)
        if hit is not None:
            return hit
        ok = self._check(g.judgment_binding, _judgment_payload(g))
        self._binding_cache.put(key, ok)
        return ok

    def verify_provenance_binding(self, g: GateResult, prov: SLSAProvenance) -> bool:
        """Verify a build verdict's provenance-bound attestation.

        Returns True iff ``g.runner_attestation`` is a valid zone-B signature
        over (this attestation tuple, THIS provenance's identity). Re-presenting
        the verdict with a TAMPERED provenance (different material/subject digest
        -> different ``content_identity()``) recomputes a different expected MAC
        and fails. Sound memo key: ``content_identity()`` covers the attestation
        tuple + the ``runner_attestation`` string, joined with the provenance
        identity so a given verdict/provenance pair caches independently."""
        key = (g.content_identity(), prov.content_identity())
        hit = self._provenance_cache.get(key)
        if hit is not None:
            return hit
        ok = self._check(g.runner_attestation, _provenance_payload(g, prov))
        self._provenance_cache.put(key, ok)
        return ok

    # ---- mutable committed reads — NEVER cached ------------------------------

    def nonce_consumed_by_other_verdict(self, g: GateResult) -> bool:
        # A judgment verdict with no nonce can never be proven fresh:
        # fail-closed -> treat as consumed.
        if not g.verdict_nonce:
            return True
        return self._records.nonce_consumed_by_other(g.verdict_nonce, g.content_identity())

    def negative_verdict_latched(self, g: GateResult) -> bool:
        return self._records.negative_verdict_latched(g.latch_key())
