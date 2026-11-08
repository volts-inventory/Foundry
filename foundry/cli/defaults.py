"""Release modes, backends, coding adapters, scopes, and the env-derived
defaults (slot cap, local server) the CLI and the Workshop share."""

from __future__ import annotations

import os

from foundry.graph.supervisor import MODE_ASSISTANT
from foundry.proxy.local_transport import DEFAULT_LOCAL_MODEL, DEFAULT_LOCAL_URL

# The release modes the CLI accepts. Only assistant remains; the flag is kept so
# existing `--mode assistant` invocations still work.
MODES = (MODE_ASSISTANT,)


# The model backends selectable via ``--backend`` / run(backend=). "mock" (DEFAULT)
# is the deterministic scripted MockTransport — keyless, no network, the historical
# smoke behavior. The others route the factory's model egress through a REAL backend
# via the single-egress proxy (foundry/proxy/cli_transports.build_backend): "anthropic"
# (SDK; needs ANTHROPIC_API_KEY), "openai" (SDK chat.completions; needs OPENAI_API_KEY),
# "claude-cli" (`claude -p`), "codex" (`codex exec`).
# Picking a real backend makes real, metered model calls; the coding substrate +
# materialization in THIS smoke CLI stay dev-grade (StubAdapter + synthetic resolver),
# so --backend exercises the proxy/lineage/determinism/manifest path, not a real
# code-writing adapter (that is a separate seam).
_MOCK_BACKEND = "mock"


BACKENDS = (_MOCK_BACKEND, "anthropic", "openai", "claude-cli", "codex")


# Coding substrates (WorkerAdapter): the thing that actually writes the code for
# each task. `stub` is the deterministic no-op (the smoke default); `raw-react` is a
# provider-agnostic single-loop substrate over the model backend (one completion ->
# one diff, any backend); `claude-code` is the AGENTIC substrate (ROADMAP Phase 1a) —
# it runs the `claude` CLI multi-turn INSIDE the task sandbox, where it edits files
# and runs builds/tests itself, and authenticates via the sandbox's typed model-auth
# grant (Max login/token = no API billing, or ANTHROPIC_API_KEY = metered).
ADAPTERS = ("stub", "raw-react", "claude-code", "proxy-agent")


_DEFAULT_ADAPTER = "stub"


# Scope tier selection: how much system to build. `auto` (the default) lets the
# planner classify the request; `demo`/`standard` PIN the tier (the planner
# keeps it and applies its caps). Pinning gives a deterministic tier for an A/B against
# the judged one. Scope caps what the agents PROPOSE, never what the gates
# require — both tiers face an identical gate set.
SCOPES = ("auto", "demo", "standard")


# When --adapter is OMITTED the coding substrate is DERIVED from the chosen backend,
# so picking a real brain writes real code without a second flag (the historical
# footgun: a real --backend silently no-op'd under a hardcoded stub default). `mock`
# has no real model, so it keeps the deterministic no-op `stub` (the default run stays
# hermetic + byte-identical). `claude-cli` — the backend whose substrate binary IS
# Claude Code — defaults to the agentic `claude-code` adapter (the roadmap's biggest
# quality lever; same binary, same auth). Every other real backend defaults to
# `raw-react`, the provider-agnostic completion substrate. FOUNDRY_CODING_ADAPTER
# (the same env var the Workshop UI reads) overrides the derivation env-side; an
# explicit --adapter always wins.
def _default_adapter_for(backend: str) -> str:
    """The coding substrate to use when ``--adapter`` is omitted, derived from
    ``backend``: ``mock`` ⇒ ``stub`` (no real model), ``claude-cli`` ⇒
    ``claude-code`` (agentic, same binary + auth), any other real backend ⇒
    ``raw-react`` (the universal completion substrate). ``FOUNDRY_CODING_ADAPTER``
    (when set to a known adapter) overrides the derivation."""
    env = os.environ.get("FOUNDRY_CODING_ADAPTER", "").strip().lower()
    if env in ADAPTERS:
        return env
    if backend == _MOCK_BACKEND:
        return _DEFAULT_ADAPTER
    return "claude-code" if backend == "claude-cli" else "raw-react"


# System-decided parallelism (no user knob): the Tech-Lead decomposes the work
# into independently-buildable tasks, and the IC pool sizes ITSELF to that plan —
# one worker per ready task, bounded by this SAFETY CAP so a large decomposition
# can't spawn unbounded concurrent model/coding sessions (rate-limit / resource
# guard). Effective parallelism = min(tasks the lead created, AUTO_SLOTS_CAP); a
# single-task build still runs serially. An explicit --max-slots / FOUNDRY_MAX_SLOTS
# only LOWERS or RAISES this ceiling for constrained/over-provisioned infra; it is
# never required, and the default is fully automatic. The ceiling itself is tunable
# via FOUNDRY_SLOTS_CAP (default 8) so a complex, many-task build can fan out wider
# without per-call flags.
AUTO_SLOTS_CAP = max(1, int(os.environ.get("FOUNDRY_SLOTS_CAP") or 8))


def env_max_slots() -> int:
    """The IC-pool ceiling when no --max-slots is given: FOUNDRY_MAX_SLOTS, else
    :data:`AUTO_SLOTS_CAP` (also when it isn't a number)."""
    try:
        return int(os.environ.get("FOUNDRY_MAX_SLOTS", "") or AUTO_SLOTS_CAP)
    except ValueError:
        return AUTO_SLOTS_CAP


def env_local_url() -> str:
    """The local server when no --local-url is given: FOUNDRY_LOCAL_URL, else
    :data:`DEFAULT_LOCAL_URL`. Not validated here."""
    return os.environ.get("FOUNDRY_LOCAL_URL", "").strip() or DEFAULT_LOCAL_URL


def env_local_model() -> str:
    """The local model when no --local-model is given: FOUNDRY_LOCAL_MODEL, else
    :data:`DEFAULT_LOCAL_MODEL`. Not validated here."""
    return os.environ.get("FOUNDRY_LOCAL_MODEL", "").strip() or DEFAULT_LOCAL_MODEL
