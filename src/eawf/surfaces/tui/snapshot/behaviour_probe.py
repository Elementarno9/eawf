"""Provenance-pinned UI behaviour-probe transcript evidence channel.

Snapshot tests pin the *rendered* frame, but a frame is markup: a
``[@click=app.do_thing()]`` link that resolves to a no-op renders
byte-identically to one that fires real behaviour, so a **dead-click**
(an action that resolves but does nothing observable) is invisible to a
golden diff. This module captures the complementary signal -- what the
running code actually *did* -- by driving each probed action through
Textual's own :meth:`~textual.app.App.run_action` dispatcher in-process
and recording the observable OUTCOME of each.

The outcome is derived by sampling a small set of observable app signals
the harness already exposes -- the active mode, the bound nav position,
the screen-stack depth + top-screen class, the modal-overlay depth, and
the mounted-toast count -- before and after the action, then classifying
the delta:

* :data:`ProbeStatus.OBSERVABLE` -- ``run_action`` reported the action
  resolved AND at least one observable signal changed (a real action).
* :data:`ProbeStatus.NO_OP` -- ``run_action`` reported the action
  resolved but NO observable signal changed (the dead-click: the click
  fired a handler that did nothing the operator can see).
* :data:`ProbeStatus.UNRESOLVED` -- ``run_action`` reported the action
  did not resolve at all (a stale / mis-namespaced action string -- the
  pre-fix dead-click, where the click never even reached a handler).

The three statuses render distinctly in
:func:`render_transcript_evidence`, so a spec-jury reading the evidence
block can tell a live action from a dead one without re-running the app.

Two driver shapes share the classification
------------------------------------------

:func:`record_behaviour_transcript` drives each probe as an *action
string* through :meth:`~textual.app.App.run_action` -- the same parser
the ``[@click=...]`` markup routes through. That path proves the handler
behind an action does something, but it ROUTES AROUND the
key->:class:`~textual.binding.Binding` layer: an advertised key that no
longer resolves to any action still has a working handler, so the action
probe never sees the broken affordance.

:func:`record_keypress_transcript` closes that gap by driving each probe
as a *real key press* (:meth:`~textual.pilot.Pilot.press`), exercising
the genuine key->Binding resolution. A key whose press resolves to no
:class:`~textual.binding.Binding` in the active screen's binding map
classifies :data:`ProbeStatus.UNRESOLVED` (the advertised-but-dead key);
a key that resolves but moves no observable signal classifies
:data:`ProbeStatus.NO_OP`; a key that resolves and moves a signal
classifies :data:`ProbeStatus.OBSERVABLE`. The two drivers reuse the same
:class:`ProbeOutcome` / :class:`BehaviourTranscript` shapes, so a
key-press transcript renders through the same evidence formatter; the
:attr:`ProbeOutcome.action` field holds the key string for a key probe.

Provenance: the transcript is stamped with the source commit it was
recorded against (mirroring the asciinema cast provenance stamp), so the
evidence the jury grounds in is pinned to a specific build of the code --
a transcript divorced from its commit is not admissible.

Determinism: every probe drains the background workers
(:func:`~eawf.surfaces.tui.snapshot.pilot_harness.settle_screen`, which
awaits ``app.workers.wait_for_complete()``) before sampling, so a probe
that offloads work to a worker is observed after the worker settles --
re-running the same probe list yields the same transcript.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ConfigDict

from eawf.surfaces.tui.snapshot.pilot_harness import settle_screen

if TYPE_CHECKING:
    from collections.abc import Sequence

    from textual.pilot import Pilot

    from eawf.surfaces.tui.app import EaApp


class ProbeStatus(StrEnum):
    """Outcome class of one probed action.

    The three classes split a resolved-and-observable action from the two
    dead-click shapes: a resolved-but-inert handler (``no_op``) and an
    action string that never resolved to a handler at all
    (``unresolved``).
    """

    OBSERVABLE = "observable"
    NO_OP = "no_op"
    UNRESOLVED = "unresolved"


class _ObservableState(BaseModel):
    """A snapshot of the app's observable signals at one instant.

    Sampled before and after each probed action; two snapshots that
    compare equal mean the action changed nothing the operator could see.
    Every field is a deterministic function of the live app, so a probe's
    classification is stable across runs once the workers are drained.

    Attributes:
        current_mode: The active content mode name.
        nav_scope: The bound nav position's scope.
        nav_mode: The bound nav position's mode.
        screen_depth: The number of screens on the stack.
        top_screen: The class name of the top-of-stack screen.
        modal_depth: The number of modal overlays on the stack.
        toast_count: The number of mounted toast notifications.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_mode: str
    nav_scope: str
    nav_mode: str
    screen_depth: int
    top_screen: str
    modal_depth: int
    toast_count: int


