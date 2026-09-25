"""Accepting a Milestone on a sealed approval bound to the exact bundle.

Three claims are driven here, each from the angle that could actually
break it.

The approval covers exact bytes. ``domain.milestone.accept`` is dispatched
through the real handler against a provisioned canary, so the refusals are
the ones a client would receive. The PendingAction is seeded into the tree
and the bundle is presented with the request, which is the interesting
shape: a caller free to present any bundle still cannot accept one the
sealed approval did not record the digest of.

The resolver is a person. That is not asserted by inspection anywhere in
this file. A sealed row naming an agent does not validate, so the payload
never becomes a record, and the acceptance path refuses it as unreadable
rather than as disallowed -- which is the stronger answer.

Reading evidence writes nothing. Every file-writing entry point in the
process is replaced with one that raises, and the whole evidence read is
performed under that guard. A second test proves the guard reds on a real
write, because a guard that cannot fail proves nothing about the code
beneath it.

The approval is produced, not seeded. The last section opens the question
through the verb verify calls once the Milestone's Batches are verified,
seals it through the operator's verb, and accepts on exactly what those
two answered; the same opened question left unsealed is refused with
``protected_approval_required``, which is the gate's fire proof.

Nothing here spawns a daemon, opens a socket, or writes outside
``tmp_path``.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.acceptance import (
    FIRST_BUNDLE_REVISION,
    AcceptanceBundleLedger,
    AcceptanceStepOutcome,
    EvidenceRow,
    EvidenceView,
    MilestoneAcceptanceBundle,
)
from eawf.kernel.state.epoch2.milestone import MilestoneStatus
from eawf.kernel.state.epoch2.pending_action import (
    PendingAction,
    PendingActionKind,
    PendingActionStatus,
)
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_acceptance import (
    BUNDLE_KEY_PREFIX,
    AcceptanceRepairParams,
    open_acceptance_repair,
)
from eawf.runtime.daemon.methods.domain import DOMAIN_LIFECYCLE_METHODS
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from eawf.workflow.delivery.acceptance import (
    AcceptanceRefusal,
    AcceptanceRefusedError,
    acceptance_evidence,
    evidence_view,
    request_repair,
    require_sealed_acceptance,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    document_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

ACCEPT_METHOD: Final = "domain.milestone.accept"
ACTOR: Final = "OP-0001"
APPROVAL_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001"
RECEIPT_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"
INSTALL_EVIDENCE: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0002"
RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
OTHER_MILESTONE: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0031"

AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LATER: Final = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)

ACCEPTED_BINDING: Final[dict[str, Any]] = seed_row("milestone", "COMPLETED")["accepted_binding"]

OPERATOR: Final[dict[str, Any]] = {"principal_kind": "human", "principal_id": "OP-0001"}
AGENT: Final[dict[str, Any]] = {
    "principal_kind": "agent",
    "principal_id": "AG-0001",
    "run_ref": RUN_URN,
}

OPTIONS: Final[list[dict[str, Any]]] = [
    {"option_id": "approve", "label": "Accept the Milestone", "effect": "approve"},
    {"option_id": "decline", "label": "Do not accept it", "effect": "decline"},
    {"option_id": "repair", "label": "Ask for changes", "effect": "request_repair"},
]


# ---------- builders ----------


def step(
    *, step_id: str = "AS-01", passed: bool = True, evidence: str = INSTALL_EVIDENCE
) -> dict[str, Any]:
    """Return one acceptance-step outcome payload."""
    return {
        "step_id": step_id,
        "passed": passed,
        "observation": "the install completed and reported the version",
        "evidence_kinds": ["artifact"],
        "evidence_refs": [evidence] if passed or evidence else [],
    }


def bundle(
    *,
    milestone_ref: str = MILESTONE_URN,
    steps: list[dict[str, Any]] | None = None,
    sealed_at: datetime = AT,
) -> MilestoneAcceptanceBundle:
    """Return revision one of a Milestone's acceptance bundle."""
    return MilestoneAcceptanceBundle.model_validate(
        {
            "milestone_ref": milestone_ref,
            "revision": FIRST_BUNDLE_REVISION,
            "accepted_binding": ACCEPTED_BINDING,
            "steps": steps if steps is not None else [step()],
            "sealed_at": sealed_at,
        }
    )


