"""REL-017: the publication operation and its per-target attempt rows.

Three things are under test here and nothing else: the field contract of
:class:`~eawf.kernel.spec.publication.PublicationOperation` and
:class:`~eawf.kernel.spec.publication.OperationAttempt`, the append-only
receipt sequence keyed by ``(target_id, attempt)``, and the shipped
fixture validating against every target the authored ``dev1``
configuration declares.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.spec.publication import (
    OperationAttempt,
    PublicationOperation,
    PublicationOperationKind,
    PublicationOperationStatus,
    assert_append_only,
    attempt_count,
    latest_attempt,
    require_attempt,
)
from eawf.kernel.spec.release import ReleaseTargetStatus
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from tests.unit.kernel.release.conftest import dev1_config

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "release"
    / "v07"
    / "publication-operation-complete.yaml"
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
REQUEST_DIGEST = f"sha256:{'2' * 64}"
PROOF_DIGEST = f"sha256:{'1' * 64}"


def attempt_row(**overrides: Any) -> OperationAttempt:
    """Return a queued ``pypi`` attempt row with *overrides* applied."""
    payload: dict[str, Any] = {
        "target_id": "pypi",
        "attempt": 1,
        "status": ReleaseTargetStatus.QUEUED,
        "request_digest": REQUEST_DIGEST,
        "started_at": NOW,
        "deadline_at": NOW + timedelta(seconds=1800),
    }
    payload.update(overrides)
    return OperationAttempt(**payload)


def operation(**overrides: Any) -> PublicationOperation:
    """Return an open ``publish`` operation with *overrides* applied."""
    payload: dict[str, Any] = {
        "operation_id": UUID(int=27),
        "release_ref": "REL-0.7.0.dev1",
        "kind": PublicationOperationKind.PUBLISH,
        "proof_digest": PROOF_DIGEST,
        "idempotency_key": "publish-0.7.0.dev1-01",
        "opened_at": NOW,
    }
    payload.update(overrides)
    return PublicationOperation(**payload)


def fixture_body() -> dict[str, Any]:
    """Return the shipped fixture's ``operation`` block as a mutable dict."""
    decoded: dict[str, Any] = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    body: dict[str, Any] = copy.deepcopy(decoded["operation"])
    return body


# --- the shipped fixture -------------------------------------------------


def test_fixture_validates_with_every_configured_target() -> None:
    loaded = PublicationOperation.model_validate(fixture_body())
    configured = {target.target_id for target in dev1_config().targets}
    assert set(loaded.target_ids) == configured
    assert loaded.status is PublicationOperationStatus.SETTLED


def test_fixture_attempt_count_is_the_highest_persisted_attempt() -> None:
    loaded = PublicationOperation.model_validate(fixture_body())
    assert attempt_count(loaded, "pypi") == 2
    assert attempt_count(loaded, "npm") == 1
    assert attempt_count(loaded, "unconfigured") == 0
    assert require_attempt(loaded, "pypi").status is ReleaseTargetStatus.OBSERVED_SUCCESS


def test_fixture_round_trips_through_the_release_store_payload_model() -> None:
    loaded = PublicationOperation.model_validate(fixture_body())
    assert PAYLOAD_MODELS[StoreKind.RELEASE] is PublicationOperation
    replayed = PAYLOAD_MODELS[StoreKind.RELEASE].model_validate(loaded.model_dump(mode="json"))
    assert replayed == loaded


# --- OperationAttempt field contract -------------------------------------


def test_operation_attempt_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        OperationAttempt.model_validate({**attempt_row().model_dump(mode="json"), "extra": 1})


def test_operation_attempt_is_frozen() -> None:
    row = attempt_row()
    with pytest.raises(ValidationError):
        row.attempt = 2  # type: ignore[misc]


def test_operation_attempt_rejects_attempt_zero() -> None:
    with pytest.raises(ValidationError):
        attempt_row(attempt=0)


def test_operation_attempt_admits_attempt_one() -> None:
    assert attempt_row(attempt=1).attempt == 1


