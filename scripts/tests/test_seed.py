#  Project:      dfe-docker
#  File:         tests/test_seed.py
#  Purpose:      Assert the seed files hold together and how the seeder reads the engine's answers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on the seed set that need no stack: its files, their references to each other and the seeder's reading of the engine's answers."""

import json
import re
from pathlib import Path
from typing import TypedDict

import pytest

import seed

# The credential rule dfe-engine applies to a fetcher config (source/models.py), so a seed file it would refuse fails here first.
_CREDENTIAL_FRAGMENTS = ("password", "secret", "token")
_CREDENTIAL_KEYS = frozenset(
    {
        "access_key_id",
        "account_key",
        "api_key",
        "client_secret",
        "integration_key",
        "private_key",
        "secret_access_key",
        "secret_key",
        "token",
    }
)
_CREDENTIAL_REFERENCES = ("bao:", "env:", "file:", "openbao:", "vault:")
# A rule names the table it reads as `FROM <database>.<table>`, quoted in backticks when the source name carries a `-`.
_FROM_TABLE = re.compile(r"\bFROM\s+(\w+)\.(?:`([\w-]+)`|(\w+)\b)")
# The starter GuardDuty rule's threshold, which the fake findings sit either side of.
_SEVERITY_FLOOR = re.compile(r"\bseverity\s*>=\s*(\d+(?:\.\d+)?)")


class ActionCase(TypedDict):
    """One status a GET can answer and what the seeder does about it."""

    expected: str
    status: int


class DeployFaultCase(TypedDict):
    """One deploy answer and whether the seeder reads its source as live."""

    answer: object
    live: bool
    status: int


class DeployedCase(TypedDict):
    """One GET /sources/{name} body and whether it names a deployed version."""

    body: object
    expected: bool


class HuntTableCase(TypedDict):
    """One hunt body and the database and table the seeder reads from it."""

    body: dict
    expected: tuple[str, str]


class RefusalCase(TypedDict):
    """One refused login and the fault the seeder reports for it."""

    expected: str
    prompted: bool
    status: int


class SkipReasonCase(TypedDict):
    """One seed object, the per-source apps the profile runs and why the seeder leaves the object alone."""

    body: dict
    expected: str
    kind: str
    per_source_apps: list[str]


_ADMIN_401 = "refused; DFE_AUTH_LOCAL_ADMIN_PASSWORD in .env is not the bootstrap admin's current password, so set it to that and run this again"
_ADMIN_UNSET = (
    "refused; set DFE_AUTH_LOCAL_ADMIN_PASSWORD in .env, which `make init` generates"
)
_FETCHER = "dfe-fetcher"
_FETCHER_BODY = {"fetcher": {"source_type": "aws"}, "source": "aws-guardduty"}
_FETCHER_VRL_BODY = {
    "fetcher": {"source_type": "okta"},
    "source": "okta-audit",
    "transform": {"engine": "vrl"},
}
_NO_FETCHER = "this profile sets no fetcher_enabled: true, so no dfe-fetcher runs it"
_NO_VRL = (
    "this profile sets no transform_vrl_enabled: true, so no dfe-transform-vrl runs it"
)
_VRL = "dfe-transform-vrl"
_VRL_BODY = {
    "match": {"field": "_source"},
    "source": "filebeat",
    "transform": {"engine": "vrl"},
}
_PROMPTED_401 = "refused; log in as an account that can write sources, rules and hunts, with its current password"
_RECEIVER_BODY = {"match": {"field": "_source"}, "source": "syslog"}

