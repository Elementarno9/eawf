"""DEL-008 and DEL-009: one order, one squashed commit, one manifest.

The order is asserted the only way that means anything: the same
candidates are offered in every permutation and every permutation has to
produce one sequence. That is also why the key is taken over content --
the Task addressed and the tree produced -- and the suite pins that by
moving the facts that differ between two productions of one piece of work
(the Run, the verdict, the seal stamp) and requiring the order and the
manifest id to be unmoved by any of them.

The squash is asserted against the configured leaves rather than against
a remembered string. Under ``batch`` there is exactly one commit whatever
the candidate count, it names every Task once, and it carries the
provenance trailer; under ``task`` there is one commit per Task with its
own single reference. Every refusal the policy declares has a case here,
because a refusal nobody reaches is a rule nobody enforces.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.integration import (
    AgentAuthority,
    ConflictFile,
    ConflictHunk,
    ConflictSide,
)
from eawf.kernel.runtime.candidate import (
    CandidateBundle,
    SealCheck,
    candidate_identity,
)
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.runtime.integration.apply import (
    ORDER_KEY_FIELDS,
    ApplyDisposition,
    CandidateApplication,
    IntegrationRefusal,
    IntegrationRefusedError,
    apply_serially,
    bundle_order_key,
    integration_change,
    integration_order,
    order_key,
)
from eawf.runtime.integration.commit_policy import (
    MAX_SUBJECT_LENGTH,
    PROVENANCE_SCHEME,
    PROVENANCE_TRAILER_KEY,
    TASK_TRAILER_KEY,
    DeliveryManifest,
    build_manifest,
    integration_policy_digest,
    render_delivery_commits,
)

pytestmark = pytest.mark.unit


REPOSITORY: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
BATCH: Final = f"{REPOSITORY}/batch/BAT-0001"
RUN: Final = f"{REPOSITORY}/run/RUN-00000010"
OTHER_RUN: Final = f"{REPOSITORY}/run/RUN-00000011"
BASE_COMMIT: Final = "9f" * 20
OTHER_BASE: Final = "ab" * 20
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LATER: Final = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)


def task_urn(key: str) -> str:
    """Return the canonical URN of one Task key."""
    return f"{REPOSITORY}/task/{key}"


def tree(letter: str) -> str:
    """Return a distinct resulting-tree digest."""
    return f"sha256:{letter * 64}"


def bundle(
    *,
    task: str = "EAWF-0001",
    digest: str = "a",
    paths: tuple[str, ...] = ("src/module.py",),
    base: str = BASE_COMMIT,
    run: str = RUN,
    sealed_at: datetime = AT,
    verdict: AgentReportVerdict = AgentReportVerdict.PASS,
) -> CandidateBundle:
    """Return one sealed candidate, derived identity included."""
    urn = task_urn(task)
    resulting = tree(digest)
    fields: dict[str, Any] = {
        "candidate_ref": candidate_identity(task_ref=urn, resulting_tree_digest=resulting),
        "run_ref": run,
        "task_ref": urn,
        "submission_ref": "artifact://candidate/executor-success",
        "report_digest": f"sha256:{'b' * 64}",
        "verdict": verdict,
        "changed_paths": paths,
        "resulting_tree_digest": resulting,
        "base_commit": base,
        "workspace_generation": 1,
        "checks_passed": tuple(SealCheck),
        "sealed_at": sealed_at,
    }
    return CandidateBundle.model_validate(fields)


def subjects_of(*bundles: CandidateBundle) -> dict[str, str]:
    """Return one subject line per candidate, keyed by candidate ref."""
    return {item.candidate_ref: f"deliver {item.task_ref.entity_key.lower()}" for item in bundles}


def manifest_of(*bundles: CandidateBundle, generation: int = 2) -> DeliveryManifest:
    """Return the manifest of *bundles* in integration order."""
    ordered = integration_order(bundles)
    return build_manifest(
        ordered, batch_ref=BATCH, generation=generation, subjects=subjects_of(*ordered)
    )


# ---- DEL-009: the order is a property of the content --------------------------


def test_integration_order_is_the_same_for_every_permutation() -> None:
    """Every arrival order of one candidate set produces one sequence."""
    candidates = [
        bundle(task="EAWF-0003", digest="c"),
        bundle(task="EAWF-0001", digest="a"),
        bundle(task="EAWF-0002", digest="b"),
    ]
    sequences = {
        tuple(item.candidate_ref for item in integration_order(permutation))
        for permutation in itertools.permutations(candidates)
    }

    assert len(sequences) == 1


def test_integration_order_key_reads_only_declared_fields() -> None:
    """The key is exactly the declared content fields, in that order."""
    item = bundle(task="EAWF-0007", digest="d")

    assert ORDER_KEY_FIELDS == ("task_ref", "resulting_tree_digest", "candidate_ref")
    assert bundle_order_key(item) == order_key(
        task_ref=str(item.task_ref),
        resulting_tree_digest=item.resulting_tree_digest,
        candidate_ref=item.candidate_ref,
    )


def test_integration_order_ignores_the_run_that_produced_the_tree() -> None:
    """Two Runs at one tree for one Task are one candidate in one place."""
    first = bundle(task="EAWF-0004", digest="a", run=RUN, sealed_at=AT)
    replayed = bundle(
        task="EAWF-0004",
        digest="a",
        run=OTHER_RUN,
        sealed_at=LATER,
        verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS,
    )

    assert first.candidate_ref == replayed.candidate_ref
    assert bundle_order_key(first) == bundle_order_key(replayed)


def test_integration_order_admits_a_single_candidate() -> None:
    """Boundary: one candidate is a sequence of one."""
    only = bundle(task="EAWF-0001")

    assert integration_order([only]) == (only,)


def test_integration_order_refuses_an_empty_offering() -> None:
    """Boundary: nothing offered is nothing to integrate."""
    with pytest.raises(IntegrationRefusedError) as error:
        integration_order([])

    assert error.value.code is IntegrationRefusal.CANDIDATES_ABSENT


def test_integration_order_refuses_a_repeated_candidate() -> None:
    """One identity offered twice would apply one tree's work twice."""
    repeated = bundle(task="EAWF-0001", digest="a")

    with pytest.raises(IntegrationRefusedError) as error:
        integration_order([repeated, repeated])

    assert error.value.code is IntegrationRefusal.CANDIDATE_REPEATED


