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
# External data location: DFE_DATA_ROOT (env, else .env) rehomes every stateful
# volume onto that path via docker-compose.storage.yml. STORAGE_FLAGS carries it
# onto the explicit `-f` targets (ci, ci-pull), which must relocate storage too.
DFE_DATA_ROOT ?= $(shell sed -n 's/^DFE_DATA_ROOT=//p' .env 2>/dev/null)
ifneq ($(strip $(DFE_DATA_ROOT)),)
    export DFE_DATA_ROOT
    STORAGE_CHAIN := :docker-compose.storage.yml
    STORAGE_FLAGS := -f docker-compose.storage.yml
endif

# Goals that work without a resolved service profile. The check-* targets belong
# here because their whole point is running on a fresh checkout -- resolving a
# profile first would make them fail for the reason they exist to detect.
#
# down/clean belong here too: neither references PROFILE_FLAGS or ACTIVE_SERVICES
# any more (both sweep every profile), and requiring a resolvable profile to STOP
# a stack is the same lockout the compose secret comments argue against -- set
# DFE_PROFILE to something that does not exist and you could not tear down.
BOOTSTRAP_GOALS := init env-files help login stack dial modes down clean limits check check-compose check-hardfail check-dockerfile check-docs check-python

# Resolve the active profile only when a goal actually needs the compose stack
ifneq (,$(filter-out $(BOOTSTRAP_GOALS),$(or $(MAKECMDGOALS),help)))
    include .profile.mk
    SERVICES ?=
    ifeq ($(strip $(SERVICES)),)
        ACTIVE_SERVICES := $(DFE_SERVICES)
    else
        # On a fresh checkout make parses once before .profile.mk exists, then
        # remakes it and parses again; the name check only means anything on
        # the second pass, when DFE_SERVICES is populated.
        ifneq ($(strip $(DFE_SERVICES)),)
            INVALID_SERVICES := $(filter-out $(DFE_SERVICES),$(SERVICES))
            ifneq ($(INVALID_SERVICES),)
                $(error 'SERVICES' contains names not in the resolved stack: $(INVALID_SERVICES). Available: $(DFE_SERVICES))
            endif
        endif
        ACTIVE_SERVICES := $(filter $(SERVICES),$(DFE_SERVICES))
    endif
    # `make dev LOCAL="dfe-engine dfe-ui"` builds only those and keeps the rest
    # on the registry pins: the build writes docker-compose.local.yml for the
    # built components and the compose call names its files explicitly, which
    # keeps the auto-loaded all-local override out. Empty LOCAL is plain dev.
    LOCAL ?=
    LOCAL_OVERLAY := docker-compose.local.yml
    ifeq ($(strip $(LOCAL)),)
        DEV_BUILD := $(ACTIVE_SERVICES)
        DEV_PULL_FLAGS :=
        DEV_FLAGS :=
        DEV_OVERLAY_ARG :=
    else
        # Validated against the buildable set rather than the resolved stack: a
        # name the builder skips builds nothing and leaves nothing to overlay.
        ifneq ($(strip $(DFE_BUILDABLE_SERVICES)),)
            UNBUILDABLE_LOCAL := $(filter-out $(DFE_BUILDABLE_SERVICES),$(LOCAL))
            ifneq ($(UNBUILDABLE_LOCAL),)
                $(error 'LOCAL' contains names this repo cannot build from source: $(UNBUILDABLE_LOCAL). Buildable: $(DFE_BUILDABLE_SERVICES))
            endif
        endif
        ifneq ($(strip $(DFE_SERVICES)),)
            INVALID_LOCAL := $(filter-out $(DFE_SERVICES),$(LOCAL))
            ifneq ($(INVALID_LOCAL),)
                $(error 'LOCAL' contains names not in the resolved stack: $(INVALID_LOCAL). Available: $(DFE_SERVICES))
            endif
        endif
        DEV_BUILD := $(LOCAL)
        # The pull runs before the build writes the overlay, so it names no
        # overlay and resolves every image to its registry pin.
        DEV_PULL_FLAGS := -f docker-compose.yml $(STORAGE_FLAGS) $(UI_FLAGS)
        # The local overlay takes the committed override's slot, keeping the
        # default chain's relative order so `LIVE=1` resolves the engine volumes
        # the same way with and without LOCAL.
        DEV_FLAGS := -f docker-compose.yml -f $(LOCAL_OVERLAY)
        ifneq ($(strip $(LIVE)),)
            DEV_FLAGS += -f docker-compose.live.yml
        endif
        DEV_FLAGS += $(STORAGE_FLAGS) $(UI_FLAGS)
        DEV_OVERLAY_ARG := --overlay $(LOCAL_OVERLAY)
    endif
