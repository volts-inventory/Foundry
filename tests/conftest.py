"""Shared pytest config.

Auto-marks the whole-pipeline end-to-end factory tests as ``slow`` (each
compiles + runs the full graph, dominating wall time) so a fast inner-loop run
is ``pytest -m "not slow"``. ``make check`` / CI still runs everything.
"""

from __future__ import annotations

import pytest

# Test FILES whose cases are full end-to-end factory-graph passes.
_SLOW_FILES = frozenset(
    {
        "test_factory_graph.py",
        "test_factory_graph_autonomous.py",
        "test_factory_graph_gated.py",
        "test_phase2_exit.py",
        "test_phase4_exit.py",
        "test_phase5_exit.py",
        "test_supervisor_packs.py",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        filename = item.nodeid.split("::", 1)[0].rsplit("/", 1)[-1]
        if filename in _SLOW_FILES:
            item.add_marker(pytest.mark.slow)
