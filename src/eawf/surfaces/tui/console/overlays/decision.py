"""The decision overlays drawn from the record each is bound to.

Question detail, pause detail, evidence viewer and readiness matrix each end with
``STATE`` and ``ENDS WHEN``, read off the record's situation (:mod:`.situations`); none
prints an ordinal, a cycling hint or an impossible-transition legend, and none binds a
key its keybar does not name. The acceptance evidence overlay is about a sealed bundle
rather than a claim, so it draws no ladder and no state rows.

The keybar each renderer returns is the promise its keys keep
(:mod:`.bound_keys`): a key that is not on it does nothing.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Sequence

from eawf.kernel.projection.truth import TruthState
from eawf.kernel.spec.release import ReleaseStatus
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.decisions import (
    ClaimRecord,
    DecisionRecords,
    DropReason,
    PauseRecord,
    PauseStatus,
    QuestionRecord,
    QuestionStatus,
    short_time,
)
from eawf.surfaces.tui.console.frame import (
    CARET,
    Fixed,
    Grid,
    View,
    bar,
    build,
    header,
    make_room,
    thin,
)
from eawf.surfaces.tui.console.keybar import Pair, keybar
from eawf.surfaces.tui.console.overlays.chassis import crumb, cursor_foot
from eawf.surfaces.tui.console.overlays.situations import (
    APPROVED_ONWARD,
    REASON_WORDS,
    Situation,
    answerable,
    claim_standing,
    pause_situation,
    question_situation,
    release_situation,
    reply_legal,
    rung_outcome,
    unknown_outcome,
)
from eawf.surfaces.tui.console.reads import can_mutate
from eawf.surfaces.tui.console.renderers.release import NO_RELEASE
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import RULE_THIN, TRUTH
from eawf.surfaces.tui.console.width import cell_len, pad
from eawf.workflow.projection.acceptance import (
    READINESS_MET,
    AcceptanceBundleView,
    CriterionRow,
    ReadinessSignal,
    ReleaseReadinessView,
)

_GUTTER = 10
UNKNOWN = TRUTH["unknown"].unicode
#: The no-value dash: a value that does not exist by design.
NO_VALUE = "–"  # noqa: RUF001
DENIED = f"{TRUTH['denied'].unicode} denied · authority class"
_ESC_BACK: Pair = ("Esc", "back")
_EVIDENCE_TABLE = Grid([12, 14, 22, 0])


def lab(label: str, value: str) -> str:
    """Return a labelled row: ``label`` in the ten-cell gutter, then ``value``."""
    return f" {pad(label, _GUTTER)}{value}"


def cont(value: str) -> str:
    """Return a continuation row under a labelled one."""
    return f" {' ' * _GUTTER}{value}"


# Where a row's value starts when it is not in the label gutter: after its leading
# indent, or after a wide label's two-space gap.
_VALUE_AT = re.compile(r"^( {2,}|\s*\S.*?\S? {2,})")


def wrap_row(row: str, w: int) -> list[str]:
    """Return ``row`` wrapped under its own value column when it is wider than ``w``.

    A value is never clipped away: the words that do not fit continue on rows indented to
    where the value started.
    """
    if cell_len(row) <= w:
        return [row]
    gutter = _GUTTER + 1
    if row[:1] == " " and row[1:2] != " " and row[gutter - 1 : gutter] == " ":
        indent = gutter
    else:
        found = _VALUE_AT.match(row)
        indent = len(found.group(1)) if found else 1
    lines = textwrap.wrap(row[indent:], width=max(8, w - indent - 1))
    return [row[:indent] + lines[0], *(" " * indent + line for line in lines[1:])]


def _fit(body: Sequence[str], room: int) -> list[str]:
    """Return ``body`` fitted to ``room`` rows: dividers go first, then a window is cut.

    The window keeps the cursor row (the first row carrying the caret in its gutter) in
    view and counts the rows it hides at each edge, so nothing is dropped without saying so.
    """
    if len(body) <= room:
        return list(body)
    kept = make_room(body, len(body) - room)
    if kept is not None:
        return kept
    rows = [r for r in body if set(r) != {RULE_THIN}]
    focus = next((i for i, r in enumerate(rows) if CARET in r[:14]), 0)
    take = room - 1
    start = max(0, min(focus - take // 2, len(rows) - take))
    if 0 < start < len(rows) - take:
        take -= 1
        start = max(0, min(focus - take // 2, len(rows) - take))
    below = len(rows) - start - take
    return [
        *([f"   … {start} rows above"] if start else []),
        *rows[start : start + take],
        *([f"   … {below} rows below"] if below else []),
    ]


def decision_frame(
    view: View,
    *,
    name: str,
    subject: str,
    context: str,
    body: Sequence[str],
    keys: Sequence[Pair],
    situation: Situation | None = None,
    foot: str | None = None,
) -> list[str]:
    """Return an overlay frame: crumb, context, body, its foot, then its state rows.

    Args:
        view: The render being built.
        name: The overlay, which names the crumb.
        subject: The record the overlay is about, which the crumb names too.
        context: The row under the header.
        body: The rows the record renders.
        keys: The keybar pairs, which are every key the overlay acts on.
        situation: The record's situation, drawn as ``STATE`` and ``ENDS WHEN`` just
            above the keybar; ``None`` for an overlay about no stateful record.
        foot: The cursor foot naming the row the cursor is on, for a cursor overlay.
    """
    w, h = view.w, view.h
    state = (
        [
            *wrap_row(lab("STATE", situation.name), w),
            *wrap_row(lab("ENDS WHEN", situation.ends), w),
        ]
        if situation
        else []
    )
    tail = [foot] if foot else []
    room = h - 1 - 3 - len(tail) - len(state)
    fitted = [*_fit([line for row in body for line in wrap_row(row, w)], room), *tail]
    fill = [""] * (h - 1 - 3 - len(fitted) - len(state))
    rows = [header(view, crumb(name, subject)), " " + context, bar(w), *fitted, *fill, *state]
    return build(view, [Fixed(pad(r, w)) if i else r for i, r in enumerate(rows)], keybar(keys, w))


# ---------- the question detail ----------


def _answer_words(q: QuestionRecord, key: str | None) -> str:
    option = q.option(key)
    return option.label if option is not None else (q.reply or f"{UNKNOWN} unknown")


def question_keys(q: QuestionRecord, session: Session, run_state: str | None) -> list[Pair]:
    """Return the question detail's keybar, which is also every key it acts on."""
    if not (answerable(q, run_state) and can_mutate(session)):
        return [_ESC_BACK]
    pairs: list[Pair] = []
    if q.options:
        last = len(q.options)
        pairs.append(("1" if last == 1 else f"1..{last}", "pick an answer"))
    if reply_legal(q, run_state):
        pairs.append(("w", "reply"))
    pairs += [("x", "decline"), ("Esc", "back — it stays open")]
    return pairs


