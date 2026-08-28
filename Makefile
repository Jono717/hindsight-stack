# Hindsight stack. `make help` lists everything.
#
# Every target that touches Docker reads .env through docker compose, so there is
# no separate config to keep in sync.

SHELL := /bin/bash
# Without pipefail a recipe's exit status is the LAST command in the pipe, so
# `api.sh | show.py` reported success even when api.sh failed. Every read target
# is such a pipe, so this line is load-bearing, not hygiene.
.SHELLFLAGS := -o pipefail -c
COMPOSE := docker compose

# Read .env for targets that need values in make itself (not just in compose).
ifneq (,$(wildcard .env))
include .env
export
endif

API_PORT ?= $(if $(HINDSIGHT_API_PORT),$(HINDSIGHT_API_PORT),8888)
CP_PORT  ?= $(if $(HINDSIGHT_CP_PORT),$(HINDSIGHT_CP_PORT),9999)
SHIM_PORT ?= 8787
# The generic shim. Override in .env to run a local, gitignored one for a
# provider you would rather not describe publicly (docs/TOKEN-SHIM.md §8).
SHIM_SCRIPT ?= scripts/token-shim.py

.DEFAULT_GOAL := help
.PHONY: help preflight up down restart logs logs-db ps health check-llm models psql shim pull \
        wipe config retain recall recall-text banks find invalidate serve-on serve-status serve-off mcp-url api-key \
        import-mem

