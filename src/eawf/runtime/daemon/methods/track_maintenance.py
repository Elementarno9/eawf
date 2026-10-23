"""The verbs that rename a Track and set its advisory Milestone limit.

Neither is a lifecycle move: the Track stays ACTIVE, so each commits a
same-status edit through
:func:`~eawf.runtime.daemon.methods.milestone_maintenance.commit_record_edit`
and emits a firehose row outside the closed ``domain`` vocabulary. A
RETIRED Track is refused, because a retired Track is read as the record
of what it was.

``domain.track.set_policy`` sets ``wip.active_milestones`` and moves the
policy's own revision with it, because that revision is what a proof taken
under the policy binds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt, TitleStr
from eawf.kernel.state.epoch2.track import Track, TrackStatus
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import CommittedTransaction
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.milestone_maintenance import (
    RecordEditRequest,
    commit_record_edit,
    run_record_edit,
)
from eawf.runtime.daemon.native_guard import native_mutator

#: The dotted JSON-RPC name of the rename.
TRACK_RENAME: Final = "domain.track.rename"

#: The dotted JSON-RPC name of the policy edit.
TRACK_SET_POLICY: Final = "domain.track.set_policy"

#: The firehose name of a committed rename.
RENAMED_EVENT: Final = "track.renamed"

#: The firehose name of a committed policy edit.
POLICY_SET_EVENT: Final = "track.policy_set"

#: The one status a Track is edited in.
_EDITABLE: Final = frozenset({TrackStatus.ACTIVE})


class TrackRenameRequest(RecordEditRequest):
    """The strict parameters of :data:`TRACK_RENAME`.

    Attributes:
        title: The Track's new title.
    """

    title: TitleStr


class TrackPolicyRequest(RecordEditRequest):
    """The strict parameters of :data:`TRACK_SET_POLICY`.

    Attributes:
        active_milestones: How many ACTIVE Milestones the Track advises at most.
    """

    active_milestones: StrictNonNegativeInt


def rename_track(
    context: Epoch2RootContext, request: TrackRenameRequest, now: datetime
) -> CommittedTransaction:
    """Commit an ACTIVE Track's new title, or refuse having written nothing.

    Raises:
        TransactionRefusedError: As ``commit_record_edit`` raises it.
    """
    return commit_record_edit(
        context,
        request,
        method=TRACK_RENAME,
        model=Track,
        editable=_EDITABLE,
        fields=lambda _track: {"title": request.title},
        event_name=RENAMED_EVENT,
        now=now,
    )


def _policy_fields(track: Track, active_milestones: int) -> dict[str, Any]:
    """Return *track*'s policy with the new advisory limit, one policy revision on."""
    policy = track.policy.model_dump(mode="json")
    policy["wip"]["active_milestones"] = active_milestones
    policy["revision"] = track.policy.revision + 1
    return {"policy": policy}


def set_track_policy(
    context: Epoch2RootContext, request: TrackPolicyRequest, now: datetime
) -> CommittedTransaction:
    """Commit an ACTIVE Track's new ``wip.active_milestones``, or refuse having written nothing.

    Raises:
        TransactionRefusedError: As ``commit_record_edit`` raises it.
    """
    return commit_record_edit(
        context,
        request,
        method=TRACK_SET_POLICY,
        model=Track,
        editable=_EDITABLE,
        fields=lambda track: _policy_fields(track, request.active_milestones),
        event_name=POLICY_SET_EVENT,
        now=now,
    )


@native_mutator(TRACK_RENAME)
async def _track_rename(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Give one ACTIVE Track a new title; its status does not move."""
    return await run_record_edit(
        ctx,
        params,
        authority,
        method=TRACK_RENAME,
        request_model=TrackRenameRequest,
        commit=rename_track,
    )


@native_mutator(TRACK_SET_POLICY)
async def _track_set_policy(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Set how many ACTIVE Milestones one ACTIVE Track advises at most."""
    return await run_record_edit(
        ctx,
        params,
        authority,
        method=TRACK_SET_POLICY,
        request_model=TrackPolicyRequest,
        commit=set_track_policy,
    )


__all__ = [
    "POLICY_SET_EVENT",
    "RENAMED_EVENT",
    "TRACK_RENAME",
    "TRACK_SET_POLICY",
    "TrackPolicyRequest",
    "TrackRenameRequest",
    "rename_track",
    "set_track_policy",
]
