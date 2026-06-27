# Scope: real `claude -p` builds, end-to-end on a LAN

**Goal.** Replace the simulated build with a *real* one: you type a request into the
Workshop on your LAN and get real, AI-authored, gate-verified software out — using
`claude -p` (your Max subscription) for both the agents' reasoning and the code itself.

**Good news from the investigation:** most of the machinery is already real and the
missing pieces are concentrated. The two-zone gates, signing, durable state, the
Workshop UI/LAN serving, the `claude-cli` model backend, and — crucially — the
`ClaudeCodeAdapter` (real `claude -p` coding substrate) and the **gates that actually
run the generated code** (`materialize_workdir` → `BuildGate` + real-pytest `TestGate`)
all exist and work. What's missing is that the CLI/webapp `run()` path is a
*deterministic smoke harness* that bypasses them. This scope is about removing that
bypass for real backends, carefully.

---

## 1. What's already real vs. what's simulated

| Piece | State | Evidence |
|---|---|---|
| Workshop UI on LAN (auth, durable, TLS-doc) | ✅ done | this branch |
| `claude-cli` model backend (agent reasoning via `claude -p`) | ✅ wired | `cli_transports.claude_cli_backend` |
| Agents author **real** artifact bodies to the CAS | ✅ real | `principal_architect` → `proxy.complete(schema=…)` → `emit_artifact(store=rt.cas)` |
| `ClaudeCodeAdapter` (real code via `claude -p` in a sandbox) | ✅ built | `execution/adapters/claude_code.py`; junior calls `rt.adapter.run` |
| Build/Test gates **execute** the code (real pytest) | ✅ real | `gate_wiring.run_build_test_gates` → `materialize_workdir` + `TestGate` |
| **Coding substrate selection** | ❌ hardcoded `StubAdapter` | `cli.py:955` |
| **Artifact resolver** | ❌ `_synthetic_resolve` overrides real bodies | `cli.py:959`, `_synthetic_resolve` |
| **Diff/code resolver** | ❌ `_synthetic_resolve_blob` returns a canned body | `cli.py:960`, `_DIFF_BODY_URI` |
| **Reviewer/admission binding** | ❌ hardcoded `subject_artifact_uri=_DIFF_BODY_URI`, `state_sha="integrated-sha"` | `cli.py:966-967` |

So: **agents real, adapter real, gates real — the CLI just doesn't let them run.**

---

## 2. The changes (file-level)

### 2a. Select a real coding adapter for real backends — `foundry/cli.py`
- Add `_coding_adapter(backend, proxy, thread_id) -> WorkerAdapter`:
  - `mock` → `StubAdapter()` (keep the deterministic suite + zero-config demo intact).
  - `claude-cli` → `ClaudeCodeAdapter(run_id=thread_id, timeout_s=…)` **if `claude` is on
    PATH** (`adapter.available()`), else fail closed with a clear message.
  - other real backends → `RawReactAdapter` over the proxy (provider-agnostic fallback).
- `cli.py:955`: `adapter=_coding_adapter(backend, proxy, thread_id)`.
- **Risk:** low. Isolated; mock path unchanged.

### 2b. Use the real resolver for real backends — `foundry/cli.py`
- When `backend != "mock"` and the caller didn't pass one, use the **real CAS resolver**
  (`store.resolve` / `store.resolve_blob`) instead of `_synthetic_resolve(_blob)`.
- Keep `_synthetic_resolve` for `mock` (it guarantees the smoke build passes).
- **Risk:** medium. The real designs/criteria/diff now flow through; downstream agents
  (senior decomposition, QA oracles) must consume *real* shapes. Most consume via the
  resolver already, so this is mostly removing the override — but it surfaces any place
  that quietly relied on the synthetic "faithful" bodies. Needs an integration pass.

### 2c. Make the trust-binding values dynamic — `foundry/cli.py` (+ maybe gate_wiring)
- `subject_artifact_uri` must be the **real** integrated FeatureDiff uri, and `state_sha`
  the **real** integrated tree sha (the one `materialize_workdir` computes), not the
  hardcoded constants — so the reviewer's judgment binding and admission bind to the
  artifact that was actually built.
- **Risk:** medium-high (trust path). This touches what the reviewer signs and what
  admission re-verifies. Must keep the H2/H3/F1 properties intact (regression-test).

