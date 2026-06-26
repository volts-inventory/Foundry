# Foundry — Codebase Review & Critic Pass (2026-06)

A subsystem-by-subsystem critic review for **dead code, bad practices, structural
issues, and efficiency**. Six reviewers fanned out across the tree; every finding
below was re-verified (grep across `foundry/` **and** `tests/`, and for the
trust-critical ones, an independent trace of the code path). Vulture/lint
candidates that turned out to be test-exercised or pydantic fields were dropped.

**Baseline health is excellent:** `ruff` clean, `mypy` clean (179 files), 1748
tests passing, 0 bare `except:`, 7 TODOs. No SQL injection, no `shell=True`, no
path-traversal vectors, statistically-sound eval machinery. The findings below are
what the tools *cannot* see.

Severity: **high** = correctness/security invariant silently broken · **medium** =
real risk under a documented future config · **low** = cleanup / hygiene.

---

## Remediation status (updated)

| Finding | Status | Note |
|---|---|---|
| **H1** retry-supersession | ✅ docs/test made honest | Documented design decision (IM3), not a bug — corrected the false docstring + false-confidence test; true supersession-via-fresh-id is a deliberate follow-up. |
| **H2** admission tree-namespace | ✅ fixed | Check #4b added; regression-tested (fails without fix). |
| **H3** nonce never consumed | 📝 documented gap (blocker found) | Specified-but-unwired trust-core seam. **Verified blocker:** all three judgment verdicts share one per-thread nonce, so naive consumption would reject the 2nd/3rd and break gated/autonomous runs — needs per-(node,state_sha) nonce *issuance* first. Defense-in-depth behind the signed binding; recommended as a focused, separately-validated feature. |
| **H4** CLI subprocess timeout | ✅ fixed | 300s default, overridable; tested. |
| **H5** cli.py argparse | ✅ fixed | De-duplicated the 13 blocks via `_take_value`/`_take_int` helpers — preserves every tested message + the `--checkpoints` heuristic (chosen over an argparse swap, which would change the tested error UX and greedily eat the request). |
| **M1** WORM/ledger concurrency | ✅ fixed | UNIQUE(prev_hash) + atomic consume_nonce. |
| **M2** CAS set canonicalization | ✅ fixed | content_identity sorts set fields; hash-seed-stable. |
| **M3** security `_common.py` | ✅ fixed | Extracted; supply_chain left as-is by design. |
| **M4** schemas/operations split | ✅ fixed | Split into operations/operability/trust_root.py; ~30 importers updated; true split, no facade. |
| **W1** SSE fan-out | ✅ fixed | Retained event log + per-subscriber cursors. |
| **W3** restart durability | ✅ fixed | Durable state default + resume. |
| **LOW** dead code / asserts / hmac | ✅ fixed | Sweep: deletions, hmac.compare_digest, assert→raise. |

Everything is fixed except **H3**, which has a verified blocker (shared per-thread
nonce) making it a trust-core *feature* — issue per-verdict nonces first, then add a
trusted judgment-commit step — rather than a safe sweep edit. The gap is documented
honestly in code (`reducers.py`) so its inertness isn't a silent surprise.

---

## HIGH

### H1 · Retry-supersession (L4) is broken — the `tasks` reducer's seq-tiebreak is inert for dicts
**`foundry/state/reducers.py:137`** · **`foundry/agents/implementation/junior_engineer.py:136-147`** · *bad-practice / correctness*

`make_union_by_key._merge` breaks ties with `getattr(prev, "seq", None)`, but the
`tasks` channel holds **plain dicts** (`senior_engineer.py:269` writes
`task.model_dump(mode="json")`; `junior` writes a done-marker dict carrying
`base_tree_sha`). `getattr(some_dict, "seq", None)` is **always `None`**, so the
seq comparison never fires and the **first-keyed entry wins** — the senior's
original Task (no `base_tree_sha`). The done-marker is dropped on merge.

Consequence: `_base_snapshot_of` can never read `base_tree_sha` off reduced state,
so a retried task resets to a fresh clone instead of its recorded base — diffs
**stack instead of supersede**, the exact failure mode the L4 comment claims to
prevent.

**The test gives false confidence.** `test_agents_implementation.py:564-580`
"proves" supersession works, but it feeds the node's *raw return* straight into
the next node, **bypassing `union_by_id` entirely**. In the real LangGraph
channel the reduce runs and the marker is lost.

