"""Namespace sandbox for running untrusted generated code — the project-wide
isolation primitive (no Docker daemon, no image; just Linux user namespaces).

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

The sandbox is the DEFAULT; opt out with ``FOUNDRY_GATE_SANDBOX=inprocess`` on hosts
without unprivileged user namespaces. The opt-out still applies the env allowlist,
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
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
from base64 import b64encode
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from .runner import GroundingUnavailable, run_grounding_command

__all__ = [
    "UNSHARE_FLAGS",
    "egress_allowed",
    "ground_untrusted",
    "run_confined",
    "sandbox_enabled",
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
    ".netrc", ".npmrc", ".pypirc", ".cargo/credentials", ".cargo/credentials.toml",
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


class _EgressRelay:
    """Host-side end of the install step's only egress: an HTTP ``CONNECT`` relay on
    a private unix socket. Vets every destination with :func:`egress_allowed`, then
    chains to the upstream proxy or (no proxy) connects to a vetted resolved address."""

    def __init__(self, upstream: _Upstream | None) -> None:
        self._upstream = upstream
        self._dir = tempfile.mkdtemp(prefix="foundry-egress-")
        self.path = os.path.join(self._dir, "relay.sock")
        self._srv = socket.socket(socket.AF_UNIX)
        self._srv.bind(self.path)
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
    mount_bin, unshare_bin = shutil.which("mount"), shutil.which(_UNSHARE_BIN)
    if not unshare_available() or mount_bin is None or unshare_bin is None:
        raise GroundingUnavailable(
            "namespace sandbox unavailable (no `unshare`/`mount`, or unprivileged user "
            "namespaces are kernel-disabled); set FOUNDRY_GATE_SANDBOX=inprocess to "
            "accept running generated code unsandboxed"
        )
    bind_list = [[str(Path(p).resolve()), bool(rw)] for p, rw in binds]
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
    unless ``FOUNDRY_ALLOW_UNSANDBOXED_INSTALL=1`` (fail-closed ERROR)."""
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
