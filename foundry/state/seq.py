"""`Seq` — the total, race-free causal order. (architecture.md §3)

`Seq` is NOT a shared mutable counter (parallel branches share one immutable
input snapshot and could not read-increment one without a race). It is the
lexicographic tuple:

    (run_id, emission_epoch, superstep_index, node_id, local)

Load-bearing invariant: **`emission_epoch` is ordered AHEAD of
`superstep_index`**, because `superstep_index` (LangGraph's `langgraph_step`)
is NOT monotonic across resume — a re-executed node keeps its step, so a
re-emitted record would *tie* (not outrank) the stale record it supersedes.
The durable per-(run, key) resume-generation counter breaks that tie in the
right direction. Do not derive monotonicity from `langgraph_step`.

Within a run `run_id` is constant, so the remaining fields give a total
order. Cross-fork resolution is NOT seq-ordered (it is fail-closed admission;
architecture.md §3, governance.md §5) — `run_id` exists purely so two forks'
records are distinct identities in the WORM/predicate stores.
"""

from __future__ import annotations

from typing import NamedTuple


class Seq(NamedTuple):
    run_id: str
    emission_epoch: int
    superstep_index: int
    node_id: str
    local: int


class EpochCounter:
    """Durable per-(run, key) resume-generation counter, in-memory protocol.

    The real counter is incremented *in the committing transaction*
    (durability/outbox.py provides the transactional variant); this class is
    the shared logic + the dev/in-memory implementation used by tests.
    """

    def __init__(self) -> None:
        self._epochs: dict[tuple[str, str], int] = {}

    def bump(self, run_id: str, key: str) -> int:
        """Increment and return the new epoch for a (re-)execution of `key`."""
        nxt = self._epochs.get((run_id, key), 0) + 1
        self._epochs[(run_id, key)] = nxt
        return nxt
