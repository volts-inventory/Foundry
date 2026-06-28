"""Data shapes for the platform composition layer.

Pure dataclasses — no engine imports, no I/O — so the planner can be tested in
isolation and a plan can be serialized to/from JSON (the manifest, the webapp).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

# The service ROLES the planner can emit. web/api/backend/data/worker are the
# conventional SaaS tiers; `game` is a self-contained client game in its own
# engine (Lua/LÖVE, pygame, HTML5 canvas…) that may optionally talk to an api.
# Roles drive request templating, the monorepo layout and the generated compose.
ROLES: tuple[str, ...] = ("web", "api", "backend", "data", "worker", "game")


@dataclass(frozen=True)
class ServiceSpec:
    """One buildable service within a platform.

    ``request`` is the one-line request handed to ``foundry.cli.run`` for THIS
    service — the planner specializes the platform request per role. ``depends_on``
    names sibling services it calls (web→api, api→data); it orders nothing on its
    own (services build in parallel) but is recorded for the contract + compose.
    """

    name: str
    role: str
    request: str
    depends_on: tuple[str, ...] = ()
    # Relative path of this service inside the stitched monorepo.
    path: str = ""

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown service role {self.role!r}; expected one of {ROLES}")
        if not self.path:
            object.__setattr__(self, "path", f"services/{self.name}")


@dataclass(frozen=True)
class PlatformPlan:
    """The decomposition of a platform request into services + a shared contract.

    ``contract`` is the inter-service agreement (an OpenAPI-ish surface + shared
    type names) the orchestrator injects into every service build as a spec, so
    the web app and the API are built against the SAME shape.
    """

    request: str
    services: tuple[ServiceSpec, ...]
    name: str = "platform"
    contract: dict[str, object] = field(default_factory=dict)

    def service(self, name: str) -> ServiceSpec | None:
        return next((s for s in self.services if s.name == name), None)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


@dataclass(frozen=True)
class ServiceResult:
    """Outcome of building one service (a thin projection of cli.RunResult)."""

    name: str
    role: str
    passed: bool
    phase: str
    # Where this service's tree landed BEFORE stitching (output_root on PASS, else
    # the best-effort draft_root on a halt). None when the build produced nothing.
    source_root: str | None
    is_draft: bool = False
    gates: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlatformResult:
    """Outcome of a whole platform build."""

    plan: PlatformPlan
    services: tuple[ServiceResult, ...]
    out_dir: str
    # True only when EVERY service passed its gates (a draft anywhere => False).
    passed: bool

    @property
    def built(self) -> int:
        return sum(1 for s in self.services if s.source_root)

    def summary(self) -> str:
        lines = [
            f"platform:  {self.plan.name}",
            f"request:   {self.plan.request!r}",
            f"services:  {len(self.services)}  ({self.built} built)",
            f"result:    {'PASS' if self.passed else 'PARTIAL'}",
            f"monorepo:  {self.out_dir}",
            "",
        ]
        for s in self.services:
            mark = "✓" if s.passed else ("◐" if s.source_root else "✗")
            note = " (draft)" if s.is_draft else ("" if s.source_root else " (no output)")
            lines.append(f"  {mark} {s.name:<14} {s.role:<8} {s.phase}{note}")
        return "\n".join(lines)
