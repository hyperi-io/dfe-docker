<!--
Project:   dfe-docker
File:      docs/observability.md
Purpose:   How the stack reports on itself, and what each surface proves
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# How the stack reports on itself

Three surfaces, three different questions.

| Surface | Answers | Where |
|---|---|---|
| Health endpoints | is this process alive, can it work right now | `/livez`, `/readyz` |
| Self-telemetry | what has the stack been doing | the `default.otel_*` tables |
| The self test | did an event actually get through | `make post` |

Ingested data is a different question -- [troubleshooting.md](troubleshooting.md).

## The health surface is three paths, with no aliases

Every DFE service serves `/livez`, `/readyz` and `/metrics` on its observability
port and nothing else. `/healthz` and the `/health/*` paths are retired and
**404 on every image pinned here**. `make check-compose` rejects any healthcheck
aimed at one.

| Service | Host port | Notes |
|---|---|---|
| dfe-receiver / loader / archiver / fetcher / transforms | 9090, 9091, 9093-9096 | all three paths |
| dfe-engine | 8003 | `/metrics` is on its container's own 9090, unpublished |
| dfe-ui | none | all three on `:3000`, alongside the app |
| dfe-proxy | 3000 | `/livez`, answered by Envoy itself |
| otel-collector | 13133 | the `health_check` extension |
| hyperdx | 8000 | `/health` -- third party, its own convention |

These bind `DFE_BIND_HOST` (`127.0.0.1`), so curl them from the box, not your
laptop.

On dfe-ui, an unknown path returns the 200 app shell rather than a 404 -- Next.js
routes anything it does not recognise to the page. Only the three paths above are
real there.

## Compose picks liveness or readiness for you

Compose has no readiness condition. `depends_on: service_healthy` is the only
startup gate, so anything others wait on must report READINESS.

| Service | Path | Why |
|---|---|---|
| dfe-loader | `/readyz` | receiver and fetcher wait on it |
| dfe-engine | `/readyz` | loader and proxy wait on it, and it provisions their schema |
| everything else | `/livez` | nothing gates on them |
| otel-collector | none | distroless, so nothing inside can probe `:13133` |

A loader gated on liveness reports healthy while unable to reach ClickHouse, and
the receiver starts against it.

Kubernetes has separate probes and ignores `HEALTHCHECK`, so it never chooses.

## Self-telemetry is pushed, never scraped

The stack's own telemetry leaves by a different door from the data it ingests, so
a broken ingest pipeline cannot take the reporting on it down too.

```mermaid
flowchart LR
    apps["DFE services"] -->|OTLP push :4317| col["otel-collector"]
    chsrv[("ClickHouse<br/>system tables")] -->|sqlquery pull| col
    col -->|self-telemetry :8888| col
    col -->|clickhouse exporter| ch[(`default.otel_*` tables)]
    ch -.->|queries it| hdx["HyperDX"]

    classDef on fill:#009E73,color:#ffffff,stroke:#000000
    class col,ch on
```

Services PUSH. HyperDX reads ClickHouse rather than receiving anything -- the
fork ships no OTLP receiver, so the collector's exporter writes the tables it
queries. This is the same chain Kubernetes runs.

Two of the collector's inputs are PULLS, not pushes, because their sources cannot
push. `sqlquery` reads ClickHouse's own `system.metrics`, `system.events` and
`system.parts` on a 30s interval -- the `:9363` Prometheus endpoint is not exposed
by the stack's image, and these system tables are readable on any provider,
managed ClickHouse included. The `prometheus` receiver scrapes the collector's own
`service.telemetry` endpoint on loopback, which is what puts queue depth and
refused/failed counts (`otelcol_*`) into the same pipeline as everything else.

Between them they are what the pre-canned DFE ClickHouse Health and DFE Pipeline
Health dashboards read. Without them those dashboards render empty here while
working on Kubernetes.

`/metrics` is a SEPARATE pathway, for anything that scrapes. This repo ships
nothing that does. Enabling push does not disable it: a Prometheus estate and a
HyperDX estate are both served.

## Turning self-monitoring on

Declare `otel: true` on a profile (`single` does) or set `DFE_OTEL_ENABLED=true`.

| Variable | Default | Effect |
|---|---|---|
| `DFE_OTEL_ENABLED` | `false` | starts the bundled collector |
| `DFE_OTEL_EXPORTER_ENDPOINT` | empty | where services push -- **empty exports nothing** |
| `DFE_OTEL_DATABASE` | `default` | database the collector writes |
| `DFE_OTEL_HEALTH_PORT` | `13133` | the `health_check` extension |
| `DFE_ENGINE_METRICS_BACKEND` | `prometheus` | `opentelemetry` makes dfe-engine push |

The endpoint picks the shape. Enabling the profile points services at the bundled
collector. Name your own endpoint and they push there instead, which is how you
feed an external OTLP backend with no bundled collector at all.

The collector's OTLP ports are NOT published. Self-monitoring stays on the
Compose network. Note `4317`/`4318` on the host are dfe-receiver's OTLP INGEST
edge -- data coming in from your estate, pointing the other way.

## Only dfe-engine reports today

| Component | Pushes? | Why |
|---|---|---|
| dfe-engine | yes, once the profile sets the backend | scalo-py has an exporter, its CLI defaults the backend to prometheus (scalo-py#11) |
| the six Rust services | no | built without scalo's `otel-metrics` feature, so nothing is linked in (scalo-rs#28) |
| dfe-ui | no | `@opentelemetry/api` only, no SDK. Serves `/metrics` on `:3000` |

`OTEL_EXPORTER_OTLP_ENDPOINT` is set on every service regardless, so a rebuilt
component starts reporting with no config change. It is inert on the Rust
services, on Kubernetes as well as here.

This is what leaves the pre-canned DFE Pipeline Health dashboard partly empty:
its records-in-vs-out, Kafka lag, buffer depth and worker saturation tiles read
metrics only the Rust services produce, and those are served on `/metrics` rather
than pushed. The collector-side and ClickHouse-side tiles on that dashboard do
work. Closing the rest needs scalo-rs#28, or a `prometheus` receiver scraping the
services' `/metrics` endpoints -- the second is a config change here, not a
rebuild, and is the cheaper of the two.

The `opentelemetry` backend is DUAL -- it pushes and keeps serving `/metrics`
(`readers=[otlp(grpc)->..., prometheus(/metrics)]`), so the switch costs a
scraping estate nothing.

Container logs and node metrics are not collected. Kubernetes gets both from a
daemonset; the Docker equivalent would mount `/var/lib/docker/containers` into
the collector, giving it every container log on the host. Use
`docker compose logs`.

## What a passing self test proves

`make post` makes up to two claims, and both must hold:

- **Ingest.** Three marked events posted at the profile's ingest edge come back
  as those exact rows in `dfe.default` inside 60s.
- **Self-monitoring**, when a collector is running. Rows in the `default.otel_*` tables
  NEWER than five minutes.

The freshness window is what makes the second claim mean "streaming now" rather
than "streamed once". These are the pair the Kubernetes bootstrap smoke asserts
as CORE 1 and CORE 2.

`complete-single-node-stack` asserts both, plus the HTTP surface through the
proxy. Outcomes and opt-out:
[operating.md](operating.md#the-power-on-self-test-and-what-a-pass-proves).

## Related

- [operating.md](operating.md) -- exposure, limits, secrets, upgrades, the dial
- [troubleshooting.md](troubleshooting.md) -- reading stack state, diagnosing failures
- [architecture.md](architecture.md) -- components, transports, how data moves
