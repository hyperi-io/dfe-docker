# dfe-docker self-update daemon (STAGED - apply parked)

Keeps a single-VM dfe-docker deployment CURRENT using the stack's OWN updater -
not watchtower. A systemd timer periodically discovers the newest certified
stack version and, only when there is something new, runs
`make stack VERSION=<new>` (pin from the signed OCI stack-manifest) then
`make ci` (pull + up -d).

Why not watchtower: watchtower bumps individual container tags blindly. The DFE
stack is versioned as a CERTIFIED SET (the signed `dfe-stack-manifest`), and
`make stack` hard-fails rather than ever pull `latest`. This daemon respects
that - it only ever moves the VM between whole certified pin sets.

## The checkout moves with the pins

A stack version is images PLUS the compose that runs them, so the daemon
fast-forwards the checkout (`git pull --ff-only`) before pinning. Pinning new
images against an old `docker-compose.yml` is a half-update that reports success:
the 2.2.0-rc.2 engine needs a `DFE_ENV` declaration and a healthcheck path that
older compose files do not have, so the schema authority restart-loops while the
timer records a clean run.

Two guards. A tree with uncommitted changes to TRACKED files is refused rather
than pulled over - a deployed box should be clean, since `.env`, `env/`,
`deployment.yaml` and the state file are all gitignored. And the pull needs a
credential for the remote; without one it fails loudly rather than carrying on
with a stale compose.

`DFE_UPDATE_SKIP_GIT_PULL=1` turns it off for a box whose checkout is managed
another way (an image, a config-management run, an air-gapped copy). The skip is
logged, because it reintroduces exactly the drift above.

`--dry-run` reports what the refresh would do, so you can see a box is dirty
before the timer does.

## Files

- `self_update.py` - discovers the newest stable stack tag (`oras repo tags`),
  compares to the last-applied version (state file `.dfe-stack-applied` in the
  checkout), and runs the updater only on a change. `--dry-run` reports without
  acting. `DFE_UPDATE_ALLOW_PRERELEASE=1` includes `-rc` builds.
- `dfe-docker-update.service` - oneshot that runs the updater.
- `dfe-docker-update.timer` - runs it 10 min after boot, then every 6h (jittered).
- `install.sh` - renders + installs the units and enables the timer.

## Install (parked - run on the devex dfe-docker VM as root)

Prerequisites on the VM: docker, `oras`, python3; the service user in the
`docker` group; a working `.env` (`make init` once); GHCR pull access for the
private images (the compose stack already needs this).

```
sudo DFE_DOCKER_DIR=/opt/dfe-docker DFE_UPDATE_USER=dfe ./install.sh
# verify
systemctl status dfe-docker-update.timer
sudo -u dfe DFE_DOCKER_DIR=/opt/dfe-docker python3 \
  /opt/dfe-docker/ops/daemon-update/self_update.py --dry-run   # shows latest vs applied
journalctl -u dfe-docker-update.service -f                     # watch a real run
```

First real run adopts the current newest stable stack and records it; subsequent
ticks are no-ops until a newer certified stack ships.

## Why staged

Installing systemd units and restarting the live stack on the devex VM is a
VM-side mutation - parked for a human to run on the box. Everything needed is
here; `install.sh` is the one command.
