"""Tests for the in-memory helpers in :mod:`eawf.surfaces.cli.commands.status`.

Exercises the git-info path with a stub :class:`subprocess.run` so no real
``git`` is invoked, the plain render of the native payload, and the
authority block the command reports for each tree shape. The status command
itself is integration-tested in ``tests/integration/test_cli_status.py``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from eawf.kernel.migration.epoch2.canary import (
    CanaryDeclaration,
    DisposableTarget,
    declaration_path,
)
from eawf.kernel.migration.epoch2.generation import atomic_write_json, write_marker
from eawf.surfaces.cli.commands import status as status_mod

_DT = datetime(2026, 5, 8, tzinfo=UTC)
_GENERATION_ID = "gen-0123456789abcdef"


# ---- _git_info ------------------------------------------------------------


def test_git_info_collapses_to_none_when_subprocess_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every git invocation hits FileNotFoundError → all fields None."""

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    info = status_mod._git_info(cwd=tmp_path)
    assert info == {"head": None, "branch": None, "dirty": None}


def test_git_info_returns_clean_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = iter(
        [
            _stub_proc("abcdef0123456789abcdef0123456789abcdef01"),  # pragma: allowlist secret
            _stub_proc("main"),
            _stub_proc(""),  # empty porcelain → not dirty
        ]
    )

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        return next(sequence)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    info = status_mod._git_info(cwd=tmp_path)
    assert info == {
        "head": "abcdef0123456789abcdef0123456789abcdef01",  # pragma: allowlist secret
        "branch": "main",
        "dirty": False,
    }


def test_git_info_marks_dirty_when_porcelain_nonempty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = iter(
        [
            _stub_proc("abcd"),
            _stub_proc("feature/x"),
            _stub_proc(" M src/foo.py"),
        ]
    )

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        return next(sequence)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    info = status_mod._git_info(cwd=tmp_path)
    assert info["dirty"] is True


