"""Launch gate — class 2 (execution-deterministic).

Foundry, not a model-written script, starts the app the way a user would and
checks that it works. It runs in the same confined sandbox as smoke
(``sandbox.ground_untrusted``: loopback only, writes fenced to the workdir).
Inside the sandbox a trusted driver (:data:`_DRIVER`, Foundry's code) starts the
app's command in its own process group and drives it:

* **http** — ``PORT``/``HOST`` name a free loopback port. The driver waits for the
  port to listen, then browses it as one user: a cookie jar, same-origin redirects
  followed, ``/`` and the entrypoint scenarios' paths and the links they serve
  crawled. It submits each distinct form a page renders once (hidden fields such
  as a CSRF token or ``_method`` kept, the rest filled by type/name), signup
  before login before the rest with the same credentials, then re-crawls what
  sign-in reveals and submits those forms too (no form at all: a scenario's POST
  path). A burst of parallel requests (an authenticated page, a repeat of a
  working form POST) follows. Then it kills the whole group. Requests and time
  are capped. FAIL when the app exits early or never listens, a request is
  refused, any response (burst included) is a 5xx, a form the app rendered is
  answered 405, or the app dies under the requests.
* **cli** — the driver runs the entrypoint scenarios' commands (else
  ``<cmd> --help``). FAIL on a traceback, a missing-entry/import error, a
  command that never exits with stdin closed, or (an app that must launch) a
  ``--help`` that exits non-zero. A scenario command's own non-zero exit is the
  scenario's answer, not a launch verdict.

When the stack declares the spec's ``interface``, a cli option/subcommand its
``--help`` does not list FAILs (an unrequested extra option is advisory only), and
an http route that answers 404/405/501 FAILs.

An app that must launch (a declared or inferred cli/http kind) but has no
runnable entry FAILs; a library, or a build with nothing to launch, gets an
attested "not pinned" PASS. The core is language-neutral: the profile/stack
supplies the command, and only the fallback entry derivation knows file names.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe, timeout_findings
from .sandbox import ground_untrusted

if TYPE_CHECKING:
    from foundry.schemas.implementation import StackDecision, Ticket
    from foundry.schemas.product import AcceptanceCriterion

    from .profiles import VerificationProfile
    from .pydeps import PythonBuildEnv

__all__ = ["LaunchGate", "LaunchTarget", "entry_run", "launch_target"]

KINDS = ("cli", "http")
LAUNCH_KEY = "launch"
_RESULT_MARK = "FOUNDRY-LAUNCH-RESULT "
_MAX_GETS = 6
_MAX_RUNS = 3
# The http probe's caps: sequential requests (redirect hops included), pages
# crawled per pass (anonymous, then signed in), forms submitted, burst size.
_MAX_REQUESTS = 80
_MAX_PAGES = 10
_MAX_FORMS = 12
_BURST = 12

# The trusted driver: argv[1] is its JSON config; its last stdout line is the result.
_LAUNCH_ID_ENV = "FOUNDRY_LAUNCH_ID"

_DRIVER = r"""
import html, http.client, json, os, re, signal, socket, subprocess, sys, threading, time
LAUNCH_ID_ENV = "FOUNDRY_LAUNCH_ID"
from urllib.parse import urlencode, urljoin, urlsplit

cfg = json.loads(sys.argv[1])
# Every process the app starts inherits this, so the host can find a daemonized
# one that left the process group (macOS has no PID namespace to reap it).
os.environ[LAUNCH_ID_ENV] = cfg["launch_id"]
MARK = cfg["mark"]
TAIL = 4000


def spawn(cmd, env):
    proc = subprocess.Popen(
        ["/bin/sh", "-c", cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=env, start_new_session=True,
    )
    buf = []

    def drain():
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                buf.append(chunk)
        except (OSError, ValueError):
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    return proc, buf, t


def kill(proc):
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    try:  # a child that outlived the leader still holds the group
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def text(buf, t):
    t.join(2)
    return b"".join(buf).decode("utf-8", "replace")[-TAIL:]


def run_cli():
    runs = []
    for cmd in cfg["runs"]:
        proc, buf, t = spawn(cmd, dict(os.environ))
        try:
            rc = proc.wait(timeout=cfg["cli_s"])
        except subprocess.TimeoutExpired:
            rc = None
        kill(proc)
        runs.append({"cmd": cmd, "rc": rc, "output": text(buf, t)})
    return {"runs": runs}


FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
ATTR = re.compile(r'([\w:-]+)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)')
INPUT = re.compile(r"<(input|textarea|button)\b([^>]*)>", re.I)
SELECT = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.I | re.S)
OPTION = re.compile(r"<option\b([^>]*)>", re.I)
LINK = re.compile(r"<a\b([^>]*)>", re.I)
EMAIL, PASSWORD, USER = "launch-check@example.com", "Launch-check-Pass-1", "launchcheck"
SIGNUP = re.compile(r"sign.?up|register|join|create.?account|new.?user|enrol", re.I)
LOGOUT = re.compile(r"log.?out|sign.?out", re.I)
LEAVE = re.compile(r"log.?out|sign.?out|delete|destroy|remove", re.I)
ASSET = re.compile(r"\.(css|js|mjs|map|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|pdf|zip)$", re.I)
REDIRECTS = (301, 302, 303, 307, 308)
DATES = {"date": "2024-01-01", "datetime-local": "2024-01-01T12:00", "time": "12:00",
         "month": "2024-01", "week": "2024-W01"}


