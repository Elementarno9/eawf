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

from eawf.kernel.state.enums import (
    AuditStatus,
    AuditVerdict,
    BacklogStatus,
    DecisionStatus,
    IterStatus,
    WaveStatus,
)
from eawf.kernel.state.ids import natural_key
from eawf.surfaces.tui.snapshot.pilot_harness import settle_screen

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from textual.pilot import Pilot

    from eawf.kernel.state.models import ActualSummary
    from eawf.surfaces.tui.app import EaApp

#: The default Pilot terminal size the affordance sweep mounts at. Wide
#: enough that the full footer hint strip renders without clipping any
#: advertised token (so no advertised key is dropped from the enumeration by
#: a too-narrow frame), matching the affordance-parity check's default.
_SWEEP_SIZE: tuple[int, int] = (120, 40)

#: Mapping from a footer hint *token* (the text before the first space in a
#: ``render_hint_label`` fragment) to the Textual key string(s) a press
#: drives. Multi-glyph tokens (the arrow pairs, the three-letter scope
#: switch) map to every key they advertise; the punctuation glyphs map to
#: their Textual key names. A token absent from this map is a single literal
#: key (``a`` / ``c`` / ``H`` / ``space`` press as themselves). Kept aligned
#: with the affordance-parity check's identical map so the sweep and the
#: per-mode parity check enumerate the same key set from one footer strip.
_TOKEN_KEYS: dict[str, tuple[str, ...]] = {
    "↑↓": ("up", "down"),
    "←→": ("left", "right"),
    "Enter": ("enter",),
    "Esc": ("escape",),
    "F5": ("f5",),
    "w/r/u": ("w", "r", "u"),
    "/": ("slash",),
    "?": ("question_mark",),
}

#: The advertised keys the sweep tolerates as still-unresolved -- the
#: documented allowlist of known-pending affordances that cannot be made to
#: resolve within the sweep's own scope (the probe module). The sweep fails
#: on ANY unresolved advertised key NOT in this set, so a real regression
#: (a footer that newly advertises a dead key) is caught; a deferral is an
#: explicit, reviewable entry here, never a silent widening.
#:
#: Each entry is a ``"<scope>/<mode>/<key>"`` triple so a deferral is pinned
#: to the exact cell it covers (a key dead in one scope but live in another
#: is deferred only where it is genuinely dead).
#:
#: Empty: every mode/scope cell currently advertises only resolving keys --
#: the recent per-mode parity waves (home/evidence/trust/research_board and
#: the autopilot/doctor/feed/agent_watch panes) closed the dead-``c`` family,
#: so there is no known-pending affordance to defer. A future cell that
#: cannot resolve a newly-advertised key within probe scope adds its triple
#: here with a one-line rationale rather than widening the allowlist
#: wholesale.
DEFERRED_KEYS: frozenset[str] = frozenset()

#: Sentinel used by state-backed terminal fields when no matching fact exists.
#: Keeping this as a string lets ``tui_flow`` specs compare terminal states
#: without a special ``None`` convention.
NO_TERMINAL_FACT: str = "none"


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
        close_gate_pass_count: Closed waves with a passing audit verdict and
            positive measured ``elapsed_eu``.
        elapsed_eu_total: Total measured EU over closed waves.
        planned_iter_count: Number of PLANNED iters in the bound state.
        planned_iter_dag: Stable dependency-edge signature for PLANNED iters.
        authority_transition: Active decision ids scoped to the current phase.
        followup_ids: Live backlog ids scoped to the current phase or iter.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_mode: str
    nav_scope: str
    nav_mode: str
    screen_depth: int
    top_screen: str
    modal_depth: int
    toast_count: int
    close_gate_pass_count: int
    elapsed_eu_total: float
    planned_iter_count: int
    planned_iter_dag: str
    authority_transition: str
    followup_ids: str


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


def _actual_for_wave(app: EaApp, wave_id: str) -> ActualSummary | None:
    """Return the actual row that records runtime for *wave_id*, if present."""
    state = app.state
    if state is None:
        return None
    actuals = state.actuals or {}
    direct = actuals.get(wave_id)
    if direct is not None:
        return direct
    for actual in actuals.values():
        if actual.scope_id == wave_id:
            return actual
    return None


def _closed_wave_ids(app: EaApp) -> tuple[str, ...]:
    """Return closed wave ids in stable order."""
    state = app.state
    if state is None:
        return ()
    return tuple(
        sorted(
            (wave.id for wave in state.waves.values() if wave.status is WaveStatus.CLOSED),
            key=natural_key,
        )
    )


def _elapsed_eu_total(app: EaApp) -> float:
    """Return total measured EU for closed waves in the bound state."""
    total = 0.0
    for wave_id in _closed_wave_ids(app):
        actual = _actual_for_wave(app, wave_id)
        if actual is not None:
            total += actual.elapsed_eu
    return round(total, 6)