def action_row(
    *,
    status: PendingActionStatus = PendingActionStatus.SEALED,
    digest: str | None = None,
    subject: str = MILESTONE_URN,
    chosen: str | None = "approve",
    resolver: dict[str, Any] | None = None,
    kind: PendingActionKind = PendingActionKind.PROTECTED_APPROVAL,
) -> dict[str, Any]:
    """Return one pending-action payload, sealed by the operator by default."""
    sealed = status is PendingActionStatus.SEALED
    row: dict[str, Any] = {
        "id": "ACT-0001",
        "kind": kind.value,
        "subject_ref": subject,
        "question": "Accept MLS-0030 on the bundle you just read?",
        "bundle_digest": bundle().digest() if digest is None else digest,
        "options": [dict(item) for item in OPTIONS],
        "idempotency_key": "req-accept-0001",
        "status": status.value,
        "requested_by": dict(AGENT),
        "created_at": AT.isoformat(),
        "updated_at": (LATER if sealed else AT).isoformat(),
    }
    if sealed:
        row["resolution_actor"] = dict(OPERATOR if resolver is None else resolver)
        row["selected_option_id"] = chosen
        row["receipt_ref"] = RECEIPT_URN
    return row


def action(**overrides: Any) -> PendingAction:
    """Return one validated pending action."""
    return PendingAction.model_validate(action_row(**overrides))


# ---------- the gate, held directly ----------


def test_a_sealed_approval_on_the_exact_digest_clears_the_acceptance() -> None:
    """The happy path: the bundle presented is the bundle approved."""
    approval = require_sealed_acceptance(
        action(),
        bundle=bundle(),
        milestone_ref=bundle().milestone_ref,
        status=MilestoneStatus.ACCEPTANCE_REVIEW,
    )

    assert approval.approved_digest == bundle().digest()
    assert approval.resolved_by.principal_id == "OP-0001"
    assert approval.bundle_revision == FIRST_BUNDLE_REVISION


def test_a_bundle_that_is_not_the_approved_one_is_refused() -> None:
    """Presenting the bundle buys nothing: a different one digests differently."""
    other = bundle(steps=[step(evidence=RECEIPT_URN)])
    with pytest.raises(AcceptanceRefusedError) as caught:
        require_sealed_acceptance(
            action(),
            bundle=other,
            milestone_ref=other.milestone_ref,
            status=MilestoneStatus.ACCEPTANCE_REVIEW,
        )

    assert caught.value.code is AcceptanceRefusal.BUNDLE_SUPERSEDED


def test_an_unsealed_approval_is_refused() -> None:
    """A question nobody answered is not an approval."""
    with pytest.raises(AcceptanceRefusedError) as caught:
        require_sealed_acceptance(
            action(status=PendingActionStatus.WAITING),
            bundle=bundle(),
            milestone_ref=bundle().milestone_ref,
            status=MilestoneStatus.ACCEPTANCE_REVIEW,
        )

    assert caught.value.code is AcceptanceRefusal.APPROVAL_UNSEALED


@pytest.mark.parametrize("chosen", ["decline", "repair"])
def test_an_answer_that_is_not_the_approving_one_is_refused(chosen: str) -> None:
    """Sealed is not the same as approved."""
    with pytest.raises(AcceptanceRefusedError) as caught:
        require_sealed_acceptance(
            action(chosen=chosen),
            bundle=bundle(),
            milestone_ref=bundle().milestone_ref,
            status=MilestoneStatus.ACCEPTANCE_REVIEW,
        )

    assert caught.value.code is AcceptanceRefusal.APPROVAL_WITHHELD


def test_an_approval_of_another_milestone_is_refused() -> None:
    """One approval covers one Milestone, named on the action itself."""
    with pytest.raises(AcceptanceRefusedError) as caught:
        require_sealed_acceptance(
            action(subject=OTHER_MILESTONE),
            bundle=bundle(),
            milestone_ref=bundle().milestone_ref,
            status=MilestoneStatus.ACCEPTANCE_REVIEW,
        )

    assert caught.value.code is AcceptanceRefusal.APPROVAL_MISBOUND


def test_an_operator_decision_is_not_an_acceptance_approval() -> None:
    """The kind decides whether a timeout could have answered it."""
    routing = action_row(kind=PendingActionKind.OPERATOR_DECISION)
    with pytest.raises(AcceptanceRefusedError) as caught:
        require_sealed_acceptance(
            PendingAction.model_validate(routing),
            bundle=bundle(),
            milestone_ref=bundle().milestone_ref,
            status=MilestoneStatus.ACCEPTANCE_REVIEW,
        )

    assert caught.value.code is AcceptanceRefusal.APPROVAL_NOT_PROTECTED


def test_a_bundle_whose_journey_did_not_pass_is_refused() -> None:
    """An approval does not make an undemonstrated outcome demonstrated."""
    open_journey = bundle(steps=[step(passed=False, evidence="")])
    approved = action(digest=open_journey.digest())
    with pytest.raises(AcceptanceRefusedError) as caught:
        require_sealed_acceptance(
            approved,
            bundle=open_journey,
            milestone_ref=open_journey.milestone_ref,
            status=MilestoneStatus.ACCEPTANCE_REVIEW,
        )

    assert caught.value.code is AcceptanceRefusal.JOURNEY_INCOMPLETE


