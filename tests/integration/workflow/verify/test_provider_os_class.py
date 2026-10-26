"""The ``provider`` row needs a certification on every OS a runtime ships on.

A ``DriverCertification`` records the one ``os_class`` its conformance ran
on. A claim graded against whichever certification of its manifest came
first would let a macOS run clear a runtime that also ships on Linux, where
the jail and the launcher are different code. Each advertised claim
therefore names its ``os_classes``, and every class without a certification
of the claim's manifest is reported as ``os_class_uncertified``.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.observability.bench.dispatch_scorecard import (
    DispatchScorecard,
    DispatchTier,
    check_scorecard,
)
from eawf.workflow.evidence.provider_certification import (
    CANARY_EVIDENCE_DIRS,
    CanaryEvidenceGap,
    RuntimeClaim,
    canary_evidence_path,
    load_canary_evidence,
    provider_evidence_refs,
    provider_findings,
)
from eawf.workflow.release.admission import checkpoint_release_config
from eawf.workflow.release.canary_receipts import DISPATCH_SMOKE_RECEIPTS_FILENAME
from eawf.workflow.release.signal_probes import provider_probe
from eawf.workflow.verify.release_readiness import ReleaseSignalContext

pytestmark = pytest.mark.integration

#: This checkout, whose committed export carries one macOS certification.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

DEV4_VERSION: Final = "0.7.0.dev4"
DEV4_KEY: Final = f"REL-{DEV4_VERSION}"

#: The URN the committed macOS certification is cited by.
MACOS_URN: Final = "certification://claude-code/2026-09-18"

#: The URN :func:`certified_on` gives a Linux certification.
LINUX_URN: Final = "certification://claude-code/2026-09-18-linux"


def certified_on(document: dict[str, Any], os_class: str) -> dict[str, Any]:
    """Return *document* with its first certification repeated on *os_class*.

    Args:
        document: A decoded export carrying at least one certification.
        os_class: The class the added certification records.

    Returns:
        A copy whose certifications end in a record identical to the first
        but for its os_class, its id and the URN it is cited by.
    """
    result = copy.deepcopy(document)
    record = copy.deepcopy(result["certifications"][0])
    record["certification_urn"] = f"{record['certification_urn']}-{os_class}"
    record["certification"]["certification_id"] = (
        f"{record['certification']['certification_id']}-{os_class}"
    )
    record["certification"]["os_class"] = os_class
    result["certifications"].append(record)
    return result


def check_document(run_key: str, *, turns: int = 7) -> dict[str, Any]:
    """Return a ``bench dispatch --check --json`` document for *run_key*.

    The rows come from the scorecard producer's own fixture check, as the
    live smoke's ``bench dispatch`` writes them.

    Args:
        run_key: The Run the document's scorecard names.
        turns: The scorecard's model calls; past 25 breaches the fixture tier.

    Returns:
        A document in the shape the live smoke files beside its receipt.
    """
    scorecard = DispatchScorecard(
        run_key=run_key,
        status=RunStatus.COMPLETED,
        turns=turns,
        read_truncation_rate=Decimal(0),
        identical_reread_rate=Decimal(0),
        total_tokens=1000,
        cache_read_tokens=100,
        first_patch_seconds=Decimal("12.5"),
        wall_seconds=Decimal("30.5"),
    )
    return {"check": check_scorecard(scorecard, DispatchTier.FIXTURE).model_dump(mode="json")}


def smoke_filed(
    root: Path,
    release_key: str,
    os_classes: tuple[str, ...] = ("linux", "macos"),
    *,
    scorecard: str = "pass",
) -> Path:
    """File a live dispatch smoke receipt per *os_classes* in *release_key*'s evidence.

    Each receipt's Run gets a clean check document beside the receipt file,
    as the live smoke writes one.

    Args:
        root: The staged checkout root.
        release_key: The release whose evidence directory is written.
        os_classes: The classes a receipt is filed for, one each.
        scorecard: What every receipt's scorecard reads.

    Returns:
        The staged checkout root.
    """
    path = root.joinpath(*CANARY_EVIDENCE_DIRS[release_key], DISPATCH_SMOKE_RECEIPTS_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    for number in range(1, len(os_classes) + 1):
        run_key = f"RUN-{number:08d}"
        (path.parent / f"dispatch-check-{run_key}.json").write_text(
            json.dumps(check_document(run_key)), encoding="utf-8"
        )
    receipts = [
        {
            "os_class": os_class,
            "runtime_id": "claude-code",
            "run_refs": [f"eawf://SMOKE/SMOKE/SMOKE/run/RUN-{number:08d}"],
            "scorecard": scorecard,
            "evidence_refs": [f"dispatch-check-RUN-{number:08d}.json"],
        }
        for number, os_class in enumerate(os_classes, start=1)
    ]
    path.write_text(
        json.dumps({"release_key": release_key, "receipts": receipts}, indent=2), encoding="utf-8"
    )
    return root


def committed(*, os_classes: list[str] | None = None) -> dict[str, Any]:
    """Return the committed dev4 export, optionally claiming *os_classes*."""
    document: dict[str, Any] = json.loads(
        canary_evidence_path(REPO_ROOT, DEV4_KEY).read_text(encoding="utf-8")
    )
    if os_classes is not None:
        document["advertised"][0]["os_classes"] = os_classes
    return document


def staged(tmp_path: Path, document: dict[str, Any]) -> Path:
    """Write *document* as the dev4 export of a checkout at *tmp_path*."""
    path = canary_evidence_path(tmp_path, DEV4_KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return tmp_path


def probe_at(repo_root: Path) -> Any:
    """Return the dev4 ``provider`` outcome for a checkout at *repo_root*."""
    config = checkpoint_release_config(DEV4_VERSION, repo_root=REPO_ROOT)
    context = ReleaseSignalContext(config, ReleaseSignalName.PROVIDER, None)
    return provider_probe(context, repo_root=repo_root)


def os_class_gaps(repo_root: Path) -> list[str]:
    """Return the detail of every ``os_class_uncertified`` finding at *repo_root*."""
    evidence = load_canary_evidence(repo_root, DEV4_KEY)
    assert evidence is not None
    return [
        finding.detail
        for finding in provider_findings(evidence)
        if finding.gap is CanaryEvidenceGap.OS_CLASS_UNCERTIFIED
    ]


# --- the gap fires ---------------------------------------------------


def test_os_class_gap_fails_linux_and_macos_with_only_macos_certified(tmp_path: Path) -> None:
    outcome = probe_at(staged(tmp_path, committed(os_classes=["linux", "macos"])))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.OS_CLASS_UNCERTIFIED.value in outcome.remediation
    assert "'claude-code' is advertised on 'linux'" in outcome.remediation
    assert "advertised on 'macos'" not in outcome.remediation
    assert outcome.evidence_refs == (MACOS_URN,)


def test_os_class_gap_fires_on_the_committed_macos_only_export() -> None:
    """The real defect: the committed export certifies macOS and ships on both."""
    gaps = os_class_gaps(REPO_ROOT)

    assert len(gaps) == 1
    assert "'linux'" in gaps[0]
    assert "driver://claude-code/2.1.274" in gaps[0]


def test_os_class_gap_names_each_uncertified_class(tmp_path: Path) -> None:
    root = staged(tmp_path, committed(os_classes=["linux", "macos", "windows"]))

    gaps = os_class_gaps(root)

    assert [gap.split(" and ")[0] for gap in gaps] == [
        "'claude-code' is advertised on 'linux'",
        "'claude-code' is advertised on 'windows'",
    ]


def test_os_class_gap_ignores_a_certification_on_an_unclaimed_class(tmp_path: Path) -> None:
    """A macOS record neither clears nor is cited for a Linux-only claim."""
    root = staged(tmp_path, committed(os_classes=["linux"]))
    evidence = load_canary_evidence(root, DEV4_KEY)
    assert evidence is not None

    findings = provider_findings(evidence)

    assert [finding.gap for finding in findings] == [CanaryEvidenceGap.OS_CLASS_UNCERTIFIED]
    assert provider_evidence_refs(evidence) == ()


def test_os_class_gap_still_grades_each_claimed_certification(tmp_path: Path) -> None:
    """Covering a class is not enough; the record on it must also earn itself."""
    document = certified_on(committed(), "linux")
    document["certifications"][-1]["certification"]["overall_status"] = "expired"

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.CERTIFICATION_NOT_VERIFIED.value in outcome.remediation
    assert CanaryEvidenceGap.OS_CLASS_UNCERTIFIED.value not in outcome.remediation


# --- the gap clears --------------------------------------------------


def test_os_class_gap_clears_when_linux_and_macos_are_certified(tmp_path: Path) -> None:
    outcome = probe_at(smoke_filed(staged(tmp_path, certified_on(committed(), "linux")), DEV4_KEY))

    assert outcome.status is ReleaseSignalStatus.PASS, outcome.remediation
    assert outcome.evidence_refs == (LINUX_URN, MACOS_URN)


def test_os_class_gap_clears_for_a_single_certified_class(tmp_path: Path) -> None:
    outcome = probe_at(
        smoke_filed(staged(tmp_path, committed(os_classes=["macos"])), DEV4_KEY, ("macos",))
    )

    assert outcome.status is ReleaseSignalStatus.PASS, outcome.remediation
    assert outcome.evidence_refs == (MACOS_URN,)


def test_os_class_gap_cites_a_repeated_class_once(tmp_path: Path) -> None:
    root = staged(tmp_path, committed(os_classes=["macos", "macos"]))
    evidence = load_canary_evidence(root, DEV4_KEY)
    assert evidence is not None

    assert provider_findings(evidence) == ()
    assert provider_evidence_refs(evidence) == (MACOS_URN,)


def test_os_class_gap_names_a_repeated_uncertified_class_once(tmp_path: Path) -> None:
    root = staged(tmp_path, committed(os_classes=["linux", "linux"]))

    assert len(os_class_gaps(root)) == 1


# --- the claim's own boundary ----------------------------------------


def test_os_classes_defaults_to_linux_and_macos() -> None:
    claim = RuntimeClaim.model_validate(committed()["advertised"][0])

    assert claim.os_classes == ("linux", "macos")


def test_os_classes_refuses_an_empty_tuple(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not validate"):
        load_canary_evidence(staged(tmp_path, committed(os_classes=[])), DEV4_KEY)


def test_os_classes_refuses_an_unknown_class(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not validate"):
        load_canary_evidence(staged(tmp_path, committed(os_classes=["freebsd"])), DEV4_KEY)


def test_os_classes_refuses_a_bare_string() -> None:
    row = committed()["advertised"][0] | {"os_classes": "linux"}

    with pytest.raises(ValidationError):
        RuntimeClaim.model_validate(row)
