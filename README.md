# dfe-docker

Docker Compose deployment packaging for HyperI Data Fusion Engine.

## Start here: which of these are you?

Pick the one that describes what you are doing. They overlap, and more than one
can apply - read both if so.

| If you are doing this | It applies to you | Start with |
|---|---|---|
| Seeing whether DFE does what you need. A first look, a demo, a proof of concept. | **Evaluating** | [docs/evaluating.md](docs/evaluating.md) |
| Running this where other people depend on it - one box, an edge site, a partner deployment. Small is still production. | **Operating** | [docs/operating.md](docs/operating.md) |
| Working on dfe-receiver, dfe-loader, dfe-engine or another component, and using this stack as your test rig. | **Developing** | [docs/developing.md](docs/developing.md) |
| Working out why a stack is misbehaving - yours or someone else's. | **Troubleshooting** | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Standing a deployment up, or moving one to a newer certified stack. | **Deploying** | [docs/deploying.md](docs/deploying.md) |
| Asking what the stack reports about itself - health endpoints, self-telemetry, what a passing self test proves. | **Observability** | [docs/observability.md](docs/observability.md) |
| Wanting to know how the pieces fit together, before any of the above. | **Everyone** | [docs/architecture.md](docs/architecture.md) |

Two things worth knowing before you start, whichever you are:

- **A default stack runs with no authentication.** That is deliberate - auth is
  primarily a Kubernetes concern in DFE - so nothing here should be reachable by
  people you have not met. The opt-in `auth` profile gates the infra UIs
  (Kafbat, HyperDX) behind oauth2-proxy with a group check; the DFE UI and the
  ingest path stay open. [operating.md](docs/operating.md) spells out exactly
  what is exposed.
- **Compose is a supported production target for small environments**, not a toy.
  Kubernetes remains the primary path; this is the right answer for a single box,
  an edge site, or a partner deployment where a cluster is not justified.

## Architecture

See [docs/architecture.md](docs/architecture.md).

## Quickstart

### Prerequisites

