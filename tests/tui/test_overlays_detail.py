"""Tests for the C06 ``DetailModal`` + the drill-in seam (P26-W19).

Two layers: pure :func:`resolve_detail` resolution (wave / backlog /
fallback) without Textual, and Pilot-driven routing of the W17 widget
selection messages (:class:`BacklogTable.RowActivated` /
:class:`RoadmapTree.WaveSelected`) into a mounted DetailModal.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
from textual.widgets import Markdown, Static, TabbedContent, TabPane

from eawf.kernel.spec.common import (
    CriterionSpec,
    OracleTier,
    QualityDimension,
    grandfather_criterion,
    tier_label,
)
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    DispatchNote,
    EffortBucket,
)
from eawf.kernel.state.models import DispatchAnnotation, SessionAttempt, State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    store_kind_for_role,
)
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.detail import (
    _TAB_LABEL_TEXT,
    DetailCard,
    DetailModal,
    render_file_tree,
    resolve_detail,
    tab_label,
)
from eawf.surfaces.tui.snapshot import capture_screen_text
from eawf.surfaces.tui.snapshot.pilot_harness import settle_screen
from eawf.surfaces.tui.widgets import sigils
from eawf.surfaces.tui.widgets.backlog_table import BacklogTable
from eawf.surfaces.tui.widgets.eu_bar import EMPTY_STATE
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree
from eawf.surfaces.tui.widgets.sigils import Sigil
from eawf.workflow.agent_report.rollup import AgentReportRow

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_BACKLOG = _FIXTURES / "07-decisions-and-backlog.json"


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _state_with_bucketed_wave(bucket: EffortBucket = EffortBucket.L) -> tuple[State, str]:
    """Return a state whose single wave carries *bucket*, plus that wave id.

    The committed fixtures leave ``effort_bucket`` unset, so the size-bar
    paths need a wave with a populated bucket. Rebuilds the state with one
    wave's bucket overridden (the model is frozen — ``model_copy`` makes
    the edit) so the resolver renders a real size bar.
    """
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    bucketed = state.waves[wave_id].model_copy(update={"effort_bucket": bucket})
    new_waves = dict(state.waves)
    new_waves[wave_id] = bucketed
    return state.model_copy(update={"waves": new_waves}), wave_id


def _state_with_attempted_wave() -> tuple[State, str]:
    """Return a state whose wave carries two dispatch attempts."""
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    attempted = state.waves[wave_id].model_copy(
        update={
            "sessions": {
                1: SessionAttempt(
                    attempt=1,
                    runtime="codex",
                    session_id="sess-1",
                    session_log_handle="urn:eawf:v1:session-log:codex:sess-1",
                    started_at=now,
                    ended_at=now + timedelta(minutes=5),
                    exit_status=0,
                    input_tokens=10,
                    output_tokens=5,
                ),
                2: SessionAttempt(
                    attempt=2,
                    runtime="claude-code",
                    session_id="sess-2",
                    session_log_handle="urn:eawf:v1:session-log:claude-code:sess-2",
                    started_at=now + timedelta(minutes=6),
                    ended_at=now + timedelta(minutes=10),
                    exit_status=9,
                    input_tokens=20,
                    output_tokens=7,
                    cache_creation_input_tokens=3,
                    cache_read_input_tokens=4,
                ),
            },
            "dispatch_history": [
                DispatchAnnotation(
                    attempt=1,
                    note=DispatchNote.FRESH_DISPATCH,
                    runtime_to="codex",
                    occurred_at=now,
                ),
                DispatchAnnotation(
                    attempt=2,
                    note=DispatchNote.SWITCH_ON_ERROR,
                    runtime_from="codex",
                    runtime_to="claude-code",
                    occurred_at=now + timedelta(minutes=6),
                    reason="timeout",
                ),
            ],
        }
    )
    new_waves = dict(state.waves)
    new_waves[wave_id] = attempted
    return state.model_copy(update={"waves": new_waves}), wave_id


def _report(wave_id: str, *, attempt: int, verdict: AgentReportVerdict) -> AgentReportRow:
    generated_at = datetime(2026, 5, 27, 12, attempt, tzinfo=UTC)
    body = ExecutorReportBody(
        role="executor",
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="attempt completed",
        wave_id=wave_id,
        outcome="done",
    )
    report_id = f"AR-executor-{wave_id}-{attempt:02d}"
    header = AgentReportHeader(
        report_id=report_id,
        role=AgentSessionRole.EXECUTOR,
        session_id=f"SES-{attempt}",
        scope_id=wave_id,
        base_id=wave_id,
        attempt=attempt,
        runtime="codex",
        generated_at=generated_at,
        summary=body.summary,
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(AgentSessionRole.EXECUTOR),
        scope_id=wave_id,
        created_at=generated_at,
        updated_at=None,
        summary=body.summary,
        payload=payload.model_dump(mode="json"),
    )
    return AgentReportRow(
        envelope=envelope,
        payload=payload,
        store_kind=store_kind_for_role(AgentSessionRole.EXECUTOR).value,
    )


# --------------------------------------------------------------------------
# resolve_detail — wave / backlog / fallback
# --------------------------------------------------------------------------


def test_resolve_detail_wave_card() -> None:
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    card = resolve_detail(state, wave_id)
    assert card.title == f"wave {wave_id}"
    row_labels = {label for label, _ in card.rows}
    assert {"id", "iter", "title", "status"} <= row_labels


def test_resolve_detail_backlog_card() -> None:
    state = _load(_BACKLOG)
    item_id = next(iter(state.backlog))
    card = resolve_detail(state, item_id)
    assert card.title == f"backlog {item_id}"
    row_labels = {label for label, _ in card.rows}
    assert {"id", "title", "priority", "status"} <= row_labels


def test_resolve_detail_unknown_id_returns_fallback() -> None:
    state = _load(_PHASE_ITER_WAVE)
    card = resolve_detail(state, "DOES-NOT-EXIST")
    assert card.title == "detail DOES-NOT-EXIST"
    assert ("id", "DOES-NOT-EXIST") in card.rows


def test_resolve_detail_none_state_returns_fallback() -> None:
    card = resolve_detail(None, "X")
    assert card.title == "detail X"
    assert any(label == "note" for label, _ in card.rows)


def test_resolve_detail_wave_includes_success_criteria_rows() -> None:
    state = _load(_PHASE_ITER_WAVE)
    # Find a wave that carries success criteria, if any.
    wave = next(
        (w for w in state.waves.values() if w.success_criteria),
        None,
    )
    if wave is None:
        return
    card = resolve_detail(state, wave.id)
    # Criteria live in their own ``criteria`` group (the criteria tab), not
    # the overview ``rows`` group; the ``criterion`` rows carry the ``.text``.
    criterion_rows = [value for label, value in card.criteria if label == "criterion"]
    assert criterion_rows == [c.text for c in wave.success_criteria]
    assert "criterion" not in {label for label, _ in card.rows}


# --------------------------------------------------------------------------
# Criteria tab — full typed CriterionSpec projection (P30-I07-W01)
# --------------------------------------------------------------------------


def _typed_criterion() -> CriterionSpec:
    """A populated, authored (non-grandfathered) :class:`CriterionSpec`.

    Carries a real oracle tier, evidence_kind, and measurable_signal so the
    criteria tab can surface all three typed fields.
    """
    return CriterionSpec(
        id="CR-01",
        text="The d tab renders the full typed criterion.",
        kind="render",
        acceptance_style="binary",
        evidence_kind="jury",
        quality_dimension=QualityDimension.INTERACTION_CAPABILITY,
        measurable_signal="the criteria tab shows tier, evidence_kind, and signal rows",
        oracle_tier=OracleTier.T7_JURY,
    )


def _state_with_criteria(criteria: list[CriterionSpec]) -> tuple[State, str]:
    """Return a state whose single wave carries *criteria*, plus its id.

    The committed fixture leaves ``success_criteria`` empty; the frozen
    model is rebuilt via ``model_copy`` so the resolver renders the typed
    criteria rows.
    """
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    with_criteria = state.waves[wave_id].model_copy(update={"success_criteria": criteria})
    new_waves = dict(state.waves)
    new_waves[wave_id] = with_criteria
    return state.model_copy(update={"waves": new_waves}), wave_id


def test_resolve_detail_typed_criterion_renders_tier_evidence_signal() -> None:
    """A typed criterion surfaces its tier label, evidence_kind, and signal."""
    criterion = _typed_criterion()
    state, wave_id = _state_with_criteria([criterion])
    card = resolve_detail(state, wave_id)
    criteria = dict(card.criteria)
    assert criteria["criterion"] == criterion.text
    assert criteria["tier"] == tier_label(OracleTier.T7_JURY)
    assert criteria["tier"] == "T7 jury"
    assert criteria["evidence"] == criterion.evidence_kind
    assert criteria["signal"] == criterion.measurable_signal
    # No grandfathered marker for an authored criterion.
    assert "grandfathered" not in {label for label, _ in card.criteria}


def test_resolve_detail_typed_criterion_without_tier_omits_tier_row() -> None:
    """An authored criterion with no computed tier omits the tier row only.

    Boundary: the evidence_kind + signal rows still render; the tier row is
    suppressed rather than fabricated when ``oracle_tier`` is unset.
    """
    criterion = _typed_criterion().model_copy(update={"oracle_tier": None})
    state, wave_id = _state_with_criteria([criterion])
    card = resolve_detail(state, wave_id)
    labels = [label for label, _ in card.criteria]
    assert "tier" not in labels
    criteria = dict(card.criteria)
    assert criteria["evidence"] == criterion.evidence_kind
    assert criteria["signal"] == criterion.measurable_signal


def test_resolve_detail_grandfathered_criterion_has_no_tier_badge() -> None:
    """A grandfathered legacy criterion shows text + marker and NO tier badge."""
    criterion = grandfather_criterion("ship the legacy chassis end to end", index=1)
    state, wave_id = _state_with_criteria([criterion])
    card = resolve_detail(state, wave_id)
    labels = [label for label, _ in card.criteria]
    criteria = dict(card.criteria)
    assert criteria["criterion"] == criterion.text
    assert "grandfathered" in labels
    # No fabricated tier badge: the legacy criterion carries no authored tier.
    assert "tier" not in labels


def test_resolve_detail_mixed_criteria_only_typed_carries_tier() -> None:
    """A mixed wave: the typed criterion carries a tier, the legacy one does not."""
    typed = _typed_criterion()
    legacy = grandfather_criterion("legacy short", index=2)
    state, wave_id = _state_with_criteria([typed, legacy])
    card = resolve_detail(state, wave_id)
    # Exactly one tier row (the typed criterion) and one grandfathered marker.
    labels = [label for label, _ in card.criteria]
    assert labels.count("tier") == 1
    assert labels.count("grandfathered") == 1
    assert labels.count("criterion") == 2


def test_detail_modal_typed_criterion_paints_tier_evidence_signal() -> None:
    """The d tab paints the tier label, evidence_kind, and signal (Pilot)."""

    async def body() -> None:
        criterion = _typed_criterion()
        state, wave_id = _state_with_criteria([criterion])
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            card = resolve_detail(state, wave_id)
            modal = DetailModal(card)
            app.push_screen(modal)
            await pilot.pause()
            await app.workers.wait_for_complete()
            tabs = modal.query_one(TabbedContent)
            tabs.active = "detail-tab-criteria"
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = capture_screen_text(app)
            assert "T7 jury" in rendered
            assert criterion.evidence_kind in rendered
            assert criterion.measurable_signal in rendered

    asyncio.run(body())


def test_detail_modal_grandfathered_criterion_paints_marker_no_tier() -> None:
    """The d tab paints the legacy text + marker and no tier badge (Pilot)."""

    async def body() -> None:
        criterion = grandfather_criterion("ship the legacy chassis end to end", index=1)
        state, wave_id = _state_with_criteria([criterion])
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            card = resolve_detail(state, wave_id)
            modal = DetailModal(card)
            app.push_screen(modal)
            await pilot.pause()
            await app.workers.wait_for_complete()
            tabs = modal.query_one(TabbedContent)
            tabs.active = "detail-tab-criteria"
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = capture_screen_text(app)
            assert "grandfathered" in rendered
            assert criterion.text in rendered
            # No fabricated tier badge is painted for the legacy criterion.
            assert "T1 " not in rendered
            assert "T7 " not in rendered

    asyncio.run(body())


def test_resolve_detail_wave_detail_includes_attempt_timeline() -> None:
    state, wave_id = _state_with_attempted_wave()
    card = resolve_detail(state, wave_id)
    # The attempt rollup folds into the evidence tab group (the wave d-tab
    # is the NarrativeBundle markdown, not aligned rollup rows).
    rows = dict(card.evidence)

    assert rows["attempts"] == "2 attempts, 1 retry, 0 blocked, 49 tokens"
    assert rows["error kinds"] == "none"
    timeline = rows["attempt timeline"]
    lines = [line.strip() for line in timeline.splitlines() if line.strip()]
    assert lines[0].split() == [
        "att",
        "runtime",
        "started",
        "ended",
        "exit",
        "retry",
        "blocked",
        "tokens",
    ]
    assert len(lines[1].split()) == 8
    assert len(lines[2].split()) == 8
    assert "switch" in lines[2]
    assert "34" in lines[2]


def test_resolve_detail_wave_attempt_rollup_uses_reports_and_errors() -> None:
    state, wave_id = _state_with_attempted_wave()
    card = resolve_detail(
        state,
        wave_id,
        reports=(
            _report(wave_id, attempt=1, verdict=AgentReportVerdict.PASS),
            _report(wave_id, attempt=2, verdict=AgentReportVerdict.BLOCKED),
        ),
        error_kind_by_attempt={2: ("timeout", "network_error", "timeout")},
    )
    rows = dict(card.evidence)

    assert rows["attempts"] == "2 attempts, 1 retry, 1 blocked, 49 tokens"
    assert rows["error kinds"] == "network_error=1, timeout=2"
    assert "yes" in rows["attempt timeline"]


# --------------------------------------------------------------------------
# render_file_tree — wave file_scopes render as a collapsed tree (review fix)
# --------------------------------------------------------------------------


def test_render_file_tree_single_path_collapses_to_one_row() -> None:
    assert render_file_tree(["src/eawf/validate/"]) == "src/eawf/validate"


def test_render_file_tree_empty_is_blank() -> None:
    assert render_file_tree([]) == ""


def test_render_file_tree_groups_shared_prefix_and_collapses_tails() -> None:
    tree = render_file_tree(
        [
            "src/eawf/dispatch/**",
            "src/eawf/surfaces/cli/commands/wave_dispatch.py",
            "tests/dispatch/**",
        ]
    )
    assert tree == (
        "src/eawf/\n  dispatch/**\n  surfaces/cli/commands/wave_dispatch.py\ntests/dispatch/**"
    )


def test_wave_card_files_row_renders_as_tree() -> None:
    """A wave's ``files`` detail row carries the indented tree, not a CSV list."""
    state = _load(_PHASE_ITER_WAVE)
    wave = next((w for w in state.waves.values() if w.file_scopes), None)
    if wave is None:
        return
    card = resolve_detail(state, wave.id)
    files_value = next((value for label, value in card.rows if label == "files"), None)
    assert files_value is not None
    # The tree drops below the label (leading newline) and indents each row.
    assert files_value.startswith("\n")
    assert ", ".join(wave.file_scopes) not in files_value


