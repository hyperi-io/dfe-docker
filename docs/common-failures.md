<!--
Project:   dfe-docker
File:      docs/common-failures.md
Purpose:   Failures the Compose stack shows most often, by symptom
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Common failures

Each section is one symptom and what causes it. If none matches, read the stack's
state first: [troubleshooting](troubleshooting.md). The open problems whose symptoms
mislead are in [known issues](known-issues.md).

## A service is unhealthy but the data is landing

Symptom: `docker compose ps` shows a DFE service unhealthy or perpetually
starting, `docker inspect` reports `Health check exceeded timeout`, and yet
events are flowing through that very service. Curl its port from inside the
container and the connection is *accepted* instantly and then never answered --
not refused, not reset, just silent. `/livez` and `/metrics` behave the same
way, which rules out the readiness logic.

That is Tokio worker-thread starvation, and the cause is the CPU ceiling.
`available_parallelism()` reads the cgroup quota and floors it, so a ceiling
under 2.0 gives the service one worker thread; scalo's Kafka `recv` polls
synchronously and owns it; the HTTP server task never runs.

```bash
docker inspect --format '{{.HostConfig.NanoCpus}}' dfe-archiver   # 1500000000 = 1.5
```

Put `DFE_SERVICE_CPUS` back to 2.0 or above and recreate the service. To confirm
the diagnosis without touching the ceiling, set `TOKIO_WORKER_THREADS=4` in
`env/<service>.env` and recreate -- if it answers, it was starvation and not a
shortage of CPU.

`make check-compose` blocks a sub-2.0 ceiling on any service that gates on
`/readyz`, so this should only reach you via a hand-edited override.

## The self test fails on the console login

`make post` logs in as `admin` with `DFE_AUTH_LOCAL_ADMIN_PASSWORD` from `env/engine.env`, the same value `make creds` prints. A 401 here means the running engine holds a different password from the one in `env/engine.env`: a rotation applied to the store alone, say. Pass the current one on the command line, since the shell environment beats `env/engine.env`:

```bash
DFE_AUTH_LOCAL_ADMIN_PASSWORD='the rotated password' make post
```

## The self test fails on self-telemetry

`make post` makes two claims on a profile running the collector, and the second
is that the `dfe.otel_*` tables has rows newer than five minutes. Two distinct
failures:

**"no table readable"** -- the collector never wrote its schema, so it cannot
reach ClickHouse. Its exporter creates the database and tables on startup, so an
absent schema is a connection or credential fault, not a quiet one:

```bash
docker compose logs otel-collector
docker compose exec clickhouse clickhouse-client --query "SHOW TABLES FROM default"
```

**"no rows newer than"** -- the tables exist and nothing is arriving, so nothing
is exporting. Check that the endpoint actually reached the containers, and that
the engine is on the OTel backend:

```bash
docker compose exec dfe-engine printenv OTEL_EXPORTER_OTLP_ENDPOINT
docker compose logs dfe-engine | grep "Metrics initialized"
```

`backend=prometheus` on that last line means it is not pushing. Drop the `DFE_API_METRICS_BACKEND` line from `env/engine.env`, then `make up`.

Every DFE app the profile runs should appear, dfe-ui aside. A service that is
running but missing from the results IS a fault. The Rust services push whenever
their
`OTEL_EXPORTER_OTLP_ENDPOINT` is set, so check that env on the absent container
the same way. Which components push, and since when, is in
[observability.md](observability.md).

## `port is already allocated`

Usually something holding the broker's host port `19092`. The broker's in-network
listener `9092` is not published, so a Kafka on the host's `9092` is not the cause.
The stack and the e2e harness reach the broker over the Docker network alias
`kafka:9092`, so any free host port works:

```bash
make test-e2e KAFKA_PLAINTEXT_HOST_PORT=29192
```

Note the argument position -- if that key is live in your `.env`, the
`VAR=x make ...` form will not take. See the shadowing issue above.

The gRPC-only tests need no broker at all:

```bash
make test-e2e E2E_TESTS="simple-receiver-to-loader-grpc rules-and-hunts"
```

Also check for a stray container from a previous profile before assuming the
port belongs to something else.

## Compose refuses to start: `is missing a value`

The stack is unpinned. Image versions come from the DFE stack SSoT, not from
defaults -- there is no silent `latest` fallback:

```bash
make stack VERSION=X.Y.Z
```

`make check-hardfail` asserts this property holds, by running compose with
nothing set and requiring that it fails for the right reason. A green run proves
the stack refuses to resolve with nothing set; it does not prove every single
image is pinned, because compose aborts on the first missing variable.

## `PyYAML is not installed in this interpreter`

The e2e harness declares PyYAML and Playwright inline (PEP 723), locked in `scripts/test_e2e.py.lock`, and `make test-e2e` runs it through uv, which installs them. A bare `python3 scripts/test_e2e.py` skips that step:

```bash
make test-e2e
uv run --locked --script scripts/test_e2e.py   # the same, without make
```

`The lockfile at uv.lock needs to be updated` from either means a pin in the script moved without its lock: run `uv lock --script scripts/test_e2e.py` and commit both.

The power-on self test (`scripts/post.py`) has no such dependency -- it is
stdlib-only and works on a bare machine.
