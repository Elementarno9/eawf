"""``affordance_parity`` audit-DSL kind (Fidelity Spine FS09).

Closes the affordance-parity half of the FSM-per-element spine: a footer
hint that advertises a key (``c config`` / ``Enter open`` / ...) is a
promise the key does something, but a golden snapshot only proves the
*label* renders -- it cannot see that the advertised key still resolves
to a live :class:`~textual.binding.Binding`. This kind drives each
advertised key through the real key->Binding path (the
:func:`~eawf.surfaces.tui.snapshot.behaviour_probe.record_keypress_transcript`
driver) and fails -- naming each offending key -- when an advertised key
does NOT resolve, so a footer that promises a dead key is caught.

Args (read from ``spec.args``)
------------------------------

* ``mode`` -- the mode name to switch to before enumerating the footer
  (e.g. ``"home"`` / ``"doctor"``). A name with no registered mode is a
  ``fail``.
* ``state_path`` -- repo-relative path to the ``state.json`` fixture the
  app binds (resolved against ``cwd``). Optional; defaults to ``None``
  (the user-scope launch with no bound state).
* ``size`` -- the ``[cols, rows]`` terminal size the Pilot harness runs
  at. Optional; defaults to ``[120, 40]`` (wide enough for the full
  footer strip so no advertised hint is clipped).

A malformed ``args`` (non-str ``mode``, bad ``size`` shape, unreadable
fixture) yields ``status="fail"`` with a ``details`` note rather than
propagating an exception, so one bad criterion cannot abort the audit
run -- the same degrade-not-raise contract
:func:`~eawf.workflow.audit_dsl.kinds.schema_validate.check_schema_validate`
follows.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

if TYPE_CHECKING:
    from textual.pilot import Pilot

    from eawf.surfaces.tui.snapshot.behaviour_probe import ProbeStatus

logger = logging.getLogger(__name__)


#: Default Pilot terminal size. Wide enough that the full footer hint
#: strip renders without clipping any advertised token.
_DEFAULT_SIZE: tuple[int, int] = (120, 40)

#: The probe commit stamp. The check does not pin a real build SHA (it
#: runs the live tree, not a recorded transcript), so a fixed sentinel
#: keeps the transcript provenance field populated without claiming a
#: provenance the live run does not have.
_PROBE_COMMIT: str = "affordance-parity-live"

#: Mapping from a footer hint *token* (the text before the first space in
#: a ``render_hint_label`` fragment) to the Textual key string(s) a press
#: drives. Multi-glyph tokens (the arrow pairs, the three-letter scope
#: switch) map to every key they advertise; the punctuation glyphs map to
#: their Textual key names. A token absent from this map is a single
#: literal key (``a`` / ``c`` / ``H`` / ``space`` press as themselves).
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


async def _advertised_keys(
    *,
    mode: str,
    state_path: Path | None,
    size: tuple[int, int],
) -> list[str]:
    """Mount the TUI, switch to *mode*, and return its advertised footer keys.

    Reads the mode screen's footer hint strip and maps each advertised token
    to its Textual key string(s) (:func:`_token_keys`), de-duplicated in
    advertised order so a key advertised twice is probed once.

    Args:
        mode: The mode name to switch to before enumerating the footer.
        state_path: The fixture ``state.json`` to bind, or ``None``.
        size: The Pilot terminal size.

    Returns:
        The advertised key strings, in first-advertised order.
    """
    from eawf.surfaces.tui.app import EaApp
    from eawf.surfaces.tui.snapshot.pilot_harness import settle_screen
    from eawf.surfaces.tui.widgets.footer import Footer

    app = EaApp(scope="repo", state_path=state_path)
    async with app.run_test(size=size) as raw_pilot:
        pilot = cast("Pilot[object]", raw_pilot)
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


async def _probe_key(
    *,
    mode: str,
    key: str,
    state_path: Path | None,
    size: tuple[int, int],
) -> ProbeStatus:
    """Drive a single advertised *key* against a fresh mount + return its status.

    Each key is probed against its OWN fresh app mount so a destructive
    affordance (``q`` quit, a scope switch, a modal push) cannot bleed into
    the next key's probe -- a press-everything-on-one-mount driver is
    order-dependent (the first ``q`` would quit and starve every later key),
    so per-key isolation keeps the classification deterministic and
    side-effect-free across the advertised set.

    Args:
        mode: The mode name to switch to before pressing the key.
        key: The Textual key string to drive.
        state_path: The fixture ``state.json`` to bind, or ``None``.
        size: The Pilot terminal size.

    Returns:
        The :class:`~eawf.surfaces.tui.snapshot.behaviour_probe.ProbeStatus`
        the key press classified.
    """
    from eawf.surfaces.tui.app import EaApp
    from eawf.surfaces.tui.snapshot.behaviour_probe import record_keypress_transcript
    from eawf.surfaces.tui.snapshot.pilot_harness import settle_screen

    app = EaApp(scope="repo", state_path=state_path)
    async with app.run_test(size=size) as raw_pilot:
        pilot = cast("Pilot[object]", raw_pilot)
        await settle_screen(pilot)
        await app.switch_mode(mode)
        await settle_screen(pilot)
        transcript = await record_keypress_transcript(pilot, [key], source_commit=_PROBE_COMMIT)
    return transcript.outcomes[0].status


async def _offending_keys(
    *,
    mode: str,
    state_path: Path | None,
    size: tuple[int, int],
) -> list[str]:
    """Return the advertised keys of *mode* that resolve to no binding.

    Enumerates the mode's advertised footer keys (:func:`_advertised_keys`)
    then drives each through :func:`_probe_key` against its own fresh mount. An
    offending key is one whose press classifies
    :data:`~eawf.surfaces.tui.snapshot.behaviour_probe.ProbeStatus.UNRESOLVED`
    -- the dead affordance the footer promises but no
    :class:`~textual.binding.Binding` answers.

    A
    :data:`~eawf.surfaces.tui.snapshot.behaviour_probe.ProbeStatus.NO_OP`
    (the key resolves to a binding but moves none of the coarse observable
    signals the probe samples -- a cursor move within a list, an F5 refresh, a
    scope re-select that lands the same scope) is NOT offending: a resolving
    binding IS a present affordance, and the coarse signal set deliberately
    does not see every intra-pane effect. Treating NO_OP as a failure would
    flag every list-navigation key on a real mode, so parity keys on the
    load-bearing dead-affordance shape -- no binding at all.

    Args:
        mode: The mode name to switch to before enumerating the footer.
        state_path: The fixture ``state.json`` to bind, or ``None``.
        size: The Pilot terminal size.

    Returns:
        The list of offending key strings, in advertised order (empty when
        every advertised key resolves to a binding).
    """
    from eawf.surfaces.tui.snapshot.behaviour_probe import ProbeStatus

    keys = await _advertised_keys(mode=mode, state_path=state_path, size=size)
    offending: list[str] = []
    for key in keys:
        status = await _probe_key(mode=mode, key=key, state_path=state_path, size=size)
        if status is ProbeStatus.UNRESOLVED:
            offending.append(key)
    return offending


def check_affordance_parity(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Drive a mode screen's advertised footer keys + flag any that do not resolve.

    Args (read from ``spec.args``):
        mode: The mode name to switch to before enumerating the footer.
        state_path: Repo-relative ``state.json`` fixture path resolved
            against ``cwd``. Optional (defaults to no bound state).
        size: The ``[cols, rows]`` Pilot terminal size. Optional (defaults
            to ``[120, 40]``).

    Returns:
        :class:`CheckResult` with ``status="pass"`` when every advertised
        footer key resolves to a binding; ``status="fail"`` (with each
        offending key named in ``details``) when an advertised key resolves
        to no binding (classifies
        :data:`~eawf.surfaces.tui.snapshot.behaviour_probe.ProbeStatus.UNRESOLVED`),
        or when the args are malformed. Never raises -- a bad criterion
        degrades to a failed check, not an aborted run.
    """
    mode = spec.args.get("mode")
    if not isinstance(mode, str) or not mode:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details="missing or non-str arg 'mode'",
        )

    state_path_arg = spec.args.get("state_path")
    if state_path_arg is not None and not isinstance(state_path_arg, str):
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details="arg 'state_path' must be a str",
        )

    try:
        size = _coerce_size(spec.args.get("size"))
    except ValueError as exc:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=str(exc),
        )

    state_path = (cwd / state_path_arg).resolve() if state_path_arg is not None else None
    if state_path is not None and not state_path.is_file():
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"state_path={state_path_arg} not found",
        )

    try:
        offending = asyncio.run(_offending_keys(mode=mode, state_path=state_path, size=size))
    except Exception as exc:
        # The TUI mount / switch path can raise a broad family of Textual /
        # runtime errors; the check degrades any of them to a fail so a bad
        # criterion cannot abort the whole audit run.
        logger.debug(f"check_affordance_parity run-fail name={spec.name!r} reason={exc!r}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"probe failed for mode={mode}: {exc}",
        )

    if offending:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"mode={mode} unresolved advertised keys: {', '.join(offending)}",
        )

    logger.debug(f"check_affordance_parity ok name={spec.name!r} mode={mode!r}")
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=True,
        status="pass",
        details=f"mode={mode} all advertised footer keys resolve",
    )


__all__ = ["check_affordance_parity"]