@pytest.mark.parametrize(
    "status",
    [MilestoneStatus.PLANNED, MilestoneStatus.ACTIVE, MilestoneStatus.COMPLETED],
)
def test_an_acceptance_outside_review_is_refused(status: MilestoneStatus) -> None:
    """Acceptance is given in review and nowhere else."""
    with pytest.raises(AcceptanceRefusedError) as caught:
        require_sealed_acceptance(
            action(),
            bundle=bundle(),
            milestone_ref=bundle().milestone_ref,
            status=status,
        )

    assert caught.value.code is AcceptanceRefusal.APPROVAL_MISBOUND


# ---------- an agent resolver is not a record that exists ----------


def test_a_sealed_row_naming_an_agent_resolver_does_not_validate() -> None:
    """The refusal is at the loader: the payload never becomes an approval."""
    with pytest.raises(ValidationError) as caught:
        PendingAction.model_validate(action_row(resolver=dict(AGENT)))

    assert "resolution_actor" in str(caught.value)


def test_the_agent_refusal_has_teeth_because_the_operator_row_validates() -> None:
    """A guard that cannot pass proves nothing about the rows it admits."""
    approved = PendingAction.model_validate(action_row(resolver=dict(OPERATOR)))

    assert approved.status is PendingActionStatus.SEALED
    assert approved.resolution_actor is not None


# ---------- a repair keeps the prior revision and opens a successor ----------


def ledger_of(*bundles: MilestoneAcceptanceBundle) -> AcceptanceBundleLedger:
    """Return the Milestone's bundle chain."""
    return AcceptanceBundleLedger(milestone_ref=bundles[0].milestone_ref, bundles=bundles)


def test_a_repair_opens_the_next_revision() -> None:
    """Asking for changes makes a successor, not an edit."""
    revised = request_repair(
        ledger_of(bundle()),
        action=action(chosen="repair"),
        steps=[AcceptanceStepOutcome.model_validate(step())],
        accepted_binding=bundle().accepted_binding,
        at=LATER,
    )

    assert [item.revision for item in revised.bundles] == [1, 2]
    assert revised.bundles[-1].supersedes_revision == 1


def test_a_repair_leaves_the_prior_revision_byte_identical() -> None:
    """The bytes an approval was given to are exactly what they were."""
    prior = bundle()
    before = prior.digest()

    revised = request_repair(
        ledger_of(prior),
        action=action(chosen="repair"),
        steps=[AcceptanceStepOutcome.model_validate(step())],
        accepted_binding=prior.accepted_binding,
        at=LATER,
    )

    assert prior.digest() == before
    assert revised.bundles[0] == prior
    assert revised.bundles[0].digest() == before


def test_the_successor_records_the_digest_it_supersedes() -> None:
    """The chain is checkable, not merely declared."""
    prior = bundle()
    revised = request_repair(
        ledger_of(prior),
        action=action(chosen="repair"),
        steps=[AcceptanceStepOutcome.model_validate(step())],
        accepted_binding=prior.accepted_binding,
        at=LATER,
    )

    assert revised.bundles[-1].supersedes_digest == prior.digest()


def test_a_rewritten_predecessor_breaks_the_chain() -> None:
    """Immutability with teeth: the ledger refuses to read a rewritten revision."""
    prior = bundle()
    revised = request_repair(
        ledger_of(prior),
        action=action(chosen="repair"),
        steps=[AcceptanceStepOutcome.model_validate(step())],
        accepted_binding=prior.accepted_binding,
        at=LATER,
    )
    tampered = bundle(steps=[step(evidence=RECEIPT_URN)])

    with pytest.raises(ValidationError, match="the chain is broken"):
        AcceptanceBundleLedger.model_validate(
            {
                "milestone_ref": MILESTONE_URN,
                "bundles": [
                    tampered.model_dump(),
                    revised.bundles[-1].model_dump(),
                ],
            }
        )


def test_a_bundle_is_frozen() -> None:
    """The successor exists because the revision itself cannot be edited."""
    prior = bundle()
    with pytest.raises(ValidationError):
        prior.revision = 2  # type: ignore[misc]


def test_a_repair_needs_an_answer_that_asked_for_one() -> None:
    """A successor nobody asked for is a revision nobody agreed to."""
    with pytest.raises(AcceptanceRefusedError) as caught:
        request_repair(
            ledger_of(bundle()),
            action=action(chosen="approve"),
            steps=[AcceptanceStepOutcome.model_validate(step())],
            accepted_binding=bundle().accepted_binding,
            at=LATER,
        )

    assert caught.value.code is AcceptanceRefusal.APPROVAL_WITHHELD


def test_a_repair_needs_a_revision_to_supersede() -> None:
    """There is no successor to an empty chain."""
    empty = AcceptanceBundleLedger(milestone_ref=bundle().milestone_ref)
    with pytest.raises(AcceptanceRefusedError) as caught:
        request_repair(
            empty,
            action=action(chosen="repair"),
            steps=[AcceptanceStepOutcome.model_validate(step())],
            accepted_binding=bundle().accepted_binding,
            at=LATER,
        )

    assert caught.value.code is AcceptanceRefusal.BUNDLE_SUPERSEDED


