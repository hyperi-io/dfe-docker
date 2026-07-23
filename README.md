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
| Wanting to know how the pieces fit together, before any of the above. | **Everyone** | [ARCHITECTURE.md](ARCHITECTURE.md) |

Two things worth knowing before you start, whichever you are:

- **There is no authentication anywhere in this stack.** That is deliberate -
  auth is a Kubernetes concern in DFE - but it means nothing here should be
  reachable by people you have not met. [operating.md](docs/operating.md) spells
  out exactly what is exposed.
- **Compose is a supported production target for small environments**, not a toy.
  Kubernetes remains the primary path; this is the right answer for a single box,
  an edge site, or a partner deployment where a cluster is not justified.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md).

## Quickstart

### Prerequisites

- Docker and Docker Compose v2
- Python 3

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

### 2. Dev Mode (Requires Repositories Cloned Locally)

See [docs/developing.md](docs/developing.md#where-it-looks-for-your-source) for which
repositories are required and where the build expects to find them.

```bash
make init   # Edit .env files as needed (versions, ports)
make dev    # Uses active_profile from service_profiles.yaml
make down   # Stop everything
```

## Deployment posture

Compose here covers laptop use - trying the stack out, demos, mini-POCs - **and
it is a supported production deploy target for small environments**: single box,
edge, and partner deployments where standing up a cluster is not justified.

**Kubernetes remains the primary path.** Compose is not co-equal; it is the right
answer for a specific, smaller shape. If both would work, choose Kubernetes.

What makes it safe to deploy is that it consumes *the same image from the same
registry* as the cluster path. One image, one contract, two consumers. There is no
promotion step, and a separately hand-built image is not supported.

One exception, and it is a real one: `hyperi-hyperdx` is still on a floating
`:latest` because the fork is unpublished, so the stack SSoT cannot pin it. It is
the only image in the stack without a digest. Until that fork ships, the
opt-in `hyperdx` profile does not carry the pinning guarantee the rest of the
stack does.

### There is no authentication. Read this before exposing anything.

**Docker mode runs in god-mode by design.** Auth (OIDC + Envoy) is a Kubernetes-only
concern. That is a deliberate decision, not an oversight - but it sets a hard limit
on what "production" can mean here.

Concretely, and this survives the port-binding defaults below:

- `dfe-proxy` on `:3000` is bound `0.0.0.0` and reverse-proxies `/api/v1/*` straight
  through to the dfe-engine API. Loopback-binding the engine's own `:8003` does
  **not** protect that API - the same endpoints are reachable through the proxy,
  unauthenticated, by anyone who can route to the box.
- HyperDX runs with `NEXT_PUBLIC_IS_LOCAL_MODE=true` (no login), and its browser
  bundle is handed a ClickHouse connection - so **whatever you set
  `CLICKHOUSE_PASSWORD` to is served to every browser that loads the HyperDX app.**
  Setting a ClickHouse password does not make HyperDX safe to expose; it just moves
  the credential into a place more people can read it.
- That same connection is the reason remote HyperDX does not work out of the box:
  the browser is told to reach ClickHouse at `${DFE_HYPERDX_APP_URL}:8123`, but
  8123 now binds loopback on the server. Using HyperDX from another machine needs
  ClickHouse reachable from that machine too - which is the exposure above. The
  opt-in `hyperdx` profile is best treated as a localhost or tunnelled tool, and
  its app port binds loopback by default to match that advice.

So: put this behind something. A VPN, an SSH tunnel, a firewall, or an
authenticating reverse proxy in front of `:3000`. The bindings below reduce
the *accidental* surface; they are not an access-control mechanism.

### What the defaults assume

The defaults are tuned for a laptop. Four things to change for anything else.

**1. Host port exposure.** Ports bind by audience, not all on `0.0.0.0`:

| Variable | Default | Covers |
|---|---|---|
| `DFE_INGRESS_BIND_HOST` | `0.0.0.0` | What users reach: receiver ingest (6000/8080), fetcher ingest (8082), UI proxy (3000) |
| `DFE_BIND_HOST` | `127.0.0.1` | What operators reach: ClickHouse (8123/9000), Kafka (9092/19092), the `/metrics` ports (9090, 9091, 9093-9096), Vector's API (8686), loader gRPC (50051), engine API (8003), Kafka UI (8081), HyperDX API (8000) and app (8090) |

The HyperDX app (8090) is a browser surface but binds the *operator* variable,
because it is the one page that hands a ClickHouse credential to whoever loads it
(see above). Widening it is a deliberate act, not a default.

Set `DFE_BIND_HOST=0.0.0.0` only knowingly. One variable opens all of it at once,
including a ClickHouse that ships with **no password** (`CLICKHOUSE_PASSWORD`
defaults to empty) and `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`, and a Kafka UI
with dynamic config enabled. Set `CLICKHOUSE_PASSWORD` if you do - but see the auth
note above first, because HyperDX will then serve that password to browsers.

Note that this is not the whole story: `:3000` is bound `0.0.0.0` regardless, and
reaches the engine API. See the auth section above.

**2. Resource limits.** Every service declares `deploy.resources.limits`. The
defaults are sized so the *default profile* fits a 16 GB box **that is also running
an operating system and whatever else that box does** - 16 GB of RAM is not 16 GB
of headroom. Enabling every profile at once wants retuning rather than these
defaults.

```bash
make limits    # per-service limits and totals, computed from the resolved config
```

Deliberately a command, not a table: totals written into a document go stale the
first time a service changes tier, and a stale number people believe is worse than
no number.

Limits are ceilings, not reservations - an idle service costs nothing. Retune
`DFE_CLICKHOUSE_*`, `DFE_BROKER_*`, `DFE_SERVICE_*`, `DFE_SIDECAR_*` for your
target. An unlimited service on a single box is how one runaway container takes the
host down.

One caveat: Docker permits an equal amount of **swap** alongside a memory limit, so
a 3G limit can become 3G RAM + 3G swap on a swap-enabled host. Set `memswap_limit`
equal to the memory limit per service if you need a genuinely hard cap.

**3. Redpanda runs in developer mode.** `--mode=dev-container --smp=1
--memory=1G` fits a 4 GB CI runner and is not a production configuration. Set
`REDPANDA_MODE=production` with real `REDPANDA_SMP`/`REDPANDA_MEMORY`, and raise
`DFE_BROKER_MEMORY` above `REDPANDA_MEMORY` or the broker is OOM-killed rather
than backpressured.

**4. Secrets.** `make init` generates `DFE_UI_NEXTAUTH_SECRET` and
`HYPERDX_POSTGRES_PASSWORD`. Compose does **not** refuse to start without them - it
carries a sentinel default, because a hard-fail would also block `make down` for
services you may not run. `make post` is what fails while a default is in place, so
run it if you want that enforced. `CLICKHOUSE_PASSWORD` is **not** generated at all:
it defaults to empty and is yours to set. Do not commit `.env`.

### Upgrading an existing deployment

Two changes in this release will bite an existing stack. Neither is silent if you
read this; both are silent if you do not.

- **ClickHouse now uses a named volume.** Previously its data lived in the
  container's writable layer, which meant it died with `docker compose down`. It
  now mounts `clickhouse-data:/var/lib/clickhouse`. On first `make ci` after
  upgrading, the container is recreated with an **empty** volume - any data still
  sitting in the old writable layer is not migrated and will appear to vanish. If
  that data matters, export it before upgrading.
- **Published ports moved to loopback.** Most ports that were on `0.0.0.0` now
  bind `127.0.0.1` - everything except the ingest and UI surfaces. Anything reaching this box from elsewhere - a remote
  `clickhouse-client`, a Prometheus scrape of `:9090-9096`, a colleague's browser -
  will get `connection refused` with no hint as to why. Set `DFE_BIND_HOST=0.0.0.0`
  to restore the old behaviour, having read the auth note above.

Also note `make clean` removes volumes (`down -v`). It always did, but with
ClickHouse now on a named volume that means it deletes the warehouse.

### Kafka backend licensing

The default broker is **Redpanda, whose core is source-available under the BSL -
not OSS.** Local development and CI sit inside its Additional Use Grant. A partner
or edge deployment is a per-deployment licence check, and that check is a question
for a human, not an assumption.

If it does not come back clean, `KAFKA_BACKEND=apache` switches to Apache Kafka
(Apache-2.0). The two are mutually exclusive and share the `kafka:9092` network
alias, so nothing downstream changes. Each backend brings **its own** topic-init
service, so the Apache path never pulls or runs a BSL-licensed artefact - the
previous shared init service used the Redpanda image on both.

Be precise about what that does and does not buy you: no Redpanda image is
*pulled or run*, but `REDPANDA_VERSION` must still be **pinned** for the file to
resolve, because Compose interpolates every service before profiles filter
anything. A Redpanda pin in `.env` is not a Redpanda deployment, but if your
licence position requires zero reference to the artefact, that is the remaining
edge to clean up.

## Service Profiles

Service selection is controlled by `service_profiles.yaml` at the repo root. Each profile declares a transport mode and which DFE services to start. The `active_profile` field is used to define the profile set and can be overridden with the `DFE_PROFILE` env var.

For `kafka` transport profiles, the Kafka backend is selected via `KAFKA_BACKEND` (default `redpanda`).

```bash
make dev                         # Uses active_profile from service_profiles.yaml
DFE_PROFILE=grpc-full make dev   # Override profile
KAFKA_BACKEND=apache make dev    # Override Kafka backend
DFE_HYPERDX_ENABLED=1 make dev   # Also start the HyperDX observability stack
```

To start only a subset of the resolved profile for a single invocation, pass `SERVICES` (space-separated). It also works with `make ci`. An empty `SERVICES` (the default) starts the whole profile; a name that is not part of the resolved stack is a hard error.

```bash
make dev SERVICES="dfe-loader dfe-ui"   # Start dev images of dfe-loader and dfe-ui only
make ci  SERVICES="dfe-loader dfe-ui"   # Same, against registry images
```

### Application Profiles (service_profiles.yaml)

| Profile                           | Transport | dfe-archiver | dfe-fetcher | dfe-loader | dfe-receiver | dfe-transform-vrl | dfe-transform-vector |
|-----------------------------------|-----------|:------------:|:-----------:|:----------:|:------------:|:-----------------:|:--------------------:|
| `kafka-fetcher`                   | Kafka     |              |      X      |     X      |              |                   |                      |
| `kafka-full`                      | Kafka     |      X       |      X      |     X      |      X       |                   |                      |
| `kafka-full-transform-vrl`        | Kafka     |              |      X      |     X      |      X       |         X         |                      |
| `kafka-minimal`                   | Kafka     |              |             |     X      |              |                   |                      |
| `kafka-receiver`                  | Kafka     |              |             |     X      |      X       |                   |                      |
| `kafka-receiver-archiver`         | Kafka     |      X       |             |     X      |      X       |                   |                      |
| `kafka-receiver-transform-vector` | Kafka     |              |             |     X      |      X       |                   |           X          |
| `grpc-fetcher`                    | gRPC      |              |      X      |     X      |              |                   |                      |
| `grpc-full`                       | gRPC      |              |      X      |     X      |      X       |                   |                      |
| `grpc-minimal`                    | gRPC      |              |             |     X      |              |                   |                      |
| `grpc-receiver`                   | gRPC      |              |             |     X      |      X       |                   |                      |

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
| `make dev`         | Build local DFE images from source and start the stack             |
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
and (2) self-monitoring - the stack's own OTel logs+metrics landing in the otel
ClickHouse database. This docker harness implements test (1) only. The docker
profile ships no OTel collector and no otel ClickHouse database, so test (2)
cannot run here without wiring the profile does not carry; it lives in the k8s
bootstrap-smoke suite that dfe-infra runs post-deploy.

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

Config files live under `config/<component>/` and are self-documenting - browse the directory. The active config for each service is set by the selected profile in `service_profiles.yaml`, which can be overridden via the `DFE_PROFILE` environment variable.

## Environment Variables

See [.env.example](.env.example) for available overrides.

### Profile Selection

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_PROFILE`                                    | Override active profile from service_profiles.yaml                                   | -                                                                   |

### Image Registry

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DOCKER_DEFAULT_PLATFORM`                        | Image architecture to pull from docker (if not wanting automatic determination)      | -                                                                   |
| `IMAGE_REGISTRY`                                 | OCI registry hosting published `dfe-*` images                                        | `ghcr.io/hyperi-io`                                                 |

### Dev Builds

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_SRC_ROOT`                                   | Directory holding your local `dfe-*` checkouts; set = build (and `LIVE=1` mount) those instead of the git cache | unset (git cache)                                 |
| `DFE_SRC_REMOTE`                                 | Git base URL the managed cache clones component repos from                           | `https://github.com/hyperi-io`                                      |
| `DFE_SRC_REF`                                    | Branch, tag or commit the managed cache builds                                       | `main`                                                              |
| `DFE_SRC_CACHE`                                  | Location of the managed source cache                                                 | `~/.cache/dfe-docker/src`                                           |

### DFE Components - General

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `LOG_LEVEL`                                      | Log level (trace\|debug\|info\|warn\|error)                                          | `info`                                                              |
| `LOG_FORMAT`                                     | Log format                                                                           | `text`                                                              |

### DFE Archiver

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_ARCHIVER_VERSION`                           | Version of dfe-archiver to use                                                       | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_ARCHIVER_PROMETHEUS_PORT`                   | Archiver Prometheus port                                                             | `9093`                                                              |

### DFE Fetcher

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_FETCHER_VERSION`                            | Version of dfe-fetcher to use                                                        | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_FETCHER_INGEST_PORT`                        | Fetcher ingest port                                                                  | `8082`                                                              |
| `DFE_FETCHER_PROMETHEUS_PORT`                    | Fetcher Prometheus port                                                              | `9094`                                                              |
| `AWS_ACCESS_KEY_ID`                              | AWS access key. Goes in `env/fetcher.env`, NOT `.env` -- the fetcher config reads it via `env:`  | -                                    |
| `AWS_SECRET_ACCESS_KEY`                          | AWS secret key. Same file as above                                                   | -                                                                   |

The AWS **region** is not an environment variable: it is a literal in
`config/fetcher/aws-*.yaml` (`region: us-west-2`), because only the two
credential fields are `env:`-interpolated. Change it there.

### DFE Loader

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_LOADER_VERSION`                             | Version of dfe-loader to use                                                         | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_LOADER_PROMETHEUS_PORT`                     | Loader Prometheus port                                                               | `9091`                                                              |
| `DFE_LOADER_GRPC_PORT`                           | Loader gRPC port                                                                     | `50051`                                                             |

### DFE Receiver

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_RECEIVER_VERSION`                           | Version of dfe-receiver to use                                                       | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_RECEIVER_BEATS_PORT`                        | Receiver Beats port                                                                  | `5044`                                                              |
| `DFE_RECEIVER_GRPC_PORT`                         | Receiver gRPC port                                                                   | `6000`                                                              |
| `DFE_RECEIVER_HEC_PORT`                          | Receiver HEC port                                                                    | `8088`                                                              |
| `DFE_RECEIVER_HTTP_PORT`                         | Receiver HTTP port                                                                   | `8080`                                                              |
| `DFE_RECEIVER_OTLP_GRPC_PORT`                    | Receiver OTLP gRPC port                                                              | `4317`                                                              |
| `DFE_RECEIVER_OTLP_HTTP_PORT`                    | Receiver OTLP HTTP port                                                              | `4318`                                                              |
| `DFE_RECEIVER_PROMETHEUS_PORT`                   | Receiver Prometheus port                                                             | `9090`                                                              |

### DFE Transform Vector

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_TRANSFORM_VECTOR_VERSION`                   | Version of dfe-transform-vector to use                                               | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_TRANSFORM_VECTOR_PROMETHEUS_PORT`           | Transform Vector Prometheus port                                                     | `9095`                                                              |
| `DFE_TRANSFORM_VECTOR_API_PORT`                  | Transform Vector API port                                                            | `8686`                                                              |

### DFE Transform VRL

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_TRANSFORM_VRL_VERSION`                      | Version of dfe-transform-vrl to use                                                  | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_TRANSFORM_VRL_PROMETHEUS_PORT`              | Transform VRL Prometheus port                                                        | `9096`                                                              |

### ClickHouse

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `CLICKHOUSE_VERSION`                             | Version of ClickHouse to use                                                         | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |
| `CLICKHOUSE_HOST`                                | External ClickHouse host (skips Docker container)                                    | `clickhouse`                                                        |
| `CLICKHOUSE_HTTP_PORT`                           | ClickHouse HTTP port                                                                 | `8123`                                                              |
| `CLICKHOUSE_NATIVE_PORT`                         | ClickHouse native protocol port                                                      | `9000`                                                              |
| `CLICKHOUSE_DB`                                  | ClickHouse initialisation database                                                   | `default`                                                           |
| `CLICKHOUSE_USERNAME`                            | ClickHouse username to connect with                                                  | `default`                                                           |
| `CLICKHOUSE_PASSWORD`                            | ClickHouse password associated to user                                               | -                                                                   |

### Kafka - General

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `KAFKA_BACKEND`                                  | Kafka backend for `kafka` transport profiles                                         | `redpanda`                                                          |
| `KAFKA_PLAINTEXT_HOST_PORT`                      | Kafka plaintext host port (host-facing)                                              | `19092`                                                             |
| `KAFKA_PLAINTEXT_PORT`                           | Kafka plaintext port (in-network)                                                    | `9092`                                                              |

### Apache Kafka

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `APACHE_KAFKA_VERSION`                           | Version of Apache Kafka to use                                                       | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |
| `KAFKA_ADVERTISED_LISTENERS`                     | Listener addresses advertised to clients/brokers                                     | `PLAINTEXT://kafka:9092,PLAINTEXT_HOST://localhost:19092`           |
| `KAFKA_AUTO_CREATE_TOPICS_ENABLE`                | Toggle auto creation of topics                                                       | `true`                                                              |
| `KAFKA_CLUSTER_ID`                               | Name of the Kafka cluster                                                            | `dfe-docker-dev-cluster-01`                                         |
| `KAFKA_CONTROLLER_LISTENER_NAMES`                | Listeners used by the controller                                                     | `CONTROLLER`                                                        |
| `KAFKA_CONTROLLER_QUORUM_VOTERS`                 | Set of voters                                                                        | `1@kafka:29092`                                                     |
| `KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS`         | Time (ms) group coordinator waits before initial rebalance                           | `0`                                                                 |
| `KAFKA_INTER_BROKER_LISTENER_NAME`               | Listener used for communication between brokers                                      | `PLAINTEXT`                                                         |
| `KAFKA_LISTENER_SECURITY_PROTOCOL_MAP`           | Map of listener names and security protocols                                         | `CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT` |
| `KAFKA_LISTENERS`                                | List of listeners                                                                    | `PLAINTEXT://:9092,PLAINTEXT_HOST://:19092,CONTROLLER://:29092`     |
| `KAFKA_NODE_ID`                                  | Node ID associated with the roles                                                    | `1`                                                                 |
| `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR`         | Replication factor for the offsets topic                                             | `1`                                                                 |
| `KAFKA_PROCESS_ROLES`                            | Roles the process will use                                                           | `broker,controller`                                                 |
| `KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR` | Replication factor for the transaction topic                                         | `1`                                                                 |
| `KAFKA_TRANSACTION_STATE_LOG_MIN_ISR`            | Minimum ISR for transaction topic                                                    | `1`                                                                 |

### Kafka Redpanda

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `REDPANDA_VERSION`                               | Version of Redpanda to use                                                           | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |
| `REDPANDA_MEMORY`                                | Memory cap for the Redpanda broker (Seastar reserves this up front)                  | `1G`                                                                |

### Kafka UI (Kafbat)

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `KAFBAT_ENABLED`                                 | Toggle to turn on Kafbat                                                             | `true`                                                              |
| `KAFBAT_VERSION`                                 | Version of Kafbat to use                                                             | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |
| `KAFBAT_DYNAMIC_CONFIG_ENABLED`                  | Toggle runtime config changes                                                        | `true`                                                              |
| `KAFBAT_KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS`       | Kafka bootstrap server                                                               | `kafka:9092`                                                        |
| `KAFBAT_KAFKA_CLUSTERS_0_NAME`                   | Kafka cluster name                                                                   | `dfe-local`                                                         |
| `KAFBAT_PORT`                                    | Kafbat port                                                                          | `8081`                                                              |

## Core DFE Components

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_CORE_ENABLED`                               | Toggle to turn on core components                                                    | `true`                                                              |
| `DFE_ENGINE_VERSION`                             | Version of dfe-engine to use                                                         | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_ENGINE_PORT`                                | Port used by dfe-engine                                                              | `8003`                                                              |
| `DFE_ENGINE_CONFIG_DIR`                          | Path to config directory                                                             | `/app/config`                                                       |
| `DFE_ENGINE_SCHEMAS_DIR`                         | Path to schemas directory                                                            | `/app/schemas`                                                      |
| `DFE_UI_VERSION`                                 | Version of dfe-ui to use                                                             | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_UI_PORT`                                    | Port used by dfe-ui                                                                  | `3000`                                                              |
| `DFE_UI_NODE_ENV`                                | Node environment of dfe-ui                                                           | `production`                                                        |

## HyperDX (opt-in observability)

Off by default. `DFE_HYPERDX_ENABLED=true` starts `hyperdx` (API + App) plus its `hyperdx-ferretdb` and `hyperdx-postgres` dependencies, sharing the always-on ClickHouse.

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_HYPERDX_ENABLED`                            | Toggle to start HyperDX + FerretDB + Postgres                                        | `false`                                                             |
| `DFE_HYPERDX_VERSION`                            | Version of hyperi-hyperdx to use                                                     | `latest` -- the ONE image not hard-failed, because the fork is unpublished so the SSoT cannot pin it |
| `DFE_HYPERDX_API_PORT`                           | HyperDX API host port                                                                | `8000`                                                              |
| `DFE_HYPERDX_APP_PORT`                           | HyperDX App UI host port                                                             | `8090`                                                              |
| `DFE_HYPERDX_APP_URL`                            | Base URL the browser uses to reach HyperDX                                           | `http://localhost`                                                  |
| `HYPERDX_THEME`                                  | UI theme (NEXT_PUBLIC_THEME)                                                         | `dfe`                                                               |
| `HYPERDX_POSTGRES_USER`                          | FerretDB/Postgres user                                                               | `hyperdx`                                                           |
| `HYPERDX_POSTGRES_PASSWORD`                      | FerretDB/Postgres password                                                           | `hyperdx`                                                           |
| `HYPERDX_FERRETDB_VERSION`                       | FerretDB image version                                                               | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |
| `HYPERDX_POSTGRES_VERSION`                       | Postgres/DocumentDB image version                                                    | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |

## Default Ports

| Port  | Service              | Protocol           |
|-------|----------------------|--------------------|
| 3000  | dfe-proxy            | Web UI (nginx fronts dfe-ui, which publishes no host port) |
| 6000  | dfe-receiver         | gRPC               |
| 8000  | hyperdx              | API                |
| 8003  | dfe-engine           | HTTP API           |
| 8080  | dfe-receiver         | HTTP ingest        |
| 8081  | Kafbat               | Web UI             |
| 8082  | dfe-fetcher          | HTTP ingest        |
| 8090  | hyperdx              | App UI             |
| 8123  | ClickHouse           | HTTP API           |
| 8686  | dfe-transform-vector | Vector API         |
| 9000  | ClickHouse           | Native protocol    |
| 9090  | dfe-receiver         | Prometheus metrics |
| 9091  | dfe-loader           | Prometheus metrics |
| 9092  | Kafka (any backend)  | Plaintext          |
| 9093  | dfe-archiver         | Prometheus metrics |
| 9094  | dfe-fetcher          | Prometheus metrics |
| 9095  | dfe-transform-vector | Prometheus metrics |
| 9096  | dfe-transform-vrl    | Prometheus metrics |
| 19092 | Kafka (any backend)  | Plaintext host     |
| 50051 | dfe-loader           | gRPC               |

Additional receiver ports (commented out by default in docker-compose.yml):
4317 (OTLP gRPC), 4318 (OTLP HTTP), 5044 (Beats), 8088 (HEC).

## ClickHouse Schema

**dfe-engine is the schema authority.** It ships `/app/schemas` inside its own
image (pinned by `DFE_ENGINE_VERSION`) and creates the ClickHouse objects at
startup; the loader pre-warms those schemas into its cache. Nothing in this repo
provisions tables.

- `dfe` - master database for all DFE related tables
- `dfe.default` - catch-all for unrouted events, carrying `_tags` (JSON) among
  the profile columns

That last detail matters more than it looks: the e2e suite and the power-on self
test both identify their own rows by reading `_tags.marker` back out. If a
deployment provisions a schema without `_tags`, both will correctly refuse to
claim they verified anything.

## Container Images

Images are published from the component repos:

- `ghcr.io/hyperi-io/dfe-archiver`
- `ghcr.io/hyperi-io/dfe-engine`
- `ghcr.io/hyperi-io/dfe-fetcher`
- `ghcr.io/hyperi-io/dfe-loader`
- `ghcr.io/hyperi-io/dfe-receiver`
- `ghcr.io/hyperi-io/dfe-transform-vector`
- `ghcr.io/hyperi-io/dfe-transform-vrl`
- `ghcr.io/hyperi-io/dfe-ui`

## Licence

BUSL-1.1 - see [LICENSE](LICENSE) for details.
