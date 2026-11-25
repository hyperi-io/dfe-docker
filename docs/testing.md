<!--
Project:   dfe-docker
File:      docs/testing.md
Purpose:   The e2e suite and its detection and console tests
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Proving the pipeline with the e2e suite

Every test here injects uniquely marked events and reads the marker back out of ClickHouse, so none passes on a stack that started but does not move data. The power-on self test that ends every `make dev`, `make ci` and `make up` is in [self-test.md](self-test.md). The outage, source and static checks are in [checks.md](checks.md).

## The e2e suite runs one stack per test

```bash
make test-e2e
make test-e2e E2E_TESTS="simple-receiver-to-loader-grpc simple-receiver-to-loader-kafka-with-archiver"
```

`tests/e2e/e2e-tests.yaml` is the definition. A `global:` block sets the data file (`tests/e2e/data/events.jsonl`), the mode (`ci` runs the registry images, `dev` builds local ones and chains the overlay that build writes) and `persistent_services`. Only ClickHouse survives between tests, so each test runs its own config against a fresh broker. The test stacks run as a compose project of their own, `<project>-e2e`, with `e2e-` before every container name, so no test reaches the volumes of the stack it found. Its engine writes custom keys and instance indexes under `.tmp/e2e/<run>/env` rather than `env/` and its apps read their custom keys from there. The loader config a test generates reaches its container as compose `configs:` content, which the daemon receives over its API. Committed configs and the engine's env directory stay bind mounts by host path, so against a remote `DOCKER_HOST` the checkout must sit at the same path on the daemon host. The run stops before `up` naming any bind source missing on the machine running it. That project and its volumes are removed when the run ends and again when the next run starts, in case a killed run left them. Each entry under `tests:` names a `service_profiles.yaml` profile, optionally `expected_topics` (created and cleaned per run, then verified) and `config_overrides` keyed by service name.

The runner brings ClickHouse and `dfe-engine` up first and gates on the engine's health, because the engine provisions `dfe.main` and registers the schemas the loader pre-warms. Then it asserts **two** things per test: the row-count delta from a per-test baseline and that those rows hold this run's marker in `_tags`. The delta alone would pass on somebody else's rows and the marker alone would not notice a partial load. A marker column it cannot locate is reported, never silently treated as a pass.

The suite's `global:` block sets `otel: false`, so a data-path test runs no collector. A test that sets `otel: true` adds a third assertion: the stack's own telemetry landing fresh in the `dfe.otel_*` tables. `expected_http` adds a fourth, a status and optional body check per URL. It makes `complete-single-node-stack` a whole-stack test rather than a data-path one.

An archiver that subscribes to the test's landing topic and archives to a `file://` destination must write a file there during the test, new or grown since a baseline taken before the send. The archive volume outlives the stack, so files from an earlier run never count.

The data file goes to the ingest edge 16 requests at a time. The receiver answers a request once its transport has taken it, the loader on gRPC and the broker on Kafka, so one at a time costs a round trip per event.


## The suite puts back the stack it found

The suite stops whatever stack the project is running, since both publish the same host ports, so it reads the project first, along with the config hash compose gives each service from its files and environment at that moment. When it ends, on a pass, a failure or a SIGTERM, it starts each service that was running or had run to completion again, from the compose files it was started from. A SIGKILL or a host failure skips the restart, so start the stack again the way you started it. A last verdict fails if one comes back on a config hash other than the one compose gave at the start.

A container keeps the hash it was created with, so one with a `.env` or compose file changed since is listed in a warning and comes back on the current config. `make ci` leaves the engine that way whenever `make post` rotates the admin password into `env/engine.env`. Run the suite as the user that started the stack: `make` writes that user's GID into the engine's config and under console TLS its UID and GID into both proxies', so another user shows up in that warning.

