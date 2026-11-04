"""Sandbox for running untrusted generated code — the project-wide isolation
primitive (no Docker daemon, no image; Linux user namespaces, or Seatbelt on macOS).

Every gate that runs generated code over a workdir (test, bdd, build, smoke,
install) goes through ONE seam, ``ground_untrusted``, which runs it CONFINED
(``run_confined``) in an unprivileged ``unshare`` with its own network / mount /
PID / IPC / UTS namespaces and a clean process-tree teardown:

* **Filesystem fence.** Every mount except the workdir is remounted read-only;
  ``/tmp`` and ``/dev/shm`` are fresh private tmpfs; ``/run`` and ``/var/run``
  are masked; host unix sockets (``/proc/net/unix``) are masked with
  ``/dev/null``; well-known credential stores are masked (symlinks are followed
  to their target). A gate-owned file or directory OUTSIDE the workdir (the BDD
  report channel) is bind-mounted back at its own path, writable or read-only
  as the gate asks. The command then runs in a NESTED user+mount namespace,
  which locks all of that (kernel ``MNT_LOCK_*``): its root cannot remount rw.
* **Env allowlist.** Toolchain, locale, proxy and CA variables only — never
  the host's cloud/GitHub/API tokens. Gate-owned variables ride ``extra_env``.
* **Network.** Every step gets its own netns with loopback UP (so a scenario
  can boot a server on 127.0.0.1). Only the install step (``network=True``) has
  egress, and only through a gate-owned HTTPS ``CONNECT`` relay: a forwarder on
  the netns' loopback is bridged over a unix socket to a host-side relay, which
  refuses non-public destinations (loopback, link-local/metadata, private, ULA,
  ``*.internal``/``localhost`` names) and chains to the host ``HTTPS_PROXY`` when
  one is set (else connects directly to the vetted, already-resolved address).
  Host loopback services and abstract unix sockets live in the host netns, so
  they are unreachable by construction.

**macOS** has no namespaces: there the same seam runs the command under Seatbelt
(``/usr/bin/sandbox-exec`` with a generated SBPL profile, the approach of OpenAI's
Codex CLI). Writes reach only the workdir, the writable binds and a private
``TMPDIR``; host temp dirs are unlistable outside those, and the credential stores,
``~/Library/Keychains``, Foundry's ``.env``, ``.run`` and state/auth dirs are
unreadable. The network is loopback-only (no DNS); the install step's egress is the
same vetted relay, on a loopback port that demands a per-step credential. The
process group is killed when the command exits or times out. What Seatbelt cannot
give: loopback is the HOST's, so host services on 127.0.0.1 are reachable (except
host proxies named in the env and the Workshop's ``WORKSHOP_PORT``, denied by
port); there is no PID namespace, so a process that calls ``setsid()`` escapes the
group kill (it stays sandboxed); reads are allowed by default, so the credential
list remains a denylist; and tools that ignore ``TMPDIR`` for
``confstr(_CS_DARWIN_USER_TEMP_DIR)`` (BSD ``mktemp``) can't write temp.

The sandbox is the DEFAULT; opt out with ``FOUNDRY_GATE_SANDBOX=inprocess`` on hosts
with neither backend. The opt-out still applies the env allowlist,
and REFUSES the networked install step (ERROR) unless
``FOUNDRY_ALLOW_UNSANDBOXED_INSTALL=1`` explicitly accepts running it on the host.
If the sandbox is selected but unavailable, grounding fails CLOSED (ERROR) — never
a silent unsandboxed run.

Residual risks: the credential list is a denylist (an unlisted readable secret on
the host can still reach the install step's network); a hostname that resolves to
a private address is caught only in direct mode — behind an upstream proxy that
proxy's own policy decides; sockets bound after the ``/proc/net/unix`` scan, or by
other network namespaces outside ``/run``, are not masked.
"""

from __future__ import annotations

import contextlib
import contextvars
import hmac
import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from base64 import b64encode
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import IO
from urllib.parse import unquote, urlsplit, urlunsplit

from .runner import GroundingUnavailable, run_grounding_command

