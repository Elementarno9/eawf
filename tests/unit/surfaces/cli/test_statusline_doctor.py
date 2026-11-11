"""``eawf statusline doctor`` names the winning statusLine entry and one verdict for it.

``HOME``, the managed settings file, ``PATH`` and the working directory all
sit under ``tmp_path``; the render probe is replaced, so no process starts
and no real settings file is read.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the contract is the verb's output and exit

from eawf.observability.doctor import checks
from eawf.runtime.runtimes.claude import statusline_install
from eawf.runtime.runtimes.claude.statusline_install import (
    STATUSLINE_ENTRY,
    _probe_render,
    _RenderProbe,
    diagnose_statusline,
)
from eawf.surfaces.cli.app import app

_FIELDS = [
    "layer",
    "command",
    "shadowed_by",
    "resolves",
    "version",
    "render_ms",
    "budget_ms",
    "refresh_interval",
    "disable_all_hooks",
    "verdict",
    "fix",
]
_FOREIGN = {"type": "command", "command": "my-own-line"}

runner = CliRunner()


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Return a tmp home, a linked-worktree checkout as cwd, a managed file and a bin with eawf."""
    home = tmp_path / "home"
    home.mkdir()
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../main/.git/worktrees/w\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    eawf = bin_dir / "eawf"
    eawf.write_text("#!/bin/sh\n", encoding="utf-8")
    eawf.chmod(0o755)
    managed = tmp_path / "etc" / "managed-settings.json"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.chdir(worktree / "src")
    monkeypatch.setattr(statusline_install, "_managed_settings_path", lambda: managed)
    monkeypatch.setattr(
        statusline_install,
        "_probe_render",
        lambda executable, argv, cwd: _RenderProbe("0.7.2", 41, None),
    )
    return {"home": home, "worktree": worktree, "managed": managed, "eawf": eawf}


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _user(box: dict[str, Path]) -> Path:
    return box["home"] / ".claude" / "settings.json"


def _project(box: dict[str, Path], name: str = "settings.json") -> Path:
    return box["worktree"] / ".claude" / name


