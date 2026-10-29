<!--
Project:   dfe-docker
File:      docs/troubleshooting.md
Purpose:   Reading stack state, then diagnosing accepted events that never land and the DLQ spool
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Troubleshooting the DFE Compose stack

This applies to you if a stack is misbehaving and you need to find out why --
yours or someone else's.

Start with the state of the stack, then the [known issues](known-issues.md), then the [common failures](common-failures.md) for the one you are actually chasing. Several of the known issues produce symptoms that look like something else entirely, so read them before you go deep.

## Reading the stack's state

`docker compose ps` (or `make ps`) is the first call. It shows which containers
exist and their health, but only for the profile resolved from `.env` -- see the
first [known issue](known-issues.md), because a stray container from a previous profile will
not show up there while still holding its ports. `make ps` also warns about a
per-source container whose source was deleted, which `make apply` removes.

Health endpoints, straight from the host. Every DFE service serves `/livez`,
`/readyz` and `/metrics` and nothing else -- the retired `/health*` paths 404, so
a probe left on one reads as a service that never came up.

```bash
curl -sf http://localhost:9091/readyz     # loader ready?
curl -sf http://localhost:8003/readyz     # engine ready?
curl -sf http://localhost:13133/          # collector healthy? (otel profile only)
```

Ports bind `DFE_BIND_HOST` (`127.0.0.1`), so curl them from the box. The full
port table, which path each HEALTHCHECK uses and why, and dfe-ui's 200-on-unknown
-paths trap are in [observability.md](observability.md).

If the stack sets `DFE_CONTAINER_PREFIX` (a second stack on the same host), every
raw `docker exec` / `docker inspect` / `docker logs` below needs that prefix on
the name -- `docker exec accept-dfe-clickhouse ...`. `docker compose` calls take
the service key and are unaffected.

## Where a DFE service's logs are

With self-monitoring on, DFE's own services log through Docker's `fluentd` driver and `docker compose logs dfe-loader` still reads them back from the daemon's local copy. The same lines are in ClickHouse: search the `otel_logs` source in HyperDX or read them directly:

```bash
docker exec dfe-clickhouse clickhouse-client --query \
  "SELECT Timestamp, Body FROM dfe.otel_logs WHERE ServiceName = 'dfe-loader' ORDER BY Timestamp DESC LIMIT 50"
```

ClickHouse, the broker, the proxies and HyperDX keep `json-file`. To put every service back on it, set `DFE_CONTAINER_LOGS_ENABLED=false` and restart.

## Events are accepted but nothing lands

The ingest edge returning 2xx only means the event was accepted. Work down this
order.

```mermaid
flowchart TD
    start(["Ingest returns 2xx, no rows in ClickHouse"])
    loader{"Loader /readyz OK?"}
    schema{"dfe.main exists<br/>in ClickHouse?"}
    topic{"Consumed topic<br/>exists on broker?"}
    dlq{"Dead letters in /var/spool/dfe<br/>or dfe_loader_dlq?"}

    start --> loader
    loader -->|no| fixloader["Read loader logs:<br/>ClickHouse unreachable or auth"]
    loader -->|yes| schema
    schema -->|no| fixschema["dfe-engine has not provisioned;<br/>check engine health and logs"]
    schema -->|yes| topic
    topic -->|no| fixtopic["Topic mismatch -- see known issues"]
    topic -->|yes| dlq
    dlq -->|yes| fixdlq["Events were dead-lettered;<br/>read the dead letters"]
    dlq -->|no| lag["Check consumer lag in Kafbat :8081"]
```

**Loader health first.** `curl -sf http://localhost:9091/readyz`. A loader that
cannot reach ClickHouse is the single most common cause, and readiness is what
tells you.

**Then the schema.** dfe-engine is the schema authority: it ships `/app/schemas`
inside its own image, creates the ClickHouse objects at startup, and the loader
pre-warms those schemas into its cache. Nothing in this repo provisions tables.
If the engine has not provisioned, the loader holds every message pending-schema
and dead-letters after a timeout. Check `dfe.main` exists and that the engine
is healthy.