ACTION_CASES = [
    ActionCase(expected="exists", status=200),
    ActionCase(expected="create", status=404),
    ActionCase(expected="fault", status=401),
    ActionCase(expected="fault", status=409),
    ActionCase(expected="fault", status=500),
]
DEPLOY_FAULT_CASES = [
    DeployFaultCase(answer={"applied": True}, live=True, status=200),
    DeployFaultCase(
        answer={"applied": True, "apps_sync_error": "overlay"}, live=True, status=200
    ),
    DeployFaultCase(
        answer={"applied": True, "topics_failed": []}, live=True, status=200
    ),
    DeployFaultCase(
        answer={"applied": True, "topics_failed": ["aws-guardduty_land"]},
        live=False,
        status=200,
    ),
    DeployFaultCase(answer={"applied": False}, live=False, status=200),
    DeployFaultCase(answer={}, live=False, status=200),
    DeployFaultCase(answer={"applied": True}, live=False, status=404),
    DeployFaultCase(answer="no_schema", live=False, status=200),
]
DEPLOYED_CASES = [
    DeployedCase(body={"deployed_version": "1.0.0"}, expected=True),
    DeployedCase(body={"deployed_version": None}, expected=False),
    DeployedCase(body={"deployed_version": ""}, expected=False),
    DeployedCase(body={}, expected=False),
    DeployedCase(body="not found", expected=False),
    DeployedCase(body=None, expected=False),
]
HUNT_TABLE_CASES = [
    HuntTableCase(
        body={"global_source_table_name": "dfe.aws-guardduty"},
        expected=("dfe", "aws-guardduty"),
    ),
    HuntTableCase(
        body={"global_source_table_name": "dfe.syslog"}, expected=("dfe", "syslog")
    ),
    HuntTableCase(
        body={"global_source_table_name": "Data_2.a"}, expected=("Data_2", "a")
    ),
]
REFUSAL_CASES = [
    RefusalCase(expected=_PROMPTED_401, prompted=True, status=401),
    RefusalCase(expected="refused", prompted=True, status=0),
    RefusalCase(expected="refused", prompted=True, status=500),
    RefusalCase(expected=_ADMIN_UNSET, prompted=False, status=0),
    RefusalCase(expected="refused", prompted=False, status=200),
    RefusalCase(expected=_ADMIN_401, prompted=False, status=401),
    RefusalCase(expected="refused", prompted=False, status=503),
]
SKIP_REASON_CASES = [
    SkipReasonCase(
        body=_FETCHER_BODY, expected=_NO_FETCHER, kind="sources", per_source_apps=[]
    ),
    SkipReasonCase(
        body=_FETCHER_BODY, expected="", kind="sources", per_source_apps=[_FETCHER]
    ),
    SkipReasonCase(
        body=_RECEIVER_BODY, expected="", kind="sources", per_source_apps=[]
    ),
    SkipReasonCase(
        body=_RECEIVER_BODY, expected="", kind="sources", per_source_apps=[_FETCHER]
    ),
    SkipReasonCase(body=_FETCHER_BODY, expected="", kind="rules", per_source_apps=[]),
    SkipReasonCase(
        body=_VRL_BODY, expected=_NO_VRL, kind="sources", per_source_apps=[_FETCHER]
    ),
    SkipReasonCase(body=_VRL_BODY, expected="", kind="sources", per_source_apps=[_VRL]),
    SkipReasonCase(
        body=_FETCHER_VRL_BODY,
        expected=_NO_VRL,
        kind="sources",
        per_source_apps=[_FETCHER],
    ),
    SkipReasonCase(
        body=_FETCHER_VRL_BODY,
        expected=_NO_FETCHER,
        kind="sources",
        per_source_apps=[_VRL],
    ),
    SkipReasonCase(
        body=_FETCHER_VRL_BODY,
        expected="",
        kind="sources",
        per_source_apps=[_FETCHER, _VRL],
    ),
    SkipReasonCase(
        body={"source": "wasm", "transform": {"engine": "wasm"}},
        expected="no profile runs dfe-transform-wasm",
        kind="sources",
        per_source_apps=[_FETCHER, _VRL],
    ),
]


def _landing_tables() -> set[str]:
    """The `database.table` every seeded source's deploy creates."""
    return {f"dfe.{name}" for name, _ in _seeded(kind="sources")}


def _plaintext_credentials(*, path: str, value: object) -> list[str]:
    """The dot-path of every credential-shaped key holding something other than a reference."""
    found = []
    if isinstance(value, dict):
        for key, inner in value.items():
            here = f"{path}.{key}"
            lowered = str(key).lower()
            credential = (lowered in _CREDENTIAL_KEYS) or (
                any(fragment in lowered for fragment in _CREDENTIAL_FRAGMENTS)
            )
            if credential and not (
                isinstance(inner, str) and inner.startswith(_CREDENTIAL_REFERENCES)
            ):
                found.append(here)
            found.extend(_plaintext_credentials(path=here, value=inner))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            found.extend(_plaintext_credentials(path=f"{path}[{index}]", value=inner))
    return found


def _seeded(*, kind: str) -> list[tuple[str, dict]]:
    """The committed seed objects of one kind."""
    return seed._objects(directory=seed.SEED_DIR / kind, key=seed.KINDS[kind].key)


def _seeded_events() -> list[tuple[str, list[dict]]]:
    """The committed fake events, per source."""
    return seed._events(directory=seed.SEED_DIR / seed.EVENTS)


def _write(*, directory: Path, name: str, text: str) -> None:
    """Write one seed file into a scratch seed directory."""
    (directory / name).write_text(data=text, encoding="utf-8")


