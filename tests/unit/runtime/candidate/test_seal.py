"""RUN-010: a bundle exists only where all six seal checks hold.

The suite drives ``executor-success.json`` twice. Once forwards: the
submission and the accepted report the fixture describes seal, and the
bundle names every check. Then once per unsealing case, each of which
moves exactly one durable fact and asserts the exact set of checks that
stops holding -- so a check reading the wrong record, or left out of the
table, changes an answer here rather than passing quietly.

Nothing here opens a tree. The seal decision is a function of three
records, so the records are built directly and the decision is taken on
them, which is also what makes the table's totality assertable at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.candidate import (
    DELIVERABLE_VERDICTS,
    CandidateBundle,
    CandidateReportBinding,
    CandidateSubmission,
    SealCheck,
    candidate_identity,
)
from eawf.kernel.runtime.lease import LeaseStatus, WorkLease
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.runtime.candidate.seal import (
    SEAL_CHECKS,
    SealCheckTableError,
    SealInputs,
    SealOutcome,
    compile_seal_checks,
    failed_seal_checks,
    path_within_roots,
    seal_candidate,
)

pytestmark = pytest.mark.unit


FIXTURE_ROOT: Final = Path(__file__).resolve().parents[3] / "fixtures" / "runtime_contract" / "v1"

RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
TASK_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
LEASE_ID: Final = "LSE-" + "1" * 32
WORKSPACE_HANDLE: Final = "wsh-" + "2" * 32
BASE_COMMIT: Final = "9f" * 20
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LATER: Final = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)


def fixture(name: str) -> dict[str, Any]:
    """Return one runtime-delivery fixture body."""
    return json.loads((FIXTURE_ROOT / f"{name}.json").read_text(encoding="utf-8"))


SUCCESS: Final = fixture("executor-success")


def submission(**overrides: Any) -> CandidateSubmission:
    """Return the fixture's submission, with *overrides* applied."""
    claim = SUCCESS["submission"]
    digest = overrides.pop("resulting_tree_digest", claim["resulting_tree_digest"])
    fields: dict[str, Any] = {
        "candidate_ref": candidate_identity(task_ref=TASK_URN, resulting_tree_digest=digest),
        "run_ref": RUN_URN,
        "task_ref": TASK_URN,
        "lease_id": LEASE_ID,
        "workspace_handle": WORKSPACE_HANDLE,
        "workspace_generation": 1,
        "base_commit": BASE_COMMIT,
        "submission_ref": claim["submission_ref"],
        "changed_paths": tuple(claim["changed_paths"]),
        "resulting_tree_digest": digest,
        "submitted_at": AT,
    }
    fields.update(overrides)
    return CandidateSubmission.model_validate(fields)


def binding(**overrides: Any) -> CandidateReportBinding:
    """Return the fixture's accepted report, with *overrides* applied."""
    report = SUCCESS["report"]
    fields: dict[str, Any] = {
        "candidate_ref": submission().candidate_ref,
        "run_ref": RUN_URN,
        "report_schema_ref": report["report_schema_ref"],
        "report_digest": report["report_digest"],
        "verdict": report["verdict"],
        "resulting_tree_digest": report["resulting_tree_digest"],
        "bound_at": LATER,
    }
    fields.update(overrides)
    return CandidateReportBinding.model_validate(fields)


def lease(**overrides: Any) -> WorkLease:
    """Return the lease the fixture's write set describes."""
    fields: dict[str, Any] = {
        "lease_id": LEASE_ID,
        "run_ref": RUN_URN,
        "task_ref": TASK_URN,
        "purpose": RunPurpose.IMPLEMENT,
        "workspace_handle": WORKSPACE_HANDLE,
        "workspace_generation": 1,
        "branch": "run/RUN-00000010",
        "base_commit": BASE_COMMIT,
        "writable_roots": tuple(SUCCESS["write_set"]),
        "issued_at": AT,
        "heartbeat_at": AT,
        "expires_at": datetime(2026, 9, 18, 14, 0, tzinfo=UTC),
        "status_at": AT,
        "status": LeaseStatus.ACTIVE,
    }
    fields.update(overrides)
    return WorkLease.model_validate(fields)