def attrs(raw):
    return {k.lower(): html.unescape(v.strip("\"'")) for k, v in ATTR.findall(raw)}


def fill(name, kind, a):
    n = name.lower()
    if kind == "email" or "email" in n:
        v = EMAIL
    elif kind == "password" or "pass" in n:
        v = PASSWORD
    elif kind in ("number", "range") or re.search(r"price|amount|qty|quantity|count|stock", n):
        v = a.get("min") or "1"
    elif kind == "url" or n.endswith("url") or "website" in n:
        v = "http://example.com"
    elif kind in DATES:
        v = DATES[kind]
    elif kind == "tel" or "phone" in n:
        v = "5555550100"
    elif kind == "color":
        v = "#336699"
    elif kind == "search" or n in ("q", "query", "search"):
        v = "launch"
    elif "user" in n or n in ("login", "handle", "nick", "nickname"):
        v = USER
    elif "title" in n or n == "name":
        v = "Launch check"
    else:
        v = "launch check text"
    ml = a.get("maxlength", "")
    return v[: int(ml)] if ml.isdigit() and int(ml) > 0 else v


class Session:
    def __init__(self, port):
        self.port = port
        self.cookies = {}
        self.lock = threading.Lock()
        self.sent = 0
        self.deadline = time.monotonic() + cfg["probe_s"]

    def take(self, limit):
        with self.lock:
            if self.sent >= limit or time.monotonic() > self.deadline:
                return False
            self.sent += 1
            return True

    def keep(self, resp):
        with self.lock:
            for k, v in resp.getheaders():
                if k.lower() != "set-cookie":
                    continue
                name, _, rest = v.partition("=")
                value = rest.split(";", 1)[0].strip()
                if not value or re.search(r"max-age=0\b|expires=thu, 01[ -]jan[ -]1970", v, re.I):
                    self.cookies.pop(name.strip(), None)
                else:
                    self.cookies[name.strip()] = value

    def local(self, base, loc):
        u = urlsplit(urljoin(f"http://127.0.0.1:{self.port}{base}", loc))
        if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost"):
            return None
        if (u.port or 80) != self.port:
            return None
        return (u.path or "/") + (f"?{u.query}" if u.query else "")

    def once(self, method, path, form, limit):
        if not self.take(limit):
            return None
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=cfg["request_s"])
        h = {"Connection": "close", "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"}
        body = None
        if form is not None:
            body = urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        with self.lock:
            if self.cookies:
                h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        try:
            conn.request(method, path, body=body, headers=h)
            resp = conn.getresponse()
            self.keep(resp)
            data = resp.read(262144).decode("utf-8", "replace")
            ctype = resp.getheader("Content-Type", "") or ""
            return {"status": resp.status, "full": data, "html": "html" in ctype.lower(),
                    "location": resp.getheader("Location", "") or ""}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            conn.close()

    def go(self, method, path, form=None, follow=5, limit=None):
        # Same-origin redirects are followed as a browser does (a 307/308 keeps the
        # method, any other turns a POST into a GET); the record is the final answer.
        limit = cfg["max_requests"] if limit is None else limit
        r = self.once(method, path, form, limit)
        if r is None:
            return None
        at_m, at = method, path
        while r.get("status") in REDIRECTS and follow > 0 and r.get("location"):
            nxt = self.local(at, r["location"])
            if nxt is None:
                break
            if r["status"] not in (307, 308):
                at_m, form = "GET", None
            nxt_r = self.once(at_m, nxt, form, limit)
            if nxt_r is None:
                break
            at, r, follow = nxt, nxt_r, follow - 1
        rec = {"method": method, "path": path, **r}
        rec.pop("location", None)
        if (at_m, at) != (method, path):
            rec["at"] = f"{at_m} {at}"
        rec["page"] = at if at_m == "GET" else None
        return rec


def norm(path):
    return re.sub(r"\d+", "N", urlsplit(path).path)


def parse_forms(page, body):
    out = []
    for head, inner in FORM.findall(body):
        a = attrs(head)
        # A browser sends any method but POST as a GET.
        method = "POST" if a.get("method", "get").lower() == "post" else "GET"
        u = urlsplit(urljoin(page, a.get("action") or page))
        action = (u.path or "/") + (f"?{u.query}" if u.query else "")
        fields, pw, submit = [], 0, None
        for tag, raw in INPUT.findall(inner):
            f = attrs(raw)
            name, kind = f.get("name"), f.get("type", "text").lower()
            if not name or re.search(r"(?<![\w-])disabled(?![\w-])", raw, re.I):
                continue
            if tag.lower() == "button" or kind in ("submit", "image"):
                if submit is None and "value" in f:
                    submit = (name, f["value"])
                continue
            if kind in ("file", "reset", "button"):
                continue
            if kind == "password":
                pw += 1
            if kind == "hidden":
                fields.append((name, f.get("value", "")))
            elif kind in ("checkbox", "radio"):
                if name not in (n for n, _ in fields):
                    fields.append((name, f.get("value", "on")))
            else:
                fields.append((name, f.get("value") or fill(name, kind, f)))
        for raw, opts in SELECT.findall(inner):
            f = attrs(raw)
            values = [v for v in (attrs(o).get("value") for o in OPTION.findall(opts)) if v]
            if f.get("name"):
                fields.append((f["name"], values[0] if values else "1"))
        if submit is not None:
            fields.append(submit)
        override = next((v.upper() for n, v in fields if n.lower() == "_method"), "")
        if LOGOUT.search(action):
            rank = 4
        elif LEAVE.search(action) or override == "DELETE":
            rank = 3
        elif pw >= 2 or SIGNUP.search(action):
            rank = 0
        elif pw == 1:
            rank = 1
        else:
            rank = 2
        key = (method, override, norm(action), " ".join(sorted(n for n, _ in fields)))
        out.append({"page": page, "method": method, "action": action, "fields": fields,
                    "override": override, "rank": rank, "key": key})
    return out


