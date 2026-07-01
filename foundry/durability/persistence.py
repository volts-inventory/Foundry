"""Single-host durable persistence — wire the dev stores to survive a restart.

(architecture.md §5 / DS3; implementation-plan §0.3; docs/deploy-compose.md)

ONE config seam, ``FOUNDRY_STATE_DIR``, turns Foundry's dev-grade in-memory
stores into a deployment that survives a PROCESS RESTART on a SINGLE host, via
the interfaces that already accept a path / connection:

* **checkpointer** — :func:`make_checkpointer` already takes a ``db_path``; we
  point it at ``<state_dir>/checkpoints.db`` (file-backed SQLite, WAL) instead
  of ``:memory:``. The LangGraph checkpoint threads resume after restart.
* **outbox + WORM audit** — ride the checkpointer's SHARED connection
  (:class:`FoundryCheckpointer` runs ``ensure_outbox_schema`` on ``.conn`` and
  ``enqueue``/``append_audit`` are called on that same conn inside
  ``superstep_txn``), so once the checkpointer is file-backed the outbox rows
  and the append-only audit chain persist with it — no separate wiring.
* **GlobalRecords trust ledgers** — the consumed-nonce ledger, the stable-FAIL
  latch, the negative-verdict / digest-disqualification latches and the
  class-3 alpha-look ledger are SQLite tables; we hand
  :class:`GlobalRecords` a file-backed connection at
  ``<state_dir>/global_records.db`` so the latches SURVIVE a restart (a
  stable-FAIL'd digest stays disqualified; a consumed nonce stays consumed).
* **CAS** — :class:`FilesystemArtifactStore` (already built + tested) backs
  ``AgentRuntime.cas`` at ``<state_dir>/cas`` instead of the in-memory
  :class:`ArtifactStore`, so artifact bodies/blobs persist.

ATOMICITY NUANCE (honest, architecture.md §5 / DS3): in PRODUCTION the
GlobalRecords ledger writes would share the SUPERSTEP transaction's connection
so the checkpoint and the ledgers commit (or roll back) ATOMICALLY — they can
never diverge. For SINGLE-HOST FILE DURABILITY we accept a SEPARATE file
connection for the ledgers (its own ``<state_dir>/global_records.db``). This is
a deliberate, documented trade-off: each store is independently durable across a
restart, but a crash in the narrow window between a checkpoint commit and a
ledger write is NOT cross-store atomic — out of scope for single-host LAN
durability.

DEFAULT UNCHANGED: when ``FOUNDRY_STATE_DIR`` is UNSET, callers take the
historical in-memory path (``:memory:`` checkpointer + in-memory
:class:`GlobalRecords` + in-memory :class:`ArtifactStore`) — byte-identical to
today. :func:`durable_backends` is only consulted when the env is set.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from foundry.durability.checkpointer import FoundryCheckpointer, make_checkpointer
from foundry.durability.fs_artifact_store import FilesystemArtifactStore
from foundry.state.global_records import GlobalRecords

__all__ = [
    "STATE_DIR_ENV",
    "DurableBackends",
    "durable_backends",
    "state_dir_from_env",
]

# The single env seam (12-factor config): set it to a mounted volume to make a
# single-host deployment durable. UNSET ⇒ in-memory everywhere (default).
STATE_DIR_ENV = "FOUNDRY_STATE_DIR"

# Per-store file/dir names under <state_dir>.
_CHECKPOINTS_DB = "checkpoints.db"
_GLOBAL_RECORDS_DB = "global_records.db"
_CAS_DIRNAME = "cas"


class DurableBackends:
    """The wired (checkpointer, global_records, cas) for a single host.

    Returned by :func:`durable_backends`. Holds the file-backed
    :class:`FoundryCheckpointer`, the file-backed :class:`GlobalRecords`, and
    the :class:`FilesystemArtifactStore` so a deployment wires durability in ONE
    place. :meth:`close` releases the two SQLite connections (the CAS holds no
    open handle).
    """

    def __init__(
        self,
        checkpointer: FoundryCheckpointer,
        global_records: GlobalRecords,
        cas: FilesystemArtifactStore,
        *,
        records_conn: sqlite3.Connection,
    ) -> None:
        self.checkpointer = checkpointer
        self.global_records = global_records
        self.cas = cas
        self._records_conn = records_conn

    def close(self) -> None:
        """Close the checkpointer + global-records connections."""
        self.checkpointer.close()
        self._records_conn.close()


def state_dir_from_env(env: dict[str, str] | None = None) -> str | None:
    """The configured state dir from ``FOUNDRY_STATE_DIR``, or ``None`` if unset.

    A blank/whitespace value is treated as UNSET (so an empty env var never
    silently roots durable state at the current directory) — the caller then
    takes the in-memory default, byte-identical to today.
    """
    source = os.environ if env is None else env
    value = source.get(STATE_DIR_ENV, "").strip()
    return value or None


def durable_backends(state_dir: str | Path) -> DurableBackends:
    """Wire file-backed checkpointer + GlobalRecords + CAS rooted at ``state_dir``.

    The ONE assembly point for single-host durability (the cli + any deployment
    call this when :data:`STATE_DIR_ENV` is set):

    * ``checkpointer`` — :func:`make_checkpointer` at ``<state_dir>/checkpoints.db``
      (honors ``FOUNDRY_CHECKPOINTER`` too; the outbox + WORM audit ride its
      shared connection, so they persist with it);
    * ``global_records`` — :class:`GlobalRecords` on a file-backed SQLite conn at
      ``<state_dir>/global_records.db`` (the trust ledgers survive a restart);
    * ``cas`` — :class:`FilesystemArtifactStore` at ``<state_dir>/cas``.

    See the module docstring for the checkpoint/ledger atomicity nuance (a
    SEPARATE ledger connection is acceptable for single-host file durability).
    """
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)

    # Checkpointer (+ outbox + WORM audit on its shared conn), file-backed.
    checkpointer = make_checkpointer(root / _CHECKPOINTS_DB)

    # GlobalRecords trust ledgers on their OWN file-backed connection. WAL +
    # check_same_thread=False mirror the dev :memory: conn's posture (the
    # GlobalRecords __init__ creates the schema idempotently).
    records_conn = sqlite3.connect(
        str(root / _GLOBAL_RECORDS_DB), check_same_thread=False
    )
    records_conn.execute("PRAGMA journal_mode=WAL")
    global_records = GlobalRecords(records_conn)

    # Durable content-addressed store (drop-in for the in-memory ArtifactStore).
    cas = FilesystemArtifactStore(root / _CAS_DIRNAME)

    return DurableBackends(
        checkpointer, global_records, cas, records_conn=records_conn
    )


