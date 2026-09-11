"""``no_external_effect`` answered from the record, not from the caller.

``CANCELLED`` carries the guard ``NO_EXTERNAL_EFFECT`` on every edge
into it, documented as "No tag, upload or other external effect has been
observed for this release yet". When ``0.7.0.dev1`` had been published
to four targets, that guard would still have passed -- not because it
was mis-declared, but because it was answered by a caller-supplied
default that no caller ever computed, and the record itself knew nothing
about the publication.

Two things are pinned here, and the first is what makes the second
mean anything.

**The blindness is real.** ``validate_release_transition`` with the
default guard context admits the cancel edge for a record whose own
fields show an adopted publication, an opened publication operation and
four observed legs. That is the pre-fix behaviour, asserted rather than
described, so the refusal below cannot be mistaken for something the
table was already doing.

**The refusal is wired.** :func:`cancel_release` derives the guard from
the record through :func:`external_effect_started`, and the operator
verb ``release.cancel`` is the only door into it -- so an operator
reaching for a cancel on a record that can see its own publication is
denied ``release_effect_already_started``, and nothing is appended to
the record collection.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release_disposition import adopt, cancel
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.lifecycle import (
    ReleaseDenialCode,
    ReleaseGuardContext,
    ReleaseTransitionError,
    cancel_release,
    external_effect_started,
    validate_release_transition,
)
from eawf.workflow.release.records import read_release_record, release_records_path
from tests._release_helpers import (
    SOURCE_SHA,
    TREE_SHA,
    dev1_adoption,
    dev1_config,
    dev1_draft,
)

pytestmark = pytest.mark.integration

DEV1_VERSION = "0.7.0.dev1"
DEV1_KEY = f"REL-{DEV1_VERSION}"

MANIFEST_DIGEST = f"sha256:{'c' * 64}"

CANCEL_REASON = "nothing was ever published under this version"

#: The statuses whose cancel edge exists, so the guard on it can be
#: reached at all. DRAFT joins the three that already had one.
CANCELLABLE_STATUSES = (
    ReleaseStatus.DRAFT,
    ReleaseStatus.CANDIDATE,
    ReleaseStatus.PREFLIGHT_FAILED,
    ReleaseStatus.APPROVED,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a throwaway state root the verbs may record into."""
    path = tmp_path / ".ea" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(path, load_state(_EMPTY_STATE))
    return path


@pytest.fixture
def ctx(state_path: Path) -> MethodContext:
    """Return a method context bound to the recording state root."""
    return MethodContext(
        started_at="2026-09-11T00:00:00+00:00",
        pid=7745,
        protocol_version="1",
        version=DEV1_VERSION,
        state_path=state_path,
    )


def at_status(status: ReleaseStatus, **overrides: Any) -> Release:
    """Return a dev1 record standing at *status*, pinned where required."""
    payload: dict[str, Any] = {"status": status}
    if status is not ReleaseStatus.DRAFT:
        payload |= {
            "source_sha": SOURCE_SHA,
            "source_tree_sha": TREE_SHA,
            "manifest_ref": "artifact://release/manifest/0.7.0.dev1",
            "manifest_digest": MANIFEST_DIGEST,
        }
    if status is ReleaseStatus.APPROVED:
        payload["approval_ref"] = "receipt://approval/dev1"
    payload.update(overrides)
    return Release.model_validate(dev1_draft().model_copy(update=payload).model_dump(mode="json"))


def adopted() -> Release:
    """Return the dev1 draft with its out-of-band publication adopted."""
    return adopt_publication(dev1_draft(), dev1_config(), adoption=dev1_adoption())


def cancel_params(record: Release, **overrides: Any) -> dict[str, Any]:
    """Return well-formed ``release.cancel`` params for *record*."""
    params: dict[str, Any] = {
        "release": record.model_dump(mode="json"),
        "expected_revision": record.revision,
        "reason": CANCEL_REASON,
    }
    params.update(overrides)
    return params


# --- the blindness the fix removes -------------------------------------


def test_the_default_guard_context_still_admits_the_cancel_edge() -> None:
    """The pre-fix answer: the table alone cannot see the publication."""
    record = adopted()
    assert record.target_statuses  # the record does carry the read-backs

    validate_release_transition(record.status, ReleaseStatus.CANCELLED, ReleaseGuardContext())


def test_a_caller_supplied_guard_is_what_used_to_decide() -> None:
    """The same edge is denied only once someone computes the predicate."""
    with pytest.raises(ReleaseTransitionError) as excinfo:
        validate_release_transition(
            ReleaseStatus.DRAFT,
            ReleaseStatus.CANCELLED,
            ReleaseGuardContext(external_effect_started=True),
        )

    assert excinfo.value.code is ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED


# --- the record answering for itself -----------------------------------


def test_external_effect_started_is_false_for_an_untouched_draft() -> None:
    """A record with nothing recorded against it has started nothing."""
    assert external_effect_started(dev1_draft()) is False


def test_external_effect_started_ignores_a_leg_that_never_started() -> None:
    """``not_started`` is the absence of effect, not a weak form of it."""
    seeded = at_status(
        ReleaseStatus.DRAFT,
        target_statuses={"pypi": ReleaseTargetStatus.NOT_STARTED},
    )

    assert external_effect_started(seeded) is False


@pytest.mark.parametrize(
    "status",
    [
        ReleaseTargetStatus.QUEUED,
        ReleaseTargetStatus.IN_FLIGHT,
        ReleaseTargetStatus.REPORTED_SUCCESS,
        ReleaseTargetStatus.REPORTED_FAILURE,
        ReleaseTargetStatus.UNKNOWN,
        ReleaseTargetStatus.OBSERVED_SUCCESS,
        ReleaseTargetStatus.OBSERVED_MISMATCH,
    ],
)
def test_external_effect_started_sees_any_leg_past_not_started(
    status: ReleaseTargetStatus,
) -> None:
    """One queued leg is already an episode somebody has to account for."""
    seeded = at_status(ReleaseStatus.DRAFT, target_statuses={"pypi": status})

    assert external_effect_started(seeded) is True