def parse_links(page, body, local):
    out = []
    for raw in LINK.findall(body):
        href = attrs(raw).get("href", "")
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        p = local(page, href.split("#", 1)[0])
        if p and not LEAVE.search(p) and not ASSET.search(urlsplit(p).path):
            out.append(p)
    return out


def probe(s, out, signed_in):
    reqs = out["requests"]
    seen, order, queue = set(), [], []
    pending, done = [], set()
    st = {"pages": 0, "forms": 0, "authed": False, "recrawled": False, "page": None, "post": None}

    def harvest(r):
        if not (r.get("html") and r.get("status", 500) < 400 and r.get("page")):
            return
        for f in parse_forms(r["page"], r.get("full", "")):
            if f["key"] not in done and all(f["key"] != p["key"] for p in pending):
                pending.append(f)
        for link in parse_links(r["page"], r.get("full", ""), s.local):
            if link not in seen and link not in queue:
                queue.append(link)

    def visit(path):
        if path in seen or st["pages"] >= cfg["max_pages"]:
            return
        seen.add(path)
        order.append(path)
        st["pages"] += 1
        r = s.go("GET", path)
        if r is not None:
            reqs.append(r)
            harvest(r)
            if st["authed"] and st["page"] is None and r.get("html") and r.get("status") == 200:
                st["page"] = r["page"]

    def submit(f):
        # Re-fetch the form's page first: its CSRF token, and the session that
        # token is bound to, may have rotated (signup, login) since it was found.
        page = s.go("GET", f["page"])
        if page is None:
            return False
        reqs.append(page)
        found = parse_forms(page["page"], page.get("full", "")) if page.get("page") else []
        fresh = [g for g in found if g["key"] == f["key"]]
        if not fresh:
            return True  # the page no longer offers it (a login form, once signed in)
        g = fresh[0]
        if g["method"] == "GET":
            sep = "&" if "?" in g["action"] else "?"
            r = s.go("GET", g["action"] + sep + urlencode(g["fields"]))
        else:
            r = s.go("POST", g["action"], g["fields"])
        if r is None:
            return False
        r["form"] = {"page": g["page"], "method": g["method"], "override": g["override"]}
        reqs.append(r)
        st["forms"] += 1
        ok = "error" not in r and r.get("status", 500) < 400
        if g["rank"] < 2 and ok:
            st["authed"] = True
        if g["rank"] == 2 and g["method"] == "POST" and ok:
            st["post"] = (g["action"], g["fields"])
        harvest(r)
        return True

    def drive(final):
        while True:
            while queue and st["pages"] < cfg["max_pages"]:
                visit(queue.pop(0))
            pending.sort(key=lambda p: p["rank"])
            nxt = [p for p in pending if final or p["rank"] < 4]
            if not nxt or st["forms"] >= cfg["max_forms"]:
                return
            pending.remove(nxt[0])
            done.add(nxt[0]["key"])
            if not submit(nxt[0]):
                return
            if st["authed"] and not st["recrawled"] and all(p["rank"] >= 2 for p in pending):
                # Signed in: the pages seen anonymously now show what is behind auth.
                st["recrawled"] = True
                st["pages"] = 0
                queue[:0] = [p for p in order if p not in queue]
                seen.clear()

    for _, path in cfg["probes"]:
        visit(path)
    drive(False)
    if st["forms"] == 0 and cfg["posts"]:
        r = s.go("POST", cfg["posts"][0], {})
        if r is not None:
            reqs.append(r)
    signed_in(st)  # the declared routes and the burst, while the session still holds
    drive(True)
    return st


