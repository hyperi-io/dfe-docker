#  Project:      dfe-docker
#  File:         tests/test_e2e_shipped_config.py
#  Purpose:      Assert the e2e suite's generated configs reach containers without a host path on the daemon
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A generated config travels as compose `configs:` content, never a bind by host path.

Against a remote DOCKER_HOST the daemon has no `.tmp/e2e/<run>/loader-*.yaml`, so a bind mount of it creates an empty root-owned directory and the service fails with "Is a directory".
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import test_e2e

LOADER_TEXT = "clickhouse:\n  password: ${NOT_COMPOSE}\n  note: 'a $ b'\nrouting: {}\n"


@pytest.fixture
def run_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(name="TMP_DIR", target=test_e2e, value=tmp_path)
    return tmp_path


def _override(*, run_dir: Path, rendered: tuple[str, ...] = ()) -> dict:
    generated = run_dir / "loader-e2e-t.yaml"
    generated.write_text(LOADER_TEXT, encoding="utf-8")
    written = test_e2e.generate_compose_override(
        engine_environment={},
        engine_volumes=[],
        rendered_services=rendered,
        services={
            "dfe-loader": str(generated),
            "dfe-receiver": "tests/e2e/configs/receiver-e2e-org.yaml",
        },
    )
    return yaml.safe_load(Path(written).read_text(encoding="utf-8"))


def test_a_generated_config_is_shipped_as_content_not_bound_by_path(
    run_dir: Path,
) -> None:
    override = _override(run_dir=run_dir)

    target = "/etc/dfe/e2e/loader.yaml"
    for name in ("dfe-loader", "config-check-dfe-loader"):
        service = override["services"][name]
        assert "volumes" not in service
        assert service["configs"] == [
            {"source": "dfe-loader-e2e-config", "target": target}
        ]
    assert override["services"]["dfe-loader"]["command"] == ["--config", target]
    assert override["services"]["config-check-dfe-loader"]["command"] == [
        "--config",
        target,
        "config-check",
    ]
    assert str(run_dir) not in json.dumps(override["services"])


def test_content_arrives_in_the_container_exactly_as_the_file_is_written(
    run_dir: Path,
) -> None:
    override = _override(run_dir=run_dir)

    shipped = override["configs"]["dfe-loader-e2e-config"]["content"]

    # Compose interpolates content and turns each `$$` back into `$`
    assert "$$" in shipped
    assert shipped.replace("$$", "$") == LOADER_TEXT


def test_a_committed_config_stays_a_bind_mount_from_the_checkout(
    run_dir: Path,
) -> None:
    override = _override(run_dir=run_dir)

    receiver = override["services"]["dfe-receiver"]
    assert receiver["volumes"] == [
        "./tests/e2e/configs/receiver-e2e-org.yaml:/etc/dfe/receiver.yaml:ro"
    ]
    assert "configs" not in receiver
    assert set(override["configs"]) == {"dfe-loader-e2e-config"}


def test_a_rendered_service_still_runs_the_file_the_engine_renders(
    run_dir: Path,
) -> None:
    override = _override(run_dir=run_dir, rendered=("dfe-loader",))

    rendered = test_e2e._sources.rendered_config(service="dfe-loader")
    assert override["services"]["dfe-loader"]["command"] == ["--config", rendered]


@pytest.mark.parametrize(
    "volumes,named",
    [
        (["./config/absent-by-test.yaml:/etc/dfe/loader.yaml:ro"], 1),
        (["/nonexistent-dir-for-test:/app/app-env"], 1),
        (
            [
                "./tests/e2e/configs/receiver-e2e-org.yaml:/etc/dfe/receiver.yaml:ro",
                "./config/absent-by-test.yaml:/etc/dfe/loader.yaml:ro",
            ],
            1,
        ),
        (["./tests/e2e/configs/receiver-e2e-org.yaml:/etc/dfe/receiver.yaml:ro"], 0),
        ([], 0),
    ],
)
def test_the_bind_sources_that_do_not_exist_are_named(
    volumes: list[str], named: int
) -> None:
    assert len(test_e2e.missing_bind_sources(volumes=volumes)) == named


def test_the_override_fails_before_up_on_a_bind_source_that_is_missing(
    run_dir: Path,
) -> None:
    with pytest.raises(SystemExit):
        test_e2e.generate_compose_override(
            engine_environment={},
            engine_volumes=[],
            rendered_services=(),
            services={"dfe-receiver": "config/absent-by-test.yaml"},
        )

    assert not list(run_dir.glob("docker-compose.*.e2e.yaml"))


def test_the_suite_checks_the_shipped_file_is_what_the_service_runs(
    monkeypatch: pytest.MonkeyPatch, run_dir: Path
) -> None:
    generated = run_dir / "loader-e2e-t.yaml"
    args = ["--config", "/etc/dfe/e2e/loader.yaml"]
    monkeypatch.setattr(test_e2e, "container_id", lambda service: "abc")
    monkeypatch.setattr(
        test_e2e,
        "run_cmd",
        lambda argv, capture=False, cwd=None: subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=json.dumps([{"Args": args}]), stderr=""
        ),
    )
    test = SimpleNamespace(
        name="t",
        services={"dfe-loader": "config/loader/grpc.yaml"},
        rendered_services=(),
    )
    ctx = test_e2e.TestContext()

    ok = test_e2e.verify_config_mounts(ctx, test, {"dfe-loader": str(generated)})
    stale = test_e2e.verify_config_mounts(
        test_e2e.TestContext(), test, {"dfe-loader": "config/loader/grpc.yaml"}
    )

    assert ok is True
    assert stale is False
