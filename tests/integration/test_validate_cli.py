"""End-to-end tests for ``eawf validate`` against fixture state files.

Combines the schema layer and invariant layer through the Typer CLI to confirm
the user-visible exit codes and JSON output contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "states"

# Map invalid fixture filenames to the violation/error code we expect to see.
# For schema errors the value is ``None`` because the schema layer emits free-form
# Pydantic messages without stable codes.
EXPECTED_INVALID: dict[str, str | None] = {
    "01-extra-field.json": None,  # schema error
    "02-iter-references-missing-phase.json": "INV.PARENT.ITER_PHASE_MISSING",
    "03-wave-id-parent-mismatch.json": "INV.PARENT.WAVE_ID_MISMATCH",
    "04-current-points-to-closed-phase.json": "INV.CURRENT.PHASE_NOT_OPEN",
    "05-closed-phase-with-open-iter.json": "INV.CLOSURE.PHASE_HAS_OPEN_ITER",
    "06-outcome-met-no-audit.json": "INV.AUDIT.OUTCOME_MISSING_AUDIT",
    "07-hypothesis-verdict-no-audit.json": "INV.AUDIT.HYPOTHESIS_MISSING_AUDIT",
    "08-bad-urn.json": None,  # schema error
    "09-active-wave-not-claimed.json": "INV.CURRENT.WAVE_NOT_ACTIVE",
    "10-mcp-non-eawf-owner.json": "INV.OWNER.MCP_NON_EAWF",
    "11-plugin-non-claude.json": "INV.OWNER.PLUGIN_NON_CLAUDE",
    "12-scope-repo-no-project.json": "INV.SCOPE.REPO_REQUIRES_PROJECT",
    "13-scope-workspace-no-index.json": "INV.SCOPE.WORKSPACE_REQUIRES_INDEX",
    "14-current-iter-phase-mismatch.json": "INV.CURRENT.ITER_PHASE_MISMATCH",
    "15-plugin-non-eawf-owner.json": "INV.OWNER.PLUGIN_NON_EAWF",
    "16-closed-phase-no-closed-at.json": "INV.CLOSURE.PHASE_NO_CLOSED_AT",
    "17-closed-wave-no-closed-at.json": "INV.CLOSURE.WAVE_NO_CLOSED_AT",
    "18-stale-session-no-ended-at.json": "INV.CLOSURE.SESSION_NO_ENDED_AT",
}


def test_validate_accepts_all_valid() -> None:
    valid_dir = FIXTURES / "valid"
    fixtures = sorted(valid_dir.glob("*.json"))
    assert len(fixtures) >= 10, f"need >=10 valid fixtures, found {len(fixtures)}"
    for fixture in fixtures:
        result = runner.invoke(app, ["validate", str(fixture)])
        assert result.exit_code == 0, f"valid fixture failed: {fixture.name}\n{result.output}"
        assert "validate: ok" in result.output


def test_validate_rejects_invalid_with_specific_codes() -> None:
    invalid_dir = FIXTURES / "invalid"
    fixtures = sorted(invalid_dir.glob("*.json"))
    assert len(fixtures) >= 10, f"need >=10 invalid fixtures, found {len(fixtures)}"
    for fixture in fixtures:
        result = runner.invoke(app, ["validate", str(fixture), "--json"])
        assert result.exit_code == 4, (
            f"invalid fixture should fail: {fixture.name}\n{result.output}"
        )
        body = json.loads(result.output.strip().splitlines()[-1])
        assert body["ok"] is False
        assert body["schema_errors"] or body["violations"]
        expected = EXPECTED_INVALID.get(fixture.name)
        if expected is None:
            # Schema-error fixture: must report at least one schema error.
            assert body["schema_errors"], (
                f"{fixture.name}: expected schema error, got violations={body['violations']}"
            )
        else:
            codes = {v["code"] for v in body["violations"]}
            assert expected in codes, (
                f"{fixture.name}: expected {expected} in {codes}; "
                f"schema_errors={body['schema_errors']}"
            )


def test_validate_strict_does_not_affect_invariant_exit_code() -> None:
    """Both ``--strict`` and the default give exit=4 on an invariant-violating fixture."""
    fixture = FIXTURES / "invalid" / "02-iter-references-missing-phase.json"
    for args in (["validate", str(fixture)], ["validate", "--strict", str(fixture)]):
        result = runner.invoke(app, args)
        assert result.exit_code == 4, (args, result.output)
        assert "INV.PARENT.ITER_PHASE_MISSING" in result.output


def test_validate_strict_flags_missing_optional_keys() -> None:
    """Default (lenient) returns 0; ``--strict`` returns 4 for absent optional keys."""
    # 01-empty-repo.json has no `subprojects`, `goals`, `outcomes`, etc.
    fixture = FIXTURES / "valid" / "01-empty-repo.json"
    lenient = runner.invoke(app, ["validate", str(fixture)])
    assert lenient.exit_code == 0, lenient.output
    strict = runner.invoke(app, ["validate", "--strict", str(fixture)])
    assert strict.exit_code == 4, strict.output
    assert "STRICT.OPTIONAL_MISSING" in strict.output


def test_validate_human_output_on_ok() -> None:
    fixture = FIXTURES / "valid" / "01-empty-repo.json"
    result = runner.invoke(app, ["validate", str(fixture)])
    assert result.exit_code == 0
    assert result.output.strip() == "validate: ok"


def test_validate_json_output_on_ok() -> None:
    fixture = FIXTURES / "valid" / "01-empty-repo.json"
    result = runner.invoke(app, ["validate", "--json", str(fixture)])
    assert result.exit_code == 0
    body = json.loads(result.output.strip())
    assert body == {"ok": True, "schema_errors": [], "violations": []}


def test_validate_human_output_lists_violations() -> None:
    fixture = FIXTURES / "invalid" / "10-mcp-non-eawf-owner.json"
    result = runner.invoke(app, ["validate", str(fixture)])
    assert result.exit_code == 4
    assert "INV.OWNER.MCP_NON_EAWF" in result.output
    assert "/mcp_servers/filesystem/owner" in result.output


def test_validate_missing_path_exits_nonzero() -> None:
    """A nonexistent file must be a hard error (Typer/Click reports != 0)."""
    result = runner.invoke(app, ["validate", "/no/such/path.json"])
    assert result.exit_code != 0
