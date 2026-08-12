"""Unit tests for the Claim + OpenQuestion research-campaign entities.

The Claim ledger is the prerequisite of the pruning pass + SaturationReport,
and per Decision D-1 :class:`OpenQuestion` is its OWN first-class entity — not
a Claim variant. These tests pin:

* both models round-trip losslessly through ``model_dump`` / ``model_validate``;
* both forbid unknown keys (``ConfigDict(extra="forbid")``, AGENTS rule 2);
* the bounded ``title`` (≤72, no trailing period by convention) rejects an
  over-cap value at ingestion;
* the closed status enums reject an off-ladder token;
* Claim and OpenQuestion are distinct types with distinct field sets (the
  D-1 separation), each hung on :class:`State` as its own optional collection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state import models
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


# ---- Claim ------------------------------------------------------------------


def test_claim_round_trips() -> None:
    claim = models.Claim.model_validate(
        _claim_payload(
            description="a longer statement of the claim",
            status="supported",
            evidence_refs=["docs/x.md", "urn:eawf:v1:artifact:NB1"],
            source_artifact_id="NB1",
            answers_question_id="OQ-001",
        )
    )
    dumped = claim.model_dump(mode="json")
    restored = models.Claim.model_validate(dumped)
    assert restored == claim
    assert restored.status is ClaimStatus.SUPPORTED
    assert restored.evidence_refs == ["docs/x.md", "urn:eawf:v1:artifact:NB1"]
    assert restored.answers_question_id == "OQ-001"


def test_claim_minimal_defaults() -> None:
    claim = models.Claim.model_validate(
        {
            "id": "CLM-001",
            "scope_id": "ABC",
            "title": "minimal claim",
            "status": "open",
            "created_at": _now().isoformat(),
        }
    )
    assert claim.description is None
    assert claim.evidence_refs == []
    assert claim.source_artifact_id is None
    assert claim.answers_question_id is None
    assert claim.superseded_by is None


def test_claim_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match=r"(unexpected|extra)"):
        models.Claim.model_validate(_claim_payload(unexpected="oops"))


def test_claim_rejects_over_cap_title() -> None:
    with pytest.raises(ValidationError):
        models.Claim.model_validate(_claim_payload(title="x" * 73))


def test_claim_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        models.Claim.model_validate(_claim_payload(title=""))


def test_claim_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        models.Claim.model_validate(_claim_payload(status="bogus"))


def test_claim_rejects_over_cap_description() -> None:
    with pytest.raises(ValidationError):
        models.Claim.model_validate(_claim_payload(description="d" * 501))


# ---- OpenQuestion -----------------------------------------------------------


def test_open_question_round_trips() -> None:
    question = models.OpenQuestion.model_validate(
        _open_question_payload(
            description="framing of the question",
            status="answered",
            blocking=True,
            answered_by_claim_id="CLM-001",
            resolved_at=_now().isoformat(),
        )
    )
    dumped = question.model_dump(mode="json")
    restored = models.OpenQuestion.model_validate(dumped)
    assert restored == question
    assert restored.status is OpenQuestionStatus.ANSWERED
    assert restored.blocking is True
    assert restored.answered_by_claim_id == "CLM-001"


def test_open_question_minimal_defaults() -> None:
    question = models.OpenQuestion.model_validate(
        {
            "id": "OQ-001",
            "scope_id": "ABC",
            "title": "minimal question",
            "status": "open",
            "created_at": _now().isoformat(),
        }
    )
    assert question.description is None
    assert question.blocking is False
    assert question.answered_by_claim_id is None
    assert question.resolved_at is None


def test_open_question_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match=r"(unexpected|extra)"):
        models.OpenQuestion.model_validate(_open_question_payload(unexpected="oops"))


def test_open_question_rejects_over_cap_title() -> None:
    with pytest.raises(ValidationError):
        models.OpenQuestion.model_validate(_open_question_payload(title="q" * 73))


def test_open_question_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        models.OpenQuestion.model_validate(_open_question_payload(status="bogus"))


# ---- D-1: Claim and OpenQuestion are SEPARATE entities ----------------------


def test_claim_and_open_question_are_distinct_types() -> None:
    """D-1: OpenQuestion is its own entity, not a Claim alias or subclass."""
    assert models.Claim is not models.OpenQuestion
    assert not issubclass(models.OpenQuestion, models.Claim)
    assert not issubclass(models.Claim, models.OpenQuestion)
    # The field sets differ: OpenQuestion carries blocking / answered_by_claim_id /
    # resolved_at; Claim carries evidence_refs / answers_question_id / superseded_by.
    claim_fields = set(models.Claim.model_fields)
    question_fields = set(models.OpenQuestion.model_fields)
    assert {"evidence_refs", "answers_question_id", "superseded_by"} <= claim_fields
    assert {"blocking", "answered_by_claim_id", "resolved_at"} <= question_fields
    assert "evidence_refs" not in question_fields
    assert "blocking" not in claim_fields


def test_state_registers_claims_and_open_questions_separately() -> None:
    """Both hang on State as their own optional collections (additive, no bump)."""
    claims_ann = models.State.model_fields["claims"].annotation
    questions_ann = models.State.model_fields["open_questions"].annotation
    assert "Claim" in str(claims_ann)
    assert "OpenQuestion" in str(questions_ann)
    # The two collections are distinct keys, not one shared dict.
    assert "claims" in models.State.model_fields
    assert "open_questions" in models.State.model_fields


def test_state_round_trips_with_claims_and_open_questions() -> None:
    """A State carrying both collections survives a model round-trip."""
    state = models.State.model_validate(
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
    restored = models.State.model_validate(state.model_dump(mode="json"))
    assert restored.claims == state.claims
    assert restored.open_questions == state.open_questions


def test_state_without_claims_and_open_questions_stays_valid() -> None:
    """Omitting both collections is valid — additive default-None, no schema bump."""
    state = models.State.model_validate(
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


# ---- Criterion 2: persistence + reconcile through the single-writer seam -----
#
# ``commit_mutation`` is the in-process canonical-writer path (the V1 carve-out
# the daemon mutator mirrors verbatim: both dump the typed State, run the SAME
# generic ``validate_state``, then ``write_state_unlocked`` the payload). A
# success round-trip through it proves the new entities reconcile *generically*
# — no per-entity daemon wiring is needed, which is what the wave criterion
# allows ("If state-resident entities reconcile generically, a round-trip test
# suffices").


def test_commit_mutation_persists_claims_and_open_questions(tmp_path: Path) -> None:
    """Claim + OpenQuestion survive a persist-then-reload through the writer seam.

    Round-trip: a typed State carrying both collections is persisted by
    ``commit_mutation`` (the canonical-writer-mirror), then re-read from disk by
    the same strict validator the daemon uses. Both entities must survive byte
    for byte, proving they reconcile through the single-writer seam.
    """
    candidate = models.State.model_validate(_state_payload())
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
    candidate = models.State.model_validate(_state_payload())
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


# ---- P29-I01-W19: shared Urgency ladder -------------------------------------
#
# One closed ``Urgency`` StrEnum is the single definition shared by the
# needs_user pause, a research-campaign OpenQuestion, and the attention feed.
# These tests pin the ladder shape, the additive default on OpenQuestion, that
# every value round-trips (model + persistence seam), and that an off-ladder
# token is rejected at ingestion.


def test_urgency_ladder_is_the_closed_four_value_set() -> None:
    """The shared Urgency vocabulary is exactly low/normal/high/urgent."""
    assert [u.value for u in Urgency] == ["low", "normal", "high", "urgent"]


def test_open_question_urgency_defaults_to_normal() -> None:
    """Urgency is additive: an omitted value defaults to NORMAL (no schema bump)."""
    question = models.OpenQuestion.model_validate(
        {
            "id": "OQ-001",
            "scope_id": "ABC",
            "title": "minimal question",
            "status": "open",
            "created_at": _now().isoformat(),
        }
    )
    assert question.urgency is Urgency.NORMAL


def test_open_question_uses_the_shared_urgency_enum() -> None:
    """OpenQuestion.urgency references the one shared Urgency type, not a copy."""
    annotation = models.OpenQuestion.model_fields["urgency"].annotation
    assert annotation is Urgency


@pytest.mark.parametrize("value", [u.value for u in Urgency])
def test_open_question_urgency_round_trips_each_value(value: str) -> None:
    """Every urgency value survives a model_dump -> model_validate round-trip."""
    question = models.OpenQuestion.model_validate(_open_question_payload(urgency=value))
    assert question.urgency is Urgency(value)
    restored = models.OpenQuestion.model_validate(question.model_dump(mode="json"))
    assert restored.urgency is Urgency(value)
    assert restored == question


def test_open_question_rejects_unknown_urgency() -> None:
    """An off-ladder urgency token is rejected at ingestion (closed StrEnum)."""
    with pytest.raises(ValidationError):
        models.OpenQuestion.model_validate(_open_question_payload(urgency="emergency"))


def test_urgent_pause_question_round_trips_through_writer_seam(tmp_path: Path) -> None:
    """An urgent, blocking question (the attention-feed/pause shape) persists intact.

    Exercises the canonical-writer seam over a question carrying a non-default
    urgency so the round-trip the wave criterion calls for ("urgency on a pause
    round-trips") is pinned end to end: typed State -> commit_mutation -> strict
    re-read, with the urgent value surviving byte for byte.
    """
    candidate = models.State.model_validate(
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