def _close_gate_pass_count(app: EaApp) -> int:
    """Count closed waves with a pass audit verdict and positive elapsed EU."""
    state = app.state
    if state is None:
        return 0
    audits = state.audits or {}
    count = 0
    for wave_id in _closed_wave_ids(app):
        actual = _actual_for_wave(app, wave_id)
        if actual is None or actual.elapsed_eu <= 0.0:
            continue
        if any(
            audit.scope_id == wave_id
            and audit.status is AuditStatus.COMPLETE
            and audit.verdict is AuditVerdict.PASS
            for audit in audits.values()
        ):
            count += 1
    return count


def _planned_iter_count(app: EaApp) -> int:
    """Return how many PLANNED iters are present in the bound state."""
    state = app.state
    if state is None:
        return 0
    return sum(1 for iteration in state.iters.values() if iteration.status is IterStatus.PLANNED)


def _planned_iter_dag(app: EaApp) -> str:
    """Return a stable dependency-edge signature for every PLANNED iter."""
    state = app.state
    if state is None:
        return NO_TERMINAL_FACT
    chunks: list[str] = []
    for iter_id in sorted(state.iters, key=natural_key):
        iteration = state.iters[iter_id]
        if iteration.status is not IterStatus.PLANNED:
            continue
        wave_ids = [wave_id for wave_id in iteration.wave_ids if wave_id in state.waves]
        wave_set = set(wave_ids)
        edges: list[str] = []
        for wave_id in wave_ids:
            wave = state.waves[wave_id]
            deps = [dep for dep in wave.deps if dep in wave_set]
            if not deps:
                edges.append(f"{wave_id}:root")
                continue
            edges.extend(f"{dep}->{wave_id}" for dep in sorted(deps, key=natural_key))
        chunks.append(f"{iter_id}:{','.join(edges)}")
    return "|".join(chunks) if chunks else NO_TERMINAL_FACT


def _authority_transition(app: EaApp) -> str:
    """Return active decision ids scoped to the current phase."""
    state = app.state
    if state is None or state.current.phase_id is None:
        return NO_TERMINAL_FACT
    decision_ids = [
        decision.id
        for decision in state.decisions.values()
        if decision.scope_id == state.current.phase_id and decision.status is DecisionStatus.ACTIVE
    ]
    return "|".join(sorted(decision_ids, key=natural_key)) if decision_ids else NO_TERMINAL_FACT


def _followup_ids(app: EaApp) -> str:
    """Return live backlog ids scoped to the current phase or iter."""
    state = app.state
    if state is None:
        return NO_TERMINAL_FACT
    backlog = state.backlog or {}
    followups: list[str] = []
    scopes = {
        scope_id
        for scope_id in (state.current.phase_id, state.current.iter_id)
        if scope_id is not None
    }
    live_statuses = {BacklogStatus.OPEN, BacklogStatus.IN_PROGRESS, BacklogStatus.DEFERRED}
    for item in backlog.values():
        if item.scope_id in scopes and item.status in live_statuses:
            followups.append(item.id)
    return "|".join(sorted(followups, key=natural_key)) if followups else NO_TERMINAL_FACT


