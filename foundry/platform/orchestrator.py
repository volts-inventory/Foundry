"""Run a :class:`PlatformPlan`'s services through the EXISTING factory.

Each service is one ``foundry.cli.run`` — the same engine a single repo uses — so
on a real backend (``claude-cli`` / ``anthropic``) each service is built with full
working logic and verified by the real gates. The ``mock`` backend writes skeletons
only; use it to exercise this layer's plumbing, not to produce shippable code.

Services build CONCURRENTLY (bounded by ``max_parallel``), each grounded in the one
shared contract so their seams line up. Per-service durable state is isolated under
a per-service ``state_dir`` + ``thread_id`` so builds never collide.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from foundry.cli import run as run_service
from foundry.platform.contracts import build_contract, contract_files, contract_spec_text
from foundry.platform.planner import plan_platform
from foundry.platform.schema import (
    PlatformPlan,
    PlatformResult,
    ServiceResult,
    ServiceSpec,
)
from foundry.platform.stitch import stitch_monorepo

# Default fan-out across services. A platform is a handful of services, so a small
# cap is plenty; raise it if you plan many. Each service ALSO parallelizes its own
# tasks internally (cli.run's max_slots), so total concurrency is the product.
DEFAULT_MAX_PARALLEL = 4


# Appended to a service request on DEEPENING passes (2..N): grow the existing tree
# toward completeness instead of re-rolling it greenfield.
_DEEPEN_DIRECTIVE = (
    " You are given the EXISTING codebase for this service as the starting tree. EXTEND it: "
    "add the missing features, edge cases, error handling, and tests to bring it to a "
    "near-complete state. Build on what's there — do not rewrite it from scratch."
)


def _build_one(
    svc: ServiceSpec,
    *,
    plan_name: str,
    backend: str,
    mode: str,
    spec_text: str,
    work_dir: str,
    max_slots: int,
    cost_cap_microusd: int | None,
    passes: int = 1,
) -> ServiceResult:
    """Build a service into ``work_dir`` and project the result.

    ``passes`` > 1 runs deepening iterations: each pass rebuilds the service ON TOP
    of the previous pass's tree (``base_dir``), layering depth the engine's single
    planning pass can't reach in one shot. The LAST pass's tree is the result.
    """
    base: str | None = None
    result = None
    for p in range(1, max(1, passes) + 1):
        svc_out = os.path.join(work_dir, svc.name if p == passes else f"{svc.name}.p{p}")
        state_dir = os.path.join(work_dir, ".state", svc.name, f"p{p}")
        os.makedirs(state_dir, exist_ok=True)
        request = svc.request + (_DEEPEN_DIRECTIVE if p > 1 else "")
        try:
            result = run_service(
                request,
                backend=backend,
                mode=mode,
                spec=spec_text or None,
                thread_id=f"{plan_name}-{svc.name}-p{p}",
                state_dir=state_dir,
                output_dir=svc_out,
                docs_dir=os.path.join(svc_out, "docs"),
                max_slots=max_slots,
                cost_cap_microusd=cost_cap_microusd,
                base_dir=base,                       # build on the prior pass's tree
            )
        except Exception as exc:  # a service failing to build must not sink the platform
            return ServiceResult(
                name=svc.name, role=svc.role, passed=False, phase=f"error: {exc}",
                source_root=base, is_draft=bool(base), gates={},
            )
        out = result.output_root or result.draft_root
        if not out:
            break                                    # nothing produced — stop deepening
        base = out                                   # next pass extends this tree

    if result is None:
        return ServiceResult(name=svc.name, role=svc.role, passed=False, phase="no output",
                             source_root=None, is_draft=False, gates={})
    source = result.output_root or result.draft_root
    return ServiceResult(
        name=svc.name,
        role=svc.role,
        passed=bool(result.passed),
        phase=result.phase,
        source_root=source,
        is_draft=bool(result.draft_root and not result.output_root),
        gates={k: getattr(v, "status", str(v)) for k, v in result.gate_statuses.items()},
    )


def build_platform(
    request: str | PlatformPlan,
    *,
    out_dir: str,
    backend: str = "mock",
    mode: str = "assistant",
    name: str | None = None,
    only: list[str] | None = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    max_slots_per_service: int = 6,
    cost_cap_microusd: int | None = None,
    passes: int = 1,
    work_dir: str | None = None,
) -> PlatformResult:
    """Plan, build every service, and stitch the monorepo at ``out_dir``.

    ``request`` may be a raw string (planned here) or a ready :class:`PlatformPlan`.
    Returns a :class:`PlatformResult`; ``out_dir`` holds the stitched monorepo.
    """
    plan = request if isinstance(request, PlatformPlan) else plan_platform(
        request, name=name, only=only,
    )
    contract = build_contract(plan)
    plan = PlatformPlan(request=plan.request, services=plan.services,
                        name=plan.name, contract=contract)
    spec_text = contract_spec_text(plan, contract)

    staging = work_dir or os.path.join(out_dir, ".platform-build")
    os.makedirs(staging, exist_ok=True)

    results: list[ServiceResult] = []
    workers = max(1, min(max_parallel, len(plan.services)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _build_one, svc,
                plan_name=plan.name, backend=backend, mode=mode, spec_text=spec_text,
                work_dir=staging, max_slots=max_slots_per_service,
                cost_cap_microusd=cost_cap_microusd, passes=passes,
            ): svc
            for svc in plan.services
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    # Stable order: plan order, not completion order.
    order = {s.name: i for i, s in enumerate(plan.services)}
    results.sort(key=lambda r: order.get(r.name, 1_000))

    stitch_monorepo(plan, results, contract_files(plan, contract), out_dir)

    passed = bool(results) and all(r.passed for r in results)
    return PlatformResult(plan=plan, services=tuple(results), out_dir=out_dir, passed=passed)
