#  Project:      dfe-docker
#  File:         _sources.py
#  Purpose:      The sources an e2e test makes through dfe-engine and what each one's transform instance owes the run
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Primitives for the e2e tests whose transforms dfe-engine starts.

Internal support module - imported by the e2e suite, not executed directly. Stdlib only, so the unit tests import it without PyYAML.

A test's `sources:` block names each source, the transform engine it runs, the fixture files uploaded to it and the schema its table is built on where the default does not fit. The suite makes each one the way the console does, a create, a deploy and an upload per file, after which dfe-engine renders the source's instance config and lists the source in `env/<app>.instances`. The instance runs as `dfe-transform-<engine>-<source>`. On the bus its consumer group carries the same name. Talking to the engine, compose, ClickHouse and the broker stays with the suite.
"""

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import instances
import resolve_profile
from _common import (
    FALSY,
    PER_SOURCE_APP_KEYS,
    SERVICE_CONFIG_FILE,
    SOURCE_NAME,
    SOURCE_NAME_MAX_LENGTH,
    TRANSFORM_APP_PREFIX,
)
from _pipeline import escape_literal

# The transform engines a source may name, one per per-source transform app
ENGINES = tuple(
    sorted(
        app.removeprefix(TRANSFORM_APP_PREFIX)
        for app in PER_SOURCE_APP_KEYS
        if app.startswith(TRANSFORM_APP_PREFIX)
    )
)
# The dfe-engine setting each tier variable resolve_profile.py exports reaches (docker-compose.yml dfe-engine `environment`)
ENGINE_SETTINGS = {
    resolve_profile.APP_CONFIG_MOUNT_VAR: "DFE_DEPLOYMENT_APP_CONFIG_MOUNT",
    resolve_profile.ENGINE_APP_CONFIG_BASE_DIR_VAR: "DFE_DEPLOYMENT_APP_CONFIG_BASE_DIR",
    resolve_profile.ENGINE_APP_CONFIG_DIR_VAR: "DFE_DEPLOYMENT_APP_CONFIG_DIR",
    resolve_profile.ENGINE_BROKER_ENV_VAR: "DFE_KAFKA_BOOTSTRAP_SERVERS",
    resolve_profile.ENGINE_CONTRACT_DIR_VAR: "DFE_APP_CONTRACT_DIR",
    resolve_profile.STACK_PROFILE_VAR: "DFE_PROFILE",
    resolve_profile.TRANSPORT_BUS_PRESENT_VAR: "DFE_TRANSPORT_BUS_PRESENT",
    resolve_profile.TRANSPORT_DEFAULT_VAR: "DFE_TRANSPORT_DEFAULT",
}
# Where a source's upload fixtures live, relative to the repo root
FIXTURE_DIR = Path("tests") / "e2e"
# The service each engine restart hint names (dfe-engine appmgmt/appconfig.py APPLY_COMMAND)
HINT_SERVICE = re.compile(r"SERVICES=(\S+)")
LAND_SUFFIX = "_land"
LOAD_SUFFIX = "_load"
# The file set a transform's programs are uploaded to (dfe-engine appmgmt/apps.yaml `files`)
PROGRAM_FILE_SET = "transforms"
# The settings a source's `schema` may name, each a string (dfe-engine source/models.py SourceSchema)
SCHEMA_KEYS = frozenset(
    {
        "additional_fields",
        "derived_schema",
        "derived_schema_version",
        "meta_schema",
        "meta_schema_version",
    }
)
# The header and schema resources/seed/sources/syslog.yaml gives a receiver source
SOURCE_HEADER = {"type": "common-header/timeseries", "version": "1.0.1"}
SOURCE_KEYS = frozenset({"files", "name", "schema", "transform"})
# The field the receiver labels a record by, which each source matches on its own name
SOURCE_LABEL_FIELD = "_source"
SOURCE_SCHEMA = {"meta_schema": "meta/syslog", "meta_schema_version": "1.0.0"}
# The `_tags` key a program the suite uploads sets to its engine's name, beside the run's marker
STAMP_KEY = "e2e_transform"
# How one `_tags` key reads back out of a landed row, in the two shapes _pipeline.MARKER_EXPRESSIONS tries for the marker
TAG_EXPRESSIONS = (
    "toString(`_tags`.{key})",
    "JSONExtractString(toString(`_tags`), '{key}')",
)
TRANSFORM_KEYS = frozenset({"engine", "variant"})


@dataclass(frozen=True, slots=True)
class RestartPlan:
    """What the engine's restart hints ask of one test's stack, with what is wrong with them."""

    ignored: tuple[str, ...]
    problems: tuple[str, ...]
    recreated: tuple[str, ...]
    started: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Source:
    """One source a test makes through dfe-engine, the fixtures uploaded to its instance and the schema settings its table is built on."""

    engine: str
    files: tuple[tuple[str, str], ...]
    name: str
    schema: tuple[tuple[str, str], ...] = ()
    variant: str = ""

    @property
    def app(self) -> str:
        """The per-source app the source's transform runs in."""
        return f"{TRANSFORM_APP_PREFIX}{self.engine}"

    @property
    def instance(self) -> str:
        """The compose service of the source's instance, also its consumer group on the bus."""
        return instances.service_name(instance=self.name, service=self.app)


