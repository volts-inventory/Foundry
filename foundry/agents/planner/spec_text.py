"""LLM-free reads of the plan's prose: the data types its design body defines with
their fields, and pinned numeric bounds a scenario's literal example breaks."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from foundry.schemas.product import AcceptanceCriterion

_IDENT = r"[A-Za-z_]\w*"
_MODIFIERS = r"(?:(?:pub|readonly|val|var|public|private|protected|final)\s+)*"
# `sides: int`, `sides?: number`, `val sides: Int`, `sides: int = 6`
_COLON_FIELD = re.compile(rf"^{_MODIFIERS}({_IDENT})\??\s*:\s*(\S.*)$")
# `int sides`, `Sides int`, `List<Term> terms` (Java, C#, Go)
_TYPED_FIELD = re.compile(rf"^{_MODIFIERS}([\w.<>\[\]?*]+(?:<[^<>]*>)?)\s+({_IDENT})$")
_HEADER = re.compile(
    r"^(\s*)(?:export\s+)?(?:(?:data|frozen|final|public|pub|abstract|sealed)\s+)*"
    r"(?:class|struct|record|interface|type)\s+([A-Z]\w*)\b(.*)$"
)
_INLINE = re.compile(r"\b([A-Z]\w*)\s*\(")
_CAMEL = re.compile(r"\b[A-Z]\w*\b")


def _balanced(text: str, start: int) -> str | None:
    """The text inside the parenthesis opening at ``text[start]``."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] in "([{":
            depth += 1
        elif text[i] in ")]}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    return None


def _split(params: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in params:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return [p for p in parts if p]


# Lowercase type names a `type name` / Go `name type` field may use; any other
# lowercase pair is prose (`Helper(the input)`).
_PRIMITIVES = frozenset(
    [
        "int",
        "long",
        "short",
        "byte",
        "char",
        "bool",
        "boolean",
        "float",
        "double",
        "string",
        "str",
        "uint",
        "int32",
        "int64",
        "uint32",
        "uint64",
        "float32",
        "float64",
        "usize",
        "isize",
        "i32",
        "i64",
        "u32",
        "u64",
        "f32",
        "f64",
        "decimal",
    ]
)


def _looks_typed(first: str, second: str) -> bool:
    return (
        first[0].isupper()
        or any(c in first for c in "<[.*")
        or first in _PRIMITIVES
        or second in _PRIMITIVES
    )


def _field(text: str) -> str | None:
    text = re.sub(r"\s*(#|//).*$", "", text).strip().rstrip(";,").strip()
    if not text or "(" in text:
        return None
    if m := _COLON_FIELD.match(text):
        return f"{m.group(1)}: {m.group(2).strip()}"
    if (m := _TYPED_FIELD.match(text)) and _looks_typed(m.group(1), m.group(2)):
        return f"{m.group(1)} {m.group(2)}"
    return None


def design_types(text: str) -> dict[str, str]:
    """``Name -> "Name(field: type, ...)"`` for each data type ``text`` defines with
    its fields, inline (``RollExpression(terms: list[SignedTerm])``, ``record
    Term(int sides)``) or as a class / struct / interface block. A name defined twice
    keeps the first definition."""
    out: dict[str, str] = {}
    for m in _INLINE.finditer(text):
        inner = _balanced(text, m.end() - 1)
        parts = _split(inner) if inner is not None else []
        params = [f for f in map(_field, parts) if f is not None]
        if params and len(params) == len(parts) and m.group(1) not in out:
            out[m.group(1)] = f"{m.group(1)}({', '.join(params)})"
    lines = text.splitlines()
    for n, line in enumerate(lines):
        h = _HEADER.match(line)
        if h is None or h.group(2) in out:
            continue
        indent, rest = len(h.group(1)), h.group(3).strip()
        braced = rest.endswith("{")
        if not braced and not rest.endswith(":"):
            continue
        fields: list[str] = []
        for body in lines[n + 1 :]:
            stripped = body.strip()
            if braced and stripped.startswith("}"):
                break
            if not braced and stripped and len(body) - len(body.lstrip()) <= indent:
                break
            if (f := _field(stripped)) is not None:
                fields.append(f)
        if fields:
            out[h.group(2)] = f"{h.group(2)}({', '.join(fields)})"
    return out


def with_referenced(names: Iterable[str], types: Mapping[str, str]) -> list[str]:
    """``names`` that ``types`` defines, then every type their fields name in turn."""
    out: list[str] = []
    todo = [n for n in names if n in types]
    while todo:
        name = todo.pop(0)
        if name in out:
            continue
        out.append(name)
        todo += [n for n in _CAMEL.findall(types[name].partition("(")[2]) if n in types]
    return out


# A numeric bound in a pinned constraint, over the noun it names.
_OPS = {">=": "≥", "<=": "≤", ">": ">", "<": "<"}
_NOUN = r"([a-z][a-z_]*)"
_VERB = r"(?:\s+(?:must|should|shall|can|may))?(?:\s+(?:be|is|are))?"
_NUM = r"(-?\d+)(?!\d|\.\d)"
# After a noun-first bound: a unit after the number (`names at most 100 characters`)
# means the bound is on the unit, not the noun before it.
_NO_UNIT = r"(?!\s*[a-z%])"
_BOUNDS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (re.compile(rf"\b{_NOUN}{_VERB}\s*(>=|<=|>|<)\s*{_NUM}{_NO_UNIT}"), "", False),
    (re.compile(rf"\b{_NOUN}{_VERB}\s+at least\s+{_NUM}{_NO_UNIT}"), ">=", False),
    (re.compile(rf"\b{_NOUN}{_VERB}\s+(?:at most|no more than)\s+{_NUM}{_NO_UNIT}"), "<=", False),
    (re.compile(rf"\bat least\s+{_NUM}\s+{_NOUN}"), ">=", True),
    (
        re.compile(rf"\b(?:at most|no more than|max(?:imum)?(?: of)?)\s+{_NUM}\s+{_NOUN}"),
        "<=",
        True,
    ),
)
_BETWEEN = re.compile(rf"\b{_NOUN}{_VERB}\s+between\s+{_NUM}\s+and\s+{_NUM}{_NO_UNIT}")
# Words too generic to tie a constraint's number to a scenario's.
_VAGUE = frozenset(
    [
        "be",
        "is",
        "are",
        "must",
        "should",
        "it",
        "its",
        "value",
        "values",
        "number",
        "numbers",
        "than",
        "of",
        "and",
        "or",
        "the",
        "a",
        "an",
        "least",
        "most",
        "each",
        "per",
        "input",
        "inputs",
        "output",
        "result",
        "results",
        "length",
        "size",
        "total",
    ]
)
# A constraint with a condition or an alternative is not one plain bound.
_CONDITIONAL = re.compile(r"\b(unless|except|when|if|or|otherwise)\b")
# A scenario proving a value is refused uses an out-of-bound value on purpose.
_REJECTS = re.compile(
    r"\b(reject\w*|error\w*|invalid|fail\w*|refus\w*|not allowed|denied|raises?|"
    r"non-?zero|usage|exits? (?:with )?(?:code |status )?[1-9])\b"
)


def _normalise(text: str) -> str:
    return text.lower().replace("≥", ">=").replace("≤", "<=").replace("\u2212", "-")


def _bounds(text: str) -> list[tuple[str, str, int]]:
    """``(noun, op, n)`` for each unambiguous bound ``text`` states."""
    text = _normalise(text)
    if _CONDITIONAL.search(text):
        return []
    out: list[tuple[str, str, int]] = []
    for pattern, fixed, number_first in _BOUNDS:
        for m in pattern.finditer(text):
            if fixed:
                num, noun = (m.group(1), m.group(2)) if number_first else (m.group(2), m.group(1))
                out.append((noun, fixed, int(num)))
            else:
                out.append((m.group(1), m.group(2), int(m.group(3))))
    for m in _BETWEEN.finditer(text):
        out += [(m.group(1), ">=", int(m.group(2))), (m.group(1), "<=", int(m.group(3)))]
    return [(noun, op, n) for noun, op, n in out if noun not in _VAGUE]


def _holds(value: int, op: str, n: int) -> bool:
    return {
        ">=": value >= n,
        "<=": value <= n,
        ">": value > n,
        "<": value < n,
    }[op]


def _values(text: str, noun: str) -> list[int]:
    """The numbers ``text`` ties to ``noun``: ``1 side``, ``sides = 1``, ``sides of 1``."""
    stem = noun[:-1] if noun.endswith("s") and len(noun) > 3 else noun
    word = rf"{re.escape(stem)}s?"
    found = re.findall(rf"(?<![\w.-]){_NUM}\s+{word}\b", text)
    found += re.findall(rf"\b{word}\s*(?:=|:|of|is)\s*{_NUM}", text)
    return [int(v) for v in found]


def bound_conflicts(
    constraints: Sequence[tuple[str, str]], criteria: Sequence[AcceptanceCriterion]
) -> tuple[str, ...]:
    """Scenarios whose literal example breaks a pinned numeric bound on the same noun
    (``sides >= 2`` beside ``a die with 1 side``): no correct implementation passes
    both. Conservative: a conditional constraint, one stating what is refused, a
    generic noun, or a scenario proving the value is rejected is never reported."""
    out: list[str] = []
    for cid, text in constraints:
        # `count > 10000 raises ParseError` bounds what is refused, not what is required.
        if _REJECTS.search(_normalise(text)):
            continue
        for noun, op, n in _bounds(text):
            for c in criteria:
                scenario = _normalise(c.text)
                if c.security_abuse or _REJECTS.search(scenario):
                    continue
                bad = [v for v in _values(scenario, noun) if not _holds(v, op, n)]
                if bad:
                    out.append(
                        f"Scenario {c.id} uses {noun} {bad[0]}, but pinned constraint {cid} "
                        f"requires {noun} {_OPS[op]} {n}: no implementation passes both. Fix "
                        "the scenario or the constraint."
                    )
    return tuple(dict.fromkeys(out))
