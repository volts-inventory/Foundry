"""make_checkpointer (TASK [14]) — the deployment-portability backend switch.

The compose/EKS profile selects the checkpoint backend via env
``FOUNDRY_CHECKPOINTER`` (12-factor config). Only the ``sqlite`` path is wired
and validatable in this environment; ``postgres`` is a documented swap point
that FAILS CLOSED. These tests pin that contract — they do NOT exercise a real
Postgres (none is available here).
"""

from __future__ import annotations

import pytest

from foundry.durability.checkpointer import FoundryCheckpointer, make_checkpointer


def test_default_is_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_CHECKPOINTER", raising=False)
    cp = make_checkpointer(":memory:")
    try:
        assert isinstance(cp, FoundryCheckpointer)
        # The wired sqlite backend works end to end (a superstep txn commits).
        with cp.superstep_txn() as conn:
            assert conn is cp.conn
    finally:
        cp.close()


def test_explicit_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_CHECKPOINTER", "SQLite")  # case-insensitive
    cp = make_checkpointer(":memory:")
    try:
        assert isinstance(cp, FoundryCheckpointer)
    finally:
        cp.close()


def test_postgres_is_documented_swap_point_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_CHECKPOINTER", "postgres")
    # Validated swap point, fail-closed until the dialect-portable outbox lands.
    with pytest.raises(NotImplementedError, match="Fail-closed"):
        make_checkpointer(":memory:")


def test_unknown_backend_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_CHECKPOINTER", "mongodb")
    with pytest.raises(ValueError, match="unknown FOUNDRY_CHECKPOINTER"):
        make_checkpointer(":memory:")
