"""Cucumber Messages (NDJSON) scenario accounting for the Test gate's BDD path.

The gate runs the planned ``bdd`` command with ``$FOUNDRY_BDD_REPORT`` pointing at a
file it created. cucumber-js, cucumber-jvm and Reqnroll write Cucumber Messages there
natively; godog and cucumber-rs have no Messages writer, so for a Go or Rust project
the gate also accepts their legacy Cucumber JSON and converts it
(``convert_cucumber_json``), reading the step patterns — which that format omits —
from the project's own source. Either way the gate then maps each trusted
scenario (``features/<ticket>.feature::<criterion id>``) to an outcome in the same
vocabulary as the pytest-bdd path (passed / failed / skipped / missing / unmatched /
catch-all).

What the report is trusted for, and what it is not: the expected steps come from the
gate's own parse of the feature files (``trusted_scenarios``), never from the report,
so a scenario passes only if the runner reports every trusted step — same keyword
type, same text, in order — as PASSED. Step-definition patterns are re-evaluated HERE
against per-run random probes to flag catch-all matchers. The report's size and
sha256 are recorded the moment the runner exits and re-checked on read, so nothing
that runs later in the gate can swap it. The runner process itself executes agent
code, though, so a deliberately hostile step definition can still write a forged
report; that residual is stated in the findings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

__all__ = [
    "CUCUMBER_RESIDUAL",
    "ReportDigest",
    "ReportError",
    "convert_cucumber_json",
    "create_report_file",
    "expression_to_regex",
    "make_probes",
    "parse_messages",
    "read_verified_report",
    "record_report",
    "scenario_outcomes",
    "source_step_patterns",
]

CUCUMBER_RESIDUAL = (
    "BDD residual limit: step definitions run inside the Cucumber runner process "
    "that writes the report, so deliberately hostile test code could still forge it; "
    "the gate checks every trusted step's text, type and status and rejects "
    "catch-all matchers, but cannot verify that a step definition asserts anything"
)

_MAX_REPORT_BYTES = 64 << 20
_PATTERN_EVAL_TIMEOUT_S = 10.0

_STEP_TYPES = {"Context": "given", "Action": "when", "Outcome": "then"}
# Worst first: any instance of a scenario (retry attempt, duplicate pickle) that did
# not pass decides the scenario.
_RANK = ("failed", "unmatched", "missing", "skipped", "catch-all", "passed")


class ReportError(Exception):
    """The report is absent, oversized, not a regular file, tampered or unparseable."""


@dataclass(frozen=True, slots=True)
class ReportDigest:
    size: int
    sha256: str


def create_report_file(directory: Path) -> Path:
    """Exclusively create an empty, unpredictably named report file in ``directory``
    (a gate-owned temp dir outside the workdir) before the runner starts."""
    path = Path(directory) / f"bdd-{secrets.token_hex(16)}.ndjson"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    return path


def _read_regular(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ReportError(f"report not readable ({exc.strerror})") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ReportError("report is not a regular file")
        if st.st_size > _MAX_REPORT_BYTES:
            raise ReportError(f"report exceeds {_MAX_REPORT_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = _MAX_REPORT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > _MAX_REPORT_BYTES:
        raise ReportError(f"report exceeds {_MAX_REPORT_BYTES} bytes")
    return data


def record_report(path: Path) -> ReportDigest | None:
    """Size + sha256 of the report, taken right after the runner exits; ``None`` if
    it is unreadable (the later read then fails closed)."""
    try:
        data = _read_regular(path)
    except ReportError:
        return None
    return ReportDigest(len(data), hashlib.sha256(data).hexdigest())


def parse_messages(data: bytes) -> list[dict[str, Any]]:
    """NDJSON envelopes; raises ``ReportError`` on anything malformed or a run that
    never reached ``testRunFinished``."""
    try:
        text = data.decode("utf-8")
        envelopes = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReportError(f"report is not valid NDJSON ({exc})") from exc
    if not envelopes or not all(isinstance(e, dict) for e in envelopes):
        raise ReportError("report holds no Cucumber Messages envelopes")
    if not any("testRunFinished" in e for e in envelopes):
        raise ReportError("report is incomplete (no testRunFinished)")
    return envelopes


def read_verified_report(
    path: Path, digest: ReportDigest | None, workdir: Path | None = None
) -> list[dict[str, Any]]:
    """Read the report, check it against the digest recorded when the runner exited,
    and parse it — Cucumber JSON from a Go / Rust project in ``workdir`` is converted.
    Raises ``ReportError`` on any mismatch."""
    if digest is None:
        raise ReportError("report was missing or unreadable when the runner exited")
    data = _read_regular(path)
    if len(data) != digest.size or hashlib.sha256(data).hexdigest() != digest.sha256:
        raise ReportError("report changed after the runner exited")
    if not data:
        raise ReportError("report is empty — the bdd command did not write Cucumber Messages")
    if data.lstrip()[:1] == b"[":
        if workdir is None:
            raise ReportError("report is legacy Cucumber JSON, not Cucumber Messages")
        return convert_cucumber_json(data, workdir)
    return parse_messages(data)


# ---- catch-all detection -----------------------------------------------------

_INT = r"-?\d+"
_FLOAT = r"(?=.*\d.*)[-+]?\d*(?:\.(?=\d.*))?\d*(?:\d+[E][+-]?\d+)?"
_BUILTIN_PARAMS = {
    "int": _INT, "biginteger": _INT, "byte": _INT, "short": _INT, "long": _INT,
    "float": _FLOAT, "double": _FLOAT, "bigdecimal": _FLOAT,
    "word": r"[^\s]+",
    "string": r'"(?:[^"\\]*(?:\\.[^"\\]*)*)"|\'(?:[^\'\\]*(?:\\.[^\'\\]*)*)\'',
    "": r".*",
}
# JS named groups; lookbehinds ``(?<=`` / ``(?<!`` are left alone.
_JS_NAMED_GROUP = re.compile(r"\(\?<(?![=!])")


def _js_to_py(source: str) -> str:
    return _JS_NAMED_GROUP.sub("(?P<", source)


def expression_to_regex(expr: str, params: dict[str, list[str]]) -> str:
    """Translate a Cucumber Expression to an anchored Python regex. Custom parameter
    types use the regexes the runner reported for them; an unknown one is ``.*``
    (when in doubt, treat it as accepting anything)."""
    out: list[str] = []
    alts: list[list[str]] = [[]]

    def flush() -> None:
        if len(alts) > 1:
            out.append("(?:" + "|".join("".join(a) for a in alts) + ")")
        else:
            out.append("".join(alts[0]))
        alts[:] = [[]]

    i, n = 0, len(expr)
    while i < n:
        c = expr[i]
        if c == "\\" and i + 1 < n:
            alts[-1].append(re.escape(expr[i + 1]))
            i += 2
        elif c.isspace():
            flush()
            out.append(re.escape(c))
            i += 1
        elif c == "/":
            alts.append([])
            i += 1
        elif c == "{":
            j = expr.find("}", i)
            if j < 0:
                raise ValueError(f"unterminated parameter in {expr!r}")
            name = expr[i + 1 : j]
            regexes = params.get(name) or [_BUILTIN_PARAMS.get(name, ".*")]
            alts[-1].append("(" + "|".join(f"(?:{r})" for r in regexes) + ")")
            i = j + 1
        elif c == "(":
            text: list[str] = []
            i += 1
            while i < n and expr[i] != ")":
                if expr[i] == "\\" and i + 1 < n:
                    i += 1
                text.append(expr[i])
                i += 1
            if i >= n:
                raise ValueError(f"unterminated optional text in {expr!r}")
            alts[-1].append("(?:" + re.escape("".join(text)) + ")?")
            i += 1
        else:
            alts[-1].append(re.escape(c))
            i += 1
    flush()
    return "^" + "".join(out) + "$"


# Letters, digits and punctuation: a probe can contain anything a step text can, so
# a pattern cannot dodge the probes by excluding a fixed literal.
_PROBE_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    ".,;:!?-_'\"()[]{}<>/\\@#$%^&*+=~|`"
)


def _random_probe(words: int | None) -> str:
    n = words if words is not None else 1 + secrets.randbelow(6)
    return " ".join(
        "".join(secrets.choice(_PROBE_ALPHABET) for _ in range(1 + secrets.randbelow(10)))
        for _ in range(max(n, 1))
    )


def make_probes(words: int | None = None) -> tuple[str, str, str]:
    """Three per-run random step texts — random characters (punctuation included)
    and token lengths, nothing fixed — that no specific step definition should
    accept. ``words`` fixes their space-separated word count, so a matcher that
    accepts any text of a step's shape (``{word} {word} {word}``) is caught on it;
    ``None`` draws a random count per probe."""
    return (_random_probe(words), _random_probe(words), _random_probe(words))


# Evaluated in a child process so a catastrophic-backtracking pattern from the
# report cannot hang the gate; a pattern with no answer counts as a catch-all.
# One output token per pattern: "E", or one 1/0 per probe set.
_MATCH_CHILD = """\
import json, re, sys
d = json.load(sys.stdin)
for kind, pat in d["patterns"]:
    try:
        r = re.compile(pat)
    except Exception:
        print("E", flush=True)
        continue
    f = r.fullmatch if kind == "full" else r.search
    print("".join("1" if all(f(p) for p in s) else "0" for s in d["sets"]), flush=True)
