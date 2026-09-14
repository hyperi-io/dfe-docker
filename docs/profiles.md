# Profiles: which are yours, and which come from Kubernetes

`service_profiles.yaml` holds two KINDS of profile, and the difference decides
where you edit one.

**Projected.** `slim` and `single` are the Compose renderings of the Kubernetes
tiers of the same name. Kubernetes is the master: the shape lives in dfe-infra's
`argocd/values/profile-<mode>.yaml`, and the block between the
`# BEGIN projected profiles` and `# END projected profiles` markers is rendered
from it. Editing that block by hand is drift -- CI fails and the next render
overwrites it.

**Hand-crafted.** Every other profile (`kafka-full`, `grpc-receiver`,
`kafka-filebeat`, ...) is a fine-grained data-plane shape with no Kubernetes
counterpart, and nothing generates them. They are yours to add, change and
delete.

## Which profile runs what

`X2` is two instances of that app: the shared passthrough one, plus the filebeat
source's own with the bundled filebeat program. `idle` is one instance started
with a config that gives it no work.

| Profile                           | Transport | dfe-archiver | dfe-fetcher | dfe-loader | dfe-receiver | dfe-transform-vrl | dfe-transform-vector | dfe-transform-elastic |
|-----------------------------------|-----------|:------------:|:-----------:|:----------:|:------------:|:-----------------:|:--------------------:|:---------------------:|
| `slim` (projected)                | gRPC      |              |             |     X      |      X       |                   |                      |                       |
| `single` (projected)              | Kafka     |     idle     |     idle    |     X      |      X       |       idle        |                      |                       |
| `kafka-fetcher`                   | Kafka     |              |      X      |     X      |              |                   |                      |                       |
| `kafka-full`                      | Kafka     |      X       |      X      |     X      |      X       |                   |                      |                       |
| `kafka-full-transform-vrl`        | Kafka     |              |      X      |     X      |      X       |         X         |                      |                       |
| `kafka-full-transform-elastic`    | Kafka     |              |      X      |     X      |      X       |                   |                      |           X           |
| `kafka-minimal`                   | Kafka     |              |             |     X      |              |                   |                      |                       |
| `kafka-receiver`                  | Kafka     |              |             |     X      |      X       |                   |                      |                       |
| `kafka-receiver-archiver`         | Kafka     |      X       |             |     X      |      X       |                   |                      |                       |
| `kafka-receiver-transform-vector` | Kafka     |              |             |     X      |      X       |                   |           X          |                       |
| `kafka-filebeat`                  | Kafka     |              |             |     X      |      X       |        X2         |                      |                       |
| `kafka-filebeat-vector`           | Kafka     |              |             |     X      |      X       |                   |          X2          |                       |
| `kafka-elastic-cisco-ios`         | Kafka     |              |             |     X      |      X       |                   |                      |           X           |
| `grpc-fetcher`                    | gRPC      |              |      X      |     X      |              |                   |                      |                       |
| `grpc-full`                       | gRPC      |              |      X      |     X      |      X       |                   |                      |                       |
| `grpc-minimal`                    | gRPC      |              |             |     X      |              |                   |                      |                       |
| `grpc-receiver`                   | gRPC      |              |             |     X      |      X       |                   |                      |                       |

Both projected profiles run the core data path -- the receiver and the loader --
plus the engine, the UI and HyperDX from the `core` and `hyperdx` footprint keys.
`slim` runs nothing else. `single` adds one archiver, one fetcher and one
transform-vrl, each started with a config that gives it no work: they are Ready,
serve health and metrics, open no broker connection and hold `pipeline_idle` at
1. `make post` asserts that on every start.

**One of each app, and no more.** Compose declares its services in this repo and
creates none at run time, so the apps a Kubernetes tier deploys one-per-source
cannot arrive with a source here. `single` therefore starts one of each in
advance, and an operator turns one on by giving it work. A second fetcher source
or a second transform source needs Kubernetes, and dfe-engine refuses it at save
rather than writing a definition nothing runs.

## Why the two tiers are projected

`slim` and `single` are the same deployment tier expressed twice: the same
transport, the same components, the same reason for existing. When they drift,
the Compose box stops being a faithful small version of the Kubernetes one, and
the difference is only found when something behaves differently on the two.

## Re-rendering

The renderer is dfe-infra's, so it reads the profile values beside the files it
reads them from:

    make render-profiles DFE_INFRA_DIR=/path/to/dfe-infra
    make check-profiles  DFE_INFRA_DIR=/path/to/dfe-infra

`DFE_INFRA_DIR` must be set explicitly -- nothing probes for a sibling checkout.
Without it, `check-profiles` reports SKIPPED rather than passing: a check that
cannot read the master cannot say the copy is current. CI checks out dfe-infra
itself, and skips the same way when the run has no token for it.

## What the projection maps

| Compose key | Where it comes from |
|---|---|
| `transport` | `kafka.mode` -- `disabled` is `grpc`, any other mode is `kafka` |
| `clickhouse` | the tier deploys a warehouse (every Kubernetes profile does) |
| `core` | `dfe-engine` and `dfe-ui` are default in `apps.yaml` |
| `hyperdx` | `hyperdx` is default in `apps.yaml` |
| `kafbat` | `kafbat.enabled`, and only on the `kafka` transport |
| `otel` | the tier deploys the collector (every Kubernetes profile does) |
| `services[]` | every other app `apps.yaml` makes default in `docker-<mode>` |
| `services[].config_path` | `<app>/<transport>.yaml` under `config/` |

WHICH apps a projected profile runs is dfe-infra `apps.yaml`'s `default_in` for
the matching `docker-<mode>` profile. That is the one place the default
composition is declared, for Kubernetes and Compose alike, so adding an app to
the default path is a manifest edit and a re-render rather than a list per
platform.

Replica counts, KEDA dials and operator choices do not cross: Compose runs one
of each and has no autoscaler, so what survives the projection is WHICH
components run, not how many.

## Changing a projected profile

Change the master, then re-render:

1. In dfe-infra, edit `argocd/values/profile-<mode>.yaml` for the tier's shape,
   or `apps.yaml`'s `default_in` for which apps it runs.
2. `make render-profiles DFE_INFRA_DIR=...` here.
3. Commit both, so the two repos move together.

A shape that suits Compose and not Kubernetes is not a change to these two --
add a hand-crafted profile beside them instead.
