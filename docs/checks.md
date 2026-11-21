<!--
Project:   dfe-docker
File:      docs/checks.md
Purpose:   Outage tests, the post-deploy source test and the static checks
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Outage, source and static checks

The outage tests stop a service under load, the source test drives a new source through the console, and the static checks are what CI runs. The e2e suite they build on is in [testing.md](testing.md).

## Outage tests stop a service under load

```bash
make test-resilience
make test-resilience E2E_TESTS="clickhouse-outage loader-outage-grpc"
```

Opt-in: `make test-e2e` never runs these, and rejects one listed in `E2E_TESTS`.
An `outage:` test sends steady load and, once its first records land, stops the
named service for `seconds`, starts it again in a `finally`, and keeps sending
for 30 s. It then asserts every other DFE service ran straight through (same
pid, no restart, no die or OOM event), the receiver answered every request (a
refusal such as 503 passes, silence fails), every accepted record landed, and on Kafka
every consumer group is back at zero lag. They stop services, so they never run
in `make post` either.

An outage's `method` is `stop` by default, a graceful stop that lets the service drain what it holds. `method: kill` sends SIGKILL and a crash gets no drain, so a record the service held in memory survives only if its acknowledgement was held until the next hop had it. The `*-kill-*` tests kill the loader, the receiver and the VRL transform on Kafka, and the loader and receiver on direct gRPC. They pass only when every record answered 2xx lands, duplicates counted but allowed. Where the killed service is the receiver itself, a request in flight while it is down may go unanswered but silence outside that window still fails.

A `pause` freezes the next hop first, so the hop taken down dies holding what it took meanwhile. The `-sigterm-` tests stop the same hops gracefully. Only three kills have an `-acks-off` control: the loader on gRPC, the receiver on gRPC and the receiver on Kafka. The VRL transform, the archiver and the loader on Kafka have none. A control runs its kill with acknowledgements off on every hop to ClickHouse and sets `loss: required`, so it fails when it loses nothing: a control that cannot lose proves nothing. `loss: unreliable` is for a hop that holds too little to lose every run, and reports a clean run as non-discriminating instead of failing it. Other `outage:` keys read the receiver's disk spool (`spool`), refused records and where they go (`poison`, `dead_letters`) and the archive (`archive`). The keys are listed in `tests/e2e/e2e-tests.yaml`.

A kill that catches nothing in flight proves nothing about a crash, so a kill test fails on it. For the receiver or a gRPC hop that means no request out at the instant the container exited. For a Kafka consumer it means nothing its group had left uncommitted just before the kill. The kill tests raise the load with `workers` and `interval` (the default is 4 workers each waiting 0.5 s) so that a kill lands with records in flight.

## The post-deploy source test is what an operator does first

```bash
DFE_INFRA_DIR=../dfe-infra make test-source
DFE_INFRA_DIR=../dfe-infra make test-source SOURCE_ARGS="--case cloudwatch --aws-service cloudtrail"
DFE_INFRA_DIR=../dfe-infra make test-source SOURCE_ARGS="--case elastic"
DFE_INFRA_DIR=../dfe-infra make test-source SOURCE_ARGS="--case vector"
DFE_INFRA_DIR=../dfe-infra make test-source SOURCE_ARGS="--case edgestream-hub"
DFE_INFRA_DIR=../dfe-infra make test-source SOURCE_ARGS="--case flow"
```

Above, sources come through the API. This one creates one in the console, attaches a transform, deploys it, feeds it data and reads the rows back, driving a browser to do it. The runner is dfe-infra's (`scripts/acceptance/source/run.py`), the same one `dfe-ops acceptance --suite source` calls for Kubernetes.

The `edgestream-hub` and `flow` cases drive the engine API and the receiver's listeners, with no browser steps. `flow` sends UDP from this machine, so `DFE_RECEIVER_NETFLOW_PORT` and `DFE_RECEIVER_SFLOW_PORT` must be set in `.env` (each publishes a receiver port, see [configuration.md](configuration.md)), and it reads its captures from a dfe-receiver checkout beside the dfe-engine one.

