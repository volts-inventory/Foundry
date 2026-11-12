"""The BUILD-MANIFEST — one index tying a build's outputs together.

A real company's release ships a manifest: what was built, which gates passed
(with their attestation), the dependency/provenance digests, the cost of the
build. Foundry already records all of this, but
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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import GateStatus

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.graph.gate_wiring import ArtifactResolver
    from foundry.schemas.base import Artifact
    from foundry.schemas.gates import GateResult
    from foundry.state.factory_state import FactoryState

__all__ = [
    "ArtifactRef",
    "BaseRef",
    "BuildManifest",
    "ContractRef",
    "GateRef",
    "ModelUsageSummary",
    "build_manifest",
    "plan_fields",
    "render_manifest_md",
    "write_manifest",
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


class ModelUsageSummary(FrozenModel):
    model_calls: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    # Agentic coding sessions (claude-code adapter), which bypass the proxy records.
    agent_sessions: int = 0
    agent_tokens_in: int = 0
    agent_tokens_out: int = 0
    agent_cache_read_tokens: int = 0
    agent_cache_creation_tokens: int = 0
    served_families: tuple[str, ...] = ()
    served_models: tuple[str, ...] = ()
    drift_events: int = 0
    # Pinned-ID drift broken down by kind (e.g. ``pinned-id``, ``tier-mismatch``)
    # so the count is actionable, not opaque — a tier-mismatch is a silent
    # within-family capability degrade (arch §8 / ops §5). Derived from the proxy
    # per-call records; absent kinds simply don't appear.
    drift_by_kind: dict[str, int] = {}  # noqa: RUF012 - pydantic deep-copies defaults
    calls_by_tier: dict[str, int] = {}  # noqa: RUF012 - pydantic deep-copies defaults
    models_by_tier: dict[str, tuple[str, ...]] = {}  # noqa: RUF012 - pydantic deep-copies defaults


class ContractRef(FrozenModel):
    name: str
    kind: str = ""
    signature: str = ""


class BaseRef(FrozenModel):
    """The shipped build a change build extended."""

    thread_id: str = ""
    request: str = ""
    digest: str = ""  # content digest of the base tree's files
    files: int = 0


class BuildManifest(FrozenModel):
    schema_version: str = "1"
    request: str
    thread_id: str
    generated_at: str
    passed: bool
    phase: str
    tree_sha: str = ""
    # Whether a passing gate verdict ran against exactly the shipped tree (None:
    # nothing shipped). False means the bytes in out/ are not the ones the gates judged.
    tree_attested: bool | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    gates: tuple[GateRef, ...] = ()
    model_usage: ModelUsageSummary = ModelUsageSummary()
    estimated_cost_microusd: int = 0
    # What a later change build on this one needs: the stack it must keep and
    # the contracts it extends rather than redefines.
    stack: dict[str, Any] = {}  # noqa: RUF012 - pydantic deep-copies defaults
    contracts: tuple[ContractRef, ...] = ()
    contracts_files: tuple[str, ...] = ()
    base: BaseRef | None = None  # set when this build is a change on a base
    # Contradictions in the spec a coding session reported instead of coding around.
    spec_conflicts: tuple[str, ...] = ()


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
    proxy_manifest: Sequence[Mapping[str, Any]],
    drift_events: int,
    agent_usage: Sequence[Mapping[str, int]] = (),
) -> ModelUsageSummary:
    tin = tout = cache = cache_new = 0
    families: set[str] = set()
    models: set[str] = set()
    by_tier: dict[str, int] = {}
    tier_models: dict[str, set[str]] = {}
    by_kind: dict[str, int] = {}
    for rec in proxy_manifest:
        usage = rec.get("usage") or {}
        tin += int(usage.get("input_tokens", 0))
        tout += int(usage.get("output_tokens", 0))
        cache += int(usage.get("cache_read_input_tokens", 0))
        cache_new += int(usage.get("cache_creation_input_tokens", 0))
        for fam in rec.get("served_family") or ():
            families.add(str(fam))
        if rec.get("served_model"):
            models.add(str(rec["served_model"]))
        tier = str(rec.get("tier", "?"))
        by_tier[tier] = by_tier.get(tier, 0) + 1
        if rec.get("served_model"):
            tier_models.setdefault(tier, set()).add(str(rec["served_model"]))
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
        total_cache_creation_tokens=cache_new,
        agent_sessions=len(agent_usage),
        agent_tokens_in=sum(a.get("input_tokens", 0) for a in agent_usage),
        agent_tokens_out=sum(a.get("output_tokens", 0) for a in agent_usage),
        agent_cache_read_tokens=sum(a.get("cache_read_input_tokens", 0) for a in agent_usage),
        agent_cache_creation_tokens=sum(
            a.get("cache_creation_input_tokens", 0) for a in agent_usage
        ),
        served_families=tuple(sorted(families)),
        served_models=tuple(sorted(models)),
        drift_events=drift_events,
        drift_by_kind=dict(sorted(by_kind.items())),
        calls_by_tier=dict(sorted(by_tier.items())),
        models_by_tier={t: tuple(sorted(m)) for t, m in sorted(tier_models.items())},
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
    proxy_manifest: Sequence[Mapping[str, Any]] = (),
    drift_events: int = 0,
    agent_usage: Sequence[Mapping[str, int]] = (),
    agent_models: Sequence[str] = (),
    tree_sha: str = "",
    estimated_cost_microusd: int = 0,
) -> BuildManifest:
    """Consolidate a finished run's outputs into a :class:`BuildManifest`. Cost is
    the budget ledger's spend, else what the backends reported per call/session."""
    reported = sum(int(r.get("usage", {}).get("cost_microusd", 0)) for r in proxy_manifest)
    reported += sum(int(a.get("cost_microusd", 0)) for a in agent_usage)
    usage = _usage_summary(proxy_manifest, drift_events, agent_usage)
    if agent_models:
        models = tuple(sorted({*usage.served_models, *agent_models}))
        usage = usage.model_copy(update={"served_models": models})
    attested = (
        any(g.run_against == tree_sha and g.status is GateStatus.PASS for g in gates)
        if tree_sha
        else None
    )
    return BuildManifest(
        request=request,
        thread_id=thread_id,
        generated_at=generated_at,
        passed=passed,
        phase=phase,
        tree_sha=tree_sha,
        tree_attested=attested,
        artifacts=_artifact_refs(artifacts),
        gates=_gate_refs(gates),
        model_usage=usage,
        estimated_cost_microusd=estimated_cost_microusd or reported,
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
        f"- **Tree:** `{m.tree_sha or 'n/a'}`"
        + {True: " (gated)", False: " (**NOT the tree the gates passed**)", None: ""}[
            m.tree_attested
        ],
        f"- **Estimated cost:** ${m.estimated_cost_microusd / 1_000_000:.4f}",
        *(
            [
                f"- **Change on base:** run `{m.base.thread_id or 'unknown'}` "
                f"({m.base.request!r}), {m.base.files} files, digest `{m.base.digest}`"
            ]
            if m.base is not None
            else []
        ),
        *(
            ["", "## Spec conflicts (reported, not coded around)", ""]
            + [f"- {c}" for c in m.spec_conflicts]
            if m.spec_conflicts
            else []
        ),
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
        f"- **Cache-read / cache-write tokens:** {u.total_cache_read_tokens:,} / "
        f"{u.total_cache_creation_tokens:,}",
        f"- **Coding sessions:** {u.agent_sessions} — tokens in / out "
        f"{u.agent_tokens_in:,} / {u.agent_tokens_out:,}, cache read / write "
        f"{u.agent_cache_read_tokens:,} / {u.agent_cache_creation_tokens:,}",
        f"- **Served families:** {', '.join(u.served_families) or 'n/a'}",
        f"- **Served models:** {', '.join(u.served_models) or 'n/a'}",
        f"- **Pinned-ID drift events:** {u.drift_events}"
        + (
            " (" + ", ".join(f"{k}={v}" for k, v in u.drift_by_kind.items()) + ")"
            if u.drift_by_kind
            else ""
        ),
    ]
    return "\n".join(lines) + "\n"


