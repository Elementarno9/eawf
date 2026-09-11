"""The ``migration`` row: green only when ten corpora rehearsed four legs each.

The signal is the one place a release says "the cutover works". These
tests pin what that sentence is allowed to mean.

**Pass requires the whole set.** Every corpus of the declared roster has
a committed record, and every leg of every record holds. Deleting a
record, dropping a leg, or flipping a leg's claim each red the row.

**Absence is not cleanliness.** A record that was never written reports
``unavailable`` and a leg that ran and disagrees reports ``fail``. Both
carry ``migration_unproven``, so neither can be mistaken for a pass, but
they are distinguished because the repairs differ: run the rehearsal
versus fix the cutover.

**The roster is the test suite's roster.** The production roster and the
one the rehearsal actually iterates are cross-checked, so the signal
cannot drift into asserting over a set nobody runs.

The ``security_review`` cases live here too. That gate reads the
vulnerability component of the ``dependencies`` row, and the failure
mode worth pinning is the quiet one: a report that was never produced,
or one that cannot be read, must red the gate rather than let it pass on
an empty finding list.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.release.signals import (
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName, load_release_config
from eawf.workflow.evidence.migration_rehearsal import (
    APPLY_JOURNAL_STAGES,
    REHEARSAL_EVIDENCE_DIR,
    REHEARSED_CORPORA,
    RehearsalDisposition,
    RehearsalGap,
    evidence_dir,
    rehearsal_evidence_refs,
    rehearsal_findings,
)
from eawf.workflow.release.dependencies import (
    LicenseDisposition,
    LockedPackage,
    ReleaseDependencyManifest,
)
from eawf.workflow.release.pipeline_receipts import receipt_path, write_receipt
from eawf.workflow.release.signal_probes import build_receipt_probes
from eawf.workflow.release.train import DEV2_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.release.vulnerability import Advisory, VulnerabilityReport
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import compute_readiness
from tests.integration.kernel.migration._rehearsal_set import REHEARSAL_FIXTURE_INDEX

#: The repository this suite reads the committed rehearsal out of.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: Instant every sweep here is computed at.
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

#: A changelog section stating the migration outcome, which the row also
#: requires: a release that needs no migration says so in one line.
CHANGELOG_BODY = """\
# Changelog

## [0.7.0.dev2] - 2026-09-11

