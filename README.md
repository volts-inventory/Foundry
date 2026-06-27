# Foundry — an AI software factory

> A graph of role-specialized AI agents that takes a request from **intent → released, audited software** — behind deterministic quality gates, a cryptographic trust root, durable state, and human-in-the-loop checkpoints.

Foundry models a *whole software organization* as a directed graph. **Product** turns a request into a PRD and testable acceptance criteria; **Architecture** designs the system; **Engineering** implements it; **QA** authors the test oracles; **Security / Audit / Supply-chain** review it; **Governance** gates, signs, and (optionally) deploys it. Agents only ever **propose** — the **signed gates are the sole authority on whether anything ships.**

```text
        request ──▶  Product ──▶ Architecture ──▶ [Spec gate] ──▶ Implementation
                       │             │                                  │
                  PRD + criteria   design + ADRs                    feature diff
                                                                        │
   release  ◀── emit ◀── [Build+Test gate] ◀── Quality ◀───────────────┘
   (PR · evidence pack · deploy)       │            (tests · oracles · review)
                                Security · Audit · Supply-chain   (Gated / Autonomous)
```

This repo is **both the design and a working build**: the design lives in [`PLAN.md`](./PLAN.md) + [`docs/`](./docs/); the implementation is `foundry/` (183 modules) with **~1,890 passing tests**. It runs end-to-end today — from a one-line request, through every gate, to generated code + a doc set on disk — on the deterministic mock backend with **no API key and no network**, and on a real model backend (`claude -p`, the Anthropic API, Codex, or local Ollama) when you give it one.

> 📖 **New here?** The [Usage Guide](./docs/guide.md) is the command-first how-to. For real builds on your own machine/LAN, see [`docs/deploy-docker.md`](./docs/deploy-docker.md).

---

## Quickstart — the Workshop (the main interface)

The primary way to use Foundry is the **Workshop**, a live browser UI. Start it
and open the page:

```sh
docker compose up            # then open  http://localhost:8765
# (or, without Docker:  uv run python -m webapp.server )
```

`docker compose up` builds the image (the UI is baked in) and serves the Workshop
at **http://localhost:8765** — it won't auto-open a browser, so navigate there
yourself. It binds `0.0.0.0`, so it's also reachable from your phone at
`http://<this-host>:8765`. Out of the box it runs the keyless `mock` backend; pick
a real backend (and `claude-code` adapter) in the new-build dialog to do real
builds. State is durable on volumes, so builds survive a restart.

### Or drive it from the CLI (optional)

If you prefer the terminal, the same factory runs headless:

```sh
make setup                                   # uv sync
uv run python -m foundry.cli --out ./repo "build a URL shortener"
```

`make` lists every target — `make run`, `make spec` (co-generate a spec),
`make token` (mint a Max-subscription token), `make up` (docker-compose).

