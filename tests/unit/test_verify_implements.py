"""Unit tests for :mod:`eawf.audit_dsl.kinds.verify_implements`.

Coverage:

* ``AuditSpec`` schema — cadence enum + extra="forbid" + non-empty
  checks + scope_urn / audit_kind / id required.
* ``VERDICT_MARKER_RE`` — accepts ``#`` / ``//`` / ``<!--`` comment
  hosts, accepts ``V12`` / ``V12-RC3`` / ``D17`` / ``H03-12``,
  rejects malformed ids.
* ``_parse_frontmatter`` — happy path + missing-open + missing-close
  + non-mapping body.
* ``_cadence_matches`` — every-wave/iter/phase/manual matrix +
  invalid cadence + invalid trigger.
* ``check_verify_implements`` — cadence short-circuit, missing spec
  dir, no WaveSpec files, empty diff (no scope changes), missing
  markers, all markers present, multiple waves, missing arg.
* registry lookup — ``CHECK_REGISTRY['verify_implements']`` resolves
  to the kind class.

All tests run against a tmp ``.ea/specs/<phase>/`` dir + monkey-
patched ``_git_diff_files`` (the integration test in
:mod:`tests.integration.test_verify_implements_cadence` exercises the
real ``git diff`` path).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.audit_dsl import CHECK_REGISTRY, CheckSpec
from eawf.audit_dsl.kinds.verify_implements import (
    VERDICT_MARKER_RE,
    _cadence_matches,
    _parse_frontmatter,
    check_verify_implements,
)
from eawf.audit_dsl.models import CheckKind
from eawf.kernel.spec.audit import AUDIT_CADENCE_VALUES, AuditSpec
from eawf.kernel.state.enums import AuditKind

# ---- AuditSpec schema -------------------------------------------------------


def _audit_spec_factory(**overrides: Any) -> AuditSpec:
    defaults: dict[str, Any] = {
        "id": "AU-P25-verify",
        "scope_urn": "urn:eawf:v1:phase:eawf/P25",
        "audit_kind": AuditKind.SHIP_GATE,
        "cadence": "every-phase",
        "checks": [
            CheckSpec(
                kind="verify_implements",
                name="walk-p25",
                args={"phase_id": "P25"},
            )
        ],
    }
    defaults.update(overrides)
    return AuditSpec.model_validate(defaults)


def test_audit_spec_minimal() -> None:
    spec = _audit_spec_factory()
    assert spec.schema_version == "1.0"
    assert spec.kind == "AuditSpec"
    assert spec.fail_fast is False
    assert spec.implements == []


@pytest.mark.parametrize("cadence", sorted(AUDIT_CADENCE_VALUES))
def test_audit_spec_accepts_each_cadence(cadence: str) -> None:
    spec = _audit_spec_factory(cadence=cadence)
    assert spec.cadence == cadence


def test_audit_spec_rejects_unknown_cadence() -> None:
    with pytest.raises(ValidationError):
        _audit_spec_factory(cadence="every-orbit")


def test_audit_spec_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        AuditSpec.model_validate(
            {
                "id": "AU-1",
                "scope_urn": "urn:eawf:v1:phase:eawf/P25",
                "audit_kind": "ship-gate",
                "cadence": "every-phase",
                "checks": [
                    {
                        "kind": "verify_implements",
                        "name": "x",
                        "args": {"phase_id": "P25"},
                    }
                ],
                "extra": "nope",
            }
        )


def test_audit_spec_rejects_empty_checks() -> None:
    with pytest.raises(ValidationError):
        _audit_spec_factory(checks=[])


def test_audit_spec_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValidationError):
        _audit_spec_factory(schema_version="2.0")


def test_audit_spec_rejects_wrong_kind() -> None:
    with pytest.raises(ValidationError):
        _audit_spec_factory(kind="WaveSpec")


def test_audit_spec_audit_kind_must_be_enum_value() -> None:
    with pytest.raises(ValidationError):
        _audit_spec_factory(audit_kind="bogus")


# ---- VERDICT_MARKER_RE ------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("# IMPLEMENTS: V12", "V12"),
        ("// IMPLEMENTS: V12-RC3", "V12-RC3"),
        ("<!-- IMPLEMENTS: H03-12 -->", "H03-12"),
        ("    # IMPLEMENTS:    D17", "D17"),
        ("# IMPLEMENTS: R5", "R5"),
    ],
)
def test_verdict_marker_re_accepts(line: str, expected: str) -> None:
    match = VERDICT_MARKER_RE.search(line)
    assert match is not None
    assert match.group(1) == expected


@pytest.mark.parametrize(
    "line",
    [
        "IMPLEMENTS: V12",  # no comment host
        "# IMPLEMENT: V12",  # wrong keyword
        "# IMPLEMENTS: v12",  # lowercase rejected by VerdictIdStr regex
        "# IMPLEMENTS: 12V",  # leading digit rejected
        "# IMPLEMENTS: X12",  # unknown letter
    ],
)
def test_verdict_marker_re_rejects(line: str) -> None:
    assert VERDICT_MARKER_RE.search(line) is None


def test_verdict_marker_re_multiple_in_file() -> None:
    body = """\