def test_integration_order_refuses_two_candidates_for_one_task() -> None:
    """Which tree is a Task's delivery must have exactly one answer."""
    twice = [bundle(task="EAWF-0001", digest="a"), bundle(task="EAWF-0001", digest="b")]

    with pytest.raises(IntegrationRefusedError) as error:
        integration_order(twice)

    assert error.value.code is IntegrationRefusal.TASK_AMBIGUOUS


def test_integration_order_refuses_candidates_from_two_bases() -> None:
    """Candidates that are not from one base cannot be sequenced."""
    with pytest.raises(IntegrationRefusedError) as error:
        integration_order(
            [bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b", base=OTHER_BASE)]
        )

    assert error.value.code is IntegrationRefusal.BASE_DIVERGENT


def test_integration_change_unions_paths_and_keeps_task_order() -> None:
    """The change set dedupes paths and keeps the integration order."""
    ordered = integration_order(
        [
            bundle(task="EAWF-0002", digest="b", paths=("src/b.py", "src/shared.py")),
            bundle(task="EAWF-0001", digest="a", paths=("src/a.py", "src/shared.py")),
        ]
    )

    change = integration_change(ordered)

    assert change.changed_paths == ("src/a.py", "src/b.py", "src/shared.py")
    assert change.task_refs == (task_urn("EAWF-0001"), task_urn("EAWF-0002"))


def test_integration_change_refuses_an_empty_offering() -> None:
    """Error path: no candidates means no change set."""
    with pytest.raises(IntegrationRefusedError) as error:
        integration_change(())

    assert error.value.code is IntegrationRefusal.CANDIDATES_ABSENT


# ---- serial application -------------------------------------------------------


def conflict_file(path: str = "src/module.py") -> ConflictFile:
    """Return a one-hunk conflict frame for *path*."""
    side = ConflictSide(
        authority=AgentAuthority(kind="agent", batch_ref=BATCH),
        at=AT,
        sha="1" * 40,
        lines=("ours",),
    )
    other = side.model_copy(update={"sha": "2" * 40, "lines": ("theirs",)})
    return ConflictFile(path=path, hunks=(ConflictHunk(index=1, ours=side, theirs=other),))


def test_apply_serially_stops_at_the_first_conflict() -> None:
    """Nothing after a conflict is tried: it would be against another tree."""
    ordered = integration_order(
        [
            bundle(task="EAWF-0001", digest="a"),
            bundle(task="EAWF-0002", digest="b"),
            bundle(task="EAWF-0003", digest="c"),
        ]
    )
    seen: list[str] = []

    def applier(item: CandidateBundle) -> CandidateApplication:
        seen.append(item.candidate_ref)
        if item.candidate_ref == ordered[1].candidate_ref:
            return CandidateApplication(
                candidate_ref=item.candidate_ref,
                disposition=ApplyDisposition.CONFLICTED,
                conflict_files=(conflict_file(),),
                ahead=3,
                behind=1,
            )
        return CandidateApplication(
            candidate_ref=item.candidate_ref, disposition=ApplyDisposition.APPLIED
        )

    outcome = apply_serially(ordered, applier=applier)

    assert seen == [ordered[0].candidate_ref, ordered[1].candidate_ref]
    assert outcome.blocked is True
    assert outcome.applied == (ordered[0].candidate_ref,)
    assert outcome.blocked_on == ordered[1].candidate_ref
    assert (outcome.ahead, outcome.behind) == (3, 1)


def test_apply_serially_applies_every_candidate_when_none_conflicts() -> None:
    """Boundary: a clean pass lists every candidate and blocks on none."""
    ordered = integration_order([bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b")])

    outcome = apply_serially(
        ordered,
        applier=lambda item: CandidateApplication(
            candidate_ref=item.candidate_ref, disposition=ApplyDisposition.APPLIED
        ),
    )

    assert outcome.blocked is False
    assert outcome.applied == tuple(item.candidate_ref for item in ordered)


def test_apply_serially_refuses_an_answer_about_another_candidate() -> None:
    """Error path: a workspace that answers about something else is a bug."""
    ordered = integration_order([bundle(task="EAWF-0001")])
    other = bundle(task="EAWF-0002", digest="b")

    with pytest.raises(ValueError, match="answered about"):
        apply_serially(
            ordered,
            applier=lambda _item: CandidateApplication(
                candidate_ref=other.candidate_ref, disposition=ApplyDisposition.APPLIED
            ),
        )


def test_apply_serially_refuses_an_empty_sequence() -> None:
    """Boundary: nothing to apply is refused rather than reported clean."""
    with pytest.raises(IntegrationRefusedError) as error:
        apply_serially(
            (),
            applier=lambda item: CandidateApplication(
                candidate_ref=item.candidate_ref, disposition=ApplyDisposition.APPLIED
            ),
        )

    assert error.value.code is IntegrationRefusal.CANDIDATES_ABSENT


def test_candidate_application_refuses_a_conflict_with_no_file() -> None:
    """Error path: a conflict frame nobody can read is not a conflict."""
    with pytest.raises(ValidationError, match="at least one conflicting file"):
        CandidateApplication(
            candidate_ref=bundle().candidate_ref, disposition=ApplyDisposition.CONFLICTED
        )


def test_candidate_application_refuses_an_applied_candidate_with_a_file() -> None:
    """Error path: a landed candidate cannot report a conflict."""
    with pytest.raises(ValidationError, match="names no conflicting file"):
        CandidateApplication(
            candidate_ref=bundle().candidate_ref,
            disposition=ApplyDisposition.APPLIED,
            conflict_files=(conflict_file(),),
        )


# ---- DEL-008: the manifest and the squashed delivery commit -------------------


def test_manifest_id_is_derived_from_content_alone() -> None:
    """Two assemblies of one ordered delivery name one manifest."""
    first = manifest_of(bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b"))
    again = manifest_of(bundle(task="EAWF-0002", digest="b"), bundle(task="EAWF-0001"))

    assert first.manifest_id == again.manifest_id
    assert first.manifest_id.startswith("MFT-")


def test_manifest_id_moves_when_the_delivered_trees_move() -> None:
    """A different tree is a different delivery and a different manifest."""
    first = manifest_of(bundle(task="EAWF-0001", digest="a"))
    other = manifest_of(bundle(task="EAWF-0001", digest="b"))

    assert first.manifest_id != other.manifest_id


def test_manifest_refuses_entries_out_of_integration_order() -> None:
    """Error path: an unordered manifest would digest by assembly order."""
    ordered = integration_order([bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b")])
    built = build_manifest(ordered, batch_ref=BATCH, generation=2, subjects=subjects_of(*ordered))

    with pytest.raises(ValidationError, match="not in integration order"):
        DeliveryManifest.model_validate(
            {**built.model_dump(mode="json"), "entries": list(reversed(built.entries))}
        )


def test_build_manifest_refuses_a_candidate_with_no_subject() -> None:
    """Error path: a row that cannot say what the Task did is refused."""
    ordered = integration_order([bundle(task="EAWF-0001")])

    with pytest.raises(KeyError):
        build_manifest(ordered, batch_ref=BATCH, generation=2, subjects={})


def test_build_manifest_refuses_an_empty_offering() -> None:
    """Boundary: no candidates means no manifest."""
    with pytest.raises(IntegrationRefusedError) as error:
        build_manifest((), batch_ref=BATCH, generation=2, subjects={})

    assert error.value.code is IntegrationRefusal.CANDIDATES_ABSENT


def test_batch_unit_squashes_into_one_commit_with_one_trailer_per_task() -> None:
    """The Batch unit delivers once, names every Task, and names the manifest."""
    manifest = manifest_of(
        bundle(task="EAWF-0001"),
        bundle(task="EAWF-0002", digest="b"),
        bundle(task="EAWF-0003", digest="c"),
    )

    commits = render_delivery_commits(
        manifest, unit="batch", task_reference="trailer", batch_subject="deliver the batch"
    )

    assert len(commits) == 1
    delivery = commits[0]
    assert [line for line in delivery.trailers if line.startswith(f"{TASK_TRAILER_KEY}: ")] == [
        f"{TASK_TRAILER_KEY}: EAWF-0001",
        f"{TASK_TRAILER_KEY}: EAWF-0002",
        f"{TASK_TRAILER_KEY}: EAWF-0003",
    ]
    provenance = f"{PROVENANCE_TRAILER_KEY}: {PROVENANCE_SCHEME}{manifest.manifest_id}"
    assert delivery.trailers[-1] == provenance
    assert delivery.message.endswith(provenance)
    assert delivery.message.splitlines()[0] == "deliver the batch"
    assert len(delivery.bullets) == 3


def test_batch_unit_squashes_a_single_candidate_into_one_commit() -> None:
    """Boundary: a one-candidate Batch is still one squashed delivery."""
    manifest = manifest_of(bundle(task="EAWF-0001"))

    commits = render_delivery_commits(
        manifest, unit="batch", task_reference="trailer", batch_subject="deliver the batch"
    )

    assert len(commits) == 1
    assert commits[0].trailers == (
        f"{TASK_TRAILER_KEY}: EAWF-0001",
        f"{PROVENANCE_TRAILER_KEY}: {PROVENANCE_SCHEME}{manifest.manifest_id}",
    )


def test_task_unit_delivers_one_commit_per_task() -> None:
    """The per-Task unit gives each Task its own commit and its own trailer."""
    manifest = manifest_of(bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b"))

    commits = render_delivery_commits(
        manifest, unit="task", task_reference="trailer", batch_subject="unused"
    )

    assert len(commits) == 2
    assert [commit.trailers[0] for commit in commits] == [
        f"{TASK_TRAILER_KEY}: EAWF-0001",
        f"{TASK_TRAILER_KEY}: EAWF-0002",
    ]
    assert all(commit.manifest_id == manifest.manifest_id for commit in commits)
    assert all(commit.bullets == () for commit in commits)


def test_subject_reference_moves_the_task_keys_into_the_subject() -> None:
    """Under ``subject`` the keys lead the subject and no Task trailer is cut."""
    manifest = manifest_of(bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b"))

    delivery = render_delivery_commits(
        manifest, unit="batch", task_reference="subject", batch_subject="deliver the batch"
    )[0]

    assert delivery.subject == "[EAWF-0001,EAWF-0002] deliver the batch"
    assert delivery.trailers == (
        f"{PROVENANCE_TRAILER_KEY}: {PROVENANCE_SCHEME}{manifest.manifest_id}",
    )


def test_none_reference_keeps_only_the_provenance_trailer() -> None:
    """Under ``none`` the manifest is the sole link, and it is still rendered."""
    manifest = manifest_of(bundle(task="EAWF-0001"))

    delivery = render_delivery_commits(
        manifest, unit="batch", task_reference="none", batch_subject="deliver the batch"
    )[0]

    assert delivery.subject == "deliver the batch"
    assert delivery.trailers == (
        f"{PROVENANCE_TRAILER_KEY}: {PROVENANCE_SCHEME}{manifest.manifest_id}",
    )


def test_subject_reference_refuses_a_subject_it_would_overflow() -> None:
    """Error path: a reference that truncates the summary is refused."""
    manifest = manifest_of(
        *(bundle(task=f"EAWF-000{index}", digest=letter) for index, letter in enumerate("abcde", 1))
    )

    with pytest.raises(IntegrationRefusedError) as error:
        render_delivery_commits(
            manifest,
            unit="batch",
            task_reference="subject",
            batch_subject="deliver the whole batch of five tasks",
        )

    assert error.value.code is IntegrationRefusal.SUBJECT_OVERFLOW


@pytest.mark.parametrize(
    "subject",
    ["", "x" * (MAX_SUBJECT_LENGTH + 1), "two\nlines", " padded", "ends in a period."],
)
def test_manifest_refuses_a_subject_that_is_not_one_clean_line(subject: str) -> None:
    """Error path: every subject rule refuses at the manifest boundary."""
    ordered = integration_order([bundle(task="EAWF-0001")])

    with pytest.raises(ValidationError):
        build_manifest(
            ordered,
            batch_ref=BATCH,
            generation=2,
            subjects={ordered[0].candidate_ref: subject},
        )


def test_subject_of_exactly_the_maximum_width_is_admitted() -> None:
    """Boundary: the widest legal subject is not one character too wide."""
    ordered = integration_order([bundle(task="EAWF-0001")])
    widest = "d" * MAX_SUBJECT_LENGTH

    manifest = build_manifest(
        ordered, batch_ref=BATCH, generation=2, subjects={ordered[0].candidate_ref: widest}
    )

    assert manifest.entries[0].subject == widest


def test_integration_policy_digest_separates_the_configured_units() -> None:
    """A generation taken under other rules cannot claim this one's digest."""
    batch_digest = integration_policy_digest(unit="batch", task_reference="trailer")

    assert batch_digest != integration_policy_digest(unit="task", task_reference="trailer")
    assert batch_digest != integration_policy_digest(unit="batch", task_reference="none")
    assert batch_digest == integration_policy_digest(unit="batch", task_reference="trailer")
