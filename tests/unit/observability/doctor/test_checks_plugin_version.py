"""The installed Claude Code and Codex eawf plugins are held to the package version.

Sessions loaded skill text from plugins left at earlier releases while the
package had moved on, and nothing said so. ``eawf doctor`` fails on such an
install and the session-start hook prints one line naming it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the doctor exit code is the contract

from eawf import __version__
from eawf.observability.doctor import checks_plugin_version
from eawf.observability.doctor.checks_plugin_version import (
    CHECK_NAME,
    MISMATCH_PREFIX,
    check_plugin_version,
    plugin_version_mismatch_line,
)
from eawf.platform.install.instrument_probe import PROBE_VERSION, ProbeReport, ProbeResult
from eawf.surfaces.cli.app import app

#: A release every installed plugin in these tests is older than.
OLDER: Final = "0.0.1rc2"

#: The installer's ``codex plugin list`` reader, which the doctor check asks.
_CODEX_LIST: Final = "eawf.runtime.runtimes.codex.plugin_install._marketplace_plugin_root"


def _codex_cache(home: Path) -> Path:
    return home / ".codex" / "plugins" / "cache" / "eawf" / "eawf"


def _codex_lists_the_valid_cache(home: Path | None) -> Path | None:
    """Stand in for ``codex plugin list``: enable the one cached install whose manifest parses."""
    assert home is not None
    cache = _codex_cache(home)
    roots = sorted(cache.iterdir()) if cache.is_dir() else []
    valid = [root for root in roots if _manifest_version(root) == root.name]
    return valid[-1] if valid else None


def _manifest_version(root: Path) -> object:
    manifest = checks_plugin_version._read_json(root / ".codex-plugin" / "plugin.json")
    return manifest.get("version") if isinstance(manifest, dict) else None


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty home the registries are read under, and a Codex that lists its one install."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(checks_plugin_version, "_registry_home", lambda: root)
    monkeypatch.setattr(_CODEX_LIST, _codex_lists_the_valid_cache)
    return root


def _claude_registry(home: Path) -> Path:
    return home / ".claude" / "plugins" / "installed_plugins.json"


def _install_claude(home: Path, version: str, *, project: Path | None = None) -> None:
    record: dict[str, str] = {"scope": "user", "installPath": "cache", "version": version}
    if project is not None:
        record |= {"scope": "project", "projectPath": str(project)}
    path = _claude_registry(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"version": 2, "plugins": {"eawf@eawf": [record], "other@x": [{"version": "9"}]}}
    path.write_text(json.dumps(document), encoding="utf-8")


def _codex_manifest(home: Path, directory: str) -> Path:
    return _codex_cache(home) / directory / ".codex-plugin"


def _install_codex(home: Path, version: str, *, written_at: float | None = None) -> None:
    manifest = _codex_manifest(home, version)
    manifest.mkdir(parents=True)
    path = manifest / "plugin.json"
    path.write_text(json.dumps({"name": "eawf", "version": version}), encoding="utf-8")
    if written_at is not None:
        os.utime(path, (written_at, written_at))


def _install(home: Path, runtime: str, version: str) -> None:
    (_install_claude if runtime == "claude" else _install_codex)(home, version)


#: The user ``config.toml`` table Codex records the eawf plugin's enablement in.
_ENABLED_CONFIG: Final = b'[plugins."eawf@eawf"]\nenabled = true\n'


def _write_codex_config(home: Path, body: bytes) -> None:
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


# ---- CR-705: the doctor check ------------------------------------------------


def _green_probe(*_args: object, **_kwargs: object) -> ProbeReport:
    return ProbeReport(
        probe_version=PROBE_VERSION,
        profile_ids=["core"],
        results=[ProbeResult(name="git", kind="hard", status="ok", path="/x/git")],
    )


def _doctor(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict[str, str]]:
    """Run ``eawf doctor --json`` on *workspace*; return its exit code and plugin row."""
    from eawf.runtime.daemon.service_install import SupervisedAgentReport

    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.detect_supervised_agent",
        lambda *_a, **_k: SupervisedAgentReport(
            supervisor="none",
            label="",
            installed=False,
            loaded=False,
            program=None,
            drift=False,
            rival_pid=None,
        ),
    )
    monkeypatch.setattr(
        "eawf.kernel.config.layered.global_config_path", lambda: workspace / "global.yaml"
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.checks._probe_running_daemon_version", lambda: None
    )
    monkeypatch.setattr("eawf.observability.doctor.daemon_strays.find_stray_daemons", lambda: [])
    result = CliRunner().invoke(app, ["--json", "-w", str(workspace), "doctor"])
    rows = json.loads(result.stdout)["checks"]
    row = next(row for row in rows if row["name"] == CHECK_NAME)
    return result.exit_code, row


@pytest.mark.parametrize(
    ("installs", "status", "named"),
    [
        pytest.param([], "ok", "skipped: no eawf plugin installed", id="absent"),
        pytest.param([("claude", __version__)], "ok", __version__, id="claude-equal"),
        pytest.param([("codex", __version__)], "ok", __version__, id="codex-equal"),
        pytest.param([("claude", OLDER)], "fail", f"claude plugin {OLDER}", id="claude-older"),
        pytest.param([("codex", OLDER)], "fail", f"codex plugin {OLDER}", id="codex-older"),
        pytest.param(
            [("claude", __version__), ("codex", OLDER)],
            "fail",
            f"codex plugin {OLDER}",
            id="one-of-two-older",
        ),
    ],
)
def test_doctor_check(
    installs: list[tuple[str, str]],
    status: str,
    named: str,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for runtime, version in installs:
        _install(home, runtime, version)
    workspace = tmp_path / "repo"
    (workspace / ".ea").mkdir(parents=True)

    result = check_plugin_version(workspace=workspace)
    exit_code, row = _doctor(workspace, monkeypatch)

    assert result.status == status
    assert named in (result.detail or "")
    assert row["status"] == status
    if status == "fail":
        assert f"eawf package {__version__}" in (result.detail or "")
        assert exit_code != 0
    else:
        assert "eawf package" not in (result.detail or "")
        assert exit_code == 0


@pytest.mark.parametrize(
    "spelling",
    [
        f"v{__version__}",
        f" {__version__} ",
        f"{__version__}.0",
        __version__.upper(),
    ],
)
def test_doctor_check_passes_a_pep440_equivalent_spelling(spelling: str, home: Path) -> None:
    _install_claude(home, spelling)
    assert check_plugin_version(workspace=None).status == "ok"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("0.7.0rc2", "0.7.0-RC.2"),
        ("0.7.0rc0", "0.7.0c"),
        ("0.7.0a1", "0.7.0alpha1"),
        ("0.7.0b1", "0.7.0_beta_1"),
        ("0.7.0rc1", "0.7.0preview1"),
        ("0.7.0.dev0", "0.7.0-dev"),
        ("0.7.0.post1", "0.7.0-1"),
        ("0.7.0.post2", "0.7.0rev2"),
        ("0.7", "0.7.0.0"),
        ("0!0.7.0", "0.7.0"),
        ("0.07.00", "0.7"),
        ("0.7.0+Local-1", "0.7.0+local.1"),
    ],
)
def test_normalized_equates_pep440_spellings(left: str, right: str) -> None:
    assert checks_plugin_version._normalized(left) == checks_plugin_version._normalized(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("0.7.0", "0.7.0rc2"),
        ("0.7.0rc1", "0.7.0rc2"),
        ("0.7.0", "0.7.1"),
        ("0.7.0", "1!0.7.0"),
        ("0.7.0", ""),
        ("0.7.0", "not-a-version"),
    ],
)
def test_normalized_separates_distinct_versions(left: str, right: str) -> None:
    assert checks_plugin_version._normalized(left) != checks_plugin_version._normalized(right)


def test_an_unparseable_installed_version_fails_naming_it(home: Path) -> None:
    _install_codex(home, "nightly")
    result = check_plugin_version(workspace=None)
    assert result.status == "fail"
    assert "codex plugin nightly" in (result.detail or "")


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{not json",
        b"\xff\xfe",
        b"[]",
        b'{"plugins": []}',
        b'{"plugins": {"eawf@eawf": {"version": "0.0.1"}}}',
        b'{"plugins": {"eawf@eawf": ["0.0.1", {"version": 1}, {}]}}',
        b'{"plugins": {"eawf-extra@eawf": [{"version": "0.0.1"}]}}',
    ],
    ids=[
        "empty",
        "malformed",
        "not-utf8",
        "not-object",
        "plugins-not-mapping",
        "records-not-list",
        "records-without-version",
        "other-plugin",
    ],
)
def test_an_unreadable_or_foreign_claude_registry_counts_as_no_install(
    body: bytes, home: Path
) -> None:
    path = _claude_registry(home)
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    result = check_plugin_version(workspace=None)
    assert (result.status, result.detail) == ("ok", "skipped: no eawf plugin installed")


@pytest.mark.parametrize(
    "manifest",
    [None, b"{not json", b'{"name": "eawf"}', b'{"version": 7}'],
    ids=["missing", "malformed", "no-version", "version-not-string"],
)
def test_an_unreadable_codex_manifest_counts_as_no_install(
    manifest: bytes | None, home: Path
) -> None:
    directory = _codex_manifest(home, "0.0.1")
    directory.mkdir(parents=True)
    if manifest is not None:
        (directory / "plugin.json").write_bytes(manifest)
    assert check_plugin_version(workspace=None).detail == "skipped: no eawf plugin installed"


def test_a_project_install_counts_only_in_its_own_project(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _install_claude(home, OLDER, project=project)

    assert check_plugin_version(workspace=project).status == "fail"
    assert check_plugin_version(workspace=tmp_path / "elsewhere").status == "ok"
    assert check_plugin_version(workspace=None).status == "ok"


def _codex_enables(version: str | None) -> Callable[[Path | None], Path | None]:
    """Stand in for ``codex plugin list`` enabling *version*, or nothing."""

    def _list(home: Path | None) -> Path | None:
        assert home is not None
        return None if version is None else _codex_cache(home) / version

    return _list


@pytest.mark.parametrize(
    ("enabled", "status"),
    [
        pytest.param(__version__, "ok", id="current-enabled"),
        pytest.param(OLDER, "fail", id="older-enabled"),
        pytest.param(None, "ok", id="none-enabled"),
    ],
)
def test_doctor_counts_only_the_codex_install_codex_enables(
    enabled: str | None, status: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_codex(home, OLDER)
    _install_codex(home, __version__)
    monkeypatch.setattr(_CODEX_LIST, _codex_enables(enabled))

    result = check_plugin_version(workspace=None)

    assert result.status == status
    if enabled is None:
        assert result.detail == "skipped: no eawf plugin installed"
    else:
        assert (f"codex plugin {OLDER}" in (result.detail or "")) is (enabled == OLDER)


def test_doctor_falls_back_to_the_last_install_when_codex_answers_badly(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.runtime.runtimes.codex.plugin_install import IntegrityViolation

    def _malformed(_home: Path | None) -> Path | None:
        raise IntegrityViolation("invalid Codex plugin inventory")

    _install_codex(home, __version__, written_at=1_000)
    _install_codex(home, OLDER, written_at=2_000)
    monkeypatch.setattr(_CODEX_LIST, _malformed)

    result = check_plugin_version(workspace=None)

    assert result.status == "fail"
    assert result.detail is not None
    assert result.detail.startswith(f"codex plugin {OLDER} installed")


def test_doctor_without_codex_counts_no_codex_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.runtime.runtimes.codex import plugin_install

    def _no_codex(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("codex")

    # No ``home`` fixture: the installer's own reader runs, with no codex on PATH.
    home = tmp_path / "home"
    _install_codex(home, OLDER)
    monkeypatch.setattr(checks_plugin_version, "_registry_home", lambda: home)
    monkeypatch.setattr(plugin_install.subprocess, "run", _no_codex)

    assert check_plugin_version(workspace=None).detail == "skipped: no eawf plugin installed"


# ---- CR-706: the session-start line ------------------------------------------


class _RefusedDaemonClient:
    """Stands in for the daemon client; the session's daemon is not running."""

    def __init__(self) -> None:
        raise ConnectionRefusedError("no daemon in this test")