def _file_problems(
    *, files: object, label: str, project_dir: Path
) -> tuple[tuple[tuple[str, str], ...], list[str]]:
    """The (file set, fixture) uploads one source declares, with what is wrong with them."""
    if files is None:
        return (), []
    if not (isinstance(files, dict)):
        return (), [f"{label} 'files' must map each file set to a list of fixtures"]
    root = (project_dir / FIXTURE_DIR).resolve()
    uploads = []
    problems = []
    for file_set, paths in sorted(files.items()):
        if not (isinstance(paths, list)) or not (paths):
            problems.append(f"{label} file set {file_set!r} lists no fixture")
            continue
        for path in paths:
            resolved = (project_dir / str(path)).resolve()
            if not (resolved.is_relative_to(root)):
                problems.append(f"{label} fixture {path!r} is outside {FIXTURE_DIR}/")
            elif not (resolved.is_file()):
                problems.append(f"{label} fixture {path!r} does not exist")
            else:
                uploads.append((str(file_set), str(path)))
    return tuple(uploads), problems


def _name_problems(*, name: object, seen: set[str]) -> list[str]:
    """What is wrong with one source's name, empty when nothing is."""
    # dfe-engine's rule allows a '-', which the ClickHouse table named for the source would need quoted
    if (
        not (isinstance(name, str))
        or not (SOURCE_NAME.fullmatch(string=name))
        or ("-" in name)
        or (len(name) > SOURCE_NAME_MAX_LENGTH)
    ):
        return [
            f"source name {name!r} must be lowercase letters and digits, start with "
            f"a letter and run to {SOURCE_NAME_MAX_LENGTH} characters at most"
        ]
    if name in seen:
        return [f"source {name!r} is declared twice"]
    return []


def _schema_problems(
    *, label: str, schema: object
) -> tuple[tuple[tuple[str, str], ...], list[str]]:
    """The schema settings one source names for its table, with what is wrong with them."""
    if schema is None:
        return (), []
    if not (isinstance(schema, dict)):
        return (), [f"{label} 'schema' must map each schema setting to its value"]
    problems = []
    unknown = sorted(str(key) for key in set(schema) - SCHEMA_KEYS)
    if unknown:
        problems.append(f"{label} schema has unknown key(s) {', '.join(unknown)}")
    if "meta_schema" not in schema:
        problems.append(f"{label} schema names no 'meta_schema'")
    settings = []
    for key, value in schema.items():
        if not (isinstance(value, str)) or not (value):
            problems.append(f"{label} schema {key!r} must be a name")
        else:
            settings.append((str(key), value))
    return tuple(sorted(settings)), problems


def _transform_problems(*, label: str, transform: object) -> tuple[str, str, list[str]]:
    """The engine and variant one source's transform names, with what is wrong with it."""
    if not (isinstance(transform, dict)):
        return "", "", [f"{label} names no 'transform' with an 'engine'"]
    problems = []
    unknown = sorted(str(key) for key in set(transform) - TRANSFORM_KEYS)
    if unknown:
        problems.append(f"{label} transform has unknown key(s) {', '.join(unknown)}")
    engine = transform.get("engine")
    if engine not in ENGINES:
        problems.append(
            f"{label} transform engine {engine!r} is not one of {', '.join(ENGINES)}"
        )
    variant = (transform.get("variant")) or ("")
    if not (isinstance(variant, str)):
        problems.append(f"{label} transform 'variant' must be a name")
    return str((engine) or ("")), str(variant), problems


def binding_consumers(*, sources: Sequence[Source]) -> Counter[str]:
    """The consumer each source's instance adds to its landing topic on the bus."""
    return Counter(f"{source.name}{LAND_SUFFIX}" for source in sources)


