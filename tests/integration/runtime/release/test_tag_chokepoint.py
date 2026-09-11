"""The ``0.7.0.dev2`` cut commit clears the tag chokepoint.

Three claims, over the files this release actually ships.

**The version module and the changelog agree.** ``eawf.__version__`` is
read out of the running package rather than pinned to a literal here, and
the changelog copied into the fixture is this repository's own. So the
``version_consistency`` and ``changelog`` rows are settled by
``src/eawf/_version.py`` and ``CHANGELOG.md`` as committed: editing
either one out of agreement reds this module rather than leaving a
literal in a test agreeing with itself.

**The section states the migration outcome.** A release that needs no
migration says so in one line, and silence is indistinguishable from a
forgotten note. This checkpoint does carry a migration, so the section
names it, and the ``migration`` row also reads the ten committed
rehearsal records -- a changelog note over an unrehearsed cutover is not
enough to green the row.

**The chokepoint passes, and the pass is earned.** The verb under test is
``eawf release tag --push --dry-run``, which runs the real sweep and the
real refusal and stops short of touching git. Each red-path case removes
exactly one input and asserts the same verb refuses, so the green is not
the sweep failing to look.

Ancestry and tree cleanliness are questions about a repository, so the
fixture is a real checkout whose ``origin/main`` already carries HEAD --
the shape the cut commit has once the phase branch merges, which is
deliberately *not* the shape this branch has while the work is in flight.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf import __version__
from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.runtime.release import sweep_for_tag
from eawf.surfaces.cli.app import app
from eawf.workflow.release.dependencies import (
    LicenseDisposition,
    LockedPackage,
    ReleaseDependencyManifest,
    compute_lock_digest,
)
from eawf.workflow.release.pipeline_receipts import write_receipt
from eawf.workflow.release.reproducibility import (
    ArtifactDigest,
    ArtifactKind,
    BuildAttempt,
    ReproducibleBuildReceipt,
)
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.release.vulnerability import VulnerabilityReport
from eawf.workflow.verify.release_readiness import ReleaseReadiness

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

#: The version this checkpoint cuts. Asserted against the version module
#: rather than substituted for it: the whole point of the module is that
#: the two agree.
CUT_VERSION = "0.7.0.dev2"
CUT_TAG = f"v{CUT_VERSION}"
CUT_KEY = f"REL-{CUT_VERSION}"
PUBLISHING_REMOTE = "origin"

_REPO_ROOT = Path(__file__).resolve().parents[4]
CHANGELOG = _REPO_ROOT / "CHANGELOG.md"

#: Copied verbatim out of this checkout, because each is read as a
#: committed fact rather than supplied as a parameter.
_COMMITTED_INPUTS = (
    Path("CHANGELOG.md"),
    Path("pyproject.toml"),
    Path("tests/golden/kernel/migration/rehearsal"),
)

LOCK_TEXT = "version = 1\nrequires-python = '>=3.14'\n"
GITIGNORE_TEXT = "dist/\n"

#: The rows a working copy can settle at this checkpoint, which are the
#: rows the chokepoint is answerable for.
WORKING_COPY_SIGNALS = (
    ReleaseSignalName.VERSION_CONSISTENCY,
    ReleaseSignalName.CHANGELOG,
    ReleaseSignalName.ANCESTRY,
    ReleaseSignalName.TREE_CLEANLINESS,
    ReleaseSignalName.MIGRATION,
)


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


def cut_config() -> ReleaseConfig:
    """Return the rendered configuration of the checkpoint being cut."""
    return load_release_config(checkpoint_config_yaml(CUT_VERSION), train=V07_TRAIN)


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
        ),
        imported_distributions=("pydantic",),
        locked_distributions=("pydantic",),
    )


def _build_receipt(source_sha: str) -> ReproducibleBuildReceipt:
    """Return a double-build receipt whose two attempts agree."""
    artifacts = (
        ArtifactDigest(
            filename=f"eawf-{CUT_VERSION}-py3-none-any.whl",
            kind=ArtifactKind.WHEEL,
            sha256="a" * 64,
        ),
        ArtifactDigest(
            filename=f"eawf-{CUT_VERSION}.tar.gz", kind=ArtifactKind.SDIST, sha256="b" * 64
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
def cut_commit(tmp_path: Path) -> Path:
    """Return the cut commit as the chokepoint will meet it after the merge."""
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


def chokepoint_sweep(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, waiver_count: int = 0
) -> ReleaseReadiness:
    """Return the sweep the chokepoint computes over *repo*.

    The working directory is moved onto *repo* because that is what the
    verb does in the pipeline: the receipt probes the pipeline's
    ``inventory-and-reproducibility`` job feeds read the checkout the
    tag is being cut in, and pointing them anywhere else would test a
    composition production never runs.
    """
    monkeypatch.chdir(repo)
    return sweep_for_tag(
        cut_config(),
        version=CUT_VERSION,
        repo_root=repo,
        remote=PUBLISHING_REMOTE,
        package_version=__version__,
        source=_git(repo, "rev-parse", "HEAD").stdout.strip(),
        waiver_count=waiver_count,
        computed_at=NOW,
    )


def tag_dry_run(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> object:
    """Invoke ``eawf release tag --push --dry-run`` inside *repo*."""
    monkeypatch.chdir(repo)
    return CliRunner().invoke(
        app, ["--json", "release", "tag", CUT_VERSION, "--push", "--dry-run", *args]
    )


# --- the version module and the changelog agree -----------------------


def test_the_package_version_is_the_checkpoint_being_cut() -> None:
    """``_version.py`` says what this checkpoint claims to be."""
    assert __version__ == CUT_VERSION


def test_the_changelog_carries_a_section_for_the_cut_version() -> None:
    """The shipped changelog has the section the chokepoint mines."""
    text = CHANGELOG.read_text(encoding="utf-8")

    assert f"## [{CUT_VERSION}]" in text
    assert text.index(f"## [{CUT_VERSION}]") < text.index("## [0.7.0.dev1]")


def test_the_cut_section_names_the_migration_and_its_limitations() -> None:
    """A checkpoint that migrates says so, and says what stays unproven."""
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.index(f"## [{CUT_VERSION}]")
    section = text[start : text.index("## [0.7.0.dev1]")]

    assert "### Migration" in section
    assert "### Limitations" in section
    assert section.count("\n- ") >= 2


def test_the_version_consistency_row_passes_on_the_cut_commit(
    cut_commit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Request, package, tag and configuration all spell one version."""
    sweep = chokepoint_sweep(cut_commit, monkeypatch)

    row = sweep.row(ReleaseSignalName.VERSION_CONSISTENCY)
    assert row.status is ReleaseSignalStatus.PASS
    assert f"version:{CUT_VERSION}" in row.evidence_refs


