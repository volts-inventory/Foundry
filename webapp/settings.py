"""Workshop build settings (plan Phases 4, 4b, 5.1): the saved model mode, level
table, difficulty mapping and local server — the defaults for every new build —
plus validation of a New Project form's advanced fields.

Everything here is a boundary: the browser sends it, so each field is type- and
range-checked before it reaches ``foundry.cli.run``. Single user: one settings
file next to the catalog.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from foundry.modes import (
    _MODEL_ID,
    DEFAULT_FIX_ATTEMPTS,
    LEVEL_TIERS,
    LOCAL_BACKEND,
    MODEL_MODES,
    parse_levels,
    preset_levels,
)
from foundry.proxy.local_transport import (
    DEFAULT_LOCAL_MODEL,
    DEFAULT_LOCAL_URL,
    validate_local_url,
)

DIFFICULTIES = ("trivial", "standard", "complex")
DEFAULT_TICKET_TIERS = {"trivial": "T3", "standard": "T2", "complex": "T1"}
SPEC_MAX_BYTES = 200 * 1024
MAX_FIX_ATTEMPTS = 20
MAX_TIME_CAP_MIN = 7 * 24 * 60
MAX_COST_USD = 100_000
_URL_MAX = 500
SETTINGS_KEYS = ("model_mode", "levels", "ticket_tiers", "local_url", "local_model")


def _cloud_models(backend: str) -> dict[str, str]:
    from foundry.cli import _tier_models

    return _tier_models(backend)


def _backends() -> tuple[str, ...]:
    from foundry.cli import BACKENDS

    return tuple(BACKENDS)


def presets(backend: str, local_url: str, local_model: str) -> dict[str, dict[str, Any]]:
    """Each mode's level table, as the Settings panel prefills it."""
    models = _cloud_models(backend)
    return {
        mode: {t: lvl.as_dict() for t, lvl in preset_levels(
            mode, cloud_backend=backend, cloud_models=models,
            local_url=local_url, local_model=local_model).items()}
        for mode in MODEL_MODES
    }


def defaults(backend: str) -> dict[str, Any]:
    return {
        "model_mode": "cloud",
        "levels": presets(backend, DEFAULT_LOCAL_URL, DEFAULT_LOCAL_MODEL)["cloud"],
        "ticket_tiers": dict(DEFAULT_TICKET_TIERS),
        "local_url": DEFAULT_LOCAL_URL,
        "local_model": DEFAULT_LOCAL_MODEL,
    }


def _local_url(raw: Any) -> str:
    if not isinstance(raw, str) or len(raw) > _URL_MAX:
        raise ValueError("local_url must be an http(s) URL")
    return validate_local_url(raw)


def _model_id(raw: Any, name: str) -> str:
    if not isinstance(raw, str) or not _MODEL_ID.match(raw):
        raise ValueError(f"{name} is not a valid model id")
    return raw


def validate(raw: Any, backend: str) -> dict[str, Any]:
    """A complete, normalized settings dict from user input, or ValueError.
    Keys the input omits keep their defaults."""
    if not isinstance(raw, Mapping):
        raise ValueError("expected a JSON object")
    out = defaults(backend)
    mode = raw.get("model_mode", out["model_mode"])
    if mode not in MODEL_MODES:
        raise ValueError(f"model_mode must be one of {', '.join(MODEL_MODES)}")
    out["model_mode"] = mode
    out["local_url"] = _local_url(raw.get("local_url", out["local_url"]))
    out["local_model"] = _model_id(raw.get("local_model", out["local_model"]), "local_model")
    if "levels" in raw:
        levels = raw["levels"]
        if not isinstance(levels, Mapping) or set(levels) != set(LEVEL_TIERS):
            raise ValueError(f"levels must set exactly {', '.join(LEVEL_TIERS)}")
        # One local server: every local level points at local_url.
        levels = {t: ({k: v for k, v in lv.items() if k != "base_url"}
                      if isinstance(lv, Mapping) else lv) for t, lv in levels.items()}
        table = parse_levels(levels, cloud_backends=_backends(), local_url=out["local_url"])
        out["levels"] = {t: table[t].as_dict() for t in LEVEL_TIERS}
    else:
        out["levels"] = presets(backend, out["local_url"], out["local_model"])[mode]
    if "ticket_tiers" in raw:
        tiers = raw["ticket_tiers"]
        if (not isinstance(tiers, Mapping) or set(tiers) != set(DIFFICULTIES)
                or any(v not in LEVEL_TIERS for v in tiers.values())):
            raise ValueError("ticket_tiers must map trivial, standard and complex to T1-T3")
        out["ticket_tiers"] = {d: str(tiers[d]) for d in DIFFICULTIES}
    return out


def load(path: Path, backend: str) -> dict[str, Any]:
    """The saved settings; defaults when none are saved or the file is unusable."""
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")), backend)
    except (OSError, ValueError):
        return defaults(backend)