endif

# ---------------------------------------------------------------------------
# Web UI exposure
#
# Two dials, because "external" means different things on the two boxes compose
# deploys to. DFE_BIND_SCOPE=localhost (the default, a developer workstation)
# publishes every UI on 127.0.0.1 so the host sees them and the LAN does not;
# DFE_BIND_SCOPE=all publishes on 0.0.0.0 for a small/VM deploy. It resolves to
# DFE_UI_BIND_HOST, the address compose interpolates into every UI port mapping.
# Ingest (DFE_INGRESS_BIND_HOST) and the operator ports (DFE_BIND_HOST) keep
# their own audiences and are untouched by it.
#
# Every UI publishes by default. An opt-out drops that service's ports mapping,
# which compose merging cannot do by override -- hence a `!reset` fragment per
# UI, chained here the same way STORAGE_CHAIN chains the storage overlay.
# DFE_INFRA_UIS_EXTERNAL=false is the kill switch: it unpublishes every
# INFRA-class UI at once and beats their individual flags. dfe-ui and the engine
# API are PRODUCT class and answer only to their own.
#
# Below the profile block because DFE_AUTH_RESOLVED comes from .profile.mk, so
# the profile key and the env var can never disagree about whether the auth
# proxies are in play. The bootstrap goals skip that include and read it empty,
# which is the auth-off branch -- the safe direction for a target that only
# stops a stack or checks a file.
# ---------------------------------------------------------------------------
DFE_BIND_SCOPE ?= localhost
ifeq ($(strip $(DFE_BIND_SCOPE)),localhost)
    export DFE_UI_BIND_HOST := 127.0.0.1
else ifeq ($(strip $(DFE_BIND_SCOPE)),all)
    export DFE_UI_BIND_HOST := 0.0.0.0
else
    $(error DFE_BIND_SCOPE must be `localhost` or `all`, got '$(strip $(DFE_BIND_SCOPE))')
endif

DFE_INFRA_UIS_EXTERNAL ?= true
DFE_UI_EXTERNAL ?= true
DFE_ENGINE_API_EXTERNAL ?= true
DFE_KAFBAT_UI_EXTERNAL ?= true
DFE_HYPERDX_UI_EXTERNAL ?= true

# A typo in an exposure flag must fail the run rather than silently pick a side.
EXPOSURE_FLAGS := DFE_INFRA_UIS_EXTERNAL DFE_UI_EXTERNAL DFE_ENGINE_API_EXTERNAL DFE_KAFBAT_UI_EXTERNAL DFE_HYPERDX_UI_EXTERNAL
BAD_EXPOSURE := $(strip $(foreach flag,$(EXPOSURE_FLAGS),$(if $(filter-out true false,$(strip $($(flag)))),$(flag)='$(strip $($(flag)))')))
ifneq ($(BAD_EXPOSURE),)
    $(error exposure flags must be `true` or `false`: $(BAD_EXPOSURE))
endif

# An infra UI is gated when the kill switch is off OR its own flag is false.
KAFBAT_GATED := $(filter-out truetrue,$(strip $(DFE_INFRA_UIS_EXTERNAL))$(strip $(DFE_KAFBAT_UI_EXTERNAL)))
HYPERDX_GATED := $(filter-out truetrue,$(strip $(DFE_INFRA_UIS_EXTERNAL))$(strip $(DFE_HYPERDX_UI_EXTERNAL)))

