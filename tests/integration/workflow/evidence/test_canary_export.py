"""A release's canary export copies the driver certifications a walk stored.

The export is built from this machine's stored rows and nothing else: a
claimed class with no stored certification of the installed version is
refused rather than exported empty, and the certification that lands in
the export is the stored record unchanged.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.runtime.certification import DriverCertification
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.runtime.runtimes.probes import sdk_baseline
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.workflow.evidence import canary_export
from eawf.workflow.evidence.canary_export import (
    build_canary_export,
    prior_canary_evidence,
    write_canary_export,
)
from eawf.workflow.evidence.machine_certification import append_machine_certification
from eawf.workflow.evidence.provider_certification import (
    CANARY_EVIDENCE_DIRS,
    CanaryEvidence,
    CertificationRecord,
    RuntimeRunLog,
    canary_evidence_path,
    load_canary_evidence,
    provider_findings,
)
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.integration

VERSION: Final = "2.1.310"
PRIOR_KEY: Final = "REL-0.7.0.dev3"
EXPORT_KEY: Final = "REL-0.7.0.dev4"
DIGEST: Final = f"sha256:{'e' * 64}"
LIFETIME: Final = timedelta(days=3650)


@pytest.fixture(scope="module")
def committed() -> CanaryEvidence:
    """Return the committed dev3 export, the prior the tests carry forward from."""
    evidence = load_canary_evidence(REPO_ROOT, PRIOR_KEY)
    assert evidence is not None
    return evidence


def driver_certification(
    committed: CanaryEvidence,
    *,
    os_class: str = "linux",
    version: str = VERSION,
    shift: int = 0,
    lifetime: timedelta = LIFETIME,
) -> DriverCertification:
    """Return a verified certification of claude-code *version* on *os_class*.

    The certification lasts *lifetime* from its verification, so the write
    path, which measures expiry against the wall clock, stays live.
    """
    document: dict[str, Any] = committed.certifications[0].certification.model_dump(mode="json")
    verified_at = committed.certifications[0].certification.verified_at + timedelta(hours=shift)
    history = document["stage_history"]
    for stage in history:
        stage["started_at"] = stage["completed_at"] = verified_at.isoformat()
    document.update(
        os_class=os_class,
        manifest_ref=f"driver://claude-code/{version}",
        distribution_version=version,
        sdk_or_server_version=version,
        verified_at=verified_at.isoformat(),
        expires_at=(verified_at + lifetime).isoformat(),
    )
    return DriverCertification.model_validate(document)


def quarantined(base: DriverCertification, *, shift: int) -> MachineCertification:
    """Return a walk of *base*'s version whose canary failed *shift* hours after *base*."""
    probe, canary, _certify = base.stage_history[-3:]
    at = base.verified_at + timedelta(hours=shift)
    failed = canary.model_copy(
        update={
            "outcome": "failed",
            "reason_code": "capability_not_observed",
            "started_at": at,
            "completed_at": at,
        }
    )
    passed = probe.model_copy(update={"started_at": at, "completed_at": at})
    return MachineCertification.model_validate(
        {
            "certification_urn": (
                f"certification://claude-code/{base.distribution_version}/{at:%Y-%m-%d}-q"
            ),
            "runtime_id": "claude-code",
            "harness_version": base.distribution_version,
            "tuple_digest": DIGEST,
            "outcome": "quarantined",
            "reason_code": "capability_not_observed",
            "findings": ["the canary observed no containment"],
            "reason": "claude-code failed the containment canary",
            "probe": passed.model_dump(mode="json"),
            "stages": [failed.model_dump(mode="json")],
            "verified_at": at,
        }
    )


def stored(
    certification: DriverCertification | None, *, base: DriverCertification
) -> MachineCertification:
    """Return the walk row that stored *certification*, or an older row without one."""
    record = base if certification is None else certification
    probe, canary, certify = record.stage_history[-3:]
    return MachineCertification.model_validate(
        {
            "certification_urn": (
                f"certification://claude-code/{record.distribution_version}/"
                f"{record.verified_at:%Y-%m-%d}"
            ),
            "runtime_id": "claude-code",
            "harness_version": record.distribution_version,
            "tuple_digest": DIGEST,
            "outcome": "certified",
            "capabilities": [row.model_dump(mode="json") for row in record.capabilities],
            "reason": "claude-code passed the containment canary and the certify stage passed",
            "probe": probe.model_dump(mode="json"),
            "stages": [canary.model_dump(mode="json"), certify.model_dump(mode="json")],
            "verified_at": record.verified_at,
            "expires_at": record.expires_at,
            "certification": None
            if certification is None
            else certification.model_dump(mode="json"),
        }
    )


