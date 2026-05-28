"""Tests for the library-level state-write primitives (:mod:`eawf.kernel.state.io`).

The happy path is exercised by the lifecycle integration tests; this
module pins the schema/invariant failure path so coverage on
``_validate_or_raise`` does not regress.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import ProjectStatus, ScopeKind
from eawf.kernel.state.io import StateValidationError, commit_mutation
from eawf.kernel.state.models import CurrentPointers, Project, State

pytestmark = pytest.mark.unit


def _state_with_dangling_phase_pointer() -> State:
    """Build a State whose ``current.phase_id`` references a missing phase.

    The State :meth:`~pydantic.BaseModel.model_validate` boundary accepts
    this payload (the cross-entity check lives in the strict invariant
    layer, not the schema layer); the invariant
    ``check_current_pointers`` then flags ``INV.CURRENT.PHASE_MISSING``.
    """
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            # P99 is not in ``phases`` — the invariant will reject.
            "current": CurrentPointers(
                project_code="QR",
                phase_id="P99",
            ).model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def test_commit_mutation_raises_state_validation_error_on_invariant_violation(
    tmp_path: Path,
) -> None:
    """Pins the error path through ``_validate_or_raise`` (io.py:364-366).

    A state whose ``current.phase_id`` references no row trips the
    ``INV.CURRENT.PHASE_MISSING`` invariant. ``_validate_or_raise``
    composes the violation list into a ``"; "``-joined message and
    raises :class:`StateValidationError` BEFORE any WAL or file IO
    happens, so ``state.json`` is never touched.
    """
    candidate = _state_with_dangling_phase_pointer()
    state_path = tmp_path / "state.json"
    with pytest.raises(StateValidationError, match=r"INV\.CURRENT\.PHASE_MISSING"):
        commit_mutation(
            state_path,
            candidate=candidate,
            before_version="0" * 16,
            command="test_command",
            args={},
            scope_id="QR",
            summary="invariant violation probe",
        )
    # The fallback writer is state-first; a validation failure must short
    # circuit BEFORE the state file is written.
    assert not state_path.exists()
