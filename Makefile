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

# Host UID/GID passed to dev containers (docker-compose.override.yml) that write
# to bind-mounted host dirs (dfe-engine config/schemas), so files are owned by
# the host user rather than the image user and writes don't hit permission errors.
export DFE_DEV_UID := $(shell id -u)
export DFE_DEV_GID := $(shell id -g)

# Goals that work without a resolved service profile
BOOTSTRAP_GOALS := init help stack

# Resolve the active profile only when a goal actually needs the compose stack
ifneq (,$(filter-out $(BOOTSTRAP_GOALS),$(or $(MAKECMDGOALS),help)))
    include .profile.mk
    SERVICES ?=
    ifeq ($(strip $(SERVICES)),)
        ACTIVE_SERVICES := $(DFE_SERVICES)
    else
        INVALID_SERVICES := $(filter-out $(DFE_SERVICES),$(SERVICES))
        ifneq ($(INVALID_SERVICES),)
            $(error 'SERVICES' contains names not in the resolved stack: $(INVALID_SERVICES). Available: $(DFE_SERVICES))
        endif
        ACTIVE_SERVICES := $(filter $(SERVICES),$(DFE_SERVICES))
    endif
endif

.profile.mk: FORCE
	@python3 scripts/resolve_profile.py

.PHONY: FORCE
FORCE:

.env:
	@python3 scripts/init.py

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

.PHONY: init
init: ## Create .env and per-service env/<service>.env files from templates
	@python3 scripts/init.py

.PHONY: stack
stack: .env ## Pin image versions into .env from the DFE stack SSoT (VERSION=X.Y.Z[-rc.N])
	@python3 scripts/stack.py $(VERSION)

# ---------------------------------------------------------------------------
# Dev (builds from local source via docker-compose.override.yml)
# ---------------------------------------------------------------------------

.PHONY: dev
dev: down ## Build local DFE images from source and start the dev stack
	docker compose $(PROFILE_FLAGS) pull
	python3 scripts/build_dev_images.py $(ACTIVE_SERVICES)
	docker compose $(PROFILE_FLAGS) up -d $(ACTIVE_SERVICES)

.PHONY: dev-build
dev-build: ## Build local DFE images from source (no start)
	docker compose $(PROFILE_FLAGS) pull
	python3 scripts/build_dev_images.py $(ACTIVE_SERVICES)

# ---------------------------------------------------------------------------
# CI / registry images (skips docker-compose.override.yml)
# ---------------------------------------------------------------------------

.PHONY: ci
ci: down  ## Pull and start infra and registry DFE images
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) pull
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) pull $(ACTIVE_SERVICES)
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) up -d $(ACTIVE_SERVICES)

.PHONY: ci-pull
ci-pull: ## Pull infra and registry DFE images
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) pull
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) pull $(ACTIVE_SERVICES)

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
	@python3 ./scripts/test_e2e.py $(E2E_TESTS)

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

.PHONY: ps
ps: ## Show running containers
	docker compose ps

.PHONY: down
down: ## Stop and remove the active (or SERVICES-selected) containers
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) down $(ACTIVE_SERVICES)

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
