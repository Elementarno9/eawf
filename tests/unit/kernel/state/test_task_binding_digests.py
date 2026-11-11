"""A Task's integrated binding loads with or without its two digests.

Nothing ever recomputed a Task binding's contract or evidence digest, so
new rows leave them out. Rows already stored with both still load and
re-serialize byte for byte, and no module reads either key off a Task
binding, so a row without them breaks no verifier.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

import eawf
from eawf.kernel.state.epoch2 import Task
from eawf.kernel.state.epoch2.values import TaskIntegratedBinding

pytestmark = pytest.mark.unit

DIGEST_KEYS: Final = frozenset({"contract_digest", "evidence_digest"})

#: The modules that spell a digest key as a string for a shape other than a
#: Task row: the sealed capsule, the compiled contract and the semantic call
#: a capsule check reads. None of them walks Task rows.
NON_TASK_SPELLERS: Final = frozenset(
    {
        "kernel/runtime/capsule.py",
        "kernel/runtime/compiled.py",
        "runtime/daemon/semantic_gateway.py",
        "runtime/mcp/semantic_stdio.py",
    }
)

SRC: Final = Path(eawf.__file__).parent

NEW_BINDING: Final[dict[str, Any]] = {
    "head_sha": "a" * 40,
    "tree_sha": "b" * 40,
    "policy_revision": 1,
}
OLD_BINDING: Final[dict[str, Any]] = {
    "head_sha": "a" * 40,
    "tree_sha": "b" * 40,
    "contract_digest": "sha256:" + "c" * 64,
    "policy_revision": 1,
    "evidence_digest": "sha256:" + "d" * 64,
}


def _completed_row(binding: dict[str, Any]) -> dict[str, Any]:
    """Return a stored COMPLETED Task row carrying *binding*."""
    return {
        "uid": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "key": "EAWF-0042",
        "urn": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042",
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 3,
        "created_at": "2026-09-08T00:00:00Z",
        "updated_at": "2026-09-09T00:00:00Z",
        "batch_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007",
        "priority": "P1",
        "due_scope": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030",
        "intent": "Publish the wheel to the index",
        "contract_revision": 1,
        "criteria": [
            {
                "id": "CR-01",
                "text": "the published wheel installs into a clean environment",
                "kind": "functional_suitability",
                "acceptance_style": "binary",
                "evidence_kind": "deterministic",
                "gate_ids": ["G-01"],
                "quality_dimension": "functional_suitability",
                "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
            }
        ],
        "status": "COMPLETED",
        "active_run_ref": None,
        "integrated_binding": binding,
    }


# ---- stored rows keep loading ------------------------------------------------


def test_old_rows_load() -> None:
    old = Task.model_validate(_completed_row(OLD_BINDING))
    new = Task.model_validate(_completed_row(NEW_BINDING))

    assert isinstance(old.integrated_binding, TaskIntegratedBinding)
    assert old.integrated_binding.contract_digest == OLD_BINDING["contract_digest"]
    assert old.integrated_binding.evidence_digest == OLD_BINDING["evidence_digest"]
    assert old.model_dump(mode="json")["integrated_binding"] == OLD_BINDING
    assert new.integrated_binding is not None
    assert new.integrated_binding.contract_digest is None
    dumped = new.model_dump(mode="json")["integrated_binding"]
    assert dumped == NEW_BINDING
    assert DIGEST_KEYS.isdisjoint(dumped)


@pytest.mark.parametrize("kept", sorted(DIGEST_KEYS))
def test_a_row_with_one_digest_keeps_only_that_one(kept: str) -> None:
    binding = TaskIntegratedBinding.model_validate({**NEW_BINDING, kept: OLD_BINDING[kept]})

    assert binding.model_dump(mode="json") == {**NEW_BINDING, kept: OLD_BINDING[kept]}


def test_a_new_binding_round_trips_through_json() -> None:
    binding = TaskIntegratedBinding.model_validate(NEW_BINDING)

    assert TaskIntegratedBinding.model_validate_json(binding.model_dump_json()) == binding


# ---- the binding stays strict ------------------------------------------------


@pytest.mark.parametrize("missing", ["head_sha", "tree_sha", "policy_revision"])
def test_a_binding_missing_a_required_member_is_refused(missing: str) -> None:
    fields = {key: value for key, value in NEW_BINDING.items() if key != missing}

    with pytest.raises(ValidationError, match=missing):
        TaskIntegratedBinding.model_validate(fields)


def test_an_empty_binding_is_refused() -> None:
    with pytest.raises(ValidationError):
        TaskIntegratedBinding.model_validate({})


def test_an_unknown_member_is_refused() -> None:
    with pytest.raises(ValidationError, match="extra"):
        TaskIntegratedBinding.model_validate({**NEW_BINDING, "parent_sha": "e" * 40})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_digest", "c" * 64),
        ("evidence_digest", "sha256:" + "d" * 63),
        ("head_sha", "a" * 39),
        ("tree_sha", "b" * 41),
        ("policy_revision", 0),
        ("policy_revision", "1"),
    ],
)
def test_a_malformed_member_is_refused(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        TaskIntegratedBinding.model_validate({**OLD_BINDING, field: value})


def test_a_non_mapping_binding_is_refused() -> None:
    with pytest.raises(ValidationError):
        TaskIntegratedBinding.model_validate(["a" * 40, "b" * 40, 1])


def test_the_binding_is_frozen() -> None:
    binding = TaskIntegratedBinding.model_validate(NEW_BINDING)

    with pytest.raises(ValidationError, match="frozen"):
        binding.head_sha = "f" * 40  # type: ignore[misc]


def test_a_completed_task_still_requires_its_binding() -> None:
    row = _completed_row(NEW_BINDING)
    row["integrated_binding"] = None

    with pytest.raises(ValidationError, match="requires integrated_binding"):
        Task.model_validate(row)


# ---- no reader depends on the digests ----------------------------------------


def _readers(root: Path) -> list[str]:
    """Return each place under *root* that could read a digest off a Task binding.

    A string spelling of either key is a dynamic read any row walker can
    apply to a Task row, so it counts unless its module addresses another
    shape. An attribute read of either key counts in any module that
    touches ``integrated_binding``.
    """
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        touches_task_binding = "integrated_binding" in source
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and node.value in DIGEST_KEYS
                and relative not in NON_TASK_SPELLERS
            ):
                found.append(f"{relative}:{node.lineno} spells {node.value!r}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr in DIGEST_KEYS
                and isinstance(node.ctx, ast.Load)
                and touches_task_binding
            ):
                found.append(f"{relative}:{node.lineno} reads .{node.attr}")
    return found


def test_no_reader_of_task_digests() -> None:
    assert _readers(SRC) == []
    assert {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")} >= NON_TASK_SPELLERS, (
        "an allowed speller moved; re-check what it reads"
    )


def test_the_reader_scan_finds_a_row_walker_keyed_on_a_digest(tmp_path: Path) -> None:
    (tmp_path / "walker.py").write_text(
        '_MARK = "contract_digest"\n\ndef bound(row):\n    return _MARK in row\n',
        encoding="utf-8",
    )

    assert _readers(tmp_path) == ["walker.py:1 spells 'contract_digest'"]


def test_the_reader_scan_finds_an_attribute_read_beside_a_task_binding(
    tmp_path: Path,
) -> None:
    (tmp_path / "check.py").write_text(
        "def check(task):\n    return task.integrated_binding.evidence_digest\n",
        encoding="utf-8",
    )

    assert _readers(tmp_path) == ["check.py:2 reads .evidence_digest"]


def test_the_reader_scan_passes_an_empty_tree(tmp_path: Path) -> None:
    assert _readers(tmp_path) == []
