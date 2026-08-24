# Pipeline Token & Latency Optimization Plan

Status: **proposal** — nothing here is implemented. Each item lists its own
verification and blast radius; the high-blast-radius items are marked for Plan
Mode + a second-opinion pass before any code lands.

## 1. Problem

Runs burn through token/usage limits and are slow. This is not a LangChain
`ChatModel` problem — there is no `ChatAnthropic`/`bind_tools` anywhere.
LangGraph only orchestrates; every model call funnels through one hand-rolled
egress (`ModelProviderProxy.complete/judge`). The proxy itself is sound (single
credential, bounded retries, prompt-cache breakpoint, per-call usage metering).
The cost lives in **what the callers feed it** and **how many serial calls a run
makes**.

## 2. Diagnosis (what we found)

Two independent problems compound:

**Token burn**
- The `proxy-agent` coding adapter re-sends a **growing transcript** every turn
  (assistant turns + full `OBSERVATION:` file dumps), up to `turn_budget=8`
  turns/task, at **T1/Opus** with **adaptive thinking on** — quadratic, on the
  highest-volume path. (`foundry/execution/adapters/proxy_agent.py:127-152`,
  tier/purpose hardcoded at `foundry/cli.py:231` and `:258`.)
- Flat `max_tokens=16000` on **every** call regardless of purpose.
  (`foundry/proxy/model_provider_proxy.py:122`, applied `:959`.)
- Prompt caching covers only the system SOP and has a 5-min TTL; rate-limit
  waits (up to 6h, `model_provider_proxy.py:676`) go cold, so "cached" SOPs
  re-bill at full input price after a wait. Non-SDK transports (Ollama/OpenAI
  HTTP) drop `cache_control` entirely.

**Latency / structure**
- The spine is **almost entirely serial**: ~18-20 sequential model calls (several
  T1) on the happy path before a single gate PASS. Each subgraph is one blocking
  supervisor node.
- The three implementation lanes (frontend/backend/data) run **serially** as a
  conditional chain, each waiting on the coding substrate — the heaviest stage —
  even though lanes own disjoint files.
  (`foundry/graph/subgraphs/implementation.py:160-186`.)
- Fix loops re-run whole layers up to `max_fix_attempts` (default 3, a single
  **global** budget never reset in-graph). One build/test fix re-runs up to 6
  engineer nodes + the coding substrate again.
- **Redundant work:** `reviewer_critic` runs twice (quality **and** assurance,
  same factory) reviewing the same integrated diff; `sdet` E2E + the assurance
  E2E gate both exercise E2E.
- **Dead outputs:** `stakeholder_proxy`'s `IntentBrief` and `eng_program_mgr`'s
  `SprintPlan` appear to be consumed by no downstream agent or gate; the
  Principal Architect doesn't even read the PRD prose body — only
  `AcceptanceCriterion[]` and `required_capabilities` flow forward from the
  5-node product layer. (Verify §6 before cutting.)
- **The reviewer reviews blind:** `reviewer_critic` is sent only the subject
  *URI*, no diff body (`foundry/agents/quality/reviewer_critic.py:166`), to a
  model with no filesystem — so its verdict is uninformed, and a spurious
  `request-changes` drives the expensive fix loop.

## 3. Proposed changes

Grouped into four workstreams. Impact = expected token/latency reduction;
Blast = reversibility/policy/trust risk.

### Workstream A — Token cost (low blast, do first)

