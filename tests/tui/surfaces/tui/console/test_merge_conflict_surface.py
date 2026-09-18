"""The Git surface and the conflict card draw delivery records, and write nothing.

Two routes leave the prototype registers here. ``git.pr`` renders the generations a Batch
has taken and ``merge.conflict`` renders one hunk of the frame a blocked attempt left
behind, both over rows a projection carried at one committed cursor.

Three claims are worth a test each.

The head of a Batch is positional. Every generation was selected when it was written, so
a reader that trusted the stored flag would draw a Batch with as many heads as it has
deliveries. The read model recomputes the flag from position and ignores the record's,
which is checked here against a ledger whose every line claims to be selected.

The card draws one hunk. A conflict spanning two files is one ordered run of hunks, the
card shows the one the cursor sits on, and the position counts across the whole conflict
rather than restarting per file. A cleared conflict keeps its frame and contributes no
hunk: the blockage is over, and drawing its regions would put an operator back on a
decision somebody already made.

Neither route writes a file. That is asserted structurally rather than by reading the
source: every write entry point reachable from Python -- ``open`` in a writing mode, the
``Path`` mutators, the ``os`` and ``shutil`` file verbs -- is replaced with one that
raises, and then every key either footer advertises is pressed in both the native and the
epoch-1 mode. :func:`test_the_write_guard_reds_on_a_real_write` is the proof the guard
has teeth, because a guard that cannot fail proves nothing about the code under it.
"""

from __future__ import annotations

import builtins
import io
import os
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.integration import (
    ConflictExitKind,
    IntegrationConflict,
    IntegrationGeneration,
)
from eawf.kernel.delivery.receipts import RevisionRefKind, canonical_digest
from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    ROUTE_READ_MODELS,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE, RECONNECT_METHOD_TEMPLATE
from eawf.kernel.projection.integration import (
    BASE_GENERATION,
    FIRST_INTEGRATED_GENERATION,
    GIT_PR_ROUTE,
    INTEGRATION_FIELDS,
    INTEGRATION_ROUTES,
    MERGE_CONFLICT_ROUTE,
    PULL_REQUEST_PRODUCER,
    GitPrReadModel,
    MergeConflictReadModel,
    authority_label,
    build_conflict_views,
    build_generation_rows,
    build_integration_view,
)
from eawf.kernel.projection.operations import build_operations_view
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.runtime.daemon.methods.projection import ROUTE_READ_METHODS, ROUTE_RECONNECT_METHODS
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import KEY_NAMES, ROUTE_KEYS
from eawf.surfaces.tui.console.keymap import route_keys
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import git_pr, merge_conflict, render_route
from eawf.surfaces.tui.console.renderers.read_model import native
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session

#: Where the tracked prototype registers live.
FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"

#: When the probe projections are stamped. The digest does not cover the stamp; a fixed
#: clock only keeps this suite's output reproducible.
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
LATER = AT + timedelta(minutes=5)

#: The scope every probe projection is built for.
SCOPE = "EAWF"

REPOSITORY = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0001"
TASK = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0004"
ACTION = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001"

#: One valid exit reference per exit kind, so the frame always names a way out.
EXIT_REFS: dict[ConflictExitKind, str] = {
    ConflictExitKind.REPAIR_TASK: TASK,
    ConflictExitKind.REBASE_TASK: TASK,
    ConflictExitKind.OPERATOR_DECISION: ACTION,
}

#: One Batch row, so the projection the routes are served is never empty by accident.
DOCUMENT: dict[str, Any] = {
    "batch": {
        "BAT-0001": {
            "urn": f"urn:eawf:{SCOPE}:batch:BAT-0001",
            "revision": 3,
            "status": "DELIVERING",
        }
    }
}


# ---------- probe records ----------


def _binding(generation: int, *, ref_kind: RevisionRefKind = RevisionRefKind.INTEGRATION) -> Any:
    """Return one revision binding of ``BATCH`` at ``generation``."""
    return {
        "repository_ref": REPOSITORY,
        "ref_kind": ref_kind,
        "head_sha": f"{generation % 10}" * 40,
        "tree_sha": "c" * 40,
        "parent_sha": None,
        "batch_ref": BATCH,
        "integration_generation": generation,
        "manifest_digest": canonical_digest("manifest"),
        "criteria_digest": canonical_digest("criteria"),
        "policy_digest": canonical_digest("policy"),
        "environment_digest": None,
        "bound_at": AT,
    }


