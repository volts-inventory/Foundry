"""Worker adapters — the swappable substrate seam (architecture.md §2).

The shipped substrates are :class:`~foundry.execution.adapters.stub.StubAdapter`
(deterministic no-op) and
:class:`~foundry.execution.adapters.claude_code.ClaudeCodeAdapter` (the agentic
``claude`` CLI coder). Import the one you need from its module directly.
"""
