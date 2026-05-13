"""Golden-output regression tests for the five status fixtures.

Per Phase 4 W01 acceptance #3 + #5:

- Each of ``ok|needs_user|blocked|failed|partial`` JSON fixtures must
  validate-strict (schema + §15.1 contract).
- The JSON Schema at ``src/eawf/schemas/skill-output.schema.json`` must
  accept ≥10 valid envelopes and reject ≥10 invalid envelopes.
- The wire-form round-trip via ``to_markdown`` / ``from_markdown`` is
  byte-stable for each fixture (mirrors the dual-stability check at
  ``tests/golden/test_golden_agents_md.py:34``).

The committed fixtures are the ground-truth shape skills must emit;
W02/W03 tests will pin individual skill bodies but the **envelope**
shape is frozen here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from eawf.render.envelope import OutputEnvelope, from_markdown, to_markdown
from eawf.validate.strict import validate_envelope

_FIXTURE_DIR: Path = Path(__file__).parent / "envelope"
_SCHEMA_PATH: Path = (
    Path(__file__).resolve().parents[2] / "src" / "eawf" / "schemas" / "skill-output.schema.json"
)

_STATUSES = ("ok", "needs_user", "blocked", "failed", "partial")


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a fixture JSON file as a Python dict."""
    return json.loads((_FIXTURE_DIR / f"{name}.json").read_text())


@pytest.mark.golden
@pytest.mark.parametrize("status", _STATUSES)
def test_envelope_fixture_passes_strict_validation(status: str) -> None:
    """Each status fixture must satisfy schema + §15.1 contracts."""
    payload = _load_fixture(status)
    report = validate_envelope(payload)
    assert report.ok, (
        f"fixture {status}.json failed strict validation: "
        f"schema_errors={report.schema_errors!r}, "
        f"contract_errors={report.contract_errors!r}"
    )
    assert report.envelope is not None
    assert report.envelope.header.status == status


@pytest.mark.golden
@pytest.mark.parametrize("status", _STATUSES)
def test_envelope_fixture_roundtrip_byte_stable(status: str) -> None:
    """Pydantic validate → to_markdown → from_markdown → to_markdown is stable."""
    payload = _load_fixture(status)
    env = OutputEnvelope.model_validate(payload)
    md1 = to_markdown(env)
    md2 = to_markdown(from_markdown(md1))
    assert md1 == md2


@pytest.mark.golden
def test_needs_user_without_body_user_question_rejected() -> None:
    """Acceptance #3: ``status=needs_user`` without ``body.user_question`` rejected."""
    payload = _load_fixture("needs_user")
    payload = copy.deepcopy(payload)
    # Replace the body so that user_question is absent.
    payload["body"] = "no user question here\n"
    report = validate_envelope(payload)
    assert not report.ok
    assert any("user_question" in err for err in report.contract_errors)


@pytest.mark.golden
def test_blocked_without_repair_commands_rejected() -> None:
    """``status=blocked`` without ``footer.repair_commands`` rejected."""
    payload = copy.deepcopy(_load_fixture("blocked"))
    del payload["footer"]["repair_commands"]
    report = validate_envelope(payload)
    assert not report.ok
    assert any("repair_commands" in err for err in report.contract_errors)


@pytest.mark.golden
def test_failed_without_repair_commands_rejected() -> None:
    """``status=failed`` without ``footer.repair_commands`` rejected."""
    payload = copy.deepcopy(_load_fixture("failed"))
    del payload["footer"]["repair_commands"]
    report = validate_envelope(payload)
    assert not report.ok
    assert any("repair_commands" in err for err in report.contract_errors)


# ----- JSON Schema valid / invalid set --------------------------------------


def _valid_envelope_examples() -> list[dict[str, Any]]:
    """≥10 hand-crafted envelopes that MUST pass JSON Schema validation."""
    base = _load_fixture("ok")

    examples: list[dict[str, Any]] = []
    # The five committed fixtures.
    for status in _STATUSES:
        examples.append(_load_fixture(status))

    # One per builtin skill plus one overlay skill (keeps the rest minimal).
    for skill in (
        "/research",
        "/prep",
        "/audit",
        "/ship",
        "/review",
        "/polish",
        "/init",
        "/roadmap",
        "/differentiate",
        "/flow",
        "/blitz",
        "/workspace-overlay",
    ):
        env = copy.deepcopy(base)
        env["header"]["skill"] = skill
        examples.append(env)

    # Adds up to 5 + 12 = 17 — safely above the ≥10 requirement.
    return examples


def _invalid_envelope_examples() -> list[dict[str, Any]]:
    """≥10 hand-crafted envelopes that MUST be rejected by JSON Schema."""
    base = _load_fixture("ok")
    invalid: list[dict[str, Any]] = []

    # 1. Missing top-level header.
    e = copy.deepcopy(base)
    del e["header"]
    invalid.append(e)

    # 2. Missing top-level body.
    e = copy.deepcopy(base)
    del e["body"]
    invalid.append(e)

    # 3. Missing top-level footer.
    e = copy.deepcopy(base)
    del e["footer"]
    invalid.append(e)

    # 4. Extra top-level key.
    e = copy.deepcopy(base)
    e["extra"] = "nope"
    invalid.append(e)

    # 5. Header missing started_at.
    e = copy.deepcopy(base)
    del e["header"]["started_at"]
    invalid.append(e)

    # 6. Skill name must be a string, even though overlay names are open.
    e = copy.deepcopy(base)
    e["header"]["skill"] = 123
    invalid.append(e)

    # 7. Unknown status literal.
    e = copy.deepcopy(base)
    e["header"]["status"] = "cancelled"
    invalid.append(e)

    # 8. Unknown instrument-probe value.
    e = copy.deepcopy(base)
    e["header"]["instrument_probe"] = {"git": "broken"}
    invalid.append(e)

    # 9. Footer with unknown key.
    e = copy.deepcopy(base)
    e["footer"]["unexpected"] = "oops"
    invalid.append(e)

    # 10. Warning with unknown key.
    e = copy.deepcopy(base)
    e["footer"]["warnings"] = [{"code": "x", "detail": "y", "level": "high"}]
    invalid.append(e)

    # 11. Footer.persisted_artifacts is a string, not a list.
    e = copy.deepcopy(base)
    e["footer"]["persisted_artifacts"] = "not-a-list"
    invalid.append(e)

    return invalid


@pytest.mark.golden
def test_schema_accepts_at_least_10_valid_envelopes() -> None:
    """Acceptance #5: ≥10 valid envelopes accepted via jsonschema."""
    schema = json.loads(_SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    valid = _valid_envelope_examples()
    assert len(valid) >= 10
    for i, env in enumerate(valid):
        errors = list(validator.iter_errors(env))
        assert not errors, f"valid example {i} unexpectedly rejected: " + ", ".join(
            e.message for e in errors
        )


@pytest.mark.golden
def test_schema_rejects_at_least_10_invalid_envelopes() -> None:
    """Acceptance #5: ≥10 invalid envelopes rejected via jsonschema."""
    schema = json.loads(_SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    invalid = _invalid_envelope_examples()
    assert len(invalid) >= 10
    for i, env in enumerate(invalid):
        errors = list(validator.iter_errors(env))
        assert errors, f"invalid example {i} unexpectedly accepted: {env!r}"
