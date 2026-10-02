"""The verification and operations routes draw read models, and say what nothing states.

Seven routes leave the prototype registers here. Four of them verify -- trust, evidence,
the rung card and health -- and three of them operate: the sandbox log, the dispatch queue
and crash recovery. Each now renders rows a projection carried at one committed cursor,
and each declares the columns no producer states rather than drawing them blank.

Health is the route the honesty rule bites on. Doctor observes nothing about a runtime
tuple itself, so each verdict states the conformance stage it was reached at, the daemon
verb that wrote the stage record and the artifact that stage filed its evidence under; a
verdict carrying no provenance renders the unknown token instead of a passing cell, and a
tuple section with no verdict in it says so rather than drawing a healthy tuple nobody
observed. A quarantined tuple names its trigger through the failure code its record was
filed under, and where two triggers share one code the row names both rather than guessing.

The unattended route's queue columns are not rows of its projection: they come from the
daemon's dispatch-queue read, so until that read arrives every such column renders the
unknown truth token naming the read it waits on. That is
the point of declaring them: a blank cell and a cell waiting on a named producer are
different answers to an operator. The sandbox log and Trust state their columns from the
facts their decision and verdict rows carry, and a row that does not state one says why.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    ROUTE_READ_MODELS,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE, RECONNECT_METHOD_TEMPLATE
from eawf.kernel.projection.operations import (
    DISPATCH_QUEUE_PRODUCER,
    NOT_A_DECISION,
    OPERATIONS_FIELDS,
    OPERATIONS_ROUTES,
    build_operations_view,
)
from eawf.kernel.projection.route_view import (
    RouteFieldSpec,
    RouteReadModel,
    build_route_read_model,
    check_field_tables,
    known_field,
    status_and,
    unknown_field,
    unstated,
)
from eawf.kernel.projection.spine import UNPRODUCED_REASON
from eawf.kernel.projection.truth import TruthKind, TruthState
from eawf.kernel.projection.verification import (
    NO_PROVENANCE_REASON,
    NO_TRIGGER_REASON,
    NOT_QUARANTINED_REASON,
    QUARANTINE_PRODUCER,
    TRIGGERS_BY_FAILURE_CODE,
    VERIFICATION_FIELDS,
    VERIFICATION_ROUTES,
    HealthReadModel,
    RuntimeTupleVerdict,
    build_runtime_tuple_rows,
    build_verification_view,
    triggers_for_code,
)
from eawf.kernel.runtime.certification import CertificationFailureCode, QuarantineTrigger
from eawf.observability.doctor.models import CheckResult, HealthProvenance
from eawf.runtime.daemon.methods.projection import ROUTE_READ_METHODS, ROUTE_RECONNECT_METHODS
from eawf.runtime.runtimes.quarantine import TRIGGER_FAILURE_CODE
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.live_reads import HEALTH_VERDICTS_READ, LIVE_READS
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.read_model import NO_VERDICT, native, noun
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session

#: When the probe projections are stamped. The digest does not cover the stamp; a fixed
#: clock only keeps this suite's output reproducible.
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

#: The scope every probe projection is built for.
SCOPE = "EAWF"

#: A requirement row id, such as ``RUN-059``: an internal code that reads as a Run id and
#: never belongs in an operator-facing reason.
REQUIREMENT_ID = re.compile(r"\b[A-Z]{2,4}-\d{3}\b")

#: The seven routes this wave binds, verification first.
BOUND_ROUTES: tuple[str, ...] = (*VERIFICATION_ROUTES, *OPERATIONS_ROUTES)

#: The routes whose every declared column a producer states, read off the row's facts.
STATED_ROUTES: frozenset[str] = frozenset(
    {"trust", "sandbox.log", "evidence", "evidence.digest", "unattended"}
)

#: The routes whose facts ride on their own records beside the rows, so no row column waits.
RECORD_STATED_ROUTES: frozenset[str] = frozenset({"crash.recovery", "health"})

#: The artifact a probe stage record filed its evidence under.
EVIDENCE_REF = "artifact://conformance/rollback-0001"

#: One row per collection the seven routes touch, so a route's own register is never empty
#: by accident and a route binding two registers is told apart from one binding one.
DOCUMENT: dict[str, Any] = {
    "claim": {
        "CLM-0004": {
            "urn": f"urn:eawf:{SCOPE}:claim:CLM-0004",
            "revision": 2,
            "status": "UNCERTIFIED",
        },
    },
    "evidence": {
        "EVD-0001": {
            "urn": f"urn:eawf:{SCOPE}:evidence:EVD-0001",
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
    "sandbox_policy": {
        "pol-2026-08-11.3": {
            "urn": f"urn:eawf:{SCOPE}:legacy:pol-2026-08-11.3",
            "revision": 3,
            "status": "ACTIVE",
        },
    },
    "health_view": {
        "hv-0001": {"urn": f"urn:eawf:{SCOPE}:legacy:hv-0001", "revision": 1, "status": "OK"},
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


def _view(route: str, *, verdicts: Any = (), **kwargs: Any) -> Any:
    """Return the read model of ``route`` over the probe document."""
    projection = _projection(route, **kwargs)
    if route in VERIFICATION_ROUTES:
        return build_verification_view(projection, verdicts=verdicts)
    return build_operations_view(projection)


def _frame(model: Any, *, width: int = 120) -> tuple[list[str], Session]:
    """Return the console frame ``model`` renders, and the session it published into."""
    session = Session()
    session.route = model.route
    view = View(
        session=session,
        fixture=load_fixture(
            Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
        ),
        w=width,
        h=30,
        projection=model,
    )
    return render_route(view), session


def _check(
    *,
    name: str = "runtime_tuple_abc123def456",
    status: str = "fail",
    producer: str = QUARANTINE_PRODUCER,
    stage: str = "rollback",
    provenance: bool = True,
) -> CheckResult:
    """Return one doctor check, with or without the provenance a verdict rests on."""
    return CheckResult(
        name=name,
        status=status,  # type: ignore[arg-type]
        detail=f"{stage} verdict",
        provenance=HealthProvenance(
            producer=producer,  # type: ignore[arg-type]
            stage=stage,  # type: ignore[arg-type]
            evidence_ref=EVIDENCE_REF,
        )
        if provenance
        else None,
    )


def _app(route: str, *, verdicts: Any = ()) -> ConsoleApp:
    """Return a console bound to a seam already holding ``route``'s projection.

    The held projection is normally adopted from a read; a test that wants one particular
    starting projection states it directly, as the connection-state suite does.
    """
    seam = ProjectionSeam(route=route, scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._projection = _projection(route)
    app = ConsoleApp(
        load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"),
        FakeClock(),
        seam=seam,
    )
    seam.retarget(route)
    seam._live[HEALTH_VERDICTS_READ] = (
        LIVE_READS[HEALTH_VERDICTS_READ].address(seam),
        tuple(verdicts),
    )
    app.session.route = route
    return app


# ---------- the console composes the read model it draws ----------


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_the_console_composes_this_routes_read_model_from_the_seam(route: str) -> None:
    """The production call site: the app turns the held projection into the route's model."""
    model = _app(route).route_view()
    assert isinstance(model, RouteReadModel)
    assert model.route == route
    assert model.read_model is ROUTE_READ_MODELS[route]


