"""Opening ``REL-0.7.0.dev2`` over the burned dev1 record, and approving it.

Two claims are pinned here, one per selector.

**approve.** The dev2 checkpoint opens as its own DRAFT record -- a
separate uid, key and revision line, not a copy of dev1 -- and it opens
*only* over a predecessor that has stopped moving. The negative half is
the load-bearing one: with dev1 recorded at a live status, or with no
dev1 record at all, ``release.create`` refuses by name and writes
nothing, so the successor can never be opened alongside a checkpoint that
can still publish. Once open, the record is pinned and approved against a
real dev2 sweep over a real checkout, so the seven rows the twelve-gate
profile derives as required have to pass for real.

**waiver.** The dev2 profile is the first to bind ``waiver_count``, and
it binds it to the waiver block rather than to any of the twelve signal
rows. Zero counted waivers report a passing gate; a count with no
explanation reds it and blocks the approval; an explained waiver reds it
until an acknowledgement names the protection being accepted as lost.
That gate is what makes the other eleven honest -- a checkpoint can reach
green by proving its gates or by waiving them, and only this row says
which happened.

The checkout is a real git repository carrying this project's own
committed rehearsal records and changelog, because ``migration``,
``changelog``, ``ancestry`` and ``tree_cleanliness`` are questions about a
repository and a fake that answers them cannot fail the way a repository
does. ``package_version`` is pinned to the dev2 literal rather than read
from the running package: this module is about the checkpoint, and the
neighbouring ``tests/integration/runtime/release/test_tag_chokepoint.py``
is the module that pins the version module's own value.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from eawf.kernel.release.waiver import ReleaseWaiver, WaiverDisposition
from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseStatus,
)
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName, load_release_config
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import approve, create
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.evidence.measured_contract import (
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
)
from eawf.workflow.release.admission import required_contract_ids
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.dependencies import (
    LicenseDisposition,
    LockedPackage,
    ReleaseDependencyManifest,
    compute_lock_digest,
)
from eawf.workflow.release.lifecycle import advance_release
from eawf.workflow.release.pipeline_receipts import write_receipt
from eawf.workflow.release.publication import burn_release
from eawf.workflow.release.records import read_release_record, record_release
from eawf.workflow.release.reproducibility import (
    ArtifactDigest,
    ArtifactKind,
    BuildAttempt,
    ReproducibleBuildReceipt,
)
from eawf.workflow.release.signal_probes import build_receipt_probes
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.release.vulnerability import VulnerabilityReport
from eawf.workflow.verify.checkpoint_succession import (
    SUCCEEDABLE_STATUSES,
    CheckpointSuccessionError,
    SuccessionDenialCode,
    assert_predecessor_terminal,
    predecessor_rung,
)
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import (
    ReleaseReadiness,
    WaiverAcknowledgement,
    compute_readiness,
)
from tests._release_helpers import dev1_adoption, dev1_config, dev1_draft

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

DEV1_VERSION = "0.7.0.dev1"
DEV1_KEY = f"REL-{DEV1_VERSION}"
DEV2_VERSION = "0.7.0.dev2"
DEV2_KEY = f"REL-{DEV2_VERSION}"
DEV2_TAG = f"v{DEV2_VERSION}"
PUBLISHING_REMOTE = "origin"
SCOPE = "P32-I01"

MANIFEST_REF = "artifact://release/manifest/0.7.0.dev2"
MANIFEST_DIGEST = f"sha256:{'d' * 64}"
APPROVAL_REF = "receipt://approval/rel-0.7.0.dev2"

#: Identity minted for the dev2 record the fixture pins directly.
DEV2_UID = UUID(int=25)

#: The seven rows the dev2 configuration derives as required. Pinned by
#: exact equality: dev2 adds ``migration`` to the dev1 six, and the row
#: it must keep excluding is ``credentials``, which no target on this
#: train can answer.
REQUIRED_DEV2_SIGNALS = (
    "version_consistency",
    "changelog",
    "ancestry",
    "tree_cleanliness",
    "migration",
    "artifacts",
    "dependencies",
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

#: Paths copied verbatim out of this checkout into the fixture, because
#: the probes read them as committed facts rather than as parameters.
_COMMITTED_INPUTS = (
    Path("CHANGELOG.md"),
    Path("pyproject.toml"),
    Path("tests/golden/kernel/migration/rehearsal"),
)

LOCK_TEXT = "version = 1\nrequires-python = '>=3.14'\n"
GITIGNORE_TEXT = "dist/\n"

#: The waiver the red-path assertions count. Explained, so it separates
#: "counted with no explanation" from "explained but unacknowledged".
EXPLAINED_WAIVER = ReleaseWaiver(
    scope="P32-I01-W21",
    reason="the front-door install smoke needs a published distribution to install",
    protected_principal="a newcomer's first contact with the release is exercised before the tag",
)

UNEXPLAINED_WAIVER = ReleaseWaiver(scope="P32-I01-W21", reason="", protected_principal="")


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


def dev2_config() -> ReleaseConfig:
    """Return the rendered dev2 checkpoint configuration."""
    return load_release_config(checkpoint_config_yaml(DEV2_VERSION), train=V07_TRAIN)


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
            filename=f"eawf-{DEV2_VERSION}-py3-none-any.whl",
            kind=ArtifactKind.WHEEL,
            sha256="a" * 64,
        ),
        ArtifactDigest(
            filename=f"eawf-{DEV2_VERSION}.tar.gz", kind=ArtifactKind.SDIST, sha256="b" * 64
        ),
    )
    return ReproducibleBuildReceipt(
        source_sha=source_sha,
        source_date_epoch=1757592000,
        attempts=(
            BuildAttempt(attempt=1, source_date_epoch=1757592000, artifacts=artifacts),
            BuildAttempt(attempt=2, source_date_epoch=1757592000, artifacts=artifacts),
        ),
        reproduced=True,
    )


@pytest.fixture
def cut_tree(tmp_path: Path) -> Path:
    """Return a checkout standing where the dev2 cut leaves the merged branch.

    HEAD is an ancestor of ``origin/main``, the tree is clean, and the
    changelog and the ten committed rehearsal records are this project's
    own -- so ``changelog`` and ``migration`` are settled by the files
    the release actually ships, not by a stand-in.
    """
    repo = tmp_path / "cut"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "EAWF Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "commit.gpgSign", "false")
    _git(repo, "config", "core.hooksPath", ".git/hooks")
    (repo / ".gitignore").write_text(GITIGNORE_TEXT, encoding="utf-8")
    (repo / "uv.lock").write_text(LOCK_TEXT, encoding="utf-8")
    for relative in _COMMITTED_INPUTS:
        source = _REPO_ROOT / relative
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", "feat: cut the dev2 checkpoint")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", f"refs/remotes/{PUBLISHING_REMOTE}/main", head)

    lock_digest = compute_lock_digest(LOCK_TEXT)
    write_receipt(repo, "dependency-manifest", _dependency_manifest(lock_digest))
    write_receipt(repo, "vulnerability-report", VulnerabilityReport(lock_digest=lock_digest))
    write_receipt(repo, "reproducible-build-receipt", _build_receipt(head))
    return repo


def _admitting_state() -> State:
    """Return the fixture state with every dev2 measured contract promoted."""
    state = load_state(_EMPTY_STATE)
    for contract_id in required_contract_ids(DEV2_VERSION):
        promote_measured_contract(
            state,
            contract=PREFLIGHT_CONTRACTS[contract_id],
            scope_id=SCOPE,
            required_band=PREFLIGHT_CHECKPOINT_BANDS[contract_id],
        )
    return state


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a state root dev2 may be admitted and recorded against."""
    path = tmp_path / ".ea" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(path, _admitting_state())
    return path


