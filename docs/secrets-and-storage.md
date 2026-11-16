<!--
Project:   dfe-docker
File:      docs/secrets-and-storage.md
Purpose:   Where the stack keeps its secrets and its data, and what survives which command
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Secrets and storage

`make init` mints the secrets `GENERATED_SECRETS` in `scripts/init.py` names, into `.env` and `env/`, beside the credentials an operator supplies there (OIDC clients, fetcher keys). Named volumes hold the stack's data. `make down` keeps the volumes and `make clean` deletes them.

## make init mints the secrets, and make post enforces them

`make init` mints a random value for every key in `GENERATED_SECRETS` (`scripts/init.py`), topping up an existing file that predates one. Compose gives each a sentinel or empty default rather than a `${VAR:?}` hard-fail, because interpolation is not profile-gated and a hard-fail would abort `make down`. `CLICKHOUSE_KEEPER_PASSWORD` is the exception, for the reason below.

The check lives in the power-on self test instead. `make post` fails while a secret in `WEAK_SECRET_DEFAULTS` (`scripts/post.py`) that a running service reads is unset or at its default, and `make dev`, `make ci` and `make up` fail with it unless `DFE_POST_ENABLED=false` skips the self test.

`CLICKHOUSE_PASSWORD` guards the ClickHouse default user, which runs with `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`, full admin. ClickHouse keeps the password its volume was created with, so a new value in `.env` against an existing `clickhouse-data` fails every query with `Authentication failed` until you `ALTER USER default IDENTIFIED BY '<value>'` or recreate the volume. That is why `make infra` makes `.env` before it starts ClickHouse. `make post` skips the check when `CLICKHOUSE_HOST` points at an external instance, as its credential is the operator's.

Only dfe-engine connects with it. Every other service connects as its own user, which the engine creates on a password `make init` mints:

| `.env` key | User | Service | Reaches |
|---|---|---|---|
| `DFE_HUNT_RUNNER_CLICKHOUSE_PASSWORD` | `dfe_hunt_runner` | dfe-hunt-runner | `SELECT` and `INSERT` on the data database |
| `DFE_LOADER_CLICKHOUSE_PASSWORD` | `dfe_loader` | dfe-loader | `INSERT` on the data database, and its schema |
| `DFE_OTEL_WRITER_CLICKHOUSE_PASSWORD` | `dfe_otel_writer` | otel-collector | `INSERT` on the otel tables, and server health |
| `DFE_QUERY_READER_CLICKHOUSE_PASSWORD` | `dfe_query_reader` | The platform connection dfe-engine hands dfe-hyperdx | `SELECT` on the data database |

An existing `.env` takes a new key on the next `make init`, and the engine creates the user on its next start. Never commit `.env`.

## The Keeper identity is fixed for the life of the volume

`CLICKHOUSE_KEEPER_PASSWORD` is the identity ClickHouse's embedded Keeper creates every znode under, so compose will not start without it. Under any other value the server cannot read its own znodes and exits with code 231 at start. Putting the old value back in `.env` and running `make ci` recovers it whole.

If the old value is lost, stop ClickHouse, move `coordination/` in the volume aside (`docker compose run --rm --no-deps --entrypoint mv clickhouse /var/lib/clickhouse/coordination /var/lib/clickhouse/coordination.lost`, with `-f docker-compose.yml -f compose/storage.yml` before `run` when `DFE_DATA_ROOT` is set, so it reaches the same directory) and start it under the new value. Then run `SYSTEM RESTORE DATABASE REPLICA <db>` for each Replicated database and `SYSTEM RESTORE REPLICA <db>.<table>` for each replicated table. The rows survive, and the engine re-creates its SQL users and roles on its next start.

`.env` and the files in `env/` are created `0640`, or `0660` when the checkout root is setgid, which is how a checkout shared with the daemon user is marked. Others never get a bit. A rewrite keeps the group-write bit the file has and drops any bit for others. The setup is in [deploying.md](deploying.md#updating-a-daemon-vm-without-sudo).

## Named volumes hold all durable state

Every path an image declares a VOLUME gets a named volume, so a down and up never strands an anonymous one:

| Volume | Holds |
|---|---|
| `clickhouse-data` | The ClickHouse warehouse (`/var/lib/clickhouse`) |
| `kafka-apache-data` / `kafka-redpanda-data` | Broker log and offsets, per backend |
| `kafka-apache-secrets` / `kafka-apache-config` | Apache Kafka's TLS and property-file inputs, empty unless a deployment supplies them |
| `archiver-data` | dfe-archiver output (`/var/lib/dfe-archiver/archive`) |
| `dlq-spool` | Shared dead-letter spool (`/var/spool/dfe`) |
| `fetcher-cursors` | dfe-fetcher cursors (`/var/lib/dfe-fetcher`), one directory for the one fetcher |
| `transform-elastic-geoip` | dfe-transform-elastic's GeoIP databases (`/var/lib/dfe/geoip`), downloaded on first start to a volume because the image root is read-only |
| `dfe-engine-config` / `dfe-engine-schemas` | Engine config and schemas, seeded from the engine image on first run |
| `dfe-app-config` | What the engine renders each app's config into, one directory per app. Its own volume because `dfe-engine-config` holds the JWT signing key and the deploy repo, which no data-plane container should read |
| `dfe-engine-content` | Each app's config schema, which the `contract-<app>` one-shots write for the engine to read (`/app/content`) |
| `hyperdx-dashboards` | The dashboards the `dfe-dashboards` one-shot generates from the engine image for dfe-hyperdx to read (`/etc/hyperdx/dashboards`) |
| `hyperdx-documentdb-data` | dfe-hyperdx metadata store, DocumentDB's PostgreSQL cluster (`/data`) |
| `hyperdx-documentdb-tls` | The CA and gateway certificate `hyperdx-documentdb-tls` mints, renewed when it nears expiry |

`make down` stops containers and leaves every volume intact. `make clean` runs `docker compose --profile "*" down -v --remove-orphans`, and the `-v` deletes all of them, the warehouse included.

Every time-series table DFE deploys, the OTel tables included, keeps rows for `DFE_ENGINE_CLICKHOUSE_DEFAULT_TTL_DAYS` days, set in `env/engine.env`: 90 unless set, 0 to disable the default TTL. `make init` asks for it once, on a TTY, when it creates `env/engine.env`. A non-interactive run keeps the template's commented 90, and the key set in the environment pre-answers it. The engine's startup apply reconciles existing tables, so a change is an `env/engine.env` edit and a `make ci`. A source or a dfe-schemas definition that declares its own TTL keeps it. The same knob is `retention.default_ttl_days` in `deployment.yaml`.

ClickHouse and Kafka grow, and `/var/lib/docker` is rarely the disk sized for them. Set `DFE_DATA_ROOT` (in `.env` or the environment) and the ten volumes `compose/storage.yml` lists become binds onto directories under it, such as `${DFE_DATA_ROOT}/clickhouse` for `clickhouse-data`. The rest stay Docker-managed. The make targets create the directories and chain the overlay on every path, `make ci` included.

Switching an existing deployment does not migrate data between locations. `make clean` removes the volume objects but the bind directories keep their contents, so reclaiming the space is an explicit delete of `${DFE_DATA_ROOT}`.