**Then the topic.** On a Kafka profile, do not check that the topic exists -- check
what the loader actually SUBSCRIBED to. Those are different questions, and the gap
between them is where this hides: with `topic_regex`, the loader logs its resolved
set once at startup, and a topic can exist, be produced to, and still not be in it.

```bash
docker compose logs dfe-loader | grep "Resolved Kafka topics"
docker compose exec kafka-redpanda rpk group describe dfe-loader -X brokers=kafka:9092
```

If the topic the receiver produces to is missing from the resolved list, read the
`main_load` issue above -- a `_load` sibling suppresses the `_land` topic.
Kafbat on `:8081` shows the produced-to topic and its high watermark, which is the
other half of the picture.

**Then the DLQ**, below.

## The DLQ spool

`dlq-spool` is a shared volume mounted at `/var/spool/dfe` by dfe-archiver,
dfe-fetcher, dfe-loader and dfe-receiver. All four run as non-root `appuser` (uid
1000), and none of the images pre-creates that path, so Docker creates the
mountpoint owned by root and no service can write inside it. The `dlq-init`
one-shot (`chown -R 1000:1000 /var/spool/dfe /var/data/archive /var/lib/dfe-fetcher`) fixes that before
the services start.

`archiver-data`, mounted at `/var/data/archive`, has the same problem and the same
fix. Without it every archive write fails with `Permission denied (os error 13)`,
and because dfe-archiver commits an offset only after a write succeeds, it never
commits one: the lag grows and nothing is archived.

`fetcher-cursors`, mounted at `/var/lib/dfe-fetcher`, gets the same fix. Without it every fetcher logs `Cursor directory is not writable` and re-fetches its lookback window on each restart.

Without it, dfe-archiver crash-loops on `DLQ init failed ... Permission denied
(os error 13)` -- it creates its DLQ writer eagerly at startup. The others create
theirs lazily on first dead-letter, so they look healthy until something needs to
dead-letter, which is the worst moment to find the DLQ never worked.

Per-service volumes do not fix it: a fresh volume is root-owned too, because
ownership is inherited from a path the image does not have. The real fix is for
the component images to create `/var/spool/dfe` as appuser. The one-shot reuses
the archiver image because that image is already pinned.

Three ways that arrangement fails:

1. `required: false` on the dependents means a **failed** dlq-init does not stop
   them -- Compose logs one warning and carries on with a zero exit. Watch for
   the warning.
2. uid 1000 is hard-coded and nothing verifies it, so an image that renumbers
   appuser breaks the DLQ silently.
3. In dev mode dlq-init runs `dfe-archiver:local`, so `make dev` builds the archiver from source even on profiles that run no archiver. `make dev LOCAL=...` without `dfe-archiver` in the list leaves dlq-init on the **registry** image instead, which needs GHCR access and a pinned `DFE_ARCHIVER_VERSION`.

**Files under `/var/spool/dfe/dlq` mean events were accepted and then could not be delivered.** They are evidence, not noise: read them to see what was rejected and why. On a gRPC profile, where there is no broker, the loader writes its own dead letters there, under `dlq/loader/`. On a Kafka profile it writes them to the `dfe_loader_dlq` topic, which Kafbat on `:8081` shows, and to that file only when the broker does not take them.

```bash
docker compose exec dfe-loader ls -la /var/spool/dfe/dlq/loader
```

A row ClickHouse refuses with no working DLQ to take it is lost. The loader logs `No working DLQ` and counts it in `dfe_loader_rows_lost_total`, so any count there is data loss.

## Related

- [known-issues.md](known-issues.md) - open problems whose symptoms mislead
- [common-failures.md](common-failures.md) - the failures seen most often, by symptom
- [observability.md](observability.md) -- the health surface and what each endpoint proves
- [operating.md](operating.md) -- auth position, exposure, limits, secrets
- [configuration.md](configuration.md) -- every variable, port and image
