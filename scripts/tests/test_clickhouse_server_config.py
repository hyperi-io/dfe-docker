#  Project:      dfe-docker
#  File:         tests/test_clickhouse_server_config.py
#  Purpose:      Assert the bundled ClickHouse bounds its own log and its system log tables
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

A text read of the compose file, as in test_hyperdx.py: these tests run with no
PyYAML.
"""

from pathlib import Path
from xml.etree import ElementTree

from _common import COMPOSE_FILE, REPO_ROOT

_SERVICE = "clickhouse"
_CONFIG_D = "/etc/clickhouse-server/config.d"
_LOGGER_FILE = REPO_ROOT / "clickhouse" / "server-logger.xml"
_SYSTEM_LOGS_FILE = REPO_ROOT / "clickhouse" / "system-logs.xml"

_LOGGER = {"level": "information", "size": "50M", "count": "3"}
_SYSTEM_LOG_TTL = {
    "asynchronous_metric_log": "event_date + INTERVAL 7 DAY DELETE",
    "metric_log": "event_date + INTERVAL 7 DAY DELETE",
    "part_log": "event_date + INTERVAL 7 DAY DELETE",
    "query_log": "event_date + INTERVAL 30 DAY DELETE",
    # HyperDX's clickhouse_system source reads it, so it stays on.
    "text_log": "event_date + INTERVAL 7 DAY DELETE",
}
_SWITCHED_OFF = ("trace_log",)


def _root(*, path: Path) -> ElementTree.Element:
    """Return the `<clickhouse>` element of one overlay."""
    root = ElementTree.parse(path).getroot()
    assert root.tag == "clickhouse", f"{path.name} is not a ClickHouse config"
    return root


def _service_volumes(*, service: str) -> list[str]:
    """Return one service's `volumes:` entries as written in the compose file."""
    found: list[str] = []
    in_service = False
    in_volumes = False
    for raw in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 2:
            in_service = line == f"{service}:"
            in_volumes = False
            continue
        if not in_service:
            continue
        if indent == 4:
            in_volumes = line == "volumes:"
            continue
        if in_volumes and indent == 6 and line.startswith("- "):
            found.append(line.removeprefix("- "))
    return found


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


def test_the_service_mounts_both_overlays_into_config_d() -> None:
    mounts = _config_d_mounts()

    for path in (_LOGGER_FILE, _SYSTEM_LOGS_FILE):
        source = str(path.relative_to(REPO_ROOT))
        assert source in mounts, f"{_SERVICE} does not mount {source} into config.d"
        # config.d picks the parser by extension, so XML must land under .xml.
        assert mounts[source].endswith(".xml"), source
    assert len(set(mounts.values())) == len(mounts), "two overlays share a name"