def _session_start_stdout(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Dispatch a Claude SessionStart through ``eawf hook run``; return its stdout."""
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _RefusedDaemonClient)
    payload = {"hook_event_name": "SessionStart", "session_id": "s-1", "source": "startup"}
    result = CliRunner().invoke(
        app,
        ["-w", str(workspace), "hook", "run", "session_start", "--runtime", "claude"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    return result.stdout


@pytest.mark.parametrize(
    ("runtime", "version", "mismatched"),
    [
        pytest.param("claude", OLDER, True, id="claude-older"),
        pytest.param("codex", OLDER, True, id="codex-older"),
        pytest.param("claude", __version__, False, id="claude-equal"),
        pytest.param("codex", __version__, False, id="codex-equal"),
    ],
)
def test_session_start_line(
    runtime: str,
    version: str,
    mismatched: bool,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(home, runtime, version)
    if runtime == "codex":
        _write_codex_config(home, _ENABLED_CONFIG)
    workspace = tmp_path / "repo"
    workspace.mkdir()

    stdout = _session_start_stdout(workspace, monkeypatch)

    context = "\n".join(
        json.loads(line)["hookSpecificOutput"]["additionalContext"]
        for line in stdout.splitlines()
        if line.strip()
    )
    lines = [line for line in context.splitlines() if line.startswith(MISMATCH_PREFIX)]
    if mismatched:
        assert len(lines) == 1
        assert f"{runtime} plugin {OLDER}" in lines[0]
        assert f"eawf package {__version__}" in lines[0]
    else:
        assert lines == []


def test_mismatch_line_is_none_without_an_install(home: Path) -> None:
    assert plugin_version_mismatch_line(workspace=None) is None


@pytest.mark.parametrize(
    ("older_at", "current_at", "mismatched"),
    [
        pytest.param(1_000, 2_000, False, id="current-installed-last"),
        pytest.param(2_000, 1_000, True, id="older-installed-last"),
    ],
)
def test_session_start_line_takes_the_codex_version_installed_last(
    older_at: float,
    current_at: float,
    mismatched: bool,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_subprocess(_home: Path | None) -> Path | None:
        raise AssertionError("the session-start line must not ask codex")

    _install_codex(home, OLDER, written_at=older_at)
    _install_codex(home, __version__, written_at=current_at)
    _write_codex_config(home, _ENABLED_CONFIG)
    monkeypatch.setattr(_CODEX_LIST, _no_subprocess)

    line = plugin_version_mismatch_line(workspace=None)

    if mismatched:
        assert line == (
            f"{MISMATCH_PREFIX} codex plugin {OLDER} installed, eawf package {__version__}; "
            "update the plugin to match"
        )
    else:
        assert line is None

    asked: list[Path | None] = []

    def _record(listed_home: Path | None) -> Path | None:
        asked.append(listed_home)
        return None

    monkeypatch.setattr(_CODEX_LIST, _record)
    check_plugin_version(workspace=None)
    assert asked == [home]


@pytest.mark.parametrize(
    ("config", "mismatched"),
    [
        pytest.param(None, False, id="config-absent"),
        pytest.param(b'model = "x"\n', False, id="table-absent"),
        pytest.param(b'[plugins."other@eawf"]\nenabled = true\n', False, id="other-plugin"),
        pytest.param(b'[plugins."eawf@eawf"]\n', False, id="enabled-missing"),
        pytest.param(b'[plugins."eawf@eawf"]\nenabled = false\n', False, id="disabled"),
        pytest.param(b'[plugins."eawf@eawf"]\nenabled = "true"\n', False, id="enabled-not-bool"),
        pytest.param(b'plugins = "eawf@eawf"\n', False, id="plugins-not-table"),
        pytest.param(b'[plugins]\n"eawf@eawf" = true\n', False, id="entry-not-table"),
        pytest.param(b"[plugins.\n", False, id="malformed"),
        pytest.param(b"\xff\xfe", False, id="not-utf8"),
        pytest.param(_ENABLED_CONFIG, True, id="enabled"),
    ],
)
def test_session_start_line_counts_codex_only_when_enabled(
    config: bytes | None,
    mismatched: bool,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_subprocess(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the session-start line must not start a subprocess")

    _install_codex(home, OLDER)
    if config is not None:
        _write_codex_config(home, config)
    monkeypatch.setattr(_CODEX_LIST, _no_subprocess)
    monkeypatch.setattr("subprocess.run", _no_subprocess)
    monkeypatch.setattr("subprocess.Popen", _no_subprocess)

    line = plugin_version_mismatch_line(workspace=None)

    if mismatched:
        assert line == (
            f"{MISMATCH_PREFIX} codex plugin {OLDER} installed, eawf package {__version__}; "
            "update the plugin to match"
        )
    else:
        assert line is None