*Verified:* live interpreter — `getattr({'id':'t1','seq':[0,1,2]}, 'seq')` → `None`;
`union_by_id([orig],[marker])` returns the orig with `base_tree_sha` absent.

**Fix:** make the tiebreak read `seq` from dicts too, e.g.
`_seq_of(x) = x.get("seq") if isinstance(x, dict) else getattr(x, "seq", None)`,
and mint the marker with a seq that outranks the original (today both serialize
the *same* seq). Add a test that asserts `base_tree_sha` is readable off
`state["tasks"]` *after* the reducer runs, not off raw node output.

### H2 · Admission's stable-FAIL check queries the wrong key namespace (trust-root fail-open)
**`foundry/trust_root/admission.py:243`** · *security*

The runner latches a stable FAIL under the **tree sha** —
`latch_stable_fail(run_against, …)` where `run_against` matches `_DIGEST_RE`
(bare hex). Admission's check #4 queries `records.admission_look(subject_digest)`,
and `subject_digest` is a `content_digest` over the pinned inputs (a *different*
value from `tree_sha`). The two live in **disjoint namespaces**, so a
tree-keyed stable-FAIL latch is **never found** by admission. The docstring
explicitly declares check #4 "is what holds a stable-FAIL'd digest down
regardless of any later PASS" — that invariant is silently false.

Cross-fork *digest disqualification* (`disqualify_digest(subject_digest, …)`) **is**
caught correctly — only the stable-FAIL half is inert. Routing
(`gate_wiring`/`assurance_wiring`) reads the latch under the correct tree key and
is the primary guard, so this is a **defense-in-depth gap** rather than an
exploit in the default flow — but it's a trust-root invariant that doesn't hold.

**The test masks it:** `test_phase4_exit.py:955` latches and admits with the
*same* `digest` value, conflating the two namespaces.

**Fix:** have `admit` additionally check stable-FAIL under `provenance.tree_sha`
(or, when a tree stably-FAILs, also `disqualify_digest` the resulting subject so
both namespaces converge). Fix the test to latch on the tree sha and admit on the
distinct subject digest.

### H3 · The single-use verdict nonce is never consumed in production
**`foundry/state/global_records.py:175`** (only writer) · *security*

`merge_gates` rejects a judgment write when
`v.nonce_consumed_by_other_verdict(g)` — which can only be true if
`consume_nonce` ever ran. Grep across `foundry/`: **zero** production callers
(only `tests/test_merge_gates_grounding.py:204`). `proxy.judge()` stamps the
binding nonce but never consumes it, so the ledger is always empty and the replay
check can never fire. The documented single-use invariant
(`global_records.py:6-8`, `attestation.py:32-35`) is not actually enforced.

Mitigation: the *bound* nonce still defeats signature forgery, so practical
exploitability is gated by signer-key custody — but a recorded verdict can be
replayed by content-identical re-emission without the ledger objecting.

**Fix:** consume the nonce transactionally at the trusted emit boundary
(`records.consume_nonce(nonce, g.content_identity())`) and treat a `False` return
as a rejected verdict. Likewise wire `latch_negative_verdict` on a recorded
request-changes verdict (same "test-only caller" gap).

### H4 · CLI model backends ship with no subprocess timeout — a hung worker hangs forever
**`foundry/proxy/cli_transports.py:621-667`** · *bad-practice*

`ClaudeCliTransport`/`CodexCliTransport` default `timeout_s=None`, and
`claude_cli_backend`/`codex_backend` construct them with no timeout and expose no
parameter to set one. So in every default-configured deployment the `claude`/
`codex` subprocess runs with `timeout=None`, the `result.timed_out` branch is
dead code, and the "hard wall-clock kill" the module + `Sandbox` docstrings
promise never applies. A hung CLI blocks the worker thread indefinitely.

**Fix:** add `timeout_s: float | None = 300.0` to both backends, surface it
through `build_backend`/`backend_from_env`, and pass it into the transport
constructors.

### H5 · `cli.py` hand-rolls argparse — 13 duplicated flag-parsing blocks
**`foundry/cli.py:1247-1410`** · *bad-practice / structure*

`main()` repeats the same `if "--flag" in args: i = args.index("--flag"); …;
args = args[:i] + args[i+2:]` idiom **13 times**, each re-implementing the
missing-value check and value validation. The `--checkpoints` block compensates
with a fragile heuristic that can mis-swallow a request token equal to a boundary
name. This is the root cause of `cli.py` being the largest file in the repo.

