"""The ten Run scopes and the one of them that may write.

Two halves are pinned here. Every variant the committed fixture spells
parses into its own class, so a scope a caller can construct is a scope
the loader admits and no eleventh shape sneaks in through a nullable
field. And the write rule holds in both directions: a write set or a
mutating purpose outside the task scope is refused, and a mutating
task-scoped Run with no write set is refused too, because a run that
intends to write and bounds nothing has declared no limit at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from eawf.kernel.state.epoch2 import (
    BatchScope,
    CampaignScope,
    ClaimScope,
    EvidenceScope,
    MilestoneScope,
    QuestionScope,
    ReleaseScope,
    RepositoryScope,
    RunPurpose,
    RunScope,
    TaskScope,
    WorkspaceScope,
)

pytestmark = pytest.mark.unit

#: ``tests/fixtures/epoch2`` - three levels up lands on ``tests/``.
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "epoch2"

TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
BATCH_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"

#: The class each ``scope_kind`` must parse into.
VARIANTS: dict[str, type] = {
    "task": TaskScope,
    "batch": BatchScope,
    "milestone": MilestoneScope,
    "campaign": CampaignScope,
    "release": ReleaseScope,
    "repository": RepositoryScope,
    "workspace": WorkspaceScope,
    "evidence": EvidenceScope,
    "claim": ClaimScope,
    "question": QuestionScope,
}

SCOPES = TypeAdapter(RunScope)


def _fixture_scopes() -> list[dict[str, Any]]:
    """Return the committed run-scope documents as fresh mappings."""
    parsed = yaml.safe_load((FIXTURES / "run_scopes.yaml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    documents = parsed["scopes"]
    assert isinstance(documents, list)
    return documents


def _task_document(**overrides: Any) -> dict[str, Any]:
    """Return a valid task-scope document with *overrides* applied."""
    document: dict[str, Any] = {
        "scope_kind": "task",
        "purpose": "implement",
        "task_ref": TASK_URN,
        "write_set": ["src/eawf/kernel/state/epoch2/run.py"],
    }
    document.update(overrides)
    return document


def test_run_scope_parses_every_fixture_variant() -> None:
    """The fixture spells all ten variants and each parses into its class."""
    documents = _fixture_scopes()
    parsed = [SCOPES.validate_python(document) for document in documents]
    assert [document["scope_kind"] for document in documents] == list(VARIANTS)
    assert [type(scope) for scope in parsed] == list(VARIANTS.values())


def test_run_scope_round_trips_every_fixture_variant() -> None:
    """Dumping a parsed variant reproduces the document it came from."""
    for document in _fixture_scopes():
        dumped = SCOPES.dump_python(SCOPES.validate_python(document), mode="json")
        assert {key: dumped[key] for key in document} == document


def test_run_scope_rejects_a_null_task_id() -> None:
    """A task scope with no task is not a task scope."""
    with pytest.raises(ValidationError, match="task_ref"):
        SCOPES.validate_python(_task_document(task_ref=None))


def test_run_scope_rejects_a_missing_task_id() -> None:
    """The typed reference is required, not defaulted to absent."""
    document = _task_document()
    del document["task_ref"]
    with pytest.raises(ValidationError, match="task_ref"):
        SCOPES.validate_python(document)


def test_run_scope_rejects_mixed_scope_fields() -> None:
    """A document holding two scopes' references has two answers."""
    with pytest.raises(ValidationError, match="batch_ref"):
        SCOPES.validate_python(_task_document(batch_ref=BATCH_URN))


def test_run_scope_rejects_an_untyped_scope_id() -> None:
    """A bare ``scope_id`` string is refused; the reference is typed."""
    with pytest.raises(ValidationError, match="scope_id"):
        SCOPES.validate_python(_task_document(scope_id="EAWF-0042"))


def test_run_scope_rejects_a_reference_of_another_kind() -> None:
    """A batch scope holding a task URN is refused at the loader."""
    with pytest.raises(ValidationError, match="task"):
        SCOPES.validate_python({"scope_kind": "batch", "purpose": "review", "batch_ref": TASK_URN})


def test_run_scope_rejects_an_unknown_scope_kind() -> None:
    """An eleventh discriminator value has no variant to parse into."""
    with pytest.raises(ValidationError, match="scope_kind"):
        SCOPES.validate_python({"scope_kind": "phase", "purpose": "plan"})


