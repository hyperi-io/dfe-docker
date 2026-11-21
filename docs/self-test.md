<!--
Project:   dfe-docker
File:      docs/self-test.md
Purpose:   The power-on self test: what make post claims and what each outcome means
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# The power-on self test

`make post` proves the running stack moves data. Its health and telemetry context is in [observability.md](observability.md).

## What a passing self test proves

`make post` makes up to seven claims. Each one it asserts must hold and it ends by printing how many it asserted and which it skipped. A run passes with at least one claim asserted and none failed, so read that line: once the bootstrap admin is retired, the observability, console, hunts and routing claims have no login to run with and skip.

- Ingest: Three marked events posted at the profile's ingest edge come back as those exact rows in `dfe.main` inside 60s. Without a login it sends the [ingest token](seeding.md#what-each-target-creates) `.tmp/ingest-token` records.
- Self-monitoring, when a collector is running. Rows in the `dfe.otel_*` tables NEWER than five minutes and a row in `dfe.otel_logs` under each of `dfe-engine`, `dfe-loader` and `dfe-receiver` written since that container started. On the gRPC tier the loader logs at startup and is quiet after it, so a five-minute window can age its only lines out while the earlier claims run. Every service that pushes has fresh metric rows under its own name and the hunt runner reports `dfe_hunt_backlog` and `dfe_hunt_tick_duration_seconds`.
- Observability, when dfe-hyperdx is running. Its API returns the six seeded sources and at least one provisioned dashboard, read through the proxy that gives it an identity.
- Console: Those same marked rows read back through the engine query API.
- Hunts: A hunt created while the runner is up is picked up without a restart and a run queued through the API detects exactly the events its rule matches and none of its near misses.
- Subscription, where the tier has a bus. The loader is fetching at least one topic. It skips on the gRPC tiers.
- Routing, where the engine renders the tier's app config. A source created through the API reaches the running receiver and a record matching its rule lands in a table of its own. The source is called after the run (`postroute` and 8 hex digits) and deleted afterwards, then its table is dropped, because the engine's source delete leaves the table. A run first sweeps the `postroute` sources and tables older than an hour, which a killed run left.

The freshness window is what makes the second claim mean "streaming now" rather than "streamed once". The first two are the pair the Kubernetes bootstrap smoke asserts as CORE 1 and CORE 2.

The e2e test `complete-single-node-stack` asserts them, plus the HTTP surface through the proxy.

## Running the self test, and what each outcome means

`make dev`, `make ci` and `make up` finish by running `scripts/post.py` and fail when it fails. It is opt-**out**, because a self test you have to remember to run is not one. Three rows is deliberate: a self test that writes thousands of rows into a landing table on every boot is one nobody leaves enabled.

```bash
make post                        # run against an already-running stack
DFE_POST_ENABLED=false make ci   # opt out
```

It is not a Docker `HEALTHCHECK`, which runs inside one container with no view of the others and repeats forever, so it would inject test events into a production ingest path on a timer.

Once every claim has run, the three ingest rows are deleted: a lightweight `DELETE` or a mutation (`ALTER TABLE ... DELETE`) on a table with projections that refuse one. A delete that does not finish within 60s is reported, never failed.

Tunables: `DFE_POST_HOST` (default `localhost`), `DFE_POST_DATABASE` (`dfe`), `DFE_POST_TABLE` (`main`). It proves nothing about profiles you are not running or about throughput.

- FAIL on weak secrets: a key in `WEAK_SECRET_DEFAULTS` (`scripts/post.py`) that a running service reads is unset or at its default or the bundled ClickHouse has no password. Run `make init`.
- FAIL, schema not converged: dfe-engine did not report its `schema` readiness check true within 300 seconds. `GET /api/v1/system/schema` on the engine names the object that failed.
- SKIP, schema convergence not checked: the engine names no `schema` check on `/readyz` and answers 404 on `/api/v1/system/schema`, as an engine that predates the schema check does. The rest of the run still asserts the rows land.
- FAIL, not ready: the profile declares an ingest component that never became ready within 90 seconds. A skip is a different outcome.
- FAIL, marker unreadable: rows arrived but the marker could not be read back, so the schema may not have `_tags`. It will not claim a check it did not make.
- SKIP: the profile has no ingest component at all, so there is no ingest path to send a row through. A run in which every claim skipped fails, because it proved nothing.
- FAIL, no otel table readable within 120s: nothing could read the otel tables, which dfe-engine creates from dfe-schemas. Read the engine's and the collector's logs.
- FAIL, no fresh rows: the tables exist and nothing recent is in them, so the services are not exporting ([common-failures.md](common-failures.md#the-self-test-fails-on-self-telemetry)).