__all__ = [
    "UNSHARE_FLAGS",
    "ToolchainCache",
    "egress_allowed",
    "ground_untrusted",
    "run_confined",
    "sandbox_backend",
    "sandbox_enabled",
    "seatbelt_available",
    "seatbelt_profile",
    "toolchain_env",
    "toolchain_scope",
    "unshare_available",
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

_ALLOW_UNSANDBOXED_INSTALL_ENV = "FOUNDRY_ALLOW_UNSANDBOXED_INSTALL"


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


# Exit code + stderr marker the fence uses for its OWN setup failures, so they surface
# as ERROR (not-a-verdict) rather than the command's FAIL. A command that forges the
# marker only downgrades its own verdict to ERROR — never to PASS.
_FENCE_SETUP_RC = 125
_FENCE_SETUP_MARKER = "foundry-sandbox-fence: setup failed"

# Credential stores masked inside the fence (the install step has network, so any
# readable credential is exfiltratable). A denylist — see the residual note above.
_HIDDEN_HOME_PATHS: tuple[str, ...] = (
    ".ssh", ".aws", ".azure", ".config/gcloud", ".config/gh", ".config/git/credentials",
    ".docker", ".kube", ".gnupg", ".claude", ".claude.json", ".git-credentials",
    ".netrc", ".npmrc", ".pypirc", ".pip", ".config/pip", ".cargo/credentials",
    ".cargo/credentials.toml",
    ".m2/settings.xml", ".gradle/gradle.properties", ".ccr",
)
_HIDDEN_ABS_PATHS: tuple[str, ...] = ("/root/.ccr",)
# Public CA material kept (read-only) inside a masked dir, by the dir's basename: the
# install step needs the egress proxy's CA, not its credentials.
_KEEP_IN_MASKED: dict[str, tuple[str, ...]] = {
    ".ccr": ("ca-bundle.crt", "agent-proxy-ca.crt", "system-trust.ca.pem"),
}

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


_PROXY_VARS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    "npm_config_proxy", "npm_config_https_proxy", "YARN_HTTPS_PROXY",
})
_JAVA_PROXY_SECRET = re.compile(r"-D(?:http|https)\.proxy(?:User|Password)=\S*")


def _without_userinfo(url: str) -> str:
    """``http://user:pass@host:3128`` -> ``http://host:3128``: proxy credentials stay
    host-side (the egress relay adds ``Proxy-Authorization`` itself)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


def _confined_env(
    env: dict[str, str] | None, extra_env: dict[str, str] | None = None
) -> dict[str, str]:
    base = os.environ if env is None else env
    kept = {k: v for k, v in base.items() if k in _ENV_ALLOW or k.startswith("LC_")}
    for k in _PROXY_VARS & kept.keys():
        kept[k] = _without_userinfo(kept[k])
    if "JAVA_TOOL_OPTIONS" in kept:
        kept["JAVA_TOOL_OPTIONS"] = _JAVA_PROXY_SECRET.sub("", kept["JAVA_TOOL_OPTIONS"])
    return {**kept, **(extra_env or {})}


def toolchain_env() -> dict[str, str]:
    """The host's toolchain, proxy and CA settings (proxy userinfo stripped), minus
    the locations a caller sets itself (``PATH``, ``HOME``) and the host's active
    virtualenv (an install must not target it)."""
    return {k: v for k, v in _confined_env(None).items()
            if k not in ("PATH", "HOME", "VIRTUAL_ENV")}


def _host_unix_sockets(limit: int = 4096) -> list[str]:
    """Filesystem unix sockets bound in the HOST netns (read before ``unshare``):
    a read-only mount does not stop ``connect()``, so the fence masks each one."""
    try:
        with open("/proc/net/unix", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return []
    paths = {ln.split(None, 7)[7] for ln in lines if len(ln.split(None, 7)) == 8}
    return sorted(p for p in paths if p.startswith("/"))[:limit]


# Runs as the namespace's PID 1 child under ``python -I -S`` (no site, no cwd on
# sys.path: the workdir can't shadow a stdlib module), then execs the command.
# argv: <config json> <cmd...>
_FENCE = r"""
import json, os, stat, sys, subprocess
cfg = json.loads(sys.argv[1])
cmd = sys.argv[2:]
mount, w = cfg["mount"], cfg["workdir"]
def die(msg):
    sys.stderr.write("%s: %s\n" % (__MARKER__, msg)); sys.exit(__RC__)
def m(*a, fds=(), check=True):
    r = subprocess.run([mount, *a], capture_output=True, text=True, pass_fds=fds)
    if r.returncode and check: die("mount %s: %s" % (" ".join(a), r.stderr.strip()))
    return r.returncode == 0
def bind_fd(fd, target, rw):
    # -c: the magic /proc/self/fd link must reach the kernel uncanonicalized, so the
    # bind takes the object we opened even though its path is now covered
    m("-c", "--bind", "/proc/self/fd/%d" % fd, target, fds=(fd,))
    if not rw: m("-o", "remount,bind,ro", target)
def mountpoint(path, is_dir):
    if os.path.lexists(path): return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if is_dir: os.mkdir(path)
    else: open(path, "a").close()
def unesc(p):
    return p.encode().decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
def under(p, root):
    return p == root or p.startswith(root.rstrip("/") + "/")
