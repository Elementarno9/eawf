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


def _write_ship_gate_fixture(workspace: Path) -> Path:
    fixture = workspace / "ship-gate-audit.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "name": "ship-gate",
                    "passed": True,
                    "details": "verified close readiness evidence",
                }
            ]
        ),
        encoding="utf-8",
    )
    return fixture


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


# ---- track add/switch -------------------------------------------------------


def _init_project(workspace: Path) -> None:
    res = runner.invoke(
        app,
        ["project", "init", "QR", "--title", "Quant", "--domains", "quant"],
    )
    assert res.exit_code == 0
    state_path = workspace / ".ea" / "state.json"
    state = orjson.loads(state_path.read_bytes())
    for session_id in ("S", "SES-1"):
        state["agent_sessions"][session_id] = {
            "id": session_id,
            "role": "executor",
            "runtime": "generic",
            "scope_id": "QR",
            "status": "active",
            "started_at": "2026-01-01T00:00:00Z",
        }
        state["current"]["active_session_ids"].append(session_id)
    state_path.write_bytes(orjson.dumps(state, option=orjson.OPT_INDENT_2))


def test_track_add_then_switch(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(
        app,
        [
            "--json",
            "track",
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
    assert payload["track"] == "COLLAR"
    state = _read_state(workspace)
    assert "COLLAR" in state["tracks"]  # type: ignore[index]

    res = runner.invoke(app, ["--json", "track", "switch", "COLLAR"])
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace)
    assert state["current"]["track_id"] == "COLLAR"  # type: ignore[index]


def test_track_add_unknown_kind_exits_nonzero(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(
        app,
        ["track", "add", "COLLAR", "--kind", "research-line", "--title", "Collar"],
    )
    assert res.exit_code != 0
    assert "unknown track kind" in res.stdout


def test_track_switch_unknown_exits_3(workspace: Path) -> None:
    _init_project(workspace)
    res = runner.invoke(app, ["track", "switch", "GHOST"])
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
    _plan_claimable_wave("P01-I01-W01")
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
    audit = runner.invoke(
        app,
        [
            "audit",
            "run",
            "AUD-1",
            "--scope-id",
            "P01",
            "--kind",
            "ship-gate",
            "--fixture",
            str(_write_ship_gate_fixture(workspace)),
        ],
    )
    assert audit.exit_code == 0, audit.stdout
    res = runner.invoke(app, ["phase", "close", "P01", "--audit", "AUD-1"])
    assert res.exit_code == 0, res.stdout


def test_phase_close_single_closed_wave_without_decision_exits_4(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    _plan_claimable_wave("P01-I01-W01")
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
    _plan_claimable_wave("P01-I01-W01")
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
    audit = runner.invoke(
        app,
        [
            "audit",
            "run",
            "AUD-1",
            "--scope-id",
            "P01",
            "--kind",
            "ship-gate",
            "--fixture",
            str(_write_ship_gate_fixture(workspace)),
        ],
    )
    assert audit.exit_code == 0, audit.stdout
    close = runner.invoke(app, ["phase", "close", "P01", "--audit", "AUD-1"])
    assert close.exit_code == 0, close.stdout
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


# ---- iter candidate-tag -----------------------------------------------------


def _seed_open_iter(workspace: Path) -> None:
    """Init a project, open P01, and open the P01-I01 iter."""
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])


def test_iter_candidate_tag_show_none_when_unset(workspace: Path) -> None:
    _seed_open_iter(workspace)
    res = runner.invoke(app, ["--json", "iter", "candidate-tag", "P01-I01"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["iter"] == "P01-I01"
    assert payload["candidate_tag"] is None


def test_iter_candidate_tag_set_then_show_reflects_tag(workspace: Path) -> None:
    _seed_open_iter(workspace)
    set_res = runner.invoke(app, ["--json", "iter", "candidate-tag", "P01-I01", "--set", "v0.5.0"])
    assert set_res.exit_code == 0, set_res.stdout
    set_payload = json.loads(set_res.stdout)
    assert set_payload["candidate_tag"] == "v0.5.0"
    # The set persisted to state.json through the mutation path.
    state = _read_state(workspace)
    assert state["iters"]["P01-I01"]["candidate_tag"] == "v0.5.0"  # type: ignore[index]
    # A subsequent show reflects the persisted tag.
    show_res = runner.invoke(app, ["--json", "iter", "candidate-tag", "P01-I01"])
    assert show_res.exit_code == 0, show_res.stdout
    assert json.loads(show_res.stdout)["candidate_tag"] == "v0.5.0"


def test_iter_candidate_tag_set_invalid_tag_exits_nonzero(workspace: Path) -> None:
    _seed_open_iter(workspace)
    res = runner.invoke(app, ["iter", "candidate-tag", "P01-I01", "--set", "0.5.0"])
    assert res.exit_code != 0
    # The rejected tag never lands on disk.
    state = _read_state(workspace)
    assert state["iters"]["P01-I01"]["candidate_tag"] is None  # type: ignore[index]


def test_iter_candidate_tag_invalid_iter_id_exits_nonzero(workspace: Path) -> None:
    _seed_open_iter(workspace)
    res = runner.invoke(app, ["iter", "candidate-tag", "not-an-iter"])
    assert res.exit_code != 0
    assert "invalid iter id" in res.stdout


def test_iter_candidate_tag_unknown_iter_exits_nonzero(workspace: Path) -> None:
    _seed_open_iter(workspace)
    res = runner.invoke(app, ["iter", "candidate-tag", "P01-I99"])
    assert res.exit_code != 0
    assert "unknown iter" in res.stdout


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
            "--effort-bucket",
            "M",
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
    # A phase holds at most one ACTIVE iter (LC-6), so the running iter must
    # close before the planned one activates.
    assert runner.invoke(app, ["iter", "close", "P01-I01", "--audit", "AUD-1"]).exit_code == 0
    res = runner.invoke(app, ["iter", "activate", "P01-I02"])
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace)
    assert state["iters"]["P01-I02"]["status"] == "active"  # type: ignore[index]
    assert state["current"]["iter_id"] == "P01-I02"  # type: ignore[index]


def test_iter_activate_second_active_iter_refused(workspace: Path) -> None:
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "x"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "i"])
    assert runner.invoke(app, ["iter", "plan", "P01-I02", "--title", "Next"]).exit_code == 0
    # P01-I01 is still ACTIVE: activating a second iter is refused (LC-6 guard).
    res = runner.invoke(app, ["iter", "activate", "P01-I02"])
    assert res.exit_code == 1, res.stdout
    assert "already has an active iter" in res.stdout


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


def _plan_claimable_wave(
    wave_id: str,
    *,
    title: str = "claimable wave",
    files_csv: str = "src/",
) -> None:
    """Plan one legacy CLI wave with an explicit non-empty criterion + waiver."""
    result = runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            wave_id,
            "--title",
            title,
            "--files",
            files_csv,
            "--effort-bucket",
            "M",
            "--success",
            "the claim persists the selected session",
            "--criteria-floor-waiver",
            "legacy CLI fixture carries an explicit claim criterion",
        ],
    )
    assert result.exit_code == 0, result.stdout


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
            "--effort-bucket",
            "M",
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
            "--effort-bucket",
            "M",
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
            "--effort-bucket",
            "M",
        ],
    )
    # closure invariant fires before structural; either way must be non-zero
    # Post C05 § 5.3 the value lands in (1, 2): USER_ERROR or VALIDATION_ERROR.
    assert res.exit_code in (1, 2)


