"""Integration tests for ``eawf wave dispatch`` / ``wave dispatch-batch`` (B025).

Drives the Typer app via :class:`typer.testing.CliRunner` against a
temp ``.ea/state.json``. Confirms text + JSON branches, atomic --output
writes, and the canonical exit-code mapping.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    yield tmp_path


def _bootstrap_chain(workspace: Path) -> None:
    """Init QR, open P01-I01, plan W01 -> W02 -> W03 chain."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    for wid, deps in (
        ("P01-I01-W01", None),
        ("P01-I01-W02", "P01-I01-W01"),
        ("P01-I01-W03", "P01-I01-W02"),
    ):
        args = [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            wid,
            "--title",
            f"title-{wid}",
            "--files",
            "src/",
        ]
        if deps is not None:
            args.extend(["--deps", deps])
        res = runner.invoke(app, args)
        assert res.exit_code == 0, res.stdout


# ---- wave dispatch ---------------------------------------------------------


def test_wave_dispatch_prints_prompt_to_stdout(workspace: Path) -> None:
    """Text mode dumps the prompt verbatim; required headers all present."""
    _bootstrap_chain(workspace)
    res = runner.invoke(app, ["wave", "dispatch", "P01-I01-W01"])
    assert res.exit_code == 0, res.stdout
    out = res.stdout
    assert "# Wave P01-I01-W01: title-P01-I01-W01" in out
    assert "## Scope" in out
    assert "## Dependencies" in out
    assert "## Workflow" in out
    assert "## Out of scope" in out


def test_wave_dispatch_json_mode_emits_envelope(workspace: Path) -> None:
    """``--json`` wraps the prompt in ``{"wave": ..., "prompt": ...}``."""
    _bootstrap_chain(workspace)
    res = runner.invoke(app, ["--json", "wave", "dispatch", "P01-I01-W02"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["wave"] == "P01-I01-W02"
    assert "# Wave P01-I01-W02" in payload["prompt"]
    # Dep on W01 surfaces in the prompt body.
    assert "P01-I01-W01" in payload["prompt"]


def test_wave_dispatch_output_writes_to_file_atomic(workspace: Path) -> None:
    """``--output`` writes the prompt to disk; envelope summary still emitted."""
    _bootstrap_chain(workspace)
    target = workspace / "out" / "prompt.md"
    res = runner.invoke(
        app,
        ["--json", "wave", "dispatch", "P01-I01-W01", "--output", str(target)],
    )
    assert res.exit_code == 0, res.stdout
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert "# Wave P01-I01-W01" in body
    payload = json.loads(res.stdout)
    assert payload["output"] == str(target)
    assert payload["wave"] == "P01-I01-W01"
    assert payload["bytes_written"] == len(body.encode("utf-8"))


def test_wave_dispatch_unknown_wave_exit_2(workspace: Path) -> None:
    """An unknown wave id surfaces as NOT_FOUND (exit 2)."""
    _bootstrap_chain(workspace)
    res = runner.invoke(app, ["wave", "dispatch", "P01-I01-W99"])
    assert res.exit_code == 1, res.stdout
    assert "unknown wave" in res.stdout


def test_wave_dispatch_closed_wave_warns_to_stderr_exit_0(workspace: Path) -> None:
    """A CLOSED wave still emits the prompt; a stderr note flags terminal status."""
    _bootstrap_chain(workspace)
    # Close W01 so its status becomes terminal.
    assert runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["wave", "close", "P01-I01-W01", "--outcome", "done"],
        ).exit_code
        == 0
    )
    # Click 8.3 exposes stderr separately on the Result object.
    res = runner.invoke(app, ["wave", "dispatch", "P01-I01-W01"])
    assert res.exit_code == 0, res.stdout
    assert "# Wave P01-I01-W01" in res.stdout
    assert "terminal status" in res.stderr
    assert "'closed'" in res.stderr


# ---- wave dispatch-batch ---------------------------------------------------


def test_wave_dispatch_batch_iter_full_list(workspace: Path) -> None:
    """No ``--ready-only``: every pending wave under the iter is rendered."""
    _bootstrap_chain(workspace)
    res = runner.invoke(app, ["--json", "wave", "dispatch-batch", "--iter", "P01-I01"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["iter"] == "P01-I01"
    ids = [entry["wave"] for entry in payload["prompts"]]
    assert ids == ["P01-I01-W01", "P01-I01-W02", "P01-I01-W03"]
    for entry in payload["prompts"]:
        assert "# Wave " in entry["prompt"]


def test_wave_dispatch_batch_ready_only_filters_to_ready(workspace: Path) -> None:
    """``--ready-only`` mirrors ``wave next-ready``: just W01 before any close."""
    _bootstrap_chain(workspace)
    res = runner.invoke(
        app,
        ["--json", "wave", "dispatch-batch", "--iter", "P01-I01", "--ready-only"],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    ids = [entry["wave"] for entry in payload["prompts"]]
    assert ids == ["P01-I01-W01"]
    # Close W01 then re-run: W02 becomes ready.
    assert runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["wave", "close", "P01-I01-W01", "--outcome", "ok"],
        ).exit_code
        == 0
    )
    res = runner.invoke(
        app,
        ["--json", "wave", "dispatch-batch", "--iter", "P01-I01", "--ready-only"],
    )
    payload = json.loads(res.stdout)
    ids = [entry["wave"] for entry in payload["prompts"]]
    assert ids == ["P01-I01-W02"]


def test_wave_dispatch_batch_no_iter_no_current_iter_exit_3(workspace: Path) -> None:
    """No ``--iter`` and no ``state.current.iter_id`` ⇒ exit 3 (INVALID_INPUT)."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ).exit_code
        == 0
    )
    # No phase / iter opened, so state.current.iter_id is None.
    res = runner.invoke(app, ["wave", "dispatch-batch"])
    assert res.exit_code == 1, res.stdout
    assert "state.current.iter_id" in res.stdout


def test_wave_dispatch_batch_text_mode_separator(workspace: Path) -> None:
    """Text mode prints ``---- WAVE <id> ----`` between prompts."""
    _bootstrap_chain(workspace)
    res = runner.invoke(app, ["wave", "dispatch-batch", "--iter", "P01-I01"])
    assert res.exit_code == 0, res.stdout
    out = res.stdout
    assert "---- WAVE P01-I01-W01 ----" in out
    assert "---- WAVE P01-I01-W02 ----" in out
    assert "---- WAVE P01-I01-W03 ----" in out