try:
    os.chdir(w)  # keep a handle on the real workdir before /tmp is covered
    binds = [(p, rw, os.open(p, os.O_PATH)) for p, rw in cfg["binds"]]
    m("-t", "tmpfs", "-o", "mode=1777,nosuid,nodev", "tmpfs", "/tmp")
    m("-t", "tmpfs", "-o", "mode=1777,nosuid,nodev", "tmpfs", "/dev/shm")
    os.makedirs(w, exist_ok=True)  # recreates the path when it lived under /tmp
    m("-c", "--bind", ".", w)      # -c: '.' resolves to the held cwd, not the path
    os.chdir(w)
    for p, rw, fd in binds:        # gate-owned channels outside the workdir
        mountpoint(p, stat.S_ISDIR(os.fstat(fd).st_mode))
        bind_fd(fd, p, rw)
        os.close(fd)
    writable = [w] + [p for p, rw, _ in binds if rw]
    with open("/proc/self/mountinfo") as f:
        mps = sorted({unesc(ln.split()[4]) for ln in f})
    for mp in mps:
        if any(under(mp, k) for k in (*writable, "/tmp", "/proc", "/dev/shm", "/dev/pts")):
            continue
        m("-o", "remount,bind,ro", mp)
    for d in ("/run", "/var/run"):  # daemon sockets (docker, dbus, ...) and runtime secrets
        if os.path.islink(d) or not os.path.isdir(d):
            continue
        if any(under(p, d) for p in [w] + [b[0] for b in binds]):
            continue
        m("-t", "tmpfs", "-o", "ro,size=4k,mode=0755,nosuid,nodev,noexec", "tmpfs", d)
    home = os.path.expanduser("~")
    seen = set()
    for p in [os.path.join(home, n) for n in cfg["hide_home"]] + cfg["hide_abs"]:
        if not os.path.lexists(p):
            continue
        real = os.path.realpath(p)  # a symlinked store is masked at its target
        if real in seen or not os.path.exists(real) or under(w, real):
            continue
        seen.add(real)
        if os.path.isdir(real):
            keep = [k for k in cfg["keep"].get(os.path.basename(real), ())
                    if os.path.isfile(os.path.join(real, k))]
            fds = [(k, os.open(os.path.join(real, k), os.O_PATH)) for k in keep]
            if fds:
                m("-t", "tmpfs", "-o", "size=64k,mode=0755,nosuid,nodev", "tmpfs", real)
                for k, fd in fds:
                    t = os.path.join(real, k)
                    open(t, "w").close()
                    bind_fd(fd, t, False)
                    os.close(fd)
                m("-o", "remount,ro", real)
            else:
                m("-t", "tmpfs", "-o", "ro,size=4k,mode=0", "tmpfs", real)
        else:
            m("--bind", "/dev/null", real)
    for sp in cfg["sockets"]:      # read-only mounts do not stop connect()
        if under(sp, w) or under(sp, "/tmp"):
            continue
        try:
            if not stat.S_ISSOCK(os.stat(sp).st_mode):
                continue
        except OSError:
            continue
        if not m("--bind", "/dev/null", sp, check=False) and os.path.lexists(sp):
            die("could not mask socket %s" % sp)
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
if cfg["relay"]:
    cmd = [cfg["python"], "-I", "-S", "-c", cfg["forwarder"],
           cfg["relay"], str(cfg["proxy_port"]), *cmd]
# We hold CAP_SYS_ADMIN over this mount ns, so the command could simply remount
# rw. Entering a NESTED user+mount namespace locks every mount we just made
# (kernel MNT_LOCK_*): its root can no longer undo the read-only fence or unmount
# the tmpfs covers.
try:
    os.execvp(cfg["unshare"], [cfg["unshare"], "--user", "--map-root-user", "--mount",
                               "--", *cmd])
except OSError as e:
    die("nested unshare: %r" % e)
"""

# Install step only: runs INSIDE the nested namespace (same privilege as the command,
# so it offers no foothold into the outer, unlocked one). Listens on the netns'
# loopback, bridges every connection to the host relay's unix socket, points the
# proxy env at itself, then execs the command (which stays PID 1).
# argv: <relay socket> <port> <cmd...>
_FORWARDER = r"""
import os, sys, socket, threading
relay, port, cmd = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
try:
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", port))
    ls.listen(64)
except OSError as e:
    sys.stderr.write("%s: egress forwarder: %r\n" % (__MARKER__, e)); sys.exit(__RC__)
def pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass
def serve(c):
    try:
        u = socket.socket(socket.AF_UNIX)
        u.connect(relay)
    except OSError:
        c.close(); return
    t = threading.Thread(target=pump, args=(u, c), daemon=True)
    t.start(); pump(c, u); t.join(); c.close(); u.close()
if os.fork() == 0:
    try:
        nul = os.open("/dev/null", os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(nul, fd)
        while True:
            c, _ = ls.accept()
            threading.Thread(target=serve, args=(c,), daemon=True).start()
    finally:
        os._exit(0)
url = "http://127.0.0.1:%d" % ls.getsockname()[1]
ls.close()
for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
          "npm_config_https_proxy", "npm_config_proxy", "YARN_HTTPS_PROXY"):
    os.environ[k] = url
