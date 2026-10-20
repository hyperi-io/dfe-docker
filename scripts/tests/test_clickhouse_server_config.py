#  Project:      dfe-docker
#  File:         tests/test_clickhouse_server_config.py
#  Purpose:      Assert the bundled ClickHouse bounds its own logs and holds Replicated DDL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The bundled ClickHouse's own telemetry has a ceiling.

On the image's config.xml alone the server logs at trace, rotates at 1000M and
keeps 10 archives, and none of the system log tables below has a TTL, so an idle
stack fills its disk with its own telemetry. Two config.d overlays bound both.
The server merges each over config.xml, so a key an overlay leaves out keeps the
image's value -- which is why these tests assert the exact key set, not a subset.

dfe-engine refuses a self-hosted server without Keeper, the {cluster}, {shard}
and {replica} macros and a remote_servers cluster the {cluster} macro names, and
it creates its SQL users and roles in the first writable access storage. A third
overlay makes this server a cluster of one with all of those, the access storage
in Keeper included. A fourth takes the place of the image's own listener file, so
Keeper, raft and interserver answer on loopback only.

A text read of the compose file, as in test_hyperdx.py: these tests run with no
PyYAML.
"""

import re
from pathlib import Path
from xml.etree import ElementTree

import init
from _common import COMPOSE_FILE, REPO_ROOT

_SERVICE = "clickhouse"
_CONFIG_D = "/etc/clickhouse-server/config.d"
_DATA_DIR = "/var/lib/clickhouse/"
_LOGGER_FILE = REPO_ROOT / "config" / "clickhouse" / "server-logger.xml"
_SYSTEM_LOGS_FILE = REPO_ROOT / "config" / "clickhouse" / "system-logs.xml"
_REPLICATION_FILE = REPO_ROOT / "config" / "clickhouse" / "replication.xml"
_LISTEN_FILE = REPO_ROOT / "config" / "clickhouse" / "listen.xml"
# The image's file that opens every listener on every interface.
_IMAGE_LISTEN_NAME = "docker_related_config.xml"
_LOOPBACK = {"127.0.0.1", "::1"}
# What other containers dial: http and native.
_CLIENT_PORTS = {"8123", "9000"}
_KEEPER_PASSWORD = "CLICKHOUSE_KEEPER_PASSWORD"
# The four-letter words that only read. The rest reset stats, move leadership or force recovery.
_READ_ONLY_WORDS = {
    "conf",
    "cons",
    "dirs",
    "envi",
    "isro",
    "mntr",
    "ruok",
    "srvr",
    "stat",
    "wchs",
}

_LOGGER = {"level": "information", "size": "50M", "count": "3"}
_SYSTEM_LOG_TTL = {
    "asynchronous_metric_log": "event_date + INTERVAL 7 DAY DELETE",
    "metric_log": "event_date + INTERVAL 7 DAY DELETE",
    "part_log": "event_date + INTERVAL 7 DAY DELETE",
    "query_log": "event_date + INTERVAL 30 DAY DELETE",
    "query_views_log": "event_date + INTERVAL 7 DAY DELETE",
    # HyperDX's clickhouse_system source reads it, so it stays on.
    "text_log": "event_date + INTERVAL 7 DAY DELETE",
}
_SWITCHED_OFF = ("trace_log",)


def _root(*, path: Path) -> ElementTree.Element:
    """Return the `<clickhouse>` element of one overlay."""
    root = ElementTree.parse(path).getroot()
    assert root.tag == "clickhouse", f"{path.name} is not a ClickHouse config"
    return root


def _service_lines(*, service: str, section: str) -> list[str]:
    """Return the lines of one service's `section:` block as written in the compose file."""
    found: list[str] = []
    in_service = False
    in_section = False
    for raw in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 2:
            in_service = line == f"{service}:"
            in_section = False
            continue
        if not in_service:
            continue
        if indent == 4:
            in_section = line == f"{section}:"
            continue
        if in_section and indent == 6:
            found.append(line)
    return found


