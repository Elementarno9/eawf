"""A certification may stand for a range of versions, each admitted after a re-probe.

The canary export claims the installed version of each advertised runtime.
When no walk certified that version but a stored certification's range
covers it, the export probes the installed binary again: a passed probe is
recorded and admits the claim on the range's certification, a failed one
refuses it, and a version outside every range is refused naming the ranges.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.certification import CertifiedVersionRange, DriverCertification
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.workflow.evidence import canary_export
from eawf.workflow.evidence.canary_export import build_canary_export, write_canary_export
from eawf.workflow.evidence.machine_certification import (
    append_machine_certification,
    read_machine_certifications,
)
from eawf.workflow.evidence.provider_certification import (
    CanaryEvidence,
    canary_evidence_path,
    load_canary_evidence,
)
from tests.conftest import REPO_ROOT
from tests.integration.workflow.evidence.test_canary_export import (
    DIGEST,
    EXPORT_KEY,
    PRIOR_KEY,
    VERSION,
    _commit_export,
    _installed,
    driver_certification,
    quarantined,
    stored,
)

INSIDE: Final = "2.1.314"
RANGE: Final = CertifiedVersionRange(min_version=VERSION, max_version="2.1.320")


@pytest.fixture(scope="module")
def committed() -> CanaryEvidence:
    """Return the committed dev3 export, the prior the exports carry forward from."""
    evidence = load_canary_evidence(REPO_ROOT, PRIOR_KEY)
    assert evidence is not None
    return evidence


def ranged(
    certification: DriverCertification, version_range: CertifiedVersionRange
) -> MachineCertification:
    """Return the walk row that stored *certification* with *version_range*."""
    document = stored(certification, base=certification).model_dump(mode="json")
    document["version_range"] = version_range.model_dump(mode="json")
    return MachineCertification.model_validate(document)


def reprobed(base: DriverCertification, version: str, *, passed: bool) -> MachineCertification:
    """Return the probe-only row a re-probe of *version* records three hours after *base*."""
    at = base.verified_at + timedelta(hours=3)
    update: dict[str, Any] = {"started_at": at, "completed_at": at}
    if not passed:
        update |= {"outcome": "failed", "reason_code": "capability_not_observed"}
    probe = base.stage_history[-3].model_copy(update=update)
    common: dict[str, Any] = {
        "certification_urn": f"certification://claude-code/{version}/{at:%Y-%m-%d}",
        "runtime_id": "claude-code",
        "harness_version": version,
        "tuple_digest": DIGEST,
        "probe": probe.model_dump(mode="json"),
        "verified_at": at,
    }
    if passed:
        return MachineCertification.model_validate(
            common
            | {
                "outcome": "certified",
                "capabilities": [row.model_dump(mode="json") for row in base.capabilities],
                "expires_at": base.expires_at,
                "reason": f"claude-code {version} passed the conformance probe",
            }
        )
    return MachineCertification.model_validate(
        common
        | {
            "outcome": "quarantined",
            "reason_code": "capability_not_observed",
            "findings": ["streaming: --output-format stream-json is not advertised"],
            "reason": f"claude-code {version} failed the conformance probe",
        }
    )


class Prober:
    """A re-probe that records its row into a tmp store, counting its calls."""

    def __init__(self, state_path: Path, row: MachineCertification) -> None:
        self.state_path = state_path
        self.row = row
        self.calls: list[str] = []

    def certify(self, runtime_id: str) -> MachineCertification:
        self.calls.append(runtime_id)
        append_machine_certification(self.state_path, self.row)
        return self.row


def export(
    committed: CanaryEvidence,
    rows: list[MachineCertification],
    *,
    installed: str,
    reprobe: Callable[[str], MachineCertification],
    os_classes: tuple[Any, ...] = ("linux",),
) -> CanaryEvidence:
    return build_canary_export(
        EXPORT_KEY,
        prior=committed,
        declared=(),
        rows=rows,
        installed={"claude-code": installed},
        os_classes=os_classes,
        now=committed.certifications[0].certification.verified_at + timedelta(days=1),
        reprobe=reprobe,
    )


def _never(runtime_id: str) -> MachineCertification:
    raise AssertionError(f"{runtime_id} was re-probed")


# ---------- the range ----------


@pytest.mark.parametrize(
    ("version", "covered"),
    [
        pytest.param("2.1.310", True, id="exactly-min"),
        pytest.param("2.1.320", True, id="exactly-max"),
        pytest.param("2.1.314", True, id="inside"),
        pytest.param("2.1.309", False, id="one-below-min"),
        pytest.param("2.1.321", False, id="one-above-max"),
        pytest.param("2.1.310-rc.1", False, id="pre-release-of-min-sorts-below-it"),
        pytest.param("2.1.320-rc.1", True, id="pre-release-of-max-sorts-below-it"),
        pytest.param("2.1.321-rc.1", False, id="pre-release-above-max"),
        pytest.param("2.1.320+build.7", True, id="build-metadata-ignored"),
        pytest.param("3.0.0", False, id="next-major"),
    ],
)
def test_a_range_covers_versions_between_its_inclusive_bounds(version: str, covered: bool) -> None:
    assert RANGE.covers(version) is covered


def test_a_single_version_range_covers_that_version_alone() -> None:
    single = CertifiedVersionRange(min_version=VERSION, max_version=VERSION)
    assert single.covers(VERSION)
    assert not single.covers("2.1.311")
    assert str(single) == f"{VERSION}..{VERSION}"


def test_a_range_whose_bounds_include_pre_releases_admits_them() -> None:
    rc = CertifiedVersionRange(min_version="2.2.0-rc.1", max_version="2.2.0")
    assert rc.covers("2.2.0-rc.2")
    assert not rc.covers("2.2.0-beta.1")


def test_an_empty_range_is_refused() -> None:
    with pytest.raises(ValidationError, match="sorts above max_version"):
        CertifiedVersionRange(min_version="2.1.320", max_version="2.1.310")


def test_a_pre_release_above_its_release_is_refused_as_an_empty_range() -> None:
    with pytest.raises(ValidationError, match="sorts above max_version"):
        CertifiedVersionRange(min_version="2.2.0", max_version="2.2.0-rc.1")


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"min_version": "2.1", "max_version": "2.1.320"}, id="bound-not-semver"),
        pytest.param({"min_version": "", "max_version": "2.1.320"}, id="bound-empty"),
        pytest.param({"min_version": 2, "max_version": "2.1.320"}, id="bound-not-a-string"),
        pytest.param({"max_version": "2.1.320"}, id="bound-missing"),
        pytest.param({"min_version": VERSION, "max_version": "2.1.320", "x": 1}, id="unknown-key"),
    ],
)
def test_a_malformed_range_is_refused(document: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CertifiedVersionRange.model_validate(document)


# ---------- the stored row ----------


def test_a_ranged_row_round_trips(committed: CanaryEvidence) -> None:
    row = ranged(driver_certification(committed), RANGE)
    assert MachineCertification.model_validate(row.model_dump(mode="json")) == row
    assert row.covers(INSIDE)
    assert row.certified_range() == f"{VERSION}..2.1.320"


def test_an_old_exact_row_covers_its_own_version_alone(committed: CanaryEvidence) -> None:
    certification = driver_certification(committed)
    document = stored(certification, base=certification).model_dump(mode="json")
    del document["version_range"]
    row = MachineCertification.model_validate(document)
    assert row.version_range is None
    assert row.covers(VERSION)
    assert not row.covers(INSIDE)
    assert row.certified_range() == VERSION


def test_a_range_without_a_kept_certification_is_refused(committed: CanaryEvidence) -> None:
    certification = driver_certification(committed)
    document = stored(None, base=certification).model_dump(mode="json")
    document["version_range"] = RANGE.model_dump(mode="json")
    with pytest.raises(ValidationError, match="rides on a kept driver certification"):
        MachineCertification.model_validate(document)


def test_a_range_that_excludes_the_row_s_own_version_is_refused(committed: CanaryEvidence) -> None:
    certification = driver_certification(committed)
    with pytest.raises(ValidationError, match="covers its own version"):
        ranged(certification, CertifiedVersionRange(min_version="2.1.311", max_version="2.1.320"))


# ---------- the export ----------


def test_a_version_inside_the_range_is_admitted_after_a_passing_re_probe(
    tmp_path: Path, committed: CanaryEvidence
) -> None:
    certification = driver_certification(committed)
    row = ranged(certification, RANGE)
    state_path = tmp_path / ".ea" / "state.json"
    prober = Prober(state_path, reprobed(certification, INSIDE, passed=True))

    evidence = export(committed, [row], installed=INSIDE, reprobe=prober.certify)

    assert prober.calls == ["claude-code"]
    (record,) = evidence.certifications
    assert record.certification == certification
    assert record.certification_urn == row.certification_urn
    assert evidence.advertised[0].manifest_ref == certification.manifest_ref
    (recorded,) = read_machine_certifications(state_path)
    assert (recorded.harness_version, recorded.outcome) == (INSIDE, "certified")


@pytest.mark.parametrize("installed", [VERSION, "2.1.320"], ids=["exactly-min", "exactly-max"])
def test_a_range_bound_is_admitted(
    committed: CanaryEvidence, installed: str, tmp_path: Path
) -> None:
    certification = driver_certification(committed)
    prober = Prober(tmp_path / "state.json", reprobed(certification, installed, passed=True))
    evidence = export(
        committed, [ranged(certification, RANGE)], installed=installed, reprobe=prober.certify
    )
    assert [record.certification for record in evidence.certifications] == [certification]
    # The certified version itself needs no re-probe; the other bound does.
    assert prober.calls == ([] if installed == VERSION else ["claude-code"])


def test_a_version_inside_the_range_is_refused_when_its_re_probe_fails(
    tmp_path: Path, committed: CanaryEvidence
) -> None:
    certification = driver_certification(committed)
    row = ranged(certification, RANGE)
    failed = reprobed(certification, INSIDE, passed=False)
    prober = Prober(tmp_path / ".ea" / "state.json", failed)

    with pytest.raises(ValueError, match="canary_export_reprobe_failed") as refusal:
        export(committed, [row], installed=INSIDE, reprobe=prober.certify)

    message = str(refusal.value)
    assert f"claude-code {INSIDE}" in message
    assert str(failed.certification_urn) in message
    assert str(row.certification_urn) in message
    assert "capability_not_observed" in message
    assert "streaming" in message


def test_a_failed_re_probe_on_record_refuses_the_next_export_as_a_quarantine(
    committed: CanaryEvidence,
) -> None:
    certification = driver_certification(committed)
    rows = [ranged(certification, RANGE), reprobed(certification, INSIDE, passed=False)]
    with pytest.raises(ValueError, match="canary_export_quarantined"):
        export(committed, rows, installed=INSIDE, reprobe=_never)


@pytest.mark.parametrize(
    "installed",
    ["2.1.309", "2.1.321", "2.1.310-rc.1", "3.0.0"],
    ids=["below", "above", "pre-release-below-min", "next-major"],
)
def test_a_version_outside_every_range_is_refused_naming_the_ranges(
    committed: CanaryEvidence, installed: str
) -> None:
    certification = driver_certification(committed)
    older = driver_certification(committed, version="2.1.300", shift=-2)
    rows = [ranged(certification, RANGE), stored(older, base=older)]

    with pytest.raises(ValueError, match="canary_export_uncovered") as refusal:
        export(committed, rows, installed=installed, reprobe=_never)

    assert f"certified ranges: {VERSION}..2.1.320, 2.1.300" in str(refusal.value)


def test_an_old_exact_row_does_not_cover_another_version(committed: CanaryEvidence) -> None:
    certification = driver_certification(committed)
    with pytest.raises(ValueError, match=f"certified ranges: {VERSION}\\)"):
        export(
            committed, [stored(certification, base=certification)], installed=INSIDE, reprobe=_never
        )


def test_no_certification_at_all_names_no_range(committed: CanaryEvidence) -> None:
    with pytest.raises(ValueError, match="certified ranges: none"):
        export(committed, [], installed=INSIDE, reprobe=_never)


def test_a_quarantine_of_the_range_s_own_version_withdraws_the_range(
    committed: CanaryEvidence,
) -> None:
    certification = driver_certification(committed)
    rows = [ranged(certification, RANGE), quarantined(certification, shift=1)]
    with pytest.raises(ValueError, match="canary_export_uncovered"):
        export(committed, rows, installed=INSIDE, reprobe=_never)


def test_an_expired_range_is_refused_before_any_re_probe(committed: CanaryEvidence) -> None:
    lapsed = driver_certification(committed, lifetime=timedelta(hours=1))
    with pytest.raises(ValueError, match="canary_export_expired"):
        export(committed, [ranged(lapsed, RANGE)], installed=INSIDE, reprobe=_never)


def test_the_newest_covering_range_is_exported(tmp_path: Path, committed: CanaryEvidence) -> None:
    older = driver_certification(committed)
    newer = driver_certification(committed, version="2.1.312", shift=2)
    wide = CertifiedVersionRange(min_version="2.1.300", max_version="2.1.320")
    prober = Prober(tmp_path / "state.json", reprobed(newer, INSIDE, passed=True))

    evidence = export(
        committed,
        [ranged(newer, wide), ranged(older, RANGE)],
        installed=INSIDE,
        reprobe=prober.certify,
    )

    assert [record.certification for record in evidence.certifications] == [newer]


def test_one_re_probe_admits_every_claimed_class(tmp_path: Path, committed: CanaryEvidence) -> None:
    linux = driver_certification(committed)
    macos = driver_certification(committed, os_class="macos")
    prober = Prober(tmp_path / "state.json", reprobed(linux, INSIDE, passed=True))

    evidence = export(
        committed,
        [ranged(linux, RANGE), ranged(macos, RANGE)],
        installed=INSIDE,
        reprobe=prober.certify,
        os_classes=("linux", "macos"),
    )

    assert prober.calls == ["claude-code"]
    assert len(evidence.certifications) == 2


# ---------- the written export ----------


def test_the_written_export_re_probes_through_the_tree_s_certifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: CanaryEvidence
) -> None:
    _commit_export(tmp_path, PRIOR_KEY)
    state_path = tmp_path / ".ea" / "state.json"
    certification = driver_certification(committed)
    append_machine_certification(state_path, ranged(certification, RANGE))
    _installed(monkeypatch, INSIDE)
    prober = Prober(state_path, reprobed(certification, INSIDE, passed=True))
    trees: list[Path] = []

    def certifier_for(tree_root: Path) -> Prober:
        trees.append(tree_root)
        return prober

    monkeypatch.setattr(canary_export, "certifier_for", certifier_for)

    evidence = write_canary_export(state_path, EXPORT_KEY, os_classes=("linux",))

    assert trees == [state_path.parent]
    assert prober.calls == ["claude-code"]
    assert load_canary_evidence(tmp_path, EXPORT_KEY) == evidence
    assert [row.harness_version for row in read_machine_certifications(state_path)] == [
        VERSION,
        INSIDE,
    ]


def test_a_failed_re_probe_writes_no_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: CanaryEvidence
) -> None:
    _commit_export(tmp_path, PRIOR_KEY)
    state_path = tmp_path / ".ea" / "state.json"
    certification = driver_certification(committed)
    append_machine_certification(state_path, ranged(certification, RANGE))
    _installed(monkeypatch, INSIDE)
    prober = Prober(state_path, reprobed(certification, INSIDE, passed=False))
    monkeypatch.setattr(canary_export, "certifier_for", lambda tree_root: prober)

    with pytest.raises(ValueError, match="canary_export_reprobe_failed"):
        write_canary_export(state_path, EXPORT_KEY, os_classes=("linux",))

    assert not canary_evidence_path(tmp_path, EXPORT_KEY).exists()
    assert read_machine_certifications(state_path)[-1].outcome == "quarantined"


def test_the_exported_record_carries_its_certified_range(committed: CanaryEvidence) -> None:
    certification = driver_certification(committed)

    evidence = export(committed, [ranged(certification, RANGE)], installed=VERSION, reprobe=_never)

    assert evidence.certifications[0].version_range == RANGE


def test_an_exact_row_exports_no_range(committed: CanaryEvidence) -> None:
    certification = driver_certification(committed)
    row = stored(certification, base=certification)

    evidence = export(committed, [row], installed=VERSION, reprobe=_never)

    assert evidence.certifications[0].version_range is None