def bindings(*, sources: Sequence[Source]) -> dict[str, str]:
    """Each source instance's consumer group and the landing topic dfe-engine binds it to on the bus.

    Every transform app's binding names the group after the instance (dfe-engine appmgmt/apps.yaml `source_binding`), dfe-transform-vector by deriving it from `dfe_source`.
    """
    return {source.instance: f"{source.name}{LAND_SUFFIX}" for source in sources}


def bindings_unassigned(
    *, assigned: Mapping[str, set[str]], bindings: Mapping[str, str]
) -> list[str]:
    """Name each bound group with no live member holding its landing topic."""
    return [
        f"{topic!r}: group {group!r} has not joined"
        for group, topic in sorted(bindings.items())
        if topic not in assigned.get(group, set())
    ]


def bindings_uncommitted(
    *, bindings: Mapping[str, str], lag: Mapping[str, Mapping[str, int]]
) -> list[str]:
    """Name each bound group that has committed nothing on its landing topic."""
    return [
        f"{topic!r}: group {group!r} has not committed"
        for group, topic in sorted(bindings.items())
        if topic not in lag.get(group, {})
    ]


def create_request(*, source: Source) -> dict[str, object]:
    """The `POST /sources` body that makes one source, receiver-matched on its own `_source` label."""
    transform = {"engine": source.engine}
    if source.variant:
        transform["variant"] = source.variant
    return {
        "display_name": f"E2E {source.name}",
        "header": dict(SOURCE_HEADER),
        "match": {
            "field": SOURCE_LABEL_FIELD,
            "operator": "equals",
            "value": source.name,
        },
        "schema": (dict(source.schema)) or (dict(SOURCE_SCHEMA)),
        "source": source.name,
        "transform": transform,
    }


def deploy_path(*, source: Source) -> str:
    """The engine route that deploys one source."""
    return f"{source_path(source=source)}/deploy"


def enabled_apps(*, profile_name: str, text: str) -> list[str]:
    """The apps one committed profile runs an instance per source of, read as resolve_profile.py reads them.

    `text` is service_profiles.yaml. A per-source key the resolver refuses raises resolve_profile.ProfileError.
    """
    parsed = resolve_profile.parse_yaml(text=text)
    profile = parsed[resolve_profile.PROFILE_LIST_YAML_FIELD][profile_name]
    core = str(profile.get("core", "true")).strip().lower()
    return resolve_profile.per_source_apps(
        profile=profile,
        profile_name=profile_name,
        renders=core not in FALSY,
    )


def engine_base_volumes(*, services: Mapping[str, str]) -> list[str]:
    """Mounts giving dfe-engine each resident service's committed config to render over, as the test's profile commits it.

    `services` maps each service to its project-relative config path.
    """
    base = resolve_profile.ENGINE_APP_CONFIG_BASE_DIR
    return [
        f"./{path}:{base}/{service}.yaml:ro"
        for service, path in sorted(services.items())
    ]


def engine_environment(*, profile_name: str, transport: str) -> dict[str, str]:
    """What dfe-engine is told about one test's tier, as resolve_profile.py exports it through make with the engine rendering app config and the in-stack broker.

    Keyed by the engine container's own variable, so a compose override sets it for one test only.
    """
    tier = resolve_profile.engine_tier(
        configured_broker="",
        content_dir=resolve_profile.ENGINE_CONTENT_DIR,
        profile_name=profile_name,
        renders=True,
        transport=transport,
    )
    return {ENGINE_SETTINGS[name]: value for name, value in tier.items()}


def engine_override(
    *, environment: Mapping[str, str], service: str, volumes: Sequence[str]
) -> str:
    """The compose override lines for dfe-engine's service: the settings one test hands it and the committed configs it renders over, empty when it has neither."""
    if not (environment) and not (volumes):
        return ""
    lines = [f"  {service}:"]
    if environment:
        lines.append("    environment:")
        lines += [
            f"      {key}: {json.dumps(obj=value)}"
            for key, value in sorted(environment.items())
        ]
    if volumes:
        lines.append("    volumes:")
        lines += [f"      - {volume}" for volume in volumes]
    return "\n".join(lines) + "\n"


def hinted_services(*, replies: Sequence[object]) -> list[str]:
    """The compose services the restart hints in the engine's replies name, each once, in the order first named."""
    named = []
    for reply in replies:
        hints = reply.get("restart_required") if isinstance(reply, dict) else None
        for hint in (hints) or ([]):
            found = HINT_SERVICE.search(string=str(hint))
            if found and found.group(1) not in named:
                named.append(found.group(1))
    return named