def test_dispatch_tab_label_is_removed() -> None:
    """The modal no longer carries a dedicated dispatch-prompt tab."""
    assert "dp" not in _TAB_LABEL_TEXT


def test_tab_label_text_is_the_five_chassis_ids() -> None:
    """The chassis carries exactly the five cosmic-terminal tab ids."""
    assert list(_TAB_LABEL_TEXT) == [
        "overview",
        "criteria",
        "gates",
        "evidence",
        "runtime",
    ]


def test_tab_label_carries_chrome_glyph_prefix() -> None:
    """Each pane label is the chrome / sigil glyph then the word, per mode."""
    # Unicode column: overview triple-bar, gate lozenge, runtime dollar,
    # evidence closed-circle, criteria right-pointing marker.
    overview_u = sigils.chrome("overview", mode="unicode")
    gate_u = sigils.chrome("gate", mode="unicode")
    runtime_u = sigils.chrome("runtime", mode="unicode")
    evidence_u = sigils.glyph(Sigil.CLOSED, mode="unicode")
    assert tab_label("overview", mode="unicode") == f"{overview_u} overview"
    assert tab_label("gates", mode="unicode") == f"{gate_u} gates"
    assert tab_label("runtime", mode="unicode") == f"{runtime_u} runtime"
    assert tab_label("evidence", mode="unicode") == f"{evidence_u} evidence"
    assert tab_label("criteria", mode="unicode").endswith(" criteria")
    # ASCII column flips to the deconflicted fallbacks.
    assert tab_label("overview", mode="ascii") == "= overview"
    assert tab_label("gates", mode="ascii") == "[] gates"
    assert tab_label("evidence", mode="ascii") == "@ evidence"
    assert tab_label("criteria", mode="ascii") == "> criteria"
    assert tab_label("runtime", mode="ascii") == "$ runtime"