# One fragment onto both chains: COMPOSE_FILE for plain `docker compose` calls,
# and the explicit -f list for the targets that pass files themselves.
define chain_fragment
UI_CHAIN := $$(UI_CHAIN):$(1)
UI_FLAGS := $$(UI_FLAGS) -f $(1)
endef

UI_CHAIN :=
UI_FLAGS :=
ifeq ($(strip $(DFE_UI_EXTERNAL)),false)
    $(eval $(call chain_fragment,docker-compose.unpublish-dfe-ui.yml))
endif
ifeq ($(strip $(DFE_ENGINE_API_EXTERNAL)),false)
    $(eval $(call chain_fragment,docker-compose.unpublish-engine-api.yml))
endif

# Infra class. With the auth profile armed each infra UI moves BEHIND its
# oauth2-proxy, so its own port is unpublished either way and the gate lands on
# the proxy instead.
ifeq ($(strip $(DFE_AUTH_RESOLVED)),true)
    $(eval $(call chain_fragment,docker-compose.unpublish-kafbat.yml))
    $(eval $(call chain_fragment,docker-compose.unpublish-hyperdx.yml))
    ifneq ($(KAFBAT_GATED),)
        $(eval $(call chain_fragment,docker-compose.unpublish-auth-kafbat.yml))
    endif
    ifneq ($(HYPERDX_GATED),)
        $(eval $(call chain_fragment,docker-compose.unpublish-auth-hyperdx.yml))
    endif
else
    ifneq ($(KAFBAT_GATED),)
        $(eval $(call chain_fragment,docker-compose.unpublish-kafbat.yml))
    endif
    ifneq ($(HYPERDX_GATED),)
        $(eval $(call chain_fragment,docker-compose.unpublish-hyperdx.yml))
    endif
endif

ifneq ($(strip $(LIVE)),)
    export COMPOSE_FILE := docker-compose.yml:docker-compose.override.yml:docker-compose.live.yml$(STORAGE_CHAIN)$(UI_CHAIN)
else ifneq ($(strip $(STORAGE_CHAIN))$(strip $(UI_CHAIN)),)
    export COMPOSE_FILE := docker-compose.yml:docker-compose.override.yml$(STORAGE_CHAIN)$(UI_CHAIN)
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

# Start targets require every per-service env file (dfe-ui reads INTERNAL_API_URL
# from env/ui.env). Compose marks them optional so `make down` never needs them.
# The guard creates what is missing (init is non-destructive) rather than refusing,
# so a release that adds a template does not stop an initialised deployment.
.PHONY: env-files
env-files: ## Assert every env/<service>.env exists, creating any the templates have gained
	@python3 scripts/env_files.py

# GHCR auth for the private dfe-* images and the signed stack-manifest. A no-op
# when DFE_GHCR_* are unset (a daemon authed out of band), so it is safe as an
# unconditional prerequisite. The helper reads .env itself and pipes the token on
# stdin -- it never reaches a make variable, so it stays out of the process list.
.PHONY: login
login: ## Authenticate docker + oras to the image registry from .env (DFE_GHCR_USERNAME/TOKEN)
	@python3 scripts/ghcr_login.py

# `make stack VERSION=X.Y.Z` pins the certified set. `make dial` writes the dial's
# version.pin to DFE_STACK_VERSION in .env, so `make dial && make stack` pins
# straight from the deployment dial. An explicit VERSION= on the command line wins.
VERSION ?= $(DFE_STACK_VERSION)

.PHONY: stack
stack: .env login ## Pin image versions into .env from the DFE stack SSoT (VERSION=X.Y.Z[-rc.N])
	@python3 scripts/stack.py $(VERSION)

# The deployment dial (deployment.yaml) is the single SSoT a deployment turns.
# This renders its docker-vm slice into .env; `make init` still mints the secrets,
# the dial merges the deploy-controlled keys over them. Canonical superset lives
# in dfe-infra; a lone dfe-docker clone deploys from deployment.yaml alone.
.PHONY: dial
dial: .env ## Render deployment.yaml (the deployment dial) into .env -- docker-vm slice
	@python3 scripts/render_dial.py

