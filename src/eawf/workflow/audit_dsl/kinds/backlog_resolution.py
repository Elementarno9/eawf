"""``backlog_resolution`` close-gate kind (P30-I10 QUAL-2).

A close-gate that reads the backlog items linked to a closing wave and
refuses the close when any linked item is left dangling -- still
``open`` / ``in_progress`` with no recorded resolution and no explicit
deferral reason. The contract pins the tech-debt leak the operator
named: a wave that "fixes B0xx" but never records the resolution (or an
explicit blocked / deferred reason) silently leaves the item open, so
the backlog drifts out of sync with the shipped tree.

Wave linkage
------------

A backlog item is *linked* to a wave when its
:attr:`~eawf.kernel.state.models.BacklogItem.scope_id` equals the wave
id (the URN-or-bare wave id the close mutation names). The state model
carries no separate wave-link field, so ``scope_id`` IS the link: a
P30-I10-W02 dogfood item triaged at ``scope_id="P30-I10-W02"`` is the
item this gate enforces on that wave's close.

Resolution contract
-------------------

Each linked item must be *resolved-or-deferred*:

* ``closed`` -- the item must additionally carry a non-empty
  :attr:`~eawf.kernel.state.models.BacklogItem.resolution`. A
  ``closed`` row with no resolution is the "no signal" trap (the close
  recorded the status flip but not what discharged it), so it fails.
* ``deferred`` -- an explicit operator deferral is a valid non-close
  outcome; it carries its own reason in ``resolution`` (when present)
  but is accepted even without one, because a deferral is a deliberate
  "stays open, on purpose" decision rather than a silent leak.
* ``open`` / ``in_progress`` -- a dangling item. Accepted ONLY when it
  carries an explicit ``resolution`` string used as a ``blocked_reason``
  (a recorded "why it stays open"); otherwise it FAILS the gate.

The gate never mutates state and never writes a file -- it reads the
linked items off the validated state model and rolls them up to a
single pass/fail outcome with a one-line ``details`` note naming each
offending item.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from eawf.kernel.state.enums import BacklogStatus
from eawf.kernel.state.models import BacklogItem, State

logger = logging.getLogger(__name__)

#: The close-gate kind string. Registered into the close-gate registry
#: (:data:`eawf.workflow.verify.readiness.CLOSE_GATE_KINDS`) so the
#: BIND-1 wired-on sweep counts it as a production-reachable kind.
BACKLOG_RESOLUTION_KIND = "backlog_resolution"


@dataclass(frozen=True, slots=True)
class BacklogResolutionResult:
    """Typed outcome of one backlog-resolution check.

    Attributes:
        passed: ``True`` when every wave-linked backlog item is
            resolved-or-deferred.
        linked_ids: The ids of the backlog items linked to the wave, in
            id-sorted order. Empty when the wave links no items.
        dangling_ids: The ids of the linked items that fail the
            resolution contract, in id-sorted order. Empty on a pass.
        details: A one-line note suitable for the close-gate record.
    """

    passed: bool
    linked_ids: list[str]
    dangling_ids: list[str]
    details: str


def linked_backlog_items(state: State, *, wave_id: str) -> list[BacklogItem]:
    """Return the backlog items linked to *wave_id*, id-sorted.

    A backlog item is linked when its
    :attr:`~eawf.kernel.state.models.BacklogItem.scope_id` equals
    *wave_id*. The wave's own backlog dogfood items (e.g. the
    P30-I10-W02 owned rows) are triaged at the wave scope so they
    resolve here.

    Args:
        state: Validated state model. Read-only.
        wave_id: The closing wave id (URN-or-bare, exactly as the close
            mutation names it).

    Returns:
        The linked :class:`BacklogItem` rows in ascending id order.
        Empty when the state carries no backlog or no item links the
        wave.
    """
    backlog = state.backlog or {}
    linked = [item for item in backlog.values() if item.scope_id == wave_id]
    return sorted(linked, key=lambda item: item.id)


def _item_is_resolved_or_deferred(item: BacklogItem) -> bool:
    """Return whether *item* satisfies the resolved-or-deferred contract.

    Args:
        item: One wave-linked backlog item.

    Returns:
        ``True`` when the item is ``closed`` with a non-empty
        resolution, ``deferred`` (a deliberate stay-open), or
        ``open`` / ``in_progress`` carrying an explicit resolution
        string used as a blocked-reason. ``False`` for a dangling
        open / in-progress item with no recorded reason, or a
        ``closed`` item missing its resolution.
    """
    has_reason = bool(item.resolution and item.resolution.strip())
    if item.status is BacklogStatus.CLOSED:
        return has_reason
    if item.status is BacklogStatus.DEFERRED:
        return True
    # open / in_progress -- a dangling item is accepted only when it
    # carries an explicit recorded reason for staying open.
    return has_reason


def check_backlog_resolution(state: State, *, wave_id: str) -> BacklogResolutionResult:
    """Assert every backlog item linked to *wave_id* is resolved-or-deferred.

    Reads the wave-linked backlog items (:func:`linked_backlog_items`)
    and rolls them up: the gate passes when every linked item is
    ``closed`` with a resolution, ``deferred``, or carries an explicit
    stay-open reason; it fails naming each dangling item otherwise. A
    wave that links no backlog items passes vacuously (there is nothing
    to leak).

    Args:
        state: Validated state model. Read-only -- the gate never
            mutates state nor writes a file.
        wave_id: The closing wave id the close mutation names.

    Returns:
        A :class:`BacklogResolutionResult` whose ``passed`` is ``True``
        only when no linked item is dangling.
    """
    linked = linked_backlog_items(state, wave_id=wave_id)
    linked_ids = [item.id for item in linked]
    dangling = [item.id for item in linked if not _item_is_resolved_or_deferred(item)]
    if not linked:
        details = f"wave={wave_id} links no backlog items"
        logger.debug(f"check_backlog_resolution ok wave={wave_id!r} linked=0")
        return BacklogResolutionResult(
            passed=True,
            linked_ids=[],
            dangling_ids=[],
            details=details,
        )
    if dangling:
        details = (
            f"wave={wave_id} leaves {len(dangling)} backlog item(s) dangling "
            f"(unresolved, no blocked_reason): {', '.join(dangling)}"
        )
        logger.info(
            f"check_backlog_resolution fail wave={wave_id!r} "
            f"linked={len(linked_ids)} dangling={len(dangling)}"
        )
        return BacklogResolutionResult(
            passed=False,
            linked_ids=linked_ids,
            dangling_ids=dangling,
            details=details,
        )
    details = f"wave={wave_id} resolves all {len(linked_ids)} linked backlog item(s)"
    logger.debug(f"check_backlog_resolution ok wave={wave_id!r} linked={len(linked_ids)}")
    return BacklogResolutionResult(
        passed=True,
        linked_ids=linked_ids,
        dangling_ids=[],
        details=details,
    )


__all__ = [
    "BACKLOG_RESOLUTION_KIND",
    "BacklogResolutionResult",
    "check_backlog_resolution",
    "linked_backlog_items",
]
