#  Project:      dfe-docker
#  File:         tests/test_init.py
#  Purpose:      Assert `make init` mints each secret into its own file and asks the retention question once
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The secrets and the retention question `make init` writes.

Each secret lands in the file it lives in, .env or an env/<component>.env. An
existing file that predates one is topped up. A TTY is asked the retention once
for a NEW env/engine.env and the answer lands live. A pipe keeps the template's
commented default, the environment pre-answers, junk is re-asked at the prompt
and refused from the environment and an existing env/engine.env is never
re-asked.
"""

import stat
from collections.abc import Callable
from pathlib import Path

import pytest

import init

KEY = init.RETENTION_KEY
_TEMPLATE = f"""\
## A template with the retention placeholder.
# CLICKHOUSE_SECURE=false
#
## Days every time-series table keeps rows.
# {KEY}=90
"""
_SETTLED = "\n".join(
    f"{key}=already-minted-value" for key in init.GENERATED_SECRETS[".env"]
)
_ENGINE_SETTLED = "".join(
    f"{key}=already-minted-value\n" for key in init.GENERATED_SECRETS["engine.env"]
)
# A component with no secret of its own, which is copied as it stands
_PLAIN_TEMPLATE = "# DFE_LOADER_LOG_LEVEL=info\n"


def _a_tty() -> bool:
    """Answer `sys.stdin.isatty` as a terminal does."""
    return True


def _engine_env(*, dotenv: Path) -> Path:
    """Return the env/engine.env beside the throwaway .env."""
    return dotenv.parent / "env" / "engine.env"


def _live(*, path: Path) -> dict[str, str]:
    """Return the live assignments of one written file."""
    live = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not (line) or (line.startswith("#")):
            continue
        key, _, value = line.partition("=")
        live[key] = value
    return live


def _never_asked(_prompt: str = "") -> str:
    """Stand in for input() where a run must not prompt."""
    raise AssertionError("input() must not be called")


def _not_a_tty() -> bool:
    """Answer `sys.stdin.isatty` as a pipe does."""
    return False


def _replies(*values: str) -> Callable[[str], str]:
    """Return an input() stand-in that answers with each of values in turn."""

    def answer(_prompt: str = "") -> str:
        return next(answers)

    answers = iter(values)
    return answer


@pytest.fixture
def layout(dotenv: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path init.main reads at the throwaway tree and return the engine template."""
    template = dotenv.parent / ".env.example"
    template.write_text("# CLICKHOUSE_SECURE=false\n", encoding="utf-8", newline="\n")
    env_templates = dotenv.parent / "env.example"
    env_templates.mkdir()
    engine_template = env_templates / "engine.env"
    engine_template.write_text(_TEMPLATE, encoding="utf-8", newline="\n")
    for name in ("hyperdx.env", "ui.env"):
        (env_templates / name).write_text("", encoding="utf-8", newline="\n")
    (env_templates / "loader.env").write_text(
        _PLAIN_TEMPLATE, encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr(init, "DOTENV_TEMPLATE", template)
    monkeypatch.setattr(init, "ENV_TEMPLATE_DIR", env_templates)
    monkeypatch.setattr(init, "ENV_DIR", dotenv.parent / "env")
    monkeypatch.delenv(KEY, raising=False)
    return engine_template


def test_help_prints_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], dotenv: Path, layout: Path
) -> None:
    with pytest.raises(SystemExit) as exited:
        init.main(argv=["--help"])
    assert exited.value.code == 0
    assert "Bootstrap local config files" in capsys.readouterr().out
    assert not (dotenv.exists())
    assert not ((dotenv.parent / "env").exists())


