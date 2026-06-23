# Usage — Setup, Run, Deploy

This is the operator-facing guide to the **built** Foundry system (the code in
`foundry/` + `tests/`). It covers local setup, running the factory in each of the
three modes, and the exact boundary between the dev-grade build and a
production-hardened deployment.

It is honest about what is real vs. a dev-grade stand-in — see
[implementation-plan.md §0.3](./implementation-plan.md) for the authoritative
split. Every command below is one that runs against this repository.

---

## Setup

### Prerequisites

- **Python 3.12** (the build pins `requires-python = ">=3.12"`).
- **[uv](https://docs.astral.sh/uv/)** — the package/venv manager.

No API key, no `claude` binary, and no network are needed for setup, the gate
chain, or the CLI smoke run — those use deterministic test doubles.

### Install

```sh
uv sync --dev
```

This resolves the runtime deps (`anthropic`, `langgraph`,
`langgraph-checkpoint-sqlite`, `pydantic`) and the dev tooling (`ruff`, `mypy`,
`pytest` + `pytest-cov`/`pytest-xdist`, `hypothesis`, `mutmut`) into a managed
virtualenv.

### The gate chain (local — there is NO CI by design)

Foundry's quality bar is enforced **locally**, not by a hosted pipeline. Run the
chain after any change:

```sh
uv run ruff check foundry tests        # lint (E,F,I,UP,B,SIM,RUF)
uv run mypy foundry                    # type check — the static-analysis bar
uv run pytest -q                       # property + idempotency + adversarial suites
```

All three pass clean on a fresh `uv sync --dev` checkout. The reducer-purity,
idempotent-resume, and trust/attestation property tests are load-bearing — they
are the gate that the state and trust machinery behave under adversarial input.

Optional milestone-scoped gates (configured in `pyproject.toml`): coverage
(`pytest --cov-fail-under=…`) and mutation testing
(`uv run mutmut run`, scoped to `foundry/state/reducers.py`).

---

## Run

### The CLI (Assistant mode, keyless)

```sh
uv run python -m foundry.cli "<one-line request>"
# e.g.
uv run python -m foundry.cli "build a tiny URL-shortener service"
```

This is the Phase-1 smoke entry point (`foundry/cli.py`). From a single
natural-language request it assembles the full dev wiring with test doubles —
**no live API key, no `claude -p` binary, no network** — compiles
`build_factory_graph(...)`, and drives the SPEC core path:

```
intake -> product -> architecture -> [spec gate] -> implementation
       -> quality -> [build+test gates] -> emit (open_pr action)
```

It prints the reviewed FeatureDiff handle, the single `open_pr` `ExternalAction`
(idempotency key + payload uri), and the attested gate statuses. Exit code is
**0** on a clean Spec/Build/Test PASS (a PR action was emitted), **1** on a
halted/failed run, **2** on a usage error.

What backs the keyless run (all dev-grade stand-ins, deliberately):

- model egress → `ModelProviderProxy` over a `MockTransport` with scripted
  per-role completions (deterministic, no key);
- coding substrate → `StubAdapter` (authors a deterministic no-op blob, not real
  code — see the materialization seam below);
- verdict signing → a dev `ZoneBSigner` (HMAC) + a real `TwoZoneGateRunner`;
- durability → an in-process `FoundryCheckpointer` (resumable).

Because `StubAdapter` never authors a real passing test suite, the CLI installs
KIND-dispatching synthetic `resolve`/`resolve_blob` seams that synthesize a
faithful body per artifact kind and a passing unified diff, so the SPEC core path
reaches a clean PASS. These seams fall through to the real `ArtifactStore`, so a
future adapter that authors a real suite needs no CLI change.

### The three modes (and how they differ)

The modes live in `foundry/graph/factory_graph.py` (constants
`MODE_ASSISTANT`/`MODE_GATED`/`MODE_AUTONOMOUS` in `foundry/graph/supervisor.py`).
The **CLI runs Assistant mode only**; Gated and Autonomous are reached
programmatically via `build_factory_graph(..., mode=...)`.

| Mode | Release boundary | What it emits | Requires |
|---|---|---|---|
| **Assistant** (default) | Spec+Build+Test all PASS | bare `open_pr` action (human merges) | nothing extra |
| **Gated** | terminal gates **+** tier-scaled assurance (Security/Audit/supply-chain/E2E) PASS **+** a recorded, approved Release sign-off | `open_pr` carrying a tamper-evident WORM `EvidencePack` | risk-tiering + recorded sign-offs in state |
| **Autonomous** | the trust root: build → admit → deploy; **admission is the sole release gate** | a `deploy` action (no human sign-off) | injected `builder`, `admission`, `deployer`, `gate_verifier` |

All three are **fail-closed**: a halted/failed/unsigned/denied run emits nothing.
An example Gated/Autonomous assembly (the doubles, the runner, the trust-root
components) is in `tests/test_factory_graph.py` and the Phase-4 exit test
(`tests/test_phase4_exit.py`); mirror that wiring to drive those modes.

### Running with a real Anthropic key + the `claude_code` adapter

Two independent swaps move you off the keyless doubles:

1. **Real model egress.** Construct the proxy over the default
   `AnthropicTransport` instead of `MockTransport`:

   ```python
   from foundry.proxy import ModelProviderProxy
   from foundry.proxy.model_provider_proxy import AnthropicTransport

   proxy = ModelProviderProxy(AnthropicTransport(), signer=ZoneBSigner(...))
   ```

   `AnthropicTransport` lazily constructs `anthropic.Anthropic()` on first use, so
   it reads the credential from the environment (`ANTHROPIC_API_KEY`) at call
   time. The CLI never imports `anthropic` — all model egress stays behind the
   proxy.

2. **Real coding substrate.** Swap `StubAdapter()` for `ClaudeCodeAdapter`
   (`foundry/execution/adapters/claude_code.py`), which runs
   `claude -p "<prompt>" --output-format json` inside the sandbox. It requires the
   `claude` binary on `PATH` (it raises `AdapterUnavailableError` otherwise). With
   a real adapter authoring real diffs, drop the CLI's synthetic
   `resolve`/`resolve_blob` so the real `ArtifactStore` views materialize the
   actual bodies the gates run against.

The CLI itself does not expose flags for either swap — it is a fixed keyless
smoke harness. Use `foundry.cli.run(...)` or `build_factory_graph(...)` directly
to pass a real proxy/adapter.

### Pointing a domain pack via the registry

Domain packs (`foundry/domains/packs/`: `RegulatedPack`, `MLDataPack`,
`FrontendPack`) engage by **capability routing** — no core change to add one.
Build a `PackRegistry`, register the packs, and thread it through:

```python
from foundry.domains.registry import PackRegistry
from foundry.domains.packs.regulated import RegulatedPack
from foundry.domains.packs.frontend import FrontendPack

registry = PackRegistry()
registry.register(RegulatedPack())
registry.register(FrontendPack())

app = build_factory_graph(rt, runner=runner, ..., pack_registry=registry)
```

The registry indexes packs by capability and engages every pack whose
capabilities intersect the request's `required_capabilities`; the engaged packs'
policies compose **strictest-wins** (tighten-only — a pack can only make the
release stricter, never relax the integrity floor). At the release boundary this
enforces, fail-closed: a regulated/class-4 release requires a recorded **human**
approver-of-record (and vetoes Autonomous), a frontend/class-5 release requires a
human perceptual HITL (never auto-approved). Adding a fourth pack is one
`registry.register(...)` call. `pack_registry=None` (the default) engages no
packs — exact prior behavior.

---

## Deploy

### What you have out of the box: a dev-grade trust root

The trust-root **logic** is real and adversarially tested — the two-zone gate
runner, input-bound attestations, deploy-time admission, the least-privilege
deployer, and the hash-chained WORM evidence pack all enforce their invariants and
are exercised by the property/adversarial suites (a malicious-test /
forged-results / forged-verdict / fork-divergence attempt cannot reach the emit).
What is dev-grade is the **roots and substrate** the logic stands on.

### Exactly what production hardening remains

Per [implementation-plan.md §0.3](./implementation-plan.md), an operator crosses
these boundaries to leave dev-grade. None require changing the trust-root logic —
they replace its roots/backends:

| Concern | Dev-grade stand-in (today) | Production hardening (the ops task) |
|---|---|---|
| **Verdict / build signing** | dev HMAC `ZoneBSigner`, single-process two zones | real **KMS / OIDC-federated keyless (Sigstore) signing**, air-gapped keys, true zone isolation |
| **Build infrastructure** | constant content-addressed input stand-ins | a **strongly-hermetic SLSA-L3 builder** on isolated infra with real source/base-image pins |
| **Durable state / artifacts** | in-process checkpointer + in-memory `ArtifactStore` | **HA Postgres** (checkpoints/outbox/WORM) + a real **content-addressed store** |
| **Scanners** | gate logic with stubbed scan inputs | real **OPA/Conftest**, **Semgrep** (SAST), **Syft/Grype** (SBOM/CVE), license scanner from a run-immutable config repo |
| **Observability** | local span/record models | a real **OpenTelemetry exporter** (+ Langfuse) |
| **Model provider** | `MockTransport` (keyless) | `AnthropicTransport` with managed credentials + real burst capacity |
| **Worker substrate** | `StubAdapter` | `ClaudeCodeAdapter` with the `claude` binary + a real sandbox |

### There is no CI — gates are local by design

Foundry does not ship a hosted CI pipeline. The release authority is the in-graph
gate/trust machinery (and, for Autonomous, deploy-time admission), and the
developer-facing quality bar is the local `ruff` / `mypy` / `pytest` chain above.
The outbox relay is the only component that ever touches the outside world; the
factory graph itself makes no GitHub/network call — it expresses a PR/deploy only
as an idempotent `ExternalAction`, so a kill-then-resume re-derives the same
idempotency key and never produces a duplicate external effect.
