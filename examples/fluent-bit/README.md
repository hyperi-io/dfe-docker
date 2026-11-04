<!--
Project:   dfe-docker
File:      examples/fluent-bit/README.md
Purpose:   Forward a JSON app log from a real Fluent Bit to dfe-receiver and read it back
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Fluent Bit

A real Fluent Bit tails five lines of a JSON app log and sends them to dfe-receiver over Forward, the protocol its `forward` output and Fluentd both speak. The receiver keeps each record's Fluent Bit tag, a source claims the `inventory` tag, and the rows land in `dfe.inventory`. Any profile works. Start the stack and log the CLI in first, as [the examples README](../README.md) shows.

First, turn the Forward listener on and recreate the receiver:

```bash
dfe apps config-put dfe-receiver default \
  --changes '{"config.fluent.enabled": true, "config.fluent.accept_unauthenticated": true}'
make apply SERVICES=dfe-receiver
```

A Forward frame carries no credential, so the receiver refuses to start the listener unless it either demands client certificates or is told to accept any client. Port 24224 is not published, so only containers on the stack's network reach it, and that is the only place `accept_unauthenticated` belongs. Before you add `24224:24224` to dfe-receiver's `ports` in `docker-compose.yml`, switch to client certificates the way [the Beats example](../beats/README.md) does, with the `config.fluent.tls` keys in place of `config.lumberjack.tls`.

Then the table. The schema names the fields each log line carries, and the source claims every record Fluent Bit tagged `inventory`.

```bash
dfe schemas import --path meta/inventory --resource-type custom --current 1.0.0 --versions '{"1.0.0": {
  "date": "2026-10-28", "type": "model", "summary": "inventory service log", "columns": [
  {"name": "level", "type": "string", "attribute": ["lowcardinality"], "use_case": "dimension", "expr": "@source: level", "comment": "Log level"},
  {"name": "message", "type": "string", "use_case": "word_search", "expr": "@source: msg", "comment": "Log message"},
  {"name": "warehouse", "type": "string", "attribute": ["lowcardinality"], "use_case": "dimension", "expr": "@source: warehouse", "comment": "Warehouse code"},
  {"name": "sku", "type": "string", "attribute": ["nullable"], "use_case": "exact_match", "expr": "@source: sku", "comment": "Stock keeping unit"}]}}'
dfe sources create --source inventory --display-name inventory \
  --match '{"field": "tag", "operator": "equals", "value": "inventory"}' \
  --header '{"type": "common-header/timeseries", "version": "1.0.1"}' \
  --schema '{"meta_schema": "meta/inventory", "meta_schema_version": "1.0.0"}'
dfe sources deploy inventory
```

The loader takes up to a minute to start writing to a new table, so give it that. Then start Fluent Bit on the receiver's network with [fluent-bit.yaml](fluent-bit.yaml) and the sample log, [inventory.txt](inventory.txt):

```bash
docker run -d --name evaluate-fluent-bit --network container:dfe-receiver \
  -v "$PWD/examples/fluent-bit/fluent-bit.yaml:/fluent-bit/etc/fluent-bit.yaml:ro" \
  -v "$PWD/examples/fluent-bit/inventory.txt:/samples/inventory.log:ro" \
  fluent/fluent-bit:5.1.5@sha256:2740f387f6e68fab66d28e4fa6a853387b9cab6ffb4cb78958f69bba4eb0e7b4 \
  -c /fluent-bit/etc/fluent-bit.yaml
```

Give it about a minute, then:

```bash
docker exec dfe-clickhouse clickhouse-client --query "
  SELECT _timestamp, level, message, warehouse FROM dfe.inventory ORDER BY _timestamp FORMAT PrettySpace"
```

```text
                 _timestamp   level   message                warehouse

1.  2026-10-28 09:10:00.000   info    stock count started    syd-1
2.  2026-10-28 09:10:04.000   info    sku counted            syd-1
3.  2026-10-28 09:10:05.000   warn    count mismatch         syd-1
4.  2026-10-28 09:10:09.000   info    sku counted            syd-1
5.  2026-10-28 09:10:12.000   info    stock count finished   syd-1
```

`_timestamp` is the `timestamp` field from each line. The parser in [fluent-bit.yaml](fluent-bit.yaml) leaves it in the record on purpose, and DFE reads the event time from there. Lines without one take the time they were loaded.

Already running Fluent Bit? Add the `forward` output from [fluent-bit.yaml](fluent-bit.yaml) beside the outputs you have, naming your receiver's address and matching the tags you want in DFE. Every output whose `match` fits a record gets a copy of it.

## Tear it down

The table and its rows stay until `make clean`, and so does the `meta/inventory` schema.

```bash
docker rm -f evaluate-fluent-bit
dfe --quiet sources delete inventory
dfe apps config-delete dfe-receiver default \
  --path config.fluent.enabled --path config.fluent.accept_unauthenticated
make apply SERVICES=dfe-receiver
```
