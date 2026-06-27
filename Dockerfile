# Foundry application image — builds the `foundry` package and its deps with uv.
#
# Builds a runnable Workshop image: `docker build -t foundry:dev .` then
# `docker run -p 8765:8765 -e WORKSHOP_HOST=0.0.0.0 foundry:dev` serves the UI.
# 12-factor / EKS-portable: config via env, no baked-in secrets, non-root user.
#
# IMAGE PINNING (supply-chain): the base images below use minor-version TAGS so
# the image builds out of the box. For a reproducible production build, PIN each
# by digest (`@sha256:…`) — resolve with e.g.
#     docker buildx imagetools inspect ghcr.io/astral-sh/uv:python3.12-bookworm-slim
# and have CI fail on an unpinned tag.

# --- builder: install deps + project into a self-contained venv --------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Layer-cache deps: copy only the lockfiles first. `--frozen` ensures the build
# matches the committed lock (no surprise resolution). `--no-install-project`
# installs only third-party deps so app-code changes don't bust this layer.
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project || \
    uv sync --no-dev --no-install-project

# Now the source, then install the project itself into the same venv.
COPY foundry ./foundry
COPY README.md* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev || uv sync --no-dev

# --- runtime: slim, non-root, venv-on-PATH -----------------------------------
# Minor-version tag (pin by digest for a reproducible production build, see top).
FROM python:3.12-slim-bookworm AS runtime

# Non-root by construction (EKS securityContext parity).
RUN groupadd --system foundry && useradd --system --gid foundry --home /app foundry

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # 12-factor defaults; every one is overridable by the orchestrator (compose
    # env / K8s env). NO secrets baked in.
    FOUNDRY_CHECKPOINTER=sqlite \
    FOUNDRY_CAS_ROOT=/var/lib/foundry/cas \
    FOUNDRY_CHECKPOINT_DB=/var/lib/foundry/state/checkpoints.sqlite

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/foundry ./foundry
COPY webapp ./webapp
COPY pyproject.toml ./

# --- optional: CLI model backends (claude -p / codex) ------------------------
# OFF by default (the anthropic / ollama backends need no extra binary, so the
# base image stays slim). Build with `--build-arg INSTALL_CLI_BACKENDS=true` to
# bake the CLIs in for FOUNDRY_MODEL_BACKEND=claude-cli|codex.
#
# HEADLESS AUTH (the load-bearing part): interactive `claude login` / `codex
# login` open a BROWSER OAuth flow — they CANNOT run in a container. Authenticate
# by INJECTING A CREDENTIAL as a secret — NEVER baked into the image (full
# precedence + options in docs/deploy-compose.md):
#   * claude-cli on a MAX SUBSCRIPTION -> CLAUDE_CODE_OAUTH_TOKEN, minted once on a
#     workstation with `claude setup-token`. `claude -p` honours it (no --bare).
#     Do NOT also set ANTHROPIC_API_KEY: it OUTRANKS the token and would bill the
#     pay-per-token Console API instead of the subscription.
#   * codex -> OPENAI_API_KEY.
#   The proxy passes its env through to the CLI subprocess (cli_transports inherits
#   os.environ), so the credential just needs to be on the process. Compose: a
#   .env value; K8s: a Secret.
#   * OR a pre-provisioned ~/.claude/.credentials.json (Linux/Win; NOT macOS
#     keychain) mounted read-only under $HOME (HOME=/app here, so /app/.claude).
#     Mount it; do not COPY it into the image.
ARG INSTALL_CLI_BACKENDS=false
RUN if [ "$INSTALL_CLI_BACKENDS" = "true" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends nodejs npm \
      && npm install -g @anthropic-ai/claude-code @openai/codex \
      && apt-get purge -y --auto-remove \
      && rm -rf /var/lib/apt/lists/* /root/.npm; \
    fi

# Volume mount points for the durable CAS + checkpoint DB + the workshop's written
# output (compose/K8s provide the actual volumes; owned by the non-root user).
RUN mkdir -p /var/lib/foundry/cas /var/lib/foundry/state /var/lib/foundry/web \
    && chown -R foundry:foundry /var/lib/foundry /app
USER foundry

# ENTRYPOINT is `python -m`, so `command:` (compose) / args pick the module. The
# default serves the Workshop UI (webapp.server reads WORKSHOP_HOST/PORT/
# WORKSHOP_OUTPUT_DIR + FOUNDRY_MODEL_BACKEND/FOUNDRY_MAX_SLOTS from the env).
# Override to `["foundry", "run", …]` for a one-shot CLI build instead.
# Durable state on the mounted volumes by default (survives a container restart):
# the Workshop's catalog, event log, and factory checkpoints/ledgers live here.
ENV WORKSHOP_OUTPUT_DIR=/var/lib/foundry/web \
    WORKSHOP_STATE_DIR=/var/lib/foundry/state \
    WORKSHOP_HOST=0.0.0.0 \
    WORKSHOP_PORT=8765

# Liveness: the Workshop answers /api/config (stdlib, no curl in the slim image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD \
    python -c "import urllib.request,os,sys; \
u='http://127.0.0.1:'+os.environ.get('WORKSHOP_PORT','8765')+'/api/config'; \
sys.exit(0 if urllib.request.urlopen(u,timeout=4).status==200 else 1)"

EXPOSE 8765
ENTRYPOINT ["python", "-m"]
CMD ["webapp.server"]
