"""Lifecycle tests for ``set_iter_candidate_tag``.

``set_iter_candidate_tag`` stamps an iter's proposed release tag in place,
mirroring how :func:`edit_phase_plan` sets a phase's ``release`` band: the
tag routes through the model assignment validator so a malformed
``vMAJOR.MINOR.PATCH`` label raises :class:`pydantic.ValidationError`. The
helper is status-agnostic (PLANNED / ACTIVE / CLOSED iters can all be
tagged) and mutates the supplied :class:`State` in place.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    IterStatus,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.iter_ import open_iter, set_iter_candidate_tag
from eawf.workflow.lifecycle.phase import open_phase


def _empty_state() -> State:
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
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
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


def _seed_iter_state() -> State:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="orig title")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    return state


def test_set_iter_candidate_tag_unknown_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown iter"):
        set_iter_candidate_tag(state, iter_id="P01-I99", tag="v0.5.0")


def test_set_iter_candidate_tag_happy_sets_tag() -> None:
    state = _seed_iter_state()
    assert state.iters["P01-I01"].candidate_tag is None
    it = set_iter_candidate_tag(state, iter_id="P01-I01", tag="v0.5.0")
    assert it.candidate_tag == "v0.5.0"
    assert state.iters["P01-I01"].candidate_tag == "v0.5.0"


def test_set_iter_candidate_tag_accepts_prerelease() -> None:
    state = _seed_iter_state()
    it = set_iter_candidate_tag(state, iter_id="P01-I01", tag="v0.5.0rc1")
    assert it.candidate_tag == "v0.5.0rc1"


def test_set_iter_candidate_tag_overwrites_existing() -> None:
    state = _seed_iter_state()
    set_iter_candidate_tag(state, iter_id="P01-I01", tag="v0.5.0")
    set_iter_candidate_tag(state, iter_id="P01-I01", tag="v0.6.0")
    assert state.iters["P01-I01"].candidate_tag == "v0.6.0"


def test_set_iter_candidate_tag_invalid_pattern_raises() -> None:
    state = _seed_iter_state()
    with pytest.raises(ValidationError):
        set_iter_candidate_tag(state, iter_id="P01-I01", tag="0.5.0")
    # The rejected assignment must not mutate the iter.
    assert state.iters["P01-I01"].candidate_tag is None


def test_set_iter_candidate_tag_two_segment_version_raises() -> None:
    state = _seed_iter_state()
    with pytest.raises(ValidationError):
        set_iter_candidate_tag(state, iter_id="P01-I01", tag="v1.2")


def test_set_iter_candidate_tag_garbage_raises() -> None:
    state = _seed_iter_state()
    with pytest.raises(ValidationError):
        set_iter_candidate_tag(state, iter_id="P01-I01", tag="foo")


def test_set_iter_candidate_tag_status_agnostic_on_closed_iter() -> None:
    """A CLOSED iter can still be tagged -- the set is cosmetic metadata."""
    state = _seed_iter_state()
    state.iters["P01-I01"].status = IterStatus.CLOSED
    it = set_iter_candidate_tag(state, iter_id="P01-I01", tag="v0.5.0")
    assert it.candidate_tag == "v0.5.0"
