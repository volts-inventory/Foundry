"""Design-spec ingestion (task [8]): read a ``--spec`` file or directory, and
derive a request from it."""

from __future__ import annotations

import os
from pathlib import Path

# Spec ingestion (task [8]). A whole-run cap on the design-spec text seeded into
# state: bounds checkpoint size + token cost on a large doc / directory; the
# product nodes truncate again for their prompts. Read as UTF-8, errors replaced
# (a spec is plain input data, never executed — no new trust surface). Tunable via
# FOUNDRY_SPEC_MAX_BYTES (default 2 MiB) so a complex build can ground itself in a
# large design doc; raising it trades a bigger checkpoint / more tokens for depth.
_SPEC_MAX_BYTES = max(64 * 1024, int(os.environ.get("FOUNDRY_SPEC_MAX_BYTES") or (2 * 1024 * 1024)))


_SPEC_SUFFIXES = (".md", ".txt")


# ---- design-spec ingestion (task [8]) ---------------------------------------


def _read_spec_path(path: str) -> str:
    """Read a design-spec FILE or DIRECTORY of ``.md``/``.txt`` docs to text.

    A directory is read recursively in sorted (deterministic) path order; only
    ``.md``/``.txt`` files are included. Each file's text is prefixed with a
    ``# === <relative-path> ===`` marker so a multi-file spec stays legible.
    Raises :class:`FileNotFoundError` for a missing path and :class:`ValueError`
    when a directory contains no spec files (a clean, actionable error).

    The spec is INPUT DATA only: it is read as UTF-8 (decode errors replaced) and
    never executed — no new egress, no new trust surface. The combined text is
    capped at :data:`_SPEC_MAX_BYTES` to bound checkpoint size / token cost.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"spec path not found: {path}")
    if p.is_dir():
        files = sorted(
            f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in _SPEC_SUFFIXES
        )
        if not files:
            raise ValueError(f"spec directory has no {'/'.join(_SPEC_SUFFIXES)} files: {path}")
        parts = [
            f"# === {f.relative_to(p)} ===\n{f.read_text(encoding='utf-8', errors='replace')}"
            for f in files
        ]
        return "\n\n".join(parts)
    return p.read_text(encoding="utf-8", errors="replace")


def _is_existing_path(value: str) -> bool:
    """True iff ``value`` names an existing path. Guards ``Path.exists()`` against
    an ``OSError`` (e.g. a long literal-spec string exceeding the OS name limit),
    so literal spec text never crashes the path probe. An empty/whitespace string
    is NEVER a path (``Path("").exists()`` resolves to cwd) — else an explicit
    ``spec=""`` would silently ingest the entire working directory."""
    if not value.strip():
        return False
    try:
        return Path(value).exists()
    except OSError:
        return False


def _load_spec(spec: str | list[str] | None) -> str:
    """Resolve the ``spec`` argument to the design-spec text seeded into state.

    ``spec`` is additive (task [8]) and may be:

    * ``None`` — no spec (request-only run, unchanged behavior);
    * a single string that names an existing file/dir — its text is read;
    * a single string that is NOT a path — treated as literal spec text;
    * a list of file/dir paths — each read and concatenated (sorted by path
      marker, deterministic).

    The combined text is capped at :data:`_SPEC_MAX_BYTES`.
    """
    if spec is None:
        return ""
    if isinstance(spec, str):
        if not spec.strip():
            return ""  # empty/blank literal == no spec (never probed as a path)
        text = _read_spec_path(spec) if _is_existing_path(spec) else spec
    else:
        text = "\n\n".join(_read_spec_path(s) for s in sorted(spec))
    encoded = text.encode("utf-8")
    if len(encoded) > _SPEC_MAX_BYTES:
        # Cap on ENCODED BYTES (deterministic, truly bounds checkpoint size even
        # for multibyte specs); decode back, dropping a partial trailing char.
        # The product nodes truncate again for their prompts.
        text = encoded[:_SPEC_MAX_BYTES].decode("utf-8", errors="ignore")
    return text


def _request_from_spec(spec_text: str) -> str:
    """Derive a one-line request from a spec's text — its first heading / non-blank
    line — so a build can run from ``--spec FILE`` alone (no positional request).
    Falls back to a generic line if the spec has no usable heading."""
    for line in spec_text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:200]
    return "Build the system described in the provided spec."
