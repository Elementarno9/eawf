"""Builders shared by every ``0.7.0.dev1`` release test.

The release suite spans two mirrors -- the kernel vocabulary under
``tests/unit/kernel/release/`` and the workflow behaviour under
``tests/unit/workflow/release/`` -- and both drive the same authored
checkpoint. Keeping the builders in one module rather than in each
directory's ``conftest`` is what stops the two mirrors from drifting
into two slightly different dev1 fixtures.

Every builder here works against the *authored* ``0.7.0.dev1``
configuration rather than a hand-built stand-in, so a change to the
checkpoint file reds these tests instead of leaving them agreeing with a
fixture nobody ships.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalProbe,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.publication import PublicationOperation, require_attempt
from eawf.kernel.spec.release import (
    AdoptedTargetObservation,
    Release,
    ReleaseAdoption,
    ReleaseChannel,
    ReleaseStatus,
    ReleaseTargetStatus,
    semver_equivalent,
)
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.workflow.release.adapters import RegistryReader
from eawf.workflow.release.advance import draft_release_for
from eawf.workflow.release.dependencies import (
    LicenseDisposition,
    LockedPackage,
    ReleaseDependencyManifest,
)
from eawf.workflow.release.observation import (
    FrozenManifest,
    ObservationRequest,
    RecordedResponse,
    observation_request,
)
from eawf.workflow.release.pipeline_receipts import write_receipt
from eawf.workflow.release.publication import begin_publication, begin_verification
from eawf.workflow.release.registry_readers import (
    HttpOpener,
    HttpReply,
    NpmRegistryReader,
    PackageIndexReader,
    SourceHostReleaseReader,
    npm_packument_url,
    package_index_url,
    source_host_release_url,
)
from eawf.workflow.release.reproducibility import (
    ArtifactDigest,
    ArtifactKind,
    BuildAttempt,
    ReproducibleBuildReceipt,
)
from eawf.workflow.release.target_machine import advance_target_attempt
from eawf.workflow.release.train import (
    DEV1_GATE_BINDINGS_YAML,
    DEV1_RELEASE_CONFIG_YAML,
    V07_TRAIN,
)
from eawf.workflow.release.vulnerability import VulnerabilityReport
from eawf.workflow.verify.release_readiness import compute_readiness

#: Instant every sweep in this package is computed at.
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

#: A pinned candidate's source commit, tree and manifest digest.
SOURCE_SHA = "a" * 40
TREE_SHA = "b" * 40
MANIFEST_DIGEST = f"sha256:{'c' * 64}"

#: Recorded registry answers and the frozen manifests they are judged
#: against. Committed rather than generated so a fixture drifting from
#: what an adapter reads shows up as a diff, not as a passing test.
#:
#: Each ``<stem>-<case>.json`` is a registry's raw answer (status and
#: decoded body), trimmed to the fields the readers and adapters read.
#: The ``-dev2`` bodies are the live ``0.7.0.dev2`` answers captured on
#: 2026-09-17 and ``manifest-dev2.json`` is the manifest that release
#: approved. The case bodies drive the ``0.7.0.dev1`` checkpoint the
#: suite walks: the npm packument is the live one, and the index and
#: release bodies are the live ones respelled onto ``0.7.0.dev1`` with
#: the digests ``manifest.json`` pins.
OBSERVATION_FIXTURES = Path(__file__).parent / "fixtures" / "release" / "observations"

#: The recorded-response stem of each adapter, by target id.
ADAPTER_STEMS: Mapping[str, str] = {
    "pypi": "package_index",
    "npm": "npm_registry",
    "github": "source_host_release",
}

#: The tarball bodies the recorded npm registry serves. The live
#: tarballs are not committed, so ``manifest.json`` pins the sha256 of
#: the frozen stand-in. The npm ``mismatch`` packument is the ``match``
#: one: a packument names its tarball but never its sha256, so only the
#: served bytes can differ -- which is why the reader has to hash them.
FROZEN_NPM_TARBALL = b"stand-in tarball: the frozen @elementarno/eawf 0.7.0-dev.1 build\n"
REPUBLISHED_NPM_TARBALL = b"stand-in tarball: a different @elementarno/eawf 0.7.0-dev.1 build\n"

#: The tarball each npm case serves; cases absent here fetch none.
NPM_TARBALLS: Mapping[str, bytes] = {
    "match": FROZEN_NPM_TARBALL,
    "default-channel": FROZEN_NPM_TARBALL,
    "mismatch": REPUBLISHED_NPM_TARBALL,
    "dev2": FROZEN_NPM_TARBALL,
}


def frozen_manifest() -> FrozenManifest:
    """Return the committed ``0.7.0.dev1`` frozen manifest."""
    return FrozenManifest.model_validate(
        json.loads((OBSERVATION_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    )


def registry_answer(target_id: str, case: str) -> tuple[int, Any]:
    """Return the recorded registry status and decoded body.

    Args:
        target_id: Publication target whose registry answered.
        case: Fixture case stem, e.g. ``match`` or ``default-channel``.

    Returns:
        The transport status and the decoded body (``None`` for none).
    """
    path = OBSERVATION_FIXTURES / f"{ADAPTER_STEMS[target_id]}-{case}.json"
    recorded = json.loads(path.read_text(encoding="utf-8"))
    return recorded["status"], recorded["payload"]


@dataclass
class RecordedRegistry:
    """An :class:`HttpOpener` answering from recorded bodies.

    A request for any URL it holds no answer for fails the test, so a
    reader cannot quietly ask for something nobody recorded.

    Attributes:
        answers: The reply served for each URL.
        requests: Every ``(url, headers)`` asked, in order.
    """

    answers: dict[str, HttpReply]
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def __call__(self, url: str, *, headers: Mapping[str, str]) -> HttpReply:
        """Return the recorded answer for *url*."""
        self.requests.append((url, dict(headers)))
        if url not in self.answers:
            raise AssertionError(
                f"unrecorded registry request {url!r}; recorded {sorted(self.answers)}"
            )
        return self.answers[url]


def recorded_registry(target_id: str, case: str, request: ObservationRequest) -> RecordedRegistry:
    """Return a registry serving the *case* answer to *request*'s reader.

    Args:
        target_id: Publication target whose registry is recorded.
        case: Fixture case stem.
        request: The read-back request the reader will be asked.

    Returns:
        The registry, also serving the npm tarball the case names.
    """
    urls = {"pypi": package_index_url, "npm": npm_packument_url, "github": source_host_release_url}
    status, payload = registry_answer(target_id, case)
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    registry = RecordedRegistry(answers={urls[target_id](request): HttpReply(status, body)})
    if target_id == "npm" and case in NPM_TARBALLS:
        dist = payload["versions"][semver_equivalent(request.version)]["dist"]
        registry.answers[dist["tarball"]] = HttpReply(200, NPM_TARBALLS[case])
    return registry


def registry_reader(target_id: str, opener: HttpOpener) -> RegistryReader:
    """Return the live reader for *target_id*'s adapter over *opener*."""
    readers: Mapping[
        str, type[PackageIndexReader] | type[NpmRegistryReader] | type[SourceHostReleaseReader]
    ] = {
        "pypi": PackageIndexReader,
        "npm": NpmRegistryReader,
        "github": SourceHostReleaseReader,
    }
    return readers[target_id](opener=opener)


