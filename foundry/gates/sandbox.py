"""Docker sandbox runner for the test gate — the HARD isolation boundary.

The in-process pinned harness (``test.py``) fences the host source off ``sys.path``
so generated code can't collide with ours, but it still runs in OUR interpreter, on
OUR filesystem and network. For real builds of untrusted model code the principled
boundary is a throwaway container that carries python + the pinned pytest and NONE
of our code: a generated test then has no way to reach, import, or shadow the
``foundry`` package, can't touch the network, and runs under resource caps.

Opt-in via ``FOUNDRY_GATE_SANDBOX=docker`` (the in-process harness stays the default
so the hermetic suite — and any host without a Docker daemon — keeps working). The
image is the ``test-sandbox`` stage of the project Dockerfile::

    docker build --target test-sandbox -t foundry-test-sandbox .

This module only BUILDS the invocation (a pure, unit-testable ``docker run`` argv)
and shells it through the same grounding seam the in-process harness uses, so the
two-zone contract (zone A observes rc/out/err; zone B decides) is unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

from .runner import run_grounding_command

__all__ = [
    "SANDBOX_PYTEST",
    "docker_run_argv",
    "run_in_sandbox",
    "sandbox_enabled",
    "sandbox_image",
]

# Default sandbox image (the Dockerfile's ``test-sandbox`` stage). Overridable so a
# deployment can pin a digest from its own registry.
_DEFAULT_IMAGE = "foundry-test-sandbox"

# The pinned harness, run INSIDE the container. Plain pytest — the container itself
# is the isolation (it holds none of our code); cache off (the rootfs is read-only).
SANDBOX_PYTEST: tuple[str, ...] = (
    "python", "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider",
)


def sandbox_enabled() -> bool:
    """True iff ``FOUNDRY_GATE_SANDBOX=docker`` selects the container boundary."""
    return os.environ.get("FOUNDRY_GATE_SANDBOX", "").strip().lower() == "docker"


def sandbox_image() -> str:
    """The sandbox image ref (``FOUNDRY_GATE_SANDBOX_IMAGE`` overrides the default)."""
    return os.environ.get("FOUNDRY_GATE_SANDBOX_IMAGE", "").strip() or _DEFAULT_IMAGE


def docker_run_argv(
    workdir: Path | str,
    image: str,
    *,
    inner: tuple[str, ...] = SANDBOX_PYTEST,
    network: str = "none",
    memory: str = "512m",
    cpus: str = "1.0",
    pids_limit: int = 256,
    docker_bin: str = "docker",
) -> list[str]:
    """The ``docker run`` argv that runs ``inner`` in ``image`` over the workdir.

    Fenced by construction: the workdir is bind-mounted READ-ONLY at ``/work``, the
    network is OFF, the rootfs is READ-ONLY (a small writable ``tmpfs`` at ``/tmp``
    backs ``tmp_path`` fixtures), and memory/cpu/pids are capped — so untrusted
    generated code can't reach the host, the network, or our code. ``--rm`` makes
    the container ephemeral.
    """
    work = Path(workdir).resolve()
    return [
        docker_bin, "run", "--rm",
        "--network", network,
        "--memory", memory,
        "--cpus", cpus,
        "--pids-limit", str(pids_limit),
        "--read-only",
        "--tmpfs", "/tmp:rw,size=64m",
        "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--volume", f"{work}:/work:ro",
        "--workdir", "/work",
        image,
        *inner,
    ]


def run_in_sandbox(
    workdir: Path | str,
    *,
    image: str | None = None,
    timeout_s: float = 600.0,
) -> tuple[int, str, str]:
    """Run the pinned pytest inside the sandbox container; return ``(rc, out, err)``.

    A missing ``docker`` binary / unreachable daemon surfaces (via the grounding
    seam) as fail-closed ERROR — never a silent PASS — exactly like any other
    missing grounding tool.
    """
    argv = docker_run_argv(workdir, image or sandbox_image())
    # env=None: the docker CLI inherits the host env; the CONTAINER's env is set by
    # the --env flags above (the harness pinning lives inside, not on the daemon).
    return run_grounding_command(argv, Path(workdir), env=None, timeout_s=timeout_s)
