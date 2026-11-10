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
- make, git and Python 3 (standard library only)
- Network access to `ghcr.io/hyperi-io`, where the images are public and pull with no login

## Getting it running

```bash
make up                          # single-kafka, newest published stable stack
make up PROFILE=slim-grpc        # another profile
make up VERSION=2.2.1            # an older stack
```

`make up` creates `.env` and `env/`, generates the secrets, pins every image from the DFE stack manifest, starts the stack and prints the console login. Its first line says the profile and stack in use. `PROFILE=` and `VERSION=` are written to `.env`, so the next `make up` keeps them.

The stack refuses to start on unpinned images rather than quietly pulling `latest`, which is why `make up` pins first. On something you might later deploy, "which image am I running?" should never have a vague answer. `make init` and `make stack VERSION=X.Y.Z` do the two steps by hand.

`make ci` finishes by running the power-on self test, which injects a few marked
events and proves they reached ClickHouse. If it prints `PASS`, the pipeline
works - you have already seen the thing you came to see.

## Seeing your own data go through

Send an event to dfe-receiver, which every profile runs:

```bash
curl -X POST http://localhost:8080/ingest \
  -H 'Content-Type: application/json' \
  -d '{"_source":"main","message":"hello dfe"}'
```

`make post` reads the resolved profile and skips on one with no receiver, so if you are not sure which you have, run that instead.

Then look for it. `make init` gave ClickHouse's `default` user a generated
password, so the read needs it -- and a password on a command line is read back
out of `ps` and your shell history, so this reads it from `.env` and sends it as
a header:

```bash
python3 - <<'PY'
import urllib.request
from pathlib import Path

password = dict(
    line.split("=", 1)
    for line in Path(".env").read_text().splitlines()
    if "=" in line and not line.startswith("#")
)["CLICKHOUSE_PASSWORD"]
query = "SELECT * FROM dfe.main ORDER BY _timestamp_load DESC LIMIT 5 FORMAT Vertical"
print(
    urllib.request.urlopen(
        urllib.request.Request(
            "http://localhost:8123/",
            data=query.encode(),
            headers={"X-ClickHouse-User": "default", "X-ClickHouse-Key": password},
        )
    )
    .read()
    .decode()
)
PY
```

`_source` is what routes the event - the loader writes it to
`<database>.<_source>`, so `"_source":"main"` lands in `dfe.main`.

It will not appear instantly. The pipeline is asynchronous, and via Kafka
genuinely so; a couple of seconds is normal.

## Raw syslog, parsed per vendor

`make demo` streams Cisco ASA and FortiGate firewall logs into the running stack as raw syslog. dfe-transform-elastic works out which device sent each line, parses it and lands it in that vendor's own table. Palo Alto and Cisco IOS come back once [dfe-receiver#212](https://github.com/hyperi-io/dfe-receiver/issues/212) and [dfe-transform-elastic#133](https://github.com/hyperi-io/dfe-transform-elastic/issues/133) land.

It needs a profile that runs dfe-transform-elastic per source, `single-kafka` (the default) or `single-grpc`. On a clone with no stack running, `make demo` does the `make up` steps first. It refuses a profile without dfe-transform-elastic before it starts anything and never moves a running stack to another profile.

```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make demo                      # 20 lines a second over TCP, until you stop it or the receiver restarts
make demo DEMO_RATE=100        # replaces a running stream with a faster one
make demo-stop                 # removes the whole demo, as does `make down`
```

### One pack

The demo is one set of pieces, listed in [`resources/demo/demo.json`](../resources/demo/demo.json). `make demo` installs all of it and `make demo-stop` removes all of it. A re-run replaces the pieces and never duplicates them, and a piece that fails to install takes the rest out again. `make up` installs none of it.

| Piece | Where | What it is |
|-------|-------|------------|
| Sources | [`resources/demo/sources/`](../resources/demo/sources/) | One `POST /sources` body per file: the `demo-syslog` intake, the `cisco-asa` and `fortinet` tables, and two fetchers |
| Fetchers | `wikipedia-recentchanges`, `component-releases` | Pull beside the receiver's push. `wikipedia-recentchanges` polls the MediaWiki recent changes API every 5 seconds, asking each window once (about 3 changes a second, with the first poll bringing in the last hour). `component-releases` takes the latest GitHub release of each public DFE component every 15 minutes, to stay inside GitHub's 60 unauthenticated requests an hour |
| Archive | `demo-syslog` has `archive: true` | On a Kafka profile dfe-archiver writes the raw syslog lines to the `archiver-data` volume (`file:///var/data/archive`). dfe-archiver has no retention of its own, so the pack runs a pruner that keeps the archive to 15 minutes and 200 MB. A gRPC profile runs no archiver, so there the Source is created without archiving. The archiver has to read the `DFE_ARCHIVER_` env names, which the archiver stack 2.2.1 pins (v1.7.39) does not, so on that stack the archive check fails and the demo removes itself |
| Console | [`resources/demo/console/`](../resources/demo/console/) | A dashboard per source and one for the stack's health, and a saved search per source with a live tail. All are named `demo-` |
| Tables | `dfe.cisco-asa`, `dfe.fortinet`, `dfe.demo-syslog`, `dfe.component-releases`, `dfe.wikipedia-recentchanges` | `make demo-stop` leaves them, with their rows, because only the engine issues ClickHouse DDL and it never drops a table. The next `make demo` carries on in them, and `make clean` removes them with the volumes |

