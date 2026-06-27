# Real `claude -p` builds — runbook (verified end-to-end)

This is the **validated** recipe for turning a one-line request into real,
gate-verified software using `claude -p` (a Claude Max subscription) as both the
agents' reasoning backend and the coding substrate. A real build was run
end-to-end on this stack: from `"build a Python function safe_divide…"` it
authored real code + tests, the Build/Test gates **executed** them (real pytest,
green), and it emitted the PR-equivalent action + wrote the tree to disk.

## What a real build produces

```
out/divide.py        # real, AI-authored implementation (handles the bool edge case…)
out/test_divide.py   # real pytest the Test gate actually ran
docs/…               # PRD, design+ADRs, test plan/report, BUILD-MANIFEST, …
```

The generated tests pass under a clean, independent `pytest` — not just the
internal gates.

## Prerequisites (once)

- **`claude` CLI on PATH**, authenticated for your Max subscription:
  - On a workstation: `claude login` (or `make token` → `CLAUDE_CODE_OAUTH_TOKEN`
    for a headless box / container).
  - The credential is the same one this CLI / the iOS app uses; `claude -p`
    inherits it. Do **not** also set `ANTHROPIC_API_KEY` (it outranks the
    subscription and bills the pay-per-token API).
- **Run as a non-root user** (the Docker image's `foundry` user qualifies). Root
  matters only for the permission mode (below).

## The command

```sh
FOUNDRY_DRIFT_POLICY=advisory FOUNDRY_PROGRESS=1 \
  python -m foundry.cli \
    --backend claude-cli \      # agents reason via claude -p
    --adapter claude-code \     # claude -p writes the actual code in a sandbox
    --out ./out --docs ./docs \
    "build a Python function safe_divide(a, b) that raises ValueError on \
     zero or non-numeric input; include pytest tests for the valid case and \
     both error cases"
```

In the **Workshop UI**, pick backend `claude-cli` + coding substrate
`claude-code` in the new-build dialog — same effect, with a live view.

## The settings that matter (learned the hard way)

| Setting | Why |
|---|---|
| `--adapter claude-code` | Without a real adapter the reviewer fail-closes — the `stub` writes no code. |
| `FOUNDRY_DRIFT_POLICY=advisory` | The proxy pins exact model ids per tier; `claude -p` serves dated snapshots (e.g. `claude-haiku-4-5-20251001`) that don't match the bare default, so the **fail-closed** drift policy would halt. `advisory` records drift instead of enforcing it. Alternatively, **pin** the exact ids: `FOUNDRY_MODEL_T1/T2/T3` (see deploy-docker.md). |
| `FOUNDRY_PROGRESS=1` | A real build runs **10-20 minutes** (many sequential `claude -p` calls; the code-writing step alone is minutes). Without this the CLI is silent and looks hung. This streams each stage to stderr. |
| `--permission-mode` (adapter) | Headless `claude -p` denies file writes by default; the adapter passes `acceptEdits` so it can write into its throwaway sandbox worktree. `acceptEdits` works as root **and** non-root; set `FOUNDRY_CLAUDE_PERMISSION_MODE=bypassPermissions` for the broader mode on a **non-root** box (root is refused by the CLI). |
| `--max-fix-attempts 1` | Each fix-loop iteration re-runs the (slow) implementation+gates. Start at 1; raise once a request is reliably green. |
| `--cost-cap-microusd N` | A real build spends real quota/tokens — set an envelope. On Max it draws subscription usage limits (no separate dollar bill), so a runaway can still rate-limit you. |

## Timing & cost expectations

- **~10-20 min wall-clock** per small build (the code-writing `claude -p` session
  is the long pole). Watch `FOUNDRY_PROGRESS` to see which stage is active.
- On a **Pi**, expect the same model-call latency (it's network-bound to the API,
  not CPU-bound) plus slower local pytest in the gates.
- Spend is against your **Max usage limits**, not a per-token invoice.

## Gotchas the gates will (correctly) catch

- **Spec gate** requires a security/abuse acceptance criterion — phrase requests
  so an abuse/error case is in scope (most real services have one).
- A real model can write code that **fails** the Build/Test gate — that's the
  point. The fix-loop retries up to `--max-fix-attempts`; bump it for harder
  asks, and tune the request/spec if a particular build keeps failing.

## LAN serving

On a trusted home LAN, run the Workshop with `docker compose up`
(→ `0.0.0.0:8765`) over **plain HTTP** — open it from any device at
`http://<this-host>:8765`. A token is optional (`FOUNDRY_WORKSHOP_TOKEN`); set
one only if others share the network. TLS only matters on an untrusted/public
network, which is out of scope here. See `docs/deploy-docker.md`. Every device
gets the same live, restart-surviving view of the build.