def export(
    committed: CanaryEvidence,
    rows: list[MachineCertification],
    *,
    os_classes: tuple[Any, ...] = ("linux",),
    release_key: str = EXPORT_KEY,
    installed: str = VERSION,
) -> CanaryEvidence:
    return build_canary_export(
        release_key,
        prior=committed,
        rows=rows,
        installed={"claude-code": installed},
        os_classes=os_classes,
        now=committed.certifications[0].certification.verified_at + timedelta(days=1),
    )


# ---------- the stored row ----------


def test_a_row_recorded_before_the_certification_was_kept_still_loads(
    committed: CanaryEvidence,
) -> None:
    row = stored(None, base=driver_certification(committed))
    document = row.model_dump(mode="json")
    del document["certification"]
    assert MachineCertification.model_validate(document).certification is None


def test_a_row_round_trips_with_its_certification(committed: CanaryEvidence) -> None:
    row = stored(driver_certification(committed), base=driver_certification(committed))
    assert MachineCertification.model_validate(row.model_dump(mode="json")) == row


def test_a_certification_on_a_probe_alone_is_refused(committed: CanaryEvidence) -> None:
    row = stored(driver_certification(committed), base=driver_certification(committed))
    document = row.model_dump(mode="json")
    document["stages"] = []
    with pytest.raises(ValidationError, match="record of this row's certify stage"):
        MachineCertification.model_validate(document)


def test_a_certification_of_another_certify_stage_is_refused(committed: CanaryEvidence) -> None:
    row = stored(driver_certification(committed), base=driver_certification(committed))
    other = driver_certification(committed, shift=1)
    document = row.model_dump(mode="json")
    document["certification"] = other.model_dump(mode="json")
    with pytest.raises(ValidationError, match="record of this row's certify stage"):
        MachineCertification.model_validate(document)


# ---------- the export ----------


def test_the_export_copies_the_stored_certification_unchanged(committed: CanaryEvidence) -> None:
    certification = driver_certification(committed)
    row = stored(certification, base=certification)

    evidence = export(committed, [row])

    (claim,) = evidence.advertised
    assert claim.runtime_id == "claude-code"
    assert claim.manifest_ref == f"driver://claude-code/{VERSION}"
    assert claim.manifest_digest == certification.manifest_digest
    assert claim.required_capabilities == committed.advertised[0].required_capabilities
    assert claim.os_classes == ("linux",)
    (record,) = evidence.certifications
    assert record.certification == certification
    assert record.certification_urn == row.certification_urn
    assert record.tuple_digest == row.tuple_digest
    assert evidence.release_key == EXPORT_KEY
    assert evidence.canaries == committed.canaries
    assert evidence.run_logs == () and evidence.isolation is None
    assert provider_findings(evidence) == ()


def test_the_newest_stored_certification_of_a_class_is_exported(
    committed: CanaryEvidence,
) -> None:
    older = driver_certification(committed)
    newer = driver_certification(committed, shift=2)

    evidence = export(committed, [stored(newer, base=newer), stored(older, base=older)])

    assert [record.certification for record in evidence.certifications] == [newer]


def test_a_repeated_class_is_claimed_once(committed: CanaryEvidence) -> None:
    certification = driver_certification(committed)

    evidence = export(
        committed, [stored(certification, base=certification)], os_classes=("linux", "linux")
    )

    assert evidence.advertised[0].os_classes == ("linux",)
    assert len(evidence.certifications) == 1


def test_one_certification_per_claimed_class_is_exported(committed: CanaryEvidence) -> None:
    linux = driver_certification(committed)
    macos = driver_certification(committed, os_class="macos")

    evidence = export(
        committed,
        [stored(linux, base=linux), stored(macos, base=macos)],
        os_classes=("linux", "macos"),
    )

    assert [record.certification.os_class for record in evidence.certifications] == [
        "linux",
        "macos",
    ]


@pytest.mark.parametrize(
    ("rows", "os_classes", "installed"),
    [
        pytest.param("none", ("linux",), VERSION, id="no-rows"),
        pytest.param("unkept", ("linux",), VERSION, id="row-without-certification"),
        pytest.param("linux", ("macos",), VERSION, id="class-not-certified"),
        pytest.param("linux", ("linux", "macos"), VERSION, id="one-class-short"),
        pytest.param("linux", ("linux",), "2.1.311", id="other-version-installed"),
    ],
)
def test_a_claimed_class_without_a_stored_certification_is_refused(
    committed: CanaryEvidence, rows: str, os_classes: tuple[Any, ...], installed: str
) -> None:
    certification = driver_certification(committed)
    stock = {
        "none": [],
        "unkept": [stored(None, base=certification)],
        "linux": [stored(certification, base=certification)],
    }
    with pytest.raises(ValueError, match="canary_export_uncovered"):
        export(committed, stock[rows], os_classes=os_classes, installed=installed)