def test_the_first_revision_cannot_claim_a_predecessor() -> None:
    """Revision one supersedes nothing, so it carries no predecessor fields."""
    with pytest.raises(ValidationError, match="supersedes nothing"):
        MilestoneAcceptanceBundle.model_validate(
            {
                "milestone_ref": MILESTONE_URN,
                "revision": FIRST_BUNDLE_REVISION,
                "supersedes_revision": 1,
                "accepted_binding": ACCEPTED_BINDING,
                "steps": [step()],
                "sealed_at": AT,
            }
        )


def test_a_bundle_needs_at_least_one_step() -> None:
    """A journey with no steps demonstrates nothing."""
    with pytest.raises(ValidationError, match=r"at least 1|too_short"):
        MilestoneAcceptanceBundle.model_validate(
            {
                "milestone_ref": MILESTONE_URN,
                "revision": FIRST_BUNDLE_REVISION,
                "accepted_binding": ACCEPTED_BINDING,
                "steps": [],
                "sealed_at": AT,
            }
        )


def test_a_passing_step_must_name_its_evidence() -> None:
    """A step claiming success with nothing behind it is the claim, not the proof."""
    with pytest.raises(ValidationError, match="passed and names no evidence"):
        AcceptanceStepOutcome.model_validate({**step(), "evidence_refs": []})


# ---------- reading the evidence view writes nothing ----------


class WroteAFileError(AssertionError):
    """Raised the moment anything under the guard reaches a file-writing entry point."""


#: The ``open`` modes that can change a file. A read-only open is left
#: alone, because a legitimate read still has to happen under the guard.
_WRITE_MODES: Final = frozenset("wxa+")

#: The ``os.open`` flags that can change a file.
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

_PATH_WRITERS: Final = (
    "write_text",
    "write_bytes",
    "mkdir",
    "touch",
    "unlink",
    "rename",
    "replace",
)
_OS_WRITERS: Final = ("remove", "unlink", "rename", "replace", "mkdir", "makedirs", "rmdir")
_SHUTIL_WRITERS: Final = ("copy", "copy2", "copyfile", "copytree", "move", "rmtree")


@pytest.fixture
def no_writes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replace every file-writing entry point with one that raises.

    The guard is structural rather than a reading of the source: it does
    not matter which module a write would come from, because the entry
    point itself is gone for the duration. ``monkeypatch`` restores every
    one of them afterwards.
    """
    real_open = builtins.open
    real_os_open = os.open

    def refuse(what: str) -> Callable[..., Any]:
        def blocked(*_args: Any, **_kwargs: Any) -> Any:
            raise WroteAFileError(f"{what} was called, so something wrote a file")

        return blocked

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if set(mode) & _WRITE_MODES:
            raise WroteAFileError(f"open({file!r}, {mode!r}) would write a file")
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & _WRITE_FLAGS:
            raise WroteAFileError(f"os.open({path!r}) would write a file")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    for name in _PATH_WRITERS:
        monkeypatch.setattr(Path, name, refuse(f"Path.{name}"))
    for name in _OS_WRITERS:
        monkeypatch.setattr(os, name, refuse(f"os.{name}"))
    for name in _SHUTIL_WRITERS:
        monkeypatch.setattr(shutil, name, refuse(f"shutil.{name}"))
    yield


def evidence_rows() -> list[dict[str, Any]]:
    """Return the evidence payloads the view is built over."""
    return [
        {
            "id": "EVD-0001",
            "kind": "decision",
            "summary": "the operator sealed the acceptance approval",
            "recorded_at": LATER.isoformat(),
        },
        {
            "id": "EVD-0002",
            "kind": "artifact",
            "summary": "the wheel installed into a clean environment",
            "recorded_at": AT.isoformat(),
        },
    ]


def test_building_and_reading_the_evidence_view_writes_no_file(no_writes: None) -> None:
    """Every ``EVD-####`` lookup happens with no write entry point in the process."""
    view = evidence_view(evidence_rows())

    assert view.keys == ("EVD-0001", "EVD-0002")
    row = view.row("EVD-0002")
    assert row is not None
    assert row.kind == "artifact"
    assert view.missing(("EVD-0002",)) == ()
    assert acceptance_evidence(view, bundle()) == ("EVD-0002",)


def test_the_no_write_guard_reds_on_a_real_write(no_writes: None, tmp_path: Path) -> None:
    """The guard has teeth: a guard that cannot fail proves nothing."""
    with pytest.raises(WroteAFileError):
        (tmp_path / "written.txt").write_text("x", encoding="utf-8")