def test_the_console_carries_its_health_verdicts_into_the_health_model() -> None:
    """The verdicts the console was given reach the tuple rows, and no other route's."""
    verdict = RuntimeTupleVerdict(
        check=_check(),
        reason_code=CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE,
        checked_at=AT,
    )
    model = _app("health", verdicts=(verdict,)).route_view()
    assert isinstance(model, HealthReadModel)
    assert [row.check for row in model.tuples] == [verdict.check.name]
    assert model.quarantined() == model.tuples


def test_the_console_holds_no_read_model_for_another_route() -> None:
    """The seam carries one route; another route's frame falls back to epoch one."""
    app = _app("trust")
    app.session.route = "unattended"
    assert app.route_view() is None


# ---------- the seven routes and the verbs that serve them ----------


def test_the_two_families_name_exactly_the_routes_this_wave_binds() -> None:
    """The family tables are the statement of what left the prototype registers."""
    assert VERIFICATION_ROUTES == ("trust", "evidence", "evidence.digest", "health")
    assert OPERATIONS_ROUTES == ("sandbox.log", "unattended", "crash.recovery")
    assert set(VERIFICATION_FIELDS) == set(VERIFICATION_ROUTES)
    assert set(OPERATIONS_FIELDS) == set(OPERATIONS_ROUTES)
    assert not set(VERIFICATION_ROUTES) & set(OPERATIONS_ROUTES)


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_every_bound_route_has_both_projection_verbs(route: str) -> None:
    """A route a console draws natively is one the daemon both reads and reconnects."""
    assert READ_METHOD_TEMPLATE.format(route=route) in ROUTE_READ_METHODS
    assert RECONNECT_METHOD_TEMPLATE.format(route=route) in ROUTE_RECONNECT_METHODS
    assert ROUTE_COLLECTIONS[route]


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_every_bound_route_reports_its_declared_read_model(route: str) -> None:
    """The view names the read model the declarations bind to the route, never another."""
    model = _view(route)
    assert model.read_model is ROUTE_READ_MODELS[route]
    assert model.scope_id == SCOPE
    assert model.source_cursor == "41208"


