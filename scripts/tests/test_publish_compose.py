#  Project:      dfe-docker
#  File:         tests/test_publish_compose.py
#  Purpose:      Assert the published compose files lose every bind mount, profile and open port and the demo waits for its sources
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on scripts/publish_compose.py that need no docker: the model rewrite, the demo overlay and the findings a release refuses on."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

import demo
import publish_compose
import secrets_init
from _common import FIRST_LOGIN_PASSWORD, REPO_ROOT, demo_pack_dir, demo_pack_names

COLLECTOR_CONFIG = "config/otel-collector/config.yaml"
SYSLOG_LINE = "%ASA-6-302013: x\n"
BEATS_LINE = '{"data_stream": {"dataset": "okta.system"}, "message": "{}"}\n'
# The committed packs with sources to seed
SEEDED_PACKS = [
    pack
    for pack in ["", *demo_pack_names()]
    if any((demo_pack_dir(pack=pack) / "sources").glob("*.yaml"))
]
ROLES = {
    "beats": [],
    "fetcher": ["releases"],
    "intake": ["demo-syslog"],
    "json": [],
    "vendor": ["cisco-asa", "fortinet"],
}


def _tree(root: Path) -> Path:
    """A scratch tree holding one config file, one config directory, the collector config and env/."""
    (root / "config" / "proxy").mkdir(parents=True)
    (root / "config" / "proxy" / "envoy.yaml").write_text(
        "path: ${HOME}\n", encoding="utf-8"
    )
    (root / "config" / "clickhouse").mkdir()
    for name in ("a.xml", "b.xml"):
        (root / "config" / "clickhouse" / name).write_text("<x/>\n", encoding="utf-8")
    (root / "config" / "otel-collector").mkdir()
    (root / COLLECTOR_CONFIG).write_text(
        "# CPUs, from its /proc at /hostfs\nhost_metrics:\n    root_path: /hostfs\n    scrapers: {}\n",
        encoding="utf-8",
    )
    (root / "env").mkdir()
    return root


def _bind(source: Path | str, target: str) -> dict:
    return {"type": "bind", "source": str(source), "target": target, "read_only": True}


def _model(tree: Path) -> dict:
    return {
        "name": "scratch",
        "networks": {"default": {"name": "scratch_default", "ipam": {}}},
        "services": {
            "dfe-engine": {
                "container_name": "dfe-engine",
                "deploy": {"placement": {}, "resources": {"limits": {"cpus": 2}}},
                "depends_on": {
                    "clickhouse": {"condition": "service_healthy"},
                    "kafka-apache": {"condition": "service_healthy", "required": False},
                },
                "entrypoint": None,
                "environment": {
                    publish_compose.APP_ENV_DIR_KEY: "/app/app-env",
                    "DFE_ENGINE_ENV": "production",
                },
                "group_add": ["1000"],
                "ports": [{"host_ip": "0.0.0.0", "published": "8003", "target": 8000}],
                "profiles": ["core"],
                "volumes": [
                    {"type": "volume", "source": "dfe-engine-config", "target": "/c"},
                    _bind(tree / "env", "/app/app-env"),
                    _bind(tree / "config" / "proxy" / "envoy.yaml", "/etc/envoy.yaml"),
                ],
            },
            "clickhouse": {
                "container_name": "dfe-clickhouse",
                "volumes": [_bind(tree / "config" / "clickhouse", "/etc/ch/")],
            },
            "otel-collector": {
                "volumes": [
                    _bind(tree / COLLECTOR_CONFIG, "/etc/otelcol/config.yaml"),
                    _bind("/proc", "/hostfs/proc"),
                ]
            },
        },
        "volumes": {"dfe-engine-config": {"name": "scratch_dfe-engine-config"}},
    }


