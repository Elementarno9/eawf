"""health: every check with its own result, the focused check's repair docked to the foot.

The native frame lists every check the read model holds -- each runtime tuple's
conformance verdict and each health record -- with one of four outcomes, ``passed``,
``warn``, ``failed`` or ``unknown``. Only a returned negative is ``failed``; a check with no
result is unknown, and ``degraded`` is never an outcome because it names a connection
state. The ``CHECKS`` line counts the list it heads, so the two cannot disagree. The repair
is named, never run: the route binds no mutation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from eawf.kernel.projection.truth import TruthState
from eawf.kernel.projection.verification import HealthReadModel
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.format import clock_time
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, g_pad, thin
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    finish,
    label,
    more,
    native,
    native_head,
    route_crumb,
    tuple_rows,
)

_KEYS = route_pairs("health")
Check = tuple[str, str, str, str | None]


def _checks() -> list[Check]:
    unknown = chip("info", "? unknown")
    passed = chip("ok", "pass")
    return [
        (
            "Policy revision reachable",
            chip("er", "FAILED"),
            "cannot read revision",
            "Re-point the sandbox policy at a readable revision.",
        ),
        ("Heartbeat probe certified", passed, "14:00", None),
        ("Digest store writable", passed, "14:00", None),
        (
            "Clock skew within 250 ms",
            unknown,
            "probe never ran",
            "Run the clock probe on the host that never answered.",
        ),
        ("Event schema accepted", passed, "14:01", None),
        ("Snapshot restore verified", passed, "13:58", None),
        (
            "Provider rate cards current",
            unknown,
            "no card for the local runner",
            "Attach a rate card for the local runner, or accept it unmetered.",
        ),
        ("Replay digests reproducible", passed, "13:31", None),
        ("Retention sweep on schedule", passed, "09:12", None),
    ]


#: The four outcomes a check can have, in the order the ``CHECKS`` line counts them.
OUTCOMES: tuple[str, ...] = ("failed", "warn", "unknown", "passed")

#: A conformance verdict's grade, as the outcome the frame draws it under.
_VERDICT_OUTCOME: Mapping[str, str] = MappingProxyType(
    {"ok": "passed", "warn": "warn", "fail": "failed"}
)

#: A health record's stored status, as the outcome the frame draws it under. A status not
#: named here is an outcome the frame cannot vouch for, so it reads unknown.
_RECORD_OUTCOME: Mapping[str, str] = MappingProxyType(
    {"OK": "passed", "PASSED": "passed", "WARN": "warn", "FAILED": "failed", "FAIL": "failed"}
)

#: What the ``REPAIR`` readout says for a check that passes.
NOTHING_TO_REPAIR = "∅ Nothing to repair · this check passes"

#: What the LAST RESULT cell says for a stored health record: it carries no check time,
#: only the conformance runner's stage records do.
NO_CHECK_TIME = "no check time on a stored record"

#: Where a named repair runs: this route only names it.
REPAIR_LIVES = "Naming it happens here; running it lives in settings."


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthCheck:
    """One check as the Health frame lists it.

    Attributes:
        name: The check's stable name.
        outcome: One of :data:`OUTCOMES`.
        reason: Why it stands where it does, as its producer worded it.
        producer: Who answered for the check.
        last_result: When the check last ran, or why no time is stated.
    """

    name: str
    outcome: str
    reason: str
    producer: str
    last_result: str


def checks_of(model: HealthReadModel) -> list[HealthCheck]:
    """Return every check the read model holds: the tuple verdicts, then the health records."""
    found = [
        HealthCheck(
            name=row.check,
            outcome=_VERDICT_OUTCOME.get(row.status, "unknown"),
            reason=row.detail or UNKNOWN_WORD,
            producer=row.producer.value or UNKNOWN_WORD,
            last_result=clock_time(row.checked_at),
        )
        for row in model.tuples
    ]
    for row in model.rows:
        status = row.field("status")
        stated = status.value if status.state is TruthState.KNOWN and status.value else ""
        found.append(
            HealthCheck(
                name=row.key,
                outcome=_RECORD_OUTCOME.get(stated.upper(), "unknown"),
                reason=f"stored {stated}" if stated else "no result recorded",
                producer=status.producer,
                last_result=NO_CHECK_TIME,
            )
        )
    return found


def checks_line(checks: list[HealthCheck]) -> str:
    """Return the ``CHECKS`` line, every count taken off the list it heads."""
    tally = [f"{sum(1 for c in checks if c.outcome == o)} {o}" for o in OUTCOMES]
    return f"{dv.plural(len(checks), 'check')} · {' · '.join(tally)}"


def _check_words(name: str) -> str:
    """Return a check's stable name as words: ``runtime_tuple_1fb4`` reads ``runtime tuple 1fb4``.

    The name stays the check's id, which the cursor is kept by; only the cell reads it.
    """
    return name.replace("_", " ")


def _repair(check: HealthCheck | None) -> list[str]:
    """Return the ``REPAIR`` readout docked at the foot: what repairs the focused check."""
    if check is None:
        return [label("REPAIR", "∅ no check is focused · nothing declared is held")]
    if check.outcome == "passed":
        return [
            label("REPAIR", f"{_check_words(check.name)} · {NOTHING_TO_REPAIR}"),
            more("Its last result stands until the next sweep."),
        ]
    return [
        label("REPAIR", f"{_check_words(check.name)} · check {check.outcome}"),
        more(f"{UNKNOWN_WORD} · no remediation is declared for it · {REPAIR_LIVES}"),
    ]


def health_frame(view: View, model: HealthReadModel) -> list[str]:
    """Return the Health frame drawn from the read model the daemon served.

    Args:
        view: The render being built.
        model: The health read model at the committed cursor, tuple verdicts included.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    wide = view.wide
    checks = checks_of(model)
    # the cursor is kept by the check's name, so a reordered list keeps the same check
    names = [check.name for check in checks]
    at = names.index(s.sel_id) if s.sel_id in names else min(max(s.sel, 0), len(checks) - 1)
    s.sel, s.sel_id = max(at, 0), (names[at] if checks else None)
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(view, model, "Health"),
        # the list counts the tuple verdicts beside the stored records, so the summary does
        summary=f"Fleet health · every declared check · {dv.plural(len(checks), 'check')}",
    )
    # a column that folds the reason and the time names both in its head
    grid = Grid([30, 11, w - 3 - 30 - 11 - 26, 0] if wide else [30, 11, 0])
    heads = ["CHECK", "RESULT", "REASON · LAST RESULT"] + (["ANSWERED BY"] if wide else [])
    body = [label("CHECKS", checks_line(checks)), grid.head(heads)]
    for i, check in enumerate(checks):
        result = UNKNOWN_WORD if check.outcome == "unknown" else check.outcome
        cells = [_check_words(check.name), result, f"{check.reason} · {check.last_result}"]
        body.append(grid.row([*cells, check.producer] if wide else cells, i == s.sel, w))
    if not checks:
        body.append(f"   {UNKNOWN_WORD} · no declared check has reported to this scope")
    if wide:
        body += [thin(w), *tuple_rows(model.tuples, w)]
    focused = checks[s.sel] if checks else None
    s.nav_rows = len(checks)
    return finish(view, top, body, _KEYS, foot=[thin(w), *_repair(focused)])