def test_milestones_are_kept_only_from_the_release_s_own_export(
    committed: CanaryEvidence,
) -> None:
    certification = driver_certification(committed)
    rows = [stored(certification, base=certification)]
    assert committed.milestones

    assert export(committed, rows).milestones == ()
    assert export(committed, rows, release_key=PRIOR_KEY).milestones == committed.milestones


# ---------- the prior and the file ----------


def _commit_export(repo: Path, release_key: str) -> None:
    target = canary_evidence_path(repo, release_key)
    target.parent.mkdir(parents=True)
    shutil.copy(canary_evidence_path(REPO_ROOT, release_key), target)


def test_the_prior_is_the_nearest_earlier_committed_export(tmp_path: Path) -> None:
    _commit_export(tmp_path, PRIOR_KEY)
    prior = prior_canary_evidence(tmp_path, EXPORT_KEY)
    assert prior.release_key == PRIOR_KEY


def test_the_prior_is_the_release_s_own_export_once_committed(tmp_path: Path) -> None:
    _commit_export(tmp_path, PRIOR_KEY)
    _commit_export(tmp_path, EXPORT_KEY)
    assert prior_canary_evidence(tmp_path, EXPORT_KEY).release_key == EXPORT_KEY


def test_no_committed_prior_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="canary_export_no_prior"):
        prior_canary_evidence(tmp_path, EXPORT_KEY)


def test_an_unmapped_release_is_refused(tmp_path: Path) -> None:
    assert "REL-9.9.9" not in CANARY_EVIDENCE_DIRS
    with pytest.raises(ValueError):
        prior_canary_evidence(tmp_path, "REL-9.9.9")


def _installed(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    def probe(runtime_id: str) -> sdk_baseline.RuntimeProbeRow:
        return sdk_baseline.RuntimeProbeRow(
            runtime_id=runtime_id,
            bin_name="claude",
            installed=version is not None,
            bin_basename=None,
            bin_parent_kind=None,
            version=None if version is None else f"{version} (Claude Code)",
            subprocess_primary_surface="cli",
            advertised_sdk_flags=(),
            advertised_features=(),
            help_excerpt_sha256=None,
            error=None,
        )

    monkeypatch.setattr(canary_export, "probe_runtime", probe)


def _stored_tree(tmp_path: Path, committed: CanaryEvidence) -> Path:
    _commit_export(tmp_path, PRIOR_KEY)
    state_path = tmp_path / ".ea" / "state.json"
    certification = driver_certification(committed)
    append_machine_certification(state_path, stored(certification, base=certification))
    return state_path


def test_the_written_export_reads_back_as_the_export_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: CanaryEvidence
) -> None:
    state_path = _stored_tree(tmp_path, committed)
    _installed(monkeypatch, VERSION)

    evidence = write_canary_export(state_path, EXPORT_KEY, os_classes=("linux",))

    assert load_canary_evidence(tmp_path, EXPORT_KEY) == evidence
    text = canary_evidence_path(tmp_path, EXPORT_KEY).read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert json.loads(text)["release_key"] == EXPORT_KEY
    assert str(tmp_path) not in text


def test_an_advertised_runtime_not_installed_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: CanaryEvidence
) -> None:
    state_path = _stored_tree(tmp_path, committed)
    _installed(monkeypatch, None)
    with pytest.raises(ValueError, match="canary_export_not_installed"):
        write_canary_export(state_path, EXPORT_KEY, os_classes=("linux",))
    assert not canary_evidence_path(tmp_path, EXPORT_KEY).exists()


def test_a_later_quarantine_refuses_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: CanaryEvidence
) -> None:
    state_path = _stored_tree(tmp_path, committed)
    append_machine_certification(state_path, quarantined(driver_certification(committed), shift=1))
    _installed(monkeypatch, VERSION)

    with pytest.raises(ValueError, match="canary_export_quarantined"):
        write_canary_export(state_path, EXPORT_KEY, os_classes=("linux",))
    assert not canary_evidence_path(tmp_path, EXPORT_KEY).exists()