**Fix:** one `argparse.ArgumentParser` with subparsers for the `evolve` verb;
removes ~160 lines, the duplicated validation, and the `--checkpoints` ambiguity.
`FOUNDRY_*` env defaults map to `default=os.environ.get(...)`.

---

## MEDIUM

### M1 · WORM audit chain can fork under concurrency  ✅ FIXED
**`foundry/durability/outbox.py`** · *bad-practice*

> **Resolved** (`tests/test_outbox.py::test_concurrent_chain_fork_fails_closed`):
> added a `UNIQUE(prev_hash)` index to `worm_audit` so two appenders off the same
> head fail closed on the second INSERT instead of silently branching the chain —
> the portable guard that holds even where `BEGIN IMMEDIATE` doesn't (Postgres
> `READ COMMITTED`). Added as an *index* so existing DBs migrate, not just fresh
> ones. Also hardened `GlobalRecords.consume_nonce`: its SELECT-then-INSERT had a
> cross-connection TOCTOU (now that concurrent Workshop builds share one durable
> ledger), replaced with an atomic `INSERT OR IGNORE` + re-read so exactly one
> consumer can win a contested nonce
> (`tests/test_global_records_admission.py::test_consume_nonce_is_atomic_across_shared_connections`).
> Audited the other four ledger writers (`latch_stable_fail`, `disqualify_digest`,
> `try_first_alpha_look`, `record_alpha_look`) — all already atomic insert-if-absent
> backed by their PRIMARY KEYs (a race fails closed). Original finding below.

### M2 · `content_identity()` doesn't canonicalize sets → non-deterministic CAS digest (latent)
**`foundry/canonical.py` / `foundry/schemas/base.py`** · *bad-practice*