@pytest.mark.parametrize("cursor", [0, 1, 41208])
@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_source_cursor_is_the_committed_canonical_sequence(route: str, cursor: int) -> None:
    """The view's cursor is the document's ordinal, decimal and verbatim."""
    assert _view(route, cursor=cursor).source_cursor == str(cursor)


# ---------- counts, rows and the columns nothing states ----------


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_every_count_is_the_rows_of_a_register_the_route_binds(route: str) -> None:
    """A count is taken off the view's own rows, one per bound collection."""
    model = _view(route)
    assert set(model.counts) == {c.value for c in ROUTE_COLLECTIONS[route]}
    assert sum(model.counts.values()) == len(model.rows)


def test_evidence_counts_both_registers_it_binds() -> None:
    """Two registers means two counts; neither borrows the other's rows."""
    model = _view("evidence")
    assert model.count("claim") == 1
    assert model.count("evidence") == 1


def test_a_count_with_no_register_is_absent_rather_than_zero() -> None:
    """A zero is a count that was taken; a count nothing holds is unavailable."""
    model = _view("trust")
    assert model.count("run") is None
    assert model.count("") is None


def test_an_empty_register_counts_zero_and_draws_no_row() -> None:
    """A register the route binds and that holds nothing counts zero, honestly."""
    model = _view("trust", document={})
    assert model.counts == {"claim": 0}
    assert model.rows == ()
    assert model.index_of("CLM-0004") is None


def test_a_single_row_register_counts_one() -> None:
    """The off-by-one boundary below a two-row register."""
    two = {
        "claim": {
            **DOCUMENT["claim"],
            "CLM-0005": {
                "urn": f"urn:eawf:{SCOPE}:claim:CLM-0005",
                "revision": 1,
                "status": "CERTIFIED",
            },
        }
    }
    assert _view("trust", document=two).count("claim") == 2
    assert _view("trust").count("claim") == 1


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_rows_carry_the_status_the_document_states(route: str) -> None:
    """The one produced field is the stored status, and it is stored, not derived."""
    for row in _view(route).rows:
        status = row.field("status")
        assert status.state is TruthState.KNOWN
        assert status.truth_kind is TruthKind.STORED
        assert status.value == DOCUMENT[row.collection.value][row.key]["status"]


def test_a_row_stating_no_status_is_unknown_rather_than_blank() -> None:
    """A record with no status renders the unknown state, which says so."""
    document = {"claim": {"CLM-0009": {"urn": f"urn:eawf:{SCOPE}:claim:CLM-0009", "revision": 1}}}
    status = _view("trust", document=document).rows[0].field("status")
    assert status.state is TruthState.UNKNOWN
    assert status.value is None
    assert status.missing_reason


@pytest.mark.parametrize(
    "route", [r for r in BOUND_ROUTES if r not in STATED_ROUTES | RECORD_STATED_ROUTES]
)
def test_unproduced_columns_are_unknown_truth_fields_naming_why(route: str) -> None:
    """A column with no producer is declared and comes back unknown, never silently absent."""
    model = _view(route)
    unproduced = model.unproduced()
    assert unproduced
    assert "status" not in {spec.name for spec in unproduced}
    for row in model.rows:
        for spec in unproduced:
            field = row.field(spec.name)
            assert field.state is TruthState.UNKNOWN
            assert field.value is None
            assert field.truth_kind is TruthKind.DERIVED
            assert field.missing_reason == spec.missing_reason()


