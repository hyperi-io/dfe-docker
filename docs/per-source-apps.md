<!--
Project:   dfe-docker
File:      docs/per-source-apps.md
Purpose:   How dfe-engine renders each app's config and how make apply starts per-source apps
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Per-source apps and make apply

On Kubernetes an app's chart turns its values overlay into a ConfigMap the pod mounts. Compose runs no chart, so dfe-engine does that step. It merges each instance's overlay over the committed `config/<app>/<transport>.yaml` and writes the app's config file, its programs and its enrichment tables into the `dfe-app-config` volume, one directory per app. Every app mounts that volume read-only at `/etc/dfe/apps`.

The committed file stays the deployment's (brokers, warehouse credentials, the spool), and the overlay wins wherever the two name the same key. So nothing deployment-specific is written into the deploy repo. `resolve_profile.py` turns this on wherever the core footprint runs. dfe-fetcher and the three transforms have no committed file: the overlay is their whole config, and their broker comes from `DFE_KAFKA_BOOTSTRAP_SERVERS` in `.env`.

Nothing restarts a container. A change the running app can't take in place is reported by the API as `restart required: make apply SERVICES=<app>`, and a change to its env file as `recreate required:` with the same command.

`make apply` creates a per-source container the engine has just declared, recreates one that exists, and removes the container of a source the engine has deleted. It finds those by the `dfe.instance-of` label, which generated services alone have. A delete names no command, so `make apply` with no `SERVICES` does that removal alone and starts nothing.

A source with a transform stops sharing `main_land`. The receiver routes it to `<source>_land`, and a transform reading that topic is the one that sees it. So a transform is deployed once per source, as the Kubernetes tier deploys one per Source definition, and its input topic, program directory and output topic all carry the source's name.

dfe-engine renders the instance's config into `dfe-app-config`. `make apply` then starts a service that extends the committed `dfe-transform-vrl`, `dfe-transform-vector` or `dfe-transform-elastic`, which have the broker env and nothing of a source's own.
