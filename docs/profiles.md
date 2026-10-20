# Profiles

This repo owns every profile in `service_profiles.yaml`. Nothing generates them.

## Which profile runs what

`X2` is two instances of that app: the shared passthrough one, plus the filebeat
source's own with the bundled filebeat program. `idle` is one instance started
with a config that gives it no work.

| Profile                           | Transport | dfe-archiver | dfe-loader | dfe-receiver | dfe-transform-vrl | dfe-transform-vector | dfe-transform-elastic |
|-----------------------------------|-----------|:------------:|:----------:|:------------:|:-----------------:|:--------------------:|:---------------------:|
| `slim`                            | gRPC      |              |     X      |      X       |                   |                      |                       |
| `single`                          | Kafka     |     idle     |     X      |      X       |       idle        |                      |         idle          |
| `kafka-full-transform-vrl`        | Kafka     |              |     X      |      X       |         X         |                      |                       |
| `kafka-full-transform-elastic`    | Kafka     |              |     X      |      X       |                   |                      |           X           |
| `kafka-minimal`                   | Kafka     |              |     X      |              |                   |                      |                       |
| `kafka-receiver`                  | Kafka     |              |     X      |      X       |                   |                      |                       |
| `kafka-receiver-archiver`         | Kafka     |      X       |     X      |      X       |                   |                      |                       |
| `kafka-receiver-transform-vector` | Kafka     |              |     X      |      X       |                   |           X          |                       |
| `kafka-filebeat`                  | Kafka     |              |     X      |      X       |        X2         |                      |                       |
| `kafka-filebeat-vector`           | Kafka     |              |     X      |      X       |                   |          X2          |                       |
| `kafka-elastic-cisco-ios`         | Kafka     |              |     X      |      X       |                   |                      |           X           |
| `grpc-minimal`                    | gRPC      |              |     X      |              |                   |                      |                       |
| `grpc-receiver`                   | gRPC      |              |     X      |      X       |                   |                      |                       |

`slim` and `single` both run the core data path (the receiver and the loader) plus the engine, the UI and HyperDX from the `core` and `hyperdx` footprint keys. `slim` runs nothing else. `single` adds one archiver, which archives each source from its landing topic, plus one transform-vrl and one transform-elastic, each started with a config that gives it no work: they are Ready, serve health and metrics, open no broker connection and hold `pipeline_idle` at 1. `make post` asserts that on every start.

**One per source, as on Kubernetes.** The fetcher and the transforms run one instance per source. dfe-engine writes each source's config into its own directory and its name into `env/<app>.instances`. `scripts/instances.py` turns each name into a service in `.tmp/docker-compose.instances.yml`, which `make apply` starts.

**`fetcher_enabled: true` runs the fetchers.** No profile names dfe-fetcher as a service. A profile with this key runs one fetcher per source instead. `make sources` seeds the sources a fetcher polls only there and skips them elsewhere. Only `slim` and `single` may set it, because each fetcher reads the config dfe-engine renders for its source; `resolve_profile.py` refuses it on any other profile. Today it is `single` alone.