def test_the_evidence_view_holds_no_path_or_handle() -> None:
    """Structural: there is no field a lookup could write through."""
    assert set(EvidenceView.model_fields) == {"rows"}
    assert set(EvidenceRow.model_fields) == {"id", "kind", "summary", "recorded_at"}


def test_reading_the_view_leaves_it_equal_to_itself(no_writes: None) -> None:
    """A lookup is arithmetic: the view after reading is the view before."""
    view = evidence_view(evidence_rows())
    before = view.model_dump(mode="json")

    view.row("EVD-0001")
    view.row("EVD-9999")
    view.missing(("EVD-0001", "EVD-9999"))

    assert view.model_dump(mode="json") == before


def test_an_unknown_evidence_key_reads_as_absent_rather_than_raising(no_writes: None) -> None:
    """A key the view does not hold is a miss, not an error."""
    view = evidence_view(evidence_rows())

    assert view.row("EVD-9999") is None
    assert view.missing(("EVD-9999",)) == ("EVD-9999",)


def test_a_bundle_citing_unheld_evidence_is_refused(no_writes: None) -> None:
    """A step demonstrated by a reference pointing at nothing is not demonstrated."""
    view = EvidenceView(rows=(EvidenceRow.model_validate(evidence_rows()[0]),))
    with pytest.raises(AcceptanceRefusedError) as caught:
        acceptance_evidence(view, bundle())

    assert caught.value.code is AcceptanceRefusal.EVIDENCE_UNHELD


def test_the_view_refuses_a_key_held_twice() -> None:
    """Two rows under one key make the lookup depend on iteration order."""
    rows = evidence_rows()
    with pytest.raises(ValidationError, match="held more than once"):
        evidence_view([rows[0], rows[0]])


def test_the_view_refuses_a_key_outside_the_evd_grammar() -> None:
    """The evidence key grammar has one home, and this alias delegates to it."""
    with pytest.raises(ValidationError):
        evidence_view([{**evidence_rows()[0], "id": "EV-0001"}])


def test_the_empty_view_holds_nothing_and_answers_every_key_missing() -> None:
    """The empty boundary answers rather than raising."""
    view = EvidenceView()

    assert view.keys == ()
    assert view.row("EVD-0001") is None
    assert view.missing(("EVD-0001",)) == ("EVD-0001",)


# ---------- the verb refuses through the real handler ----------


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one Milestone in review and one sealed approval."""
    provisioned = provision(tmp_path / "accept", code="ACCEPT")
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            Epoch2Collection.PENDING_ACTION.value: {"ACT-0001": action_row()},
        },
    )
    return provisioned


def accept(canary: CanaryProvision, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """Dispatch the acceptance verb and return the machine envelope."""
    ctx = method_context(tmp_path / "runtime")
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": MILESTONE_URN,
        "expected_revision": 1,
        "idempotency_key": "req-accept-0001",
        "actor": ACTOR,
        "updates": {"accepted_binding": ACCEPTED_BINDING},
        "approval_receipt_ref": APPROVAL_URN,
        "acceptance_bundle": bundle().model_dump(mode="json"),
    }
    params.update(overrides)
    return asyncio.run(methods.dispatch(ACCEPT_METHOD, ctx, params))


def refusal(answer: dict[str, Any]) -> dict[str, Any]:
    """Return the single error row of a refused envelope."""
    assert answer["status"] == "error", answer
    return dict(answer["errors"][0])


def test_the_acceptance_verb_is_registered() -> None:
    """The suite drives the real handler, not a stand-in for it."""
    assert ACCEPT_METHOD in DOMAIN_LIFECYCLE_METHODS
    assert ACCEPT_METHOD in methods.registered_methods()


def test_the_acceptance_verb_clears_on_the_approved_bundle(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The gate passes when the approval covers exactly what is presented."""
    answer = accept(canary, tmp_path)

    assert answer["status"] == "ok", answer
    assert answer["revision_after"] == 2


def test_the_acceptance_verb_refuses_a_bundle_the_approval_did_not_cover(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Presenting a different bundle refuses: the digest is the binding."""
    before = document_path(canary).read_bytes()
    other = bundle(steps=[step(evidence=RECEIPT_URN)])

    answer = accept(canary, tmp_path, acceptance_bundle=other.model_dump(mode="json"))

    row = refusal(answer)
    assert row["code"] == DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value
    assert AcceptanceRefusal.BUNDLE_SUPERSEDED.value in row["message"]
    assert document_path(canary).read_bytes() == before


def test_the_acceptance_verb_refuses_when_no_bundle_is_presented(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The approval is held against a bundle, so one has to arrive."""
    answer = accept(canary, tmp_path, acceptance_bundle=None)

    assert refusal(answer)["code"] == DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value


def test_the_acceptance_verb_refuses_when_the_tree_holds_no_such_action(
    tmp_path: Path,
) -> None:
    """The approval is read from the tree, so a reference to nothing refuses."""
    provisioned = provision(tmp_path / "bare", code="BARE")
    seed(provisioned, {"milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")}})

    row = refusal(accept(provisioned, tmp_path))

    assert row["code"] == DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value
    assert "ACT-0001" in row["message"]