def _sample_observable_state(app: EaApp) -> _ObservableState:
    """Capture the app's observable signals into an immutable snapshot.

    Reads already-exposed signals -- the active mode, the bound nav
    position, the screen stack (depth + top class), the modal-overlay
    depth, the mounted-toast count, and typed lifecycle facts from the
    app-bound state -- so a flow can assert a real terminal outcome rather
    than only a screen switch.

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
        close_gate_pass_count=_close_gate_pass_count(app),
        elapsed_eu_total=_elapsed_eu_total(app),
        planned_iter_count=_planned_iter_count(app),
        planned_iter_dag=_planned_iter_dag(app),
        authority_transition=_authority_transition(app),
        followup_ids=_followup_ids(app),
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
        "close_gate_pass_count",
        "elapsed_eu_total",
        "planned_iter_count",
        "planned_iter_dag",
        "authority_transition",
        "followup_ids",
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


#: The stable field order of the observable signals
#: :func:`_sample_observable_state` samples. Exported so a caller (the
#: ``tui_flow`` audit kind) can validate a declared terminal-state spec
#: against the exact field set the probe knows, rather than duplicating the
#: list and drifting from it. The first seven fields are app chrome; the
#: trailing fields are state-backed terminal facts for scenario-gate flows
#: that must prove lifecycle outcomes.
OBSERVABLE_FIELDS: tuple[str, ...] = (
    "current_mode",
    "nav_scope",
    "nav_mode",
    "screen_depth",
    "top_screen",
    "modal_depth",
    "toast_count",
    "close_gate_pass_count",
    "elapsed_eu_total",
    "planned_iter_count",
    "planned_iter_dag",
    "authority_transition",
    "followup_ids",
)


async def record_flow_terminal_state(
    pilot: Pilot[object],
    keys: Sequence[str],
) -> dict[str, object]:
    """Drive *keys* as a flow and return the terminal observable state.

    The journey complement to :func:`record_keypress_transcript`: where the
    transcript driver records the per-key OUTCOME (the dead-click signal), a
    flow gate cares only about the TERMINAL observable state the whole key
    sequence lands in. This driver presses each key through the real
    key->:class:`~textual.binding.Binding` path
    (:meth:`~textual.pilot.Pilot.press`), draining the background workers
    after each press (via :func:`settle_screen`) so a binding that offloads to
    a worker is observed after it settles -- keeping the terminal state
    deterministic across runs. A key that resolves to no binding is still
    pressed (Textual no-ops it) so a flow spec that includes a benign
    unresolved key does not abort; the terminal-state comparison is what the
    gate asserts.

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from ``app.run_test()``,
            already settled.
        keys: Ordered Textual key strings to drive as the flow (e.g.
            ``["2", "1"]`` for "open autopilot then return home").

    Returns:
        The terminal observable state as a plain ``dict`` keyed by
        :data:`OBSERVABLE_FIELDS`, suitable for an equality comparison against
        a declared ``terminal_state`` spec.
    """
    app = cast("EaApp", pilot.app)
    await settle_screen(pilot)
    for key in keys:
        await pilot.press(key)
        await settle_screen(pilot)
    state = _sample_observable_state(app)
    return {field: getattr(state, field) for field in OBSERVABLE_FIELDS}


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


def _token_keys(token: str) -> tuple[str, ...]:
    """Resolve a footer hint *token* to the Textual key string(s) it advertises.

    Args:
        token: The leading token of a footer hint fragment (e.g. ``"c"`` /
            ``"↑↓"`` / ``"Enter"``).

    Returns:
        The key string(s) a press of the advertised affordance drives -- the
        mapped tuple for a multi-glyph / named token, or the single literal
        token otherwise.
    """
    return _TOKEN_KEYS.get(token, (token,))


async def _advertised_keys_at(
    *,
    scope: str,
    mode: str,
    state_path: Path | None,
    size: tuple[int, int],
) -> list[str]:
    """Mount the TUI at *scope* + *mode* and return its advertised footer keys.

    Drives the live scope axis through the app's bound nav state machine
    (``action_switch_scope``) so the sweep enumerates a mode's footer exactly
    as the operator reaches it at that scope, then maps each advertised token
    to its Textual key string(s) (:func:`_token_keys`), de-duplicated in
    advertised order so a key advertised twice is probed once.

    Args:
        scope: The nav scope to switch to before enumerating (``repo`` /
            ``workspace`` / ``user``).
        mode: The mode name to switch to before enumerating the footer.
        state_path: The fixture ``state.json`` to bind, or ``None``.
        size: The Pilot terminal size.

    Returns:
        The advertised key strings, in first-advertised order.
    """
    from eawf.surfaces.tui.app import EaApp
    from eawf.surfaces.tui.widgets.footer import Footer

    app = EaApp(scope="repo", state_path=state_path)
    async with app.run_test(size=size) as raw_pilot:
        pilot = cast("Pilot[object]", raw_pilot)
        await settle_screen(pilot)
        app.action_switch_scope(scope)
        await settle_screen(pilot)
        await app.switch_mode(mode)
        await settle_screen(pilot)
        footers = app.screen.query(Footer)
        keys: list[str] = []
        if footers:
            footer = footers.first(Footer)
            for hint in footer.hints:
                token = hint.split(" ", 1)[0]
                keys.extend(_token_keys(token))
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


async def _unresolved_keys_at(
    *,
    scope: str,
    mode: str,
    keys: Sequence[str],
    state_path: Path | None,
    size: tuple[int, int],
) -> list[str]:
    """Mount once at *scope* + *mode* and return the *keys* that resolve to no binding.

    The sweep branches only on whether an advertised key is UNRESOLVED --
    absent from the active screen's :attr:`~textual.screen.Screen.active_bindings`
    map -- which is a pure read of the resolved binding table taken with NO
    key press. Reading the map once per cell (rather than pressing each key
    against its own fresh mount) is both faster and safe: a destructive
    advertised key (``q`` quit, ``w``/``r``/``u`` scope switch) would corrupt
    the sweep if pressed, and the map already answers the only question the
    sweep asks.

    Args:
        scope: The nav scope to switch to before reading the binding map.
        mode: The mode name to switch to before reading the binding map.
        keys: The advertised Textual key strings to classify.
        state_path: The fixture ``state.json`` to bind, or ``None``.
        size: The Pilot terminal size.

    Returns:
        The subset of *keys* absent from the active screen's binding map, in
        the given order -- the dead affordances (:data:`ProbeStatus.UNRESOLVED`).
    """
    from eawf.surfaces.tui.app import EaApp

    app = EaApp(scope="repo", state_path=state_path)
    async with app.run_test(size=size) as raw_pilot:
        pilot = cast("Pilot[object]", raw_pilot)
        await settle_screen(pilot)
        app.action_switch_scope(scope)
        await settle_screen(pilot)
        await app.switch_mode(mode)
        await settle_screen(pilot)
        return [key for key in keys if not _key_resolves(app, key)]


async def sweep_unresolved_affordances(
    *,
    state_path: Path | None = None,
    size: tuple[int, int] = _SWEEP_SIZE,
) -> tuple[str, ...]:
    """Sweep every legal ``(scope, mode)`` cell + return the unresolved keys.

    The W12 affordance-matrix sweep: iterates :data:`NAV_SCOPES` crossed with
    :data:`MODE_REGISTRY`, restricted to the legal cells the nav state machine
    permits (:func:`~eawf.surfaces.tui.modes.nav.legal_scopes_for_mode`), so
    an illegal corner (the portfolio ``user`` scope crossed with a single-scope
    data mode) is never probed -- it has no honest footer to advertise. For
    each legal cell it enumerates the mode's advertised footer keys
    (:func:`_advertised_keys_at`) and classifies them against the resolved
    binding map (:func:`_unresolved_keys_at`), collecting every advertised key
    absent from that map (:data:`ProbeStatus.UNRESOLVED`) -- the dead
    affordance the footer promises but no
    :class:`~textual.binding.Binding` answers.

    The sweep presses NOTHING: whether a key is UNRESOLVED is a pure
    ``active_bindings`` read, and pressing would be both wasteful (a remount
    per key) and unsafe (``q`` quits, ``w``/``r``/``u`` switch scope, either
    corrupting the sweep). Each legal cell mounts exactly twice -- once to
    enumerate the footer keys, once to read the binding map -- so the sweep's
    mount budget is bounded at twice the legal-cell count rather than growing
    with the advertised-key count.

    The returned set is the unresolved keys MINUS the documented
    :data:`DEFERRED_KEYS` allowlist, so a caller asserts the result is empty:
    a known-pending deferral is excluded (its triple is in the allowlist) but
    a newly-dead key (a real regression) surfaces. Each entry is a
    ``"<scope>/<mode>/<key>"`` triple naming the exact offending cell.

    A :data:`ProbeStatus.NO_OP` (a key that resolves to a binding but moves
    none of the coarse observable signals -- an intra-pane cursor move, an F5
    refresh, a scope re-select that lands the same scope) is NOT unresolved: a
    resolving binding IS a present affordance, so the sweep keys on the
    load-bearing dead-affordance shape (no binding at all), matching the
    per-mode affordance-parity check's semantics.

    Args:
        state_path: The fixture ``state.json`` to bind for every cell, or
            ``None`` (the user-scope launch with no bound state).
        size: The Pilot terminal size to mount every cell at.

    Returns:
        A sorted tuple of ``"<scope>/<mode>/<key>"`` triples for every
        advertised key that resolved to no binding and is not in
        :data:`DEFERRED_KEYS`. Empty when every advertised key in every legal
        cell resolves (the green sweep).
    """
    from eawf.surfaces.tui.modes.nav import legal_scopes_for_mode
    from eawf.surfaces.tui.modes.registry import MODE_REGISTRY

    unresolved: list[str] = []
    for spec in MODE_REGISTRY:
        for scope in legal_scopes_for_mode(spec.name):
            keys = await _advertised_keys_at(
                scope=scope, mode=spec.name, state_path=state_path, size=size
            )
            if not keys:
                continue
            dead_keys = await _unresolved_keys_at(
                scope=scope,
                mode=spec.name,
                keys=keys,
                state_path=state_path,
                size=size,
            )
            for key in dead_keys:
                triple = f"{scope}/{spec.name}/{key}"
                if triple not in DEFERRED_KEYS:
                    unresolved.append(triple)
    return tuple(sorted(unresolved))


__all__ = [
    "DEFERRED_KEYS",
    "OBSERVABLE_FIELDS",
    "BehaviourTranscript",
    "ProbeOutcome",
    "ProbeStatus",
    "record_behaviour_transcript",
    "record_flow_terminal_state",
    "record_keypress_transcript",
    "render_transcript_evidence",
    "sweep_unresolved_affordances",
]