def test_field_names_lead_with_the_stored_status() -> None:
    """Column order is declared once; the produced field is always first."""
    for route in BOUND_ROUTES:
        assert _view(route).field_names()[0] == "status"


def test_a_row_names_no_field_the_route_did_not_declare() -> None:
    """Asking a row for an undeclared column raises rather than answering a blank."""
    with pytest.raises(KeyError):
        _view("trust").rows[0].field("progress")


# ---------- the two producers that have not shipped ----------


@pytest.mark.parametrize("route", sorted(STATED_ROUTES))
def test_a_stated_column_a_row_does_not_state_is_unknown_saying_why(route: str) -> None:
    """A policy row states no decision and a claim no verdict; each cell says so."""
    model = _view(route)
    assert model.unproduced() == ()
    for row in model.rows:
        for spec in model.specs[1:]:
            field = row.field(spec.name)
            assert field.state is TruthState.UNKNOWN
            assert field.missing_reason == spec.absent
            assert not REQUIREMENT_ID.search(str(field.missing_reason))


def test_a_policy_row_states_no_decision() -> None:
    """The decision columns of a policy row name why they are blank, never a producer."""
    row = _view("sandbox.log").rows[0]
    assert row.field("decision").missing_reason == NOT_A_DECISION


def test_the_queue_names_the_dispatch_read_it_waits_on() -> None:
    """The queue state and the progress of a queued Run both wait on the same read."""
    row = _view("unattended").rows[0]
    for name in ("queue_state", "progress"):
        reason = str(row.field(name).missing_reason)
        assert DISPATCH_QUEUE_PRODUCER in reason
        assert not REQUIREMENT_ID.search(reason)


def test_a_column_with_no_named_producer_falls_back_to_the_generic_reason() -> None:
    """Not every silent column has an item to name; that one still says it is silent."""
    assert unstated("outcome").missing_reason() == UNPRODUCED_REASON
    assert unstated("outcome", missing_producer="WP-01").missing_reason() != UNPRODUCED_REASON


@pytest.mark.parametrize("route", sorted(RECORD_STATED_ROUTES))
def test_a_route_whose_facts_are_produced_declares_no_silent_column(route: str) -> None:
    """Recovery and check time ride on their own records, never as unknown columns."""
    assert _view(route).unproduced() == ()


def test_the_unattended_frame_names_the_read_its_progress_waits_on() -> None:
    """The frame prints the read beside the unknown token, not only the token."""
    rows, _session = _frame(_view("unattended"))
    assert any(DISPATCH_QUEUE_PRODUCER in row for row in rows)


def test_the_rung_card_frame_says_every_column_is_stated() -> None:
    """The rung card's columns are all produced, so the frame names none as silent."""
    model = _view("evidence.digest")
    rows, _session = _frame(model)
    assert model.unproduced() == ()
    assert any(row.startswith(" UNSTATED  every declared column is stated") for row in rows)


#: The routes whose packet frame lists the read model's rows as its cursor list. Evidence
#: draws one Claim, Health lists checks, and Recovery lists its three doors and counts the
#: Runs; ``test_native_route_frames`` holds each of those.
LISTING_ROUTES: tuple[str, ...] = ("trust", "evidence.digest", "unattended")


@pytest.mark.parametrize("route", LISTING_ROUTES)
def test_the_frame_draws_one_line_per_read_model_row(route: str) -> None:
    """Every row the read model holds reaches the frame, by key and by collection."""
    model = _view(route)
    rows, session = _frame(model)
    body = "\n".join(rows)
    for row in model.rows:
        assert row.key in body
        assert row.collection.value[:11].replace("_", " ") in body
    assert session.sel_id == (model.rows[0].key if model.rows else None)


def test_the_sandbox_log_lists_its_policies_and_rests_the_cursor_on_decisions() -> None:
    """A policy is listed by key; the cursor walks decisions, and there are none here."""
    model = _view("sandbox.log")
    rows, session = _frame(model)
    assert all(row.key in "\n".join(rows) for row in model.rows)
    assert session.sel_id is None


