# dfe-docker

The whole DFE stack on one host with Docker Compose, from pinned release images or local builds.

[![Build Status](https://github.com/hyperi-io/dfe-docker/actions/workflows/ci.yml/badge.svg)](https://github.com/hyperi-io/dfe-docker/actions)
[![License](https://img.shields.io/badge/license-BUSL--1.1-blue)](https://github.com/hyperi-io/dfe-docker/blob/main/LICENSE)

## What it does

- Runs DFE on one box, an edge site, a partner deployment or a developer's test rig. Compose is a supported production target for small environments. Kubernetes stays the primary path, so where both would work, choose Kubernetes.
- Pins every image by digest from a certified DFE stack release with `make stack`. With a pin unset the stack refuses to start rather than pull `latest`.
- Starts from the registry images with `make up`, or builds chosen components from source with `make dev`.
- Runs Kafka or Redpanda as the broker, with opt-in overlays for TLS, storage and an oauth2-proxy `auth` profile.
- Checks that the running stack moves data from the ingest edge to a ClickHouse row with `make post`, which fails if an enabled run asserts nothing. `DFE_POST_ENABLED=false` skips it.
- Loads seed sources, rules and hunts into a running engine with `make sources`, `make rules` and `make hunts`.

It consumes the same images from the same registry as the Kubernetes path, pinned from the same stack release. There is no promotion step and no Compose-specific build.

## Quick start

With only Docker, at the `min_engine` and `min_compose` each release's `quickstart.json` names, on macOS, Linux or Windows:

```bash
docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:compose up -d
```

Point your browser to http://localhost:3000 and log in as `admin` with the password `dfe-first-login`. The console makes you change it at first login. The demo is a throwaway stack of its own that seeds its sources and streams firewall syslog through them beside two fetchers:

```bash
docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo up -d
```

`down` with the same `-f` stops either one, and `down -v` deletes its data too.

From a clone, on a host with Docker Compose v2, make, git and Python 3:

```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make up                          # single-kafka, the newest published stable stack
make up PROFILE=slim-grpc        # another profile
make up VERSION=<stack version>  # an older stack
make demo                        # the same stack with the demo stream on it
make down                        # stop everything, keep the data
```

`make up` creates `.env`, mints the secrets, pins the newest published stable stack and prints the console URL and login. `PROFILE=` and `VERSION=` are written to `.env`, so later runs keep them.

## Configuration

A profile is `<tier>-<transport>`: `slim-grpc`, `slim-kafka`, `single-grpc` or `single-kafka`. `slim` is the core data path alone and `single` adds the per-source apps. There is no `scale` tier, because Compose cannot scale out. `KAFKA_BACKEND=apache` swaps Redpanda, which is BSL, for Apache Kafka on the kafka profiles.

Each service reads its own `env/<service>.env`, created from `env.example/` by `make init`, which never overwrites a file and reports the keys an old `.env` is missing. Only the console and Search authenticate. The ingest ports, Kafbat and the metrics ports ask for no login, so put the stack behind a VPN, a tunnel or a firewall before anyone you have not met can reach it, or arm the `auth` profile. Every variable, the port map and the image list are in [docs/configuration.md](docs/configuration.md).

## Documentation

Pick the page for what you are doing:

- [docs/evaluating.md](docs/evaluating.md): a first look, a demo, a proof of concept.
- [docs/operating.md](docs/operating.md): running it where other people depend on it, and exactly what is exposed.
- [docs/deploying.md](docs/deploying.md): standing a deployment up, or moving one to a newer certified stack.
- [docs/developing.md](docs/developing.md): using the stack as a test rig for a component you are changing.
- [docs/troubleshooting.md](docs/troubleshooting.md), then [known issues](docs/known-issues.md) and [common failures](docs/common-failures.md).
- [docs/observability.md](docs/observability.md): health endpoints, self-telemetry and what a passing self test proves.
- [docs/profiles.md](docs/profiles.md) and [docs/architecture.md](docs/architecture.md): how the profiles and the pieces fit together.

The suite's published docs are at <https://dfe.docs.hyperi.io>.

## Help and contributing

How to work in this repo, the commands that prove a change and what tends to bite are in [CONTRIBUTING.md](CONTRIBUTING.md). A question that is not a bug goes in a new issue.

Open bugs: [this repo](https://github.com/hyperi-io/dfe-docker/issues?q=is%3Aopen+is%3Aissue+type%3ABug+-label%3Abacklog+-label%3Anext-minor) | [whole DFE suite](https://github.com/search?type=issues&q=is%3Aopen+is%3Aissue+type%3ABug+-label%3Abacklog+-label%3Anext-minor+repo%3Ahyperi-io%2Fdfe-infra+repo%3Ahyperi-io%2Fdfe-docker+repo%3Ahyperi-io%2Fdfe-deploy+repo%3Ahyperi-io%2Fdfe-engine+repo%3Ahyperi-io%2Fdfe-loader+repo%3Ahyperi-io%2Fdfe-receiver+repo%3Ahyperi-io%2Fdfe-fetcher+repo%3Ahyperi-io%2Fdfe-archiver+repo%3Ahyperi-io%2Fdfe-transform-vrl+repo%3Ahyperi-io%2Fdfe-transform-vector+repo%3Ahyperi-io%2Fdfe-transform-elastic+repo%3Ahyperi-io%2Fdfe-ui+repo%3Ahyperi-io%2Fdfe-hyperdx+repo%3Ahyperi-io%2Fscalo-rs+repo%3Ahyperi-io%2Fscalo-py+repo%3Ahyperi-io%2Flogreducer+repo%3Ahyperi-io%2Fdfe-schemas+repo%3Ahyperi-io%2Fvector-vrl+repo%3Ahyperi-io%2Fculvert+repo%3Ahyperi-io%2Fdfe-clickhouse-rs+repo%3Ahyperi-io%2Ffactbook-rs)

## Licence

BUSL-1.1, see [LICENSE](LICENSE).
