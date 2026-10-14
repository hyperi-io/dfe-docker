#  Project:      dfe-docker
#  File:         _envoy.py
#  Purpose:      Read connection managers, virtual hosts, routes and clusters out of the Envoy proxy configs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Read what the proxy checks assert on out of config/proxy/*.yaml.

Internal support module - imported by check_compose and the unit tests, not executed directly. Stdlib only, so it reads the one block style those configs are written in rather than YAML in general: indentation for nesting, one `- ` line per list item, and flat `{ key: value }` flow mappings. A shape outside that style raises ValueError, so a config rewritten another way fails its check instead of passing unread.
"""

from dataclasses import dataclass

_HCM_TYPE = "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager"

type Keys = dict[str, str | list[str]]


@dataclass(frozen=True, slots=True)
class Route:
    """One entry of a virtual host's `routes:` list.

    Attributes:
        match: The route's match fields, values as written.
        cluster: The cluster it forwards to, empty for a direct response.
        status: The direct response's status, empty for a forwarded route.
    """

    match: dict[str, str]
    cluster: str
    status: str


@dataclass(frozen=True, slots=True)
class VirtualHost:
    """One virtual host and the request headers it changes.

    Attributes:
        name: The virtual host's name.
        added: Every request header key it adds, as written.
        removed: Every request header name it removes, as written.
        routes: Its routes, in match order.
    """

    name: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    routes: tuple[Route, ...]


@dataclass(frozen=True, slots=True)
class ConnectionManager:
    """One HTTP connection manager, the listener it sits on and the hosts it serves.

    Attributes:
        listener: The name of the listener whose filter chain carries it.
        settings: Its scalar keys, values as written.
        virtual_hosts: The virtual hosts of its route config, in order.
    """

    listener: str
    settings: dict[str, str]
    virtual_hosts: tuple[VirtualHost, ...]


def _significant(*, text: str) -> list[str]:
    """Return the lines that carry YAML, without blank and comment-only lines."""
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _indent(*, line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _keys(*, lines: list[str]) -> Keys:
    """Return the mapping whose keys sit at the first line's indentation.

    A key with a nested block maps to the block's lines, an anchored one included; a key with only a scalar maps to that scalar.
    """
    if not (lines):
        return {}
    depth = _indent(line=lines[0])
    found: Keys = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if _indent(line=line) != depth:
            raise ValueError(f"unexpected indentation at {line.strip()!r}")
        key, separator, value = line.strip().partition(":")
        if not (separator):
            raise ValueError(f"expected a key at {line.strip()!r}")
        nested = []
        index += 1
        while index < len(lines) and _indent(line=lines[index]) > depth:
            nested.append(lines[index])
            index += 1
        found[key.strip('"')] = nested if nested else value.strip()
    return found


def _items(*, lines: list[str]) -> list[list[str]]:
    """Split a block list into its items, each item's first `- ` turned into indentation."""
    if not (lines) or not (lines[0].lstrip().startswith("- ")):
        raise ValueError(f"expected a block list at {lines[:1]!r}")
    depth = _indent(line=lines[0])
    items: list[list[str]] = []
    for line in lines:
        if _indent(line=line) == depth and line.lstrip().startswith("- "):
            items.append([f"{line[:depth]}  {line[depth + 2 :]}"])
        elif _indent(line=line) > depth:
            items[-1].append(line)
        else:
            raise ValueError(f"unexpected indentation at {line.strip()!r}")
    return items


def _scalars(*, lines: list[str]) -> list[str]:
    """Return the values of a block list of scalars."""
    values = []
    for item in _items(lines=lines):
        if len(item) != 1:
            raise ValueError(f"expected a scalar list item at {item[0].strip()!r}")
        values.append(item[0].strip().strip('"'))
    return values


def _flow(*, value: str) -> dict[str, str]:
    """Return a flat `{ key: value, ... }` flow mapping as a dict."""
    if not (value.startswith("{") and value.endswith("}")):
        raise ValueError(f"expected a flow mapping, got {value!r}")
    pairs = {}
    for part in value[1:-1].split(","):
        key, separator, item = part.partition(":")
        if not (separator):
            raise ValueError(f"expected key: value in {value!r}")
        pairs[key.strip()] = item.strip().strip('"')
    return pairs


