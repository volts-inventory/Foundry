"""Per-ticket size ceilings, by where each ticket's tier runs (cloud or local).

A ticket's size is its work files (``files[]`` less tests, package markers,
stack-command files and manifests/config, which the folds add or every ticket
carries) and its scenarios. Calibrated on the passing bench plans: cloud tickets
reached 12 work files (an entrypoint with 11 templates) and 12 scenarios; local
ones 3 work files and 8 scenarios.
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


CLOUD_CEILING = TicketCeiling(files=12, scenarios=12, where="cloud")
LOCAL_CEILING = TicketCeiling(files=4, scenarios=8, where="local")

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


def work_files(files: Iterable[str], protected: Collection[str] = ()) -> tuple[str, ...]:
    """The files of ``files`` that are the ticket's own work: never a test or step
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


def over_ceiling(
    ticket: Ticket, ceiling: TicketCeiling, protected: Collection[str] = ()
) -> tuple[int, int] | None:
    """(work files, scenarios) when ``ticket`` is over ``ceiling``, else None."""
    files, scenarios = len(work_files(ticket.files, protected)), len(ticket.scenario_ids)
    if files > ceiling.files or scenarios > ceiling.scenarios:
        return files, scenarios
    return None


def size_text(ceilings: Ceilings) -> str:
    """The run's TICKET SIZE rule: each difficulty's ceiling, grouped by where it runs."""
    groups: dict[TicketCeiling, list[str]] = {}
    for d in Difficulty:
        groups.setdefault(ceilings[d], []).append(d.value)
    rows = "; ".join(
        f"{' / '.join(ds)} tickets run on a {c.where} model: at most {c.files} files and "
        f"{c.scenarios} scenarios each"
        for c, ds in groups.items()
    )
    return (
        "\n\nTICKET SIZE (this run; checked: a ticket over it FAILS the Spec gate): "
        + rows
        + ". The entrypoint ticket is exempt. Files count without tests, package markers "
        "(__init__.py), stack-command files and manifests/config."
    )
