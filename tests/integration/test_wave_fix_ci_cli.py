"""Integration tests for ``eawf wave fix-ci`` and ``wave fix-ci-loop``.

The tests stand up a temp ``.ea/state.json`` via the regular CLI
(``project init`` + ``phase open`` + ``iter open`` + ``wave plan``),
write a CI log to a temp file, then drive the new verbs and assert on
both the JSON envelope and the post-mutation state file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


# ---- harness ----------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Init a temp workspace + ``.ea/state.json`` and route the CLI at it."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    yield tmp_path


def _bootstrap_with_parent(workspace: Path) -> str:
    """Init project, open P01-I01, plan a single parent wave; return its id."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    parent_id = "P01-I01-W01"
    res = runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            parent_id,
            "--title",
            "parent wave",
            "--files",
            "src/eawf/foo.py",
        ],
    )
    assert res.exit_code == 0, res.stdout
    return parent_id


def _state_payload(workspace: Path) -> dict[str, Any]:
    state_path = workspace / ".ea" / "state.json"
    return orjson.loads(state_path.read_bytes())  # type: ignore[no-any-return]


def _write_log(tmp_path: Path, content: str) -> Path:
    log_path = tmp_path / "ci.log"
    log_path.write_text(content, encoding="utf-8")
    return log_path


# ---- wave fix-ci ------------------------------------------------------------


def test_wave_fix_ci_dry_run_emits_envelope_no_state_change(workspace: Path) -> None:
    """``--dry-run`` describes the would-be wave but does not mutate state."""
    parent = _bootstrap_with_parent(workspace)
    log_path = _write_log(
        workspace,
        "FAILED tests/foo.py::test_bar - AssertionError: 1 != 2\n"
        "src/eawf/baz.py:10:1: E501 line too long\n",
    )
    before = _state_payload(workspace)
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "fix-ci",
            parent,
            "--log",
            str(log_path),
            "--dry-run",
        ],
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["parent"] == parent
    assert envelope["failures"] == 2
    assert envelope["dry_run"] is True
    pw = envelope["planned_wave"]
    assert pw["deps"] == [parent]
    assert pw["iter"] == "P01-I01"
    # Sorted-unique file scope union: test_path + ruff path.
    assert pw["file_scope"] == ["src/eawf/baz.py", "tests/foo.py"]
    assert "CI fix follow-up" in pw["title"]
    # State unchanged: same wave set as before.
    after = _state_payload(workspace)
    assert set(after["waves"].keys()) == set(before["waves"].keys())


def test_wave_fix_ci_plans_follow_up_with_correct_deps_and_scope(
    workspace: Path,
) -> None:
    """Live run plans a new wave with the parent in ``deps`` and the union scope."""
    parent = _bootstrap_with_parent(workspace)
    log_path = _write_log(
        workspace,
        "FAILED tests/foo.py::test_bar - AssertionError: 1 != 2\n"
        "src/eawf/baz.py:10:1: E501 line too long\n"
        "src/eawf/qux.py:5: error: Incompatible types\n",
    )
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "fix-ci",
            parent,
            "--log",
            str(log_path),
        ],
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["parent"] == parent
    assert envelope["failures"] == 3
    planned = envelope["planned_wave"]
    # Default allocation: smallest free wave-id under P01-I01.
    assert planned["id"] == "P01-I01-W02"
    assert planned["deps"] == [parent]
    assert planned["file_scope"] == [
        "src/eawf/baz.py",
        "src/eawf/qux.py",
        "tests/foo.py",
    ]
    # State now carries the new wave with the expected shape.
    payload = _state_payload(workspace)
    new_wave = payload["waves"]["P01-I01-W02"]
    assert new_wave["status"] == "pending"
    assert new_wave["deps"] == [parent]
    assert new_wave["file_scopes"] == [
        "src/eawf/baz.py",
        "src/eawf/qux.py",
        "tests/foo.py",
    ]
    # Reverse index: parent.blocks gains the new wave id.
    assert "P01-I01-W02" in payload["waves"][parent]["blocks"]


def test_wave_fix_ci_no_failures_exit_zero_envelope(workspace: Path) -> None:
    """A clean log → exit 0, ``planned_wave=null``, no state mutation."""
    parent = _bootstrap_with_parent(workspace)
    log_path = _write_log(
        workspace,
        "=========== passed ===========\nAll checks passed.\n",
    )
    before = _state_payload(workspace)
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "fix-ci",
            parent,
            "--log",
            str(log_path),
        ],
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["parent"] == parent
    assert envelope["failures"] == 0
    assert envelope["planned_wave"] is None
    # State unchanged.
    after = _state_payload(workspace)
    assert set(after["waves"].keys()) == set(before["waves"].keys())


# ---- wave fix-ci-loop -------------------------------------------------------


def test_wave_fix_ci_loop_refuses_on_repeated_signature(workspace: Path) -> None:
    """Same failure signature on consecutive iters → exit 4.

    The loop re-parses the same log file each iteration. Identical
    failure counts → identical ``pytest:N ruff:N mypy:N`` summary →
    the second iteration sees the prior signature and refuses with
    VALIDATION_FAILED (exit 4). The first follow-up wave should have
    landed in state; the second should NOT.
    """
    parent = _bootstrap_with_parent(workspace)
    log_path = _write_log(
        workspace,
        "src/eawf/foo.py:1:1: E501 line too long\n",
    )
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "fix-ci-loop",
            parent,
            "--log",
            str(log_path),
            "--max-iters",
            "3",
        ],
    )
    assert res.exit_code == 4, res.stdout
    envelope = json.loads(res.stdout)
    history = envelope["history"]
    # Iter 1: planned W02. Iter 2: same signature → refused.
    assert len(history) == 2
    assert history[0]["planned_wave"] == "P01-I01-W02"
    assert history[1]["planned_wave"] is None
    assert "not converging" in history[1]["refused"]
    assert envelope["converged"] is False
    # State: only the first follow-up landed. W03 must not exist.
    payload = _state_payload(workspace)
    assert "P01-I01-W02" in payload["waves"]
    assert "P01-I01-W03" not in payload["waves"]


def test_wave_fix_ci_loop_respects_max_iters(workspace: Path) -> None:
    """``--max-iters 1`` plans at most one follow-up wave."""
    parent = _bootstrap_with_parent(workspace)
    log_path = _write_log(
        workspace,
        "FAILED tests/foo.py::test_bar - AssertionError: 1 != 2\n",
    )
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "fix-ci-loop",
            parent,
            "--log",
            str(log_path),
            "--max-iters",
            "1",
        ],
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["iters"] == 1
    assert len(envelope["history"]) == 1
    assert envelope["history"][0]["planned_wave"] == "P01-I01-W02"
    # Only one new wave was added — W03 must NOT exist.
    payload = _state_payload(workspace)
    assert "P01-I01-W02" in payload["waves"]
    assert "P01-I01-W03" not in payload["waves"]