@pytest.mark.parametrize("purpose", sorted(RunPurpose.__members__))
def test_run_scope_accepts_every_purpose_on_a_task_scope(purpose: str) -> None:
    """The task scope is the one scope every purpose is legal on."""
    scope = SCOPES.validate_python(_task_document(purpose=RunPurpose[purpose].value))
    assert isinstance(scope, TaskScope)


@pytest.mark.parametrize("purpose", ["implement", "integrate", "repair"])
def test_run_scope_rejects_a_mutating_purpose_outside_task_scope(purpose: str) -> None:
    """Only a task-scoped Run owns a worktree a mutating purpose needs."""
    with pytest.raises(ValidationError, match="mutates a repository"):
        SCOPES.validate_python({"scope_kind": "batch", "purpose": purpose, "batch_ref": BATCH_URN})


def test_run_scope_rejects_a_write_set_outside_task_scope() -> None:
    """A scope with nothing to write to may not declare a write set."""
    with pytest.raises(ValidationError, match="task-scoped Run"):
        SCOPES.validate_python(
            {
                "scope_kind": "batch",
                "purpose": "review",
                "batch_ref": BATCH_URN,
                "write_set": ["src/eawf/a.py"],
            }
        )


def test_run_scope_accepts_an_empty_write_set_outside_task_scope() -> None:
    """The empty write set is the boundary a read-only scope sits at."""
    scope = SCOPES.validate_python(
        {"scope_kind": "batch", "purpose": "review", "batch_ref": BATCH_URN, "write_set": []}
    )
    assert scope.write_set == ()


def test_run_scope_rejects_a_mutating_purpose_with_an_empty_write_set() -> None:
    """A run that intends to write must bound what it may touch."""
    with pytest.raises(ValidationError, match="write_set must name"):
        SCOPES.validate_python(_task_document(write_set=[]))


def test_run_scope_accepts_a_read_only_purpose_with_an_empty_write_set() -> None:
    """A task-scoped review writes nothing, so it bounds nothing."""
    scope = SCOPES.validate_python(_task_document(purpose="review", write_set=[]))
    assert isinstance(scope, TaskScope)
    assert scope.write_set == ()


def test_run_scope_accepts_a_single_write_set_path() -> None:
    """One path is the smallest bounded write set."""
    scope = SCOPES.validate_python(_task_document(write_set=["a"]))
    assert scope.write_set == ("a",)


def test_run_scope_rejects_an_escaping_write_set_path() -> None:
    """A ``..`` segment walks out of the tree the write set bounds."""
    with pytest.raises(ValidationError, match="walks out of the repository"):
        SCOPES.validate_python(_task_document(write_set=["src/../../etc/passwd"]))


def test_run_scope_rejects_an_absolute_write_set_path() -> None:
    """An absolute path names something outside the repository."""
    with pytest.raises(ValidationError, match="write_set"):
        SCOPES.validate_python(_task_document(write_set=["/etc/passwd"]))


def test_run_scope_rejects_a_duplicate_write_set_path() -> None:
    """One path listed twice is an authoring slip, not a wider grant."""
    with pytest.raises(ValidationError, match="same path twice"):
        SCOPES.validate_python(_task_document(write_set=["src/a.py", "src/a.py"]))


def test_run_scope_accepts_a_max_length_write_set_path() -> None:
    """500 characters is inside the bound."""
    scope = SCOPES.validate_python(_task_document(write_set=["a" * 500]))
    assert scope.write_set == ("a" * 500,)


def test_run_scope_rejects_an_over_length_write_set_path() -> None:
    """501 characters is outside it."""
    with pytest.raises(ValidationError, match="write_set"):
        SCOPES.validate_python(_task_document(write_set=["a" * 501]))


def test_run_scope_rejects_an_empty_write_set_path() -> None:
    """A blank path bounds nothing and names nothing."""
    with pytest.raises(ValidationError, match="write_set"):
        SCOPES.validate_python(_task_document(write_set=[""]))


def test_run_scope_rejects_a_non_string_write_set_path() -> None:
    """Strict scalars refuse an integer where a path belongs."""
    with pytest.raises(ValidationError, match="write_set"):
        SCOPES.validate_python(_task_document(write_set=[1]))


def test_run_scope_rejects_an_unknown_purpose() -> None:
    """The purpose vocabulary is closed."""
    with pytest.raises(ValidationError, match="purpose"):
        SCOPES.validate_python(_task_document(purpose="deploy"))
