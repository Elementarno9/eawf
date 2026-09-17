"""An approved record walks to BAKED through the daemon handlers alone.

Nothing here calls the release library to move a record or patches a
probe. The record is pinned to a real dev1 checkout, approved by
``release.approve``, and then carried by ``release.publish``, three
receipt-bearing ``release.reconcile`` calls and three
``release.observe_target`` calls. ``release.show`` is asked after every
step, because the defect this module pins is that the verbs moved the
record in their replies while the store went on reporting ``approved``.

Four claims are pinned.

**The receipt proves the dispatch.** A publish job's receipt names the
run that made the call, so a reconcile carrying it settles a leg that
nothing marked as dispatched: ``queued`` to ``in_flight`` to
``reported_success`` in one call. An asserted status proves nothing and
still cannot.

**The guarded edges fire in the handlers.** The reconcile that completes
the required reports opens verification; the observation that completes
the required read-backs bakes, including for a record that was still
publishing when its last timed-out leg was read back.

**Every transition is recorded.** The record collection carries one row
per revision from the approval to the bake, so the lineage has no gap.

**A replay writes nothing.** Repeating any walked call, with the payload
it was first sent or with the record as it stands now, answers the
original receipt and leaves both stores byte-identical.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.publication import PublicationOperation, require_attempt
from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import observe, publish, reconcile, show
from eawf.runtime.daemon.methods.release_keyed import (
    persist_walk,
    replay_record,
    request_identity,
)
from eawf.workflow.release.ledger import ledger_path
from eawf.workflow.release.records import record_release, release_records_path
from tests.integration.runtime.daemon.methods.conftest import (
    DEV1_VERSION,
    EFFECT,
    RELEASE_KEY,
    RUN_ID,
    TARGET_IDS,
    approve_pinned,
    dev1_config,
    matched_observe_params,
    pinned_payload,
    pinned_publish_params,
    publication_receipt,
    receipt_reconcile_params,
)

pytestmark = pytest.mark.integration

Handler = Callable[[MethodContext, dict[str, Any]], Awaitable[dict[str, Any]]]

#: How far the handler clock is moved to let every dev1 leg's own
#: deadline elapse.
PAST_DEADLINE = timedelta(
    seconds=max(target.timeout_seconds for target in dev1_config().targets) + 1
)

#: The handler module's clock, as :func:`monkeypatch.setattr` addresses it.
HANDLER_CLOCK = "eawf.runtime.daemon.methods.release.datetime"


def call(handler: Handler, ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Drive one handler to completion and return its result."""

    async def run() -> dict[str, Any]:
        return await handler(ctx, params)

    return asyncio.run(run())


def shown(ctx: MethodContext) -> dict[str, Any]:
    """Return the record ``release.show`` reports for the dev1 rung."""
    record: dict[str, Any] = call(show, ctx, {"version": DEV1_VERSION})["record"]
    return record


def store_bytes(ctx: MethodContext) -> tuple[bytes, bytes]:
    """Return the ledger and the record collection, byte for byte."""
    state_path = Path(str(ctx.state_path))
    return ledger_path(state_path).read_bytes(), release_records_path(state_path).read_bytes()


