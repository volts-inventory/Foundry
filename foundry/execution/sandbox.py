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
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from foundry.state.tree_sha import normalized_source_tree

__all__ = ["DEFAULT_ENV_ALLOWLIST", "GRANTABLE_MODEL_AUTH", "Sandbox", "SandboxResult"]

# Allowlist, not denylist: nothing is inherited unless named here (and even
# named vars are re-checked against the credential deny patterns below).
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    # Not a credential — a sandbox-mode flag. Lets an agentic CLI (claude) accept
    # --dangerously-skip-permissions when the factory itself runs as root (CI /
    # containers), where the CLI otherwise refuses that flag. Only ever passed
    # through when the PARENT sets it, and it carries no secret.
    "IS_SANDBOX",
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

# ---- typed credential grant (model-auth only; see README adapter docs) -------
#
# The ONLY credential class that may ever enter the cage is MODEL AUTH — what an
# agentic CLI substrate (Claude Code) needs to authenticate its own model calls.
# Blast radius = model spend/quota. TARGET-SYSTEM credentials (AWS_*, deploy
# keys, signing keys) are NEVER grantable: generated code must not be able to
# touch real infrastructure; those belong exclusively to the credentialed phase
# (arch §2(d) — the sandbox is destroyed and rebuilt between phases). The grant
# is an explicit, name-by-name allowlist checked against this frozen set — a
# caller asking for anything else fails closed at construction, before any
# subprocess exists.
GRANTABLE_MODEL_AUTH: frozenset[str] = frozenset(
    {
        "CLAUDE_CODE_OAUTH_TOKEN",  # Max-plan token (claude setup-token) — no API billing
        "ANTHROPIC_API_KEY",  # metered Anthropic API
    }
)


def _denied(name: str) -> bool:
    upper = name.upper()
    return (
        upper in _DENIED_EXACT
        or any(upper.endswith(s) for s in _DENIED_SUFFIXES)
        or any(upper.startswith(p) for p in _DENIED_PREFIXES)
    )


