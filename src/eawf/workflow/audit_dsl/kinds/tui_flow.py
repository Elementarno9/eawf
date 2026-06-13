"""``tui_flow`` audit-DSL kind (P30-I00 RS-27).

Closes the operator-journey half of the FSM-per-element spine. The
existing TUI gates each prove ONE step of a journey:
:func:`~eawf.workflow.audit_dsl.kinds.transition_coverage.check_transition_coverage`
proves the *lifecycle* status FSM is exercised, the per-mode
:func:`~eawf.workflow.audit_dsl.kinds.affordance_parity.check_affordance_parity`
proves each advertised key resolves, and a golden snapshot proves a single
surface renders. None of them proves a *multi-step journey reaching its
terminal state* -- a before->after operator flow the mock enumerates (open
a pane, switch scope, open + dismiss a modal). Until this kind, "the journey
landed where it should" was claim-only.

This kind drives a named ``key_sequence`` through the real
key->:class:`~textual.binding.Binding` path (the
:func:`~eawf.surfaces.tui.snapshot.behaviour_probe.record_flow_terminal_state`
driver) and asserts the TERMINAL observable state -- app chrome plus the
state-backed lifecycle facts the probe samples -- equals the declared
``terminal_state``. On divergence it FAILS, naming each divergent observable
field with its actual-vs-expected value, so a journey that lands in the wrong
place is wave-provable, not silently green.

Args (read from ``spec.args``)
------------------------------

* ``flow`` -- the journey name (e.g. ``"recover-refused-close"``), surfaced
  in ``details`` so a failing gate names the journey. Required, non-empty
  str.
* ``key_sequence`` -- the ordered Textual key strings the journey drives
  (e.g. ``["2", "1"]``). Required, list of strs.
* ``terminal_state`` -- a mapping of one or more of the observable
  fields (:data:`~eawf.surfaces.tui.snapshot.behaviour_probe.OBSERVABLE_FIELDS`)
  to the value the journey must land each at. A PARTIAL spec is allowed --
  only the named fields are asserted, so a flow that pins the terminal mode
  without pinning the toast count is expressible. Required, non-empty dict;
  an unknown field key fails.
* ``scope`` -- the launch nav scope (``repo`` / ``workspace`` / ``user``).
  Optional; defaults to ``"repo"``.
* ``state_path`` -- repo-relative path to the ``state.json`` fixture the
  app binds (resolved against ``cwd``). Optional; defaults to ``None`` (the
  launch with no bound state).
* ``state`` -- inline ``State`` payload to bind for in-process probes.
  Optional; mutually exclusive with ``state_path``.
* ``size`` -- the ``[cols, rows]`` terminal size the Pilot harness runs at.
  Optional; defaults to ``[120, 40]``.

A malformed ``args`` (missing / mistyped ``flow`` / ``key_sequence`` /
``terminal_state``, an unknown terminal-state field, a bad ``size`` shape,
an unreadable fixture, an invalid inline state) yields ``status="fail"`` with
a ``details`` note
rather than propagating an exception, so one bad criterion cannot abort the
audit run -- the same degrade-not-raise contract
:func:`~eawf.workflow.audit_dsl.kinds.affordance_parity.check_affordance_parity`
follows.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

if TYPE_CHECKING:
    from textual.pilot import Pilot

    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


#: Default Pilot terminal size. Wide enough that the full footer hint strip
#: renders without clipping any advertised token, matching the
#: affordance-parity check's default.
_DEFAULT_SIZE: tuple[int, int] = (120, 40)


def _coerce_size(raw: Any) -> tuple[int, int]:
    """Coerce a ``size`` arg into a ``(cols, rows)`` tuple.

    Args:
        raw: The ``size`` arg value (a two-element list / tuple of ints), or
            ``None`` to take :data:`_DEFAULT_SIZE`.

    Returns:
        The ``(cols, rows)`` terminal size.

    Raises:
        ValueError: When *raw* is set but is not a two-element sequence of
            ints.
    """
    if raw is None:
        return _DEFAULT_SIZE
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError(f"size must be a two-element [cols, rows] list: {raw!r}")
    cols, rows = raw
    if not (isinstance(cols, int) and isinstance(rows, int)):
        raise ValueError(f"size entries must be ints: {raw!r}")
    return (cols, rows)


def _diverging_fields(
    *,
    actual: dict[str, object],
    expected: dict[str, object],
) -> list[str]:
    """Return the terminal-state fields whose actual value differs from expected.

    Compares only the fields the *expected* spec names (a partial spec is
    allowed), so a flow that pins the terminal mode without pinning the toast
    count asserts only what it declared.

    Args:
        actual: The terminal observable state the flow landed in, keyed by
            :data:`~eawf.surfaces.tui.snapshot.behaviour_probe.OBSERVABLE_FIELDS`.
        expected: The declared ``terminal_state`` mapping (a subset of the
            observable fields).

    Returns:
        A list of ``"<field>: expected <expected> got <actual>"`` strings,
        one per divergent field, in declared order. Empty when the journey
        landed where every declared field said it should.
    """
    diverging: list[str] = []
    for field, want in expected.items():
        got = actual.get(field)
        if got != want:
            diverging.append(f"{field}: expected {want!r} got {got!r}")
    return diverging


async def _flow_terminal_state(
    *,
    key_sequence: list[str],
    scope: str,
    state_path: Path | None,
    state: State | None,
    size: tuple[int, int],
) -> dict[str, object]:
    """Mount the TUI, drive *key_sequence*, and return the terminal state.

    Drives the launch scope through the bound nav state machine
    (``action_switch_scope``) so the journey starts at the operator's launch
    scope, then presses each key in *key_sequence* through the real
    key->Binding path
    (:func:`~eawf.surfaces.tui.snapshot.behaviour_probe.record_flow_terminal_state`)
    and samples the terminal observable state.

    Args:
        key_sequence: The ordered Textual key strings to drive.
        scope: The launch nav scope (``repo`` / ``workspace`` / ``user``).
        state_path: The fixture ``state.json`` to bind, or ``None``.
        state: The inline state to bind, or ``None``.
        size: The Pilot terminal size.

    Returns:
        The terminal observable state as a plain ``dict`` keyed by
        :data:`~eawf.surfaces.tui.snapshot.behaviour_probe.OBSERVABLE_FIELDS`.
    """
    from eawf.surfaces.tui.app import EaApp
    from eawf.surfaces.tui.snapshot.behaviour_probe import record_flow_terminal_state
    from eawf.surfaces.tui.snapshot.pilot_harness import settle_screen

    app = EaApp(scope="repo", state_path=state_path, initial_state=state)
    async with app.run_test(size=size) as raw_pilot:
        pilot = cast("Pilot[object]", raw_pilot)
        await settle_screen(pilot)
        if scope != "repo":
            app.action_switch_scope(scope)
            await settle_screen(pilot)
        return await record_flow_terminal_state(pilot, key_sequence)


def _drive_flow(
    *,
    key_sequence: list[str],
    scope: str,
    state_path: Path | None,
    state: State | None,
    size: tuple[int, int],
) -> dict[str, object]:
    """Run :func:`_flow_terminal_state` to completion from a sync caller, loop-safe.

    The check kind is invoked synchronously by the gate runner, but that
    runner can itself be driven from inside a live event loop -- the daemon
    close path scores the deterministic floor while its JSON-RPC handler's
    loop is running. A bare :func:`asyncio.run` raises ``RuntimeError`` in
    that context, so when a running loop is detected the Pilot mount is
    offloaded to a dedicated worker thread with its own fresh loop; the
    no-loop path (a one-shot CLI, recovery shell, or test) runs
    :func:`asyncio.run` inline. Mirrors the affordance-parity check's
    loop-safe wrapper.

    Args:
        key_sequence: The ordered Textual key strings to drive.
        scope: The launch nav scope.
        state_path: The fixture ``state.json`` to bind, or ``None``.
        state: The inline state to bind, or ``None``.
        size: The Pilot terminal size.

    Returns:
        The terminal observable state dict.
    """

    def _run() -> dict[str, object]:
        return asyncio.run(
            _flow_terminal_state(
                key_sequence=key_sequence,
                scope=scope,
                state_path=state_path,
                state=state,
                size=size,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


@dataclass(frozen=True)
class _FlowArgs:
    """The parsed + validated ``tui_flow`` spec args.

    Attributes:
        flow: The journey name, surfaced in ``details``.
        key_sequence: The ordered Textual key strings the journey drives.
        terminal_state: The declared subset of observable fields the journey
            must land each at.
        scope: The launch nav scope (``repo`` / ``workspace`` / ``user``).
        state_path: The resolved fixture ``state.json`` path, or ``None``.
        state: The validated inline state, or ``None``.
        size: The Pilot terminal size.
    """

    flow: str
    key_sequence: list[str]
    terminal_state: dict[str, object]
    scope: str
    state_path: Path | None
    state: State | None
    size: tuple[int, int]


class _FlowArgsError(ValueError):
    """A malformed ``tui_flow`` spec arg, carrying the ``details`` note.

    Raised by :func:`_parse_flow_args` so :func:`check_tui_flow` can map any
    validation failure to a single degrade-not-raise ``status="fail"``
    result, keeping the per-guard branching out of the dispatch function.
    """


def _parse_state_source(
    args: dict[str, Any], cwd: Path, flow: str
) -> tuple[Path | None, State | None]:
    """Validate the optional ``state_path`` / inline ``state`` source."""
    state_path_arg = args.get("state_path")
    if state_path_arg is not None and not isinstance(state_path_arg, str):
        raise _FlowArgsError(f"flow={flow} arg 'state_path' must be a str")

    state_arg = args.get("state")
    if state_path_arg is not None and state_arg is not None:
        raise _FlowArgsError(f"flow={flow} args 'state_path' and 'state' are mutually exclusive")
    if state_arg is not None and not isinstance(state_arg, dict):
        raise _FlowArgsError(f"flow={flow} arg 'state' must be a mapping")

    state_path = (cwd / state_path_arg).resolve() if state_path_arg is not None else None
    if state_path is not None and not state_path.is_file():
        raise _FlowArgsError(f"flow={flow} state_path={state_path_arg} not found")

    if state_arg is None:
        return state_path, None

    from eawf.kernel.state.models import State

    try:
        return state_path, State.model_validate(state_arg)
    except ValidationError as exc:
        raise _FlowArgsError(f"flow={flow} arg 'state' failed validation: {exc}") from exc


def _parse_flow_args(args: dict[str, Any], cwd: Path) -> _FlowArgs:
    """Validate + resolve the ``tui_flow`` spec args into :class:`_FlowArgs`.

    Args:
        args: The raw ``spec.args`` mapping.
        cwd: The repo root a relative ``state_path`` resolves against.

    Returns:
        The parsed, validated args.

    Raises:
        _FlowArgsError: When any arg is missing / mistyped / out of range, or
            the resolved ``state_path`` fixture does not exist. The exception
            message is the ``details`` note for the failed check.
    """
    from eawf.surfaces.tui.snapshot.behaviour_probe import OBSERVABLE_FIELDS

    flow = args.get("flow")
    if not isinstance(flow, str) or not flow:
        raise _FlowArgsError("missing or non-str arg 'flow'")

    key_sequence = args.get("key_sequence")
    if not isinstance(key_sequence, list) or not all(isinstance(k, str) for k in key_sequence):
        raise _FlowArgsError(f"flow={flow} arg 'key_sequence' must be a list of strings")

    terminal_state = args.get("terminal_state")
    if not isinstance(terminal_state, dict) or not terminal_state:
        raise _FlowArgsError(f"flow={flow} arg 'terminal_state' must be a non-empty mapping")
    unknown = sorted(field for field in terminal_state if field not in OBSERVABLE_FIELDS)
    if unknown:
        raise _FlowArgsError(
            f"flow={flow} terminal_state unknown observable field(s): {', '.join(unknown)}"
        )

    scope = args.get("scope", "repo")
    if not isinstance(scope, str) or scope not in ("repo", "workspace", "user"):
        raise _FlowArgsError(f"flow={flow} arg 'scope' must be one of repo/workspace/user")

    try:
        size = _coerce_size(args.get("size"))
    except ValueError as exc:
        raise _FlowArgsError(f"flow={flow} {exc}") from exc

    state_path, state = _parse_state_source(args, cwd, flow)

    return _FlowArgs(
        flow=flow,
        key_sequence=list(key_sequence),
        terminal_state=dict(terminal_state),
        scope=scope,
        state_path=state_path,
        state=state,
        size=size,
    )


def check_tui_flow(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Drive a named operator journey + assert its terminal observable state.

    Args (read from ``spec.args``):
        flow: The journey name, surfaced in ``details``.
        key_sequence: The ordered Textual key strings the journey drives.
        terminal_state: A mapping of one or more of the observable
            fields to the value the journey must land each at (a partial
            spec is allowed).
        scope: The launch nav scope (``repo`` / ``workspace`` / ``user``).
            Optional (defaults to ``"repo"``).
        state_path: Repo-relative ``state.json`` fixture path resolved
            against ``cwd``. Optional (defaults to no bound state).
        state: Inline ``State`` payload to bind. Optional; mutually
            exclusive with ``state_path``.
        size: The ``[cols, rows]`` Pilot terminal size. Optional (defaults
            to ``[120, 40]``).

    Returns:
        :class:`CheckResult` with ``status="pass"`` when the journey's
        terminal observable state matches every declared ``terminal_state``
        field; ``status="fail"`` (with each divergent field named in
        ``details``) when the journey lands elsewhere, or when the args are
        malformed. Never raises -- a bad criterion degrades to a failed
        check, not an aborted run.
    """
    try:
        parsed = _parse_flow_args(spec.args, cwd)
    except _FlowArgsError as exc:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=str(exc),
        )

    try:
        actual = _drive_flow(
            key_sequence=parsed.key_sequence,
            scope=parsed.scope,
            state_path=parsed.state_path,
            state=parsed.state,
            size=parsed.size,
        )
    except Exception as exc:
        # The TUI mount / press path can raise a broad family of Textual /
        # runtime errors; the check degrades any of them to a fail so a bad
        # criterion cannot abort the whole audit run.
        logger.debug(
            f"check_tui_flow run-fail name={spec.name!r} flow={parsed.flow!r} reason={exc!r}"
        )
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"flow={parsed.flow} probe failed: {exc}",
        )

    diverging = _diverging_fields(actual=actual, expected=parsed.terminal_state)
    if diverging:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"flow={parsed.flow} terminal-state divergence: {'; '.join(diverging)}",
        )

    logger.debug(f"check_tui_flow ok name={spec.name!r} flow={parsed.flow!r}")
    asserted = len(parsed.terminal_state)
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=True,
        status="pass",
        details=f"flow={parsed.flow} reached terminal state ({asserted} field(s) asserted)",
    )


__all__ = ["check_tui_flow"]
