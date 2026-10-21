# Profiles

This repo owns every profile in `service_profiles.yaml`. Nothing generates them.

## Which profile runs what

| Profile        | Transport | dfe-archiver | dfe-loader | dfe-receiver | dfe-fetcher | dfe-transform-elastic | dfe-transform-vector | dfe-transform-vrl |
|----------------|-----------|:------------:|:----------:|:------------:|:-----------:|:---------------------:|:--------------------:|:-----------------:|
| `slim-grpc`    | gRPC      |              |     X      |      X       |             |                       |                      |                   |
| `slim-kafka`   | Kafka     |              |     X      |      X       |             |                       |                      |                   |
| `single-grpc`  | gRPC      |      X       |     X      |      X       |      X      |                       |          X           |         X         |
| `single-kafka` | Kafka     |      X       |     X      |      X       |      X      |           X           |          X           |         X         |

The last four columns run one container per source once a source created through the engine binds one.

Every profile also runs the engine, the UI, HyperDX and the self-monitoring collector, from its `core`, `hyperdx` and `otel` footprint keys. The Kafka profiles add the broker and Kafbat. The matching `.env` flag overrides each key ([configuration.md](configuration.md)).

**A name is `<tier>-<transport>`.** dfe-engine runs as `docker-slim` on a `slim-*` profile and as `docker-single` on a `single-*` one. It seeds its default apps from that tier. `slim` is the core data path alone. `single` adds one archiver and the per-source apps. On Kafka the archiver archives each source from its landing topic. On gRPC nothing sends it records ([architecture.md](architecture.md#events-arrive-two-ways-and-leave-one)). `resolve_profile.py` refuses any other name. An old one such as `single` fails with the profile that replaces it.

**One per source, as on Kubernetes.** The fetcher and the transforms run one instance per source. dfe-engine writes each source's config into its own directory and its name into `env/<app>.instances`. `scripts/instances.py` turns each name into a service in `.tmp/docker-compose.instances.yml`, which `make apply` starts.

**A key runs each per-source app.** No profile names a per-source app as a service. `fetcher_enabled`, `transform_elastic_enabled`, `transform_vector_enabled` and `transform_vrl_enabled` each let dfe-engine start that app once per source. A key left out is false. dfe-transform-elastic is Kafka only, so a gRPC profile refuses its key. Each instance reads the config dfe-engine renders for its source, so the keys do nothing while `DFE_CORE_ENABLED=false`. For the filebeat, Vector and Elastic transform scenarios, run [`make test-source`](developing.md#post-deploy-source-test----what-an-operator-does-first).

**An instance of an app the profile does not run fails the resolve.** Every make target that resolves the profile stops on an `env/<app>.instances` naming one, with the app, its sources and the key to set. A checkout switched to a profile that runs fewer apps stops the same way until those sources are deleted through dfe-engine. `make sources` skips a seed source whose fetcher or transform the profile does not run.

**[`LOCAL`](developing.md#some-from-source-the-rest-pinned) builds a per-source app the profile runs.** `make dev LOCAL=dfe-fetcher` builds it from source and every fetcher instance runs that image. An instance `extends` the committed service and never sees the overlay, so the resolver writes the image into it. `make apply DEV=1` needs the same `LOCAL` to recreate an instance on it.