# The two deploy-currency modes, stated in one place: PINNED (make stack + make
# ci) vs TRACK-LATEST (the ops/daemon-update timer). Reads local files only, so
# it is safe on a fresh checkout; the live latest-vs-applied number comes from
# `self_update.py --dry-run`, which this points at.
.PHONY: modes
modes: ## Show the deploy modes -- pinned vs track-latest -- and which one this checkout uses
	@python3 scripts/show_modes.py

# ---------------------------------------------------------------------------
# Dev (builds from local source via docker-compose.override.yml)
# ---------------------------------------------------------------------------

# The bind directories must exist before the local volume driver mounts them.
.PHONY: storage-dirs
storage-dirs:
ifneq ($(strip $(DFE_DATA_ROOT)),)
	@mkdir -p $(addprefix $(DFE_DATA_ROOT)/,clickhouse kafka-redpanda kafka-apache archiver dlq-spool engine-config engine-schemas hyperdx-pg)
endif

.PHONY: dev
dev: env-files down storage-dirs ## Build local DFE images from source and start the dev stack (LOCAL="svc ..." builds only those, rest pinned)
	docker compose $(DEV_PULL_FLAGS) $(PROFILE_FLAGS) pull
	python3 scripts/build_dev_images.py $(DEV_OVERLAY_ARG) $(DEV_BUILD)
	docker compose $(DEV_FLAGS) $(PROFILE_FLAGS) up -d $(ACTIVE_SERVICES)
	@$(MAKE) --no-print-directory post || { echo "post: SELF TEST FAILED -- the stack is up, but it did not prove it moves data. Run 'make post' for detail."; exit 1; }

.PHONY: dev-build
dev-build: ## Build local DFE images from source (no start; honours LOCAL)
	docker compose $(DEV_PULL_FLAGS) $(PROFILE_FLAGS) pull
	python3 scripts/build_dev_images.py $(DEV_OVERLAY_ARG) $(DEV_BUILD)

# ---------------------------------------------------------------------------
# CI / registry images (skips docker-compose.override.yml)
# ---------------------------------------------------------------------------

.PHONY: ci
ci: login env-files down storage-dirs  ## Pull and start infra and registry DFE images
	docker compose -f docker-compose.yml $(STORAGE_FLAGS) $(UI_FLAGS) $(PROFILE_FLAGS) pull
	docker compose -f docker-compose.yml $(STORAGE_FLAGS) $(UI_FLAGS) $(PROFILE_FLAGS) pull $(ACTIVE_SERVICES)
	docker compose -f docker-compose.yml $(STORAGE_FLAGS) $(UI_FLAGS) $(PROFILE_FLAGS) up -d $(ACTIVE_SERVICES)
	@$(MAKE) --no-print-directory post || { echo "post: SELF TEST FAILED -- the stack is up, but it did not prove it moves data. Run 'make post' for detail."; exit 1; }

.PHONY: ci-pull
ci-pull: login ## Pull infra and registry DFE images
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) pull
	docker compose -f docker-compose.yml $(PROFILE_FLAGS) pull $(ACTIVE_SERVICES)

# ---------------------------------------------------------------------------
# Infrastructure only (Kafka + ClickHouse)
# ---------------------------------------------------------------------------

.PHONY: infra
infra: storage-dirs ## Start infrastructure services
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

# Pinned so a new ruff release cannot turn this red on an unrelated change, and
# so a local run lints with the SAME version CI does. CI installs the pin itself
# and sets RUFF=ruff; override the same way if you have no uvx.
RUFF_VERSION := 0.15.22
RUFF ?= uvx ruff@$(RUFF_VERSION)

.PHONY: print-ruff-version
print-ruff-version:
	@echo $(RUFF_VERSION)

.PHONY: check-python
check-python: ## Lint the helper scripts (config in ruff.toml)
	$(RUFF) check scripts/ ops/
	$(RUFF) format --check scripts/ ops/

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