class TestPublished:
    def test_rewrites_the_model_into_a_file_a_host_with_only_docker_runs(
        self, tmp_path: Path
    ) -> None:
        tree = _tree(tmp_path)

        model = publish_compose.published(
            container_prefix="demo-", model=_model(tree), name="dfe", tree=tree
        )

        engine = model["services"]["dfe-engine"]
        assert model["name"] == "dfe"
        assert model["networks"] == {"default": {"ipam": {}}}
        assert model["volumes"] == {"dfe-engine-config": {}}
        assert engine["container_name"] == "demo-dfe-engine"
        assert engine["ports"] == [
            {"host_ip": "127.0.0.1", "published": "8003", "target": 8000}
        ]
        assert engine["depends_on"] == {"clickhouse": {"condition": "service_healthy"}}
        assert engine["environment"][publish_compose.APP_ENV_DIR_KEY] == ""
        # No checkout means no `make creds`, and the stack file runs no per-source instance
        assert (
            FIRST_LOGIN_PASSWORD
            in (engine["environment"][publish_compose.CREDENTIAL_COMMAND_KEY])
        )
        assert engine["environment"][publish_compose.HOSTED_INSTANCES_KEY] == ""
        assert engine["environment"][publish_compose.APPLY_COMMAND_KEY] == (
            "docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:compose up -d "
            "--force-recreate {service}"
        )
        assert engine["deploy"] == {"resources": {"limits": {"cpus": 2}}}
        for gone in ("profiles", "group_add", "entrypoint"):
            assert gone not in engine
        assert engine["volumes"] == [
            {"type": "volume", "source": "dfe-engine-config", "target": "/c"}
        ]
        assert engine["configs"] == [
            {"source": "config-proxy-envoy.yaml", "target": "/etc/envoy.yaml"}
        ]
        assert model["configs"]["config-proxy-envoy.yaml"] == {
            "content": "path: $${HOME}\n"
        }

    def test_a_bound_directory_becomes_one_config_per_file(
        self, tmp_path: Path
    ) -> None:
        tree = _tree(tmp_path)

        model = publish_compose.published(
            container_prefix="", model=_model(tree), name="dfe", tree=tree
        )

        assert model["services"]["clickhouse"]["configs"] == [
            {"source": "config-clickhouse-a.xml", "target": "/etc/ch/a.xml"},
            {"source": "config-clickhouse-b.xml", "target": "/etc/ch/b.xml"},
        ]
        assert model["services"]["clickhouse"]["container_name"] == "dfe-clickhouse"

    def test_the_collector_loses_the_host_proc_and_reads_its_own(
        self, tmp_path: Path
    ) -> None:
        tree = _tree(tmp_path)

        model = publish_compose.published(
            container_prefix="", model=_model(tree), name="dfe", tree=tree
        )

        collector = model["services"]["otel-collector"]
        assert collector["volumes"] == []
        assert (
            "root_path"
            not in (model["configs"]["config-otel-collector-config.yaml"]["content"])
        )

    @pytest.mark.parametrize(
        ("source", "match"),
        [
            ("/var/run/docker.sock", "neither in the tree"),
            ("/etc/passwd", "neither in the tree"),
        ],
        ids=["docker-socket", "host-file"],
    )
    def test_refuses_a_bind_mount_it_cannot_publish(
        self, match: str, source: str, tmp_path: Path
    ) -> None:
        tree = _tree(tmp_path)
        model = _model(tree)
        model["services"]["clickhouse"]["volumes"].append(_bind(source, "/x"))

        with pytest.raises(publish_compose.PublishError, match=match):
            publish_compose.published(
                container_prefix="", model=model, name="dfe", tree=tree
            )


class TestConfigContent:
    @pytest.mark.parametrize(
        ("relative", "text", "expected"),
        [
            ("config/a.yaml", "plain\n", "plain\n"),
            ("config/a.yaml", "${env:X} $$ $y\n", "$${env:X} $$$$ $$y\n"),
            (
                COLLECTOR_CONFIG,
                "  # CPUs, from its /proc at /hostfs\n  m:\n    root_path: /hostfs\n",
                "  # CPUs, from the collector's own /proc\n  m:\n",
            ),
        ],
        ids=["untouched", "every-dollar-escaped", "collector-edit"],
    )
    def test_matches_expected(self, expected: str, relative: str, text: str) -> None:
        assert publish_compose.config_content(relative=relative, text=text) == expected

    def test_refuses_a_collector_config_the_edit_no_longer_fits(self) -> None:
        with pytest.raises(publish_compose.PublishError, match="no longer holds"):
            publish_compose.config_content(relative=COLLECTOR_CONFIG, text="a: 1\n")


class TestInterpolations:
    @pytest.mark.parametrize(
        ("text", "count"),
        [
            ("", 0),
            ("a$", 0),
            ("$$code", 0),
            ("$${HOME}", 0),
            ("${HOME}", 1),
            ("$HOME", 1),
            ("$$${HOME} and $X", 2),
        ],
    )
    def test_counts_what_compose_would_interpolate(self, count: int, text: str) -> None:
        assert len(publish_compose.interpolations(text=text)) == count


class TestFindings:
    def test_a_clean_model_has_none(self) -> None:
        model = {
            "services": {
                "a": {
                    "image": "x@sha256:" + "0" * 64,
                    "ports": [{"host_ip": "127.0.0.1", "target": 1}],
                    "volumes": [{"type": "volume", "source": "v", "target": "/v"}],
                }
            }
        }

        found = publish_compose.findings(model=model, text="a: $$x\n")

        assert {kind: hits for kind, hits in found.items() if hits} == {}

    def test_names_each_thing_that_blocks_publishing(self) -> None:
        model = {
            "services": {
                "a": {
                    "env_file": [{"path": "a.env"}],
                    "environment": {
                        "CLICKHOUSE_PASSWORD": "hyperdx",
                        "DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD": "letmein",
                        "DFE_ENGINE_SECRETS_PATH": "/app/config/.secrets",
                        "KEY_FILE": "/etc/tls/tls.key",
                        "MONGO_URI": "mongodb://u:p@h/db",
                        "NEXTAUTH_SECRET": "",
                    },
                    "image": "x:latest",
                    "ports": [{"host_ip": "0.0.0.0", "target": 1}, "8080:8080"],
                    "profiles": ["core"],
                    "volumes": [{"type": "bind", "source": "/s", "target": "/t"}],
                }
            }
        }

        found = publish_compose.findings(model=model, text="${X}\n")

        assert {kind: len(hits) for kind, hits in found.items()} == {
            "bind mounts": 1,
            "env_file": 1,
            "interpolation": 1,
            "literal secrets": 3,
            "profiles": 1,
            "undigested images": 1,
            "unlooped ports": 2,
        }

    def test_the_first_login_password_is_the_one_public_value(self) -> None:
        model = {
            "services": {
                "dfe-engine": {
                    "environment": {
                        "DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD": FIRST_LOGIN_PASSWORD
                    },
                    "image": "x@sha256:" + "0" * 64,
                }
            }
        }

        assert publish_compose.findings(model=model, text="")["literal secrets"] == []


