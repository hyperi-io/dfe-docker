<!--
Project:   dfe-docker
File:      examples/syslog/README.md
Purpose:   Send your own syslog to dfe-receiver over TCP and UDP and read it back
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Your own syslog

Four lines of syslog go in, two from `logger` and two from netcat, over TCP and UDP. They land parsed in `dfe.syslog`, one row per line. Start the stack and log the CLI in first, as [the examples README](../README.md) shows.

First, the source. The receiver labels every syslog line `_source: syslog`. This source claims that label and gives it a table with the syslog columns.

```bash
dfe sources create --source syslog --display-name Syslog \
  --match '{"field": "_source", "operator": "equals", "value": "syslog"}' \
  --header '{"type": "common-header/timeseries", "version": "1.0.1"}' \
  --schema '{"meta_schema": "meta/syslog", "meta_schema_version": "1.0.0"}'
dfe sources deploy syslog
```

The deploy answers with `"applied": true` and the `CREATE TABLE` it ran.

Then turn the syslog listener on and recreate the receiver:

```bash
dfe apps config-put dfe-receiver default \
  --changes '{"config.syslog.enabled": true, "config.syslog.accept_unauthenticated": true}'
make apply SERVICES=dfe-receiver
```

Plain syslog has no handshake to authenticate at, so the receiver refuses to start the UDP and TCP listeners without `accept_unauthenticated`. Port 514 is not published, so only containers on the stack's network can reach it. Take syslog from other machines by adding `514:514/tcp` and `514:514/udp` to dfe-receiver's `ports` in `docker-compose.yml`, and hold the senders to your own networks with `config.server.ip_filter`.

Now send four lines. Each sender runs in a throwaway container on the receiver's network, so `127.0.0.1:514` is the receiver.

```bash
LOGGER=debian:13-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
NETCAT=busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e

docker run --rm --network container:dfe-receiver "$LOGGER" \
  logger --server 127.0.0.1 --port 514 --tcp --tag evaluate "hello from logger over TCP"
docker run --rm --network container:dfe-receiver "$LOGGER" \
  logger --server 127.0.0.1 --port 514 --udp --tag evaluate "hello from logger over UDP"
docker run --rm --network container:dfe-receiver "$NETCAT" sh -c \
  'echo "<134>Oct 28 09:00:00 laptop evaluate: hello from netcat over TCP" | nc -w 2 127.0.0.1 514'
docker run --rm --network container:dfe-receiver "$NETCAT" sh -c \
  'echo "<134>Oct 28 09:00:00 laptop evaluate: hello from netcat over UDP" | nc -u -w 1 127.0.0.1 514'
```

And read them back:

```bash
docker exec dfe-clickhouse clickhouse-client --query "
  SELECT hostname, app_name, facility, severity, message
  FROM dfe.syslog ORDER BY _timestamp_load FORMAT PrettySpace"
```

```text
    hostname       app_name   facility   severity   message

1.  5d1da279ac89   evaluate   user       notice     hello from logger over TCP
2.  5d1da279ac89   evaluate   user       notice     hello from logger over UDP
3.  laptop         evaluate   local0     info       hello from netcat over TCP
4.  laptop         evaluate   local0     info       hello from netcat over UDP
```

`logger` stamps its own hostname, which here is the container id. The netcat lines carry `laptop` because the line says so. Give it a couple of seconds if a row is missing: UDP lines are batched, and the last one can trail the others.

## Tear it down

```bash
dfe --quiet sources delete syslog
dfe apps config-delete dfe-receiver default \
  --path config.syslog.enabled --path config.syslog.accept_unauthenticated
make apply SERVICES=dfe-receiver
```