def test_the_changelog_row_passes_on_the_cut_commit(
    cut_commit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped changelog settles the row, not a fixture stand-in."""
    sweep = chokepoint_sweep(cut_commit, monkeypatch)

    assert sweep.row(ReleaseSignalName.CHANGELOG).status is ReleaseSignalStatus.PASS


def test_the_migration_row_passes_over_the_committed_rehearsals(
    cut_commit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ten rehearsal records plus a stated outcome green the row."""
    sweep = chokepoint_sweep(cut_commit, monkeypatch)

    assert sweep.row(ReleaseSignalName.MIGRATION).status is ReleaseSignalStatus.PASS


# --- the chokepoint passes, and the pass is earned ---------------------


def test_the_tag_chokepoint_passes_on_the_cut_commit(
    cut_commit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``release tag --push`` clears its preflight and would tag."""
    result = tag_dry_run(cut_commit, monkeypatch)

    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["tag"] == CUT_TAG
    assert plan["push"] is True
    assert plan["dry_run"] is True
    assert plan["preflight_ready"] is True
    assert plan["waiver"] is None


def test_every_working_copy_row_passes_on_the_cut_commit(
    cut_commit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The five rows a checkout can settle are all green, none unavailable."""
    sweep = chokepoint_sweep(cut_commit, monkeypatch)

    statuses = {signal: sweep.row(signal).status for signal in WORKING_COPY_SIGNALS}
    assert set(statuses.values()) == {ReleaseSignalStatus.PASS}
    assert sweep.ready is True
    assert sweep.release_key == CUT_KEY


def test_the_chokepoint_refuses_when_the_changelog_section_is_removed(
    cut_commit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the section reds the same verb, so the green was earned."""
    path = cut_commit / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(f"## [{CUT_VERSION}]", "## [9.9.9]", 1), encoding="utf-8")
    _git(cut_commit, "commit", "--quiet", "--all", "--message", "docs: drop the section")
    _git(cut_commit, "update-ref", f"refs/remotes/{PUBLISHING_REMOTE}/main", "HEAD")

    result = tag_dry_run(cut_commit, monkeypatch)

    assert result.exit_code != 0
    assert "changelog" in result.output


def test_the_chokepoint_refuses_when_the_tree_is_dirty(
    cut_commit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncommitted release input is never tagged over silently.

    The refusal lands before the sweep rather than in it: a dirty tree is
    the one condition the verb rejects outright, and the only way past it
    records the reason as a waiver that keeps the sweep from reading
    green. So the row the sweep would have redded is asserted beside the
    verb's own refusal.
    """
    (cut_commit / "uv.lock").write_text(f"{LOCK_TEXT}# edited\n", encoding="utf-8")

    result = tag_dry_run(cut_commit, monkeypatch)

    assert result.exit_code != 0
    assert json.loads(result.output)["data"]["kind"] == "DirtyReleaseTree"
    sweep = chokepoint_sweep(cut_commit, monkeypatch)
    assert sweep.row(ReleaseSignalName.TREE_CLEANLINESS).status is ReleaseSignalStatus.FAIL
    assert sweep.ready is False


def test_the_chokepoint_refuses_when_the_commit_is_not_on_the_remote_branch(
    cut_commit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit the remote branch does not carry cannot be published."""
    _git(cut_commit, "update-ref", "-d", f"refs/remotes/{PUBLISHING_REMOTE}/main")

    result = tag_dry_run(cut_commit, monkeypatch)

    assert result.exit_code != 0
    assert "ancestry" in result.output


def test_the_chokepoint_refuses_when_the_rehearsal_records_are_absent(
    cut_commit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changelog note over an unrehearsed cutover does not green migration."""
    shutil.rmtree(cut_commit / "tests" / "golden" / "kernel" / "migration" / "rehearsal")
    _git(cut_commit, "commit", "--quiet", "--all", "--message", "test: drop the rehearsals")
    _git(cut_commit, "update-ref", f"refs/remotes/{PUBLISHING_REMOTE}/main", "HEAD")

    result = tag_dry_run(cut_commit, monkeypatch)

    assert result.exit_code != 0
    assert "migration" in result.output


def test_the_chokepoint_refuses_a_version_the_train_never_declared(
    cut_commit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unauthored checkpoint has no gates, so it cannot be tagged."""
    monkeypatch.chdir(cut_commit)

    result = CliRunner().invoke(app, ["release", "tag", "9.9.9", "--push", "--dry-run"])

    assert result.exit_code != 0
    assert "no release configuration authored" in result.output


# --- sweep_for_tag's own boundaries ------------------------------------


def test_sweep_for_tag_refuses_a_naive_instant(cut_commit: Path) -> None:
    """A sweep with no zone cannot be ordered against a receipt's expiry."""
    with pytest.raises(ValueError, match="computed_at must be timezone-aware"):
        sweep_for_tag(
            cut_config(),
            version=CUT_VERSION,
            repo_root=cut_commit,
            remote=PUBLISHING_REMOTE,
            package_version=__version__,
            source=None,
            waiver_count=0,
            computed_at=datetime(2026, 9, 11, 12, 0),
        )


def test_sweep_for_tag_refuses_a_blank_remote(cut_commit: Path) -> None:
    """A blank remote names no branch for ancestry to be proven against."""
    with pytest.raises(ValueError, match="remote must not be blank"):
        sweep_for_tag(
            cut_config(),
            version=CUT_VERSION,
            repo_root=cut_commit,
            remote="",
            package_version=__version__,
            source=None,
            waiver_count=0,
            computed_at=NOW,
        )


def test_sweep_for_tag_refuses_a_negative_waiver_count(cut_commit: Path) -> None:
    """A negative count is not a waiver disposition the sweep can report."""
    with pytest.raises(ValueError, match="waiver_count must not be negative"):
        sweep_for_tag(
            cut_config(),
            version=CUT_VERSION,
            repo_root=cut_commit,
            remote=PUBLISHING_REMOTE,
            package_version=__version__,
            source=None,
            waiver_count=-1,
            computed_at=NOW,
        )


def test_sweep_for_tag_reds_version_consistency_when_the_package_disagrees(
    cut_commit: Path,
) -> None:
    """The row catches a tag cut from a checkout at another version."""
    sweep = sweep_for_tag(
        cut_config(),
        version=CUT_VERSION,
        repo_root=cut_commit,
        remote=PUBLISHING_REMOTE,
        package_version="0.7.0.dev1",
        source=None,
        waiver_count=0,
        computed_at=NOW,
    )

    row = sweep.row(ReleaseSignalName.VERSION_CONSISTENCY)
    assert row.status is ReleaseSignalStatus.FAIL
    assert "0.7.0.dev1" in row.remediation
