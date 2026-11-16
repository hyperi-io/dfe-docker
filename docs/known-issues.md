<!--
Project:   dfe-docker
File:      docs/known-issues.md
Purpose:   Open problems on the Compose stack whose symptoms mislead
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Known issues

These are open, and each has a symptom that misleads.

Several of these look like something else entirely, so read them before you go deep
into [troubleshooting](troubleshooting.md).

## `.env` shadows command-line environment variables through make

The Makefile does `-include .env`. GNU make gives makefile assignments precedence over environment variables and re-exports them to recipes. So for any key with a live (uncommented) assignment in `.env`, `VAR=x make ...` silently no-ops: the child process sees the `.env` value.

This bites hardest after `make stack` writes live `*_VERSION` pins into `.env` and after `make init` writes the generated secrets. Three ways around it:

```bash
make dev KAFKA_BACKEND=apache     # a make command-line variable wins and is exported
KAFKA_BACKEND=apache make -e dev  # -e flips the precedence
```

...or edit `.env`, which is the durable answer.

`make up` and `make demo` go further: they read `PROFILE=` and `VERSION=` from the make line alone, so `PROFILE=slim-grpc make up` starts whatever profile `.env` names, and a `PROFILE` exported in your shell never moves the stack. Write `make up PROFILE=slim-grpc` instead.

## A `_load` topic changes what the loader reads

The receiver produces to `main_land` and a transform to `main_load`. The loader reads `main_load` when it exists, and `main_land` when it does not.

```mermaid
flowchart LR
    accTitle: L4 Detail -- Loader topic choice
    accDescr: The loader resolves its topics. If main_load exists it reads main_load and drops main_land from its subscription. If not, it reads main_land.

    resolve(["Loader resolves<br/>its topics"])
    exists{"main_load<br/>exists?"}
    land(["Reads main_land"])
    load(["Reads main_load,<br/>drops main_land"])

    resolve --> exists
    exists -->|no| land
    exists -->|yes| load

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef decision fill:#FCF1D6,stroke:#9A6700,stroke-width:1px,color:#3D2A00
    classDef error fill:#FBE3D6,stroke:#C2410C,stroke-width:1px,color:#4A1A05
    class exists decision
    class load error
```

*Figure 1. L4 Detail -- Loader topic choice. The orange end is the trap when no transform reads `main_land`.* Up: [L2 Services -- DFE Compose stack](architecture.md#events-arrive-two-ways-and-leave-one).

`config/loader/kafka.yaml` names no topics, so the loader auto-discovers every topic ending in `_land` or `_load` (its `routing.topic_suffixes` default). scalo drops `<base>_land` from the subscription whenever `<base>_load` exists. `dfe-engine` creates the bootstrap topic set `dfe-schemas` declares, which is `main_land` and the five DLQ topics, so `main_load` exists only once a transform first produces to it.

Once a transform runs, the loader therefore reads `main_land` until that first write, then switches to `main_load`. The first records land twice: once raw, once transformed. Create the transform's `_load` topic before sending anything to avoid it. The e2e suite does, from each test's `expected_topics`.

**Do not close this gap by adding `main_load` to the bootstrap topic set.** The loader auto-discovers topics and scalo suppresses `<base>_land` whenever `<base>_load` exists, on the reasoning that a `_load` topic means a transform has produced the loadable form. Creating `main_load` on every Kafka profile therefore drops `main_land` from the subscription of every loader not running a transform, which is most of them. The symptom is maximally misleading: ingest returns 200, the receiver produces to `main_land`, the topic shows a rising high watermark and no rows reach ClickHouse.

Reverting the change does not restore a broker that has the topic.
`kafka-apache-data` and `kafka-redpanda-data` outlive the containers, so the topic
persists and keeps suppressing `main_land` on every later run. `rpk topic list` in the broker container shows whether it is there. Delete it:

```bash
docker compose exec kafka-redpanda rpk topic delete main_load -X brokers=kafka:9092
```

The e2e harness does this itself: `clean_topics` deletes the `_load`
sibling of every expected `_land` topic, so a run depends on the test definition
rather than on the broker's history.

When the injected events do not land, `make post` asks the loader which topics it
is fetching, so an empty subscription is reported as one. It cannot ask first: the
loader publishes a topic's lag only once it has committed an offset there, which a
stack started from empty volumes has not.
