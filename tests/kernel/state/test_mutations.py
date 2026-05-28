"""Tests for the five ``MEMORY_*`` mutations + replay round-trip (P28-I02-W04).

Covers the apply functions added in :mod:`eawf.kernel.state.mutations`:

- :func:`apply_memory_add` — insert; rejects duplicate id.
- :func:`apply_memory_update` — patch in place; rejects unknown id.
- :func:`apply_memory_supersede` — flip + insert link; rejects bad
  parents or duplicate replacement id.
- :func:`apply_memory_prune` — soft-delete via status flip; rejects
  unknown or already-pruned ids.
- :func:`apply_memory_review` — bump ``review_due``; rejects unknown id.

The replay round-trip test serialises the state to JSON, re-validates,
re-applies the same sequence of mutations against a fresh deserialised
copy, and asserts the resulting state equals the in-place mutated
state. This pins the contract that the apply functions are pure state
mutators (no JSONL side-effects) and that the post-apply state still
satisfies the strict :class:`State` schema.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.state.enums import (
    Confidence,
    DecisionStatus,
    MemoryStatus,
    MemoryTier,
)
from eawf.kernel.state.models import Decision, MemorySummary, State
from eawf.kernel.state.mutations import (
    DecisionMutationError,
    MemoryAddPayload,
    MemoryMutationError,
    Mutation,
    MutationKind,
    apply_decision_obsolete,
    apply_memory_add,
    apply_memory_prune,
    apply_memory_review,
    apply_memory_supersede,
    apply_memory_update,
)

pytestmark = pytest.mark.unit


# ---- fixtures ---------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def _state_payload() -> dict[str, Any]:
    """Minimal valid State payload for the mutation tests."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _state() -> State:
    return State.model_validate(_state_payload())


def _seed_entry(
    state: State,
    *,
    mem_id: str = "MEM-20260527-01",
    scope_id: str = "ABC",
    confidence: Confidence = Confidence.MEDIUM,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    tier: MemoryTier = MemoryTier.WORKING,
    review_due: datetime | None = None,
) -> MemorySummary:
    """Seed *state* with one :class:`MemorySummary` and return it."""
    summary = MemorySummary(
        id=mem_id,
        scope_id=scope_id,
        summary=f"{mem_id} summary",
        confidence=confidence,
        status=status,
        store_record_id=mem_id,
        review_due=review_due,
        tier=tier,
    )
    if state.memory_index is None:
        state.memory_index = {}
    state.memory_index[mem_id] = summary
    return summary


def _mutation(kind: MutationKind, scope_id: str, **params: Any) -> Mutation:
    return Mutation(
        kind=kind,
        scope_id=scope_id,
        mutation_id=uuid.uuid4().hex,
        params=params,
    )


# ---- MEMORY_ADD -------------------------------------------------------------


def test_memory_add_inserts_new_entry() -> None:
    state = _state()
    # ``_mutation`` shadows the payload's ``scope_id`` field with the
    # envelope's, so MEMORY_ADD builds its params dict explicitly.
    mutation = Mutation(
        kind=MutationKind.MEMORY_ADD,
        scope_id="ABC",
        mutation_id=uuid.uuid4().hex,
        params={
            "id": "MEM-20260527-01",
            "scope_id": "ABC",
            "summary": "first entry",
            "confidence": Confidence.MEDIUM.value,
            "store_record_id": "MEM-20260527-01",
        },
    )
    apply_memory_add(state, mutation)
    assert state.memory_index is not None
    entry = state.memory_index["MEM-20260527-01"]
    assert entry.summary == "first entry"
    assert entry.status == MemoryStatus.ACTIVE
    assert entry.tier == MemoryTier.WORKING


def test_memory_add_initialises_index_when_missing() -> None:
    state = _state()
    state.memory_index = None
    mutation = Mutation(
        kind=MutationKind.MEMORY_ADD,
        scope_id="ABC",
        mutation_id=uuid.uuid4().hex,
        params={
            "id": "MEM-20260527-02",
            "scope_id": "ABC",
            "summary": "lazy init",
            "confidence": Confidence.LOW.value,
            "store_record_id": "MEM-20260527-02",
        },
    )
    apply_memory_add(state, mutation)
    assert state.memory_index is not None
    assert "MEM-20260527-02" in state.memory_index


