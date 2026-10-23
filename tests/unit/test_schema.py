from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from eawf.kernel.migration.epoch2.cutover import DOCUMENT_SCHEMA_KEY
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.manifest import MANIFEST_SCHEMA_VERSION
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.state.epoch2.repository import Repository
from eawf.kernel.store.compaction import CANONICAL_SEQUENCE_KEY, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.docs import autogen as schema_mod
from eawf.platform.install.epoch2_birth import bear_epoch2_tree
from eawf.platform.install.steps import STEP_LIFECYCLE_DEPTH
from eawf.platform.install.wizard import WizardAnswers, run_wizard_no_input

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "src" / "eawf" / "schemas"


def test_state_schema_generates_deterministically() -> None:
    a = schema_mod.generate_state_schema()
    b = schema_mod.generate_state_schema()
    assert a == b


def test_state_schema_committed_matches_generated(tmp_path: Path) -> None:
    schema_mod.dump_bundled_schemas(tmp_path)
    generated = json.loads((tmp_path / "state.schema.json").read_text())
    committed = json.loads((SCHEMAS_DIR / "state.schema.json").read_text())
    assert generated == committed


def test_state_schema_describes_the_generation_document() -> None:
    s = schema_mod.generate_state_schema()
    assert s["title"] == "EawfGenerationDocument"
    assert s["title"] != "EawfState"
    assert s["$schema"].startswith("https://json-schema.org/draft/2020-12")
    assert s["required"] == [DOCUMENT_SCHEMA_KEY]
    assert set(s["properties"]) == {
        DOCUMENT_SCHEMA_KEY,
        CANONICAL_SEQUENCE_KEY,
        *(collection.value for collection in Epoch2Collection),
    }


def _born_generation_document(root: Path) -> dict[str, Any]:
    """Init a native tree under *root* the way ``eawf init --no-input`` does; read its document."""
    answers = WizardAnswers(
        state_path=".ea/state.json",
        project_code="DEMO",
        project_title="Demo",
        profiles=("core",),
        runtime="claude-code",
        lifecycle_depth=STEP_LIFECYCLE_DEPTH.default,
    )
    result = run_wizard_no_input(answers, root)
    generation_id = bear_epoch2_tree(
        result.state_path.parent, project_code=result.project_code, born_at=datetime.now(UTC)
    )
    authority = require_native_authority(root / ".ea")
    assert authority.target is not None and authority.generation_id == generation_id
    return read_document(authority.target.generation_path(generation_id) / GENERATION_DOCUMENT)


#: A repository row as the daemon admits it on a git-backed init.
_ADMITTED_REPOSITORY_ROW: dict[str, Any] = {
    "created_at": "2026-10-23T15:22:10.198777Z",
    "head_sha": "a" * 40,
    "key": "DEMO",
    "revision": 1,
    "updated_at": "2026-10-23T15:22:10.198777Z",
    "urn": "eawf://DEMO/DEMO/DEMO/repository/DEMO",
}


def test_state_schema_validates_an_init_born_generation_document(tmp_path: Path) -> None:
    schema = json.loads((SCHEMAS_DIR / "state.schema.json").read_text())
    document = _born_generation_document(tmp_path)
    assert document[DOCUMENT_SCHEMA_KEY] == MANIFEST_SCHEMA_VERSION
    jsonschema.validate(document, schema)
    Repository.model_validate(_ADMITTED_REPOSITORY_ROW)
    document["repository"] = {"DEMO": _ADMITTED_REPOSITORY_ROW}
    document[CANONICAL_SEQUENCE_KEY] = 1
    jsonschema.validate(document, schema)


def test_state_schema_accepts_the_bare_document() -> None:
    schema = schema_mod.generate_state_schema()
    jsonschema.validate({DOCUMENT_SCHEMA_KEY: MANIFEST_SCHEMA_VERSION}, schema)


def test_state_schema_accepts_an_imported_row_and_an_unmodelled_collection() -> None:
    schema = schema_mod.generate_state_schema()
    document = {
        DOCUMENT_SCHEMA_KEY: MANIFEST_SCHEMA_VERSION,
        "task": {"T-1": {"status": "active", "recorded_at": "2026-05-08T00:00:00Z", "payload": {}}},
        "receipt": {"R-1": {"anything": [1, 2]}},
    }
    jsonschema.validate(document, schema)


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({}, id="missing-schema-version"),
        pytest.param({DOCUMENT_SCHEMA_KEY: "1.0"}, id="epoch1-schema-version"),
        pytest.param({DOCUMENT_SCHEMA_KEY: MANIFEST_SCHEMA_VERSION, "phases": {}}, id="v1-key"),
        pytest.param({DOCUMENT_SCHEMA_KEY: MANIFEST_SCHEMA_VERSION, "task": []}, id="not-a-map"),
        pytest.param(
            {DOCUMENT_SCHEMA_KEY: MANIFEST_SCHEMA_VERSION, "repository": {"R": {"key": "R"}}},
            id="invalid-native-row",
        ),
        pytest.param(
            {DOCUMENT_SCHEMA_KEY: MANIFEST_SCHEMA_VERSION, CANONICAL_SEQUENCE_KEY: "1"},
            id="sequence-not-an-integer",
        ),
    ],
)
def test_state_schema_rejects_a_document_off_the_native_shape(document: dict[str, Any]) -> None:
    schema = schema_mod.generate_state_schema()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


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
    schema_mod.dump_bundled_schemas(tmp_path)
    generated = json.loads((tmp_path / "skill-output.schema.json").read_text())
    committed = json.loads((SCHEMAS_DIR / "skill-output.schema.json").read_text())
    assert generated == committed


def test_bundled_schemas_publish_no_plan_view(tmp_path: Path) -> None:
    schema_mod.dump_bundled_schemas(tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "config.schema.json",
        "skill-output.schema.json",
        "state.schema.json",
    ]


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
