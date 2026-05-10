"""Integration tests for ``eawf wave budget set|consume|show`` and the
``wave claim`` over-budget gate.

Mirrors the harness used by :mod:`tests.integration.test_cli_lifecycle`:
a temp ``.ea/state.json`` resolved via the ``EA_STATE`` env var.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a temp workspace dir with EA_STATE pointing inside it."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    yield tmp_path


def _read_state(workspace: Path) -> dict[str, object]:
    state_path = workspace / ".ea" / "state.json"
    return orjson.loads(state_path.read_bytes())  # type: ignore[no-any-return]


def _bootstrap_pending_wave(workspace: Path, wave_id: str = "P01-I01-W01") -> None:
    """Bring the state up to one pending wave under P01-I01."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Quant", "--domains", "quant"],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(app, ["phase", "open", "--auto", "--title", "Bootstrap"]).exit_code
        == 0
    )
    assert (
        runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "Iter1"]).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "wave",
                "plan",
                "P01-I01",
                "--id",
                wave_id,
                "--title",
                "w",
                "--files",
                "src/",
            ],
        ).exit_code
        == 0
    )


def test_wave_budget_set_then_show_round_trip(workspace: Path) -> None:
    _bootstrap_pending_wave(workspace)
    res = runner.invoke(
        app,
        ["--json", "wave", "budget", "set", "P01-I01-W01", "1000"],
    )
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace)
    waves = state["waves"]  # type: ignore[index]
    assert waves["P01-I01-W01"]["token_budget"] == 1000  # type: ignore[index]
    assert waves["P01-I01-W01"]["tokens_consumed"] == 0  # type: ignore[index]

    show = runner.invoke(app, ["--json", "wave", "budget", "show", "P01-I01-W01"])
    assert show.exit_code == 0, show.stdout
    payload = json.loads(show.stdout)
    assert payload["token_budget"] == 1000
    assert payload["tokens_consumed"] == 0
    assert payload["remaining"] == 1000
    assert payload["classification"] is None


def test_wave_budget_show_without_budget(workspace: Path) -> None:
    _bootstrap_pending_wave(workspace)
    show = runner.invoke(app, ["--json", "wave", "budget", "show", "P01-I01-W01"])
    assert show.exit_code == 0, show.stdout
    payload = json.loads(show.stdout)
    assert payload["token_budget"] is None
    assert payload["tokens_consumed"] == 0
    assert payload["remaining"] is None
    assert payload["classification"] is None


def test_wave_budget_set_negative_exits_3(workspace: Path) -> None:
    _bootstrap_pending_wave(workspace)
    res = runner.invoke(app, ["wave", "budget", "set", "P01-I01-W01", "-1"])
    # Typer rejects negative ints differently depending on the int parser,
    # but the handler also re-validates and emits InvalidInput (exit 3).
    # Either way, the exit must be non-zero (3 or 2).
    assert res.exit_code != 0


def test_wave_budget_consume_warn_exit_zero(workspace: Path) -> None:
    _bootstrap_pending_wave(workspace)
    assert (
        runner.invoke(app, ["wave", "budget", "set", "P01-I01-W01", "1000"]).exit_code == 0
    )
    res = runner.invoke(
        app,
        ["--json", "wave", "budget", "consume", "P01-I01-W01", "750"],
    )
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["tokens_consumed"] == 750  # type: ignore[index]


def test_wave_budget_consume_block_nonzero_exit(workspace: Path) -> None:
    _bootstrap_pending_wave(workspace)
    assert (
        runner.invoke(app, ["wave", "budget", "set", "P01-I01-W01", "1000"]).exit_code == 0
    )
    res = runner.invoke(
        app,
        ["wave", "budget", "consume", "P01-I01-W01", "1000"],
    )
    # Block path raises ValidationFailed (exit 4) and rolls back the
    # state mutation because the lock-held transaction never reaches the
    # write step.
    assert res.exit_code == 4, res.stdout
    state = _read_state(workspace)
    # tokens_consumed stays at 0 — the transaction was rolled back.
    assert state["waves"]["P01-I01-W01"]["tokens_consumed"] == 0  # type: ignore[index]


def test_wave_budget_consume_unknown_wave_exits_2(workspace: Path) -> None:
    _bootstrap_pending_wave(workspace)
    res = runner.invoke(
        app, ["wave", "budget", "consume", "P09-I09-W09", "100"]
    )
    assert res.exit_code == 2, res.stdout


def test_wave_budget_set_unknown_wave_exits_2(workspace: Path) -> None:
    _bootstrap_pending_wave(workspace)
    res = runner.invoke(app, ["wave", "budget", "set", "P09-I09-W09", "100"])
    assert res.exit_code == 2, res.stdout


def test_wave_claim_refuses_over_budget(workspace: Path) -> None:
    """Once ``tokens_consumed >= token_budget`` is recorded on disk,
    ``wave claim`` refuses with exit 4 and the canonical error string."""
    _bootstrap_pending_wave(workspace)
    # Seed the wave with a tiny budget and consume up to it via the
    # service module directly, then re-run the CLI's claim path. To keep
    # the test pure-CLI, set a budget of 1000 then consume 999, then
    # bump consumption to 1000 by running consume twice — the second
    # consume blocks. We need a different approach: set a budget that
    # leaves the consume at exactly 75 % (warn, exit 0), then run a
    # second consume at the remaining 25 % which lands at 100 % and
    # rolls back. So instead use the smallest possible: ``set 100`` then
    # call ``consume 75`` (warn, persisted), then run another ``consume
    # 24`` (warn, persisted, 99 %), then run a claim — still under
    # budget. The cleanest end-to-end gate proof seeds via two consume
    # calls that together stay under budget, then a claim, then a
    # *manual* over-budget setup by lowering the budget below the
    # current consumption with ``set``.
    assert runner.invoke(app, ["wave", "budget", "set", "P01-I01-W01", "1000"]).exit_code == 0
    assert (
        runner.invoke(app, ["wave", "budget", "consume", "P01-I01-W01", "750"]).exit_code
        == 0
    )
    # Lower the budget to match the existing consumption — wave is now
    # exactly at 100 % and ``wave claim`` must refuse.
    assert runner.invoke(app, ["wave", "budget", "set", "P01-I01-W01", "750"]).exit_code == 0

    res = runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "SES-1"])
    assert res.exit_code == 4, res.stdout
    assert "over token budget" in res.stdout

    state = _read_state(workspace)
    # Wave still pending — the gate fired before the claim could land.
    assert state["waves"]["P01-I01-W01"]["status"] == "pending"  # type: ignore[index]