def test_memory_add_rejects_duplicate_id() -> None:
    state = _state()
    _seed_entry(state)
    mutation = Mutation(
        kind=MutationKind.MEMORY_ADD,
        scope_id="ABC",
        mutation_id=uuid.uuid4().hex,
        params={
            "id": "MEM-20260527-01",
            "scope_id": "ABC",
            "summary": "dup",
            "confidence": Confidence.MEDIUM.value,
            "store_record_id": "MEM-20260527-01",
        },
    )
    with pytest.raises(MemoryMutationError, match="already exists"):
        apply_memory_add(state, mutation)


# ---- MEMORY_UPDATE ----------------------------------------------------------


def test_memory_update_patches_supplied_fields() -> None:
    state = _state()
    _seed_entry(state, confidence=Confidence.MEDIUM, tier=MemoryTier.WORKING)
    mutation = _mutation(
        MutationKind.MEMORY_UPDATE,
        scope_id="MEM-20260527-01",
        id="MEM-20260527-01",
        confidence=Confidence.HIGH.value,
        tier=MemoryTier.ARCHIVAL.value,
    )
    apply_memory_update(state, mutation)
    assert state.memory_index is not None
    entry = state.memory_index["MEM-20260527-01"]
    assert entry.confidence == Confidence.HIGH
    assert entry.tier == MemoryTier.ARCHIVAL
    # Unsupplied field unchanged.
    assert entry.status == MemoryStatus.ACTIVE


def test_memory_update_review_due_clear_sets_none() -> None:
    state = _state()
    _seed_entry(state, review_due=_now())
    mutation = _mutation(
        MutationKind.MEMORY_UPDATE,
        scope_id="MEM-20260527-01",
        id="MEM-20260527-01",
        review_due_clear=True,
    )
    apply_memory_update(state, mutation)
    assert state.memory_index is not None
    assert state.memory_index["MEM-20260527-01"].review_due is None


def test_memory_update_rejects_unknown_id() -> None:
    state = _state()
    mutation = _mutation(
        MutationKind.MEMORY_UPDATE,
        scope_id="MEM-missing",
        id="MEM-missing",
        confidence=Confidence.HIGH.value,
    )
    with pytest.raises(MemoryMutationError, match="unknown memory entry"):
        apply_memory_update(state, mutation)


# ---- MEMORY_SUPERSEDE -------------------------------------------------------


def test_memory_supersede_flips_old_and_inserts_new() -> None:
    state = _state()
    _seed_entry(state, mem_id="MEM-old-01")
    payload = MemoryAddPayload(
        id="MEM-new-01",
        scope_id="ABC",
        summary="replacement",
        confidence=Confidence.HIGH,
        store_record_id="MEM-new-01",
    )
    mutation = Mutation(
        kind=MutationKind.MEMORY_SUPERSEDE,
        scope_id="MEM-old-01",
        mutation_id=uuid.uuid4().hex,
        params={
            "old_id": "MEM-old-01",
            "new_entry": payload.model_dump(mode="json"),
        },
    )
    apply_memory_supersede(state, mutation)
    assert state.memory_index is not None
    old = state.memory_index["MEM-old-01"]
    new = state.memory_index["MEM-new-01"]
    assert old.status == MemoryStatus.SUPERSEDED
    assert new.status == MemoryStatus.ACTIVE
    # Default supersede link points back at the old id.
    assert new.promoted_to_artifact_id == "MEM-old-01"


def test_memory_supersede_rejects_missing_old_id() -> None:
    state = _state()
    payload = MemoryAddPayload(
        id="MEM-new-01",
        scope_id="ABC",
        summary="replacement",
        confidence=Confidence.HIGH,
        store_record_id="MEM-new-01",
    )
    mutation = Mutation(
        kind=MutationKind.MEMORY_SUPERSEDE,
        scope_id="MEM-old-missing",
        mutation_id=uuid.uuid4().hex,
        params={
            "old_id": "MEM-old-missing",
            "new_entry": payload.model_dump(mode="json"),
        },
    )
    with pytest.raises(MemoryMutationError, match="unknown memory entry"):
        apply_memory_supersede(state, mutation)


def test_memory_supersede_rejects_duplicate_new_id() -> None:
    state = _state()
    _seed_entry(state, mem_id="MEM-old-01")
    _seed_entry(state, mem_id="MEM-new-01")
    payload = MemoryAddPayload(
        id="MEM-new-01",
        scope_id="ABC",
        summary="replacement",
        confidence=Confidence.HIGH,
        store_record_id="MEM-new-01",
    )
    mutation = Mutation(
        kind=MutationKind.MEMORY_SUPERSEDE,
        scope_id="MEM-old-01",
        mutation_id=uuid.uuid4().hex,
        params={
            "old_id": "MEM-old-01",
            "new_entry": payload.model_dump(mode="json"),
        },
    )
    with pytest.raises(MemoryMutationError, match="replacement memory entry already exists"):
        apply_memory_supersede(state, mutation)


