"""The ``0.7.0.dev1`` record reaches APPROVED on merged main, and is kept.

Two claims are pinned here.

**The record exists.** Before this, ``release.create`` and
``release.approve`` returned a record and forgot it: the checkpoint
lived in one RPC reply and nowhere else, so nothing downstream could ask
whether dev1 had been cut. Both verbs now append to the release-record
collection, and the assertion is a *read back* -- create, approve, then
load the collection from disk and find the APPROVED record there. A
round-trip through the reply value would prove nothing about
persistence.

**The sweep that admitted it was green.** The approval guard reads the
derived required set, so the six required dev1 rows have to pass for
real: ancestry against a ``main`` the source is already an ancestor of,
version and changelog out of the tree, and artifacts and dependencies
out of the receipts the pipeline writes. Those last two have no
working-copy producer, which is why the fixture stages the receipts a
tagged CI run would have uploaded rather than stubbing the probes: the
row is then settled by the code that reads receipts, not by the test.

The fixture repository is deliberately a real git checkout rather than a
mocked one. Ancestry and tree-cleanliness are questions about a
repository, and a fake that answers them cannot fail the way a
repository does.

``credentials`` is absent from the required set and must stay absent:
every dev1 target authenticates by OIDC or the ambient workflow token,
so none declares a ``credential_handle`` for the row to assert. The set
is pinned by exact equality so re-adding a handle cannot quietly widen
the gate.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

from eawf import __version__
from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.kernel.spec.release import Release, ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import approve, create, show
from eawf.surfaces.cli.app import app
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.release.advance import draft_release_for
from eawf.workflow.release.dependencies import (
    LicenseDisposition,
    LockedPackage,
    ReleaseDependencyManifest,
    compute_lock_digest,
)
from eawf.workflow.release.lifecycle import advance_release
from eawf.workflow.release.producers import build_receipt_probes
from eawf.workflow.release.publication import begin_publication
from eawf.workflow.release.receipts import write_receipt
from eawf.workflow.release.records import (
    read_release_record,
    read_release_records,
    record_envelope_id,
    record_release,
    release_records_path,
)
from eawf.workflow.release.reproducibility import (
    ArtifactDigest,
    ArtifactKind,
    BuildAttempt,
    ReproducibleBuildReceipt,
)
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.release.vulnerability import VulnerabilityReport
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import ReleaseReadiness, compute_readiness

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

DEV1_VERSION = "0.7.0.dev1"
DEV1_KEY = f"REL-{DEV1_VERSION}"
DEV1_TAG = f"v{DEV1_VERSION}"
PUBLISHING_REMOTE = "origin"

MANIFEST_REF = "artifact://release/manifest/0.7.0.dev1"
MANIFEST_DIGEST = f"sha256:{'c' * 64}"
APPROVAL_REF = "receipt://approval/rel-0.7.0.dev1"

#: Identity minted for the record the RPC opens under test.
DEV1_UID = UUID(int=24)

#: The six rows the dev1 configuration derives as required. Pinned by
#: exact equality, not membership: the point of the row is as much what
#: it excludes (``credentials``, which no dev1 target can answer) as
#: what it demands.
REQUIRED_DEV1_SIGNALS = (
    ReleaseSignalName.VERSION_CONSISTENCY,
    ReleaseSignalName.CHANGELOG,
    ReleaseSignalName.ANCESTRY,
    ReleaseSignalName.TREE_CLEANLINESS,
    ReleaseSignalName.ARTIFACTS,
    ReleaseSignalName.DEPENDENCIES,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

#: A lock body the fixture digests. Its contents are irrelevant; that
#: the manifest names *this* lock's digest is the whole check.
LOCK_TEXT = "version = 1\nrequires-python = '>=3.14'\n"

CHANGELOG_TEXT = f"""# Changelog

## [{DEV1_VERSION}] - 2026-09-08

