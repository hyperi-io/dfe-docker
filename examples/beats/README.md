<!--
Project:   dfe-docker
File:      examples/beats/README.md
Purpose:   Ship an nginx access log from a real Filebeat to dfe-receiver, alongside Elastic or instead of it
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Elastic Beats

A real Filebeat ships eight lines of nginx access log to dfe-receiver's Lumberjack listener, the protocol Beats already speak. The source matches on `data_stream.dataset`, the field every Elastic integration stamps, so dfe-transform-elastic runs Elastic's own nginx pipeline and the rows land in `dfe.nginx-access` with ECS columns. Start the stack on `single-kafka` and log the CLI in first, as [the examples README](../README.md) shows.

The source is the catalogue's `nginx-access` entry, on the Beats intake. Any other entry works the same way, and `dfe sources catalogue list` shows each one with the dataset it matches.

```bash
dfe sources create --source nginx-access --display-name nginx.access \
  --match '{"field": "data_stream.dataset", "operator": "equals", "value": "nginx.access"}' \
  --schema '{"meta_schema": "meta/elastic/ecs", "derived_schema": "derived/nginx/access", "additional_fields": "additional/nginx/access"}' \
  --transform '{"engine": "elastic", "variant": "filebeat.nginx_access.default"}' \
  --from-catalogue-item nginx-access --from-catalogue-intake beats
dfe sources deploy nginx-access
make apply SERVICES=dfe-transform-elastic-nginx-access
```

## Client certificates

The listener takes Beats only from a client holding a certificate your CA issued. These commands mint that CA, the receiver's server certificate and Filebeat's client certificate into `certs/beats/`, which git ignores. They need OpenSSL 3.

```bash
mkdir -p certs/beats && cd certs/beats && umask 077
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-384 -noenc -days 365 -sha384 \
  -keyout ca.key -out ca.crt -subj "/CN=DFE evaluation CA"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-384 -noenc -days 365 -sha384 \
  -keyout receiver.key -out receiver.crt -CA ca.crt -CAkey ca.key -subj "/CN=dfe-receiver" \
  -addext "basicConstraints=critical,CA:FALSE" -addext "extendedKeyUsage=serverAuth" \
  -addext "subjectAltName=DNS:dfe-receiver,DNS:localhost"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-384 -noenc -days 365 -sha384 \
  -keyout filebeat.key -out filebeat.crt -CA ca.crt -CAkey ca.key -subj "/CN=filebeat" \
  -addext "basicConstraints=critical,CA:FALSE" -addext "extendedKeyUsage=clientAuth"
cd ../..
```

The receiver reads its half from `env/receiver.env`, which is yours to edit and survives every `make apply`. Each PEM goes in as one quoted line:

```bash
pem() { awk '{printf "%s\\n", $0}' "$1"; }
{
  echo
  echo "DFE_BEATS_TLS_CA=\"$(pem certs/beats/ca.crt)\""
  echo "DFE_BEATS_TLS_CERT=\"$(pem certs/beats/receiver.crt)\""
  echo "DFE_BEATS_TLS_KEY=\"$(pem certs/beats/receiver.key)\""
} >> env/receiver.env
```

Switch the listener on, pointing it at those three variables:

```bash
dfe apps config-put dfe-receiver default --changes '{
  "config.lumberjack.enabled": true,
  "config.lumberjack.tls.enabled": true,
  "config.lumberjack.tls.client_auth": "required",
  "config.lumberjack.tls.ca_secret": "env:DFE_BEATS_TLS_CA",
  "config.lumberjack.tls.cert_secret": "env:DFE_BEATS_TLS_CERT",
  "config.lumberjack.tls.key_secret": "env:DFE_BEATS_TLS_KEY"}'
make apply SERVICES=dfe-receiver
```

Now ship the log. Filebeat runs on the receiver's network with [filebeat.yml](filebeat.yml), the sample log and its own certificate. `--strict.perms=false` is there because the config is mounted from your checkout, which Filebeat's own user does not own.