class TestAction:
    @pytest.mark.parametrize("case", ACTION_CASES)
    def test_matches_expected(self, case: ActionCase) -> None:
        assert seed._action(status=case["status"]) == case["expected"]


class TestDeployFault:
    @pytest.mark.parametrize("case", DEPLOY_FAULT_CASES)
    def test_matches_expected(self, case: DeployFaultCase) -> None:
        fault = seed.deploy_fault(answer=case["answer"], status=case["status"])

        assert (fault == "") is case["live"], fault


class TestDeployed:
    @pytest.mark.parametrize("case", DEPLOYED_CASES)
    def test_matches_expected(self, case: DeployedCase) -> None:
        assert seed._deployed(body=case["body"]) is case["expected"]


class TestEvents:
    def test_reads_files_in_name_order(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="b.yaml", text="- Id: b\n")
        _write(directory=tmp_path, name="a.yaml", text="- Id: a\n")

        events = seed._events(directory=tmp_path)

        assert events == [("a", [{"Id": "a"}]), ("b", [{"Id": "b"}])]

    def test_rejects_a_file_not_named_for_a_source(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="aws_guardduty.yaml", text="- Id: a\n")

        with pytest.raises(seed.SeedError, match="not named for a valid source"):
            seed._events(directory=tmp_path)

    def test_rejects_a_file_that_is_not_a_list(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="a.yaml", text="Id: a\n")

        with pytest.raises(seed.SeedError, match="list of records"):
            seed._events(directory=tmp_path)

    def test_rejects_a_record_that_is_not_a_mapping(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="a.yaml", text="- a\n")

        with pytest.raises(seed.SeedError, match="list of records"):
            seed._events(directory=tmp_path)


class TestHuntTable:
    @pytest.mark.parametrize("case", HUNT_TABLE_CASES)
    def test_matches_expected(self, case: HuntTableCase) -> None:
        assert seed._hunt_table(body=case["body"]) == case["expected"]

    @pytest.mark.parametrize(
        "value",
        [
            None,
            5,
            "",
            "aws-guardduty",
            "dfe.",
            "dfe.Aws",
            "dfe.aws-",
            "dfe.aws_guardduty",
            "dfe.`aws`",
            "dfe.a.b",
            "dfe.a' OR '1",
        ],
    )
    def test_rejects_a_value_that_is_not_a_table(self, value: object) -> None:
        with pytest.raises(seed.SeedError, match="not a <database>.<source> table"):
            seed._hunt_table(body={"global_source_table_name": value})

    def test_rejects_a_hunt_that_names_no_table(self) -> None:
        with pytest.raises(seed.SeedError, match="not a <database>.<source> table"):
            seed._hunt_table(body={})


class TestObjects:
    def test_ignores_files_that_are_not_yaml(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="a.json", text='{"name": "a"}')

        assert seed._objects(directory=tmp_path, key="name") == []

    def test_reads_files_in_name_order(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="b.yaml", text="name: b\n")
        _write(directory=tmp_path, name="a.yaml", text="name: a\n")

        names = [name for name, _ in seed._objects(directory=tmp_path, key="name")]

        assert names == ["a", "b"]

    def test_reads_nothing_from_an_empty_directory(self, tmp_path: Path) -> None:
        assert seed._objects(directory=tmp_path, key="name") == []

    def test_rejects_a_body_that_is_not_a_mapping(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="a.yaml", text="- name: a\n")

        with pytest.raises(seed.SeedError, match="not a request body"):
            seed._objects(directory=tmp_path, key="name")

    def test_rejects_a_file_that_is_not_yaml(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="a.yaml", text="name: [a\n")

        with pytest.raises(seed.SeedError, match="not readable YAML"):
            seed._objects(directory=tmp_path, key="name")

    def test_rejects_a_value_json_cannot_carry(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="a.yaml", text="name: a\nwhen: 2026-10-13\n")

        with pytest.raises(seed.SeedError, match="JSON cannot carry"):
            seed._objects(directory=tmp_path, key="name")

    def test_rejects_an_empty_file(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="a.yaml", text="")

        with pytest.raises(seed.SeedError, match="not a request body"):
            seed._objects(directory=tmp_path, key="name")

    def test_rejects_a_missing_name(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="a.yaml", text="other: a\n")

        with pytest.raises(seed.SeedError, match="must be named for"):
            seed._objects(directory=tmp_path, key="name")

    def test_rejects_a_name_that_differs_from_its_file(self, tmp_path: Path) -> None:
        _write(directory=tmp_path, name="a.yaml", text="name: b\n")

        with pytest.raises(seed.SeedError, match="must be named for"):
            seed._objects(directory=tmp_path, key="name")


