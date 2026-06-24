"""Product-feedback loop — production signal RE-ENTERS the factory. (lifecycle.md §3)

The pipeline ends at "Released"; real products iterate on user reality. A
production SIGNAL (a defect, a feature request, an observed regression) is not a
fresh build — it is a *maintained change to an already-released artifact*, so it
RE-ENTERS the factory through the SAME maintenance re-gate path
(:mod:`foundry.lifecycle.maintenance`), NOT a side door that bypasses the gates.

The closed loop is::

    production signal
      -> feedback_to_backlog(signal)      # this module: a backlog item
      -> the maintenance route             # maintenance.plan_maintenance / maintain
      -> RE-VERIFY (change-proportional) + RE-GATE + RE-ADMIT (fail-closed)
      -> re-gated release                  # provenance links the prior version

A :class:`BacklogItem` therefore LINKS to the affected released artifact's
provenance (the maintenance-chain anchor — ``prior_subject_uri``) so the
maintained change can re-enter against the right prior version. A **regression**
re-enters with ``re_gate_required=True`` and the maintenance re-gate path
explicitly named: a regression is the canonical "this slipped, re-prove it"
trigger and MUST NOT short-circuit the gates.

Determinism (L7): :func:`feedback_to_backlog` is a pure function of the signal
(no clock, no network, no agent code) — an equal signal yields an equal backlog
item. The signal carries its own ``observed_at`` if a timestamp is needed; we
never read the wall clock.
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.base import FrozenModel

__all__ = [
    "BacklogItem",
    "ProductSignal",
    "SignalKind",
    "feedback_to_backlog",
]


class SignalKind(StrEnum):
    """The taxonomy of production signals that re-enter the factory (§3).

    Every kind re-enters via the SAME maintenance re-gate path; the kind only
    shapes the backlog framing (a regression hard-requires the re-gate, a feature
    request is a net-new maintained change). None of them bypass the gates.
    """

    DEFECT = "defect"  # a reported bug in released behaviour
    FEATURE_REQUEST = "feature_request"  # a net-new capability ask
    REGRESSION = "regression"  # released behaviour that slipped (re-prove it)
    PERFORMANCE = "performance"  # a performance hotspot / metric decay
    DATA_DRIFT = "data_drift"  # ML: input distribution drift (retrain trigger)


# The single route a backlog item re-enters by — the maintenance re-gate path.
# Named as a constant so the loop is closed against ONE seam, not a free string.
MAINTENANCE_ROUTE = "maintenance"


class ProductSignal(FrozenModel):
    """A signal observed in production against a RELEASED artifact (§3).

    ``released_subject_uri`` is the content-addressed handle of the released
    artifact the signal is about (the maintenance-chain anchor — the maintained
    change re-enters against THIS prior version). ``provenance_uri`` is the
    handle of that artifact's :class:`~foundry.lifecycle.provenance.ProvenanceRecord`
    so the backlog item links the change back to the release it maintains.

    Bodies (telemetry payloads, repro steps) are content-addressed handles
    (DS9); this record carries the routing-bearing fields. ``observed_at`` is an
    OPTIONAL caller-supplied epoch (we never read the clock — determinism L7).
    """

    signal_id: str  # stable identity of this production signal
    kind: SignalKind
    released_subject_uri: str  # the released artifact the signal is about
    provenance_uri: str  # handle to that artifact's ProvenanceRecord (§5 link)
    summary: str  # one-line human-legible summary
    detail_uri: str | None = None  # CAS handle to telemetry/repro detail (DS9)
    observed_at: int | None = None  # OPTIONAL caller epoch (never the wall clock)


class BacklogItem(FrozenModel):
    """A production signal turned into a maintained-change backlog item (§3).

    The item RE-ENTERS the factory via the maintenance route: it carries the
    ``prior_subject_uri`` (the released version being maintained — the
    maintenance-chain link consumed by
    :func:`foundry.lifecycle.maintenance.plan_maintenance`) and the
    ``provenance_uri`` linking back to that release's provenance.

    ``re_gate_required`` is the load-bearing bit: it is ALWAYS True — a maintained
    change never bypasses the gates (fail-closed; lifecycle.md §1/§3). It is
    surfaced explicitly so a caller cannot construct a backlog item that opts out
    of the re-gate. A regression additionally sets ``is_regression`` so the
    re-gate framing is unambiguous, but the re-gate requirement is universal.
    """

    backlog_id: str  # stable identity of the backlog item
    origin_signal_id: str  # the ProductSignal that produced it (loop provenance)
    kind: SignalKind
    prior_subject_uri: str  # the released version being maintained (chain link)
    provenance_uri: str  # handle to the prior release's ProvenanceRecord
    summary: str
    route: str = MAINTENANCE_ROUTE  # re-enters via maintenance — never a side door
    re_gate_required: bool = True  # ALWAYS True: maintenance never bypasses gates
    is_regression: bool = False  # a regression — the canonical re-prove trigger
    detail_uri: str | None = None

    def reenters_via_regate(self) -> bool:
        """True iff this item re-enters through the maintenance re-gate path.

        Fail-closed: a backlog item is only loop-valid when it routes to the
        maintenance route AND requires the re-gate. Both hold by construction,
        so this is a self-check the closed loop can assert on.
        """
        return self.route == MAINTENANCE_ROUTE and self.re_gate_required


def feedback_to_backlog(signal: ProductSignal) -> BacklogItem:
    """Turn a production signal into a maintained-change backlog item (§3).

    Pure derivation. The resulting :class:`BacklogItem`:

    * LINKS to the affected released artifact's provenance
      (``prior_subject_uri`` + ``provenance_uri``) so the maintained change
      re-enters against the right prior version (the maintenance-chain anchor);
    * RE-ENTERS via the maintenance route with ``re_gate_required=True`` — it
      does NOT bypass the gates (fail-closed; lifecycle.md §1/§3);
    * marks ``is_regression`` for a :attr:`SignalKind.REGRESSION` signal so the
      "re-prove it" framing is explicit (a regression re-enters with the
      maintenance re-gate path, never short-circuiting the gates).

    The backlog id is derived deterministically from the signal id so an equal
    signal yields an equal backlog item (L7).
    """
    return BacklogItem(
        backlog_id=f"backlog:{signal.signal_id}",
        origin_signal_id=signal.signal_id,
        kind=signal.kind,
        prior_subject_uri=signal.released_subject_uri,
        provenance_uri=signal.provenance_uri,
        summary=signal.summary,
        route=MAINTENANCE_ROUTE,
        re_gate_required=True,
        is_regression=signal.kind is SignalKind.REGRESSION,
        detail_uri=signal.detail_uri,
    )