`scripts/test_source.py` supplies what this repo owns: the published ports, the archiver container, the admin login from `env/engine.env` and `make apply` for each service the engine says must restart, which is what creates the source's own transform container. Run `make test-source DEV=1` against a `make dev` stack so that apply keeps the local images. `SOURCE_ARGS` reaches the runner untouched (`--keep`, `--per-module`, `--headed`).

Beyond a running stack it needs `DFE_INFRA_DIR` (the runner), `DFE_ENGINE_REPO` (the synthetic corpus and its reader), `DFE_TRANSFORM_VRL_REPO` for the filebeat case (the bundled pipeline), a dfe-transform-vector checkout for the vector case (`DFE_TRANSFORM_VECTOR_REPO`, else the one beside the engine repo) and an interpreter carrying Playwright (`DFE_ACCEPTANCE_PYTHON` where the system one is externally managed). The cloudwatch case polls a real AWS upstream and writes nothing to it: put the two `AWS_*` credentials in `env/fetcher.env` and `DFE_AWS_REGION` in `.env`.

Every step is a report row and a screenshot under `--shots-dir` (`.tmp/source`).
A step the console cannot do falls back to the engine API and says so, because
that is a finding about the console rather than about the pipeline.

`make proof` runs the filebeat and vector cases on every stack version. A failed run keeps its screenshots under `.tmp/proof-shots/<UTC time>/` in the checkout, names that path in its `RESULT` line and keeps the newest three runs. Each transform checkout is cloned at an explicit ref override when one is set, else at the tag the stack pins, else at its default branch with a WARN, and only the pinned tag proves that pin.

## Static checks run what CI runs

`make check` runs every static check CI runs, and CI calls each through the same make target.

| Target | What it does |
|---|---|
| `make check-compose` | `docker compose config` on the registry, dev, `LIVE_UI=1` and `LOCAL=` paths, against both Kafka backends, plus the CPU floor and dev-overlay coverage assertions |
| `make check-hardfail` | asserts an unpinned checkout fails to resolve rather than pull `latest` |

| `make check-dockerfile` | hadolint on `docker/dfe-rust-builder.Dockerfile` and `docker/dfe-docker.Dockerfile` |
| `make check-docs` | asserts every relative link across the README and every page under `docs/` resolves |
| `make check-proxy` | `envoy --mode validate` on both proxy configs and their console TLS variants, on the Envoy the stack pins |
| `make check-python` | `ruff check` and `ruff format --check` on `scripts/`, `ops/` and the demo packs |
| `make check-tests` | `pytest scripts/tests` -- unit tests over the helper scripts, including the theatre churn tiles' SQL run on the stack's ClickHouse image over seeded tracks |
| `make check-locks` | `uv lock --check` on each script that declares packages inline, so a pin moved without `uv lock --script <script>` fails here rather than mid-suite |

`check-compose` is hermetic: it reads the mandatory `${VAR:?}` keys out of the
compose file and substitutes placeholders, so it reads neither the stack SSoT nor
the registry. It validates compose *structure*, not that any digest
resolves. It rejects a `DFE_SERVICE_CPUS` under 2.0 on any service that gates
on `/readyz` -- see [operating.md](operating.md#dfe_service_cpus-has-a-floor-of-20-and-it-is-not-about-speed)
for why a lower ceiling takes health endpoints dark.

`check-docs` resolves the file half of every relative link. It does not fetch
external URLs and does not check `#anchors`, so a live file with a stale anchor
still passes.

`check-python`, `check-tests` and `check-locks` fetch their pinned tools through `uvx`, `check-dockerfile` pulls the pinned hadolint image and `check-proxy` pulls Envoy, so those want a network the first time. The churn tile test in `check-tests` needs `docker` and pulls the ClickHouse image the stack pins (`DFE_CLICKHOUSE_VERSION` overrides the tag, else `scripts/stack.py --print CLICKHOUSE_VERSION` reads it through `oras`). It skips without docker, and fails under `CI`.


`check-proxy` validates on the Envoy in `DFE_PROXY_VERSION`. With that unset it asks `scripts/stack.py --print DFE_PROXY_VERSION` for the one the stack at `VERSION` pins, falling back to `deployment.example.yaml`'s `version.pin`. That lookup reads the stack manifest anonymously through `oras`. The check mints a throwaway P-384 certificate with the host's `openssl` for the TLS variants.