- Cuts the epoch-2 migration over the whole rehearsed corpus set.
"""

#: A lock digest the receipts agree on.
LOCK_DIGEST = f"sha256:{'d' * 64}"


def dev2_config() -> ReleaseConfig:
    """Return the generated ``dev2`` configuration, loaded against the train."""
    return load_release_config(DEV2_RELEASE_CONFIG_YAML, train=V07_TRAIN)


def stage_rehearsal(root: Path, *, mutate: Any = None) -> Path:
    """Copy the committed rehearsal records under *root*, optionally mutated.

    Args:
        root: Temporary directory to build a fake checkout in.
        mutate: Callable handed ``{corpus: record}`` to edit in place, or
            ``None`` to stage the records verbatim.

    Returns:
        The fake repo root, carrying the records and a changelog that
        states the migration outcome.
    """
    records: dict[str, Any] = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(evidence_dir(REPO_ROOT).glob("*.json"))
    }
    staged = copy.deepcopy(records)
    if mutate is not None:
        mutate(staged)
    target = root.joinpath(*REHEARSAL_EVIDENCE_DIR)
    target.mkdir(parents=True, exist_ok=True)
    for corpus, record in staged.items():
        (target / f"{corpus}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    (root / "CHANGELOG.md").write_text(CHANGELOG_BODY, encoding="utf-8")
    return root


def migration_row(repo_root: Path) -> Any:
    """Return the ``migration`` row of a sweep over the checkout at *repo_root*."""
    inputs = TagPreflightInputs(
        repo_root=repo_root,
        version="0.7.0.dev2",
        tag="v0.7.0.dev2",
        package_version="0.7.0.dev2",
        remote="origin",
    )
    readiness = compute_readiness(dev2_config(), probes=build_tag_probes(inputs), computed_at=NOW)
    return readiness.row(ReleaseSignalName.MIGRATION)


# --- the declared roster ------------------------------------------------------


def test_migration_signal_declares_ten_corpora() -> None:
    """The roster the signal computes over is exactly ten."""
    assert len(REHEARSED_CORPORA) == 10


def test_migration_signal_roster_matches_the_rehearsed_set() -> None:
    """The production roster and the set the rehearsal iterates agree."""
    assert set(REHEARSED_CORPORA) == set(REHEARSAL_FIXTURE_INDEX)


@pytest.mark.parametrize("corpus", sorted(REHEARSED_CORPORA), ids=lambda name: name)
def test_migration_signal_roster_agrees_on_each_disposition(corpus: str) -> None:
    """Each corpus is declared to do what the rehearsal expects of it."""
    assert REHEARSED_CORPORA[corpus].value == REHEARSAL_FIXTURE_INDEX[corpus].disposition.value


def test_migration_signal_expects_the_stages_the_cutover_declares() -> None:
    """The expected apply stages come from the production enum, not a literal."""
    assert len(APPLY_JOURNAL_STAGES) == 12
    assert APPLY_JOURNAL_STAGES[0] == "fence_cleared"
    assert APPLY_JOURNAL_STAGES[-1] == "maintenance_exited"


def test_migration_signal_evidence_refs_name_every_corpus() -> None:
    """A passing row cites one reference per rehearsed corpus."""
    refs = rehearsal_evidence_refs()

    assert len(refs) == len(REHEARSED_CORPORA)
    assert refs[0].startswith("rehearsal:")


# --- the committed evidence is complete --------------------------------------


def test_migration_signal_finds_no_gap_in_the_committed_rehearsal() -> None:
    """The records this repository ships satisfy every leg of every corpus."""
    assert rehearsal_findings(REPO_ROOT) == ()


def test_migration_signal_passes_over_a_complete_checkout(tmp_path: Path) -> None:
    """A checkout carrying the records and the note greens the row."""
    row = migration_row(stage_rehearsal(tmp_path))

    assert row.status is ReleaseSignalStatus.PASS
    assert row.failure_code is None
    assert len(row.evidence_refs) == len(REHEARSED_CORPORA)


# --- an absent fixture or leg leaves it unproven ------------------------------


def test_migration_signal_is_unproven_when_a_corpus_record_is_absent(tmp_path: Path) -> None:
    """Deleting one of the ten records reports migration_unproven, not a pass."""

    def drop(records: dict[str, Any]) -> None:
        del records["minimal-active"]

    row = migration_row(stage_rehearsal(tmp_path, mutate=drop))

    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert row.failure_code is ReleaseSignalFailureCode.MIGRATION_UNPROVEN
    assert "minimal-active" in row.remediation


@pytest.mark.parametrize("leg", ("dry_run", "apply", "rerun", "rollback"))
def test_migration_signal_is_unproven_when_a_leg_is_absent(tmp_path: Path, leg: str) -> None:
    """Each of the four legs is load-bearing on its own."""

    def drop(records: dict[str, Any]) -> None:
        del records["minimal-active"][leg]

    row = migration_row(stage_rehearsal(tmp_path, mutate=drop))

    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert row.failure_code is ReleaseSignalFailureCode.MIGRATION_UNPROVEN
    assert leg in row.remediation


def test_migration_signal_is_unproven_when_the_refusal_leg_is_absent(tmp_path: Path) -> None:
    """A refusing corpus rehearses the refusal; dropping it is a missing leg."""

    def drop(records: dict[str, Any]) -> None:
        del records["corrupt-reference"]["refusal"]

    row = migration_row(stage_rehearsal(tmp_path, mutate=drop))

    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert "corrupt-reference" in row.remediation


def test_migration_signal_is_unproven_with_no_evidence_directory(tmp_path: Path) -> None:
    """A checkout with no records at all names the directory it wanted."""
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG_BODY, encoding="utf-8")

    row = migration_row(tmp_path)

    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert "/".join(REHEARSAL_EVIDENCE_DIR) in row.remediation


def test_migration_signal_reports_one_finding_for_an_empty_evidence_tree() -> None:
    """The whole-set gap is reported once rather than ten times."""
    findings = rehearsal_findings(Path("/nonexistent-checkout"))

    assert len(findings) == 1
    assert findings[0].gap is RehearsalGap.MISSING_EVIDENCE_DIR
    assert findings[0].is_absence is True


# --- a leg that ran and disagrees fails --------------------------------------


def test_migration_signal_fails_when_the_dry_run_is_not_reproducible(tmp_path: Path) -> None:
    """Two seals over the same bytes must produce the same manifest digest."""

    def flip(records: dict[str, Any]) -> None:
        records["minimal-active"]["dry_run"]["reproducible"] = False

    row = migration_row(stage_rehearsal(tmp_path, mutate=flip))

    assert row.status is ReleaseSignalStatus.FAIL
    assert row.failure_code is ReleaseSignalFailureCode.MIGRATION_UNPROVEN


def test_migration_signal_fails_when_the_apply_published_nothing(tmp_path: Path) -> None:
    """An apply that wrote no generation is not a passing leg."""

    def flip(records: dict[str, Any]) -> None:
        records["minimal-active"]["apply"]["applied"] = False

    assert migration_row(stage_rehearsal(tmp_path, mutate=flip)).status is (
        ReleaseSignalStatus.FAIL
    )


def test_migration_signal_fails_when_a_journal_stage_is_missing(tmp_path: Path) -> None:
    """The stage list is compared against the cutover's own enum."""

    def truncate(records: dict[str, Any]) -> None:
        records["minimal-active"]["apply"]["journal_stages"] = list(APPLY_JOURNAL_STAGES[:-1])

    row = migration_row(stage_rehearsal(tmp_path, mutate=truncate))

    assert row.status is ReleaseSignalStatus.FAIL
    assert "12 stages" in row.remediation