def inputs_for(case: dict[str, Any]) -> SealInputs:
    """Build the seal inputs one unsealing case describes.

    The mutation names which of the three records moves, so a case that
    names an unknown mutation fails loudly rather than being read as the
    unmutated happy path.
    """
    mutation = case["mutation"]
    value = case.get("value")
    if mutation == "omit_report":
        return SealInputs(submission=submission(), binding=None, lease=lease())
    if mutation == "report_resulting_tree_digest":
        return SealInputs(
            submission=submission(), binding=binding(resulting_tree_digest=value), lease=lease()
        )
    if mutation == "verdict":
        return SealInputs(submission=submission(), binding=binding(verdict=value), lease=lease())
    if mutation == "changed_paths":
        return SealInputs(
            submission=submission(changed_paths=tuple(value)), binding=binding(), lease=lease()
        )
    if mutation == "lease_workspace_generation":
        return SealInputs(
            submission=submission(), binding=binding(), lease=lease(workspace_generation=value)
        )
    if mutation == "lease_base_commit":
        return SealInputs(
            submission=submission(), binding=binding(), lease=lease(base_commit=value)
        )
    raise AssertionError(f"the fixture names an unknown mutation {mutation!r}")


# ---- the table is total and cannot drift ------------------------------------


def test_seal_checks_answers_every_declared_check() -> None:
    """The compiled table is the one place the six checks are listed."""
    assert set(SEAL_CHECKS) == set(SealCheck)


def test_the_fixture_names_the_same_six_checks_the_enumeration_does() -> None:
    """A check renamed in code and not in the contract fixture reds here."""
    assert [check.value for check in SealCheck] == SUCCESS["seal_checks"]


def test_compile_seal_checks_refuses_a_table_missing_one_check() -> None:
    """A check with no evaluator would seal without the proof it names."""
    partial = {
        check: SEAL_CHECKS[check] for check in SealCheck if check is not SealCheck.REPORT_BOUND
    }

    with pytest.raises(SealCheckTableError, match=SealCheck.REPORT_BOUND.value):
        compile_seal_checks(partial)


def test_compile_seal_checks_refuses_an_empty_table() -> None:
    """The boundary case of no evaluators at all names every check."""
    with pytest.raises(SealCheckTableError, match=SealCheck.BASE_COMMIT_AGREES.value):
        compile_seal_checks({})


def test_compile_seal_checks_returns_a_total_table_unchanged() -> None:
    """A table that answers every check is handed straight back."""
    assert compile_seal_checks(SEAL_CHECKS) is SEAL_CHECKS


# ---- the happy path ---------------------------------------------------------


def test_the_fixture_submission_and_report_seal() -> None:
    """Every check holds, so a bundle exists and names no failure."""
    outcome = seal_candidate(
        SealInputs(submission=submission(), binding=binding(), lease=lease()), now=LATER
    )

    assert outcome.sealed is SUCCESS["expected"]["sealed"]
    assert outcome.failed_checks == tuple(SUCCESS["expected"]["failed_checks"])
    assert outcome.bundle is not None
    assert outcome.candidate_ref == submission().candidate_ref


def test_a_sealed_bundle_names_every_check_in_declaration_order() -> None:
    """The bundle states what was proven rather than summarising it."""
    outcome = seal_candidate(
        SealInputs(submission=submission(), binding=binding(), lease=lease()), now=LATER
    )

    assert outcome.bundle is not None
    assert outcome.bundle.checks_passed == tuple(SealCheck)
    assert outcome.bundle.sealed_at == LATER
    assert outcome.bundle.report_digest == SUCCESS["report"]["report_digest"]
    assert outcome.bundle.resulting_tree_digest == SUCCESS["submission"]["resulting_tree_digest"]


def test_a_submission_reads_pending_however_it_is_built() -> None:
    """The claim never carries its own report, which is its immutability."""
    assert submission().report_binding == SUCCESS["expected"]["report_binding"]


def test_a_submission_is_frozen_after_it_is_built() -> None:
    """An immutable record refuses the edit that would revise a claim."""
    claim = submission()

    with pytest.raises(ValidationError):
        claim.changed_paths = ("src/other.py",)  # type: ignore[misc]


# ---- one moved fact, one named failure --------------------------------------


