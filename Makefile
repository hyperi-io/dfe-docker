# Project:   dfe-docker
# File:      Makefile
# Purpose:   Convenience targets for Docker Compose stack
# Language:  Makefile
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

.DEFAULT_GOAL := help

# Read .env for port vars, credentials, versions (NOT profile/config)
-include .env

# Resolve active profile from service_profiles.yaml (override: DFE_PROFILE env var)
# .profile.mk is created by resolve-profile.py
# This is because $(shell) collapses newlines and we need to set multiple variables (PROFILE_FLAGS, PROFILE_NAME)
$(shell python3 scripts/resolve-profile.py)
include .profile.mk

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

.PHONY: init
init: ## Create .env and per-service env/<service>.env files from templates
	@if [ -f .env ]; then \
		echo "  .env: skipped (exists)"; \
	else \
		cp .env.example .env; \
		echo "  .env: created"; \
	fi
	@mkdir -p env
	@for src in env.example/*.env; do \
		[ -e "$$src" ] || { echo "  env.example/ has no *.env templates"; exit 1; }; \
		name=$$(basename "$$src"); \
		dst="env/$$name"; \
		if [ -f "$$dst" ]; then \
			echo "  $$name: skipped (exists)"; \
		else \
			cp "$$src" "$$dst"; \
			echo "  $$name: created"; \
		fi; \
	done

# ---------------------------------------------------------------------------
# Dev (builds from local source via docker-compose.override.yml)
# ---------------------------------------------------------------------------

.PHONY: dev
dev: down
	docker compose $(PROFILE_FLAGS) up --build -d $(DFE_SERVICES)

.PHONY: build-local
build-local: ## Build images from local source
	docker compose $(PROFILE_FLAGS) build $(DFE_SERVICES)

.PHONY: dev-logs
dev-logs: ## Tail all service logs
	docker compose logs -f

# ---------------------------------------------------------------------------
# CI / registry images (skips docker-compose.override.yml)
# ---------------------------------------------------------------------------

.PHONY: ci
ci: down ## Start stack using published registry images (no local build)
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) build --no-cache --pull $(DFE_SERVICES)
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) up -d $(DFE_SERVICES)

.PHONY: pull
pull: ## Pull latest images from registry
	docker compose -f docker-compose.yml pull

.PHONY: rebuild
rebuild: ## Force rebuild DFE images (removes old, pulls fresh)
	docker rmi -f dfe-loader:$${DFE_LOADER_VERSION:-latest} dfe-receiver:$${DFE_RECEIVER_VERSION:-latest} 2>/dev/null
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) build --no-cache --pull $(DFE_SERVICES)
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) up -d $(DFE_SERVICES)

# ---------------------------------------------------------------------------
# Infrastructure only (Kafka + ClickHouse)
# ---------------------------------------------------------------------------

.PHONY: infra
infra: ## Start infrastructure services (Kafka + ClickHouse)
	docker compose --profile clickhouse --profile kafka up -d

.PHONY: infra-logs
infra-logs: ## Tail infrastructure logs
	docker compose --profile clickhouse --profile kafka logs -f

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

.PHONY: test-infra
test-infra: ## Smoke test infrastructure (Kafka + ClickHouse)
	./scripts/test-infra.sh

.PHONY: test
test: ## Send test events and verify in ClickHouse
	./scripts/send-test-events.sh

.PHONY: test-e2e
test-e2e: ## End-to-end test executor (pass test names via E2E_TESTS)
	@python3 ./scripts/test-e2e.py $(E2E_TESTS)

.PHONY: test-vector
test-vector: ## Feed events via Vector (HTTP + gRPC inbound)
	./scripts/test-vector.sh both

.PHONY: verify
verify: ## Query ClickHouse to check ingested data
	./scripts/verify-clickhouse.sh

# ---------------------------------------------------------------------------
# Certificates (for gRPC TLS on :6000)
# ---------------------------------------------------------------------------

.PHONY: certs
certs: ## Generate self-signed dev certs (ECDSA P-384)
	./scripts/gen-dev-certs.sh

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

.PHONY: ps
ps: ## Show running containers
	docker compose ps

.PHONY: down
down: ## Stop and remove all containers across every profile
	docker compose -f docker-compose.yml --profile "*" down --remove-orphans

.PHONY: clean
clean: ## Stop all services and remove volumes
	docker compose -f docker-compose.yml --profile "*" down -v --remove-orphans

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