`--out` produces a single **GitHub-ready repository**:
- the **generated source** at the root (the exact tree the gates verified), with a real **`README.md`**;
- a **`docs/`** folder — PRD, design + ADRs, interfaces, test plan/report, runbook, known-issues, upgrade & perf/cost docs — plus a **`BUILD-MANIFEST`** indexing every artifact, every gate verdict (and whether it's *attested*), provenance digests, and the proxy's *observed* model usage;
- the release: a single `open_pr` action (Assistant), a WORM **evidence pack** (Gated), or an attested **deploy** (Autonomous).

(`--docs DIR` still writes the doc set to a separate location if you want it elsewhere.)

---

## The core idea: agents propose, gates decide

Everything is built around one invariant:

> **The signed gates are the only thing that can release software.** Every other mechanism — model backends, human checkpoints, the agent journal, ideation, evolution — sits *on top* of the gates. It can *halt* a release; it can **never** turn a failing gate into a passing one.

Enforced structurally, not by convention:

- **Two-zone gate runner.** Untrusted agent code runs in *Zone A* (no signing key). *Zone B* re-derives the config, observes the run over a sealed channel, and **signs the verdict**. The reducer `merge_gates` admits **only attested verdicts** and is fail-closed by default — an unsigned or forged `GateResult` is dropped.
- **Single-egress model proxy.** Every model call goes through one mediating proxy, the sole credential holder. It records the *actually-served* model off the wire and resolves its lineage through a curated map — it never trusts what an adapter *claims* ("observed, not trusted"). Drift from the pinned model fails closed by default.
- **Content-addressed artifacts.** Bodies live behind `cas://sha256/…` handles; state carries only handles. Identical work coalesces; nothing is silently mutated.
- **Total causal order + cross-fork ledgers.** A `Seq` tuple gives a race-free order; `GlobalRecords` holds consumed nonces, the stable-FAIL latch, and the statistical alpha-look ledger — a failed gate stays failed and a nonce can't be replayed.
- **Trust root (Autonomous).** A hermetic SLSA-style builder + input-bound provenance + a fail-closed admission controller + a non-agent least-privilege deployer + a held-out-mutant discriminator together block the four canonical attacks: malicious test, forged results, forged verdict, fork divergence.

The adversarial suite *proves* these: forged verdicts are dropped, a stable FAIL never auto-clears, a vacuous test suite fails its gate, a checkpoint "approve" can't release a failing build, and an evolved change can't ride its predecessor's pass.

---

## Running real builds

Two independent choices turn the deterministic smoke harness into a real build: the **model backend** (the agents' reasoning) and the **coding adapter** (who writes the code).

**Backend** — `--backend` / `FOUNDRY_MODEL_BACKEND`; the proxy egress is the only thing that changes:

| Backend | Engine | Credential |
|---|---|---|
| `mock` *(default)* | deterministic scripted transport | none |
| `anthropic` | Claude API (SDK) | `ANTHROPIC_API_KEY` |
| `claude-cli` | `claude -p` (Max subscription) | `CLAUDE_CODE_OAUTH_TOKEN` (`make token`) |
| `codex` | `codex exec` | `OPENAI_API_KEY` |
| `ollama-gemma` | local Ollama server | none (`FOUNDRY_OLLAMA_URL`) |

**Coding adapter** — `--adapter` (CLI) or the per-build option in the Workshop:

- `stub` *(default)* — deterministic no-op; the smoke/demo substrate.
- `claude-code` — `claude -p` writes the real diff in a sandbox (needs the `claude` binary).
- `raw-react` — provider-agnostic; drives the chosen backend through the proxy to author code.

**Models.** Each backend pins a model id per reasoning tier (T1 deep / T2 mid / T3 cheap). Pin the exact ids your egress serves with `FOUNDRY_MODEL_T{1,2,3}` — the fail-closed drift policy rejects a served model that doesn't resolve to the pinned one (or set `FOUNDRY_DRIFT_POLICY=advisory` to record drift instead of enforcing it). Defaults and the full contract: [`docs/deploy-docker.md`](./docs/deploy-docker.md).

```sh
# A real build with claude -p as both the reasoning backend and the coder:
make token                                   # one-time: mint CLAUDE_CODE_OAUTH_TOKEN
FOUNDRY_MODEL_BACKEND=claude-cli \
  uv run python -m foundry.cli --backend claude-cli --adapter claude-code \
  --out ./out "build a tiny URL shortener"
```

> On the `mock` backend the run is a deterministic **smoke harness** (synthetic artifacts, guaranteed pass). A real backend resolves the agents' *real* authored designs/criteria/diff off the content store and runs them through the same gates. A real backend with the `stub` adapter writes no code and fail-closes honestly — real builds need a real coding adapter.

---

## The Workshop — a live browser UI

```sh
docker compose up      # http://localhost:8765  (binds 0.0.0.0 → reachable on the LAN)
```

A stdlib HTTP + Server-Sent-Events server (no extra deps) renders each build as a live "software factory" game: real stage boundaries, the agents at work, checkpoint pauses you approve in-browser. It is **durable and multi-device** — state survives a server restart, every connected device sees the same live view, and the first checkpoint decision wins. On a trusted home LAN run it over plain HTTP; set `FOUNDRY_WORKSHOP_TOKEN` if others share the network (a same-origin/CSRF guard always applies). See [`docs/deploy-docker.md`](./docs/deploy-docker.md).

---

## More capabilities — each runnable today

**Deployment-trust modes** (`--mode`): `assistant` (default; emits a PR-equivalent action, reviewer advisory) → `gated` (full gate set + human Design/Release sign-off + WORM evidence pack) → `autonomous` (builds, attests, deploys behind the full trust root). A product graduates only when the evidence justifies it.

**Domain packs** (`--packs regulated,ml_data,frontend`): compose extra requirements, strictest-wins, and can **veto an unsafe mode** — a regulated build can't run autonomous, and the CLI refuses fail-closed rather than silently downgrading.

**Spec ingestion & ideation**: bring a design doc (`--spec ./design.md`) or co-generate one first (`python -m foundry.ideation`). Advisory front-of-funnel — it shapes the spec, never overrides the spec gate.

**Human checkpoints** (`--checkpoints [product,design,…]`): pause at stage boundaries (resumable), inspect that stage's artifacts **and the agents' work-journal**, resume on a decision. An approve only lets the run reach the gates.

**Agent work-journal**: each agent records a strictly-advisory rationale entry; at a checkpoint you can "interview" an agent — re-instantiate its role + journal and ask *"why this design?"* Observability, never a trust input.

**Evolution** (`evolve ./v1 "add rate limiting" --apply ./changes`): a change re-enters the **same factory** as a maintained change — re-verifies only the touched scope (reusing prior attested verdicts), re-gates and re-admits fail-closed, chains provenance to the prior version, and carries the prior oracles forward as regression. Not trusted just because v1 passed.

**Durable state** (`FOUNDRY_STATE_DIR=/data`): one env var makes the checkpointer, outbox + WORM audit, the trust ledgers, and the CAS file/volume-backed — a single-host deployment survives a restart. Unset, it's the in-memory default (byte-identical).

---

## Deployment

| Tier | How | What you get |
|---|---|---|
| **Dev / smoke** | `uv run python -m foundry.cli …` | deterministic, no network, in-memory state |
| **Single host (LAN)** | **docker-compose** ([`docs/deploy-docker.md`](./docs/deploy-docker.md)) + `FOUNDRY_STATE_DIR` | durable filesystem CAS + file-backed checkpoints/ledgers; the Workshop UI on `0.0.0.0:8765` over plain HTTP; real backends + an optional token |

Built to run on a home LAN — a Raspberry Pi or a spare box — over plain HTTP, for a single user: `docker compose up` and open it from any device on the network.

---

## Repository map

```text
foundry/
├── schemas/      frozen pydantic records (artifacts · gates · governance · docs · …)
├── state/        FactoryState, None-safe reducers, Seq total order, tree_sha, GlobalRecords
├── graph/        the StateGraph: supervisor · role subgraphs · gate-wiring · factory_graph
├── agents/       role agents (product · architecture · implementation · quality · security) + journal
├── gates/        two-zone runner · attestation/signing · Spec/Build/Test/Security/Audit gates · discriminator
├── proxy/        single-egress model proxy · lineage map · swappable backends (SDK · claude-cli · codex · ollama)
├── trust_root/   hermetic builder · input-bound provenance · admission controller · deployer (Autonomous)
├── governance/   sign-off · evidence pack · verdict classes · mode/tier policy
├── domains/      DomainPackContract · registry · regulated / ml_data / frontend packs
├── durability/   content-addressed ArtifactStore (+ filesystem backend) · checkpointer · outbox · WORM audit
├── execution/    WorkerAdapter contract (stub · claude-code · raw-react) · Sandbox · work-pool seam
├── lifecycle/    maintenance · evolve · feedback loop · steering · provenance · modes
├── evaluation/   pre-registered A/B · power/pilot · role ablation · scorecard
└── ideation/     interactive spec co-generation front-end
webapp/           the Workshop: stdlib HTTP + SSE server, durable catalog, browser game UI
```

**Design docs** (authoritative spec): [`PLAN.md`](./PLAN.md) · [`architecture.md`](./docs/architecture.md) · [`agents.md`](./docs/agents.md) · [`governance.md`](./docs/governance.md) · [`domains.md`](./docs/domains.md) · [`lifecycle.md`](./docs/lifecycle.md) · [`evaluation.md`](./docs/evaluation.md) · [`operations.md`](./docs/operations.md) · [`roadmap.md`](./docs/roadmap.md).

---

## Development

```sh
make check                       # lint + typecheck + full suite (the CI gate)
uv run pytest -q -n auto         # full suite (~1,890 passing) in parallel
uv run ruff check foundry tests  # lint
uv run mypy foundry              # type-check (strict on foundry/)
```

Quality bars are enforced as gates: ruff clean, mypy clean, suite green, mutation testing on the trust-critical reducer (`merge_gates`). There is **no hosted CI by design** — the gate chain is local.

---

## Honesty: what's real, what's dev-grade

Foundry's *logic* — the gates, the trust model, fail-closed admission, the statistical decision machinery — is real, adversarially tested, and load-bearing. It runs **hands-off (autonomous)** on a home LAN: from a one-line request it builds, attests, and releases behind the real gates + the admission trust root, with no human in the loop unless you add a checkpoint.

The infrastructure underneath is deliberately **dev-grade** and labeled as such:

- It runs on a **trusted LAN over plain HTTP** (single user; optional shared token).
- The crypto root signs with **HMAC** by default (Ed25519 is wired for the asymmetric root); state is **sqlite + a filesystem CAS** on volumes, durable across restarts.
- The scanners (OPA/Semgrep/Syft) are **dev-grade seams**, and the coding adapter (`stub` default; `claude-code` / `raw-react` real) still needs prompt/fix-loop tuning against your live backend ([`docs/real-builds-scope.md`](./docs/real-builds-scope.md)).

The point is that the *trust logic* is real and enforced even on this modest substrate — nothing dev-grade is hidden; it's labeled in code and here.

---

## License & liability

See [`docs/lifecycle.md` §5](./docs/lifecycle.md) for the licensing & liability posture of AI-generated code.