# IMPLEMENTS: V12
def foo():
    # IMPLEMENTS: D17
    pass
"""
    seen = [m.group(1) for m in VERDICT_MARKER_RE.finditer(body)]
    assert seen == ["V12", "D17"]


# ---- _parse_frontmatter -----------------------------------------------------


def test_parse_frontmatter_happy() -> None:
    body = """\
---
kind: WaveSpec
id: P25-I01-W01
---
markdown body
"""
    parsed = _parse_frontmatter(body)
    assert parsed == {"kind": "WaveSpec", "id": "P25-I01-W01"}


def test_parse_frontmatter_no_open() -> None:
    body = "no frontmatter here\n"
    assert _parse_frontmatter(body) is None


def test_parse_frontmatter_no_close() -> None:
    body = "---\nkind: WaveSpec\nbody without close marker\n"
    assert _parse_frontmatter(body) is None


def test_parse_frontmatter_empty_block() -> None:
    body = "---\n---\nbody\n"
    assert _parse_frontmatter(body) is None


def test_parse_frontmatter_non_mapping_raises() -> None:
    body = "---\n- a\n- b\n---\nbody\n"
    with pytest.raises(ValueError, match="not a mapping"):
        _parse_frontmatter(body)


# ---- _cadence_matches -------------------------------------------------------


@pytest.mark.parametrize(
    "cadence, current_trigger, expected",
    [
        ("every-wave", "every-wave", True),
        ("every-iter", "every-iter", True),
        ("every-phase", "every-phase", True),
        ("manual", "manual", True),
        ("every-wave", "every-iter", False),
        ("every-wave", "every-phase", False),
        ("every-phase", "every-wave", False),
        ("every-phase", "manual", False),
        ("manual", "every-phase", False),
    ],
)
def test_cadence_matches_matrix(cadence: str, current_trigger: str, expected: bool) -> None:
    assert _cadence_matches(cadence, current_trigger) is expected


def test_cadence_matches_rejects_unknown_cadence() -> None:
    with pytest.raises(ValueError, match="unknown cadence"):
        _cadence_matches("bogus", "every-phase")


def test_cadence_matches_rejects_unknown_trigger() -> None:
    with pytest.raises(ValueError, match="unknown current_trigger"):
        _cadence_matches("every-phase", "bogus")


# ---- check_verify_implements ------------------------------------------------


def _write_wave_spec_file(
    spec_dir: Path,
    *,
    wave_id: str,
    iter_id: str,
    phase_id: str,
    file_scopes: list[str],
    implements: list[dict[str, Any]],
) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    frontmatter_lines = [
        "---",
        "kind: WaveSpec",
        f"id: {wave_id}",
        f"iter_id: {iter_id}",
        f"phase_id: {phase_id}",
        f"title: {wave_id} title",
        "agent_role: executor",
        "effort_bucket: L",
        "file_scopes:",
    ]
    frontmatter_lines.extend(f"  - {p}" for p in file_scopes)
    frontmatter_lines.append("implements:")
    for cit in implements:
        frontmatter_lines.append(f"  - verdict_id: {cit['verdict_id']}")
        frontmatter_lines.append(f"    brief: {cit['brief']}")
    frontmatter_lines.append("behaviors:")
    frontmatter_lines.append("  - id: B1")
    frontmatter_lines.append(
        "    text: observable behaviour described in twenty characters or more"
    )
    frontmatter_lines.append("failure_modes:")
    frontmatter_lines.append("  - drift between spec and implementation")
    frontmatter_lines.append("---")
    frontmatter_lines.append("body")
    target = spec_dir / f"{wave_id}.md"
    target.write_text("\n".join(frontmatter_lines) + "\n", encoding="utf-8")
    return target


@pytest.fixture
def fake_phase_tree(tmp_path: Path) -> Path:
    spec_dir = tmp_path / ".ea" / "specs" / "P25"
    _write_wave_spec_file(
        spec_dir,
        wave_id="P25-I01-W01",
        iter_id="P25-I01",
        phase_id="P25",
        file_scopes=["src/eawf/spec/common.py"],
        implements=[
            {
                "verdict_id": "V12",
                "brief": ".ea/artifacts/research/2026-05-16-c03-spec-infrastructure.md",
            }
        ],
    )
    return tmp_path


def _build_spec(args: dict[str, Any]) -> CheckSpec:
    return CheckSpec(kind="verify_implements", name="vi-test", args=args)


def test_cadence_short_circuit_returns_pass(
    fake_phase_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cadence=every-wave but trigger=every-phase → short-circuit pass
    def fail_if_called(*_args: Any, **_kwargs: Any) -> set[str]:
        raise AssertionError("git diff must not run when cadence skips")

    monkeypatch.setattr(
        "eawf.audit_dsl.kinds.verify_implements._git_diff_files",
        fail_if_called,
    )
    spec = _build_spec(
        {
            "phase_id": "P25",
            "diff_base": "main",
            "cadence": "every-wave",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, fake_phase_tree)
    assert result.passed is True
    assert result.details is not None
    assert "skipped" in result.details
    assert "cadence=every-wave" in result.details


def test_missing_phase_dir_fails(tmp_path: Path) -> None:
    spec = _build_spec(
        {
            "phase_id": "P99",
            "cadence": "every-phase",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, tmp_path)
    assert result.passed is False
    assert result.details is not None
    assert "no spec dir at .ea/specs/P99" in result.details


def test_phase_dir_with_no_wavespec_fails(tmp_path: Path) -> None:
    # Phase dir exists but contains no WaveSpec frontmatter files.
    (tmp_path / ".ea" / "specs" / "P25").mkdir(parents=True)
    (tmp_path / ".ea" / "specs" / "P25" / "spec.md").write_text(
        "---\nkind: PhaseSpec\n---\nbody\n", encoding="utf-8"
    )
    spec = _build_spec(
        {
            "phase_id": "P25",
            "cadence": "every-phase",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, tmp_path)
    assert result.passed is False
    assert result.details is not None
    assert "no WaveSpec files" in result.details


def test_no_files_in_diff_fails(fake_phase_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "eawf.audit_dsl.kinds.verify_implements._git_diff_files",
        lambda cwd, diff_base: set(),
    )
    spec = _build_spec(
        {
            "phase_id": "P25",
            "cadence": "every-phase",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, fake_phase_tree)
    assert result.passed is False
    assert result.details is not None
    assert "unmet verify-implements: wave='P25-I01-W01'" in result.details
    assert "expected_marker=V12" in result.details
    assert "(no file_scopes in diff)" in result.details


def test_marker_missing_in_changed_file_fails(
    fake_phase_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_phase_tree / "src" / "eawf" / "spec").mkdir(parents=True, exist_ok=True)
    target = fake_phase_tree / "src" / "eawf" / "spec" / "common.py"
    target.write_text("# unrelated comment\nvalue = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        "eawf.audit_dsl.kinds.verify_implements._git_diff_files",
        lambda cwd, diff_base: {"src/eawf/spec/common.py"},
    )
    spec = _build_spec(
        {
            "phase_id": "P25",
            "cadence": "every-phase",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, fake_phase_tree)
    assert result.passed is False
    assert result.details is not None
    assert "wave='P25-I01-W01'" in result.details
    assert "expected_marker=V12" in result.details


def test_marker_present_passes(fake_phase_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (fake_phase_tree / "src" / "eawf" / "spec").mkdir(parents=True, exist_ok=True)
    target = fake_phase_tree / "src" / "eawf" / "spec" / "common.py"
    target.write_text("# IMPLEMENTS: V12\nvalue = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        "eawf.audit_dsl.kinds.verify_implements._git_diff_files",
        lambda cwd, diff_base: {"src/eawf/spec/common.py"},
    )
    spec = _build_spec(
        {
            "phase_id": "P25",
            "cadence": "every-phase",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, fake_phase_tree)
    assert result.passed is True
    assert result.details is not None
    assert "all WaveSpec.implements markers satisfied" in result.details


def test_marker_in_unrelated_file_does_not_satisfy(
    fake_phase_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Marker lives in src/eawf/foo.py but the wave's file_scopes is
    # only src/eawf/spec/common.py — kind must NOT grep outside scope.
    (fake_phase_tree / "src" / "eawf").mkdir(parents=True, exist_ok=True)
    (fake_phase_tree / "src" / "eawf" / "foo.py").write_text(
        "# IMPLEMENTS: V12\n", encoding="utf-8"
    )
    (fake_phase_tree / "src" / "eawf" / "spec").mkdir(parents=True, exist_ok=True)
    (fake_phase_tree / "src" / "eawf" / "spec" / "common.py").write_text(
        "# unrelated\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "eawf.audit_dsl.kinds.verify_implements._git_diff_files",
        lambda cwd, diff_base: {
            "src/eawf/foo.py",
            "src/eawf/spec/common.py",
        },
    )
    spec = _build_spec(
        {
            "phase_id": "P25",
            "cadence": "every-phase",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, fake_phase_tree)
    assert result.passed is False
    assert result.details is not None
    assert "expected_marker=V12" in result.details


def test_multiple_waves_partial_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_dir = tmp_path / ".ea" / "specs" / "P25"
    _write_wave_spec_file(
        spec_dir,
        wave_id="P25-I01-W01",
        iter_id="P25-I01",
        phase_id="P25",
        file_scopes=["src/eawf/spec/common.py"],
        implements=[
            {
                "verdict_id": "V12",
                "brief": ".ea/artifacts/research/2026-05-16-c03-spec-infrastructure.md",
            }
        ],
    )
    _write_wave_spec_file(
        spec_dir,
        wave_id="P25-I01-W02",
        iter_id="P25-I01",
        phase_id="P25",
        file_scopes=["src/eawf/audit_dsl/kinds/verify_implements.py"],
        implements=[
            {
                "verdict_id": "D10",
                "brief": ".ea/artifacts/research/2026-05-16-c03-spec-infrastructure.md",
            }
        ],
    )
    (tmp_path / "src" / "eawf" / "spec").mkdir(parents=True)
    (tmp_path / "src" / "eawf" / "spec" / "common.py").write_text(
        "# IMPLEMENTS: V12\n", encoding="utf-8"
    )
    (tmp_path / "src" / "eawf" / "audit_dsl" / "kinds").mkdir(parents=True)
    (tmp_path / "src" / "eawf" / "audit_dsl" / "kinds" / "verify_implements.py").write_text(
        "# no marker here\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "eawf.audit_dsl.kinds.verify_implements._git_diff_files",
        lambda cwd, diff_base: {
            "src/eawf/spec/common.py",
            "src/eawf/audit_dsl/kinds/verify_implements.py",
        },
    )
    spec = _build_spec(
        {
            "phase_id": "P25",
            "cadence": "every-phase",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, tmp_path)
    assert result.passed is False
    assert result.details is not None
    assert "wave='P25-I01-W02'" in result.details
    assert "expected_marker=D10" in result.details
    # W01 satisfied, W02 not — only W02 appears in the diagnostic
    assert "wave='P25-I01-W01'" not in result.details


def test_missing_phase_id_raises() -> None:
    spec = _build_spec({})
    with pytest.raises(ValueError, match="missing or non-str arg 'phase_id'"):
        check_verify_implements(spec, Path("/tmp"))


def test_non_str_phase_id_raises() -> None:
    spec = CheckSpec(kind="verify_implements", name="x", args={"phase_id": 25})
    with pytest.raises(ValueError, match="missing or non-str arg 'phase_id'"):
        check_verify_implements(spec, Path("/tmp"))


def test_empty_diff_base_raises() -> None:
    spec = _build_spec({"phase_id": "P25", "diff_base": ""})
    with pytest.raises(ValueError, match="arg 'diff_base'"):
        check_verify_implements(spec, Path("/tmp"))


def test_non_str_cadence_raises() -> None:
    spec = CheckSpec(
        kind="verify_implements",
        name="x",
        args={"phase_id": "P25", "cadence": 7},
    )
    with pytest.raises(ValueError, match="arg 'cadence'"):
        check_verify_implements(spec, Path("/tmp"))


def test_non_str_current_trigger_raises() -> None:
    spec = CheckSpec(
        kind="verify_implements",
        name="x",
        args={"phase_id": "P25", "current_trigger": 7},
    )
    with pytest.raises(ValueError, match="arg 'current_trigger'"):
        check_verify_implements(spec, Path("/tmp"))


# ---- registry binding -------------------------------------------------------


def test_verify_implements_in_check_registry() -> None:
    assert "verify_implements" in CHECK_REGISTRY
    # The bound callable is the kind module's exported function.
    assert CHECK_REGISTRY["verify_implements"] is check_verify_implements


def test_check_kind_literal_includes_verify_implements() -> None:
    from typing import get_args

    assert "verify_implements" in set(get_args(CheckKind))