for k in ("NO_PROXY", "no_proxy", "npm_config_noproxy"):
    os.environ[k] = "localhost,127.0.0.1,::1"   # nothing else is reachable directly
for k in ("ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.execvp(cmd[0], cmd)
"""


def _fill(src: str) -> str:
    return src.replace("__MARKER__", repr(_FENCE_SETUP_MARKER)).replace(
        "__RC__", str(_FENCE_SETUP_RC)
    )


_FENCE = _fill(_FENCE)
_FORWARDER = _fill(_FORWARDER)


# ---- install egress: the host-side CONNECT relay ----------------------------------

_HOST_RE = re.compile(r"[a-z0-9_.-]+")
_BLOCKED_SUFFIXES = (".localhost", ".internal", ".local", ".localdomain", ".home.arpa")


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:  # inet_aton forms resolvers also accept: "2130706433", "0x7f.1", "127.1"
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def _ip_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def egress_allowed(host: str) -> bool:
    """Whether the install relay may open a tunnel to ``host``: public addresses and
    ordinary DNS names only — never loopback, link-local (cloud metadata), private,
    CGNAT, ULA (``fd00:ec2::254``) or local-only names."""
    h = host.strip().strip("[]").rstrip(".").lower()
    ip = _as_ip(h)
    if ip is not None:
        return _ip_public(ip)
    if not h or h == "localhost" or h.endswith(_BLOCKED_SUFFIXES):
        return False
    return _HOST_RE.fullmatch(h) is not None


def _split_target(target: str) -> tuple[str, int] | None:
    host, sep, port = target.rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536 or not host:
        return None
    return host.strip("[]"), int(port)


def _pump(a: socket.socket, b: socket.socket) -> None:
    with contextlib.suppress(OSError):
        while data := a.recv(65536):
            b.sendall(data)
    with contextlib.suppress(OSError):
        b.shutdown(socket.SHUT_WR)


class _Upstream:
    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError(url)
        self.host, self.port = parts.hostname, parts.port or 80
        self.auth = ""
        if parts.username is not None:
            cred = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
            self.auth = f"Proxy-Authorization: Basic {b64encode(cred.encode()).decode()}\r\n"

    @property
    def loopback(self) -> bool:
        return self.host in ("localhost", "127.0.0.1", "::1")


def _upstream_from(env: Mapping[str, str]) -> _Upstream | None:
    url = env.get("HTTPS_PROXY") or env.get("https_proxy")
    if not url:
        return None
    try:
        return _Upstream(url)
    except ValueError:
        return None


def _proxy_auth_ok(header: bytes, token: str) -> bool:
    want = b"basic " + b64encode(f"foundry:{token}".encode())
    for line in header.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"proxy-authorization":
            return hmac.compare_digest(value.strip().lower()[:6] + value.strip()[6:], want)
    return False


class _EgressRelay:
    """Host-side end of the install step's only egress: an HTTP ``CONNECT`` relay on
    a private unix socket. Vets every destination with :func:`egress_allowed`, then
    chains to the upstream proxy or (no proxy) connects to a vetted resolved address."""

    def __init__(self, upstream: _Upstream | None, *, tcp: bool = False) -> None:
        self._upstream = upstream
        self._dir = tempfile.mkdtemp(prefix="foundry-egress-")
        self.path = os.path.join(self._dir, "relay.sock")
        self._srv = socket.socket(socket.AF_INET if tcp else socket.AF_UNIX)
        self._srv.bind(("127.0.0.1", 0) if tcp else self.path)
        self.port: int = self._srv.getsockname()[1] if tcp else 0
        # A loopback TCP relay is reachable by every local process (other gates' offline
        # steps included), so it serves only the step holding this credential.
        self.token = os.urandom(16).hex() if tcp else ""
        self._srv.listen(64)
        self._srv.settimeout(0.2)
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self._srv.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(30)
            head = b""
            try:
                while b"\r\n\r\n" not in head:
                    chunk = conn.recv(4096)
                    if not chunk or len(head) > 16384:
                        return
                    head += chunk
                header, _, early = head.partition(b"\r\n\r\n")
                parts = header.split(b"\r\n", 1)[0].decode("latin-1").split()
                if len(parts) != 3 or parts[0] != "CONNECT":
                    conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                    return
                if self.token and not _proxy_auth_ok(header, self.token):
                    conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                                 b"Proxy-Authenticate: Basic realm=\"foundry\"\r\n"
                                 b"Connection: close\r\n\r\n")
                    return
                target = _split_target(parts[1])
                if target is None or not egress_allowed(target[0]):
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    return
                up = self._open(*target)
                if up is None:
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    return
            except OSError:
                return
            with up:
                conn.settimeout(None)
                up.settimeout(None)
                try:
                    if self._upstream is None:
                        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    if early:
                        up.sendall(early)
                except OSError:
                    return
                back = threading.Thread(target=_pump, args=(up, conn), daemon=True)
                back.start()
                _pump(conn, up)
                back.join()

    def _open(self, host: str, port: int) -> socket.socket | None:
        if self._upstream is not None:
            up = socket.create_connection((self._upstream.host, self._upstream.port), 30)
            hp = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            up.sendall(
                f"CONNECT {hp} HTTP/1.1\r\nHost: {hp}\r\n{self._upstream.auth}\r\n".encode()
            )
            return up
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        # Refuse if ANY answer is non-public (DNS pointing at metadata/loopback), and
        # connect to the vetted address itself: no second, re-bindable resolution.
        if not infos or not all(_ip_public(ipaddress.ip_address(i[4][0])) for i in infos):
            return None
        return socket.create_connection((str(infos[0][4][0]), port), 30)


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


# ---- macOS: the Seatbelt (sandbox-exec) backend -------------------------------------

_SEATBELT_BIN = "/usr/bin/sandbox-exec"
_SEATBELT_OK: bool | None = None
_HIDDEN_HOME_PATHS_DARWIN: tuple[str, ...] = ("Library/Keychains",)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_DIR_VARS = ("FOUNDRY_AUTH_DIR", "WORKSHOP_STATE_DIR", "FOUNDRY_STATE_DIR",
                   "WORKSHOP_OUTPUT_DIR")

# The base profile. Every host path reaches the profile as a ``-D`` parameter, never
# as profile text, so a build-chosen path cannot inject SBPL. Later rules win, which
# is what lets the workdir be re-allowed under a denied tree, but a rule naming one
# operation (``file-read-data``) outranks a wildcard (``file-read*``) wherever it
# sits: every read rule after the base names both.
_SEATBELT_BASE = """(version 1)
(deny default)
(allow process-exec process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow ipc-posix-sem)
(allow ipc-posix-shm-read* (ipc-posix-name-prefix "apple.cfprefs."))
(allow user-preference-read)
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.cfprefsd.daemon")
  (global-name "com.apple.cfprefsd.agent")
  (local-name "com.apple.cfprefsd.agent"))
