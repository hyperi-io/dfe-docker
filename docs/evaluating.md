<!--
Project:   dfe-docker
File:      docs/evaluating.md
Purpose:   First look at DFE - get one event through the pipeline and see it
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Taking DFE for a first look

**This applies to you if** you want to see DFE actually work - on your laptop, a
demo box, or a proof of concept - without committing to anything yet.

The goal here is narrow and worth stating: get one event in one end and see it
come out the other. Everything else can wait.

## What you need

- Docker and Docker Compose v2
- Python 3
- Access to `ghcr.io/hyperi-io` (the images are private)

## Getting it running

```bash
make init                  # create .env and env/, generate secrets
make stack VERSION=X.Y.Z   # pin every image from the DFE stack manifest
make ci                    # pull and start
```

`make stack` is not optional. The stack refuses to start on unpinned images
rather than quietly pulling `latest`, so a fresh checkout without it fails with a
message telling you exactly which pin is missing. That is deliberate: on
something you might later deploy, "which image am I running?" should never have a
vague answer.

`make ci` finishes by running the power-on self test, which injects a few marked
events and proves they reached ClickHouse. If it prints `PASS`, the pipeline
works - you have already seen the thing you came to see.

## Seeing your own data go through

Send an event. **Which port depends on your profile**, because not every profile
runs a receiver - the shipped default (`kafka-fetcher`) does not:

```bash
# Default profile (kafka-fetcher): the fetcher is the ingest edge.
curl -X POST http://localhost:8082/ingest/default \
  -H 'Content-Type: application/json' \
  -d '{"_source":"default","message":"hello dfe"}'

# Any profile that runs dfe-receiver (grpc-receiver, kafka-receiver, ...).
curl -X POST http://localhost:8080/ingest \
  -H 'Content-Type: application/json' \
  -d '{"_source":"default","message":"hello dfe"}'
```

`make post` works this out for you from the resolved profile, so if you are not
sure which you have, run that instead.

Then look for it:

```bash
curl 'http://localhost:8123/?query=SELECT%20*%20FROM%20dfe.default%20ORDER%20BY%20_timestamp_load%20DESC%20LIMIT%205%20FORMAT%20Vertical'
```

`_source` is what routes the event - the loader writes it to
`<database>.<_source>`, so `"_source":"default"` lands in `dfe.default`.

It will not appear instantly. The pipeline is asynchronous, and via Kafka
genuinely so; a couple of seconds is normal.

## What you are looking at

| Where | What |
|-------|------|
| http://localhost:3000 | DFE UI |
| http://localhost:8081 | Kafka UI (topics, consumer groups, lag) |
| http://localhost:8123 | ClickHouse HTTP |
| http://localhost:8003 | dfe-engine API |

The UI and the ingest ports bind all interfaces so you can reach them from
another machine. Everything else binds loopback by default - see
[operating.md](operating.md) if you need to change that, and read the auth
section there first, because there is no authentication anywhere in this stack.

## Stopping

```bash
make down    # stop the containers, keep the data
make clean   # stop everything and DELETE the volumes, including ClickHouse
```

`make clean` really does delete the warehouse. On a laptop that is usually what
you want; anywhere else, read it twice.

## What this is and is not

It **is** the same image, from the same registry, that a Kubernetes deployment
would run. There is no separate "demo build" - what you are evaluating is the
real thing, packaged differently.

It is **not** secured. There is no login on anything, by design: authentication
is a Kubernetes concern in DFE, so Docker mode runs open. That is fine on your
machine and on a throwaway demo box. It is not fine on anything reachable by
people you have not met.

If the proof of concept goes well and the box becomes something real - even
something small - read [operating.md](operating.md) before it does. Small is
still production, and the defaults here are tuned for a laptop.

## Where next

| You want to | Read |
|-------------|------|
| Understand how the pieces fit | [architecture.md](architecture.md) |
| Run this where others depend on it | [operating.md](operating.md) |
| Work on a DFE component | [developing.md](developing.md) |
| Work out why something is broken | [troubleshooting.md](troubleshooting.md) |
