# Project:   dfe-docker
# File:      Makefile
# Purpose:   Convenience targets for Docker Compose stack
# Language:  Makefile
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

.DEFAULT_GOAL := help

# Non-fatal: init creates .env, so it must not exist on a fresh checkout
-include .env

# Goals that work without a resolved service profile
BOOTSTRAP_GOALS := init help

# Resolve the active profile only when a goal actually needs the compose stack
ifneq (,$(filter-out $(BOOTSTRAP_GOALS),$(or $(MAKECMDGOALS),help)))
    include .profile.mk
endif

.profile.mk: service_profiles.yaml .env
	@python3 scripts/resolve_profile.py

.env:
	@python3 scripts/init.py

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

.PHONY: init
init: ## Create .env and per-service env/<service>.env files from templates
	@python3 scripts/init.py

# ---------------------------------------------------------------------------
# Dev (builds from local source via docker-compose.override.yml)
# ---------------------------------------------------------------------------

.PHONY: dev
dev: down ## Build local DFE images from source and start the dev stack
	docker compose $(PROFILE_FLAGS) pull
	python3 scripts/build_dev_images.py $(DFE_SERVICES)
	docker compose $(PROFILE_FLAGS) up -d $(DFE_SERVICES)

.PHONY: dev-build
dev-build: ## Build local DFE images from source (no start)
	docker compose $(PROFILE_FLAGS) pull
	python3 scripts/build_dev_images.py $(DFE_SERVICES)

# ---------------------------------------------------------------------------
# CI / registry images (skips docker-compose.override.yml)
# ---------------------------------------------------------------------------

.PHONY: ci
ci: down  ## Pull and start infra and registry DFE images
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) pull
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) pull $(DFE_SERVICES)
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) up -d $(DFE_SERVICES)

.PHONY: ci-pull
ci-pull: ## Pull infra and registry DFE images
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) pull
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) pull $(DFE_SERVICES)

# ---------------------------------------------------------------------------
# Infrastructure only (Kafka + ClickHouse)
# ---------------------------------------------------------------------------

.PHONY: infra
infra: ## Start infrastructure services
	docker compose $(PROFILE_FLAGS) pull
	docker compose $(PROFILE_FLAGS) up -d

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

.PHONY: test-e2e
test-e2e: ## End-to-end test executor (pass test names via E2E_TESTS)
	@python3 ./scripts/test-e2e.py $(E2E_TESTS)

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
clean: ## Stop and remove all containers and volumes across every profile
	docker compose -f docker-compose.yml --profile "*" down -v --remove-orphans

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
    	awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
