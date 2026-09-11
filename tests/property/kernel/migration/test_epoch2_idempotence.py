"""An approved plan describes one corpus, and refuses any other.

A plan's approval digest is a promise about bytes. The refusal tested here
closes the window between taking that digest and acting on it: if the
corpus moved in between -- one field edited, one row added, one surface
rewritten -- the manifest describes rows the apply will never read, so the
plan refuses with ``migration_source_changed`` rather than running against
a corpus nobody approved.

The second property is the other side of the same promise. A plan that
names a row the cutover cannot place keeps apply refused, because applying
it would write a target census that can never be reconciled against the
source. An ambiguous row is not the same thing: ambiguity about *which
Track owns a record* is a question for an operator and the record still
imports, while a row with no target at all is a row the cutover must not
pretend to have handled.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from eawf.kernel.identity.errors import IdentityError
from eawf.kernel.migration.epoch2.errors import (
    MigrationPlanNotApplicableError,
    MigrationRowValidationError,
    MigrationSourceChangedError,
    MigrationSourceUnreadableError,
)
from eawf.kernel.migration.epoch2.lifecycle import DeferralReason
from eawf.kernel.migration.epoch2.manifest import (
    OUTCOME_TRACK_FIELD,
    UnresolvedReason,
)
from eawf.kernel.migration.epoch2.plan_mode import (
    Epoch2PlanRequest,
    MigrationPlan,
    plan_cutover,
)
from tests.property.kernel.migration.conftest import (
    ALLOWLIST_PATH,
    AMBIGUOUS_HISTORY_SNAPSHOT,
    CUTOVER_IDENTITY,
    EPOCH1_FULL_SNAPSHOT,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
MALFORMED_SNAPSHOT = FIXTURES / "epoch1-malformed" / "snapshot"
ALIAS_COLLISION_SNAPSHOT = FIXTURES / "alias-collision" / "snapshot"

SEALED_AT = datetime(2026, 1, 1, tzinfo=UTC)
SEALED_BY = "idempotence-test"

#: The five rows the full-shape corpus declares a conversion for and no
#: importer rule places yet. Measured against the fixture rather than
#: quoted from a design note: the fixture holds two decisions, one
#: incident, one sandbox policy and the project block.
FULL_UNRESOLVED_ADDRESSES = (
    "decisions/D01",
    "decisions/D02",
    "incidents/INC01",
    "project",
    "sandbox_policies/SP01",
)


def _plan(snapshot_root: Path) -> MigrationPlan:
    """Build one plan over ``snapshot_root`` under the shared test seal."""
    return plan_cutover(
        Epoch2PlanRequest(
            snapshot_root=str(snapshot_root),
            allowlist_path=str(ALLOWLIST_PATH),
            workspace_key=CUTOVER_IDENTITY.workspace_key,
            project_key=CUTOVER_IDENTITY.project_key,
            repository_key=CUTOVER_IDENTITY.repository_key,
            sealed_by=SEALED_BY,
        ),
        sealed_at=SEALED_AT,
    )


@pytest.fixture
def staged_corpus(tmp_path: Path) -> Path:
    """A writable copy of the full-shape corpus, for perturbation."""
    root = tmp_path / "snapshot"
    shutil.copytree(EPOCH1_FULL_SNAPSHOT, root)
    return root


@pytest.fixture(scope="module")
def full_plan() -> MigrationPlan:
    """One plan over the pinned full-shape corpus, built once."""
    return _plan(EPOCH1_FULL_SNAPSHOT)


def test_unchanged_source_passes_the_barrier(staged_corpus: Path) -> None:
    """The refusal is a change detector, not a blanket refusal."""
    plan = _plan(staged_corpus)
    plan.require_source_unchanged(staged_corpus)


def test_changed_document_refuses_with_migration_source_changed(
    staged_corpus: Path,
) -> None:
    """One edited field after the digest is enough to refuse the plan."""
    plan = _plan(staged_corpus)
    document = staged_corpus / "document.json"
    payload: dict[str, Any] = json.loads(document.read_text())
    payload["updated_at"] = "2027-01-01T00:00:00Z"
    document.write_text(json.dumps(payload))

    with pytest.raises(MigrationSourceChangedError) as excinfo:
        plan.require_source_unchanged(staged_corpus)
    assert excinfo.value.code == "migration_source_changed"
    assert plan.manifest.source_digest in str(excinfo.value)


def test_changed_ledger_refuses_with_migration_source_changed(
    staged_corpus: Path,
) -> None:
    """The barrier covers every pinned surface, not only the document."""
    plan = _plan(staged_corpus)
    ledger = staged_corpus / "store" / "audit.jsonl"
    ledger.write_text(ledger.read_text() + '{"id": "A999", "kind": "evaluation"}\n')

    with pytest.raises(MigrationSourceChangedError):
        plan.require_source_unchanged(staged_corpus)


def test_removed_surface_refuses_rather_than_counting_as_empty(
    staged_corpus: Path,
) -> None:
    """A deleted surface is a corpus that cannot be re-read, not an empty one."""
    plan = _plan(staged_corpus)
    (staged_corpus / "telemetry.json").unlink()

    with pytest.raises(MigrationSourceUnreadableError):
        plan.require_source_unchanged(staged_corpus)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(collection=st.sampled_from(["goals", "audits", "worktrees", "estimates"]))
def test_any_added_row_refuses_the_approved_plan(staged_corpus: Path, collection: str) -> None:
    """A row added to any collection invalidates the approval, whichever it is."""
    plan = _plan(staged_corpus)
    document = staged_corpus / "document.json"
    original = document.read_text()
    payload: dict[str, Any] = json.loads(original)
    payload[collection] = dict(payload[collection]) | {"ZZZ-INJECTED": {"id": "ZZZ-INJECTED"}}
    document.write_text(json.dumps(payload))
    try:
        with pytest.raises(MigrationSourceChangedError):
            plan.require_source_unchanged(staged_corpus)
    finally:
        document.write_text(original)


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(stamp=st.text(alphabet="0123456789-T:Z", min_size=1, max_size=24))
def test_any_edited_metadata_field_refuses_the_approved_plan(
    staged_corpus: Path, stamp: str
) -> None:
    """Whatever the edit, the refusal names the revision the plan promised."""
    plan = _plan(staged_corpus)
    document = staged_corpus / "document.json"
    original = document.read_text()
    payload: dict[str, Any] = json.loads(original)
    if payload["updated_at"] == stamp:
        return
    payload["updated_at"] = stamp
    document.write_text(json.dumps(payload))
    try:
        with pytest.raises(MigrationSourceChangedError, match=plan.manifest.source_digest):
            plan.require_source_unchanged(staged_corpus)
    finally:
        document.write_text(original)


def test_unresolved_rows_keep_apply_refused(full_plan: MigrationPlan) -> None:
    """A row with no epoch-2 target refuses the apply and names itself."""
    addresses = tuple(row.address for row in full_plan.manifest.unresolved_rows)
    assert addresses == FULL_UNRESOLVED_ADDRESSES
    assert all(
        row.reason is UnresolvedReason.NO_CONVERTER for row in full_plan.manifest.unresolved_rows
    )
    with pytest.raises(MigrationPlanNotApplicableError) as excinfo:
        full_plan.require_applicable()
    assert excinfo.value.code == "migration_plan_not_applicable"
    for address in FULL_UNRESOLVED_ADDRESSES:
        assert address in str(excinfo.value)


def test_ambiguous_track_is_an_assignment_and_still_refuses_apply() -> None:
    """Ambiguity about an owner is a question; a row with no target is a refusal."""
    plan = _plan(AMBIGUOUS_HISTORY_SNAPSHOT)
    goal = next(row for row in plan.manifest.track_assignments if row.address == "goals/G01")
    assert goal.target_field == OUTCOME_TRACK_FIELD
    assert goal.reason is DeferralReason.AMBIGUOUS_SOURCE_CANDIDATES
    assert len(goal.candidates) > 1
    assert "goals/G01" not in {row.address for row in plan.manifest.unresolved_rows}
    with pytest.raises(MigrationPlanNotApplicableError):
        plan.require_applicable()


def test_an_invalid_row_yields_no_plan_to_approve() -> None:
    """A corpus with unreadable rows produces no plan, so none can be applied."""
    with pytest.raises(MigrationRowValidationError) as excinfo:
        _plan(MALFORMED_SNAPSHOT)
    assert excinfo.value.code == "migration_row_validation"
    assert "fail their declared schema" in str(excinfo.value)


def test_a_colliding_corpus_yields_no_plan_to_approve() -> None:
    """Two source rows resolving to one record is refused before any seal."""
    with pytest.raises(IdentityError, match="alias_target_not_injective"):
        _plan(ALIAS_COLLISION_SNAPSHOT)


def test_idempotence_digest_is_stable_across_runs_and_roots(
    staged_corpus: Path, full_plan: MigrationPlan
) -> None:
    """A rerun over a copy at another path must reproduce the same placement."""
    copied = _plan(staged_corpus)
    assert copied.manifest.idempotence_digest == full_plan.manifest.idempotence_digest
    assert copied.manifest.manifest_digest == full_plan.manifest.manifest_digest
    assert copied.approval_digest == full_plan.approval_digest


def _strings_in(value: Any) -> Iterator[str]:
    """Yield every string the nested JSON value holds, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_in(item)


def test_plan_records_no_absolute_path(full_plan: MigrationPlan) -> None:
    """A manifest is portable: it pins locators, never where they were read.

    The check is generic rather than a denylist of machine prefixes: any
    string that opens with a POSIX separator or carries a Windows drive
    root is an absolute path, whatever machine produced it.
    """
    rendered = full_plan.manifest.model_dump_json()
    assert str(EPOCH1_FULL_SNAPSHOT) not in rendered
    absolute = [
        text for text in _strings_in(json.loads(rendered)) if text.startswith("/") or ":\\" in text
    ]
    assert absolute == []