def indexed(*, sources: Sequence[Source]) -> dict[str, list[str]]:
    """The sources dfe-engine's instance index lists now, by app, the shape scripts/instances.py writes a fragment from."""
    found = {}
    for source in sources:
        listed = instances.read_index(path=instances.index_file(service=source.app))
        if source.name in listed:
            found.setdefault(source.app, []).append(source.name)
    return found


def instance_group(*, service: str, sources: Sequence[Source]) -> str | None:
    """The consumer group `service` commits as when it is a source's instance, None when it is not one."""
    return service if (service in bindings(sources=sources)) else None


def kafka_bootstrap(*, transport: str) -> str:
    """The broker a test's apps and dfe-engine are pointed at, the in-stack one on the bus and none on direct gRPC."""
    return resolve_profile.tier_broker(configured="", transport=transport)


def engine_transport(*, transport: str) -> dict[str, str]:
    """The settings that tell dfe-engine whether a bus exists, since it waits for dead-letter topics on any deployment that has one."""
    bus = transport == "kafka"
    return {
        resolve_profile.ENGINE_BROKER_ENV_VAR: kafka_bootstrap(transport=transport),
        resolve_profile.TRANSPORT_BUS_PRESENT_VAR: "true" if bus else "false",
        resolve_profile.TRANSPORT_DEFAULT_VAR: "bus" if bus else "direct",
    }


def merged_topics(
    *, expected_topics: Sequence[str], sources: Sequence[Source], transport: str
) -> list[str]:
    """The topics a test expects: its own, then on the bus each source's landing and transformed topic it does not already name."""
    merged = list(expected_topics)
    if transport != "kafka":
        return merged
    for topic in topics(sources=sources):
        if topic not in merged:
            merged.append(topic)
    return merged


def outage_problems(*, service: str, sources: Sequence[Source]) -> list[str]:
    """Say what is wrong with an outage that stops a per-source instance, empty when nothing is."""
    made = {source.instance for source in sources}
    declared = (", ".join(sorted(made))) or ("none")
    for app in PER_SOURCE_APP_KEYS:
        if service.startswith(f"{app}-") and service not in made:
            return [
                f"outage stops {service!r}, which is no instance of a source this "
                f"test makes (it makes {declared})"
            ]
    return []


def parse(*, project_dir: Path, raw: object) -> tuple[list[Source], list[str]]:
    """Read one test's `sources:` block, with what is wrong with it."""
    if raw is None:
        return [], []
    if not (isinstance(raw, list)):
        return [], ["'sources' must be a list of sources"]
    sources = []
    problems = []
    seen = set()
    for index, entry in enumerate(raw):
        if not (isinstance(entry, dict)):
            problems.append(f"sources[{index}] must be a mapping")
            continue
        name = entry.get("name")
        label = f"source {name!r}"
        found = _name_problems(name=name, seen=seen)
        if isinstance(name, str):
            seen.add(name)
        unknown = sorted(str(key) for key in set(entry) - SOURCE_KEYS)
        if unknown:
            found.append(f"{label} has unknown key(s) {', '.join(unknown)}")
        engine, variant, transform_problems = _transform_problems(
            label=label, transform=entry.get("transform")
        )
        files, file_problems = _file_problems(
            files=entry.get("files"), label=label, project_dir=project_dir
        )
        schema, schema_problems = _schema_problems(
            label=label, schema=entry.get("schema")
        )
        found += transform_problems + file_problems + schema_problems
        problems += found
        if not (found):
            sources.append(
                Source(
                    engine=engine,
                    files=files,
                    name=str(name),
                    schema=schema,
                    variant=variant,
                )
            )
    return sources, problems


def rendered_config(*, instance: str = "", service: str) -> str:
    """Where one service reads the config dfe-engine renders for it, an instance's under a directory of its own."""
    parts = [
        instances.APP_CONFIG_MOUNT,
        service,
        instance,
        SERVICE_CONFIG_FILE[service],
    ]
    return "/".join(part for part in parts if part)


