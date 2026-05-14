"""Integration tests for the lifecycle CLI sub-apps.

Drives the root ``eawf`` Typer app via :class:`typer.testing.CliRunner` against
a temp ``.ea/state.json``. Honours the ``EA_STATE`` env var (set per-test via
:func:`monkeypatch.setenv`) the W00 scope resolver supports.
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


# ---- project init -----------------------------------------------------------


def test_project_init_creates_state_json(workspace: Path) -> None:
    res = runner.invoke(
        app,
        [
            "--json",
            "project",
            "init",
            "QR",
            "--title",
            "Quant",
            "--domains",
            "quant,ml",
        ],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["project"] == "QR"
    state = _read_state(workspace)
    assert state["scope_kind"] == "repo"
    assert state["project"]["code"] == "QR"
    assert state["project"]["domains"] == ["quant", "ml"]
    assert state["current"]["project_code"] == "QR"


def test_project_init_invalid_code_exits_3(workspace: Path) -> None:
    res = runner.invoke(
        app,
        [
            "project",
            "init",
            "lowercase",
            "--title",
            "x",
            "--domains",
            "x",
        ],
    )
    assert res.exit_code == 3
    assert "invalid project code" in res.stdout


def test_project_init_empty_domains_exits_3(workspace: Path) -> None:
    res = runner.invoke(
        app,
        [
            "project",
            "init",
            "QR",
            "--title",
            "x",
            "--domains",
            "",
        ],
    )
    assert res.exit_code == 3


def test_project_init_existing_state_exits_3(workspace: Path) -> None:
    res = runner.invoke(
        app,
        ["project", "init", "QR", "--title", "x", "--domains", "x"],
    )
    assert res.exit_code == 0
    res2 = runner.invoke(
        app,
        ["project", "init", "QR2", "--title", "y", "--domains", "y"],
    )
    assert res2.exit_code == 3
    assert "already exists" in res2.stdout


# ---- subproject add/switch --------------------------------------------------


def _init_project(workspace: Path) -> None:
    res = runner.invoke(
        app,
        ["project", "init", "QR", "--title", "Quant", "--domains", "quant"],
    )
    assert res.exit_code == 0


def test_subproject_add_then_switch(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(
        app,
        [
            "--json",
            "subproject",
            "add",
            "COLLAR",
            "--kind",
            "strategy",
            "--title",
            "Collar",
            "--domains",
            "options,risk",
        ],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["subproject"] == "COLLAR"
    state = _read_state(workspace)
    assert "COLLAR" in state["subprojects"]  # type: ignore[index]

    res = runner.invoke(app, ["--json", "subproject", "switch", "COLLAR"])
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace)
    assert state["current"]["subproject_id"] == "COLLAR"  # type: ignore[index]


def test_subproject_switch_unknown_exits_3(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(app, ["subproject", "switch", "GHOST"])
    assert res.exit_code == 3
    assert "unknown" in res.stdout


# ---- phase open/close -------------------------------------------------------


def test_phase_open_auto_allocates_p01(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(app, ["--json", "phase", "open", "--auto", "--title", "Bootstrap"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["phase"] == "P01"


def test_phase_open_explicit_p03(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(
        app,
        [
            "--json",
            "phase",
            "open",
            "P03",
            "--title",
            "Specific phase",
        ],
    )
    assert res.exit_code == 0, res.stdout


def test_phase_open_requires_id_or_auto(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(app, ["phase", "open", "--title", "x"])
    assert res.exit_code == 3


def test_phase_open_auto_and_explicit_conflict(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(
        app,
        ["phase", "open", "P01", "--auto", "--title", "x"],
    )
    assert res.exit_code == 3


def test_phase_close_with_open_iter_exits_4(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    res = runner.invoke(app, ["phase", "close", "P01", "--audit", "AUD-1"])
    assert res.exit_code == 4, res.stdout


def test_phase_close_happy(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "w",
            "--files",
            "src/",
        ],
    )
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"])
    runner.invoke(app, ["wave", "close", "P01-I01-W01", "--outcome", "done"])
    runner.invoke(app, ["iter", "close", "P01-I01", "--audit", "AUD-I"])
    # close_wave + close_iter satisfy the W03 closed-wave gate.
    runner.invoke(
        app,
        [
            "decision",
            "add",
            "D001",
            "--scope-id",
            "P01",
            "--summary",
            "P01 scope collapse: single-wave close",
            "--rationale",
            "minimal scenario",
            "--alternative",
            "plan more waves",
        ],
    )
    res = runner.invoke(app, ["phase", "close", "P01", "--audit", "AUD-1"])
    assert res.exit_code == 0, res.stdout


def test_phase_close_single_closed_wave_without_decision_exits_4(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "w",
            "--files",
            "src/",
        ],
    )
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "SES-1"])
    runner.invoke(
        app,
        [
            "wave",
            "close",
            "P01-I01-W01",
            "--outcome",
            "done",
        ],
    )
    runner.invoke(app, ["iter", "close", "P01-I01", "--audit", "AUD-1"])

    res = runner.invoke(app, ["phase", "close", "P01", "--audit", "AUD-1"])

    assert res.exit_code == 4, res.stdout
    assert "single closed wave" in res.stdout


def test_phase_reopen_happy_allows_followup_iter(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "w",
            "--files",
            "src/",
        ],
    )
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"])
    runner.invoke(app, ["wave", "close", "P01-I01-W01", "--outcome", "done"])
    runner.invoke(app, ["iter", "close", "P01-I01", "--audit", "AUD-I"])
    runner.invoke(
        app,
        [
            "decision",
            "add",
            "D001",
            "--scope-id",
            "P01",
            "--summary",
            "P01 scope collapse: single-wave close",
            "--rationale",
            "minimal scenario",
            "--alternative",
            "plan more waves",
        ],
    )
    runner.invoke(app, ["phase", "close", "P01", "--audit", "AUD-1"])
    res = runner.invoke(app, ["phase", "reopen", "P01"])
    assert res.exit_code == 0, res.stdout
    res = runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "follow-up"])
    assert res.exit_code == 0, res.stdout


def test_phase_reopen_already_open_exits_nonzero(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    res = runner.invoke(app, ["phase", "reopen", "P01"])
    assert res.exit_code != 0


def test_phase_reopen_unknown_exits_nonzero(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(app, ["phase", "reopen", "P99"])
    assert res.exit_code != 0


# ---- iter open/close --------------------------------------------------------


def test_iter_open_auto_allocates_i01(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    res = runner.invoke(
        app,
        ["--json", "iter", "open", "--phase", "P01", "--title", "Iter1"],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["iter"] == "P01-I01"


def test_iter_open_explicit_id(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    res = runner.invoke(app, ["--json", "iter", "open", "P01-I05", "--title", "Iter5"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["iter"] == "P01-I05"


def test_iter_open_unknown_phase_exits_3(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(
        app,
        ["iter", "open", "--phase", "P99", "--title", "x"],
    )
    assert res.exit_code == 3


def test_iter_close_with_pending_wave_exits_4(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "w",
            "--files",
            "src/",
        ],
    )
    res = runner.invoke(app, ["iter", "close", "P01-I01", "--audit", "AUD-1"])
    assert res.exit_code == 4, res.stdout


# ---- wave plan/claim/close/fail --------------------------------------------


def _bootstrap_to_iter(workspace: Path) -> None:
    _init_project(workspace)
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "Bootstrap"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "Iter1"]).exit_code == 0


def test_wave_plan_happy(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "Implement allocator",
            "--files",
            "src/eawf/lifecycle/",
        ],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["wave"] == "P01-I01-W01"
    assert payload["files"] == ["src/eawf/lifecycle/"]


def test_wave_plan_id_iter_mismatch_exits_3(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    res = runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I02-W01",  # wrong iter prefix
            "--title",
            "x",
            "--files",
            "src/",
        ],
    )
    assert res.exit_code == 3


def test_wave_plan_closed_iter_exits_3(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    runner.invoke(app, ["iter", "close", "P01-I01", "--audit", "AUD-1"])
    res = runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "x",
            "--files",
            "src/",
        ],
    )
    # closure invariant fires before structural; either way must be non-zero
    assert res.exit_code in (3, 4)


def test_wave_claim_happy(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "x",
            "--files",
            "src/",
        ],
    )
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "claim",
            "P01-I01-W01",
            "--session",
            "SES-1",
        ],
    )
    assert res.exit_code == 0, res.stdout


def test_wave_claim_invalid_policy_exits_3(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "x",
            "--files",
            "src/",
        ],
    )
    res = runner.invoke(
        app,
        [
            "wave",
            "claim",
            "P01-I01-W01",
            "--session",
            "S",
            "--worktree-policy",
            "bogus",
        ],
    )
    assert res.exit_code == 3


def test_wave_close_without_outcome_exits_3(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "x",
            "--files",
            "src/",
        ],
    )
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"])
    res = runner.invoke(app, ["wave", "close", "P01-I01-W01"])
    assert res.exit_code == 3


def test_wave_close_happy(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "x",
            "--files",
            "src/",
        ],
    )
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"])
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "close",
            "P01-I01-W01",
            "--outcome",
            "done",
        ],
    )
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["status"] == "closed"  # type: ignore[index]


def test_wave_fail_without_reason_exits_3(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "x",
            "--files",
            "src/",
        ],
    )
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"])
    res = runner.invoke(app, ["wave", "fail", "P01-I01-W01"])
    assert res.exit_code == 3


def test_wave_fail_happy(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "x",
            "--files",
            "src/",
        ],
    )
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"])
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "fail",
            "P01-I01-W01",
            "--reason",
            "broken assumption",
        ],
    )
    assert res.exit_code == 0
    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["status"] == "failed"  # type: ignore[index]


# ---- end-to-end happy path + events.jsonl audit trail ----------------------


def test_full_lifecycle_emits_events(workspace: Path) -> None:
    """Complete project init → wave close path; events.jsonl gets one record per mutation."""
    runner.invoke(
        app,
        ["project", "init", "QR", "--title", "Quant", "--domains", "quant"],
    )
    runner.invoke(app, ["phase", "open", "--auto", "--title", "Bootstrap"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "Iter1"])
    runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "W1",
            "--files",
            "src/",
        ],
    )
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "SES-1"])
    runner.invoke(
        app,
        [
            "wave",
            "close",
            "P01-I01-W01",
            "--outcome",
            "done",
        ],
    )

    events_path = workspace / ".ea" / "store" / "event.jsonl"
    assert events_path.exists()
    lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # 6 mutations → 6 events.
    assert len(lines) == 6
    commands = [json.loads(line)["payload"]["command"] for line in lines]
    assert commands == [
        "project init",
        "phase open",
        "iter open",
        "wave plan",
        "wave claim",
        "wave close",
    ]


def test_phase_open_auto_records_allocated_id_in_event(workspace: Path) -> None:
    """``phase open --auto`` records the allocated phase id (e.g. ``P01``) as
    the event ``scope_id`` rather than the placeholder string ``"auto"``.

    Regression for the bug where ``scope_id=phase_id or "auto"`` recorded the
    fallback literal whenever the explicit positional ``phase_id`` argument
    was omitted in favour of ``--auto``.
    """
    _init_project(workspace)
    res = runner.invoke(app, ["phase", "open", "--auto", "--title", "Bootstrap"])
    assert res.exit_code == 0, res.stdout

    events_path = workspace / ".ea" / "store" / "event.jsonl"
    lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    phase_open_events = [
        json.loads(line) for line in lines if json.loads(line)["payload"]["command"] == "phase open"
    ]
    assert len(phase_open_events) == 1
    assert phase_open_events[-1]["scope_id"] == "P01"


def test_iter_open_auto_records_allocated_id_in_event(workspace: Path) -> None:
    """``iter open --phase P01`` (auto-allocate iter) records the allocated
    iter id (e.g. ``P01-I01``) as the event ``scope_id`` rather than the
    parent phase id.

    Regression for the bug where ``scope_id=explicit_iter or explicit_phase``
    recorded the parent phase whenever the iter was auto-allocated.
    """
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "Bootstrap"])
    res = runner.invoke(
        app,
        ["iter", "open", "--phase", "P01", "--title", "Iter1"],
    )
    assert res.exit_code == 0, res.stdout

    events_path = workspace / ".ea" / "store" / "event.jsonl"
    lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    iter_open_events = [
        json.loads(line) for line in lines if json.loads(line)["payload"]["command"] == "iter open"
    ]
    assert len(iter_open_events) == 1
    assert iter_open_events[-1]["scope_id"] == "P01-I01"


def test_iter_open_explicit_id_records_iter_id_in_event(workspace: Path) -> None:
    """``iter open <P01-I05>`` (explicit) records the iter id, not the parent
    phase id. This is the non-auto branch of the same regression."""
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "Bootstrap"])
    res = runner.invoke(app, ["iter", "open", "P01-I05", "--title", "Iter5"])
    assert res.exit_code == 0, res.stdout

    events_path = workspace / ".ea" / "store" / "event.jsonl"
    lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    iter_open_events = [
        json.loads(line) for line in lines if json.loads(line)["payload"]["command"] == "iter open"
    ]
    assert len(iter_open_events) == 1
    assert iter_open_events[-1]["scope_id"] == "P01-I05"


def test_resolve_state_path_no_state_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When EA_STATE points at a non-existent file and project init is NOT
    the command, mutating commands fail with NOT_FOUND (exit 2)."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    res = runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    assert res.exit_code == 2
