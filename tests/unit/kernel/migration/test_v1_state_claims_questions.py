"""The v1 ``State`` holds claims and open questions, and its writer round-trips them."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.migration.v1_state import State
from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus, StoreKind, Urgency
from eawf.kernel.state.io import commit_mutation, fallback_wal_dir
from eawf.kernel.store.paths import store_path
from eawf.kernel.validate.strict import validate_path
from eawf.runtime.daemon.recovery import replay_wal


def _now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _claim_payload(claim_id: str = "CLM-001", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": claim_id,
        "scope_id": "ABC",
        "title": "Vale wraps cleanly inside eawf hook",
        "description": None,
        "status": "open",
        "evidence_refs": [],
        "source_artifact_id": None,
        "answers_question_id": None,
        "created_at": _now().isoformat(),
        "superseded_by": None,
    }
    payload.update(overrides)
    return payload


def _open_question_payload(qid: str = "OQ-001", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": qid,
        "scope_id": "ABC",
        "title": "Which prose linter covers every artifact type",
        "description": None,
        "status": "open",
        "blocking": False,
        "answered_by_claim_id": None,
        "created_at": _now().isoformat(),
        "resolved_at": None,
    }
    payload.update(overrides)
    return payload


def _state_payload(
    *,
    schema_version: str = "1.2",
    with_research_entities: bool = True,
) -> dict[str, object]:
    """Return a minimal valid State payload, optionally carrying the entities.

    Mirrors the inline payload used by the model round-trip tests above but is
    reused by the persistence + reconcile round-trip so the canonical-writer
    seam is exercised over a payload that passes strict invariant validation
    (``current`` points only at ``project_code`` so no dangling-pointer
    invariant trips).
    """
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": "",
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
    if with_research_entities:
        payload["claims"] = {"CLM-001": _claim_payload()}
        payload["open_questions"] = {"OQ-001": _open_question_payload()}
    return payload


def test_state_registers_claims_and_open_questions_separately() -> None:
    """Both hang on State as their own optional collections (additive, no bump)."""
    claims_ann = State.model_fields["claims"].annotation
    questions_ann = State.model_fields["open_questions"].annotation
    assert "Claim" in str(claims_ann)
    assert "OpenQuestion" in str(questions_ann)
    # The two collections are distinct keys, not one shared dict.
    assert "claims" in State.model_fields
    assert "open_questions" in State.model_fields


def test_state_round_trips_with_claims_and_open_questions() -> None:
    """A State carrying both collections survives a model round-trip."""
    state = State.model_validate(
        {
            "schema_version": "1.2",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:ABC",
            "updated_at": _now().isoformat(),
            "project": {
                "code": "ABC",
                "slug": "abc",
                "title": "ABC",
                "description": "",
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
            "claims": {"CLM-001": _claim_payload()},
            "open_questions": {"OQ-001": _open_question_payload()},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )
    assert state.claims is not None and "CLM-001" in state.claims
    assert state.open_questions is not None and "OQ-001" in state.open_questions
    restored = State.model_validate(state.model_dump(mode="json"))
    assert restored.claims == state.claims
    assert restored.open_questions == state.open_questions


def test_state_without_claims_and_open_questions_stays_valid() -> None:
    """Omitting both collections is valid — additive default-None, no schema bump."""
    state = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:ABC",
            "updated_at": _now().isoformat(),
            "project": {
                "code": "ABC",
                "slug": "abc",
                "title": "ABC",
                "description": "",
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
    )
    assert state.claims is None
    assert state.open_questions is None


def test_commit_mutation_persists_claims_and_open_questions(tmp_path: Path) -> None:
    """Claim + OpenQuestion survive a persist-then-reload through the writer seam.

    Round-trip: a typed State carrying both collections is persisted by
    ``commit_mutation`` (the canonical-writer-mirror), then re-read from disk by
    the same strict validator the daemon uses. Both entities must survive byte
    for byte, proving they reconcile through the single-writer seam.
    """
    candidate = State.model_validate(_state_payload())
    state_path = tmp_path / "state.json"

    commit_mutation(
        state_path,
        candidate=candidate,
        before_version="0" * 16,
        command="claim_open_question_roundtrip",
        args={},
        scope_id="ABC",
        summary="persist claim + open_question probe",
    )

    # The seam wrote state.json; re-read it through the daemon's strict validator.
    assert state_path.exists()
    report = validate_path(state_path)
    assert report.ok, report.schema_errors + [v.message for v in report.violations]
    reloaded = report.state
    assert reloaded is not None

    assert reloaded.claims is not None and "CLM-001" in reloaded.claims
    assert reloaded.open_questions is not None and "OQ-001" in reloaded.open_questions
    # Byte-for-byte identity with the candidate the writer was handed.
    assert reloaded.claims == candidate.claims
    assert reloaded.open_questions == candidate.open_questions
    assert reloaded.claims["CLM-001"].status is ClaimStatus.OPEN
    assert reloaded.open_questions["OQ-001"].status is OpenQuestionStatus.OPEN

    # The seam appended the paired event row (state-first, then event).
    events_path = store_path(state_path, StoreKind.EVENT)
    assert events_path.exists()
    assert events_path.read_text().strip(), "expected one event row appended"


def test_replay_wal_reconcile_preserves_claims_and_open_questions(tmp_path: Path) -> None:
    """The reconcile pass over a clean WAL leaves both entities on disk intact.

    After a successful ``commit_mutation`` the WAL record is retired; running
    ``replay_wal`` (the daemon-startup reconcile) over that clean WAL is a
    state no-op by the outcome-WAL invariant. This pins that reconcile does not
    drop or mangle the additive collections — state.json still carries them and
    re-validates after the reconcile walk.
    """
    candidate = State.model_validate(_state_payload())
    state_path = tmp_path / "state.json"
    commit_mutation(
        state_path,
        candidate=candidate,
        before_version="0" * 16,
        command="claim_open_question_reconcile",
        args={},
        scope_id="ABC",
        summary="reconcile claim + open_question probe",
    )

    wal_dir = fallback_wal_dir(state_path)
    events_path = store_path(state_path, StoreKind.EVENT)
    before = state_path.read_bytes()

    # Reconcile pass: idempotent on a clean WAL, never mutates state.
    replay_wal(wal_dir, state_path=state_path, event_path=events_path)

    assert state_path.read_bytes() == before, "reconcile must not rewrite state.json"
    report = validate_path(state_path)
    assert report.ok, report.schema_errors + [v.message for v in report.violations]
    assert report.state is not None
    assert report.state.claims is not None and "CLM-001" in report.state.claims
    assert report.state.open_questions is not None and "OQ-001" in report.state.open_questions


def test_urgent_pause_question_round_trips_through_writer_seam(tmp_path: Path) -> None:
    """An urgent, blocking question (the attention-feed/pause shape) persists intact.

    Exercises the canonical-writer seam over a question carrying a non-default
    urgency so the round-trip the wave criterion calls for ("urgency on a pause
    round-trips") is pinned end to end: typed State -> commit_mutation -> strict
    re-read, with the urgent value surviving byte for byte.
    """
    candidate = State.model_validate(
        _state_payload(
            with_research_entities=False,
        )
        | {
            "open_questions": {
                "OQ-001": _open_question_payload(
                    status="blocked",
                    blocking=True,
                    urgency="urgent",
                )
            }
        }
    )
    state_path = tmp_path / "state.json"
    commit_mutation(
        state_path,
        candidate=candidate,
        before_version="0" * 16,
        command="urgent_pause_roundtrip",
        args={},
        scope_id="ABC",
        summary="persist urgent blocking question probe",
    )

    report = validate_path(state_path)
    assert report.ok, report.schema_errors + [v.message for v in report.violations]
    assert report.state is not None
    assert report.state.open_questions is not None
    reloaded = report.state.open_questions["OQ-001"]
    assert reloaded.urgency is Urgency.URGENT
    assert reloaded.blocking is True
    assert reloaded == candidate.open_questions["OQ-001"]