def _setup(box: dict[str, Path], case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lay out the settings files of verdict fixture *case*."""
    if case != "missing":
        _write(_user(box), {"statusLine": STATUSLINE_ENTRY})
    if case == "shadowed-project":
        _write(_project(box), {"statusLine": _FOREIGN})
    if case == "shadowed-hooks-off":
        _write(_project(box, "settings.local.json"), {"disableAllHooks": True})
    if case == "managed-wins":
        _write(box["managed"], {"statusLine": STATUSLINE_ENTRY})
    if case == "broken-path":
        box["eawf"].unlink()
    if case == "broken-render":
        monkeypatch.setattr(
            statusline_install,
            "_probe_render",
            lambda executable, argv, cwd: _RenderProbe("0.7.2", 12, "exit 1: Traceback"),
        )


_EXPECTED: dict[str, tuple[int, dict[str, Any]]] = {
    "ok": (0, {"layer": "user", "verdict": "ok", "shadowed_by": None, "fix": None}),
    "managed-wins": (0, {"layer": "managed", "verdict": "ok"}),
    "missing": (
        0,
        {
            "layer": "none",
            "command": None,
            "verdict": "missing",
            "resolves": None,
            "fix": "run eawf statusline install",
        },
    ),
    "shadowed-project": (
        0,
        {
            "layer": "user",
            "command": "my-own-line",
            "shadowed_by": "project",
            "verdict": "shadowed",
            "render_ms": None,
            "refresh_interval": None,
        },
    ),
    "shadowed-hooks-off": (
        0,
        {
            "layer": "user",
            "shadowed_by": "local:disableAllHooks",
            "disable_all_hooks": True,
            "verdict": "shadowed",
        },
    ),
    "broken-path": (
        1,
        {
            "layer": "user",
            "resolves": None,
            "verdict": "broken",
            "fix": "eawf is not on PATH; install eawf or name it by absolute path",
        },
    ),
    "broken-render": (
        1,
        {
            "layer": "user",
            "render_ms": 12,
            "verdict": "broken",
            "fix": "eawf cc statusline failed: exit 1: Traceback",
        },
    ),
}


@pytest.mark.parametrize("case", list(_EXPECTED))
def test_doctor_fields_and_verdicts(
    box: dict[str, Path], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _setup(box, case, monkeypatch)
    user_before = _user(box).read_bytes() if _user(box).exists() else None
    exit_code, expected = _EXPECTED[case]

    shown = runner.invoke(app, ["statusline", "doctor", "--json"])
    text = runner.invoke(app, ["statusline", "doctor"])

    assert shown.exit_code == exit_code, shown.output
    assert text.exit_code == exit_code, text.output
    report = json.loads(shown.stdout)
    assert list(report) == _FIELDS
    assert {key: report[key] for key in expected} == expected
    assert report["budget_ms"] == 300
    assert [line.split(": ", 1)[0] for line in text.stdout.splitlines()] == _FIELDS
    assert f"verdict: {expected['verdict']}" in text.stdout.splitlines()
    assert (_user(box).read_bytes() if _user(box).exists() else None) == user_before
    if case == "ok":
        assert report["command"] == "eawf cc statusline"
        assert report["resolves"] == str(box["eawf"])
        assert report["version"] == "0.7.2"
        assert report["render_ms"] == 41
        assert report["refresh_interval"] == 30
        assert report["disable_all_hooks"] is False
        assert "shadowed_by: -" in text.stdout.splitlines()
        assert "disable_all_hooks: false" in text.stdout.splitlines()


def test_missing_fix_names_the_hand_edit_when_the_user_file_is_read_only(
    box: dict[str, Path],
) -> None:
    _write(_user(box), {"model": "opus"})
    _user(box).chmod(0o400)

    report = diagnose_statusline(cwd=Path.cwd(), render=False)

    assert report.verdict == "missing"
    assert report.fix == (
        f"add {statusline_install.statusline_line()} to {_user(box)} (file is read-only (mode 400))"
    )
    assert stat.S_IMODE(_user(box).stat().st_mode) == 0o400


def test_foreign_user_entry_is_shadowed_with_the_replace_fix(box: dict[str, Path]) -> None:
    _write(_user(box), {"statusLine": _FOREIGN})
    report = diagnose_statusline(cwd=Path.cwd(), render=False)
    assert (report.layer, report.verdict, report.shadowed_by) == ("none", "shadowed", "user")
    assert report.fix == "run eawf statusline install --yes to replace it"


def test_statusline_repro_doctor_fix_for_a_missing_absolute_path(box: dict[str, Path]) -> None:
    # An absolute command path is not looked up on PATH, so the fix says what is wrong.
    gone = box["home"] / "venv" / "bin" / "eawf"
    _write(_user(box), {"statusLine": {**STATUSLINE_ENTRY, "command": f"{gone} cc statusline"}})
    report = diagnose_statusline(cwd=Path.cwd(), render=False)
    assert report.verdict == "broken"
    assert report.fix == (
        f"{gone} does not exist or is not executable; reinstall eawf "
        "or run eawf statusline install --yes to name the one on PATH"
    )


def test_unreadable_layer_counts_as_unset(box: dict[str, Path]) -> None:
    _write(_user(box), {"statusLine": STATUSLINE_ENTRY})
    _project(box).parent.mkdir(parents=True)
    _project(box).write_text("{ not json", encoding="utf-8")
    assert diagnose_statusline(cwd=Path.cwd(), render=False).verdict == "ok"


def test_without_render_the_probe_does_not_run(
    box: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(_user(box), {"statusLine": STATUSLINE_ENTRY})

    def refuse(*_args: object) -> _RenderProbe:
        raise AssertionError("render probe ran")

    monkeypatch.setattr(statusline_install, "_probe_render", refuse)
    report = diagnose_statusline(cwd=Path.cwd(), render=False)
    assert (report.verdict, report.version, report.render_ms) == ("ok", None, None)


@pytest.mark.parametrize(
    ("verdict_case", "status"),
    [
        pytest.param("ok", "ok", id="ok"),
        pytest.param("missing", "warn", id="missing"),
        pytest.param("shadowed-project", "warn", id="shadowed"),
        pytest.param("broken-path", "fail", id="broken"),
    ],
)
def test_doctor_check_claude_statusline(
    box: dict[str, Path], monkeypatch: pytest.MonkeyPatch, verdict_case: str, status: str
) -> None:
    _setup(box, verdict_case, monkeypatch)
    result = checks.check_claude_statusline(workspace=box["worktree"])
    assert result.name == "claude.statusline"
    assert result.status == status
    assert result.detail is not None
    assert result.detail.startswith(f"verdict {_EXPECTED[verdict_case][1]['verdict']}, layer ")


# --- the render probe, with the process runner replaced -----------------------


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize(
    ("rendered", "error"),
    [
        pytest.param(_completed(0, "line\n"), None, id="clean"),
        pytest.param(_completed(2, "", "boom\nmore\n"), "exit 2: boom", id="non-zero"),
        pytest.param(_completed(0, "line\n", "warning\n"), "stderr: warning", id="stderr"),
    ],
)
def test_probe_render_judges_exit_and_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rendered: SimpleNamespace, error: str | None
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append({"argv": argv, **kwargs})
        return _completed(0, "0.7.2\n") if argv[-1] == "--version" else rendered

    monkeypatch.setattr(statusline_install.subprocess, "run", fake_run)
    probe = _probe_render("/x/eawf", ["eawf", "cc", "statusline"], tmp_path)

    assert probe.error == error
    assert probe.version == "0.7.2"
    assert probe.render_ms is not None
    assert calls[1]["argv"] == ["/x/eawf", "cc", "statusline"]
    assert "session_id" not in json.loads(calls[1]["input"])  # no cache entry is written
    assert calls[1]["cwd"] == tmp_path


def test_probe_render_reports_a_timeout_and_a_spawn_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def hang(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        raise statusline_install.subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(statusline_install.subprocess, "run", hang)
    assert _probe_render("/x/eawf", ["eawf"], tmp_path) == _RenderProbe(
        None, None, "timed out after 10 s"
    )

    def missing(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(statusline_install.subprocess, "run", missing)
    assert _probe_render("/x/eawf", ["eawf"], tmp_path) == _RenderProbe(
        None, None, "No such file or directory"
    )