# ---- MEMORY_PRUNE -----------------------------------------------------------


def test_memory_prune_flips_status() -> None:
    state = _state()
    _seed_entry(state, status=MemoryStatus.STALE)
    mutation = _mutation(
        MutationKind.MEMORY_PRUNE,
        scope_id="MEM-20260527-01",
        id="MEM-20260527-01",
    )
    apply_memory_prune(state, mutation)
    assert state.memory_index is not None
    assert state.memory_index["MEM-20260527-01"].status == MemoryStatus.PRUNED


def test_memory_prune_rejects_unknown_id() -> None:
    state = _state()
    mutation = _mutation(
        MutationKind.MEMORY_PRUNE,
        scope_id="MEM-missing",
        id="MEM-missing",
    )
    with pytest.raises(MemoryMutationError, match="unknown memory entry"):
        apply_memory_prune(state, mutation)


def test_memory_prune_rejects_already_pruned() -> None:
    state = _state()
    _seed_entry(state, status=MemoryStatus.PRUNED)
    mutation = _mutation(
        MutationKind.MEMORY_PRUNE,
        scope_id="MEM-20260527-01",
        id="MEM-20260527-01",
    )
    with pytest.raises(MemoryMutationError, match="already pruned"):
        apply_memory_prune(state, mutation)


# ---- MEMORY_REVIEW ----------------------------------------------------------


def test_memory_review_bumps_review_due() -> None:
    state = _state()
    _seed_entry(state)
    future = datetime(2026, 8, 1, tzinfo=UTC)
    mutation = _mutation(
        MutationKind.MEMORY_REVIEW,
        scope_id="MEM-20260527-01",
        id="MEM-20260527-01",
        review_due=future.isoformat(),
    )
    apply_memory_review(state, mutation)
    assert state.memory_index is not None
    assert state.memory_index["MEM-20260527-01"].review_due == future


def test_memory_review_rejects_unknown_id() -> None:
    state = _state()
    mutation = _mutation(
        MutationKind.MEMORY_REVIEW,
        scope_id="MEM-missing",
        id="MEM-missing",
        review_due=_now().isoformat(),
    )
    with pytest.raises(MemoryMutationError, match="unknown memory entry"):
        apply_memory_review(state, mutation)


# ---- MutationKind enum closure ---------------------------------------------


def test_memory_mutation_kinds_enumerated() -> None:
    """Five MEMORY_* kinds present; MEMORY_RECALL is absent (read-only)."""
    members = {m.value for m in MutationKind}
    assert "memory_add" in members
    assert "memory_update" in members
    assert "memory_supersede" in members
    assert "memory_prune" in members
    assert "memory_review" in members
    # Recall is intentionally not a mutation.
    assert "memory_recall" not in members


# ---- replay round-trip ------------------------------------------------------


def _apply(state: State, mutation: Mutation) -> None:
    """Dispatch *mutation* to the matching ``apply_memory_*`` helper."""
    if mutation.kind == MutationKind.MEMORY_ADD:
        apply_memory_add(state, mutation)
    elif mutation.kind == MutationKind.MEMORY_UPDATE:
        apply_memory_update(state, mutation)
    elif mutation.kind == MutationKind.MEMORY_SUPERSEDE:
        apply_memory_supersede(state, mutation)
    elif mutation.kind == MutationKind.MEMORY_PRUNE:
        apply_memory_prune(state, mutation)
    elif mutation.kind == MutationKind.MEMORY_REVIEW:
        apply_memory_review(state, mutation)
    else:  # pragma: no cover — defensive guard for the test dispatch
        raise AssertionError(f"unsupported mutation kind in test dispatch: {mutation.kind!r}")


