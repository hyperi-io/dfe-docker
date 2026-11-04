# dfe-docker self-update daemon

A systemd timer that keeps a single-VM deployment on the newest published stack, so nobody has to log in to move it. It is opt-in: a box stays where `make stack VERSION=X.Y.Z && make ci` put it unless this is installed. It moves between whole signed stacks, where watchtower would bump container tags one at a time.

## What a tick does

A tick runs 10 minutes after boot, then every hour:

1. Takes `.dfe-stack.lock`, the lock `make proof` holds. If it is held the tick logs `skipping this tick` and exits 0, because `make ci` would take down the stack the proof is testing.
2. Lists the stack-manifest tags with `oras repo tags` and picks the highest stable version.
3. Compares it to `.dfe-stack-applied`. The same version only fast-forwards the checkout and restarts nothing. A lower version is refused, because a box installed from a cut branch can sit ahead of the newest published stack.
4. A higher version runs `git pull --ff-only`, `make init`, `make stack VERSION=<new>` and `make ci`, then records the version.

A failure exits non-zero, so systemd marks the unit failed and the next tick retries. `make ci` pulls the images before it stops the stack, so a failed pull leaves the running stack up. Output reaches the journal as it happens: `journalctl -u dfe-docker-update -f`.

## The checkout moves with the pins

A stack is images plus the compose that runs them, so the tick fast-forwards the checkout before pinning. It pulls the branch the checkout is on, which is the newest compose on that branch and not one tied to the stack version.

A tree with uncommitted changes to tracked files is refused rather than pulled over. `.env`, `env/`, `deployment.yaml` and the state file are gitignored, so a deployed box should be clean. The pull needs a credential for the remote and fails the tick without one.

`make init` adds any generated secret the new version needs and never overwrites a value, so an existing `.env` and `env/` carry over as they are.

## Settings

Set these in a drop-in on `dfe-docker-update.service`:

| Variable                        | Effect                                                            |
|---------------------------------|-------------------------------------------------------------------|
| `DFE_UPDATE_ALLOW_PRERELEASE=1` | Also takes `-rc` stacks                                           |
| `DFE_UPDATE_WIPE_STATE=1`       | Runs `make clean` before `make ci`, once per new stack. See below |

## Disposable boxes: fresh state on every version

`DFE_UPDATE_WIPE_STATE=1` brings each new version up on empty volumes, so its first-start paths run for real: schema creation, the break-glass seed and the engine's built-in defaults.

It deletes ClickHouse, Kafka, the DLQ spool, HyperDX and the engine's config volume. The image ships an empty `/app/config`, so sources, orgs and locally created accounts are gone for good. Only the two logins return, both from `env/engine.env`. The engine issues `admin` again from `DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD`, with a forced change at first login. It hashes `breakglass` from `DFE_ENGINE_AUTH_BREAKGLASS_PASSWORD` into the new deploy repo, which needs `DFE_ENGINE_GITOPS_ENABLED=true` there too. `make orgs` and `make accounts` create the seed orgs and accounts again. Pin both passwords to known values on a box running this.

With `DFE_DATA_ROOT` set, `compose/storage.yml` turns every volume into a bind onto that path, and `make clean` removes the volume objects while the directories keep their contents. The wipe then does nothing, so leave `DFE_DATA_ROOT` unset on a box that relies on this flag.

## Install

Run this as root on the VM. It needs docker, `oras`, python3, a working `.env` (`make init` once) and the service user in the `docker` group. Images and the stack manifest pull anonymously, so the VM needs no registry login.

```bash
sudo DFE_DOCKER_DIR=/opt/dfe-docker DFE_UPDATE_USER=dfe ./install.sh
systemctl status dfe-docker-update.timer
sudo -u dfe python3 /opt/dfe-docker/ops/daemon-update/self_update.py --dry-run
```

The first real run adopts the newest stable stack and records it. Later ticks do nothing but fast-forward until a newer one ships.

## Operating without sudo

After `install.sh`, a login in the service user's group runs everything itself:

```bash
systemctl start dfe-docker-update.service      # one tick, as the service user, waits for it
systemctl start --no-block dfe-docker-update.service && journalctl -u dfe-docker-update -f
git -C /opt/dfe-docker pull --ff-only          # and make stack, make ci, make post
```

A tick follows the newest tag in the stack-manifest repository. Any other version goes through `make stack VERSION=X.Y.Z && make ci` as the operator. The conversion of an existing install is in [docs/deploying.md](../../docs/deploying.md#updating-a-daemon-vm-without-sudo).

## Files

- `self_update.py`: one tick. `--dry-run` reports without acting and takes no lock.
- `dfe-docker-update.service`: the oneshot that runs a tick.
- `dfe-docker-update.timer`: the schedule.
- `dfe-docker-update.rules`: a polkit rule letting the service user's group start, stop and restart the update and proof units.
- `install.sh`: renders and installs the units and polkit rule, then enables the timer. It also makes the checkout bar `certs/` group-writable and setgid, closes secrets to others, sets `core.sharedRepository group` and `safe.directory` and adds the operator to the group. Its header lists its settings.