def test_each_secret_is_minted_into_the_file_it_lives_in(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", _not_a_tty)

    assert init.main() == 0
    for name, keys in init.GENERATED_SECRETS.items():
        path = dotenv if name == dotenv.name else dotenv.parent / "env" / name
        live = _live(path=path)
        minted = sorted(key for key in live if key in init._SECRET_LENGTHS)
        assert minted == sorted(keys), name
        assert all(live[key] for key in keys), name
    loader_env = dotenv.parent / "env" / "loader.env"
    assert loader_env.read_text(encoding="utf-8") == _PLAIN_TEMPLATE


def test_an_existing_env_file_without_its_secrets_is_topped_up_and_closed(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", _not_a_tty)
    dotenv.write_text(_SETTLED + "\n", encoding="utf-8", newline="\n")
    env_dir = dotenv.parent / "env"
    env_dir.mkdir()
    ui_env = env_dir / "ui.env"
    ui_env.write_text("# NEXTAUTH_SECRET=\nNODE_ENV=production\n", encoding="utf-8")
    ui_env.chmod(0o664)
    loader_env = env_dir / "loader.env"
    loader_env.write_text(_PLAIN_TEMPLATE, encoding="utf-8")
    loader_env.chmod(0o664)

    assert init.main() == 0
    live = _live(path=ui_env)
    assert len(live["NEXTAUTH_SECRET"]) == init._SECRET_LENGTHS["NEXTAUTH_SECRET"]
    assert live["NODE_ENV"] == "production"
    assert stat.S_IMODE(ui_env.stat().st_mode) & stat.S_IRWXO == 0
    # No secret make init mints, but an operator's credentials go in it all the same
    assert stat.S_IMODE(loader_env.stat().st_mode) == 0o660
    assert loader_env.read_text(encoding="utf-8") == _PLAIN_TEMPLATE
    assert dotenv.read_text(encoding="utf-8") == _SETTLED + "\n"


def test_a_tty_answer_is_written_live_into_the_engine_env(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", _a_tty)
    monkeypatch.setattr("builtins.input", _replies("30"))

    assert init.main() == 0
    written = _engine_env(dotenv=dotenv).read_text(encoding="utf-8")
    assert f"\n{KEY}=30\n" in written
    assert f"# {KEY}=90" not in written
    assert "# CLICKHOUSE_SECURE=false" in written
    assert KEY not in dotenv.read_text(encoding="utf-8")


def test_junk_is_re_asked_until_a_whole_number_arrives(
    capsys: pytest.CaptureFixture[str],
    dotenv: Path,
    layout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", _a_tty)
    monkeypatch.setattr("builtins.input", _replies("ninety", "-1", "1.5", "0"))

    assert init.main() == 0
    assert f"\n{KEY}=0\n" in _engine_env(dotenv=dotenv).read_text(encoding="utf-8")
    assert capsys.readouterr().err.count("whole number of days") == 3


def test_an_empty_answer_takes_the_default(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", _a_tty)
    monkeypatch.setattr("builtins.input", _replies(""))

    assert init.main() == 0
    assert f"\n{KEY}=90\n" in _engine_env(dotenv=dotenv).read_text(encoding="utf-8")


def test_a_pipe_keeps_the_template_line(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", _not_a_tty)
    monkeypatch.setattr("builtins.input", _never_asked)

    assert init.main() == 0
    written = _engine_env(dotenv=dotenv).read_text(encoding="utf-8")
    assert f"# {KEY}=90" in written
    assert f"\n{KEY}=" not in written


def test_the_environment_pre_answers_without_a_prompt(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY, "14")
    monkeypatch.setattr("sys.stdin.isatty", _a_tty)
    monkeypatch.setattr("builtins.input", _never_asked)

    assert init.main() == 0
    assert f"\n{KEY}=14\n" in _engine_env(dotenv=dotenv).read_text(encoding="utf-8")


def test_junk_in_the_environment_is_refused(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY, "soon")
    monkeypatch.setattr("sys.stdin.isatty", _not_a_tty)

    with pytest.raises(SystemExit, match=KEY):
        init.main()


def test_a_template_without_the_key_still_gets_the_line(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout.write_text("# CLICKHOUSE_SECURE=false\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv(KEY, "7")

    assert init.main() == 0
    written = _engine_env(dotenv=dotenv).read_text(encoding="utf-8")
    assert written.endswith(f"{KEY}=7\n")


def test_a_rerun_never_asks_and_leaves_the_files_alone(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv.write_text(_SETTLED + "\n", encoding="utf-8", newline="\n")
    engine_env = _engine_env(dotenv=dotenv)
    engine_env.parent.mkdir()
    engine_env.write_text(_ENGINE_SETTLED, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdin.isatty", _a_tty)
    monkeypatch.setattr("builtins.input", _never_asked)

    assert init.main() == 0
    assert dotenv.read_text(encoding="utf-8") == _SETTLED + "\n"
    assert engine_env.read_text(encoding="utf-8") == _ENGINE_SETTLED
