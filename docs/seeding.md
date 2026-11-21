<!--
Project:   dfe-docker
File:      docs/seeding.md
Purpose:   Seeding a running stack with starter sources, rules, hunts, orgs and accounts
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Seeding a starter set

`make sources`, `make rules` and `make hunts` create a starter set of detection content on a running stack through the engine API, from `resources/seed/`. `make hunts` creates the orgs and rules, because the engine rejects a hunt without them. A rule over an undeployed source reads nothing until its source is deployed. A hunt over a table that does not exist is not created, because the runner would fail every run. `make hunts` creates it once `make sources` has made the table, on a profile that runs the source.

## What each target creates

| Target | What it creates |
|---|---|
| `make sources` | `aws-cloudtrail`, `aws-guardduty` and `aws-securityhub` (polled by the one dfe-fetcher, which takes a single `aws` source until hyperi-io/dfe-fetcher#289, so the other two are skipped with the engine's reason) and `syslog`, which takes any record sent to the receiver with `_source: syslog`. Each lands in `dfe.<source>`. Sources a profile cannot run are skipped, so [`slim-grpc`](profiles.md#which-profile-runs-what) gets `syslog` alone |
| `make rules` | `aws-guardduty-high-severity`, matching a GuardDuty finding rated 7 or above |
| `make hunts` | The org, that rule and the hunt `aws-guardduty-high-severity` once `dfe.aws-guardduty` exists, running it every five minutes into `dfe.detection` |
| `make events` | Two fake GuardDuty findings from `resources/seed/events/`, rated 8.0 and 2.0, so the hunt's next run detects the first and leaves the second |
| `make orgs` | The org `acme` (Acme Corp), named as the deployment's own (`resources/seed/organisation.yaml`) unless the engine names one already, so the setup wizard skips its organisation step. The seeded AWS fetchers stamp their records with it and the seeded hunt reads it |
| `make accounts` | That org, then the admin account `ops` in `dfe-admins` |

`make events` posts its records to dfe-receiver tagged with the source each file is called after, which routes them over the stack's transport, Kafka or gRPC. It waits until the source's table holds them and fails if they do not arrive, so it needs the source deployed by `make sources`, dfe-receiver and an engine that renders the apps' config. It skips the records of a source `make sources` skipped. It sends the records again on every run. The hunt that finds them runs in dfe-hunt-runner.

`make events`, `make post` and `make test-e2e` send records with the token of the ingest credential `dfe-docker`, bound to `acme`. The first run creates it and records the token in `env/engine.env` as `DFE_INGEST_TOKEN`. A credential the engine holds without a recorded token is rotated. dfe-receiver refuses unauthenticated records once a credential names its HTTP listener, so the first run restarts it if it still takes any token.

A run of any other target creates only what the engine lacks and leaves an object it holds as it is, so editing a seed file changes nothing on a stack that has the object. Delete the object and the next run creates it from the file. A source is deployed as it is created, which creates its table. A source the engine holds undeployed is deployed. `make sources` ends by asking the engine to bring every app in step with the sources, which retries a deploy its apps did not take.

Every target logs in as `admin` with `DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD` from `env/engine.env` and replaces an issued password as `make post` does. Once the setup wizard has retired `admin`, they ask at the terminal for an account that can write what the target seeds. They ask once per run and store the login nowhere. Without a terminal they stop. An account still on the password it was issued has to change it in the console first.

Every fetcher source runs in the one `dfe-fetcher` container, from the file the engine merges. The deploy prints the command that applies a source the running fetcher cannot take in place, such as `restart required: make apply SERVICES=dfe-fetcher`. The fetcher reads `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` from `env/fetcher.env` and cannot fetch until both are set. Compose reads that file only when it creates a container, so set the keys before `make apply` or run it again after setting them.

## Seeding orgs and accounts

`make accounts` creates the orgs first, because the engine rejects an account naming an org it does not hold. An account file holds no password. A new account gets a 32-character one, recorded in `env/engine.env` as `DFE_SEED_ACCOUNT_<USERNAME>_PASSWORD` before the account is created. `make creds` hands it over the way it hands over `admin`. A volume wipe deletes the orgs and accounts, so run both targets again after one. An account comes back on the recorded password.