def test_the_acceptance_verb_refuses_an_unsealed_action(tmp_path: Path) -> None:
    """A question still waiting for an answer is not an approval."""
    provisioned = provision(tmp_path / "waiting", code="WAIT")
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            Epoch2Collection.PENDING_ACTION.value: {
                "ACT-0001": action_row(status=PendingActionStatus.WAITING)
            },
        },
    )

    row = refusal(accept(provisioned, tmp_path))

    assert AcceptanceRefusal.APPROVAL_UNSEALED.value in row["message"]


def test_the_acceptance_verb_refuses_an_action_naming_an_agent_resolver(
    tmp_path: Path,
) -> None:
    """A seeded agent row is not a readable pending action, so it never approves."""
    provisioned = provision(tmp_path / "agent", code="AGENT")
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            Epoch2Collection.PENDING_ACTION.value: {"ACT-0001": action_row(resolver=dict(AGENT))},
        },
    )

    row = refusal(accept(provisioned, tmp_path))

    assert row["code"] == DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value
    assert "cannot read ACT-0001" in row["message"]


def test_the_acceptance_verb_still_refuses_a_request_naming_no_approval(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The oldest refusal keeps its shape: no reference, no acceptance."""
    answer = accept(canary, tmp_path, approval_receipt_ref=None)

    row = refusal(answer)
    assert row["code"] == DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value
    assert row["guard"] == "acceptance_journey_passed"
    assert "PendingAction" in row["remediation"]


# ---------- the repair verb files the successor on the Milestone ledger ----------


def bundle_line(item: MilestoneAcceptanceBundle) -> LedgerRecord:
    """Return the Milestone-ledger line one bundle revision is filed as."""
    return LedgerRecord(
        collection=Epoch2Collection.MILESTONE,
        record_key=f"{BUNDLE_KEY_PREFIX}{item.revision:04d}-{item.milestone_ref.entity_key}",
        status=f"revision-{item.revision}",
        recorded_at=item.sealed_at,
        payload=item.model_dump(mode="json"),
    )


def evidence_line(payload: dict[str, Any]) -> LedgerRecord:
    """Return the evidence-ledger line one observation is filed as."""
    return LedgerRecord(
        collection=Epoch2Collection.EVIDENCE,
        record_key=payload["id"],
        status="recorded",
        recorded_at=datetime.fromisoformat(payload["recorded_at"]),
        payload=payload,
    )


def repairing_tree(tmp_path: Path, *, code: str, chosen: str = "repair") -> Epoch2RootContext:
    """Return a canary holding one bundle revision, its evidence and the answer."""
    provisioned = provision(tmp_path / code.lower(), code=code)
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            Epoch2Collection.PENDING_ACTION.value: {"ACT-0001": action_row(chosen=chosen)},
        },
    )
    context = root_context(provisioned, tmp_path / "runtime")
    with context.session([MILESTONE_URN]) as session:
        append_ledger_record(session.ledger_path(Epoch2Collection.MILESTONE), bundle_line(bundle()))
        for payload in evidence_rows():
            append_ledger_record(
                session.ledger_path(Epoch2Collection.EVIDENCE), evidence_line(payload)
            )
    return context


def repair_params(**overrides: Any) -> AcceptanceRepairParams:
    """Return the validated request one repair pass runs on."""
    params: dict[str, Any] = {
        "urn": MILESTONE_URN,
        "actor": ACTOR,
        "idempotency_key": "req-repair-0001",
        "approval_ref": APPROVAL_URN,
        "steps": [step()],
        "accepted_binding": ACCEPTED_BINDING,
    }
    params.update(overrides)
    return AcceptanceRepairParams.model_validate(params)


def test_the_repair_verb_files_the_successor_and_keeps_the_prior_digest(
    tmp_path: Path,
) -> None:
    """Both revisions are readable, and the one that was approved is unchanged."""
    context = repairing_tree(tmp_path, code="REPAIR")

    answer = open_acceptance_repair(context, repair_params(), now=LATER)

    assert answer.from_revision == 1
    assert answer.to_revision == 2
    assert answer.prior_digest == bundle().digest()
    assert answer.successor_digest != answer.prior_digest
    assert answer.evidence_keys == ("EVD-0002",)


def test_the_repair_verb_appends_rather_than_rewriting(tmp_path: Path) -> None:
    """The prior line is still on the ledger, byte-identical to what it was."""
    context = repairing_tree(tmp_path, code="APPEND")

    open_acceptance_repair(context, repair_params(), now=LATER)

    with context.session([MILESTONE_URN]) as session:
        path = session.ledger_path(Epoch2Collection.MILESTONE)
    filed = {
        item.record_key: item.payload
        for item in read_ledger_records(path)
        if item.record_key.startswith(BUNDLE_KEY_PREFIX)
    }
    assert sorted(filed) == ["MAB-0001-MLS-0030", "MAB-0002-MLS-0030"]
    assert filed["MAB-0001-MLS-0030"] == bundle().model_dump(mode="json")


def test_the_repair_verb_refuses_an_answer_that_approved(tmp_path: Path) -> None:
    """A successor nobody asked for is a revision nobody agreed to."""
    context = repairing_tree(tmp_path, code="NOASK", chosen="approve")

    with pytest.raises(DaemonValidationError, match="acceptance_approval_withheld"):
        open_acceptance_repair(context, repair_params(), now=LATER)


def test_the_repair_verb_refuses_an_approval_the_tree_does_not_hold(
    tmp_path: Path,
) -> None:
    """The answer is read from the tree, so a reference to nothing refuses."""
    provisioned = provision(tmp_path / "bareact", code="BAREACT")
    seed(provisioned, {"milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")}})
    context = root_context(provisioned, tmp_path / "runtime")

    with pytest.raises(DaemonValidationError, match="acceptance_approval_unsealed"):
        open_acceptance_repair(context, repair_params(), now=LATER)


def test_the_repair_verb_refuses_a_successor_citing_unheld_evidence(
    tmp_path: Path,
) -> None:
    """Every ``EVD-####`` the successor cites is resolved before it is filed."""
    context = repairing_tree(tmp_path, code="UNHELD")
    unheld = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0404"

    with pytest.raises(DaemonValidationError, match="acceptance_evidence_unheld"):
        open_acceptance_repair(context, repair_params(steps=[step(evidence=unheld)]), now=LATER)


def test_the_repair_request_refuses_an_unknown_key() -> None:
    """The request model is strict, and a misspelled field is a rejection."""
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        repair_params(reason="because")


def test_the_repair_request_needs_at_least_one_step() -> None:
    """A repaired journey with no steps demonstrates nothing."""
    with pytest.raises(ValidationError, match=r"at least 1|too_short"):
        repair_params(steps=[])


# ---------- the approval verify opens and the operator seals ----------

OPEN_METHOD: Final = "runtime.delivery.open_acceptance_approval"
SEAL_METHOD: Final = "runtime.delivery.seal_acceptance_approval"


def verified_canary(
    tmp_path: Path, *, code: str, batch_status: str = "READY_TO_MERGE"
) -> CanaryProvision:
    """Return a canary holding MLS-0030 in review, one Batch and its evidence rows."""
    provisioned = provision(tmp_path / code.lower(), code=code)
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            "batch": {"BAT-0007": seed_row("batch", batch_status)},
        },
    )
    context = root_context(provisioned, tmp_path / "runtime")
    with context.session([MILESTONE_URN]) as session:
        for payload in evidence_rows():
            append_ledger_record(
                session.ledger_path(Epoch2Collection.EVIDENCE), evidence_line(payload)
            )
    return provisioned


