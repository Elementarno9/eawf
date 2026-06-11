"""Tests for the ``backlog_resolution`` close-gate kind (P30-I10-W02).

The wave's success criterion pinned by these tests: ``backlog_resolution``
fires on at least one wave-linked backlog item -- a wave linked to a
closed-with-resolution item passes, and a wave that "fixes B0xx" but leaves the
linked row dangling (open, no resolution, no blocked-reason) FAILS the gate.

Boundary + error paths covered:

* zero linked items -- the gate passes vacuously (nothing to leak);
* one closed-with-resolution item -- pass (the binding-proof);
* one open item with no recorded reason -- fail (the load-bearing negative);
* one closed item with NO resolution -- fail (the "no signal" trap);
* one deferred item -- pass (a deliberate stay-open);
* one open item carrying an explicit blocked-reason -- pass;
* a mix of one resolved + one dangling item -- fail naming only the dangling
  id;
* a linked item under a DIFFERENT wave is not counted (linkage is scope-id).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.state.enums import (
    BacklogPriority,
    BacklogStatus,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import BacklogItem, CurrentPointers, Project, State
from eawf.workflow.audit_dsl.kinds.backlog_resolution import (
    BACKLOG_RESOLUTION_KIND,
    BacklogResolutionResult,
    check_backlog_resolution,
    linked_backlog_items,
)

WAVE_ID = "P30-I10-W02"
OTHER_WAVE_ID = "P30-I10-W99"


def _item(
    item_id: str,
    *,
    status: BacklogStatus,
    resolution: str | None = None,
    scope_id: str = WAVE_ID,
) -> BacklogItem:
    """Build a synthetic :class:`BacklogItem` linked to *scope_id*."""
    return BacklogItem(
        id=item_id,
        scope_id=scope_id,
        title=f"item {item_id}",
        priority=BacklogPriority.P3,
        status=status,
        created_at=datetime.now(UTC),
        resolution=resolution,
    )


def _state(items: list[BacklogItem]) -> State:
    """Build a minimal valid State carrying *items* in the backlog map."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:VFY",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="VFY",
                slug="vfy",
                title="VFY",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:VFY",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="VFY").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
            "backlog": {item.id: item.model_dump(mode="json") for item in items},
        }
    )


def test_no_linked_items_passes_vacuously() -> None:
    # A wave that links no backlog items has nothing to leak: pass, no ids.
    result = check_backlog_resolution(_state([]), wave_id=WAVE_ID)
    assert isinstance(result, BacklogResolutionResult)
    assert result.passed is True
    assert result.linked_ids == []
    assert result.dangling_ids == []
    assert "links no backlog items" in result.details


def test_closed_with_resolution_passes() -> None:
    # The binding-proof: a wave linked to a closed-with-resolution item passes.
    state = _state([_item("B093", status=BacklogStatus.CLOSED, resolution="wired into CI")])
    result = check_backlog_resolution(state, wave_id=WAVE_ID)
    assert result.passed is True
    assert result.linked_ids == ["B093"]
    assert result.dangling_ids == []


def test_open_without_reason_fails() -> None:
    # The load-bearing negative: a wave that leaves a linked open item dangling
    # (no resolution, no blocked-reason) FAILS, naming the dangling id.
    state = _state([_item("B093", status=BacklogStatus.OPEN)])
    result = check_backlog_resolution(state, wave_id=WAVE_ID)
    assert result.passed is False
    assert result.dangling_ids == ["B093"]
    assert "B093" in result.details


def test_closed_without_resolution_fails() -> None:
    # A closed row with NO resolution is the "no signal" trap: the close
    # recorded the status flip but not what discharged it -- fail.
    state = _state([_item("B097", status=BacklogStatus.CLOSED, resolution=None)])
    result = check_backlog_resolution(state, wave_id=WAVE_ID)
    assert result.passed is False
    assert result.dangling_ids == ["B097"]


