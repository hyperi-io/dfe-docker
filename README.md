# dfe-docker

[![Build Status](https://github.com/hyperi-io/dfe-docker/actions/workflows/ci.yml/badge.svg)](https://github.com/hyperi-io/dfe-docker/actions)
[![License](https://img.shields.io/badge/license-BUSL--1.1-blue)](https://github.com/hyperi-io/dfe-docker/blob/main/LICENSE)

> The whole DFE stack on one host, pinned to a certified release rather than
> whatever the tags point at today. Every image is `tag@sha256`, and the compose
> file refuses to start if a pin is missing.

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
| Adding or changing a profile, and wanting to know which ones are yours to edit. | **Profiles** | [docs/profiles.md](docs/profiles.md) |
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

`VERSION=latest` instead pins the newest certified stack and then repins every
DFE image at its own newest published tag - development currency, not a
deployment. `make modes` states the three modes and which one this checkout is
on.

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

Service selection is controlled by `service_profiles.yaml` at the repo root. Each profile declares a transport mode, which DFE services to start, and optionally its whole footprint - the `clickhouse`, `core`, `kafbat`, `hyperdx` and `otel` keys. The `active_profile` field is used to define the profile set and can be overridden with the `DFE_PROFILE` env var; it ships as `slim`, the Compose default, the way a Kubernetes deploy defaults to `scale`. The matching `.env` flags (`DFE_CLICKHOUSE_ENABLED`, `DFE_CORE_ENABLED`, `KAFBAT_ENABLED`, `DFE_HYPERDX_ENABLED`, `DFE_OTEL_ENABLED`) override the profile's keys.

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

`slim` and `single` share their names with the Kubernetes tiers and are rendered
from them, so they are edited THERE and re-rendered here; every other profile is
a hand-crafted data-plane shape you own. There is no `scale` - Compose cannot run
an HA broker or a ClickHouse cluster. Which profile runs what, and how the two
projected ones are re-rendered: [docs/profiles.md](docs/profiles.md).

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
| `make init`        | Create .env and per-service .env files from templates, minting the admin and break-glass passwords |
| `make env-files`   | Assert every `env/<service>.env` exists, creating any the templates have gained |
| `make creds`       | Print the access summary -- console URL, admin login, where the break-glass password lives. The password prints on a TTY only; a pipe, a file or `DFE_CREDS_SHOW=0` gets the `.env` key instead |
| `make up`          | Start the pinned stack and print the access summary                |
| `make dev`         | Build local DFE images from source and start the stack (`LOCAL="..."` builds only those, rest pinned) |
| `make dev-build`   | Build local DFE images from source (no start)                      |
| `make ci`          | Pull and start infra and registry DFE images. Prints no credentials -- `make up` is the same start plus `make creds` |
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
| `make test-resilience` | Opt-in outage tests: stop a service under load, prove the rest survives |
| `make check-tests` | Unit tests over the credential helpers (`scripts/tests`)          |

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

## Context

### What this is

dfe-docker stands the whole DFE suite up as one Docker Compose stack on a single host, and it is the definition-of-done target for the suite's docker path: a change is proven when it runs on the deployment target, not because it ran on someone's laptop. It is packaging, not product - it builds no production image, publishes nothing, and provisions no ClickHouse table and no Kafka topic. dfe-engine is the only schema and topic controller, and `scripts/tests/test_engine_only_schema_control.py` fails the build on a DDL file or a topic-creating step appearing here. When the data path is wrong the fix is nearly always in a component repo. It is not the Kubernetes path either: Kubernetes stays primary and dfe-infra owns it, which is why there is deliberately no `scale` profile - Compose cannot run an HA broker or a ClickHouse cluster. `main` is protected, so every change arrives as a pull request, with Conventional Commits and a DCO sign-off ([CONTRIBUTING.md](CONTRIBUTING.md)).

### Where things live

| Path | What it holds |
|---|---|
| `docker-compose.yml` | Every service and every infrastructure profile. The one file `make ci` names explicitly |
| `docker-compose.override.yml` | Auto-loaded by `docker compose`, so `make dev` builds from source; `make ci` passes `-f docker-compose.yml` to skip it |
| `docker-compose.{live,storage,container-logs,unpublish-*}.yml` | Opt-in overlays the Makefile chains onto `COMPOSE_FILE` |
| `service_profiles.yaml` | Which DFE services start, the transport, and the config each one mounts. Ships `active_profile: slim` |
| `Makefile` | Every entry point, plus the profile, overlay and UI-exposure resolution that decides which compose files a target passes |
| `scripts/resolve_profile.py` | Reads `service_profiles.yaml` and writes the generated `.profile.mk` the Makefile includes |
| `scripts/stack.py` | `make stack` - writes `tag@sha256` pins into `.env` from a local dfe-infra checkout (`DFE_INFRA_DIR`) or the signed OCI stack manifest |
| `scripts/post.py` | The power-on self test `make dev` and `make ci` end with |
| `scripts/test_e2e.py` + `tests/e2e/e2e-tests.yaml` | The declarative e2e suite, one stack per test |
| `scripts/check_*.py` | The gates `make check` runs |
| `scripts/tests/` | Unit tests over the helper scripts - the only tests CI runs |
| `config/<component>/` | The config files services mount, one directory per component |
| `.env.example` + `env.example/` | Committed templates. `make init` copies them to `.env` and `env/`, both gitignored |
| `ops/daemon-update/` | The systemd timer that keeps a single-VM deployment on the newest certified stack |
| `docs/` | Nine audience-scoped documents. Read [docs/architecture.md](docs/architecture.md) first |

### Commands that prove a change

```bash
make check       # every static gate CI runs
make check-tests # pytest over scripts/tests only
make dev         # build from source, start, self-test
make ci          # pull the pinned registry images, start, self-test
make post        # re-run the self test against an already-running stack
make test-e2e    # the declarative suite, one stack per test
make down        # stop and remove containers across EVERY profile
```

`make check` is `check-compose check-hardfail check-dockerfile check-docs check-python check-tests`, and CI runs each one through the same make target so the two cannot drift.

Three ways a green run says less than it looks:

- **`make check` starts nothing.** It resolves compose, lints the helpers and unit-tests them. `make dev`, `make ci`, `make post` and `make test-e2e` are the only things that prove the stack moves data, and CI runs none of them.
- **`check-compose` validates compose STRUCTURE, not digests.** It substitutes placeholders for the mandatory keys so it needs neither the private stack SSoT nor registry credentials, which means no particular pin is proved to resolve.
- **`check-profiles` is not part of `make check` and has never gated anything.** Locally with `DFE_INFRA_DIR` unset it prints `projection NOT checked (not a pass)` and exits 0. In CI the job reads dfe-infra with a short-lived, read-only token minted from the org's CI GitHub App; until this repo can use the `GH_APP_PRIVATE_KEY` org secret, the job is skipped with a notice and the run is still green (issue #119).

`check-hardfail` does earn its pass. It resolves compose with a scrubbed environment and an empty `--env-file` so a developer's pinned `.env` cannot mask it, requires the run to fail on a missing pin, then reads every `image:` line and requires each to resolve to an `@sha256:` digest.

### What tends to bite

| Don't | Do | Why |
|---|---|---|
| Leave containers running when you are finished | `make down`, on every host you touched, the same session | `down` sweeps every profile rather than the active one, because passing only the active profile's services left the previous profile's containers up - still holding host ports and still answering health probes for a pipeline that was no longer wired (`Makefile`, the `down` target) |
| Assume the deployment target is idle | Check what is already running before you start anything | The docker definition-of-done target carries a long-lived stack refreshed on a 6-hourly systemd timer (`ops/daemon-update/`), not a box you get to yourself |
| Assume events go through Kafka | Read the profile's `transport` first | `slim`, the shipped `active_profile`, is `transport: grpc`: the receiver dials `dfe-loader:6000` directly and no broker starts. `single` is the Kafka one. The core data path is otherwise identical, and `post` and `test-e2e` assert the same landing row either way |
| Add a DDL file or a topic-creating step here | Change the schema in dfe-engine | dfe-engine ships the schemas in its own image and reports healthy only once it has applied them. `scripts/tests/test_engine_only_schema_control.py` fails the build on one (#118) |
| Point the stack at an external ClickHouse with `CLICKHOUSE_HOST` alone | Edit `config/loader/*.yaml` as well | That variable moves dfe-engine only. The loader's host is a literal - `config/loader/grpc.yaml:15` is `- clickhouse:8123` - so the loader keeps talking to a container that is not running |
| Remap the published ClickHouse port | Leave it, or fix the engine's reference first | One variable is both the published port and the in-network one, so moving the publish breaks dfe-engine (#75) |
| Move the loader's `grpc.listen` off 6000, or confuse it with the receiver's 6000 | Keep the loader on container port 6000, published on host port 50051 | dfe-engine compiles every sender's loader endpoint as `dfe-loader:6000`, so a loader listening anywhere else drops every record behind an HTTP 202 with nothing logged. The receiver's 6000 is the external Vector protocol and binds `DFE_INGRESS_BIND_HOST` (`0.0.0.0`). The loader's is the internal `DfeTransport/Push` and binds `DFE_BIND_HOST` (`127.0.0.1`). `scripts/tests/test_loader_grpc_port.py` fails the build on a mismatch |
| Publish the UIs beyond loopback and leave `DFE_EXTERNAL_ORIGIN` alone | Set it to the address browsers actually use | `DFE_BIND_SCOPE=all` with a loopback origin builds HyperDX's frame-ancestors policy and every next-auth redirect from the wrong host, so the console comes up with its observability views blocked and sends logins to the wrong machine. The Makefile now refuses the combination (#108) |
| Raise `REDPANDA_MEMORY` on its own | Raise `DFE_BROKER_MEMORY` above it | The broker gets at most the container limit less the host's `vm.min_free_kbytes`: `redpanda/start.sh` lowers `--memory` to that and logs a WARN naming the `DFE_BROKER_MEMORY` that restores it (`.env.example:229`, `.env.example:519`) |
| Run Kafka-transport tests on a host that already has a broker on 9092 | `make test-e2e KAFKA_PLAINTEXT_PORT=29092 KAFKA_PLAINTEXT_HOST_PORT=29192`, or run the gRPC-only tests | The broker publishes 9092 and 19092 and collides with an always-on dev daemon (#36) |
| Set `DFE_UPDATE_WIPE_STATE=1` on a box that also sets `DFE_DATA_ROOT` | Leave `DFE_DATA_ROOT` unset if you rely on the wipe | `make clean` does not chain the storage overlay, so it removes the volume objects while the bind directories keep their contents and the wipe silently becomes a no-op (`ops/daemon-update/README.md`) |
| Take the local `CLAUDE.md` as current | Read the file you are asking about | It is gitignored and unmaintained. It still names `dfe-operator` as the Kubernetes repo, claims `hyperi-hyperdx` floats on `:latest`, counts seven docs where there are nine, and points at a `scripts/deprecated/` directory that has been removed |

### Where this sits

The suite graph in `dfe-infra/suite.yaml` records exactly one edge on this repo and no outbound edges at all. Read it with `dfe-stack suite --consumer dfe-docker` and `--producer dfe-docker`.

| Repo | Direction | How they interact |
|---|---|---|
| dfe-infra | inbound, `derived-pins` | dfe-docker holds no copy of the pins. `scripts/stack.py` renders them at `make stack` time, from a local dfe-infra checkout when `DFE_INFRA_DIR` names one, else from the signed OCI stack manifest pulled with `oras`. `ops/daemon-update/self_update.py` is the second consumer of that manifest. The edge's declared check is "nothing" - there is no second copy that can drift, and the graph records the edge so the suite tooling knows to walk past it. Do not go hunting for a pin file here to update |
| dfe-infra | inbound, not carried by the graph | `slim` and `single` are projections of the Kubernetes tiers of the same name. Edit `dfe-infra argocd/values/profile-<mode>.yaml`, then `make render-profiles` here, which needs `DFE_INFRA_DIR`. Every other profile in `service_profiles.yaml` is this repo's own |
| dfe-receiver, dfe-fetcher, dfe-loader, dfe-archiver, dfe-transform-elastic, dfe-transform-vector, dfe-transform-vrl, dfe-engine, dfe-ui, dfe-hyperdx | inbound | This stack runs their published GHCR images at the digests `make stack` pins, and `make dev` builds the same components from source into local images. Nothing flows the other way |

Nothing in the suite graph depends on dfe-docker, so a change here breaks a deployment rather than another repo's build. `dfe-transform-splack` and `dfe-transform-wasm` have repos but no service in this stack.