# --------------------------------------------------------------------------
# resolve_detail — iter / phase cards (new in W05)
# --------------------------------------------------------------------------


def test_resolve_detail_iter_card() -> None:
    state = _load(_PHASE_ITER_WAVE)
    iter_id = next(iter(state.iters))
    card = resolve_detail(state, iter_id)
    assert card.title == f"iter {iter_id}"
    row_labels = {label for label, _ in card.rows}
    assert {"id", "phase", "title", "status", "waves"} <= row_labels


def test_resolve_detail_phase_card() -> None:
    state = _load(_PHASE_ITER_WAVE)
    phase_id = next(iter(state.phases))
    card = resolve_detail(state, phase_id)
    assert card.title == f"phase {phase_id}"
    row_labels = {label for label, _ in card.rows}
    assert {"id", "scope", "title", "status", "iters", "waves"} <= row_labels


def test_resolve_detail_iter_runtime_has_completion_bar() -> None:
    state = _load(_PHASE_ITER_WAVE)
    iter_id = next(iter(state.iters))
    card = resolve_detail(state, iter_id)
    runtime_labels = {label for label, _ in card.runtime}
    assert "completion" in runtime_labels


def test_resolve_detail_phase_runtime_has_completion_bar() -> None:
    state = _load(_PHASE_ITER_WAVE)
    phase_id = next(iter(state.phases))
    card = resolve_detail(state, phase_id)
    runtime_labels = {label for label, _ in card.runtime}
    assert "completion" in runtime_labels