def recorded_response(
    target_id: str,
    case: str,
    *,
    request: ObservationRequest | None = None,
) -> RecordedResponse:
    """Return the reader's answer when the registry serves *case*.

    The raw fixture is passed through the target's live reader, so the
    response carries the fields only a reader adds, exactly as the
    daemon's reader would record it.

    Args:
        target_id: Publication target whose adapter judges it.
        case: Fixture case stem, e.g. ``match`` or ``default-channel``.
        request: The read-back request; defaults to the dev1 request.

    Returns:
        The recorded answer.
    """
    asked = request or read_back_request(target_id)
    return registry_reader(target_id, recorded_registry(target_id, case, asked))(asked)


def read_back_request(target_id: str, *, config: ReleaseConfig | None = None) -> ObservationRequest:
    """Return the read-back request for one configured target."""
    return observation_request(config or dev1_config(), frozen_manifest(), target_id=target_id)


def dev1_config(**overrides: Any) -> ReleaseConfig:
    """Return the authored dev1 configuration with top-level *overrides*."""
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    body.update(overrides)
    return load_release_config({"release": body}, train=V07_TRAIN)


def dev1_binding_rows() -> list[dict[str, Any]]:
    """Return the authored dev1 binding rows as mutable dicts."""
    decoded: dict[str, Any] = yaml.safe_load(DEV1_GATE_BINDINGS_YAML)
    rows: list[dict[str, Any]] = copy.deepcopy(decoded["bindings"])
    return rows


