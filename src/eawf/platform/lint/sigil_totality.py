"""Structural sigil-totality gate over every TUI-render status enum.

The reskin's two-axis visual vocabulary promises that NO pane ever prints a
bare ``.value`` word or a ``?`` fallthrough for a status: every status enum
value resolves to a real, ratified glyph through the single resolver
:func:`eawf.surfaces.tui.widgets.sigils.status_sigil`. That promise is only a
promise unless something proves it holds for EVERY value of EVERY status enum
the reskin renders -- a hand-maintained map silently rots the moment a new enum
member lands without a row.

This module is that proof. :func:`check_sigil_totality` is parametrized over
``list(EnumCls)`` for every TUI-render status enum (WaveStatus, IterStatus,
PhaseStatus, AgentReportVerdict, AgentSessionStatus, AuditVerdict,
OutcomeStatus, BacklogStatus, ClaimStatus, OpenQuestionStatus) PLUS the
lifecycle FSM terminals -- the keys of ``WAVE_TRANSITIONS`` /
``PHASE_TRANSITIONS`` / ``ITER_TRANSITIONS`` in
:mod:`eawf.workflow.lifecycle.spec`, so a status that exists only as an FSM
state (never as a rendered row yet) is still covered. For each member it asserts
the resolver returns a glyph that is:

- present -- :func:`~eawf.surfaces.tui.widgets.sigils.status_sigil` resolves it
  rather than raising (no enum drifted past the map);
- a REAL mark -- a non-empty glyph string that is NOT the literal ``?``
  fallthrough and NOT the bare ``.value`` word (a row that printed
  ``"pending"`` instead of the ring would fail here).

The gate MUST FIRE -- it is not an idle contract. The deterministic
negative-control (a resolver stub that returns a bare ``.value`` for one
deliberately-unmapped value) is proven to make the check FAIL, so a future
regression that drops a row -- or makes the resolver rubber-stamp a ``.value``
word -- is caught in CI rather than shipping a word where a glyph belongs.

The check is pure: it builds no widget, spawns no model, mutates no state,
writes no file. :func:`check_sigil_totality` returns a typed :class:`GateResult`.
The production call-site is the ``eawf hook sigil-totality`` command, which maps
the result onto an exit code; the thin ``tools/sigil_totality_gate.py`` CLI
delegates here too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionStatus,
    AuditVerdict,
    BacklogStatus,
    ClaimStatus,
    IterStatus,
    OpenQuestionStatus,
    OutcomeStatus,
    PhaseStatus,
    WaveStatus,
)
from eawf.surfaces.tui.widgets.sigils import ResolvedSigil, status_sigil
from eawf.workflow.lifecycle.spec import (
    ITER_TRANSITIONS,
    PHASE_TRANSITIONS,
    WAVE_TRANSITIONS,
)

#: A resolver with the shape of
#: :func:`eawf.surfaces.tui.widgets.sigils.status_sigil`. Injected so the
#: negative-control test can pass a stub that returns a bare ``.value`` word for
#: one value and confirm this gate catches a resolver that prints a word where a
#: glyph belongs.
type ResolveFn = Callable[[object], ResolvedSigil]

#: The literal fallthrough mark a non-total resolver would emit. The gate
#: rejects it explicitly so a ``?`` placeholder never passes as a "glyph".
_FALLTHROUGH: str = "?"

#: Every TUI-render status enum the reskin draws rows for. The gate is
#: parametrized over ``list(EnumCls)`` for each, so adding a member to any of
#: these without a resolver row fails the gate.
_RENDER_ENUMS: tuple[type[StrEnum], ...] = (
    WaveStatus,
    IterStatus,
    PhaseStatus,
    AgentReportVerdict,
    AgentSessionStatus,
    AuditVerdict,
    OutcomeStatus,
    BacklogStatus,
    ClaimStatus,
    OpenQuestionStatus,
)


@dataclass(frozen=True, slots=True)
class GateResult:
    """Typed outcome of one sigil-totality check.

    Attributes:
        passed: Whether every covered status value resolved to a real glyph.
        misses: One human-readable line per value that did NOT resolve to a
            real glyph (an unmapped member, a ``?`` fallthrough, or a bare
            ``.value`` word). Empty on a pass.
        checked: The total count of status values the gate evaluated.
    """

    passed: bool
    misses: tuple[str, ...] = field(default_factory=tuple)
    checked: int = 0

    @property
    def message(self) -> str:
        """Return a one-line human-readable summary of the check."""
        if self.passed:
            return f"sigil-totality OK: {self.checked} status value(s) resolve to a real glyph"
        miss_count = len(self.misses)
        return f"sigil-totality FAILED: {miss_count} of {self.checked} value(s) did not resolve"


def covered_members() -> tuple[StrEnum, ...]:
    """Return every status member the gate must prove resolves, de-duplicated.

    The union of ``list(EnumCls)`` for every enum in :data:`_RENDER_ENUMS`
    PLUS the lifecycle FSM terminal keys (the keys of ``WAVE_TRANSITIONS`` /
    ``PHASE_TRANSITIONS`` / ``ITER_TRANSITIONS``). The FSM keys are members of
    the same status enums, so they are already in the render-enum sweep; they
    are unioned in explicitly so a status that exists ONLY as an FSM state
    (never as a rendered enum the gate listed) is still covered, and the
    coverage stays correct if the FSM tables ever name a status the render
    list misses.

    Returns:
        Every distinct status member to check, ordered by ``(enum-name,
        member-name)`` so the failure report is deterministic.
    """
    members: dict[tuple[str, str], StrEnum] = {}
    for enum_cls in _RENDER_ENUMS:
        for member in enum_cls:
            members[(enum_cls.__name__, member.name)] = member
    fsm_keys: Iterable[StrEnum] = (
        *WAVE_TRANSITIONS.keys(),
        *PHASE_TRANSITIONS.keys(),
        *ITER_TRANSITIONS.keys(),
    )
    for member in fsm_keys:
        members[(type(member).__name__, member.name)] = member
    return tuple(value for _key, value in sorted(members.items()))


def _resolves_to_real_glyph(member: StrEnum, *, resolve_fn: ResolveFn) -> str | None:
    """Return a miss reason for *member*, or ``None`` when it resolves cleanly.

    A value resolves cleanly when the resolver returns a :class:`ResolvedSigil`
    whose unicode AND ascii glyph columns are each a non-empty mark that is
    neither the ``?`` fallthrough nor the bare enum ``.value`` word.

    Args:
        member: The status enum member to resolve.
        resolve_fn: The resolver under test (the real
            :func:`~eawf.surfaces.tui.widgets.sigils.status_sigil` or an
            injected stub).

    Returns:
        ``None`` when *member* resolves to a real glyph in both columns, else a
        one-line reason naming the failure mode.
    """
    label = f"{type(member).__name__}.{member.name}"
    try:
        resolved = resolve_fn(member)
    except KeyError:
        return f"{label}: unmapped -- resolver raised KeyError (no row)"
    value_word = member.value
    for column_name, mark in (("unicode", resolved.glyph_unicode), ("ascii", resolved.glyph_ascii)):
        if not mark:
            return f"{label}: empty {column_name} glyph"
        if mark == _FALLTHROUGH:
            return f"{label}: {column_name} glyph is the {_FALLTHROUGH!r} fallthrough"
        if mark == value_word:
            return f"{label}: {column_name} glyph is the bare .value word {value_word!r}"
    return None


def check_sigil_totality(*, resolve_fn: ResolveFn = status_sigil) -> GateResult:
    """Assert every covered status value resolves to a real glyph.

    Sweeps :func:`covered_members` and resolves each through *resolve_fn*,
    collecting a miss line for any value that does not resolve to a real glyph.
    The resolver is injectable so the negative-control test can pass a stub that
    rubber-stamps a bare ``.value`` for one value and confirm this gate FIRES.

    Args:
        resolve_fn: The status->glyph resolver under test. Defaults to the real
            :func:`~eawf.surfaces.tui.widgets.sigils.status_sigil`.

    Returns:
        A :class:`GateResult`; ``passed`` is ``True`` only when every covered
        value resolved to a real glyph.
    """
    members = covered_members()
    misses = tuple(
        miss
        for member in members
        if (miss := _resolves_to_real_glyph(member, resolve_fn=resolve_fn)) is not None
    )
    return GateResult(passed=not misses, misses=misses, checked=len(members))
