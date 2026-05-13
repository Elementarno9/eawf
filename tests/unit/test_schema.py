from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from eawf.state import schema as schema_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "src" / "eawf" / "schemas"


def test_state_schema_generates_deterministically() -> None:
    a = schema_mod.generate_state_schema()
    b = schema_mod.generate_state_schema()
    assert a == b


def test_state_schema_committed_matches_generated(tmp_path: Path) -> None:
    schema_mod.dump_schemas(tmp_path)
    generated = json.loads((tmp_path / "state.schema.json").read_text())
    committed = json.loads((SCHEMAS_DIR / "state.schema.json").read_text())
    assert generated == committed


def test_state_schema_has_required_top_level_fields() -> None:
    s = schema_mod.generate_state_schema()
    assert s["title"] == "EawfState"
    assert s["$schema"].startswith("https://json-schema.org/draft/2020-12")


def test_state_schema_validates_minimal_payload() -> None:
    schema = json.loads((SCHEMAS_DIR / "state.schema.json").read_text())
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant-research",
            "title": "Quant Research",
            "description": "",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    jsonschema.validate(payload, schema)


def test_state_schema_rejects_extra_field() -> None:
    schema = json.loads((SCHEMAS_DIR / "state.schema.json").read_text())
    payload = {"foo": "bar"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_placeholder_schema_parses_as_jsonschema() -> None:
    """``config.schema.json`` is still the placeholder until Phase 5/6 fills it."""
    body = json.loads((SCHEMAS_DIR / "config.schema.json").read_text())
    assert body["$schema"].startswith("https://json-schema.org/draft/2020-12")
    assert body["type"] == "object"
    assert body["additionalProperties"] is True


def test_skill_output_schema_committed_matches_generated(tmp_path: Path) -> None:
    """Phase 4 W01 generated skill-output schema must match the committed file."""
    schema_mod.dump_schemas(tmp_path)
    generated = json.loads((tmp_path / "skill-output.schema.json").read_text())
    committed = json.loads((SCHEMAS_DIR / "skill-output.schema.json").read_text())
    assert generated == committed


def test_plan_view_schema_committed_matches_generated(tmp_path: Path) -> None:
    """Phase 5 W05 generated plan-view schema must match the committed file."""
    schema_mod.dump_schemas(tmp_path)
    generated = json.loads((tmp_path / "plan-view.schema.json").read_text())
    committed = json.loads((SCHEMAS_DIR / "plan-view.schema.json").read_text())
    assert generated == committed


def test_skill_output_schema_has_required_top_level_fields() -> None:
    """The skill-output schema is a typed envelope, not the placeholder."""
    body = json.loads((SCHEMAS_DIR / "skill-output.schema.json").read_text())
    assert body["title"] == "EawfSkillOutput"
    assert body["$schema"].startswith("https://json-schema.org/draft/2020-12")
    # Phase 4 W01 freezes header/body/footer.
    assert body["additionalProperties"] is False
    assert body["required"] == ["header", "body", "footer"]
    # The five envelope statuses must be present in the schema.
    statuses = body["$defs"]["EnvelopeHeader"]["properties"]["status"]["enum"]
    assert set(statuses) == {"ok", "needs_user", "blocked", "failed", "partial"}
    # Skill names are open so workspace/user overlays can emit envelopes.
    skill_schema = body["$defs"]["EnvelopeHeader"]["properties"]["skill"]
    assert skill_schema == {"title": "Skill", "type": "string"}
