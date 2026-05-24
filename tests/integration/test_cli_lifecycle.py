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

from eawf.surfaces.cli.app import app

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
    assert res.exit_code == 1
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
    assert res.exit_code == 1


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
    assert res2.exit_code == 1
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
    assert res.exit_code == 1
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
    assert res.exit_code == 1


def test_phase_open_auto_and_explicit_conflict(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(
        app,
        ["phase", "open", "P01", "--auto", "--title", "x"],
    )
    assert res.exit_code == 1


def test_phase_close_with_open_iter_exits_4(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    res = runner.invoke(app, ["phase", "close", "P01", "--audit", "AUD-1"])
    assert res.exit_code == 2, res.stdout


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

    assert res.exit_code == 2, res.stdout
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
    assert res.exit_code == 1


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
    assert res.exit_code == 2, res.stdout


# ---- iter plan (stage PLANNED iter, no current-pointer move) ----------------


def test_iter_plan_stages_planned_iter_keeps_current(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    res = runner.invoke(app, ["--json", "iter", "plan", "P01-I02", "--title", "Follow-up"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["iter"] == "P01-I02"
    assert payload["status"] == "planned"
    state = _read_state(workspace)
    assert state["iters"]["P01-I02"]["status"] == "planned"  # type: ignore[index]
    # The active iter keeps running — plan must not move the current pointer.
    assert state["current"]["iter_id"] == "P01-I01"  # type: ignore[index]


def test_iter_plan_then_activate_flips_to_active(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    assert runner.invoke(app, ["iter", "plan", "P01-I02", "--title", "Next"]).exit_code == 0
    res = runner.invoke(app, ["iter", "activate", "P01-I02"])
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace)
    assert state["iters"]["P01-I02"]["status"] == "active"  # type: ignore[index]
    assert state["current"]["iter_id"] == "P01-I02"  # type: ignore[index]


def test_iter_plan_invalid_id_exits_nonzero(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    res = runner.invoke(app, ["iter", "plan", "not-an-iter", "--title", "x"])
    assert res.exit_code == 1
    assert "invalid iter id" in res.stdout


def test_iter_plan_requires_title(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    res = runner.invoke(app, ["iter", "plan", "P01-I02"])
    assert res.exit_code == 1
    assert "--title required" in res.stdout


def test_iter_plan_duplicate_exits_nonzero(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    assert runner.invoke(app, ["iter", "plan", "P01-I02", "--title", "Next"]).exit_code == 0
    res = runner.invoke(app, ["iter", "plan", "P01-I02", "--title", "Dup"])
    assert res.exit_code != 0


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
    assert res.exit_code == 1


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
    # Post C05 § 5.3 the value lands in (1, 2): USER_ERROR or VALIDATION_ERROR.
    assert res.exit_code in (1, 2)


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
    assert res.exit_code == 1


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
    assert res.exit_code == 1


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
    assert res.exit_code == 1


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


# ---- wave update --files / --add-file / --remove-file (B046) ---------------


def _bootstrap_update_pending_wave(
    workspace: Path,
    wave_id: str = "P01-I01-W01",
    files_csv: str = "src/a.py",
) -> None:
    """Bring the state up to one PENDING wave with *files_csv* as file_scopes."""
    _bootstrap_to_iter(workspace)
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
                files_csv,
            ],
        ).exit_code
        == 0
    )


def test_wave_update_files_set_replaces_scope(workspace: Path) -> None:
    _bootstrap_update_pending_wave(workspace, files_csv="src/a.py")
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "update",
            "P01-I01-W01",
            "--files",
            "src/b.py,src/c.py",
        ],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["wave"] == "P01-I01-W01"
    assert payload["mode"] == "set"
    assert payload["file_scopes"] == ["src/b.py", "src/c.py"]
    assert payload["added"] == ["src/b.py", "src/c.py"]
    assert payload["removed"] == ["src/a.py"]
    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["file_scopes"] == [  # type: ignore[index]
        "src/b.py",
        "src/c.py",
    ]


def test_wave_update_files_add_one_appends_and_dedups(workspace: Path) -> None:
    _bootstrap_update_pending_wave(workspace, files_csv="src/a.py")
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "update",
            "P01-I01-W01",
            "--add-file",
            "src/b.py",
        ],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["file_scopes"] == ["src/a.py", "src/b.py"]
    assert payload["added"] == ["src/b.py"]
    # Re-adding an existing path is a no-op (dedup).
    res2 = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "update",
            "P01-I01-W01",
            "--add-file",
            "src/b.py",
        ],
    )
    assert res2.exit_code == 0, res2.stdout
    payload2 = json.loads(res2.stdout)
    assert payload2["file_scopes"] == ["src/a.py", "src/b.py"]
    assert payload2["added"] == []


def test_wave_update_files_add_many_preserves_order(workspace: Path) -> None:
    _bootstrap_update_pending_wave(workspace, files_csv="src/a.py")
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "update",
            "P01-I01-W01",
            "--add-file",
            "src/b.py,src/c.py,src/d.py",
        ],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["file_scopes"] == ["src/a.py", "src/b.py", "src/c.py", "src/d.py"]
    assert payload["added"] == ["src/b.py", "src/c.py", "src/d.py"]


