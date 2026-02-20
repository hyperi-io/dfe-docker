<!--
  Project:      DFE Docker
  File:         docs/SCOPE.md
  Purpose:      Project scope derived from DFE 2.2 rework architecture
  Language:     Markdown

  License:      FSL-1.1-ALv2
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# DFE Docker - Project Scope

## Overview

DFE 2.2 defines two deployment modes for the Data Forwarding Engine: a
single-machine **Docker** deployment and a scalable **Kubernetes** deployment.
This repository (`dfe-docker`) implements the Docker deployment — a
self-contained Docker Compose stack shipped as the `hyperi-dfe` package.

The Kubernetes deployment is a separate concern and is out of scope for this
repository.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  hyperi-dfe  (Docker Compose)                                       │
│                                                                     │
│  ┌───────────────┐   Zenoh IPC   ┌─────────────┐   ┌────────────┐ │
│  │ EdgeStream Hub│──────────────▶│dfe-receiver │──▶│ dfe-loader │ │
│  │  (ingest)     │               │  (Rust)     │   │  (Rust)    │ │
│  └───────────────┘               └─────────────┘   └─────┬──────┘ │
│                                                           │        │
│                                                           ▼        │
│  ┌─────────────┐    ┌──────────────────┐          ┌────────────┐  │
│  │  HyperDX    │    │ DFE Control Plane│          │ ClickHouse │  │
│  │ (observ.)   │    │   (Python)       │          │            │  │
│  └─────────────┘    └──────────────────┘          └────────────┘  │
│                              │                                     │
│                     ┌────────┴───────┐                             │
│                     │    DFE UI      │                             │
│                     │ (TypeScript)   │                             │
│                     └────────────────┘                             │
│                                                                     │
│  Config: YAML only (Postgres removed)                               │
│  Auth:   None (god-mode)                                            │
└─────────────────────────────────────────────────────────────────────┘
```

## Components

| Component | Language / Runtime | Notes |
|---|---|---|
| **EdgeStream Hub** | — | Data ingest entry point |
| **dfe-receiver** | Rust (native) | Receives events from EdgeStream Hub via Zenoh IPC |
| **dfe-loader** | Rust (native) | Loads processed events into ClickHouse |
| **ClickHouse** | — | Columnar analytics store |
| **HyperDX** | — | Observability UI; replaces Grafana and Discovery |
| **DFE Control Plane** | Python | Includes dfe-engine; orchestrates pipeline behaviour |
| **DFE UI** | TypeScript | Management and configuration interface |
| **YAML Config** | YAML | All configuration is YAML; Postgres has been removed |

## Source Repositories

Each DFE component lives in its own repository under `/projects/`:

| Component | Repository | Language | Description |
|---|---|---|---|
| **dfe-receiver** | `/projects/dfe-receiver` | Rust | Event receiver, Zenoh IPC ingest |
| **dfe-loader** | `/projects/dfe-loader` | Rust | ClickHouse loader |
| **dfe-archiver** | `/projects/dfe-archiver` | Rust | High-volume Kafka-to-storage archiver |
| **dfe-control-plane** | `/projects/dfe-control-plane` | Python | Pipeline orchestration and API |
| **dfe-engine** | `/projects/dfe-engine` | Python | Core processing engine (used by control plane) |
| **dfe-docker** | `/projects/dfe-docker` | Docker Compose | This repository — deployment packaging |
| **dfe-developer** | `/projects/dfe-developer` | Ansible / shell | Developer environment tooling |

## Compiled Binaries and Artifacts

Rust components are compiled to native binaries and published to **JFrog
Artifactory** (private). All components will be open-sourced publicly at a
later date.

| Component | Binary status | Artifact location |
|---|---|---|
| **dfe-loader** | Published | JFrog Artifactory |
| **dfe-archiver** | In progress | JFrog Artifactory (pending) |
| **dfe-receiver** | In progress | JFrog Artifactory (pending) |

## Key Design Decisions

- **Zenoh IPC** between dfe-receiver and dfe-loader — no message broker in Docker mode.
- **No authentication** — the Docker deployment runs in god-mode. Auth (OIDC + Envoy) is a Kubernetes-only concern.
- **Postgres removed** — all configuration is file-based YAML.
- **HyperDX replaces Grafana and Discovery** as the unified observability layer.
- **Native Rust** for both dfe-receiver and dfe-loader in Docker mode.

## Out of Scope (Kubernetes Deployment)

The following are part of the DFE 2.2 Kubernetes deployment and are **not**
covered by this repository:

| Concern | Kubernetes approach |
|---|---|
| Message broker | Strimzi Kafka, AutoMQ, Confluent, or MSK |
| Transforms | dfe-transforms (Rust); Vector with WASM planned |
| Loader runtime | Vector (WASM later) instead of native Rust |
| ClickHouse hosting | Operator-managed or cloud-managed |
| Autoscaling | Keda-managed pods |
| Auth | OIDC + Envoy (replaces nginx ingress and oauth2-proxy) |
| Metrics | OpenTelemetry (replaces Prometheus) |
| Deployment | Terraform + Helm + Argo CD + Keda; on-prem and AWS |
| Config management | Git-managed YAML |