@pytest.fixture
def ctx(state_path: Path) -> MethodContext:
    """Return a method context bound to the recording state root."""
    return MethodContext(
        started_at="2026-09-11T00:00:00+00:00",
        pid=7821,
        protocol_version="1",
        version=DEV2_VERSION,
        state_path=state_path,
    )


def burned_dev1() -> Release:
    """Return the dev1 record as the burn leaves it: adopted, terminal."""
    adopted = adopt_publication(dev1_draft(), dev1_config(), adoption=dev1_adoption())
    burned, operation = burn_release(adopted, dev1_config(), None)
    assert operation is None
    return burned


@pytest.fixture
def terminal_dev1(state_path: Path) -> Release:
    """Record the burned dev1 record, which is what dev2 succeeds."""
    record = burned_dev1()
    record_release(state_path, record, recorded_at=NOW, summary=f"burn {record.key}")
    return record


def head_sha(repo: Path) -> str:
    """Return the commit the cut checkout stands on."""
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def dev2_sweep(
    repo: Path,
    *,
    waiver_count: int = 0,
    waivers: tuple[ReleaseWaiver, ...] = (),
    acknowledgements: tuple[WaiverAcknowledgement, ...] = (),
) -> ReleaseReadiness:
    """Return the dev2 sweep computed against *repo*.

    Every probe is bound to *repo* explicitly rather than inherited from
    the process working directory, so the sweep the assertions read is
    the sweep of the fixture and of nothing else.
    """
    probes = {
        **build_tag_probes(
            TagPreflightInputs(
                repo_root=repo,
                version=DEV2_VERSION,
                tag=DEV2_TAG,
                package_version=DEV2_VERSION,
                remote=PUBLISHING_REMOTE,
            )
        ),
        **build_receipt_probes(repo),
    }
    return compute_readiness(
        dev2_config(),
        probes=probes,
        observed_revision=head_sha(repo),
        computed_at=NOW,
        waiver_count=waiver_count,
        waivers=waivers,
        acknowledgements=acknowledgements,
    )


