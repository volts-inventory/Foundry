"""Namespace sandbox for running untrusted generated code — the project-wide
isolation primitive (no Docker daemon, no image; just Linux user namespaces).

A single seam, ``run_in_sandbox(cmd, workdir)``, wraps ANY grounding command in an
unprivileged ``unshare`` so model-generated code runs with **no network** (can't
exfiltrate), an isolated **mount / PID / IPC / UTS** namespace, and a clean
process-tree teardown — while the in-process pinned harness still fences our source
off ``sys.path`` (so a generated module can't import or shadow ``foundry.*``). It is
reusable by every gate that grounds over a workdir (test, build, e2e) and by the
upcoming run/preview feature.

Build/smoke/install additionally run CONFINED (``run_confined``): inside the mount
namespace every mount except the workdir is remounted read-only, ``/tmp`` and
``/dev/shm`` are fresh private tmpfs, and loopback is brought up (offline steps can
still boot a server on 127.0.0.1). Under ``--map-root-user`` the command is root in
the userns, and when the host caller is root that maps to real root — so without
the fence an agent's ``postinstall`` could rewrite any host file. Only the install
step (``network=True``) shares the host network namespace; build/smoke are offline.

It is the DEFAULT (untrusted code is always sandboxed); opt out with
``FOUNDRY_GATE_SANDBOX=inprocess`` on hosts without unprivileged user namespaces
that accept the weaker in-process-only isolation. If the sandbox is selected but
unavailable, grounding fails CLOSED (ERROR) — never a silent unsandboxed run.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .runner import GroundingUnavailable, run_grounding_command

__all__ = [
    "UNSHARE_FLAGS",
    "ground_untrusted",
    "run_confined",
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


# Exit code + stderr marker the fence uses for its OWN setup failures, so they surface
# as ERROR (not-a-verdict) rather than the command's FAIL. A command that forges the
# marker only downgrades its own verdict to ERROR — never to PASS.
_FENCE_SETUP_RC = 125
_FENCE_SETUP_MARKER = "foundry-sandbox-fence: setup failed"

# $HOME entries masked inside the fence (the install step has network, so any
# readable credential is exfiltratable). A denylist: see residual risks in §5.
_HIDDEN_HOME_PATHS: tuple[str, ...] = (
    ".ssh", ".aws", ".azure", ".config/gcloud", ".config/gh", ".docker", ".kube",
    ".gnupg", ".claude", ".claude.json", ".git-credentials", ".netrc", ".npmrc",
    ".pypirc", ".cargo/credentials", ".cargo/credentials.toml",
)

# Env passed to confined commands: an ALLOWLIST (host env carries cloud/GitHub/API
# tokens, and the install step has network). Toolchain locations, locale, proxy
# and CA settings only.
_ENV_ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "CI",
    "SOURCE_DATE_EPOCH",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "npm_config_proxy", "npm_config_https_proxy", "npm_config_noproxy", "YARN_HTTPS_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "PIP_CERT", "CARGO_HTTP_CAINFO", "GIT_SSL_CAINFO",
    "NIX_SSL_CERT_FILE", "JAVA_TOOL_OPTIONS",
    "JAVA_HOME", "GOROOT", "GOPATH", "GOPROXY", "GOFLAGS", "CARGO_HOME", "RUSTUP_HOME",
    "RUSTUP_TOOLCHAIN", "DOTNET_ROOT", "NUGET_PACKAGES", "VIRTUAL_ENV", "NODE_PATH",
})


def _confined_env(env: dict[str, str] | None) -> dict[str, str]:
    base = os.environ if env is None else env
    return {k: v for k, v in base.items() if k in _ENV_ALLOW or k.startswith("LC_")}

# Runs as the namespace's PID 1 child under ``python -I -S`` (no site, no cwd on
# sys.path: the workdir can't shadow a stdlib module), then execs the command.
# argv: <mount bin> <unshare bin> <workdir> <net: 0|1> <cmd...>
_FENCE = r"""
import os, sys, subprocess
mount, unshare, w, net = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
cmd = sys.argv[5:]
def die(msg):
    sys.stderr.write("%s: %s\n" % (__MARKER__, msg)); sys.exit(__RC__)
def m(*a):
    r = subprocess.run([mount, *a], capture_output=True, text=True)
    if r.returncode: die("mount %s: %s" % (" ".join(a), r.stderr.strip()))
def unesc(p):
    return p.encode().decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
def under(p, root):
    return p == root or p.startswith(root.rstrip("/") + "/")
