"""The acceptance routes draw read models, and an opened card says what it was read at.

Four routes leave the prototype registers here. The Milestone renders the sealed bundle an
acceptance is given against and the approval bound to the exact head that bundle names;
the Release renders the candidate's membership over the readiness the records in hand
state; a receipt opens as a card copied out of the frozen proof record; and the export
card states the plan of the view it would report, at that view's digest.

Two honesty rules are the point of the suite. An approval names the digest it was given
to, so an approval of other bytes is not drawn against a bundle it did not approve --
otherwise a later revision would inherit an earlier consent. And a readiness signal no
producer observes renders the unknown truth token naming why, never a cell an operator
would read as a gate that passed.

An export is a read. Taking a report leaves the document it was read from exactly as it
was and carries the digest of the view, so two reports at one cursor are one answer.

The epoch-1 mode is the mode the tracked golden contract replays, and binding costs it
nothing: every key each of the four footers advertises still resolves to a handler.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.acceptance import MilestoneAcceptanceBundle
from eawf.kernel.delivery.receipts import (
    ProofFreshnessKey,
    ProofReceipt,
    RevisionBinding,
    RevisionRefKind,
    canonical_digest,
)
from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    ROUTE_READ_MODELS,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE, RECONNECT_METHOD_TEMPLATE
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.state.enums import GateReceiptResult
from eawf.runtime.daemon.methods.projection import (
    EXPORT_REPORT_METHOD,
    ROUTE_READ_METHODS,
    ROUTE_RECONNECT_METHODS,
    ExportParams,
)
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import KEY_NAMES
from eawf.surfaces.tui.console.keymap import route_keys
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.milestone import NO_APPROVAL, NO_BUNDLE
from eawf.surfaces.tui.console.renderers.receipt import NO_RECEIPT
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session
from eawf.workflow.delivery.acceptance import AcceptanceApproval
from eawf.workflow.projection.acceptance import (
    ACCEPTANCE_ROUTES,
    GATE_SIGNALS,
    RC_GATE_REASON,
    READINESS_MET,
    READINESS_SIGNALS,
    STATED_SIGNALS,
    AcceptanceBundleView,
    ReceiptCardView,
    ReleaseReadinessView,
    RunReportPlanView,
    build_acceptance_view,
    export_report,
)

#: When the probe records and projections are stamped. The digest does not cover the
#: stamp; a fixed clock only keeps this suite's output reproducible.
AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

#: The scope every probe projection is built for.
SCOPE = "EAWF"

#: The container slot every probe URN is spelled under.
CONTAINER = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"

#: The Milestone the probe bundle is sealed for.
MILESTONE_URN = f"{CONTAINER}/milestone/MLS-0030"

#: The commit and tree the probe approval is bound to.
HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40

#: Where the tracked prototype registers live.
FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"

#: The recorded coverage grid, read to check the four rows moved out of the hole list.
MANIFEST = Path(__file__).resolve().parents[4] / "fixtures/console/coverage-manifest.json"

#: The receipt the card suite opens.
RECEIPT_KEY = "RCP-0001"

#: One row per collection the four routes touch, so a route's own register is never empty
#: by accident and a route binding two registers is told apart from one binding one.
DOCUMENT: dict[str, Any] = {
    "milestone": {
        "MLS-0030": {
            "urn": f"urn:eawf:{SCOPE}:milestone:MLS-0030",
            "revision": 4,
            "status": "COMPLETED",
        },
    },
    "batch": {
        "BAT-0007": {
            "urn": f"urn:eawf:{SCOPE}:batch:BAT-0007",
            "revision": 2,
            "status": "MERGED",
        },
    },
    "release": {
        "REL-0001": {
            "urn": f"urn:eawf:{SCOPE}:release:REL-0001",
            "revision": 1,
            "status": "CANDIDATE",
        },
    },
    "receipt": {
        RECEIPT_KEY: {
            "urn": f"urn:eawf:{SCOPE}:receipt:{RECEIPT_KEY}",
            "revision": 1,
            "status": "RECORDED",
        },
    },
    "run": {
        "RUN-9e3779b1": {
            "urn": f"urn:eawf:{SCOPE}:run:RUN-9e3779b1",
            "revision": 7,
            "status": "RUNNING",
        },
    },
}


def _projection(route: str, *, cursor: int = 41208, document: Any = None) -> RouteProjection:
    """Return one route's projection over the probe document at ``cursor``."""
    return build_route_projection(
        route=route,
        document=DOCUMENT if document is None else document,
        cursor=cursor,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _bundle(*, passed: bool = True, revision: int = 1) -> MilestoneAcceptanceBundle:
    """Return one sealed acceptance bundle, with its single step passing or open."""
    step: dict[str, Any] = {
        "step_id": "AS-01",
        "passed": passed,
        "observation": "the install completed and reported the version",
        "evidence_kinds": ["artifact"],
        "evidence_refs": [f"{CONTAINER}/evidence/EVD-0002"] if passed else [],
    }
    return MilestoneAcceptanceBundle.model_validate(
        {
            "milestone_ref": MILESTONE_URN,
            "revision": revision,
            "accepted_binding": {
                "head_sha": HEAD_SHA,
                "tree_sha": TREE_SHA,
                "contract_digest": canonical_digest("contract"),
                "policy_revision": 1,
                "evidence_digest": canonical_digest("evidence"),
            },
            "steps": [step],
            "sealed_at": AT.isoformat(),
        }
    )


def _approval(*, digest: str | None = None) -> AcceptanceApproval:
    """Return the approval given to ``digest``, defaulting to the probe bundle's own."""
    bundle = _bundle()
    return AcceptanceApproval(
        milestone_ref=MILESTONE_URN,  # type: ignore[arg-type]
        bundle_revision=bundle.revision,
        approved_digest=digest or bundle.digest(),
        resolved_by={"principal_kind": "human", "principal_id": "OP-0001"},  # type: ignore[arg-type]
        receipt_ref=f"{CONTAINER}/evidence/EVD-0001",  # type: ignore[arg-type]
        accepted_binding=bundle.accepted_binding,
        approved_at=AT,
    )


def _receipt(*, result: GateReceiptResult = GateReceiptResult.PASS) -> ProofReceipt:
    """Return one proof receipt the card opens."""
    binding = RevisionBinding(
        repository_ref=f"{CONTAINER}/repository/REP-EAWF",  # type: ignore[arg-type]
        ref_kind=RevisionRefKind.INTEGRATION,
        head_sha=HEAD_SHA,
        tree_sha=TREE_SHA,
        parent_sha=None,
        batch_ref=f"{CONTAINER}/batch/BAT-0007",  # type: ignore[arg-type]
        integration_generation=2,
        manifest_digest=canonical_digest("manifest"),
        criteria_digest=canonical_digest(["CR-01"]),
        policy_digest=canonical_digest("policy"),
        environment_digest=canonical_digest("environment"),
        bound_at=AT,
    )
    freshness = ProofFreshnessKey(
        revision_binding=binding,
        criterion_digest=canonical_digest("CR-01"),
        gate_digest=canonical_digest("G-01"),
        selector_digest=canonical_digest("selector"),
        policy_digest=canonical_digest("policy"),
        runner_digest=canonical_digest("runner"),
        environment_digest=canonical_digest("environment"),
    )
    return ProofReceipt(
        id=RECEIPT_KEY,
        scope_id="EAWF-0042",
        gate_id="G-01",
        criterion_ids=("CR-01",),
        evidence_kind="deterministic",
        freshness=freshness,
        freshness_key=freshness.digest(),
        result=result,
        exit_status=0 if result is GateReceiptResult.PASS else 1,
        started_at=AT,
        ended_at=AT,
    )


def _view(route: str, **kwargs: Any) -> Any:
    """Return the acceptance read model of ``route`` over the probe document."""
    projection_kwargs = {k: kwargs.pop(k) for k in ("cursor", "document") if k in kwargs}
    return build_acceptance_view(_projection(route, **projection_kwargs), **kwargs)


def _fixture() -> Fixture:
    """Return the tracked prototype registers the epoch-1 mode renders from."""
    return load_fixture(FIXTURE_ROOT)


def _frame(model: Any, *, subject: str | None = None, width: int = 120) -> list[str]:
    """Return the console frame ``model`` renders."""
    session = Session()
    session.route = model.route
    session.subj_id = subject
    view = View(session=session, fixture=_fixture(), w=width, h=30, projection=model)
    return render_route(view)


def _app(route: str, **kwargs: Any) -> ConsoleApp:
    """Return a console bound to a seam already holding ``route``'s projection."""
    seam = ProjectionSeam(route=route, scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._projection = _projection(route)
    app = ConsoleApp(_fixture(), FakeClock(), seam=seam, **kwargs)
    app.session.route = route
    return app


class _Host:
    """The dispatcher's host: a held clock and a quit that records it was asked."""

    def __init__(self) -> None:
        self.quits = 0
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record that the console was asked to end."""
        self.quits += 1


#: The dispatcher name of every key the bar prints under another name.
_DISPATCHER_NAME: dict[str, str] = {name: key for key, name in KEY_NAMES.items()}


def _advertised(route: str, fixture: Fixture) -> list[str]:
    """Return the dispatcher key name of every key the route's footer advertises."""
    session = Session()
    session.route = route
    names: list[str] = []
    for entry in route_keys(session, fixture, route):
        for token in entry.keys:
            glyphs = list(token) if all(ch in _DISPATCHER_NAME for ch in token) else [token]
            names.extend(_DISPATCHER_NAME.get(glyph, glyph) for glyph in glyphs)
    return names


# ---------- the console composes the read model it draws ----------


@pytest.mark.parametrize(
    ("route", "kind"),
    [
        ("milestone", AcceptanceBundleView),
        ("release", ReleaseReadinessView),
        ("receipt", ReceiptCardView),
        ("export", RunReportPlanView),
    ],
)
def test_the_console_composes_this_routes_read_model_from_the_seam(
    route: str, kind: type[Any]
) -> None:
    """The production call site: the app turns the held projection into the route's model."""
    model = _app(route).route_view()
    assert isinstance(model, kind)
    assert model.route == route
    assert model.read_model is ROUTE_READ_MODELS[route]


def test_the_console_carries_its_bundle_and_approval_into_the_milestone_model() -> None:
    """The records the console was given reach the view, and no other route's."""
    app = _app("milestone", acceptance_bundle=_bundle(), acceptance_approval=_approval())
    model = app.route_view()
    assert isinstance(model, AcceptanceBundleView)
    assert model.bundle_digest == _bundle().digest()
    assert model.approval is not None
    assert model.approval.head_sha == HEAD_SHA


def test_the_console_carries_its_receipts_into_the_receipt_model() -> None:
    """A card is opened off the receipts the console holds, never off the registers."""
    model = _app("receipt", proof_receipts=(_receipt(),)).route_view()
    assert isinstance(model, ReceiptCardView)
    assert [card.key for card in model.cards] == [RECEIPT_KEY]


def test_the_console_holds_no_read_model_for_another_route() -> None:
    """The seam carries one route; another route's frame falls back to epoch one."""
    app = _app("milestone")
    app.session.route = "release"
    assert app.route_view() is None


# ---------- the four routes and the verbs that serve them ----------


def test_the_family_names_exactly_the_routes_this_wave_binds() -> None:
    """The family table is the statement of what left the prototype registers."""
    assert ACCEPTANCE_ROUTES == ("milestone", "release", "receipt", "export")


@pytest.mark.parametrize("route", ACCEPTANCE_ROUTES)
def test_every_acceptance_route_has_both_projection_verbs(route: str) -> None:
    """A route a console draws natively is one the daemon both reads and reconnects."""
    assert READ_METHOD_TEMPLATE.format(route=route) in ROUTE_READ_METHODS
    assert RECONNECT_METHOD_TEMPLATE.format(route=route) in ROUTE_RECONNECT_METHODS
    assert ROUTE_COLLECTIONS[route]


@pytest.mark.parametrize("route", ACCEPTANCE_ROUTES)
def test_every_acceptance_route_is_listed_bound_in_the_recorded_grid(route: str) -> None:
    """The four routes moved out of the hole list in the same commit that bound them."""
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    row = next(item for item in document["routes"] if item["route"] == route)
    assert row["binding"] == "bound"
    assert "bound_by" not in row


@pytest.mark.parametrize("route", ACCEPTANCE_ROUTES)
def test_every_count_is_the_rows_of_a_register_the_route_binds(route: str) -> None:
    """A count is taken off the view's own rows, one per bound collection."""
    model = _view(route)
    assert set(model.counts) == {c.value for c in ROUTE_COLLECTIONS[route]}
    assert sum(model.counts.values()) == len(model.rows)


def test_an_empty_register_counts_zero_and_draws_no_row() -> None:
    """A register the route binds and that holds nothing counts zero, honestly."""
    model = _view("release", document={})
    assert model.counts == {"release": 0, "milestone": 0}
    assert model.rows == ()
    assert model.index_of("REL-0001") is None


def test_a_single_row_register_counts_one() -> None:
    """The off-by-one boundary below a two-row register."""
    two = {
        "release": {
            **DOCUMENT["release"],
            "REL-0002": {
                "urn": f"urn:eawf:{SCOPE}:release:REL-0002",
                "revision": 1,
                "status": "CANDIDATE",
            },
        }
    }
    assert _view("release", document=two).count("release") == 2
    assert _view("release").count("release") == 1


def test_a_row_names_no_field_the_route_did_not_declare() -> None:
    """Asking a row for an undeclared column raises rather than answering a blank."""
    with pytest.raises(KeyError):
        _view("milestone").rows[0].field("approval")


def test_a_projection_of_another_family_is_refused() -> None:
    """A frame drawn from another route's rows would show records under other columns."""
    with pytest.raises(ValueError, match="has no acceptance read model"):
        build_acceptance_view(_projection("trust"))


# ---------- the Milestone draws the bundle an acceptance was given against ----------


def test_the_milestone_frame_states_the_sealed_bundle_it_was_accepted_at() -> None:
    """The frame names the revision and the digest, which is what an approval binds to."""
    bundle = _bundle()
    rows = _frame(_view("milestone", bundle=bundle), subject="MLS-0030")
    assert any(bundle.digest() in row for row in rows)
    assert any(f"revision {bundle.revision}" in row for row in rows)


def test_the_milestone_frame_states_the_absence_when_no_bundle_is_sealed() -> None:
    """A Milestone nobody sealed a bundle for says so rather than borrowing a fixture's."""
    rows = _frame(_view("milestone"), subject="MLS-0030")
    assert any(NO_BUNDLE in row for row in rows)
    assert any(NO_APPROVAL in row for row in rows)


def test_the_approval_row_names_the_exact_head_it_binds() -> None:
    """An approval is given to bytes, so the row names the commit those bytes were on."""
    model = _view("milestone", bundle=_bundle(), approval=_approval())
    assert model.approval is not None
    assert model.approval.head_sha == HEAD_SHA
    assert model.approval.tree_sha == TREE_SHA
    assert model.approval.approved_digest == _bundle().digest()
    rows = _frame(model, subject="MLS-0030")
    assert any(HEAD_SHA in row and TREE_SHA in row for row in rows)


def test_an_approval_given_to_other_bytes_is_not_shown_against_this_bundle() -> None:
    """Inheriting a consent would be the console saying a later revision was approved."""
    other = _approval(digest=canonical_digest("some other bundle"))
    model = _view("milestone", bundle=_bundle(), approval=other)
    assert model.approval is None
    assert any(NO_APPROVAL in row for row in _frame(model, subject="MLS-0030"))


def test_the_criteria_rows_carry_every_step_and_the_evidence_behind_it() -> None:
    """A step that passed names its evidence; the frame prints both."""
    model = _view("milestone", bundle=_bundle())
    assert [row.step_id for row in model.criteria] == ["AS-01"]
    assert model.criteria[0].evidence_keys == ("EVD-0002",)
    assert model.proven() == 1
    assert model.blocking() == ()
    assert any("EVD-0002" in row for row in _frame(model, subject="MLS-0030"))


def test_a_step_that_did_not_pass_is_counted_as_open() -> None:
    """The verdict is read off the step, never off the fact that a bundle exists."""
    model = _view("milestone", bundle=_bundle(passed=False))
    assert model.proven() == 0
    assert [row.step_id for row in model.blocking()] == ["AS-01"]


# ---------- the Release states what it knows and stays silent on what it does not ----------


def test_the_release_states_acceptance_and_the_approval_from_the_records_in_hand() -> None:
    """Two signals rest on records the console holds, so both are stated, not guessed."""
    model = _view("release", bundle=_bundle(), approval=_approval())
    assert isinstance(model, ReleaseReadinessView)
    assert [signal.name for signal in model.signals] == list(READINESS_SIGNALS)
    for name in STATED_SIGNALS:
        signal = model.signal(name)
        assert signal is not None
        assert signal.state.state is TruthState.KNOWN
        assert signal.state.value == READINESS_MET


def test_a_candidate_gate_no_producer_observes_renders_unknown() -> None:
    """A readiness matrix must never draw an unobserved gate as one that passed."""
    model = _view("release", bundle=_bundle(), approval=_approval())
    for name in GATE_SIGNALS:
        signal = model.signal(name)
        assert signal is not None
        assert signal.state.state is TruthState.UNKNOWN
        assert signal.state.value is None
        assert signal.state.missing_reason == RC_GATE_REASON
    assert {signal.name for signal in model.unmet()} == set(GATE_SIGNALS)


def test_a_candidate_with_no_bundle_states_no_acceptance_rather_than_an_unmet_one() -> None:
    """Nothing held is a different answer from something held and not met."""
    model = _view("release")
    signal = model.signal("acceptance")
    assert signal is not None
    assert signal.state.state is TruthState.UNKNOWN
    assert signal.evidence == ""


def test_a_blocking_step_makes_acceptance_read_as_not_met() -> None:
    """A bundle whose journey is incomplete states an unmet acceptance, and says so."""
    model = _view("release", bundle=_bundle(passed=False))
    signal = model.signal("acceptance")
    assert signal is not None
    assert signal.state.value == "not met"


def test_the_release_approval_row_names_the_exact_head_it_binds() -> None:
    """The candidate's approval row names the commit the approved bytes were sealed on."""
    model = _view("release", bundle=_bundle(), approval=_approval())
    assert any(HEAD_SHA in row for row in _frame(model))


def test_the_release_signal_lookup_answers_none_for_a_name_it_does_not_state() -> None:
    """Asking for a signal the candidate does not carry is answered, not raised."""
    assert _view("release").signal("publication") is None


# ---------- an opened receipt card is immutable ----------


def test_an_opened_receipt_card_is_immutable() -> None:
    """A card is frozen, so nothing that holds one can edit what the receipt recorded."""
    model = _view("receipt", receipts=(_receipt(),))
    card = model.card(RECEIPT_KEY)
    assert card is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        card.result = "fail"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        model.cards = ()  # type: ignore[misc]


def test_an_opened_receipt_card_draws_the_same_bytes_twice() -> None:
    """Two opens at one cursor are one answer, or the card is not what it claims."""
    model = _view("receipt", receipts=(_receipt(),))
    assert _frame(model, subject=RECEIPT_KEY) == _frame(model, subject=RECEIPT_KEY)


def test_an_opened_receipt_card_states_what_the_record_holds() -> None:
    """Every cell is copied out of the record: the gate, the result and the head."""
    model = _view("receipt", receipts=(_receipt(),))
    rows = _frame(model, subject=RECEIPT_KEY)
    assert any("G-01" in row for row in rows)
    assert any("pass" in row for row in rows)
    assert any(HEAD_SHA in row for row in rows)


def test_a_receipt_card_for_an_unheld_id_states_the_absence() -> None:
    """An id no receipt is filed under renders the absence rather than failing."""
    model = _view("receipt", receipts=(_receipt(),))
    assert model.card("RCP-9999") is None
    assert model.card(None) is None
    assert any(NO_RECEIPT in row for row in _frame(model, subject="RCP-9999"))


def test_no_key_the_receipt_footer_advertises_moves_the_opened_card() -> None:
    """A card an advertised key could edit would not be an immutable record."""
    model = _view("receipt", receipts=(_receipt(),))
    before = _frame(model, subject=RECEIPT_KEY)
    fixture = _fixture()
    for key in _advertised("receipt", fixture):
        session = Session()
        session.route = "receipt"
        session.subj_id = RECEIPT_KEY
        view = View(session=session, fixture=fixture, w=120, h=30, projection=model)
        render_route(view)
        dispatch(Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30), key, False)
        assert model.card(RECEIPT_KEY) is not None
    assert _frame(model, subject=RECEIPT_KEY) == before


# ---------- the export plans a report and writes nothing ----------


def test_the_export_card_plans_the_view_it_would_report() -> None:
    """Each part is sized off the rows in hand rather than promised as an estimate."""
    model = _view("export")
    assert isinstance(model, RunReportPlanView)
    assert [part.name for part in model.parts] == ["rows", "counts", "unstated", "secrets"]
    assert [part.name for part in model.included()] == ["rows", "counts", "unstated"]
    assert model.parts[0].size == "1 row"


def test_the_export_card_states_the_digest_the_report_would_carry() -> None:
    """A report is comparable only through the digest of the view it was taken at."""
    model = _view("export")
    assert any(model.digest in row for row in _frame(model))


def test_the_export_report_is_taken_at_the_views_digest_and_cursor() -> None:
    """The report names the exact rows it reported, by digest and by ordinal."""
    model = _view("export")
    report = export_report(model)
    assert report.digest == model.digest
    assert report.source_cursor == "41208"
    assert f"digest {model.digest}" in report.lines


def test_taking_the_export_report_twice_yields_one_answer() -> None:
    """Two exports of one cursor are byte-identical, or they cannot be compared."""
    model = _view("export")
    assert export_report(model).lines == export_report(model).lines


def test_taking_the_export_report_leaves_the_document_it_read_untouched() -> None:
    """An export allocates no ordinal and moves no record: it is a read of what is there."""
    document = copy.deepcopy(DOCUMENT)
    before = json.dumps(document, sort_keys=True)
    export_report(build_acceptance_view(_projection("export", document=document)))
    assert json.dumps(document, sort_keys=True) == before


def test_the_export_report_carries_every_row_and_every_silent_column() -> None:
    """The report states what the frame states, so the two cannot disagree."""
    model = _view("export")
    lines = export_report(model).lines
    assert sum(1 for line in lines if line.startswith("row ")) == len(model.rows)
    assert sum(1 for line in lines if line.startswith("unstated ")) == len(model.unproduced())


def test_the_export_report_of_an_empty_register_states_the_cursor_and_no_row() -> None:
    """The empty boundary: a report of nothing still says what it was taken at."""
    lines = export_report(_view("export", document={})).lines
    assert not [line for line in lines if line.startswith("row ")]
    assert "cursor 41208" in lines


def _press_export_enter(model: Any) -> Session:
    """Press Enter on the export card over ``model`` and return the session it acted on."""
    session = Session()
    session.route = "export"
    fixture = _fixture()
    view = View(session=session, fixture=fixture, w=120, h=30, projection=model)
    render_route(view)
    ctx = Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30, projection=model)
    dispatch(ctx, "Enter", False)
    assert session.trace is not None
    assert not session.trace.endswith("unclaimed")
    return session