def call(canary: CanaryProvision, tmp_path: Path, method: str, **params: Any) -> dict[str, Any]:
    """Dispatch one verb through the real handler against *canary*."""
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def open_approval(canary: CanaryProvision, tmp_path: Path) -> dict[str, Any]:
    """Open the question the way verify does once the Batches are verified."""
    return call(
        canary,
        tmp_path,
        OPEN_METHOD,
        urn=MILESTONE_URN,
        actor="SKILL-VERIFY",
        requested_by=dict(OPERATOR),
        steps=[step()],
        accepted_binding=ACCEPTED_BINDING,
    )


def seal_approval(
    canary: CanaryProvision, tmp_path: Path, opened: dict[str, Any], *, chosen: str = "approve"
) -> dict[str, Any]:
    """Seal the opened question with the operator's answer."""
    return call(
        canary,
        tmp_path,
        SEAL_METHOD,
        urn=opened["action_ref"],
        expected_revision=opened["revision"],
        idempotency_key="req-seal-0001",
        actor=ACTOR,
        resolver=dict(OPERATOR),
        option_id=chosen,
        receipt_ref=RECEIPT_URN,
    )


def accept_on(canary: CanaryProvision, tmp_path: Path, opened: dict[str, Any]) -> dict[str, Any]:
    """Accept the Milestone on the approval and bundle the open verb answered with."""
    return accept(
        canary,
        tmp_path,
        approval_receipt_ref=opened["action_ref"],
        acceptance_bundle=opened["acceptance_bundle"],
    )


def test_the_approval_verbs_are_registered() -> None:
    """The suite drives the real producer handlers, not stand-ins for them."""
    assert {OPEN_METHOD, SEAL_METHOD} <= set(methods.registered_methods())


