"""``eawf statusline install`` sets one key of one settings file, and project installs write none.

Every test runs with ``HOME`` and the working directory inside ``tmp_path``
and asserts that nothing outside them changes, so no real Claude settings
file is read or written.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the contract is the verb's exit and streams

from eawf.platform.install.wizard import _auto_install_runtime_plugin
from eawf.runtime.runtimes.claude import plugin_conflict, plugin_install, statusline_install
from eawf.runtime.runtimes.claude.plugin_install import install_plugin
from eawf.runtime.runtimes.claude.statusline_install import (
    STATUSLINE_ENTRY,
    SettingsNotWritable,
    install_statusline,
    is_eawf_project_leftover,
    names_eawf,
    project_root,
    read_settings,
    settings_path,
    statusline_line,
)
from eawf.surfaces.cli.app import app

_LINE = '"statusLine": {"type": "command", "command": "eawf cc statusline", "refreshInterval": 30}'
_LEGACY = {"type": "command", "command": "eawf cc statusline"}
_FOREIGN = {"type": "command", "command": "my-own-line"}

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``HOME`` at an empty directory, run from an empty one, restore the home seams."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(work)
    monkeypatch.setattr(
        plugin_install, "detect_marketplace_install", plugin_conflict.detect_marketplace_install
    )
    monkeypatch.setattr(
        plugin_install, "global_settings_path", statusline_install.global_settings_path
    )
    return home_dir


def _user_file(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _tree(root: Path) -> dict[str, bytes]:
    """Return every file under *root* with its bytes, to prove what changed."""
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# --- user scope, key order, mode, rename -------------------------------------


def test_user_scope_write_keeps_order(home: Path, tmp_path: Path) -> None:
    path = _user_file(home)
    _write(path, {"model": "opus", "permissions": {"allow": ["Bash"]}, "theme": "dark"})
    path.chmod(0o640)
    inode_before = path.stat().st_ino
    outside_before = _tree(tmp_path / "work")

    result = runner.invoke(app, ["statusline", "install"])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"statusline installed: {path}\n"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert list(written) == ["model", "permissions", "theme", "statusLine"]
    assert written["statusLine"] == STATUSLINE_ENTRY
    assert written["permissions"] == {"allow": ["Bash"]}
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.stat().st_ino != inode_before  # renamed into place, not written over
    assert sorted(p.name for p in path.parent.iterdir()) == ["settings.json"]
    assert _tree(tmp_path / "work") == outside_before


def test_existing_key_is_replaced_in_place(home: Path) -> None:
    path = _user_file(home)
    _write(path, {"a": 1, "statusLine": _FOREIGN, "z": 2})

    result = runner.invoke(app, ["statusline", "install", "--yes"])

    assert result.exit_code == 0, result.output
    assert list(json.loads(path.read_text(encoding="utf-8"))) == ["a", "statusLine", "z"]


@pytest.mark.parametrize(
    ("scope", "relative"),
    [
        pytest.param("project", ".claude/settings.json", id="project"),
        pytest.param("local", ".claude/settings.local.json", id="local"),
    ],
)
def test_project_scopes_write_the_repo_files(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, relative: str
) -> None:
    repo = tmp_path / "work" / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    monkeypatch.chdir(repo / "sub")

    result = runner.invoke(app, ["statusline", "install", "--scope", scope])

    assert result.exit_code == 0, result.output
    assert read_settings(repo / relative) == {"statusLine": STATUSLINE_ENTRY}
    assert not _user_file(home).exists()


def test_scope_rejects_an_unknown_value(home: Path) -> None:
    result = runner.invoke(app, ["statusline", "install", "--scope", "global"])
    assert result.exit_code == 2
    assert not _user_file(home).exists()


# --- exit status per case ----------------------------------------------------


@pytest.mark.parametrize(
    ("before", "args", "exit_code", "stdout", "stderr", "after"),
    [
        pytest.param(
            {"statusLine": STATUSLINE_ENTRY},
            [],
            0,
            "statusline already installed: {path}\n",
            "",
            "unchanged",
            id="same-value",
        ),
        pytest.param(
            {"statusLine": _FOREIGN},
            [],
            1,
            f"statusLine in {{path}}: {json.dumps(_FOREIGN)}\n",
            "statusline not installed: another entry is set; --yes replaces it\n",
            "unchanged",
            id="another-value",
        ),
        pytest.param(
            {"statusLine": _LEGACY},
            [],
            1,
            f"statusLine in {{path}}: {json.dumps(_LEGACY)}\n",
            "statusline not installed: another entry is set; --yes replaces it\n",
            "unchanged",
            id="legacy-value-is-another-value",
        ),
        pytest.param(
            {"statusLine": _FOREIGN},
            ["--yes"],
            0,
            "statusline installed: {path}\n",
            "",
            "installed",
            id="another-value-yes",
        ),
        pytest.param(None, ["--print"], 0, f"{_LINE}\n", "", "absent", id="print-absent"),
        pytest.param(
            {"statusLine": _FOREIGN},
            ["--print"],
            0,
            f"{_LINE}\n",
            "",
            "unchanged",
            id="print-present",
        ),
        pytest.param(None, [], 0, "statusline installed: {path}\n", "", "installed", id="absent"),
    ],
)
def test_install_exits(
    home: Path,
    before: dict[str, Any] | None,
    args: list[str],
    exit_code: int,
    stdout: str,
    stderr: str,
    after: str,
) -> None:
    path = _user_file(home)
    if before is not None:
        _write(path, before)
    original = path.read_bytes() if path.exists() else None

    result = runner.invoke(app, ["statusline", "install", *args])

    assert result.exit_code == exit_code, result.output
    assert result.stdout == stdout.replace("{path}", str(path))
    assert result.stderr == stderr
    if after == "unchanged":
        assert path.read_bytes() == original
    elif after == "absent":
        assert not path.exists()
    else:
        assert read_settings(path)["statusLine"] == STATUSLINE_ENTRY


@pytest.mark.parametrize(
    ("raw", "mode", "reason"),
    [
        pytest.param('{"model": "opus"}\n', 0o400, "file is read-only (mode 400)", id="mode-400"),
        pytest.param("{ not json", 0o600, "not a valid JSON object", id="unparseable"),
        pytest.param("[1, 2]", 0o600, "not a valid JSON object", id="not-an-object"),
    ],
)
def test_install_exits_6_and_leaves_the_file(home: Path, raw: str, mode: int, reason: str) -> None:
    path = _user_file(home)
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    path.chmod(mode)

    result = runner.invoke(app, ["statusline", "install"])

    assert result.exit_code == 6
    assert result.stdout == f"{_LINE}\n"
    assert result.stderr == (
        f"statusline not installed: {path}: {reason}; add the line above to that file\n"
    )
    assert path.read_text(encoding="utf-8") == raw
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert sorted(p.name for p in path.parent.iterdir()) == ["settings.json"]


# --- library boundaries and error paths --------------------------------------


def test_statusline_line_is_the_settings_line() -> None:
    assert statusline_line() == _LINE
    assert json.loads(f"{{{statusline_line()}}}") == {"statusLine": STATUSLINE_ENTRY}


def test_new_file_gets_parent_and_mode_644(tmp_path: Path) -> None:
    path = tmp_path / "nested" / ".claude" / "settings.json"
    result = install_statusline(path)
    assert result.status == "installed"
    assert result.current is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert path.read_text(encoding="utf-8") == (
        '{\n  "statusLine": {\n    "type": "command",\n    "command": "eawf cc statusline",\n'
        '    "refreshInterval": 30\n  }\n}\n'
    )


def test_rerun_is_byte_stable(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    install_statusline(path)
    first = path.read_bytes()
    assert install_statusline(path).status == "already"
    assert path.read_bytes() == first


def test_blank_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("  \n", encoding="utf-8")
    assert install_statusline(path).status == "installed"
    assert read_settings(path) == {"statusLine": STATUSLINE_ENTRY}


def test_non_ascii_values_stay_unescaped(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"name": "Eä"})
    install_statusline(path)
    assert '"name": "Eä"' in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("indent", "unit"),
    [(4, "    "), ("\t", "\t"), (1, " ")],
)
def test_statusline_repro_install_reindents_the_file(
    tmp_path: Path, indent: int | str, unit: str
) -> None:
    # The file keeps its own indentation, spaces of any width or tabs.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "opus", "env": {"A": "1"}}, indent=indent) + "\n")
    install_statusline(path)
    text = path.read_text(encoding="utf-8")
    assert (
        text
        == json.dumps(
            {"model": "opus", "env": {"A": "1"}, "statusLine": STATUSLINE_ENTRY}, indent=indent
        )
        + "\n"
    )
    assert f'\n{unit}"model": "opus"' in text


@pytest.mark.parametrize("raw", ['{"model": "opus"}', '{"permissions": {"allow": ["a", "b"]}}'])
def test_a_file_without_an_indented_line_gets_two_spaces(tmp_path: Path, raw: str) -> None:
    # boundary: a one-line file has no indentation to keep; inline arrays are laid out.
    path = tmp_path / "settings.json"
    path.write_text(raw, encoding="utf-8")
    install_statusline(path)
    assert path.read_text(encoding="utf-8") == (
        json.dumps({**json.loads(raw), "statusLine": STATUSLINE_ENTRY}, indent=2) + "\n"
    )


def test_symlinked_file_is_written_through_the_link(tmp_path: Path) -> None:
    real = tmp_path / "managed" / "settings.json"
    _write(real, {"model": "opus"})
    link = tmp_path / "settings.json"
    link.symlink_to(real)

    install_statusline(link)

    assert link.is_symlink()
    assert read_settings(real) == {"model": "opus", "statusLine": STATUSLINE_ENTRY}


def test_unwritable_directory_refuses_and_leaves_no_temp(tmp_path: Path) -> None:
    folder = tmp_path / "ro"
    folder.mkdir()
    path = folder / "settings.json"
    _write(path, {"model": "opus"})
    folder.chmod(0o500)
    try:
        if os.access(folder, os.W_OK):
            pytest.skip("the directory stays writable for this user")
        with pytest.raises(SettingsNotWritable) as caught:
            install_statusline(path)
    finally:
        folder.chmod(0o700)
    assert caught.value.path == path
    assert caught.value.reason == "Permission denied"
    assert sorted(p.name for p in folder.iterdir()) == ["settings.json"]
    assert read_settings(path) == {"model": "opus"}


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        pytest.param(STATUSLINE_ENTRY, True, id="current"),
        pytest.param(_LEGACY, True, id="legacy"),
        pytest.param(
            {"type": "command", "command": "/opt/bin/eawf cc statusline"}, True, id="absolute-path"
        ),
        pytest.param(
            {"type": "command", "command": "eawf cc statusline --theme none"},
            False,
            id="extra-args",
        ),
        pytest.param({"type": "command", "command": "eawf cc"}, False, id="too-short"),
        pytest.param({"type": "static", "command": "eawf cc statusline"}, False, id="wrong-type"),
        pytest.param({"type": "command", "command": 'eawf "cc'}, False, id="unbalanced-quote"),
        pytest.param({"type": "command", "command": 7}, False, id="command-not-a-string"),
        pytest.param({}, False, id="empty"),
        pytest.param("eawf cc statusline", False, id="not-a-mapping"),
        pytest.param(None, False, id="absent"),
    ],
)
def test_names_eawf(entry: object, expected: bool) -> None:
    assert names_eawf(entry) is expected


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        pytest.param(STATUSLINE_ENTRY, True, id="current"),
        pytest.param(_LEGACY, True, id="legacy"),
        pytest.param({**_LEGACY, "padding": 1}, False, id="extra-key"),
        pytest.param(
            {"type": "command", "command": "/opt/bin/eawf cc statusline"}, False, id="absolute-path"
        ),
        pytest.param(None, False, id="absent"),
    ],
)
def test_is_eawf_project_leftover(entry: object, expected: bool) -> None:
    assert is_eawf_project_leftover(entry) is expected


def test_project_root_walks_up_to_git_else_cwd(tmp_path: Path) -> None:
    assert project_root(tmp_path) == tmp_path
    (tmp_path / "repo" / "a" / "b").mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    assert project_root(tmp_path / "repo" / "a" / "b") == tmp_path / "repo"


def test_settings_path_per_scope(home: Path, tmp_path: Path) -> None:
    assert settings_path("user", cwd=tmp_path) == home / ".claude" / "settings.json"
    assert settings_path("project", cwd=tmp_path) == tmp_path / ".claude" / "settings.json"
    assert settings_path("local", cwd=tmp_path) == tmp_path / ".claude" / "settings.local.json"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        pytest.param("{ not json", "not valid json", id="invalid-json"),
        pytest.param("[1, 2, 3]", "must be a json object", id="not-an-object"),
    ],
)
def test_read_settings_rejects(tmp_path: Path, raw: str, message: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        read_settings(path)


def test_read_settings_absent_file_is_empty(tmp_path: Path) -> None:
    assert read_settings(tmp_path / "settings.json") == {}


# --- no project write --------------------------------------------------------


def _project_settings(project: Path) -> dict[str, Any]:
    return read_settings(project / ".claude" / "settings.json")


def test_no_project_write(home: Path, tmp_path: Path) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    install_plugin(fresh, extra_tools={})
    assert "statusLine" not in _project_settings(fresh)

    quick = tmp_path / "quick"
    quick.mkdir()
    assert _auto_install_runtime_plugin(target_dir=quick, runtime="claude-code", force=False) == [
        "claude-code"
    ]
    assert (quick / ".claude" / "settings.json").is_file()
    assert "statusLine" not in _project_settings(quick)
    assert not _user_file(home).exists()

    alias = runner.invoke(app, ["cc", "statusline", "install", "--print"])
    assert alias.exit_code == 0, alias.output
    assert alias.stdout == f"{_LINE}\n"
    listed = runner.invoke(app, ["cc", "statusline", "--help"])
    assert listed.exit_code == 0, listed.output
    assert "prewarm" in listed.stdout
    assert "install" not in listed.stdout


@pytest.mark.parametrize(
    ("user", "project_entry", "kept"),
    [
        pytest.param(STATUSLINE_ENTRY, _LEGACY, False, id="user-eawf-legacy-project-removed"),
        pytest.param(_LEGACY, STATUSLINE_ENTRY, False, id="user-legacy-current-project-removed"),
        pytest.param(STATUSLINE_ENTRY, _FOREIGN, True, id="foreign-project-kept"),
        pytest.param(STATUSLINE_ENTRY, {**_LEGACY, "padding": 2}, True, id="edited-project-kept"),
        pytest.param(None, _LEGACY, True, id="user-without-line-keeps-project"),
        pytest.param(_FOREIGN, _LEGACY, True, id="user-foreign-keeps-project"),
    ],
)
def test_project_leftover_removed_only_when_user_names_eawf(
    home: Path,
    tmp_path: Path,
    user: dict[str, Any] | None,
    project_entry: dict[str, Any],
    kept: bool,
) -> None:
    if user is not None:
        _write(_user_file(home), {"statusLine": user})
    project = tmp_path / "project"
    _write(project / ".claude" / "settings.json", {"model": "opus", "statusLine": project_entry})
    user_before = _user_file(home).read_bytes() if user is not None else None

    install_plugin(project, extra_tools={})
    install_plugin(project, extra_tools={})

    settings = _project_settings(project)
    assert settings.get("statusLine") == (project_entry if kept else None)
    assert settings["model"] == "opus"
    if user_before is not None:
        assert _user_file(home).read_bytes() == user_before