def test_enter_on_the_export_card_takes_the_report_at_the_views_digest() -> None:
    """The console call site: Enter reports the model the frame in front of it drew."""
    model = _view("export")
    session = _press_export_enter(model)
    assert [toast.title for toast in session.toasts] == ["report"]
    assert model.digest in session.toasts[0].text
    assert "nothing was written" in session.toasts[0].text


def test_enter_on_the_export_card_moves_no_record_it_reported() -> None:
    """Taking a report leaves the rows it reported exactly as it found them."""
    model = _view("export")
    before = export_report(model).lines
    _press_export_enter(model)
    assert export_report(model).lines == before


def test_enter_on_the_export_card_with_no_read_model_reports_nothing() -> None:
    """A console drawing the prototype registers has no view an export could be of."""
    session = _press_export_enter(None)
    assert session.toasts[0].text == "no read model is held, so there is nothing to report"


def test_the_export_verb_is_registered_and_defaults_to_the_bundle_view() -> None:
    """The report verb answers for the Milestone unless the caller names another route."""
    assert EXPORT_REPORT_METHOD == "projection.export.report"
    assert ExportParams().route == "milestone"


@pytest.mark.parametrize("route", ACCEPTANCE_ROUTES)
def test_the_export_verb_reports_every_acceptance_route(route: str) -> None:
    """Every route of the family is reportable; the parameter is validated, not trusted."""
    assert ExportParams(route=route).route == route


