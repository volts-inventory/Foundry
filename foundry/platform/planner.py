"""Decompose a one-line platform request into per-service build requests.

This is the DETERMINISTIC planner: keyword heuristics over the request, no model
call — so it is keyless, fast and unit-testable, and it gives the composition
layer a stable spine. A model-backed planner can later replace :func:`plan_platform`
behind the same signature (return a :class:`PlatformPlan`); nothing downstream
cares how the plan was produced.

It adapts to the request shape:

* a data-backed site/app ("a forum site that stores users") → the SaaS spine:
  web → backend(REST API + auth + domain) → data, plus a worker when async work
  is implied;
* a self-contained client ("a NES-style game", "a markdown editor") → just a `web`
  service with NO backend — everything runs in the browser.
"""

from __future__ import annotations

import re

from foundry.platform.schema import PlatformPlan, ServiceSpec

# role -> signal words. A role is included when ANY of its signals appear; the
# web/api/data spine is additionally forced for data-backed apps (see below).
_SIGNALS: dict[str, tuple[str, ...]] = {
    "web": ("web", "website", "frontend", "front-end", "react", "vue", "svelte",
            "ui", "dashboard", "spa", "next.js", "page", "site", "app"),
    "data": ("database", "db", "postgres", "mysql", "sqlite", "persistence",
             "schema", "migration"),
    "worker": ("worker", "queue", "background", "job", "cron", "async", "email",
               "notification", "pipeline"),
    # the BACKEND owns the whole server tier: the REST API surface AND the domain
    # logic (auth, accounts, tenancy) behind it. (There is no separate "api" role —
    # "backend" covers the API and beyond; the data layer stays its own service.)
    "backend": ("api", "rest", "graphql", "endpoint", "service", "backend", "server",
                "auth", "authentication", "login", "users", "accounts", "billing",
                "payments", "tenant", "multi-tenant", "subscription"),
}

# Words that mean "this needs a server + persistence" — they force the full spine
# even for a request that reads like a single app ("a game WITH a leaderboard").
_BACKEND_HINTS: tuple[str, ...] = (
    "store", "stores", "stored", "save", "saved", "persist", "account", "accounts",
    "login", "sign in", "sign-up", "signup", "user", "users", "forum", "forums",
    "post", "posts", "comment", "blog", "database", "multiplayer", "leaderboard",
    "online", "server", "api", "backend", "tenant", "subscription", "payment",
)

# Words that mean "this is a self-contained client app/game" — no backend unless a
# backend hint also appears.
_GAME_HINTS: tuple[str, ...] = (
    "game", "nes", "snes", "arcade", "retro", "tetris", "snake", "pong", "pixel",
    "platformer", "shooter", "roguelike", "puzzle", "8-bit", "8 bit", "sprite",
)

_TEMPLATES: dict[str, str] = {
    "web_api": ("the {kind} WEB FRONTEND: a {fw} single-page app that calls the platform "
                "REST API defined in the shared contract — pages, components, API client, "
                "and auth-aware routing. Talks ONLY to the backend service over HTTP."),
    "web_standalone": ("the {kind} as a self-contained {fw} web app: all UI, state and "
                       "logic run in the browser with NO backend — fully working, not a "
                       "skeleton. Include the complete feature logic and local persistence."),
    "game": ("the {kind} as a complete, fully-playable game in {engine}: a real game loop "
             "(update + draw), input handling, the world/tilemap and entities, "
             "collision/physics, game states (title / play / pause / game-over), and "
             "save/load. Implement the ACTUAL gameplay — not a skeleton or a stub loop."),
    "game_online": ("the {kind} game CLIENT in {engine}: the full local game (loop, input, "
                    "world, entities, save/load) PLUS a thin client to the platform backend's "
                    "REST API for online features (accounts, leaderboards, sync) per the "
                    "shared contract."),
    # the backend is the WHOLE server tier: REST API surface + the domain logic behind it.
    "backend": ("the {kind} BACK-END service — the whole server tier: implement the REST "
                "API (every path in the shared OpenAPI contract: routing, request validation, "
                "JSON responses, auth middleware) TOGETHER WITH the domain logic behind it — "
                "user accounts, authentication (signup/login/session or JWT), and multi-tenant "
                "access control. Persists through the data layer; exposes the contract the web "
                "app consumes."),
    "data": ("the {kind} DATA LAYER: the relational schema, migrations, and a typed "
             "data-access module for the entities named in the shared contract "
             "(users, tenants, and the core domain rows). No HTTP — a library the "
             "back-end imports."),
    "worker": ("the {kind} background WORKER: a queue consumer that runs async jobs "
               "(emails/notifications and scheduled tasks) off the shared data layer. "
               "No HTTP surface; driven by a job queue."),
}