def open_dev2(ctx: MethodContext) -> dict[str, Any]:
    """Open the dev2 checkpoint through the operator-facing verb."""
    return asyncio.run(create(ctx, {"version": DEV2_VERSION}))


def pinned_dev2(record: Release, repo: Path) -> Release:
    """Return *record* advanced to a CANDIDATE pinned on *repo*."""
    return advance_release(
        record,
        ReleaseStatus.CANDIDATE,
        source_sha=head_sha(repo),
        source_tree_sha=_git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(),
        manifest_ref=MANIFEST_REF,
        manifest_digest=MANIFEST_DIGEST,
        supersedes_release_ref=DEV1_KEY,
    )


def approve_dev2(
    ctx: MethodContext, candidate: Release, readiness: ReleaseReadiness
) -> dict[str, Any]:
    """Approve *candidate* against *readiness* through the operator verb."""
    return asyncio.run(
        approve(
            ctx,
            {
                "release": candidate.model_dump(mode="json"),
                "readiness": readiness.model_dump(mode="json"),
                "approval_ref": APPROVAL_REF,
            },
        )
    )


def walk_to_approved(ctx: MethodContext, repo: Path) -> Release:
    """Open, pin and approve dev2, returning the approved record."""
    opened = Release.model_validate(open_dev2(ctx)["release"])
    result = approve_dev2(ctx, pinned_dev2(opened, repo), dev2_sweep(repo))
    return Release.model_validate(result["release"])


# --- approve: the record opens, and only over a finished dev1 ----------


def test_dev2_opens_as_a_distinct_draft_before_it_is_approved(
    ctx: MethodContext, terminal_dev1: Release
) -> None:
    """The successor is a new object, not a continuation of dev1."""
    result = open_dev2(ctx)

    opened = Release.model_validate(result["release"])
    assert opened.key == DEV2_KEY
    assert opened.status is ReleaseStatus.DRAFT
    assert opened.revision == 0
    assert opened.uid != terminal_dev1.uid
    assert opened.key != terminal_dev1.key
    assert result["supersedes_release_ref"] == DEV1_KEY


def test_dev2_draft_carries_dev_epoch_one_and_no_membership_when_approved(
    ctx: MethodContext, cut_tree: Path, terminal_dev1: Release
) -> None:
    """The three rung facts survive the whole walk to the approval."""
    approved = walk_to_approved(ctx, cut_tree)

    assert approved.channel is ReleaseChannel.DEV
    assert approved.authority_epoch == 1
    assert approved.membership_refs == ()


def test_dev2_is_not_opened_or_approved_while_dev1_is_still_live(
    ctx: MethodContext, state_path: Path
) -> None:
    """A dev1 that can still move blocks its successor by name."""
    live = dev1_draft()
    record_release(state_path, live, recorded_at=NOW, summary=f"draft {live.key}")

    with pytest.raises(DaemonValidationError) as excinfo:
        open_dev2(ctx)

    assert SuccessionDenialCode.PREDECESSOR_LIVE.value in str(excinfo.value)
    assert ReleaseStatus.DRAFT.value in str(excinfo.value)
    assert read_release_record(state_path, DEV2_KEY) is None


def test_dev2_is_not_opened_or_approved_without_any_dev1_record(
    ctx: MethodContext, state_path: Path
) -> None:
    """An unrecorded predecessor is a refusal, not a permitted open."""
    with pytest.raises(DaemonValidationError) as excinfo:
        open_dev2(ctx)

    assert SuccessionDenialCode.PREDECESSOR_UNRECORDED.value in str(excinfo.value)
    assert DEV1_KEY in str(excinfo.value)
    assert read_release_record(state_path, DEV2_KEY) is None


