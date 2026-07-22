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

# Host UID/GID passed to live-mode containers (docker-compose.live.yml) that
# write to bind-mounted host dirs (dfe-engine config/schemas), so files are owned
# by the host user rather than the image user and writes don't hit permission
# errors.
export DFE_DEV_UID := $(shell id -u)
export DFE_DEV_GID := $(shell id -g)

# Live-edit mode: `make dev LIVE=1` layers docker-compose.live.yml, which
# bind-mounts dfe-engine config/schemas from DFE_SRC_ROOT so edits land without
# a rebuild. COMPOSE_FILE chains the overlay for every plain `docker compose`
# call this make run spawns; the targets that pass -f explicitly (ci, down,
# clean) ignore COMPOSE_FILE by design, so the registry path stays untouched.
ifneq ($(strip $(LIVE)),)
    export COMPOSE_FILE := docker-compose.yml:docker-compose.override.yml:docker-compose.live.yml
endif

# Goals that work without a resolved service profile. The check-* targets belong
# here because their whole point is running on a fresh checkout -- resolving a
# profile first would make them fail for the reason they exist to detect.
#
# down/clean belong here too: neither references PROFILE_FLAGS or ACTIVE_SERVICES
# any more (both sweep every profile), and requiring a resolvable profile to STOP
# a stack is the same lockout the compose secret comments argue against -- set
# DFE_PROFILE to something that does not exist and you could not tear down.
BOOTSTRAP_GOALS := init help stack down clean limits check check-compose check-hardfail check-dockerfile check-docs check-python

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
	@$(MAKE) --no-print-directory post || echo "post: SELF TEST FAILED -- the stack is up, but it did not prove it moves data. Run 'make post' for detail."

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
	@$(MAKE) --no-print-directory post || echo "post: SELF TEST FAILED -- the stack is up, but it did not prove it moves data. Run 'make post' for detail."

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
# Validation
# The same commands CI runs, so a green local run means a green pipeline.
#
# Safe on a fresh checkout: no stack SSoT and no credentials needed. Two honest
# caveats. Make remakes the `-include .env` above before any target, so a fresh
# checkout gets a generated .env as a side effect of running these -- CI therefore
# leaves one on the runner. And check-dockerfile pulls the pinned hadolint image,
# so it wants a registry the first time; the other three need no network.
# ---------------------------------------------------------------------------

.PHONY: check
check: check-compose check-hardfail check-dockerfile check-docs check-python ## Run every static check CI runs

.PHONY: limits
limits: ## Show the resource limits and totals, computed from the resolved config
	@python3 scripts/show_limits.py

.PHONY: check-python
check-python: ## Lint the helper scripts (config in ruff.toml)
	ruff check scripts/
	ruff format --check scripts/

.PHONY: check-compose
check-compose: ## Resolve compose on the registry, dev and live paths, both Kafka backends
	@python3 scripts/check_compose.py

.PHONY: check-hardfail
check-hardfail: ## Assert an unpinned stack refuses to start instead of pulling `latest`
	@python3 scripts/check_hardfail.py

.PHONY: check-docs
check-docs: ## Assert every relative link in the docs set still resolves
	@python3 scripts/check_docs.py

# `--failure-threshold error` matches the house gate (hyperi-ci's
# quality/hadolint.py gates on error severity only). Warning/info/style still
# print, they just do not fail -- which is the point: the estate's routine noise
# (DL3008 apt-pin, DL4006 pipefail) is all warning-tier, so a threshold of
# `info` turns every one of those into a blocked build and pushes people towards
# blanket ignores. Suppressed findings tell you nothing; visible ones do.
#
# hadolint version tracks tools.hadolint in hyperi-ci's config/versions.yaml
# (currently v2.14.0). We additionally pin the DIGEST, which that SSoT does not
# yet do -- see hyperi-ci#66.
.PHONY: check-dockerfile
check-dockerfile: ## Lint the dev builder Dockerfile (hadolint gates on error severity)
	docker run --rm -i hadolint/hadolint:v2.14.0@sha256:27086352fd5e1907ea2b934eb1023f217c5ae087992eb59fde121dce9c9ff21e hadolint --failure-threshold error - < docker/dfe-rust-builder.Dockerfile

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

.PHONY: test-e2e
test-e2e: ## End-to-end test executor (pass test names via E2E_TESTS)
	@python3 ./scripts/test_e2e.py $(E2E_TESTS)

# ---------------------------------------------------------------------------
# Power-on self test
# Runs automatically after `make dev` / `make ci`. Opt OUT with
# DFE_POST_ENABLED=false -- a self test you have to remember to run is not one.
# Transport-agnostic: it injects at the ingest edge and reads ClickHouse, so it
# behaves identically on the kafka and kafka-less (grpc) profiles.
#
# `make post` itself exits non-zero on failure, so it is usable as a gate. The
# auto-run after dev/ci deliberately does NOT abort the target: it has been seen
# to fail on a clean-slate kafka-fetcher stack for reasons not yet isolated (the
# loader's topic resolver did not pick up default_land), and until that is
# understood it must not brick the primary start command. Wire it to fail the
# target once it is proven stable -- that is the intended end state, not this.
# ---------------------------------------------------------------------------

.PHONY: post
post: ## Prove the RUNNING stack moves data end to end (DFE_POST_ENABLED=false to skip)
	@python3 ./scripts/post.py

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

.PHONY: ps
ps: ## Show running containers
	docker compose ps

# Tears down across EVERY profile, not just the active one. It used to pass only
# the active profile's services, which meant switching profiles left the previous
# profile's containers running -- still holding host ports, and still answering
# health probes for a pipeline that was no longer wired up. Anything discovering
# services by probing then found the stray. Volumes survive; `make clean` is the
# one that deletes data.
.PHONY: down
down: ## Stop and remove containers across all profiles (keeps volumes)
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
