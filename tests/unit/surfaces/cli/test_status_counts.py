"""Tests: ``eawf status`` reports the true open-backlog count, not a truncation.

``_open_backlog`` slices its preview to 10 rows so the status envelope stays
small. Deriving the human-readable count from that truncated preview instead
of the full live set is what made ``eawf status`` print "open backlog: 10"
against a state holding 68 open items. These tests pin the fix at two tiers:
the counting helper itself, and the command's wiring of that count into both
the JSON envelope and the text rendering.

The handler is driven in process (a constructed :class:`typer.Context` plus
:class:`~eawf.surfaces.cli.flags.GlobalFlags`) rather than through a CLI
runner, so the suite stays inside the unit tier.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import click
import orjson
import pytest
import typer

from eawf.kernel.state.enums import BacklogPriority, BacklogStatus, ProjectStatus, ScopeKind
from eawf.kernel.state.models import BacklogItem, CurrentPointers, Project, State
from eawf.surfaces.cli.commands.status import _open_backlog, _open_backlog_count, status
from eawf.surfaces.cli.flags import GlobalFlags

pytestmark = pytest.mark.unit

_CREATED_AT = datetime(2026, 5, 8, tzinfo=UTC)


def _backlog_item(index: int, *, status: BacklogStatus) -> BacklogItem:
    """Build one triaged backlog entry at *status*, keyed by a padded index."""
    return BacklogItem(
        id=f"B{index:04d}",
        scope_id="QR",
        title=f"Backlog item {index}",
        priority=BacklogPriority.P2,
        status=status,
        created_at=_CREATED_AT,
    )


def _empty_state() -> State:
    """Build a minimal valid state carrying one project and no backlog."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["workflow"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _state_with_backlog(
    *, open_count: int, in_progress_count: int = 0, closed_count: int = 0
) -> State:
    """Return a state carrying the given mix of backlog statuses."""
    items = [
        *(_backlog_item(i, status=BacklogStatus.OPEN) for i in range(open_count)),
        *(
            _backlog_item(1000 + i, status=BacklogStatus.IN_PROGRESS)
            for i in range(in_progress_count)
        ),
        *(_backlog_item(2000 + i, status=BacklogStatus.CLOSED) for i in range(closed_count)),
    ]
    payload = _empty_state().model_dump(mode="json")
    payload["backlog"] = {item.id: item.model_dump(mode="json") for item in items}
    return State.model_validate(payload)


# --- _open_backlog_count -------------------------------------------------


def test_open_backlog_count_is_zero_on_an_empty_backlog() -> None:
    """No backlog field at all counts as zero open items."""
    assert _open_backlog_count(_empty_state()) == 0


def test_open_backlog_count_matches_a_single_open_item() -> None:
    """One open item counts as one."""
    state = _state_with_backlog(open_count=1)
    assert _open_backlog_count(state) == 1


def test_open_backlog_count_includes_in_progress() -> None:
    """IN_PROGRESS is live work, so it counts alongside OPEN."""
    state = _state_with_backlog(open_count=2, in_progress_count=3)
    assert _open_backlog_count(state) == 5


def test_open_backlog_count_excludes_closed() -> None:
    """A CLOSED item is not live work and must not inflate the count."""
    state = _state_with_backlog(open_count=2, closed_count=6)
    assert _open_backlog_count(state) == 2


def test_open_backlog_count_exceeds_the_display_limit_of_ten() -> None:
    """The bug: a backlog deeper than the 10-row preview still reports true.

    ``_open_backlog`` truncates its preview to 10 rows; the count helper must
    not derive its answer from that truncated list.
    """
    state = _state_with_backlog(open_count=68)

    assert _open_backlog_count(state) == 68
    assert len(_open_backlog(state)) == 10


def test_open_backlog_count_at_exactly_the_display_limit() -> None:
    """Off-by-one boundary: exactly 10 open items, none truncated away."""
    state = _state_with_backlog(open_count=10)

    assert _open_backlog_count(state) == 10
    assert len(_open_backlog(state)) == 10


def test_open_backlog_count_one_past_the_display_limit() -> None:
    """Off-by-one boundary: 11 open items, one truncated out of the preview."""
    state = _state_with_backlog(open_count=11)

    assert _open_backlog_count(state) == 11
    assert len(_open_backlog(state)) == 10


# --- eawf status (end to end) ---------------------------------------------


def _write_state(workspace: Path, state: State) -> None:
    """Persist *state* to ``<workspace>/.ea/state.json``."""
    state_dir = workspace / ".ea"
    state_dir.mkdir(exist_ok=True)
    state_path = state_dir / "state.json"
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json"), option=orjson.OPT_INDENT_2))


@pytest.fixture(autouse=True)
def _no_state_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ``EA_STATE`` so the workspace flag decides which state is read.

    Gate runners export ``EA_STATE`` for their sandbox, and it outranks the
    workspace flag, so without this the handler reads the runner's state.
    """
    monkeypatch.delenv("EA_STATE", raising=False)


def _run_status(workspace: Path, *, json_output: bool, capsys: pytest.CaptureFixture[str]) -> str:
    """Run the status handler in process and return its captured stdout."""
    ctx = typer.Context(click.Command("status"))
    ctx.obj = GlobalFlags(json_output=json_output, workspace=workspace)
    status(ctx, workspace=None, scope=None, json_output=False)
    return capsys.readouterr().out


def test_status_json_reports_the_true_count_past_the_display_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The JSON envelope's ``open_backlog_count`` is not ``len(open_backlog)``."""
    _write_state(tmp_path, _state_with_backlog(open_count=68))

    stdout = _run_status(tmp_path, json_output=True, capsys=capsys)

    payload = orjson.loads(stdout)
    assert payload["open_backlog_count"] == 68
    assert len(payload["open_backlog"]) == 10


def test_status_text_reports_the_true_count_past_the_display_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The human-readable line prints the true total, not the preview length."""
    _write_state(tmp_path, _state_with_backlog(open_count=68))

    stdout = _run_status(tmp_path, json_output=False, capsys=capsys)

    assert "open backlog: 68 " in stdout
    assert "open backlog: 10 " not in stdout