| # | Change | Where | Impact | Blast |
|---|--------|-------|--------|-------|
| A1 | Stop hardcoding the coding adapter at `T1` + `purpose="ideation"`. Route most tasks to T2/T3; reserve T1 for the last fix attempt. Drop `ideation` (kills adaptive-thinking tokens on the highest-volume path). | `cli.py:229-265` | High | Low |
| A2 | Cap the `proxy-agent` transcript: don't re-send full `OBSERVATION` file bodies every turn — keep the latest N, summarize/elide older ones (or prefer the `claude-code` adapter, see B1, which moots this). Consider lowering `turn_budget` from 8. | `proxy_agent.py:127-152`; `junior_engineer.py:98` | High | Low |
| A3 | Per-**purpose** `max_tokens` instead of a flat 16k. Verdicts/structured JSON ~1-2k; decomposition ~2-4k; only free-form ideation needs a large ceiling. | `model_provider_proxy.py:122,959` (add a purpose→cap map in `_build_request`) | Medium | Low |
| A4 | Lower `max_fix_attempts` default from the CLI wrapper (5) toward the graph default (3), and wire the existing `cost_cap_microusd` into an early stop so a run halts on spend, not just on attempt count. | `cli.py:1288/2187`; `junior_engineer.py` pool path already has the ledger | Medium | Medium |

### Workstream B — Claude Code CLI integration (medium blast)

Context: the `claude-code` adapter is already a long-lived per-task process that
runs its own internal agentic turns with native caching — the efficient shape.
The `proxy-agent` adapter re-implements the loop externally (the A2 hog). See §5
for why a persistent session **per reasoning agent** is explicitly rejected.

| # | Change | Where | Impact | Blast |
|---|--------|-------|--------|-------|
| B1 | Default the coding substrate to `claude-code` (per-task process, native caching) over `proxy-agent`. Captures ~all of "long-lived CLI" for coding with no protocol work. Keep `proxy-agent` only where the mediating-proxy credential locus is a hard requirement. | `cli.py` adapter selection | High | Medium (trust: `claude-code` moves the credential into the sandbox cage — already a documented trade) |
| B2 | **Cross-fix session continuity** (the bidirectional stream-json idea, applied where it pays): on a fix attempt, `--resume <session_id>` the prior coding session instead of re-priming the whole task. The result envelope already exposes `session_id` (`cli_transports.py:366`). Directly attacks the fix-loop multiplier. | new adapter capability + fix-loop wiring | High | **High** — conflicts with destroy-per-task / reset-to-base idempotency; needs the worktree + `session_id` kept alive across the loop and stored durably for graph resume. Design doc + second opinion before building. |

### Workstream C — Context exposure per agent (low–medium blast)

We do **not** summarize context anywhere; exposure is limited only by ad-hoc
per-node truncation caps (spec 24k, span 4k, judge-evidence 800 chars). Two
miscalibrations point in opposite directions.

| # | Change | Where | Impact | Blast |
|---|--------|-------|--------|-------|
| C1 | **Give the reviewer the actual diff.** Feed the changed hunks (scoped, capped) into `reviewer_critic`'s message instead of a bare URI. Fixes uninformed verdicts that drive the fix loop. This is *more* context, not less. | `reviewer_critic.py:158-177`; reuse the `_retrieved_spans` resolve pattern from `junior_engineer.py:253` | High (indirect: fewer bogus fix loops) | Low–Medium (correctness of a gate input — security-review the change) |
| C2 | Generalize `_ai_judge_evidence` (`factory_graph.py:404`) into a per-role **context contract**: an explicit "what this agent must see, capped, and nothing else" digest, replacing scattered magic-number caps. | new helper; adopt per node incrementally | Medium | Low |
| C3 | If `proxy-agent` is kept (B1 not universal): summarize/prune stale observations rather than re-sending file bodies (overlaps A2). | `proxy_agent.py` | Medium | Low |

### Workstream D — Pipeline redundancy & parallelism (medium–high blast)