def test_wave_claim_happy(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    _plan_claimable_wave("P01-I01-W01")
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


def test_wave_claim_daemonless_event_records_committed_session_reference(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback claim event carries the post-mutation session binding."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    _bootstrap_to_iter(workspace)
    _plan_claimable_wave("P01-I01-W01")

    res = runner.invoke(
        app,
        ["--json", "wave", "claim", "P01-I01-W01", "--session", "SES-1"],
    )

    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["claim_session_id"] == "SES-1"  # type: ignore[index]
    event_path = workspace / ".ea" / "store" / "event.jsonl"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    claims = [row for row in events if row["payload"]["command"] == "wave claim"]
    assert len(claims) == 1
    assert claims[0]["payload"]["extras"] == {"claim_session_id": "SES-1"}


def test_wave_claim_daemonless_resolves_repo_capacity_under_lock(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct fallback uses repo config and status rows, not stale active pointers."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    _bootstrap_to_iter(workspace)
    _plan_claimable_wave("P01-I01-W01", title="occupied lane")
    _plan_claimable_wave("P01-I01-W02", title="capacity contender")
    first = runner.invoke(
        app,
        ["wave", "claim", "P01-I01-W01", "--session", "S"],
    )
    assert first.exit_code == 0, first.stdout

    state_path = workspace / ".ea" / "state.json"
    state = orjson.loads(state_path.read_bytes())
    state["current"]["active_wave_ids"] = []
    state_path.write_bytes(orjson.dumps(state, option=orjson.OPT_INDENT_2))
    config_path = workspace / ".ea" / "config.yaml"
    config_path.write_text(
        "planning:\n  max_parallel_waves: 1\n",
        encoding="utf-8",
    )
    before = state_path.read_bytes()
    event_path = workspace / ".ea" / "store" / "event.jsonl"
    events_before = event_path.read_bytes()

    result = runner.invoke(
        app,
        ["wave", "claim", "P01-I01-W02", "--session", "SES-1"],
    )

    assert result.exit_code != 0
    assert "claim_parallel_limit_reached" in result.stdout
    assert state_path.read_bytes() == before
    assert event_path.read_bytes() == events_before


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
            "--effort-bucket",
            "M",
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
            "--effort-bucket",
            "M",
        ],
    )
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"])
    res = runner.invoke(app, ["wave", "close", "P01-I01-W01"])
    assert res.exit_code == 1


def test_wave_close_happy(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    _plan_claimable_wave("P01-I01-W01")
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
            "--effort-bucket",
            "M",
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
            "--effort-bucket",
            "M",
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


# ---- wave release (P29-I02-W01) --------------------------------------------


def _plan_and_claim_wave(workspace: Path) -> None:
    """Bootstrap to a CLAIMED ``P01-I01-W01`` ready to release."""
    _bootstrap_to_iter(workspace)
    _plan_claimable_wave("P01-I01-W01")
    runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"])


def test_wave_release_happy_returns_to_pending(workspace: Path) -> None:
    _plan_and_claim_wave(workspace)
    res = runner.invoke(
        app,
        ["--json", "wave", "release", "P01-I01-W01", "--reason", "cannot finish"],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    wave = state["waves"]["P01-I01-W01"]  # type: ignore[index]
    assert wave["status"] == "pending"
    assert wave["claim_session_id"] is None
    assert "P01-I01-W01" not in state["current"]["active_wave_ids"]  # type: ignore[index]


def test_wave_release_without_reason_succeeds(workspace: Path) -> None:
    _plan_and_claim_wave(workspace)
    res = runner.invoke(app, ["--json", "wave", "release", "P01-I01-W01"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["status"] == "pending"  # type: ignore[index]


def test_wave_release_closed_wave_exits_3(workspace: Path) -> None:
    _plan_and_claim_wave(workspace)
    runner.invoke(app, ["wave", "close", "P01-I01-W01", "--outcome", "done"])
    res = runner.invoke(app, ["wave", "release", "P01-I01-W01"])
    assert res.exit_code != 0
    # State untouched: the wave stays CLOSED.
    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["status"] == "closed"  # type: ignore[index]


def test_wave_release_pending_wave_is_noop(workspace: Path) -> None:
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
            "--effort-bucket",
            "M",
        ],
    )
    # Releasing a never-claimed (PENDING) wave is idempotent, not an error.
    res = runner.invoke(app, ["--json", "wave", "release", "P01-I01-W01"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["waves"]["P01-I01-W01"]["status"] == "pending"  # type: ignore[index]


def test_wave_release_invalid_wave_id_rejected(workspace: Path) -> None:
    _bootstrap_to_iter(workspace)
    res = runner.invoke(app, ["wave", "release", "not-a-wave-id"])
    assert res.exit_code != 0, res.stdout


# ---- wave update --files / --add-file / --remove-file (B046) ---------------


def _bootstrap_update_pending_wave(
    workspace: Path,
    wave_id: str = "P01-I01-W01",
    files_csv: str = "src/a.py",
) -> None:
    """Bring the state up to one PENDING wave with *files_csv* as file_scopes."""
    _bootstrap_to_iter(workspace)
    _plan_claimable_wave(wave_id, files_csv=files_csv)


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
    _init_project(workspace)
    runner.invoke(app, ["phase", "open", "--auto", "--title", "Bootstrap"])
    runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "Iter1"])
    _plan_claimable_wave("P01-I01-W01")
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
