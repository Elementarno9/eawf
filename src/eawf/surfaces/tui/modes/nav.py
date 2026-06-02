"""Bound SCOPE x MODE navigation state machine (the nav validator).

The TUI runs two orthogonal axes: a **scope** (``repo`` / ``workspace`` /
``user``, switched with ``w`` / ``r`` / ``u``) and a **mode** (``home`` /
``trust`` / ``doctor`` / ``evidence`` / ``feed`` / ``config`` /
``research_board`` / ``agent_watch``, switched with digit keys
``1``..``8``). The W16 chassis left every ``(scope, mode)`` pair reachable;
this module pins the **bounded** subset that is genuinely legal and refuses
the rest at the boundary, so a switch never lands the operator in a view that
has no honest data source.

CLI-is-dispatch analogue
------------------------
The rules live here as a pure, unit-testable validator; the app
(:class:`~eawf.surfaces.tui.app.EaApp`) is the dispatcher -- it calls
:func:`is_legal_position` / :meth:`NavState.resolve_scope` /
:meth:`NavState.resolve_mode` and acts on the verdict (swap the screen, or
toast + no-op). No navigation rule is duplicated in the app.

The legal matrix
----------------
Derived honestly from what each mode pane reads (not from a guess):

================  ====  =========  ====
mode              repo  workspace  user
================  ====  =========  ====
home              yes   yes        yes
doctor            yes   yes        yes
config            yes   yes        yes
trust             yes   yes        no
evidence          yes   yes        no
feed              yes   yes        no
research_board    yes   yes        no
agent_watch       yes   yes        no
================  ====  =========  ====

* ``home`` is the scope-bearing mode -- it *renders* the resolved scope
  screen, so it is legal at every scope by construction.
* ``doctor`` is scope-independent (it folds the same install / state /
  drift health regardless of scope -- see
  :func:`~eawf.surfaces.tui.modes.doctor.doctor_mode_factory`), and
  ``config`` opens the registry-driven config window (scope-agnostic), so
  both stay legal everywhere.
* ``trust`` / ``evidence`` / ``feed`` / ``research_board`` / ``agent_watch``
  read a **single scope's** ``state.json`` + its per-scope stores under
  ``<state_dir>/store/`` (the trust scorecard, the agent-report rollup, the
  event feed, the research-campaign / claim / open-question board, and the
  dispatched-session table the agent-watch zoom streams). The **user** scope
  is the cross-repo portfolio aggregate -- it has no single repo's
  ``state.json`` (its state is synthesized from the registry with
  ``project=None`` and no ``phases``), so those data-bound modes have no
  honest single-scope source there. They would render honest-empty, but
  honest-empty at the portfolio scope reads as "no reports / no trust /
  no campaign data / no dispatched session exist" when the truth is "this is
  not a report-bearing scope" -- a misleading view. So the portfolio scope is
  excluded for the data-bound modes; ``repo`` and ``workspace`` (which each
  anchor on one real ``state.json``) keep them.

Everywhere the matrix is unconstrained the W16 orthogonality holds: scope
and mode switch independently. The bound only bites the genuinely-invalid
corner (user x {trust, evidence, feed, research_board, agent_watch}).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)

#: The three navigation scopes, in ``w`` / ``r`` / ``u`` switch order. Kept
#: as a module constant so the validator and its tests share one source.
NAV_SCOPES: tuple[str, ...] = ("repo", "workspace", "user")

#: The modes whose pane reads a single scope's ``state.json`` + per-scope
#: stores, so they are illegal at the cross-repo ``user`` portfolio scope
#: (which has no single repo's state). ``research_board`` reads a single
#: scope's claim / open-question ledgers plus the per-scope
#: ``research_campaign`` store; ``agent_watch`` reads a single scope's
#: ``agent_sessions`` table to pick the dispatched session it streams, so the
#: portfolio aggregate has no honest single-scope source for either. Every
#: other mode is scope-agnostic and legal at every scope.
_SCOPE_BOUND_MODES: frozenset[str] = frozenset(
    {"trust", "evidence", "feed", "research_board", "agent_watch"}
)

#: The scopes the scope-bound modes are illegal at -- the cross-repo
#: portfolio aggregate, which carries no single repo's ``state.json``.
_PORTFOLIO_SCOPES: frozenset[str] = frozenset({"user"})


def is_legal_position(scope: str, mode: str) -> bool:
    """Return whether the ``(scope, mode)`` pair is a legal nav position.

    The single rule source the app consults before any scope / mode switch.
    A mode in :data:`_SCOPE_BOUND_MODES` is illegal at a portfolio scope in
    :data:`_PORTFOLIO_SCOPES` (no single-scope ``state.json`` to read);
    every other pair is legal (the W16 orthogonality).

    Args:
        scope: The target scope (``repo`` / ``workspace`` / ``user``).
        mode: The target mode name (``home`` / ``trust`` / ...).

    Returns:
        ``True`` when the pair is reachable, ``False`` when the bound
        rejects it.
    """
    return not (mode in _SCOPE_BOUND_MODES and scope in _PORTFOLIO_SCOPES)


def legal_scopes_for_mode(mode: str) -> tuple[str, ...]:
    """Return the scopes *mode* is legal at, in :data:`NAV_SCOPES` order.

    Args:
        mode: The mode name to filter scopes for.

    Returns:
        The legal scopes for *mode* (a subset of :data:`NAV_SCOPES`).
    """
    return tuple(scope for scope in NAV_SCOPES if is_legal_position(scope, mode))


@dataclass(frozen=True)
class NavPosition:
    """A bound nav position -- one legal ``(scope, mode)`` pair.

    Immutable so a transition yields a fresh position rather than mutating
    in place; the app holds the current one and replaces it on an accepted
    switch.

    Attributes:
        scope: The active scope (``repo`` / ``workspace`` / ``user``).
        mode: The active mode name (``home`` / ``trust`` / ...).
    """

    scope: str
    mode: str

    @property
    def is_legal(self) -> bool:
        """Return whether this position is itself a legal pair."""
        return is_legal_position(self.scope, self.mode)


@dataclass(frozen=True)
class NavTransition:
    """The verdict of a requested scope / mode switch.

    Attributes:
        position: The :class:`NavPosition` the app should land on -- the
            requested one when *accepted*, else the unchanged current
            position (so the app can no-op against it).
        accepted: ``True`` when the requested target is legal and the app
            should switch; ``False`` when the bound rejected it and the app
            should toast + no-op.
        reason: A short lowercase machine reason when *accepted* is
            ``False`` (e.g. ``"mode trust illegal at scope user"``), else
            the empty string.
    """

    position: NavPosition
    accepted: bool
    reason: str = ""


@dataclass(frozen=True)
class NavState:
    """The bound SCOPE x MODE navigation state machine.

    Holds the current :class:`NavPosition` and validates a requested scope
    or mode switch against the legal matrix (:func:`is_legal_position`).
    Pure + immutable: :meth:`resolve_scope` / :meth:`resolve_mode` return a
    :class:`NavTransition` carrying the next position and an accept/reject
    verdict; the caller (the app) holds the new state on an accepted switch
    and no-ops on a rejection. The state machine itself touches no Textual.

    Attributes:
        position: The current bound nav position.
    """

    position: NavPosition

    @classmethod
    def initial(cls, scope: str, mode: str) -> NavState:
        """Build the launch nav state for *scope* + *mode*.

        Raises:
            ValueError: when ``(scope, mode)`` is not a legal pair -- a
                launch position must be reachable.

        Args:
            scope: The launch scope.
            mode: The launch mode.

        Returns:
            The initial :class:`NavState` at ``(scope, mode)``.
        """
        if not is_legal_position(scope, mode):
            raise ValueError(f"illegal launch position scope={scope!r} mode={mode!r}")
        return cls(position=NavPosition(scope=scope, mode=mode))

    def resolve_scope(self, scope: str) -> NavTransition:
        """Resolve a scope switch, keeping the current mode.

        The mode axis is unchanged; only the scope moves. When the target
        ``(scope, current_mode)`` pair is illegal the transition is rejected
        with the current position preserved so the app no-ops.

        Args:
            scope: The requested target scope.

        Returns:
            A :class:`NavTransition` with the next position + verdict.
        """
        return self._resolve(scope=scope, mode=self.position.mode)

    def resolve_mode(self, mode: str) -> NavTransition:
        """Resolve a mode switch, keeping the current scope.

        The scope axis is unchanged; only the mode moves. When the target
        ``(current_scope, mode)`` pair is illegal the transition is rejected
        with the current position preserved so the app no-ops.

        Args:
            mode: The requested target mode.

        Returns:
            A :class:`NavTransition` with the next position + verdict.
        """
        return self._resolve(scope=self.position.scope, mode=mode)

    def _resolve(self, *, scope: str, mode: str) -> NavTransition:
        """Resolve a requested ``(scope, mode)`` target against the matrix.

        Args:
            scope: The requested target scope.
            mode: The requested target mode.

        Returns:
            An accepted :class:`NavTransition` to the requested position
            when it is legal, else a rejected transition that preserves the
            current position and names the reason.
        """
        if is_legal_position(scope, mode):
            return NavTransition(
                position=replace(self.position, scope=scope, mode=mode),
                accepted=True,
            )
        reason = f"mode {mode} illegal at scope {scope}"
        logger.info(f"_resolve rejected scope={scope!r} mode={mode!r} reason={reason!r}")
        return NavTransition(position=self.position, accepted=False, reason=reason)


__all__ = [
    "NAV_SCOPES",
    "NavPosition",
    "NavState",
    "NavTransition",
    "is_legal_position",
    "legal_scopes_for_mode",
]