def burst(s, st):
    # Requests in parallel against shared state the sequential probe never races.
    page = st["page"] or "/"
    jobs = [
        ("POST", *st["post"]) if st["post"] and i % 2 else ("GET", page, None)
        for i in range(cfg["burst"])
    ]
    start = threading.Barrier(len(jobs))
    results = [None] * len(jobs)
    limit = s.sent + len(jobs) * 2

    def one(i, job):
        try:
            start.wait(cfg["request_s"])
        except threading.BrokenBarrierError:
            pass
        results[i] = s.go(job[0], job[1], job[2], follow=1, limit=limit)

    s.deadline = max(s.deadline, time.monotonic() + cfg["request_s"])
    threads = [threading.Thread(target=one, args=(i, j), daemon=True) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(cfg["request_s"] * 2 + 2)
    got = [r for r in results if r is not None]
    statuses = {}
    for r in got:
        k = str(r.get("status", "error"))
        statuses[k] = statuses.get(k, 0) + 1
    bad = [r for r in got if r.get("status", 0) >= 500]
    return {"sent": len(got), "statuses": statuses, "failures": bad[:3], "count": len(bad)}


def excerpt(body):
    i = body.rfind("Traceback (most recent call last)")
    if i >= 0:
        return re.sub(r"<[^>]+>", "", body[i:])[-800:]
    return body[:600]


def run_http():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, PORT=str(port), HOST="127.0.0.1")
    proc, buf, t = spawn(cfg["cmd"], env)
    out = {"port": port, "boot": "timeout", "requests": []}
    deadline = time.monotonic() + cfg["boot_s"]
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out["boot"] = "exited"
            out["rc"] = proc.returncode
            break
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            out["boot"] = "listening"
            break
        except OSError:
            time.sleep(0.2)
    if out["boot"] == "listening":
        sess = Session(port)

        def signed_in(st):
            out["routes"] = []
            for m, p in cfg["routes"]:  # each declared route is asked once, whatever the budget
                sess.deadline = max(sess.deadline, time.monotonic() + 1)
                r = sess.go(m, p, {} if m != "GET" else None, follow=0, limit=sess.sent + 1)
                out["routes"].append(r or {"method": m, "path": p, "error": "probe budget spent"})
            if proc.poll() is None and cfg["burst"]:
                out["burst"] = burst(sess, st)

        probe(sess, out, signed_in)
        out["sent"] = sess.sent
        for r in out["routes"]:
            for k in ("full", "html", "page"):
                r.pop(k, None)
        for r in out["requests"] + out.get("burst", {}).get("failures", []):
            r["body"] = excerpt(r.pop("full", ""))
            r.pop("html", None)
            r.pop("page", None)
        out["alive"] = proc.poll() is None
        if not out["alive"]:
            out["rc"] = proc.returncode
    kill(proc)
    out["output"] = text(buf, t)
    return out


result = run_cli() if cfg["kind"] == "cli" else run_http()
result["kind"] = cfg["kind"]
sys.stdout.write("\n" + MARK + json.dumps(result) + "\n")
sys.stdout.flush()
"""


@dataclass(frozen=True)
class LaunchTarget:
    """What the launch gate starts and how: ``kind`` ("cli" | "http" | ""), the
    shell line ``cmd`` ("" = no runnable entry), whether the app must launch
    (``required``: a declared or inferred app kind), the http ``probes``
    ((method, path), GET only) and ``posts`` (scenario POST paths), the cli
    ``runs``, and the files the entry ``needs`` (derived from planned files)."""

    kind: str = ""
    cmd: str = ""
    required: bool = False
    probes: tuple[tuple[str, str], ...] = (("GET", "/"),)
    posts: tuple[str, ...] = ()
    runs: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()
    source: str = ""  # where the command came from, for the findings
    interface: tuple[str, ...] = ()  # the spec's options/subcommands or routes


# ---- the target: stack fields, else the run command, else the entry's files ----

_SMOKE_LIKE = re.compile(r"\b(smoke|tests?|pytest|check|verify)\b|&&|;|\|\|")


def _server_run(stack: StackDecision) -> bool:
    from foundry.agents.planner.checks import _APP_ENTRY, _SERVER_COMMAND, _WEB_FRAMEWORK

    run = " ".join(stack.run.split())
    return bool(
        _SERVER_COMMAND.match(run)
        or (_APP_ENTRY.match(run) and _WEB_FRAMEWORK.search(stack.framework))
    )


def _web_stack(stack: StackDecision) -> bool:
    from foundry.agents.planner.checks import _WEB_FRAMEWORK

    return bool(_WEB_FRAMEWORK.search(stack.framework)) or _server_run(stack)


def entry_run(stack: StackDecision, kind: str) -> str:
    """``stack.run`` when it starts the app itself (a server entry for http, a plain
    CLI invocation for cli), not a smoke script; else ""."""
    if not stack.run.strip():
        return ""
    if kind == "http":
        return stack.run if _server_run(stack) else ""
    return stack.run if kind == "cli" and not _SMOKE_LIKE.search(stack.run) else ""


def _kind(stack: StackDecision | None, deliverable: str) -> tuple[str, bool]:
    """(kind, required): a declared kind or an inferred web stack / declared
    deliverable must launch; anything else is a CLI if it has an entry at all."""
    if stack is not None and stack.kind in KINDS:
        return stack.kind, True
    if deliverable == "library":
        return "", False
    if stack is not None and _web_stack(stack):
        return "http", True
    if deliverable == "service":
        return "http", True
    if deliverable == "cli":
        return "cli", True
    return "cli", False


_PY_SCRIPTS = ("__main__.py", "main.py", "app.py", "server.py", "cli.py", "run.py", "wsgi.py")
_JS_SCRIPTS = tuple(
    f"{n}.{x}" for n in ("server", "index", "app", "main", "cli") for x in ("js", "mjs")
)


def _default_entry(language: str, files: Sequence[str], python: str) -> tuple[str, str]:
    """(command, the file it needs) for the entry the planned ``files`` hold, or
    ("", "") when none is recognizable."""
    paths = sorted({f.removeprefix("./") for f in files}, key=lambda p: (p.count("/"), p))
    if language == "python":
        for name in _PY_SCRIPTS:
            for p in paths:
                pp = PurePosixPath(p)
                if pp.name != name or "test" in pp.parts[0]:
                    continue
                if name == "__main__.py" and len(pp.parts) > 1:
                    return f"{shlex.quote(python)} -m {'.'.join(pp.parts[:-1])}", p
                if name != "__main__.py":
                    return f"{shlex.quote(python)} {shlex.quote(p)}", p
    elif language in ("javascript", "typescript"):
        for name in _JS_SCRIPTS:
            for p in paths:
                if PurePosixPath(p).name == name and p.count("/") <= 1:
                    return f"node {shlex.quote(p)}", p
    elif language == "go":
        for p in paths:
            pp = PurePosixPath(p)
            if pp.name == "main.go" and (len(pp.parts) == 1 or pp.parts[0] == "cmd"):
                return ("go run ." if len(pp.parts) == 1 else f"go run ./{pp.parent}"), p
    return "", ""


_HTTP_STEP = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s\"'`),]*)")
_QUOTED_PATH = re.compile(r"[\"'`](/[\w\-./?=&%]*)[\"'`]")
_QUOTED = re.compile(r"`([^`]+)`|\"([^\"]+)\"")
_INTERPRETERS = frozenset(
    {"python", "python3", "node", "go", "npx", "npm", "cargo", "java", "dotnet"}
)


def _probes(lines: Iterable[str]) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    gets: list[str] = ["/"]
    posts: list[str] = []
    for line in lines:
        for method, path in _HTTP_STEP.findall(line):
            if re.search(r"[<{]", path):
                continue
            (gets if method == "GET" else posts if method == "POST" else []).append(path)
        gets += [p for p in _QUOTED_PATH.findall(line) if not re.search(r"[<{]", p)]
    gets = list(dict.fromkeys(gets))[:_MAX_GETS]
    return tuple(("GET", p) for p in gets), tuple(dict.fromkeys(posts))


def _cli_runs(cmd: str, lines: Iterable[str]) -> tuple[str, ...]:
    """The scenarios' quoted commands that invoke this app: one starting with the
    launch command, or with its program name (``dice roll`` for ``python -m dice``,
    run as ``python -m dice roll``)."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return ()
    if not argv:
        return ()
    names = {PurePosixPath(argv[-1]).stem, PurePosixPath(argv[0]).name} - _INTERPRETERS - {""}
    out: list[str] = []
    for line in lines:
        for a, b in _QUOTED.findall(line):
            text = " ".join((a or b).split())
            if text.startswith(" ".join(cmd.split())):
                out.append(text)
                continue
            try:
                words = shlex.split(text)
            except ValueError:
                continue
            if words and words[0] in names:
                out.append(" ".join([cmd, *map(shlex.quote, words[1:])]))
    return tuple(dict.fromkeys(out))[:_MAX_RUNS]