### 2d. Wire it through the webapp — `webapp/server.py`
- `_run_factory` already calls `run(backend=p.backend, …)`; once `run()` chooses the
  adapter/resolver by backend, the UI's "claude-cli" selection Just Works. Verify the
  per-build cost cap + timeout are surfaced (they are: `cost_cap_microusd`,
  `max_fix_attempts`). Likely **no webapp code change** beyond confirming.

### 2e. Container/LAN packaging — `Dockerfile` / `docker-compose.yml` / docs
- Build the image with the CLIs: `docker build --build-arg INSTALL_CLI_BACKENDS=true`.
- **Auth (headless, the load-bearing part):** `claude -p` on a Max subscription needs a
  subscription OAuth token minted once on a workstation: `claude setup-token` →
  `CLAUDE_CODE_OAUTH_TOKEN` env (a `.env` secret; **do NOT** also set `ANTHROPIC_API_KEY`
  — it outranks the token and bills the pay-per-token API). The compose already documents
  this; set `FOUNDRY_MODEL_BACKEND=claude-cli`.
- The adapter shells `claude -p` **inside the per-task sandbox workdir** — confirm the
  sandbox passes the OAuth env through (the transport inherits `os.environ`; verify the
  Sandbox does too) and that the worktree is writable by the non-root `foundry` user.

---

## 3. Risks & unknowns (the honest part)

1. **Real LLM output won't always satisfy the gates or schemas.** A real `claude -p`
   design/criteria/diff can fail the Spec/Build/Test gates — that's *correct* (a real
   build can fail), but it means the fix-loop (`max_fix_attempts`) and prompts will need
   **tuning** against the real model before builds reliably go green. This is iterative
   and needs your real `claude -p`.
2. **Hidden smoke assumptions.** The synthetic path may be load-bearing in spots beyond
   the four above (e.g. the senior's task decomposition expecting a specific design
   shape). Expect to find 1-3 more during the integration pass.
3. **Trust-path constants (2c)** are the riskiest edit — they feed the signed reviewer
   binding + admission. The H2/H3/F1 tests must stay green.
4. **Performance & cost.** A real build = many `claude -p` calls (each agent + each task).
   Minutes per build, real subscription usage. Cost cap + per-call timeout (already
   added) are essential; consider `FOUNDRY_MAX_SLOTS` for parallelism.
5. **Sandbox isolation.** `claude -p` runs in a worktree and is told not to escape it,
   but in the single-process dev sandbox that's advisory. For untrusted requests on a
   shared LAN, the real container/gVisor sandbox (the `isolated` profile, not yet built)
   matters; for your own use on your own LAN it's acceptable.

---

## 4. Validation plan

- **Deterministic (CI, no LLM):** unit-test `_coding_adapter` selection per backend;
  test `RawReactAdapter`/`ClaudeCodeAdapter` turn a model response/`git diff` into a real
  FeatureDiff; test that `run(backend!="mock")` uses the real resolver; keep the full
  H2/H3/F1/M1 trust suite green.
- **Live (needs your `claude -p`):** a real end-to-end build of a tiny service, asserting
  real code is written, the Test gate runs the real suite, and the output tree is a
  working project. `claude` is on PATH in this environment, so an initial smoke of the
  adapter is possible here, but a representative build must be run against your Max auth.

---

## 5. Sequencing (suggested)

1. **Phase 1 — code real, gates real (small, mostly safe):** 2a + 2b + 2c behind
   `backend != "mock"`, with the trust suite kept green. Deterministic tests.
   → Real code is authored and verified; bindings are real.
2. **Phase 2 — integration pass (the unknowns):** run real `claude -p` builds, fix the
   1-3 hidden smoke assumptions, tune the fix-loop/prompts until builds reliably pass.
   → Requires your `claude -p`.
3. **Phase 3 — LAN packaging:** image with CLI backends + headless OAuth + the TLS
   reverse-proxy guide; a "first real build" runbook.

**Rough effort:** Phase 1 ≈ half a day + tests; Phase 2 is the open-ended one (depends
on how much the real model fights the gates) — plan a few iterations; Phase 3 ≈ a couple
hours. Phase 1 is low-risk and I can do it + validate deterministically now; Phases 2-3
need your backend in the loop.
