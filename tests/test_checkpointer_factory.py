"""make_checkpointer — the single-host sqlite checkpoint backend.

``FOUNDRY_CHECKPOINTER`` is accepted for back-compat but only ``sqlite`` is
supported (durable on a mounted volume, survives a restart)."""

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


def test_unknown_backend_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for backend in ("postgres", "mongodb"):
        monkeypatch.setenv("FOUNDRY_CHECKPOINTER", backend)
        with pytest.raises(ValueError, match="unknown FOUNDRY_CHECKPOINTER"):
            make_checkpointer(":memory:")