- Cut the first epoch-1 development checkpoint of the 0.7 train.
- No migration is required: the state schema is unchanged.
"""

GITIGNORE_TEXT = "dist/\n"


def dev1_config() -> ReleaseConfig:
    """Return the authored dev1 checkpoint configuration."""
    return load_release_config(checkpoint_config_yaml(DEV1_VERSION), train=V07_TRAIN)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command inside *repo*, refusing on failure."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _dependency_manifest(lock_digest: str) -> ReleaseDependencyManifest:
    """Return an inventory the gate passes, pinned to *lock_digest*."""
    return ReleaseDependencyManifest(
        lock_digest=lock_digest,
        packages=(
            LockedPackage(
                name="pydantic",
                version="2.12.0",
                license_id="MIT",
                disposition=LicenseDisposition.ALLOWED,
            ),
            LockedPackage(
                name="typer",
                version="0.19.0",
                license_id="MIT",
                disposition=LicenseDisposition.ALLOWED,
            ),
        ),
        imported_distributions=("pydantic", "typer"),
        locked_distributions=("pydantic", "typer"),
    )


def _build_receipt(source_sha: str) -> ReproducibleBuildReceipt:
    """Return a double-build receipt whose two attempts agree."""
    artifacts = (
        ArtifactDigest(
            filename="eawf-0.7.0.dev1-py3-none-any.whl", kind=ArtifactKind.WHEEL, sha256="a" * 64
        ),
        ArtifactDigest(filename="eawf-0.7.0.dev1.tar.gz", kind=ArtifactKind.SDIST, sha256="b" * 64),
    )
    return ReproducibleBuildReceipt(
        source_sha=source_sha,
        source_date_epoch=1757332800,
        attempts=(
            BuildAttempt(attempt=1, source_date_epoch=1757332800, artifacts=artifacts),
            BuildAttempt(attempt=2, source_date_epoch=1757332800, artifacts=artifacts),
        ),
        reproduced=True,
    )


@pytest.fixture
def merged_main(tmp_path: Path) -> Path:
    """Return a checkout standing exactly where the merged phase leaves it.

    HEAD is an ancestor of ``origin/main``, the tree is clean, the
    version module and changelog agree with the checkpoint, and the
    three pipeline receipts are staged where the probes read them.
    """
    repo = tmp_path / "merged"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "EAWF Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "commit.gpgSign", "false")
    _git(repo, "config", "core.hooksPath", ".git/hooks")
    (repo / ".gitignore").write_text(GITIGNORE_TEXT, encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(CHANGELOG_TEXT, encoding="utf-8")
    (repo / "uv.lock").write_text(LOCK_TEXT, encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", "feat: cut the dev1 checkpoint")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # The merge is what makes the source publishable, and ancestry is
    # the row that reads it. Writing the remote-tracking ref directly
    # models a fetched `origin/main` without needing a second checkout.
    _git(repo, "update-ref", f"refs/remotes/{PUBLISHING_REMOTE}/main", head)

    lock_digest = compute_lock_digest(LOCK_TEXT)
    write_receipt(repo, "dependency-manifest", _dependency_manifest(lock_digest))
    write_receipt(repo, "vulnerability-report", VulnerabilityReport(lock_digest=lock_digest))
    write_receipt(repo, "reproducible-build-receipt", _build_receipt(head))
    return repo


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a state root the release verbs may record into."""
    path = tmp_path / ".ea" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(path, load_state(_EMPTY_STATE))
    return path


@pytest.fixture
def ctx(state_path: Path) -> MethodContext:
    """Return a method context bound to the recording state root."""
    return MethodContext(
        started_at="2026-09-08T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version=DEV1_VERSION,
        state_path=state_path,
    )


def head_sha(repo: Path) -> str:
    """Return the commit the merged checkout stands on."""
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def green_sweep(repo: Path) -> ReleaseReadiness:
    """Return the dev1 sweep computed against the merged checkout.

    Every probe is bound to *repo* explicitly rather than inherited from
    the process working directory, so the sweep the assertions read is
    the sweep of the fixture and of nothing else.
    """
    probes = {
        **build_tag_probes(
            TagPreflightInputs(
                repo_root=repo,
                version=DEV1_VERSION,
                tag=DEV1_TAG,
                package_version=__version__,
                remote=PUBLISHING_REMOTE,
            )
        ),
        **build_receipt_probes(repo),
    }
    return compute_readiness(
        dev1_config(), probes=probes, observed_revision=head_sha(repo), computed_at=NOW
    )


