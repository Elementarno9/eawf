"""End-to-end tests for the evidence CLI sub-apps.

Drives every command through :class:`typer.testing.CliRunner` against a
temporary ``.ea/state.json`` derived from the empty-repo fixture. The audit-
evidence guard is exercised across all five verdict-bearing commands
(``outcome set``, ``hypothesis verdict``, ``incident close``,
``backlog close``) — each must exit ``4`` (``VALIDATION_FAILED``) without an
``--audit`` of a complete audit, and exit ``0`` once a complete audit exists.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

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


def _seed_complete_audit(audit_id: str = "AUD-001") -> None:
    """Helper: run ``audit add`` against the test state with a report so the
    resulting audit lands in status=complete and unlocks verdict-bearing
    commands.

    Pre-seeds ``ART-<audit_id>`` via ``artifact add`` because ``add_audit``
    rejects a ``--report`` that does not point at an existing artifact.
    """
    artifact_id = f"ART-{audit_id}"
    art = runner.invoke(
        app,
        [
            "artifact",
            "add",
            artifact_id,
            "--kind",
            "audit_report",
            "--uri",
            f"repo:.ea/artifacts/{artifact_id}.md",
        ],
    )
    assert art.exit_code == 0, art.stdout

    result = runner.invoke(
        app,
        [
            "audit",
            "add",
            audit_id,
            "--scope-id",
            "QR",
            "--kind",
            "evaluation",
            "--report",
            artifact_id,
            "--verdict",
            "pass",
        ],
    )
    assert result.exit_code == 0, result.stdout


# ---- goal ------------------------------------------------------------------


def test_goal_define_writes_state_and_event(state_path: Path) -> None:
    result = runner.invoke(
        app,
        ["goal", "define", "G01", "--title", "Test goal"],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    assert body["goals"]["G01"]["title"] == "Test goal"
    events = (state_path.parent / "store" / "event.jsonl").read_text().splitlines()
    assert len(events) == 1


def test_goal_define_duplicate_exits_invalid_input(state_path: Path) -> None:
    runner.invoke(app, ["goal", "define", "G01", "--title", "t"])
    result = runner.invoke(app, ["goal", "define", "G01", "--title", "t2"])
    assert result.exit_code == 1  # INVALID_INPUT


# ---- outcome ---------------------------------------------------------------


def test_outcome_set_without_audit_exits_validation_failed(state_path: Path) -> None:
    runner.invoke(
        app,
        [
            "outcome",
            "define",
            "OUT-001",
            "--scope-id",
            "QR",
            "--metric",
            "sharpe",
            "--threshold",
            "1.0",
            "--direction",
            "min",
        ],
    )
    result = runner.invoke(
        app,
        [
            "outcome",
            "set",
            "OUT-001",
            "--value",
            "0.5",
            "--status",
            "missed",
            "--audit",
            "AUD-NOPE",
        ],
    )
    assert result.exit_code == 2
    assert "INV.AUDIT.UNKNOWN" in result.stdout


def test_outcome_set_with_complete_audit_succeeds(state_path: Path) -> None:
    runner.invoke(
        app,
        [
            "outcome",
            "define",
            "OUT-001",
            "--scope-id",
            "QR",
            "--metric",
            "sharpe",
            "--threshold",
            "1.0",
            "--direction",
            "min",
        ],
    )
    _seed_complete_audit("AUD-001")
    result = runner.invoke(
        app,
        [
            "outcome",
            "set",
            "OUT-001",
            "--value",
            "0.85",
            "--status",
            "missed",
            "--audit",
            "AUD-001",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    assert body["outcomes"]["OUT-001"]["status"] == "missed"
    assert body["outcomes"]["OUT-001"]["audit_id"] == "AUD-001"


# ---- hypothesis ------------------------------------------------------------


def test_hypothesis_verdict_without_audit_exits_validation_failed(
    state_path: Path,
) -> None:
    runner.invoke(
        app,
        [
            "hypothesis",
            "define",
            "H03-12",
            "--scope-id",
            "QR",
            "--text",
            "t",
            "--metric",
            "m",
            "--confirm",
            "c",
            "--reject",
            "r",
        ],
    )
    result = runner.invoke(
        app,
        [
            "hypothesis",
            "verdict",
            "H03-12",
            "--verdict",
            "confirmed",
            "--audit",
            "AUD-NOPE",
        ],
    )
    assert result.exit_code == 2


def test_hypothesis_verdict_happy_path(state_path: Path) -> None:
    runner.invoke(
        app,
        [
            "hypothesis",
            "define",
            "H03-12",
            "--scope-id",
            "QR",
            "--text",
            "t",
            "--metric",
            "m",
            "--confirm",
            "c",
            "--reject",
            "r",
        ],
    )
    _seed_complete_audit("AUD-001")
    result = runner.invoke(
        app,
        [
            "hypothesis",
            "verdict",
            "H03-12",
            "--verdict",
            "confirmed",
            "--audit",
            "AUD-001",
        ],
    )
    assert result.exit_code == 0
    body = json.loads(state_path.read_text())
    assert body["hypotheses"]["H03-12"]["verdict"] == "confirmed"


def test_hypothesis_list_filters(state_path: Path) -> None:
    for hid in ("H01-01", "H02-01"):
        runner.invoke(
            app,
            [
                "hypothesis",
                "define",
                hid,
                "--scope-id",
                "QR",
                "--text",
                "t",
                "--metric",
                "m",
                "--confirm",
                "c",
                "--reject",
                "r",
            ],
        )
    result = runner.invoke(app, ["--json", "hypothesis", "list", "--scope-id", "QR"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert {h["id"] for h in payload["hypotheses"]} == {"H01-01", "H02-01"}


# ---- audit -----------------------------------------------------------------


def test_audit_add_without_report_lands_pending(state_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "audit",
            "add",
            "AUD-001",
            "--scope-id",
            "QR",
            "--kind",
            "evaluation",
        ],
    )
    assert result.exit_code == 0
    body = json.loads(state_path.read_text())
    assert body["audits"]["AUD-001"]["status"] == "pending"


def test_audit_add_with_report_lands_complete(state_path: Path) -> None:
    _seed_complete_audit("AUD-001")
    body = json.loads(state_path.read_text())
    assert body["audits"]["AUD-001"]["status"] == "complete"
    assert body["audits"]["AUD-001"]["verdict"] == "pass"
    audit_lines = (state_path.parent / "store" / "audit.jsonl").read_text().splitlines()
    assert len(audit_lines) == 1


def test_audit_add_with_unknown_artifact_returns_exit_3(state_path: Path) -> None:
    """The orphan-ref guard surfaces as exit 3 (INVALID_INPUT) with the
    canonical error envelope, citing the unknown artifact id."""
    result = runner.invoke(
        app,
        [
            "--json",
            "audit",
            "add",
            "AUD-001",
            "--scope-id",
            "QR",
            "--kind",
            "evaluation",
            "--report",
            "BOGUS-001",
            "--verdict",
            "pass",
        ],
    )
    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    # Post C05 § 5.3 the envelope reports the new bucket name; legacy
    # ``InvalidInput`` surfaces through ``data.kind``.
    assert payload["error"] == "UserError"
    assert payload["exit_code"] == 1
    assert payload["exit_name"] == "USER_ERROR"
    assert payload["data"]["kind"] == "InvalidInput"
    assert "BOGUS-001" in payload["message"]
    body = json.loads(state_path.read_text())
    assert "AUD-001" not in (body.get("audits") or {})


def test_audit_run_stub_writes_record(state_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "audit",
            "run",
            "AUD-001",
            "--scope-id",
            "QR",
            "--kind",
            "evaluation",
        ],
    )
    assert result.exit_code == 0
    body = json.loads(state_path.read_text())
    assert body["audits"]["AUD-001"]["status"] == "complete"
    audit_lines = (state_path.parent / "store" / "audit.jsonl").read_text().splitlines()
    assert len(audit_lines) == 1


def test_audit_run_with_fixture(state_path: Path, tmp_path: Path) -> None:
    fixture = tmp_path / "checks.json"
    fixture.write_text(
        json.dumps([{"name": "lint", "passed": True}, {"name": "tests", "passed": True}])
    )
    result = runner.invoke(
        app,
        [
            "audit",
            "run",
            "AUD-001",
            "--scope-id",
            "QR",
            "--kind",
            "ship-gate",
            "--fixture",
            str(fixture),
        ],
    )
    assert result.exit_code == 0
    body = json.loads(state_path.read_text())
    assert body["audits"]["AUD-001"]["verdict"] == "pass"


def test_audit_integrity_appends(state_path: Path) -> None:
    _seed_complete_audit("AUD-001")
    result = runner.invoke(
        app,
        [
            "audit",
            "integrity",
            "AUD-001",
            "--check",
            "leakage",
            "--status",
            "passed",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    integrity = body["audits"]["AUD-001"]["integrity_results"]
    assert len(integrity) == 1
    assert integrity[0]["check"] == "leakage"
    assert integrity[0]["passed"] is True


def test_audit_show_unknown_exits_not_found(state_path: Path) -> None:
    result = runner.invoke(app, ["audit", "show", "AUD-DOES-NOT-EXIST"])
    assert result.exit_code == 1  # NOT_FOUND


def test_audit_set_verdict_lifts_pending(state_path: Path) -> None:
    """Happy path: pending audit + --report lifts to complete with verdict."""
    runner.invoke(
        app,
        [
            "artifact",
            "add",
            "ART-CLOSE",
            "--kind",
            "audit_report",
            "--uri",
            "repo:.ea/artifacts/close.md",
        ],
    )
    runner.invoke(
        app,
        ["audit", "add", "AUD-001", "--scope-id", "QR", "--kind", "evaluation"],
    )
    result = runner.invoke(
        app,
        [
            "audit",
            "set-verdict",
            "AUD-001",
            "--verdict",
            "pass",
            "--report",
            "ART-CLOSE",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    assert body["audits"]["AUD-001"]["status"] == "complete"
    assert body["audits"]["AUD-001"]["verdict"] == "pass"
    assert body["audits"]["AUD-001"]["report_artifact_id"] == "ART-CLOSE"
    audit_lines = (state_path.parent / "store" / "audit.jsonl").read_text().splitlines()
    assert len(audit_lines) == 2  # add + set-verdict
    events = (state_path.parent / "store" / "event.jsonl").read_text().splitlines()
    assert any("audit.set_verdict" in line for line in events)


def test_audit_set_verdict_pending_without_report_exits_invalid_input(
    state_path: Path,
) -> None:
    """Error path: pending audit + no --report exits 3 (INVALID_INPUT)."""
    runner.invoke(
        app,
        ["audit", "add", "AUD-001", "--scope-id", "QR", "--kind", "evaluation"],
    )
    result = runner.invoke(
        app,
        ["--json", "audit", "set-verdict", "AUD-001", "--verdict", "pass"],
    )
    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    # C05 § 5.3: ``InvalidInput`` folds to ``UserError``; legacy name in data.kind.
    assert payload["error"] == "UserError"
    assert payload["data"]["kind"] == "InvalidInput"
    assert "pending" in payload["message"]
    body = json.loads(state_path.read_text())
    assert body["audits"]["AUD-001"]["verdict"] is None
    assert body["audits"]["AUD-001"]["status"] == "pending"


def test_audit_list_filters(state_path: Path) -> None:
    _seed_complete_audit("AUD-001")
    runner.invoke(
        app,
        ["audit", "add", "AUD-002", "--scope-id", "QR", "--kind", "ship-gate"],
    )
    result = runner.invoke(app, ["--json", "audit", "list", "--kind", "evaluation"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert {a["id"] for a in payload["audits"]} == {"AUD-001"}


# ---- incident --------------------------------------------------------------


def test_incident_close_without_audit_exits_validation_failed(state_path: Path) -> None:
    runner.invoke(
        app,
        [
            "incident",
            "open",
            "INC-001",
            "--severity",
            "high",
            "--title",
            "leak",
        ],
    )
    result = runner.invoke(
        app,
        [
            "incident",
            "close",
            "INC-001",
            "--root-cause",
            "x",
            "--audit",
            "AUD-NOPE",
        ],
    )
    assert result.exit_code == 2


def test_incident_close_happy_path(state_path: Path) -> None:
    runner.invoke(
        app,
        [
            "incident",
            "open",
            "INC-001",
            "--severity",
            "high",
            "--title",
            "leak",
        ],
    )
    _seed_complete_audit("AUD-001")
    result = runner.invoke(
        app,
        [
            "incident",
            "close",
            "INC-001",
            "--root-cause",
            "config",
            "--audit",
            "AUD-001",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    assert body["incidents"]["INC-001"]["status"] == "resolved"
    incident_lines = (state_path.parent / "store" / "incident.jsonl").read_text().splitlines()
    # one open + one close
    assert len(incident_lines) == 2


def test_incident_view_unknown_exits_not_found(state_path: Path) -> None:
    result = runner.invoke(app, ["incident", "view", "INC-MISSING"])
    assert result.exit_code == 1


# ---- decision --------------------------------------------------------------


def test_decision_add_and_list(state_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "decision",
            "add",
            "D012",
            "--scope-id",
            "QR",
            "--summary",
            "Use phase-bundled PR",
            "--rationale",
            "Coupled refactor",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    assert body["decisions"]["D012"]["status"] == "active"
    decision_lines = (state_path.parent / "store" / "decision.jsonl").read_text().splitlines()
    assert len(decision_lines) == 1

    listed = runner.invoke(app, ["--json", "decision", "list", "--scope-id", "QR"])
    assert listed.exit_code == 0
    payload = json.loads(listed.stdout)
    assert payload["decisions"][0]["id"] == "D012"


def _add_decision(decision_id: str, summary: str) -> None:
    result = runner.invoke(
        app,
        [
            "decision",
            "add",
            decision_id,
            "--scope-id",
            "QR",
            "--summary",
            summary,
            "--rationale",
            "because",
        ],
    )
    assert result.exit_code == 0, result.stdout


def test_decision_supersede_flips_status_and_link(state_path: Path) -> None:
    _add_decision("D010", "old choice")
    _add_decision("D011", "new choice")

    result = runner.invoke(
        app,
        ["--json", "decision", "supersede", "D010", "--by", "D011"],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["decision_id"] == "D010"
    assert payload["superseded_by"] == "D011"
    assert payload["status"] == "superseded"

    body = json.loads(state_path.read_text())
    assert body["decisions"]["D010"]["status"] == "superseded"
    assert body["decisions"]["D010"]["superseded_by"] == "D011"
    # The superseding decision stays active.
    assert body["decisions"]["D011"]["status"] == "active"
    assert body["decisions"]["D011"]["superseded_by"] is None


def test_decision_supersede_unknown_old_errors(state_path: Path) -> None:
    _add_decision("D011", "new choice")
    result = runner.invoke(
        app,
        ["decision", "supersede", "D999", "--by", "D011"],
    )
    assert result.exit_code != 0
    assert "D999" in result.stdout
    assert "not found" in result.stdout


def test_decision_supersede_unknown_by_errors(state_path: Path) -> None:
    _add_decision("D010", "old choice")
    result = runner.invoke(
        app,
        ["decision", "supersede", "D010", "--by", "D999"],
    )
    assert result.exit_code != 0
    assert "D999" in result.stdout
    assert "not found" in result.stdout


# ---- artifact --------------------------------------------------------------


def test_artifact_add_and_show(state_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "artifact",
            "add",
            "ART-001",
            "--kind",
            "audit_report",
            "--uri",
            "repo:.ea/artifacts/audits/p13-i04.md",
            "--sha256",
            "a" * 64,
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    assert body["artifacts"]["ART-001"]["urn"] == "urn:eawf:v1:artifact:QR/ART-001"

    show = runner.invoke(app, ["--json", "artifact", "show", "ART-001"])
    assert show.exit_code == 0
    shown = json.loads(show.stdout)
    assert shown["id"] == "ART-001"
    assert shown["sha256"] == "a" * 64


def test_artifact_add_rejects_local_uri(state_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "artifact",
            "add",
            "ART-001",
            "--kind",
            "audit_report",
            "--uri",
            "file:///tmp/audit.md",
        ],
    )
    assert result.exit_code == 1
    body = json.loads(state_path.read_text())
    assert "ART-001" not in body["artifacts"]


def test_artifact_add_rejects_local_path_option(state_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "artifact",
            "add",
            "ART-001",
            "--kind",
            "audit_report",
            "--uri",
            "repo:.ea/artifacts/audits/p13-i04.md",
            "--local-path",
            "tmp/audit.md",
        ],
    )
    assert result.exit_code == 2


def test_artifact_show_unknown_exits_not_found(state_path: Path) -> None:
    result = runner.invoke(app, ["artifact", "show", "ART-MISSING"])
    assert result.exit_code == 1


# ---- backlog ---------------------------------------------------------------


def test_backlog_close_without_audit_exits_validation_failed(state_path: Path) -> None:
    runner.invoke(
        app,
        [
            "backlog",
            "add",
            "B023",
            "--title",
            "Split workflow",
            "--priority",
            "P1",
        ],
    )
    result = runner.invoke(
        app,
        [
            "backlog",
            "close",
            "B023",
            "--resolution",
            "done",
            "--commit",
            "abc",
            "--audit",
            "AUD-NOPE",
        ],
    )
    assert result.exit_code == 2


def test_backlog_close_happy_path(state_path: Path) -> None:
    runner.invoke(
        app,
        [
            "backlog",
            "add",
            "B023",
            "--title",
            "Split workflow",
            "--priority",
            "P1",
        ],
    )
    _seed_complete_audit("AUD-001")
    result = runner.invoke(
        app,
        [
            "backlog",
            "close",
            "B023",
            "--resolution",
            "implemented",
            "--commit",
            "abc123",
            "--audit",
            "AUD-001",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    assert body["backlog"]["B023"]["status"] == "closed"
    assert body["backlog"]["B023"]["commit"] == "abc123"


def test_backlog_set_priority_happy_path(state_path: Path) -> None:
    """Happy path: open backlog item priority bumped P2 → P0; one event appended."""
    runner.invoke(
        app,
        [
            "backlog",
            "add",
            "B023",
            "--title",
            "Split workflow",
            "--priority",
            "P2",
        ],
    )
    result = runner.invoke(
        app,
        ["backlog", "set-priority", "B023", "--priority", "P0"],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    assert body["backlog"]["B023"]["priority"] == "P0"
    events = (state_path.parent / "store" / "event.jsonl").read_text().splitlines()
    assert any("backlog.set_priority" in line for line in events)


def test_backlog_edit_intent_happy_path(state_path: Path) -> None:
    """Happy path: backlog edit attaches a typed IntentBrief."""
    runner.invoke(
        app,
        [
            "backlog",
            "add",
            "B023",
            "--title",
            "Split workflow",
            "--priority",
            "P2",
        ],
    )
    result = runner.invoke(
        app,
        [
            "backlog",
            "edit",
            "B023",
            "--intent-goal",
            "Keep backlog work tied to a goal",
            "--intent-motivation",
            "Audit repair needs durable planner context.",
            "--intent-success-signal",
            "The item renders with intent.",
            "--intent-evidence-refs",
            "urn:eawf:v1:artifact:QR/ART-001,repo:.ea/artifacts/research/bootstrap.md",
            "--intent-source-brief-ids",
            ".ea/artifacts/research/bootstrap.md",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    intent = body["backlog"]["B023"]["intent"]
    assert intent["goal"] == "Keep backlog work tied to a goal"
    assert intent["evidence_refs"] == [
        "urn:eawf:v1:artifact:QR/ART-001",
        "repo:.ea/artifacts/research/bootstrap.md",
    ]
    events = (state_path.parent / "store" / "event.jsonl").read_text().splitlines()
    assert any("backlog B023 edited fields=intent" in line for line in events)


def test_backlog_edit_clear_intent(state_path: Path) -> None:
    """Happy path: --clear-intent removes an attached brief."""
    runner.invoke(
        app,
        [
            "backlog",
            "add",
            "B023",
            "--title",
            "Split workflow",
            "--priority",
            "P2",
        ],
    )
    result = runner.invoke(
        app,
        ["backlog", "edit", "B023", "--intent-goal", "Keep backlog work tied to a goal"],
    )
    assert result.exit_code == 0, result.stdout
    result = runner.invoke(app, ["backlog", "edit", "B023", "--clear-intent"])
    assert result.exit_code == 0, result.stdout
    body = json.loads(state_path.read_text())
    assert body["backlog"]["B023"]["intent"] is None


def test_backlog_edit_intent_without_goal_exits_invalid_input(state_path: Path) -> None:
    """Error path: optional intent flags require --intent-goal."""
    runner.invoke(
        app,
        [
            "backlog",
            "add",
            "B023",
            "--title",
            "Split workflow",
            "--priority",
            "P2",
        ],
    )
    result = runner.invoke(
        app,
        ["--json", "backlog", "edit", "B023", "--intent-motivation", "missing goal"],
    )
    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["data"]["kind"] == "InvalidInput"


def test_backlog_set_priority_unknown_exits_not_found(state_path: Path) -> None:
    """Error path: missing backlog id exits 2 (NOT_FOUND)."""
    result = runner.invoke(
        app,
        ["--json", "backlog", "set-priority", "B999", "--priority", "P1"],
    )
    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    # C05 § 5.3: ``NotFound`` folds to ``UserError``; legacy name in data.kind.
    assert payload["error"] == "UserError"
    assert payload["data"]["kind"] == "NotFound"


# ---- cross-cutting ---------------------------------------------------------


def test_every_mutation_appends_one_event(state_path: Path) -> None:
    """Every state-mutating evidence command appends exactly one line to events.jsonl.

    Sequence counts:
    - goal define = 1
    - artifact add (seeded by _seed_complete_audit) = 1
    - audit add (with report) = 1
    - decision add = 1
    - artifact add = 1
    - incident open = 1
    Total = 6.
    """
    runner.invoke(app, ["goal", "define", "G01", "--title", "t"])
    _seed_complete_audit("AUD-001")
    runner.invoke(
        app,
        [
            "decision",
            "add",
            "D001",
            "--scope-id",
            "QR",
            "--summary",
            "s",
            "--rationale",
            "r",
        ],
    )
    runner.invoke(
        app,
        [
            "artifact",
            "add",
            "ART-001",
            "--kind",
            "audit_report",
            "--uri",
            "repo:foo.md",
        ],
    )
    runner.invoke(
        app,
        [
            "incident",
            "open",
            "INC-001",
            "--severity",
            "low",
            "--title",
            "t",
        ],
    )
    events = (state_path.parent / "store" / "event.jsonl").read_text().splitlines()
    assert len(events) == 6


def test_resolve_state_path_uses_env_var(state_path: Path) -> None:
    """EA_STATE wins over -w/pwd-upward, per W00 scope.py."""
    assert os.environ.get("EA_STATE") == str(state_path)
    result = runner.invoke(app, ["--json", "audit", "list"])
    assert result.exit_code == 0