def _build_journal() -> list[Mutation]:
    """Build a journal exercising all five MEMORY_* kinds in order."""
    return [
        Mutation(
            kind=MutationKind.MEMORY_ADD,
            scope_id="ABC",
            mutation_id="m1",
            params={
                "id": "MEM-A",
                "scope_id": "ABC",
                "summary": "alpha",
                "confidence": Confidence.MEDIUM.value,
                "store_record_id": "MEM-A",
            },
        ),
        Mutation(
            kind=MutationKind.MEMORY_ADD,
            scope_id="ABC",
            mutation_id="m2",
            params={
                "id": "MEM-B",
                "scope_id": "ABC",
                "summary": "beta",
                "confidence": Confidence.LOW.value,
                "store_record_id": "MEM-B",
            },
        ),
        Mutation(
            kind=MutationKind.MEMORY_UPDATE,
            scope_id="MEM-A",
            mutation_id="m3",
            params={
                "id": "MEM-A",
                "tier": MemoryTier.ARCHIVAL.value,
                "confidence": Confidence.HIGH.value,
            },
        ),
        Mutation(
            kind=MutationKind.MEMORY_REVIEW,
            scope_id="MEM-B",
            mutation_id="m4",
            params={
                "id": "MEM-B",
                "review_due": datetime(2026, 9, 1, tzinfo=UTC).isoformat(),
            },
        ),
        Mutation(
            kind=MutationKind.MEMORY_SUPERSEDE,
            scope_id="MEM-A",
            mutation_id="m5",
            params={
                "old_id": "MEM-A",
                "new_entry": {
                    "id": "MEM-A2",
                    "scope_id": "ABC",
                    "summary": "alpha2",
                    "confidence": Confidence.HIGH.value,
                    "store_record_id": "MEM-A2",
                },
            },
        ),
        Mutation(
            kind=MutationKind.MEMORY_PRUNE,
            scope_id="MEM-B",
            mutation_id="m6",
            params={"id": "MEM-B"},
        ),
    ]


def test_replay_round_trips_five_memory_mutations() -> None:
    """Apply → serialise → re-validate → re-apply → compare.

    Pins the contract that the apply functions are pure state mutators:
    serialising the post-apply state, re-validating it under the strict
    :class:`State` schema, and replaying the same journal against a
    fresh deserialised copy must produce a state that compares equal to
    the in-place mutated state.
    """
    journal = _build_journal()

    # First pass: apply the journal in place.
    state_in_place = _state()
    for mutation in journal:
        _apply(state_in_place, mutation)
    in_place_payload = state_in_place.model_dump(mode="json")

    # Replay-from-disk pass: re-validate, replay, compare.
    replayed = State.model_validate(_state_payload())
    for mutation in journal:
        _apply(replayed, mutation)
    replayed_payload = replayed.model_dump(mode="json")

    assert in_place_payload["memory_index"] == replayed_payload["memory_index"]

    # Re-validate the post-apply payload to confirm the apply functions
    # left :class:`State` in a still-schema-valid shape.
    revalidated = State.model_validate(in_place_payload)
    assert revalidated.memory_index is not None
    # MEM-A flipped to SUPERSEDED, MEM-A2 inserted, MEM-B pruned.
    assert revalidated.memory_index["MEM-A"].status == MemoryStatus.SUPERSEDED
    assert revalidated.memory_index["MEM-A2"].status == MemoryStatus.ACTIVE
    assert revalidated.memory_index["MEM-A2"].promoted_to_artifact_id == "MEM-A"
    assert revalidated.memory_index["MEM-B"].status == MemoryStatus.PRUNED
    assert revalidated.memory_index["MEM-A"].tier == MemoryTier.ARCHIVAL


# ---- MEMORY_UPDATE branch coverage -----------------------------------------


def test_memory_update_summary_field_patched() -> None:
    # ``summary`` is the first conditional branch in ``apply_memory_update``;
    # the existing patches-fields test covers ``confidence`` + ``tier`` only.
    state = _state()
    _seed_entry(state)
    mutation = _mutation(
        MutationKind.MEMORY_UPDATE,
        scope_id="MEM-20260527-01",
        id="MEM-20260527-01",
        summary="updated summary text",
    )
    apply_memory_update(state, mutation)
    assert state.memory_index is not None
    assert state.memory_index["MEM-20260527-01"].summary == "updated summary text"


def test_memory_update_status_field_patched() -> None:
    # Pins the ``payload.status is not None`` branch (line 333-334).
    state = _state()
    _seed_entry(state, status=MemoryStatus.ACTIVE)
    mutation = _mutation(
        MutationKind.MEMORY_UPDATE,
        scope_id="MEM-20260527-01",
        id="MEM-20260527-01",
        status=MemoryStatus.STALE.value,
    )
    apply_memory_update(state, mutation)
    assert state.memory_index is not None
    assert state.memory_index["MEM-20260527-01"].status == MemoryStatus.STALE


def test_memory_update_review_due_set_without_clear() -> None:
    # Pins the ``elif payload.review_due is not None`` branch (line 337-338);
    # the existing test only covers the ``review_due_clear=True`` arm.
    state = _state()
    _seed_entry(state, review_due=None)
    future = datetime(2026, 9, 1, tzinfo=UTC)
    mutation = _mutation(
        MutationKind.MEMORY_UPDATE,
        scope_id="MEM-20260527-01",
        id="MEM-20260527-01",
        review_due=future.isoformat(),
    )
    apply_memory_update(state, mutation)
    assert state.memory_index is not None
    assert state.memory_index["MEM-20260527-01"].review_due == future


