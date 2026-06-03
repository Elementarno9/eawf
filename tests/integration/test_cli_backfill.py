"""End-to-end tests for the ``eawf backfill titles`` CLI command.

Drives the generalized entity-title backfill (P29-I07-W07) through
:class:`typer.testing.CliRunner` against a temporary ``.ea/state.json``
derived from the empty-repo fixture. The library-level sweep behaviour across
all five kinds is covered in ``tests/unit/test_title_backfill.py``; these tests
pin the CLI wiring: the default dry-run mutates nothing, ``--apply`` persists
and appends one event, the ``--kind`` filter narrows the sweep, and an unknown
``--kind`` exits ``USER_ERROR``.

A backlog item is the seed entity because ``backlog add`` is the one entity
``add`` verb without a lifecycle-ordering gate, so the test stays focused on
the backfill command rather than lifecycle plumbing.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.exit_codes import USER_ERROR

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
runner = CliRunner()


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    monkeypatch.setenv("EA_STATE", str(target))
    return target


def _add_backlog(item_id: str, title: str) -> None:
    """Seed one backlog item through the CLI."""
    result = runner.invoke(
        app,
        ["backlog", "add", item_id, "--title", title, "--priority", "P2"],
    )
    assert result.exit_code == 0, result.stdout


def test_backfill_titles_dry_run_reports_no_mutation(state_path: Path) -> None:
    """Default --dry-run reports the proposed change but leaves state intact."""
    _add_backlog("B023", "Trailing period title.")
    result = runner.invoke(app, ["--json", "backfill", "titles"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["changed"] == 1
    row = next(r for r in payload["rows"] if r["entity_id"] == "B023")
    assert row["kind"] == "backlog"
    assert row["before"] == "Trailing period title."
    assert row["after"] == "Trailing period title"
    # Dry-run mutated nothing on disk.
    body = json.loads(state_path.read_text())
    assert body["backlog"]["B023"]["title"] == "Trailing period title."


def test_backfill_titles_apply_persists_and_appends_event(state_path: Path) -> None:
    """--apply normalizes the title on disk and appends one event."""
    _add_backlog("B023", "Trailing period title.")
    result = runner.invoke(app, ["--json", "backfill", "titles", "--apply"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert payload["changed"] == 1
    body = json.loads(state_path.read_text())
    assert body["backlog"]["B023"]["title"] == "Trailing period title"
    events = (state_path.parent / "store" / "event.jsonl").read_text().splitlines()
    assert any("state.backfill_titles" in line for line in events)


def test_backfill_titles_kind_filter(state_path: Path) -> None:
    """--kind narrows the sweep to the named kind(s)."""
    _add_backlog("B023", "Trailing period title.")
    result = runner.invoke(app, ["--json", "backfill", "titles", "--kind", "backlog"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert {r["kind"] for r in payload["rows"]} == {"backlog"}
    # A filter that excludes the only seeded kind reports nothing to change.
    result_wave = runner.invoke(app, ["--json", "backfill", "titles", "--kind", "wave"])
    assert result_wave.exit_code == 0, result_wave.stdout
    payload_wave = json.loads(result_wave.stdout)
    assert payload_wave["total"] == 0


def test_backfill_titles_unknown_kind_exits_user_error(state_path: Path) -> None:
    """Error path: an unknown --kind value exits USER_ERROR."""
    result = runner.invoke(app, ["--json", "backfill", "titles", "--kind", "bogus"])
    assert result.exit_code == USER_ERROR, result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] == "UserError"
    assert payload["data"]["kind"] == "InvalidInput"


def test_cli_kind_names_match_library_entity_kinds() -> None:
    """The CLI's local kind list stays in lockstep with the library's canonical tuple.

    The CLI duplicates the kind names as plain strings to avoid pulling the
    heavy profiles import chain at app-build time; this guard fails if the two
    drift.
    """
    from eawf.platform.lint.tools.title_backfill import ENTITY_KINDS
    from eawf.surfaces.cli.commands.backfill import _KIND_NAMES

    assert _KIND_NAMES == ENTITY_KINDS