@pytest.mark.parametrize("case", SUCCESS["unsealing"], ids=lambda case: str(case["case_id"]))
def test_each_unsealing_case_fails_exactly_the_checks_it_names(case: dict[str, Any]) -> None:
    """One durable fact moves and exactly the checks reading it stop holding."""
    outcome = seal_candidate(inputs_for(case), now=LATER)

    assert outcome.sealed is False
    assert outcome.bundle is None
    assert outcome.failed_checks == tuple(case["failed_checks"])


@pytest.mark.parametrize("case", SUCCESS["unsealing"], ids=lambda case: str(case["case_id"]))
def test_failed_seal_checks_agrees_with_the_decision(case: dict[str, Any]) -> None:
    """The reported failures are the ones the table itself produced."""
    assert failed_seal_checks(inputs_for(case)) == tuple(case["failed_checks"])


def test_an_absent_lease_fails_every_check_that_reads_one() -> None:
    """A lease nobody can read is unmet proof, never absent proof."""
    outcome = seal_candidate(
        SealInputs(submission=submission(), binding=binding(), lease=None), now=LATER
    )

    assert outcome.failed_checks == (
        SealCheck.PATHS_WITHIN_WRITE_SET,
        SealCheck.WORKSPACE_GENERATION_CURRENT,
        SealCheck.BASE_COMMIT_AGREES,
    )


def test_an_absent_binding_and_lease_fails_all_six() -> None:
    """The maximal failure is every check, not an exception."""
    outcome = seal_candidate(
        SealInputs(submission=submission(), binding=None, lease=None), now=LATER
    )

    assert outcome.failed_checks == tuple(SealCheck)


@pytest.mark.parametrize(
    "verdict", [verdict for verdict in AgentReportVerdict if verdict not in DELIVERABLE_VERDICTS]
)
def test_a_verdict_outside_the_deliverable_set_refuses_the_seal(
    verdict: AgentReportVerdict,
) -> None:
    """Every non-deliverable verdict is refused, not only the fixture's."""
    outcome = seal_candidate(
        SealInputs(submission=submission(), binding=binding(verdict=verdict), lease=lease()),
        now=LATER,
    )

    assert outcome.failed_checks == (SealCheck.VERDICT_ADMITS_DELIVERY,)


@pytest.mark.parametrize("verdict", sorted(DELIVERABLE_VERDICTS))
def test_every_deliverable_verdict_seals(verdict: AgentReportVerdict) -> None:
    """Both verdicts that propose work for integration reach a bundle."""
    outcome = seal_candidate(
        SealInputs(submission=submission(), binding=binding(verdict=verdict), lease=lease()),
        now=LATER,
    )

    assert outcome.sealed is True


# ---- the write-set predicate at its edges -----------------------------------


@pytest.mark.parametrize(
    ("path", "roots", "contained"),
    [
        ("src", ("src",), True),
        ("src/module.py", ("src",), True),
        ("src/a/b/c.py", ("src",), True),
        ("srcfoo/module.py", ("src",), False),
        ("s", ("src",), False),
        ("docs/note.md", ("src", "tests"), False),
        ("tests/unit/test_x.py", ("src", "tests"), True),
        ("src/module.py", (), False),
    ],
)
def test_path_within_roots_matches_whole_segments_only(
    path: str, roots: tuple[str, ...], contained: bool
) -> None:
    """A root contains a path only at a segment boundary or exactly."""
    assert path_within_roots(path, roots) is contained


def test_a_single_escaping_path_among_many_refuses_the_whole_claim() -> None:
    """The check is over every path, so one escape is enough."""
    inside = tuple(f"src/module{index}.py" for index in range(8))
    outcome = seal_candidate(
        SealInputs(
            submission=submission(changed_paths=(*inside, "docs/note.md")),
            binding=binding(),
            lease=lease(),
        ),
        now=LATER,
    )

    assert outcome.failed_checks == (SealCheck.PATHS_WITHIN_WRITE_SET,)


# ---- the records refuse what they cannot mean -------------------------------


def test_a_submission_whose_identity_is_not_derived_is_refused() -> None:
    """A forged id would let a replay answer with the wrong claim."""
    with pytest.raises(ValidationError, match="is not the identity"):
        submission(candidate_ref="CND-" + "0" * 32)