def pinned_candidate(repo: Path) -> Release:
    """Return the dev1 DRAFT record advanced to a CANDIDATE pinned on *repo*."""
    draft = draft_release_for(V07_TRAIN.checkpoint_for_version(DEV1_VERSION), uid=DEV1_UID)
    return advance_release(
        draft,
        ReleaseStatus.CANDIDATE,
        source_sha=head_sha(repo),
        source_tree_sha=_git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(),
        manifest_ref=MANIFEST_REF,
        manifest_digest=MANIFEST_DIGEST,
    )


def approve_via_rpc(ctx: MethodContext, repo: Path) -> dict[str, Any]:
    """Approve the pinned candidate through the operator-facing verb."""
    return asyncio.run(
        approve(
            ctx,
            {
                "release": pinned_candidate(repo).model_dump(mode="json"),
                "readiness": green_sweep(repo).model_dump(mode="json"),
                "approval_ref": APPROVAL_REF,
            },
        )
    )


# --- the record exists ------------------------------------------------


def test_create_persists_the_dev1_draft_under_the_release_record_kind(
    ctx: MethodContext, state_path: Path
) -> None:
    """Opening the checkpoint writes a row a later reader can find."""
    result = asyncio.run(create(ctx, {"version": DEV1_VERSION}))

    assert result["release"]["key"] == DEV1_KEY
    assert result["release"]["status"] == ReleaseStatus.DRAFT.value
    assert release_records_path(state_path).name == "release_record.jsonl"
    persisted = read_release_record(state_path, DEV1_KEY)
    assert persisted is not None
    assert persisted.status is ReleaseStatus.DRAFT
    assert result["release_record_id"] == record_envelope_id(persisted)


def test_approve_persists_the_dev1_record_at_approved(
    ctx: MethodContext, state_path: Path, merged_main: Path
) -> None:
    """The approval survives the call that made it."""
    asyncio.run(create(ctx, {"version": DEV1_VERSION}))

    result = approve_via_rpc(ctx, merged_main)

    assert result["release"]["status"] == ReleaseStatus.APPROVED.value
    persisted = read_release_record(state_path, DEV1_KEY)
    assert persisted is not None
    assert persisted.status is ReleaseStatus.APPROVED
    assert persisted.approval_ref == APPROVAL_REF
    assert persisted.source_sha == head_sha(merged_main)


def test_approve_leaves_state_validating_beside_the_releases_collection(
    ctx: MethodContext, state_path: Path, merged_main: Path
) -> None:
    """The collection is a store, so state.json still loads unchanged."""
    before = load_state(state_path)
    asyncio.run(create(ctx, {"version": DEV1_VERSION}))
    approve_via_rpc(ctx, merged_main)

    assert load_state(state_path) == before
    collection = read_release_records(state_path)
    assert set(collection) == {DEV1_KEY}
    assert collection[DEV1_KEY].status is ReleaseStatus.APPROVED


def test_show_reports_the_recorded_checkpoint_after_the_approval(
    ctx: MethodContext, merged_main: Path
) -> None:
    """The operator asking where dev1 stands is answered by the collection."""
    assert asyncio.run(show(ctx, {"version": DEV1_VERSION}))["record"] is None
    asyncio.run(create(ctx, {"version": DEV1_VERSION}))
    approve_via_rpc(ctx, merged_main)

    result = asyncio.run(show(ctx, {"version": DEV1_VERSION}))

    assert result["record"]["key"] == DEV1_KEY
    assert result["record"]["status"] == ReleaseStatus.APPROVED.value


def test_show_reports_no_record_without_a_state_root() -> None:
    """Describing the ladder stays possible where nothing is recorded."""
    rootless = MethodContext(
        started_at="2026-09-08T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version=DEV1_VERSION,
    )

    assert asyncio.run(show(rootless, {"version": DEV1_VERSION}))["record"] is None


def test_show_refuses_a_corrupt_release_record_collection(
    ctx: MethodContext, state_path: Path
) -> None:
    """A row that will not parse must not read as a checkpoint never cut."""
    path = release_records_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"not": "an envelope"}\n', encoding="utf-8")

    with pytest.raises(DaemonValidationError, match="is not an envelope"):
        asyncio.run(show(ctx, {"version": DEV1_VERSION}))