def test_resolve_detail_wave_runtime_has_size_bar() -> None:
    state, wave_id = _state_with_bucketed_wave(EffortBucket.L)
    card = resolve_detail(state, wave_id)
    size_values = [value for label, value in card.runtime if label == "size"]
    assert size_values
    assert "L" in size_values[0]
    assert EMPTY_STATE not in size_values[0]


def test_resolve_detail_wave_runtime_eu_tokens_empty_state() -> None:
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    card = resolve_detail(state, wave_id)
    runtime = dict(card.runtime)
    # Honest-empty: a no-runtime wave shows the sentinel, never 0.00/0.00.
    assert runtime["eu"] == EMPTY_STATE
    assert runtime["tokens"] == EMPTY_STATE
    assert "0.00" not in runtime["eu"]
    assert "0.00" not in runtime["tokens"]


def test_resolve_detail_iter_runtime_eu_tokens_empty_state() -> None:
    state = _load(_PHASE_ITER_WAVE)
    iter_id = next(iter(state.iters))
    card = resolve_detail(state, iter_id)
    runtime = dict(card.runtime)
    assert runtime["eu"] == EMPTY_STATE
    assert runtime["tokens"] == EMPTY_STATE


def test_resolve_detail_wave_has_narrative_preview() -> None:
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    card = resolve_detail(state, wave_id)
    assert card.detail_markdown is not None
    assert "## What" in card.detail_markdown
    assert "## Validation" in card.detail_markdown


def test_resolve_detail_iter_has_no_narrative_preview() -> None:
    state = _load(_PHASE_ITER_WAVE)
    iter_id = next(iter(state.iters))
    card = resolve_detail(state, iter_id)
    assert card.detail_markdown is None


def test_resolve_detail_backlog_has_no_runtime_or_narrative() -> None:
    state = _load(_BACKLOG)
    item_id = next(iter(state.backlog))
    card = resolve_detail(state, item_id)
    assert card.runtime == ()
    assert card.detail_markdown is None


# --------------------------------------------------------------------------
# DetailModal._present_tabs — only data-bearing tabs are built
# --------------------------------------------------------------------------


def test_present_tabs_overview_always_present() -> None:
    card = DetailCard(title="t", rows=(("id", "x"),))
    assert DetailModal._present_tabs(card) == ("overview",)


def test_present_tabs_wave_includes_runtime_and_criteria() -> None:
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    card = resolve_detail(state, wave_id)
    tabs = DetailModal._present_tabs(card)
    assert "overview" in tabs
    assert "runtime" in tabs
    assert "evidence" in tabs


def test_present_tabs_skips_empty_gates() -> None:
    # No wave carries gate rows yet (I06 fills them), so no gates tab.
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    card = resolve_detail(state, wave_id)
    assert card.gates == ()
    assert "gates" not in DetailModal._present_tabs(card)