def _tmp_name(digest: str) -> str:
    """A staging name unique across processes AND threads before the atomic publish.

    The IC pool (Wave C, max_slots>1) runs many sandbox workers in ONE process over
    a shared snapshot_store, so ``os.getpid()`` alone collides — two threads staging
    the same content-addressed digest would clobber each other's tmp. Thread id +
    a uuid make the staging path unique; the final publish stays content-addressed.
    """
    return f".tmp-{digest}-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}"


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
    credential_grant:
        TYPED model-auth grant (ROADMAP Phase 1b): env var names to admit into
        the child env DESPITE the credential deny patterns, checked fail-closed
        against :data:`GRANTABLE_MODEL_AUTH` — only model-auth class is ever
        admissible; ``AWS_*``/target-system credentials cannot be granted, ever.
        This is what lets an agentic CLI substrate (Claude Code) authenticate
        inside the cage. Empty (default) keeps the env fully scrubbed.
    home_passthrough:
        Copy the invoking user's ``~/.claude`` (+ ``~/.claude.json``) into the
        sandbox-local HOME so a cached ``claude /login`` session authenticates
        inside the cage. A COPY, never a bind: untrusted task code can corrupt
        at most a throwaway copy of the session, not the real login state.
    toolchain_env:
        Pass the host's toolchain, proxy and CA settings through (the gate
        sandbox's non-secret allowlist, proxy userinfo stripped) so an agent can
        run the project's install/build/typecheck behind a TLS-intercepting
        proxy. Off by default.
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
        credential_grant: Sequence[str] = (),
        home_passthrough: bool = False,
        toolchain_env: bool = False,
    ) -> None:
        self.proxy_endpoint = proxy_endpoint
        self._wall_clock_timeout_s = wall_clock_timeout_s
        self._cpu_seconds = cpu_seconds
        self._env_allowlist = tuple(env_allowlist)
        self._destroyed = False
        # Fail-closed BEFORE any subprocess exists: only model-auth is grantable.
        ungrantable = [n for n in credential_grant if n not in GRANTABLE_MODEL_AUTH]
        if ungrantable:
            raise ValueError(
                f"credential_grant {ungrantable!r} is not grantable: only model-auth "
                f"credentials ({', '.join(sorted(GRANTABLE_MODEL_AUTH))}) may enter "
                f"the sandbox; target-system credentials belong to the credentialed "
                f"phase (arch §2(d), ROADMAP Phase 1b)"
            )
        self._credential_grant = tuple(dict.fromkeys(credential_grant))
        self._home_passthrough = bool(home_passthrough)
        self._toolchain_env = bool(toolchain_env)

        self._root = Path(tempfile.mkdtemp(prefix="foundry-sandbox-"))
        self.worktree = self._root / "worktree"
        self._home = self._root / "home"  # HOME redirect: no ~/.aws, ~/.netrc, ...
        self._home.mkdir()
        shutil.copytree(Path(source_dir), self.worktree, symlinks=True)
        if self._home_passthrough:
            self._copy_claude_session()

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
        adapter cannot be handed a credential even by a buggy caller. The ONE
        exception is the constructor's typed ``credential_grant`` — model-auth
        names validated against :data:`GRANTABLE_MODEL_AUTH` at construction —
        admitted LAST so the grant is explicit, auditable, and never widened by
        ``extra_env``.
        """
        env: dict[str, str] = {}
        for name in self._env_allowlist:
            if name in os.environ and not _denied(name):
                env[name] = os.environ[name]
        if self._toolchain_env:
            from foundry.gates.sandbox import toolchain_env

            env.update({k: v for k, v in toolchain_env().items() if not _denied(k)})
        env.setdefault("PATH", os.defpath)
        env["HOME"] = str(self._home)
        if self.proxy_endpoint is not None:
            env[PROXY_ENV_VAR] = self.proxy_endpoint
        for name, value in (extra_env or {}).items():
            if not _denied(name):
                env[name] = value
        for name in self._credential_grant:  # typed grant: model-auth only
            if name in os.environ:
                env[name] = os.environ[name]
        return env

    @property
    def credential_grant_record(self) -> tuple[str, ...]:
        """What entered the cage — for the evidence pack, never a secret VALUE.

        The granted model-auth var names that are actually present in the
        parent env (a requested-but-unset name never entered), plus a
        ``home:~/.claude`` marker when the login-session copy was made.
        """
        record = [n for n in self._credential_grant if n in os.environ]
        if self._home_passthrough and self._claude_session_copied:
            record.append("home:~/.claude")
        return tuple(record)

    def _copy_claude_session(self) -> None:
        """COPY the user's cached ``claude`` login session into the sandbox HOME.

        A copy, never a bind-mount: task code can corrupt at most the throwaway
        copy. Missing sources are fine (nothing to pass through — the CLI will
        act logged-out unless an env grant supplies a token instead).
        """
        self._claude_session_copied = False
        real_home = Path(os.path.expanduser("~"))
        src_dir = real_home / ".claude"
        if src_dir.is_dir():
            shutil.copytree(src_dir, self._home / ".claude", symlinks=False)
            self._claude_session_copied = True
        src_json = real_home / ".claude.json"
        if src_json.is_file():
            shutil.copy2(src_json, self._home / ".claude.json")
            self._claude_session_copied = True

    # -- execution -----------------------------------------------------------

    def run(
        self,
        cmd: Sequence[str] | str,
        *,
        timeout_s: float | None = None,
        cpu_seconds: int | None = None,
        extra_env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        on_stdout_line: Callable[[str], None] | None = None,
    ) -> SandboxResult:
        """Run a command with cwd=worktree, scrubbed env, own session.

        On wall-clock breach the ENTIRE process group is SIGKILLed (dev
        stand-in for the cgroup hard-kill, arch §2 (b)); RLIMIT_CPU is set in
        the child via preexec_fn when a CPU cap applies.

        ``on_stdout_line`` opts into STREAMING: each stdout line is handed over as it
        arrives (for live progress out of a long agentic run) and still accumulated, so
        the returned ``stdout`` is byte-identical to the buffered path. The wall-clock
        cap is enforced by a timer that kills the process group on schedule — never by
        the read loop — so a child that stops writing cannot outlive its budget. The
        callback is advisory: an exception from it can't touch the run.
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
        if on_stdout_line is not None:
            stdout, stderr, timed_out = self._stream_output(
                proc, timeout, input_text, on_stdout_line
            )
        else:
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

    def _kill_group(self, proc: subprocess.Popen[str]) -> None:
        """SIGKILL the child's whole process group (pgid == pid: start_new_session)."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    def _stream_output(
        self,
        proc: subprocess.Popen[str],
        timeout: float,
        input_text: str | None,
        on_line: Callable[[str], None],
    ) -> tuple[str, str, bool]:
        """Read stdout line-by-line (feeding ``on_line``) and return
        ``(stdout, stderr, timed_out)``.

        The cap is a TIMER, not a read deadline: it fires on schedule whether or not
        the child is writing, so streaming can never extend the wall-clock budget.
        stderr is drained on its own thread — a chatty stderr would otherwise fill its
        pipe and deadlock the child while we block on stdout.
        """
        timed_out = threading.Event()

        def _on_deadline() -> None:
            timed_out.set()
            self._kill_group(proc)

        killer = threading.Timer(timeout, _on_deadline)
        killer.daemon = True
        killer.start()
        err: list[str] = []

        def _drain_err() -> None:
            with contextlib.suppress(Exception):
                if proc.stderr is not None:
                    err.append(proc.stderr.read())

        err_thread = threading.Thread(target=_drain_err, daemon=True)
        err_thread.start()
        out: list[str] = []
        try:
            if input_text is not None and proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.write(input_text)
                    proc.stdin.close()
            if proc.stdout is not None:
                for line in proc.stdout:          # yields as the child flushes
                    out.append(line)
                    with contextlib.suppress(Exception):
                        on_line(line)             # advisory: never break the run
            proc.wait()
        finally:
            killer.cancel()
            err_thread.join(timeout=2.0)
        return "".join(out), "".join(err), timed_out.is_set()

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
        digest = normalized_source_tree(self.worktree)
        dest = self._snapshots_dir / digest
        if not dest.exists():
            tmp = self._snapshots_dir / _tmp_name(digest)
            shutil.copytree(self.worktree, tmp, symlinks=True)
            try:
                os.replace(tmp, dest)  # atomic publish: never a half-written snapshot
            except OSError:
                # A concurrent worker (pool path, max_slots>1) published the SAME
                # content-addressed snapshot first — os.replace onto a now-existing
                # dir fails. The store is content-addressed, so the winner's copy is
                # byte-identical; discard ours. (dest is guaranteed present here.)
                shutil.rmtree(tmp, ignore_errors=True)
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
            digest = normalized_source_tree(src)
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
            tmp = self._blobs_dir / _tmp_name(digest)
            tmp.write_bytes(data)
            os.replace(tmp, path)  # atomic for a file; last identical writer wins
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
