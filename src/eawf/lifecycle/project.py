"""Pure-functional project/subproject lifecycle transitions.

Every helper mutates the supplied :class:`State` in place. See
:mod:`eawf.lifecycle.transitions` for the shared design rules and the
re-export surface that keeps ``eawf.lifecycle.transitions`` import paths
working after the per-entity split.
"""

from __future__ import annotations

import logging

from eawf.lifecycle._errors import LifecycleError
from eawf.state.enums import SubprojectStatus
from eawf.state.models import State, Subproject

logger = logging.getLogger(__name__)


def add_subproject(
    state: State,
    *,
    code: str,
    kind: str,
    title: str,
    domains: list[str] | None = None,
) -> Subproject:
    """Add a new subproject. Raises if ``code`` already exists."""
    if state.project is None:
        raise LifecycleError("cannot add subproject: state has no project")
    if state.subprojects is None:
        state.subprojects = {}
    if code in state.subprojects:
        raise LifecycleError(f"subproject {code!r} already exists")
    sub = Subproject(
        id=code,
        code=code,
        slug=code.lower(),
        title=title,
        kind=kind,
        domains=list(domains or []),
        status=SubprojectStatus.ACTIVE,
        owner=None,
        goal_ids=[],
    )
    state.subprojects[code] = sub
    logger.info(f"add_subproject code={code} title={title!r}")
    return sub


def switch_subproject(state: State, *, code: str) -> None:
    """Set ``current.subproject_id`` to *code*. Raises if unknown."""
    if state.subprojects is None or code not in state.subprojects:
        raise LifecycleError(f"unknown subproject {code!r}")
    state.current.subproject_id = code
    logger.info(f"switch_subproject code={code}")
