"""Spec-path enforcement integration tests (P25-W05 success criterion 1).

Each layer must catch the RC-1 stale-paths failure class on its own
(per the C03 brief §1). This module verifies the loader layer end-to-end:

- **Layer 1 — Pydantic + loader.** A WaveSpec that names a missing
  test path passes ``model_validate`` (paths are not checked at the
  Pydantic level) but fails when the loader function
  :func:`eawf.kernel.spec.validators.validate_wave_spec_at_load` is invoked
  against a temp project root.
- **Layer 1 (UI heuristic).** A UI-scope WaveSpec without mockup +
  without waiver fails at ``model_validate`` time — the heuristic
  catches the failure even before the loader runs.

The pre-commit hook that grepped committed wave specs under ``.ea/specs``
is retired with the wave lifecycle, and the third layer (audit DSL
``verify-implements``) is covered by its own tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import VerdictCitation
from eawf.kernel.spec.validators import SpecValidationError, validate_wave_spec_at_load
from eawf.kernel.spec.wave import WaveBehavior, WaveMockup, WaveSpec
from eawf.kernel.state.enums import AgentSessionRole, EffortBucket


def _verdict_citation(
    brief: str = ".ea/artifacts/research/2026-05-16-c03-spec-infrastructure.md",
) -> VerdictCitation:
    return VerdictCitation(verdict_id="V12", brief=brief)


def _wave_spec(
    *,
    file_scopes: list[str] | None = None,
    tests: list[str] | None = None,
    mockup: WaveMockup | None = None,
    mockup_waiver_reason: str | None = None,
) -> WaveSpec:
    return WaveSpec.model_validate(
        {
            "id": "P25-I01-W05",
            "iter_id": "P25-I01",
            "phase_id": "P25",
            "title": "C03 validators",
            "agent_role": AgentSessionRole.EXECUTOR,
            "effort_bucket": EffortBucket.M,
            "file_scopes": file_scopes or ["src/eawf/spec/validators.py"],
            "implements": [_verdict_citation()],
            "behaviors": [
                WaveBehavior(
                    id="B1",
                    text="loader validators reject missing test paths",
                )
            ],
            "failure_modes": ["false negative: stale path slips through pre-commit"],
            "tests": tests or [],
            "mockup": mockup,
            "mockup_waiver_reason": mockup_waiver_reason,
        }
    )


# ---- Layer 1 — Pydantic (model_validator) ----------------------------------


def test_layer1_pydantic_ui_scope_without_mockup_fails_at_model_validate() -> None:
    # No tmp_path / disk lookup — the UI heuristic catches this at
    # ``model_validate`` time so the failure surfaces before any loader
    # function is called.
    with pytest.raises(ValidationError) as excinfo:
        _wave_spec(file_scopes=["src/eawf/surfaces/tui/header.py"])
    assert "ui-scope wave requires mockup reference" in str(excinfo.value)


def test_layer1_pydantic_ui_scope_with_mockup_passes() -> None:
    spec = _wave_spec(
        file_scopes=["src/eawf/surfaces/tui/header.py"],
        mockup=WaveMockup(ascii="+---+\n|.|\n+---+"),
    )
    assert spec.mockup is not None


# ---- Layer 1 — Loader (validate_wave_spec_at_load) -------------------------


def test_layer1_loader_passes_when_test_paths_exist(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "unit" / "test_real.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("# test\n", encoding="utf-8")
    brief = tmp_path / ".ea" / "artifacts" / "research" / "2026-05-16-c03-spec-infrastructure.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("# brief\n", encoding="utf-8")
    spec = _wave_spec(tests=["tests/unit/test_real.py"])
    validate_wave_spec_at_load(spec, tmp_path)


def test_layer1_loader_rejects_missing_test_path(tmp_path: Path) -> None:
    # Brief path exists; only the test path is stale.
    brief = tmp_path / ".ea" / "artifacts" / "research" / "2026-05-16-c03-spec-infrastructure.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("# brief\n", encoding="utf-8")
    spec = _wave_spec(tests=["tests/unit/test_missing.py"])
    with pytest.raises(SpecValidationError) as excinfo:
        validate_wave_spec_at_load(spec, tmp_path)
    assert "tests/unit/test_missing.py" in str(excinfo.value)


def test_layer1_loader_rejects_missing_brief_path(tmp_path: Path) -> None:
    # Test path exists; brief path is stale.
    test_file = tmp_path / "tests" / "unit" / "test_real.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("# test\n", encoding="utf-8")
    spec = _wave_spec(tests=["tests/unit/test_real.py"])
    with pytest.raises(SpecValidationError) as excinfo:
        validate_wave_spec_at_load(spec, tmp_path)
    assert "2026-05-16-c03-spec-infrastructure.md" in str(excinfo.value)
