<!--
Project:   dfe-docker
File:      docs/broker.md
Purpose:   The Kafka broker: the two backends, Redpanda's developer mode and its licence
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

## Redpanda ships in developer mode

The default broker runs `--mode=dev-container --smp=1` with `REDPANDA_MEMORY=1G`: Redpanda's own developer mode, with relaxed fsync on one core. It is not a production configuration.

For a small-environment deploy, set `REDPANDA_MODE=production` with real `REDPANDA_SMP` and `REDPANDA_MEMORY` values, and raise `DFE_BROKER_MEMORY` **above** `REDPANDA_MEMORY`. Seastar reserves its `--memory` up front, so a container limit that does not exceed it gets the broker OOM-killed rather than backpressured.

Redpanda subtracts the host's `vm.min_free_kbytes` from the container limit before it checks `--memory`, so the usable figure is lower than the limit you set and varies by host. A 92 GiB host reserving 1.84 GiB left the 2560M default 675M against a 1G request. `redpanda/start.sh` reads both at start and passes the smaller of `REDPANDA_MEMORY` and what the limit leaves, with a WARN naming the `DFE_BROKER_MEMORY` that restores the full amount. Under 512M it refuses to start and names the value that would fit. Check a host with `sysctl vm.min_free_kbytes`.