#: The routes whose line under the header states their own subject rather than a count of
#: the register they read: Recovery is about the console's lost projection, not the Runs.
SUBJECT_LINE_ROUTES: frozenset[str] = frozenset({"crash.recovery"})


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_the_frame_prints_the_derived_counts_and_no_cursor(route: str) -> None:
    """Every count on the frame is one the view derived; a complete read names no cursor."""
    model = _view(route)
    rows, _session = _frame(model)
    counts = rows[1]
    for name, count in model.counts.items():
        if route in SUBJECT_LINE_ROUTES:
            assert noun(count, name) not in counts, counts
        else:
            assert noun(count, name) in counts, counts
    assert "cursor" not in counts


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_the_epoch_one_frame_still_renders_when_no_read_model_is_held(route: str) -> None:
    """The prototype mode is the tracked golden contract; binding must not cost it."""
    session = Session()
    session.route = route
    rows = render_route(
        View(
            session=session,
            fixture=load_fixture(
                Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
            ),
            w=120,
            h=30,
        )
    )
    assert len(rows) == 30
    assert "UNSTATED" not in "\n".join(rows)


def test_a_read_model_for_another_route_is_not_adopted() -> None:
    """A frame drawn from another route's rows would show one route's records as another's."""
    model = _view("trust")
    session = Session()
    session.route = "unattended"
    view = View(
        session=session,
        fixture=load_fixture(
            Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
        ),
        w=120,
        h=30,
        projection=model,
    )
    assert native(view) is None


# ---------- health: the stage, the provenance and the trigger ----------


def test_every_trigger_files_under_a_code_and_the_inverse_is_total() -> None:
    """The inverse map is derived from the forward table, so the two cannot drift."""
    assert set(TRIGGER_FAILURE_CODE) == set(QuarantineTrigger)
    flattened = [t for triggers in TRIGGERS_BY_FAILURE_CODE.values() for t in triggers]
    assert sorted(flattened, key=lambda t: t.value) == sorted(
        QuarantineTrigger, key=lambda t: t.value
    )


def test_a_code_two_triggers_share_names_both() -> None:
    """Two triggers are one check failing, so the row names both rather than guessing."""
    both = triggers_for_code(CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE)
    assert both == (QuarantineTrigger.DENIED_TOOL_ESCAPE, QuarantineTrigger.CANARY_FAILURE)


def test_a_code_one_trigger_files_under_names_one() -> None:
    """The single-trigger boundary below the shared code."""
    assert triggers_for_code(CertificationFailureCode.SCHEMA_MISMATCH) == (
        QuarantineTrigger.SCHEMA_DRIFT,
    )


def test_a_code_no_trigger_files_under_names_none() -> None:
    """A refusal the runner reached without a quarantine names no trigger at all."""
    assert triggers_for_code(CertificationFailureCode.NO_LAST_KNOWN_GOOD_PIN) == ()


def test_a_quarantined_tuple_states_its_stage_provenance_and_trigger() -> None:
    """The three facts a health line must carry, and the trigger that put the tuple out."""
    verdict = RuntimeTupleVerdict(
        check=_check(),
        reason_code=CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE,
        checked_at=AT,
    )
    row = build_runtime_tuple_rows([verdict])[0]
    assert row.quarantined is True
    assert row.stage.value == "rollback"
    assert row.producer.value == QUARANTINE_PRODUCER
    assert row.evidence_ref.value == EVIDENCE_REF
    assert row.triggers == (QuarantineTrigger.DENIED_TOOL_ESCAPE, QuarantineTrigger.CANARY_FAILURE)
    assert row.trigger.value == "denied_tool_escape · canary_failure"
    assert row.trigger.provenance_refs == (EVIDENCE_REF,)


def test_a_tuple_in_service_names_no_trigger_and_says_why() -> None:
    """A row that is not quarantined states the absence rather than an empty cell."""
    row = build_runtime_tuple_rows(
        [
            RuntimeTupleVerdict(
                check=_check(status="ok", producer="conformance.certify", stage="certify"),
                checked_at=AT,
            )
        ]
    )[0]
    assert row.quarantined is False
    assert row.triggers == ()
    assert row.trigger.state is TruthState.UNKNOWN
    assert row.trigger.missing_reason == NOT_QUARANTINED_REASON