class TestDemoRoles:
    @pytest.mark.usefixtures("engine_python_here")
    @pytest.mark.parametrize("pack", SEEDED_PACKS, ids=lambda p: p or "demo")
    def test_reads_each_committed_pack_as_its_seeding_does(self, pack: str) -> None:
        root = demo_pack_dir(pack=pack)
        roles = publish_compose.demo_roles(
            events=root / "events", sources=root / "sources"
        )
        loaded = demo.load_demo(events=root / "events", sources=root / "sources")

        assert roles == {
            "beats": sorted(body["source"] for body in loaded.beats),
            "fetcher": sorted(body["source"] for body in loaded.fetchers),
            "intake": [] if loaded.intake is None else [loaded.intake["source"]],
            "json": sorted(body["source"] for body in loaded.json),
            "vendor": sorted(body["source"] for body in loaded.vendors),
        }

    @pytest.mark.parametrize(
        ("tool", "expected"),
        [
            (
                True,
                {
                    "beats": ["okta"],
                    "fetcher": [],
                    "intake": ["intake"],
                    "json": [],
                    "vendor": ["cisco-asa"],
                },
            ),
            (False, None),
        ],
        ids=["replayed", "no-replay-tool"],
    )
    def test_a_source_with_beats_events_is_replayed_not_detected(
        self, expected: dict | None, tmp_path: Path, tool: bool
    ) -> None:
        (tmp_path / "sources").mkdir()
        for name in ("cisco-asa", "intake", "okta"):
            (tmp_path / "sources" / f"{name}.yaml").write_text(
                "x: 1\n", encoding="utf-8"
            )
        for name, line in (("cisco-asa", SYSLOG_LINE), ("okta", BEATS_LINE)):
            (tmp_path / "events" / name).mkdir(parents=True)
            (tmp_path / "events" / name / "demo.log").write_text(line, encoding="utf-8")
        if tool:
            (tmp_path / "events" / "generate.py").write_text("", encoding="utf-8")

        if expected is None:
            with pytest.raises(publish_compose.PublishError, match="generate.py"):
                publish_compose.demo_roles(
                    events=tmp_path / "events", sources=tmp_path / "sources"
                )
            return
        roles = publish_compose.demo_roles(
            events=tmp_path / "events", sources=tmp_path / "sources"
        )
        assert roles == expected
        assert publish_compose.demo_instances(roles=roles) == {
            "dfe-transform-elastic": ["intake", "okta"]
        }
        assert publish_compose.demo_hosted(roles=roles) == (
            "dfe-transform-elastic=intake,cisco-asa,okta"
        )

    @pytest.mark.parametrize(
        ("sources", "events"),
        [
            ({"a": "x: 1\n", "b": "x: 1\n"}, ["a", "b"]),
            ({"a": "x: 1\n", "f": "fetcher:\n  x: 1\n"}, []),
            ({"a": "x: 1\n", "b": "y: 1\n", "c": "z: 1\n"}, ["a"]),
        ],
        ids=["no-intake", "no-vendor", "two-intakes"],
    )
    def test_refuses_a_demo_make_demo_would_refuse(
        self, events: list[str], sources: dict[str, str], tmp_path: Path
    ) -> None:
        (tmp_path / "sources").mkdir()
        (tmp_path / "events").mkdir()
        for name, text in sources.items():
            (tmp_path / "sources" / f"{name}.yaml").write_text(text, encoding="utf-8")
        for name in events:
            (tmp_path / "events" / name).mkdir()

        with pytest.raises(publish_compose.PublishError, match="needs one intake"):
            publish_compose.demo_roles(
                events=tmp_path / "events", sources=tmp_path / "sources"
            )

    @pytest.mark.parametrize("dirs", [[], ["sources", "events"]], ids=["bare", "empty"])
    def test_a_pack_with_no_source_files_has_no_roles(
        self, dirs: list[str], tmp_path: Path
    ) -> None:
        for name in dirs:
            (tmp_path / name).mkdir()

        roles = publish_compose.demo_roles(
            events=tmp_path / "events", sources=tmp_path / "sources"
        )

        assert roles == {
            "beats": [],
            "fetcher": [],
            "intake": [],
            "json": [],
            "vendor": [],
        }
        assert publish_compose.demo_instances(roles=roles) == {}
        assert publish_compose.demo_hosted(roles=roles) == ""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("match:\n  field: kind\n", {"json": ["kpis"], "vendor": []}),
            ("match:\n  field: kind\ntransform:\n  x: 1\n", None),
            ("x: 1\n", None),
        ],
        ids=["matched-no-transform", "matched-with-transform", "unmatched"],
    )
    def test_a_matched_source_with_no_transform_replays_its_json_and_needs_no_intake(
        self, expected: dict | None, tmp_path: Path, text: str
    ) -> None:
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "kpis.yaml").write_text(text, encoding="utf-8")
        (tmp_path / "sources" / "bgp.yaml").write_text(
            "fetcher:\n  x: 1\n", encoding="utf-8"
        )
        (tmp_path / "events" / "kpis").mkdir(parents=True)
        (tmp_path / "events" / "generate.py").write_text("", encoding="utf-8")

        if expected is None:
            # Anything else with events is a vendor, which needs an intake to detect it
            with pytest.raises(publish_compose.PublishError, match="needs one intake"):
                publish_compose.demo_roles(
                    events=tmp_path / "events", sources=tmp_path / "sources"
                )
            return
        roles = publish_compose.demo_roles(
            events=tmp_path / "events", sources=tmp_path / "sources"
        )

        assert roles == {"beats": [], "fetcher": ["bgp"], "intake": [], **expected}
        assert publish_compose.demo_instances(roles=roles) == {}
        assert publish_compose.demo_hosted(roles=roles) == ""

    def test_refuses_json_sources_with_no_replay_tool(self, tmp_path: Path) -> None:
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "kpis.yaml").write_text(
            "match:\n  field: kind\n", encoding="utf-8"
        )
        (tmp_path / "events" / "kpis").mkdir(parents=True)

        with pytest.raises(
            publish_compose.PublishError, match=r"\['kpis'\].*generate.py"
        ):
            publish_compose.demo_roles(
                events=tmp_path / "events", sources=tmp_path / "sources"
            )


