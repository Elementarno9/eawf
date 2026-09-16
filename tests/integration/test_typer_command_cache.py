"""In-process CLI invocations reuse one Click tree per Typer app.

``tests/conftest.py`` installs ``typer_command_cache`` so ``CliRunner.invoke``
stops reconverting the app on every call. Under the ``sysmon`` coverage core
each conversion left its evaluated annotations alive for the life of the
worker, and a full parallel ``--cov`` run outgrew the CI runner. These tests
pin what makes the cache safe to rely on: repeated invocations convert once,
a changed registration converts again, and two apps never share a tree.
"""

from __future__ import annotations

from typing import Any

import pytest
import typer
import typer.main
from typer.testing import CliRunner


def _two_command_app() -> typer.Typer:
    """Return a fresh app with two commands, so typer builds a group."""
    app = typer.Typer()

    @app.command()
    def hello() -> None:
        typer.echo("hello")

    @app.command()
    def bye() -> None:
        typer.echo("bye")

    return app


def _count_group_builds(monkeypatch: pytest.MonkeyPatch) -> list[typer.Typer]:
    """Record every Typer-to-Click group conversion for the rest of the test.

    Args:
        monkeypatch: Fixture used to wrap typer's group builder.

    Returns:
        The apps converted, one entry per conversion.
    """
    builds: list[typer.Typer] = []
    real = typer.main.get_group

    def _counting(app: typer.Typer) -> Any:
        builds.append(app)
        return real(app)

    monkeypatch.setattr(typer.main, "get_group", _counting)
    return builds


def test_repeated_invocations_convert_the_app_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three invokes of one app build its tree once and still run each time."""
    builds = _count_group_builds(monkeypatch)
    app = _two_command_app()
    runner = CliRunner()

    results = [runner.invoke(app, ["hello"]) for _ in range(3)]

    assert [result.exit_code for result in results] == [0, 0, 0]
    assert [result.output for result in results] == ["hello\n"] * 3
    assert builds == [app]


def test_a_new_registration_is_converted_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A command registered after the first invoke is reachable on the next."""
    builds = _count_group_builds(monkeypatch)
    app = _two_command_app()
    runner = CliRunner()
    assert runner.invoke(app, ["hello"]).exit_code == 0

    @app.command()
    def later() -> None:
        typer.echo("later")

    result = runner.invoke(app, ["later"])

    assert result.exit_code == 0
    assert result.output == "later\n"
    assert builds == [app, app]


def test_two_apps_never_share_a_tree() -> None:
    """A command added to one app is unknown to an otherwise identical app."""
    first = _two_command_app()
    second = _two_command_app()

    @second.command()
    def extra() -> None:
        typer.echo("extra")

    runner = CliRunner()

    assert runner.invoke(second, ["extra"]).output == "extra\n"
    assert runner.invoke(first, ["extra"]).exit_code != 0


def test_a_single_command_app_runs_through_the_cache() -> None:
    """The one-command shape, which typer converts without a group, still runs."""
    app = typer.Typer()

    @app.command()
    def only(name: str) -> None:
        typer.echo(f"hi {name}")

    runner = CliRunner()

    assert [runner.invoke(app, ["ada"]).output for _ in range(2)] == ["hi ada\n"] * 2