def test_present_tabs_skips_empty_criteria() -> None:
    # A wave with no success criteria yields no criteria tab.
    card = DetailCard(title="t", rows=(("id", "x"),))
    assert "criteria" not in DetailModal._present_tabs(card)


def test_present_tabs_order_follows_chassis_sequence() -> None:
    card = DetailCard(
        title="t",
        rows=(("id", "x"),),
        criteria=(("criterion", "ship it"),),
        gates=(("gate", "pytest"),),
        evidence=(("attempt 1", "fresh"),),
        runtime=(("size", "M"),),
        detail_markdown="body",
    )
    assert DetailModal._present_tabs(card) == (
        "overview",
        "criteria",
        "gates",
        "evidence",
        "runtime",
    )


# --------------------------------------------------------------------------
# DetailCard contract
# --------------------------------------------------------------------------


def test_detail_card_is_frozen() -> None:
    card = DetailCard(title="t", rows=())
    try:
        card.title = "x"  # type: ignore[misc]
    except AttributeError, TypeError:
        return
    raise AssertionError("DetailCard should be frozen")


# --------------------------------------------------------------------------
# Drill-in seam — W17 messages route to a DetailModal (Pilot)
# --------------------------------------------------------------------------


def test_backlog_row_activated_opens_detail_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_BACKLOG)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            item_id = next(iter(app.state.backlog))  # type: ignore[union-attr]
            table = app.screen.query_one(BacklogTable)
            table.post_message(BacklogTable.RowActivated(item_id))
            await settle_screen(pilot)
            assert isinstance(app.screen, DetailModal)
            assert item_id in app.export_screenshot()

    asyncio.run(body())


def test_roadmap_wave_selected_opens_detail_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            wave_id = next(iter(app.state.waves))  # type: ignore[union-attr]
            tree = app.screen.query_one(RoadmapTree)
            tree.post_message(RoadmapTree.WaveSelected(wave_id))
            await settle_screen(pilot)
            assert isinstance(app.screen, DetailModal)
            assert wave_id in app.export_screenshot()

    asyncio.run(body())


def test_detail_modal_esc_closes() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            wave_id = next(iter(app.state.waves))  # type: ignore[union-attr]
            tree = app.screen.query_one(RoadmapTree)
            tree.post_message(RoadmapTree.WaveSelected(wave_id))
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


# --------------------------------------------------------------------------
# DetailModal tabs + bars (Pilot, W05)
# --------------------------------------------------------------------------


def test_detail_modal_iter_shows_completion_bar() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            iter_id = next(iter(app.state.iters))  # type: ignore[union-attr]
            card = resolve_detail(app.state, iter_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, DetailModal)
            tabs = modal.query_one(TabbedContent)
            tabs.active = "detail-tab-runtime"
            await pilot.pause()
            rendered = capture_screen_text(app)
            # The closed/total count suffix of the completion bar is visible.
            assert "0/1" in rendered

    asyncio.run(body())


def test_detail_modal_wave_shows_size_bar() -> None:
    async def body() -> None:
        state, wave_id = _state_with_bucketed_wave(EffortBucket.L)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            card = resolve_detail(state, wave_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, DetailModal)
            tabs = modal.query_one(TabbedContent)
            tabs.active = "detail-tab-runtime"
            await pilot.pause()
            rendered = capture_screen_text(app)
            # The size-bar row carries the bucket label.
            assert "L" in rendered

    asyncio.run(body())


def test_detail_modal_metrics_show_empty_state_for_eu_tokens() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            wave_id = next(iter(app.state.waves))  # type: ignore[union-attr]
            card = resolve_detail(app.state, wave_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            tabs = app.screen.query_one(TabbedContent)
            tabs.active = "detail-tab-runtime"
            await pilot.pause()
            rendered = capture_screen_text(app)
            assert EMPTY_STATE in rendered

    asyncio.run(body())


def test_detail_modal_tab_cycles_forward_and_back() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            iter_id = next(iter(app.state.iters))  # type: ignore[union-attr]
            card = resolve_detail(app.state, iter_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, DetailModal)
            tabs = modal.query_one(TabbedContent)
            # Lands on the overview tab by default.
            assert tabs.active == "detail-tab-overview"
            await pilot.press("tab")
            await pilot.pause()
            after_tab = tabs.active
            assert after_tab != "detail-tab-overview"
            await pilot.press("shift+tab")
            await pilot.pause()
            assert tabs.active == "detail-tab-overview"

    asyncio.run(body())


def test_detail_modal_arrows_do_not_switch_tabs() -> None:
    """Arrow keys keep their scroll role — they must not cycle the tabs."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            iter_id = next(iter(app.state.iters))  # type: ignore[union-attr]
            card = resolve_detail(app.state, iter_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            tabs = app.screen.query_one(TabbedContent)
            before = tabs.active
            for key in ("down", "up", "left", "right"):
                await pilot.press(key)
                await pilot.pause()
            assert tabs.active == before

    asyncio.run(body())


def test_detail_modal_iter_opens_with_tabs() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            iter_id = next(iter(app.state.iters))  # type: ignore[union-attr]
            card = resolve_detail(app.state, iter_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            assert isinstance(app.screen, DetailModal)
            assert iter_id in app.export_screenshot()

    asyncio.run(body())


# --------------------------------------------------------------------------
# DetailModal tab hotkeys + markdown detail + label escape + bucket text (W13)
# --------------------------------------------------------------------------


def _full_card() -> DetailCard:
    """A card carrying every section so all five chassis tabs are built."""
    return DetailCard(
        title="wave P00-I01-W01",
        rows=(("id", "P00-I01-W01"), ("title", "demo"), ("status", "closed")),
        criteria=(("criterion", "ship the chassis"),),
        gates=(("gate", "pytest"),),
        evidence=(("attempt 1", "fresh (claude)"),),
        runtime=(("size", "M"),),
        detail_markdown="# heading\n\n**bold** body",
    )


def test_detail_modal_hotkey_activates_matching_tab() -> None:
    """``o``/``c``/``g``/``v``/``r`` jump straight to their pane when present."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailModal(_full_card()))
            await pilot.pause()
            tabs = app.screen.query_one(TabbedContent)
            for key, expected in (
                ("c", "detail-tab-criteria"),
                ("g", "detail-tab-gates"),
                ("v", "detail-tab-evidence"),
                ("r", "detail-tab-runtime"),
                ("o", "detail-tab-overview"),
            ):
                await pilot.press(key)
                await pilot.pause()
                assert tabs.active == expected

    asyncio.run(body())


def test_detail_modal_hotkey_absent_tab_is_noop() -> None:
    """A hotkey for a tab the card lacks leaves the active tab unchanged.

    Boundary: a card carrying only the ``overview`` tab (field rows, no
    criteria / gates / evidence / runtime). Pressing ``g`` (and the other
    absent-tab keys) must be a no-op rather than raising or switching to a
    missing pane.
    """

    async def body() -> None:
        card = DetailCard(title="t", rows=(("id", "x"),))
        assert DetailModal._present_tabs(card) == ("overview",)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailModal(card))
            await pilot.pause()
            tabs = app.screen.query_one(TabbedContent)
            assert tabs.active == "detail-tab-overview"
            for key in ("c", "g", "v", "r"):
                await pilot.press(key)
                await pilot.pause()
                assert tabs.active == "detail-tab-overview"

    asyncio.run(body())