def test_dev2_reaches_approved_over_a_green_dev2_profile(
    ctx: MethodContext, state_path: Path, cut_tree: Path, terminal_dev1: Release
) -> None:
    """The approval survives the call that made it, at the dev2 profile."""
    sweep = dev2_sweep(cut_tree)
    assert sweep.ready is True

    approved = walk_to_approved(ctx, cut_tree)

    assert approved.status is ReleaseStatus.APPROVED
    assert approved.approval_ref == APPROVAL_REF
    assert approved.source_sha == head_sha(cut_tree)
    persisted = read_release_record(state_path, DEV2_KEY)
    assert persisted is not None
    assert persisted.status is ReleaseStatus.APPROVED


def test_dev2_approved_record_supersedes_the_burned_dev1_record(
    ctx: MethodContext, cut_tree: Path, terminal_dev1: Release
) -> None:
    """The correction lineage names the version it replaces."""
    approved = walk_to_approved(ctx, cut_tree)

    assert approved.supersedes_release_ref == terminal_dev1.key
    assert terminal_dev1.status is ReleaseStatus.PARTIALLY_RELEASED
    assert terminal_dev1.approval_ref is None
    assert terminal_dev1.adoption is not None


def test_dev2_sweep_derives_seven_required_rows_and_passes_each_before_approve(
    cut_tree: Path,
) -> None:
    """The twelve-gate profile requires migration on top of the dev1 six."""
    sweep = dev2_sweep(cut_tree)

    assert tuple(row.value for row in sweep.required_signals) == REQUIRED_DEV2_SIGNALS
    assert sweep.first_red is None
    assert sweep.first_red_gate is None


def test_dev2_approve_is_refused_when_the_sweep_is_not_green(
    ctx: MethodContext, state_path: Path, cut_tree: Path, terminal_dev1: Release
) -> None:
    """A red required row denies the approval and records nothing new."""
    (cut_tree / "dist" / "release-receipts" / "reproducible-build-receipt.json").unlink()
    opened = Release.model_validate(open_dev2(ctx)["release"])

    with pytest.raises(DaemonValidationError) as excinfo:
        approve_dev2(ctx, pinned_dev2(opened, cut_tree), dev2_sweep(cut_tree))

    assert "release_not_ready" in str(excinfo.value)
    persisted = read_release_record(state_path, DEV2_KEY)
    assert persisted is not None
    assert persisted.status is ReleaseStatus.DRAFT


# --- waiver: REL-025, the gate that says which way green was reached ---


def test_dev2_readiness_reports_waiver_count_zero_on_a_clean_sweep(cut_tree: Path) -> None:
    """Nothing counted, nothing to explain, and the gate passes."""
    sweep = dev2_sweep(cut_tree)

    row = sweep.gate_row(ReleaseGateName.WAIVER_COUNT)
    assert sweep.waiver_count == 0
    assert sweep.waiver_disposition is WaiverDisposition.NONE
    assert row.status.value == "pass"
    assert sweep.ready is True


def test_dev2_readiness_reds_a_waiver_counted_with_no_explanation(cut_tree: Path) -> None:
    """A non-zero count nobody explained is red, and blocks the sweep."""
    sweep = dev2_sweep(cut_tree, waiver_count=1)

    row = sweep.gate_row(ReleaseGateName.WAIVER_COUNT)
    assert sweep.waiver_count == 1
    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED
    assert row.status.value == "fail"
    assert "nothing to acknowledge" in row.remediation
    assert sweep.ready is False


def test_dev2_readiness_reds_a_waiver_row_with_blank_fields(cut_tree: Path) -> None:
    """An unexplained row is the same finding as an unexplained count."""
    sweep = dev2_sweep(cut_tree, waiver_count=1, waivers=(UNEXPLAINED_WAIVER,))

    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED
    assert sweep.gate_row(ReleaseGateName.WAIVER_COUNT).status.value == "fail"
    assert sweep.ready is False


def test_dev2_readiness_reds_an_explained_waiver_until_it_is_acknowledged(
    cut_tree: Path,
) -> None:
    """Explained is not accepted: somebody has to name the loss."""
    sweep = dev2_sweep(cut_tree, waiver_count=1, waivers=(EXPLAINED_WAIVER,))

    row = sweep.gate_row(ReleaseGateName.WAIVER_COUNT)
    assert sweep.waiver_disposition is WaiverDisposition.AWAITING_ACKNOWLEDGEMENT
    assert row.status.value == "fail"
    assert EXPLAINED_WAIVER.scope in row.remediation
    assert sweep.ready is False


