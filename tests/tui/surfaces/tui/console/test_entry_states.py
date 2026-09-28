"""The process layer drawn from the real attach path: CON-001 to CON-009, CON-011, CON-012.

Every state here is reached through :func:`eawf.surfaces.tui.launch.launch_tui` exactly as
``eawf tui`` reaches it, over a temporary tree, a temporary machine registry and a real
condition -- an unregistered root, two workspaces claiming one root, a declared tree with
no marker, a stopped cutover journal, a marker or state document written by a newer
console. Only the event loop is stood in for (``_run_console`` hands back the app it was
given), and for the attached case the daemon behind the seam's client. No fixture row is
edited to draw a state.

CON-124 closes the file: the first run lists its four steps under ``STEP``, ``STATE``
and ``WHY IT IS NEEDED``, and a skipped step says what stays unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
import typer

import eawf.surfaces.tui.chassis.state_binding as state_binding
import eawf.surfaces.tui.launch as launch
from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    GENERATIONS_DIRNAME,
    MARKER_FILENAME,
)
from eawf.kernel.migration.epoch2.journal import CutoverStage
from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, build_route_projection
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE
from eawf.surfaces.cli.app import app as cli_app
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.attach import (
    AttachStep,
    ambiguous_state,
    failed_state,
    resolving_state,
)
from eawf.surfaces.tui.console.chrome import EntryState, load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.registry import ENTRY_STATE_IDS, ENTRY_STATES
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES
from eawf.surfaces.tui.console.tokens import CONNECTION

CODE = "ABC"
AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_GENERATION_ID = "gen-" + "ab" * 8
_DIGEST = "cd" * 32
#: The keys a pre-session frame may bind: movement, copying a command, leaving.
_SAFE_KEY_LABELS = frozenset(
    {
        "candidate",
        "path",
        "choice",
        "step",
        "copy the select command",
        "copy the register command",
        "copy the export command",
        "show the exact command",
        "cancel · not attached",
        "quit · exit 4",
        "exit",
        "do it",
        "skip",
    }
)


# ---------- the real conditions ----------


def _declare(ea: Path) -> None:
    """Declare ``ea`` a disposable epoch-2 canary with no marker yet."""
    (ea / GENERATIONS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (ea / CANARY_DECLARATION_FILENAME).write_text(
        json.dumps({"disposable": True, "declared_by": "test", "purpose": "entry states"})
    )


def _marker(ea: Path, **overrides: Any) -> None:
    """Write the epoch marker, activating the declared tree unless ``overrides`` break it."""
    payload = {
        "schema_version": "1",
        "epoch": 2,
        "generation_id": _GENERATION_ID,
        "manifest_digest": _DIGEST,
        "generation_digest": _DIGEST,
        "written_at": "2026-09-26T00:00:00Z",
        **overrides,
    }
    (ea / GENERATIONS_DIRNAME / MARKER_FILENAME).write_text(json.dumps(payload))


def _journal(ea: Path, stages: list[CutoverStage]) -> None:
    """Write a cutover journal whose apply reached ``stages``, in order."""
    rows = [
        json.dumps(
            {
                "schema_version": "1",
                "sequence": i,
                "stage": stage.value,
                "boundary": "plan_only",
                "recorded_at": "2026-09-26T14:02:11Z",
                "detail": f"stage {stage.value}",
                "previous_digest": "epoch2-cutover-journal",
                "digest": _DIGEST,
            }
        )
        for i, stage in enumerate(stages, start=1)
    ]
    (ea / GENERATIONS_DIRNAME / "journal.jsonl").write_text("\n".join(rows) + "\n")


def _registry(home: Path, root: Path, workspaces: dict[str, str]) -> None:
    """Register ``root`` as project ``CODE`` and one workspace per key, all claiming it."""
    path = home / ".eawf" / "registry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": "1",
                "repos": {
                    CODE: {"code": CODE, "path": str(root), "last_seen": "2026-09-20T09:41:00Z"}
                },
                "workspaces": {
                    key: {
                        "key": key,
                        "title": title,
                        "member_project_codes": [CODE],
                        "home_project_code": CODE,
                        "updated_at": "2026-07-02T00:00:00Z",
                    }
                    for key, title in workspaces.items()
                },
            }
        )
    )


def _digests(root: Path) -> dict[str, str]:
    """Return every file under ``root`` by its digest, to prove nothing was written."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """A machine with an empty home, a repository root to stand in, and a caught console."""
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.delenv(launch.WORKSPACE_KEY_ENV, raising=False)
    monkeypatch.chdir(repo)
    yield SimpleNamespace(home=home, repo=repo, ea=repo / ".ea")


def _launch(
    monkeypatch: pytest.MonkeyPatch, *, tty: bool = True, **kwargs: Any
) -> tuple[int, ConsoleApp | None]:
    """Launch ``eawf tui`` bare, the way an operator standing in the repository does."""

    class _Stdout:
        @staticmethod
        def isatty() -> bool:
            return tty

    monkeypatch.setattr(launch, "sys", SimpleNamespace(stdout=_Stdout(), stderr=sys.stderr))
    runs: list[ConsoleApp] = []

    def _caught(app: ConsoleApp, _seam: object) -> int:
        runs.append(app)
        return 0

    monkeypatch.setattr(launch, "_run_console", _caught)
    rc = launch.launch_tui(workspace=None, no_input=False, plain=False, **kwargs)
    return rc, (runs[0] if runs else None)


def _state(app: ConsoleApp) -> EntryState:
    assert app.session.route == "entry"
    return app.fixture.proto.entry[app.session.entry_sel]


def _frame(app: ConsoleApp, size: int, *, linked: bool = False) -> list[str]:
    w, h = SIZES[size]
    view = View(session=app.session, fixture=app.fixture, w=w, h=h, linked=linked)
    return render_route(view)


def _text(rows: list[str]) -> str:
    return "\n".join(rows)


def _land(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, state_id: str) -> ConsoleApp:
    """Build the real condition behind ``state_id`` and land a launch in it."""
    if state_id == "failed":
        _declare(world.ea)
        _marker(world.ea)
        _registry(world.home, world.repo / "elsewhere", {"DEF": "def"})
    elif state_id == "ambiguous":
        _declare(world.ea)
        _marker(world.ea)
        _registry(world.home, world.repo, {"DEF": "def-core", "GHI": "ghi-research"})
    elif state_id == "onboarding":
        _declare(world.ea)
        _marker(world.ea)
    elif state_id == "migration":
        _declare(world.ea)
        _registry(world.home, world.repo, {"DEF": "def"})
    elif state_id == "interrupted":
        _declare(world.ea)
        _registry(world.home, world.repo, {"DEF": "def"})
        _journal(world.ea, [CutoverStage.FENCE_CLEARED, CutoverStage.WORKSPACE_RESOLVED])
    elif state_id == "schema":
        _declare(world.ea)
        _marker(world.ea, schema_version="2")
        _registry(world.home, world.repo, {"DEF": "def"})
    else:
        raise AssertionError(f"no real condition for {state_id}")
    _rc, app = _launch(monkeypatch)
    assert app is not None
    assert _state(app).id == state_id
    return app


#: The states the attach path lands in from a real condition, and whether each ends the
#: process.
LANDED = {
    "failed": True,
    "ambiguous": False,
    "migration": True,
    "interrupted": True,
    "schema": True,
    "onboarding": False,
}


# ---------- CON-001: a closed set of eight, each drawn from the attach path ----------


def test_con_001_the_process_layer_is_a_closed_set_of_eight() -> None:
    assert len(ENTRY_STATES) == 8
    assert ENTRY_STATE_IDS == (
        "resolving",
        "ambiguous",
        "failed",
        "migration",
        "interrupted",
        "schema",
        "offline",
        "onboarding",
    )


@pytest.mark.parametrize("state_id", sorted(LANDED))
def test_con_001_a_real_condition_lands_the_launch_in_its_state(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, state_id: str
) -> None:
    app = _land(world, monkeypatch, state_id)
    state = _state(app)
    assert state.id in ENTRY_STATE_IDS
    assert (state.exit == "terminal") is LANDED[state_id]
    for size, (_w, h) in enumerate(SIZES):
        rows = _frame(app, size)
        assert len(rows) == h
        assert rows[1].strip() == state.title


def test_con_001_the_resolving_state_is_drawn_by_the_attached_launch(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    _marker(world.ea)
    _registry(world.home, world.repo, {"DEF": "def"})
    rc, app = _launch(monkeypatch)
    assert rc == 0 and app is not None and app.seam is not None
    rows = _frame(app, 0, linked=True)
    assert rows[0].endswith("◈ RESOLVING")


# ---------- CON-002: the header carries a process value, never a connection value ----------


@pytest.mark.parametrize("state_id", sorted(LANDED))
def test_con_002_the_header_slot_carries_the_process_value(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, state_id: str
) -> None:
    app = _land(world, monkeypatch, state_id)
    spec = next(s for s in ENTRY_STATES if s.id == state_id)
    for size in range(len(SIZES)):
        header = _frame(app, size)[0]
        assert header.endswith(f"{_state(app).glyph} {spec.label.upper()}")
        assert not any(header.endswith(value) for value in CONNECTION)
        assert "NEEDS YOU" not in header


def test_con_002_first_run_reads_first_run_not_a_connection_value(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _land(world, monkeypatch, "onboarding")
    assert _frame(app, 0)[0].endswith("◈ FIRST RUN")


# ---------- CON-003: the resolving frame enumerates its steps ----------


def test_con_003_resolving_lists_each_step_its_result_and_a_derived_elapsed(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    _marker(world.ea)
    _registry(world.home, world.repo, {"DEF": "def"})
    _rc, app = _launch(monkeypatch)
    assert app is not None
    narrow = _text(_frame(app, 0, linked=True))
    assert f"registered root           matched · {CODE}" in narrow
    assert "workspace                 resolved · DEF" in narrow
    assert f"project membership        member · {CODE}" in narrow
    assert "daemon                    contacted · waiting on the first projection" in narrow
    assert "elapsed                   ~" in narrow
    assert "Nothing has been read and nothing has been changed." in narrow
    assert "READS" not in narrow
    assert "the registry only" in _text(_frame(app, 1, linked=True))


@pytest.mark.parametrize("elapsed", [-0.1])
def test_con_003_a_negative_elapsed_is_refused(elapsed: float) -> None:
    with pytest.raises(ValueError, match="negative"):
        resolving_state(load_chrome(), (AttachStep(name="x", result="y"),), elapsed=elapsed)


def test_con_003_a_resolving_frame_with_no_step_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        resolving_state(load_chrome(), (), elapsed=0.0)


def test_con_003_a_single_step_resolving_frame_heads_the_step_column() -> None:
    state = resolving_state(load_chrome(), (AttachStep(name="x", result="y"),), elapsed=0.0)
    assert state.panes is not None and state.panes[0][0] == "STEP"


# ---------- CON-004: no frame progresses by guessing ----------


def test_con_004_a_parent_registration_never_matches_a_child_root(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    _marker(world.ea)
    _registry(world.home, world.repo.parent, {"DEF": "def"})
    _rc, app = _launch(monkeypatch)
    assert app is not None
    state = _state(app)
    assert state.id == "failed"
    text = _text(_frame(app, 0))
    assert "Parent" in text and "never scanned" in text
    assert "workspace_not_registered" not in text


def test_con_004_an_explicit_tree_wins_outright_without_the_registry(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    _marker(world.ea)
    monkeypatch.setenv("EA_STATE", str(world.ea / "state.json"))
    rc, app = _launch(monkeypatch)
    assert rc == 0 and app is not None and app.seam is not None
    assert "named outright · EA_STATE" in _text(_frame(app, 0, linked=True))


def test_con_004_a_named_workspace_is_never_swapped_for_another(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    _marker(world.ea)
    _registry(world.home, world.repo, {"DEF": "def"})
    monkeypatch.setenv(launch.WORKSPACE_KEY_ENV, "ZZZ")
    _rc, app = _launch(monkeypatch)
    assert app is not None
    assert _state(app).id == "failed"
    assert "ZZZ" in _text(_frame(app, 0))


def test_con_004_an_unparseable_registry_fails_rather_than_assumes(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    _marker(world.ea)
    (world.home / ".eawf").mkdir()
    (world.home / ".eawf" / "registry.json").write_text("not json")
    _rc, app = _launch(monkeypatch)
    assert app is not None
    state = _state(app)
    assert state.id == "failed"
    for line in state.commands:
        _declared(shlex.split(line)[1:])


# ---------- CON-005: ambiguous resolution offers exactly two exits ----------


def test_con_005_ambiguous_offers_select_or_cancel_and_says_the_pick_is_session_local(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _land(world, monkeypatch, "ambiguous")
    state = _state(app)
    assert [label for _key, label in state.keys] == [
        "candidate",
        "copy the select command",
        "cancel · not attached",
    ]
    assert state.commands == ("eawf workspace select DEF", "eawf workspace select GHI")
    narrow = _text(_frame(app, 0))
    assert "LAST SESSION" in narrow and "Sep 20" in narrow
    assert "session only" in narrow
    assert "--workspace" in narrow
    assert str(world.repo) not in narrow
    wide = _text(_frame(app, 2))
    assert str(world.repo) in wide
    assert "legal" in wide


def test_con_005_enter_copies_the_select_command_and_escape_cancels(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _land(world, monkeypatch, "ambiguous")

    async def drive() -> tuple[list[str], bool]:
        async with app.run_test(size=SIZES[0]) as pilot:
            app.press_key("ArrowDown")
            app.press_key("Enter")
            notes = [row.note for row in app.session.log]
            app.press_key("Escape")
            await pilot.pause()
            return notes, app.is_running

    notes, running = asyncio.run(drive())
    assert any("copied: eawf workspace select GHI" in note for note in notes)
    assert running is False


def test_con_005_one_candidate_is_not_an_ambiguity() -> None:
    from eawf.platform.registry import Registry

    with pytest.raises(ValueError, match="two candidates"):
        ambiguous_state(load_chrome(), Registry(), ["DEF"], Path("root"))


# ---------- CON-006: every terminal state hands over a runnable command ----------


def _declared(argv: list[str]) -> None:
    """Assert ``argv`` names a subcommand path and options the CLI declares."""
    command: click.Command = typer.main.get_command(cli_app)
    words = list(argv)
    while words and isinstance(command, click.Group) and words[0] in command.commands:
        command = command.commands[words.pop(0)]
    if isinstance(command, click.Group):
        assert command.invoke_without_command, f"{argv} stops at a group"
    options = {opt for param in command.params for opt in getattr(param, "opts", [])}
    for word in words:
        if word.startswith("--"):
            assert word in options, f"{word} is not an option of {command.name}"


@pytest.mark.parametrize("state_id", sorted(s for s, terminal in LANDED.items() if terminal))
def test_con_006_every_terminal_command_is_one_the_cli_declares(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, state_id: str
) -> None:
    state = _state(_land(world, monkeypatch, state_id))
    assert state.commands
    for line in state.commands:
        words = shlex.split(line.replace("<", "").replace(">", ""))
        assert words[0] == "eawf"
        _declared(words[1:])


@pytest.mark.parametrize("state_id", sorted(s for s, terminal in LANDED.items() if terminal))
def test_con_006_a_terminal_state_leaves_its_command_on_stderr(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state_id: str,
) -> None:
    state = _state(_land(world, monkeypatch, state_id))
    capsys.readouterr()
    rc, _app = _launch(monkeypatch)
    assert rc == launch.TERMINAL_ENTRY_EXIT_CODE
    assert state.commands[0] in capsys.readouterr().err


def test_con_006_off_a_tty_a_terminal_state_prints_the_command_and_exits_4(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _declare(world.ea)
    monkeypatch.setenv("EA_STATE", str(world.ea / "state.json"))
    rc, app = _launch(monkeypatch, tty=False)
    assert rc == 4 and app is None
    err = capsys.readouterr().err
    assert f"eawf migrate epoch2 --rollback --target-root {world.ea}" in err


def test_con_006_enter_copies_the_command_under_the_cursor(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _land(world, monkeypatch, "migration")

    async def drive() -> tuple[list[str], bool]:
        async with app.run_test(size=SIZES[0]) as pilot:
            app.press_key("ArrowDown")
            app.press_key("Enter")
            notes = [row.note for row in app.session.log]
            app.press_key("Escape")
            await pilot.pause()
            return notes, app.is_running

    notes, running = asyncio.run(drive())
    assert any("copied: eawf backup create" in note for note in notes)
    assert running is False


def test_con_006_a_failed_frame_without_a_command_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one command"):
        failed_state(load_chrome(), ("why",), ())


# ---------- CON-007: migration never auto-applies ----------


def test_con_007_migration_orders_its_paths_and_applies_nothing(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    _registry(world.home, world.repo, {"DEF": "def"})
    before = _digests(world.repo)
    _rc, app = _launch(monkeypatch)
    assert app is not None
    assert _digests(world.repo) == before
    rows = _frame(app, 0)
    text = _text(rows)
    pane = next(row for row in rows if row.startswith(" MIGRATION"))
    assert "never auto-applies" in pane
    order = [text.index(f"{i}  {name}") for i, name in enumerate(PATH_ORDER, start=1)]
    assert order == sorted(order)
    assert "Every normal command is refused until this finishes." in text


PATH_ORDER = ("dry run", "backup", "apply", "rollback")


def test_con_007_a_rolled_back_journal_owes_the_migration_again(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    _registry(world.home, world.repo, {"DEF": "def"})
    _journal(world.ea, [CutoverStage.FENCE_CLEARED, CutoverStage.SURFACES_RESTORED])
    _rc, app = _launch(monkeypatch)
    assert app is not None
    assert _state(app).id == "migration"


# ---------- CON-008: interrupted migration names the authoritative generation ----------


def test_con_008_before_selection_generation_one_rules_and_resume_or_discard(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _land(world, monkeypatch, "interrupted")
    text = _text(_frame(app, 0))
    assert "authoritative now         generation 1 · unchanged" in text
    assert "stage 2 of 12 · 14:02:11" in text
    assert "last stage done           workspace resolved" in text
    assert "resume" in text and "discard" in text
    assert "Both generations are never written at once." in text


def test_con_008_after_selection_the_exit_is_a_reconcile(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    _registry(world.home, world.repo, {"DEF": "def"})
    _journal(world.ea, [CutoverStage.FENCE_CLEARED, CutoverStage.GENERATION_SELECTED])
    _rc, app = _launch(monkeypatch)
    assert app is not None
    text = _text(_frame(app, 0))
    assert "authoritative now" in text and "generation 1" in text
    assert "reconcile" in text and "resume" not in text


def test_con_008_a_damaged_marker_is_interrupted_not_unsupported(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    (world.ea / GENERATIONS_DIRNAME / MARKER_FILENAME).write_text("not json")
    _registry(world.home, world.repo, {"DEF": "def"})
    _rc, app = _launch(monkeypatch)
    assert app is not None
    assert _state(app).id == "interrupted"
    assert "authoritative now" in _text(_frame(app, 0))


def test_con_008_an_unparseable_journal_is_still_interrupted(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    (world.ea / GENERATIONS_DIRNAME / "journal.jsonl").write_text("{broken\n")
    _registry(world.home, world.repo, {"DEF": "def"})
    _rc, app = _launch(monkeypatch)
    assert app is not None
    assert _state(app).id == "interrupted"


# ---------- CON-009: schema unsupported refuses and offers the export ----------


def test_con_009_a_newer_marker_is_refused_with_the_export_offered(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _land(world, monkeypatch, "schema")
    text = _text(_frame(app, 0))
    assert "workspace writes          marker schema v2" in text
    assert "written by                a console newer than eawf" in text
    assert "--export" in text
    assert "Upgrading the console is the only way to attach." in text
    assert _state(app).keys[0][1] == "copy the export command"


def test_con_009_a_newer_state_schema_on_an_epoch1_tree_is_refused(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.ea.mkdir()
    (world.ea / "state.json").write_text(json.dumps({"schema_version": "99.0"}))
    called: list[object] = []

    def _epoch1(**kwargs: object) -> int:
        called.append(kwargs)
        return 0

    monkeypatch.setattr(launch, "_launch_epoch1", _epoch1)
    _rc, app = _launch(monkeypatch)
    assert called == [] and app is not None
    assert _state(app).id == "schema"
    assert "state schema v99.0" in _text(_frame(app, 0))


@pytest.mark.parametrize("version", ["1.20", "1.0", "not-a-version", ""])
def test_con_009_a_readable_or_unparseable_state_schema_is_not_refused(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    world.ea.mkdir()
    (world.ea / "state.json").write_text(json.dumps({"schema_version": version}))
    monkeypatch.setattr(launch, "_launch_epoch1", lambda **_kw: 0)
    rc, app = _launch(monkeypatch)
    assert rc == 0 and app is None


# ---------- CON-011: no claim of completeness, no control that mutates ----------


@pytest.mark.parametrize("state_id", sorted(LANDED))
def test_con_011_no_frame_claims_a_complete_count_or_binds_a_mutating_key(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, state_id: str
) -> None:
    app = _land(world, monkeypatch, state_id)
    before = _digests(world.repo)
    state = _state(app)
    assert {label for _key, label in state.keys} <= _SAFE_KEY_LABELS
    for size in range(len(SIZES)):
        text = _text(_frame(app, size)).lower()
        assert "complete" not in text
    _launch(monkeypatch)
    assert _digests(world.repo) == before


# ---------- CON-012: until a projection exists the layer owns the frame ----------

_WRITES: list[str] = []


class _Daemon:
    """Serves route reads, standing in for the socket client."""

    def __init__(self, **_options: Any) -> None:
        return None

    def __enter__(self) -> _Daemon:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        reads = {READ_METHOD_TEMPLATE.format(route=r): r for r in ROUTE_COLLECTIONS}
        if method not in reads:
            _WRITES.append(method)
            return {}
        return build_route_projection(
            route=reads[method], document={}, cursor=1, scope_id=CODE, generated_at=AT
        ).model_dump(mode="json")


def test_con_012_the_happy_path_draws_the_layer_then_lands_on_scope_home(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(world.ea)
    _marker(world.ea)
    _registry(world.home, world.repo, {"DEF": "def"})
    monkeypatch.setattr(state_binding, "DaemonClient", _Daemon)
    rc, app = _launch(monkeypatch)
    assert rc == 0 and app is not None and app.seam is not None
    seam = app.seam
    app.console_clock = FakeClock()
    before = _frame(app, 0, linked=True)

    async def drive() -> list[str]:
        async with app.run_test(size=SIZES[0]) as pilot:
            await seam.sync()
            app.render_frame()
            await pilot.pause()
            return list(app.frame_rows)

    after = asyncio.run(drive())
    assert app.session.route == "scope.home"
    assert before[0].endswith("◈ RESOLVING")
    assert "NEEDS YOU" not in before[0]
    assert before[-1].strip().startswith("Esc cancel")
    assert "RESOLVING" not in after[0]
    assert _WRITES == []


# ---------- CON-124: the first run names the four things a session needs ----------


def test_con_124_the_first_run_lists_its_four_steps_under_step_state_why(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _land(world, monkeypatch, "onboarding")
    for size in range(len(SIZES)):
        rows = _frame(app, size)
        assert rows[0].rstrip().endswith("◈ FIRST RUN")
        assert "NEEDS YOU" not in rows[0]
        assert not any(rows[0].rstrip().endswith(value) for value in CONNECTION)
        head = next(i for i, row in enumerate(rows) if re.match(r"^ STEP\s+STATE\s+WHY IT", row))
        steps = [re.search(r"\d (\w+)", row) for row in rows[head + 1 : head + 5]]
        assert [m.group(1) if m else "" for m in steps] == [
            "workspace",
            "provider",
            "sandbox",
            "first",
        ]
        assert "No migration is applied and no agent is started here." in _text(rows)
        assert rows[-1].split()[:8] == ["↑↓", "step", "Enter", "do", "it", "s", "skip", "Esc"]


def test_con_124_skipping_a_step_says_what_stays_unavailable_and_changes_nothing(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _land(world, monkeypatch, "onboarding")
    before = _digests(world.repo)

    async def drive() -> tuple[list[str], bool]:
        async with app.run_test(size=SIZES[0]) as pilot:
            app.press_key("ArrowDown")
            app.press_key("s")
            app.press_key("Enter")
            notes = [row.note for row in app.session.log]
            app.press_key("Escape")
            await pilot.pause()
            return notes, app.is_running

    notes, running = asyncio.run(drive())
    assert any(
        "skipped step 2" in note and "every run verb stays refused" in note for note in notes
    )
    # the skip moved the cursor on to the sandbox step, whose Enter runs nothing here
    assert any("no declared command runs this step yet" in note for note in notes)
    assert running is False
    assert _digests(world.repo) == before