def test_a_submission_naming_the_same_path_twice_is_refused() -> None:
    """A repeated path hides which of the two entries was meant."""
    with pytest.raises(ValidationError):
        submission(changed_paths=("src/module.py", "src/module.py"))


def test_a_submission_naming_no_path_is_refused() -> None:
    """The empty boundary: a claim that touched nothing is not a candidate."""
    with pytest.raises(ValidationError):
        submission(changed_paths=())


def test_a_submission_walking_out_of_the_repository_is_refused() -> None:
    """A path grammar that admitted ``..`` would leave the tree entirely."""
    with pytest.raises(ValidationError):
        submission(changed_paths=("../outside.py",))


def test_a_submission_naming_more_paths_than_the_bound_is_refused() -> None:
    """The maximum-length boundary is refused one path past it."""
    with pytest.raises(ValidationError):
        submission(changed_paths=tuple(f"src/m{index}.py" for index in range(513)))


def test_a_submission_naming_exactly_the_bound_is_admitted() -> None:
    """The maximum-length boundary itself is admitted."""
    claim = submission(changed_paths=tuple(f"src/m{index}.py" for index in range(512)))

    assert len(claim.changed_paths) == 512


def test_a_bundle_that_names_fewer_than_every_check_is_refused() -> None:
    """A bundle claiming a partial seal would report a proof nobody took."""
    outcome = seal_candidate(
        SealInputs(submission=submission(), binding=binding(), lease=lease()), now=LATER
    )
    assert outcome.bundle is not None
    body = outcome.bundle.model_dump(mode="json")
    body["checks_passed"] = body["checks_passed"][:-1]

    with pytest.raises(ValidationError, match=SealCheck.BASE_COMMIT_AGREES.value):
        CandidateBundle.model_validate(body)


def test_a_bundle_with_an_unknown_field_is_refused() -> None:
    """The record is closed, so a key nobody declared never lands."""
    outcome = seal_candidate(
        SealInputs(submission=submission(), binding=binding(), lease=lease()), now=LATER
    )
    assert outcome.bundle is not None

    with pytest.raises(ValidationError):
        CandidateBundle.model_validate({**outcome.bundle.model_dump(mode="json"), "extra": 1})


def test_a_sealed_outcome_carrying_no_bundle_is_refused() -> None:
    """The verdict and its evidence are tied, so neither can stand alone."""
    with pytest.raises(ValidationError, match="carries its bundle"):
        SealOutcome(candidate_ref=submission().candidate_ref, sealed=True)


def test_an_unsealed_outcome_naming_no_failure_is_refused() -> None:
    """An unsealed outcome that named nothing would refuse without a reason."""
    with pytest.raises(ValidationError, match="at least one failed check"):
        SealOutcome(candidate_ref=submission().candidate_ref, sealed=False)


# ---- identity is derived, so a resubmission is not a duplicate --------------


def test_the_same_task_and_tree_derive_the_same_candidate() -> None:
    """Identity is content, which is what makes a retry replay."""
    digest = SUCCESS["submission"]["resulting_tree_digest"]

    assert candidate_identity(
        task_ref=TASK_URN, resulting_tree_digest=digest
    ) == candidate_identity(task_ref=TASK_URN, resulting_tree_digest=digest)


def test_another_tree_under_one_task_derives_another_candidate() -> None:
    """Two different trees are two candidates, however close they are."""
    first = candidate_identity(task_ref=TASK_URN, resulting_tree_digest=f"sha256:{'a' * 64}")
    second = candidate_identity(task_ref=TASK_URN, resulting_tree_digest=f"sha256:{'b' * 64}")

    assert first != second


def test_one_tree_under_two_tasks_derives_two_candidates() -> None:
    """The Task is part of the identity, not only the tree."""
    digest = SUCCESS["submission"]["resulting_tree_digest"]
    other = TASK_URN.replace("EAWF-0042", "EAWF-0043")

    assert candidate_identity(
        task_ref=TASK_URN, resulting_tree_digest=digest
    ) != candidate_identity(task_ref=other, resulting_tree_digest=digest)


def test_a_derived_identity_satisfies_the_candidate_grammar() -> None:
    """The derived id is the one the record's own field admits."""
    claim = submission()

    assert claim.candidate_ref.startswith("CND-")
    assert len(claim.candidate_ref) == 36
