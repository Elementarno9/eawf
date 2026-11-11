"""The leak gates honour their own marker, and the secret pragma stays where it is needed.

A line exempt from the path and email gates carries ``eawf: allow-leak``; the
``detect-secrets`` allowlist pragma no longer exempts it, so one comment can
never silence both scans. The pragma itself stays only on lines the secret
scan would flag without it.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from detect_secrets.main import main as detect_secrets_main
from detect_secrets.settings import transient_settings
from typer.testing import CliRunner  # noqa: EAWF024 pins the leak gate's CLI exit status

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import hook as hook_command

_REPO_ROOT = Path(__file__).resolve().parents[4]
_HOOK_REPO = "https://github.com/Yelp/detect-secrets"
#: Spelled in two parts so this module's own source carries no pragma.
_PRAGMA = "pragma: " + "allowlist secret"
_PRAGMA_COMMENT = re.compile(r"[ \t]*#[ \t]*" + re.escape(_PRAGMA) + r"[ \t]*$")
_SCANNED_TREES = ("src", "tests", "tools", "docs")

runner = CliRunner()


def _home_path_line(marker: str) -> str:
    home = "/" + "home" + "/" + "fixture-user" + "/project/notes.txt"
    return f'NOTES = "{home}"  # {marker}\n'


def _scan_args() -> list[str]:
    """Return the detect-secrets hook arguments minus ``--baseline <path>``."""
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    (entry,) = [repo for repo in config["repos"] if repo["repo"] == _HOOK_REPO]
    args = list(entry["hooks"][0]["args"])
    at = args.index("--baseline")
    del args[at : at + 2]
    return args


def _flags(line: str, suffix: str, root: Path, args: list[str]) -> bool:
    """Return whether the secret scan flags *line* alone in a file of *suffix*.

    One line per file, because the scan reports a repeated secret only once per
    file. ``--no-verify`` keeps the scan offline.
    """
    name = f"line{suffix}"
    (root / name).write_text(line + "\n", encoding="utf-8")
    out = io.StringIO()
    with transient_settings({"plugins_used": []}), contextlib.redirect_stdout(out):
        detect_secrets_main(["scan", "--no-verify", *args, name])
    result: dict[str, Any] = json.loads(out.getvalue())
    return bool(result["results"])


def _pragma_lines() -> list[tuple[str, int, str]]:
    """Return every ``(path, line number, line)`` ending in the secret pragma comment."""
    found: list[tuple[str, int, str]] = []
    for tree in _SCANNED_TREES:
        for path in sorted((_REPO_ROOT / tree).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if _PRAGMA not in text:
                continue
            rel = path.relative_to(_REPO_ROOT).as_posix()
            found.extend(
                (rel, number, line)
                for number, line in enumerate(text.splitlines(), start=1)
                if _PRAGMA_COMMENT.search(line)
            )
    return found


def test_allow_leak_marker(tmp_path: Path) -> None:
    assert hook_command._LEAK_ALLOW_MARKER == "eawf: allow-leak"
    pragma_only = tmp_path / "pragma_only.py"
    pragma_only.write_text(_home_path_line(_PRAGMA), encoding="utf-8")
    allowed = tmp_path / "allowed.py"
    allowed.write_text(_home_path_line("eawf: allow-leak"), encoding="utf-8")

    refused = runner.invoke(app, ["hook", "path-leak-lint", str(pragma_only)])
    passed = runner.invoke(app, ["hook", "path-leak-lint", str(allowed)])

    assert refused.exit_code == 1, refused.stdout
    assert passed.exit_code == 0, passed.stdout


def test_pragma_only_where_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    args = _scan_args()
    lines = _pragma_lines()

    unflagged = [
        f"{rel}:{number}"
        for rel, number, line in lines
        if not _flags(_PRAGMA_COMMENT.sub("", line), Path(rel).suffix, tmp_path, args)
    ]

    assert lines, "the keyword fixtures still need the pragma"
    assert unflagged == []
