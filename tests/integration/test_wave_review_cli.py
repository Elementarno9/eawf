"""CLI integration tests for ``eawf wave review`` (B041).

Drives the Typer app via :class:`typer.testing.CliRunner` against a
temp ``.ea/state.json`` bootstrapped with a single open phase / iter /
wave. Confirms the parser + audit-attachment loop, exit codes for the
NOT_FOUND / INVALID_INPUT paths, and the ``--diff`` prompt-prep path
that intentionally does not mutate state.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    yield tmp_path


def _bootstrap_wave(workspace: Path, wave_id: str = "P01-I01-W01") -> None:
    """Init QR + open phase/iter + plan a wave under it."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    res = runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            wave_id,
            "--title",
            f"title-{wave_id}",
            "--files",
            "src/",
        ],
    )
    assert res.exit_code == 0, res.stdout


def _write_findings(path: Path, body: str) -> Path:
    """Write *body* to *path* (creating parents) and return *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---- happy path: --findings -----------------------------------------------


def test_wave_review_findings_attaches_audit_and_emits_envelope(workspace: Path) -> None:
    """A canonical findings file attaches a review audit and surfaces the verdict."""
    _bootstrap_wave(workspace)
    findings = _write_findings(
        workspace / "findings.md",
        "\n".join(
            [
                "src/a.py:1: \U0001f534 blocker: hardcoded secret. Move to env.",
                "src/b.py:2: \U0001f7e0 must-fix: missing return. Add `return None`.",
                "src/c.py:3: \U0001f7e1 should-fix: rename variable. Use snake_case.",
                "src/d.py:4: \U0001f535 nit: trailing comma. Remove.",
            ]
        ),
    )
    res = runner.invoke(
        app,
        ["--json", "wave", "review", "P01-I01-W01", "--findings", str(findings)],
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["wave"] == "P01-I01-W01"
    assert envelope["verdict"] == "request-changes"
    assert envelope["summary"] == "blocker:1 must-fix:1 should-fix:1 nit:1"
    assert envelope["finding_count"] == 4
    assert envelope["audit"].startswith("A01-P01-I01-W01")

    # State now carries the audit record (status=complete, kind=review).
    state_path = workspace / ".ea" / "state.json"
    body = orjson.loads(state_path.read_bytes())
    audit_id = envelope["audit"]
    assert audit_id in body["audits"]
    audit = body["audits"][audit_id]
    assert audit["kind"] == "review"
    assert audit["status"] == "complete"
    assert audit["verdict"] == "major"  # request-changes maps to major
    # Audit's report points at a synthesised artifact.
    assert audit["report_artifact_id"] in body["artifacts"]
    artifact = body["artifacts"][audit["report_artifact_id"]]
    assert artifact["kind"] == "review_findings"
    assert artifact["sha256"] is not None


def test_wave_review_explicit_audit_id_round_trips(workspace: Path) -> None:
    """``--audit-id`` overrides the allocator and is stamped on the record."""
    _bootstrap_wave(workspace)
    findings = _write_findings(
        workspace / "findings.md",
        "src/a.py:1: \U0001f7e1 should-fix: minor naming. Rename.",
    )
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "review",
            "P01-I01-W01",
            "--findings",
            str(findings),
            "--audit-id",
            "REV-CUSTOM-001",
        ],
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["audit"] == "REV-CUSTOM-001"


# ---- exit-code paths -------------------------------------------------------


def test_wave_review_missing_findings_path_exit_2(workspace: Path) -> None:
    """A non-existent ``--findings`` path surfaces NOT_FOUND (exit 2)."""
    _bootstrap_wave(workspace)
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "review",
            "P01-I01-W01",
            "--findings",
            str(workspace / "does-not-exist.md"),
        ],
    )
    assert res.exit_code == 2, res.stdout
    payload = json.loads(res.stdout)
    assert payload["error"] == "NotFound"


def test_wave_review_both_findings_and_diff_exit_3(workspace: Path) -> None:
    """Passing both ``--findings`` and ``--diff`` is rejected at exit 3."""
    _bootstrap_wave(workspace)
    findings = _write_findings(workspace / "findings.md", "")
    diff = _write_findings(workspace / "diff.patch", "diff --git a b\n")
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "review",
            "P01-I01-W01",
            "--findings",
            str(findings),
            "--diff",
            str(diff),
        ],
    )
    assert res.exit_code == 3, res.stdout


def test_wave_review_neither_findings_nor_diff_exit_3(workspace: Path) -> None:
    """Omitting both ``--findings`` and ``--diff`` is rejected at exit 3."""
    _bootstrap_wave(workspace)
    res = runner.invoke(app, ["--json", "wave", "review", "P01-I01-W01"])
    assert res.exit_code == 3, res.stdout


def test_wave_review_unknown_wave_id_exit_2(workspace: Path) -> None:
    """An unknown but well-formed wave id is NOT_FOUND (exit 2)."""
    _bootstrap_wave(workspace)
    findings = _write_findings(workspace / "f.md", "")
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "review",
            "P99-I99-W99",
            "--findings",
            str(findings),
        ],
    )
    assert res.exit_code == 2, res.stdout


# ---- --diff (prompt-prep, no state mutation) -------------------------------


def test_wave_review_diff_only_renders_review_prompt(workspace: Path) -> None:
    """``--diff`` renders the wave prompt + review section, no state writes."""
    _bootstrap_wave(workspace)
    diff = _write_findings(workspace / "diff.patch", "diff --git a b\n")
    # Snapshot state before invocation so we can assert it is unchanged.
    state_path = workspace / ".ea" / "state.json"
    before = state_path.read_bytes()

    res = runner.invoke(
        app,
        ["--json", "wave", "review", "P01-I01-W01", "--diff", str(diff)],
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["wave"] == "P01-I01-W01"
    assert envelope["diff_path"] == str(diff)
    request = envelope["review_request"]
    # Carries both the standard wave-prompt header and the review section.
    assert "# Wave P01-I01-W01" in request
    assert "## Review prompt" in request
    assert "path:line:" in request

    # State file unchanged byte-for-byte.
    assert state_path.read_bytes() == before


# ---- empty findings -> approve --------------------------------------------


def test_wave_review_empty_findings_verdict_approve(workspace: Path) -> None:
    """An empty findings document (no parseable lines) lands ``approve``."""
    _bootstrap_wave(workspace)
    findings = _write_findings(
        workspace / "findings.md",
        "Reviewed the diff. Nothing actionable to flag.\n",
    )
    res = runner.invoke(
        app,
        ["--json", "wave", "review", "P01-I01-W01", "--findings", str(findings)],
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["verdict"] == "approve"
    assert envelope["finding_count"] == 0
    assert envelope["summary"] == "blocker:0 must-fix:0 should-fix:0 nit:0"

    # Verdict on disk is `pass` (the AuditVerdict mapping for approve).
    state_path = workspace / ".ea" / "state.json"
    body = orjson.loads(state_path.read_bytes())
    audit_id = envelope["audit"]
    assert body["audits"][audit_id]["verdict"] == "pass"