def launch_target(
    stack: StackDecision | None,
    profile: VerificationProfile | None,
    *,
    tickets: Sequence[Ticket] = (),
    criteria: Iterable[AcceptanceCriterion] = (),
    deliverable: str = "",
    python: str = sys.executable,
) -> LaunchTarget:
    """The app to launch for this run. A pinned profile (or no stack) uses the
    profile's ``launch_cmd``/``launch_kind``. A planner stack: ``stack.launch``,
    else ``stack.run`` when it is the app's own entry, else the language's default
    entry among the entrypoint ticket's files."""
    if stack is None or stack.profile:
        kind = profile.launch_kind if profile is not None else ""
        cmd = (profile.launch_cmd or "") if profile is not None else ""
        required = kind in KINDS
        language = ""
    else:
        kind, required = _kind(stack, deliverable)
        cmd = stack.launch.strip() or entry_run(stack, kind or "cli")
        language = stack.language
    if not kind:
        return LaunchTarget()
    entries = [t for t in tickets if t.entrypoint]
    owners = {t.id for t in entries}
    lines = [
        s
        for c in criteria
        if not owners or c.ticket_id in owners
        for s in (*c.given, *c.when, *c.then)
    ]
    source = "the stack's launch command" if stack is not None and stack.launch.strip() else ""
    needs: tuple[str, ...] = ()
    if cmd and not source:
        source = (
            "the stack's run command" if stack is not None and cmd == stack.run else "the profile"
        )
    if not cmd and language:
        files = [f for t in (entries or tickets) for f in t.files]
        cmd, need = _default_entry(language, files, python)
        needs = (need,) if need else ()
        source = f"the entrypoint's {need}" if need else ""
    interface = stack.interface if stack is not None and not stack.profile else ()
    if kind == "http":
        probes, posts = _probes(lines)
        return LaunchTarget(
            kind, cmd, required, probes, posts, needs=needs, source=source, interface=interface
        )
    # A security_abuse scenario's command is meant to be refused: no launch verdict.
    runs = _cli_runs(
        cmd,
        (
            s
            for c in criteria
            if (not owners or c.ticket_id in owners) and not c.security_abuse
            for s in c.when
        ),
    )
    # A subcommand's own options show only in its own --help.
    helps = (f"{cmd} --help", *_sub_helps(cmd, interface))
    return LaunchTarget(
        kind,
        cmd,
        required,
        runs=tuple(dict.fromkeys((*runs, *helps))) if cmd else (),
        needs=needs,
        source=source,
        interface=interface,
    )


