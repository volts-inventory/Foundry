"""Gate verdict reuse: a boundary's attested verdicts, re-referenced when the same
gates re-run on provably identical inputs (a fix pass back on a tree an earlier pass
judged, a restored best tree, a resume re-running an interrupted gate node).

The unit of reuse is the ordered list of gates one boundary runs over one fresh
workdir, not a single gate: a later gate reads what an earlier one left behind (the
build's install puts ``node_modules`` / the venv in place for test and smoke), so a
test verdict is only reproducible together with the build that preceded it. Reuse
is all-or-nothing over the unit, and a unit is stored only when every verdict in it
is PASS or FAIL — an ERROR (infra, including a flake quarantine) is never reused.

The key (:func:`unit_key`) is a digest over everything the verdicts are a function
of, and is ``None`` (never reuse) whenever any part cannot be pinned down:

- the workdir's full content at the unit's start: every path, file bytes, exec bit,
  symlink target and empty directory — no exclusions (generated ``.feature`` files
  and the scaffolded manifest included), and any special file disables reuse;
- each gate's class, name and every instance attribute (commands, flags, scope,
  timeouts, harness pins, profile, the venv's manifest digest), except the
  per-process observation seal key; an attribute of a type not understood here (a
  discriminator, a venv with no manifest digest) disables reuse;
- the source bytes of ``foundry`` (the harness, decision and wiring code);
- the interpreter and its installed distributions, the host toolchains and their
  versions, the env the sandbox passes to gate commands, and every ``FOUNDRY_*``
  variable;
- the runner's ``run_against`` / scanner / lineage revisions.

Attestation: a reused verdict IS the original zone-B-signed record, re-referenced
with a freshly minted ``Seq`` — ``seq`` is not part of the attestation tuple, so the
original signature still verifies and nothing is signed over results not produced
now. Before reuse the runner re-derives each record's attestation under its own key
(a record planted in the on-disk store does not verify) and checks it names this
gate, this ``run_against`` and this runner's revisions.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import importlib.metadata
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.canonical import content_digest
from foundry.schemas.gates import GateResult, GateStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from foundry.gates.runner import GateImpl

__all__ = ["VerdictCache", "durable_unit", "unit_key"]

_KEY_VERSION = 1


class _Unkeyable(Exception):
    """An input the key cannot pin down: the unit is never reused."""


def _tree_digest(root: Path) -> str:
    entries: list[list[object]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        rel_dir = base.relative_to(root).as_posix()
        if not dirnames and not filenames and rel_dir != ".":
            entries.append([rel_dir, "d"])
        for name in (*dirnames, *filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                entries.append([rel, "l", os.readlink(path)])
            elif stat.S_ISREG(st.st_mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                entries.append([rel, "f", digest, bool(st.st_mode & 0o111)])
            elif not stat.S_ISDIR(st.st_mode):
                raise _Unkeyable(f"special file {rel}")
    entries.sort(key=lambda e: str(e[0]).encode("utf-8"))
    return content_digest({"kind": "gate_reuse_tree", "entries": entries})


def _value(v: object) -> object:
    from foundry.gates.pydeps import PythonBuildEnv

    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (tuple, list)):
        return [_value(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted((_value(x) for x in v), key=repr)
    if isinstance(v, dict):
        return {str(k): _value(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, PythonBuildEnv):
        if v.digest is None:  # local-path requirements: the venv is not a pure function
            raise _Unkeyable("venv without a manifest digest")
        return {"requirements": list(v.requirements), "digest": v.digest}
    raise _Unkeyable(f"gate attribute of type {type(v).__name__}")


def _gate_config(gate: GateImpl) -> dict[str, object]:
    cls = type(gate)
    attrs = {k: _value(v) for k, v in sorted(vars(gate).items()) if k != "_seal_key"}
    return {"class": f"{cls.__module__}.{cls.__qualname__}", "name": gate.name, "attrs": attrs}


@functools.cache
def _code_digest() -> str:
    # All of foundry, not only foundry/gates: the gates import python_env, fs,
    # toolchains, canonical and the gate wiring.
    return _source_digest(Path(__file__).parents[1])


def _source_digest(root: Path) -> str:
    files = sorted(root.rglob("*.py"), key=lambda p: p.as_posix())
    return content_digest(
        {
            "kind": "gate_code",
            "files": [
                [p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()]
                for p in files
            ],
        }
    )


@functools.cache
def _host_identity() -> str:
    """The process-constant part of the env: the interpreter, its distributions and
    the host toolchains — a rescan per unit key cost every gate run."""
    from foundry.toolchains import available_toolchains

    return content_digest(
        {
            "python": [sys.executable, sys.version],
            # The harness runs this interpreter's pytest / pytest-bdd: a `uv sync`
            # that bumps one changes the key.
            "distributions": sorted(
                {(d.metadata["Name"] or "", d.version) for d in importlib.metadata.distributions()}
            ),
            "toolchains": [t.model_dump(mode="json") for t in available_toolchains()],
        }
    )


def _env_identity() -> str:
    from foundry.gates.sandbox import _confined_env

    # The sandbox env is a cheap filter over os.environ, which a process may change.
    return content_digest(
        {
            "host": _host_identity(),
            "sandbox_env": _confined_env(None),
            "foundry_env": {k: v for k, v in os.environ.items() if k.startswith("FOUNDRY_")},
        }
    )


def durable_unit(gates: Sequence[GateImpl]) -> bool:
    """May this unit's PASS outlive the process (the on-disk store)? Not when a gate
    holds a build venv: it is a per-process temp dir rebuilt on resume, and pip
    resolves its transitive dependencies afresh, so the resumed venv may differ."""
    from foundry.gates.pydeps import PythonBuildEnv

    return not any(isinstance(v, PythonBuildEnv) for g in gates for v in vars(g).values())


def unit_key(
    gates: Sequence[GateImpl],
    workdir: Path,
    *,
    run_against: str,
    scanner_config_rev: str,
    lineage_map_revision: str,
) -> str | None:
    """The reuse key of running ``gates`` in order over ``workdir`` as it is now,
    or ``None`` when some input cannot be pinned down (never reuse)."""
    try:
        return content_digest(
            {
                "kind": "gate_unit_reuse_key",
                "v": _KEY_VERSION,
                "run_against": run_against,
                "scanner_config_rev": scanner_config_rev,
                "lineage_map_revision": lineage_map_revision,
                "gates": [_gate_config(g) for g in gates],
                "tree": _tree_digest(Path(workdir)),
                "code": _code_digest(),
                "env": _env_identity(),
            }
        )
    except (_Unkeyable, OSError):
        return None


class VerdictCache:
    """A run's reusable gate units: unit key -> the original attested records.

    In memory (PASS and FAIL units), and under ``root`` when given: the run's state
    dir, so a resumed process reuses what the interrupted one judged. Only all-PASS
    units of :func:`durable_unit` gates go to disk: a resume re-runs every FAIL (a
    suspected flake gets its retry) and every venv-backed unit.

    Disk trust equals checkpoint trust. The filename key is NOT bound to the signed
    records: the runner re-derives each record's attestation and checks its gate,
    tree and revisions, but cannot prove a record was stored under the key it is
    found at. Anyone who can write the state dir can already tamper with the
    checkpoint the run resumes from, so this adds no new authority.

    Residuals the key does not cover: a gate whose verdict depends on the network
    (an unpinned ``npm install`` resolving afresh) or the clock, and config files
    under ``HOME`` (``~/.npmrc``, ``~/.config/pip``), which the env digest keys by
    path only, not content."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root
        self._mem: dict[str, list[GateResult]] = {}

    def get(self, key: str) -> list[GateResult] | None:
        hit = self._mem.get(key)
        if hit is not None or self._root is None:
            return hit
        path = self._root / f"{key}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return [GateResult.model_validate(r) for r in raw]
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            return None  # a corrupt entry is a miss, never a verdict

    def put(self, key: str, records: list[GateResult], *, durable: bool) -> None:
        self._mem[key] = list(records)
        if self._root is None or not durable:
            return
        if any(r.status is not GateStatus.PASS for r in records):
            return
        body: list[Any] = [r.model_dump(mode="json") for r in records]
        # Best-effort, as get treats an unreadable entry as a miss: the gates already
        # ran, so a full disk or a read-only state dir must not fail the unit.
        with contextlib.suppress(OSError):
            self._root.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{key}.", suffix=".tmp", dir=self._root)
            tmp = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(body))
                os.replace(tmp, self._root / f"{key}.json")
            finally:
                with contextlib.suppress(FileNotFoundError):
                    tmp.unlink()