"""


def _catch_all_ids(
    step_defs: dict[str, dict[str, Any]],
    params: dict[str, list[str]],
    probes: tuple[str, ...],
) -> tuple[set[str], tuple[str, ...]]:
    [flagged], notes = _catch_all_sets(step_defs, params, [probes])
    return flagged, notes


def _eval_patterns(
    jobs: list[tuple[str, str]], sets: list[list[str]]
) -> tuple[list[str | None], tuple[str, ...]]:
    """Per pattern (``("full"|"search", regex)``): one ``1``/``0`` per set (every
    text in the set matches), ``"E"`` when it does not compile, ``None`` when the
    child ran out of time before answering it."""
    if not jobs:
        return [], ()
    payload = json.dumps({"patterns": jobs, "sets": sets})
    notes: tuple[str, ...] = ()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _MATCH_CHILD],
            input=payload.encode(),
            capture_output=True,
            timeout=_PATTERN_EVAL_TIMEOUT_S,
        )
        answers = proc.stdout.decode().split()
    except subprocess.TimeoutExpired as exc:
        answers = (exc.stdout or b"").decode().split()
        notes = (
            "BDD step-definition patterns took too long to evaluate; the "
            "unevaluated ones are treated as catch-all matchers",
        )
    return [*answers, *[None] * (len(jobs) - len(answers))][: len(jobs)], notes


def _def_job(step_def: dict[str, Any], params: dict[str, list[str]]) -> tuple[str, str] | None:
    pattern = step_def.get("pattern") or {}
    source = str(pattern.get("source", ""))
    try:
        if pattern.get("type") == "REGULAR_EXPRESSION":
            return ("search", _js_to_py(source))
        return ("full", expression_to_regex(source, params))
    except ValueError:
        return None


def _catch_all_sets(
    step_defs: dict[str, dict[str, Any]],
    params: dict[str, list[str]],
    probe_sets: list[tuple[str, ...]],
) -> tuple[list[set[str]], tuple[str, ...]]:
    """For each probe set, the definition ids that accept every probe in it."""
    ids = sorted(step_defs)
    patterns = [_def_job(step_defs[sid], params) for sid in ids]
    answers, notes = _eval_patterns([p for p in patterns if p is not None],
                                    [list(s) for s in probe_sets])
    flagged: list[set[str]] = [set() for _ in probe_sets]
    it = iter(answers)
    for sid, pat in zip(ids, patterns, strict=True):
        answer = next(it, None) if pat is not None else None
        for i, out in enumerate(flagged):
            if answer is None or len(answer) != len(probe_sets) or answer[i] != "0":
                out.add(sid)
    return flagged, notes


# ---- legacy Cucumber JSON (godog, cucumber-rs) --------------------------------

_SKIP_DIRS = frozenset({".git", "vendor", "target", "node_modules"})
_MAX_SOURCE_FILE = 1 << 20
_MAX_SOURCE_TOTAL = 32 << 20
# godog: sc.Step / Given / When / Then with a string-literal regex (optionally
# wrapped in regexp.MustCompile).
_GO_STEP = re.compile(
    r"\.(?:Step|Given|When|Then)\(\s*(?:regexp\.MustCompile\(\s*)?"
    r'(`[^`]*`|"(?:[^"\\\n]|\\.)*")'
)
# cucumber-rs: #[given("literal")], #[when(expr = "...")], #[then(regex = r"...")].
_RS_STEP = re.compile(
    r"#\[\s*(?:cucumber\s*::\s*)?(?:given|when|then|step)\s*\(\s*(?:(expr|regex)\s*=\s*)?"
    r'(r(#*)"(?s:.*?)"\3|"(?:[^"\\]|\\.)*")'
)
_STEP_KEYWORD_TYPES = {"given": "Context", "when": "Action", "then": "Outcome"}
_JSON_STATUS = {
    "passed": "PASSED", "failed": "FAILED", "undefined": "UNDEFINED",
    "pending": "PENDING", "skipped": "SKIPPED", "ambiguous": "AMBIGUOUS",
}


def _quoted(literal: str) -> str | None:
    """A Go / Rust double-quoted string literal's value (JSON escapes); ``None`` if
    it uses an escape JSON lacks."""
    try:
        value = json.loads(literal)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def _source_files(workdir: Path, suffix: str) -> list[Path]:
    out: list[Path] = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        out.extend(Path(root) / f for f in sorted(files) if f.endswith(suffix))
    return out


def source_step_patterns(workdir: Path) -> list[tuple[str, str]] | None:
    """``(pattern type, source)`` of every step definition registered with a string
    literal in a Go (godog) or Rust (cucumber-rs) project, or ``None`` when the
    workdir is neither. A step definition registered any other way is not found, so
    the steps it binds count as unchecked — fail-closed."""
    workdir = Path(workdir)
    kinds = [(s, rx) for marker, s, rx in (("go.mod", ".go", _GO_STEP),
                                          ("Cargo.toml", ".rs", _RS_STEP))
             if (workdir / marker).is_file()]
    if not kinds:
        return None
    found: list[tuple[str, str]] = []
    total = 0
    for suffix, rx in kinds:
        for path in _source_files(workdir, suffix):
            if path.is_symlink():
                continue
            try:
                size = path.stat().st_size
                if size > _MAX_SOURCE_FILE or total + size > _MAX_SOURCE_TOTAL:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            total += size
            for m in rx.finditer(text):
                lit = m.group(1 if suffix == ".go" else 2)
                if lit.startswith("`"):
                    value: str | None = lit[1:-1]
                elif lit.startswith("r"):
                    value = lit[len(m.group(3)) + 2 : -1 - len(m.group(3))]
                else:
                    value = _quoted(lit)
                if value is None:
                    continue
                if suffix == ".go" or m.group(1) == "regex":
                    found.append(("REGULAR_EXPRESSION", value))
                elif m.group(1) == "expr":
                    found.append(("CUCUMBER_EXPRESSION", value))
                else:
                    found.append(("REGULAR_EXPRESSION", "^" + re.escape(value) + "$"))
    return list(dict.fromkeys(found))


def _step_type(keyword: str, previous: str) -> str:
    word = keyword.strip().lower()
    if word in ("and", "but", "*"):
        return previous
    return _STEP_KEYWORD_TYPES.get(word, "")


def convert_cucumber_json(data: bytes, workdir: Path) -> list[dict[str, Any]]:
    """Cucumber Messages envelopes from a godog / cucumber-rs Cucumber JSON report.

    The report carries step text, keyword and status but no step patterns, so the
    ``stepDefinition`` envelopes are the literal patterns found in the project's
    source, and each executed step is bound to every one of them that matches its
    text: ``scenario_outcomes`` then probes them for catch-alls exactly as it does a
    native report, and a step no source pattern matches is unchecked. A skipped step
    that no source pattern matches is undefined (cucumber-rs reports it so)."""
    patterns = source_step_patterns(workdir)
    if patterns is None:
        raise ReportError(
            "report is legacy Cucumber JSON, which the gate accepts only from godog (Go) "
            "and cucumber-rs (Rust); this runner can write Cucumber Messages — use its "
            "message formatter (cucumber-js `--format message:$FOUNDRY_BDD_REPORT`, "
            "cucumber-jvm `-Dcucumber.plugin=message:$FOUNDRY_BDD_REPORT`)"
        )
    try:
        features = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReportError(f"report is not valid Cucumber JSON ({exc})") from exc
    if not isinstance(features, list) or not all(isinstance(f, dict) for f in features):
        raise ReportError("report is not a Cucumber JSON feature list")

    def dicts(value: Any) -> list[dict[str, Any]]:
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    scenarios: list[tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]] = []
    for feature in features:
        background: list[dict[str, Any]] = []
        for el in dicts(feature.get("elements")):
            if el.get("type") == "background":
                background = dicts(el.get("steps"))
                continue
            hooks = dicts(el.get("before")) + dicts(el.get("after"))
            steps = background + dicts(el.get("steps"))
            background = []
            scenarios.append((str(feature.get("uri", "")), str(el.get("name", "")), steps, hooks))

    texts = sorted({str(s.get("name", "")) for _, _, steps, _ in scenarios for s in steps})
    defs = {f"sd{i}": {"id": f"sd{i}", "pattern": {"type": t, "source": src}}
            for i, (t, src) in enumerate(patterns)}
    jobs = {sid: _def_job(d, {}) for sid, d in defs.items()}
    runnable = [sid for sid, j in jobs.items() if j is not None]
    answers, _ = _eval_patterns([j for j in jobs.values() if j is not None],
                                [[t] for t in texts])
    binds: dict[str, list[str]] = {t: [] for t in texts}
    for sid, answer in zip(runnable, answers, strict=True):
        if answer is not None and len(answer) == len(texts):
            for text, bit in zip(texts, answer, strict=True):
                if bit == "1":
                    binds[text].append(sid)

    envelopes: list[dict[str, Any]] = [{"stepDefinition": d} for d in defs.values()]
    for n, (uri, name, steps, hooks) in enumerate(scenarios):
        pickle_steps: list[dict[str, Any]] = []
        test_steps: list[dict[str, Any]] = []
        finished: list[dict[str, Any]] = []
        kind = ""
        for k, step in enumerate(steps):
            text = str(step.get("name", ""))
            kind = _step_type(str(step.get("keyword", "")), kind)
            status = _JSON_STATUS.get(
                str((step.get("result") or {}).get("status", "")).lower(), "UNKNOWN")
            if status == "SKIPPED" and not binds[text]:
                status = "UNDEFINED"
            pid, tid = f"p{n}s{k}", f"t{n}s{k}"
            pickle_steps.append({"id": pid, "type": kind, "text": text})
            test_steps.append({"id": tid, "pickleStepId": pid,
                               "stepDefinitionIds": binds[text] if status != "UNDEFINED" else []})
            finished.append({"testCaseStartedId": f"r{n}", "testStepId": tid,
                             "testStepResult": {"status": status}})
        for k, hook in enumerate(hooks):
            tid = f"t{n}h{k}"
            status = _JSON_STATUS.get(
                str((hook.get("result") or {}).get("status", "")).lower(), "UNKNOWN")
            test_steps.append({"id": tid, "hookId": tid})
            finished.append({"testCaseStartedId": f"r{n}", "testStepId": tid,
                             "testStepResult": {"status": status}})
        envelopes += [
            {"pickle": {"id": f"p{n}", "uri": uri, "name": name, "steps": pickle_steps}},
            {"testCase": {"id": f"c{n}", "pickleId": f"p{n}", "testSteps": test_steps}},
            {"testCaseStarted": {"id": f"r{n}", "testCaseId": f"c{n}"}},
            *({"testStepFinished": f} for f in finished),
        ]
    envelopes.append({"testRunFinished": {}})
    return envelopes


# ---- outcome mapping ---------------------------------------------------------


def _ref_uri(uri: str, workdir: Path) -> str:
    if uri.startswith("file:"):
        uri = unquote(urlparse(uri).path)
    if uri.startswith("classpath:"):
        uri = uri[len("classpath:") :]
    p = Path(uri)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(Path(workdir).resolve()).as_posix()
        except ValueError:
            return uri
    return p.as_posix()


def scenario_outcomes(
    envelopes: list[dict[str, Any]],
    trusted: dict[str, list[tuple[str, str]]],
    workdir: Path,
    *,
    probes: tuple[str, ...] | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """``{scenario_ref: outcome}`` for every trusted scenario (unreported ones are
    "missing"), plus any notes for the findings.

    A passing scenario is "catch-all" when ANY Then step, or every step, is bound
    to a catch-all definition, or when any step is bound to no definition or to one
    the report never declared (fail-closed)."""
    pickles: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {}
    step_defs: dict[str, dict[str, Any]] = {}
    params: dict[str, list[str]] = {}
    cases: dict[str, dict[str, Any]] = {}
    started: dict[str, str] = {}
    results: dict[tuple[str, str], list[str]] = {}
    for env in envelopes:
        if isinstance(p := env.get("pickle"), dict):
            ref = f"{_ref_uri(str(p.get('uri', '')), workdir)}::{p.get('name', '')}"
            steps = {
                str(s.get("id")): (_STEP_TYPES.get(str(s.get("type")), ""), str(s.get("text")))
                for s in p.get("steps") or []
                if isinstance(s, dict)
            }
            pickles[str(p.get("id"))] = (ref, steps)
        elif isinstance(d := env.get("stepDefinition"), dict):
            step_defs[str(d.get("id"))] = d
        elif isinstance(t := env.get("parameterType"), dict):
            regs = t.get("regularExpressions") or []
            params[str(t.get("name"))] = [_js_to_py(str(r)) for r in regs] or [".*"]
        elif isinstance(c := env.get("testCase"), dict):
            cases[str(c.get("id"))] = c
        elif isinstance(s := env.get("testCaseStarted"), dict):
            started[str(s.get("id"))] = str(s.get("testCaseId"))
        elif isinstance(f := env.get("testStepFinished"), dict):
            status = str((f.get("testStepResult") or {}).get("status", "UNKNOWN"))
            key = (str(f.get("testCaseStartedId")), str(f.get("testStepId")))
            results.setdefault(key, []).append(status)

    # Probe each definition with random texts of every step shape it may bind (the
    # word count), so "any N-word text" is caught on an N-word step.
    if probes is not None:
        shapes: list[int | None] = [None]
        probe_sets = [probes]
    else:
        words = {len(text.split()) for _, steps in pickles.values() for _, text in steps.values()}
        shapes = [*sorted(words)]
        probe_sets = [make_probes(k) for k in sorted(words)]
    sets, notes = _catch_all_sets(step_defs, params, probe_sets)
    flagged_by_shape = dict(zip(shapes, sets, strict=True))

    def attempt(tcs_id: str, case: dict[str, Any], ref: str, steps: dict) -> str:
        executed: list[tuple[str, str]] = []
        step_status: list[str] = []
        catch_all: list[bool] = []
        # Fail-closed: a step bound to no definition, or to one the report never
        # declared, cannot be checked, so it voids the scenario's pass on its own.
        unchecked = then_catch_all = False
        for ts in case.get("testSteps") or []:
            if not isinstance(ts, dict):
                continue
            got = results.get((tcs_id, str(ts.get("id"))), [])
            status = next((s for s in got if s != "PASSED"), "PASSED") if got else "NONE"
            if status in ("FAILED", "AMBIGUOUS"):
                return "failed"
            psid = ts.get("pickleStepId")
            if psid is None:
                step_status.append(status)  # a hook
                continue
            if str(psid) not in steps:
                return "unmatched"
            executed.append(steps[str(psid)])
            step_status.append(status)
            defs = [str(x) for x in ts.get("stepDefinitionIds") or []]
            shape = None if probes is not None else len(steps[str(psid)][1].split())
            flagged = flagged_by_shape.get(shape, set(step_defs))
            unknown = not defs or any(x not in step_defs for x in defs)
            unchecked |= unknown
            bound_catch_all = unknown or any(x in flagged for x in defs)
            catch_all.append(bound_catch_all)
            then_catch_all |= bound_catch_all and steps[str(psid)][0] == "then"
        if "UNDEFINED" in step_status:
            return "missing"
        if executed != trusted[ref]:
            return "unmatched"
        if any(s in ("PENDING", "SKIPPED") for s in step_status):
            return "skipped"
        if any(s != "PASSED" for s in step_status):
            return "missing"
        if unchecked or then_catch_all or (catch_all and all(catch_all)):
            return "catch-all"
        return "passed"

    outcomes: dict[str, list[str]] = {}
    for pickle_ref, _ in pickles.values():
        if pickle_ref in trusted:
            outcomes.setdefault(pickle_ref, [])
    for tcs_id, case_id in started.items():
        case = cases.get(case_id)
        if case is None or str(case.get("pickleId")) not in pickles:
            continue
        ref, steps = pickles[str(case.get("pickleId"))]
        if ref in trusted:
            outcomes[ref].append(attempt(tcs_id, case, ref, steps))
    out = {}
    for ref in sorted(trusted):
        got = outcomes.get(ref) or ["missing"]
        out[ref] = min(got, key=_RANK.index)
    return out, notes
