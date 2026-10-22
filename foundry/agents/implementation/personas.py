"""Area-expert engineer personas.

Each canonical area (``schemas/architecture.CANONICAL_AREAS``) maps to an expert
engineer persona — a short role preamble injected into the adapter prompt for a
task in that area, so a frontend task is written by a frontend specialist, a data
task by a data specialist, and so on. This is the IMPLEMENTATION-layer counterpart
to the architect's per-area ownership split: the org already routes work by area;
the persona decides WHO shows up for it.

Ticket-driven tasks pick the persona by the ticket's declared specialty
(frontend/backend/data/test) through the same :func:`persona_for` lookup.

Personas are advisory PROMPT guidance only — they never relax a pinned constraint
or a gate (those stay verbatim + first). An unknown/empty area yields no persona
(``""``), so a task with no area is byte-identical to the pre-persona prompt.
"""

from __future__ import annotations

# area -> expert engineer persona (role preamble). Kept concise and concrete:
# each names the discipline and the things that area's code must get right.
AREA_PERSONAS: dict[str, str] = {
    "frontend": (
        "You are a SENIOR FRONTEND ENGINEER. Expertise: component architecture, "
        "state management, accessibility (semantic HTML / ARIA), responsive layout, "
        "and a polished UX. Write idiomatic, typed components; handle the loading, "
        "empty and error states; keep the API client thin and typed against the "
        "shared contract."
    ),
    "backend": (
        "You are a SENIOR BACKEND ENGINEER. Expertise: clean HTTP/service APIs, "
        "request validation, authentication/authorization, and clear domain logic. "
        "Write cohesive modules, validate every input, return precise status codes, "
        "keep side effects explicit, and never trust client data."
    ),
    "data": (
        "You are a SENIOR DATA ENGINEER. Expertise: relational schema design, "
        "migrations, indexing, and safe typed data access. Model entities and their "
        "relationships precisely, enforce constraints at the schema, and use "
        "parameterized queries — never string-built SQL."
    ),
    "infra": (
        "You are a SENIOR INFRASTRUCTURE / PLATFORM ENGINEER. Expertise: build and "
        "deploy, containerization, configuration, and operability. Make the service "
        "reproducible, configurable via environment, and observable (health, logs)."
    ),
    "security": (
        "You are a SENIOR SECURITY ENGINEER. Expertise: authn/authz, input "
        "sanitization, secret handling, and least privilege. Treat all input as "
        "hostile; avoid injection, data leakage, and unsafe defaults."
    ),
    "test": (
        "You are a SENIOR TEST / QA ENGINEER. Expertise: behaviour-driven testing "
        "with pytest-bdd. Write the step definitions that make this ticket's "
        "scenarios executable: one module under tests/steps/ that binds each "
        "scenario from the feature file named in the task via scenarios(...) (path "
        "relative to the module unless the project sets bdd_features_base_dir) and "
        "implements every Given/When/Then step with "
        "@given/@when/@then and parsers, sharing state through fixtures. Drive the "
        "real code through its public interface; assert observable behaviour, never "
        "internals. NEVER edit .feature files — they are the spec; if a step cannot "
        "pass, report it as TRIAGE instead of weakening the assertion."
    ),
}


def persona_for(area: str | None) -> str:
    """The expert persona for ``area`` (case-insensitive), or ``""`` if none."""
    return AREA_PERSONAS.get((area or "").strip().lower(), "")