def _question_outcome(q: QuestionRecord, principal: str | None) -> list[str]:
    """Return the rows the question's resolution adds beneath its options."""
    if q.status is QuestionStatus.ANSWERED and q.resolution_actor == principal:
        return [lab("ANSWER", f"{_answer_words(q, q.chosen_option)} · immutable")]
    if q.status is QuestionStatus.ANSWERED:
        won = (
            f"{_answer_words(q, q.chosen_option)} · by {q.resolution_actor}"
            if q.disclosed
            else f"{TRUTH['unavailable'].unicode} not disclosed"
        )
        rows = [lab("WON", won)]
        rows += [
            lab("YOURS", f"{_answer_words(q, late.option)} · superseded")
            for late in q.late_answers
            if late.principal == principal
        ]
        return rows
    if q.status in (QuestionStatus.AUTO_RESOLVED, QuestionStatus.SEALED):
        return [
            lab(
                "DEFAULT",
                f"{_answer_words(q, q.default_option)} · selected by policy, not an answer",
            )
        ]
    if q.drop_reason is DropReason.SUPERSEDED:
        return [lab("SUCCESSOR", f"▸ {q.superseded_by} · an answer here is handed to it")]
    if q.status is QuestionStatus.DROPPED and q.drop_reason is not None:
        why = q.drop_reason.value.replace("_", " ")
        return [lab("WITHDRAWN", f"{why} · by {q.resolution_actor or 'the system'}")]
    return []