class TestPromptedToken:
    def test_refuses_without_a_terminal(self) -> None:
        with pytest.raises(seed.SeedError, match="no terminal"):
            seed._prompted_token(base="http://engine.invalid", terminal=False)


class TestRefusal:
    @pytest.mark.parametrize("case", REFUSAL_CASES)
    def test_matches_expected(self, case: RefusalCase) -> None:
        refusal = seed._refusal(
            fault="refused", prompted=case["prompted"], status=case["status"]
        )

        assert refusal == case["expected"]


class TestSendEvents:
    def test_skips_the_events_of_a_source_the_profile_cannot_run(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        failed = seed._send_events(
            events=[("aws-guardduty", [{"Id": "a"}])],
            per_source_apps=[],
            sources={"aws-guardduty": _FETCHER_BODY},
        )

        assert failed == 0
        assert f"SKIP events for source 'aws-guardduty': {_NO_FETCHER}" in (
            capsys.readouterr().err
        )


class TestSkipReason:
    @pytest.mark.parametrize("case", SKIP_REASON_CASES)
    def test_matches_expected(self, case: SkipReasonCase) -> None:
        reason = seed.skip_reason(
            body=case["body"],
            kind=case["kind"],
            per_source_apps=case["per_source_apps"],
        )

        assert reason == case["expected"]


class TestTagged:
    def test_labels_the_record_with_its_source(self) -> None:
        body = seed._tagged(record={"Id": "a"}, source="aws-guardduty")

        assert json.loads(s=body) == {"Id": "a", "_source": "aws-guardduty"}

    def test_overrides_a_source_the_record_carries(self) -> None:
        body = seed._tagged(record={"_source": "main"}, source="aws-guardduty")

        assert json.loads(s=body)["_source"] == "aws-guardduty"


class TestSeedSet:
    @pytest.mark.parametrize("kind", sorted(seed.KINDS))
    def test_every_kind_has_files(self, kind: str) -> None:
        assert _seeded(kind=kind)

    def test_every_events_file_names_a_seeded_source(self) -> None:
        sources = {name for name, _ in _seeded(kind="sources")}
        for source, records in _seeded_events():
            assert source in sources
            assert records, source

    def test_every_fetcher_config_holds_credentials_only_as_references(self) -> None:
        for name, body in _seeded(kind="sources"):
            config = (body.get("fetcher") or {}).get("config") or {}
            assert not (_plaintext_credentials(path=name, value=config))

    def test_every_fetcher_config_leaves_the_engine_keys_alone(self) -> None:
        for name, body in _seeded(kind="sources"):
            config = (body.get("fetcher") or {}).get("config") or {}
            assert not ({"enabled", "topic"} & config.keys()), name

    def test_every_hunt_names_seeded_rules(self) -> None:
        rules = {name for name, _ in _seeded(kind="rules")}
        for name, body in _seeded(kind="hunts"):
            assert body["rules"], name
            assert set(body["rules"]) <= rules, name

    def test_every_hunt_reads_a_seeded_landing_table(self) -> None:
        tables = _landing_tables()
        for name, body in _seeded(kind="hunts"):
            assert body["global_source_table_name"] in tables, name

    def test_every_hunt_names_its_table_in_the_form_the_seeder_checks(self) -> None:
        for name, body in _seeded(kind="hunts"):
            database, table = seed._hunt_table(body=body)
            assert f"{database}.{table}" == body["global_source_table_name"], name

    def test_every_rule_reads_a_seeded_landing_table(self) -> None:
        tables = _landing_tables()
        for name, body in _seeded(kind="rules"):
            found = _FROM_TABLE.search(string=body["user_sql"])
            assert found, name
            table = found.group(2) or found.group(3)
            assert f"{found.group(1)}.{table}" in tables, name

    def test_every_seed_file_is_yaml(self) -> None:
        for path in seed.SEED_DIR.glob(pattern="*/*"):
            assert path.suffix == ".yaml", path

    def test_the_guardduty_events_straddle_the_rules_floor(self) -> None:
        rule = dict(_seeded(kind="rules"))["aws-guardduty-high-severity"]
        found = _SEVERITY_FLOOR.search(string=rule["user_sql"])
        assert found
        floor = float(found.group(1))
        severities = []
        for source, records in _seeded_events():
            if source == "aws-guardduty":
                severities.extend(record["Severity"] for record in records)

        assert any(severity >= floor for severity in severities)
        assert any(severity < floor for severity in severities)
