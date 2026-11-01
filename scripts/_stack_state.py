#  Project:      dfe-docker
#  File:         _stack_state.py
#  Purpose:      The containers a compose project holds, and the commands that start them again
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What a compose project was running before the e2e suite, read off `docker inspect`.

Internal support module - imported by the e2e suite, not executed directly. The
suite stops every service to run one stack per test, so it reads the project
first and starts the same services from the same compose files at the end. Compose
labels each container with the files, directory and project it came from, and
with a hash of its resolved config.

That label is the config at the moment the container was created, and the files
can move on without it: `make post` writes the admin password it rotates into
.env after `make ci` has started the engine, and an edited .env or a pulled
compose file waits for the next `up`. So the suite also asks compose, before it
stops anything, for the hash the same files and environment give each service,
and judges what comes back against that.
"""

import dataclasses
import ipaddress
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import proxy_tls

SERVICE_LABEL = "com.docker.compose.service"
PROJECT_LABEL = "com.docker.compose.project"
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
ENVIRONMENT_FILE_LABEL = "com.docker.compose.project.environment_file"
CONFIG_HASH_LABEL = "com.docker.compose.config-hash"
ONEOFF_LABEL = "com.docker.compose.oneoff"

# What the suite adds to the found project's name and to its container prefix.
SUITE_PROJECT_SUFFIX = "-e2e"
SUITE_CONTAINER_PREFIX = "e2e-"

# The tier and transport dfe-engine runs with, the variable make exports each from and compose's default
_ENGINE_SETTINGS = (
    ("DFE_PROFILE", "DFE_STACK_PROFILE", "docker-slim"),
    ("DFE_TRANSPORT_DEFAULT", "DFE_TRANSPORT_DEFAULT", "direct"),
)


@dataclass(frozen=True, slots=True)
class Container:
    """One service container, as compose labelled it and docker reports it.

    Attributes:
        service: The compose service it runs.
        running: Whether its process is running.
        exit_code: The code its process last exited with.
        restart: Its restart policy, empty when none was set.
        config_hash: The hash compose took of the service's resolved config.
        project: The compose project it belongs to.
        working_dir: The project directory compose resolved relative paths against.
        config_files: The compose files it was started from, in order.
        environment_files: The `--env-file` arguments it was started with.
    """

    service: str
    running: bool
    exit_code: int
    restart: str
    config_hash: str
    project: str
    working_dir: str
    config_files: tuple[str, ...]
    environment_files: tuple[str, ...]

    @property
    def run_to_completion(self) -> bool:
        """A one-shot that finished cleanly, which `up` runs to completion again."""
        return not (self.running) and self.exit_code == 0 and self.restart in ("", "no")

    @property
    def restored(self) -> bool:
        """Started again at the end: running, or a one-shot that finished cleanly."""
        return self.running or self.run_to_completion


def _split(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.split(",") if part)


def containers(documents: Iterable[Mapping]) -> list[Container]:
    """Every service container in `docker inspect` output, `docker compose run` ones left out."""
    found = []
    for document in documents:
        labels = (document.get("Config") or {}).get("Labels") or {}
        if not (labels.get(SERVICE_LABEL)) or labels.get(ONEOFF_LABEL) == "True":
            continue
        state = document.get("State") or {}
        policy = (document.get("HostConfig") or {}).get("RestartPolicy") or {}
        found.append(
            Container(
                service=labels[SERVICE_LABEL],
                running=bool(state.get("Running")),
                exit_code=int(state.get("ExitCode") or 0),
                restart=policy.get("Name") or "",
                config_hash=labels.get(CONFIG_HASH_LABEL, ""),
                project=labels.get(PROJECT_LABEL, ""),
                working_dir=labels.get(WORKING_DIR_LABEL, ""),
                config_files=_split(labels.get(CONFIG_FILES_LABEL, "")),
                environment_files=_split(labels.get(ENVIRONMENT_FILE_LABEL, "")),
            )
        )
    return sorted(found, key=lambda container: container.service)


@dataclass(frozen=True, slots=True)
class Restore:
    """One `docker compose up` that starts services from the files they came from.

    Attributes:
        missing: Compose files the services were started from that no longer exist,
            left out of the command, and the config hash check judges what that
            changed.
        undefined: Services the remaining files no longer define, left out of the
            command because compose refuses a whole `up` that names one.
    """

    project: str
    working_dir: str
    config_files: tuple[str, ...]
    environment_files: tuple[str, ...]
    services: tuple[str, ...]
    missing: tuple[str, ...] = ()
    undefined: tuple[str, ...] = ()

    def compose(self) -> list[str]:
        """`docker compose` on the project, directory and files the services came from."""
        command = ["docker", "compose", "--project-name", self.project]
        command += ["--project-directory", self.working_dir]
        for path in self.environment_files:
            command += ["--env-file", path]
        for path in self.config_files:
            command += ["-f", path]
        return command

    def config(self) -> list[str]:
        """The command that prints every service those files define, as JSON."""
        return self.compose() + ["--profile", "*", "config", "--format", "json"]

    def hashes(self, profiles: Iterable[str] = ()) -> list[str]:
        """The command that prints the config hash of each service those files enable.

        It takes the profiles the `up` takes, so each hash is the one that `up`
        labels the service's container with.
        """
        return self.compose() + _profile_flags(profiles) + ["config", "--hash", "*"]

    def command(self, profiles: Iterable[str] = ()) -> list[str]:
        """The `up`, with only the given profiles enabled.

        Every profile at once is wrong: both brokers are optional dependencies
        of dfe-engine, and the profile is what picks which one starts.
        """
        return self.compose() + _profile_flags(profiles) + ["up", "-d", *self.services]


def _profile_flags(profiles: Iterable[str]) -> list[str]:
    return [
        part for profile in sorted(set(profiles)) for part in ("--profile", profile)
    ]


def _profile_name(*, settings: Mapping[str, str]) -> str:
    """The service profile a dfe-engine tier and transport belong to, such as single-grpc."""
    tier = settings["DFE_PROFILE"].removeprefix("docker-")
    transport = "kafka" if (settings["DFE_TRANSPORT_DEFAULT"] == "bus") else "grpc"
    return f"{tier}-{transport}"


def still_defined(restore: Restore, model: Mapping) -> Restore:
    """The restore with only the services `docker compose config` JSON still defines.

    A per-source instance outlives its source when nothing prunes it, and the
    fragment that declared it is removed with the source. It shares its `up`
    with every service started from the same files, so naming it would keep
    those down as well.
    """
    known = model.get("services") or {}
    return dataclasses.replace(
        restore,
        services=tuple(service for service in restore.services if service in known),
        undefined=tuple(
            service for service in restore.services if service not in known
        ),
    )


def profiles_of(model: Mapping, services: Iterable[str]) -> set[str]:
    """The profiles the named services belong to, from `docker compose config` JSON."""
    defined = model.get("services") or {}
    return {
        profile
        for service in services
        for profile in (defined.get(service) or {}).get("profiles") or ()
    }


def config_hashes(output: str, services: Iterable[str]) -> dict[str, str]:
    """The named services' hashes in `docker compose config --hash` output.

    Each line is `<service> <hash>`. A named service the output leaves out, which
    the files no longer define, gets no entry.
    """
    wanted = set(services)
    hashes = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in wanted:
            hashes[parts[0]] = parts[1]
    return hashes


def stale(found: Iterable[Container], expected: Mapping[str, str]) -> list[str]:
    """The restored services whose container runs a config other than its files give.

    Any `up` recreates such a container, so it comes back on the files' config
    whatever the run does.
    """
    return [
        f"'{container.service}' runs config hash {container.config_hash[:12]}, "
        f"its files give {expected[container.service][:12]}"
        for container in found
        if container.restored
        and container.service in expected
        and expected[container.service] != container.config_hash
    ]


def profile_change(
    *, engine_environment: Mapping[str, str], environ: Mapping[str, str]
) -> str:
    """Why starting the stack again from `environ` would move it to another profile, empty when it would not or no dfe-engine runs."""
    if not (engine_environment):
        return ""
    running = {key: engine_environment.get(key, "") for key, _, _ in _ENGINE_SETTINGS}
    wanted = {
        key: (environ.get(exported)) or (default)
        for key, exported, default in _ENGINE_SETTINGS
    }
    if running == wanted:
        return ""
    found = _profile_name(settings=running)
    return f"the stack runs profile {found!r} but these make flags give {_profile_name(settings=wanted)!r}. Rerun with DFE_PROFILE={found}"


def suite_environ(
    environ: Mapping[str, str], *, project: str, taken: Iterable[str] = ()
) -> dict[str, str]:
    """The environment the suite runs its test stacks under, apart from `project`.

    A test deletes and creates topics on its broker, so the suite's stacks get a
    compose project of their own, which gives them their own volumes and network,
    and a container prefix of their own, since container names are daemon-wide.
    Under console TLS its network takes a subnet clear of the found one and of
    every subnet in `taken`.
    """
    suite = {
        **environ,
        "COMPOSE_PROJECT_NAME": f"{project}{SUITE_PROJECT_SUFFIX}",
        "DFE_CONTAINER_PREFIX": f"{SUITE_CONTAINER_PREFIX}{environ.get('DFE_CONTAINER_PREFIX', '')}",
    }
    found_subnet = environ.get("DFE_NETWORK_SUBNET", "").strip()
    if found_subnet:
        subnet = _free_subnet(found_subnet, taken=taken)
        suite["DFE_PROXY_IP"], suite["DFE_NETWORK_IP_RANGE"] = proxy_tls.derive(
            subnet=subnet
        )
        suite["DFE_NETWORK_SUBNET"] = subnet
    return suite


def _free_subnet(found: str, *, taken: Iterable[str]) -> str:
    """The same-size private subnet nearest the found one that overlaps neither it nor any in `taken`."""
    network = ipaddress.IPv4Network(found)
    used = [network, *(ipaddress.IPv4Network(net) for net in taken)]
    block = next((b for b in proxy_tls.PRIVATE_BLOCKS if network.subnet_of(b)), None)
    candidates = list(block.subnets(new_prefix=network.prefixlen)) if block else []
    for candidate in sorted(
        candidates,
        key=lambda net: abs(int(net.network_address) - int(network.network_address)),
    ):
        if any(candidate.overlaps(net) for net in used):
            continue
        try:
            proxy_tls.derive(subnet=str(candidate))
        except ValueError:
            continue
        return str(candidate)
    raise ValueError(
        f"no free private subnet the size of {found} for the suite's network"
    )


def network_subnets(inspected: str) -> list[str]:
    """The IPv4 subnets in `docker network inspect` output, none when it does not parse."""
    try:
        networks = json.loads(inspected)
    except ValueError:
        return []
    subnets = []
    for network in networks if isinstance(networks, list) else []:
        for pool in ((network or {}).get("IPAM") or {}).get("Config") or []:
            subnet = (pool or {}).get("Subnet", "")
            try:
                if isinstance(ipaddress.ip_network(subnet), ipaddress.IPv4Network):
                    subnets.append(subnet)
            except ValueError:
                continue
    return subnets


def restores(
    found: Iterable[Container], *, exists: Callable[[str], bool] = lambda path: True
) -> list[Restore]:
    """One `up` per set of compose files the restored containers were started from.

    A file `exists` rejects is left out, as an e2e run's own override is once the
    run has cleaned up after itself.
    """
    groups: dict[tuple, list[str]] = defaultdict(list)
    for container in found:
        if container.restored:
            key = (
                container.project,
                container.working_dir,
                container.config_files,
                container.environment_files,
            )
            groups[key].append(container.service)
    return [
        Restore(
            project,
            working_dir,
            tuple(path for path in files if exists(path)),
            env_files,
            tuple(sorted(services)),
            missing=tuple(path for path in files if not (exists(path))),
        )
        for (project, working_dir, files, env_files), services in sorted(groups.items())
    ]


def differences(
    before: Iterable[Container],
    after: Iterable[Container],
    expected: Mapping[str, str] | None = None,
    left_out: Iterable[str] = (),
) -> list[str]:
    """How the project differs from how it was found, empty when it does not.

    Each restored service must have a container on the config its files gave
    when the run found it, still running if it was. Nothing may be running that
    was not.

    Args:
        before: The containers the run found.
        after: The containers the project holds now.
        expected: The config hash each service's files gave when the run found
            it. A service it leaves out is held to its found container's hash.
        left_out: Services the restore did not start because their files no
            longer define them, so none is held to its found container.

    Returns:
        One line per difference.
    """
    if expected is None:
        expected = {}
    before = list(before)
    skipped = set(left_out)
    now = {container.service: container for container in after}
    problems = []
    for was in before:
        if not (was.restored) or was.service in skipped:
            continue
        current = now.get(was.service)
        reference = expected.get(was.service, was.config_hash)
        if current is None:
            problems.append(f"'{was.service}' has no container")
        elif was.running and not (current.running):
            problems.append(f"'{was.service}' is not running")
        elif current.config_hash != reference:
            problems.append(
                f"'{was.service}' is configured differently (config hash "
                f"{reference[:12]} became {current.config_hash[:12]})"
            )
    was_running = {container.service for container in before if container.running}
    problems += [
        f"'{service}' is running and was not"
        for service, current in sorted(now.items())
        if current.running and service not in was_running
    ]
    return problems
