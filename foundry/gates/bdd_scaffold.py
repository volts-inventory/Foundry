"""Gherkin step lines and the pytest-bdd step-module skeleton that binds them.

A small Gherkin reader (the subset Foundry's features and planners write:
Feature/Rule/Background/Scenario/Scenario Outline/Examples, And/But/``*``,
doc strings and data tables skipped) and a deterministic step scaffold: one
function per distinct step, with the exact decorator text pytest-bdd matches.
pytest-bdd's own generator emits outline steps as literal ``<param>`` text,
which pytest-bdd 8+ never matches, so it is not used."""

from __future__ import annotations

import json
import keyword
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_HEADER = re.compile(
    r"^(Feature|Rule|Background|Scenario Outline|Scenario Template|Scenario|Example|"
    r"Examples|Scenarios)\s*:"
)
_STEP = re.compile(r"^(Given|When|Then|And|But|\*)\s+(.+?)\s*$")
_PLACEHOLDER = re.compile(r"<([^<>]+)>")


@dataclass(frozen=True)
class FeatureStep:
    kind: str  # given / when / then (And/But resolved)
    text: str  # as written; an outline step may hold <param>


@dataclass(frozen=True)
class FeatureScenario:
    name: str
    steps: tuple[FeatureStep, ...]  # background steps first
    examples: tuple[dict[str, str], ...] = ()

    def rendered(self) -> list[tuple[FeatureStep, ...]]:
        """The concrete step lists pytest-bdd runs: one per example row."""
        if not self.examples:
            return [self.steps]

        def fill(text: str, row: dict[str, str]) -> str:
            return _PLACEHOLDER.sub(lambda m: row.get(m.group(1), m.group(0)), text)

        return [tuple(FeatureStep(s.kind, fill(s.text, row)) for s in self.steps)
                for row in self.examples]


def parse_feature(text: str) -> list[FeatureScenario]:
    feature_bg: list[FeatureStep] = []
    rule_bg: list[FeatureStep] = []
    in_rule = False
    scenarios: list[tuple[str, list[FeatureStep], list[dict[str, str]]]] = []
    target: list[FeatureStep] | None = None
    examples: list[dict[str, str]] | None = None
    header: list[str] | None = None
    last = "given"
    doc: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if doc is not None:
            if line.startswith(doc):
                doc = None
            continue
        if line.startswith(('"""', "```")):
            doc = line[:3]
            continue
        if not line or line.startswith(("#", "@")):
            continue
        if m := _HEADER.match(line):
            word = m.group(1)
            examples, header = None, None
            if word == "Feature":
                target = None
            elif word == "Rule":
                in_rule, rule_bg, target = True, [], None
            elif word == "Background":
                target = rule_bg if in_rule else feature_bg
            elif word in ("Examples", "Scenarios"):
                if scenarios:
                    examples = scenarios[-1][2]
                target = None
            else:
                steps = [*feature_bg, *rule_bg]
                scenarios.append((line[m.end():].strip(), steps, []))
                target = steps
            last = "given"
            continue
        if line.startswith("|"):
            if examples is not None:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if header is None:
                    header = cells
                else:
                    examples.append(dict(zip(header, cells, strict=False)))
            continue
        if (sm := _STEP.match(line)) and target is not None:
            word = sm.group(1).lower()
            if word in ("given", "when", "then"):
                last = word
            target.append(FeatureStep(last, sm.group(2)))
    return [FeatureScenario(n, tuple(s), tuple(e)) for n, s, e in scenarios]


# ---- step patterns ----------------------------------------------------------

_PARSE_FIELD = re.compile(r"\{([A-Za-z_]\w*)?(?::([^{}]*))?\}")


def parse_fields(pattern: str) -> tuple[str, ...]:
    """The named fields of a ``parsers.parse`` pattern."""
    return tuple(m.group(1) for m in _PARSE_FIELD.finditer(_unescape_braces(pattern))
                 if m.group(1))


def _unescape_braces(pattern: str) -> str:
    return pattern.replace("{{", "\0").replace("}}", "\1")


def parse_regex(pattern: str) -> re.Pattern[str]:
    """A full-match regex for a ``parsers.parse`` pattern (without the ``parse``
    package: ``{name}`` matches lazily, ``{name:d}`` an integer)."""
    out: list[str] = []
    pos = 0
    text = _unescape_braces(pattern)
    for m in _PARSE_FIELD.finditer(text):
        out.append(re.escape(text[pos:m.start()]))
        out.append(r"[-+]?\d+" if (m.group(2) or "") in ("d", "n") else r".+?")
        pos = m.end()
    out.append(re.escape(text[pos:]))
    return re.compile("".join(out).replace("\0", r"\{").replace("\1", r"\}"))


def step_matches(parser: str, pattern: str, line: str) -> bool | None:
    """Whether a step definition's text matches a feature step line; ``None`` when
    its parser is not one this check can judge."""
    if parser == "":
        return pattern == line
    if parser in ("parse", "cfparse"):
        return parse_regex(pattern).fullmatch(line) is not None
    if parser == "re":
        try:
            return re.fullmatch(pattern, line) is not None
        except re.error:
            return None
    return None


# ---- the scaffold -------------------------------------------------------------