def test_closed_with_whitespace_resolution_fails() -> None:
    # A whitespace-only resolution carries no signal -- it fails like an empty one.
    state = _state([_item("B097", status=BacklogStatus.CLOSED, resolution="   ")])
    result = check_backlog_resolution(state, wave_id=WAVE_ID)
    assert result.passed is False
    assert result.dangling_ids == ["B097"]


def test_deferred_passes_without_resolution() -> None:
    # A deferral is a deliberate "stays open, on purpose" decision, accepted
    # even without a recorded resolution.
    state = _state([_item("B099", status=BacklogStatus.DEFERRED)])
    result = check_backlog_resolution(state, wave_id=WAVE_ID)
    assert result.passed is True
    assert result.dangling_ids == []


def test_open_with_blocked_reason_passes() -> None:
    # An open item carrying an explicit recorded reason is an accepted stay-open.
    state = _state([_item("B100", status=BacklogStatus.OPEN, resolution="blocked on upstream X")])
    result = check_backlog_resolution(state, wave_id=WAVE_ID)
    assert result.passed is True
    assert result.dangling_ids == []


def test_mixed_resolved_and_dangling_fails_only_on_dangling() -> None:
    # A wave linked to one resolved + one dangling item fails, naming ONLY the
    # dangling id (the resolved one is not flagged).
    state = _state(
        [
            _item("B093", status=BacklogStatus.CLOSED, resolution="done"),
            _item("B100", status=BacklogStatus.IN_PROGRESS),
        ]
    )
    result = check_backlog_resolution(state, wave_id=WAVE_ID)
    assert result.passed is False
    assert result.linked_ids == ["B093", "B100"]
    assert result.dangling_ids == ["B100"]


def test_item_under_other_wave_is_not_counted() -> None:
    # Linkage is by scope_id: an item triaged at a DIFFERENT wave is not linked
    # to WAVE_ID, so a dangling item there does not block this wave.
    state = _state([_item("B093", status=BacklogStatus.OPEN, scope_id=OTHER_WAVE_ID)])
    result = check_backlog_resolution(state, wave_id=WAVE_ID)
    assert result.passed is True
    assert result.linked_ids == []


def test_linked_backlog_items_returns_id_sorted() -> None:
    # The linked-items helper returns the wave's items in ascending id order.
    state = _state(
        [
            _item("B100", status=BacklogStatus.CLOSED, resolution="x"),
            _item("B093", status=BacklogStatus.CLOSED, resolution="y"),
        ]
    )
    linked = linked_backlog_items(state, wave_id=WAVE_ID)
    assert [item.id for item in linked] == ["B093", "B100"]


def test_none_backlog_map_passes_vacuously() -> None:
    # The state model's ``backlog`` field is optional (``None``); the gate
    # tolerates a state with no backlog map at all.
    state = _state([])
    state.backlog = None
    result = check_backlog_resolution(state, wave_id=WAVE_ID)
    assert result.passed is True
    assert result.linked_ids == []


def test_kind_constant_is_stable() -> None:
    # The registered kind string is the stable id the registry + the close-gate
    # view + the wired-on sweep all key on.
    assert BACKLOG_RESOLUTION_KIND == "backlog_resolution"


@pytest.mark.parametrize(
    ("status", "resolution", "expected_pass"),
    [
        (BacklogStatus.CLOSED, "done", True),
        (BacklogStatus.CLOSED, None, False),
        (BacklogStatus.DEFERRED, None, True),
        (BacklogStatus.OPEN, None, False),
        (BacklogStatus.OPEN, "reason", True),
        (BacklogStatus.IN_PROGRESS, None, False),
        (BacklogStatus.IN_PROGRESS, "reason", True),
    ],
)
def test_resolution_contract_matrix(
    status: BacklogStatus, resolution: str | None, expected_pass: bool
) -> None:
    # The full per-status resolved-or-deferred contract, one row per branch.
    state = _state([_item("B001", status=status, resolution=resolution)])
    result = check_backlog_resolution(state, wave_id=WAVE_ID)
    assert result.passed is expected_pass
