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
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        filename = item.nodeid.split("::", 1)[0].rsplit("/", 1)[-1]
        if filename in _SLOW_FILES:
            item.add_marker(pytest.mark.slow)


@pytest.fixture(autouse=True)
def _in_process_test_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite hermetic regardless of the developer's environment:

    * The Docker sandbox is the PRODUCTION default for running generated tests, but
      the hermetic suite has no daemon — so default every test to the in-process gate
      harness. A test that specifically exercises the sandbox path opts back in with
      ``monkeypatch.setenv("FOUNDRY_GATE_SANDBOX", "docker")``.
    * Pin the model backend to the deterministic scripted ``mock``. Importing
      ``webapp.server`` runs ``_load_env_file()`` at module load, which reads the
      repo ``.env`` — on a dev box that sets ``FOUNDRY_MODEL_BACKEND=claude-cli``,
      which would otherwise leak into ``main()``/``run()`` calls and spawn REAL
      ``claude -p`` subprocesses (network-bound; they hang where the binary exists).
      A test that wants a real/other backend overrides with its own
      ``monkeypatch.setenv("FOUNDRY_MODEL_BACKEND", ...)`` or an explicit ``env=``.
    """
    monkeypatch.setenv("FOUNDRY_GATE_SANDBOX", "inprocess")
    monkeypatch.setenv("FOUNDRY_MODEL_BACKEND", "mock")
    # Same .env-leak class as the backend above: a dev `.env` (loaded process-wide
    # by webapp.server's import-time _load_env_file()) may set
    # FOUNDRY_CODING_ADAPTER=claude-code, which _default_adapter_for() lets OVERRIDE
    # the mock->stub derivation — so a mock build would run the real claude-code
    # coding adapter, author no FeatureDiff, and materialize an empty tree. Pin the
    # deterministic stub substrate; a test wanting a real adapter overrides it.
    monkeypatch.setenv("FOUNDRY_CODING_ADAPTER", "stub")