def test_wave_update_files_remove_drops_entries(workspace: Path) -> None:
    _bootstrap_update_pending_wave(workspace, files_csv="src/a.py,src/b.py,src/c.py")
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "update",
            "P01-I01-W01",
            "--remove-file",
            "src/b.py,src/missing.py",
        ],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    # ``src/missing.py`` is silently ignored — remove of a path not present
    # is a no-op so reactive scripts can be idempotent.
    assert payload["file_scopes"] == ["src/a.py", "src/c.py"]
    assert payload["removed"] == ["src/b.py"]


def test_wave_update_files_allowed_on_claimed_wave(workspace: Path) -> None:
    _bootstrap_update_pending_wave(workspace, files_csv="src/a.py")
    assert runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "SES-1"]).exit_code == 0
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "update",
            "P01-I01-W01",
            "--add-file",
            "src/b.py",
        ],
    )
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["status"] == "claimed"  # type: ignore[index]
    assert state["waves"]["P01-I01-W01"]["file_scopes"] == [  # type: ignore[index]
        "src/a.py",
        "src/b.py",
    ]


def test_wave_update_files_closed_wave_exits_4(workspace: Path) -> None:
    _bootstrap_update_pending_wave(workspace, files_csv="src/a.py")
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "SES-1"])
    assert (
        runner.invoke(
            app,
            ["wave", "close", "P01-I01-W01", "--outcome", "done"],
        ).exit_code
        == 0
    )
    res = runner.invoke(
        app,
        ["wave", "update", "P01-I01-W01", "--files", "src/b.py"],
    )
    assert res.exit_code == 2, res.stdout
    assert "closed" in res.stdout.lower() or "pending or claimed" in res.stdout.lower()


def test_wave_update_files_unknown_wave_exits_2(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    res = runner.invoke(
        app,
        ["wave", "update", "P01-I01-W99", "--files", "src/b.py"],
    )
    assert res.exit_code == 1, res.stdout
    assert "unknown wave" in res.stdout.lower()


def test_wave_update_files_invalid_wave_id_exits_3(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    res = runner.invoke(
        app,
        ["wave", "update", "not-a-wave-id", "--files", "src/b.py"],
    )
    assert res.exit_code == 1, res.stdout


def test_wave_update_files_no_mode_exits_3(workspace: Path) -> None:
    _bootstrap_update_pending_wave(workspace, files_csv="src/a.py")
    res = runner.invoke(app, ["wave", "update", "P01-I01-W01"])
    assert res.exit_code == 1, res.stdout


def test_wave_update_files_multiple_modes_exits_3(workspace: Path) -> None:
    _bootstrap_update_pending_wave(workspace, files_csv="src/a.py")
    res = runner.invoke(
        app,
        [
            "wave",
            "update",
            "P01-I01-W01",
            "--files",
            "src/b.py",
            "--add-file",
            "src/c.py",
        ],
    )
    assert res.exit_code == 1, res.stdout


def test_wave_update_files_empty_files_list_exits_3(workspace: Path) -> None:
    _bootstrap_update_pending_wave(workspace, files_csv="src/a.py")
    # Pure whitespace / empty-after-strip CSV resolves to zero paths.
    res = runner.invoke(
        app,
        ["wave", "update", "P01-I01-W01", "--files", "   ,  ,"],
    )
    assert res.exit_code == 1, res.stdout
    assert "at least one path" in res.stdout.lower()


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
    assert res.exit_code == 1
