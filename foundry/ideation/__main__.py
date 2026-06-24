"""Standalone runner: ``python -m foundry.ideation "<rough idea>"``.

Drives the ideation loop and prints the rendered spec markdown to stdout, so a
human can ideate then redirect the output to a file and feed it to a build:

    python -m foundry.ideation "a CLI todo app" > spec.md
    python -m foundry.cli --spec spec.md "build a CLI todo app"

By default it constructs a :class:`ModelProviderProxy` over the real Anthropic
transport (the proxy is the single credential holder; this entry point holds no
key of its own). With ``--mock`` it runs entirely on a deterministic
:class:`MockTransport` (no live API key, no network) — useful for a dry run and
for the tests, which inject the proxy directly via :func:`foundry.ideation.ideate`.

This entry point makes NO decision and writes to no channel — it only prints the
advisory spec markdown (see :mod:`foundry.ideation`).
"""

from __future__ import annotations

import sys

from foundry.agents._runtime import AgentRuntime
from foundry.ideation.loop import _DEFAULT_ROUNDS, ideate
from foundry.proxy import ModelProviderProxy


def _build_proxy(*, mock: bool) -> ModelProviderProxy:
    """The single egress for the run. ``--mock`` swaps in the deterministic
    test double so a dry run needs no API key or network."""
    if mock:
        from foundry.proxy.test_doubles import MockTransport

        # A deterministic, schema-shaped ideation payload served on the free-text
        # (non-verdict) channel for the dry run.
        return ModelProviderProxy(transport=MockTransport(default_text=_mock_text()))
    return ModelProviderProxy()


def _mock_text() -> str:
    """A canned ideation JSON payload for the ``--mock`` dry run."""
    import json

    return json.dumps(
        {
            "title": "Mock Spec",
            "summary": "A deterministic placeholder spec for a --mock dry run.",
            "clarifying_questions": ["Who are the users?"],
            "assumptions": ["single user"],
            "capabilities": ["cli"],
            "acceptance_criteria": [
                {
                    "id": "AC-001",
                    "text": "Given the tool, When invoked, Then it prints a spec.",
                    "oracle_hint": "run the command; stdout is non-empty markdown",
                    "security_abuse": False,
                }
            ],
            "architecture_sketch": ["single-process CLI"],
        }
    )


def main(argv: list[str] | None = None) -> int:
    """Parse args, run the ideation loop, print the spec markdown."""
    args = list(sys.argv[1:] if argv is None else argv)

    mock = False
    if "--mock" in args:
        mock = True
        args = [a for a in args if a != "--mock"]

    rounds = _DEFAULT_ROUNDS
    if "--rounds" in args:
        i = args.index("--rounds")
        if i + 1 >= len(args):
            print("error: --rounds requires an integer argument", file=sys.stderr)
            return 2
        try:
            rounds = int(args[i + 1])
        except ValueError:
            print("error: --rounds must be an integer", file=sys.stderr)
            return 2
        args = args[:i] + args[i + 2 :]

    idea = " ".join(args).strip()
    if not idea:
        print(
            'usage: python -m foundry.ideation [--mock] [--rounds N] "<rough idea>"',
            file=sys.stderr,
        )
        return 2

    rt = AgentRuntime(proxy=_build_proxy(mock=mock))
    try:
        result = ideate(rt, idea, rounds=rounds)
    except Exception as exc:
        # Ideation is a best-effort authoring aid (no trust path); a transport
        # failure / unparseable reply prints a clean error + nonzero exit rather
        # than crashing with a traceback.
        print(f"error: ideation failed: {exc}", file=sys.stderr)
        return 1
    print(result.spec_markdown)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
