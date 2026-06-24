"""The BUILD-MANIFEST — one index tying a build's outputs together.

A real company's release ships a manifest: what was built, which gates passed
(with their attestation), the dependency/provenance digests, the cost of the
build, and the documents produced. Foundry already records all of this, but
scattered — across ``state['artifacts']``, ``state['gates']``, the proxy's
per-call manifest, and the evidence pack. :class:`BuildManifest` consolidates it
into one content-addressed record that renders to both JSON (machine) and
markdown (human).

Everything here is GROUNDED: artifact/gate entries come from the terminal state,
the model-usage figures come from the proxy's observed per-call manifest (the
same observed-not-trusted record the lineage check uses), and the cost comes
from the budget ledger when present — never invented.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.schemas.base import Artifact
    from foundry.schemas.gates import GateResult

__all__ = [
    "ArtifactRef",
    "BuildManifest",
    "DocRef",
    "GateRef",
    "ModelUsageSummary",
    "build_manifest",
    "render_manifest_md",
]


class ArtifactRef(FrozenModel):
    kind: str
    uri: str
    produced_by: str
    run_id: str = ""
    emission_epoch: int = 0


class GateRef(FrozenModel):
    name: str
    status: str
    run_against: str = ""
    attested: bool = False


class DocRef(FrozenModel):
    path: str
    title: str
    sha256: str


class ModelUsageSummary(FrozenModel):
    model_calls: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cache_read_tokens: int = 0
    served_families: tuple[str, ...] = ()
    served_models: tuple[str, ...] = ()
    drift_events: int = 0
    # Pinned-ID drift broken down by kind (e.g. ``pinned-id``, ``tier-mismatch``)
    # so the count is actionable, not opaque — a tier-mismatch is a silent
    # within-family capability degrade (arch §8 / ops §5). Derived from the proxy
    # per-call records; absent kinds simply don't appear.
    drift_by_kind: dict[str, int] = {}  # noqa: RUF012 - pydantic deep-copies defaults
    calls_by_tier: dict[str, int] = {}  # noqa: RUF012 - pydantic deep-copies defaults


class BuildManifest(FrozenModel):
    schema_version: str = "1"
    request: str
    thread_id: str
    generated_at: str
    passed: bool
    phase: str
    tree_sha: str = ""
    artifacts: tuple[ArtifactRef, ...] = ()
    gates: tuple[GateRef, ...] = ()
    docs: tuple[DocRef, ...] = ()
    model_usage: ModelUsageSummary = ModelUsageSummary()
    estimated_cost_microusd: int = 0


def _artifact_refs(artifacts: Sequence[Artifact]) -> tuple[ArtifactRef, ...]:
    refs = [
        ArtifactRef(
            kind=a.kind,
            uri=a.uri,
            produced_by=a.produced_by,
            run_id=a.seq.run_id,
            emission_epoch=a.seq.emission_epoch,
        )
        for a in artifacts
    ]
    return tuple(sorted(refs, key=lambda r: (r.kind, r.uri)))


def _gate_refs(gates: Sequence[GateResult]) -> tuple[GateRef, ...]:
    refs = [
        GateRef(
            name=g.name,
            status=g.status.value,
            run_against=g.run_against,
            # A verdict is attested by EITHER a zone-B deterministic-gate signature
            # (``runner_attestation``) OR a proxy-stamped judgment binding
            # (``judgment_binding``) — a bound Review / judgment verdict carries the
            # latter, not the former, so counting only ``runner_attestation`` showed
            # it as attested:no. Both are trusted, proxy/zone-B-minted bindings.
            attested=bool(getattr(g, "runner_attestation", None))
            or bool(getattr(g, "judgment_binding", None)),
        )
        for g in gates
    ]
    return tuple(sorted(refs, key=lambda r: (r.name, r.run_against)))


def _usage_summary(
    proxy_manifest: Sequence[Mapping[str, Any]], drift_events: int
) -> ModelUsageSummary:
    tin = tout = cache = 0
    families: set[str] = set()
    models: set[str] = set()
    by_tier: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for rec in proxy_manifest:
        usage = rec.get("usage") or {}
        tin += int(usage.get("input_tokens", 0))
        tout += int(usage.get("output_tokens", 0))
        cache += int(usage.get("cache_read_input_tokens", 0))
        for fam in rec.get("served_family") or ():
            families.add(str(fam))
        if rec.get("served_model"):
            models.add(str(rec["served_model"]))
        tier = str(rec.get("tier", "?"))
        by_tier[tier] = by_tier.get(tier, 0) + 1
        # Drift breakdown by kind (tier-mismatch vs pinned-id) — the record only
        # carries ``drift_kind`` when it drifted, so absent kinds never count.
        if rec.get("drift"):
            kind = str(rec.get("drift_kind") or "pinned-id")
            by_kind[kind] = by_kind.get(kind, 0) + 1
    return ModelUsageSummary(
        model_calls=len(proxy_manifest),
        total_tokens_in=tin,
        total_tokens_out=tout,
        total_cache_read_tokens=cache,
        served_families=tuple(sorted(families)),
        served_models=tuple(sorted(models)),
        drift_events=drift_events,
        drift_by_kind=dict(sorted(by_kind.items())),
        calls_by_tier=dict(sorted(by_tier.items())),
    )


def build_manifest(
    *,
    request: str,
    thread_id: str,
    generated_at: str,
    passed: bool,
    phase: str,
    artifacts: Sequence[Artifact],
    gates: Sequence[GateResult],
    docs: Sequence[DocRef],
    proxy_manifest: Sequence[Mapping[str, Any]] = (),
    drift_events: int = 0,
    tree_sha: str = "",
    estimated_cost_microusd: int = 0,
) -> BuildManifest:
    """Consolidate a finished run's outputs into a :class:`BuildManifest`."""
    return BuildManifest(
        request=request,
        thread_id=thread_id,
        generated_at=generated_at,
        passed=passed,
        phase=phase,
        tree_sha=tree_sha,
        artifacts=_artifact_refs(artifacts),
        gates=_gate_refs(gates),
        docs=tuple(docs),
        model_usage=_usage_summary(proxy_manifest, drift_events),
        estimated_cost_microusd=estimated_cost_microusd,
    )