try:
    os.chdir(w)  # keep a handle on the real workdir before /tmp is covered
    m("-t", "tmpfs", "-o", "mode=1777,nosuid,nodev", "tmpfs", "/tmp")
    m("-t", "tmpfs", "-o", "mode=1777,nosuid,nodev", "tmpfs", "/dev/shm")
    os.makedirs(w, exist_ok=True)  # recreates the path when it lived under /tmp
    m("-c", "--bind", ".", w)      # -c: '.' resolves to the held cwd, not the path
    os.chdir(w)
    with open("/proc/self/mountinfo") as f:
        mps = sorted({unesc(ln.split()[4]) for ln in f})
    for mp in mps:
        if under(mp, w) or any(under(mp, k) for k in ("/tmp", "/proc", "/dev/shm", "/dev/pts")):
            continue
        m("-o", "remount,bind,ro", mp)
    home = os.path.expanduser("~")
    for name in __HIDE__:  # well-known credential stores: readable is enough to leak them
        p = os.path.join(home, name)
        if under(w, p) or not os.path.lexists(p):
            continue
        if os.path.isdir(p) and not os.path.islink(p):
            m("-t", "tmpfs", "-o", "ro,size=4k,mode=0", "tmpfs", p)
        elif os.path.isfile(p) and not os.path.islink(p):
            m("--bind", "/dev/null", p)
    if not net:
        import fcntl, socket, struct
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # SIOCGIFFLAGS / SIOCSIFFLAGS |= IFF_UP: a fresh netns starts with lo down
            req = struct.pack("16sH22x", b"lo", 0)
            flags = struct.unpack("16sH", fcntl.ioctl(s, 0x8913, req)[:18])[1]
            fcntl.ioctl(s, 0x8914, struct.pack("16sH22x", b"lo", flags | 1))
except SystemExit:
    raise
except BaseException as e:
    die(repr(e))
os.environ.update(TMPDIR="/tmp", XDG_CACHE_HOME="/tmp/.cache",
                  npm_config_cache="/tmp/.npm", GOCACHE="/tmp/.cache/go-build")
# We hold CAP_SYS_ADMIN over this mount ns, so the command could simply remount
# rw. Entering a NESTED user+mount namespace locks every mount we just made
# (kernel MNT_LOCK_*): its root can no longer undo the read-only fence or unmount
# the tmpfs covers.
try:
    os.execvp(unshare, [unshare, "--user", "--map-root-user", "--mount", "--", *cmd])
except OSError as e:
    die("nested unshare: %r" % e)
"""
_FENCE = (
    _FENCE.replace("__MARKER__", repr(_FENCE_SETUP_MARKER))
    .replace("__RC__", str(_FENCE_SETUP_RC))
    .replace("__HIDE__", repr(_HIDDEN_HOME_PATHS))
)


def _require_binary(
    cmd: tuple[str, ...] | list[str], workdir: Path, env: dict[str, str] | None
) -> None:
    """Missing toolchain -> ``GroundingUnavailable`` (ERROR), as an unwrapped run would
    raise — inside the sandbox it would otherwise surface as exit 127 (a FAIL)."""
    exe = cmd[0]
    if "/" in exe:
        found = os.access(workdir / exe, os.X_OK)
    else:
        path = (env if env is not None else os.environ).get("PATH", os.defpath)
        found = shutil.which(exe, path=path) is not None
    if not found:
        raise GroundingUnavailable(f"grounding tool not available: {exe!r}")


def run_confined(
    cmd: tuple[str, ...] | list[str],
    workdir: Path | str,
    *,
    network: bool = False,
    env: dict[str, str] | None = None,
    timeout_s: float = 600.0,
) -> tuple[int, str, str]:
    """Run ``cmd`` in the namespace sandbox with the filesystem fence (writes reach
    only ``workdir`` and private tmpfs; credential stores in ``$HOME`` masked; env
    allowlisted). ``network=True`` drops ``--net`` — reserved for the
    dependency-install step. Fails CLOSED like :func:`run_in_sandbox`."""
    if not cmd:
        raise GroundingUnavailable("empty grounding command")
    wd = Path(workdir).resolve()
    _require_binary(cmd, wd, env)
    mount_bin, unshare_bin = shutil.which("mount"), shutil.which(_UNSHARE_BIN)
    if not unshare_available() or mount_bin is None or unshare_bin is None:
        raise GroundingUnavailable(
            "namespace sandbox unavailable (no `unshare`/`mount`, or unprivileged user "
            "namespaces are kernel-disabled); set FOUNDRY_GATE_SANDBOX=inprocess to "
            "accept running build/smoke/install unsandboxed"
        )
    flags = UNSHARE_FLAGS if not network else tuple(f for f in UNSHARE_FLAGS if f != "--net")
    argv = [
        _UNSHARE_BIN, *flags,
        sys.executable, "-I", "-S", "-c", _FENCE,
        mount_bin, unshare_bin, str(wd), "1" if network else "0", *cmd,
    ]
    rc, out, err = run_grounding_command(argv, wd, env=_confined_env(env), timeout_s=timeout_s)
    if rc == _FENCE_SETUP_RC and _FENCE_SETUP_MARKER in err:
        raise GroundingUnavailable(f"sandbox fence setup failed: {err.strip()[-500:]}")
    return rc, out, err


def ground_untrusted(
    cmd: tuple[str, ...] | list[str],
    workdir: Path,
    *,
    network: bool = False,
    env: dict[str, str] | None = None,
    timeout_s: float = 600.0,
) -> tuple[int, str, str]:
    """The build/smoke/install grounding seam: confined sandbox by default; the SAME
    ``FOUNDRY_GATE_SANDBOX`` opt-out TestGate honours runs it directly on the host."""
    if sandbox_enabled():
        return run_confined(cmd, workdir, network=network, env=env, timeout_s=timeout_s)
    return run_grounding_command(cmd, workdir, env=env, timeout_s=timeout_s)
