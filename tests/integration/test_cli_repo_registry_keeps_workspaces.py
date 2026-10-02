"""Every ``eawf repo`` registry write keeps the registered workspaces.

Each verb rebuilds the registry before persisting it; a rebuild that
names only the repo fields drops every workspace on disk. The
integration conftest forces ``EAWF_DAEMONLESS=1``, so these runs
exercise the in-process write arm.
"""

from __future__ import annotations

import json
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    """Register ABC (live) and DEF (path missing) plus workspace GHI."""
    parent = tmp_path / "Repos"
    abc = parent / "abc"
    (abc / ".ea").mkdir(parents=True)
    (abc / ".ea" / "state.json").write_text(json.dumps({"project": {"code": "ABC"}}))
    target = tmp_path / "registry.json"
    target.write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "repos": {
                    "ABC": {"code": "ABC", "path": str(abc.resolve())},
                    "DEF": {"code": "DEF", "path": str(tmp_path / "gone")},
                },
                "workspaces": {
                    "GHI": {
                        "key": "GHI",
                        "member_project_codes": ["ABC", "DEF"],
                        "home_project_code": "ABC",
                        "revision": 1,
                    }
                },
            }
        )
    )
    return abc, target


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "Repos" / "jkl"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text(json.dumps({"project": {"code": "JKL"}}))
    return repo


@pytest.mark.parametrize("verb", ["add", "register", "readd-active", "remove", "prune"])
def test_repo_verb_keeps_registered_workspaces(tmp_path: Path, verb: str) -> None:
    abc, target = _seed(tmp_path)
    if verb in {"add", "register"}:
        args = ["repo", verb, str(_new_repo(tmp_path)), "--yes"]
    elif verb == "readd-active":
        args = ["repo", "add", str(abc), "--set-active", "--yes"]
    elif verb == "remove":
        args = ["repo", "remove", "DEF"]
    else:
        args = ["repo", "prune", "--yes"]
    result = runner.invoke(app, [*args, "--registry-path", str(target)])
    assert result.exit_code == 0, result.output
    payload = json.loads(target.read_text())
    assert payload["workspaces"]["GHI"]["home_project_code"] == "ABC"
