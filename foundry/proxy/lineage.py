"""Curated, versioned model-id → lineage-family equivalence map. (arch §2b/§4)

The proxy never trusts an adapter's declared model: it observes the
provider-returned model id (``response.model``) and resolves it HERE, through
a curated map that lives under the scanner-config trust boundary
(governance.md §4). Two load-bearing properties:

- **Versioned by content.** ``revision`` is a sha256 digest over the map's
  canonical content; it is stamped into every manifest record and into each
  judgment-verdict binding, and re-checked at admission, so a CI-vs-admission
  map drift or a lineage-splitting edit surfaces as a superseded-revision
  alert instead of silently changing a verdict.
- **Fail-closed resolution.** A served model id that matches no curated
  pattern resolves to NO attestable set — ``resolve`` raises
  :class:`UnknownModelLineageError` rather than guessing, making the node
  ineligible for high-risk judgment (arch §2b).

Patterns are ``fnmatch``-style globs, FIRST MATCH WINS (the map is an ordered
curated artifact, not a scoring heuristic). A single entry may map to a
multi-element family set (e.g. a pinned router/ensemble id whose components
are known and curated).
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase

from foundry.canonical import content_digest


class UnknownModelLineageError(LookupError):
    """Served model id is not pinned to any curated lineage — fail closed."""


# The single-lineage self-judge profile (implementation-plan §0.2): every
# Anthropic Claude tier (Opus/Sonnet/Haiku) is the SAME training lineage —
# the tier mix is a cost choice, never a decorrelation choice.
DEFAULT_LINEAGE_ENTRIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("claude-*", frozenset({"anthropic-claude"})),
)

# Curated lineage for the alternative configured backends (proxy/cli_transports).
# Each is a DISTINCT training lineage from anthropic-claude, so swapping the
# backend genuinely changes the served_family the proxy observes — under the
# `decorrelated` profile that makes reviewer/implementer disjointness a REAL
# guarantee rather than a self-judge stamp. Globs are first-match-wins; extend
# per the exact pinned ids an operator runs.
OPENAI_LINEAGE_ENTRIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("gpt-*", frozenset({"openai-gpt"})),
    ("o1*", frozenset({"openai-gpt"})),
    ("o3*", frozenset({"openai-gpt"})),
    ("o4*", frozenset({"openai-gpt"})),
    ("codex-*", frozenset({"openai-gpt"})),
)
GEMMA_LINEAGE_ENTRIES: tuple[tuple[str, frozenset[str]], ...] = (
    # Ollama tags carry a `:tag` suffix (e.g. ``gemma3:27b``) — the glob spans it.
    ("gemma*", frozenset({"google-gemma"})),
)


class LineageMap:
    """Immutable ordered (pattern → family-set) map with a content revision."""

    def __init__(
        self,
        entries: Iterable[tuple[str, Iterable[str]]] = DEFAULT_LINEAGE_ENTRIES,
    ) -> None:
        self._entries: tuple[tuple[str, frozenset[str]], ...] = tuple(
            (str(pattern), frozenset(str(f) for f in families))
            for pattern, families in entries
        )
        if not self._entries:
            raise ValueError("LineageMap requires at least one curated entry")
        # Canonical content: ordered patterns, sorted family sets — the digest
        # is what gets bound into bindings/attestations (arch §4).
        self._revision = content_digest(
            [[pattern, sorted(families)] for pattern, families in self._entries]
        )

    @property
    def revision(self) -> str:
        """Content digest of the curated map — bound into judgment bindings."""
        return self._revision

    @property
    def entries(self) -> tuple[tuple[str, frozenset[str]], ...]:
        return self._entries

    def resolve(self, served_model: str) -> frozenset[str]:
        """Resolve a provider-OBSERVED model id to its lineage-family set.

        Fail-closed: an unknown / unpinned id raises — the proxy never guesses
        a lineage, and the caller never gets an attestable set for it.
        """
        if not served_model:
            raise UnknownModelLineageError(
                "empty served model id — provider observation missing; fail closed"
            )
        for pattern, families in self._entries:
            if fnmatchcase(served_model, pattern):
                return families
        raise UnknownModelLineageError(
            f"served model {served_model!r} is not pinned to any curated lineage "
            f"(map revision {self._revision}); node is ineligible for attestable "
            "judgment — escalate, never guess"
        )