def test_migration_signal_fails_when_the_rerun_wrote_something(tmp_path: Path) -> None:
    """Re-applying an approved plan must be inert."""

    def flip(records: dict[str, Any]) -> None:
        records["minimal-active"]["rerun"]["tree_unchanged"] = False

    assert migration_row(stage_rehearsal(tmp_path, mutate=flip)).status is (
        ReleaseSignalStatus.FAIL
    )


def test_migration_signal_fails_when_the_rollback_left_a_generation(tmp_path: Path) -> None:
    """A rollback that leaves epoch-2 bytes behind did not return the tree."""

    def flip(records: dict[str, Any]) -> None:
        records["minimal-active"]["rollback"]["generation_count"] = 1

    row = migration_row(stage_rehearsal(tmp_path, mutate=flip))

    assert row.status is ReleaseSignalStatus.FAIL
    assert "generation(s) remain" in row.remediation


def test_migration_signal_fails_when_the_refusal_is_not_repeatable(tmp_path: Path) -> None:
    """A refused corpus must refuse the same way on every attempt."""

    def flip(records: dict[str, Any]) -> None:
        records["corrupt-reference"]["refusal"]["retry_code"] = "migration_collection_unknown"

    row = migration_row(stage_rehearsal(tmp_path, mutate=flip))

    assert row.status is ReleaseSignalStatus.FAIL
    assert "one code" in row.remediation


def test_migration_signal_fails_when_a_refusal_touched_the_target(tmp_path: Path) -> None:
    """A refused import must leave nothing behind."""

    def flip(records: dict[str, Any]) -> None:
        records["corrupt-reference"]["refusal"]["target_unchanged"] = False

    assert migration_row(stage_rehearsal(tmp_path, mutate=flip)).status is (
        ReleaseSignalStatus.FAIL
    )


def test_migration_signal_fails_on_a_disposition_the_roster_denies(tmp_path: Path) -> None:
    """A corpus that suddenly imports where it should refuse is a contract change."""

    def flip(records: dict[str, Any]) -> None:
        records["corrupt-reference"]["disposition"] = RehearsalDisposition.IMPORTS.value

    row = migration_row(stage_rehearsal(tmp_path, mutate=flip))

    assert row.status is ReleaseSignalStatus.FAIL
    assert RehearsalGap.DISPOSITION_MISMATCH.value in row.remediation


def test_migration_signal_fails_on_an_unscrubbed_corpus(tmp_path: Path) -> None:
    """A staged corpus carrying a concrete home path is never evidence."""

    def flip(records: dict[str, Any]) -> None:
        records["minimal-active"]["scrub_findings"] = 2

    row = migration_row(stage_rehearsal(tmp_path, mutate=flip))

    assert row.status is ReleaseSignalStatus.FAIL
    assert RehearsalGap.UNSCRUBBED_CORPUS.value in row.remediation


def test_migration_signal_fails_when_the_changelog_states_no_outcome(tmp_path: Path) -> None:
    """Complete evidence plus a silent changelog is still not a passing row."""
    root = stage_rehearsal(tmp_path)
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.7.0.dev2]\n\n- Ships.\n", "utf-8")

    row = migration_row(root)

    assert row.status is ReleaseSignalStatus.FAIL
    assert "no migration outcome" in row.remediation


# --- error paths on the reader ------------------------------------------------


def test_migration_signal_reader_refuses_a_non_path_root() -> None:
    """The reader is the boundary; a string root is refused, not coerced."""
    with pytest.raises(TypeError, match="repo_root must be Path"):
        rehearsal_findings("tests")  # type: ignore[arg-type]


def test_migration_signal_reader_reports_an_unreadable_record(tmp_path: Path) -> None:
    """A record present but unparseable is a repair task, not an absent one."""
    target = tmp_path.joinpath(*REHEARSAL_EVIDENCE_DIR)
    target.mkdir(parents=True)
    (target / "minimal-active.json").write_text("{not json", encoding="utf-8")

    findings = rehearsal_findings(
        tmp_path, corpora={"minimal-active": RehearsalDisposition.IMPORTS}
    )

    assert findings[0].gap is RehearsalGap.UNREADABLE_RECORD


