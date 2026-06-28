"""Namespace sandbox for running untrusted generated code — the project-wide
isolation primitive (no Docker daemon, no image; just Linux user namespaces).

A single seam, ``run_in_sandbox(cmd, workdir)``, wraps ANY grounding command in an
unprivileged ``unshare`` so model-generated code runs with **no network** (can't
exfiltrate), an isolated **mount / PID / IPC / UTS** namespace, and a clean
process-tree teardown — while the in-process pinned harness still fences our source
off ``sys.path`` (so a generated module can't import or shadow ``foundry.*``). It is
reusable by every gate that grounds over a workdir (test, build, e2e) and by the
upcoming run/preview feature.

It is the DEFAULT (untrusted code is always sandboxed); opt out with
``FOUNDRY_GATE_SANDBOX=inprocess`` on hosts without unprivileged user namespaces
that accept the weaker in-process-only isolation. If the sandbox is selected but
unavailable, grounding fails CLOSED (ERROR) — never a silent unsandboxed run.
"""

from __future__ import annotations

import os
from pathlib import Path

from .runner import GroundingUnavailable, run_grounding_command

__all__ = [
    "UNSHARE_FLAGS",
    "run_in_sandbox",
    "sandbox_enabled",
    "unshare_available",
    "wrap_argv",
]

# Explicit opt-OUT values: run WITHOUT the namespace sandbox (in-process only).
_OPT_OUT = frozenset({"inprocess", "in-process", "none", "off", "0", "false", "no"})

# Unprivileged namespace isolation (util-linux ``unshare``):
#   --user --map-root-user  unprivileged; the caller becomes root INSIDE the userns
#   --net                   empty network namespace -> NO network (no exfiltration)
#   --mount --pid --uts --ipc   isolate mounts / processes / hostname / IPC
#   --fork --kill-child     run the cmd as a child and reap the whole tree on exit
#   --mount-proc            give the new PID namespace a correct /proc
UNSHARE_FLAGS: tuple[str, ...] = (
    "--user", "--map-root-user",
    "--net", "--mount", "--pid", "--uts", "--ipc",
    "--fork", "--kill-child", "--mount-proc",
)

_UNSHARE_BIN = "unshare"
# Memoized capability probe (user namespaces can be kernel-disabled); None = unprobed.
_UNSHARE_OK: bool | None = None


def sandbox_enabled() -> bool:
    """The namespace sandbox is the DEFAULT for running generated code; disabled only
    when ``FOUNDRY_GATE_SANDBOX`` is an explicit opt-out (``inprocess``/``none``/…)."""
    return os.environ.get("FOUNDRY_GATE_SANDBOX", "sandbox").strip().lower() not in _OPT_OUT


def unshare_available() -> bool:
    """True iff this host can create the unprivileged namespaces we need (memoized).

    Probes with the SAME flags we run for real over ``true``, so a kernel that
    forbids unprivileged userns / netns is detected up front — letting any later
    non-zero exit be trusted as the *command's* verdict, not a setup failure."""
    global _UNSHARE_OK
    if _UNSHARE_OK is None:
        try:
            rc, _out, _err = run_grounding_command(
                (_UNSHARE_BIN, *UNSHARE_FLAGS, "true"), Path("."), env=None, timeout_s=30.0
            )
            _UNSHARE_OK = rc == 0
        except GroundingUnavailable:
            _UNSHARE_OK = False  # no `unshare` binary at all
    return _UNSHARE_OK


def wrap_argv(cmd: tuple[str, ...] | list[str]) -> list[str]:
    """Prefix ``cmd`` with the unprivileged-namespace ``unshare`` invocation."""
    return [_UNSHARE_BIN, *UNSHARE_FLAGS, *cmd]


def run_in_sandbox(
    cmd: tuple[str, ...] | list[str],
    workdir: Path | str,
    *,
    env: dict[str, str] | None = None,
    timeout_s: float = 600.0,
) -> tuple[int, str, str]:
    """Run ``cmd`` over ``workdir`` inside the namespace sandbox; return (rc, out, err).

    Fails CLOSED (``GroundingUnavailable`` → ERROR) when the sandbox can't be
    established, so untrusted code is NEVER run unsandboxed by accident. When the
    capability probe passes, the wrapped command's own exit code is authoritative."""
    if not unshare_available():
        raise GroundingUnavailable(
            "namespace sandbox unavailable (no `unshare` / unprivileged user "
            "namespaces are kernel-disabled); set FOUNDRY_GATE_SANDBOX=inprocess to "
            "accept in-process-only isolation"
        )
    return run_grounding_command(wrap_argv(cmd), Path(workdir), env=env, timeout_s=timeout_s)