help: ## Show this help
	@printf '\nHindsight stack\n\n'
	@{ grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'; } || true
	@printf '\n'

preflight: ## Check config, Docker, and shim reachability before starting
	@bash scripts/preflight.sh

up: ## Start both containers (first run pulls ~3.7 GB and takes a while)
	@test -f .env || { echo "no .env -- run: cp .env.example .env"; exit 1; }
	$(COMPOSE) up -d
	@printf '\nWaiting for the API to report healthy (cold start loads the local\n'
	@printf 'embedder and reranker, then runs migrations -- allow a few minutes)...\n\n'
	@$(MAKE) --no-print-directory health

down: ## Stop both containers, keep the database volume
	$(COMPOSE) down

restart: ## Recreate both containers with the current .env
	$(COMPOSE) up -d --force-recreate

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Follow the Hindsight API log
	$(COMPOSE) logs -f hindsight

logs-db: ## Follow the PostgreSQL log
	$(COMPOSE) logs -f db

config: ## Show the fully-resolved compose config (secrets included -- do not paste)
	$(COMPOSE) config

health: ## Poll the API health endpoint until it passes
	@for i in $$(seq 1 60); do \
	  if curl -fsS http://localhost:$(API_PORT)/health >/dev/null 2>&1; then \
	    printf '\033[32mAPI healthy\033[0m\n'; \
	    printf '  API:           http://localhost:%s\n' '$(API_PORT)'; \
	    printf '  Control Plane: http://localhost:%s\n' '$(CP_PORT)'; \
	    exit 0; \
	  fi; \
	  printf '.'; sleep 5; \
	done; \
	printf '\n\033[31mstill not healthy\033[0m -- check: make logs\n'; exit 1

BANK ?= test

check-llm: ## Do one real round trip to the LLM and print the actual error
	@bash scripts/api.sh POST /v1/default/banks/$(BANK)/health/llm '{}' \
	  | python3 scripts/show.py json

retain: ## Store a memory synchronously. TEXT="..." [BANK=test]
	@test -n "$(TEXT)" || { echo 'usage: make retain TEXT="something to remember"'; exit 1; }
	@bash scripts/api.sh POST /v1/default/banks/$(BANK)/memories \
	  "$$(python3 scripts/mkbody.py retain "$(TEXT)")" \
	  | python3 scripts/show.py json

recall: ## Query stored memories. QUERY="..." [BANK=test]
	@test -n "$(QUERY)" || { echo 'usage: make recall QUERY="what do you know?"'; exit 1; }
	@bash scripts/api.sh POST /v1/default/banks/$(BANK)/memories/recall \
	  "$$(python3 scripts/mkbody.py recall "$(QUERY)")" \
	  | python3 scripts/show.py json

find: ## Find memories and print their ids. QUERY="..." [BANK=test]
	@test -n "$(QUERY)" || { echo 'usage: make find QUERY="stale claim"'; exit 1; }
	@bash scripts/api.sh POST /v1/default/banks/$(BANK)/memories/recall \
	  "$$(python3 scripts/mkbody.py recall "$(QUERY)")" \
	  | python3 scripts/show.py find

invalidate: ## Soft-retire a memory. ID=<uuid> REASON="why" [BANK=test]
	@test -n "$(ID)" || { echo 'usage: make invalidate ID=<uuid> REASON="why it is wrong"'; exit 1; }
	@test -n "$(REASON)" || { echo 'REASON is required -- an invalidation without a reason is unreadable later'; exit 1; }
	@bash scripts/api.sh PATCH /v1/default/banks/$(BANK)/memories/$(ID) \
	  "$$(python3 -c "import json,sys; print(json.dumps(dict(state='invalidated', reason=sys.argv[1])))" "$(REASON)")" \
	  | python3 scripts/show.py invalidate

recall-text: ## Like recall, but print just the matched memory lines
	@test -n "$(QUERY)" || { echo 'usage: make recall-text QUERY="..."'; exit 1; }
	@bash scripts/api.sh POST /v1/default/banks/$(BANK)/memories/recall \
	  "$$(python3 scripts/mkbody.py recall "$(QUERY)")" \
	  | python3 scripts/show.py recall-text

banks: ## List memory banks and their fact counts
	@bash scripts/api.sh GET /v1/default/banks \
	  | python3 scripts/show.py banks

# Imports default to `work`, not the scratch BANK the read targets use -- an
# import into `test` is never what you meant.
IMPORT_BANK ?= work

import-mem: ## Import the claude-mem archive into a bank [IMPORT_BANK=work] [ARGS=--dry-run]
	@python3 scripts/claude-mem-import.py --bank $(IMPORT_BANK) $(ARGS)

# The path that lists models on your provider, relative to SHIM_UPSTREAM.
# OpenAI-shaped APIs use /v1/models; others differ. Gateways that front several
# providers often mount each under its own prefix, and the SAME model then has a
# different id per prefix -- set HINDSIGHT_API_LLM_BASE_URL to the prefix whose
# id you copied, or the model 404s.
MODELS_PATH ?= /v1/models

models: ## List models your provider serves, through the shim [MODELS_PATH=/v1/models]
	@curl -fsS "http://127.0.0.1:$(SHIM_PORT)$(MODELS_PATH)" -H 'Authorization: Bearer dummy' \
	  | python3 -c "import json,sys; d=json.load(sys.stdin); \
	      ids=[m.get('id') or m.get('name') for m in (d.get('data') or d.get('models') or [])]; \
	      print('\n'.join('  '+str(i) for i in ids)); \
	      print(f'\n  {len(ids)} models')" \
	  || { echo "failed -- is the shim running (make shim)?"; exit 1; }

# ── Remote access over Tailscale (docs/REMOTE-ACCESS.md) ─────────────────────
# The container ports stay bound to 127.0.0.1. `tailscale serve` fronts them with
# a real HTTPS cert, reachable only by devices signed into your tailnet.

serve-on: ## Expose the dashboard + API to your tailnet over HTTPS
	@tailscale serve --bg --https 443 http://127.0.0.1:$(CP_PORT)
	@tailscale serve --bg --https 8443 http://127.0.0.1:$(API_PORT)
	@$(MAKE) --no-print-directory serve-status

serve-status: ## Show what is currently exposed to the tailnet, and to whom
	@tailscale serve status 2>/dev/null || echo "no serve config"
	@printf '\nTailnet devices that can reach it:\n'
	@{ tailscale status 2>/dev/null | awk 'NF{printf "  %s  %s\n", $$2, $$5" "$$6" "$$7}'; } || true
	@printf '\nFunnel (public internet) capable: '
	@{ tailscale status --json 2>/dev/null | python3 -c "import json,sys; \
	  caps=json.load(sys.stdin).get('Self',{}).get('Capabilities') or []; \
	  print('YES -- review this' if any('funnel' in str(c).lower() for c in caps) else 'no')"; } \
	  || echo 'unknown (tailscale not available)'

api-key: ## Print the API key from .env (add | pbcopy to copy it)
	@test -f .env || { echo "no .env"; exit 1; }
	@k=$$(grep -E '^HINDSIGHT_API_TENANT_API_KEY=' .env | cut -d= -f2-); \
	  test -n "$$k" || { echo "HINDSIGHT_API_TENANT_API_KEY is not set -- the API is UNAUTHENTICATED"; exit 1; }; \
	  printf '%s\n' "$$k"

mcp-url: ## Print the claude mcp add command for a remote device [BANK=work]
	@test -n "$${HINDSIGHT_API_TENANT_API_KEY:-}" \
	  || { echo "HINDSIGHT_API_TENANT_API_KEY is not set in .env -- the API would be unauthenticated"; exit 1; }
	@printf 'Run this on the remote device:\n\n'
	@printf '  claude mcp add --transport http hindsight-%s \\\n' '$(BANK)'
	@printf '    https://%s:8443/mcp/%s/ \\\n' "$$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')" '$(BANK)'
	@printf '    --header "Authorization: Bearer %s"\n\n' "$$HINDSIGHT_API_TENANT_API_KEY"

serve-off: ## Stop exposing anything to the tailnet
	@tailscale serve --https=443 off 2>/dev/null || true
	@tailscale serve --https=8443 off 2>/dev/null || true
	@tailscale serve status 2>/dev/null || echo "no serve config -- nothing exposed"

psql: ## Open a psql shell on the database
	$(COMPOSE) exec db psql -U $${HINDSIGHT_DB_USER:-hindsight_user} \
	                        -d $${HINDSIGHT_DB_NAME:-hindsight_db}

shim: ## Run the credential shim in the foreground (Ctrl-C to stop)
	@python3 $(SHIM_SCRIPT) --verbose

pull: ## Pull newer images
	$(COMPOSE) pull

wipe: ## DESTRUCTIVE: stop containers and delete every stored memory
	@printf 'This deletes the pg_data volume and every memory in it.\n'
	@read -r -p 'Type the word DELETE to confirm: ' c; [[ "$$c" == "DELETE" ]] || { echo aborted; exit 1; }
	$(COMPOSE) down -v