class ProbeOutcome(BaseModel):
    """The recorded outcome of one probed action.

    Attributes:
        action: The action string the probe drove (e.g.
            ``"app.switch_mode('doctor')"``).
        status: The classified :class:`ProbeStatus` for the probe.
        signals: The observable signals that changed, in a stable order
            (empty for a ``no_op`` / ``unresolved`` probe). Each entry is
            a ``"<signal>: <before> -> <after>"`` string so the rendered
            evidence shows *what* moved, not merely *that* something did.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str
    status: ProbeStatus
    signals: tuple[str, ...] = ()


class BehaviourTranscript(BaseModel):
    """A provenance-pinned record of a behaviour-probe run.

    Attributes:
        source_commit: The commit SHA the transcript was recorded
            against -- the provenance pin that makes the evidence
            admissible (an outcome divorced from its build is not).
        outcomes: One :class:`ProbeOutcome` per probed action, in probe
            order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_commit: str
    outcomes: tuple[ProbeOutcome, ...]


def _sample_observable_state(app: EaApp) -> _ObservableState:
    """Capture the app's observable signals into an immutable snapshot.

    Reads only already-exposed signals -- the active mode, the bound nav
    position, the screen stack (depth + top class), the modal-overlay
    depth, and the mounted-toast count -- so the sample touches no
    private rendering internals beyond what other harness code already
    reads.

    Args:
        app: The live :class:`~eawf.surfaces.tui.app.EaApp` under a Pilot
            harness, already settled.

    Returns:
        The observable-state snapshot at this instant.
    """
    position = app._nav.position
    return _ObservableState(
        current_mode=app.current_mode,
        nav_scope=position.scope,
        nav_mode=position.mode,
        screen_depth=len(app.screen_stack),
        top_screen=type(app.screen).__name__,
        modal_depth=app.modal_depth(),
        toast_count=len(app._notifications),
    )


def _diff_signals(before: _ObservableState, after: _ObservableState) -> tuple[str, ...]:
    """Return the observable signals that changed between two snapshots.

    Args:
        before: The snapshot taken before the action.
        after: The snapshot taken after the action.

    Returns:
        A stable-ordered tuple of ``"<signal>: <before> -> <after>"``
        strings, one per field whose value moved. Empty when nothing
        observable changed (the no-op signature).
    """
    fields = (
        "current_mode",
        "nav_scope",
        "nav_mode",
        "screen_depth",
        "top_screen",
        "modal_depth",
        "toast_count",
    )
    changed: list[str] = []
    for name in fields:
        old = getattr(before, name)
        new = getattr(after, name)
        if old != new:
            changed.append(f"{name}: {old!r} -> {new!r}")
    return tuple(changed)


async def record_behaviour_transcript(
    pilot: Pilot[object],
    probes: Sequence[str],
    *,
    source_commit: str,
) -> BehaviourTranscript:
    """Drive each probed action in-process and record its observable outcome.

    Call inside an ``async with app.run_test() as pilot:`` block. Each
    probe is one action string passed to Textual's
    :meth:`~textual.app.App.run_action` dispatcher (the same parser the
    ``[@click=...]`` markup routes through), so a probe exercises the real
    resolution path a click would. Around each probe the harness samples
    the observable app state (:func:`_sample_observable_state`), draining
    the background workers first (via
    :func:`~eawf.surfaces.tui.snapshot.pilot_harness.settle_screen`) so a
    probe that offloads work to a worker is observed after it settles --
    keeping the transcript deterministic across runs.

    The outcome is classified from ``run_action``'s return plus the
    observable delta:

    * resolved (``run_action`` returned a truthy value) AND a signal
      changed -> :attr:`ProbeStatus.OBSERVABLE`;
    * resolved but NO signal changed -> :attr:`ProbeStatus.NO_OP` (the
      dead-click: the handler fired but did nothing the operator sees);
    * did not resolve (``run_action`` returned a falsy value) ->
      :attr:`ProbeStatus.UNRESOLVED` (the action string never reached a
      handler).

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from
            ``app.run_test()``, already settled.
        probes: Ordered action strings to drive (e.g.
            ``["app.switch_mode('doctor')", "app.switch_mode('home')"]``).
        source_commit: The commit SHA the transcript is recorded against
            -- stamped onto the result as its provenance pin.

    Returns:
        The provenance-pinned :class:`BehaviourTranscript`, one
        :class:`ProbeOutcome` per probe in probe order.
    """
    # The behaviour-probe harness only ever drives the real operator app, so
    # narrow the generic Pilot app to read its EaApp-specific signals (the
    # bound nav position + the modal-overlay depth).
    app = cast("EaApp", pilot.app)
    await settle_screen(pilot)
    outcomes: list[ProbeOutcome] = []
    for action in probes:
        before = _sample_observable_state(app)
        resolved = await app.run_action(action)
        await settle_screen(pilot)
        after = _sample_observable_state(app)
        signals = _diff_signals(before, after)
        if not resolved:
            status = ProbeStatus.UNRESOLVED
            signals = ()
        elif signals:
            status = ProbeStatus.OBSERVABLE
        else:
            status = ProbeStatus.NO_OP
        outcomes.append(ProbeOutcome(action=action, status=status, signals=signals))
    return BehaviourTranscript(source_commit=source_commit, outcomes=tuple(outcomes))