def test_the_export_verb_refuses_a_route_it_cannot_report() -> None:
    """A report of a route nobody asked for would carry a digest of the wrong rows."""
    with pytest.raises(ValidationError, match="is not reportable"):
        ExportParams(route="trust")


def test_the_export_verb_refuses_an_unknown_parameter() -> None:
    """The request forbids extras, so a typo is a refusal and not a dropped key."""
    with pytest.raises(ValidationError):
        ExportParams.model_validate({"route": "milestone", "cursor": 1})


# ---------- the epoch-1 mode still resolves every advertised key ----------


@pytest.mark.parametrize("route", ACCEPTANCE_ROUTES)
def test_the_epoch_one_mode_resolves_every_key_this_footer_advertises(route: str) -> None:
    """A footer that advertises a key nothing handles promises an action it does not have."""
    fixture = _fixture()
    advertised = _advertised(route, fixture)
    assert advertised
    unclaimed: list[str] = []
    for key in advertised:
        session = Session()
        session.route = route
        session.subj_id = REGISTRY.by_id[route].fixed_subject
        render_route(View(session=session, fixture=fixture, w=120, h=30))
        ctx = Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30)
        dispatch(ctx, key, False)
        if session.trace is None or session.trace.endswith("unclaimed"):
            unclaimed.append(f"{route}:{key}")
    assert not unclaimed, f"advertised but unhandled: {', '.join(unclaimed)}"