def test_git_info_handles_called_process_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rev-parse HEAD fails on a fresh repo with no commits — head must be None."""

    sequence = iter(
        [
            subprocess.CalledProcessError(returncode=128, cmd=["git", "rev-parse", "HEAD"]),
            _stub_proc("main"),
            _stub_proc(""),
        ]
    )

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        nxt = next(sequence)
        if isinstance(nxt, subprocess.CalledProcessError):
            raise nxt
        return nxt

    monkeypatch.setattr(subprocess, "run", _fake_run)
    info = status_mod._git_info(cwd=tmp_path)
    assert info["head"] is None
    assert info["branch"] == "main"
    assert info["dirty"] is False


def _stub_proc(stdout: str) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout + "\n"
    proc.returncode = 0
    return proc


# ---- _format_text ---------------------------------------------------------


def _payload(**overrides: Any) -> dict[str, Any]:
    """Return an epoch-2 status payload with no native records, overridden by key."""
    payload: dict[str, Any] = {
        "git": {"head": "abcdef0123456789", "branch": "main", "dirty": False},
        "authority": {"epoch": 2, "gap": None, "generation_id": _GENERATION_ID},
        "native": {
            "track_key": None,
            "milestone_key": None,
            "open_task_count": 0,
            "open_tasks": [],
        },
        "recent_decisions": [],
        "blockers": [],
    }
    payload.update(overrides)
    return payload


def test_format_text_renders_the_native_pointers_and_tasks() -> None:
    native = {
        "track_key": "TRK-0001",
        "milestone_key": "MLS-0002",
        "open_task_count": 2,
        "open_tasks": [
            {"key": "QR-0001", "status": "DRAFT", "title": None},
            {"key": "QR-0002", "status": "READY", "title": "t"},
        ],
    }
    text = status_mod._format_text(_payload(native=native))
    assert text.splitlines()[:2] == [
        "track: TRK-0001 milestone: MLS-0002",
        "open tasks: 2 (QR-0001 DRAFT, QR-0002 READY)",
    ]
    assert "git: head=abcdef012345 branch=main" in text
    assert "blockers: none" in text


def test_format_text_renders_absent_pointers_and_no_tasks() -> None:
    text = status_mod._format_text(_payload())
    assert "track: <none> milestone: <none>" in text
    assert "open tasks: none" in text
    assert "recent decisions: none" in text
    assert "phase=" not in text
    assert "iter=" not in text


def test_format_text_handles_unknown_git_and_blockers() -> None:
    payload = _payload(git={"head": None, "branch": None, "dirty": None}, blockers=["one"])
    text = status_mod._format_text(payload)
    assert "blockers: one" in text
    # An unset git branch renders as the explicit ``<unknown>`` sentinel,
    # never the literal string ``"None"`` from f-string coercion.
    assert "branch=<unknown>" in text
    assert "branch=None" not in text


def test_format_text_renders_epoch1_authority_with_gap_and_no_native_lines() -> None:
    payload = _payload(
        authority={"epoch": 1, "gap": "marker_absent", "generation_id": None}, native=None
    )
    text = status_mod._format_text(payload)
    assert "authority: epoch 1 (marker_absent)" in text
    assert "track:" not in text
    assert "open tasks:" not in text


def test_format_text_renders_epoch2_authority_with_generation() -> None:
    text = status_mod._format_text(_payload())
    assert f"authority: epoch 2 (generation {_GENERATION_ID})" in text


def test_format_text_lists_recent_decision_ids() -> None:
    decisions = [
        {"id": "D02", "title": "b", "status": "active"},
        {"id": "D01", "title": "a", "status": "superseded"},
    ]
    text = status_mod._format_text(_payload(recent_decisions=decisions))
    assert "recent decisions: D02, D01" in text


# ---- authority block through the CLI -------------------------------------


def _seed_state(tmp_path: Path) -> Path:
    """Write a v1 ``state.json`` slot under ``tmp_path/.ea``; status never reads it."""
    root = tmp_path / ".ea"
    root.mkdir()
    state_path = root / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    return state_path


def _declare(root: Path) -> None:
    atomic_write_json(
        declaration_path(root),
        CanaryDeclaration(disposable=True, declared_by="status-test", purpose="throwaway"),
    )


def _mark(root: Path) -> None:
    write_marker(
        target=DisposableTarget.require(root),
        generation_id=_GENERATION_ID,
        manifest_digest="a" * 64,
        published_digest="b" * 64,
        written_at=_DT,
    )


def _status_json(state_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run ``eawf --json status`` against *state_path* with git stubbed away."""
    from typer.testing import CliRunner

    from eawf.surfaces.cli.app import app

    def _no_git(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("git stubbed away")

    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setattr(subprocess, "run", _no_git)
    result = CliRunner().invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def test_status_authority_undeclared_tree_is_epoch1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _seed_state(tmp_path)

    payload = _status_json(state_path, monkeypatch)

    assert payload["authority"] == {"epoch": 1, "gap": "undeclared", "generation_id": None}


def test_status_authority_declared_unmarked_tree_is_marker_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _seed_state(tmp_path)
    _declare(state_path.parent)

    payload = _status_json(state_path, monkeypatch)

    assert payload["authority"] == {"epoch": 1, "gap": "marker_absent", "generation_id": None}


def test_status_authority_unreadable_marker_is_marker_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _seed_state(tmp_path)
    root = state_path.parent
    _declare(root)
    (root / "generations").mkdir()
    (root / "generations" / "EPOCH2_ACTIVE.json").write_text("{", encoding="utf-8")

    payload = _status_json(state_path, monkeypatch)

    assert payload["authority"] == {"epoch": 1, "gap": "marker_unreadable", "generation_id": None}


def test_status_authority_canary_tree_is_epoch2_with_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _seed_state(tmp_path)
    _declare(state_path.parent)
    _mark(state_path.parent)

    payload = _status_json(state_path, monkeypatch)

    assert payload["authority"] == {"epoch": 2, "gap": None, "generation_id": _GENERATION_ID}
    # The marker names a generation that has written no document yet.
    assert payload["native"] is None
