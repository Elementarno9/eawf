"""Options that were accepted and then ignored are gone, so passing one is refused.

Each of these options parsed, echoed or logged its value and changed nothing the
command did. Removing them lets the parser say so: a caller who passes one gets the
usage error for an unknown option instead of output that silently ignored the request.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()

#: The terminal styling the usage panel carries, which a substring check must look past.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.mark.parametrize(
    ("argv", "option"),
    [
        (["status", "--scope", "P01"], "--scope"),
        (["config", "validate", "--scope", "repo"], "--scope"),
        (["memory", "compact", "--scope", "P01"], "--scope"),
        (["memory", "compact", "--budget", "1024"], "--budget"),
        (["render-output", "--strict"], "--strict"),
        (["hook", "eawf016-title-clarity", "--base", "origin/main"], "--base"),
    ],
)
def test_an_ignored_option_is_refused_as_unknown(
    argv: list[str], option: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An accepting parser would run the command, so keep it off the checkout's tree.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    result = runner.invoke(app, argv)
    assert result.exit_code == 2, result.output
    assert f"No such option: {option}" in _ANSI.sub("", result.output)
