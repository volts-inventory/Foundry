"""Sandbox — the cage the adapter boundary is *enforced* by. (arch §2, plan §3.2)

The adapter runs arbitrary bash, so "writes worktree only / no external
side-effect" must be a property of the cage, not a promise of the adapter.

HONESTY LABEL (plan §0.3) — production vs this dev-grade stand-in:

================  =========================================  ====================
Control           PRODUCTION enforcement                      DEV-GRADE here
================  =========================================  ====================
Write isolation   only writable mount is the task worktree   isolated temp
                  (mount namespace)                           worktree copy; cwd
                                                              pinned to it
Network denial    separate netns; the ONLY route is the      scrubbed env (no
                  mediating proxy over loopback/UDS           credentials to spend)
                                                              + no proxy address
                                                              unless explicitly
                                                              passed
Hard kill         cgroup CPU/wall-clock cap, external        wall-clock timeout
                  SIGKILL at budget breach                    with process-group
                                                              SIGKILL; RLIMIT_CPU
                                                              via preexec_fn
Credentials       endpoints live in namespaces the adapter   env allowlist; deny
                  process tree cannot address                 *_KEY/*_TOKEN/
                                                              *_SECRET/AWS_*/
                                                              ANTHROPIC_*/... and
                                                              HOME redirected to a
                                                              sandbox-local dir
================  =========================================  ====================

The *logic* is correct (kill the whole process group, allowlist not denylist
on env, snapshot only recorded state); only the enforcement mechanism is the
container-friendly equivalent.

Turn snapshots (arch §2 atomicity / §5): `snapshot()` is content-addressed
(`foundry.state.tree_sha.normalized_source_tree`) and copies the worktree into
a digest-keyed snapshot store; `restore()` reconstitutes the worktree from a
recorded snapshot and DISCARDS any uncommitted filesystem delta — the worktree
is never a third durable store outside the checkpoint transaction. The handle
returned here is what the runner writes in the same outbox transaction as the
resumable marker.

Phase separation: `destroy()` tears the sandbox down. Per arch §2(d) the
sandbox is destroyed and rebuilt between the untrusted-execution phase and any
credentialed phase — never reused. Pass a durable `snapshot_store` so recorded
turn snapshots survive `destroy()` for the rebuilt sandbox to `restore()` from.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from foundry.state.tree_sha import normalized_source_tree

__all__ = ["DEFAULT_ENV_ALLOWLIST", "Sandbox", "SandboxResult"]

# Allowlist, not denylist: nothing is inherited unless named here (and even
# named vars are re-checked against the credential deny patterns below).
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
)

# Defense-in-depth: even explicitly-passed extra_env may not smuggle these in.
_DENIED_SUFFIXES: tuple[str, ...] = (
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIALS",
    "_API_KEY",
)
_DENIED_PREFIXES: tuple[str, ...] = (
    "AWS_",
    "ANTHROPIC_",
    "GITHUB_",
    "OPENAI_",
    "GOOGLE_",
    "GCP_",
    "AZURE_",
    "HF_",
)
_DENIED_EXACT: frozenset[str] = frozenset({"SSH_AUTH_SOCK", "GH_TOKEN", "NETRC"})

# The single env var through which a sandbox tells an adapter its ONLY egress.
PROXY_ENV_VAR = "FOUNDRY_PROXY_ENDPOINT"


def _denied(name: str) -> bool:
    upper = name.upper()
    return (
        upper in _DENIED_EXACT
        or any(upper.endswith(s) for s in _DENIED_SUFFIXES)
        or any(upper.startswith(p) for p in _DENIED_PREFIXES)
    )


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of one sandboxed command. `timed_out` means the process group
    was SIGKILLed at the wall-clock cap (the dev stand-in for the cgroup
    hard-kill) — treat it as a budget breach, never a verdict."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    wall_clock_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class Sandbox:
    """Isolated worktree + scrubbed-env subprocess runner + turn-snapshot store.

    Parameters
    ----------
    source_dir:
        Tree to clone into the isolated worktree. The source is never written
        to again by this sandbox.
    proxy_endpoint:
        The ONLY egress an adapter may be told about (arch §2b). When None
        (default), no proxy address is exposed at all — dev-grade network
        denial is "nothing to talk to and no credentials to spend".
    wall_clock_timeout_s / cpu_seconds:
        Default resource caps for `run` (overridable per call).
    snapshot_store:
        Digest-keyed snapshot + blob store directory. Defaults to an
        ephemeral dir inside the sandbox root (dies with `destroy()`); pass a
        durable path so turn snapshots survive the destroy/rebuild between
        untrusted and credentialed phases.
    """

    def __init__(
        self,
        source_dir: Path | str,
        *,
        proxy_endpoint: str | None = None,
        wall_clock_timeout_s: float = 600.0,
        cpu_seconds: int | None = None,
        env_allowlist: Sequence[str] = DEFAULT_ENV_ALLOWLIST,
        snapshot_store: Path | str | None = None,
    ) -> None:
        self.proxy_endpoint = proxy_endpoint
        self._wall_clock_timeout_s = wall_clock_timeout_s
        self._cpu_seconds = cpu_seconds
        self._env_allowlist = tuple(env_allowlist)
        self._destroyed = False

        self._root = Path(tempfile.mkdtemp(prefix="foundry-sandbox-"))
        self.worktree = self._root / "worktree"
        self._home = self._root / "home"  # HOME redirect: no ~/.aws, ~/.netrc, ...
        self._home.mkdir()
        shutil.copytree(Path(source_dir), self.worktree, symlinks=True)

        store = self._root / "store" if snapshot_store is None else Path(snapshot_store)
        self._snapshots_dir = store / "snapshots"
        self._blobs_dir = store / "blobs"
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._blobs_dir.mkdir(parents=True, exist_ok=True)

    # -- env -----------------------------------------------------------------

    def scrubbed_env(self, extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Allowlisted, credential-free child environment.

        Nothing from os.environ is inherited unless allowlisted AND not
        matching a credential pattern; extra_env is deny-filtered too, so an
        adapter cannot be handed a credential even by a buggy caller.
        """
        env: dict[str, str] = {}
        for name in self._env_allowlist:
            if name in os.environ and not _denied(name):
                env[name] = os.environ[name]
        env.setdefault("PATH", os.defpath)
        env["HOME"] = str(self._home)
        if self.proxy_endpoint is not None:
            env[PROXY_ENV_VAR] = self.proxy_endpoint
        for name, value in (extra_env or {}).items():
            if not _denied(name):
                env[name] = value
        return env

    # -- execution -----------------------------------------------------------

    def run(
        self,
        cmd: Sequence[str] | str,
        *,
        timeout_s: float | None = None,
        cpu_seconds: int | None = None,
        extra_env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> SandboxResult:
        """Run a command with cwd=worktree, scrubbed env, own session.

        On wall-clock breach the ENTIRE process group is SIGKILLed (dev
        stand-in for the cgroup hard-kill, arch §2 (b)); RLIMIT_CPU is set in
        the child via preexec_fn when a CPU cap applies.
        """
        self._check_alive()
        argv = ["/bin/sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
        timeout = self._wall_clock_timeout_s if timeout_s is None else timeout_s
        cpu = self._cpu_seconds if cpu_seconds is None else cpu_seconds

        def _preexec() -> None:  # runs in the child, post-fork pre-exec
            if cpu is not None:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

        start = time.monotonic()
        proc = subprocess.Popen(
            argv,
            cwd=self.worktree,
            env=self.scrubbed_env(extra_env),
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # own process group: killable as a unit
            preexec_fn=_preexec,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # start_new_session=True => pgid == pid; kill the whole group so
            # grandchildren cannot outlive the budget breach.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        return SandboxResult(
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=timed_out,
            wall_clock_s=time.monotonic() - start,
        )

    # -- turn snapshots (arch §2 atomicity, §5) --------------------------------

    def snapshot(self) -> str:
        """Content-addressed turn snapshot of the worktree -> digest handle.

        The digest is `normalized_source_tree` over the worktree (timestamp-
        and permission-free, so two byte-identical worktrees produce the SAME
        handle); the bytes are copied into the digest-keyed snapshot store.
        The returned handle is what gets written in the same outbox
        transaction as the resumable marker.
        """
        self._check_alive()
        # prefer_git=False: adapter writes are typically untracked; the
        # snapshot must cover what is on disk, not what git happens to track.
        digest = normalized_source_tree(self.worktree, prefer_git=False)
        dest = self._snapshots_dir / digest
        if not dest.exists():
            tmp = self._snapshots_dir / f".tmp-{digest}-{os.getpid()}"
            shutil.copytree(self.worktree, tmp, symlinks=True)
            os.replace(tmp, dest)  # atomic publish: never a half-written snapshot
        return digest

    def restore(self, snapshot: str | Path) -> str:
        """Reconstitute the worktree from a recorded snapshot; the uncommitted
        delta is DISCARDED (arch §5: the worktree is never a third durable
        store — only recorded snapshots count).

        Accepts a digest handle (preferred) or a path to a snapshot copy dir.
        Returns the digest restored to.
        """
        self._check_alive()
        candidate = self._snapshots_dir / str(snapshot)
        if candidate.is_dir():
            src, digest = candidate, str(snapshot)
        elif Path(snapshot).is_dir():
            src = Path(snapshot)
            digest = normalized_source_tree(src, prefer_git=False)
        else:
            raise FileNotFoundError(f"no recorded snapshot: {snapshot!r}")
        shutil.rmtree(self.worktree)
        shutil.copytree(src, self.worktree, symlinks=True)
        return digest

    # -- content-addressed blob store (DS9: bodies out-of-band) ----------------

    def put_blob(self, data: bytes) -> str:
        """Store bytes content-addressed; return a `cas://sha256/<hex>` handle.

        Diffs/test sources travel as handles (foundry.schemas.base.Artifact
        discipline): large bodies never live in state.
        """
        self._check_alive()
        digest = hashlib.sha256(data).hexdigest()
        path = self._blobs_dir / digest
        if not path.exists():
            tmp = self._blobs_dir / f".tmp-{digest}-{os.getpid()}"
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return f"cas://sha256/{digest}"

    def get_blob(self, uri: str) -> bytes:
        prefix = "cas://sha256/"
        if not uri.startswith(prefix):
            raise ValueError(f"not a cas handle: {uri!r}")
        return (self._blobs_dir / uri.removeprefix(prefix)).read_bytes()

    # -- lifecycle -------------------------------------------------------------

    def destroy(self) -> None:
        """Tear the sandbox down (idempotent).

        Per arch §2(d) the sandbox MUST be destroyed and rebuilt between the
        untrusted-execution phase and any credentialed phase — no reuse. A
        durable `snapshot_store` (constructor arg) survives this call so the
        rebuilt sandbox can `restore()` the last recorded turn snapshot.
        """
        if self._destroyed:
            return
        self._destroyed = True
        shutil.rmtree(self._root, ignore_errors=True)

    def _check_alive(self) -> None:
        if self._destroyed:
            raise RuntimeError("sandbox destroyed — build a new one (arch §2(d): no reuse)")

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.destroy()