class TestDemoFile:
    @pytest.mark.parametrize(
        ("pack", "expected"),
        [
            ("", ("dfe-demo.yaml", "demo-", "dfe-demo", "demo")),
            (
                "cyber",
                ("dfe-demo-cyber.yaml", "demo-cyber-", "dfe-demo-cyber", "demo-cyber"),
            ),
            (
                "theatre",
                (
                    "dfe-demo-theatre.yaml",
                    "demo-theatre-",
                    "dfe-demo-theatre",
                    "demo-theatre",
                ),
            ),
        ],
        ids=["demo", "cyber", "theatre"],
    )
    def test_a_pack_renders_its_own_project_and_prefix(
        self, expected: tuple[str, str, str, str], pack: str, tmp_path: Path
    ) -> None:
        target = publish_compose.demo_file(pack=pack)
        tree = _tree(tmp_path)

        model = publish_compose.published(
            container_prefix=target.prefix,
            model=_model(tree),
            name=target.project,
            tree=tree,
        )

        assert (target.file, target.prefix, target.project, target.tag) == expected
        assert model["name"] == target.project
        assert model["services"]["dfe-engine"]["container_name"] == (
            f"{target.prefix}dfe-engine"
        )

    @pytest.mark.parametrize(
        "argv",
        [["--pack", "nope"], ["--pack", "../demo"], ["--pack", "Cyber"]],
        ids=["no-directory", "path", "upper-case"],
    )
    def test_an_unknown_pack_exits_2_naming_the_packs_that_exist(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exited:
            publish_compose.main(argv)

        assert exited.value.code == 2
        assert "resources/demo-cyber" in capsys.readouterr().err


def _stack(services: list[str]) -> dict:
    service = {"deploy": {"resources": {}}, "dns_search": ["."], "image": "i"}
    model = {"services": {name: dict(service) for name in services}}
    model["services"]["dfe-engine"]["environment"] = {
        publish_compose.BREAKGLASS_PASSWORD_KEY: "bg"
    }
    model["services"][publish_compose.TRANSFORM_ELASTIC_CONTRACT]["image"] = (
        "ghcr.io/hyperi-io/dfe-transform-elastic:v1.1.31@sha256:" + "a" * 64
    )
    return model


DEMO_STACK = [
    "dfe-engine",
    "dfe-receiver",
    "dfe-hyperdx",
    "dlq-init",
    publish_compose.TRANSFORM_ELASTIC_CONTRACT,
    "dfe-transform-elastic-demo-syslog",
    "dfe-fetcher",
]
ARCHIVE = {
    "max_megabytes": 1,
    "retention_minutes": 2,
    "sources": ["demo-syslog"],
    "sweep_minutes": 3,
}


class TestDemoOverlay:
    def test_holds_the_data_path_until_the_sources_exist(self) -> None:
        model = publish_compose.demo_overlay(
            archive=ARCHIVE,
            image="ghcr.io/hyperi-io/dfe-docker@sha256:" + "b" * 64,
            model=_stack(DEMO_STACK),
            roles=ROLES,
            stream=["scripts/replay_syslog.py", "tcp", "20"],
        )

        services = model["services"]
        for held in (
            "dfe-receiver",
            "dfe-transform-elastic-demo-syslog",
            "dfe-fetcher",
        ):
            assert services[held]["depends_on"]["demo-sources"] == {
                "condition": "service_completed_successfully",
                "required": True,
            }
        assert services["demo-sources"]["environment"] == {
            "DFE_IN_STACK": "true",
            publish_compose.BREAKGLASS_PASSWORD_KEY: "bg",
        }
        assert services["demo-feed"]["environment"][demo.TRANSFORM_VERSION_KEY] == (
            "v1.1.31@sha256:" + "a" * 64
        )
        stream = services["demo-stream"]["entrypoint"]
        assert stream[:3] == ["python3", "-I", "/demo/scripts/replay_syslog.py"]
        assert stream[stream.index("--target") + 1] == "tcp://dfe-receiver:514"
        assert stream[stream.index("--sources") + 1] == "cisco-asa,fortinet"
        assert services["demo-stream"]["volumes"][0]["volume"] == {"subpath": "feed"}
        assert services["demo-prune"]["entrypoint"][2] == (
            "/opt/dfe-docker/resources/demo/archive.py"
        )
        assert model["volumes"][publish_compose.DEMO_FEED_VOLUME] == {}
        assert "demo-beats" not in services
        # The engine accepts exactly the demo's transformed sources, so a console-added one is refused
        assert (
            services["dfe-engine"]["environment"][publish_compose.HOSTED_INSTANCES_KEY]
            == "dfe-transform-elastic=demo-syslog,cisco-asa,fortinet"
        )
        assert services["dfe-engine"]["environment"][
            publish_compose.APPLY_COMMAND_KEY
        ] == (
            "docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo up -d "
            "--force-recreate {service}"
        )

    def test_a_pack_with_no_sources_seeds_itself_and_replays_nothing(self) -> None:
        stack = _stack(
            [name for name in DEMO_STACK if name != "dfe-transform-elastic-demo-syslog"]
        )
        roles = {"beats": [], "fetcher": [], "intake": [], "json": [], "vendor": []}

        model = publish_compose.demo_overlay(
            archive={**ARCHIVE, "sources": []},
            image="i",
            model=stack,
            pack="cyber",
            roles=roles,
            stream=["scripts/replay_syslog.py", "tcp", "20"],
        )

        services = model["services"]
        for helper in ("demo-sources", "demo-console"):
            assert services[helper]["entrypoint"] == [
                "python3",
                "/opt/dfe-docker/scripts/demo_seed.py",
                "--pack",
                "cyber",
            ]
        for absent in ("demo-feed", "demo-stream", "demo-prune", "demo-beats"):
            assert absent not in services
        assert publish_compose.DEMO_FEED_VOLUME not in model.get("volumes", {})
        assert "demo-sources" in services["dfe-receiver"]["depends_on"]
        assert "demo-sources" not in services["dfe-fetcher"].get("depends_on", {})
        engine = services["dfe-engine"]["environment"]
        assert engine[publish_compose.HOSTED_INSTANCES_KEY] == ""
        assert engine[publish_compose.APPLY_COMMAND_KEY] == (
            "docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo-cyber up -d "
            "--force-recreate {service}"
        )

    def test_a_pack_with_beats_sources_replays_them_and_hosts_their_transforms(
        self,
    ) -> None:
        beats = ["crowdstrike", "okta"]
        instances = [f"dfe-transform-elastic-{name}" for name in beats]
        roles = {**ROLES, "beats": beats}

        model = publish_compose.demo_overlay(
            archive=ARCHIVE,
            image="i",
            model=_stack([*DEMO_STACK, *instances]),
            pack="cyber",
            roles=roles,
            stream=["scripts/replay_syslog.py", "tcp", "20"],
        )

        services = model["services"]
        replay = services["demo-beats"]
        assert replay["entrypoint"] == [
            "python3",
            "-I",
            "/opt/dfe-docker/resources/demo-cyber/events/generate.py",
            "replay",
            "--target",
            "http://dfe-receiver:8080/ingest",
            "--sources",
            "crowdstrike,okta",
            "--loop",
            "--duration",
            str(demo.STREAM_SECONDS),
        ]
        assert replay["depends_on"] == {
            "demo-console": {"condition": "service_completed_successfully"},
            "demo-sources": {"condition": "service_completed_successfully"},
            "dfe-receiver": {"condition": "service_healthy"},
        }
        assert replay["restart"] == "unless-stopped"
        for held in instances:
            assert "demo-sources" in services[held]["depends_on"]
        assert services["dfe-engine"]["environment"][
            publish_compose.HOSTED_INSTANCES_KEY
        ] == ("dfe-transform-elastic=demo-syslog,cisco-asa,fortinet,crowdstrike,okta")
        assert publish_compose.demo_instances(roles=roles) == {
            "dfe-transform-elastic": ["demo-syslog", "crowdstrike", "okta"]
        }

    def test_a_pack_of_json_sources_replays_them_with_no_transform_or_stream(
        self,
    ) -> None:
        stack = _stack(
            [name for name in DEMO_STACK if name != "dfe-transform-elastic-demo-syslog"]
        )
        roles = {
            "beats": [],
            "fetcher": ["bgp-updates"],
            "intake": [],
            "json": ["core-probes", "ran-kpis"],
            "vendor": [],
        }

        model = publish_compose.demo_overlay(
            archive={**ARCHIVE, "sources": []},
            image="i",
            model=stack,
            pack="telco",
            roles=roles,
            stream=["scripts/replay_syslog.py", "tcp", "20"],
        )

        services = model["services"]
        replay = services["demo-beats"]["entrypoint"]
        assert replay[2:4] == [
            "/opt/dfe-docker/resources/demo-telco/events/generate.py",
            "replay",
        ]
        assert replay[replay.index("--sources") + 1] == "core-probes,ran-kpis"
        assert services["demo-beats"]["depends_on"]["demo-console"] == {
            "condition": "service_completed_successfully"
        }
        for absent in ("demo-feed", "demo-stream", "demo-prune"):
            assert absent not in services
        assert "demo-sources" in services["dfe-fetcher"]["depends_on"]
        assert publish_compose.demo_instances(roles=roles) == {}
        assert (
            services["dfe-engine"]["environment"][publish_compose.HOSTED_INSTANCES_KEY]
            == ""
        )

    def test_refuses_a_stack_missing_a_beats_transform(self) -> None:
        with pytest.raises(publish_compose.PublishError, match="okta"):
            publish_compose.demo_overlay(
                archive=ARCHIVE,
                image="i",
                model=_stack(DEMO_STACK),
                pack="cyber",
                roles={**ROLES, "beats": ["okta"]},
                stream=["scripts/replay_syslog.py", "tcp", "20"],
            )

    def test_the_demo_seeds_with_no_pack_argument(self) -> None:
        model = publish_compose.demo_overlay(
            archive=ARCHIVE,
            image="i",
            model=_stack(DEMO_STACK),
            roles=ROLES,
            stream=["scripts/replay_syslog.py", "tcp", "20"],
        )

        for helper in ("demo-sources", "demo-feed", "demo-console"):
            assert model["services"][helper]["entrypoint"] == [
                "python3",
                "/opt/dfe-docker/scripts/demo_seed.py",
            ]

    def test_declares_no_fetcher_instance(self) -> None:
        # Every fetcher source runs in the one committed dfe-fetcher
        assert publish_compose.demo_instances(roles=ROLES) == {
            "dfe-transform-elastic": ["demo-syslog"]
        }

    def test_refuses_a_stack_that_declares_no_demo_instance(self) -> None:
        stack = _stack(
            [name for name in DEMO_STACK if name != "dfe-transform-elastic-demo-syslog"]
        )

        with pytest.raises(publish_compose.PublishError, match="demo-syslog"):
            publish_compose.demo_overlay(
                archive=ARCHIVE,
                image="i",
                model=stack,
                roles=ROLES,
                stream=["scripts/replay_syslog.py", "tcp", "20"],
            )


class TestTags:
    @pytest.mark.parametrize(
        ("moving", "expected"),
        [(False, ["compose-1.0.5"]), (True, ["compose-1.0.5", "compose"])],
        ids=["pre-release", "release"],
    )
    def test_matches_expected(self, expected: list[str], moving: bool) -> None:
        assert (
            publish_compose.published_tags(
                moving=moving, release="1.0.5", tag="compose"
            )
            == expected
        )


CH_SECRET = "c" * 24
OTEL_SECRET = "o" * 24
JWT_SECRET = "j" * 48
MINTED = {
    CH_SECRET: "CLICKHOUSE_PASSWORD",
    OTEL_SECRET: "DFE_OTEL_WRITER_CLICKHOUSE_PASSWORD",
    JWT_SECRET: "DFE_ENGINE_API_JWT_SECRET",
}
# What each pinned image's registry config runs, standing in for the registry read
IMAGE_COMMANDS = {
    "clickhouse": (["/entrypoint.sh"], []),
    "documentdb": (["/bin/bash", "-c", 'run.sh "$@"', "--"], []),
}


def _image_command(*, image: str) -> tuple[list[str], list[str]]:
    return IMAGE_COMMANDS[image]


def _secret_stack() -> dict:
    sidecar = {"deploy": {"resources": {}}, "dns_search": ["."], "image": "i"}
    return {
        "configs": {
            "config-otel-collector-config.yaml": {
                "content": "password: $${env:CLICKHOUSE_PASSWORD}\nhost: $${env:CLICKHOUSE_HOST}\n"
            }
        },
        "services": {
            "clickhouse": {
                "environment": {
                    "CLICKHOUSE_PASSWORD": CH_SECRET,
                    "CLICKHOUSE_DB": "default",
                },
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        'clickhouse-client --password "$$CLICKHOUSE_PASSWORD"',
                    ]
                },
                "image": "clickhouse",
            },
            "dfe-hunt-runner": {
                "command": ["run"],
                "entrypoint": ["dfe-hunt-runner"],
                "environment": {"DFE_ENGINE_API_JWT_SECRET": JWT_SECRET},
                "image": "engine",
            },
            "dlq-init": sidecar,
            "hyperdx-documentdb": {
                "environment": {"URI": f"mongodb://u:{CH_SECRET}@h/db?tls=true"},
                "healthcheck": {"test": ["CMD", "/usr/local/bin/check"]},
                "image": "documentdb",
            },
            "otel-collector": {
                "environment": {
                    "CLICKHOUSE_PASSWORD": OTEL_SECRET,
                    "CLICKHOUSE_HOST": "clickhouse",
                },
                "image": "otel",
            },
            "dfe-receiver": {"environment": {"LOG_LEVEL": "info"}, "image": "receiver"},
        },
    }