def test_an_expired_certification_refuses_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: CanaryEvidence
) -> None:
    _commit_export(tmp_path, PRIOR_KEY)
    state_path = tmp_path / ".ea" / "state.json"
    lapsed = driver_certification(committed, lifetime=timedelta(days=1))
    append_machine_certification(state_path, stored(lapsed, base=lapsed))
    _installed(monkeypatch, VERSION)

    with pytest.raises(ValueError, match="canary_export_expired"):
        write_canary_export(state_path, EXPORT_KEY, os_classes=("linux",))
    assert not canary_evidence_path(tmp_path, EXPORT_KEY).exists()


def test_a_re_export_keeps_isolation_run_logs_and_other_os_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: CanaryEvidence
) -> None:
    state_path = _stored_tree(tmp_path, committed)
    _installed(monkeypatch, VERSION)
    (macos,) = committed.certifications
    replaced = CertificationRecord(
        certification_urn="certification://claude-code/2.1.309/2026-09-20",
        runtime_id="claude-code",
        tuple_digest=DIGEST,
        certification=driver_certification(committed, version="2.1.309"),
    )
    run_log = RuntimeRunLog(
        runtime_id="claude-code",
        manifest_ref=f"driver://claude-code/{VERSION}",
        ran_at=macos.certification.verified_at,
        exit_code=0,
        log_ref="artifact://dev4-conformance/2026-09-26/run-log",
    )
    assert committed.isolation is not None
    own = committed.model_copy(
        update={
            "release_key": EXPORT_KEY,
            "certifications": (replaced, macos),
            "run_logs": (run_log,),
        }
    )
    target = canary_evidence_path(tmp_path, EXPORT_KEY)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(own.model_dump(mode="json")), encoding="utf-8")

    evidence = write_canary_export(state_path, EXPORT_KEY, os_classes=("linux",))
    first = target.read_text(encoding="utf-8")
    again = write_canary_export(state_path, EXPORT_KEY, os_classes=("linux",))

    assert evidence.isolation == committed.isolation
    assert evidence.run_logs == (run_log,)
    assert evidence.milestones == committed.milestones
    assert [record.certification for record in evidence.certifications] == [
        driver_certification(committed),
        macos.certification,
    ]
    assert again == evidence
    assert target.read_text(encoding="utf-8") == first


def test_a_quarantine_older_than_the_certification_is_shadowed(
    committed: CanaryEvidence,
) -> None:
    certification = driver_certification(committed, shift=2)
    rows = [
        quarantined(driver_certification(committed), shift=1),
        stored(certification, base=certification),
    ]

    assert [record.certification for record in export(committed, rows).certifications] == [
        certification
    ]


def test_a_quarantine_verified_with_the_certification_wins_when_appended_later(
    committed: CanaryEvidence,
) -> None:
    certification = driver_certification(committed, shift=1)
    rows = [
        stored(certification, base=certification),
        quarantined(driver_certification(committed), shift=1),
    ]

    with pytest.raises(ValueError, match="canary_export_quarantined"):
        export(committed, rows)


def test_a_quarantine_of_another_version_does_not_refuse(committed: CanaryEvidence) -> None:
    certification = driver_certification(committed)
    other = driver_certification(committed, version="2.1.311")
    rows = [stored(certification, base=certification), quarantined(other, shift=1)]

    assert len(export(committed, rows).certifications) == 1


@pytest.mark.parametrize(
    ("lifetime", "refused"),
    [
        pytest.param(timedelta(days=1), True, id="expires-at-the-export"),
        pytest.param(timedelta(days=1, seconds=1), False, id="expires-just-after"),
    ],
)
def test_the_expiry_is_measured_against_the_export_time(
    committed: CanaryEvidence, lifetime: timedelta, refused: bool
) -> None:
    certification = driver_certification(committed, lifetime=lifetime)
    rows = [stored(certification, base=certification)]
    if refused:
        with pytest.raises(ValueError, match="canary_export_expired"):
            export(committed, rows)
    else:
        assert len(export(committed, rows).certifications) == 1


def test_another_release_s_isolation_and_run_logs_are_not_carried(
    committed: CanaryEvidence,
) -> None:
    certification = driver_certification(committed)
    evidence = export(committed, [stored(certification, base=certification)])
    assert committed.isolation is not None
    assert evidence.isolation is None and evidence.run_logs == ()
    assert [record.certification for record in evidence.certifications] == [certification]


# ---------- the verb ----------