def _budget_spend(state: FactoryState) -> int:
    spend = getattr(state.get("budget"), "spend_microusd", 0)
    return spend if isinstance(spend, int) and not isinstance(spend, bool) else 0


def plan_fields(state: FactoryState, resolve: ArtifactResolver | None) -> dict[str, Any]:
    """The manifest fields a later change on this build reads back (``read_base``):
    the stack and, given a resolver, the contracts and the files implementing them."""
    from foundry.base_tree import read_base

    update: dict[str, Any] = {}
    stack = state.get("stack")
    if stack is not None:
        update["stack"] = {k: v for k, v in stack.model_dump(mode="json").items() if v}
    if resolve is None:
        return update
    from foundry.graph.gate_wiring import contract_paths
    from foundry.schemas.architecture import InterfaceContract

    base_dir = state.get("base_dir")
    prior = read_base(base_dir) if base_dir else None
    newest: dict[str, InterfaceContract] = {}
    for a in sorted(state.get("artifacts") or (), key=lambda a: a.seq):
        if a.kind == "interface_contract":
            body = resolve(a)
            if isinstance(body, InterfaceContract):
                newest[body.name] = body
    contracts = [
        ContractRef(name=c.name, kind=c.kind, signature=c.signature) for c in newest.values()
    ]
    files = contract_paths(state, resolve)
    if prior is not None:
        # A change keeps the base's contracts in force beside its own.
        contracts = [
            ContractRef(**c) for c in prior.contracts if c["name"] not in newest
        ] + contracts
        files = tuple(dict.fromkeys((*prior.contracts_files, *files)))
    update["contracts"] = tuple(contracts)
    update["contracts_files"] = tuple(files)
    return update