def release_record(**overrides: Any) -> Release:
    """Return a pinned ``0.7.0.dev1`` candidate with *overrides* applied."""
    payload: dict[str, Any] = {
        "uid": UUID(int=29),
        "key": "REL-0.7.0.dev1",
        "version": "0.7.0.dev1",
        "channel": ReleaseChannel.DEV,
        "authority_epoch": 1,
        "status": ReleaseStatus.CANDIDATE,
        "source_sha": SOURCE_SHA,
        "source_tree_sha": TREE_SHA,
        "manifest_ref": "artifact://release/manifest",
        "manifest_digest": MANIFEST_DIGEST,
    }
    payload.update(overrides)
    return Release(**payload)


#: The four targets ``0.7.0.dev1`` reached without a release record, and
#: the state each was independently read back in. ``plugins-dist`` is
#: deliberately here and deliberately absent from the authored
#: configuration: an uncontrolled publication can reach somewhere the
#: checkpoint never declared, and a fixture that drops that row would
#: test a tidier incident than the one that happened.
ADOPTED_TARGET_DETAIL: Mapping[str, tuple[str, str]] = {
    "pypi": (
        "observed_mismatch",
        "holds the 2026-09-07 wheel and source distribution, built from a "
        "commit neither later tag target names; the filename cannot be reused",
    ),
    "npm": (
        "observed_success",
        "the next dist-tag resolves 0.7.0-dev.1 from the final tag target; latest is untouched",
    ),
    "github": (
        "observed_mismatch",
        "the source-host release exists with zero assets and prerelease false, "
        "targeting the default branch rather than the tagged commit",
    ),
    "plugins-dist": (
        "observed_mismatch",
        "the version directory holds the 2026-09-07 render and refused both "
        "later publications; no configured leg declares this target",
    ),
}

#: The reason the dev1 adoption carries. A terminal record has no later
#: transition that could explain it, so the explanation arrives here.
ADOPTION_REASON = (
    "0.7.0.dev1 was published to four targets with no release record open, and "
    "the tag was then moved twice over already-published artifacts, so the "
    "targets disagree on which commit the version denotes; the version is spent "
    "and is superseded by 0.7.0.dev2"
)

#: The incident the dev1 adoption disposes of.
ADOPTION_INCIDENT_REF = "INC-P32-01"

#: Identity the adoption fixtures mint for the dev1 record.
DEV1_DRAFT_UID = UUID(int=744)


def adopted_observation(target_id: str, **overrides: Any) -> AdoptedTargetObservation:
    """Return the recorded read-back of one out-of-band dev1 target.

    Args:
        target_id: One key of :data:`ADOPTED_TARGET_DETAIL`.
        **overrides: Field overrides applied to the built row.

    Returns:
        The observation row.
    """
    status, detail = ADOPTED_TARGET_DETAIL[target_id]
    payload: dict[str, Any] = {
        "target_id": target_id,
        "observed_status": status,
        "observed_at": NOW,
        "evidence_ref": f"observation://adopted/{target_id}/eawf@0.7.0.dev1",
        "detail": detail,
    }
    payload.update(overrides)
    return AdoptedTargetObservation(**payload)


def dev1_adoption(*, targets: Sequence[str] | None = None, **overrides: Any) -> ReleaseAdoption:
    """Return the dev1 adoption over *targets*, defaulting to all four.

    Args:
        targets: Target ids to observe. ``None`` observes every row of
            :data:`ADOPTED_TARGET_DETAIL`.
        **overrides: Field overrides applied to the built adoption.

    Returns:
        The adoption record.
    """
    chosen = tuple(ADOPTED_TARGET_DETAIL) if targets is None else tuple(targets)
    payload: dict[str, Any] = {
        "adopted_at": NOW,
        "reason": ADOPTION_REASON,
        "incident_ref": ADOPTION_INCIDENT_REF,
        "observations": tuple(adopted_observation(target) for target in chosen),
    }
    payload.update(overrides)
    return ReleaseAdoption(**payload)


