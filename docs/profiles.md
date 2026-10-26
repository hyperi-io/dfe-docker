# Profiles

This repo owns every profile in `service_profiles.yaml`. Nothing generates them.

## Which profile runs what

| Profile        | Transport | dfe-archiver | dfe-loader | dfe-receiver | dfe-fetcher | dfe-transform-elastic | dfe-transform-vector | dfe-transform-vrl |
|----------------|-----------|:------------:|:----------:|:------------:|:-----------:|:---------------------:|:--------------------:|:-----------------:|
| `slim-grpc`    | gRPC      |              |     X      |      X       |             |                       |                      |                   |
| `slim-kafka`   | Kafka     |              |     X      |      X       |             |                       |                      |                   |
| `single-grpc`  | gRPC      |              |     X      |      X       |      X      |           X           |          X           |         X         |
| `single-kafka` | Kafka     |      X       |     X      |      X       |      X      |           X           |          X           |         X         |

The last four columns run one container per source once a source created through the engine binds one.

Every profile also runs the engine, the UI, HyperDX and the self-monitoring collector, from its `core`, `hyperdx` and `otel` footprint keys. The Kafka profiles add the broker and Kafbat. The matching `.env` flag overrides each key ([configuration.md](configuration.md)).

**A name is `<tier>-<transport>`.** dfe-engine runs as `docker-slim` on a `slim-*` profile and as `docker-single` on a `single-*` one. It seeds its default apps from that tier. `slim` is the core data path alone. `single` adds the per-source apps. `single-kafka` also runs one archiver, which archives each source from its landing topic. `single-grpc` runs no archiver, so archiving needs a Kafka profile ([architecture.md](architecture.md#events-arrive-two-ways-and-leave-one)). That is a deliberate choice: on gRPC the receiver would have to fan every record out to the loader and the archiver as two destinations, each with its own at-least-once ack, and under docker that multi-destination path cost more than it was worth. On Kafka the archiver reads the landing topic, so it adds no work to the receiver. A source with archiving on, in a gRPC profile, makes the receiver retry a missing archiver and answer 503, so switch to `single-kafka` before turning it on. `resolve_profile.py` refuses any other name. An old one such as `single` fails with the profile that replaces it.

**One per source, as on Kubernetes.** The fetcher and the transforms run one instance per source. dfe-engine writes each source's config into its own directory and its name into `env/<app>.instances`. `scripts/instances.py` turns each name into a service in `.tmp/docker-compose.instances.yml`, which `make apply` starts.

**A key runs each per-source app.** No profile names a per-source app as a service. `fetcher_enabled`, `transform_elastic_enabled`, `transform_vector_enabled` and `transform_vrl_enabled` each let dfe-engine start that app once per source. A key left out is false. Each instance reads the config dfe-engine renders for its source, so the keys do nothing while `DFE_CORE_ENABLED=false`. For the filebeat, Vector and Elastic transform scenarios, run [`make test-source`](developing.md#post-deploy-source-test----what-an-operator-does-first).

**An instance of an app the profile does not run is left out.** Every make target that resolves the profile warns on an `env/<app>.instances` naming one, with the app, its sources and the key to set. It starts none of them. dfe-engine still starts, so those sources can be deleted through it. `make clean` deletes every index with the engine's volume. `make sources` skips a seed source whose fetcher or transform the profile does not run.

**`make dev` builds every per-source app the profile runs.** Each instance runs the local image, because it `extends` the committed service and never sees an overlay, so the resolver writes the image into it. [`LOCAL`](developing.md#some-from-source-the-rest-pinned) narrows the build to what it names: `make dev LOCAL=dfe-fetcher` builds the fetcher alone. The build records what it made in `docker-compose.local.yml`. `make apply` puts a recreated instance back on that image, so the engine's `make apply SERVICES=<instance>` needs no `LOCAL`. `make ci` deletes the record.
