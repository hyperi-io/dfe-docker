# Profiles

This repo owns every profile in `service_profiles.yaml`. Nothing generates them.

Pick one with `make up PROFILE=<profile>`, which writes it to `.env` as `DFE_PROFILE`. With none set, `make up` and `make demo` use `single` (`DEFAULT_PROFILE` in the Makefile). `make demo` needs a profile that runs dfe-transform-elastic, which is `single`.

## Which profile runs what

| Profile  | Transport | dfe-archiver | dfe-loader | dfe-receiver | dfe-fetcher | dfe-transform-elastic | dfe-transform-vector | dfe-transform-vrl |
|----------|-----------|:------------:|:----------:|:------------:|:-----------:|:---------------------:|:--------------------:|:-----------------:|
| `slim`   | gRPC      |              |     X      |      X       |             |                       |                      |                   |
| `single` | Kafka     |      X       |     X      |      X       |      X      |           X           |          X           |         X         |

dfe-fetcher runs one container for every fetcher source. The three transforms run one container per source once a source created through the engine binds one. dfe-engine runs a fetcher or a transform for a source on Kafka only, so `slim` runs neither and there is no gRPC profile that does.

Every profile runs the engine, the UI, dfe-hyperdx and the self-monitoring collector, from its `core`, `hyperdx` and `otel` footprint keys. `single` adds the broker and Kafbat. The matching `.env` flag overrides each key and a profile that leaves a key out gets its default:

| Key | Runs | Default | `.env` flag |
|---|---|---|---|
| `clickhouse` | the bundled ClickHouse | true | `DFE_CLICKHOUSE_ENABLED` |
| `core` | dfe-engine, dfe-hunt-runner, dfe-ui, dfe-proxy | true | `DFE_CORE_ENABLED` |
| `kafbat` | Kafbat, on the Kafka transport only | true | `KAFBAT_ENABLED` |
| `hyperdx` | dfe-hyperdx and its DocumentDB store | false | `DFE_HYPERDX_ENABLED` |
| `otel` | the collector for the stack's own telemetry | false | `DFE_OTEL_ENABLED` |
| `gnmic` | gnmic, streaming gNMI telemetry from network devices to the collector ([gnmi.md](gnmi.md)). Needs `otel` | false | `DFE_GNMIC_ENABLED` |
| `auth` | the oauth2-proxy gates ([auth-profile.md](auth-profile.md)) | false | `DFE_AUTH_ENABLED` |

Each profile's working set and sum of container limits are in the [README](../README.md#memory) and `make help`.

**Two profiles, as on Kubernetes.** `slim` is the core data path over gRPC and `single` is the Kafka stack with every per-source app. dfe-engine runs as `docker-slim` or `docker-single` and seeds its default apps from that name. `single` runs one archiver, which archives each source from its landing topic, so archiving needs `single` ([architecture.md](architecture.md#events-arrive-two-ways-and-leave-one)). `resolve_profile.py` rejects any other name.

**A retired name fails with the profile that replaces it.** `slim-grpc` and `single-grpc` point at `slim`. `slim-kafka`, `single-kafka` and every `kafka-*` name point at `single`, and so does any `grpc-*` name that ran a fetcher, an archiver or a transform, because those run on Kafka only. The message says when the replacement moves the stack to the other transport.

**No archiver on gRPC.** There the receiver would send every record to both the loader and the archiver, each with its own at-least-once ack. On Kafka the archiver reads the landing topic instead, so it adds no receiver work. Archiving a source on a gRPC profile makes the receiver retry the missing archiver and answer 503, so use `single`.

## Per-source apps

**One fetcher for every source.** dfe-engine merges every fetcher source into one file, `dfe-fetcher/fetcher.yaml` in `dfe-app-config`, which the committed `dfe-fetcher` service reads. A source with fetcher settings that clash with another's is saved and reported as not run.

**One transform per source, as on Kubernetes.** dfe-engine writes each transformed source's config into its own directory and its name into `env/<app>.instances`. `scripts/instances.py` turns each name into a service in `.tmp/docker-compose.instances.yml`, which `make apply` starts. A file published for `docker compose -f oci://` runs only the transform instances it declares, so dfe-engine rejects any other transformed source there.

**A key runs each per-source app.** No profile names a per-source app as a service. `fetcher_enabled` runs the fetcher, while `transform_elastic_enabled`, `transform_vector_enabled` and `transform_vrl_enabled` each let dfe-engine start that transform once per source. A key left out is false. Each instance reads the config dfe-engine renders for its source, so the keys do nothing while `DFE_CORE_ENABLED=false`. For the filebeat, Vector and Elastic transform scenarios, run [`make test-source`](checks.md#the-post-deploy-source-test-is-what-an-operator-does-first).

**An instance of an app the profile does not run is left out.** Every make target that resolves the profile warns on an `env/<app>.instances` naming one, with the app, its sources and the key to set. It starts none of them. dfe-engine still starts, so those sources can be deleted through it. `make clean` deletes every index with the engine's volume. `make sources` skips a seed source when the profile does not run its fetcher or transform.

**`make dev` builds every per-source app the profile runs.** Each instance runs the local image, because it `extends` the committed service and never sees an overlay, so the resolver writes the image into it. [`LOCAL`](developing.md#some-from-source-the-rest-pinned) narrows the build to what it names: `make dev LOCAL=dfe-fetcher` builds the fetcher alone. The build records what it made in `.tmp/docker-compose.local.yml`, which every `make dev` compose call chains. `make apply` puts a recreated instance back on that image, so the engine's `make apply SERVICES=<instance>` needs no `LOCAL`. `make ci` deletes the record.