class TestSecure:
    def _secured(self) -> dict:
        return publish_compose.secure(
            image="dfe-docker@sha256:" + "d" * 64,
            image_command=_image_command,
            minted=MINTED,
            model=_secret_stack(),
        )

    def test_no_minted_value_is_left_and_secrets_init_holds_the_templates(self) -> None:
        model = self._secured()

        text = json.dumps(model)
        assert all(secret not in text for secret in MINTED)
        plan = json.loads(
            model["configs"]["secrets-plan.json"]["content"].replace("$$", "$")
        )
        assert plan["consumers"]["clickhouse"]["env"] == {
            "CLICKHOUSE_PASSWORD": "@@CLICKHOUSE_PASSWORD@@"
        }
        assert plan["consumers"]["hyperdx-documentdb"]["env"] == {
            "URI": "mongodb://u:@@CLICKHOUSE_PASSWORD@@@h/db?tls=true"
        }
        assert plan["consumers"]["otel-collector"]["files"] == {
            "CLICKHOUSE_PASSWORD": "@@DFE_OTEL_WRITER_CLICKHOUSE_PASSWORD@@"
        }
        init = model["services"]["secrets-init"]
        assert (init["user"], init["image"]) == ("0:0", "dfe-docker@sha256:" + "d" * 64)
        assert model["volumes"]["dfe-secrets"] == {}

    def test_a_consumer_sources_its_own_directory_and_then_runs_what_it_ran(
        self,
    ) -> None:
        services = self._secured()["services"]

        clickhouse = services["clickhouse"]
        assert clickhouse["environment"] == {"CLICKHOUSE_DB": "default"}
        assert clickhouse["entrypoint"][:2] == ["/bin/sh", "-c"]
        assert clickhouse["command"] == ["/entrypoint.sh"]
        assert clickhouse["healthcheck"]["test"][1].startswith(
            "set -a && . /run/dfe-secrets/env && set +a && clickhouse-client"
        )
        assert services["dfe-hunt-runner"]["command"] == ["dfe-hunt-runner", "run"]
        assert services["hyperdx-documentdb"]["command"] == [
            "/bin/bash",
            "-c",
            'run.sh "$$@"',
            "--",
        ]
        assert services["hyperdx-documentdb"]["healthcheck"]["test"][-1] == (
            "/usr/local/bin/check"
        )
        gids = {
            name: services[name]["group_add"]
            for name in ("clickhouse", "otel-collector")
        }
        assert len({gid[0] for gid in gids.values()}) == 2
        mount = services["clickhouse"]["volumes"][-1]
        assert (mount["source"], mount["read_only"], mount["volume"]) == (
            "dfe-secrets",
            True,
            {"subpath": "clickhouse"},
        )
        assert services["clickhouse"]["depends_on"]["secrets-init"]["condition"] == (
            "service_completed_successfully"
        )
        assert "group_add" not in services["dfe-receiver"]

    def test_the_collector_reads_its_password_through_its_file_provider(self) -> None:
        model = self._secured()

        collector = model["services"]["otel-collector"]
        assert "entrypoint" not in collector
        assert collector["environment"] == {"CLICKHOUSE_HOST": "clickhouse"}
        assert model["configs"]["config-otel-collector-config.yaml"]["content"] == (
            "password: $${file:/run/dfe-secrets/CLICKHOUSE_PASSWORD}\nhost: $${env:CLICKHOUSE_HOST}\n"
        )

    def test_refuses_a_collector_config_that_reads_the_secret_another_way(self) -> None:
        stack = _secret_stack()
        stack["configs"]["config-otel-collector-config.yaml"]["content"] = (
            "password: x\n"
        )

        with pytest.raises(publish_compose.PublishError, match="some other way"):
            publish_compose.secure(
                image="i", image_command=_image_command, minted=MINTED, model=stack
            )

    def test_a_stack_with_no_secret_gets_no_secrets_init(self) -> None:
        stack = {"services": {"a": {"environment": {"X": "1"}, "image": "a"}}}

        assert (
            publish_compose.secure(
                image="i", image_command=_image_command, minted=MINTED, model=stack
            )
            == stack
        )


