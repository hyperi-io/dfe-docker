<!--
Project:   dfe-docker
File:      docs/gnmi.md
Purpose:   Streaming gNMI telemetry from network devices into the otel tables with gnmic
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Network device telemetry over gNMI

Network devices stream telemetry over gNMI. The opt-in `gnmic` service subscribes to them and pushes what it reads as OTLP metrics to the stack's collector at `otel-collector:4317`, which writes the otel tables. That is the self-monitoring path and not DFE ingest, so there is no source to create and no table of your own. It runs under the sidecar limits (`DFE_SIDECAR_CPUS`, `DFE_SIDECAR_MEMORY`) and keeps no state, so a restart resubscribes.

## Turn it on

1. Name the devices. `config/gnmic/gnmic.yaml` takes a `targets` map and gnmic exits until it holds one. A target dials TLS unless it sets `insecure: true` and takes `skip-verify`, `tls-ca`, `tls-cert` and `tls-key` as well.

   ```yaml
   targets:
     core-router-1:
       address: core-router-1.example.com:57400
       skip-verify: true
   ```

2. Put the login in `.env` as `DFE_GNMIC_USERNAME` and `DFE_GNMIC_PASSWORD`. A target without its own `username` and `password` uses them.
3. Set `DFE_GNMIC_ENABLED=true` in `.env` and run `make up`. gnmic pushes to the bundled collector, so a stack with the `otel` key off refuses the setting.

`DFE_GNMIC_CONFIG` names another file under `config/`. `DFE_GNMIC_VERSION` moves the image pin, which is a digest. The shipped config subscribes to `/interfaces/interface/state/counters` every 30s and more paths go under `subscriptions`. A device decides what a sample returns, so a subscription can send one snapshot and then only changes.

The rows land in the otel database, `dfe` unless `DFE_OTEL_DATABASE` says otherwise.

| What | Where |
|---|---|
| Gauges | `otel_metrics_gauge` |
| Values whose name matches `counter-patterns` | `otel_metrics_sum`, as cumulative sums |
| `ServiceName` | `gnmic` |
| `MetricName` | `gnmic_` then the gNMI path with `/` and `-` as `_` |
| `Attributes` | `source` (the target's name), `subscription-name` and the path's keys, such as `process_pid` |

A subscription path that names an origin keeps it in the metric name, as in `gnmic_openconfig:_system_processes_process_state_memory_usage`. The last sample per device and metric:

```sql
SELECT MetricName, Attributes['source'] AS device, max(TimeUnix) AS last_sample
FROM dfe.otel_metrics_gauge
WHERE ServiceName = 'gnmic'
GROUP BY MetricName, device
```

## Check it

`make test-gnmi` proves the path against openconfig's lemming device emulator. Start the stack on the emulator's config, then run it:

```bash
make up PROFILE=slim-grpc DFE_GNMIC_ENABLED=true DFE_GNMIC_CONFIG=../tests/gnmi/gnmic-emulator.yaml
make test-gnmi
```

It starts the emulator from `tests/gnmi/compose.yml` on the stack's network and restarts gnmic so it subscribes. It passes when `otel_metrics_gauge` holds `gnmic` rows stamped after the run began and fails after 120s with none. The emulator image is amd64 only. `GNMI_ARGS=--keep` leaves it running. `make test-e2e` never runs this test.