def _override_window(q: QuestionRecord) -> str:
    """Return what the ``OVERRIDE`` row says about a defaulting question's window."""
    if q.status is QuestionStatus.AUTO_RESOLVED:
        return f"open until {short_time(q.override_until)}"
    if q.override_until is not None:
        # a waiting decision files its window up front: the default stands when it closes
        return f"the default stands at {short_time(q.override_until)} unless you answer"
    return f"{TRUTH['unavailable'].unicode} no override window is open"


def render_question(view: View, q: QuestionRecord, decisions: DecisionRecords) -> list[str]:
    """Return the question detail over ``q``."""
    run_state = decisions.run_states.get(q.run) if q.run is not None else None
    situation = question_situation(q, principal=decisions.principal, run_state=run_state)
    body = [lab("QUESTION", q.question)]
    if q.rationale:
        body.append(lab("WHY", q.rationale))
    body.append(thin(view.w))
    for i, option in enumerate(q.options, start=1):
        marks = " · recommended · not consent" if option.recommended else ""
        marks += " · the declared default" if option.key == q.default_option else ""
        body.append(lab("OPTIONS" if i == 1 else "", f"{i}  {option.label}{marks}"))
    if not q.options:
        body.append(lab("OPTIONS", "none · it is answered in your own words"))
    if q.default_option is not None:
        body.append(lab("OVERRIDE", _override_window(q)))
    if q.status is QuestionStatus.BLOCKED:
        body.append(lab("HALTED", f"the work under {q.scope} waits on this answer"))
    if q.escalated_by:
        body.append(lab("URGENCY", f"raised by {q.escalated_by} · a deadline never answers"))
    if run_state is not None and situation.name.startswith("unanswerable"):
        body.append(lab("RUN", f"{q.run} is {run_state.lower()} · it cannot hear an answer"))
    outcome = _question_outcome(q, decisions.principal)
    if outcome:
        body += [thin(view.w), *outcome]
    return decision_frame(
        view,
        name="question",
        subject=q.id,
        context=f"asked by {q.run or 'a person'} under {q.scope} · {short_time(q.asked_at)}",
        body=body,
        keys=question_keys(q, view.session, run_state),
        situation=situation,
    )


# ---------- the pause detail ----------


def pause_keys(p: PauseRecord, session: Session, run_state: str | None) -> list[Pair]:
    """Return the pause detail's keybar, which is also every key it acts on."""
    back: Pair = ("Esc", "back — the pause stays as it is")
    if unknown_outcome(p, run_state) and can_mutate(session):
        return [("n", "reconcile"), ("c", "let go"), back]
    return [back]


def _control_rows(p: PauseRecord) -> list[str]:
    control = p.control
    if control is None:
        return []
    accepted = short_time(control.accepted) if control.accepted else "not yet"
    confirmed = (
        f"confirmed   {short_time(control.confirmed)}"
        if control.confirmed
        else "confirmed   —  the run never answered"
    )
    return [
        lab("PAUSE", f"requested   {short_time(control.requested)} · accepted {accepted}"),
        cont(confirmed),
    ]