| # | Change | Where | Impact | Blast |
|---|--------|-------|--------|-------|
| D1 | **Parallelize the three implementation lanes.** Convert the lead chain and the engineer chain from a conditional serial chain to concurrent fan-out (lanes own disjoint files; reducers are keyed unions built for concurrent writes). Biggest wall-clock win. | `implementation.py:127-186` | High (latency) | **High** — touches state reducers, determinism, resume, and the serial-overlay "shared evolving tree." Plan Mode + second opinion. |
| D2 | **Drop or scope the second `reviewer_critic`** (assurance). Either remove it, or scope it to reviewing *security-agent findings* the quality reviewer didn't see, or gate it behind "tree changed since the quality review." | `assurance.py:356,481,489`; check `assurance_gate` doesn't require a fresh `review` verdict first | Medium | Medium (verify the assurance gate's inputs — security-review) |
| D3 | **Cut the dead-output product nodes.** Pending the §6 consumer check: remove `stakeholder_proxy` (T2) and `eng_program_mgr` (T2), or downgrade to T3/no-LLM if only the UI/interview layer reads them. Removes 2 serial T2 calls/run. | `product.py:161-162,177` | Medium | Medium (verify no webapp/interview consumer) |
| D4 | **Fold `criteria_completeness_critic` into the PM prompt.** The producer/critic pair over one artifact costs a T1 call + up to 2 PM revision round-trips; the PM SOP can carry the completeness checklist directly. Keep the critic only if an independent second pass is genuinely required. | `product.py:163,172-176`; `product_manager.py` SOP | Medium | Medium |
| D5 | **De-dupe E2E.** `sdet` authors an E2EReport for the build/test boundary and the assurance E2E gate re-runs E2E at HIGH tier — run E2E once and share the attested result. | `sdet.py`; `assurance_wiring.py:138-139` | Low–Medium | Medium |
| D6 | Make `docs` (T2, every PASS) cheaper (T3) or async — it's on the release critical path but not release-gating. | `factory_graph.py:1158-1166` | Low | Low |

## 4. Sequencing

1. **Now (low blast, high return):** A1, A2, A3, C1. Independent, individually
   shippable, each with a narrow test. A1+A2 are most of the token win; C1 is
   most of the *wasted-loop* win.
2. **Next:** A4, B1, D2, D3, D4, D6 — each a self-contained change behind its own
   verification. B1 unlocks retiring the A2 hog entirely.
3. **Design-first (high blast, needs Plan Mode + second opinion):** D1 (lane
   parallelism) and B2 (cross-fix session resume). Write a short design doc for
   each — state-reducer/determinism/resume implications for D1; worktree +
   session lifecycle for B2 — before touching code.

## 5. Explicitly rejected: persistent session per reasoning agent

Do **not** convert the reasoning agents (PM, architect, reviewer, judges) to
long-lived per-agent sessions. They are stateless single-shot by design; a
persistent session would **accumulate** context they deliberately avoid
(increasing tokens and corrupting judgment independence), break per-call
lineage/drift observation and temperature-0 determinism, and fall outside the
LangGraph checkpoint so a resume couldn't restore it. Their token cost is already
addressed by SOP caching + A3, not by session reuse. The long-lived-CLI idea
belongs to the coding substrate (B1/B2) only.

## 6. Open questions / verify-before-cut

- **Dead-output confirmation (blocks D3):** grep the `webapp/` and any
  interview/event-log/office code for consumers of `IntentBrief` and
  `SprintPlan` before deleting the nodes. If only the narrative UI reads them,
  downgrade rather than remove.
- **Assurance reviewer inputs (blocks D2):** confirm `run_assurance_gates`
  doesn't require a `review` verdict minted at the assurance stage specifically.
- **Lane parallelism invariants (blocks D1):** the serial-overlay "shared
  evolving tree" assumes ordered predecessor slices; confirm disjoint ownership
  makes cross-lane visibility unnecessary, and that keyed-union reducers +
  `rt.seq` stay deterministic under concurrent lane writes and a mid-run resume.
- **Trust sign-off for B1/C1/D2:** these touch the coding-credential locus and a
  gate input — run `security-review` on each.

## 7. Expected outcome

- Token: A1+A2 remove the quadratic Opus/thinking coding path (the dominant
  sink); A3 trims output ceilings everywhere; C1+D2+D4 remove the bogus and
  duplicated review spend that drives fix loops.
- Latency: D1 collapses the serial lane chain (the biggest wall-clock item); D3/D4
  shorten the serial product chain; A4 bounds worst-case rework.
- No change weakens a gate: verdicts still come only from the attested
  `TwoZoneGateRunner`; the cuts target advisory/redundant model calls, not the
  trust boundaries.