def test_a_quarantined_tuple_with_no_recorded_code_still_names_no_trigger() -> None:
    """An absent failure code renders unknown; it never picks a plausible trigger."""
    row = build_runtime_tuple_rows([RuntimeTupleVerdict(check=_check(), checked_at=AT)])[0]
    assert row.quarantined is True
    assert row.triggers == ()
    assert row.trigger.state is TruthState.UNKNOWN
    assert row.trigger.missing_reason == NO_TRIGGER_REASON


def test_a_verdict_with_no_provenance_renders_the_unknown_token() -> None:
    """A health line an operator cannot trace back is the defect this rule exists for."""
    check = _check(name="clock_skew", status="ok", provenance=False)
    row = build_runtime_tuple_rows([RuntimeTupleVerdict(check=check, checked_at=AT)])[0]
    for field in (row.stage, row.producer, row.evidence_ref):
        assert field.state is TruthState.UNKNOWN
        assert field.value is None
        assert field.missing_reason == NO_PROVENANCE_REASON
    assert row.quarantined is False


def test_a_runtime_tuple_check_without_provenance_is_refused_upstream() -> None:
    """The verdict shape itself will not carry an untraceable runtime-tuple check."""
    with pytest.raises(ValidationError, match="requires provenance"):
        _check(provenance=False)


def test_no_verdict_at_all_produces_no_tuple_rows() -> None:
    """The empty boundary: an absent verdict is an absence, never a healthy tuple."""
    model = _view("health")
    assert isinstance(model, HealthReadModel)
    assert model.tuples == ()
    assert model.quarantined() == ()


def test_the_health_frame_states_the_stage_the_verb_and_the_trigger() -> None:
    """The three facts reach the frame, beside the check that repeats them."""
    verdict = RuntimeTupleVerdict(
        check=_check(),
        reason_code=CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE,
        checked_at=AT,
    )
    rows, _session = _frame(_view("health", verdicts=(verdict,)), width=160)
    body = "\n".join(rows)
    assert "1 runtime tuple · 1 quarantined" in body
    assert "rollback" in body
    assert QUARANTINE_PRODUCER in body
    assert "denied_tool_escape · canary_failure" in body


def test_the_health_frame_says_when_no_verdict_is_held() -> None:
    """An empty tuple section says so; it never draws a tuple as in service."""
    rows, _session = _frame(_view("health"))
    assert any(NO_VERDICT in row for row in rows)


def test_the_health_frame_draws_the_unknown_token_for_a_traceless_verdict() -> None:
    """A verdict with no stage record shows the token in all three provenance cells."""
    check = _check(name="clock_skew", status="warn", provenance=False)
    rows, _session = _frame(
        _view("health", verdicts=(RuntimeTupleVerdict(check=check, checked_at=AT),))
    )
    # the check is listed once with its result, then once in the runner group beside the
    # stage, the verb and the trigger its provenance would have named
    group = rows[next(i for i, row in enumerate(rows) if row.startswith(" TUPLES")) :]
    line = next(row for row in group if "clock_skew" in row)
    assert line.count("?") >= 3


def test_only_the_health_route_carries_tuple_rows() -> None:
    """The other verification routes carry no conformance verdict, so they hold none."""
    for route in VERIFICATION_ROUTES:
        model = _view(route, verdicts=(RuntimeTupleVerdict(check=_check(), checked_at=AT),))
        assert isinstance(model, HealthReadModel) is (route == "health")


# ---------- refusals ----------


@pytest.mark.parametrize("route", OPERATIONS_ROUTES)
def test_an_operations_route_has_no_verification_read_model(route: str) -> None:
    """A projection of another family is not this frame's rows, so it is not adopted."""
    with pytest.raises(ValueError, match="has no verification read model"):
        build_verification_view(_projection(route))


@pytest.mark.parametrize("route", VERIFICATION_ROUTES)
def test_a_verification_route_has_no_operations_read_model(route: str) -> None:
    """The refusal runs both ways; neither family answers for the other."""
    with pytest.raises(ValueError, match="has no operations read model"):
        build_operations_view(_projection(route))


