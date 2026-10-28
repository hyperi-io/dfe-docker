<!--
Project:   dfe-docker
File:      examples/otlp/README.md
Purpose:   Export logs from a real OpenTelemetry Collector to dfe-receiver over OTLP and read them back
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# OpenTelemetry over OTLP

A real OpenTelemetry Collector tails six lines of a JSON app log and exports them over OTLP/HTTP to dfe-receiver. A source claims the collector's `service.name` and lands the rows in `dfe.checkout`. Any profile works. Start the stack and log the CLI in first, as [the examples README](../README.md) shows.

First, turn the OTLP listener on and recreate the receiver:

```bash
dfe apps config-put dfe-receiver default --changes '{"config.otlp.enabled": true}'
make apply SERVICES=dfe-receiver
```

It listens for gRPC on 4317 and HTTP on 4318. Neither port is published, so only containers on the stack's network reach them. Take OTLP from other machines by uncommenting the 4317 and 4318 lines under dfe-receiver's `ports` in `docker-compose.yml`.

Then the table. The receiver turns each log record into the fields HyperDX reads, such as `ServiceName`, `SeverityText` and `Body`. This schema picks four of them, and the source claims every record from the `checkout` service.

```bash
dfe schemas import --path meta/checkout --resource-type custom --current 1.0.0 --versions '{"1.0.0": {
  "date": "2026-10-28", "type": "model", "summary": "checkout service logs over OTLP", "columns": [
  {"name": "time", "type": "datetime", "use_case": "range", "expr": "@source: Timestamp", "comment": "When the app logged the line"},
  {"name": "service", "type": "string", "attribute": ["lowcardinality"], "use_case": "dimension", "expr": "@source: ServiceName", "comment": "OTel service.name"},
  {"name": "severity", "type": "string", "attribute": ["lowcardinality"], "use_case": "dimension", "expr": "@source: SeverityText", "comment": "OTel severity text"},
  {"name": "message", "type": "string", "use_case": "word_search", "expr": "@source: Body", "comment": "The log record body"}]}}'
dfe sources create --source checkout --display-name checkout \
  --match '{"field": "ServiceName", "operator": "equals", "value": "checkout"}' \
  --header '{"type": "common-header/timeseries", "version": "1.0.1"}' \
  --schema '{"meta_schema": "meta/checkout", "meta_schema_version": "1.0.0"}'
dfe sources deploy checkout
```

The loader takes up to a minute to start writing to a new table, so give it that. Then start the collector on the receiver's network with [otel-collector.yaml](otel-collector.yaml) and the sample log, [checkout.txt](checkout.txt). The config parses each JSON line, moves its `msg` into the record body, and sets `service.name` to `checkout`.

```bash
docker run -d --name evaluate-otelcol --network container:dfe-receiver \
  -v "$PWD/examples/otlp/otel-collector.yaml:/etc/otelcol-contrib/config.yaml:ro" \
  -v "$PWD/examples/otlp/checkout.txt:/samples/checkout.log:ro" \
  otel/opentelemetry-collector-contrib:0.164.0@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b
```

Give it about a minute, then:

```bash
docker exec dfe-clickhouse clickhouse-client --query "
  SELECT time, service, severity, message FROM dfe.checkout ORDER BY time FORMAT PrettySpace"
```

```text
                       time   service    severity   message

1.  2026-10-28 09:00:01.000   checkout   info       cart loaded
2.  2026-10-28 09:00:02.000   checkout   info       payment authorised
3.  2026-10-28 09:00:02.000   checkout   info       order placed
4.  2026-10-28 09:00:05.000   checkout   warn       stock low
5.  2026-10-28 09:00:07.000   checkout   info       cart loaded
6.  2026-10-28 09:00:09.000   checkout   error      payment declined
```

The parsed attributes, such as `user` and `sku`, are in `_json.LogAttributes` on the same rows.

Already running a collector? Add the `otlp_http` exporter from [otel-collector.yaml](otel-collector.yaml) to it, naming your receiver's address, and list it in your logs pipeline beside the exporter you have. Each exporter gets every record.

## Tear it down

The table and its rows stay until `make clean`, and so does the `meta/checkout` schema.

```bash
docker rm -f evaluate-otelcol
dfe --quiet sources delete checkout
dfe apps config-delete dfe-receiver default --path config.otlp.enabled
make apply SERVICES=dfe-receiver
```