# ---- the declared interface: the spec's options/subcommands or routes -------------

_ROUTE = re.compile(r"(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)", re.I)
_OPTION = re.compile(r"(?<![\w-])(--?[A-Za-z][\w-]*)")
_SUBCOMMAND = re.compile(r"[A-Za-z][\w-]*")
# Options every CLI framework adds on its own: never an unrequested extra.
_FRAMEWORK_OPTIONS = frozenset(
    {"h", "help", "version", "V", "install-completion", "show-completion"}
)
_HELP_TAIL = 4000  # the driver's TAIL: a help this long may have lost its head
_MAX_ROUTES = 12
_MAX_SUB_HELPS = 4


def _routes(interface: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """The declared routes the gate can request as written (no ``{id}`` / ``<id>`` /
    ``:id`` placeholder to fill)."""
    out: list[tuple[str, str]] = []
    for entry in interface:
        m = _ROUTE.fullmatch(" ".join(entry.split()))
        if m and not re.search(r"[<{]|/:", m[2]):
            out.append((m[1].upper(), m[2]))
    return tuple(dict.fromkeys(out))[:_MAX_ROUTES]


def _cli_interface(interface: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(options, subcommands) the declared cli entries name: every ``-x``/``--name``
    token, and an entry's leading bare word (``roll`` in ``roll --seed``)."""
    opts: list[str] = []
    subs: list[str] = []
    for entry in interface:
        words = entry.split()
        if not words or _ROUTE.fullmatch(" ".join(words)):
            continue
        opts += _OPTION.findall(entry)
        if _SUBCOMMAND.fullmatch(words[0]):
            subs.append(words[0])
    return tuple(dict.fromkeys(opts)), tuple(dict.fromkeys(subs))


def _sub_helps(cmd: str, interface: Iterable[str]) -> tuple[str, ...]:
    subs = _cli_interface(interface)[1][:_MAX_SUB_HELPS]
    return tuple(f"{cmd} {shlex.quote(s)} --help" for s in subs) if cmd else ()


def _cli_interface_findings(r: dict[str, Any], cmd: str) -> tuple[list[str], list[str]]:
    """(failures, advisories): a declared option/subcommand the CLI's --help lacks
    FAILs; an option it lists that the spec never asked for is only advisory."""
    interface = r.get("interface") or ()
    opts, subs = _cli_interface(interface)
    ran = {run.get("cmd"): run for run in r.get("runs") or () if run.get("rc") is not None}
    top = ran.get(f"{cmd} --help")
    if not (opts or subs) or top is None:
        return [], []
    helps = [str(top.get("output", ""))] + [
        str(ran[c].get("output", "")) for c in _sub_helps(cmd, interface) if c in ran
    ]
    listed: dict[str, str] = {}
    for text in helps:
        for o in _OPTION.findall(text):
            listed.setdefault(o.lstrip("-"), o)
    fails: list[str] = []
    if all(len(text) < _HELP_TAIL for text in helps):
        fails += [
            f"launch: spec option {o} is missing from the CLI: `{cmd} --help` does not "
            "list it — implement the option the spec declares, spelled as the spec spells it"
            for o in opts
            if o.lstrip("-") not in listed
        ]
        fails += [
            f"launch: spec subcommand `{s}` is missing from the CLI: `{cmd} --help` does "
            "not list it — implement the subcommand the spec declares"
            for s in subs
            if not re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", helps[0])
        ]
    declared = {o.lstrip("-") for o in opts}
    extras = [o for k, o in listed.items() if k not in declared and k not in _FRAMEWORK_OPTIONS]
    advisories = (
        [
            f"launch (advisory): `{cmd} --help` lists {', '.join(extras)}, which the spec's "
            "interface does not declare — drop an option the request never asked for"
        ]
        if extras
        else []
    )
    return fails, advisories


# ---- the gate ---------------------------------------------------------------

_TRACEBACK = re.compile(
    r"Traceback \(most recent call last\)|^panic: |^\S*Error: .*\n\s+at |Uncaught \w*Error",
    re.M,
)
# Only a failure to start the program: a CLI's own error for a bad argument or a
# missing record (`Error: task 99: not found`, exit 1) is the scenario's answer.
_ENTRY_ERROR = re.compile(
    r"No module named|ModuleNotFoundError|ImportError|cannot import name|"
    r"can't find '__main__' module|is a package and cannot be directly executed|"
    r"can't open file|Cannot find module|ERR_MODULE_NOT_FOUND|SyntaxError|no Go files|"
    r"cannot find package|package \S+ is not a main package|undefined: "
)


def _program_missing(cmd: str, text: str) -> bool:
    """The shell could not find the launch command's program itself."""
    try:
        prog = shlex.split(cmd)[0]
    except (ValueError, IndexError):
        return False
    p = re.escape(prog)
    return bool(re.search(rf"{p}: (?:command )?not found|command not found: {p}\b", text))


_NO_ENTRY = (
    "no runnable entry point: the app has no command a user can launch — add a "
    "__main__/main/app entry the entrypoint ticket owns (an http app serves on "
    "$PORT/$HOST) and declare it as the stack's launch command"
)


def _tail(text: str, lines: int = 20, chars: int = 1500) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[-chars:] or "(no output)"


def _what(q: dict[str, Any]) -> str:
    at = f" (redirected to {q['at']})" if q.get("at") else ""
    return f"{q.get('method')} {q.get('path')}{at}"


def _body(q: dict[str, Any]) -> str:
    return str(q.get("body", ""))[-800:] or "(empty)"


def _http_findings(r: dict[str, Any], cmd: str) -> list[str]:
    out: list[str] = []
    output = _tail(str(r.get("output", "")))
    boot = r.get("boot")
    if boot == "exited":
        out.append(
            f"launch: `{cmd}` exited (code {r.get('rc')}) before listening on $PORT "
            f"({r.get('port')}): a web app must start its server and keep serving when "
            f"launched — read the port from the PORT env var and the host from HOST. "
            f"Its output:\n{output}"
        )
        return out
    if boot != "listening":
        out.append(
            f"launch: `{cmd}` never listened on $PORT ({r.get('port')}) within the boot "
            "timeout: bind the server to int(os.environ['PORT']) (the PORT env var) on "
            f"HOST, not a fixed port. Its output:\n{output}"
        )
        return out
    for q in r.get("requests") or ():
        what = _what(q)
        status = int(q.get("status", 0))
        form = q.get("form") or {}
        if "error" in q:
            out.append(f"launch: {what} failed ({q['error']}) against the running app")
        elif status >= 500:
            out.append(
                f"launch: {what} answered {status} under the real server: a server "
                f"error the tests did not catch. Response: {_body(q)}"
            )
        elif status == 405 and form and "at" not in q:
            sent = (
                f"{form.get('method')} with _method={form['override']}"
                if form.get("override")
                else str(form.get("method"))
            )
            out.append(
                f"launch: the form on {form.get('page')} submits {what} ({sent}) and the "
                "app answers 405 Method Not Allowed: its own form uses a method/action the "
                "route does not accept. An HTML form sends only GET or POST — accept the "
                "method the form really sends, or honor the _method override before routing. "
                f"Response: {_body(q)}"
            )
    burst = r.get("burst") or {}
    for q in burst.get("failures") or ():
        out.append(
            f"launch: under {burst.get('sent')} concurrent requests, {_what(q)} answered "
            f"{q.get('status')} ({burst.get('count')} of them 5xx; it answers fine "
            "one at a time): state shared across the server's threads (one DB connection "
            "or cursor, a global) breaks under the real threaded server — open a "
            f"connection per request or guard the shared state. Response: {_body(q)}"
        )
    for q in r.get("routes") or ():
        if q.get("status") in (404, 405, 501):
            out.append(
                f"launch: spec route {q.get('method')} {q.get('path')} is missing from the "
                f"running app (it answered {q['status']}) — serve the route the spec declares"
            )
    if r.get("alive") is False:
        out.append(f"launch: the app died while serving requests (code {r.get('rc')})")
    if out:
        out.append(f"launch: `{cmd}` output:\n{output}")
    return out


def _cli_findings(r: dict[str, Any], launch: str) -> list[str]:
    out: list[str] = []
    for run in r.get("runs") or ():
        cmd, rc, text = run.get("cmd"), run.get("rc"), str(run.get("output", ""))
        if rc is None and r.get("required", True):
            out.append(
                f"launch: `{cmd}` did not exit with stdin closed: a CLI must finish (or "
                f"exit on end of input) when run. Its output:\n{_tail(text)}"
            )
        elif rc is None:
            continue  # an inferred CLI that keeps running (a game loop) is no verdict
        elif _TRACEBACK.search(text):
            out.append(f"launch: `{cmd}` crashed (exit {rc}) with a traceback:\n{_tail(text)}")
        elif rc != 0 and (_ENTRY_ERROR.search(text) or _program_missing(cmd, text)):
            out.append(
                f"launch: `{cmd}` cannot start (exit {rc}): the entry point or an import "
                f"is broken — the user's launch command must work as written:\n{_tail(text)}"
            )
        # A missing subcommand's own --help already fails as missing from the CLI.
        elif rc != 0 and cmd == f"{launch} --help" and r.get("required", True):
            out.append(
                f"launch: `{cmd}` exited {rc}; --help must print usage and exit 0. "
                f"Its output:\n{_tail(text)}"
            )
    return out + _cli_interface_findings(r, launch)[0]


class LaunchGate:
    """PASS iff Foundry can start the app as a user would and it works (see module
    doc); the verdict is decided from the driver's observed result."""

    name = "launch"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(
        self,
        target: LaunchTarget,
        *,
        seal_key: bytes | None = None,
        pyenv: PythonBuildEnv | None = None,
        boot_s: float = 30.0,
        request_s: float = 10.0,
        cli_s: float = 30.0,
    ) -> None:
        self.target = target
        self._pyenv = pyenv
        self._seal_key = seal_key
        runs = len(target.runs) if target.kind == "cli" else 1
        self._routes = _routes(target.interface) if target.kind == "http" else ()
        # Each CLI run may also spend its kill (3s + 5s) and output join (2s).
        self._timeout_s = (
            boot_s
            + request_s * (len(target.probes) + len(self._routes) + 2)
            + (cli_s + 10) * runs
            + 30
        )
        # The http probe stops starting requests at probe_s, inside that timeout:
        # after it one request may be in flight, each declared route and the burst
        # (barrier wait + two hops) take a request_s each, then the kill and join.
        probe_s = self._timeout_s - boot_s - request_s * (len(self._routes) + 5) - 20
        self._cfg = {
            "boot_s": boot_s,
            "request_s": request_s,
            "cli_s": cli_s,
            "probe_s": max(probe_s, request_s),
            "max_requests": _MAX_REQUESTS,
            "max_pages": _MAX_PAGES,
            "max_forms": _MAX_FORMS,
            "burst": _BURST,
        }
        self.mutant_rotation_id = 0
        self.scanner_rule_version = "launch-v1"

    def _verdict(self, structured: dict[str, Any], findings: Sequence[str]) -> ZoneAObservation:
        return observe(
            self.name,
            structured={LAUNCH_KEY: structured},
            findings=tuple(findings),
            seal_key=self._seal_key,
        )

    def execute(self, workdir: Path) -> ZoneAObservation:
        t = self.target
        if not t.kind:
            return self._verdict(
                {"pinned": False}, ("launch not pinned: nothing to launch (a library)",)
            )
        missing = [p for p in t.needs if not (workdir / p).is_file()]
        if not t.cmd or missing:
            why = _NO_ENTRY + (f" (expected {', '.join(missing)})" if missing else "")
            return self._verdict(
                {"pinned": False, "required": t.required, "kind": t.kind},
                (why if t.required else f"launch not pinned: {why}",),
            )
        cfg = {
            **self._cfg,
            "mark": _RESULT_MARK,
            "kind": t.kind,
            "cmd": t.cmd,
            "probes": [list(p) for p in t.probes],
            "posts": list(t.posts),
            "runs": list(t.runs),
            "routes": [list(p) for p in self._routes],
            "launch_id": (launch_id := secrets.token_hex(16)),
        }
        try:
            rc, out, err = ground_untrusted(
                (sys.executable, "-I", "-S", "-c", _DRIVER, json.dumps(cfg)),
                workdir,
                timeout_s=self._timeout_s,
                extra_env=self._pyenv.run_env() if self._pyenv is not None else None,
                binds=self._pyenv.binds if self._pyenv is not None else (),
            )
        except subprocess.TimeoutExpired as exc:
            _reap_escapees(launch_id)
            return observe(
                self.name,
                structured={"cmd": t.cmd, "timed_out_s": self._timeout_s},
                findings=(
                    f"launch: the launch check of `{t.cmd}` did not finish",
                    *timeout_findings(exc, workdir),
                ),
                seal_key=self._seal_key,
            )
        _reap_escapees(launch_id)
        result = _driver_result(out)
        if result is None:
            return observe(
                self.name,
                exit_codes=(rc,),
                stdout=out,
                stderr=err,
                structured={"cmd": t.cmd},
                findings=(f"launch driver exited {rc} with no result: {_tail(err or out)}",),
                seal_key=self._seal_key,
            )
        result.update(required=t.required, interface=list(t.interface))
        findings = (
            _http_findings(result, t.cmd) if t.kind == "http" else _cli_findings(result, t.cmd)
        )
        if findings:
            source = f" ({t.source})" if t.source else ""
            findings.insert(
                0, f"launch FAILED: Foundry started `{t.cmd}`{source} as a {t.kind} app"
            )
        if t.kind == "cli":
            findings += _cli_interface_findings(result, t.cmd)[1]
        result.update(cmd=t.cmd, pinned=True)
        return observe(
            self.name,
            exit_codes=(rc,),
            stdout=out,
            stderr=err,
            structured={LAUNCH_KEY: result},
            findings=tuple(findings),
            seal_key=self._seal_key,
        )

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        r = obs.structured.get(LAUNCH_KEY)
        if not isinstance(r, dict):
            return GateStatus.ERROR
        if r.get("pinned") is False:
            return GateStatus.FAIL if r.get("required") else GateStatus.PASS
        if r.get("kind") == "http":
            bad = _http_findings(r, str(r.get("cmd", "")))
        elif r.get("kind") == "cli":
            bad = _cli_findings(r, str(r.get("cmd", "")))
        else:
            return GateStatus.ERROR
        return GateStatus.FAIL if bad else GateStatus.PASS


def _driver_result(stdout: str) -> dict[str, Any] | None:
    """The driver's one result line. More than one means the app wrote a forged one
    (it can add lines to the driver's stdout but never remove the driver's own): no
    verdict, so never a PASS it didn't earn."""
    marked = [line for line in stdout.splitlines() if line.startswith(_RESULT_MARK)]
    if len(marked) != 1:
        return None
    try:
        got = json.loads(marked[0][len(_RESULT_MARK) :])
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def _reap_escapees(launch_id: str) -> None:
    """SIGKILL any process still carrying ``launch_id`` in its environment: an app
    that daemonized (setsid) out of the driver's process group outlives its kill on
    macOS, which has no PID namespace. Linux reaps them with the namespace."""
    if sys.platform != "darwin":
        return
    try:
        listing = subprocess.run(
            ("ps", "-Eww", "-x", "-o", "pid=,command="),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    needle = f"{_LAUNCH_ID_ENV}={launch_id}"
    for line in listing.splitlines():
        pid, _, rest = line.strip().partition(" ")
        if needle in rest and pid.isdigit() and int(pid) != os.getpid():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(pid), signal.SIGKILL)