def test_dev2_readiness_greens_an_explained_and_acknowledged_waiver(cut_tree: Path) -> None:
    """The count stays non-zero and the checkpoint is approvable again."""
    sweep = dev2_sweep(
        cut_tree,
        waiver_count=1,
        waivers=(EXPLAINED_WAIVER,),
        acknowledgements=(
            WaiverAcknowledgement(
                scope=EXPLAINED_WAIVER.scope,
                protected_principal=EXPLAINED_WAIVER.protected_principal,
                acknowledged_by="release-operator",
            ),
        ),
    )

    assert sweep.waiver_count == 1
    assert sweep.gate_row(ReleaseGateName.WAIVER_COUNT).status.value == "pass"
    assert sweep.unacknowledged_waivers == ()
    assert sweep.ready is True


def test_dev2_approve_is_refused_while_a_waiver_is_unexplained(
    ctx: MethodContext, state_path: Path, cut_tree: Path, terminal_dev1: Release
) -> None:
    """Every gate green plus one unexplained waiver still denies."""
    opened = Release.model_validate(open_dev2(ctx)["release"])
    sweep = dev2_sweep(cut_tree, waiver_count=1)

    with pytest.raises(DaemonValidationError) as excinfo:
        approve_dev2(ctx, pinned_dev2(opened, cut_tree), sweep)

    assert "release_not_ready" in str(excinfo.value)
    assert f"first red gate {ReleaseGateName.WAIVER_COUNT.value!r}" in str(excinfo.value)
    assert sweep.first_red is None
    persisted = read_release_record(state_path, DEV2_KEY)
    assert persisted is not None
    assert persisted.status is ReleaseStatus.DRAFT


def test_dev2_waiver_count_gate_reads_the_waiver_block_not_a_signal_row(
    cut_tree: Path,
) -> None:
    """The gate has no signal row to inherit, which is why it is new."""
    sweep = dev2_sweep(cut_tree)

    row = sweep.gate_row(ReleaseGateName.WAIVER_COUNT)
    assert row.evidence_kind.value == "waiver_block"
    assert row.required is True
    assert ReleaseGateName.WAIVER_COUNT in set(dev2_config().gates.required)


# --- the succession guard's own boundaries ----------------------------


def test_predecessor_rung_is_none_at_the_head_of_the_ladder() -> None:
    """The first rung succeeds nothing and needs no record."""
    assert predecessor_rung(V07_TRAIN, DEV1_VERSION) is None
    assert assert_predecessor_terminal(V07_TRAIN, version=DEV1_VERSION, predecessor=None) is None


def test_predecessor_rung_of_dev2_is_dev1() -> None:
    """The ladder's one-step-back lookup is the rung below, not any rung."""
    rung = predecessor_rung(V07_TRAIN, DEV2_VERSION)

    assert rung is not None
    assert rung.release_key == DEV1_KEY


def test_predecessor_rung_refuses_a_version_the_train_never_declared() -> None:
    """A version off the ladder cannot be opened at all."""
    with pytest.raises((KeyError, ValueError)):
        predecessor_rung(V07_TRAIN, "9.9.9")


def test_assert_predecessor_terminal_accepts_the_burned_record() -> None:
    """``partially_released`` is finished with, so a successor may open."""
    rung = assert_predecessor_terminal(V07_TRAIN, version=DEV2_VERSION, predecessor=burned_dev1())

    assert rung is not None
    assert rung.release_key == DEV1_KEY
    assert ReleaseStatus.PARTIALLY_RELEASED.value in SUCCEEDABLE_STATUSES


def test_assert_predecessor_terminal_refuses_a_record_of_another_rung() -> None:
    """A record that is not the rung below proves nothing about it."""
    with pytest.raises(ValueError, match="is not the rung below"):
        assert_predecessor_terminal(
            V07_TRAIN,
            version=DEV2_VERSION,
            predecessor=Release(
                uid=DEV2_UID,
                key=DEV2_KEY,
                version=DEV2_VERSION,
                channel=ReleaseChannel.DEV,
                authority_epoch=1,
            ),
        )


def test_assert_predecessor_terminal_names_the_live_status_it_refused() -> None:
    """The refusal is readable without opening the record it refused."""
    with pytest.raises(CheckpointSuccessionError) as excinfo:
        assert_predecessor_terminal(V07_TRAIN, version=DEV2_VERSION, predecessor=dev1_draft())

    assert excinfo.value.code is SuccessionDenialCode.PREDECESSOR_LIVE
    assert excinfo.value.release_key == DEV2_KEY
    assert excinfo.value.predecessor_key == DEV1_KEY
