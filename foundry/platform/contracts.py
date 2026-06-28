"""Derive ONE shared contract for a platform plan.

The contract is the seam every service is built against, so the web app and the
API agree on paths and shapes without a round-trip. It is intentionally small and
deterministic: a handful of conventional REST resources inferred from the plan,
emitted as (a) a human-readable spec injected into each service build and (b) an
OpenAPI-ish dict + a few files dropped into ``packages/contracts/`` of the monorepo.

A model-backed planner can later supply a richer contract; downstream only needs
the same two outputs (``contract_spec_text`` and ``contract_files``).
"""

from __future__ import annotations

import json

from foundry.platform.schema import PlatformPlan

# Conventional resources for a multi-tenant SaaS spine. Kept generic on purpose —
# the per-service factory run fleshes out the domain; the contract pins the SEAM.
_RESOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("health", ("GET",)),
    ("auth", ("POST",)),            # login / signup
    ("users", ("GET", "POST")),
    ("tenants", ("GET", "POST")),
    ("items", ("GET", "POST", "PUT", "DELETE")),  # the core domain resource
)


def build_contract(plan: PlatformPlan) -> dict[str, object]:
    """An OpenAPI-ish dict describing the platform's inter-service API surface."""
    paths: dict[str, object] = {}
    item_methods = ("GET", "PUT", "DELETE")
    for resource, methods in _RESOURCES:
        base = "/" + resource
        paths[base] = {m.lower(): {"summary": f"{m} {resource}"} for m in methods}
        has_item = resource not in ("health", "auth") and any(m in item_methods for m in methods)
        if has_item:
            paths[f"{base}/{{id}}"] = {
                m.lower(): {"summary": f"{m} {resource} by id"}
                for m in methods if m in item_methods
            }
    return {
        "openapi": "3.0.0",
        "info": {"title": f"{plan.name} platform API", "version": "0.1.0"},
        "x-services": [s.name for s in plan.services],
        "x-entities": ["User", "Tenant", "Item"],
        "paths": paths,
    }


def contract_spec_text(plan: PlatformPlan, contract: dict[str, object]) -> str:
    """A prose spec of the shared contract, injected into EVERY service build so
    the web client and the API are generated against the same surface."""
    lines = [
        f"# Shared platform contract — {plan.name}",
        "",
        "Every service in this platform MUST agree on this seam. Build YOUR service "
        "to consume/expose exactly these resources; do not invent divergent paths.",
        "",
        "## Services",
    ]
    for s in plan.services:
        dep = f" → calls {', '.join(s.depends_on)}" if s.depends_on else ""
        lines.append(f"- **{s.name}** ({s.role}){dep}")
    lines += ["", "## REST API surface (the api service exposes; the web app consumes)"]
    raw_paths = contract.get("paths", {})
    paths: dict[str, dict[str, object]] = raw_paths if isinstance(raw_paths, dict) else {}
    for path, ops in paths.items():
        methods = ", ".join(sorted(m.upper() for m in ops))
        lines.append(f"- `{methods} {path}`")
    lines += [
        "",
        "## Shared entities (the data layer owns; all services reference)",
        "- `User(id, email, tenant_id, password_hash, created_at)`",
        "- `Tenant(id, name, plan, created_at)`",
        "- `Item(id, tenant_id, title, body, created_at)`  ← the core domain row",
        "",
        "Auth: bearer token from `POST /auth`; every non-health request is tenant-scoped.",
    ]
    return "\n".join(lines)


def contract_files(plan: PlatformPlan, contract: dict[str, object]) -> dict[str, str]:
    """Files written to ``packages/contracts/`` of the stitched monorepo."""
    spec_text = contract_spec_text(plan, contract)
    return {
        "openapi.json": json.dumps(contract, indent=2, sort_keys=True) + "\n",
        "CONTRACT.md": spec_text + "\n",
    }