Under [console TLS](configuration.md#console-tls-opt-in) each test's stack chains the fragments `make ci` does, the exposure opt-outs and container logs included. Its network takes the same-size subnet beside `DFE_NETWORK_SUBNET`, so a found network left behind cannot block it. The console checks go to `https://<DFE_EXTERNAL_ORIGIN host>:<DFE_UI_PORT>` and verify its certificate by chain and name, against `DFE_PROXY_CA_BUNDLE` when a private CA signed it. A certificate that does not verify fails the check with the variable to change.

Two things about the runner to know before you debug it:

- It waits on the services it lists, never on a whole profile. `docker compose up --wait` counts a container that EXITS as a failure even on exit 0 and the init one-shots must exit. Waiting on the profile sweeps them in and fails regardless of broker health.
- It deletes the `_load` sibling of every expected `_land` topic before each run. Broker volumes outlive containers, so a `main_load` left by any transform run suppresses `main_land` for every non-transform loader thereafter. See [known-issues.md](known-issues.md#a-_load-topic-changes-what-the-loader-reads). Deleting it per run makes a run depend on the test definition, not on broker history.

## Rules and hunts detect exactly the matching events

```bash
make test-e2e E2E_TESTS=rules-and-hunts
```

A test with a `detection:` block makes the `e2e` organisation when absent, a rule (`POST /rules`) and a one-minute hunt for it over `dfe.main` into `dfe.detection` (`POST /hunts`) and the runner must commit its first window. It sends the `matching` events and `other` near misses, with `"_org_id": "e2e"`, as the runner reads only the org's rows. Its receiver config, `tests/e2e/configs/receiver-e2e-org.yaml`, keeps a sender's `_org_id` and binds the HTTP listener to `default` and `e2e`, so the field survives the receiver's org clamp. It queues a run (`POST /hunts/{name}/run`) and waits for `last_run` to pass the last event's load time.

The hunt's rows must name, by `matched_uuid`, every landed `matching` row once and no `other` row, under the rule and severity the test gave. The hunt, rule and any org it made are deleted and asserted gone. The rows stay, tagged with the run marker.

The `core` footprint starts `dfe-hunt-runner`, as `make dev` does. On a profile with it off the test skips with that reason and a skip fails the run.

## Search to rule to hunt drives the console

```bash
make test-e2e E2E_TESTS=search-rule-hunt
```

A `detection:` block carrying `from_search` makes its rule the way an analyst does, in a browser. It adds dfe-hyperdx to the stack, signs in to the console as the stack's admin and opens Observe search on `main`. The search bar gets the block's `search`, held to the run's marker and the side panel picks its `filter`. Then Create Rule and the rule page it opens.

The stored rule has to scan `dfe.main` with no SQL errors and a WHERE that holds both filters. The hunt over it is added and triggered from the console's own form and the verdict is the rules-and-hunts one: every matching event detected once, no near miss. Each filter alone lets a near miss through, so a filter lost between the search and the rule fails it.

The side panel only offers values rows hold, so both event sets are sent once before the search. The hunt's first window can reach those rows, so their near misses are judged and their matches are not.

The console holds every page behind its setup wizard, so the test first makes the `e2e` organisation and one user of the deployment's own (`e2e-analyst`), as the wizard would and deletes whichever of the two it made once the hunt and the rule are gone. uv installs Playwright from the script's inline metadata and the test drives Google Chrome (`uvx playwright install chrome` where it is absent). `DFE_E2E_HEADED=1` shows the browser and `DFE_E2E_SHOTS_DIR` keeps the step screenshots, which otherwise go to a new temporary directory the log gives.

Under console TLS the suite first verifies the console certificate itself, then pins Chrome to that certificate's public key, because Chrome takes no CA bundle.

## The dfe-ui Playwright suite needs the e2e settings

The suite seeds its fixtures through the engine's `/api/e2e` routes, which exist only with `DFE_ENGINE_E2E_SERVER=true`. Its seeders refuse anything but `DFE_ENGINE_ENV=test`.

```bash
make e2e-posture         # DFE_ENGINE_ENV=test in .env, DFE_ENGINE_E2E_SERVER=true and the shipped admin password in env/engine.env
make e2e-posture-check   # exit 1 naming what is missing, the suite runs this first
make e2e-posture-off     # put back both files from before
```

`e2e-posture` copies each file it replaces to `<name>.pre-e2e` (0600) the first time and `e2e-posture-off` restores them. It stops (exit 2) on a `.env` that names a deployment, since it writes the shipped admin password over the real one. The engine reads both files when it starts, so recreate the stack after either switch.

## Related

- [developing.md](developing.md) - dev mode, profiles, the dev loop
- [checks.md](checks.md) - outage tests, the post-deploy source test, static checks
- [self-test.md](self-test.md) - the power-on self test