def render(view: View) -> list[str]:
    """Return the Health frame, native when a read model is held."""
    model = native(view)
    # the health route's read model always carries its tuple verdicts
    if isinstance(model, HealthReadModel):
        return health_frame(view, model)
    s, w, h = view.session, view.w, view.h
    # two columns of air between the longest check name and its result cell
    grid = Grid([30, 11, 0])
    checks = _checks()
    dv.sel_in(s, len(checks))
    body = [
        f" CHECKS       {len(checks)} checks · 1 failed · 2 unknown · 6 pass · as of 14:02",
        grid.head(["CHECK", "RESULT", "REASON"]),
    ]
    body.extend(grid.row([c[0], c[1], c[2]], i == s.sel, w) for i, c in enumerate(checks))
    # the repair follows the cursor at the foot, so it never floats with the list's height
    repair = checks[s.sel][3]
    foot = [
        thin(w),
        " REPAIR       " + (repair or "∅ Nothing to repair · this check passes."),
        "              "
        + (
            "Naming it happens here; running it lives in settings."
            if repair
            else "Its last result stands until the next sweep."
        ),
    ]
    body.extend(g_pad("", w) for _ in range(h - 4 - len(foot) - len(body)))
    body.extend(foot)
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Health",
        ctx="Fleet health · conformance runner · as of 14:02",
        body=body,
        keys=_KEYS,
    )