def test_approve_binds_a_manifest_digest_the_bake_reuses_as_its_proof_digest(
    ctx: MethodContext, state_path: Path, merged_main: Path
) -> None:
    """The post-merge bake never has to re-pin what the approval bound."""
    asyncio.run(create(ctx, {"version": DEV1_VERSION}))
    approve_via_rpc(ctx, merged_main)

    persisted = read_release_record(state_path, DEV1_KEY)
    assert persisted is not None
    assert persisted.manifest_digest == MANIFEST_DIGEST
    _published, operation = begin_publication(
        persisted,
        dev1_config(),
        green_sweep(merged_main),
        operation_id=UUID(int=240),
        approved_manifest_digest=persisted.manifest_digest or "",
        idempotency_key="rel-0.7.0.dev1-bake-1",
        proof_digest=persisted.manifest_digest or "",
        opened_at=NOW,
    )
    assert operation.proof_digest == MANIFEST_DIGEST


def test_approve_keeps_the_draft_row_beside_the_approved_one(
    ctx: MethodContext, state_path: Path, merged_main: Path
) -> None:
    """The collection is append-only, so the lineage stays readable."""
    asyncio.run(create(ctx, {"version": DEV1_VERSION}))
    approve_via_rpc(ctx, merged_main)

    rows = release_records_path(state_path).read_text(encoding="utf-8").splitlines()
    statuses = [json.loads(row)["payload"]["status"] for row in rows if row.strip()]
    assert statuses == [ReleaseStatus.DRAFT.value, ReleaseStatus.APPROVED.value]


def test_approve_refuses_without_an_on_disk_state_root(merged_main: Path) -> None:
    """An approval nobody could record is refused rather than returned."""
    rootless = MethodContext(
        started_at="2026-09-08T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version=DEV1_VERSION,
    )

    with pytest.raises(DaemonValidationError, match="on-disk state root"):
        approve_via_rpc(rootless, merged_main)


# --- the sweep that admitted it ---------------------------------------


def test_preflight_sweep_green_passes_every_required_dev1_signal(merged_main: Path) -> None:
    """The six derived rows all pass on the merged source."""
    sweep = green_sweep(merged_main)

    assert tuple(sweep.required_signals) == REQUIRED_DEV1_SIGNALS
    assert ReleaseSignalName.CREDENTIALS not in set(sweep.required_signals)
    for signal in REQUIRED_DEV1_SIGNALS:
        assert sweep.row(signal).status is ReleaseSignalStatus.PASS, signal
    assert sweep.ready is True


def test_preflight_sweep_green_leaves_no_required_signal_unavailable(
    merged_main: Path,
) -> None:
    """Artifacts and dependencies are settled by receipts, not by a stub."""
    sweep = green_sweep(merged_main)

    unavailable = {
        row.signal
        for row in sweep.signals
        if row.signal in set(sweep.required_signals)
        and row.status is ReleaseSignalStatus.UNAVAILABLE
    }
    assert unavailable == set()
    assert sweep.first_red is None