def test_an_unbound_route_has_no_projection_to_build_from() -> None:
    """A route with no document binding is refused at the projection, not papered over."""
    with pytest.raises(ValueError, match="renders no epoch-2 collection"):
        _projection("settings.stack")


def test_a_negative_cursor_is_refused() -> None:
    """A cursor is a committed ordinal, so there is no projection before the first one."""
    with pytest.raises(ValueError, match="never -1"):
        _projection("trust", cursor=-1)


def test_a_family_naming_an_unbound_route_refuses_to_build() -> None:
    """A route no projection serves cannot be declared as a family route."""
    with pytest.raises(ValueError, match="binds no collection"):
        check_field_tables(
            family="probe",
            routes=("settings.stack",),
            fields={"settings.stack": status_and(unstated("x"))},
        )


def test_a_family_route_declaring_no_field_refuses_to_build() -> None:
    """A route with an empty column table is a frame with nothing in it."""
    with pytest.raises(ValueError, match="declares no field to render"):
        check_field_tables(family="probe", routes=("trust",), fields={})


def test_a_table_not_leading_with_the_status_refuses_to_build() -> None:
    """Column order is a contract: the stored status is always the first column."""
    with pytest.raises(ValueError, match="does not lead with the stored status"):
        check_field_tables(
            family="probe", routes=("trust",), fields={"trust": (unstated("verdict"),)}
        )


def test_a_table_naming_one_field_twice_refuses_to_build() -> None:
    """One column named twice is a row whose second cell overwrites the first."""
    with pytest.raises(ValueError, match="declares 'verdict' twice"):
        check_field_tables(
            family="probe",
            routes=("trust",),
            fields={"trust": status_and(unstated("verdict"), unstated("verdict"))},
        )


def test_a_table_for_a_route_the_family_does_not_hold_refuses_to_build() -> None:
    """A stray column table is a route the family would never be asked to render."""
    with pytest.raises(ValueError, match="is not a probe route"):
        check_field_tables(
            family="probe",
            routes=("trust",),
            fields={"trust": status_and(unstated("verdict")), "health": status_and()},
        )


def test_the_shipped_family_tables_are_valid() -> None:
    """The check has teeth above; here it passes on what ships."""
    check_field_tables(
        family="verification", routes=VERIFICATION_ROUTES, fields=VERIFICATION_FIELDS
    )
    check_field_tables(family="operations", routes=OPERATIONS_ROUTES, fields=OPERATIONS_FIELDS)


def test_building_a_read_model_for_an_undeclared_route_raises() -> None:
    """The builder refuses a projection its field table says nothing about."""
    with pytest.raises(ValueError, match="has no probe read model"):
        build_route_read_model(
            _projection("trust"), family="probe", fields={"health": status_and()}
        )


# ---------- the truth-cell helpers ----------


def test_a_known_cell_states_its_value_precision_and_provenance() -> None:
    """A stated cell is exact and names what it rests on."""
    field = known_field(value="rollback", urn=EVIDENCE_REF, revision=1)
    assert field.state is TruthState.KNOWN
    assert field.value == "rollback"
    assert field.provenance_refs == (EVIDENCE_REF,)


def test_an_unknown_cell_carries_no_value_and_names_its_reason() -> None:
    """A missing value never renders as a zero or a blank."""
    field = unknown_field(urn=EVIDENCE_REF, revision=1, reason="nothing states it")
    assert field.state is TruthState.UNKNOWN
    assert field.value is None
    assert field.missing_reason == "nothing states it"


def test_a_blank_reason_is_refused_by_the_truth_field() -> None:
    """A missing value with a blank reason is a cell that says nothing at all."""
    with pytest.raises(ValidationError):
        unknown_field(urn=EVIDENCE_REF, revision=1, reason="")


def test_a_field_spec_without_a_named_producer_reads_the_generic_reason() -> None:
    """The fallback is stated once, so a column that names no item still says why."""
    assert RouteFieldSpec(name="door").missing_reason() == UNPRODUCED_REASON
    assert unstated("door", missing_producer="RUN-059").missing_reason().endswith("RUN-059")