def test_detail_modal_footer_omits_tab_hotkey_list() -> None:
    """The footer hint drops the per-letter list — the tab labels carry them.

    Each tab pane label carries its own chrome-glyph mnemonic, so repeating
    a per-letter list in the footer hint is redundant. The footer keeps only
    the cycle + close affordances.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailModal(_full_card()))
            await pilot.pause()
            hint = str(app.screen.query_one(".detail-hint", Static).render())
            assert "o/c/g/v/r" not in hint
            assert "Tab/Shift+Tab cycle" in hint
            assert "Esc close" in hint

    asyncio.run(body())


def test_detail_modal_wave_overview_pane_mounts_narrative_markdown() -> None:
    """The wave ``overview`` tab body is Markdown, not aligned field rows."""

    async def body() -> None:
        state, wave_id = _state_with_bucketed_wave(EffortBucket.L)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            card = resolve_detail(state, wave_id)
            assert card.detail_markdown is not None
            modal = DetailModal(card)
            app.push_screen(modal)
            await pilot.pause()
            pane = modal.query_one("#detail-tab-overview")
            assert pane.query(Markdown)
            # The narrative preview is not emitted as aligned detail rows.
            assert not pane.query("Static.detail-row")

    asyncio.run(body())


def test_detail_modal_wave_size_row_is_bucket_text() -> None:
    """The wave ``size`` runtime row is the plain bucket string (no bar glyphs)."""
    state, wave_id = _state_with_bucketed_wave(EffortBucket.M)
    card = resolve_detail(state, wave_id)
    size_values = [value for label, value in card.runtime if label == "size"]
    assert size_values == ["M"]
    assert "#" not in size_values[0]
    assert "-" not in size_values[0]
    assert EMPTY_STATE not in size_values[0]


def test_detail_modal_row_value_with_bracket_renders() -> None:
    """A field value containing ``[`` is escaped, so markup cannot break.

    Without escaping, a ``[`` in a value (e.g. a file-glob criterion) opens
    an unterminated content-markup tag and Textual raises on render.
    """

    async def body() -> None:
        card = DetailCard(
            title="t",
            rows=(("files", "src/**/[ab]*.py"), ("note", "[unterminated")),
        )
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailModal(card))
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, DetailModal)
            # Render succeeded (no markup-parse exception) and the literal
            # bracket survives in the painted screen text.
            rendered = capture_screen_text(app)
            assert "[ab]" in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Backlog description block (W13)
# --------------------------------------------------------------------------


def _state_with_described_backlog(
    description: str = "Long-form purpose for this backlog item",
) -> tuple[State, str]:
    """Return a state whose first backlog item carries *description*, plus its id.

    The committed backlog fixture leaves ``description`` unset, so the
    description-block paths need an item with a populated field. The model
    is frozen — ``model_copy`` makes the edit.
    """
    state = _load(_BACKLOG)
    item_id = next(iter(state.backlog))
    described = state.backlog[item_id].model_copy(update={"description": description})
    new_backlog = dict(state.backlog)
    new_backlog[item_id] = described
    return state.model_copy(update={"backlog": new_backlog}), item_id


def test_resolve_detail_backlog_card_includes_description_under_title() -> None:
    state, item_id = _state_with_described_backlog("Why this item exists")
    card = resolve_detail(state, item_id)
    labels = [label for label, _ in card.rows]
    assert "description" in labels
    # The description sits directly under the title row.
    assert labels.index("description") == labels.index("title") + 1
    assert ("description", "Why this item exists") in card.rows


def test_resolve_detail_backlog_card_omits_description_when_absent() -> None:
    state = _load(_BACKLOG)
    item_id = next(iter(state.backlog))
    card = resolve_detail(state, item_id)
    labels = {label for label, _ in card.rows}
    assert "description" not in labels


def test_detail_modal_backlog_shows_description_block() -> None:
    async def body() -> None:
        state, item_id = _state_with_described_backlog("Persist cost rows regardless")
        app = EaApp(scope="repo", state_path=_BACKLOG)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            card = resolve_detail(state, item_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, DetailModal)
            rendered = capture_screen_text(app)
            assert "description" in rendered
            assert "Persist cost rows regardless" in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Wave card divergence (W55) — two waves under one phase render distinct d-tabs
# --------------------------------------------------------------------------


def _state_with_two_distinct_waves() -> tuple[State, str, str]:
    """Return a state carrying two waves under one phase with distinct intents.

    The phase + iter under :data:`_PHASE_ITER_WAVE` already has one wave;
    this helper appends a second wave to the same iter and decorates both
    with their own :class:`IntentBrief`, so the rendered ``d`` tabs can be
    compared for divergence.
    """
    state = _load(_PHASE_ITER_WAVE)
    existing_id = next(iter(state.waves))
    existing = state.waves[existing_id]
    second_id = "P01-I01-W02"
    decorated_first = existing.model_copy(
        update={
            "intent": IntentBrief(
                problem="Wave detail body re-runs the phase rollup.",
                desired_outcome="Wave d-tab quotes the wave's own intent.",
                planned_steps=["Add wave-specific narrative builder"],
                risks=["NarrativeBundle shape change breaks PR tests"],
                priority_rationale="Pre-ship blocker for the v0.4 PR text.",
            ),
        }
    )
    second_wave = existing.model_copy(
        update={
            "id": second_id,
            "title": "Wave card divergence",
            "intent": IntentBrief(
                problem="Sibling waves still share the phase rollup body.",
                desired_outcome="Each sibling wave d-tab is provably distinct.",
                planned_steps=["Dispatch by scope kind in build_narrative"],
                risks=["Sibling waves still collide on rollup"],
                priority_rationale="Operator audit ranked divergence above polish.",
            ),
        }
    )
    new_waves = dict(state.waves)
    new_waves[existing_id] = decorated_first
    new_waves[second_id] = second_wave
    return state.model_copy(update={"waves": new_waves}), existing_id, second_id


def test_resolve_detail_two_waves_under_one_phase_have_distinct_d_tabs() -> None:
    """The wave ``d`` tab Markdown for two sibling waves diverges (pure path)."""
    state, first_id, second_id = _state_with_two_distinct_waves()
    first = resolve_detail(state, first_id)
    second = resolve_detail(state, second_id)

    assert first.detail_markdown is not None
    assert second.detail_markdown is not None
    assert first.detail_markdown != second.detail_markdown
    # Each preview quotes its own wave's intent in the What block.
    assert "Wave detail body re-runs" in first.detail_markdown
    assert "Sibling waves still share" in second.detail_markdown
    # Each card carries its wave id in the title metadata.
    assert first.title == f"wave {first_id}"
    assert second.title == f"wave {second_id}"


def test_detail_modal_two_waves_render_distinct_d_tab_bodies() -> None:
    """Two sibling waves under one phase paint different ``d`` tab bodies."""

    async def body() -> None:
        state, first_id, second_id = _state_with_two_distinct_waves()
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()

            first_modal = DetailModal(resolve_detail(state, first_id))
            app.push_screen(first_modal)
            await pilot.pause()
            await app.workers.wait_for_complete()
            first_rendered = capture_screen_text(app)
            first_modal.dismiss(None)
            await pilot.pause()
            await app.workers.wait_for_complete()

            second_modal = DetailModal(resolve_detail(state, second_id))
            app.push_screen(second_modal)
            await pilot.pause()
            await app.workers.wait_for_complete()
            second_rendered = capture_screen_text(app)

            assert first_rendered != second_rendered
            assert first_id in first_rendered
            assert second_id in second_rendered
            # The two sibling intents land in their own renders.
            assert "Sibling waves still share" in second_rendered
            assert "Wave detail body re-runs" in first_rendered

    asyncio.run(body())


_GOLDEN_NARRATIVE = Path(__file__).resolve().parents[1] / "golden" / "narrative"


def _wave_narrative_markdown(state: State, wave_id: str) -> str:
    """Return the wave ``d`` tab markdown body for *wave_id* (pure path)."""
    card = resolve_detail(state, wave_id)
    assert card.detail_markdown is not None
    return card.detail_markdown


def test_wave_narrative_golden_first_wave() -> None:
    """Golden-pinned first-wave markdown body (W55 divergence anchor)."""
    state, first_id, _ = _state_with_two_distinct_waves()
    actual = _wave_narrative_markdown(state, first_id) + "\n"
    golden_path = _GOLDEN_NARRATIVE / "wave_first.md"
    _assert_golden(golden_path, actual)


def test_wave_narrative_golden_second_wave() -> None:
    """Golden-pinned second-wave markdown body — must differ from the first."""
    state, _, second_id = _state_with_two_distinct_waves()
    actual = _wave_narrative_markdown(state, second_id) + "\n"
    golden_path = _GOLDEN_NARRATIVE / "wave_second.md"
    _assert_golden(golden_path, actual)


def _assert_golden(golden_path: Path, actual: str) -> None:
    """Compare *actual* to *golden_path* with the standard regen toggle."""
    import os

    from eawf.surfaces.tui.snapshot.pilot_harness import SNAPSHOT_REGEN_ENV

    if os.environ.get(SNAPSHOT_REGEN_ENV) == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return
    expected = golden_path.read_text(encoding="utf-8")
    assert actual == expected, f"golden drift at {golden_path}"


# --------------------------------------------------------------------------
# Five-tab chassis acceptance (P30-I02-W24)
# --------------------------------------------------------------------------


def _five_tab_card() -> DetailCard:
    """A wave card carrying overview / criteria / evidence / runtime (no gates).

    Mirrors a real grandfathered wave: an identity overview, a legacy
    success-criterion ``.text`` row, a dispatch attempt for evidence, and
    the honest-empty runtime rows. ``gates`` is left empty so the chassis
    renders no gates tab (the W24 acceptance boundary).
    """
    return DetailCard(
        title="wave P30-I02-W24",
        rows=(
            ("id", "P30-I02-W24"),
            ("title", "five-tab chassis"),
            ("status", "in_progress"),
        ),
        criteria=(("criterion", "the five tabs render with chrome glyphs"),),
        evidence=(("attempt 1", "fresh (claude)"),),
        runtime=(("size", "M"), ("eu", EMPTY_STATE), ("tokens", EMPTY_STATE)),
        detail_markdown=None,
    )


def test_chassis_five_tab_ids_present_with_chrome_labels() -> None:
    """The modal builds the four populated chassis tabs with chrome labels."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = DetailModal(_five_tab_card())
            app.push_screen(modal)
            await pilot.pause()
            tabs = modal.query_one(TabbedContent)
            mode = app.render_mode
            # overview + criteria + evidence + runtime are present (no gates).
            present_ids = {pane.id for pane in tabs.query(TabPane)}
            assert present_ids == {
                "detail-tab-overview",
                "detail-tab-criteria",
                "detail-tab-evidence",
                "detail-tab-runtime",
            }
            # Each present tab's chrome-glyph word is painted in the tab bar.
            rendered = capture_screen_text(app)
            for tab_id in ("overview", "criteria", "evidence", "runtime"):
                word = tab_label(tab_id, mode=mode).split(" ", 1)[1]
                assert word in rendered

    asyncio.run(body())


def test_chassis_no_gates_wave_renders_no_gates_tab() -> None:
    """A wave with no gate rows renders no gates tab and ``g`` is a no-op."""

    async def body() -> None:
        card = _five_tab_card()
        assert card.gates == ()
        assert "gates" not in DetailModal._present_tabs(card)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = DetailModal(card)
            app.push_screen(modal)
            await pilot.pause()
            tabs = modal.query_one(TabbedContent)
            assert "detail-tab-gates" not in {pane.id for pane in tabs.query(TabPane)}
            before = tabs.active
            await pilot.press("g")
            await pilot.pause()
            # ``g`` jumps nowhere: the gates tab does not exist.
            assert tabs.active == before

    asyncio.run(body())


def test_chassis_hotkeys_jump_to_each_present_tab() -> None:
    """``o``/``c``/``v``/``r`` jump to each present tab; ``Tab`` cycles."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = DetailModal(_five_tab_card())
            app.push_screen(modal)
            await pilot.pause()
            tabs = modal.query_one(TabbedContent)
            # Opens on overview.
            assert tabs.active == "detail-tab-overview"
            for key, expected in (
                ("c", "detail-tab-criteria"),
                ("v", "detail-tab-evidence"),
                ("r", "detail-tab-runtime"),
                ("o", "detail-tab-overview"),
            ):
                await pilot.press(key)
                await pilot.pause()
                assert tabs.active == expected
            # Tab cycles forward off the overview tab; Shift+Tab returns.
            await pilot.press("tab")
            await pilot.pause()
            assert tabs.active != "detail-tab-overview"
            await pilot.press("shift+tab")
            await pilot.pause()
            assert tabs.active == "detail-tab-overview"

    asyncio.run(body())


def test_chassis_runtime_tab_shows_empty_sentinel_not_fabricated_zero() -> None:
    """The no-runtime wave's runtime tab paints the sentinel, not 0.00/0.00."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = DetailModal(_five_tab_card())
            app.push_screen(modal)
            await pilot.pause()
            tabs = modal.query_one(TabbedContent)
            tabs.active = "detail-tab-runtime"
            await pilot.pause()
            rendered = capture_screen_text(app)
            assert EMPTY_STATE in rendered
            assert "0.00/0.00" not in rendered
            assert "0.00" not in rendered

    asyncio.run(body())


def test_chassis_overview_status_row_is_sigil_prefixed() -> None:
    """The overview ``status:`` row prepends the lifecycle sigil glyph."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # A non-wave (iter) card renders its rows directly (no markdown),
            # so the sigil-prefixed status row is paint-visible.
            iter_id = next(iter(app.state.iters))  # type: ignore[union-attr]
            card = resolve_detail(app.state, iter_id)
            modal = DetailModal(card)
            app.push_screen(modal)
            await pilot.pause()
            rendered = capture_screen_text(app)
            status_value = dict(card.rows)["status"]
            expected_glyph = sigils.glyph(
                Sigil.PENDING if status_value in {"planned", "pending", "open"} else Sigil.RUNNING,
                mode=app.render_mode,
            )
            # The bare status word is sigil-prefixed in the painted overview.
            assert f"{expected_glyph} {status_value}" in rendered

    asyncio.run(body())