def render_pause(view: View, p: PauseRecord, decisions: DecisionRecords) -> list[str]:
    """Return the pause detail over ``p``."""
    w = view.w
    run_state = decisions.run_states.get(p.scope)
    unknown = unknown_outcome(p, run_state)
    body = _control_rows(p)
    if p.waiting_on is not None:
        body.append(lab("WAITS ON", f"▸ {p.waiting_on} · a person answers it"))
        body.append(lab("ELIGIBLE", " · ".join(p.eligible) or f"{UNKNOWN} unknown · none named"))
    else:
        body.append(f" RESUME PREDICATE  {p.resume_predicate}")
        checked = (
            f"last {short_time(p.last_evaluated)}" if p.last_evaluated else "not evaluated yet"
        )
        body.append(f" EVALUATED BY      {p.evaluator or 'the daemon'} · {checked}")
    if p.status is PauseStatus.HELD:
        body.append(lab("TIMERS", "frozen while the Hold stands"))
    if p.escalation is not None:
        esc = p.escalation
        body.append(
            lab("RAISED", f"{esc.cause.value} at {short_time(esc.raised_at)} · ▸ {esc.raised_ref}")
        )
    if p.retry_used is not None and p.retry_allowed is not None:
        budget = f"{p.retry_used} of {p.retry_allowed} attempts used"
        if unknown:
            budget += " · retry is not offered while the outcome is unknown"
        body.append(f" RETRY BUDGET      {budget}")
    if unknown:
        body += [
            thin(w),
            lab("UNKNOWN", "This is not a failure and not a success. Until the run answers,"),
            cont("neither a resume nor a let-go can be confirmed — only requested."),
            thin(w),
            lab("NOT", "Reconciling does not restart the run; letting go does not fail it."),
        ]
    return decision_frame(
        view,
        name="pause",
        subject=p.scope,
        context=REASON_WORDS[p.reason],
        body=body,
        keys=pause_keys(p, view.session, run_state),
        situation=pause_situation(p, run_state),
    )


# ---------- the evidence viewer ----------

#: The rows the evidence viewer's cursor walks: the claim itself, then each rung.
CLAIM_ROWS = 5


def claim_copy(c: ClaimRecord, row: int) -> str:
    """Return what ``y`` copies on cursor row ``row``: the claim, or one rung of it."""
    return c.urn if row <= 0 else f"{c.urn}#rung-{row}"


def _edges(label: str, edges: Sequence[tuple[str, str, bool]]) -> list[str]:
    """Return a claim's typed edges under ``label``, which heads its own row when it is wide."""
    texts = [
        DENIED if denied else f"{ref}" + (f" · {what}" if what else "")
        for ref, what, denied in edges
    ] or [f"{TRUTH['unavailable'].unicode} none recorded"]
    if cell_len(label) >= _GUTTER:
        return [f" {label}", *(cont(text) for text in texts)]
    return [lab(label if i == 0 else "", text) for i, text in enumerate(texts)]


def render_evidence(view: View, c: ClaimRecord) -> list[str]:
    """Return the evidence viewer over claim ``c``."""
    s, w = view.session, view.w
    s.sel = max(0, min(s.sel, CLAIM_ROWS - 1))
    standing = claim_standing(c)
    mark = "▸" if s.sel == 0 else " "
    body = [f" {pad('CLAIM', _GUTTER - 2)}{mark} {c.title}"]
    for label, value in (
        ("IN WORDS", c.in_words),
        ("IT PROVES", c.proves),
        ("BREAKS IF", c.breaks_if),
    ):
        if value:
            body.append(lab(label, value))
    source = " · ".join(
        x for x in (c.source, f"asked by {c.asked_by}" if c.asked_by else None) if x
    )
    body.append(lab("FROM", source or f"{TRUTH['unavailable'].unicode} not recorded"))
    body += _edges("SUPPORT", [(e.ref, e.what, e.denied) for e in c.supports])
    body += _edges("CONTRADICTION", [(e.ref, e.what, e.denied) for e in c.contradictions])
    body.append(thin(w))
    for r in c.rungs:
        mark = "▸" if s.sel == r.rung else " "
        label = pad("LADDER" if r.rung == 1 else "", _GUTTER - 2)
        body.append(f" {label}{mark} {pad(f'{r.rung} {r.name}', 12)}{rung_outcome(c, r)}")
        body.append(cont(f"  {r.check}"))
    body += [thin(w), lab("STANDING", standing.name)]
    return decision_frame(
        view,
        name="evidence",
        subject=c.id,
        context=f"a claim and the ladder it stands on · as of {short_time(c.as_of)}",
        body=body,
        keys=[("↑↓", "row"), ("y", "copy"), ("Esc", "back")],
        situation=standing,
        foot=cursor_foot("ROW", s.sel + 1, CLAIM_ROWS),
    )