def test_memory_update_promoted_to_artifact_id_patched() -> None:
    # Pins the ``payload.promoted_to_artifact_id is not None`` branch
    # (line 341-342) — the last optional field in ``apply_memory_update``.
    state = _state()
    _seed_entry(state)
    mutation = _mutation(
        MutationKind.MEMORY_UPDATE,
        scope_id="MEM-20260527-01",
        id="MEM-20260527-01",
        promoted_to_artifact_id="ART-20260528-promoted",
    )
    apply_memory_update(state, mutation)
    assert state.memory_index is not None
    entry = state.memory_index["MEM-20260527-01"]
    assert entry.promoted_to_artifact_id == "ART-20260528-promoted"


def test_memory_update_with_no_supplied_fields_is_noop() -> None:
    """Pins the ``if updates:`` False branch (line 343->345) — no patch."""
    state = _state()
    original = _seed_entry(state, confidence=Confidence.MEDIUM)
    # Only ``id`` supplied; every optional field defaults to ``None``, so
    # ``updates`` is empty and the index entry is not rewritten.
    mutation = _mutation(
        MutationKind.MEMORY_UPDATE,
        scope_id="MEM-20260527-01",
        id="MEM-20260527-01",
    )
    apply_memory_update(state, mutation)
    assert state.memory_index is not None
    entry = state.memory_index["MEM-20260527-01"]
    # Same object identity preserved — model_copy was not invoked.
    assert entry is original
    assert entry.confidence == Confidence.MEDIUM


# ---- DECISION_OBSOLETE ------------------------------------------------------


def _seed_decision(
    state: State,
    *,
    decision_id: str = "D01",
    status: DecisionStatus = DecisionStatus.ACTIVE,
) -> Decision:
    """Seed *state* with one :class:`Decision` and return it."""
    decision = Decision(
        id=decision_id,
        scope_id="ABC",
        title=f"{decision_id} title",
        rationale="why",
        status=status,
        created_at=_now(),
    )
    if state.decisions is None:
        state.decisions = {}
    state.decisions[decision_id] = decision
    return decision


def test_decision_obsolete_flips_active_to_obsolete() -> None:
    state = _state()
    _seed_decision(state)
    obsoleted_at = datetime(2026, 6, 1, tzinfo=UTC)
    mutation = _mutation(
        MutationKind.DECISION_OBSOLETE,
        scope_id="D01",
        id="D01",
        obsoleted_at=obsoleted_at.isoformat(),
    )
    apply_decision_obsolete(state, mutation)
    assert state.decisions is not None
    row = state.decisions["D01"]
    assert row.status == DecisionStatus.OBSOLETE
    assert row.obsoleted_at == obsoleted_at


def test_decision_obsolete_rejects_unknown_id() -> None:
    """Pins line 499 — ``existing is None`` raises ``DecisionMutationError``."""
    state = _state()
    mutation = _mutation(
        MutationKind.DECISION_OBSOLETE,
        scope_id="D-missing",
        id="D-missing",
        obsoleted_at=_now().isoformat(),
    )
    with pytest.raises(DecisionMutationError, match="unknown decision"):
        apply_decision_obsolete(state, mutation)


def test_decision_obsolete_rejects_already_obsolete() -> None:
    """Pins line 501 — already-OBSOLETE row rejects with a distinct error."""
    state = _state()
    _seed_decision(state, status=DecisionStatus.OBSOLETE)
    mutation = _mutation(
        MutationKind.DECISION_OBSOLETE,
        scope_id="D01",
        id="D01",
        obsoleted_at=_now().isoformat(),
    )
    with pytest.raises(DecisionMutationError, match="already obsolete"):
        apply_decision_obsolete(state, mutation)


def test_decision_obsolete_rejects_non_active_status() -> None:
    """Pins the ``status != ACTIVE`` branch (the SUPERSEDED / REVERSED arm)."""
    state = _state()
    _seed_decision(state, status=DecisionStatus.SUPERSEDED)
    mutation = _mutation(
        MutationKind.DECISION_OBSOLETE,
        scope_id="D01",
        id="D01",
        obsoleted_at=_now().isoformat(),
    )
    with pytest.raises(DecisionMutationError, match="cannot obsolete decision"):
        apply_decision_obsolete(state, mutation)
