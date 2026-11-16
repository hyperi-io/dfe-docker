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

A repeatable deploy turns ONE file. `deployment.example.yaml` is the template.
Copy it to `deployment.yaml` (gitignored, never committed) and populate it. That
copy is the SSoT for the deploy -- registry, pinned version, service footprint,
host exposure and the broker -- and `make dial` renders its docker-vm slice into
`.env`. A redeploy is a dial edit, not a hunt through `.env`:

```bash
make dial && make stack && make ci
```

`make dial` writes the deploy-controlled keys over the `.env` that `make init`
generated, `make stack` pins the certified image set from the dial's
`version.pin`, and `make ci` pulls and starts it. Both read the registry
anonymously, so neither needs a login.

Two properties keep the file safe to hand around. **Secrets are references,
never values** -- `secrets.backend` and `secrets.ref` name WHERE the deployment's
secrets live (OpenBao by default), and whoever deploys resolves them, so the dial
itself holds no secret. **Estate endpoints stay blank** -- an empty
`endpoints.clickhouse_host` means "use the in-stack container", and you set it
(or let the caller inject it) only to point the engine at an external warehouse.

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
to (e.g. `DFE_ENGINE_VERSION=v<version>@sha256:<digest>`), so a retagged image
cannot change what a pinned box runs, the dfe-hyperdx fork included. Where the
SSoT has no digest for an image, `make stack` rejects the render and names the
pin rather than write a bare tag. The registries publish `latest`
and `sha-<commit>` tags for every
image, the dfe-hyperdx fork included -- none of them is ever consumed here, so what
a release publishes and what a deploy runs stay two separate decisions.

**Track-latest (opt-in).** `ops/daemon-update/install.sh` installs a systemd timer that discovers the newest certified stack tag and runs the SAME `make stack` + `make ci`, but only when something newer has shipped. It never pulls `latest`. By default it tracks STABLE releases only. `DFE_UPDATE_ALLOW_PRERELEASE=1` takes `-rc` builds. `DFE_STACK_REPIN_IMAGES=1` on its service follows every DFE image release instead, a mix nobody certified. See [ops/daemon-update/README.md](../ops/daemon-update/README.md).

**Latest (development only).** `make stack VERSION=latest` pins the newest
published stack, read anonymously from the OCI registry, and moves forward on
every re-run, and `DFE_STACK_REPIN_IMAGES=1` repins each DFE image at its GitHub
Latest release. It is not a third deploy mode: `make modes` reports it as LATEST
rather than PINNED. Do not deploy off it -- pin a version.

The daemon fast-forwards the checkout before pinning, because a stack version is images plus the compose that runs them. It stops rather than pull over uncommitted changes to tracked files. On the pinned path that is your job: `git pull` before `make stack`, or you get new images under an old compose file.

The two are mutually exclusive: a track-latest box lets the daemon own the
version, so leave `version.pin` out of the dial there. `make modes` calls it
`AMBIGUOUS` if it finds both set.

## Updating a daemon VM without sudo

A box on the track-latest daemon has two users. The service user (`dfe`) runs the timer and owns the checkout. The operator logs in as someone else. Left alone, every `git` and `make` step needs `sudo -u dfe`, which an unattended agent cannot use (git runs the repo's config as the target user).

`install.sh` sets the box up so the operator never needs it:

- **Start an update on demand.** A polkit rule lets members of the service user's group start, stop and restart `dfe-docker-update.service` and `dfe-docker-proof.service`, where the box has one (`install.sh` installs the update units and the rule, and tunes a proof unit it finds). `systemctl start dfe-docker-update.service` runs the timer's tick as `dfe` and waits for it. Add `--no-block` to return at once and follow with `journalctl -u dfe-docker-update -f`. The tick takes the newest tag in the signed stack-manifest repository, not a git branch, so a cycle moves the version by publishing one and starting the unit.
- **Work in the checkout directly.** The checkout bar `certs/` is group-writable with the setgid bit and `UMask=0002` makes the service's own files match. `.env` is group read/write and closed to others. The operator can `git pull` and start the proof with `systemctl start dfe-docker-proof.service` as themselves and read it with `journalctl -u dfe-docker-proof` from the `adm` group. Without console TLS they can run `make stack VERSION=X.Y.Z`, `make ci` and `make post` directly. With it, `make` stops, because the proxies run as the user who runs make and `DFE_PROXY_CERT_DIR` is readable by the service user only, so those targets run through the units or as the service user. Use this path, without console TLS, for a version the manifest does not list as newest.

The tick runs `make stack`, which checks the manifest's signature with cosign or cosign's pinned image through Docker. `DFE_STACK_VERIFY=false` in the checkout's `.env` turns the check off for a box that can run neither and the timer's runs read it there.

Secret files go no wider than group read/write. `.env`, `.env.*` bar `.env.example`, `access-summary.md` and everything in `env/` are closed to others. A rewrite by either user keeps the group-write bit it finds.

**Converting an existing install.** Once, as a user with sudo, from the box's own checkout:

```bash
sudo -u dfe git -C /opt/dfe-docker pull --ff-only
sudo DFE_DOCKER_DIR=/opt/dfe-docker DFE_UPDATE_USER=dfe DFE_OPERATOR_USER=ubuntu \
  DFE_UPDATE_ENABLE_TIMER=0 /opt/dfe-docker/ops/daemon-update/install.sh
```

`DFE_UPDATE_ENABLE_TIMER=0` leaves the timer as it is, so an operator-triggered box stays that way. The operator logs in again to pick up the group. Check it from that login, with no sudo:

```bash
id -nG                                   # includes dfe
git -C /opt/dfe-docker pull --ff-only
systemctl start --no-block dfe-docker-update.service && systemctl status dfe-docker-update.service
```

Starting the unit is the check for the polkit rule. A refusal prints `Interactive authentication required`. `pkcheck --detail` answers only to root, so it cannot test the rule from the operator's login.

## What an upgrade keeps

`make stack VERSION=X.Y.Z && make ci` pulls the new pins before it stops anything, so a failed pull leaves the running stack up. It then takes the stack down and starts it on the new images, and every named volume survives ([secrets-and-storage.md](secrets-and-storage.md)).

ClickHouse keeps the default-user password its `clickhouse-data` volume was created with. A `CLICKHOUSE_PASSWORD` that differs from it fails every query with `Authentication failed`, so set the user to the new value once (`ALTER USER default IDENTIFIED BY '<value>'`) or recreate the volume if it holds nothing you need. `make init` never writes a blank one back, and `make post` fails a stack on a blank one.

Every port but ingest binds `127.0.0.1` by default. A box reached from elsewhere sets `DFE_BIND_SCOPE=all` (or `docker.bind_scope: all` in the dial) for the UIs, and `DFE_BIND_HOST=0.0.0.0` for the operator ports, having read [operating.md](operating.md).

## Related

- [operating.md](operating.md) -- exposure, limits, secrets, persistence
- [observability.md](observability.md) -- health, self-telemetry, the self test
- [troubleshooting.md](troubleshooting.md) -- diagnosing a stack that misbehaves