def dev1_draft(uid: UUID | None = None) -> Release:
    """Return a fresh DRAFT dev1 record, exactly as the train opens it."""
    return draft_release_for(
        V07_TRAIN.checkpoint_for_version("0.7.0.dev1"), uid=uid or DEV1_DRAFT_UID
    )


def verifying_publication(
    *,
    operation_id: UUID,
    config: ReleaseConfig | None = None,
) -> tuple[Release, PublicationOperation, ReleaseConfig]:
    """Return a dev1 record at VERIFYING, every leg reported at its deadline.

    Args:
        operation_id: Identity of the publication episode opened.
        config: Checkpoint configuration; defaults to the dev1 one.

    Returns:
        The verifying record, its operation and the configuration.
    """
    checkpoint = config or dev1_config()
    published, operation = begin_publication(
        release_record(status=ReleaseStatus.APPROVED, approval_ref="receipt://approval/dev1"),
        checkpoint,
        compute_readiness(checkpoint, probes=all_passing(), computed_at=NOW),
        operation_id=operation_id,
        approved_manifest_digest=MANIFEST_DIGEST,
        idempotency_key=f"publish-0.7.0.dev1-{operation_id.int}",
        proof_digest=f"sha256:{'1' * 64}",
        opened_at=NOW,
    )
    for target in checkpoint.targets:
        row = require_attempt(operation, target.target_id)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
        operation = advance_target_attempt(
            operation,
            target=target,
            to=ReleaseTargetStatus.REPORTED_SUCCESS,
            now=row.deadline_at,
            effect_receipt_ref="receipt://target/effect",
        )
    return begin_verification(published, checkpoint, operation), operation, checkpoint


def fixed_probe(status: ReleaseSignalStatus) -> ReleaseSignalProbe:
    """Return a probe reporting *status* for whichever signal it is asked."""
    remediation = "" if status is ReleaseSignalStatus.PASS else f"repair the {status.value} row"

    def run(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        return ReleaseSignalOutcome(status=status, remediation=remediation)

    return run


def all_passing() -> dict[ReleaseSignalName, ReleaseSignalProbe]:
    """Return a probe registry in which every signal passes."""
    return dict.fromkeys(ReleaseSignalName, fixed_probe(ReleaseSignalStatus.PASS))


def stage_passing_receipts(repo_root: Path, *, version: str, source_sha: str) -> None:
    """Write the three receipts a clean CI run leaves under *repo_root*.

    The dependency manifest pins its own lock digest, so a checkout that
    carries no ``uv.lock`` agrees with it and the ``dependencies`` row
    passes on the receipts alone.

    Args:
        repo_root: Directory the receipts are written under.
        version: Version the built artifacts are named for.
        source_sha: The 40-hex commit both builds were made from.
    """
    lock_digest = f"sha256:{'e' * 64}"
    manifest = ReleaseDependencyManifest(
        lock_digest=lock_digest,
        packages=(
            LockedPackage(
                name="pydantic",
                version="2.12.0",
                license_id="MIT",
                disposition=LicenseDisposition.ALLOWED,
            ),
        ),
        imported_distributions=("pydantic",),
        locked_distributions=("pydantic",),
    )
    artifacts = (
        ArtifactDigest(
            filename=f"eawf-{version}-py3-none-any.whl", kind=ArtifactKind.WHEEL, sha256="a" * 64
        ),
        ArtifactDigest(filename=f"eawf-{version}.tar.gz", kind=ArtifactKind.SDIST, sha256="b" * 64),
    )
    epoch = 1757592000
    write_receipt(repo_root, "dependency-manifest", manifest)
    write_receipt(repo_root, "vulnerability-report", VulnerabilityReport(lock_digest=lock_digest))
    write_receipt(
        repo_root,
        "reproducible-build-receipt",
        ReproducibleBuildReceipt(
            source_sha=source_sha,
            source_date_epoch=epoch,
            attempts=(
                BuildAttempt(attempt=1, source_date_epoch=epoch, artifacts=artifacts),
                BuildAttempt(attempt=2, source_date_epoch=epoch, artifacts=artifacts),
            ),
            reproduced=True,
        ),
    )