def _key_resolves(app: EaApp, key: str) -> bool:
    """Return True when *key* resolves to a Binding in the active screen.

    Reads the active screen's :attr:`~textual.screen.Screen.active_bindings`
    map -- the merged app + screen + focus-chain binding table Textual itself
    resolves a key press against. A key present in the map resolves to a real
    :class:`~textual.binding.Binding`; a key absent from it has no binding, so
    pressing it would reach no handler (the advertised-but-dead affordance the
    key-press driver classifies :data:`ProbeStatus.UNRESOLVED`).

    Args:
        app: The live :class:`~eawf.surfaces.tui.app.EaApp` under a Pilot
            harness, already settled.
        key: The Textual key string to test (e.g. ``"c"`` / ``"enter"`` /
            ``"f5"``).

    Returns:
        ``True`` when *key* is a key in the active screen's binding map.
    """
    return key in app.screen.active_bindings


async def record_keypress_transcript(
    pilot: Pilot[object],
    keys: Sequence[str],
    *,
    source_commit: str,
) -> BehaviourTranscript:
    """Drive each key through the real key->Binding path and record its outcome.

    The key-press complement to :func:`record_behaviour_transcript`. Where the
    action-string driver routes around the binding layer (it dispatches the
    action directly), this driver presses the *actual key*
    (:meth:`~textual.pilot.Pilot.press`), exercising the genuine
    key->:class:`~textual.binding.Binding` resolution a live operator hits. For
    each key:

    * resolution is read from the active screen's binding map
      (:func:`_key_resolves`) BEFORE the press -- a key absent from the map has
      no Binding, so it classifies :data:`ProbeStatus.UNRESOLVED` and is NOT
      pressed (there is nothing to drive);
    * a resolving key is pressed with the observable state sampled
      (:func:`_sample_observable_state`) before + after, draining the
      background workers first (via :func:`settle_screen`) so a binding that
      offloads to a worker is observed after it settles -- keeping the
      transcript deterministic across runs;
    * a resolving key that moves at least one observable signal classifies
      :data:`ProbeStatus.OBSERVABLE`; one that resolves but moves nothing
      classifies :data:`ProbeStatus.NO_OP` (the resolved-but-inert key).

    The :attr:`ProbeOutcome.action` field holds the key string for a key probe,
    so the result renders through the shared
    :func:`render_transcript_evidence` formatter unchanged.

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from
            ``app.run_test()``, already settled.
        keys: Ordered Textual key strings to drive (e.g.
            ``["c", "enter", "f5"]``).
        source_commit: The commit SHA the transcript is recorded against
            -- stamped onto the result as its provenance pin.

    Returns:
        The provenance-pinned :class:`BehaviourTranscript`, one
        :class:`ProbeOutcome` per key in key order.
    """
    app = cast("EaApp", pilot.app)
    await settle_screen(pilot)
    outcomes: list[ProbeOutcome] = []
    for key in keys:
        if not _key_resolves(app, key):
            outcomes.append(ProbeOutcome(action=key, status=ProbeStatus.UNRESOLVED))
            continue
        before = _sample_observable_state(app)
        await pilot.press(key)
        await settle_screen(pilot)
        after = _sample_observable_state(app)
        signals = _diff_signals(before, after)
        status = ProbeStatus.OBSERVABLE if signals else ProbeStatus.NO_OP
        outcomes.append(ProbeOutcome(action=key, status=status, signals=signals))
    return BehaviourTranscript(source_commit=source_commit, outcomes=tuple(outcomes))


def render_transcript_evidence(transcript: BehaviourTranscript) -> str:
    """Format *transcript* as the auditor ``evidence_block`` text.

    Produces a compact Markdown block: a provenance line pinning the
    source commit, then one bullet per probe naming the action, its
    outcome status, and (for an observable probe) the signals that moved.
    A ``no_op`` and an ``unresolved`` probe render with an explicit
    dead-click marker so a jury reading the block can tell an inert click
    from a live one without re-running the app -- the whole point of the
    channel.

    Args:
        transcript: The recorded behaviour transcript to render.

    Returns:
        The Markdown evidence block (no trailing newline).
    """
    lines = [
        "### UI behaviour-probe transcript",
        "",
        f"source_commit: {transcript.source_commit}",
        "",
    ]
    for outcome in transcript.outcomes:
        if outcome.status is ProbeStatus.OBSERVABLE:
            detail = "; ".join(outcome.signals)
            lines.append(f"- `{outcome.action}` -> observable ({detail})")
        elif outcome.status is ProbeStatus.NO_OP:
            lines.append(
                f"- `{outcome.action}` -> NO-OP (resolved, no observable change -- dead click)"
            )
        else:
            lines.append(
                f"- `{outcome.action}` -> UNRESOLVED (action did not resolve -- dead click)"
            )
    return "\n".join(lines)


__all__ = [
    "BehaviourTranscript",
    "ProbeOutcome",
    "ProbeStatus",
    "record_behaviour_transcript",
    "record_keypress_transcript",
    "render_transcript_evidence",
]