def _generation(ordinal: int, **overrides: Any) -> IntegrationGeneration:
    """Return one generation at ``ordinal``, marked selected as every written line is."""
    payload: dict[str, Any] = {
        "id": f"ING-{ordinal:06d}",
        "batch_ref": BATCH,
        "generation": ordinal,
        "parent_generation_id": f"ING-{ordinal - 1:06d}" if ordinal > 1 else None,
        "source_candidate_bundle_id": f"CB-{ordinal:08d}",
        "source_base": _binding(ordinal - 1, ref_kind=RevisionRefKind.CANDIDATE),
        "target_base": _binding(ordinal - 1),
        "integrated_revision": _binding(ordinal),
        "patch_digest": canonical_digest("patch"),
        "diff_digest": canonical_digest("diff"),
        "tree_digest": canonical_digest("tree"),
        "changed_paths": ["src/pkg/loader.py"],
        "integration_policy_digest": canonical_digest("policy"),
        "selected": True,
        "created_at": AT,
    }
    return IntegrationGeneration.model_validate(payload | overrides)


def _side(**overrides: Any) -> dict[str, Any]:
    side: dict[str, Any] = {
        "authority": {"kind": "agent", "batch_ref": BATCH},
        "at": AT,
        "sha": "a" * 40,
        "lines": ["seq = acked_cursor(window)"],
    }
    return side | overrides


def _hunk(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "ours": _side(),
        "theirs": _side(
            authority={"kind": "principal", "principal_key": "OPERATOR"},
            sha="b" * 40,
            lines=["seq = arrival_order(window)"],
        ),
    }


def _conflict(**overrides: Any) -> IntegrationConflict:
    """Return one standing conflict over two files, three hunks in all."""
    payload: dict[str, Any] = {
        "id": "INC-000001",
        "attempt_id": "INA-000004",
        "batch_ref": BATCH,
        "repository_ref": REPOSITORY,
        "branch": "eawf/delivery/BAT-0001",
        "ahead": 2,
        "behind": 1,
        "files": [
            {"path": "src/pkg/loader.py", "hunks": [_hunk(1), _hunk(2)]},
            {"path": "src/pkg/replay.py", "hunks": [_hunk(1)]},
        ],
        "exit": {"kind": "repair_task", "ref": TASK},
    }
    return IntegrationConflict.model_validate(payload | overrides)


