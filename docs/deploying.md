<!--
Project:   dfe-docker
File:      docs/deploying.md
Purpose:   Deploying a stack and moving it to a newer one
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Deploying, and moving to a newer stack

A repeatable deploy turns ONE file and pins ONE version. This covers both, plus
what an upgrade actually does to a running stack.

Running the thing day to day is [operating.md](operating.md).

## The deployment dial: one file the deploy reads

A repeatable deploy turns ONE file. `deployment.example.yaml` is the template;
copy it to `deployment.yaml` (gitignored, never committed) and populate it. That
copy is the SSoT for the deploy -- registry, pinned version, service footprint,
host exposure, the broker, and where the pull credential comes from -- and `make
dial` renders its docker-vm slice into `.env`. A redeploy is a dial edit, not a
hunt through `.env`:

```bash
make dial && make stack && make ci
```

`make dial` writes the deploy-controlled keys over the `.env` that `make init`
generated, `make stack` pins the certified image set from the dial's
`version.pin`, and `make ci` pulls and starts it. Both `stack` and `ci` depend
on `make login`, so registry auth happens on the way through.

Two properties keep the file safe to hand around. **Secrets are references,
never values** -- `secrets.backend` and `secrets.ref` name WHERE the GHCR pull
credential lives (OpenBao by default), and whoever deploys resolves it. On a
lone box you set `DFE_GHCR_USERNAME` and `DFE_GHCR_TOKEN` in `.env` by hand; in
the estate a thin caller reads them from the backend and injects them, so the
dial itself carries no token. **Estate endpoints stay blank** -- an empty
`endpoints.clickhouse_host` means "use the in-stack container", and you set it
(or let the caller inject it) only to point the engine at an external warehouse.

`make login` authenticates docker and oras to `registry` from those two `.env`
keys. `scripts/ghcr_login.py` pipes the token on stdin, so it never reaches a
make variable or the process list, and it is a no-op when the keys are unset --
a daemon that authed out of band is not re-authed -- which is why `stack` and
`ci` depend on it unconditionally.

This file is the docker-vm SLICE. The canonical superset -- docker-vm plus the
Kubernetes and cloud dials, and the schema -- lives in dfe-infra, but a lone
dfe-docker clone deploys from its own `deployment.yaml` alone, with no dfe-infra
checkout needed.

## Staying current: pinned or track-latest

A box stays current one of two mutually exclusive ways. `make modes` states both
and reports which one THIS checkout is on -- ask the stack rather than this page,
for the same reason `make limits` exists.

```bash
make modes                                          # the contract + this box's mode
python3 ops/daemon-update/self_update.py --dry-run  # the live latest-vs-applied
```

**Pinned (default).** `make stack VERSION=X.Y.Z && make ci` pins the whole
certified set from the signed stack-manifest and starts it. It hard-fails rather
than ever pull `latest`. `make dial` sets the pin from the deployment dial's
`version.pin`, so a redeploy is a dial edit plus `make dial && make stack && make
ci`. This is the production-safe default: nothing moves until you move it.

Per image, the pin is an explicit release tag plus the digest that tag resolved
to (e.g. `DFE_ENGINE_VERSION=v1.17.13@sha256:50c4428...`), so a retagged image
cannot change what a pinned box runs. That now covers the hyperdx fork too. Where
the SSoT has no digest for an image the render emits a bare tag instead, which
downgrades the pin without announcing it. The registries also publish `latest`
and `sha-<commit>` tags for every
image, the hyperdx fork included -- none of them is ever consumed here, so what
a release publishes and what a deploy runs stay two separate decisions.

**Track-latest (opt-in).** `ops/daemon-update/install.sh` installs a systemd timer
that discovers the newest certified stack tag and runs the SAME `make stack` +
`make ci`, but only when something newer has shipped. It never pulls `latest`. By
default it tracks STABLE releases only; `DFE_UPDATE_ALLOW_PRERELEASE=1` (commented
in the service unit) also takes `-rc` builds. See
[ops/daemon-update/README.md](../ops/daemon-update/README.md).

The daemon fast-forwards the checkout before pinning, because a stack version is
images plus the compose that runs them. It refuses rather than pull over
uncommitted changes to tracked files, and `DFE_UPDATE_SKIP_GIT_PULL=1` turns the
refresh off for a checkout managed another way. On the pinned path that is your
job: `git pull` before `make stack`, or you get new images under an old compose
file.

The two are mutually exclusive: a track-latest box lets the daemon own the
version, so leave `version.pin` out of the dial there. `make modes` calls it
`AMBIGUOUS` if it finds both set.

> Until a GA stack ships, the only published tag is a pre-release, so a
> stable-only track-latest daemon finds nothing to apply -- pin explicitly, or set
> `DFE_UPDATE_ALLOW_PRERELEASE=1` knowingly.

## Upgrading an existing deployment

Two changes bite an existing stack. Neither is silent if you read this; both are
silent if you do not.

**ClickHouse moved to a named volume.** Its data previously lived in the
container's writable layer and died with `docker compose down`. It now mounts
`clickhouse-data:/var/lib/clickhouse`. On the first `make ci` after upgrading,
the container is recreated against an **empty** volume -- anything still in the
old writable layer is not migrated and will appear to have vanished. Export it
before upgrading if it matters.

**Most published ports moved from `0.0.0.0` to `127.0.0.1`.** Anything
reaching this box from elsewhere -- a remote `clickhouse-client`, a Prometheus
scrape of `:9090-9096`, a colleague's browser -- gets `connection refused` with
no hint as to why. `DFE_BIND_HOST=0.0.0.0` restores the old behaviour, having
read the auth section above.

**The DFE UI moved from `0.0.0.0` to `127.0.0.1` as well.** Every web UI now
binds `DFE_BIND_SCOPE`, which defaults to `localhost` for a developer
workstation, so `:3000` stops answering from off the box. A VM or small deploy
sets `DFE_BIND_SCOPE=all` (or `docker.bind_scope: all` in the deployment dial) to
publish the UIs on every interface --
[operating.md](operating.md#web-uis-one-scope-dial-one-kill-switch).

**`CLICKHOUSE_PASSWORD` is now generated, not blank.** `make init` mints one, so
an upgraded stack that runs `make init` (to pick up new `.env.example` keys) gets
a real password. ClickHouse stores the default-user credential in its data volume
on first init, so a pre-existing `clickhouse-data` volume still expects the OLD
(blank) password and the loader/engine/HyperDX now authenticate with the new one
-- every query fails `Authentication failed`. Two ways through:

- Keep the old behaviour: set `CLICKHOUSE_PASSWORD=` (blank) in `.env` before
  starting. `make init` only tops up a MISSING key, so an explicit blank is kept.
- Adopt the password: set the ClickHouse default user to the generated value once
  (`ALTER USER default IDENTIFIED BY '<value>'` against the running instance, or
  recreate the volume if it holds nothing you need), then restart the stack.

A fresh deployment has neither problem -- the volume is created with the generated
password from the start.

## Related

- [operating.md](operating.md) -- exposure, limits, secrets, persistence
- [observability.md](observability.md) -- health, self-telemetry, the self test
- [troubleshooting.md](troubleshooting.md) -- diagnosing a stack that misbehaves
