"""A Task's title, description, role and effort labels.

The labels are optional on the create document, the stored Task and the
planned Task, they stay out of the dump while unset so stored rows and
approved plan digests do not move, and a Task stored without a title
reads one from its intent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from eawf.kernel.projection.row_facts import title_of
from eawf.kernel.state.enums import EffortBucket
from eawf.kernel.state.epoch2.plan_revision import PlanBody, PlannedTask, plan_content_digest
from eawf.kernel.state.epoch2.task import Task, TaskCreateSpec, TaskStatus, derived_title
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands import domain as domain_cmd
from eawf.surfaces.cli.commands import domain_target
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.workflow.planning.materialise import _planned_task
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import rekeyed, seed_row
from tests.unit.workflow.planning.lenses.test_ownership_lens import SLOT, _task, body_payload

pytestmark = pytest.mark.unit

AT = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

#: A copy of the applied PRV-0303 plan body as the generation stores it.
PRV_0303_BODY = Path(__file__).parent / "fixtures" / "prv-0303-body.json"

#: The digest the stored PRV-0303 body yields before the labels existed.
PRV_0303_DIGEST = "sha256:97fd820d44ac30a0ad2889e19f68e6285d8f4222076501eaeedded63fe5e2e39"

LABELS: dict[str, str] = {
    "title": "Give a Task a title",
    "description": "Split the packed intent into a title and a description.",
    "role": "executor",
    "effort": "S",
}


def _create_document(**extra: Any) -> dict[str, Any]:
    """Return a valid Task create document carrying *extra*."""
    return {"key": "EAWF-0001", "priority": "P1", "intent": "do the work", **extra}


def _canonical(payload: Any) -> bytes:
    """Return *payload* as sorted-key JSON bytes, the way a row is compared."""
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)


# ---- CR-0481-1: optional, bounded and byte-stable ---------------------------


def test_title_fields_are_optional_and_byte_stable() -> None:
    spec = TaskCreateSpec.model_validate(_create_document(**LABELS))
    assert (spec.title, spec.role, spec.effort) == (
        "Give a Task a title",
        "executor",
        EffortBucket.S,
    )
    planned = PlannedTask.model_validate({**_task(f"{SLOT}/task/EAWF-0001"), **LABELS})
    assert planned.description == LABELS["description"]
    stored = Task.model_validate({**seed_row("task", "DRAFT"), **LABELS})
    assert stored.model_dump(mode="json")["title"] == LABELS["title"]

    # Unset labels never reach the dump, so an old row re-serialises byte for byte.
    for status in ("DRAFT", "PLANNED"):
        row = Task.model_validate(seed_row("task", status)).model_dump(mode="json")
        assert set(row).isdisjoint(LABELS)
        assert _canonical(Task.model_validate(row).model_dump(mode="json")) == _canonical(row)
    row = Task.model_validate(seed_row("task", "DRAFT")).model_dump(mode="json")
    assert _canonical(Task.model_validate(row).model_dump(mode="json")) == _canonical(row)
    bare = TaskCreateSpec.model_validate(_create_document()).model_dump(mode="json")
    assert set(bare).isdisjoint(LABELS)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "x" * 81),
        ("title", "   "),
        ("title", 7),
        ("description", ""),
        ("role", "planner"),
        ("role", "Executor"),
        ("effort", "XXL"),
        ("effort", "s"),
    ],
)
def test_title_fields_refuse_out_of_range_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        TaskCreateSpec.model_validate(_create_document(**{field: value}))
    with pytest.raises(ValidationError):
        PlannedTask.model_validate({**_task(f"{SLOT}/task/EAWF-0001"), field: value})


def test_title_fields_accept_the_boundary_values() -> None:
    longest = TaskCreateSpec.model_validate(_create_document(title="x" * 80))
    assert longest.title == "x" * 80
    single = TaskCreateSpec.model_validate(_create_document(title="x", description="y"))
    assert (single.title, single.description) == ("x", "y")
    for role in ("executor", "auditor", "researcher", "domain-specialist"):
        assert TaskCreateSpec.model_validate(_create_document(role=role)).role == role
    for effort in EffortBucket:
        assert TaskCreateSpec.model_validate(_create_document(effort=effort.value)).effort is effort


def test_title_fields_refuse_an_unknown_label() -> None:
    with pytest.raises(ValidationError):
        TaskCreateSpec.model_validate(_create_document(label="x"))


# ---- CR-0481-2: an approved plan keeps its digest ---------------------------


def test_an_approved_plan_digest_is_unchanged() -> None:
    body = PlanBody.model_validate(orjson.loads(PRV_0303_BODY.read_bytes()))
    assert plan_content_digest(body) == PRV_0303_DIGEST
    assert all(set(task).isdisjoint(LABELS) for task in body.model_dump(mode="json")["tasks"])


# ---- CR-0481-3: create and apply carry the labels ---------------------------


def _sent_create_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, document: dict[str, Any], **flags: Any
) -> dict[str, Any]:
    """Run ``task create`` over *document* with *flags* and return what it sends."""
    sent: list[dict[str, Any]] = []

    def _capture(request: domain_cmd.DomainCreateRequest, *, flags: GlobalFlags) -> object:
        sent.append(request.document)
        return object()

    monkeypatch.setattr(domain_cmd, "_call_domain_create", _capture)
    monkeypatch.setattr(domain_cmd, "preview", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(domain_cmd, "emit_envelope", lambda *_args, **_kwargs: None)
    spec_path = tmp_path / "task.json"
    spec_path.write_bytes(orjson.dumps(document))
    domain_target.task_create_cmd(
        SimpleNamespace(obj=GlobalFlags(json_output=True, no_input=True)),  # type: ignore[arg-type]
        urn=f"{SLOT}/task/EAWF-0001",
        expected_revision=0,
        idempotency_key="create-eawf-0001",
        actor="OPERATOR",
        from_spec=spec_path,
        yes=True,
        **flags,
    )
    assert len(sent) == 1
    return sent[0]


def test_create_and_apply_carry_title_and_description(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = _sent_create_document(
        monkeypatch, tmp_path, _create_document(), title="T", description="D"
    )
    spec = TaskCreateSpec.model_validate(document)
    assert (spec.key, spec.priority.value, spec.title, spec.description) == (
        "EAWF-0001",
        "P1",
        "T",
        "D",
    )

    payload = body_payload()
    payload["tasks"][0].update(LABELS)
    body = PlanBody.model_validate(payload)
    created = _planned_task({}, body=body, task=body.tasks[0], at=AT)
    assert (created.title, created.description, created.role, created.effort) == (
        LABELS["title"],
        LABELS["description"],
        "executor",
        EffortBucket.S,
    )

    payload = body_payload()
    payload["tasks"][1].update(title="T", description="D")
    body = PlanBody.model_validate(payload)
    draft = rekeyed(seed_row("task", "DRAFT"), key="EAWF-0002")
    promoted = _planned_task({"task": {"EAWF-0002": draft}}, body=body, task=body.tasks[1], at=AT)
    assert (promoted.status, promoted.title, promoted.description) == (
        TaskStatus.PLANNED,
        "T",
        "D",
    )


def test_create_flags_agreeing_with_the_document_are_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = _sent_create_document(monkeypatch, tmp_path, _create_document(title="T"), title="T")
    assert document["title"] == "T"
    bare = _sent_create_document(monkeypatch, tmp_path, _create_document())
    assert set(bare).isdisjoint(LABELS)


def test_create_flag_disagreeing_with_the_document_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    errors: list[cli_errors.CliError] = []

    def _record(exc: cli_errors.CliError, *, flags: GlobalFlags) -> None:
        errors.append(exc)

    monkeypatch.setattr(cli_errors, "emit_error", _record)
    monkeypatch.setattr(
        domain_cmd, "_call_domain_create", lambda *_a, **_k: pytest.fail("sent a refused create")
    )
    spec_path = tmp_path / "task.json"
    spec_path.write_bytes(orjson.dumps(_create_document(description="D")))
    domain_target.task_create_cmd(
        SimpleNamespace(obj=GlobalFlags(json_output=True, no_input=True)),  # type: ignore[arg-type]
        urn=f"{SLOT}/task/EAWF-0001",
        expected_revision=0,
        idempotency_key="create-eawf-0001",
        actor="OPERATOR",
        from_spec=spec_path,
        description="E",
        yes=True,
    )
    assert [str(error) for error in errors] == [
        "--description E disagrees with the create document's description 'D'"
    ]


def test_apply_keeps_a_draft_title_the_plan_leaves_unset() -> None:
    body = PlanBody.model_validate(body_payload())
    draft = {**rekeyed(seed_row("task", "DRAFT"), key="EAWF-0002"), "title": "Kept"}
    promoted = _planned_task({"task": {"EAWF-0002": draft}}, body=body, task=body.tasks[1], at=AT)
    assert promoted.title == "Kept"


# ---- CR-0481-4: an untitled Task reads its title from its intent ------------


def test_title_derives_from_intent_when_unset() -> None:
    row = seed_row("task", "DRAFT")
    stored = Task.model_validate({**row, "intent": "Split the intent: give a Task a title"})
    assert stored.title is None
    assert derived_title(stored.intent) == "Split the intent"
    shown = stored.model_dump(mode="json")
    assert title_of(Epoch2Collection.TASK, shown) == "Split the intent"
    assert title_of(Epoch2Collection.TASK, {**shown, "title": "Own title"}) == "Own title"
    assert derived_title("no separator here") == "no separator here"
    assert derived_title("a" * 120) == "a" * 80
    assert derived_title(f"{'b' * 90}: rest") == "b" * 80
    assert derived_title("a: b: c") == "a"
    assert derived_title("x") == "x"
    assert derived_title("ratio 3:4 holds") == "ratio 3:4 holds"