(allow file-write-data file-ioctl
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/dtracehelper")
  (regex #"^/dev/fd/[0-9]+$"))
(allow file-read*)
(allow network-bind network-inbound (local ip "localhost:*"))
(allow network-outbound (remote ip "localhost:*"))
"""
# Install step only: TLS trust evaluation (Security.framework clients such as Go).
_SEATBELT_NETWORK = """(allow mach-lookup
  (global-name "com.apple.trustd.agent") (global-name "com.apple.ocspd"))
"""


def seatbelt_available() -> bool:
    """True iff this is macOS and ``sandbox-exec`` can apply a profile here (memoized;
    a process that is itself already sandboxed cannot)."""
    global _SEATBELT_OK
    if _SEATBELT_OK is None:
        _SEATBELT_OK = False
        if sys.platform == "darwin" and os.access(_SEATBELT_BIN, os.X_OK):
            with contextlib.suppress(GroundingUnavailable):
                rc, _out, _err = run_grounding_command(
                    (_SEATBELT_BIN, "-p", "(version 1)(allow default)", "/usr/bin/true"),
                    Path("."), env=None, timeout_s=30.0,
                )
                _SEATBELT_OK = rc == 0
    return _SEATBELT_OK


def sandbox_backend() -> str | None:
    """The confinement this host supports: ``"seatbelt"`` on macOS, ``"unshare"``
    elsewhere; ``None`` when it is unavailable (grounding then fails closed)."""
    if sys.platform == "darwin":
        return "seatbelt" if seatbelt_available() else None
    return "unshare" if unshare_available() else None


def _real(p: str | Path) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def _seatbelt_hidden(home: str | None) -> list[str]:
    """Credential stores, Foundry's own ``.env`` and state dir, at both their lexical
    and resolved paths (SBPL matches the resolved one)."""
    homes = {h for h in (home, os.path.expanduser("~"), pwd.getpwuid(os.getuid()).pw_dir)
             if h}
    paths = [os.path.join(h, n) for h in homes
             for n in (*_HIDDEN_HOME_PATHS, *_HIDDEN_HOME_PATHS_DARWIN)]
    paths += _HIDDEN_ABS_PATHS
    # Foundry's own state, wherever the CLI or the Workshop puts it (mirrors the
    # precedence in webapp/auth.py and webapp/server.py, which can't be imported
    # here): the Workshop's auth.json holds the session-cookie HMAC key. A relative
    # setting is resolved against the cwd and the repo root alike.
    rel = [".env", ".run", *(v for k in _STATE_DIR_VARS
                             if (v := os.environ.get(k, "").strip()))]
    paths += [str(Path(root, p).expanduser())
              for root in (os.getcwd(), _REPO_ROOT) for p in rel]
    out: set[str] = set()
    for p in paths:
        out.update((os.path.abspath(p), _real(p)))
    return sorted(out)


def _workshop_port() -> int:
    """The Workshop's HTTP port (``webapp.server`` reads the same variable): its API is
    on the host's loopback, which a Seatbelt step shares."""
    try:
        return int(os.environ.get("WORKSHOP_PORT", "8765"))
    except ValueError:
        return 8765


def _loopback_proxy_ports(env: Mapping[str, str]) -> set[int]:
    """Host proxies listening on loopback: the sandbox shares the host's loopback, so
    each is denied to it explicitly (the relay chains to them from the host side)."""
    ports: set[int] = set()
    for k in _PROXY_VARS:
        with contextlib.suppress(ValueError):
            parts = urlsplit(env.get(k, ""))
            if parts.hostname in ("localhost", "127.0.0.1", "::1"):
                ports.add(parts.port or 80)
    return ports


def seatbelt_profile(
    *,
    workdir: str,
    private_tmp: str,
    binds: list[tuple[str, bool]],
    home: str | None,
    network: bool,
    deny_ports: Iterable[int] = (),
) -> tuple[str, list[str]]:
    """The SBPL profile and its ``-D NAME=path`` arguments for one confined step."""
    params: list[str] = []

    def refs(paths: Iterable[str], kind: str = "(subpath {})") -> str:
        out = []
        for path in paths:
            if "\0" in path or not path.startswith("/"):
                raise GroundingUnavailable(f"refusing sandbox path {path!r}")
            name = f"P{len(params) // 2}"
            params.extend(("-D", f"{name}={path}"))
            out.append(kind.format(f'(param "{name}")'))
        return " ".join(out)

    tmp_roots = sorted({_real(p) for p in ("/tmp", "/var/tmp", tempfile.gettempdir())})
    trusted = sorted({_real(p) for p in (_REPO_ROOT, sys.prefix, sys.base_prefix,
                                         os.path.dirname(sys.executable))})
    hidden = _seatbelt_hidden(home)
    kept = [os.path.join(h, k) for h in hidden
            for k in _KEEP_IN_MASKED.get(os.path.basename(h), ())]
    writable = [workdir, private_tmp, *(p for p, rw in binds if rw)]
    readable = [workdir, private_tmp, *(p for p, _ in binds)]
    # getcwd() and realpath() list each ancestor directory: those stay listable.
    ancestors = sorted({str(a) for p in readable for a in Path(p).parents})
    rules = [
        _SEATBELT_BASE,
        _SEATBELT_NETWORK if network else "",
        # Host temp dirs hold other runs' workdirs and relays (Linux shadows /tmp): no
        # listing or reading them, while path lookups through them keep working.
        f"(deny file-read-data {refs(tmp_roots)})\n",
        f"(allow file-read* file-read-data {refs(trusted)})\n",
        f"(deny file-read* file-read-data file-write* {refs(hidden)})\n",
        f"(allow file-read* file-read-data {refs(kept)})\n" if kept else "",
        f"(allow file-read-data {refs(ancestors, '(literal {})')})\n",
        f"(allow file-read* file-read-data {refs(readable)})\n",
        f"(allow file-write* {refs(writable)})\n",
        "(allow network-bind network-inbound "
        f"{refs(writable, '(local unix-socket (subpath {}))')})\n",
        f"(allow network-outbound {refs(writable, '(remote unix-socket (subpath {}))')})\n",
    ]
    rules += [f'(deny network-outbound (remote ip "localhost:{int(port)}"))\n'
              for port in sorted(set(deny_ports))]
    return "".join(rules), params


def _run_session(
    argv: list[str], workdir: Path, env: dict[str, str], timeout_s: float
) -> tuple[int, str, str]:
    """``run_grounding_command`` in its own session, the whole process group killed
    when the command exits or times out (``--kill-child`` on Linux)."""
    try:
        proc = subprocess.Popen(
            argv, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise GroundingUnavailable(f"grounding tool not available: {argv[0]!r}") from exc
    bufs: tuple[list[bytes], list[bytes]] = ([], [])

    def drain(f: IO[bytes], buf: list[bytes]) -> None:
        with contextlib.suppress(OSError, ValueError):
            while chunk := f.read1(65536):  # type: ignore[attr-defined]
                buf.append(chunk)

    assert proc.stdout is not None and proc.stderr is not None
    readers = [threading.Thread(target=drain, args=(f, b), daemon=True)
               for f, b in zip((proc.stdout, proc.stderr), bufs, strict=True)]
    for t in readers:
        t.start()
    rc: int | None = None
    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        pass
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    for t in readers:
        t.join(5)  # a process that left the group may still hold the pipes
    out, err = b"".join(bufs[0]), b"".join(bufs[1])
    if rc is None:
        raise subprocess.TimeoutExpired(argv, timeout_s, out, err)
    return rc, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


def _run_seatbelt(
    cmd: tuple[str, ...] | list[str],
    wd: Path,
    *,
    network: bool,
    env: dict[str, str] | None,
    extra_env: dict[str, str] | None,
    binds: list[tuple[str, bool]],
    timeout_s: float,
) -> tuple[int, str, str]:
    host_env = os.environ if env is None else env
    run_env = _confined_env(env, extra_env)
    private_tmp = _real(tempfile.mkdtemp(prefix="foundry-sbx-"))
    upstream = _upstream_from(host_env) if network else None
    relay = _EgressRelay(upstream, tcp=True) if network else None
    try:
        run_env.update(TMPDIR=private_tmp, XDG_CACHE_HOME=f"{private_tmp}/.cache",
                       npm_config_cache=f"{private_tmp}/.npm",
                       GOCACHE=f"{private_tmp}/.cache/go-build")
        if relay is not None:
            url = f"http://foundry:{relay.token}@127.0.0.1:{relay.port}"
            for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                      "npm_config_https_proxy", "npm_config_proxy", "YARN_HTTPS_PROXY"):
                run_env[k] = url
            for k in ("NO_PROXY", "no_proxy", "npm_config_noproxy"):
                run_env[k] = "localhost,127.0.0.1,::1"
            for k in ("ALL_PROXY", "all_proxy"):
                run_env.pop(k, None)
        profile, params = seatbelt_profile(
            workdir=str(wd), private_tmp=private_tmp, binds=binds,
            home=host_env.get("HOME"), network=network,
            deny_ports=(_loopback_proxy_ports(host_env) | _loopback_proxy_ports(os.environ)
                        | {_workshop_port()}),
        )
        return _run_session(
            [_SEATBELT_BIN, *params, "-p", profile, "--", *cmd], wd, run_env, timeout_s
        )
    finally:
        if relay is not None:
            relay.close()
        shutil.rmtree(private_tmp, ignore_errors=True)


def run_confined(
    cmd: tuple[str, ...] | list[str],
    workdir: Path | str,
    *,
    network: bool = False,
    env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    binds: Iterable[tuple[Path | str, bool]] = (),
    timeout_s: float = 600.0,
) -> tuple[int, str, str]:
    """Run ``cmd`` in the namespace sandbox behind the filesystem fence (writes reach
    only ``workdir``, private tmpfs and the gate-owned ``binds`` marked writable;
    ``/run``, host sockets and credential stores masked; ``env`` allowlisted, then
    ``extra_env`` added verbatim). ``network=True`` — the dependency-install step —
    adds egress through the vetted CONNECT relay only. Fails CLOSED when the
    sandbox is unavailable."""
    if not cmd:
        raise GroundingUnavailable("empty grounding command")
    wd = Path(workdir).resolve()
    _require_binary(cmd, wd, env)
    bind_list = [[str(Path(p).resolve()), bool(rw)] for p, rw in binds]
    backend = sandbox_backend()
    if backend == "seatbelt":
        return _run_seatbelt(
            cmd, wd, network=network, env=env, extra_env=extra_env,
            binds=[(str(p), bool(rw)) for p, rw in bind_list], timeout_s=timeout_s,
        )
    mount_bin, unshare_bin = shutil.which("mount"), shutil.which(_UNSHARE_BIN)
    if backend != "unshare" or mount_bin is None or unshare_bin is None:
        raise GroundingUnavailable(
            "gate sandbox unavailable (Linux: no `unshare`/`mount`, or unprivileged user "
            "namespaces are kernel-disabled; macOS: `sandbox-exec` missing or cannot "
            "apply a profile here); set FOUNDRY_GATE_SANDBOX=inprocess to accept running "
            "generated code unsandboxed"
        )
    upstream = _upstream_from(os.environ if env is None else env) if network else None
    relay = _EgressRelay(upstream) if network else None
    try:
        cfg = {
            "mount": mount_bin,
            "unshare": unshare_bin,
            "python": sys.executable,
            "workdir": str(wd),
            "binds": bind_list + ([[os.path.dirname(relay.path), False]] if relay else []),
            "hide_home": _HIDDEN_HOME_PATHS,
            "hide_abs": _HIDDEN_ABS_PATHS,
            "keep": _KEEP_IN_MASKED,
            "sockets": _host_unix_sockets(),
            "relay": relay.path if relay else None,
            "proxy_port": upstream.port if upstream is not None and upstream.loopback else 0,
            "forwarder": _FORWARDER,
        }
        argv = [
            _UNSHARE_BIN, *UNSHARE_FLAGS,
            sys.executable, "-I", "-S", "-c", _FENCE, json.dumps(cfg), *cmd,
        ]
        rc, out, err = run_grounding_command(
            argv, wd, env=_confined_env(env, extra_env), timeout_s=timeout_s
        )
    finally:
        if relay is not None:
            relay.close()
    if rc == _FENCE_SETUP_RC and _FENCE_SETUP_MARKER in err:
        raise GroundingUnavailable(f"sandbox fence setup failed: {err.strip()[-500:]}")
    return rc, out, err


# ---- the run's toolchain cache: dependencies the install step downloads ------------


class ToolchainCache:
    """Where non-Python toolchains keep downloaded dependencies (Go modules, the cargo
    registry, NuGet, Maven, Gradle, Yarn/pnpm stores) for gate steps: a directory
    OUTSIDE the workdir and the user's home (which the sandbox leaves read-only, and
    whose real caches are never copied in). The install step, the only networked
    one, writes it; the offline build/test/smoke steps read it. One per run, created
    on first use, removed by :meth:`close`."""

    def __init__(self) -> None:
        self._root: str | None = None
        self._lock = threading.Lock()

    @property
    def root(self) -> str:
        with self._lock:
            if self._root is None:
                self._root = _real(tempfile.mkdtemp(prefix="foundry-toolchain-"))
            return self._root

    def env(self) -> dict[str, str]:
        r = self.root
        return {
            "GOPATH": f"{r}/go", "GOMODCACHE": f"{r}/go/pkg/mod",
            "CARGO_HOME": f"{r}/cargo",
            "NUGET_PACKAGES": f"{r}/nuget", "DOTNET_CLI_HOME": f"{r}/dotnet",
            "MAVEN_OPTS": f"-Dmaven.repo.local={r}/m2", "GRADLE_USER_HOME": f"{r}/gradle",
            "YARN_CACHE_FOLDER": f"{r}/yarn", "npm_config_store_dir": f"{r}/pnpm",
        }

    def close(self) -> None:
        with self._lock:
            root, self._root = self._root, None
        if root is None:
            return
        # Go extracts modules read-only: make them removable first.
        for d, _dirs, _files in os.walk(root):
            with contextlib.suppress(OSError):
                os.chmod(d, 0o700)
        shutil.rmtree(root, ignore_errors=True)


_OFFLINE_ENV = {"CARGO_NET_OFFLINE": "true"}

_TOOLCHAIN: contextvars.ContextVar[ToolchainCache | None] = contextvars.ContextVar(
    "foundry_toolchain_cache", default=None
)


@contextlib.contextmanager
def toolchain_scope(cache: ToolchainCache | None = None) -> Iterator[ToolchainCache]:
    """Gate steps run inside share ``cache`` (the run's). Without one, the scope
    already active is reused, else a fresh cache lives for this scope only."""
    active = _TOOLCHAIN.get()
    if cache is None and active is not None:
        yield active
        return
    use = cache or ToolchainCache()
    token = _TOOLCHAIN.set(use)
    try:
        yield use
    finally:
        _TOOLCHAIN.reset(token)
        if cache is None:
            use.close()


def ground_untrusted(
    cmd: tuple[str, ...] | list[str],
    workdir: Path,
    *,
    network: bool = False,
    env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    binds: Iterable[tuple[Path | str, bool]] = (),
    timeout_s: float = 600.0,
) -> tuple[int, str, str]:
    """The grounding seam for generated code (test, bdd, build, smoke, install):
    confined sandbox by default. The ``FOUNDRY_GATE_SANDBOX`` opt-out runs it on the
    host — still with the env allowlist — and refuses the networked install step
    unless ``FOUNDRY_ALLOW_UNSANDBOXED_INSTALL=1`` (fail-closed ERROR). Inside a
    :func:`toolchain_scope` the toolchains' dependency caches point at its cache,
    writable only by the networked step."""
    if not network:
        # Offline steps have no DNS: cargo would retry the registry and fail on a
        # host lookup; offline mode names the missing dependency instead.
        extra_env = {**_OFFLINE_ENV, **(extra_env or {})}
    if (cache := _TOOLCHAIN.get()) is not None:
        extra_env = {**cache.env(), **(extra_env or {})}
        binds = (*binds, (cache.root, network))
    if sandbox_enabled():
        return run_confined(
            cmd, workdir, network=network, env=env, extra_env=extra_env, binds=binds,
            timeout_s=timeout_s,
        )
    if network and os.environ.get(_ALLOW_UNSANDBOXED_INSTALL_ENV, "").strip() != "1":
        raise GroundingUnavailable(
            "refusing to run the networked install step unsandboxed "
            "(FOUNDRY_GATE_SANDBOX opt-out): set FOUNDRY_ALLOW_UNSANDBOXED_INSTALL=1 to "
            "accept it reaching the host network and filesystem"
        )
    return run_grounding_command(
        cmd, workdir, env=_confined_env(env, extra_env), timeout_s=timeout_s
    )