# ---------- the acceptance evidence ----------


def _short_digest(digest: str) -> str:
    bare = digest.split(":", 1)[-1]
    return f"{bare[:4]}…{bare[-3:]}"


def evidence_rows(model: AcceptanceBundleView) -> list[tuple[str, CriterionRow, str]]:
    """Return one ``(evidence key, criterion, kind)`` row per piece of evidence the bundle cites.

    A criterion that cites nothing still has a row, so an unmet criterion is never hidden.
    """
    rows: list[tuple[str, CriterionRow, str]] = []
    for criterion in model.criteria:
        kinds = " · ".join(criterion.evidence_kinds) or NO_VALUE
        keys = criterion.evidence_keys or (f"{TRUTH['unavailable'].unicode} none",)
        rows.extend((key, criterion, kinds) for key in keys)
    return rows


def _criteria_counts(model: AcceptanceBundleView) -> str:
    met = sum(1 for c in model.criteria if c.passed)
    failed = sum(1 for c in model.criteria if not c.passed and c.evidence_keys)
    unmet = len(model.criteria) - met - failed
    return f"{met} met · {unmet} unmet · {failed} failed · 0 denied"


def acceptance_copy(model: AcceptanceBundleView, row: int) -> str:
    """Return what ``y`` copies on cursor row ``row``: that evidence key, or the digest."""
    rows = evidence_rows(model)
    if 0 <= row < len(rows):
        return rows[row][0]
    return model.bundle_digest or f"{UNKNOWN} unknown"


def render_acceptance(view: View, model: AcceptanceBundleView) -> list[str]:
    """Return the acceptance evidence overlay over the Milestone's sealed bundle."""
    s, w = view.session, view.w
    subject = s.ov_subject or next((r.key for r in model.rows), f"{UNKNOWN} unknown")
    if model.bundle_digest is None:
        body = [lab("BUNDLE", f"{TRUTH['unavailable'].unicode} none sealed · nothing to quote yet")]
        return decision_frame(
            view,
            name="acceptance",
            subject=subject,
            context=f"no bundle is sealed for {subject}",
            body=[*body, thin(w), lab("NOT", "Reading evidence does not accept the milestone.")],
            keys=[_ESC_BACK],
        )
    rows = evidence_rows(model)
    s.sel = max(0, min(s.sel, len(rows) - 1))
    when = f"{model.sealed_at:%b %d}" if model.sealed_at else NO_VALUE
    body = [_EVIDENCE_TABLE.head(["EVIDENCE", "CRITERION", "KIND", "WHEN"])]
    body += [
        _EVIDENCE_TABLE.row([key, criterion.step_id, kind, when], i == s.sel, w)
        for i, (key, criterion, kind) in enumerate(rows)
    ]
    body += [
        thin(w),
        lab("CRITERIA", _criteria_counts(model)),
        lab("DENIED", "none · no criterion is denied to your class"),
        thin(w),
        lab("NOT", "Reading evidence does not accept the milestone."),
    ]
    return decision_frame(
        view,
        name="acceptance",
        subject=subject,
        context=f"at digest {_short_digest(model.bundle_digest)} · quotable at this exact revision",
        body=body,
        keys=[("↑↓", "row"), ("y", "copy"), ("Esc", "back")],
        foot=cursor_foot("EVIDENCE", s.sel + 1, len(rows)),
    )


# ---------- the readiness matrix ----------

_SIGNAL_TABLE = Grid([18, 12, 18, 0])


def _release_status(value: str | None) -> ReleaseStatus | None:
    lowered = (value or "").lower()
    return ReleaseStatus(lowered) if lowered in ReleaseStatus._value2member_map_ else None


def _signal_state(signal: ReadinessSignal) -> str:
    if signal.state.state is TruthState.KNOWN:
        return signal.state.value or ""
    return f"{UNKNOWN} unknown"