# Conventional dependency edges within the spine.
_EDGES: dict[str, tuple[str, ...]] = {
    "web": ("backend",), "backend": ("data",),
    "worker": ("data",), "data": (), "game": ("backend",),
}

# Appended to EVERY service request: push the engine for technical depth — a
# near-finished implementation, no stubs — while explicitly excluding business,
# marketing, legal and go-to-market concerns (technical scope only).
_DEPTH_DIRECTIVE = (
    " Make it technically near-complete: implement the FULL feature set with real "
    "working logic, input validation, error handling, edge cases, a sensible data "
    "model, and tests — no placeholders, TODOs, or stubbed functions. Scope is "
    "TECHNICAL ONLY: exclude business/marketing/legal/go-to-market concerns."
)


def _engine_hint(low: str) -> str:
    """The game engine/language to build in, inferred from the request.

    Defaults to Lua/LÖVE — a great fit for the retro/2D games these requests
    describe — unless another engine is named.
    """
    if any(w in low for w in ("godot", "gdscript")):
        return "GDScript on the Godot engine"
    if "unity" in low or "c#" in low:
        return "C# on the Unity engine"
    if any(w in low for w in ("pygame", "python game")):
        return "Python with pygame"
    if any(w in low for w in ("phaser", "html5", "canvas", "browser game", "javascript")):
        return "JavaScript with an HTML5 canvas"
    # lua / love2d / löve / stardew-like / unspecified retro game
    return "Lua with the LÖVE (Love2D) framework"


def _framework_hint(request: str) -> str:
    low = request.lower()
    for fw in ("next.js", "nextjs", "react", "vue", "svelte", "angular", "solid"):
        if fw in low:
            return "Next.js" if fw in ("next.js", "nextjs") else fw.capitalize()
    return "React"


def _slug(request: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", request.lower()).strip("-")
    return "-".join([p for p in s.split("-") if p][:5]) or "platform"


def _is_game(low: str) -> bool:
    return any(w in low for w in _GAME_HINTS)


def _needs_backend(low: str, hits: set[str]) -> bool:
    spine = bool(hits & {"data", "backend", "worker"})
    return spine or any(w in low for w in _BACKEND_HINTS)


def _selected_roles(request: str) -> list[str]:
    low = request.lower()
    hits = {role for role, words in _SIGNALS.items() if any(w in low for w in words)}
    needs_backend = _needs_backend(low, hits)
    if _is_game(low):
        # A game is its own engine (Lua/LÖVE, pygame…), NOT a web tier. Add a
        # backend+data tier only when the request implies online features.
        return ["game", "backend", "data"] if needs_backend else ["game"]
    if not needs_backend and "data" not in hits and hits <= {"web"}:
        return ["web"]                       # plain self-contained web app/tool
    hits |= {"web", "backend", "data"}       # data-backed app: web → backend → data
    order = ["web", "backend", "data", "worker"]
    return [r for r in order if r in hits]


def _web_template_key(present: set[str]) -> str:
    """The web-frontend template key. A game is never built under the ``web`` role
    (``_selected_roles`` routes games to the ``game``/``game_online`` template), so
    the only choice here is API-coupled vs. self-contained."""
    return "web_api" if "backend" in present else "web_standalone"


def plan_platform(request: str, *, name: str | None = None,
                  only: list[str] | None = None) -> PlatformPlan:
    """Decompose ``request`` into a :class:`PlatformPlan`.

    ``only`` restricts the build to a subset of roles (e.g. ``["web", "backend"]``);
    otherwise the planner infers the right shape — full spine for a data-backed
    app, a single ``web`` service for a self-contained client/game.
    """
    kind = request.strip().rstrip(".")
    low = request.lower()
    roles = _selected_roles(request)
    if only:
        want = {r.strip().lower() for r in only}
        roles = [r for r in roles if r in want] or ["web"]

    fw = _framework_hint(request)
    engine = _engine_hint(low)
    present = set(roles)
    services: list[ServiceSpec] = []
    for role in roles:
        if role == "web":
            body = _TEMPLATES[_web_template_key(present)].format(kind=kind, fw=fw)
        elif role == "game":
            key = "game_online" if "backend" in present else "game"
            body = _TEMPLATES[key].format(kind=kind, engine=engine)
        else:
            body = _TEMPLATES[role].format(kind=kind, fw=fw)
        deps = tuple(d for d in _EDGES[role] if d in present)
        request_text = f"Build {body}{_DEPTH_DIRECTIVE}"
        services.append(ServiceSpec(name=role, role=role, request=request_text, depends_on=deps))

    return PlatformPlan(request=request, name=(name or _slug(request)), services=tuple(services))
