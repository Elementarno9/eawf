"""A compiled Run spec is immutable down to its run scope.

A sealed :class:`CompiledRunSpec` carries a ``scope_digest`` taken over
its run scope, so the scope has to refuse an in-place edit the way the
spec itself does. A writable scope would let ``purpose`` or
``write_set`` change after sealing, bypass the scope's own mutation
rule, leave the digest stale, and make the spec unhashable. Every scope
variant is pinned frozen, and the sealed spec is pinned hashable with
equal specs hashing equal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from eawf.kernel.runtime.compiled import CompiledRunSpec, canonical_digest
from eawf.kernel.state.epoch2.run import RunPurpose, RunScope
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx

pytestmark = pytest.mark.unit

#: ``tests/fixtures/epoch2`` - three levels up lands on ``tests/``.
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "epoch2"
SCOPES = TypeAdapter(RunScope)
WRITE_SET = ("src/eawf/kernel/runtime/provider.py",)
OTHER_TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0043"


def _compile(**request: Any) -> CompiledRunSpec:
    return compile_run_spec(
        fx.task_request(**request),
        configuration=fx.configuration(),
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
    )


def _fixture_scopes() -> list[dict[str, Any]]:
    parsed = yaml.safe_load((FIXTURES / "run_scopes.yaml").read_text(encoding="utf-8"))
    return list(parsed["scopes"])


def _assert_still_sealed(spec: CompiledRunSpec) -> None:
    assert spec.scope_digest == canonical_digest(spec.run_scope.model_dump(mode="json"))
    assert CompiledRunSpec.model_validate(spec.model_dump()) == spec


# ---- nested run_scope assignment ---------------------------------------------


@pytest.mark.parametrize("purpose", list(RunPurpose), ids=lambda purpose: purpose.value)
def test_compiled_run_spec_refuses_run_scope_purpose_assignment(purpose: RunPurpose) -> None:
    spec = _compile()
    with pytest.raises(ValidationError, match="frozen"):
        spec.run_scope.purpose = purpose
    assert spec.run_scope.purpose is RunPurpose.IMPLEMENT
    _assert_still_sealed(spec)


@pytest.mark.parametrize(
    "write_set",
    [
        (),
        ("src/eawf/kernel/state/epoch2/run.py",),
        (*WRITE_SET, "src/eawf/workflow/runtime/compile.py"),
        ("../outside/the/repository.py",),
    ],
    ids=["empty", "single", "widened", "escaping"],
)
def test_compiled_run_spec_refuses_run_scope_write_set_assignment(
    write_set: tuple[str, ...],
) -> None:
    spec = _compile()
    with pytest.raises(ValidationError, match="frozen"):
        spec.run_scope.write_set = write_set
    assert spec.run_scope.write_set == WRITE_SET
    _assert_still_sealed(spec)


@pytest.mark.parametrize(
    ("field", "value"),
    [("task_ref", OTHER_TASK_URN), ("scope_kind", "batch")],
)
def test_compiled_run_spec_refuses_run_scope_reference_assignment(field: str, value: str) -> None:
    spec = _compile()
    before = spec.run_scope.model_dump(mode="json")
    with pytest.raises(ValidationError, match="frozen"):
        setattr(spec.run_scope, field, value)
    assert spec.run_scope.model_dump(mode="json") == before
    _assert_still_sealed(spec)


def test_compiled_run_spec_refuses_run_scope_replacement() -> None:
    spec = _compile()
    replacement = spec.run_scope.model_copy(update={"write_set": ()})
    with pytest.raises(ValidationError, match="frozen"):
        spec.run_scope = replacement
    assert spec.run_scope.write_set == WRITE_SET


# ---- hashability --------------------------------------------------------------


def test_compiled_run_spec_is_hashable() -> None:
    first, second = _compile(), _compile()
    assert hash(first) == hash(second)
    assert len({first, second}) == 1
    assert {first: "sealed"}[second] == "sealed"


def test_compiled_run_spec_distinct_scopes_stay_distinct_members() -> None:
    scope = {**fx.task_request().run_scope.model_dump(mode="json"), "write_set": ["src/other.py"]}
    first, other = _compile(), _compile(run_scope=scope)
    assert first.scope_digest != other.scope_digest
    assert len({first, other}) == 2


# ---- every scope variant -----------------------------------------------------


@pytest.mark.parametrize(
    "document", _fixture_scopes(), ids=lambda document: str(document["scope_kind"])
)
def test_run_scope_variant_refuses_assignment(document: dict[str, Any]) -> None:
    scope = SCOPES.validate_python(document)
    with pytest.raises(ValidationError, match="frozen"):
        scope.purpose = RunPurpose.OBSERVE
    with pytest.raises(ValidationError, match="frozen"):
        scope.write_set = ()
    assert scope == SCOPES.validate_python(document)
    assert hash(scope) == hash(SCOPES.validate_python(document))
