#  Project:      dfe-docker
#  File:         tests/test_source_flow.py
#  Purpose:      Assert test_source.py points the flow case at the receiver's published UDP ports
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The flow case sends UDP from this machine, so the wrapper hands the runner the host ports .env publishes and refuses a run that has none."""

import pytest

import test_source

PORTS = {"DFE_RECEIVER_NETFLOW_PORT": "2055", "DFE_RECEIVER_SFLOW_PORT": "6343"}


@pytest.mark.parametrize(
    "case", ["filebeat", "cloudwatch", "elastic", "vector", "edgestream-hub"]
)
def test_only_the_flow_case_gets_flow_flags(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    for key, value in PORTS.items():
        monkeypatch.setenv(key, value)
    assert test_source._flow_flags(case=case, host="localhost") == []


def test_the_flow_case_gets_the_host_and_both_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in PORTS.items():
        monkeypatch.setenv(key, value)
    assert test_source._flow_flags(case="flow", host="10.1.2.3") == [
        "--flow-host",
        "10.1.2.3",
        "--flow-netflow-port",
        "2055",
        "--flow-sflow-port",
        "6343",
    ]


@pytest.mark.parametrize("missing", sorted(PORTS))
@pytest.mark.parametrize("blank", ["", "  "])
def test_an_unpublished_port_stops_the_run_naming_its_key(
    monkeypatch: pytest.MonkeyPatch, missing: str, blank: str
) -> None:
    for key, value in PORTS.items():
        monkeypatch.setenv(key, blank if key == missing else value)
    with pytest.raises(SystemExit, match=missing):
        test_source._flow_flags(case="flow", host="localhost")