def test_the_verb_writes_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: CanaryEvidence
) -> None:
    _stored_tree(tmp_path, committed)
    _installed(monkeypatch, VERSION)
    monkeypatch.delenv("EA_STATE", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "--json",
            "release",
            "canary-export",
            "0.7.0.dev4",
            "--os-class",
            "linux",
        ],
    )

    assert result.exit_code == 0, result.output
    assert load_canary_evidence(tmp_path, EXPORT_KEY) is not None
    assert EXPORT_KEY in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["0.7.0.dev4", "--os-class", "solaris"], id="unknown-class"),
        pytest.param(["0.7.0.dev4", "--os-class", "macos"], id="uncovered-class"),
        pytest.param(["not-a-version", "--os-class", "linux"], id="bad-version"),
    ],
)
def test_the_verb_refuses_what_it_cannot_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: CanaryEvidence, args: list[str]
) -> None:
    _stored_tree(tmp_path, committed)
    _installed(monkeypatch, VERSION)
    monkeypatch.delenv("EA_STATE", raising=False)

    result = CliRunner().invoke(app, ["-w", str(tmp_path), "release", "canary-export", *args])

    assert result.exit_code == exit_codes.VALIDATION_ERROR, result.output
    assert not canary_evidence_path(tmp_path, EXPORT_KEY).exists()


def test_the_verb_needs_a_class(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["-w", str(tmp_path), "release", "canary-export", "0.7.0.dev4"]
    )
    assert result.exit_code != 0


# ---------- the kept classes of a re-export ----------


def _re_export(
    prior: CanaryEvidence,
    rows: list[MachineCertification],
    os_class: Any,
    *,
    now: datetime,
) -> CanaryEvidence:
    return build_canary_export(
        EXPORT_KEY,
        prior=prior,
        rows=rows,
        installed={"claude-code": VERSION},
        os_classes=(os_class,),
        now=now,
    )


def test_a_re_export_claims_the_kept_os_classes(committed: CanaryEvidence) -> None:
    start = committed.certifications[0].certification.verified_at
    macos = driver_certification(committed, os_class="macos")
    linux = driver_certification(committed, shift=1)
    on_mac = _re_export(
        committed, [stored(macos, base=macos)], "macos", now=start + timedelta(days=1)
    )
    assert on_mac.advertised[0].os_classes == ("macos",)

    on_linux = _re_export(
        on_mac, [stored(linux, base=linux)], "linux", now=start + timedelta(days=1)
    )

    (claim,) = on_linux.advertised
    assert claim.os_classes == ("linux", "macos")
    assert [record.certification for record in on_linux.certifications] == [linux, macos]
    assert provider_findings(on_linux) == ()
    assert (
        _re_export(on_mac, [stored(linux, base=linux)], "linux", now=start + timedelta(days=1))
        == on_linux
    )

    lapsing = driver_certification(committed, os_class="macos", lifetime=timedelta(days=2))
    short_lived = _re_export(
        committed, [stored(lapsing, base=lapsing)], "macos", now=start + timedelta(days=1)
    )
    lasting = driver_certification(committed, shift=72)
    with pytest.raises(ValueError, match=r"canary_export_expired: .*'macos'"):
        _re_export(
            short_lived, [stored(lasting, base=lasting)], "linux", now=start + timedelta(days=3)
        )


def test_a_newer_local_quarantine_refuses_a_kept_class(committed: CanaryEvidence) -> None:
    start = committed.certifications[0].certification.verified_at
    macos = driver_certification(committed, os_class="macos")
    linux = driver_certification(committed, shift=2)
    on_mac = _re_export(
        committed, [stored(macos, base=macos)], "macos", now=start + timedelta(days=1)
    )
    newer = [quarantined(macos, shift=1), stored(linux, base=linux)]
    with pytest.raises(ValueError, match="canary_export_quarantined"):
        _re_export(on_mac, newer, "linux", now=start + timedelta(days=1))

    older = [quarantined(macos, shift=-1), stored(linux, base=linux)]
    assert _re_export(on_mac, older, "linux", now=start + timedelta(days=1)).advertised[
        0
    ].os_classes == ("linux", "macos")


def test_a_kept_class_of_another_version_is_kept_but_not_claimed(
    committed: CanaryEvidence,
) -> None:
    (macos,) = committed.certifications
    own = committed.model_copy(update={"release_key": EXPORT_KEY})
    linux = driver_certification(committed)

    evidence = _re_export(
        own, [stored(linux, base=linux)], "linux", now=macos.certification.expires_at
    )

    assert evidence.advertised[0].os_classes == ("linux",)
    assert evidence.certifications[-1] == macos
