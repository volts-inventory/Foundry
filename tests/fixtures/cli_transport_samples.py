"""Captured-style real-shape CLI output samples for the claude -p backend.

These mirror what the current binary actually emits so the extraction in
``foundry.proxy.cli_transports`` is exercised against the wire shapes, not just
synthetic ones:

* ``CLAUDE_P_JSON_RESULT`` — current ``claude -p --output-format json`` single
  result envelope (carries ``total_cost_usd`` + ``usage`` + ``modelUsage``).
* ``CLAUDE_P_JSON_NO_MODELUSAGE`` — an older/degraded envelope that omits
  ``modelUsage`` but still carries a top-level ``model`` and a ``usage`` block.

Token/cost numbers are illustrative; the field NAMES and NESTING are the load-
bearing part being asserted.
"""

from __future__ import annotations

# --- claude -p --output-format json -------------------------------------- #
# Single JSON object on stdout (current shape).
CLAUDE_P_JSON_RESULT = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"duration_ms":2410,"num_turns":1,'
    '"result":"Here is the final answer from claude.",'
    '"session_id":"a1b2c3d4-0000-4444-8888-abcdef012345",'
    '"total_cost_usd":0.0123,'
    '"usage":{"input_tokens":140,"output_tokens":52,'
    '"cache_read_input_tokens":96,"cache_creation_input_tokens":0},'
    '"modelUsage":{"claude-opus-4-8":'
    '{"inputTokens":140,"outputTokens":52,"cacheReadInputTokens":96}}}'
)

# Older/degraded envelope: no modelUsage, but a top-level model + usage block.
CLAUDE_P_JSON_NO_MODELUSAGE = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"Answer without modelUsage.",'
    '"session_id":"deadbeef-0000-4444-8888-000000000000",'
    '"total_cost_usd":0.004,"model":"claude-opus-4-8",'
    '"usage":{"input_tokens":20,"output_tokens":8}}'
)
