"""Model modes — Cloud · Hybrid · Local (workshop plan, Phase 4b).

A run's model levels are a table ``{T1, T2, T3} -> Level(backend, model[, base_url])``.
The planner still rates each ticket's difficulty, which picks its tier; the table
picks where that tier runs. A mode is a preset of the table:

========  ===============  ===============  ===========
level     cloud            hybrid           local
========  ===============  ===============  ===========
T1        cloud            cloud            local
T2        cloud            local            local
T3        cloud            local            local
========  ===============  ===============  ===========

``cloud`` is today's mapping exactly (the run builds no table at all). The proxy
routes every call by its tier to that level's transport and stays the only
credential holder: a local level is reached with no credential at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from foundry.proxy.lineage import (
    DEFAULT_LINEAGE_ENTRIES,
    LOCAL_LINEAGE_ENTRIES,
    OPENAI_LINEAGE_ENTRIES,
    LineageMap,
)
from foundry.proxy.local_transport import (
    DEFAULT_LOCAL_MODEL,
    DEFAULT_LOCAL_TIMEOUT_S,
    DEFAULT_LOCAL_URL,
    LocalOpenAITransport,
    validate_local_url,
)
from foundry.proxy.model_provider_proxy import ModelProviderProxy, ProviderTransport

__all__ = [
    "DEFAULT_FIX_ATTEMPTS",
    "LEVEL_TIERS",
    "LOCAL_BACKEND",
    "MODEL_MODES",
    "Level",
    "build_level_proxy",
    "level_lineage",
    "local_tiers",
    "parse_levels",
    "preset_levels",
    "resolve_levels",
]

LOCAL_BACKEND = "local"
MODEL_MODES = ("cloud", "hybrid", "local")
LEVEL_TIERS = ("T1", "T2", "T3")
# Repair rounds per mode. Extra rounds plateau fast (fix loops stall after ~2-3 passes),
# so local gets no more than cloud despite the weaker model.
DEFAULT_FIX_ATTEMPTS: dict[str, int] = {"cloud": 3, "hybrid": 3, "local": 3}

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")


@dataclass(frozen=True)
class Level:
    """Where one tier runs: a cloud backend + model id, or the local server."""

    backend: str
    model: str
    base_url: str | None = None

    @property
    def local(self) -> bool:
        return self.backend == LOCAL_BACKEND

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"backend": self.backend, "model": self.model}
        if self.base_url:
            out["base_url"] = self.base_url
        return out


def preset_levels(
    mode: str,
    *,
    cloud_backend: str,
    cloud_models: Mapping[str, str],
    local_url: str = DEFAULT_LOCAL_URL,
    local_model: str = DEFAULT_LOCAL_MODEL,
) -> dict[str, Level]:
    """The level table a mode stands for."""
    if mode not in MODEL_MODES:
        raise ValueError(f"unknown model mode {mode!r}; choose one of {', '.join(MODEL_MODES)}")
    local_tiers = {"cloud": (), "hybrid": ("T2", "T3"), "local": LEVEL_TIERS}[mode]
    url = validate_local_url(local_url)
    return {
        tier: (Level(LOCAL_BACKEND, local_model, url) if tier in local_tiers
               else Level(cloud_backend, cloud_models[tier]))
        for tier in LEVEL_TIERS
    }


def parse_levels(
    raw: Mapping[str, Any], *, cloud_backends: tuple[str, ...], local_url: str = DEFAULT_LOCAL_URL
) -> dict[str, Level]:
    """Validate a level table from the CLI/UI (a boundary: backend names, model ids
    and URLs are user input). Each value is a :class:`Level` or a mapping with
    ``backend``, ``model`` and, for a local level, an optional ``base_url``."""
    out: dict[str, Level] = {}
    for tier, value in raw.items():
        if tier not in LEVEL_TIERS:
            raise ValueError(f"levels: unknown tier {tier!r}; choose from {', '.join(LEVEL_TIERS)}")
        if isinstance(value, Level):
            backend, model, base_url = value.backend, value.model, value.base_url
        elif isinstance(value, Mapping):
            backend = str(value.get("backend") or "")
            model = str(value.get("model") or "")
            base_url = value.get("base_url")
        else:
            raise ValueError(f"levels: {tier} must be a mapping with backend and model")
        if backend != LOCAL_BACKEND and backend not in cloud_backends:
            raise ValueError(f"levels: {tier} has unknown backend {backend!r}; choose from "
                             f"{', '.join((*cloud_backends, LOCAL_BACKEND))}")
        if not _MODEL_ID.match(model):
            raise ValueError(f"levels: {tier} has an invalid model id {model!r}")
        if backend == LOCAL_BACKEND:
            out[tier] = Level(backend, model, validate_local_url(str(base_url or local_url)))
        else:
            out[tier] = Level(backend, model)
    return out


def resolve_levels(
    *,
    model_mode: str | None,
    levels: Mapping[str, Any] | None,
    backend: str,
    cloud_models: Mapping[str, str],
    cloud_backends: tuple[str, ...],
    local_url: str | None = None,
    local_model: str | None = None,
) -> dict[str, Level] | None:
    """The run's level table, or None for today's single-backend routing.

    An explicit ``levels`` table wins (tiers it omits run on ``backend``); else
    the ``model_mode`` preset; ``cloud`` (or no mode) is None."""
    url = local_url or DEFAULT_LOCAL_URL
    if levels:
        table = parse_levels(levels, cloud_backends=cloud_backends, local_url=url)
        return {t: table.get(t) or Level(backend, cloud_models[t]) for t in LEVEL_TIERS}
    if model_mode is not None and model_mode not in MODEL_MODES:
        raise ValueError(
            f"unknown model mode {model_mode!r}; choose one of {', '.join(MODEL_MODES)}"
        )
    if model_mode is None or model_mode == "cloud":
        return None
    model = local_model or DEFAULT_LOCAL_MODEL
    if not _MODEL_ID.match(model):
        raise ValueError(f"invalid local model id {model!r}")
    return preset_levels(model_mode, cloud_backend=backend, cloud_models=cloud_models,
                         local_url=url, local_model=model)


def local_tiers(levels: Mapping[str, Level]) -> frozenset[str]:
    return frozenset(t for t, lvl in levels.items() if lvl.local)


def level_lineage(levels: Mapping[str, Level]) -> LineageMap:
    """Curated lineage for every backend the table can serve. A local model id no
    curated pattern knows is pinned to its own family (``local:<id>``): the
    operator named it, so it is attested as exactly that model, never guessed
    into a known lineage."""
    curated = LineageMap((*LOCAL_LINEAGE_ENTRIES, *OPENAI_LINEAGE_ENTRIES,
                          *DEFAULT_LINEAGE_ENTRIES))
    pins: list[tuple[str, frozenset[str]]] = []
    for lvl in levels.values():
        if not lvl.local:
            continue
        try:
            curated.resolve(lvl.model)
        except LookupError:
            pins.append((lvl.model, frozenset({f"local:{lvl.model}"})))
    return LineageMap((*pins, *curated.entries))


def build_level_proxy(
    levels: Mapping[str, Level],
    *,
    cloud_transport: Callable[[str], ProviderTransport],
    local_transport: Callable[[str], ProviderTransport] | None = None,
    **proxy_kwargs: Any,
) -> ModelProviderProxy:
    """One proxy that routes each tier to its level's transport. Transports are
    shared between tiers on the same cloud backend / the same local server, so a
    local server's schema-support probe happens once per run."""
    def _local(url: str) -> ProviderTransport:
        return LocalOpenAITransport(url, timeout_s=DEFAULT_LOCAL_TIMEOUT_S)

    make_local = local_transport or _local
    built: dict[tuple[str, str], ProviderTransport] = {}
    tier_transports: dict[str, ProviderTransport] = {}
    for tier, lvl in levels.items():
        key = (LOCAL_BACKEND, lvl.base_url or "") if lvl.local else (lvl.backend, "")
        if key not in built:
            built[key] = make_local(key[1]) if lvl.local else cloud_transport(lvl.backend)
        tier_transports[tier] = built[key]
    return ModelProviderProxy(
        transport=tier_transports["T1"],
        lineage_map=level_lineage(levels),
        tier_models={t: lvl.model for t, lvl in levels.items()},
        tier_transports=tier_transports,
        local_tiers=local_tiers(levels),
        **proxy_kwargs,
    )
