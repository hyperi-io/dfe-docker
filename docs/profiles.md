# Profiles

This repo owns every profile in `service_profiles.yaml`. Nothing generates them.

## Which profile runs what

| Profile                   | Transport | dfe-archiver | dfe-loader | dfe-receiver |
|---------------------------|-----------|:------------:|:----------:|:------------:|
| `slim`                    | gRPC      |              |     X      |      X       |
| `single`                  | Kafka     |      X       |     X      |      X       |
| `kafka-minimal`           | Kafka     |              |     X      |              |
| `kafka-receiver`          | Kafka     |              |     X      |      X       |
| `kafka-receiver-archiver` | Kafka     |      X       |     X      |      X       |
| `grpc-minimal`            | gRPC      |              |     X      |              |
| `grpc-receiver`           | gRPC      |              |     X      |      X       |

`slim` and `single` both run the core data path (the receiver and the loader) plus the engine, the UI and HyperDX from the `core` and `hyperdx` footprint keys. `slim` runs nothing else. `single` adds one archiver, which archives each source from its landing topic.

**One per source, as on Kubernetes.** The fetcher and the transforms run one instance per source. dfe-engine writes each source's config into its own directory and its name into `env/<app>.instances`. `scripts/instances.py` turns each name into a service in `.tmp/docker-compose.instances.yml`, which `make apply` starts.

**No profile starts a transform.** One runs once a source created through the engine binds it. For the filebeat, Vector and Elastic transform scenarios, run [`make test-source`](developing.md#post-deploy-source-test----what-an-operator-does-first).

**`fetcher_enabled: true` runs the fetchers.** No profile names dfe-fetcher as a service. A profile with this key runs one fetcher per source instead. `make sources` seeds the sources a fetcher polls only there and skips them elsewhere. Only `slim` and `single` may set it, because each fetcher reads the config dfe-engine renders for its source; `resolve_profile.py` refuses it on any other profile. Today it is `single` alone.