def _block(*, keys: Keys, key: str) -> list[str]:
    """Return a key's nested block, empty when the key is absent."""
    value = keys.get(key, [])
    if isinstance(value, str):
        raise ValueError(f"{key} must be a block, got {value!r}")
    return value


def _route(*, lines: list[str]) -> Route:
    keys = _keys(lines=lines)
    match = keys.get("match")
    if not (isinstance(match, str)):
        raise ValueError("a route's match must be a flow mapping")
    forward = keys.get("route")
    direct = keys.get("direct_response")
    if isinstance(forward, str) and direct is None:
        return Route(
            match=_flow(value=match), cluster=_flow(value=forward)["cluster"], status=""
        )
    if forward is None and isinstance(direct, list):
        status = _keys(lines=direct).get("status")
        if not (isinstance(status, str)):
            raise ValueError("a direct response must carry a status")
        return Route(match=_flow(value=match), cluster="", status=status)
    raise ValueError(
        f"route {match} neither forwards to a cluster nor answers directly"
    )


def _virtual_host(*, lines: list[str]) -> VirtualHost:
    keys = _keys(lines=lines)
    added = []
    to_add = _block(keys=keys, key="request_headers_to_add")
    if to_add:
        for item in _items(lines=to_add):
            header = _keys(lines=_block(keys=_keys(lines=item), key="header"))
            added.append(str(header.get("key", "")))
    to_remove = _block(keys=keys, key="request_headers_to_remove")
    removed = _scalars(lines=to_remove) if to_remove else []
    route_lines = _block(keys=keys, key="routes")
    routes = (
        [_route(lines=item) for item in _items(lines=route_lines)]
        if route_lines
        else []
    )
    return VirtualHost(
        name=str(keys.get("name", "")),
        added=tuple(added),
        removed=tuple(removed),
        routes=tuple(routes),
    )


def _managers_on(*, listener: list[str]) -> list[ConnectionManager]:
    keys = _keys(lines=listener)
    name = str(keys.get("name", ""))
    managers = []
    for chain in _items(lines=_block(keys=keys, key="filter_chains")):
        for network_filter in _items(
            lines=_block(keys=_keys(lines=chain), key="filters")
        ):
            config = _keys(
                lines=_block(keys=_keys(lines=network_filter), key="typed_config")
            )
            if config.get("@type") != _HCM_TYPE:
                continue
            route_config = _keys(lines=_block(keys=config, key="route_config"))
            hosts = _items(lines=_block(keys=route_config, key="virtual_hosts"))
            managers.append(
                ConnectionManager(
                    listener=name,
                    settings={k: v for k, v in config.items() if isinstance(v, str)},
                    virtual_hosts=tuple(_virtual_host(lines=host) for host in hosts),
                )
            )
    return managers


def _static_resources(*, text: str) -> Keys:
    return _keys(
        lines=_block(keys=_keys(lines=_significant(text=text)), key="static_resources")
    )


def connection_managers(*, text: str) -> list[ConnectionManager]:
    """Return every HTTP connection manager in one config, in file order.

    Args:
        text: The config file's contents.

    Returns:
        One entry per connection manager, a listener with several filter chains giving several.

    Raises:
        ValueError: The config strays outside the block style this module reads.
    """
    managers = []
    for listener in _items(
        lines=_block(keys=_static_resources(text=text), key="listeners")
    ):
        managers.extend(_managers_on(listener=listener))
    return managers


def cluster_hosts(*, text: str) -> dict[str, frozenset[str]]:
    """Return each cluster's endpoint addresses by cluster name.

    Args:
        text: The config file's contents.

    Returns:
        The socket addresses each cluster's endpoints name.

    Raises:
        ValueError: The config strays outside the block style this module reads.
    """
    found = {}
    for cluster in _items(
        lines=_block(keys=_static_resources(text=text), key="clusters")
    ):
        name = str(_keys(lines=cluster).get("name", ""))
        addresses = set()
        for line in cluster:
            key, _, value = line.strip().partition(":")
            if key == "socket_address":
                addresses.add(_flow(value=value.strip())["address"])
        found[name] = frozenset(addresses)
    return found