def _projection(route: str, *, cursor: int = 41208, document: Any = None) -> RouteProjection:
    """Return one route's projection over the probe document at ``cursor``."""
    return build_route_projection(
        route=route,
        document=DOCUMENT if document is None else document,
        cursor=cursor,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _view(route: str, **kwargs: Any) -> RouteReadModel:
    """Return the read model of ``route`` over the probe document."""
    return build_integration_view(_projection(route), **kwargs)


def _fixture() -> Fixture:
    """Return the tracked prototype registers the epoch-1 mode renders from."""
    return load_fixture(FIXTURE_ROOT)


def _frame(route: str, model: RouteReadModel | None, *, width: int = 120) -> list[str]:
    """Return the console frame ``route`` draws, natively or in the epoch-1 mode."""
    session = Session()
    session.route = route
    return render_route(View(session=session, fixture=_fixture(), w=width, h=30, projection=model))


def _app(route: str, **kwargs: Any) -> ConsoleApp:
    """Return a console bound to a seam already holding ``route``'s projection."""
    seam = ProjectionSeam(route=route, scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._projection = _projection(route)
    app = ConsoleApp(_fixture(), FakeClock(), seam=seam, **kwargs)
    app.session.route = route
    return app


# ---------- the console composes the read model it draws ----------


@pytest.mark.parametrize("route", INTEGRATION_ROUTES)
def test_the_console_composes_this_routes_read_model_from_the_seam(route: str) -> None:
    """The production call site: the app turns the held projection into the route's model."""
    model = _app(route).route_view()
    assert isinstance(model, RouteReadModel)
    assert model.route == route
    assert model.read_model is ROUTE_READ_MODELS[route]


def test_the_console_carries_its_generations_into_the_git_model() -> None:
    """The generations the console was given reach the Git surface, and no other route's."""
    model = _app(GIT_PR_ROUTE, integration_generations=(_generation(2),)).route_view()
    assert isinstance(model, GitPrReadModel)
    assert [row.key for row in model.generations] == ["ING-000002"]


def test_the_console_carries_its_conflicts_into_the_card_model() -> None:
    """The conflict frames the console was given reach the card, hunks and all."""
    model = _app(MERGE_CONFLICT_ROUTE, integration_conflicts=(_conflict(),)).route_view()
    assert isinstance(model, MergeConflictReadModel)
    assert [frame.key for frame in model.conflicts] == ["INC-000001"]
    assert len(model.hunks) == 3


def test_the_console_holds_no_integration_model_for_another_route() -> None:
    """The seam carries one route; another route's frame falls back to epoch one."""
    app = _app(GIT_PR_ROUTE)
    app.session.route = "unattended"
    assert app.route_view() is None


@pytest.mark.parametrize("route", INTEGRATION_ROUTES)
def test_every_bound_route_has_both_projection_verbs(route: str) -> None:
    """A route a console draws natively is one the daemon both reads and reconnects."""
    assert READ_METHOD_TEMPLATE.format(route=route) in ROUTE_READ_METHODS
    assert RECONNECT_METHOD_TEMPLATE.format(route=route) in ROUTE_RECONNECT_METHODS
    assert ROUTE_COLLECTIONS[route]


def test_the_family_names_exactly_the_routes_this_wave_binds() -> None:
    """The family table is the statement of what left the prototype registers."""
    assert INTEGRATION_ROUTES == (GIT_PR_ROUTE, MERGE_CONFLICT_ROUTE)
    assert set(INTEGRATION_FIELDS) == set(INTEGRATION_ROUTES)


@pytest.mark.parametrize("route", INTEGRATION_ROUTES)
def test_the_frame_prints_the_derived_counts_and_the_cursor(route: str) -> None:
    """Every count on the frame is one the view derived, and the cursor is beside it."""
    model = _view(route)
    body = "\n".join(_frame(route, model))
    assert "cursor 41,208" in body
    assert "1 batch" in body


# ---------- the head of a Batch is positional ----------


def test_the_newest_generation_is_the_head_however_the_lines_were_written() -> None:
    """Every written line claims to be selected; exactly the newest one is the head now."""
    rows = build_generation_rows([_generation(2), _generation(3), _generation(4)])
    assert [row.selected for row in rows] == [False, False, True]
    assert [row.ordinal for row in rows] == [2, 3, 4]


def test_a_single_generation_is_its_own_head() -> None:
    """The one-row boundary: a Batch with one delivery has that delivery as its head."""
    rows = build_generation_rows([_generation(FIRST_INTEGRATED_GENERATION)])
    assert [row.selected for row in rows] == [True]
    assert rows[0].ordinal == 2


def test_no_generation_at_all_yields_no_row_and_no_head() -> None:
    """The empty boundary: a Batch that has taken no delivery has no head to name."""
    model = _view(GIT_PR_ROUTE, generations=())
    assert isinstance(model, GitPrReadModel)
    assert model.generations == ()
    assert model.selected_generation() is None


def test_no_generation_record_exists_at_the_base_ordinal() -> None:
    """The off-by-one under the first delivery: ordinal one is a binding, not a record.

    The record refuses a target base that does not precede its own ordinal, and a base
    generation is a positive integer, so the lowest ordinal a generation can carry is
    two. That is why the Git surface counts every record it holds as a delivery.
    """
    with pytest.raises(ValidationError):
        _generation(BASE_GENERATION)
    assert FIRST_INTEGRATED_GENERATION == BASE_GENERATION + 1
    assert build_generation_rows([_generation(FIRST_INTEGRATED_GENERATION)])[0].ordinal == 2


def test_a_generation_row_carries_the_commit_and_what_it_touched() -> None:
    """A row states the integrated revision rather than deriving one from the ordinal."""
    row = build_generation_rows([_generation(3, changed_paths=["a.py", "b.py"])])[0]
    assert row.head_sha == "3" * 40
    assert row.changed_paths == ("a.py", "b.py")
    assert row.parent_key == "ING-000002"
    assert row.candidate_bundle_id == "CB-00000003"


def test_the_git_frame_marks_the_head_and_names_every_generation() -> None:
    """Every generation the read model holds reaches the frame, the head marked."""
    model = _view(GIT_PR_ROUTE, generations=(_generation(2), _generation(3)))
    body = "\n".join(_frame(GIT_PR_ROUTE, model))
    assert "ING-000002" in body
    assert "ING-000003 ◂ head" in body
    assert "head ING-000003" in body


def test_the_git_frame_says_so_when_the_batch_has_taken_nothing() -> None:
    """An empty history is stated; the frame never draws a Batch as delivered."""
    body = "\n".join(_frame(GIT_PR_ROUTE, _view(GIT_PR_ROUTE)))
    assert git_pr.NO_GENERATION in body


def test_the_git_frame_names_the_pull_request_producer_it_waits_on() -> None:
    """The review and check columns wait on a named item, not on a blank cell."""
    model = _view(GIT_PR_ROUTE)
    waiting = {spec.name: spec.missing_producer for spec in model.unproduced()}
    assert waiting == dict.fromkeys(("review", "checks"), PULL_REQUEST_PRODUCER)
    assert PULL_REQUEST_PRODUCER in "\n".join(_frame(GIT_PR_ROUTE, model))


# ---------- the card draws one hunk ----------


def test_the_hunks_of_every_file_are_one_run_numbered_from_one() -> None:
    """The card's position counts across the conflict, not across one file of it."""
    _frames, hunks = build_conflict_views([_conflict()])
    assert [hunk.position for hunk in hunks] == [1, 2, 3]
    assert [hunk.path for hunk in hunks] == [
        "src/pkg/loader.py",
        "src/pkg/loader.py",
        "src/pkg/replay.py",
    ]
    assert [hunk.file_index for hunk in hunks] == [1, 2, 1]


def test_the_card_draws_exactly_the_hunk_the_cursor_sits_on() -> None:
    """One hunk at a time: the other two files' regions are not on the frame."""
    model = _view(MERGE_CONFLICT_ROUTE, conflicts=(_conflict(),))
    session = Session()
    session.route = MERGE_CONFLICT_ROUTE
    rows = render_route(View(session=session, fixture=_fixture(), w=120, h=30, projection=model))
    body = "\n".join(rows)
    assert "hunk 1 of 3" in body
    assert "src/pkg/loader.py" in body
    assert "src/pkg/replay.py" not in body


def test_walking_the_run_moves_the_card_to_the_next_hunk() -> None:
    """The off-by-one walk: the second position draws the second hunk and only it."""
    model = _view(MERGE_CONFLICT_ROUTE, conflicts=(_conflict(),))
    session = Session()
    session.route = MERGE_CONFLICT_ROUTE
    session.sel = 2
    body = "\n".join(
        render_route(View(session=session, fixture=_fixture(), w=120, h=30, projection=model))
    )
    assert "hunk 3 of 3" in body
    assert "src/pkg/replay.py" in body
    assert "src/pkg/loader.py" not in body


def test_a_cursor_past_the_last_hunk_is_clamped_onto_it() -> None:
    """A cursor kept across a shrinking conflict lands on a hunk, never past the end."""
    model = _view(MERGE_CONFLICT_ROUTE, conflicts=(_conflict(),))
    session = Session()
    session.route = MERGE_CONFLICT_ROUTE
    session.sel = 99
    render_route(View(session=session, fixture=_fixture(), w=120, h=30, projection=model))
    assert session.sel == 2


def test_a_hunk_at_an_offset_no_hunk_holds_is_none() -> None:
    """The two ends of the run: below zero and past the last are both absent."""
    model = _view(MERGE_CONFLICT_ROUTE, conflicts=(_conflict(),))
    assert isinstance(model, MergeConflictReadModel)
    assert model.hunk_at(-1) is None
    assert model.hunk_at(3) is None
    assert model.hunk_at(2) is model.hunks[2]


def test_a_cleared_conflict_keeps_its_frame_and_draws_no_hunk() -> None:
    """The blockage is over, so the card never puts an operator back on that decision."""
    frames, hunks = build_conflict_views([_conflict(cleared_at=LATER)])
    assert [frame.cleared for frame in frames] == [True]
    assert hunks == ()


def test_no_conflict_at_all_says_so_rather_than_drawing_an_empty_box() -> None:
    """The empty boundary: a Batch that is not blocked states that it is not."""
    model = _view(MERGE_CONFLICT_ROUTE, conflicts=())
    assert isinstance(model, MergeConflictReadModel)
    assert model.hunks == ()
    assert merge_conflict.NO_CONFLICT in "\n".join(_frame(MERGE_CONFLICT_ROUTE, model))


def test_a_one_hunk_conflict_reads_as_one_of_one() -> None:
    """The single boundary below a multi-hunk conflict."""
    one = _conflict(files=[{"path": "src/pkg/loader.py", "hunks": [_hunk(1)]}])
    model = _view(MERGE_CONFLICT_ROUTE, conflicts=(one,))
    body = "\n".join(_frame(MERGE_CONFLICT_ROUTE, model))
    assert "hunk 1 of 1" in body
    assert "MERGE CONFLICT · 1 hunk" in body


def test_the_card_names_both_authorities_and_neither_choice() -> None:
    """Both sides carry who wrote them; neither is chosen and neither is retracted."""
    model = _view(MERGE_CONFLICT_ROUTE, conflicts=(_conflict(),))
    body = "\n".join(_frame(MERGE_CONFLICT_ROUTE, model))
    assert "the agent of BAT-0001" in body
    assert "OPERATOR" in body
    assert "Neither side is retracted and neither is chosen here." in body


def test_an_authority_is_a_typed_reference_and_never_a_display_name() -> None:
    """The two authorities a side may carry, each named by its own identity."""
    hunk = build_conflict_views([_conflict()])[1][0]
    assert hunk.ours.authority == "the agent of BAT-0001"
    assert hunk.theirs.authority == "OPERATOR"


def test_a_side_longer_than_the_card_states_how_many_lines_it_did_not_draw() -> None:
    """A long side is cut and says so; the card never silently truncates a hunk."""
    long_side = _side(lines=[f"line {n}" for n in range(1, 9)])
    wide = _conflict(
        files=[{"path": "src/pkg/loader.py", "hunks": [{**_hunk(1), "ours": long_side}]}]
    )
    body = "\n".join(_frame(MERGE_CONFLICT_ROUTE, _view(MERGE_CONFLICT_ROUTE, conflicts=(wide,))))
    assert f"… {8 - merge_conflict.SIDE_LINES} more lines" in body


# ---------- the frame always names a way out ----------


@pytest.mark.parametrize("kind", list(ConflictExitKind))
def test_the_card_prints_the_typed_exit_the_daemon_opened(kind: ConflictExitKind) -> None:
    """A conflict frame without a way out invites a hand edit of the workspace."""
    conflict = _conflict(exit={"kind": kind.value, "ref": EXIT_REFS[kind]})
    body = "\n".join(
        _frame(MERGE_CONFLICT_ROUTE, _view(MERGE_CONFLICT_ROUTE, conflicts=(conflict,)))
    )
    assert kind.value in body
    assert EXIT_REFS[kind] in body


def test_the_exit_kinds_are_exactly_the_three_the_daemon_opens() -> None:
    """The card re-spells no exit: the enum is the vocabulary."""
    assert {kind.value for kind in ConflictExitKind} == {
        "repair_task",
        "rebase_task",
        "operator_decision",
    }


def test_the_card_states_the_branch_and_how_far_the_candidate_diverged() -> None:
    """Ahead and behind are counted against the selected base, and the card says so."""
    body = "\n".join(
        _frame(MERGE_CONFLICT_ROUTE, _view(MERGE_CONFLICT_ROUTE, conflicts=(_conflict(),)))
    )
    assert "branch eawf/delivery/BAT-0001 · ahead 2 · behind 1" in body


# ---------- no verb on either route writes a file ----------


class WroteAFileError(AssertionError):
    """Raised the moment anything under the guard reaches a file-writing entry point."""


#: The ``open`` modes that can change a file. A read-only open is left alone, because the
#: console legitimately reads its fixture.
_WRITE_MODES = frozenset("wxa+")

#: The ``os.open`` flags that can change a file.
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

_PATH_WRITERS = ("write_text", "write_bytes", "mkdir", "touch", "unlink", "rename", "replace")
_OS_WRITERS = ("remove", "unlink", "rename", "replace", "mkdir", "makedirs", "rmdir", "truncate")
_SHUTIL_WRITERS = ("copy", "copy2", "copyfile", "copytree", "move", "rmtree")


@pytest.fixture
def no_writes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replace every file-writing entry point with one that raises.

    The guard is structural rather than a reading of the source: it does not matter which
    module a write would come from, because the entry point itself is gone for the
    duration. ``monkeypatch`` restores every one of them afterwards.
    """
    real_open = builtins.open
    real_os_open = os.open

    def refuse(what: str) -> Callable[..., Any]:
        def blocked(*_args: Any, **_kwargs: Any) -> Any:
            raise WroteAFileError(f"{what} was called, so something wrote a file")

        return blocked

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if set(mode) & _WRITE_MODES:
            raise WroteAFileError(f"open({file!r}, {mode!r}) would write a file")
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & _WRITE_FLAGS:
            raise WroteAFileError(f"os.open({path!r}) would write a file")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    for name in _PATH_WRITERS:
        monkeypatch.setattr(Path, name, refuse(f"Path.{name}"))
    for name in _OS_WRITERS:
        monkeypatch.setattr(os, name, refuse(f"os.{name}"))
    for name in _SHUTIL_WRITERS:
        monkeypatch.setattr(shutil, name, refuse(f"shutil.{name}"))
    yield


class _Host:
    """The dispatcher's host: a held clock and a quit that records it was asked."""

    def __init__(self) -> None:
        self.quits = 0
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record that the console was asked to end."""
        self.quits += 1


#: The dispatcher name of every key the bar prints under another name.
_DISPATCHER_NAME: dict[str, str] = {name: key for key, name in KEY_NAMES.items()}


def _advertised(route: str, fixture: Fixture) -> list[str]:
    """Return the dispatcher key name of every key ``route``'s footer advertises."""
    session = Session()
    session.route = route
    names: list[str] = []
    for entry in route_keys(session, fixture, route):
        for token in entry.keys:
            glyphs = list(token) if all(ch in _DISPATCHER_NAME for ch in token) else [token]
            names.extend(_DISPATCHER_NAME.get(glyph, glyph) for glyph in glyphs)
    return names


def _press_every_key(route: str, fixture: Fixture, model: RouteReadModel | None) -> list[str]:
    """Render ``route`` and press every key its footer advertises; return unclaimed keys."""
    unclaimed: list[str] = []
    for key in _advertised(route, fixture):
        session = Session()
        session.route = route
        session.subj_id = REGISTRY.by_id[route].fixed_subject
        render_route(View(session=session, fixture=fixture, w=120, h=30, projection=model))
        ctx = Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30)
        dispatch(ctx, key, False)
        if session.trace is None or session.trace.endswith("unclaimed"):
            unclaimed.append(f"{route}:{key}")
    return unclaimed


def test_the_write_guard_reds_on_a_real_write(
    tmp_path: Path,
    no_writes: None,
) -> None:
    """The guard has teeth: a guard that cannot fail proves nothing about the code under it."""
    with pytest.raises(WroteAFileError):
        (tmp_path / "probe.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WroteAFileError), builtins.open(tmp_path / "probe.txt", "w"):
        pass  # pragma: no cover - the open itself raises, so the body never runs
    with pytest.raises(WroteAFileError):
        os.open(str(tmp_path / "probe.txt"), os.O_CREAT | os.O_WRONLY)


@pytest.mark.parametrize("route", INTEGRATION_ROUTES)
def test_no_key_on_either_route_writes_a_file_in_the_native_mode(
    route: str, no_writes: None
) -> None:
    """Every advertised key is pressed with the write entry points gone, and none raises."""
    fixture = _fixture()
    model = _view(route, generations=(_generation(2),), conflicts=(_conflict(),))
    assert _press_every_key(route, fixture, model) == []


@pytest.mark.parametrize("route", INTEGRATION_ROUTES)
def test_no_key_on_either_route_writes_a_file_in_the_epoch_one_mode(
    route: str, no_writes: None
) -> None:
    """Binding the route must not have added a write to the prototype mode either."""
    assert _press_every_key(route, _fixture(), None) == []


@pytest.mark.parametrize("route", INTEGRATION_ROUTES)
def test_the_epoch_one_mode_resolves_every_key_its_footer_advertises(route: str) -> None:
    """A footer advertising a key nothing handles promises an action it does not have."""
    unclaimed = _press_every_key(route, _fixture(), None)
    assert not unclaimed, f"advertised but unhandled: {', '.join(unclaimed)}"


@pytest.mark.parametrize("route", INTEGRATION_ROUTES)
def test_the_native_mode_resolves_every_key_its_footer_advertises(route: str) -> None:
    """Native rendering is not a reason to start promising an action the route lacks."""
    model = _view(route, generations=(_generation(2),), conflicts=(_conflict(),))
    unclaimed = _press_every_key(route, _fixture(), model)
    assert not unclaimed, f"advertised but unhandled: {', '.join(unclaimed)}"


@pytest.mark.parametrize("route", INTEGRATION_ROUTES)
def test_each_footer_advertises_at_least_one_key(route: str) -> None:
    """A frame with no footer key is one an operator cannot leave."""
    assert _advertised(route, _fixture())
    assert ROUTE_KEYS[route]


def test_an_unadvertised_key_is_recorded_as_unclaimed() -> None:
    """The claim check has teeth: a key the route does not bind reads as unclaimed."""
    fixture = _fixture()
    session = Session()
    session.route = GIT_PR_ROUTE
    render_route(View(session=session, fixture=fixture, w=120, h=30))
    dispatch(Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30), "Q", False)
    assert session.trace is not None
    assert session.trace.endswith("unclaimed")


# ---------- refusals ----------


@pytest.mark.parametrize("route", INTEGRATION_ROUTES)
def test_an_integration_route_has_no_operations_read_model(route: str) -> None:
    """A projection of another family is not this frame's rows, so it is not adopted."""
    with pytest.raises(ValueError, match="has no operations read model"):
        build_operations_view(_projection(route))


def test_an_operations_route_has_no_integration_read_model() -> None:
    """The refusal runs both ways; neither family answers for the other."""
    with pytest.raises(ValueError, match="has no integration read model"):
        build_integration_view(_projection("crash.recovery"))


def test_a_read_model_for_another_route_is_not_adopted() -> None:
    """A frame drawn from another route's rows would show one route's records as another's."""
    model = _view(GIT_PR_ROUTE)
    session = Session()
    session.route = MERGE_CONFLICT_ROUTE
    view = View(session=session, fixture=_fixture(), w=120, h=30, projection=model)
    assert native(view) is None


def test_a_generation_bound_to_another_ordinal_is_refused_upstream() -> None:
    """The record itself will not carry a revision that binds a different generation."""
    with pytest.raises(ValidationError, match="binds generation"):
        _generation(3, integrated_revision=_binding(9))


def test_a_conflict_with_no_file_is_refused_upstream() -> None:
    """A conflict frame with nothing in it is not a frame the card could draw."""
    with pytest.raises(ValidationError):
        _conflict(files=[])


def test_an_authority_of_an_unknown_kind_is_refused_upstream() -> None:
    """A display name is not an authority; the union admits two typed references."""
    with pytest.raises(ValidationError):
        _conflict(
            files=[
                {
                    "path": "src/pkg/loader.py",
                    "hunks": [{**_hunk(1), "ours": _side(authority={"kind": "somebody"})}],
                }
            ]
        )


def test_an_unknown_authority_kind_raises_rather_than_guessing_a_label() -> None:
    """The label helper answers for the two kinds and refuses to invent a third."""
    with pytest.raises(AttributeError):
        authority_label(object())  # type: ignore[arg-type]


def test_a_negative_cursor_is_refused() -> None:
    """A cursor is a committed ordinal, so there is no projection before the first one."""
    with pytest.raises(ValueError, match="never -1"):
        _projection(GIT_PR_ROUTE, cursor=-1)


def test_a_row_names_no_field_the_route_did_not_declare() -> None:
    """Asking a row for an undeclared column raises rather than answering a blank."""
    with pytest.raises(KeyError):
        _view(GIT_PR_ROUTE).rows[0].field("approvals")


def test_an_empty_register_counts_zero_and_draws_no_row() -> None:
    """A register the route binds and that holds nothing counts zero, honestly."""
    model = build_integration_view(_projection(GIT_PR_ROUTE, document={}))
    assert model.counts == {"batch": 0}
    assert model.rows == ()
