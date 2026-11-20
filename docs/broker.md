<!--
Project:   dfe-docker
File:      docs/broker.md
Purpose:   The Kafka broker: the two backends, Redpanda's developer mode, its licence and Enterprise features
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# The Kafka broker

The `*-kafka` profiles run one broker, Redpanda by default or Apache Kafka with `KAFKA_BACKEND=apache`. The `*-grpc` profiles run none. Every broker variable is in [configuration.md](configuration.md#kafka).

The two backends are mutually exclusive and share the `kafka` network alias and the same host port, so nothing downstream knows which one runs. Every config addresses `kafka:9092`, and dfe-engine creates the bootstrap topic set over the Kafka protocol on both. The host listener is `19092` (`KAFKA_PLAINTEXT_HOST_PORT`) on `DFE_BIND_HOST`.

Compose interpolates every service before profiles filter anything, so `REDPANDA_VERSION` must still be pinned for the file to resolve on the Apache path. No Redpanda image is pulled or run. A Redpanda pin in `.env` is not a Redpanda deployment, but if your licence position needs zero reference to the artefact, that pin is what is left.

Redpanda's core is source-available under the BSL. It is not OSS. Local development and CI sit inside its Additional Use Grant. A partner or edge deployment needs a licence check of its own, and that check is a question for a human, not an assumption.

If it does not come back clean, `KAFKA_BACKEND=apache` selects Apache Kafka (Apache-2.0), and the Apache path pulls and runs no BSL-licensed artefact.

## What the config one-shot sets

A new Redpanda cluster starts on a 30-day trial licence with two Enterprise features on by default, continuous partition balancing and continuous core balancing. If either is still on when the trial lapses, the broker keeps its data but refuses upgrades to new feature releases.

The `kafka-redpanda-config` one-shot runs on every start, after the broker is healthy. It runs `redpanda/configure.sh`, which sets `partition_autobalancing_mode` to `node_add` and `core_balancing_continuous` to `false`, then exits 1 if `rpk cluster license info` still lists any Enterprise feature. POST runs the same licence check, so `make up`, `make dev` and `make ci` fail on a broker that has one on.

A running stack picks the fix up at its next `make up`, because the one-shot applies it to the existing cluster. To apply it now, without a restart:

```bash
docker compose exec kafka-redpanda rpk cluster config set partition_autobalancing_mode node_add
docker compose exec kafka-redpanda rpk cluster config set core_balancing_continuous false
docker compose exec kafka-redpanda rpk cluster license info
```

The broker and ClickHouse share the docker data disk, and every source and route adds a topic, so a cap on each partition cannot bound the total. The same one-shot sets two floors instead:

| Setting | Default | What it does |
|---|---|---|
| `REDPANDA_MIN_FREE_BYTES` | 20 GiB | The broker refuses writes once free disk falls below it, so the disk never fills. Producers back off, and the receiver spools |
| `REDPANDA_RETENTION_BYTES` | 10 GiB | The most one partition keeps, beside the seven-day time retention |

Both apply at the next `make up`, to a running stack and a fresh volume alike.

## Redpanda ships in developer mode

The default broker runs `--mode=dev-container --smp=1` with `REDPANDA_MEMORY=1G`: Redpanda's own developer mode, with relaxed fsync on one core. It is not a production configuration.

For a small-environment deploy, set `REDPANDA_MODE=production` with real `REDPANDA_SMP` and `REDPANDA_MEMORY` values, and raise `DFE_BROKER_MEMORY` **above** `REDPANDA_MEMORY`. Seastar reserves its `--memory` up front, so a container limit that does not exceed it gets the broker OOM-killed rather than backpressured.

Redpanda subtracts the host's `vm.min_free_kbytes` from the container limit before it checks `--memory`, so the usable figure is lower than the limit you set and varies by host. A 92 GiB host reserving 1.84 GiB left the 2560M default 675M against a 1G request. `redpanda/start.sh` reads both at start and passes the smaller of `REDPANDA_MEMORY` and what the limit leaves, with a WARN naming the `DFE_BROKER_MEMORY` that restores the full amount. Under 512M it will not start and names the value that would fit. Check a host with `sysctl vm.min_free_kbytes`.
