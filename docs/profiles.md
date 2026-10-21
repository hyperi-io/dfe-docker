# Profiles

This repo owns every profile in `service_profiles.yaml`. Nothing generates them.

## Which profile runs what

| Profile        | Transport | dfe-archiver | dfe-fetcher | dfe-loader | dfe-receiver |
|----------------|-----------|:------------:|:-----------:|:----------:|:------------:|
| `slim-grpc`    | gRPC      |              |             |     X      |      X       |
| `slim-kafka`   | Kafka     |              |             |     X      |      X       |
| `single-grpc`  | gRPC      |      X       |      X      |     X      |      X       |
| `single-kafka` | Kafka     |      X       |      X      |     X      |      X       |

Every profile also runs the engine, the UI, HyperDX and the self-monitoring collector, from its `core`, `hyperdx` and `otel` footprint keys. The Kafka profiles add the broker and Kafbat. The matching `.env` flag overrides each key ([configuration.md](configuration.md)).

**A name is `<tier>-<transport>`.** dfe-engine runs as `docker-slim` on a `slim-*` profile and as `docker-single` on a `single-*` one. It seeds its default apps from that tier. `slim` is the core data path alone. `single` adds one archiver and a fetcher per source. On Kafka the archiver archives each source from its landing topic. On gRPC nothing sends it records ([architecture.md](architecture.md#events-arrive-two-ways-and-leave-one)). `resolve_profile.py` refuses any other name. An old one such as `single` fails with the profile that replaces it.

**One per source, as on Kubernetes.** The fetcher and the transforms run one instance per source. dfe-engine writes each source's config into its own directory and its name into `env/<app>.instances`. `scripts/instances.py` turns each name into a service in `.tmp/docker-compose.instances.yml`, which `make apply` starts.

**No profile starts a transform.** One runs once a source created through the engine binds it. For the filebeat, Vector and Elastic transform scenarios, run [`make test-source`](developing.md#post-deploy-source-test----what-an-operator-does-first).

**`fetcher_enabled: true` runs the fetchers.** No profile names dfe-fetcher as a service. A profile with this key runs one fetcher per source instead. `make sources` seeds the sources a fetcher polls only there and skips them elsewhere. Each fetcher reads the config dfe-engine renders for its source, so the key does nothing while `DFE_CORE_ENABLED=false`. The `single-*` profiles set it.
