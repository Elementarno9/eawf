"""The recorded rehearsal says how far the native path carried one Milestone.

A rehearsal record is worth keeping only if it can be wrong. This suite is what
makes it falsifiable: the five steps are a closed set recorded in order, every
surface a step names has to be a shipped one, and the census of epoch-2 record
kinds is re-derived from the code rather than believed.

**A step is executed or refused, and a refusal carries a code.** Both halves are
enforced on the row itself, so a step that claims to have been refused without
saying by what never reaches the comparison. ``first_unexecutable_step`` is not
taken on trust either -- it is recomputed from the steps, because the field
exists to be read by someone who will not read the rest.

**The census is total and its read side is derived.** One row per collection the
tier table declares, with what writes it and what reads it, and ``nothing`` is a
real and common answer. The ``read_by`` column is recomputed from the
route-to-collection table, so a row claiming a reader it does not have fails
here; the ``written_by`` column names registered daemon verbs, so a renamed verb
fails here too.

**The record names no absolute path.** A rehearsal is driven in scratch
directories on one machine, and the whole value of committing its record is that
it says nothing about that machine. The scan runs over the raw bytes rather than
over parsed fields, so a path smuggled into a prose sentence is caught as
readily as one in a path-shaped key.

Every check is exercised on a deliberately broken copy as well as on the
recorded file, so a check that has quietly stopped reading anything is a failing
test rather than a green one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

import eawf.runtime.daemon.methods.candidate
import eawf.runtime.daemon.methods.delivery
import eawf.runtime.daemon.methods.delivery_acceptance
import eawf.runtime.daemon.methods.domain
import eawf.runtime.daemon.methods.planning
import eawf.runtime.daemon.methods.projection
import eawf.runtime.daemon.methods.run
import eawf.runtime.daemon.methods.run_budget
import eawf.runtime.daemon.methods.semantic  # noqa: F401  (registers the semantic verbs)
from eawf.kernel.projection.compute import ROUTE_COLLECTIONS
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE
from eawf.kernel.projection.registers import UNWRITTEN_COLLECTIONS
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods import registered_methods

pytestmark = pytest.mark.integration

#: Four levels up from this file lands on the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Where the rehearsal record is committed.
MANIFEST = _REPO_ROOT / ".ea/artifacts/evidence/2026-09-18-canary-rehearsal/rehearsal-manifest.json"

#: The five steps of the native path, in the order a Milestone takes them.
STEP_ORDER: tuple[str, ...] = ("plan_apply", "dispatch", "integrate", "verify", "accept")

#: What a recorded surface may be: a shipped skill invocation, or a dotted
#: JSON-RPC method the daemon registers. Anything else is a surface nobody can
#: run, which is the shape an invented rehearsal takes.
_SKILL_SURFACE = re.compile(r"^/[a-z][a-z-]*( [a-z][a-z-]*| --[a-z-]+)*$")
_METHOD_SURFACE = re.compile(r"^[a-z][a-z_]*(\.[a-z_]+)+$")

#: The path prefixes that name one machine. Stated as data rather than folded
#: into the pattern so the teeth cases are the very shapes the scan looks for,
#: and a shape added here is proved caught without anybody adding a case.
LEAK_SHAPES: tuple[str, ...] = (
    "/Users/",  # pragma: allowlist secret
    "/home/",  # pragma: allowlist secret
    "/private/",
    "/var/folders/",
    "/tmp/",
    "~/",
    "C:\\",
)

#: The ways a path leaks a machine into a record meant to be read anywhere.
_ABSOLUTE_PATH = re.compile("|".join(re.escape(shape) for shape in LEAK_SHAPES))

#: A collection name no tier table holds, for the cases whose subject is row
#: validation rather than any particular record.
SYNTHETIC_RECORD = "spike_collection"


class CensusRow(BaseModel):
    """What writes one epoch-2 record kind, and what reads it.

    Attributes:
        record: The collection's storage name.
        producer: What writes it at all.
        written_by: The registered daemon verbs that write it.
        consumer: What reads it at all.
        read_by: The route read verbs that render it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: Annotated[str, Field(min_length=1)]
    producer: Literal["native_verb", "migration_cutover", "none"]
    written_by: tuple[str, ...]
    consumer: Literal["console_route", "none"]
    read_by: tuple[str, ...]

    @model_validator(mode="after")
    def _a_named_kind_lists_its_names(self) -> Self:
        """Refuse a row whose summary and its list disagree.

        Raises:
            ValueError: The producer or the consumer says one thing and the
                list beside it says another, which would let a record with no
                writer read as written by summarising it that way.
        """
        problems: list[str] = []
        if (self.producer == "native_verb") != bool(self.written_by):
            problems.append(f"record {self.record!r} names verbs only when a native verb writes it")
        if self.producer == "migration_cutover" and self.written_by:
            problems.append(f"record {self.record!r} is cutover-written but names a native verb")
        if (self.consumer == "console_route") != bool(self.read_by):
            problems.append(f"record {self.record!r} names readers only when a route reads it")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class Attempt(BaseModel):
    """One call made inside one step, and what came back.

    Attributes:
        surface: The skill invocation or daemon verb that was called.
        executed: Whether the call did what it was asked to do.
        code: The stable refusal code, present exactly when it did not.
        note: What the answer said, in one line.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    surface: Annotated[str, Field(min_length=1)]
    executed: bool
    code: Annotated[str, Field(min_length=1)] | None = None
    note: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _a_refusal_carries_its_code(self) -> Self:
        """Refuse an attempt that does not say why it failed, or why it did not.

        Raises:
            ValueError: A refused attempt names no code, or an executed one
                names a code anyway.
        """
        if self.executed == (self.code is not None):
            raise ValueError(
                f"attempt on {self.surface!r} carries a refusal code exactly when it was refused"
            )
        return self


class Step(BaseModel):
    """One of the five steps of the native path.

    Attributes:
        step: Which step this is.
        disposition: Whether the step as a whole executed.
        code: The stable code it stopped on, present exactly when it did not.
        surface: The surface whose answer decided the step.
        detail: What happened, in one sentence.
        attempts: Every call the step made, in the order it made them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: Literal["plan_apply", "dispatch", "integrate", "verify", "accept"]
    disposition: Literal["executed", "refused"]
    code: Annotated[str, Field(min_length=1)] | None = None
    surface: Annotated[str, Field(min_length=1)]
    detail: Annotated[str, Field(min_length=1)]
    attempts: Annotated[tuple[Attempt, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _a_refused_step_names_its_code_and_its_surface(self) -> Self:
        """Refuse a step whose verdict, code and attempts do not agree.

        Raises:
            ValueError: A refused step names no code, an executed one names a
                code anyway, or the deciding surface made no recorded attempt.
        """
        problems: list[str] = []
        if (self.disposition == "refused") != (self.code is not None):
            problems.append(f"step {self.step!r} carries a code exactly when it was refused")
        if self.surface not in {attempt.surface for attempt in self.attempts}:
            problems.append(f"step {self.step!r} decided on a surface it never called")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class Milestone(BaseModel):
    """The one canary Milestone the rehearsal drove.

    Attributes:
        urn: Its qualified URN inside the canary.
        key: Its public key.
        title: What it was for.
        created_by: The verb that materialised it.
        status_reached: Where it stood when the walk stopped.
        status_target: Where a completed walk would have left it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    urn: Annotated[str, Field(pattern=r"^eawf://[A-Za-z0-9/-]+/milestone/MLS-\d{4,}$")]
    key: Annotated[str, Field(pattern=r"^MLS-\d{4,}$")]
    title: Annotated[str, Field(min_length=1)]
    created_by: Annotated[str, Field(min_length=1)]
    status_reached: Annotated[str, Field(min_length=1)]
    status_target: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _the_urn_addresses_the_key(self) -> Self:
        """Refuse a Milestone whose URN and key name two different records.

        Raises:
            ValueError: The URN's entity key is not the recorded key.
        """
        if self.urn.rsplit("/", 1)[-1] != self.key:
            raise ValueError(f"milestone {self.key!r} is addressed by a URN naming another record")
        return self


class Finding(BaseModel):
    """One thing the walk established that is nobody's recorded gap yet.

    Attributes:
        id: The finding's id inside this record.
        severity: How badly it blocks the native path.
        statement: What is wrong, in one sentence.
        observed: What the walk actually saw.
        known_gap: Whether the rehearsal re-confirmed a gap already recorded
            elsewhere rather than establishing a new one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^F-\d{2}$")]
    severity: Literal["P0", "P1", "P2", "P3"]
    statement: Annotated[str, Field(min_length=1)]
    observed: Annotated[str, Field(min_length=1)]
    known_gap: bool


class Precondition(BaseModel):
    """What had to be true before the walk could start.

    Attributes:
        surface: What was written to make it true.
        code: The stable name of the gap that made the write necessary.
        note: Why nothing shipped could do it instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    surface: Annotated[str, Field(min_length=1)]
    code: Annotated[str, Field(min_length=1)]
    note: Annotated[str, Field(min_length=1)]


class Canary(BaseModel):
    """The disposable repository the walk happened in.

    Attributes:
        project_code: The registry code it was provisioned under.
        repository: Its repository URN.
        generation_id: The born-native generation it read from.
        epoch: Always 2; a canary is born native.
        runtime_dir: How its runtime directory was allocated, stated rather
            than located, because a location is a fact about one machine.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_-]{1,15}$")]
    repository: Annotated[str, Field(pattern=r"^eawf://[A-Za-z0-9/-]+/repository/REP-[A-Z0-9-]+$")]
    generation_id: Annotated[str, Field(pattern=r"^gen-[0-9a-f]{16}$")]
    epoch: Literal[2]
    runtime_dir: Annotated[str, Field(min_length=1)]


class RehearsalManifest(BaseModel):
    """The whole recorded walk, as it is committed.

    Attributes:
        schema_version: The record shape this suite reads.
        rehearsed_on: The day the walk was driven.
        canary: Where it happened.
        milestone: The one Milestone it drove.
        precondition: What was written before it could start.
        steps: The five steps, in path order.
        first_unexecutable_step: The first step that could not execute.
        record_without_producer: The record kind the acceptance step needs and
            nothing writes.
        records_without_producer: Every record kind nothing writes.
        census: One row per epoch-2 record kind.
        findings: What the walk established.
        provider_processes_started: How many provider processes ran. Zero: the
            walk stops at the surface that would have opened one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["canary-rehearsal/v1"]
    rehearsed_on: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    canary: Canary
    milestone: Milestone
    precondition: Precondition
    steps: Annotated[tuple[Step, ...], Field(min_length=1)]
    first_unexecutable_step: Literal["plan_apply", "dispatch", "integrate", "verify", "accept"]
    record_without_producer: Annotated[str, Field(min_length=1)]
    records_without_producer: tuple[str, ...]
    census: Annotated[tuple[CensusRow, ...], Field(min_length=1)]
    findings: tuple[Finding, ...]
    provider_processes_started: Annotated[int, Field(ge=0)]


def load_manifest(document: Any = None) -> RehearsalManifest:
    """Return the validated record, from ``document`` or from the recorded file.

    Args:
        document: A decoded record to validate instead of the committed one.

    Returns:
        The validated record.

    Raises:
        pydantic.ValidationError: The document is not a rehearsal record.
    """
    if document is None:
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return RehearsalManifest.model_validate(document)


def derived_readers(record: str) -> tuple[str, ...]:
    """Return the route read verbs that render ``record``, from the route table.

    Args:
        record: An epoch-2 collection's storage name.

    Returns:
        The read method names, sorted. Empty when no route renders it.
    """
    return tuple(
        sorted(
            READ_METHOD_TEMPLATE.format(route=route)
            for route, bound in ROUTE_COLLECTIONS.items()
            if any(collection.value == record for collection in bound)
        )
    )


def census_defects(manifest: RehearsalManifest) -> tuple[str, ...]:
    """Return every way the census disagrees with the code, sorted by record.

    Args:
        manifest: The validated record.

    Returns:
        One message per disagreeing record: a collection the census does not
        cover, a census row for no collection, a row naming a reader the route
        table does not give it, and a row naming a verb the daemon does not
        register. Empty when the census and the code agree.
    """
    declared = {collection.value for collection in Epoch2Collection}
    listed = {row.record: row for row in manifest.census}
    registered = set(registered_methods())
    defects: list[str] = []
    repeated = sorted(
        {
            row.record
            for row in manifest.census
            if [r.record for r in manifest.census].count(row.record) > 1
        }
    )
    defects += [f"record {record!r} is listed twice" for record in repeated]
    for record in sorted(declared | set(listed)):
        row = listed.get(record)
        if row is None:
            defects.append(f"record {record!r} is uncensused: the census is not total")
            continue
        if record not in declared:
            defects.append(f"record {record!r} is censused but the tier table does not declare it")
            continue
        readers = derived_readers(record)
        if row.read_by != readers:
            defects.append(
                f"record {record!r} names readers {row.read_by} but routes give {readers}"
            )
        unknown = tuple(sorted(set(row.written_by) - registered))
        if unknown:
            defects.append(f"record {record!r} names unregistered writer(s) {unknown}")
    return tuple(defects)


def shipped_surface(surface: str) -> bool:
    """Return whether ``surface`` is something an operator could actually run.

    Args:
        surface: A surface name as the record spells it.

    Returns:
        ``True`` for a skill invocation, or for a dotted JSON-RPC method the
        daemon registers. A dotted name nobody serves is ``False``, which is
        what keeps an invented verb out of the record.
    """
    if _SKILL_SURFACE.match(surface):
        return True
    if not _METHOD_SURFACE.match(surface):
        return False
    return surface in registered_methods()


def absolute_path_hits(text: str) -> tuple[str, ...]:
    """Return every machine-local path fragment ``text`` carries, deduplicated.

    Args:
        text: The raw bytes of a record, decoded.

    Returns:
        The matched fragments, sorted. Empty when the text names no machine.
    """
    return tuple(sorted({match.group(0) for match in _ABSOLUTE_PATH.finditer(text)}))


def _recorded() -> dict[str, Any]:
    """Return the committed record, decoded and mutable."""
    parsed: dict[str, Any] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return parsed


def _step(name: str, **changes: Any) -> dict[str, Any]:
    """Return the recorded ``name`` step with ``changes`` applied."""
    row = next(item for item in _recorded()["steps"] if item["step"] == name)
    return {**row, **changes}


def _census_row(record: str, **changes: Any) -> dict[str, Any]:
    """Return the recorded census row of ``record`` with ``changes`` applied."""
    row = next(item for item in _recorded()["census"] if item["record"] == record)
    return {**row, **changes}


def _without_census_row(record: str) -> dict[str, Any]:
    """Return the recorded document with ``record``'s census row dropped."""
    document = _recorded()
    document["census"] = [row for row in document["census"] if row["record"] != record]
    return document


def _attempt(**changes: Any) -> dict[str, Any]:
    """Return a minimal attempt row with ``changes`` applied."""
    return {
        "surface": "/dispatch",
        "executed": False,
        "code": "run_request_uncompilable",
        "note": "stopped for an operator",
        **changes,
    }


# ---------- the record is a record ----------


def test_the_recorded_manifest_validates() -> None:
    """The committed file is a rehearsal record, not merely well-formed JSON."""
    manifest = load_manifest()
    assert manifest.schema_version == "canary-rehearsal/v1"
    assert manifest.rehearsed_on == "2026-09-18"


def test_the_record_names_exactly_one_canary_milestone() -> None:
    """One Milestone, addressed by a URN inside the canary that carries it."""
    manifest = load_manifest()
    assert manifest.milestone.key == manifest.milestone.urn.rsplit("/", 1)[-1]
    assert manifest.canary.project_code in manifest.milestone.urn
    assert manifest.milestone.status_reached != manifest.milestone.status_target


def test_the_canary_was_born_native_under_a_runtime_directory_of_its_own() -> None:
    """A canary is epoch 2 by birth, and its runtime directory is stated, not located."""
    canary = load_manifest().canary
    assert canary.epoch == 2
    assert canary.project_code in canary.repository
    assert absolute_path_hits(canary.runtime_dir) == ()


# ---------- the five steps ----------


def test_the_five_steps_are_recorded_once_each_in_path_order() -> None:
    """A step the record omits is a step nobody has to account for."""
    assert tuple(step.step for step in load_manifest().steps) == STEP_ORDER


@pytest.mark.parametrize("name", STEP_ORDER)
def test_every_step_says_whether_it_executed_and_under_which_code(name: str) -> None:
    """The whole point of the record: each step's verdict and its reason."""
    step = next(item for item in load_manifest().steps if item.step == name)
    assert step.disposition in {"executed", "refused"}
    assert (step.code is None) == (step.disposition == "executed")
    assert step.detail


@pytest.mark.parametrize("name", STEP_ORDER)
def test_every_step_was_driven_through_a_shipped_surface(name: str) -> None:
    """A step driven by nothing shippable is a step nobody can repeat."""
    step = next(item for item in load_manifest().steps if item.step == name)
    assert shipped_surface(step.surface), step.surface
    for attempt in step.attempts:
        assert shipped_surface(attempt.surface), attempt.surface


def test_plan_apply_is_the_one_step_that_executed() -> None:
    """The walk moved the tree exactly once, and the record says where."""
    steps = {step.step: step for step in load_manifest().steps}
    assert steps["plan_apply"].disposition == "executed"
    assert [name for name, step in steps.items() if step.disposition == "refused"] == [
        "dispatch",
        "integrate",
        "verify",
        "accept",
    ]


def test_the_first_unexecutable_step_is_recomputed_rather_than_believed() -> None:
    """The headline field is checked against the steps it summarises."""
    manifest = load_manifest()
    refused = [step.step for step in manifest.steps if step.disposition == "refused"]
    assert manifest.first_unexecutable_step == refused[0]


def test_no_provider_process_was_started() -> None:
    """The walk stops at the surface that would have opened one, so none ran."""
    assert load_manifest().provider_processes_started == 0


def test_the_precondition_names_the_gap_that_made_it_necessary() -> None:
    """A direct write before the walk is recorded as a gap, not hidden as setup."""
    precondition = load_manifest().precondition
    assert precondition.code == "epoch2_record_admission_absent"
    assert precondition.note


# ---------- the census ----------


def test_the_census_covers_every_epoch_two_record_kind_exactly_once() -> None:
    """A record kind the census omits is one nobody has decided the producer of."""
    censused = [row.record for row in load_manifest().census]
    assert sorted(censused) == sorted(collection.value for collection in Epoch2Collection)
    assert len(censused) == len(set(censused))


def test_the_recorded_census_has_no_defects() -> None:
    """The census and the code agree in both directions today."""
    assert census_defects(load_manifest()) == ()


@pytest.mark.parametrize("collection", sorted(Epoch2Collection, key=lambda item: item.value))
def test_every_census_row_names_the_readers_the_route_table_gives_it(
    collection: Epoch2Collection,
) -> None:
    """The read side is derived from the console's own binding table."""
    row = next(item for item in load_manifest().census if item.record == collection.value)
    assert row.read_by == derived_readers(collection.value)
    assert (row.consumer == "console_route") == bool(row.read_by)


def test_every_named_writer_is_a_verb_the_daemon_registers() -> None:
    """A census naming a verb nobody serves describes a producer that is not there."""
    registered = set(registered_methods())
    for row in load_manifest().census:
        assert set(row.written_by) <= registered, row.record


def test_the_producerless_records_are_the_rows_that_name_no_writer() -> None:
    """The summary list is recomputed from the census it summarises."""
    manifest = load_manifest()
    derived = sorted(row.record for row in manifest.census if row.producer == "none")
    assert list(manifest.records_without_producer) == derived


def test_the_declared_unwritten_registers_are_censused_as_producerless() -> None:
    """The code names registers nothing writes; the census must not disagree."""
    producerless = set(load_manifest().records_without_producer)
    assert {collection.value for collection in UNWRITTEN_COLLECTIONS} <= producerless


def test_the_record_no_producer_writes_is_the_one_acceptance_needs() -> None:
    """Acceptance is taken against a sealed pending action, which nothing writes."""
    manifest = load_manifest()
    assert manifest.record_without_producer == Epoch2Collection.PENDING_ACTION.value
    assert manifest.record_without_producer in manifest.records_without_producer
    row = next(item for item in manifest.census if item.record == manifest.record_without_producer)
    assert row.producer == "none"
    assert row.consumer == "console_route"


def test_the_census_records_more_producerless_readers_than_the_code_declares() -> None:
    """The gap the census exists to surface: read registers nobody writes."""
    manifest = load_manifest()
    unread_declared = {collection.value for collection in UNWRITTEN_COLLECTIONS}
    producerless_readers = {
        row.record
        for row in manifest.census
        if row.producer == "none" and row.consumer == "console_route"
    }
    assert unread_declared < producerless_readers


# ---------- the findings ----------


def test_every_finding_says_what_it_saw_and_whether_it_is_new() -> None:
    """A finding without an observation is an opinion; the record carries neither."""
    findings = load_manifest().findings
    assert findings
    assert len({finding.id for finding in findings}) == len(findings)
    assert any(not finding.known_gap for finding in findings)
    for finding in findings:
        assert finding.observed


# ---------- no machine is named ----------


def test_the_recorded_manifest_names_no_absolute_path() -> None:
    """The record is committed, so it must say nothing about the host that made it."""
    assert absolute_path_hits(MANIFEST.read_text(encoding="utf-8")) == ()


def test_the_companion_documents_name_no_absolute_path() -> None:
    """The prose and the isolation record are held to the same rule as the record."""
    for path in sorted(MANIFEST.parent.iterdir()):
        assert absolute_path_hits(path.read_text(encoding="utf-8")) == (), path.name


@pytest.mark.parametrize("shape", LEAK_SHAPES)
def test_a_machine_local_path_is_reported(shape: str) -> None:
    """The scan has teeth: every shape it looks for is caught in a sentence."""
    assert absolute_path_hits(f"the canary lived at {shape}somebody/scratch during the run")


@pytest.mark.parametrize(
    "surface",
    ["/dispatch", "/integrate show", "/verify --mode all", "planning.plan_revision.apply"],
)
def test_a_shipped_surface_is_accepted(surface: str) -> None:
    """The four shapes the record actually uses all resolve."""
    assert shipped_surface(surface)


@pytest.mark.parametrize(
    "surface",
    ["", "dispatch", "runtime.delivery.invent", "Make It So", "/Dispatch", "./script.sh"],
)
def test_a_surface_nobody_ships_is_rejected(surface: str) -> None:
    """The check has teeth, including on the empty and the plausible-but-absent."""
    assert not shipped_surface(surface)


def test_a_record_relative_path_is_not_reported() -> None:
    """A repository-relative locator is what the record is supposed to carry."""
    assert absolute_path_hits(".ea/artifacts/evidence/2026-09-18-canary-rehearsal") == ()


def test_an_empty_text_names_no_path() -> None:
    """The empty boundary: nothing to scan means nothing found."""
    assert absolute_path_hits("") == ()


# ---------- the checks have teeth ----------


def test_a_missing_census_row_is_reported() -> None:
    """Dropping a row makes the census non-total, and the check says which record."""
    defects = census_defects(load_manifest(_without_census_row("run")))
    assert defects == ("record 'run' is uncensused: the census is not total",)


def test_a_census_row_for_no_collection_is_reported() -> None:
    """A row the tier table does not hold is a collection renamed or invented."""
    document = _recorded()
    document["census"].append(
        {
            "record": SYNTHETIC_RECORD,
            "producer": "none",
            "written_by": [],
            "consumer": "none",
            "read_by": [],
        }
    )
    defects = census_defects(load_manifest(document))
    assert defects == (
        f"record {SYNTHETIC_RECORD!r} is censused but the tier table does not declare it",
    )


def test_a_row_claiming_a_reader_it_does_not_have_is_reported() -> None:
    """The derived read side refuses a row that promises a console route."""
    document = _without_census_row("workspace")
    document["census"].append(
        _census_row("workspace", consumer="console_route", read_by=["projection.scope.home.read"])
    )
    defects = census_defects(load_manifest(document))
    assert defects == (
        "record 'workspace' names readers ('projection.scope.home.read',) but routes give ()",
    )


def test_a_row_naming_an_unregistered_writer_is_reported() -> None:
    """A producer nobody serves is the claim the census exists to refuse."""
    document = _without_census_row("release")
    document["census"].append(
        _census_row("release", producer="native_verb", written_by=["runtime.release.invent"])
    )
    defects = census_defects(load_manifest(document))
    assert defects == ("record 'release' names unregistered writer(s) ('runtime.release.invent',)",)


def test_a_record_censused_twice_is_reported() -> None:
    """One record with two rows could answer the producer question two ways."""
    document = _recorded()
    document["census"].append(_census_row("task"))
    assert "is listed twice" in census_defects(load_manifest(document))[0]


def test_a_row_summarising_itself_as_written_while_naming_no_verb_is_refused() -> None:
    """The row's own rule: the summary and the list have to agree."""
    document = _without_census_row("claim")
    document["census"].append(_census_row("claim", producer="native_verb", written_by=[]))
    with pytest.raises(ValidationError, match="names verbs only when a native verb writes it"):
        load_manifest(document)


def test_a_cutover_row_naming_a_native_verb_is_refused() -> None:
    """A collection only the cutover projects has no native writer to name."""
    document = _without_census_row("audit")
    document["census"].append(
        _census_row("audit", producer="migration_cutover", written_by=["domain.transition.apply"])
    )
    with pytest.raises(ValidationError, match="names verbs only when a native verb writes it"):
        load_manifest(document)


def test_a_row_summarising_itself_as_read_while_naming_no_reader_is_refused() -> None:
    """The same rule on the consumer side, which is the side the console draws."""
    document = _without_census_row("lease")
    document["census"].append(_census_row("lease", consumer="console_route", read_by=[]))
    with pytest.raises(ValidationError, match="names readers only when a route reads it"):
        load_manifest(document)


def test_a_refused_step_naming_no_code_is_refused() -> None:
    """A step that failed for no stated reason is the record's central failure."""
    document = _recorded()
    document["steps"] = [_step("dispatch", code=None)]
    with pytest.raises(ValidationError, match="carries a code exactly when it was refused"):
        load_manifest(document)


def test_an_executed_step_naming_a_code_is_refused() -> None:
    """A step cannot both have worked and have stopped on something."""
    document = _recorded()
    document["steps"] = [_step("plan_apply", code="run_request_uncompilable")]
    with pytest.raises(ValidationError, match="carries a code exactly when it was refused"):
        load_manifest(document)


def test_a_step_deciding_on_a_surface_it_never_called_is_refused() -> None:
    """A verdict has to come from a call the record actually holds."""
    document = _recorded()
    document["steps"] = [_step("verify", surface="/verify --mode invented")]
    with pytest.raises(ValidationError, match="decided on a surface it never called"):
        load_manifest(document)


def test_a_step_with_no_attempts_is_refused() -> None:
    """The empty boundary: a step nobody attempted proves nothing about the path."""
    document = _recorded()
    document["steps"] = [_step("dispatch", attempts=[])]
    with pytest.raises(ValidationError):
        load_manifest(document)


def test_a_refused_attempt_naming_no_code_is_refused() -> None:
    """The same rule one level down, where the codes actually come from."""
    with pytest.raises(ValidationError, match="carries a refusal code exactly when"):
        Attempt.model_validate(_attempt(code=None))


def test_an_executed_attempt_naming_a_code_is_refused() -> None:
    """An answer cannot be both a result and a refusal."""
    with pytest.raises(ValidationError, match="carries a refusal code exactly when"):
        Attempt.model_validate(_attempt(executed=True))


def test_an_attempt_with_an_empty_note_is_refused() -> None:
    """An attempt nobody described is one a reader cannot check."""
    with pytest.raises(ValidationError):
        Attempt.model_validate(_attempt(note=""))


def test_a_milestone_whose_urn_names_another_record_is_refused() -> None:
    """A URN and a key that disagree address two Milestones and prove neither."""
    document = _recorded()
    document["milestone"]["key"] = "MLS-0002"
    with pytest.raises(ValidationError, match="addressed by a URN naming another record"):
        load_manifest(document)


def test_a_milestone_key_outside_the_grammar_is_refused() -> None:
    """The key is a symbol, not free text."""
    document = _recorded()
    document["milestone"]["key"] = "milestone-one"
    with pytest.raises(ValidationError):
        load_manifest(document)


def test_a_canary_at_epoch_one_is_refused() -> None:
    """A canary is born native; a tree at epoch 1 is not one."""
    document = _recorded()
    document["canary"]["epoch"] = 1
    with pytest.raises(ValidationError):
        load_manifest(document)


def test_a_first_unexecutable_step_outside_the_five_is_refused() -> None:
    """The steps are a closed set, so the summary cannot name a sixth."""
    document = _recorded()
    document["first_unexecutable_step"] = "release"
    with pytest.raises(ValidationError):
        load_manifest(document)


def test_a_negative_provider_count_is_refused() -> None:
    """The off-by-one boundary below zero, refused at the loader."""
    document = _recorded()
    document["provider_processes_started"] = -1
    with pytest.raises(ValidationError):
        load_manifest(document)


def test_an_unknown_key_is_refused() -> None:
    """The record forbids extras, so a typo fails rather than being dropped."""
    document = _recorded()
    document["rehearsed_by"] = "somebody"
    with pytest.raises(ValidationError):
        load_manifest(document)


def test_a_record_of_another_schema_version_is_refused() -> None:
    """A version this suite does not read is refused rather than read as if it were."""
    document = _recorded()
    document["schema_version"] = "canary-rehearsal/v2"
    with pytest.raises(ValidationError):
        load_manifest(document)


def test_an_empty_census_is_refused() -> None:
    """The empty boundary: a census with no rows covers nothing and claims everything."""
    document = _recorded()
    document["census"] = []
    with pytest.raises(ValidationError):
        load_manifest(document)


def test_a_record_that_is_not_a_mapping_is_refused() -> None:
    """The wrong-type boundary, refused at the loader rather than downstream."""
    with pytest.raises(ValidationError):
        load_manifest([])