Each vendor replays about 6,000 sample lines from [`resources/demo/events/<source>/`](../resources/demo/events/), ten minutes at the default rate before a line repeats, which `python3 resources/demo/events/generate.py` regenerates. The run turns on dfe-receiver's syslog listener (UDP and TCP on port 514) and recreates what that changes. It returns once every vendor table has gained rows and the archive holds a file, and leaves the stream running:

```text
demo -   console demo-cisco-asa: http://localhost:8090/dashboards/<id>
demo -   console demo-wikipedia-live: http://localhost:8090/search/<id>?isLive=true
demo -   dfe.cisco-asa: +N rows
demo -   dfe.fortinet: +N rows
demo -   dfe.demo-syslog: +0 rows
demo -   dfe.main: +0 rows
demo -   dfe.component-releases: 0 rows
demo -   dfe.wikipedia-recentchanges: N rows
demo -   dfe-archiver archive of demo-syslog: N files
demo -     demo-syslog_land/default/2026/11/03/01-0001-<id>.jsonl.zst  77645 bytes
demo -     first line of the newest: {"message":"%ASA-6-302014: Teardown TCP connection ...
demo - WARNING no rows in 'dfe.component-releases' yet. A fetcher's first poll waits 900s and none land when the stack has no egress to its upstream
demo - PASS every vendor table gained rows and the archiver wrote files, ready to query in the console or HyperDX. The stream runs until `make demo-stop` or a receiver restart
```

`dfe.component-releases` fills about 15 minutes in, and the demo does not wait for it: an empty fetcher table only warns. A fetcher the profile cannot run or the engine cannot set up is skipped with a WARNING while the vendor demo runs on. A profile with no HyperDX gets no console pieces.

`dfe.demo-syslog` holds the lines no detector claimed. The replay script comes from the dfe-transform-elastic release the stack pins, so the demo needs a release with agentless detection and stops before changing anything on one without it. A `make dev` stack takes `make demo DEV=1`, which reads the script from `DFE_SRC_ROOT` when that is set.

## What you are looking at

| Where | What |
|-------|------|
| http://localhost:3000 | DFE UI |
| http://localhost:8081 | Kafka UI (topics, consumer groups, lag) |
| http://localhost:8123 | ClickHouse HTTP |
| http://localhost:8003 | dfe-engine API |

The console asks for a login; Kafbat, HyperDX and the ClickHouse port do not
authenticate you as a person. `make creds` prints the console login again.

The ingest ports bind all interfaces so anything can push to them. Everything
else -- every web UI included -- binds loopback by default, so the four links
above work on this box and nowhere else. `DFE_BIND_SCOPE=all` publishes the UIs
on every interface, and needs `DFE_EXTERNAL_ORIGIN` set to the address browsers
use -- `make` stops and says so otherwise. Read the auth section of
[operating.md](operating.md) before you widen anything.

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

It is **not** secured, though it is not open either. `make init` mints two
logins -- `admin` for the console and the engine API, and `breakglass` for
recovery -- and generates ClickHouse's password with them. `make creds` prints
the admin one on a terminal and names the `.env` key holding the other;
`access-summary.md`, written beside `.env` and readable only by you, carries
both. Kafbat and HyperDX answer to anyone who can reach them on these profiles,
the opt-in `auth` profile puts an OIDC sign-in in front of both, and nothing here
serves TLS until you turn [console TLS](configuration.md#console-tls-opt-in) on.
That is fine on your machine and on a throwaway demo box. It is not
fine on anything reachable by people you have not met.

If the proof of concept goes well and the box becomes something real - even
something small - read [operating.md](operating.md) before it does. Small is
still production, and the defaults here are tuned for a laptop.

## Where next

| You want to | Read |
|-------------|------|
| Understand how the pieces fit | [architecture.md](architecture.md) |
| Run this where others depend on it | [operating.md](operating.md) |
| Stand up a real deployment, or upgrade one | [deploying.md](deploying.md) |
| Know what the stack reports about itself | [observability.md](observability.md) |
| Look up a variable or a port | [configuration.md](configuration.md) |
| Work on a DFE component | [developing.md](developing.md) |
| Work out why something is broken | [troubleshooting.md](troubleshooting.md) |
