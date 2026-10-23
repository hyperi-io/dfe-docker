#  Project:      dfe-docker
#  File:         tests/test_loader_grpc_port.py
#  Purpose:      Assert dfe-loader listens on the port its senders dial
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""dfe-loader's Push listener binds the port dfe-engine points every sender at.

dfe-engine compiles the receiver's loader destination from its app manifest, while
the loader binds `config/loader/grpc.yaml`'s `grpc.listen`, else its own default. A mismatch is silent: the
receiver answers HTTP 202 and drops every record into a closed port. A text read,
because these tests run with no PyYAML.
"""

import re

from _common import COMPOSE_FILE, REPO_ROOT

# dfe-engine's appmgmt/apps.yaml declares dfe-loader `endpoints.push.port: 6000`.
MANIFEST_PUSH_PORT = 6000
# dfe-loader's GrpcConfig default listen, 0.0.0.0:6000 (src/config/kafka.rs)
LOADER_DEFAULT_LISTEN_PORT = 6000

_LOADER_CONFIG = REPO_ROOT / "config" / "loader" / "grpc.yaml"


def _loader_listen_port() -> int:
    body = _LOADER_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^\s+listen:\s*\S+:(\d+)\s*$", body, re.M)
    return int(match.group(1)) if match else LOADER_DEFAULT_LISTEN_PORT


def test_the_loader_listens_where_the_engine_points_its_senders() -> None:
    assert _loader_listen_port() == MANIFEST_PUSH_PORT


def test_compose_publishes_the_port_the_loader_listens_on() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    published = re.findall(r"\$\{DFE_LOADER_GRPC_PORT:-\d+\}:(\d+)", compose)

    assert published == [str(_loader_listen_port())]