def _remedy(signal: ReadinessSignal) -> str:
    """Return what would state ``signal``: nothing for a stated one, else why it is silent."""
    if signal.state.state is TruthState.KNOWN:
        return NO_VALUE
    return signal.state.missing_reason or NO_VALUE


def _red_signal(model: ReleaseReadinessView) -> str | None:
    return next(
        (
            x.name
            for x in model.signals
            if x.state.state is TruthState.KNOWN and x.state.value != READINESS_MET
        ),
        None,
    )


def release_status(model: ReleaseReadinessView) -> ReleaseStatus | None:
    """Return the state the release's own row states, or ``None`` when it states none."""
    release = next((r for r in model.rows if r.collection is Epoch2Collection.RELEASE), None)
    field = release.fields.get("status") if release is not None else None
    return _release_status(field.value if field and field.state is TruthState.KNOWN else None)


def readiness_situation(model: ReleaseReadinessView) -> tuple[str, Situation]:
    """Return the release's key and its situation, read off the release's own row."""
    release = next((r for r in model.rows if r.collection is Epoch2Collection.RELEASE), None)
    if release is None:
        # no release is cut, which is a state of the tree rather than a gap in the read
        return "no release", Situation(NO_RELEASE, "a release is cut from accepted Milestones")
    status = release_status(model)
    if status is None:
        return release.key, Situation(
            f"{UNKNOWN} unknown · the release states no status", "the release states its status"
        )
    head = model.approval.head_sha[:7] if model.approval else None
    return release.key, release_situation(status, head=head, red=_red_signal(model), cause=None)


def render_readiness(view: View, model: ReleaseReadinessView) -> list[str]:
    """Return the readiness matrix over the Release's readiness view."""
    s, w = view.session, view.w
    key, situation = readiness_situation(model)
    as_of = f"revision {model.source_cursor}"
    signals = model.signals
    s.sel = max(0, min(s.sel, max(0, len(signals) - 1)))
    cut = any(r.collection is Epoch2Collection.RELEASE for r in model.rows)
    members = [r for r in model.rows if r.collection is Epoch2Collection.MILESTONE] if cut else []
    none_held = NO_RELEASE if not cut else "none held"
    body = [
        lab(
            "MEMBERS" if i == 0 else "",
            f"{m.key} · {(m.fields['status'].value or '').lower() or UNKNOWN} · as of {as_of}",
        )
        for i, m in enumerate(members)
    ] or [lab("MEMBERS", f"{TRUTH['unavailable'].unicode} {none_held} · as of {as_of}")]
    body += [thin(w), _SIGNAL_TABLE.head(["READINESS", "STATE", "EVIDENCE", "FRESH · REMEDY"])]
    for i, signal in enumerate(signals):
        remedy = f"{as_of} · {_remedy(signal)}"
        cells = [signal.name, _signal_state(signal), signal.evidence or NO_VALUE, remedy]
        body.append(_SIGNAL_TABLE.row(cells, i == s.sel, w))
    if signals:
        # a narrow cell cuts the remedy, so the focused signal's is also stated whole
        focus = signals[s.sel]
        body.append(lab("REMEDY", f"{focus.name} · {_remedy(focus)}"))
    approval = model.approval if release_status(model) in APPROVED_ONWARD else None
    body += [
        thin(w),
        lab(
            "APPROVAL",
            f"granted at head {approval.head_sha[:7]} · still exact"
            if approval
            else f"{TRUTH['unavailable'].unicode} none · awaiting approval",
        ),
        cont("if that head moves this approval is invalidated and the release"),
        cont("returns to DRAFT with the cause named"),
        lab("PUBLISH", f"{UNKNOWN} unknown · no producer states a publication target yet"),
    ]
    return decision_frame(
        view,
        name="readiness",
        subject=key
        if situation.name == NO_RELEASE
        else f"{key} · {situation.name.split(' · ')[0]}",
        context="every member and signal under one as-of instant",
        body=body,
        keys=[("↑↓", "row"), _ESC_BACK],
        situation=situation,
        foot=cursor_foot("SIGNAL", s.sel + 1, len(signals)),
    )
