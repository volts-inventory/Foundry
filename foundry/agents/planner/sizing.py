"""Per-ticket size ceilings, by where each ticket's tier runs (cloud or local).

A ticket's size is its work files (``files[]`` less tests, package markers,
stack-command files and manifests/config, which the folds add or every ticket
carries, and less templates and static assets, which a session writes from the
code that renders them), its scenarios, and a looser guard on all its files
(work files plus assets) so a ticket of many templates can't balloon either.
Calibrated on the passing bench plans (p4-p6, hybrid27): non-entrypoint cloud
tickets reached 2 work files, 5 in all and 7 scenarios, local ones 3 work files,
4 in all and 8 scenarios; entrypoints (exempt) 2 work files, 12 in all (a route
module and 11 templates) and 12 scenarios. The local all-files guard leaves room
for a whole web layer (p6-hybrid's shop pages came to 18 files in all) in one.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from foundry.agents.implementation.tickets.tasks import tier_for
from foundry.schemas.implementation import FEATURE_DIR, Difficulty, Ticket


@dataclass(frozen=True)
class TicketCeiling:
    files: int
    scenarios: int
    where: str
    total: int  # work files plus templates and static assets


CLOUD_CEILING = TicketCeiling(files=12, scenarios=12, where="cloud", total=32)
LOCAL_CEILING = TicketCeiling(files=4, scenarios=8, where="local", total=20)

Ceilings = Mapping[Difficulty, TicketCeiling]

ALL_CLOUD: Ceilings = dict.fromkeys(Difficulty, CLOUD_CEILING)


def ticket_ceilings(
    local_tiers: Collection[str], ticket_tiers: Mapping[Any, str] | None = None
) -> dict[Difficulty, TicketCeiling]:
    """Each difficulty's ceiling: local when the tier it maps to runs locally."""
    return {
        d: LOCAL_CEILING if tier_for(d, ticket_tiers) in local_tiers else CLOUD_CEILING
        for d in Difficulty
    }


_CONFIG_FILES = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "mypy.ini",
        "pytest.ini",
        "tox.ini",
        "uv.lock",
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "cucumber.json",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
        "pom.xml",
        ".gitignore",
    }
)

_TEST_DIRS = frozenset({"tests", "test", "spec", "specs", "__tests__"})

_TEST_NAME = re.compile(r"^(test_.*|.*_test\.\w+|.*\.(test|spec)\.\w+|conftest\.py)$")

# Templates and static assets by extension: markup, styles and media, not logic. A
# script or module is work wherever it lives (static/game.js, templates/macros.py).
_ASSET_EXT = frozenset(
    {
        ".html",
        ".htm",
        ".jinja",
        ".jinja2",
        ".j2",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".mp3",
        ".wav",
        ".ogg",
    }
)


def is_asset(path: str) -> bool:
    """A template or static asset by its extension: markup, a stylesheet, an image,
    a font or audio."""
    return posixpath.splitext(path)[1].lower() in _ASSET_EXT


def is_scaffold(path: str) -> bool:
    """A package marker (``__init__.py``) or a manifest/config file: project
    scaffolding, not a feature's work."""
    name = posixpath.basename(path)
    return name == "__init__.py" or name in _CONFIG_FILES


def ticket_files(files: Iterable[str], protected: Collection[str] = ()) -> tuple[str, ...]:
    """The files of ``files`` the ticket writes as its own: never a test or step
    module, a package marker, a ``protected`` stack-command file or a manifest.
    BDD material is the plan's top-level feature dir (feature files, cucumber step
    definitions) and ``.feature`` files: a ``src/features/`` source tree is work."""
    out: list[str] = []
    for f in files:
        name = posixpath.basename(f)
        parts = f.split("/")[:-1]
        if (
            f in protected
            or name in _CONFIG_FILES
            or name == "__init__.py"
            or _TEST_NAME.match(name)
            or name.endswith(".feature")
            or parts[:1] == [FEATURE_DIR]
            or any(p in _TEST_DIRS for p in parts)
        ):
            continue
        out.append(f)
    return tuple(out)


def work_files(files: Iterable[str], protected: Collection[str] = ()) -> tuple[str, ...]:
    """The :func:`ticket_files` that are source code, not a template or asset."""
    return tuple(f for f in ticket_files(files, protected) if not is_asset(f))


def over_ceiling(
    ticket: Ticket, ceiling: TicketCeiling, protected: Collection[str] = ()
) -> tuple[int, int, int] | None:
    """(work files, files in all, scenarios) when ``ticket`` is over ``ceiling``,
    else None."""
    total = ticket_files(ticket.files, protected)
    files = sum(1 for f in total if not is_asset(f))
    scenarios = len(ticket.scenario_ids)
    if files > ceiling.files or len(total) > ceiling.total or scenarios > ceiling.scenarios:
        return files, len(total), scenarios
    return None


def size_text(ceilings: Ceilings) -> str:
    """The run's TICKET SIZE rule: each difficulty's ceiling, grouped by where it runs."""
    groups: dict[TicketCeiling, list[str]] = {}
    for d in Difficulty:
        groups.setdefault(ceilings[d], []).append(d.value)
    rows = "; ".join(
        f"{' / '.join(ds)} tickets run on a {c.where} model: at most {c.files} work "
        f"files, {c.total} files in all and {c.scenarios} scenarios each"
        for c, ds in groups.items()
    )
    return (
        "\n\nTICKET SIZE (this run; checked: a ticket over it FAILS the Spec gate): "
        + rows
        + ". Count each ticket's scenarios and work files before you emit it: one "
        "over the ceiling is split by feature area into two tickets that each own "
        "their own files and scenarios (never two tickets writing one file; a module "
        "carrying too many scenarios becomes two modules). The entrypoint ticket is "
        "exempt. Work files are source code: templates "
        "and static assets (HTML/Jinja templates, CSS, images, fonts, audio; a script "
        "or module is work wherever it lives) count only toward files in all, and "
        "neither counts tests, package markers (__init__.py), stack-command files "
        "or manifests/config. It "
        "is a ceiling, not a target; a web layer whose routes and "
        "templates fit stays ONE ticket, never one per page. Every feature ticket "
        "owns at least one scenario."
    )