def restart_plan(
    *,
    found: Mapping[str, Sequence[str]],
    hinted: Sequence[str],
    rendered_services: Sequence[str],
    transport: str,
) -> RestartPlan:
    """Sort the services the engine's restart hints name into the instances it declared, the rendered services to recreate and the rest, which a test leaves alone.

    On direct the receiver reads where to hand each record only as it starts, so a rendered service the hints skip there would send every record around the transform.
    """
    started = tuple(instances.services(instances=found))
    recreated = tuple(service for service in hinted if service in rendered_services)
    ignored = tuple(
        service
        for service in hinted
        if service not in started and service not in recreated
    )
    missed = [service for service in rendered_services if service not in hinted]
    problems = ()
    if transport == "grpc" and missed:
        problems = (
            f"dfe-engine named no restart of {', '.join(missed)} after making a "
            f"direct source, so its records would skip the transform",
        )
    return RestartPlan(
        ignored=ignored, problems=problems, recreated=recreated, started=started
    )


def source_path(*, source: Source) -> str:
    """The engine route of one source."""
    return f"/sources/{quote(safe='', string=source.name)}"


def stamp_counts(*, raw: str) -> tuple[int, int] | None:
    """The (rows, stamped rows) one stamp query answered, None when its answer is not two counts."""
    cells = raw.split()
    if (len(cells) != 2) or not (all(cell.isdigit() for cell in cells)):
        return None
    return int(cells[0]), int(cells[1])


def stamp_problems(
    *, counts: tuple[int, int] | None, source: Source, table: str
) -> list[str]:
    """Say what the stamp check finds wrong in `table`: no row of this run to judge or rows the source's instance never ran on."""
    if (counts is None) or (counts[0] == 0):
        return [
            f"no row of this run could be read in {table!r}, so whether "
            f"{source.instance!r} ran on its records is unknown"
        ]
    rows, stamped = counts
    if stamped < rows:
        return [
            f"{rows - stamped}/{rows} rows of this run in {table!r} lack the "
            f"`_tags.{STAMP_KEY}` {source.instance!r} sets, so they reached the "
            f"table around it"
        ]
    return []


def stamp_queries(
    *, database: str, marker: str, source: Source, table: str
) -> list[str]:
    """One query per way `_tags` reads back, each counting this run's rows in `table` and those carrying the stamp the source's program sets."""
    head = escape_literal(value=marker)
    value = escape_literal(value=source.engine)
    queries = []
    for template in TAG_EXPRESSIONS:
        marked = template.format(key="marker")
        stamped = template.format(key=STAMP_KEY)
        queries.append(
            f"SELECT count(), countIf({stamped} = '{value}') FROM {database}.{table} "
            f"WHERE startsWith({marked}, '{head}') FORMAT TabSeparated"
        )
    return queries


def stamping_source(*, sources: Sequence[Source], table: str) -> Source | None:
    """The source `table` is named for when it uploads a program, which stamps each record it runs on, None otherwise."""
    for source in sources:
        file_sets = {file_set for file_set, _ in source.files}
        if source.name == table and PROGRAM_FILE_SET in file_sets:
            return source
    return None


def target_problems(
    *, outage_sources: Iterable[str], sources: Sequence[Source], table: str
) -> list[str]:
    """Say which `_source` label this test sends records under names no source it makes, empty when each one does.

    dfe-engine sends a record that matches no source to the deployment's default destination (services/source_routing.py), which crosses no source's transform.
    """
    made = [source.name for source in sources]
    declared = ", ".join(made)
    problems = []
    if table not in made:
        problems.append(
            f"'table' {table!r} names no source this test makes ({declared})"
        )
    for label in sorted(outage_sources):
        if label not in made:
            problems.append(
                f"outage source {label!r} names no source this test makes ({declared})"
            )
    return problems


def topics(*, sources: Sequence[Source]) -> list[str]:
    """Each source's landing and transformed topic on the bus, in the order declared."""
    names = []
    for source in sources:
        names += [f"{source.name}{LAND_SUFFIX}", f"{source.name}{LOAD_SUFFIX}"]
    return names


def unindexed(
    *, found: Mapping[str, Sequence[str]], sources: Sequence[Source]
) -> list[str]:
    """The instance of each source dfe-engine's index does not list yet."""
    return [
        source.instance
        for source in sources
        if source.name not in found.get(source.app, ())
    ]


def upload_requests(
    *, project_dir: Path, source: Source
) -> list[tuple[str, dict[str, str]]]:
    """The `PUT` route and body for each fixture one source uploads, the file named for the fixture."""
    requests = []
    for file_set, path in source.files:
        route = (
            f"/apps/{source.app}/{quote(safe='', string=source.name)}/files/"
            f"{quote(safe='', string=file_set)}/{quote(safe='', string=Path(path).name)}"
        )
        content = (project_dir / path).read_text(encoding="utf-8")
        requests.append((route, {"content": content}))
    return requests