def test_external_effect_started_sees_an_opened_publication_operation() -> None:
    """An episode reference is effect even before a leg reports."""
    seeded = at_status(
        ReleaseStatus.CANDIDATE,
        publication_operation_ref="operation://REL-0.7.0.dev1/1",
    )

    assert external_effect_started(seeded) is True


def test_external_effect_started_sees_an_adoption() -> None:
    """The adopted read-backs are the effect the record used to miss."""
    assert external_effect_started(adopted()) is True


# --- the refusal, through the operator surface -------------------------


def test_cancel_refuses_the_adopted_record(ctx: MethodContext) -> None:
    """A record that can see its own publication cannot be cancelled."""
    with pytest.raises(DaemonValidationError) as excinfo:
        asyncio.run(cancel(ctx, cancel_params(adopted())))

    assert "release_effect_already_started" in str(excinfo.value)


def test_cancel_refuses_the_record_the_adopt_verb_just_wrote(
    ctx: MethodContext, state_path: Path
) -> None:
    """The refusal holds on the persisted record, not just a built one."""
    asyncio.run(
        adopt(
            ctx,
            {
                "release": dev1_draft().model_dump(mode="json"),
                "expected_revision": 0,
                "adoption": dev1_adoption().model_dump(mode="json"),
            },
        )
    )
    stored = read_release_record(state_path, DEV1_KEY)
    assert stored is not None

    with pytest.raises(DaemonValidationError, match="release_effect_already_started"):
        asyncio.run(cancel(ctx, cancel_params(stored)))


def test_a_refused_cancel_appends_nothing(ctx: MethodContext, state_path: Path) -> None:
    """A denied verb leaves the collection exactly as it found it."""
    before = release_records_path(state_path).exists()

    with pytest.raises(DaemonValidationError):
        asyncio.run(cancel(ctx, cancel_params(adopted())))

    assert release_records_path(state_path).exists() is before
    assert read_release_record(state_path, DEV1_KEY) is None


@pytest.mark.parametrize("status", CANCELLABLE_STATUSES)
def test_cancel_refuses_every_cancellable_status_that_carries_effect(
    ctx: MethodContext, status: ReleaseStatus
) -> None:
    """The guard bites wherever the edge exists, not only out of DRAFT."""
    record = at_status(status, target_statuses={"pypi": ReleaseTargetStatus.OBSERVED_MISMATCH})

    with pytest.raises(DaemonValidationError, match="release_effect_already_started"):
        asyncio.run(cancel(ctx, cancel_params(record)))


# --- the refusal is not vacuous ----------------------------------------


@pytest.mark.parametrize("status", CANCELLABLE_STATUSES)
def test_cancel_admits_a_record_that_touched_nothing(
    ctx: MethodContext, state_path: Path, status: ReleaseStatus
) -> None:
    """A clean record still cancels, so the guard is a filter not a wall."""
    result = asyncio.run(cancel(ctx, cancel_params(at_status(status))))

    assert result["release"]["status"] == ReleaseStatus.CANCELLED.value
    assert result["reason"] == CANCEL_REASON
    settled = read_release_record(state_path, DEV1_KEY)
    assert settled is not None
    assert settled.status is ReleaseStatus.CANCELLED


def test_cancel_records_its_reason_on_the_row_it_appends(
    ctx: MethodContext, state_path: Path
) -> None:
    """``cancelled`` is terminal, so the cause arrives with the call."""
    asyncio.run(cancel(ctx, cancel_params(at_status(ReleaseStatus.DRAFT))))

    rows = release_records_path(state_path).read_text(encoding="utf-8").splitlines()
    assert any(CANCEL_REASON in row for row in rows)


def test_cancel_refuses_a_blank_reason(ctx: MethodContext) -> None:
    """Whitespace is stripped before the length check."""
    with pytest.raises(Exception, match="at least 1 character"):
        asyncio.run(cancel(ctx, cancel_params(at_status(ReleaseStatus.DRAFT), reason="   ")))


def test_cancel_refuses_a_stale_revision(ctx: MethodContext) -> None:
    """A caller holding an old record is refused, not obeyed."""
    with pytest.raises(DaemonValidationError, match="stale_release_revision"):
        asyncio.run(cancel(ctx, cancel_params(at_status(ReleaseStatus.DRAFT), expected_revision=4)))


@pytest.mark.parametrize(
    "status",
    [
        ReleaseStatus.PUBLISHING,
        ReleaseStatus.VERIFYING,
        ReleaseStatus.RECOVERING,
        ReleaseStatus.CANCELLED,
    ],
)
def test_cancel_is_an_illegal_edge_where_the_table_declares_none(
    ctx: MethodContext, status: ReleaseStatus
) -> None:
    """Past the pre-effect states there is no cancel edge to guard."""
    record = at_status(status, approval_ref="receipt://approval/dev1")

    with pytest.raises(DaemonValidationError, match="illegal_release_transition"):
        asyncio.run(cancel(ctx, cancel_params(record)))


def test_cancel_release_raises_the_typed_denial_not_a_bare_value_error() -> None:
    """The library surface answers with the named code the operator quotes."""
    with pytest.raises(ReleaseTransitionError) as excinfo:
        cancel_release(adopted())

    assert excinfo.value.code is ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED
    assert excinfo.value.frm is ReleaseStatus.DRAFT
    assert excinfo.value.to is ReleaseStatus.CANCELLED
