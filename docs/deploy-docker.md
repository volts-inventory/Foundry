# Running the Foundry Workshop in Docker (on a web server)

The default image **is** the Workshop UI. Out of the box it is fully functional with
**no secrets**: the deterministic `mock` backend, sqlite checkpoints, and durable
state on volumes. To do real builds you add a model backend + its key; to put it on
a public web server you add a TLS reverse proxy + a token.

Verified: the app serves the UI + assets, runs a build end-to-end (all gates pass, code
+ docs written), survives a restart, and enforces auth. (`docker build`/`up` themselves
were not run in the authoring sandbox — no daemon — but the exact code + config the
container runs is exercised by the test suite and a live server smoke.)

## 1. Quick start (zero config)

```sh
docker compose up            # → http://localhost:8765   (mock backend, no keys)
# or a single container:
docker build -t foundry:dev .
docker run -p 8765:8765 -v foundry_state:/var/lib/foundry/state \
  -v foundry_web:/var/lib/foundry/web foundry:dev
```

`docker compose up` starts **only** the `app` service (postgres + the proxy/signer/
builder topology stubs are behind the `postgres` / `isolated` profiles). State lives on
the `foundry_state` / `foundry_web` / `foundry_cas` volumes, so builds survive a restart.

## 2. Real model builds

Pick a backend and supply its credential (via a gitignored `.env` or `-e`):

```sh
# Anthropic SDK:
FOUNDRY_MODEL_BACKEND=anthropic ANTHROPIC_API_KEY=sk-… docker compose up
# claude-cli (Max subscription) or codex: build with the CLIs baked in, then inject a token
docker build --build-arg INSTALL_CLI_BACKENDS=true -t foundry:cli .
#   FOUNDRY_MODEL_BACKEND=claude-cli CLAUDE_CODE_OAUTH_TOKEN=… docker compose up
```

Set a **cost cap** for real backends (`FOUNDRY_COST_CAP_MICROUSD`) — a build spends real
tokens. The UI's backend picker also selects per build.

### Which models it uses

Each backend pins a model id per reasoning **tier** (T1 deep / T2 mid / T3 cheap). The
defaults:

| Backend | T1 | T2 | T3 |
|---|---|---|---|
| `anthropic` / `claude-cli` | `claude-opus-4-8` | `claude-sonnet-4-6` | `claude-haiku-4-5` |
| `codex` | `gpt-5-codex` | `gpt-5` | `gpt-5-mini` |
| `ollama-gemma` | `gemma3:27b` | `gemma3:12b` | `gemma3:4b` |

The proxy **pins** these ids and the **fail-closed** drift policy (`FOUNDRY_DRIFT_POLICY`,
default `fail-closed`) rejects a gate/judge call whose *served* model doesn't resolve to
the pinned one. So if your egress serves a model the default map doesn't name (a newer
snapshot, a dated id like `claude-haiku-4-5-20251001`, a Bedrock/Vertex alias), **pin the
exact id** per tier — no code edit needed:

```sh
FOUNDRY_MODEL_T1=claude-opus-4-8           # deep reasoning / architecture
FOUNDRY_MODEL_T2=claude-sonnet-4-6         # the bulk of the agents
FOUNDRY_MODEL_T3=claude-haiku-4-5-20251001 # cheap/parallel
```

A partial set overlays the default (set only the tiers you need). The Workshop's
`/api/config` reports the **effective** map (`models`) so the UI can show what runs. If
you'd rather not pin and just record mismatches, set `FOUNDRY_DRIFT_POLICY=advisory`
(the gates still run; drift is logged, not enforced).

## 3. Exposing it on a web server  ← the important part

The Workshop is a stdlib HTTP server: **no TLS, not hardened for direct internet
exposure.** Put it behind a TLS-terminating reverse proxy and require a token. **Do not
publish port 8765 to the internet directly.**

1. **Set a token.** Every state-changing request (start/steer a build) then requires it:
   ```sh
   FOUNDRY_WORKSHOP_TOKEN=$(openssl rand -hex 24)
   ```
   Open the UI at `https://your.domain/?token=<that value>`. (A same-origin/CSRF guard
   always applies even without a token, but on a public server the token is required.)

2. **Bind to localhost on the host** and let the proxy reach it (or keep it on an
   internal docker network and only publish the proxy). E.g. publish to loopback only:
   ```yaml
   # docker-compose override:
   services: { app: { ports: ["127.0.0.1:8765:8765"] } }
   ```

3. **Terminate TLS at a reverse proxy.** Caddy (auto-HTTPS) is simplest — note the
   SSE-friendly settings (the build stream is Server-Sent Events; disable buffering):
   ```caddy
   your.domain {
       reverse_proxy 127.0.0.1:8765 {
           flush_interval -1          # stream SSE immediately (no buffering)
       }
   }
   ```
   nginx equivalent:
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8765;
       proxy_buffering off;           # SSE: don't buffer the event stream
       proxy_read_timeout 1h;         # long-running builds + the live stream
       proxy_set_header Host $host;
   }
   ```

4. (Optional) Add **rate limiting / IP allow-listing** at the proxy. The app has no
   per-user accounts — the token is a single shared secret; treat it like one.

## 4. Durable + HA persistence

- **Single host (default):** sqlite + the filesystem CAS on the mounted volumes is
  durable and survives a restart. This is the validated default.
- **Postgres (HA checkpoints):** `docker compose --profile postgres up` with
  `FOUNDRY_CHECKPOINTER=postgres` + `POSTGRES_PASSWORD`. NOTE: the PostgresSaver
  checkpoint path is validated (`tests/test_postgres_checkpointer.py`), but the
  outbox/WORM SQL dialect shim is the one remaining scoped step — until it lands,
  `make_checkpointer` fails closed on `postgres` (see `foundry/durability/checkpointer.py`).
  Stay on sqlite for now.

## 5. What this gives you vs. doesn't

**Fully functional:** the Workshop UI, real (or mock) builds end-to-end behind the gates,
durable restart-surviving state, multi-device live view, auth (token + CSRF), a
healthcheck, non-root container.

**Still dev-grade (does not affect running the Workshop, matters only for the
autonomous-release *trust* guarantees):** the crypto root defaults to HMAC (set
`FOUNDRY_SIGNING_SCHEME=ed25519` + keys, build with `--extra crypto`, for the asymmetric
root); the two zones run in one process (physical container isolation is the
`isolated`-profile topology, not yet built); real scanners (OPA/Semgrep/Syft) are seams.
For *running builds and serving the UI* none of these block you.