def save(path: Path, settings: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(settings), fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def client_view(settings: Mapping[str, Any], backend: str) -> dict[str, Any]:
    """GET /api/settings: the saved settings plus what the panel and form offer."""
    from foundry.cli import SCOPES
    from foundry.gates.profiles import PROFILES

    return {
        "settings": dict(settings),
        "presets": presets(backend, settings["local_url"], settings["local_model"]),
        "backend": backend,
        "backends": [*_backends(), LOCAL_BACKEND],
        "profiles": sorted(PROFILES),
        "scopes": list(SCOPES),
        "fix_defaults": dict(DEFAULT_FIX_ATTEMPTS),
        "spec_max_bytes": SPEC_MAX_BYTES,
    }


def optional_number(raw: Any, name: str) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be a number")
    try:
        n = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(n):
        raise ValueError(f"{name} must be a number")
    return n


def parse_build_options(
    body: Mapping[str, Any], saved: Mapping[str, Any], backend: str
) -> dict[str, Any]:
    """The New Project form's model settings + advanced fields, validated.

    Returns ``run_kwargs`` (passed to ``run`` as-is), ``view`` (what the floor's
    HUD shows, also enough to rebuild ``run_kwargs`` after a restart),
    ``max_fix_attempts``, ``profile`` and ``ticket_tiers`` (the Project's own
    fields) and ``spec`` (text or None). Raises ValueError."""
    overrides = {k: body[k] for k in SETTINGS_KEYS if k in body}
    eff = validate({**saved, **overrides}, backend)
    mode = eff["model_mode"]

    fix_raw = body.get("max_fix_attempts")
    if fix_raw is None or fix_raw == "":
        max_fix = DEFAULT_FIX_ATTEMPTS[mode]
    else:
        if isinstance(fix_raw, bool) or not (
            (isinstance(fix_raw, int) and fix_raw >= 0)
            or (isinstance(fix_raw, str) and fix_raw.strip().isdigit())
        ):
            raise ValueError("max_fix_attempts must be a whole number, 0 or more")
        max_fix = int(fix_raw)
        if max_fix > MAX_FIX_ATTEMPTS:
            raise ValueError(f"max_fix_attempts must be 0 to {MAX_FIX_ATTEMPTS}")

    time_cap = optional_number(body.get("time_cap_minutes"), "time_cap_minutes")
    if time_cap is not None and not 0 < time_cap <= MAX_TIME_CAP_MIN:
        raise ValueError(f"time_cap_minutes must be more than 0 and at most {MAX_TIME_CAP_MIN}")

    cost = optional_number(body.get("cost_cap_usd"), "cost_cap_usd")
    if cost is not None and not 0 < cost <= MAX_COST_USD:
        raise ValueError(f"cost_cap_usd must be more than 0 and at most {MAX_COST_USD}")
    cost_micro = round(cost * 1_000_000) if cost is not None else None

    from foundry.cli import SCOPES
    from foundry.gates.profiles import PROFILES

    profile = body.get("profile") or "auto"
    if profile != "auto" and profile not in PROFILES:
        raise ValueError(f"profile must be auto or one of {', '.join(sorted(PROFILES))}")
    scope = body.get("scope") or "auto"
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")

    spec = body.get("spec")
    if spec is not None and not isinstance(spec, str):
        raise ValueError("spec must be text")
    if spec is not None and len(spec.encode("utf-8")) > SPEC_MAX_BYTES:
        raise ValueError(f"spec is larger than {SPEC_MAX_BYTES // 1024} KB")
    spec = spec if spec and spec.strip() else None
    spec_name = body.get("spec_name")
    spec_name = (os.path.basename(spec_name)[:120] if isinstance(spec_name, str) else "") or ""

    preset = presets(backend, eff["local_url"], eff["local_model"])[mode]
    custom = eff["levels"] != preset
    view = {
        "model_mode": mode,
        "levels": eff["levels"],
        "levels_custom": custom,
        "local_url": eff["local_url"],
        "local_model": eff["local_model"],
        "time_cap_minutes": time_cap,
        "scope": scope,
        "cost_cap_microusd": cost_micro,
        "spec": spec is not None,
        "spec_name": spec_name if spec is not None else "",
    }
    return {
        "run_kwargs": run_kwargs(view),
        "view": view,
        "max_fix_attempts": max_fix,
        "profile": profile,
        "ticket_tiers": eff["ticket_tiers"],
        "spec": spec,
    }


def run_kwargs(view: Mapping[str, Any]) -> dict[str, Any]:
    """``run`` keyword arguments from a floor's view. An unedited preset is sent
    as the mode alone, so a Cloud build keeps today's single-backend routing."""
    return {
        "model_mode": view.get("model_mode") or "cloud",
        "levels": view.get("levels") if view.get("levels_custom") else None,
        "local_url": view.get("local_url"),
        "local_model": view.get("local_model"),
        "time_cap_minutes": view.get("time_cap_minutes"),
        "scope": view.get("scope") or "auto",
        "cost_cap_microusd": view.get("cost_cap_microusd"),
    }


def write_spec(inputs: Path, text: str) -> Path:
    """The spec as a server-owned file. ``run(spec=)`` treats a string naming an
    existing path as that path, so pasted text is never handed over raw — it
    could name a file on this host."""
    inputs.mkdir(parents=True, exist_ok=True)
    path = inputs / "spec.md"
    path.write_text(text, encoding="utf-8")
    return path


def write_tree(dest: Path, tree: Mapping[str, bytes]) -> None:
    """Write a build's materialized tree under ``dest`` (fail-closed on escape)."""
    root = dest.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in tree.items():
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or target == root:
            raise ValueError(f"tree path escapes the base dir: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def check_local(body: Any) -> tuple[int, dict[str, Any]]:
    """POST /api/settings/local-check: is the local server up, what does it serve?
    The URL is validated first; the probe is ``GET <url>/models`` server-side."""
    if not isinstance(body, Mapping):
        return 400, {"error": "expected a JSON object"}
    try:
        url = _local_url(body.get("url"))
        model = body.get("model") or None
        if model is not None:
            model = _model_id(model, "model")
    except ValueError as exc:
        return 400, {"error": str(exc)}
    from foundry.proxy.local_transport import check_local_server

    return 200, check_local_server(url, model=model).as_dict()
