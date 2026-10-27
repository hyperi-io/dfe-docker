<!--
Project:   dfe-docker
File:      examples/README.md
Purpose:   Hand-fed data for a local stack, one directory per common source
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Feeding a local stack by hand

Each directory here gets one common source into a local DFE and shows you the rows. Every command was run against a fresh stack, and the counts shown are what it returned.

| Example | What goes in | How it arrives |
|---------|--------------|----------------|
| [syslog](syslog/README.md) | Lines from `logger` and netcat | Syslog over TCP and UDP |
| [beats](beats/README.md) | An nginx access log | Filebeat, shipping Lumberjack to dfe-receiver |
| [aws](aws/README.md) | CloudTrail and CloudWatch Logs | dfe-fetcher polling your AWS account |

## Before any of them

The Beats and AWS examples parse with dfe-transform-elastic, so run the `single-kafka` profile. Syslog works on any profile. [evaluating.md](../docs/evaluating.md) covers what each of these does, and `make up` ends with the self test, where `PASS` means data moves.

```bash
make init
make stack VERSION=X.Y.Z
echo 'DFE_PROFILE=single-kafka' >> .env
make up
```

Every example defines its source with the `dfe` CLI inside the engine container. Wrap it once per shell and log in as `admin`. It answers `You are now logged in as admin`, and the login lasts until the engine container is recreated.

```bash
dfe() { docker exec dfe-engine dfe "$@"; }
dfe login --url http://localhost:8000 --username admin --password "$(sed -n 's/^DFE_AUTH_LOCAL_ADMIN_PASSWORD=//p' .env)"
```

Rows are read straight out of ClickHouse:

```bash
docker exec dfe-clickhouse clickhouse-client --query "SHOW TABLES FROM dfe"
```

A source deploy or a listener switch prints the command that applies it, such as `restart required: make apply SERVICES=dfe-receiver`. Run that line as printed. Nothing here edits a compose file.

## Stopping

```bash
make down    # stop everything, keep the data
make clean   # stop everything and delete the volumes
```
