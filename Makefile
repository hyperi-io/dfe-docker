# Project:   dfe-docker
# File:      Makefile
# Purpose:   Convenience targets for Docker Compose stack
# Language:  Makefile
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

.DEFAULT_GOAL := help

# Read transport selection from .env (default: grpc)
-include .env
DFE_TRANSPORT ?= grpc

ifeq ($(DFE_TRANSPORT),kafka)
  TRANSPORT_PROFILES = --profile full-kafka --profile ui
else
  TRANSPORT_PROFILES = --profile full
endif

# ---------------------------------------------------------------------------
# Dev (builds from local source via docker-compose.override.yml)
# ---------------------------------------------------------------------------

.PHONY: dev
dev:
	docker compose $(TRANSPORT_PROFILES) up --build -d

.PHONY: build-local
build-local: ## Build images from local source
	docker compose $(TRANSPORT_PROFILES) build

.PHONY: dev-logs
dev-logs: ## Tail all service logs
	docker compose logs -f

# ---------------------------------------------------------------------------
# CI / registry images (skips docker-compose.override.yml)
# ---------------------------------------------------------------------------

.PHONY: ci-up
ci-up: ## Start stack using published registry images (no local build)
	docker compose down -v && docker compose -f docker-compose.yml $(TRANSPORT_PROFILES) up -d --build

.PHONY: pull
pull: ## Pull latest images from registry
	docker compose -f docker-compose.yml pull

# ---------------------------------------------------------------------------
# Infrastructure only (Kafka + ClickHouse)
# ---------------------------------------------------------------------------

.PHONY: infra
infra: ## Start infrastructure services (Kafka + ClickHouse)
	docker compose --profile infra up -d

.PHONY: infra-logs
infra-logs: ## Tail infrastructure logs
	docker compose --profile infra logs -f

# ---------------------------------------------------------------------------
# External ClickHouse (set CLICKHOUSE_HOST in .env, no Docker CH container)
# ---------------------------------------------------------------------------

.PHONY: up-ext
up-ext: ## Start stack using external ClickHouse (CLICKHOUSE_HOST must be set)
	docker compose --profile bare-bones up -d

# ---------------------------------------------------------------------------
# Debug mode (file sinks, no ClickHouse writes)
# Requires FileSink support in dfe-receiver + dfe-loader (pre-wired, pending impl)
# ---------------------------------------------------------------------------

.PHONY: debug
debug: ## Start debug stack (file sinks to /var/spool/dfe/debug/)
	docker compose --profile debug up -d

.PHONY: debug-logs
debug-logs: ## Tail debug stack logs
	docker compose --profile debug logs -f

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
test-e2e: ## End-to-end test executor
	./scripts/test-e2e.sh

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
down: ## Stop all services
	docker compose down

.PHONY: clean
clean: ## Stop all services and remove volumes
	docker compose down -v

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
