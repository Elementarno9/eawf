"""A ``--success`` value under the 20-char floor is refused, not grandfathered.

A criterion shorter than the ``measurable_signal`` floor used to fall back
to a placeholder signal, so a comma fragment became a criterion that proves
nothing. ``roadmap revise --add-wave`` and ``wave plan`` now refuse the
value at the CLI boundary with an ``InvalidInput`` error and leave
``state.json`` byte-identical.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import orjson
import pytest
from click.testing import Result
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()

#: One char under the floor: the longest value that must still be refused.
TOO_SHORT = "legacy success text"

#: Exactly on the floor: the shortest value that must be accepted.
AT_FLOOR = "legacy success texts"

_WAIVER = "test fixture models legacy success strings"


@pytest.fixture
def planned_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Init a project with a PLANNED P21 phase; yield the state path."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    assert (
        runner.invoke(app, ["project", "init", "RJ", "--title", "R", "--domains", "x"]).exit_code
        == 0
    )
    assert (
        runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"]).exit_code == 0
    )
    return state_path


@pytest.fixture
def open_iter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Init a project with an ACTIVE P01-I01 iter; yield the state path."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    assert (
        runner.invoke(app, ["project", "init", "WJ", "--title", "W", "--domains", "x"]).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    return state_path


def _revise_add_wave(*success: str) -> Result:
    """Invoke ``roadmap revise --add-wave`` with one flag per *success* value."""
    argv = [
        "roadmap",
        "revise",
        "P21",
        "--add-wave",
        "W01",
        "--title",
        "Foo handling",
        "--files",
        "src/",
        "--effort-bucket",
        "S",
        "--criteria-floor-waiver",
        _WAIVER,
        "--intent-problem",
        "criteria fragment on commas",
        "--intent-desired-outcome",
        "one flag stores one criterion",
        "--intent-priority-rationale",
        "authoring surface correctness",
    ]
    for text in success:
        argv += ["--success", text]
    return runner.invoke(app, argv)


def _wave_plan(*success: str) -> Result:
    """Invoke ``wave plan`` with one ``--success`` flag per *success* value."""
    argv = [
        "wave",
        "plan",
        "P01-I01",
        "--id",
        "P01-I01-W01",
        "--title",
        "wave",
        "--files",
        "src/",
        "--effort-bucket",
        "M",
        "--criteria-floor-waiver",
        _WAIVER,
    ]
    for text in success:
        argv += ["--success", text]
    return runner.invoke(app, argv)


def _combined(result: Result) -> str:
    """Return stdout + stderr of a CLI result as one searchable string."""
    return f"{result.stdout}{result.stderr}"


@pytest.mark.parametrize(
    "value",
    [TOO_SHORT, "legacy", "", "   "],
    ids=["off-by-one", "single-word", "empty", "whitespace"],
)
def test_roadmap_revise_refuses_short_success(planned_phase: Path, value: str) -> None:
    """``roadmap revise`` refuses a sub-floor criterion and leaves state alone."""
    before = planned_phase.read_bytes()
    res = _revise_add_wave(value)
    assert res.exit_code != 0
    assert "InvalidInput" in _combined(res)
    assert planned_phase.read_bytes() == before


@pytest.mark.parametrize(
    "value",
    [TOO_SHORT, "legacy", "", "   "],
    ids=["off-by-one", "single-word", "empty", "whitespace"],
)
def test_wave_plan_refuses_short_success(open_iter: Path, value: str) -> None:
    """``wave plan`` refuses a sub-floor criterion and leaves state alone."""
    before = open_iter.read_bytes()
    res = _wave_plan(value)
    assert res.exit_code != 0
    assert "InvalidInput" in _combined(res)
    assert open_iter.read_bytes() == before


def test_roadmap_revise_refuses_when_only_the_second_flag_is_short(
    planned_phase: Path,
) -> None:
    """A long first criterion does not rescue a short second one."""
    before = planned_phase.read_bytes()
    res = _revise_add_wave("a criterion long enough to stand", TOO_SHORT)
    assert res.exit_code != 0
    assert "InvalidInput" in _combined(res)
    assert planned_phase.read_bytes() == before


def test_wave_plan_refuses_when_only_the_second_flag_is_short(open_iter: Path) -> None:
    """``wave plan`` checks every flag, not just the first."""
    before = open_iter.read_bytes()
    res = _wave_plan("a criterion long enough to stand", TOO_SHORT)
    assert res.exit_code != 0
    assert "InvalidInput" in _combined(res)
    assert open_iter.read_bytes() == before


def test_refusal_message_names_the_floor_and_the_no_split_rule(planned_phase: Path) -> None:
    """The error explains the floor and that values are never comma-split."""
    res = _revise_add_wave(TOO_SHORT)
    combined = _combined(res)
    assert "at least 20" in combined
    assert "never split on commas" in combined


@pytest.mark.parametrize(
    ("invoke", "wave_id", "fixture_name"),
    [
        (_revise_add_wave, "P21-I01-W01", "planned_phase"),
        (_wave_plan, "P01-I01-W01", "open_iter"),
    ],
    ids=["roadmap-revise", "wave-plan"],
)
def test_success_at_the_floor_is_accepted(
    invoke: Callable[..., Result],
    wave_id: str,
    fixture_name: str,
    request: pytest.FixtureRequest,
) -> None:
    """Off-by-one the other way: exactly 20 chars clears the floor."""
    state_path: Path = request.getfixturevalue(fixture_name)
    assert len(AT_FLOOR) == 20
    res = invoke(AT_FLOOR)
    assert res.exit_code == 0, res.output
    state = orjson.loads(state_path.read_bytes())
    criterion = state["waves"][wave_id]["success_criteria"][0]
    assert criterion["text"] == AT_FLOOR
    # The floor value serves as its own signal, so the grandfathered
    # placeholder never lands.
    assert criterion["measurable_signal"] == AT_FLOOR
