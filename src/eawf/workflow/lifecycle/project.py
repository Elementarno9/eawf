"""Pure-functional project/track lifecycle transitions.

Every helper mutates the supplied :class:`State` in place. See
:mod:`eawf.workflow.lifecycle.transitions` for the shared design rules and the
re-export surface that keeps ``eawf.workflow.lifecycle.transitions`` import paths
working after the per-entity split.
"""

from __future__ import annotations

import logging

from eawf.kernel.state.enums import TrackKind, TrackStatus
from eawf.kernel.state.models import State, Track
from eawf.workflow.lifecycle._errors import LifecycleError

logger = logging.getLogger(__name__)


def add_track(
    state: State,
    *,
    code: str,
    kind: TrackKind,
    title: str,
    domains: list[str] | None = None,
) -> Track:
    """Add a new track.

    Raises:
        LifecycleError: State has no project or ``code`` already exists.
    """
    if state.project is None:
        raise LifecycleError("cannot add track: state has no project")
    if state.tracks is None:
        state.tracks = {}
    if code in state.tracks:
        raise LifecycleError(f"track {code!r} already exists")
    track = Track(
        id=code,
        code=code,
        slug=code.lower(),
        title=title,
        kind=kind,
        domains=list(domains or []),
        status=TrackStatus.ACTIVE,
        owner=None,
        goal_ids=[],
    )
    state.tracks[code] = track
    logger.info(f"add_track code={code} title={title!r}")
    return track


def switch_track(state: State, *, code: str) -> None:
    """Set ``current.track_id`` to *code*.

    Raises:
        LifecycleError: ``code`` is unknown.
    """
    if state.tracks is None or code not in state.tracks:
        raise LifecycleError(f"unknown track {code!r}")
    state.current.track_id = code
    logger.info(f"switch_track code={code}")
