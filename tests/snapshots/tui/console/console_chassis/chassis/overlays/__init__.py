"""Full-frame overlays, discovered by module name: ``chassis/overlays/<name>.py``.

Contract for an overlay module:

``render(session, fixture, w, h) -> list[str]``
    Returns the FULL frame (``h`` rows of ``w`` cells, keybar last) via
    ``chassis.frame.build``, exactly as the prototype's ``overlayX()`` does. An overlay
    draws its own header crumb (`` Eä ▸ help · activity``) and its own keybar.

``hits(session, fixture) -> list[dict]`` (palette only)
    The ranked hit list the dispatcher's Enter reads; each hit is
    ``{"kind": "route"|"entity"|"search", "id": str, "to": route, "what": str}``.

The four decision overlays (question, pause, evidence, readiness) end with the three
state-model rows ``ov_state_rows`` supplies, spliced in before the last row exactly as
the prototype's ``wrap`` does; call ``with_state_rows(name, rows, session, w)`` on the
row list before ``build``.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING

from ...chassis.frame import Fixed
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_cache: dict[str, ModuleType | None] = {}

OV_MODEL: dict[str, tuple[tuple[str, str], ...]] = {
    "question": (
        ("open", "You answer, or another eligible principal does."),
        ("answered", "Nothing — an answer is immutable, and a change is a new question."),
        ("answered elsewhere", "Nothing — your outcome is superseded, not refused."),
        ("expired", "Nothing — the deadline passed with no answer."),
        ("withdrawn", "Nothing — the agent resolved it without you."),
        ("unanswerable", "The asking Run is recovered first."),
    ),
    "pause": (
        ("paused by you", "You resume it."),
        ("waiting on a check", "A predicate the system evaluates comes true."),
        ("waiting on a person", "An eligible principal answers."),
        ("escalated", "An operator with release authority answers."),
        ("held", "A person lifts the hold — no predicate will."),
    ),
    "evidence": (
        ("uncertified", "Rung 4 passes — only rung 4 certifies."),
        ("certified", "Nothing — certification is an outcome, never an inference."),
        ("rung failed", "Rung 3 returned a negative, so rung 4 cannot run."),
        ("digest unavailable", "The input digest becomes fetchable again."),
    ),
    "readiness": (
        ("draft", "Every member reaches ready."),
        ("candidate", "An operator approves against the current head."),
        ("approved", "Publication succeeds, or the bound head moves."),
        ("invalidated → draft", "The members are re-checked — candidate is an illegal target."),
        ("published", "Nothing — publication is immutable."),
    ),
}
OV_IMPOSSIBLE: dict[str, str] = {
    "question": "answered → open · expired → answered · withdrawn → answered",
    "pause": "held → resumed by you · waiting on a check → escalated",
    "evidence": "a passed rung above an unpassed rung · failed by inference",
    "readiness": "approved → candidate · draft → approved · published → draft",
}


def ov_state_rows(name: str, session: Session, w: int) -> list[str]:
    model = OV_MODEL[name]
    i = (session.ov_state.get(name, 0)) % len(model)
    st = model[i]
    return [
        Fixed(pad(f" STATE      {st[0]}   ({i + 1} of {len(model)} · s cycles)", w)),
        Fixed(pad(f" ENDS WHEN  {st[1]}", w)),
        Fixed(pad(f" IMPOSSIBLE {OV_IMPOSSIBLE[name]}", w)),
    ]


def with_state_rows(name: str, rows: list[str], session: Session, w: int) -> list[str]:
    """Splice the three state rows before the last row, as the prototype's wrap does."""
    if not rows:
        return rows
    at = len(rows) - 1
    st = ov_state_rows(name, session, w)
    keep = max(0, at - len(st))
    out = list(rows[:keep]) + st + [rows[at]]
    return [
        Fixed(pad("", w)) if r is None else (r if isinstance(r, Fixed) else Fixed(pad(r, w)))
        for r in out
    ]


def module_name(name: str) -> str:
    return f"{__package__}.{name}"


def module_for(name: str) -> ModuleType | None:
    if name in _cache:
        return _cache[name]
    try:
        mod: ModuleType | None = importlib.import_module(module_name(name))
    except ModuleNotFoundError as exc:
        if exc.name != module_name(name):
            raise
        mod = None
    _cache[name] = mod
    return mod


def render_overlay(name: str, session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    mod = module_for(name)
    if mod is None or not hasattr(mod, "render"):
        return placeholder(name, session, fixture, w, h)
    return mod.render(session, fixture, w, h)


def placeholder(name: str, session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    from ...chassis.frame import bar, build, header_row, keybar, thin

    rows = [
        header_row(session, fixture, f" Eä ▸ {name}", w),
        f" NOT PORTED · overlay {name} · no module at {module_name(name)}",
        bar(w),
        " PLACEHOLDER  port the prototype's overlay renderer into the module named above",
        thin(w),
    ]
    return build(session, rows, keybar([("Esc", "close")], w), w, h)