def test_migration_signal_reader_refuses_a_record_with_an_unknown_key(tmp_path: Path) -> None:
    """A golden whose shape drifted is not evidence about this cutover."""
    target = tmp_path.joinpath(*REHEARSAL_EVIDENCE_DIR)
    target.mkdir(parents=True)
    (target / "minimal-active.json").write_text(
        json.dumps(
            {
                "fixture": "minimal-active",
                "disposition": "imports",
                "scrub_findings": 0,
                "invented_key": 1,
            }
        ),
        encoding="utf-8",
    )

    findings = rehearsal_findings(
        tmp_path, corpora={"minimal-active": RehearsalDisposition.IMPORTS}
    )

    assert findings[0].gap is RehearsalGap.UNREADABLE_RECORD


def test_migration_signal_reader_accepts_an_empty_roster(tmp_path: Path) -> None:
    """Requiring nothing finds nothing, once the directory itself exists."""
    tmp_path.joinpath(*REHEARSAL_EVIDENCE_DIR).mkdir(parents=True)

    assert rehearsal_findings(tmp_path, corpora={}) == ()


def test_migration_signal_reader_reports_every_missing_corpus(tmp_path: Path) -> None:
    """Ten absent records are ten findings, not one: each is its own repair."""
    tmp_path.joinpath(*REHEARSAL_EVIDENCE_DIR).mkdir(parents=True)

    findings = rehearsal_findings(tmp_path)

    assert len(findings) == len(REHEARSED_CORPORA)
    assert all(finding.gap is RehearsalGap.MISSING_RECORD for finding in findings)


# --- security_review: an absent or unreadable report reds the gate ------------


def dependencies_row_probes(root: Path) -> Mapping[ReleaseSignalName, Any]:
    """Return the receipt-reading probes bound to the checkout at *root*."""
    return build_receipt_probes(root)


def sweep_with_receipts(root: Path) -> Any:
    """Return a dev2 sweep whose dependency row reads the receipts under *root*."""
    return compute_readiness(dev2_config(), probes=dependencies_row_probes(root), computed_at=NOW)


def write_manifest(root: Path) -> None:
    """Write a clean one-row dependency manifest receipt under *root*."""
    write_receipt(
        root,
        "dependency-manifest",
        ReleaseDependencyManifest(
            lock_digest=LOCK_DIGEST,
            packages=(
                LockedPackage(
                    name="example",
                    version="1.0.0",
                    license_id="MIT",
                    disposition=LicenseDisposition.ALLOWED,
                ),
            ),
        ),
    )


def test_migration_signal_suite_reds_security_review_without_a_report(tmp_path: Path) -> None:
    """An absent vulnerability report leaves the gate unavailable, never passing."""
    write_manifest(tmp_path)

    row = sweep_with_receipts(tmp_path).gate_row(ReleaseGateName.SECURITY_REVIEW)

    assert row.status is not ReleaseSignalStatus.PASS
    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert "vulnerability-report" in row.remediation


def test_migration_signal_suite_reds_security_review_on_a_stub_report(tmp_path: Path) -> None:
    """A receipt that is present but unreadable blocks the gate, never passes it."""
    write_manifest(tmp_path)
    receipt_path(tmp_path, "vulnerability-report").write_text('{"stub": true}', encoding="utf-8")

    row = sweep_with_receipts(tmp_path).gate_row(ReleaseGateName.SECURITY_REVIEW)

    assert row.status is ReleaseSignalStatus.BLOCKED
    assert "vulnerability-report" in row.remediation


def test_migration_signal_suite_reds_security_review_on_a_blocking_advisory(
    tmp_path: Path,
) -> None:
    """A report that found a blocking advisory fails the gate and names it."""
    write_manifest(tmp_path)
    write_receipt(
        tmp_path,
        "vulnerability-report",
        VulnerabilityReport(
            lock_digest=LOCK_DIGEST,
            advisories=(
                Advisory(
                    advisory_id="GHSA-0000-0000-0000",
                    package="example",
                    installed_version="1.0.0",
                    fixed_versions=("1.0.1",),
                    blocking=True,
                ),
            ),
        ),
    )

    row = sweep_with_receipts(tmp_path).gate_row(ReleaseGateName.SECURITY_REVIEW)

    assert row.status is ReleaseSignalStatus.FAIL
    assert "GHSA-0000-0000-0000" in row.remediation
