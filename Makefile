# Project:   dfe-docker
# File:      Makefile
# Purpose:   Convenience targets for Docker Compose stack
# Language:  Makefile
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Infrastructure only (Redpanda + ClickHouse)
# ---------------------------------------------------------------------------

.PHONY: infra
infra: ## Start infrastructure (Redpanda + ClickHouse)
	docker compose --profile infra up -d

.PHONY: infra-logs
infra-logs: ## Tail infrastructure logs
	docker compose --profile infra logs -f

# ---------------------------------------------------------------------------
# Full stack (infrastructure + receiver + loader)
# ---------------------------------------------------------------------------

.PHONY: up
up: ## Start full stack (receiver + loader + infra)
	docker compose --profile full up -d

.PHONY: up-ui
up-ui: ## Start full stack + Redpanda Console UI
	docker compose --profile full --profile ui up -d

.PHONY: logs
logs: ## Tail all service logs
	docker compose --profile full logs -f

.PHONY: ps
ps: ## Show running containers
	docker compose --profile full --profile ui ps

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

.PHONY: test
test: ## Send test events and verify ClickHouse
	./scripts/send-test-events.sh

.PHONY: verify
verify: ## Query ClickHouse to check data
	./scripts/verify-clickhouse.sh

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

.PHONY: down
down: ## Stop all services
	docker compose --profile full --profile ui down

.PHONY: clean
clean: ## Stop all services and remove volumes
	docker compose --profile full --profile ui down -v

.PHONY: restart
restart: down up ## Restart full stack

.PHONY: pull
pull: ## Pull latest GHCR images
	docker compose --profile full pull

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
