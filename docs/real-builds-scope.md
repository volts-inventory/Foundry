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
| **Coding substrate selection** | ✅ selectable (`--adapter` / API `adapter`) | `cli.py` `make_coding_adapter`; default `stub` |
| **Artifact resolver** | ✅ real off the CAS for real backends | `cli.py` `_materialization_seam`; synthetic only for `mock` |
| **Diff/code resolver** | ✅ real `store.resolve_blob` for real backends | same; `_DIFF_BODY_URI` only on the mock smoke |
| **Reviewer/admission binding** | ✅ dynamic for real backends | `cli.py` `_judgment_anchors` (unpinned ⇒ reviewer binds the real diff + real tree) |

So: **agents real, adapter real, gates real — and for a real backend the CLI now lets
them run** (the `mock` smoke is byte-identical via the synthetic doubles).

---

## 2. The changes (file-level)

### 2a. Select a real coding adapter — `foundry/cli.py`  ✅ DONE
- `make_coding_adapter(name, *, proxy, thread_id, timeout_s)` constructs the chosen
  substrate from `ADAPTERS = ("stub", "claude-code", "raw-react")`:
  - `stub` → `StubAdapter()` (the default — keeps the deterministic suite + zero-config
    demo intact; `None` also resolves here for back-compat).
  - `claude-code` → `ClaudeCodeAdapter(run_id=thread_id, …)` **only if `claude` is on
    PATH** (`adapter.available()`), else it fails closed with a clear message.
  - `raw-react` → `RawReactAdapter` over the single-egress proxy (any backend).
- Selectable two ways (this is the wired-in part):
  - **CLI:** `foundry run --adapter {stub|claude-code|raw-react} "<request>"`.
  - **API:** `POST /api/projects {"adapter": "claude-code", …}` (UI dropdown +
    `FOUNDRY_CODING_ADAPTER` default); persisted on the build row and replayed on restart.
- `run(..., adapter=…)` threads it into `AgentRuntime`; an explicit `WorkerAdapter`
  instance still passes through (tests inject one). **Risk:** low — isolated; the default
  (`stub`) path is unchanged, so the deterministic smoke build is untouched.

  NOTE: adapter selection is independent of `backend`. Picking `claude-code`/`raw-react`
  makes the agents author code via that substrate; **for a real backend `run()` now
  resolves off the real CAS** (2b) so those authored bodies flow through to the gates.

### 2b. Use the real resolver for real backends — `foundry/cli.py`  ✅ DONE
- `_materialization_seam(real_artifacts, store, diff_body)`: `backend != "mock"` resolves
  off the **real CAS** (`store.resolve` / `store.resolve_blob`); `mock` keeps the
  synthetic doubles (the smoke build's guaranteed PASS). Default `stub` adapter on a real
  backend authors no diff ⇒ the reviewer fail-closes honestly (a real build needs a real
  coding adapter).
- **Validated deterministically** (`tests/test_real_materialization.py`): the seam picks
  the real store for real backends and the synthetic doubles (returning the canned body)
  for mock. The live end-to-end (real bodies actually passing the gates) is the Phase-2
  integration pass — it needs your `claude -p` and may surface 1-3 hidden smoke
  assumptions in the downstream agents (senior decomposition, QA oracles).

### 2c. Make the trust-binding values dynamic — `foundry/cli.py`  ✅ DONE
- **Key finding from the trace:** the load-bearing trust binds are ALREADY real. Admission's
  expected per-gate trees (`factory_graph._autonomous_admission_inputs`), the BuildOutput
  provenance, the evidence pack (`_attested_run_against`), and the stable-FAIL latch all
  bind the **real verdict-carried `run_against`** (the `tree_sha` `materialize_workdir`
  computes), NEVER the config label. The config `subject_artifact_uri`/`state_sha` are
  pre-build LABELS the *reviewer's advisory verdict* + the gated sign-off bind to — and
  the real integrated values literally don't exist when the config is built (the diff is
  authored mid-run), so they can't be static.
- `_judgment_anchors(real_artifacts)`: `mock` pins the synthetic subject/label (byte-
  identical smoke); a real backend pins **nothing**, so the reviewer binds the **real**
  newest FeatureDiff uri (its existing state fallback) + the **real** integrated tree sha
  (`reviewer_critic._attested_tree_sha`, read off the attested gate verdicts). The gated
  sign-off anchors derive consistently because both the supervisor boundary and the emit
  re-check read the (now unpinned) config — `release_allowed` checks equality, so "" == ""
  still admits a legitimate release.
- **Risk retired deterministically:** the full H2/H3/F1/M1 trust suite stays green
  (admission trusts the real verdict trees, not the unpinned label), and the `mock` path
  is byte-identical (config-pin precedence). Live trust behaviour on real output is part
  of the Phase-2 pass.

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

1. **Phase 1 — code real, gates real (small, mostly safe): ✅ DONE.** 2a + 2b + 2c behind
   `backend != "mock"`, trust suite green, mock byte-identical. Deterministic tests
   (`tests/test_adapter_selection.py`, `tests/test_real_materialization.py`).
   → Real code is authored and verified; bindings are real. The remaining gap is purely
   live-tuning (Phase 2) — the wiring no longer bypasses the real path.
2. **Phase 2 — integration pass (the unknowns):** run real `claude -p` builds, fix the
   1-3 hidden smoke assumptions, tune the fix-loop/prompts until builds reliably pass.
   → Requires your `claude -p`.
3. **Phase 3 — LAN packaging:** image with CLI backends + headless OAuth + the TLS
   reverse-proxy guide; a "first real build" runbook.

**Rough effort:** Phase 1 ≈ half a day + tests; Phase 2 is the open-ended one (depends
on how much the real model fights the gates) — plan a few iterations; Phase 3 ≈ a couple
hours. Phase 1 is low-risk and I can do it + validate deterministically now; Phases 2-3
need your backend in the loop.
