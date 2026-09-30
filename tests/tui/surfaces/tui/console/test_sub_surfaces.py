"""Entity sub-surfaces: opened only from a parent, drawn at every size, walked back on Escape.

Requirement rows proved here, by id:

- ``UI-050``: an entity sub-surface is a route whose subject is one record of a parent
  entity. The set is closed by the route registry and counted from it; each takes its
  parent's group, is never a ``g`` or palette destination, renders at all three sizes,
  and climbs to its parent on Escape. ``notifications`` has no subject, so it is a global
  diagnostics route rather than a sub-surface.
- ``UI-051``: entering a rung on the Evidence route opens ``evidence.digest``, whose
  unknown rung says unknown rather than failed, whose unrun rung names what it awaits,
  whose keybar promises only copy and close, and whose copy is the claim URN with the
  rung fragment rather than the design's ``urn:eawf:`` spelling.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eawf.kernel.state.enums import GateReceiptResult
from eawf.surfaces.tui.console import attention as att
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx, go
from eawf.surfaces.tui.console.registry import REGISTRY, RouteGroup
from eawf.surfaces.tui.console.renderers import copy_target, render_route
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console import test_enter_opened_cards as cards
from tests.tui.surfaces.tui.console.test_route_registry_closure import SUB_SURFACES

SIZES = ((80, 24), (120, 30), (160, 40))

#: The sub-surfaces the packet row names, each beside the parent it is about.
NAMED: dict[str, str] = {
    "evidence.digest": "evidence",
    "settings.stack": "settings",
    "merge.conflict": "git.pr",
    "export": "run.detail",
    "receipt": "task.detail",
    "campaign.step": "campaign",
    "campaign.artifact": "campaign",
}

#: A subject for each sub-surface that needs one to open.
SUBJECTS: dict[str, str] = {"receipt": "EVT-2218"}


class _Host:
    """The dispatcher's host: a held clock and a quit nobody asks for."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """End nothing; the tests never quit."""


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the golden fixture the console is built over."""
    return load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")


def _ctx(session: Session, fixture: Fixture) -> Ctx:
    return Ctx(session=session, fixture=fixture, host=_Host(), w=80, h=24)


def _at(route: str) -> Session:
    session = Session()
    session.route = route
    session.subj_id = SUBJECTS.get(route)
    return session


# ---------- UI-050: the sub-surface set and its rules ----------


def test_ui050_the_sub_surface_set_is_the_registrys_and_names_each_parent() -> None:
    """The set is counted from the registry rows, never kept beside them."""
    assert dict(SUB_SURFACES) == NAMED
    assert len(SUB_SURFACES) == 7


@pytest.mark.parametrize(("route", "parent"), sorted(NAMED.items()))
def test_ui050_a_sub_surface_takes_its_parents_group_and_is_never_a_global_door(
    route: str, parent: str
) -> None:
    """No ``g`` letter and no palette row: a sub-surface needs its subject."""
    spec = REGISTRY.by_id[route]

    assert spec.group is REGISTRY.by_id[parent].group
    assert route not in REGISTRY.go_map.values()
    assert route not in REGISTRY.route_list
    assert REGISTRY.escapes[route].route == parent


def test_ui050_notifications_is_a_global_diagnostics_route_not_a_sub_surface() -> None:
    """It has no subject, so it opens from the go prefix and the palette."""
    assert "notifications" not in SUB_SURFACES
    assert REGISTRY.by_id["notifications"].group is RouteGroup.DIAGNOSTICS
    assert "notifications" in REGISTRY.go_map.values()
    assert "notifications" in REGISTRY.route_list


@pytest.mark.parametrize("route", sorted(NAMED))
@pytest.mark.parametrize(("w", "h"), SIZES)
def test_ui050_every_sub_surface_renders_at_all_three_sizes(
    fixture: Fixture, route: str, w: int, h: int
) -> None:
    """Each frame is exactly the size asked for, keybar last."""
    rows = render_route(View(session=_at(route), fixture=fixture, w=w, h=h))

    assert len(rows) == h
    assert rows[-1].strip()


@pytest.mark.parametrize(("route", "parent"), sorted(NAMED.items()))
def test_ui050_escape_on_an_empty_back_stack_climbs_to_the_parent(
    fixture: Fixture, route: str, parent: str
) -> None:
    """A sub-surface opened by a typed id still walks back to the entity it is about."""
    session = _at(route)

    dispatch(_ctx(session, fixture), "Escape", False)

    assert session.route == parent


def test_ui050_opening_pushes_the_back_stack_and_escape_returns_to_the_opener(
    fixture: Fixture,
) -> None:
    """Opened from its parent, Escape returns to the record it was opened from."""
    session = _at("evidence")
    ctx = _ctx(session, fixture)

    assert go(ctx, "evidence.digest", "Enter")
    dispatch(ctx, "Escape", False)

    assert session.route == "evidence"


# ---------- UI-051: the rung card ----------


def _rung(fixture: Fixture, index: int, *, w: int = 120, h: int = 30) -> str:
    session = _at("evidence.digest")
    session.rung = index
    return "\n".join(render_route(View(session=session, fixture=fixture, w=w, h=h)))


def test_ui051_an_unknown_rung_says_unknown_not_failed_in_the_same_fields(
    fixture: Fixture,
) -> None:
    """The card is never empty for a rung with no outcome."""
    body = _rung(fixture, 2)

    assert "no outcome yet — unknown, not failed" in body
    for label in ("CHECK", "OVER", "FOUND", "AS OF", "RECORD", "MEANS"):
        assert label in body


def test_ui051_a_rung_not_yet_run_names_the_rung_it_awaits(fixture: Fixture) -> None:
    body = _rung(fixture, 3)

    assert "awaiting rung 3" in body


def test_ui051_a_passing_rung_holds_and_does_not_certify(fixture: Fixture) -> None:
    body = _rung(fixture, 0)

    assert "This rung holds — it does not certify the claim on its own." in body


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_ui051_the_keybar_promises_only_copy_and_close(fixture: Fixture, w: int, h: int) -> None:
    session = _at("evidence.digest")
    rows = render_route(View(session=session, fixture=fixture, w=w, h=h))

    assert rows[-1].strip() == "y copy   Esc close"


@pytest.mark.parametrize(("index", "n"), [(0, 1), (3, 4)])
def test_ui051_copy_is_the_claim_urn_with_the_rung_fragment(
    fixture: Fixture, index: int, n: int
) -> None:
    """The first and last rung: the fragment is the rung number, never a design spelling."""
    session = _at("evidence.digest")
    session.rung = index

    copied = copy_target(session, fixture)

    assert copied == f"eawf://{fixture.scope}/{fixture.scope}/_/claim/CLM-0004#rung-{n}"
    assert not copied.startswith("urn:eawf:")


def test_ui051_a_rung_past_the_end_opens_the_last_rung(fixture: Fixture) -> None:
    """The off-by-one boundary: an overshot cursor clamps rather than failing."""
    session = _at("evidence.digest")
    session.rung = 99

    assert copy_target(session, fixture).endswith("#rung-4")


def _rows(
    fixture: Fixture, route: str, *, w: int = 120, h: int = 30, **fields: object
) -> list[str]:
    session = _at(route)
    for name, value in fields.items():
        setattr(session, name, value)
    return render_route(View(session=session, fixture=fixture, w=w, h=h))


# ---------- UI-054: the export card is the plan, and the plan is the preview ----------


def test_ui_054_the_plan_names_every_part_its_inclusion_and_its_size(fixture: Fixture) -> None:
    text = "\n".join(_rows(fixture, "export"))
    for part in ("timeline", "usage and cost", "transcript", "secrets", "sandbox decisions"):
        assert f"{part}  " in text
    secrets = next(row for row in text.split("\n") if " secrets " in row)
    assert " never " in secrets and "redacted by policy" in secrets
    assert "nothing leaves the machine" in text


def test_ui_054_a_native_plan_states_secrets_never_and_the_digest_it_reads() -> None:
    model = cards._view("export")
    rows = cards._frame(model, subject="RUN-538453eb")
    secrets = next(row for row in rows if " secrets " in row)
    assert " never " in secrets
    assert any(f"Plain text at digest {model.digest}" in row for row in rows)


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_ui_054_the_keybar_reads_part_export_cancel(fixture: Fixture, w: int, h: int) -> None:
    assert _rows(fixture, "export", w=w, h=h)[-1].strip() == "↑↓ part   Enter export   Esc cancel"


def test_ui_054_no_row_offers_a_key_that_toggles_inclusion(fixture: Fixture) -> None:
    assert "space includes" not in "\n".join(_rows(fixture, "export"))
    session = _at("export")
    dispatch(_ctx(session, fixture), " ", False)
    assert session.trace is not None and session.trace.endswith("→ unclaimed")


def test_ui_054_enter_opens_no_second_card_and_answers_in_the_rack() -> None:
    session = cards._press_export_enter(cards._view("export"))
    assert (session.route, session.overlay) == ("export", None)
    assert [toast.title for toast in session.toasts] == ["report"]


@pytest.mark.xfail(
    strict=True,
    reason="the export seam takes the report in memory and holds no tree root to hand "
    "write_run_report, so Enter writes no file and names no path; the plan's parts count "
    "no purged range either",
)
def test_ui_054_enter_writes_the_report_file_and_answers_with_its_path() -> None:
    session = cards._press_export_enter(cards._view("export"))
    assert ".ea/local/" in session.toasts[0].text


# ---------- UI-055: a receipt opens as an immutable card over its RCP key ----------


def test_ui_055_the_card_states_what_was_checked_where_it_is_kept_and_the_verdict() -> None:
    model = cards._view("receipt", receipts=(cards._receipt(),))
    text = "\n".join(cards._frame(model, subject=cards.RECEIPT_KEY))
    assert "G-01 · CR-01" in text
    assert "pass · started" in text
    assert f"at head {cards.HEAD_SHA}" in text
    assert "A receipt is immutable: a later check writes a new one." in text


def test_ui_055_a_failed_check_reads_as_its_result_never_as_a_zero() -> None:
    model = cards._view("receipt", receipts=(cards._receipt(result=GateReceiptResult.FAIL),))
    card = model.card(cards.RECEIPT_KEY)
    assert card is not None and card.result == "fail"
    result = next(row for row in cards._frame(model, subject=cards.RECEIPT_KEY) if "RESULT" in row)
    assert "fail" in result and " 0 " not in result


def test_ui_055_the_receipt_key_is_the_rcp_public_key() -> None:
    assert re.fullmatch(r"RCP-\d{4,}", cards.RECEIPT_KEY)
    model = cards._view("receipt", receipts=(cards._receipt(),))
    assert f"RECEIPT · {cards.RECEIPT_KEY}" in "\n".join(
        cards._frame(model, subject=cards.RECEIPT_KEY)
    )


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_ui_055_the_card_has_no_cursor_and_its_keybar_reads_copy_back(
    fixture: Fixture, w: int, h: int
) -> None:
    before = _rows(fixture, "receipt", w=w, h=h)
    assert before[-1].strip() == "y copy   Esc back"
    session = _at("receipt")
    dispatch(_ctx(session, fixture), "ArrowDown", False)
    # the card has no row to move to, so the arrow changes nothing it draws
    assert render_route(View(session=session, fixture=fixture, w=w, h=h)) == before


@pytest.mark.xfail(
    strict=True,
    reason="the Task frame's PROOF row lists its receipts as text, oldest first, under the "
    "design's EVT spelling; no producer hands the Task detail its RCP receipts as cursor stops",
)
def test_ui_055_a_record_lists_every_receipt_newest_first(fixture: Fixture) -> None:
    session = _at("task.detail")
    session.subj_id = "EAWF-0042"
    proof = next(
        row
        for row in render_route(View(session=session, fixture=fixture, w=120, h=30))
        if "PROOF" in row
    )
    assert "RCP-" in proof and proof.index("EVT-2214") < proof.index("EVT-2210")


# ---------- UI-056: a step and an artifact are sub-surfaces of the Campaign ----------


def _step(fixture: Fixture, index: int) -> list[str]:
    return _rows(fixture, "campaign.step", cam_step=index)


def test_ui_056_a_step_names_what_it_waits_on_its_runner_and_its_spend(fixture: Fixture) -> None:
    text = "\n".join(_step(fixture, 2))
    assert " WAITS ON    Steps 1 and 2 are finished." in text
    assert " RUNNER      RUN-cc623ab9 · claude" in text
    assert " SPENT       2.4h" in text


def test_ui_056_a_running_step_states_its_spend_against_the_bound_never_a_fraction(
    fixture: Fixture,
) -> None:
    rows = _step(fixture, 4)
    spent = next(row for row in rows if row.startswith(" SPENT"))
    assert "1.3h of ≤2h" in spent
    assert "%" not in "\n".join(rows)
    assert "⋯ running" in "\n".join(rows) and "►" not in "\n".join(rows)
    assert rows[-1].strip() == "↑↓ line   y copy   Esc back"


def test_ui_056_a_blocked_step_names_its_blocker_and_shows_nothing_not_zero(
    fixture: Fixture,
) -> None:
    rows = _step(fixture, 5)
    text = "\n".join(rows)
    assert "○ blocked · never started" in text
    assert " WAITS ON    Step 5 is not finished yet." in text
    spent = next(row for row in rows if row.startswith(" SPENT"))
    assert spent.split()[-1] != "0"


def test_ui_056_a_step_past_the_last_opens_the_last(fixture: Fixture) -> None:
    last = len(fixture.registers.cam_steps) - 1
    assert _step(fixture, last + 5) == _step(fixture, last)


def test_ui_056_escape_returns_to_the_campaign_row_it_was_opened_from(fixture: Fixture) -> None:
    for route in ("campaign.step", "campaign.artifact"):
        session = _at("campaign")
        ctx = _ctx(session, fixture)
        assert go(ctx, route, "Enter")
        dispatch(ctx, "Escape", False)
        assert session.route == "campaign"


def test_ui_056_the_campaign_view_carries_its_steps_and_artifacts() -> None:
    from eawf.kernel.projection.campaign import CampaignStepView, CampaignView

    assert {"steps", "artifacts"} <= set(CampaignView.model_fields)
    assert {"state", "waits_on", "runner_ref"} <= set(CampaignStepView.model_fields)


# ---------- UI-070: the artifact card's read model and its render form ----------


def test_ui_070_the_card_states_source_and_record_and_counts_its_hidden_lines(
    fixture: Fixture,
) -> None:
    rows = _rows(fixture, "campaign.artifact")
    text = "\n".join(rows)
    assert " SOURCE     drift-report.md · markdown · 2.1 KB · written 13:58 by step 4" in text
    assert " RECORD     Kept with CAM-0001 · sha256 b41e…7c" in text
    assert "… 2 lines below" in text
    assert "the console renders it, it does not rewrite it" in text


def test_ui_070_the_artifact_card_view_is_a_typed_read_model() -> None:
    from eawf.kernel.projection.campaign import ArtifactCardView

    assert set(ArtifactCardView.model_fields) == {
        "artifact_ref",
        "file_name",
        "media_kind",
        "size_bytes",
        "written_at",
        "written_by",
        "digest",
        "kept_with",
        "ordinal_of_total",
        "lines",
    }


# ---------- UI-071: a notice row opens its detail, never a consequence card ----------


def test_ui_071_enter_on_a_notice_row_opens_its_detail_not_a_consequence_card(
    fixture: Fixture,
) -> None:
    session = _at("attention")
    rows = att.attn_list(session, fixture)
    session.sel = next(i for i, row in enumerate(rows) if att.is_notice(row))
    notice = rows[session.sel].id
    dispatch(_ctx(session, fixture), "Enter", False)
    assert session.overlay is None
    assert session.route == "notifications"
    assert session.trace is not None and notice in session.trace


def test_ui_071_a_notice_accepts_only_snooze_and_resolve(fixture: Fixture) -> None:
    notice = next(row for row in fixture.proto.attention if att.is_notice(row))
    assert att.verbs_for(notice) == ["z", "v"]
    assert att.verbs_for(None) == ["a", "x", "z", "v"]


@pytest.mark.xfail(
    strict=True,
    reason="no NoticeDetailView is produced: the read-model kind is named, but no "
    "projection builds the notice detail's fields for the notifications record form",
)
def test_ui_071_the_notice_detail_is_a_typed_read_model() -> None:
    from eawf.kernel.projection import read_models

    assert hasattr(read_models, "NoticeDetailView")