def render_manifest_md(m: BuildManifest) -> str:
    """Human-readable rendering of the manifest."""
    u = m.model_usage
    lines = [
        "# Build Manifest",
        "",
        f"- **Request:** {m.request!r}",
        f"- **Run:** `{m.thread_id}`",
        f"- **Generated:** {m.generated_at}",
        f"- **Result:** {'PASS' if m.passed else 'HALTED'} (phase: {m.phase})",
        f"- **Tree:** `{m.tree_sha or 'n/a'}`",
        f"- **Estimated cost:** ${m.estimated_cost_microusd / 1_000_000:.4f}",
        "",
        "## Gates",
        "",
        "| Gate | Status | Attested | Run against |",
        "|------|--------|----------|-------------|",
    ]
    for g in m.gates:
        lines.append(
            f"| {g.name} | {g.status} | {'yes' if g.attested else 'no'} | `{g.run_against}` |"
        )
    if not m.gates:
        lines.append("| _none_ | — | — | — |")

    lines += ["", "## Artifacts", ""]
    lines += ["| Kind | Produced by | URI |", "|------|-------------|-----|"]
    for a in m.artifacts:
        lines.append(f"| {a.kind} | {a.produced_by} | `{a.uri}` |")
    if not m.artifacts:
        lines.append("| _none_ | — | — |")

    by_tier = ", ".join(f"{k}={v}" for k, v in u.calls_by_tier.items()) or "n/a"
    lines += [
        "",
        "## Model usage (observed)",
        "",
        f"- **Calls:** {u.model_calls} ({by_tier})",
        f"- **Tokens in / out:** {u.total_tokens_in:,} / {u.total_tokens_out:,}",
        f"- **Cache-read tokens:** {u.total_cache_read_tokens:,}",
        f"- **Served families:** {', '.join(u.served_families) or 'n/a'}",
        f"- **Served models:** {', '.join(u.served_models) or 'n/a'}",
        f"- **Pinned-ID drift events:** {u.drift_events}"
        + (
            " (" + ", ".join(f"{k}={v}" for k, v in u.drift_by_kind.items()) + ")"
            if u.drift_by_kind
            else ""
        ),
        "",
        "## Documents",
        "",
        "| Document | Path | sha256 |",
        "|----------|------|--------|",
    ]
    for d in m.docs:
        lines.append(f"| {d.title} | `{d.path}` | `{d.sha256[:12]}…` |")
    if not m.docs:
        lines.append("| _none_ | — | — |")
    return "\n".join(lines) + "\n"
