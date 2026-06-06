"""Unit tests for the ``schema_validate`` audit-DSL kind (FS04).

Coverage:

* ``check_schema_validate`` boundary -- a valid inline target yields
  ``status="pass"``; a minimal invalid target yields ``status="fail"``.
* ``check_schema_validate`` file-path target -- a repo-relative JSON
  file resolved against ``cwd`` validates.
* ``check_schema_validate`` error-path -- malformed args (missing
  ``model`` / missing ``target`` / unimportable dotted path / non-model
  object) degrade to ``status="fail"`` rather than raising.
* registry lookup -- ``CHECK_REGISTRY['schema_validate']`` resolves to
  the kind callable and runs (CR-1).
* ``compile_gate`` accepts a deterministic ``schema_validate`` gate and
  returns a non-None CheckSpec (CR-2).
* ``CheckKind`` Literal source text contains ``"schema_validate"``
  (CR-3).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckSpec
from eawf.workflow.audit_dsl import models as models_module
from eawf.workflow.audit_dsl.kinds.schema_validate import check_schema_validate
from eawf.workflow.verify.compile import compile_gate

# A small, well-known Pydantic model with a tight shape to validate
# against. ``GateSpec`` requires id / criterion_id / kind / policy /
# cadence, so a target missing those is a deterministic failure.
_MODEL = "eawf.kernel.spec.common.GateSpec"


def _valid_gate_payload() -> dict[str, object]:
    return {
        "id": "G-1",
        "criterion_id": "CR-1",
        "kind": "schema_validate",
        "policy": "block",
        "cadence": "every-wave",
    }


# ---- boundary: valid + invalid inline targets -------------------------------


def test_check_schema_validate_valid_inline_target_passes(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="schema_validate",
        name="g-valid",
        args={"model": _MODEL, "target": _valid_gate_payload()},
    )
    result = check_schema_validate(spec, tmp_path)
    assert result.status == "pass"
    assert result.passed is True


def test_check_schema_validate_invalid_inline_target_fails(tmp_path: Path) -> None:
    # Missing every required GateSpec field -> ValidationError -> fail.
    spec = CheckSpec(
        kind="schema_validate",
        name="g-invalid",
        args={"model": _MODEL, "target": {"id": "G-1"}},
    )
    result = check_schema_validate(spec, tmp_path)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "GateSpec" in result.details or _MODEL in result.details


# ---- boundary: repo-relative JSON file target -------------------------------


def test_check_schema_validate_file_target_passes(tmp_path: Path) -> None:
    fixture = tmp_path / "gate.json"
    fixture.write_text(json.dumps(_valid_gate_payload()), encoding="utf-8")
    spec = CheckSpec(
        kind="schema_validate",
        name="g-file",
        args={"model": _MODEL, "target": "gate.json"},
    )
    result = check_schema_validate(spec, tmp_path)
    assert result.status == "pass"


def test_check_schema_validate_missing_file_target_fails(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="schema_validate",
        name="g-missing-file",
        args={"model": _MODEL, "target": "does-not-exist.json"},
    )
    result = check_schema_validate(spec, tmp_path)
    assert result.status == "fail"
    assert result.details is not None
    assert "does-not-exist.json" in result.details


# ---- error-path: malformed args degrade to fail, never raise ----------------


def test_check_schema_validate_missing_model_arg_fails(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="schema_validate",
        name="g-no-model",
        args={"target": _valid_gate_payload()},
    )
    # No pytest.raises: a malformed args dict must degrade, not raise.
    result = check_schema_validate(spec, tmp_path)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "model" in result.details


def test_check_schema_validate_missing_target_arg_fails(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="schema_validate",
        name="g-no-target",
        args={"model": _MODEL},
    )
    result = check_schema_validate(spec, tmp_path)
    assert result.status == "fail"
    assert result.details is not None
    assert "target" in result.details


def test_check_schema_validate_unimportable_model_fails(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="schema_validate",
        name="g-bad-import",
        args={"model": "eawf.does.not.exist.Nope", "target": {}},
    )
    result = check_schema_validate(spec, tmp_path)
    assert result.status == "fail"
    assert result.details is not None


def test_check_schema_validate_non_model_object_fails(tmp_path: Path) -> None:
    # ``json.dumps`` is a function, not a BaseModel subclass.
    spec = CheckSpec(
        kind="schema_validate",
        name="g-non-model",
        args={"model": "json.dumps", "target": {}},
    )
    result = check_schema_validate(spec, tmp_path)
    assert result.status == "fail"
    assert result.details is not None
    assert "BaseModel" in result.details


# ---- CR-1: registry dispatch ------------------------------------------------


def test_check_registry_schema_validate_dispatches_pass(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="schema_validate",
        name="g-registry",
        args={"model": _MODEL, "target": _valid_gate_payload()},
    )
    fn = CHECK_REGISTRY["schema_validate"]
    result = fn(spec, tmp_path)
    assert result.status == "pass"


# ---- CR-2: compile_gate accepts a deterministic schema_validate gate --------


def test_compile_gate_schema_validate_returns_checkspec() -> None:
    criterion = CriterionSpec(
        id="CR-1",
        text="the gate fixture is a valid GateSpec",
        kind="schema",
        acceptance_style="binary",
        evidence_kind="deterministic",
    )
    gate = GateSpec(
        id="G-1",
        criterion_id=criterion.id,
        kind="schema_validate",
        args={"model": _MODEL, "target": "tests/fixtures/gate.json"},
        policy="block",
        cadence="every-wave",
    )
    compiled = compile_gate(gate, criterion=criterion)
    assert compiled is not None
    assert compiled.kind == "schema_validate"
    assert compiled.name == gate.id


# ---- CR-3: CheckKind Literal source text contains "schema_validate" ---------


def test_check_kind_literal_source_contains_schema_validate() -> None:
    source = inspect.getsource(models_module)
    assert '"schema_validate"' in source