- Docker and Docker Compose v2
- Python 3
- [`oras`](https://oras.land) - `make stack` reads the signed OCI stack-manifest
  through it. Not needed if you render from a local dfe-infra checkout instead
  (`DFE_INFRA_DIR`).

### Initialisation

DFE services accept environment variable overrides that are isolated per container (e.g. `DFE_ARCHIVER_DLQ_ENABLED=false`). To create the local env files from the committed templates:

```bash
make init
```

This creates `.env` from `.env.example` and `env/<service>.env` for every template found under `env.example/`. The `.env` file and the entire `env/` directory are gitignored meaning your local edits stay local. The committed templates (`.env.example` and everything under `env.example/`) are tracked, so edits there propagate to everyone.

Re-running `make init` is safe - existing files are never overwritten. It does two
things beyond the copy:

- **Generates secrets.** A random value is minted for `DFE_UI_NEXTAUTH_SECRET` and
  `HYPERDX_POSTGRES_PASSWORD`. An existing `.env` that predates a key is topped up,
  so upgrading does not break your checkout. Compose carries a sentinel default for
  both rather than hard-failing (a hard-fail would abort `make down` too); the
  power-on self test is what refuses to pass while a default is still in place.
- **Reports drift.** The copy is one-shot, so an `.env` created months ago never
  learns that `.env.example` grew a setting. A re-run lists the keys you are
  missing. It only reports - editing your `.env` is yours to do.

> **Note**: Anything explicitly listed in a service's `environment:` block in `docker-compose.yml` takes precedence over the same key in `env/<service>.env`. Use the per-service file for *new* overrides that the compose file does not already forward. For example: `LOG_LEVEL` is set by compose's `environment:` block, so adding `LOG_LEVEL=debug` to `env/fetcher.env` will have no effect - but `DFE_ARCHIVER_DLQ_ENABLED` is not forwarded by compose, so setting it in the per-service file will reach the container.

### 1. Registry mode (GHCR images)

The CI path *and* the small-environment production path - same images, same
command. See [Deployment posture](#deployment-posture) before running this
anywhere that is not a laptop.

```bash
make init   # Edit .env files as needed (versions, ports)
make stack VERSION=X.Y.Z   # Pin image versions from the DFE stack SSoT
make ci     # Uses active_profile from service_profiles.yaml
make down   # Stop everything
```

### 2. Dev mode (builds from component source)

Source comes from a managed git cache by default, or your own checkouts when
`DFE_SRC_ROOT` is set - see
[docs/developing.md](docs/developing.md#where-it-looks-for-your-source).

```bash
make init   # Edit .env files as needed (versions, ports)
make dev    # Uses active_profile from service_profiles.yaml
make down   # Stop everything
```

## Deployment posture

Compose covers laptop use AND is a supported production target for small
environments -- single box, edge, partner deployments. **Kubernetes stays the
primary path**: if both would work, choose Kubernetes.

What makes it safe to deploy is that it consumes the same image from the same
registry as the cluster path, pinned from the same stack SSoT. No promotion step,
no Compose-specific build.

Three things to read before running this anywhere real:

- **A default stack has no authentication.** Docker mode is god-mode by design,
  and `dfe-proxy` on `:3000` reverse-proxies straight to the engine API. Put it
  behind a VPN, a tunnel, a firewall, or an authenticating proxy - or arm the
  opt-in `auth` profile, which covers the infra UIs but not the DFE UI itself.
- **The defaults assume a laptop.** Port bindings, resource limits, Redpanda's
  developer mode and the generated secrets all want review.
- **Redpanda is BSL, not OSS.** `KAFKA_BACKEND=apache` is the Apache-2.0 path.

All three, in full, plus what an upgrade does to an existing stack:
[operating.md](docs/operating.md) and [deploying.md](docs/deploying.md).

## Service Profiles

Service selection is controlled by `service_profiles.yaml` at the repo root. Each profile declares a transport mode, which DFE services to start, and optionally its whole footprint - the `clickhouse`, `core`, `kafbat`, `hyperdx` and `otel` keys. The `active_profile` field is used to define the profile set and can be overridden with the `DFE_PROFILE` env var. The matching `.env` flags (`DFE_CLICKHOUSE_ENABLED`, `DFE_CORE_ENABLED`, `KAFBAT_ENABLED`, `DFE_HYPERDX_ENABLED`, `DFE_OTEL_ENABLED`) override the profile's keys.

For `kafka` transport profiles, the Kafka backend is selected via `KAFKA_BACKEND` (default `redpanda`).

```bash
make dev                         # Uses active_profile from service_profiles.yaml
DFE_PROFILE=grpc-full make dev   # Override profile
KAFKA_BACKEND=apache make dev    # Override Kafka backend
DFE_HYPERDX_ENABLED=1 make dev   # Also start the HyperDX observability stack
DFE_OTEL_ENABLED=1 make dev      # Also start the self-monitoring collector
```

To start only a subset of the resolved profile for a single invocation, pass `SERVICES` (space-separated). It also works with `make ci`. An empty `SERVICES` (the default) starts the whole profile; a name that is not part of the resolved stack is a hard error.

```bash
make dev SERVICES="dfe-loader dfe-ui"   # Start dev images of dfe-loader and dfe-ui only
make ci  SERVICES="dfe-loader dfe-ui"   # Same, against registry images
make dev LOCAL="dfe-engine dfe-ui"      # Build only these from source; the rest run the pinned registry images
```

### Application Profiles (service_profiles.yaml)

`slim` and `single` name a whole-stack shape and share their names with the
Kubernetes tier, so one deployment dial reads the same on both. `single` is the
complete stack. There is no `scale` - Compose cannot run an HA broker or a
ClickHouse cluster.

| Profile                           | Transport | dfe-archiver | dfe-fetcher | dfe-loader | dfe-receiver | dfe-transform-vrl | dfe-transform-vector |
|-----------------------------------|-----------|:------------:|:-----------:|:----------:|:------------:|:-----------------:|:--------------------:|
| `slim`                            | gRPC      |              |             |     X      |      X       |                   |                      |
| `single`                          | Kafka     |      X       |      X      |     X      |      X       |         X         |                      |
| `kafka-fetcher`                   | Kafka     |              |      X      |     X      |              |                   |                      |
| `kafka-full`                      | Kafka     |      X       |      X      |     X      |      X       |                   |                      |
| `kafka-full-transform-vrl`        | Kafka     |              |      X      |     X      |      X       |         X         |                      |
| `kafka-minimal`                   | Kafka     |              |             |     X      |              |                   |                      |
| `kafka-receiver`                  | Kafka     |              |             |     X      |      X       |                   |                      |
| `kafka-receiver-archiver`         | Kafka     |      X       |             |     X      |      X       |                   |                      |
| `kafka-receiver-transform-vector` | Kafka     |              |             |     X      |      X       |                   |           X          |
| `kafka-filebeat`                  | Kafka     |              |             |     X      |      X       |        X2         |                      |
| `kafka-filebeat-vector`           | Kafka     |              |             |     X      |      X       |                   |          X2          |
| `grpc-fetcher`                    | gRPC      |              |      X      |     X      |              |                   |                      |
| `grpc-full`                       | gRPC      |              |      X      |     X      |      X       |                   |                      |
| `grpc-minimal`                    | gRPC      |              |             |     X      |              |                   |                      |
| `grpc-receiver`                   | gRPC      |              |             |     X      |      X       |                   |                      |

`X2` is two instances of that app: the shared passthrough one, plus the filebeat
source's own with the bundled filebeat program.

### Infrastructure Profiles (docker-compose.yml)

| Profile          | Service             | Use Case                                                |
|------------------|---------------------|---------------------------------------------------------|
| `clickhouse`     | ClickHouse          | Local analytics store                                   |
| `kafka-apache`   | Apache Kafka KRaft  | Kafka transport (opt-in - Real-Kafka compat + fallback) |
| `kafka-redpanda` | Redpanda            | Kafka transport (default - fits 4 GB CI runners)        |
| `kafka-ui`       | Kafbat UI (:8081)   | Web UI for Kafka                                        |

Infrastructure profiles are activated automatically based on the selected application profile. The `kafka-ui` profile is on by default when transport is `kafka`. Opt out by setting `KAFBAT_ENABLED=false` in `.env`.

All `kafka-*` profiles are **mutually exclusive**. They expose the network alias `kafka` on the same host ports, so only one can run at a time. Downstream services and configs always address `kafka:9092` and work with either backend.

### DFE Core Components (Engine + UI)

| Component    | Port | Purpose                     |
|--------------|------|-----------------------------|
| `dfe-engine` | 8003 | Config + schema API backend |
| `dfe-ui`     | 3000 | Web console frontend        |

`dfe-engine` (config/schema API backend) and `dfe-ui` (web console frontend) are independent of the transport/infra profiles. They start alongside whichever profile is active and are toggled as a pair by env var `DFE_CORE_ENABLED` (defaults to `true`). Set this to `false` to run without the core components.

In `dev` mode, they build from each repo's own Dockerfile (not the shared Rust builder). Source comes via git into a managed cache by default, or from your own checkouts when `DFE_SRC_ROOT` is set -- see [docs/developing.md](docs/developing.md#where-it-looks-for-your-source).

## Make Commands

### Services

| Command            | Description                                                        |
|--------------------|--------------------------------------------------------------------|
| `make init`        | Create .env and per-service .env files from templates              |
| `make env-files`   | Assert every `env/<service>.env` exists, creating any the templates have gained |
| `make dev`         | Build local DFE images from source and start the stack (`LOCAL="..."` builds only those, rest pinned) |
| `make dev-build`   | Build local DFE images from source (no start)                      |
| `make ci`          | Pull and start infra and registry DFE images                       |
| `make ci-pull`     | Pull infra and registry DFE images (no start)                      |
| `make infra`       | Start infrastructure services                                      |
| `make ps`          | Show running containers                                            |
| `make down`        | Stop and remove containers across ALL profiles (keeps volumes; ignores `SERVICES`) |
| `make clean`       | Stop and remove all containers and volumes across every profile    |
| `make help`        | Show the help message                                              |

### Testing

| Command            | Description                                                       |
|--------------------|-------------------------------------------------------------------|
| `make test-e2e`    | End-to-end test executor                                          |

The e2e harness (`scripts/test_e2e.py`, config `tests/e2e/e2e-tests.yaml`) runs
the core data-path acceptance: POST known JSON at the ingest edge and assert the
row lands in the ClickHouse table. It is transport-agnostic - each test selects a
`service_profiles.yaml` profile, so the same assertion covers both the Kafka and
the direct-gRPC paths. Run a subset by name:

```
make test-e2e E2E_TESTS="simple-receiver-to-loader-grpc simple-fetcher-to-loader"
```

### Power-on self test

`make dev` and `make ci` finish by injecting a few uniquely marked events at the
ingest edge and tracing them through to ClickHouse. It proves the stack **moves
data** - not merely that containers started and ports answer.

```bash
make post                        # run it against an already-running stack
DFE_POST_ENABLED=false make ci   # opt out
```

It is opt-**out**. A self test you have to remember to run is not a self test.

Transport-agnostic: it posts to the ingest edge and reads ClickHouse, so it
behaves identically on the Kafka and kafka-less (gRPC) profiles. The edge it uses
comes from the resolved profile - receiver when the profile has one, otherwise
fetcher. On a profile with no ingest component at all (loader-only) it skips with
a stated reason rather than failing: there is nothing to prove end to end.

Two things it deliberately is not. It is not a Docker `HEALTHCHECK` - that runs
inside one container with no view of the others, and repeats forever, so it would
re-inject test data into a production ingest path on a timer. And it is not baked
into the component images - this repo does not own those, and a per-component
dependency preflight (`config-check`-style: can I reach ClickHouse, does my table
exist, is my topic there) belongs in the scalo deployment contract, not here.

### The two default acceptance tests

The DFE stack ships two default "it is working" e2e tests: (1) the core data path
and (2) self-monitoring - the stack's own telemetry landing in the
`dfe.otel_*` tables. Both run here. Test (2) needs a profile that declares
`otel`, which `single` does, and `make post` makes the same pair of claims
against an already-running stack. See
[observability.md](docs/observability.md) for which services report.

### Shared dev hosts (port collision)

Kafka-transport tests start a broker that publishes to host ports `9092` and
`19092`. On a shared host already running a Kafka/Redpanda on `9092` (e.g. an
always-on dev daemon) that clashes. Two clean ways to run without the clash:

- Remap the host ports. The harness only reaches the broker over the docker
  network alias `kafka:9092`, so any free host port works:

  ```
  make test-e2e KAFKA_PLAINTEXT_PORT=29092 KAFKA_PLAINTEXT_HOST_PORT=29192
  ```

- Or run the gRPC-only tests, which need no broker at all:

  ```
  make test-e2e E2E_TESTS="simple-receiver-to-loader-grpc simple-fetcher-to-loader"
  ```

## Configuration

Config files live under `config/<component>/` and are self-documenting - browse
the directory. Which one each service mounts is the profile's call.

Every environment variable, the port map and the image list are in
[docs/configuration.md](docs/configuration.md).

## Licence

BUSL-1.1 - see [LICENSE](LICENSE) for details.
