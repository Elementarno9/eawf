"""The product console replays the golden contract, journeys first, in one app.

This is the wave gate over the port's replay, so it is bounded: five journeys and the
80-column route frames, which run in seconds. The full 261-frame / 25-journey census is
the console-replay CI job's, under ``tests/snapshots/tui/console``.

What it holds the replay to is the shape rather than the size. One ``ConsoleApp`` is
mounted for the whole run, so the frames replayed after the journeys still match in a
session the journeys left deep in state; every frame goes through the harness' single
render entry point rather than a second private path; and each frame and each journey
step settles in one cycle, which is what a held clock buys. It also holds the port's
inheritance of the contract: the test-only chassis is gone, nothing under the snapshot
package imports it any more, and the epoch-1 stylesheet rules the port keeps until the
release candidate are still in ``theme.tcss``.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.surfaces.tui.console import harness as harness_mod
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.harness import (
    Capture,
    Contract,
    GoldenLayout,
    Harness,
    Result,
    grid_errors,
    load_contract,
    pilot_key,
    select,
    visible,
)
from eawf.surfaces.tui.console.session import SIZES, SessionSetup

from .test_chassis_route_addition import EPOCH1_SELECTORS, THEME_TCSS

TESTS_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_ROOT = TESTS_ROOT / "fixtures" / "console" / "golden"
SNAPSHOT_PACKAGE = TESTS_ROOT / "snapshots" / "tui" / "console"
CHASSIS_PACKAGE = SNAPSHOT_PACKAGE / "console_chassis"
CHASSIS_NAME = "console_chassis"

#: The bounded selection this gate replays: five journeys, then the 80-column route frames.
JOURNEY_IDS: tuple[str, ...] = ("J1", "J2", "J3", "J4", "J5")
FRAME_GLOBS: tuple[str, ...] = ("route/*@80",)


class _CountingApp(ConsoleApp):
    """A console that counts how many of itself the replay mounts."""

    mounted = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).mounted += 1
        super().__init__(*args, **kwargs)


class _CountingHarness(Harness):
    """A harness that counts the frames rendered through the one render entry point."""

    rendered = 0

    async def render(self, *args: Any, **kwargs: Any) -> Capture:
        type(self).rendered += 1
        return await super().render(*args, **kwargs)


@pytest.fixture(scope="module")
def contract() -> Contract:
    """Return the tracked contract, loaded strict."""
    return load_contract(GOLDEN_ROOT / "sequences")


@pytest.fixture(scope="module")
def bounded(contract: Contract) -> dict[str, Any]:
    """Replay the bounded selection once and return its results and instrumentation."""
    _CountingApp.mounted = 0
    _CountingHarness.rendered = 0
    layout = GoldenLayout.under(GOLDEN_ROOT)
    real_app, real_harness = harness_mod.ConsoleApp, harness_mod.Harness
    harness_mod.ConsoleApp = _CountingApp  # type: ignore[misc]
    harness_mod.Harness = _CountingHarness  # type: ignore[misc]
    try:
        results = asyncio.run(
            harness_mod.replay(layout, frame_globs=FRAME_GLOBS, journey_ids=JOURNEY_IDS)
        )
    finally:
        harness_mod.ConsoleApp = real_app  # type: ignore[misc]
        harness_mod.Harness = real_harness  # type: ignore[misc]
    journeys, states = select(contract, frame_globs=FRAME_GLOBS, journey_ids=JOURNEY_IDS)
    return {
        "results": results,
        "apps": _CountingApp.mounted,
        "renders": _CountingHarness.rendered,
        "journeys": journeys,
        "states": states,
    }


def _results(bounded: dict[str, Any]) -> list[Result]:
    results: list[Result] = bounded["results"]
    return results


def test_bounded_replay_selects_five_journeys_then_the_eighty_column_route_frames(
    bounded: dict[str, Any],
) -> None:
    results = _results(bounded)
    kinds = [result.kind for result in results]
    assert kinds[: len(JOURNEY_IDS)] == ["journey"] * len(JOURNEY_IDS)
    assert "journey" not in kinds[len(JOURNEY_IDS) :]
    assert [r.id for r in results[: len(JOURNEY_IDS)]] == list(JOURNEY_IDS)
    assert bounded["states"], "the 80-column route selection is empty"
    assert [r.id for r in results[len(JOURNEY_IDS) :]] == [s.id for s in bounded["states"]]


def test_bounded_replay_matches_every_selected_record(bounded: dict[str, Any]) -> None:
    failed = [
        f"{r.id}: {r.detail}\n exp |{visible(r.expected or '')}|\n got |{visible(r.actual or '')}|"
        for r in _results(bounded)
        if not r.ok
    ]
    assert not failed, "\n".join(failed)


def test_bounded_replay_mounts_exactly_one_console(bounded: dict[str, Any]) -> None:
    assert bounded["apps"] == 1


def test_every_frame_goes_through_the_one_render_entry_point(bounded: dict[str, Any]) -> None:
    assert bounded["renders"] == len(bounded["states"])


def test_every_replayed_frame_settles_in_one_cycle(bounded: dict[str, Any]) -> None:
    noisy = {r.id: r.settle_cycles for r in _results(bounded) if r.settle_cycles != 1}
    assert not noisy, f"frames needing more than one settle cycle: {noisy}"


def test_every_replayed_journey_step_settles_in_one_cycle(bounded: dict[str, Any]) -> None:
    noisy = [
        (r.id, step["step"], step["cycles"])
        for r in _results(bounded)
        if r.kind == "journey"
        for step in r.steps
        if step.get("cycles", 1) != 1
    ]
    assert not noisy, f"journey steps needing more than one settle cycle: {noisy}"


def test_every_replayed_journey_replays_every_recorded_step(bounded: dict[str, Any]) -> None:
    recorded = {journey.id: len(journey.steps) for journey in bounded["journeys"]}
    replayed = {r.id: len(r.steps) for r in _results(bounded) if r.kind == "journey"}
    assert replayed == recorded


def test_select_with_no_globs_selects_the_whole_contract(contract: Contract) -> None:
    journeys, states = select(contract)
    assert len(journeys) == len(contract.journeys)
    assert len(states) == len(contract.states)


def test_select_of_an_unmatched_glob_selects_nothing(contract: Contract) -> None:
    journeys, states = select(contract, frame_globs=["no/such/frame@1"], journey_ids=["J-none"])
    assert journeys == []
    assert states == []


def test_load_contract_raises_for_a_missing_sequences_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_contract(tmp_path / "absent")


def test_grid_errors_name_a_short_frame_a_narrow_row_and_an_escape() -> None:
    assert grid_errors("ab\ncd", (2, 2), [2, 2]) == []
    assert grid_errors("ab", (2, 2), [2]) == ["1 rows, not 2"]
    assert grid_errors("ab\ncd", (2, 2), [2, 1]) == ["row 1 is 1 cells, not 2"]
    assert grid_errors("a\x1bb\ncd", (2, 2), [2, 2]) == ["row 0 carries an escape sequence"]


def test_grid_errors_of_an_empty_frame_names_the_row_count() -> None:
    assert grid_errors("", (0, 0), []) == ["1 rows, not 0"]


def test_pilot_key_maps_a_named_key_a_shift_pair_and_a_bare_character() -> None:
    assert pilot_key("ArrowDown") == "down"
    assert pilot_key("Shift-Tab") == "shift+tab"
    assert pilot_key("a") == "a"


def test_visible_marks_every_space() -> None:
    assert visible("a b") == "a·b"
    assert visible("") == ""


def test_regeneration_refuses_to_write_over_the_tracked_sequences() -> None:
    layout = GoldenLayout.under(GOLDEN_ROOT)
    with pytest.raises(ValueError, match="never writes over the tracked sequences"):
        asyncio.run(harness_mod.regenerate(layout, layout.sequences))


def test_regeneration_refuses_a_target_that_does_not_exist(tmp_path: Path) -> None:
    layout = GoldenLayout.under(GOLDEN_ROOT)
    with pytest.raises(FileNotFoundError, match="does not exist"):
        asyncio.run(harness_mod.regenerate(layout, tmp_path / "absent"))


def test_a_setup_naming_a_frame_size_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ValidationError, match="less than"):
        SessionSetup(size=len(SIZES))


def _imported_names(source: str) -> list[str]:
    """Return every module named by an import statement in ``source``."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def _snapshot_modules() -> Sequence[Path]:
    return sorted(SNAPSHOT_PACKAGE.rglob("*.py"))


def test_the_test_only_chassis_package_is_gone() -> None:
    assert not CHASSIS_PACKAGE.exists()


def test_no_snapshot_module_imports_the_removed_chassis() -> None:
    modules = _snapshot_modules()
    assert modules, "the console snapshot package holds no module"
    offenders = [
        path.relative_to(TESTS_ROOT).as_posix()
        for path in modules
        if any(CHASSIS_NAME in name for name in _imported_names(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []


def test_no_snapshot_module_names_the_removed_chassis_at_all() -> None:
    offenders = [
        path.relative_to(TESTS_ROOT).as_posix()
        for path in _snapshot_modules()
        if CHASSIS_NAME in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_epoch_one_stylesheet_rules_stay() -> None:
    tcss = THEME_TCSS.read_text(encoding="utf-8")
    assert [rule for rule in EPOCH1_SELECTORS if rule not in tcss] == []