def _service_scalar(*, service: str, key: str) -> str | None:
    """Return one service's top-level `key:` value as written in the compose file."""
    in_service = False
    for raw in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 2:
            in_service = line == f"{service}:"
        elif in_service and indent == 4 and line.startswith(f"{key}:"):
            return line.removeprefix(f"{key}:").strip()
    return None


def _service_volumes(*, service: str) -> list[str]:
    """Return one service's `volumes:` entries as written in the compose file."""
    return [
        line.removeprefix("- ")
        for line in _service_lines(service=service, section="volumes")
        if line.startswith("- ")
    ]


def _service_environment(*, service: str) -> dict[str, str]:
    """Return one service's `environment:` mapping as written in the compose file."""
    pairs = (
        line.partition(":")
        for line in _service_lines(service=service, section="environment")
    )
    return {key.strip(): value.strip() for key, _, value in pairs}


def _config_d_mounts() -> dict[str, str]:
    """Return {repo file: file name inside config.d} for the service's overlays."""
    mounts: dict[str, str] = {}
    for volume in _service_volumes(service=_SERVICE):
        source, _, rest = volume.partition(":")
        target, _, mode = rest.partition(":")
        directory, _, name = target.rpartition("/")
        if directory == _CONFIG_D:
            assert mode == "ro", f"{source} is mounted writable"
            mounts[source.removeprefix("./")] = name
    return mounts


def test_the_server_log_rotates_small_and_keeps_few_archives() -> None:
    logger = _root(path=_LOGGER_FILE).find("logger")

    assert logger is not None, f"{_LOGGER_FILE.name} sets no <logger>"
    # No <log> or <errorlog>, so the image's file paths stay where they are.
    assert {child.tag: (child.text or "").strip() for child in logger} == _LOGGER


def test_the_logger_overlay_sets_nothing_else() -> None:
    assert [child.tag for child in _root(path=_LOGGER_FILE)] == ["logger"]


def test_each_kept_system_log_table_carries_its_ttl() -> None:
    root = _root(path=_SYSTEM_LOGS_FILE)

    for table, ttl in sorted(_SYSTEM_LOG_TTL.items()):
        section = root.find(table)
        assert section is not None, f"{table} has no section"
        # Only the ttl, so the image's partitioning and flush settings stay.
        assert {child.tag: (child.text or "").strip() for child in section} == {
            "ttl": ttl
        }, table


def test_trace_log_is_removed_rather_than_kept() -> None:
    root = _root(path=_SYSTEM_LOGS_FILE)

    for table in _SWITCHED_OFF:
        section = root.find(table)
        assert section is not None, f"{table} is not removed"
        assert section.attrib == {"remove": "remove"}, table
        assert list(section) == [], f"{table} is removed and configured at once"


def test_the_system_logs_overlay_names_no_other_table() -> None:
    tables = [child.tag for child in _root(path=_SYSTEM_LOGS_FILE)]

    assert sorted(tables) == sorted([*_SYSTEM_LOG_TTL, *_SWITCHED_OFF])


def test_the_service_mounts_every_overlay_into_config_d() -> None:
    mounts = _config_d_mounts()

    for path in (_LOGGER_FILE, _SYSTEM_LOGS_FILE, _REPLICATION_FILE, _LISTEN_FILE):
        source = str(path.relative_to(REPO_ROOT))
        assert source in mounts, f"{_SERVICE} does not mount {source} into config.d"
        # config.d picks the parser by extension, so XML must land under .xml.
        assert mounts[source].endswith(".xml"), source
    assert len(set(mounts.values())) == len(mounts), "two overlays share a name"
    # Under any other name the image's file still merges and reopens every interface.
    assert mounts[str(_LISTEN_FILE.relative_to(REPO_ROOT))] == _IMAGE_LISTEN_NAME


def _text(root: ElementTree.Element, path: str) -> str:
    """Return the stripped text at `path`, failing on a missing element."""
    element = root.find(path)
    assert element is not None, f"no {path}"
    return (element.text or "").strip()


