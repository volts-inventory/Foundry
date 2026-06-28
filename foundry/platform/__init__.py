"""Platform composition layer — build a MULTI-SERVICE app from one request.

The factory graph (``foundry.cli.run``) is excellent at ONE service / one repo.
A SaaS platform is many cooperating services (web + API + backend + data +
worker). This package adds a thin layer ABOVE the engine that:

  1. PLANS  — decompose a platform request into per-service build requests with
              explicit dependencies (:mod:`foundry.platform.planner`).
  2. CONTRACTS — derive ONE shared contract (API surface + shared types) so the
              services agree at their seams (:mod:`foundry.platform.contracts`).
  3. BUILDS — run each service through the EXISTING factory (parallel, bounded),
              grounding every build in the shared contract
              (:mod:`foundry.platform.orchestrator`).
  4. STITCHES — assemble the per-service trees into ONE monorepo with a root
              README, docker-compose, CI and a manifest
              (:mod:`foundry.platform.stitch`).

It reuses the engine wholesale — it never reaches into the graph. Each service
is exactly as good as a single factory run (real code on a real backend); the
layer owns the BREADTH (all the pieces) and the WIRING (how they fit), not the
per-service depth.
"""

from __future__ import annotations

from foundry.platform.orchestrator import build_platform
from foundry.platform.planner import plan_platform
from foundry.platform.schema import PlatformPlan, PlatformResult, ServiceResult, ServiceSpec

__all__ = [
    "PlatformPlan",
    "PlatformResult",
    "ServiceResult",
    "ServiceSpec",
    "build_platform",
    "plan_platform",
]