def test_preflight_cli_sweep_green_exits_zero_on_the_merged_source(
    merged_main: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verb the operator runs agrees with the sweep, and exits 0."""
    monkeypatch.chdir(merged_main)

    result = CliRunner().invoke(app, ["release", "preflight", DEV1_VERSION])

    assert result.exit_code == 0, result.output
    assert f"{DEV1_KEY}  profile=dev1  ready=True" in result.output
    assert "first red:" not in result.output


def test_preflight_cli_sweep_green_reports_pass_for_each_required_row(
    merged_main: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ``REQ`` row the operator reads says ``pass``."""
    monkeypatch.chdir(merged_main)

    result = CliRunner().invoke(app, ["--json", "release", "preflight", DEV1_VERSION])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    required = set(payload["required_signals"])
    assert required == {signal.value for signal in REQUIRED_DEV1_SIGNALS}
    reported = {row["signal"]: row["status"] for row in payload["signals"]}
    assert {reported[name] for name in required} == {ReleaseSignalStatus.PASS.value}


def test_preflight_cli_exits_non_zero_when_the_receipts_are_absent(
    merged_main: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The green exit is earned: removing one receipt reds the same verb."""
    (merged_main / "dist" / "release-receipts" / "reproducible-build-receipt.json").unlink()
    monkeypatch.chdir(merged_main)

    result = CliRunner().invoke(app, ["release", "preflight", DEV1_VERSION])

    assert result.exit_code != 0
    assert "ready=False" in result.output


# --- the collection's own boundaries ----------------------------------


def test_read_release_records_returns_empty_before_anything_is_recorded(
    state_path: Path,
) -> None:
    """An absent collection is empty, not an error."""
    assert read_release_records(state_path) == {}
    assert read_release_record(state_path, DEV1_KEY) is None


def test_read_release_records_returns_the_highest_revision_of_one_key(
    state_path: Path, merged_main: Path
) -> None:
    """Two rows for one release read back as the later one."""
    draft = draft_release_for(V07_TRAIN.checkpoint_for_version(DEV1_VERSION), uid=DEV1_UID)
    candidate = pinned_candidate(merged_main)
    record_release(state_path, candidate, recorded_at=NOW, summary="candidate")
    record_release(state_path, draft, recorded_at=NOW, summary="draft")

    current = read_release_record(state_path, DEV1_KEY)

    assert current is not None
    assert current.revision == candidate.revision
    assert current.status is ReleaseStatus.CANDIDATE


def test_record_release_rejects_a_naive_instant(state_path: Path) -> None:
    """A row whose instant carries no zone cannot be ordered."""
    draft = draft_release_for(V07_TRAIN.checkpoint_for_version(DEV1_VERSION), uid=DEV1_UID)

    with pytest.raises(ValueError, match="recorded_at must be timezone-aware"):
        record_release(state_path, draft, recorded_at=datetime(2026, 9, 8, 12, 0), summary="x")


def test_record_release_rejects_a_payload_that_is_not_a_release(state_path: Path) -> None:
    """The collection stores release records and refuses anything else."""
    with pytest.raises(TypeError, match="release must be Release"):
        record_release(state_path, {"key": DEV1_KEY}, recorded_at=NOW, summary="x")  # type: ignore[arg-type]


def test_record_release_truncates_an_overlong_summary(state_path: Path) -> None:
    """A 501-character summary is clipped rather than rejecting the append."""
    draft = draft_release_for(V07_TRAIN.checkpoint_for_version(DEV1_VERSION), uid=DEV1_UID)

    record_release(state_path, draft, recorded_at=NOW, summary="s" * 501)

    row = json.loads(release_records_path(state_path).read_text(encoding="utf-8").splitlines()[0])
    assert len(row["summary"]) == 500


def test_read_release_records_refuses_a_line_that_is_not_an_envelope(
    state_path: Path,
) -> None:
    """A corrupt line is a refusal; skipping it would lose an approval."""
    path = release_records_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"not": "an envelope"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 1 is not an envelope"):
        read_release_records(state_path)


def test_read_release_records_refuses_a_payload_that_is_not_a_release(
    state_path: Path,
) -> None:
    """An envelope of the right kind carrying the wrong body still refuses."""
    append_envelope(
        release_records_path(state_path),
        Envelope(
            id="REL-0.7.0.dev1@0",
            kind=StoreKind.RELEASE_RECORD,
            scope_id=DEV1_KEY,
            created_at=NOW,
            summary="corrupt",
            payload={"key": DEV1_KEY},
        ),
    )

    with pytest.raises(ValueError, match="payload is not a release"):
        read_release_records(state_path)


def test_read_release_records_refuses_a_row_filed_under_another_kind(
    state_path: Path,
) -> None:
    """Kind drift inside the collection is a refusal, not a silent read."""
    draft = draft_release_for(V07_TRAIN.checkpoint_for_version(DEV1_VERSION), uid=DEV1_UID)
    append_envelope(
        release_records_path(state_path),
        Envelope(
            id="REL-0.7.0.dev1@0",
            kind=StoreKind.RELEASE,
            scope_id=DEV1_KEY,
            created_at=NOW,
            summary="misfiled",
            payload=draft.model_dump(mode="json"),
        ),
    )

    with pytest.raises(ValueError, match="is filed under 'release'"):
        read_release_records(state_path)