def _ident(name: str, taken: set[str]) -> str:
    base = re.sub(r"\W+", "_", name.strip()).strip("_").lower() or "value"
    if base[0].isdigit() or keyword.iskeyword(base) or base == "context":
        base = f"{base}_"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}_{n}", n + 1
    taken.add(out)
    return out


def _pattern(step: FeatureStep) -> tuple[str, tuple[str, ...]]:
    """(``parsers.parse`` pattern, parameter names) for an outline step; the step's
    own text and no parameters for a literal one."""
    if not _PLACEHOLDER.search(step.text):
        return step.text, ()
    taken: set[str] = set()
    names: dict[str, str] = {}
    pieces: list[str] = []
    pos = 0
    for m in _PLACEHOLDER.finditer(step.text):
        pieces.append(step.text[pos:m.start()].replace("{", "{{").replace("}", "}}"))
        if m.group(1) not in names:
            names[m.group(1)] = _ident(m.group(1), taken)
        pieces.append("{" + names[m.group(1)] + "}")
        pos = m.end()
    pieces.append(step.text[pos:].replace("{", "{{").replace("}", "}}"))
    return "".join(pieces), tuple(names.values())


def scaffold_steps(features: Mapping[str, str]) -> list[tuple[FeatureStep, str, tuple[str, ...]]]:
    """(step, pattern, parameters) per distinct step of ``features``, in first-use
    order. A concrete step line an outline's pattern already matches is left to
    that pattern (two definitions would both match it)."""
    seen: dict[tuple[str, str], tuple[FeatureStep, str, tuple[str, ...]]] = {}
    for text in features.values():
        for sc in parse_feature(text):
            for step in sc.steps:
                if (step.kind, step.text) not in seen:
                    pat, params = _pattern(step)
                    seen[(step.kind, step.text)] = (step, pat, params)
    patterns = [(s.kind, parse_regex(pat)) for s, pat, params in seen.values() if params]
    return [
        entry for (kind, text), entry in seen.items()
        if entry[2] or not any(k == kind and rx.fullmatch(text) for k, rx in patterns)
    ]


def step_scaffold(module: str, features: Mapping[str, str]) -> str:
    """The pytest-bdd step module skeleton for ``module`` binding ``features``
    (repo-relative path -> Gherkin text): the ``scenarios(...)`` calls with paths
    relative to the module, a per-scenario ``context`` dict for values one step
    hands to a later one, and one function per distinct step whose body raises
    ``NotImplementedError``."""
    steps = scaffold_steps(features)
    kinds = sorted({s.kind for s, _a, _p in steps}, key=("given", "when", "then").index)
    names = ", ".join([*kinds, *(["parsers"] if any(p for *_x, p in steps) else []),
                       "scenarios"])
    here = posixpath.dirname(module) or "."
    lines = [
        f'"""Step definitions for {", ".join(features)}."""',
        "",
        "import pytest",
        f"from pytest_bdd import {names}",
        "",
        *(f"scenarios({json.dumps(posixpath.relpath(p, here))})" for p in features),
        "",
        "",
        "@pytest.fixture",
        "def context():",
        '    """Values a step hands to a later step of the same scenario."""',
        "    return {}",
    ]
    taken: set[str] = set()
    for step, pat, params in steps:
        arg = json.dumps(pat, ensure_ascii=False)
        if params:
            arg = f"parsers.parse({arg})"
        fn = _ident(f"{step.kind}_{' '.join(re.findall(r'[A-Za-z0-9]+', step.text))[:48]}",
                    taken)
        lines += ["", "", f"@{step.kind}({arg})",
                  f"def {fn}({', '.join(('context', *params))}):"]
        if step.kind != "then":
            lines.append('    # store what later steps need: context["name"] = value '
                         '(or add target_fixture="name" and return it)')
        lines.append("    raise NotImplementedError")
    return "\n".join(lines) + "\n"


# A test-named module a step module imports from, never a binder itself.
_HELPER_MODULE = re.compile(
    r"(^|_)(helpers?|utils?|common|fixtures?|support|shared|base|factories|mocks?)$"
)
_STEPISH = re.compile(r"(^|_)(steps?|bdd|features?|scenarios?|acceptance)(_|$)")


def is_python_test(path: str) -> bool:
    name = posixpath.basename(path)
    return path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_helper_module(path: str) -> bool:
    stem = posixpath.basename(path).removesuffix(".py")
    return bool(_HELPER_MODULE.search(stem.removeprefix("test_").removesuffix("_test")))


def ticket_step_module(ticket_id: str) -> str:
    """The step module named for a ticket: tests/test_<id>.py."""
    return f"tests/test_{re.sub(r'[^a-z0-9]+', '_', ticket_id.lower()).strip('_')}.py"


def step_module(files: Sequence[str], ticket_id: str = "") -> str | None:
    """The ONE module of ``files`` that binds a ticket's feature files with pytest-bdd:
    its ticket-named tests/test_<id>.py, else a module named for steps / bdd /
    features, else the first test module that is not a helper. Each further module
    binding them too would run every scenario again."""
    tests = [f for f in files if is_python_test(f) and not is_helper_module(f)]
    named = ticket_step_module(ticket_id) if ticket_id else None
    return next((f for f in tests if f == named), None) or next(
        (f for f in tests
         if _STEPISH.search(posixpath.basename(f).removesuffix(".py").removeprefix("test_"))),
        tests[0] if tests else None)