def spec_conflicts(state: FactoryState) -> tuple[str, ...]:
    """Each ticket's reported spec contradiction, as ``task/<id>: <note>``."""
    from foundry.execution.adapters.tiered import SPEC_CONFLICT_TRIAGE

    return tuple(
        f"{t.run_against}: {t.explanation}"
        for t in sorted(state.get("gate_triage") or (), key=lambda t: t.seq)
        if t.name == SPEC_CONFLICT_TRIAGE
    )


def _with_plan(manifest: BuildManifest, state: FactoryState, rt: AgentRuntime) -> BuildManifest:
    """Record the stack, the contracts and, for a change build, its base."""
    from foundry.base_tree import base_digest, read_base

    update = plan_fields(state, rt.cas.resolve if rt.cas is not None else None)
    base_dir = state.get("base_dir")
    prior = read_base(base_dir) if base_dir else None
    update["spec_conflicts"] = spec_conflicts(state)
    if prior is not None and base_dir:
        update["base"] = BaseRef(
            thread_id=prior.thread_id,
            request=prior.request,
            digest=base_digest(base_dir),
            files=len(prior.files),
        )
    return manifest.model_copy(update=update)


def write_manifest(
    out_dir: str | Path,
    *,
    request: str,
    thread_id: str,
    state: FactoryState,
    rt: AgentRuntime,
    passed: bool,
    tree_sha: str = "",
) -> BuildManifest:
    """Build the manifest for a finished run and write BUILD-MANIFEST.{json,md}."""
    manifest = _with_plan(
        build_manifest(
            request=request,
            thread_id=thread_id,
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            passed=passed,
            phase=str(state.get("phase") or "unknown"),
            artifacts=tuple(state.get("artifacts") or ()),
            gates=tuple(state.get("gates") or ()),
            proxy_manifest=rt.proxy.manifest,
            drift_events=len(rt.proxy.drift_events),
            agent_usage=getattr(rt.adapter, "usage", ()),
            agent_models=tuple(getattr(rt.adapter, "served_models", ())),
            tree_sha=tree_sha,
            estimated_cost_microusd=_budget_spend(state),
        ),
        state,
        rt,
    )
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "BUILD-MANIFEST.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (root / "BUILD-MANIFEST.md").write_text(render_manifest_md(manifest), encoding="utf-8")
    return manifest
