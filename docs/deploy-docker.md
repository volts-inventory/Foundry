# Running the Foundry Workshop in Docker (on your LAN)

The default image **is** the Workshop UI. Out of the box it is fully functional with
**no secrets**: the deterministic `mock` backend, sqlite checkpoints, and durable
state on volumes. To do real builds you add a model backend + its key. It's built to
run on a trusted home LAN (e.g. a Raspberry Pi) over **plain HTTP** — open it from any
device on the network.

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

`docker compose up` starts the single `app` service (the Workshop, bound to
`0.0.0.0:8765`). From another device on the LAN open `http://<this-host>:8765` (the
host's IP or hostname — e.g. `http://raspberrypi.local:8765`). State lives on the
`foundry_state` / `foundry_web` / `foundry_cas` volumes, so builds survive a restart.

## 2. Real model builds

Pick a backend and supply its credential (via a gitignored `.env` or `-e`):

```sh
# Anthropic SDK:
FOUNDRY_MODEL_BACKEND=anthropic ANTHROPIC_API_KEY=sk-… docker compose up
# claude-cli (Max subscription) or codex: build with the CLIs baked in, then inject a token
docker build --build-arg INSTALL_CLI_BACKENDS=true -t foundry:cli .
#   FOUNDRY_MODEL_BACKEND=claude-cli CLAUDE_CODE_OAUTH_TOKEN=… docker compose up
```

The backends and their credentials:

| Backend | Engine | Credential |
|---|---|---|
| `mock` *(default)* | deterministic scripted transport | none |
| `anthropic` | Claude API (SDK) | `ANTHROPIC_API_KEY` |
| `claude-cli` | `claude -p` (Max subscription) | `CLAUDE_CODE_OAUTH_TOKEN` (`make token`) |
| `codex` | `codex exec` | `OPENAI_API_KEY` |
| `ollama-gemma` | local Ollama server on the host | none (`FOUNDRY_OLLAMA_URL`) |

For `claude-cli` on a Max subscription, mint the token once on a workstation with
`make token` (`claude setup-token`) and inject it as `CLAUDE_CODE_OAUTH_TOKEN`. Do
**not** also set `ANTHROPIC_API_KEY` — it outranks the token and bills the pay-per-token
API. For `ollama-gemma`, Ollama runs on the **host** (it owns the GPU natively) and the
container reaches it over HTTP via `FOUNDRY_OLLAMA_URL`.

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

## 3. Access on the LAN (plain HTTP)

The Workshop is a stdlib HTTP server and on a **trusted home LAN it's served over plain
HTTP** — no TLS, no reverse proxy needed. `docker compose up` binds it to
`0.0.0.0:8765`; reach it from any device on the network at `http://<this-host>:8765`.

A token is **optional**:

- **Just you on a private LAN:** no token needed. A same-origin/CSRF guard always
  applies to state-changing requests (start/steer a build).
- **Others share the network:** set a shared token, which then gates every
  state-changing request:
  ```sh
  FOUNDRY_WORKSHOP_TOKEN=$(openssl rand -hex 24)
  ```
  Open the UI at `http://<this-host>:8765/?token=<that value>`. The app has no per-user
  accounts — the token is a single shared secret; treat it like one.

(TLS only matters if you ever expose the Workshop on an untrusted or public network —
that's out of scope here; this is a single-user / trusted-LAN deployment.)

## 4. Durable state

sqlite + the filesystem CAS on the mounted volumes (`foundry_state` / `foundry_cas` /
`foundry_web`) is durable and survives a restart. This is the validated default — the
Workshop's catalog, event log, factory checkpoints/ledgers, and written output all live
on the volumes.

## 5. What this gives you

The Workshop UI, real (or mock) builds end-to-end behind the gates, durable
restart-surviving state, multi-device live view (every device on the LAN sees the same
build), auth (optional token + the CSRF guard), a healthcheck, and a non-root container.