def test_operation_attempt_rejects_not_started() -> None:
    with pytest.raises(ValidationError, match="at least queued"):
        attempt_row(status=ReleaseTargetStatus.NOT_STARTED)


def test_operation_attempt_rejects_reported_success_without_effect_receipt() -> None:
    with pytest.raises(ValidationError, match="requires effect_receipt_ref"):
        attempt_row(status=ReleaseTargetStatus.REPORTED_SUCCESS, settled_at=NOW)


def test_operation_attempt_admits_unknown_without_effect_receipt() -> None:
    row = attempt_row(status=ReleaseTargetStatus.UNKNOWN, settled_at=NOW)
    assert row.effect_receipt_ref is None
    assert row.settled


def test_operation_attempt_rejects_observation_receipt_on_reported_row() -> None:
    with pytest.raises(ValidationError, match="must not carry observation_receipt_ref"):
        attempt_row(
            status=ReleaseTargetStatus.REPORTED_SUCCESS,
            effect_receipt_ref="receipt://pypi/upload",
            observation_receipt_ref="receipt://pypi/read-back",
            settled_at=NOW,
        )


def test_operation_attempt_rejects_observed_row_without_observation_receipt() -> None:
    with pytest.raises(ValidationError, match="requires observation_receipt_ref"):
        attempt_row(
            status=ReleaseTargetStatus.OBSERVED_SUCCESS,
            effect_receipt_ref="receipt://pypi/upload",
            settled_at=NOW,
        )


def test_operation_attempt_rejects_deadline_at_the_start_instant() -> None:
    with pytest.raises(ValidationError, match="deadline_at must be after started_at"):
        attempt_row(deadline_at=NOW)


def test_operation_attempt_admits_deadline_one_second_after_the_start() -> None:
    assert attempt_row(deadline_at=NOW + timedelta(seconds=1)).deadline_at > NOW


def test_operation_attempt_rejects_settled_at_on_queued_row() -> None:
    with pytest.raises(ValidationError, match="must not carry settled_at"):
        attempt_row(settled_at=NOW)


def test_operation_attempt_rejects_settled_status_without_settled_at() -> None:
    with pytest.raises(ValidationError, match="requires settled_at"):
        attempt_row(status=ReleaseTargetStatus.UNKNOWN)


def test_operation_attempt_rejects_settled_at_before_started_at() -> None:
    with pytest.raises(ValidationError, match="precedes started_at"):
        attempt_row(status=ReleaseTargetStatus.UNKNOWN, settled_at=NOW - timedelta(seconds=1))


def test_operation_attempt_rejects_malformed_request_digest() -> None:
    with pytest.raises(ValidationError):
        attempt_row(request_digest="deadbeef")


def test_operation_attempt_rejects_malformed_target_id() -> None:
    with pytest.raises(ValidationError):
        attempt_row(target_id="PyPI")


# --- PublicationOperation field contract ---------------------------------


def test_publication_operation_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        PublicationOperation.model_validate({**operation().model_dump(mode="json"), "extra": 1})


def test_publication_operation_is_frozen() -> None:
    op = operation()
    with pytest.raises(ValidationError):
        op.revision = 1  # type: ignore[misc]


def test_publication_operation_rejects_short_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        operation(idempotency_key="short7c")


def test_publication_operation_admits_eight_character_idempotency_key() -> None:
    assert operation(idempotency_key="k" * 8).idempotency_key == "k" * 8


def test_publication_operation_rejects_release_ref_that_is_not_a_release_key() -> None:
    with pytest.raises(ValidationError):
        operation(release_ref="0.7.0.dev1")


def test_publication_operation_admits_empty_receipt_sequence() -> None:
    assert operation().publication_receipts == ()
    assert operation().target_ids == ()
    assert latest_attempt(operation(), "pypi") is None


def test_publication_operation_admits_single_receipt() -> None:
    op = operation(publication_receipts=(attempt_row(),))
    assert attempt_count(op, "pypi") == 1