```bash
docker run -d --name evaluate-filebeat --network container:dfe-receiver \
  -v "$PWD/examples/beats/filebeat.yml:/usr/share/filebeat/filebeat.yml:ro" \
  -v "$PWD/examples/beats/nginx-access.txt:/samples/nginx-access.txt:ro" \
  -v "$PWD/certs/beats:/certs:ro" \
  docker.elastic.co/beats/filebeat:9.5.5@sha256:4b2042fcbeb781800bc7271d400f764bb7d670310dfd4fe03ade53d6c7ab78aa \
  filebeat -e --strict.perms=false
```

Give it about 30 seconds, then:

```bash
docker exec dfe-clickhouse clickhouse-client --query "
  SELECT source_ip, http_request_method, url_original, http_response_status_code, user_agent_name
  FROM dfe.\`nginx-access\` ORDER BY _timestamp FORMAT PrettySpace"
```

```text
    source_ip              http_request_method   url_original     http_response_status_code   user_agent_name

1.  ::ffff:203.0.113.10    GET                   /                                      200   Firefox
2.  ::ffff:203.0.113.10    GET                   /favicon.ico                           404   Firefox
3.  ::ffff:198.51.100.23   POST                  /api/cart                              201   Safari
4.  ::ffff:198.51.100.23   GET                   /products/42                           200   Safari
5.  ::ffff:192.0.2.77      GET                   /admin                                 403   Other
6.  ::ffff:192.0.2.77      GET                   /wp-login.php                          404   Other
7.  ::ffff:203.0.113.54    GET                   /static/app.js                         304   Chrome
8.  ::ffff:203.0.113.54    GET                   /checkout                              502   Chrome
```

Every field there came out of Elastic's nginx pipeline, and `DESCRIBE TABLE` on the same table lists the rest. Addresses are stored as IPv6, so an IPv4 client reads back as `::ffff:` and its address.

For a first look without certificates, the listener can take any client instead. Port 5044 is not published, so only containers on the stack's network reach it, and that is the only place this setting belongs. Go back to certificates before the port is published. In place of the certificate steps and the listener switch above:

```bash
dfe apps config-put dfe-receiver default \
  --changes '{"config.lumberjack.enabled": true, "config.lumberjack.accept_unauthenticated": true}'
make apply SERVICES=dfe-receiver
docker run -d --name evaluate-filebeat --network container:dfe-receiver \
  -v "$PWD/examples/beats/filebeat.yml:/usr/share/filebeat/filebeat.yml:ro" \
  -v "$PWD/examples/beats/nginx-access.txt:/samples/nginx-access.txt:ro" \
  docker.elastic.co/beats/filebeat:9.5.5@sha256:4b2042fcbeb781800bc7271d400f764bb7d670310dfd4fe03ade53d6c7ab78aa \
  filebeat -e --strict.perms=false -E output.logstash.ssl.enabled=false
```

The same query gives the same eight rows.

## Alongside Elastic, or instead of it

Filebeat sends to one output at a time, so the choice is where that output points.

- To swap, replace the Elastic output in your own `filebeat.yml` with the `output.logstash` block from [filebeat.yml](filebeat.yml), naming your receiver's address. Your inputs stay as they are.
- To run alongside, leave the Filebeat that feeds Elastic untouched and start a second one with this config, reading the same files. Each keeps its own registry, so both read every line.

An Elastic Agent integration stamps `data_stream.dataset` itself. A plain Filebeat input or module does not, which is what the `add_fields` processor in [filebeat.yml](filebeat.yml) is for.

Shipping from other machines needs the `5044` line under dfe-receiver's `ports` in `docker-compose.yml` uncommented, and the name they dial added to the receiver certificate's `subjectAltName`.

To tear it down, remove Filebeat and the source, and put the listener back to its defaults. The table and its rows stay until `make clean`. Then delete the three `DFE_BEATS_TLS_` lines from `env/receiver.env`, and `certs/beats/` once you are done with it.

```bash
docker rm -f evaluate-filebeat
dfe --quiet sources delete nginx-access
dfe apps config-delete dfe-receiver default --path config.lumberjack.enabled \
  --path config.lumberjack.accept_unauthenticated \
  --path config.lumberjack.tls.enabled --path config.lumberjack.tls.client_auth \
  --path config.lumberjack.tls.ca_secret --path config.lumberjack.tls.cert_secret \
  --path config.lumberjack.tls.key_secret
make apply SERVICES=dfe-receiver
```