class TestMintedSecrets:
    def test_reads_every_generated_secret_but_the_public_first_login(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "env").mkdir()
        (tmp_path / ".env").write_text(
            "CLICKHOUSE_PASSWORD=ch\nOTHER=x\n", encoding="utf-8"
        )
        for name in ("engine.env", "hyperdx.env", "ui.env"):
            (tmp_path / "env" / name).write_text("", encoding="utf-8")
        (tmp_path / "env" / "engine.env").write_text(
            f"DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD={FIRST_LOGIN_PASSWORD}\nDFE_ENGINE_AUTH_BREAKGLASS_PASSWORD=bg\n",
            encoding="utf-8",
        )

        assert publish_compose.minted_secrets(tree=tmp_path) == {
            "ch": "CLICKHOUSE_PASSWORD",
            "bg": "DFE_ENGINE_AUTH_BREAKGLASS_PASSWORD",
        }


class TestSecretsInit:
    def _plan(self) -> dict:
        gid = os.getgid()
        return {
            "consumers": {
                "dfe-hyperdx": {
                    "env": {
                        "MONGO_URI": "mongodb://u:@@HYPERDX_DOCUMENTDB_PASSWORD@@@h/db?a=1&b=2"
                    },
                    "gid": gid,
                },
                "otel-collector": {
                    "files": {
                        "CLICKHOUSE_PASSWORD": "@@DFE_OTEL_WRITER_CLICKHOUSE_PASSWORD@@"
                    },
                    "gid": gid,
                },
            }
        }

    def test_mints_once_and_writes_each_service_only_its_own(
        self, tmp_path: Path
    ) -> None:
        secrets_init.apply(plan=self._plan(), root=tmp_path)
        first = (tmp_path / "otel-collector" / "CLICKHOUSE_PASSWORD").read_text(
            encoding="utf-8"
        )
        secrets_init.apply(plan=self._plan(), root=tmp_path)

        assert (tmp_path / "otel-collector" / "CLICKHOUSE_PASSWORD").read_text(
            encoding="utf-8"
        ) == first
        assert re.fullmatch(r"[A-Za-z0-9]{24}", first)
        env = (tmp_path / "dfe-hyperdx" / "env").read_text(encoding="utf-8")
        assert re.fullmatch(
            r"MONGO_URI='mongodb://u:[A-Za-z0-9]{24}@h/db\?a=1&b=2'\n", env
        )
        assert stat.S_IMODE((tmp_path / "dfe-hyperdx" / "env").stat().st_mode) == 0o440
        assert stat.S_IMODE((tmp_path / "dfe-hyperdx").stat().st_mode) == 0o750
        assert sorted(path.name for path in (tmp_path / ".minted").iterdir()) == [
            "DFE_OTEL_WRITER_CLICKHOUSE_PASSWORD",
            "HYPERDX_DOCUMENTDB_PASSWORD",
        ]

    def test_the_env_file_sources_back_to_the_rendered_value(
        self, tmp_path: Path
    ) -> None:
        secrets_init.apply(plan=self._plan(), root=tmp_path)

        sourced = subprocess.run(
            [
                "sh",
                "-c",
                '. "$0" && printf %s "$MONGO_URI"',
                str(tmp_path / "dfe-hyperdx" / "env"),
            ],
            capture_output=True,
            check=True,
            text=True,
        ).stdout

        assert re.fullmatch(r"mongodb://u:[A-Za-z0-9]{24}@h/db\?a=1&b=2", sourced)

    @pytest.mark.parametrize(
        "template", ["@@NOT_A_SECRET@@", "x@@lower@@"], ids=["unknown-key", "not-a-key"]
    )
    def test_refuses_a_plan_naming_a_key_init_does_not_mint(
        self, template: str, tmp_path: Path
    ) -> None:
        plan = {"consumers": {"a": {"env": {"X": template}, "gid": os.getgid()}}}

        if template == "x@@lower@@":
            secrets_init.apply(plan=plan, root=tmp_path)
            assert (tmp_path / "a" / "env").read_text(
                encoding="utf-8"
            ) == "X=x@@lower@@\n"
        else:
            with pytest.raises(secrets_init.SecretsError, match="does not mint"):
                secrets_init.apply(plan=plan, root=tmp_path)


def test_the_scratch_tree_holds_what_the_image_carries() -> None:
    allowlist = publish_compose.image_allowlist(
        dockerignore=(REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    )

    assert {"scripts", "resources", "config", "docker-compose.yml"} <= set(allowlist)
    assert all((REPO_ROOT / name).exists() for name in allowlist)