def test_publication_operation_rejects_duplicate_target_attempt_pair() -> None:
    with pytest.raises(ValidationError, match="duplicate receipt"):
        operation(publication_receipts=(attempt_row(), attempt_row()))


def test_publication_operation_rejects_gapped_attempt_numbers() -> None:
    with pytest.raises(ValidationError, match="out of order"):
        operation(publication_receipts=(attempt_row(), attempt_row(attempt=3)))


def test_publication_operation_rejects_descending_attempt_numbers() -> None:
    rows = (
        attempt_row(attempt=1, status=ReleaseTargetStatus.UNKNOWN, settled_at=NOW),
        attempt_row(attempt=2, status=ReleaseTargetStatus.UNKNOWN, settled_at=NOW),
    )
    with pytest.raises(ValidationError, match="out of order"):
        operation(publication_receipts=(rows[1], rows[0]))


def test_publication_operation_rejects_settled_status_with_an_open_leg() -> None:
    with pytest.raises(ValidationError, match="open legs"):
        operation(
            status=PublicationOperationStatus.SETTLED,
            publication_receipts=(attempt_row(),),
        )


def test_publication_operation_rejects_settled_status_with_no_receipts() -> None:
    with pytest.raises(ValidationError, match="no publication receipts"):
        operation(status=PublicationOperationStatus.SETTLED)


def test_publication_operation_target_ids_preserve_first_seen_order() -> None:
    op = operation(
        publication_receipts=(
            attempt_row(target_id="npm"),
            attempt_row(target_id="pypi"),
            attempt_row(target_id="npm", attempt=2),
        )
    )
    assert op.target_ids == ("npm", "pypi")


# --- append-only receipt sequence ----------------------------------------


def test_assert_append_only_admits_a_pure_extension() -> None:
    previous = operation(publication_receipts=(attempt_row(),))
    successor = previous.model_copy(
        update={
            "publication_receipts": (*previous.publication_receipts, attempt_row(target_id="npm")),
            "revision": 1,
        }
    )
    assert assert_append_only(previous, successor) is None


def test_assert_append_only_admits_in_place_status_advance() -> None:
    previous = operation(publication_receipts=(attempt_row(),))
    successor = previous.model_copy(
        update={
            "publication_receipts": (attempt_row(status=ReleaseTargetStatus.IN_FLIGHT),),
            "revision": 1,
        }
    )
    assert assert_append_only(previous, successor) is None


def test_assert_append_only_rejects_a_dropped_row() -> None:
    previous = operation(publication_receipts=(attempt_row(), attempt_row(target_id="npm")))
    successor = previous.model_copy(
        update={"publication_receipts": (attempt_row(),), "revision": 1}
    )
    with pytest.raises(ValueError, match="append-only"):
        assert_append_only(previous, successor)


def test_assert_append_only_rejects_a_reordered_row() -> None:
    previous = operation(publication_receipts=(attempt_row(), attempt_row(target_id="npm")))
    successor = previous.model_copy(
        update={
            "publication_receipts": (attempt_row(target_id="npm"), attempt_row()),
            "revision": 1,
        }
    )
    with pytest.raises(ValueError, match="append-only"):
        assert_append_only(previous, successor)


def test_assert_append_only_rejects_a_stale_revision() -> None:
    previous = operation(revision=3)
    successor = previous.model_copy(update={"revision": 3})
    with pytest.raises(ValueError, match="must exceed"):
        assert_append_only(previous, successor)


def test_assert_append_only_rejects_a_different_operation_id() -> None:
    previous = operation()
    successor = previous.model_copy(update={"operation_id": UUID(int=99), "revision": 1})
    with pytest.raises(ValueError, match="does not match"):
        assert_append_only(previous, successor)


def test_require_attempt_raises_key_error_for_an_unattempted_target() -> None:
    with pytest.raises(KeyError, match="no attempt for target"):
        require_attempt(operation(), "pypi")
