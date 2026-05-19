"""Unit tests for the C03 loader-side validators + heuristics.

Coverage matrix (P25-W05 success criteria):

- Heuristics (:mod:`eawf.spec.heuristics`):
  - :func:`is_ui_scope` — true for UI prefixes, false for non-UI, false
    for empty input.
  - :func:`requires_mockup_reference` — fires only when (UI + no mockup
    + no waiver); does NOT fire when mockup present, when waiver set,
    or when the wave is non-UI.
  - :func:`missing_test_paths` — returns missing refs only.

- Pydantic model_validator (:class:`~eawf.spec.wave.WaveSpec._mockup_required`):
  - UI-scope wave without mockup AND without waiver raises
    ``ValidationError`` at ``model_validate`` time (success criterion 2,
    "at schema load").
  - UI-scope wave with mockup passes.
  - UI-scope wave with waiver passes.
  - Non-UI wave without mockup passes.

- Loader-side (:mod:`eawf.spec.validators`):
  - :func:`validate_wave_spec_tests_exist` raises when any test path is
    missing; passes when every path exists (tmp_path fixture).
  - :func:`validate_wave_spec_brief_paths_exist` raises when any brief
    path is missing; passes otherwise.
  - :func:`validate_phase_spec_has_kpis` raises on empty
    ``kpis``; passes when at least one KPI is present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.spec.common import VerdictCitation
from eawf.spec.heuristics import (
    is_ui_scope,
    missing_test_paths,
    requires_mockup_reference,
)
from eawf.spec.phase import PhaseKPI, PhaseShipCriterion, PhaseSpec
from eawf.spec.validators import (
    SpecValidationError,
    validate_phase_spec_has_kpis,
    validate_wave_spec_brief_paths_exist,
    validate_wave_spec_tests_exist,
)
from eawf.spec.wave import WaveBehavior, WaveMockup, WaveSpec
from eawf.state.enums import AgentSessionRole, EffortBucket

# ---- Test fixtures ----------------------------------------------------------


def _verdict_citation(
    brief: str = ".ea/artifacts/research/2026-05-16-c03-spec-infrastructure.md",
) -> VerdictCitation:
    return VerdictCitation(verdict_id="V12", brief=brief)


def _wave_spec_factory(**overrides: Any) -> WaveSpec:
    defaults: dict[str, Any] = {
        "id": "P25-I01-W05",
        "iter_id": "P25-I01",
        "phase_id": "P25",
        "title": "C03 validators + heuristics + pre-commit hook",
        "agent_role": AgentSessionRole.EXECUTOR,
        "effort_bucket": EffortBucket.M,
        "file_scopes": ["src/eawf/spec/validators.py"],
        "implements": [_verdict_citation()],
        "behaviors": [
            WaveBehavior(
                id="B1",
                text="loader validators reject missing test paths",
            )
        ],
        "failure_modes": ["false negative: stale path slips through pre-commit"],
    }
    defaults.update(overrides)
    return WaveSpec.model_validate(defaults)


def _phase_spec_factory(**overrides: Any) -> PhaseSpec:
    defaults: dict[str, Any] = {
        "id": "P25",
        "title": "Spec infrastructure phase",
        "outcome": "land C03/C07a/C07b/C08 deliverables together",
        "failure_modes": ["specs drift from state tree"],
        "ship_criteria": [PhaseShipCriterion(id="SC1", text="all waves closed")],
    }
    defaults.update(overrides)
    return PhaseSpec.model_validate(defaults)


# ---- is_ui_scope ------------------------------------------------------------


@pytest.mark.parametrize(
    "scopes,expected",
    [
        (["src/eawf/tui_v2/app.py"], True),
        (["src/eawf/render/envelope.py"], True),
        (
            [
                "src/eawf/state/models.py",
                "src/eawf/tui_v2/widgets/header.py",
            ],
            True,
        ),
        (["src/eawf/spec/wave.py"], False),
        (["src/eawf/state/models.py"], False),
        (["tools/scripts/x.py"], False),
        ([], False),
    ],
)
def test_is_ui_scope_matches_prefixes(scopes: list[str], expected: bool) -> None:
    assert is_ui_scope(scopes) is expected


# ---- requires_mockup_reference ----------------------------------------------


def test_requires_mockup_reference_fires_when_ui_and_no_mockup_no_waiver() -> None:
    assert requires_mockup_reference(
        file_scopes=["src/eawf/tui_v2/app.py"],
        mockup_present=False,
        mockup_waiver_reason=None,
    )


def test_requires_mockup_reference_skips_when_mockup_present() -> None:
    assert not requires_mockup_reference(
        file_scopes=["src/eawf/tui_v2/app.py"],
        mockup_present=True,
        mockup_waiver_reason=None,
    )


def test_requires_mockup_reference_skips_when_waiver_set() -> None:
    assert not requires_mockup_reference(
        file_scopes=["src/eawf/tui_v2/app.py"],
        mockup_present=False,
        mockup_waiver_reason="non-rendering helper module",
    )


def test_requires_mockup_reference_skips_for_non_ui_scope() -> None:
    assert not requires_mockup_reference(
        file_scopes=["src/eawf/spec/validators.py"],
        mockup_present=False,
        mockup_waiver_reason=None,
    )


def test_requires_mockup_reference_treats_whitespace_waiver_as_missing() -> None:
    # Empty / whitespace-only waiver does not satisfy D11.
    assert requires_mockup_reference(
        file_scopes=["src/eawf/render/envelope.py"],
        mockup_present=False,
        mockup_waiver_reason="   ",
    )


# ---- WaveSpec model_validator _mockup_required ------------------------------


def test_wave_spec_ui_scope_without_mockup_or_waiver_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _wave_spec_factory(file_scopes=["src/eawf/tui_v2/app.py"])
    assert "ui-scope wave requires mockup reference" in str(excinfo.value)


def test_wave_spec_ui_scope_with_mockup_accepted() -> None:
    spec = _wave_spec_factory(
        file_scopes=["src/eawf/tui_v2/app.py"],
        mockup=WaveMockup(ascii="+--header--+\n|...|\n+----+"),
    )
    assert spec.mockup is not None


def test_wave_spec_ui_scope_with_waiver_accepted() -> None:
    spec = _wave_spec_factory(
        file_scopes=["src/eawf/render/envelope.py"],
        mockup_waiver_reason="re-export module only; no visible surface",
    )
    assert spec.mockup is None
    assert spec.mockup_waiver_reason is not None


def test_wave_spec_non_ui_scope_without_mockup_accepted() -> None:
    spec = _wave_spec_factory(file_scopes=["src/eawf/spec/validators.py"])
    assert spec.mockup is None


def test_wave_spec_mixed_scopes_ui_triggers_heuristic() -> None:
    # A wave with one UI and one non-UI scope still needs a mockup.
    with pytest.raises(ValidationError):
        _wave_spec_factory(
            file_scopes=[
                "src/eawf/spec/validators.py",
                "src/eawf/tui_v2/widgets/header.py",
            ]
        )


# ---- missing_test_paths -----------------------------------------------------


def test_missing_test_paths_returns_empty_when_all_exist(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    test_file = tmp_path / "tests" / "unit" / "test_x.py"
    test_file.write_text("# test\n", encoding="utf-8")
    assert missing_test_paths(["tests/unit/test_x.py"], tmp_path) == []


def test_missing_test_paths_returns_each_missing_ref(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_x.py").write_text("# test\n", encoding="utf-8")
    refs = [
        "tests/unit/test_x.py",
        "tests/unit/test_missing.py",
        "tests/integration/test_gone.py",
    ]
    assert missing_test_paths(refs, tmp_path) == [
        "tests/unit/test_missing.py",
        "tests/integration/test_gone.py",
    ]


def test_missing_test_paths_empty_iterable(tmp_path: Path) -> None:
    assert missing_test_paths([], tmp_path) == []


def test_missing_test_paths_directory_is_not_file(tmp_path: Path) -> None:
    # A directory at the path doesn't count as the test file existing.
    (tmp_path / "tests" / "unit" / "test_x.py").mkdir(parents=True)
    assert missing_test_paths(["tests/unit/test_x.py"], tmp_path) == ["tests/unit/test_x.py"]


# ---- validate_wave_spec_tests_exist -----------------------------------------


def _seed_test_file(tmp_path: Path, rel: str) -> None:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# test\n", encoding="utf-8")


def _seed_brief(tmp_path: Path, rel: str) -> None:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# brief\n", encoding="utf-8")


def test_validate_wave_spec_tests_exist_passes_when_all_present(tmp_path: Path) -> None:
    _seed_test_file(tmp_path, "tests/unit/test_x.py")
    _seed_test_file(tmp_path, "tests/integration/test_y.py")
    spec = _wave_spec_factory(
        tests=["tests/unit/test_x.py"],
        behaviors=[
            WaveBehavior(
                id="B1",
                text="loader validators reject missing test paths",
                test_refs=["tests/integration/test_y.py"],
            )
        ],
    )
    validate_wave_spec_tests_exist(spec, tmp_path)


def test_validate_wave_spec_tests_exist_raises_when_top_level_missing(
    tmp_path: Path,
) -> None:
    spec = _wave_spec_factory(tests=["tests/unit/test_missing.py"])
    with pytest.raises(SpecValidationError) as excinfo:
        validate_wave_spec_tests_exist(spec, tmp_path)
    assert "tests/unit/test_missing.py" in str(excinfo.value)
    assert "P25-I01-W05" in str(excinfo.value)


def test_validate_wave_spec_tests_exist_raises_when_behavior_ref_missing(
    tmp_path: Path,
) -> None:
    spec = _wave_spec_factory(
        behaviors=[
            WaveBehavior(
                id="B1",
                text="loader validators reject missing test paths",
                test_refs=["tests/unit/test_behavior.py"],
            )
        ],
    )
    with pytest.raises(SpecValidationError) as excinfo:
        validate_wave_spec_tests_exist(spec, tmp_path)
    assert "tests/unit/test_behavior.py" in str(excinfo.value)


def test_validate_wave_spec_tests_exist_aggregates_all_missing(
    tmp_path: Path,
) -> None:
    _seed_test_file(tmp_path, "tests/unit/test_exists.py")
    spec = _wave_spec_factory(
        tests=["tests/unit/test_exists.py", "tests/unit/test_missing_a.py"],
        behaviors=[
            WaveBehavior(
                id="B1",
                text="loader validators reject missing test paths",
                test_refs=["tests/unit/test_missing_b.py"],
            )
        ],
    )
    with pytest.raises(SpecValidationError) as excinfo:
        validate_wave_spec_tests_exist(spec, tmp_path)
    diag = str(excinfo.value)
    assert "tests/unit/test_missing_a.py" in diag
    assert "tests/unit/test_missing_b.py" in diag
    assert "tests/unit/test_exists.py" not in diag


# ---- validate_wave_spec_brief_paths_exist -----------------------------------


def test_validate_wave_spec_brief_paths_exist_passes_when_present(
    tmp_path: Path,
) -> None:
    brief = ".ea/artifacts/research/2026-05-19-x.md"
    _seed_brief(tmp_path, brief)
    spec = _wave_spec_factory(implements=[_verdict_citation(brief=brief)])
    validate_wave_spec_brief_paths_exist(spec, tmp_path)


def test_validate_wave_spec_brief_paths_exist_raises_when_missing(
    tmp_path: Path,
) -> None:
    spec = _wave_spec_factory(
        implements=[_verdict_citation(brief=".ea/local/research/2026-05-19-missing.md")],
    )
    with pytest.raises(SpecValidationError) as excinfo:
        validate_wave_spec_brief_paths_exist(spec, tmp_path)
    assert ".ea/local/research/2026-05-19-missing.md" in str(excinfo.value)


# ---- validate_phase_spec_has_kpis -------------------------------------------


def test_validate_phase_spec_has_kpis_passes_with_one_kpi() -> None:
    spec = _phase_spec_factory(
        kpis=[
            PhaseKPI(
                metric="agent_eu_total",
                target=100.0,
                direction="min",
                threshold_kind="soft",
            )
        ],
    )
    validate_phase_spec_has_kpis(spec)


def test_validate_phase_spec_has_kpis_raises_when_empty() -> None:
    spec = _phase_spec_factory()
    assert spec.kpis == []
    with pytest.raises(SpecValidationError) as excinfo:
        validate_phase_spec_has_kpis(spec)
    assert "P25" in str(excinfo.value)
    assert "kpis=[]" in str(excinfo.value)


# ---- SpecValidationError ----------------------------------------------------


def test_spec_validation_error_inherits_from_value_error() -> None:
    # CLI dispatch wants one ``except ValueError`` branch to cover
    # both pydantic.ValidationError (already a ValueError subclass) and
    # the loader-side errors.
    assert issubclass(SpecValidationError, ValueError)
