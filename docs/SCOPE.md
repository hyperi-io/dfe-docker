<!--
  Project:      dfe-docker
  File:         docs/SCOPE.md
  Purpose:      Project scope derived from DFE 2.2 rework architecture
  Language:     Markdown

  License:      BUSL-1.1
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# DFE Docker - Project Scope

## Overview

DFE 2.2 defines two deployment modes for the Data Fusion Engine: a single-machine **Docker** deployment and a scalable **Kubernetes** deployment.

This repository (`dfe-docker`) implements the Docker deployment - a self-contained Docker Compose stack shipped as the `hyperi-dfe` package.

The Kubernetes deployment is a separate concern and is out of scope for this repository.

## Architecture

```mermaid
flowchart LR
	dfe_archiver["dfe-archiver (rust)"]
	dfe_fetcher["dfe-fetcher (rust)"]
	dfe_loader["dfe-loader (rust)"]
	dfe_receiver["dfe-receiver (rust)"]
	dfe_t_elastic["dfe-transform-elastic (rust)"]
	dfe_t_splack["dfe-transform-splack (rust)"]
	dfe_t_vector["dfe-transform-vector (rust)"]
	dfe_t_vrl["dfe-transform-vrl (rust)"]
	dfe_t_wasm["dfe-transform-wasm (rust)"]

	clickhouse["ClickHouse"]
	kafka_land["Kafka (_land topic)"]
	kafka_load["Kafka (_load topic)"]

	c_dfe_transport{"DFE_TRANSPORT"}
	c_load_topic_exists{"_load Topic Exists"}

	subgraph source_components [Source Components]
		dfe_fetcher
		dfe_receiver
	end

	subgraph transform_components [Transform Components]
		direction TB
		dfe_t_elastic ~~~ dfe_t_splack ~~~ dfe_t_vector ~~~ dfe_t_vrl ~~~ dfe_t_wasm
	end

	subgraph sink_components [Sink Components]
		dfe_archiver
		clickhouse
	end

	dfe_receiver --> c_dfe_transport
	c_dfe_transport -- grpc --> dfe_loader
	c_dfe_transport -- kafka --> kafka_land
	dfe_fetcher --> kafka_land

	kafka_land --> c_load_topic_exists
	c_load_topic_exists -- No --> dfe_archiver
	c_load_topic_exists -- No --> dfe_loader
	c_load_topic_exists -- Yes --> transform_components
	transform_components --> kafka_load
	kafka_load --> dfe_loader

	dfe_loader --> clickhouse
```

## Components

| Component                 | Internal/External | Description                   | Supports                                                                      |
|---------------------------|-------------------|-------------------------------|-------------------------------------------------------------------------------|
| **dfe-archiver**          | Internal          | Archive sink                  | filesystem, MinIO, S3, Azure Blob, GCS                                        |
| **dfe-fetcher**           | Internal          | Pulls events from APIs        | AWS, Azure, Microsoft 365, GCP, Vector.dev, container extractors, HTTP ingest |
| **dfe-loader**            | Internal          | Loads events into ClickHouse  | -                                                                             |
| **dfe-receiver**          | Internal          | Ingress                       | HTTP, gRPC/Vector, OTLP, Loki, Beats, Splunk HEC                              |
| **dfe-transform-elastic** | Internal          | Elastic Stack transform       | Beats, Elastic Agent, ECS, GeoIP                                              |
| **dfe-transform-splack**  | Internal          | Splunk-equivalent transform   | syslog, CEF, LEEF, Windows Event XML                                          |
| **dfe-transform-vector**  | Internal          | Vector.dev subprocess wrapper | lua, aggregate, dedupe, throttle, route                                       |
| **dfe-transform-vrl**     | Internal          | Embedded VRL engine           | -                                                                             |
| **dfe-transform-wasm**    | Internal          | WebAssembly transform host    | Rust, Go (TinyGo), AssemblyScript                                             |
| **ClickHouse**            | External          | Columnar analytics store      | Docker container or external (e.g. ClickHouse Cloud)                          |
| **Kafka**                 | External          | Event streaming platform      | -                                                                             |

## Source Repositories

When running dfe-docker in `dev` mode, source repositories are required to be cloned under `$PROJECTS_PATH`:

| Repository                | Language | Description                                                               |
|---------------------------|----------|---------------------------------------------------------------------------|
| **clickhouse-arrow**      | Rust     | HyperI fork of the arrow based ClickHouse client                          |
| **clickhouse-rs**         | Rust     | HyperI fork of the clickhouse-rs Rust client                              |
| **dfe-archiver**          | Rust     | Archive sink (filesystem, S3, MinIO, GCS, Azure Blob)                     |
| **dfe-fetcher**           | Rust     | Cursor-based puller for AWS, Azure, M365, GCP audit/log APIs              |
| **dfe-loader**            | Rust     | ClickHouse loader                                                         |
| **dfe-receiver**          | Rust     | Event receiver / ingress (HTTP, gRPC/Vector, OTLP, Loki, Beats, HEC)      |
| **dfe-transform-elastic** | Rust     | Elastic Stack ingest pipeline transform (Beats, Agent, ECS, GeoIP)        |
| **dfe-transform-splack**  | Rust     | Splunk-equivalent transform (syslog, CEF, LEEF, Windows Event XML)        |
| **dfe-transform-vector**  | Rust     | Vector.dev subprocess wrapper for Kafka-to-Kafka transforms               |
| **dfe-transform-vrl**     | Rust     | Embedded VRL transform engine (no Vector subprocess)                      |
| **dfe-transform-wasm**    | Rust     | Wasmtime host for user-supplied WebAssembly transform modules             |
| **hyperi-rustlib**        | Rust     | Shared internal library (config, logging, metrics, transport, resilience) |

## Compiled Binaries and Artifacts

DFE components are compiled to native Rust binaries, packaged in multi-arch Docker images and published to GHCR (ghcr.io/hyperi-io).

| Component                 | Image Status | Location                                |
|---------------------------|--------------|-----------------------------------------|
| **dfe-archiver**          | Published    | ghcr.io/hyperi-io/dfe-archiver          |
| **dfe-fetcher**           | Published    | ghcr.io/hyperi-io/dfe-fetcher           |
| **dfe-loader**            | Published    | ghcr.io/hyperi-io/dfe-loader            |
| **dfe-receiver**          | Published    | ghcr.io/hyperi-io/dfe-receiver          |
| **dfe-transform-elastic** | In Progress  | ghcr.io/hyperi-io/dfe-transform-elastic |
| **dfe-transform-splack**  | In Progress  | ghcr.io/hyperi-io/dfe-transform-splack  |
| **dfe-transform-vector**  | In Progress  | ghcr.io/hyperi-io/dfe-transform-vector  |
| **dfe-transform-vrl**     | Published    | ghcr.io/hyperi-io/dfe-transform-vrl     |
| **dfe-transform-wasm**    | In Progress  | ghcr.io/hyperi-io/dfe-transform-wasm    |