def test_keeper_runs_embedded_and_keeps_its_data_with_the_tables() -> None:
    root = _root(path=_REPLICATION_FILE)

    assert _text(root, "keeper_server/server_id") == "1"
    servers = root.findall("keeper_server/raft_configuration/server")
    assert [_text(server, "id") for server in servers] == ["1"]
    # Snapshots default to the data dir. Lose the log without them and every replicated table goes read-only.
    assert _text(root, "keeper_server/log_storage_path").startswith(_DATA_DIR)
    assert root.find("keeper_server/snapshot_storage_path") is None


def test_the_server_reaches_its_own_keeper_under_a_generated_identity() -> None:
    root = _root(path=_REPLICATION_FILE)
    nodes = root.findall("zookeeper/node")

    # Keeper's client port binds the image's loopback listen_host defaults.
    assert len(nodes) == 1 and _text(nodes[0], "host") in _LOOPBACK
    assert _text(nodes[0], "port") == _text(root, "keeper_server/tcp_port")
    identity = root.find("zookeeper/identity")
    assert identity is not None, "Keeper's znodes would be open to every container"
    # Without it the identity is written in clear to preprocessed_configs/config.xml.
    assert identity.attrib.get("hide_in_preprocessed") == "1", identity.attrib
    env_name = identity.attrib.get("from_env", "")
    value = _service_environment(service=_SERVICE).get(env_name, "")
    # The key `make init` mints, never a literal, and mandatory: a server started on
    # an empty value exits 231 once a real one lands.
    assert re.fullmatch(rf"\w+:\$\{{{_KEEPER_PASSWORD}:\?[^}}]+\}}", value), value
    assert _KEEPER_PASSWORD in init.GENERATED_SECRETS


def test_the_server_keeps_its_host_name_across_a_recreate() -> None:
    hostname = _service_scalar(service=_SERVICE, key="hostname")

    # Docker's default, the container id, changes on every recreate: the Replicated
    # database rewrites its host id, and the old one stays in
    # /clickhouse/task_queue/replicas for the server to resolve.
    assert hostname, f"{_SERVICE} sets no hostname"
    assert "$" not in hostname, hostname


def test_only_the_read_only_four_letter_words_answer() -> None:
    words = _text(
        _root(path=_REPLICATION_FILE), "keeper_server/four_letter_word_white_list"
    )

    assert set(words.split(",")) <= _READ_ONLY_WORDS, words


def test_the_macros_name_a_cluster_of_this_one_server() -> None:
    root = _root(path=_REPLICATION_FILE)
    macros = {child.tag: (child.text or "").strip() for child in root.find("macros")}

    assert set(macros) == {"cluster", "shard", "replica"}
    cluster = root.find(f"remote_servers/{macros['cluster']}")
    assert cluster is not None, f"no remote_servers cluster named {macros['cluster']}"
    replicas = cluster.findall("shard/replica")
    assert len(cluster.findall("shard")) == 1
    assert len(replicas) == 1 and _text(replicas[0], "host") in _LOOPBACK
    assert _text(replicas[0], "port") == "9000"


def test_sql_users_and_roles_land_in_keeper() -> None:
    directories = _root(path=_REPLICATION_FILE).find("user_directories")

    assert directories is not None
    # Merged rather than replaced, the image's local_directory stays first and wins.
    assert directories.attrib == {"replace": "replace"}
    assert [child.tag for child in directories] == ["users_xml", "replicated"]
    assert _text(directories, "users_xml/path") == "users.xml"
    assert _text(directories, "replicated/zookeeper_path")


def test_keeper_raft_and_interserver_listen_on_loopback_only() -> None:
    root = _root(path=_LISTEN_FILE)

    # Keeper's client port binds listen_host, which the image's config.xml sets to ::1 and 127.0.0.1.
    assert root.find("listen_host") is None
    assert _text(root, "interserver_listen_host") in _LOOPBACK


def test_only_the_client_protocols_listen_beyond_the_container() -> None:
    root = _root(path=_LISTEN_FILE)
    protocols = list(root.find("protocols"))

    wide = {_text(p, "port") for p in protocols if _text(p, "host") not in _LOOPBACK}
    assert wide == _CLIENT_PORTS, wide
    # The image's own listeners for these ports would bind listen_host instead.
    for port in ("http_port", "tcp_port"):
        element = root.find(port)
        assert element is not None and element.attrib == {"remove": "remove"}, port
