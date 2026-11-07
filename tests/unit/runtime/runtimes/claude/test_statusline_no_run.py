"""The scope and budget segments appear only for a session bound to a Run.

An operator session is not a Run, so its rows carry neither segment, and no
row ever shows the ``n/a`` marker: a segment that cannot be sourced is left
out. A session bound to a Run shows both on line 1.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.runtimes.claude import statusline as orchestrator
from tests.unit.runtime.runtimes.claude.test_statusline_truth import (
    HOST_PAYLOAD,
    RUN_KEY,
    SESSION_ID,
    bind_token_cap,
    canary_state_path,
    observe_usage,
    session_state,
)

_SGR: Final = re.compile(r"\x1b\[[0-9;]*m")
_ARROW: Final = ""


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep canary runtimes, ``$HOME`` and the cache under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def _rows(state_path: Path | None, payload: dict[str, Any]) -> list[str]:
    workspace = state_path.parent.parent if state_path is not None else None
    rendered = orchestrator.render_pipeline(payload, workspace=workspace, palette="dark")
    return _SGR.sub("", rendered).split("\n")


def _line_one(rows: list[str]) -> list[str]:
    return [part.strip() for part in rows[0].split(_ARROW) if part.strip()]


def test_a_session_without_a_run_shows_neither_scope_nor_budget(tmp_path: Path) -> None:
    state_path = canary_state_path(tmp_path, {})

    rows = _rows(state_path, {**HOST_PAYLOAD, "cwd": str(tmp_path)})

    segments = _line_one(rows)
    assert segments[0] == "Opus"
    assert not any(text.startswith(("EAWF-", "budget")) for text in segments)
    assert all("n/a" not in row for row in rows)


def test_a_session_on_a_run_shows_its_scope_and_budget_on_line_one(tmp_path: Path) -> None:
    state_path, run = session_state(tmp_path)
    bind_token_cap(state_path, run, 10_000)
    observe_usage(state_path, run, 1, tokens=4_000)

    rows = _rows(state_path, {**HOST_PAYLOAD, "cwd": str(tmp_path)})

    segments = _line_one(rows)
    assert segments[-2:] == ["EAWF-0126", "budget: 4.0k/10.0k"]
    assert all("n/a" not in row for row in rows)


def test_a_run_whose_budget_cannot_be_sourced_shows_its_scope_alone(tmp_path: Path) -> None:
    # boundary: a Run sealed without a token cap has a scope and no budget.
    state_path, _ = session_state(tmp_path)

    rows = _rows(state_path, {**HOST_PAYLOAD, "cwd": str(tmp_path)})

    assert _line_one(rows)[-1] == "EAWF-0126"
    assert all("n/a" not in row and "budget" not in row for row in rows)


@pytest.mark.parametrize("payload", [{}, {"session_id": SESSION_ID}, {"model": 7}])
def test_a_payload_with_nothing_to_source_renders_no_marker(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    # error-path: an empty, session-only or malformed payload, with and without a tree.
    for state_path in (None, canary_state_path(tmp_path, {RUN_KEY: {}})):
        rendered = orchestrator.render_pipeline(
            payload,
            workspace=state_path.parent.parent if state_path is not None else None,
            palette="light",
        )
        assert "n/a" not in rendered