`canonical_bytes` sorts dict *keys* but not *list values*, and
`model_dump(mode="json")` serializes a `frozenset` field in iteration order.
Confirmed empirically: a `frozenset[str]` field yields three different orderings
under `PYTHONHASHSEED=1/2/3` → a hash-seed-dependent content digest, which would
break CAS addressing and the WORM/evidence chain. `GateResult` already hand-sorts
its `served_family` set (proof the author knows the hazard); the **base** method
does not. Latent today (only `GateResult` carries a set, and it's defended), but
the base contract is unsafe by default.

**Fix:** handle `set`/`frozenset` in `canonical_bytes` by emitting `sorted(o)`, so
every hashed model is stable regardless of whether a subclass remembers.

### M3 · Security agents copy-paste boilerplate with no `_common.py`
**`foundry/agents/security/{appsec_engineer,audit_compliance,supply_chain_reviewer}.py`** · *structure*

The `_run_against`/`_tree_refs` config-resolution helper and the
blocking-severity `_triage` formatting are duplicated across all three security
agents, even though `product/` and `quality/` already centralize exactly this in
a `_common.py` (per CR5). `security/` has no such module.

**Fix:** add `foundry/agents/security/_common.py` with shared `run_against` and
`blocking_triage` helpers, mirroring `product/_common.py`.

### M4 · `schemas/operations.py` is a 594-line grab-bag of 6 unrelated record families
**`foundry/schemas/operations.py`** · *structure*

Six distinct clusters behind `=====` banners: L6 ops artifacts, operator/SLO/alert
surface, capacity/fairness, HITL load, store-health/upgrade, and Phase-4
trust-root build/admission records. Clusters 2–5 map 1:1 to separate runtime
modules in `foundry/operations/`.

**Fix:** keep cluster 1 in `operations.py`; move 2–5 to `operability.py`; move the
trust-root cluster beside `security.py`.

---

## LOW (cleanup / hygiene)

**Dead code** (verified unreferenced in `foundry/` *and* `tests/`):
- `_DEFAULT_DEPLOY_TARGET`, `_RELEASED_ARTIFACT_KIND` — `graph/factory_graph.py:142,180` (latter is a stale alias of `FEATURE_DIFF_KIND`; its comment is misleading).
- `_PYTEST_GREEN`, `_NO_TESTS_COLLECTED` — `gates/discriminator.py:72,74`.
- `PolicyMonitor.deny_count` — `monitors/policy_monitor.py:61`.
- `Span.served_model`, `Span.requested_model` — `observability/telemetry.py:101-109` (sibling `.drift`/`.usage` *are* used).
- `REASON_FAIR_SHARE` — `operations/capacity.py:64`.
- `IdeationResult.final` — `ideation/loop.py:155`.
- `AdmissionController.admit(evidence_pack=…)` — `trust_root/admission.py:126`: param accepted, never read, never passed (likely an unfinished what-you-admit-is-what-you-see check).
- `_area_by_task_id` — `agents/implementation/junior_engineer.py:161`: kept alive only by its own test; the node builds the map inline. Either delete the helper+test or use it in the node as the single source of truth.

**Bad practice:**
- Content-integrity invariants enforced with bare `assert` (compiled out under `python -O`): `junior_engineer.py:310` (`mirrored == diff_uri`), `agents/_common.py:111` (`stored == uri`). Replace with explicit `raise RuntimeError`.
- `governance/sign_off.py:218` compares the server nonce with `==`, not `hmac.compare_digest` — inconsistent with `attestation.py:219`/`runner.py:193` (low exposure: the token is a deterministic binding, not a secret).
- `webapp/server.py:687` imports the private `foundry.cli._build_proxy`; build via the public `build_backend` path instead (it already does in `_chat_proxy`).
- `durability/persistence.py:142` opens `records_conn` with different `isolation_level`/`foreign_keys` posture than the checkpointer connection — align or document.

**Structure / duplication:**
- `_build_packet` vs `execution_task_for` (`junior_engineer.py:194,238`) build the same `TaskPacket` twice; the "byte-identical packet" invariant is maintained by hand. Extract one builder.
- `evaluation/scorecard.py:73` imports the private `_holm_bonferroni` across modules; the per-task-rate (`pilot.py:86` vs `ab_runner.py:205`) and variance helpers are copy-pasted. Promote shared stats to `evaluation/_stats.py`.
- `version: int = 1` + `supersedes: str | None` is duplicated across ~16 schema models; add a `VersionedRecord` mixin to `base.py` (mind pydantic field ordering vs `seq`).
- The two-zone runner's `seal_key` is unreachable for built-in gates (only custom red-team gates thread it into `observe`) — thread it through `gate.execute` or document it as cooperation-required.

**Efficiency** (small N today; structurally suboptimal):
- `graph/factory_graph.py:946` calls `pack_registry.compose(state)` twice in `autonomous_emit`; compute once and pass the policy.
- `durability/outbox.py:190` re-parses + re-hashes the stored payload to dedup; compare canonical text directly.
- `domains/registry.py:170` `selected.sort(key=order.index)` is O(n²); precompute a position map.

---

## Verified clean (negative results worth recording)
SQL in `webapp/store.py` (fully parameterized) · path-traversal in `webapp/server.py`
(guarded) · XSS in `game.js` (`textContent`/canvas) · eval statistics
(power/alpha/bootstrap/decision-table all sound) · HMAC comparisons in
`attestation`/`runner` (`compare_digest`) · atomic FS writes in `fs_artifact_store`
· strictest-wins veto composition in `domains/` · fail-closed `_RejectAll` default
· no mutable default args · no `shell=True` in any subprocess call.

---

## The Workshop UI & its backend wiring (`webapp/`)

The frontend is a Phaser canvas "factory floor" game (`static/game.js`, 1647 lines).
The backend is a stdlib `ThreadingHTTPServer` (`server.py`, 923 lines) that drives a
**real** `foundry.cli.run(...)` build in a per-project daemon thread and streams its
telemetry to the browser over **Server-Sent Events**. `events.py` is a clean, pure,
deterministic mapping from factory telemetry → game events (no I/O, unit-tested —
good separation). The wiring is sound in the common single-viewer case; the gaps are
in concurrency, exposure, and lifecycle.

### W1 · SSE fan-out is broken — multiple viewers steal each other's events  ✅ FIXED
**`webapp/server.py:849-873`** · *correctness* · **medium-high**

> **Resolved** (see `tests/test_webapp_stream.py`): the single per-project queue was
> replaced with a retained, durably-backed event log that every viewer tails at its
> own cursor (SSE `id:` + `Last-Event-ID` resume). Many devices now view one build
> live, a reconnect/restart replays the whole story, and checkpoint decisions are
> first-wins + idempotent so multiple devices can drive a build safely. Original
> finding below.


Each project has **one** `events: queue.Queue`, and `_stream` consumes it with
`p.events.get()`. The server is threaded (one handler thread per request), and the
frontend opens an `EventSource` to `/api/stream/<id>` both on build start
(`game.js:1164`) **and** on reload / another device (`game.js:1631`, under a comment
that advertises "reconnect from another device … reconnects the live stream"). When
two connections to the same running build are live, each game event is delivered to
**exactly one** of them (whichever `get()` wins the race) — so the advertised
multi-device / reload-while-running feature split-brains the animation. A same-tab
reload also races: the old `EventSource` can still be draining when the new one
connects, stealing events during the overlap.

**Fix:** fan out per-subscriber. Keep a `set` of subscriber queues per project; `_emit`
publishes to all of them; `_stream` registers/unregisters its own queue. (A ring
buffer of recent events also lets a reconnect replay what it missed.)

### W2 · No auth, binds `0.0.0.0`, triggers real backends — and no CSRF guard
**`webapp/server.py:580-593, 747-835, 878-898`** · *security* · **medium**

`POST /api/projects` launches a real build: with a non-mock backend that shells out to
`claude`/`codex` and can spend against the cost cap. The server already default-binds
`127.0.0.1` and only exposes the LAN on an explicit `WORKSHOP_HOST=0.0.0.0` opt-in
(with a printed warning) — good. The remaining gaps: (1) no endpoint checks any
credential, so on the opt-in LAN anyone can start builds; (2) there's no `Origin`/CSRF
check, so any web page the operator visits can `POST` to
`http://localhost:<port>/api/projects` from their browser even on the default bind.

**Fix:** gate state-changing routes behind a shared token (env var) and/or an
`Origin`/`Host` allowlist; the localhost-default + LAN warning are already in place.

### W3 · Live build state is in-memory; a build in flight is lost on restart  ✅ ADDRESSED
**`webapp/server.py`** · *structure* · **low**

> **Resolved.** Durable factory state is now ON by default for the Workshop
> (`_STATE_DIR`, file-backed checkpointer + trust ledgers + CAS + WORM audit) — the
> right call for a long-lived server (the one-shot CLI keeps its hermetic in-memory
> default). On restart, `_load_persisted` replays the persisted event log and, when a
> durable checkpoint exists for the thread (probed read-only via the checkpointer),
> **resumes the build from its last checkpoint** to completion; a build with no
> recoverable checkpoint is closed out honestly. See
> `tests/test_webapp_stream.py::test_interrupted_build_resumes_to_completion`.
> Residual: concurrent builds now share one checkpoint/ledger DB (see M1) — fine at
> localhost-game concurrency.

### W4 · UI pacing runs inside the real build thread; the event queue is unbounded
**`webapp/server.py:306-309`** · *efficiency* · **low**

`_emit_paced` does `time.sleep(_PACE)` between events **on the build-driver thread**,
so the real build's wall-clock is inflated by the animation cadence (the build is
literally slowed down to look good). Separately, `p.events` is an unbounded queue with
no backpressure: a long build with no connected viewer accumulates every event in
memory until someone streams. Both are bounded in practice (short demo builds) but
couple the build to the costume. Consider pacing on the *consumer* (stream) side, not
the producer.

### W5 · `server.py` is a 923-line god-module
**`webapp/server.py`** · *structure* · **low**

It mixes HTTP routing, the build-driver thread (`_run_factory`/`responder`), SSE
pacing, in-memory project state, SQLite persistence, ideation chat, and the interview
proxy. Same shape as `cli.py` (H5). Split into `routes` / `run_driver` / `state` /
`spec_io`. (Also see the LOW item above: it imports the private `foundry.cli._build_proxy`.)

**Frontend (`game.js`) checked and clean:** rendering uses `textContent` / Phaser
canvas text (not HTML); the lone `innerHTML` injects only a numeric floor id; fetch
errors are swallowed deliberately (a game, not a console). No XSS vector found.

---

## Suggested order of attack
1. **H1** (silently broken feature + a test that hides it) and **H4** (every default deployment can hang).
2. **H2 / H3** (trust-root invariants that don't hold; both also have tests giving false confidence — fix the tests too).
3. **M1 / M2** before the Postgres/multi-host swap they'd surface in.
4. **H5 / M3 / M4** structural splits, then the LOW dead-code sweep (mechanical, low-risk).
