# Foundry — one front door for setup, build, run, test, and deploy.
# `make` (or `make help`) lists every target. Override any variable inline, e.g.
#   make run REQUEST="build a link shortener" BACKEND=claude-cli MODE=gated
#
# Requires `uv` (https://docs.astral.sh/uv/). Docker targets additionally need a
# Docker daemon; the `token` target needs the Claude Code CLI on a Max account.

# ---- tunables (override on the command line) -------------------------------
# MODE:    assistant | gated | autonomous
# BACKEND: mock | anthropic | claude-cli | codex | ollama-gemma
# INSTALL_CLI_BACKENDS: `true` bakes the claude/codex CLIs into the docker image
REQUEST ?= build a tiny URL-shortener service
MODE ?= assistant
BACKEND ?= mock
# Run output goes to an ephemeral, gitignored dir — NOT ./build (that is the
# project's tracked planning-doc directory).
OUT ?= ./.run/code
DOCS ?= ./.run/docs
PORT ?= 8765
IMAGE ?= foundry:dev
INSTALL_CLI_BACKENDS ?= false

UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help setup check lint fix typecheck test run spec workshop token env \
        docker-build compose-config up down clean

help: ## Show this help
	@echo "Foundry — make targets:"
	@grep -hE '^[a-zA-Z0-9_.-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Vars: REQUEST MODE BACKEND OUT DOCS PORT IMAGE INSTALL_CLI_BACKENDS"

# ---- develop ---------------------------------------------------------------
setup: ## Install deps + the project into a local venv (uv sync)
	$(UV) sync

check: lint typecheck test ## Lint + typecheck + full test suite (the CI gate)

lint: ## Ruff lint
	$(UV) run ruff check .

fix: ## Ruff autofix + format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## Mypy (strict, per pyproject)
	$(UV) run mypy foundry webapp

test: ## Full test suite, parallel
	$(UV) run pytest -n auto -q

# ---- run the factory -------------------------------------------------------
run: ## Build REQUEST with BACKEND/MODE, writing code to OUT (+ docs to DOCS)
	$(UV) run python -m foundry.cli "$(REQUEST)" \
	  --backend $(BACKEND) --mode $(MODE) --out $(OUT) --docs $(DOCS)

spec: ## Co-generate a spec from REQUEST (ideation) -> spec.md
	$(UV) run python -m foundry.ideation --mock "$(REQUEST)" > spec.md
	@echo "wrote spec.md — refine it, then: make run REQUEST=... (or --spec spec.md)"

workshop: ## Launch the localhost Workshop game UI (http://localhost:$(PORT))
	WORKSHOP_PORT=$(PORT) $(UV) run python -m webapp.server

# ---- credentials / config --------------------------------------------------
token: ## Mint a Max-subscription OAuth token for the claude-cli backend
	@command -v claude >/dev/null 2>&1 || { \
	  echo "✗ 'claude' CLI not found. Install Claude Code: https://code.claude.com"; exit 1; }
	@echo "→ Running 'claude setup-token' (log into your Max account in the browser)."
	@echo "  Put the printed token in .env as:  CLAUDE_CODE_OAUTH_TOKEN=<token>"
	@echo "  (Do NOT also set ANTHROPIC_API_KEY — it outranks the token and bills the API.)"
	claude setup-token

env: ## Scaffold a .env (secrets; gitignored) if missing
	@if [ -f .env ]; then echo ".env already exists — leaving it untouched."; else \
	  printf '%s\n' \
	    'POSTGRES_PASSWORD=change-me' \
	    'FOUNDRY_MODEL_BACKEND=mock' \
	    '# claude-cli on a Max subscription (mint with: make token):' \
	    '# FOUNDRY_MODEL_BACKEND=claude-cli' \
	    '# CLAUDE_CODE_OAUTH_TOKEN=' \
	    '# ollama-gemma (Ollama runs on the HOST; never GPU-passthrough):' \
	    '# FOUNDRY_MODEL_BACKEND=ollama-gemma' \
	    '# FOUNDRY_OLLAMA_URL=http://host.docker.internal:11434' \
	    '# codex backend: OPENAI_API_KEY=' > .env; \
	  echo "wrote .env — edit it before 'make up'."; fi

# ---- docker / compose ------------------------------------------------------
docker-build: ## Build the image (INSTALL_CLI_BACKENDS=true bakes claude/codex CLIs)
	docker build --build-arg INSTALL_CLI_BACKENDS=$(INSTALL_CLI_BACKENDS) -t $(IMAGE) .

compose-config: ## Validate docker-compose.yml without starting anything
	docker compose config

up: env ## Build + start the compose stack (reads .env)
	docker compose up --build

down: ## Stop the compose stack
	docker compose down

# ---- housekeeping ----------------------------------------------------------
clean: ## Remove caches + the ephemeral .run/ output (never touches build/)
	rm -rf .pytest_cache .mypy_cache .ruff_cache ./.run spec.md
	find . -path ./.git -prune -o -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