def recorded_rows(ctx: MethodContext) -> list[dict[str, Any]]:
    """Return the payload of every row in the record collection, in order."""
    path = release_records_path(Path(str(ctx.state_path)))
    return [
        json.loads(line)["payload"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def leg(result: dict[str, Any], target_id: str) -> ReleaseTargetStatus:
    """Return where *target_id* stands in the operation a verb answered with."""
    operation = PublicationOperation.model_validate(result["operation"])
    return require_attempt(operation, target_id).status


def publish_params_for(repo: Path, approved: dict[str, Any]) -> dict[str, Any]:
    """Return ``release.publish`` params presenting the recorded approval."""
    return pinned_publish_params(repo, release=approved, expected_revision=approved["revision"])


def walk(
    ctx: MethodContext,
    repo: Path,
    approved: dict[str, Any],
) -> list[tuple[Handler, dict[str, Any], dict[str, Any]]]:
    """Walk *approved* to BAKED and return every call with its params and reply."""
    calls: list[tuple[Handler, dict[str, Any], dict[str, Any]]] = []
    params = publish_params_for(repo, approved)
    result = call(publish, ctx, params)
    calls.append((publish, params, result))
    for target_id in TARGET_IDS:
        params = receipt_reconcile_params(result["release"], target_id)
        result = call(reconcile, ctx, params)
        calls.append((reconcile, params, result))
    for target_id in TARGET_IDS:
        params = matched_observe_params(result["release"], target_id)
        result = call(observe, ctx, params)
        calls.append((observe, params, result))
    return calls


def timed_out_params(release: dict[str, Any], target_id: str) -> dict[str, Any]:
    """Return a reconcile whose receipt says the job was cancelled."""
    return receipt_reconcile_params(
        release,
        target_id,
        receipt=publication_receipt(target_id, job_conclusion="cancelled"),
    )


class _PastDeadlineClock:
    """Stands in for the handler module's ``datetime`` once time has passed.

    The handlers stamp wall-clock ``now`` and a leg's deadline is half an
    hour out, so the clock is moved rather than waited on.
    """

    @staticmethod
    def now(tz: tzinfo | None = None) -> datetime:
        """Return the real instant, moved past every leg's deadline."""
        return datetime.now(tz) + PAST_DEADLINE


@pytest.fixture
def approved(walk_ctx: MethodContext, dev1_checkout: Path) -> dict[str, Any]:
    """Return the dev1 record approved and recorded by the daemon verbs."""
    return asyncio.run(approve_pinned(walk_ctx, dev1_checkout))


# --- the walk ---------------------------------------------------------------


def test_handlers_walk_an_approved_record_to_baked(
    walk_ctx: MethodContext, dev1_checkout: Path, approved: dict[str, Any]
) -> None:
    """``release.show`` reports publishing, then verifying, then baked."""
    assert shown(walk_ctx)["status"] == ReleaseStatus.APPROVED.value

    result = call(publish, walk_ctx, publish_params_for(dev1_checkout, approved))
    assert shown(walk_ctx) == result["release"]
    assert shown(walk_ctx)["status"] == ReleaseStatus.PUBLISHING.value
    assert {leg(result, target_id) for target_id in TARGET_IDS} == {ReleaseTargetStatus.QUEUED}

    reconciled = []
    for target_id in TARGET_IDS:
        result = call(reconcile, walk_ctx, receipt_reconcile_params(result["release"], target_id))
        assert leg(result, target_id) is ReleaseTargetStatus.REPORTED_SUCCESS
        assert shown(walk_ctx) == result["release"]
        reconciled.append(shown(walk_ctx)["status"])
    assert reconciled == ["publishing", "publishing", "verifying"]

    observed = []
    for target_id in TARGET_IDS:
        result = call(observe, walk_ctx, matched_observe_params(result["release"], target_id))
        assert leg(result, target_id) is ReleaseTargetStatus.OBSERVED_SUCCESS
        assert shown(walk_ctx) == result["release"]
        observed.append(shown(walk_ctx)["status"])
    assert observed == ["verifying", "verifying", "baked"]
    baked = Release.model_validate(shown(walk_ctx))
    assert set(baked.target_statuses.values()) == {ReleaseTargetStatus.OBSERVED_SUCCESS}


def test_handlers_record_one_row_per_revision_from_approval_to_bake(
    walk_ctx: MethodContext, dev1_checkout: Path, approved: dict[str, Any]
) -> None:
    """The third reconcile records two rows: the settled leg and the verification."""
    walk(walk_ctx, dev1_checkout, approved)

    rows = recorded_rows(walk_ctx)
    first = approved["revision"]
    assert [row["revision"] for row in rows] == list(range(first, first + 9))
    assert [row["status"] for row in rows] == [
        "approved",
        "publishing",
        "publishing",
        "publishing",
        "publishing",
        "verifying",
        "verifying",
        "verifying",
        "baked",
    ]
    assert shown(walk_ctx) == rows[-1]


def test_replaying_every_walked_call_writes_nothing(
    walk_ctx: MethodContext, dev1_checkout: Path, approved: dict[str, Any]
) -> None:
    """Each repeat answers its original receipt and the recorded record."""
    calls = walk(walk_ctx, dev1_checkout, approved)
    before = store_bytes(walk_ctx)

    for handler, params, first in calls:
        again = call(handler, walk_ctx, params)
        assert again["replayed"] is True
        assert again["operation"] == first["operation"]
        assert again["release"] == shown(walk_ctx)

    assert store_bytes(walk_ctx) == before
    assert shown(walk_ctx)["status"] == ReleaseStatus.BAKED.value


def test_replaying_with_the_current_record_writes_nothing(
    walk_ctx: MethodContext, dev1_checkout: Path, approved: dict[str, Any]
) -> None:
    """A caller that re-read the record before repeating still gets the replay."""
    calls = walk(walk_ctx, dev1_checkout, approved)
    before = store_bytes(walk_ctx)
    current = shown(walk_ctx)

    for handler, params, _first in calls:
        repeated = {**params, "release": current, "expected_revision": current["revision"]}
        assert call(handler, walk_ctx, repeated)["replayed"] is True

    assert store_bytes(walk_ctx) == before


# --- reconcile --------------------------------------------------------------


def test_reconcile_moves_a_queued_leg_through_in_flight_on_a_receipt(
    walk_ctx: MethodContext, dev1_checkout: Path, approved: dict[str, Any]
) -> None:
    """The receipt's run id stands in for the dispatch nothing recorded."""
    published = call(publish, walk_ctx, publish_params_for(dev1_checkout, approved))

    result = call(reconcile, walk_ctx, receipt_reconcile_params(published["release"], "pypi"))

    operation = PublicationOperation.model_validate(result["operation"])
    row = require_attempt(operation, "pypi")
    assert row.status is ReleaseTargetStatus.REPORTED_SUCCESS
    assert row.effect_receipt_ref == f"receipt://pypi/run/{RUN_ID}"
    assert row.settled_at is not None
    assert (
        operation.revision
        == PublicationOperation.model_validate(published["operation"]).revision + 2
    )
    assert leg(result, "npm") is ReleaseTargetStatus.QUEUED
    assert shown(walk_ctx)["status"] == ReleaseStatus.PUBLISHING.value
    assert shown(walk_ctx)["target_statuses"]["pypi"] == "reported_success"


def test_reconcile_refuses_an_asserted_status_on_a_queued_leg(
    walk_ctx: MethodContext, dev1_checkout: Path, approved: dict[str, Any]
) -> None:
    """An operator's word proves no dispatch, so the queued leg stays refused."""
    published = call(publish, walk_ctx, publish_params_for(dev1_checkout, approved))
    before = store_bytes(walk_ctx)
    params = {
        "release": published["release"],
        "expected_revision": published["release"]["revision"],
        "idempotency_key": "reconcile-asserted-queued",
        "target_id": "pypi",
        "status": ReleaseTargetStatus.REPORTED_SUCCESS.value,
        "effect_receipt_ref": EFFECT,
    }

    with pytest.raises(DaemonValidationError, match="illegal_target_transition"):
        call(reconcile, walk_ctx, params)

    assert store_bytes(walk_ctx) == before


def test_reconcile_keeps_the_record_publishing_while_a_required_leg_failed(
    walk_ctx: MethodContext, dev1_checkout: Path, approved: dict[str, Any]
) -> None:
    """A reported failure is a recovery question, so verification stays shut."""
    result = call(publish, walk_ctx, publish_params_for(dev1_checkout, approved))
    failed = publication_receipt("pypi", job_conclusion="failure")

    result = call(
        reconcile, walk_ctx, receipt_reconcile_params(result["release"], "pypi", receipt=failed)
    )
    for target_id in ("npm", "github"):
        result = call(reconcile, walk_ctx, receipt_reconcile_params(result["release"], target_id))

    assert leg(result, "pypi") is ReleaseTargetStatus.REPORTED_FAILURE
    assert shown(walk_ctx)["status"] == ReleaseStatus.PUBLISHING.value
    assert shown(walk_ctx)["target_statuses"]["pypi"] == "reported_failure"


def test_reconcile_refuses_a_second_receipt_for_a_settled_leg(
    walk_ctx: MethodContext, dev1_checkout: Path, approved: dict[str, Any]
) -> None:
    """A leg that already reported cannot report again under a new key."""
    result = call(publish, walk_ctx, publish_params_for(dev1_checkout, approved))
    result = call(reconcile, walk_ctx, receipt_reconcile_params(result["release"], "pypi"))
    before = store_bytes(walk_ctx)

    with pytest.raises(DaemonValidationError, match="illegal_target_transition"):
        call(
            reconcile,
            walk_ctx,
            receipt_reconcile_params(
                result["release"], "pypi", idempotency_key="reconcile-pypi-again"
            ),
        )

    assert store_bytes(walk_ctx) == before


# --- observe ----------------------------------------------------------------


def test_observe_opens_verification_for_a_record_still_publishing(
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    approved: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading back the one timed-out leg completes the required reports."""
    result = call(publish, walk_ctx, publish_params_for(dev1_checkout, approved))
    monkeypatch.setattr(HANDLER_CLOCK, _PastDeadlineClock)
    for target_id in ("pypi", "npm"):
        result = call(reconcile, walk_ctx, receipt_reconcile_params(result["release"], target_id))
    result = call(reconcile, walk_ctx, timed_out_params(result["release"], "github"))
    assert leg(result, "github") is ReleaseTargetStatus.UNKNOWN
    assert shown(walk_ctx)["status"] == ReleaseStatus.PUBLISHING.value

    result = call(observe, walk_ctx, matched_observe_params(result["release"], "github"))
    assert shown(walk_ctx)["status"] == ReleaseStatus.VERIFYING.value

    for target_id in ("pypi", "npm"):
        result = call(observe, walk_ctx, matched_observe_params(result["release"], target_id))
    assert shown(walk_ctx)["status"] == ReleaseStatus.BAKED.value


def test_observe_bakes_a_publishing_record_in_the_call_that_reads_the_last_leg(
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    approved: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One call records the read-back, the verification and the bake."""
    result = call(publish, walk_ctx, publish_params_for(dev1_checkout, approved))
    monkeypatch.setattr(HANDLER_CLOCK, _PastDeadlineClock)
    for target_id in TARGET_IDS:
        result = call(reconcile, walk_ctx, timed_out_params(result["release"], target_id))
    for target_id in ("pypi", "npm"):
        result = call(observe, walk_ctx, matched_observe_params(result["release"], target_id))
    assert shown(walk_ctx)["status"] == ReleaseStatus.PUBLISHING.value
    rows_before = len(recorded_rows(walk_ctx))

    result = call(observe, walk_ctx, matched_observe_params(result["release"], "github"))

    added = recorded_rows(walk_ctx)[rows_before:]
    assert [row["status"] for row in added] == ["publishing", "verifying", "baked"]
    assert result["release"] == added[-1] == shown(walk_ctx)


# --- the shared keyed-verb plumbing -----------------------------------------


def test_request_identity_ignores_the_revision_the_caller_holds() -> None:
    """The payload and expected revision describe the caller, not the request."""
    first = {"release": {"revision": 5}, "expected_revision": 5, "target_id": "pypi"}
    moved_on = {"release": {"revision": 9}, "expected_revision": 9, "target_id": "pypi"}
    assert request_identity("release.reconcile", first, release_key=RELEASE_KEY) == (
        request_identity("release.reconcile", moved_on, release_key=RELEASE_KEY)
    )


def test_request_identity_ignores_the_idempotency_key_itself() -> None:
    """Two keys asking the same thing ask the same thing."""
    one = {"idempotency_key": "a", "target_id": "pypi"}
    two = {"idempotency_key": "b", "target_id": "pypi"}
    assert request_identity("release.reconcile", one, release_key=RELEASE_KEY) == (
        request_identity("release.reconcile", two, release_key=RELEASE_KEY)
    )


@pytest.mark.parametrize(
    ("method", "params", "release_key"),
    [
        ("release.reconcile", {"target_id": "npm"}, RELEASE_KEY),
        ("release.reconcile", {"target_id": "pypi"}, "REL-0.7.0.dev2"),
        ("release.observe_target", {"target_id": "pypi"}, RELEASE_KEY),
    ],
)
def test_request_identity_separates_a_different_request(
    method: str, params: dict[str, Any], release_key: str
) -> None:
    """Another leg, another checkpoint or another verb is another request."""
    baseline = request_identity("release.reconcile", {"target_id": "pypi"}, release_key=RELEASE_KEY)
    assert request_identity(method, params, release_key=release_key) != baseline


def test_request_identity_of_empty_params_still_names_the_checkpoint() -> None:
    """With nothing asked, the key alone still separates two checkpoints."""
    assert request_identity("release.publish", {}, release_key=RELEASE_KEY) != (
        request_identity("release.publish", {}, release_key="REL-0.7.0.dev2")
    )


def test_persist_walk_refuses_an_empty_walk(tmp_path: Path) -> None:
    """A call that walked nowhere has nothing to answer with."""
    with pytest.raises(ValueError, match="at least one record"):
        persist_walk(
            tmp_path / ".ea" / "state.json",
            (),
            recorded_at=datetime.now().astimezone(),
            summary="x",
        )


def test_persist_walk_refuses_a_naive_instant(tmp_path: Path, dev1_checkout: Path) -> None:
    """A row whose instant carries no zone cannot be ordered."""
    record = Release.model_validate(pinned_payload(dev1_checkout))
    with pytest.raises(ValueError, match="timezone-aware"):
        persist_walk(
            tmp_path / ".ea" / "state.json", (record,), recorded_at=datetime.now(), summary="x"
        )


def test_replay_record_falls_back_to_the_presented_record(
    tmp_path: Path, dev1_checkout: Path
) -> None:
    """A ledger row that landed without its record rows answers the payload."""
    presented = Release.model_validate(pinned_payload(dev1_checkout))
    assert replay_record(tmp_path / ".ea" / "state.json", presented) == presented


def test_replay_record_answers_the_recorded_record(tmp_path: Path, dev1_checkout: Path) -> None:
    """Where the collection holds the key, the recorded record answers."""
    state_path = tmp_path / ".ea" / "state.json"
    presented = Release.model_validate(pinned_payload(dev1_checkout))
    later = presented.model_copy(update={"revision": presented.revision + 3})
    record_release(state_path, later, recorded_at=datetime.now().astimezone(), summary="later")
    assert replay_record(state_path, presented) == later


def test_replay_record_refuses_a_corrupt_collection(tmp_path: Path, dev1_checkout: Path) -> None:
    """A collection that cannot be read is a refusal, not a silent fallback."""
    state_path = tmp_path / ".ea" / "state.json"
    path = release_records_path(state_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not an envelope\n", encoding="utf-8")
    presented = Release.model_validate(pinned_payload(dev1_checkout))
    with pytest.raises(DaemonValidationError, match="is not an envelope"):
        replay_record(state_path, presented)