def test_a_verify_sealed_approval_clears_the_acceptance(tmp_path: Path) -> None:
    """End to end: verify opens, the operator seals, acceptance reads it and moves."""
    canary = verified_canary(tmp_path, code="E2E")
    opened = open_approval(canary, tmp_path)
    sealed = seal_approval(canary, tmp_path, opened)

    answer = accept_on(canary, tmp_path, opened)

    assert opened["created"] is True
    assert sealed["status"] == PendingActionStatus.SEALED.value
    assert answer["status"] == "ok", answer
    assert answer["revision_after"] == 2


def test_the_acceptance_is_denied_while_the_seal_is_absent(tmp_path: Path) -> None:
    """Gate-fire: the same opened question, unanswered, refuses the acceptance."""
    canary = verified_canary(tmp_path, code="NOSEAL")
    opened = open_approval(canary, tmp_path)
    before = document_path(canary).read_bytes()

    row = refusal(accept_on(canary, tmp_path, opened))

    assert row["code"] == DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value
    assert AcceptanceRefusal.APPROVAL_UNSEALED.value in row["message"]
    assert document_path(canary).read_bytes() == before


def test_a_declined_answer_does_not_clear_the_acceptance(tmp_path: Path) -> None:
    """A sealed no is still a no."""
    canary = verified_canary(tmp_path, code="DECLINE")
    opened = open_approval(canary, tmp_path)
    seal_approval(canary, tmp_path, opened, chosen="decline")

    row = refusal(accept_on(canary, tmp_path, opened))

    assert row["code"] == DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value
    assert AcceptanceRefusal.APPROVAL_WITHHELD.value in row["message"]


def test_a_bundle_other_than_the_sealed_one_is_refused(tmp_path: Path) -> None:
    """The produced seal binds the exact bytes verify filed, and nothing else."""
    canary = verified_canary(tmp_path, code="MISBOUND")
    opened = open_approval(canary, tmp_path)
    seal_approval(canary, tmp_path, opened)
    other = bundle(steps=[step(evidence=RECEIPT_URN)]).model_dump(mode="json")

    row = refusal(
        accept(canary, tmp_path, approval_receipt_ref=opened["action_ref"], acceptance_bundle=other)
    )

    assert AcceptanceRefusal.BUNDLE_SUPERSEDED.value in row["message"]


def test_reverifying_the_same_batches_files_no_duplicate(tmp_path: Path) -> None:
    """Asking about the same bytes twice finds the standing question and writes nothing."""
    canary = verified_canary(tmp_path, code="TWICE")
    first = open_approval(canary, tmp_path)
    before = document_path(canary).read_bytes()

    second = open_approval(canary, tmp_path)

    assert second["created"] is False
    assert second["action_ref"] == first["action_ref"]
    assert second["bundle_digest"] == first["bundle_digest"]
    assert document_path(canary).read_bytes() == before
    rows = read_document(document_path(canary))[Epoch2Collection.PENDING_ACTION.value]
    assert sorted(rows) == ["ACT-0001"]


def test_a_changed_journey_is_refused_rather_than_refiled(tmp_path: Path) -> None:
    """A filed revision is never rewritten; a different journey is a repair."""
    canary = verified_canary(tmp_path, code="DIVERGE")
    open_approval(canary, tmp_path)

    with pytest.raises(DaemonValidationError, match="acceptance_bundle_diverged"):
        call(
            canary,
            tmp_path,
            OPEN_METHOD,
            urn=MILESTONE_URN,
            actor="SKILL-VERIFY",
            requested_by=dict(OPERATOR),
            steps=[step(evidence=RECEIPT_URN)],
            accepted_binding=ACCEPTED_BINDING,
        )


@pytest.mark.parametrize("status", ["ACTIVE", "FAILED"])
def test_the_question_is_not_opened_while_a_batch_is_unverified(
    tmp_path: Path, status: str
) -> None:
    """Verify asks only once the Milestone's Batches stand on a verified head."""
    canary = verified_canary(tmp_path, code="UNVERIFIED", batch_status=status)
    before = document_path(canary).read_bytes()

    with pytest.raises(DaemonValidationError, match="acceptance_batches_unverified"):
        open_approval(canary, tmp_path)

    assert document_path(canary).read_bytes() == before


def test_the_question_is_not_opened_on_unheld_evidence(tmp_path: Path) -> None:
    """A journey citing evidence the tree does not hold is not put to the operator."""
    canary = verified_canary(tmp_path, code="NOEVD")
    unheld = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0404"

    with pytest.raises(DaemonValidationError, match="acceptance_evidence_unheld"):
        call(
            canary,
            tmp_path,
            OPEN_METHOD,
            urn=MILESTONE_URN,
            actor="SKILL-VERIFY",
            requested_by=dict(OPERATOR),
            steps=[step(evidence=unheld)],
            accepted_binding=ACCEPTED_BINDING,
        )
