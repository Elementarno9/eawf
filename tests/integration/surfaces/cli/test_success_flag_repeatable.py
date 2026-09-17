"""``--success`` is repeatable and never split on commas.

``roadmap revise --add-wave`` and ``wave plan`` used to comma-split one
``--success`` value, so a criterion carrying its own commas landed as
several fragments. Both verbs now take ``--success`` once per whole
criterion, in flag order, and advertise the repeatable form in their help.
"""

from __future__ import annotations

import re
from pathlib import Path

import orjson
import pytest
from click.testing import Result
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()

#: One criterion that carries its own commas. Comma-splitting shatters it
#: into three fragments, two of them under the 20-char criterion floor.
COMMA_CRITERION = "a, b and c long enough text"

#: Longest criterion the ``CriterionSpec`` text bound accepts, built with
#: commas so the max-length case also exercises the no-split contract.
MAX_CRITERION = "long, criterion, " + "x" * (500 - len("long, criterion, "))

_WAIVER = "test fixture models legacy success strings"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _criteria_texts(state_path: Path, wave_id: str) -> list[str]:
    """Return the persisted criterion texts for *wave_id*, in stored order."""
    state = orjson.loads(state_path.read_bytes())
    return [row["text"] for row in state["waves"][wave_id]["success_criteria"]]


def _flatten(help_text: str) -> str:
    """Collapse ANSI codes, rich panel borders and wrapping into one line."""
    plain = _ANSI.sub("", help_text).replace("│", " ")
    return " ".join(plain.split())


@pytest.fixture
def planned_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Init a project with a PLANNED P21 phase; yield the state path."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    assert (
        runner.invoke(app, ["project", "init", "RP", "--title", "R", "--domains", "x"]).exit_code
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
        runner.invoke(app, ["project", "init", "WP", "--title", "W", "--domains", "x"]).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    return state_path


def _revise_add_wave(*success: str, wave: str = "W01") -> Result:
    """Invoke ``roadmap revise --add-wave`` with one flag per *success* value."""
    argv = [
        "roadmap",
        "revise",
        "P21",
        "--add-wave",
        wave,
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


def _wave_plan(*success: str, wave: str = "P01-I01-W01") -> Result:
    """Invoke ``wave plan`` with one ``--success`` flag per *success* value."""
    argv = [
        "wave",
        "plan",
        "P01-I01",
        "--id",
        wave,
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


def test_roadmap_revise_success_keeps_commas_in_one_criterion(planned_phase: Path) -> None:
    """One ``--success`` flag stores exactly one criterion, commas intact."""
    res = _revise_add_wave(COMMA_CRITERION)
    assert res.exit_code == 0, res.output
    assert _criteria_texts(planned_phase, "P21-I01-W01") == [COMMA_CRITERION]


def test_roadmap_revise_success_repeats_in_flag_order(planned_phase: Path) -> None:
    """Two ``--success`` flags store two criteria in the order they were passed."""
    first = "first criterion, with a comma"
    second = "second criterion, also commas"
    res = _revise_add_wave(first, second)
    assert res.exit_code == 0, res.output
    assert _criteria_texts(planned_phase, "P21-I01-W01") == [first, second]


def test_roadmap_revise_success_omitted_stores_no_criteria(planned_phase: Path) -> None:
    """Empty boundary: no ``--success`` flag leaves the criteria list empty."""
    res = _revise_add_wave()
    assert res.exit_code == 0, res.output
    assert _criteria_texts(planned_phase, "P21-I01-W01") == []


def test_roadmap_revise_success_accepts_max_length_criterion(planned_phase: Path) -> None:
    """Max-length boundary: a 500-char criterion with commas stores whole."""
    assert len(MAX_CRITERION) == 500
    res = _revise_add_wave(MAX_CRITERION)
    assert res.exit_code == 0, res.output
    assert _criteria_texts(planned_phase, "P21-I01-W01") == [MAX_CRITERION]


def test_wave_plan_success_keeps_commas_in_one_criterion(open_iter: Path) -> None:
    """``wave plan`` stores one whole criterion per ``--success`` flag."""
    res = _wave_plan(COMMA_CRITERION)
    assert res.exit_code == 0, res.output
    assert _criteria_texts(open_iter, "P01-I01-W01") == [COMMA_CRITERION]


def test_wave_plan_success_repeats_in_flag_order(open_iter: Path) -> None:
    """``wave plan`` keeps two ``--success`` flags distinct, in flag order."""
    first = "first criterion, with a comma"
    second = "second criterion, also commas"
    res = _wave_plan(first, second)
    assert res.exit_code == 0, res.output
    assert _criteria_texts(open_iter, "P01-I01-W01") == [first, second]


def test_wave_plan_success_omitted_stores_no_criteria(open_iter: Path) -> None:
    """Empty boundary: ``wave plan`` without ``--success`` plans zero criteria."""
    res = _wave_plan()
    assert res.exit_code == 0, res.output
    assert _criteria_texts(open_iter, "P01-I01-W01") == []


@pytest.mark.parametrize(
    "argv",
    [
        ["roadmap", "revise", "--help"],
        ["wave", "plan", "--help"],
    ],
    ids=["roadmap-revise", "wave-plan"],
)
def test_success_help_names_the_repeatable_form(argv: list[str]) -> None:
    """Both verbs advertise ``--success`` as repeatable and never comma-split."""
    res = runner.invoke(app, argv)
    assert res.exit_code == 0, res.output
    flat = _flatten(res.output)
    assert "Repeatable: pass --success once per criterion." in flat
    assert "never split on commas" in flat
