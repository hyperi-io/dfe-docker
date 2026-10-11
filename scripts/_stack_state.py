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
with a hash of its resolved config, which says whether the container that comes
back is configured as the one that went.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

SERVICE_LABEL = "com.docker.compose.service"
PROJECT_LABEL = "com.docker.compose.project"
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
ENVIRONMENT_FILE_LABEL = "com.docker.compose.project.environment_file"
CONFIG_HASH_LABEL = "com.docker.compose.config-hash"
ONEOFF_LABEL = "com.docker.compose.oneoff"


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
    """One `docker compose up` that starts services from the files they came from."""

    project: str
    working_dir: str
    config_files: tuple[str, ...]
    environment_files: tuple[str, ...]
    services: tuple[str, ...]

    def command(self) -> list[str]:
        """The compose command, every profile enabled so each service resolves."""
        command = ["docker", "compose", "--project-name", self.project]
        command += ["--project-directory", self.working_dir]
        for path in self.environment_files:
            command += ["--env-file", path]
        for path in self.config_files:
            command += ["-f", path]
        return command + ["--profile", "*", "up", "-d", *self.services]


def restores(found: Iterable[Container]) -> list[Restore]:
    """One `up` per set of compose files the restored containers were started from."""
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
        Restore(project, working_dir, files, env_files, tuple(sorted(services)))
        for (project, working_dir, files, env_files), services in sorted(groups.items())
    ]


def differences(before: Iterable[Container], after: Iterable[Container]) -> list[str]:
    """How the project differs from how it was found, empty when it does not.

    Each restored service must have a container configured as before, still
    running if it was. Nothing may be running that was not.
    """
    before = list(before)
    now = {container.service: container for container in after}
    problems = []
    for was in before:
        if not (was.restored):
            continue
        current = now.get(was.service)
        if current is None:
            problems.append(f"'{was.service}' has no container")
        elif was.running and not (current.running):
            problems.append(f"'{was.service}' is not running")
        elif current.config_hash != was.config_hash:
            problems.append(
                f"'{was.service}' is configured differently (config hash "
                f"{was.config_hash[:12]} became {current.config_hash[:12]})"
            )
    was_running = {container.service for container in before if container.running}
    problems += [
        f"'{service}' is running and was not"
        for service, current in sorted(now.items())
        if current.running and service not in was_running
    ]
    return problems
