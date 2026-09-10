"""Integration tests for ``eawf release tag`` and the tag-push chokepoint.

The verb shells out to git, so each test drives it inside a throwaway
git repo under ``tmp_path``. Covers the dry-run plan, real annotated-tag
creation, the already-exists guard, the dirty-tree refusal and its
waiver, and the readiness sweep ``--push`` is gated on -- including the
working-copy probes that sweep produces its verdict from.

The push path is never exercised against a real remote here: every
``--push`` case is refused by the preflight before a tag is created, and
the probe cases drive the sweep directly against a local bare remote.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.spec.release_config import load_release_config
from eawf.surfaces.cli.app import app
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.verify.release_probes import (
    TagPreflightInputs,
    _changelog_section,
    build_tag_probes,
)
from eawf.workflow.verify.release_readiness import (
    ReleaseSignalName,
    ReleaseSignalStatus,
    compute_readiness,
)

runner = CliRunner()

pytestmark = pytest.mark.integration

DEV1_VERSION = "0.7.0.dev1"

_READY_CHANGELOG = f"""\
# Changelog

## [{DEV1_VERSION}]

### Added
- The release preflight runs at the tag-push chokepoint.

### Migration
- None required: no persisted schema changed in this checkpoint.

## [0.6.8]

### Fixed
- An older entry the section miner must not read.
"""


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.invalid"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "f.txt"], path)
    _git(["commit", "-m", "init"], path)


def _init_published_repo(tmp_path: Path) -> Path:
    """Return a clean checkout whose HEAD is already on ``origin/main``."""
    work = tmp_path / "work"
    work.mkdir()
    _init_repo(work)
    (work / "CHANGELOG.md").write_text(_READY_CHANGELOG, encoding="utf-8")
    _git(["add", "CHANGELOG.md"], work)
    _git(["commit", "-m", "changelog"], work)
    bare = tmp_path / "remote.git"
    _git(["init", "--bare", str(bare)], tmp_path)
    _git(["remote", "add", "origin", str(bare)], work)
    _git(["push", "origin", "main"], work)
    _git(["fetch", "origin"], work)
    return work


def _ready_inputs(repo_root: Path) -> TagPreflightInputs:
    return TagPreflightInputs(
        repo_root=repo_root,
        version=DEV1_VERSION,
        tag=f"v{DEV1_VERSION}",
        package_version=DEV1_VERSION,
        remote="origin",
    )


def _sweep(inputs: TagPreflightInputs) -> dict[ReleaseSignalName, ReleaseSignalStatus]:
    """Return each row's status for *inputs*.

    Args:
        inputs: The tag-preflight inputs the probes read.

    Returns:
        Status by signal name.
    """
    body = yaml.safe_load(checkpoint_config_yaml(DEV1_VERSION))
    config = load_release_config(body, train=V07_TRAIN)
    readiness = compute_readiness(
        config,
        probes=build_tag_probes(inputs),
        observed_revision="deadbeef",
        computed_at=datetime.now(UTC),
    )
    return {row.signal: row.status for row in readiness.signals}


# --- plan / tag creation ----------------------------------------------------


def test_release_tag_dry_run_creates_no_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9", "--dry-run"])
    assert result.exit_code == 0
    assert "would create tag v9.9.9" in result.stdout
    assert _git(["tag", "--list"], tmp_path).strip() == ""


def test_release_tag_creates_annotated_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9"])
    assert result.exit_code == 0
    assert "tagged v9.9.9" in result.stdout
    assert "v9.9.9" in _git(["tag", "--list"], tmp_path)


def test_release_tag_existing_tag_without_force_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _git(["tag", "v9.9.9"], tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9"])
    assert result.exit_code != 0
    assert "already exists" in (result.stdout + str(result.exception or ""))


# --- dirty tree: refusal, waiver, and the waiver's reach --------------------


def test_release_tag_dirty_tree_without_a_waiver_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dirty release tree is refused by its named failure code."""
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9"])
    assert result.exit_code != 0
    assert "dirty_release_tree" in (result.stdout + str(result.exception or ""))
    assert _git(["tag", "--list"], tmp_path).strip() == ""


def test_release_tag_force_no_longer_admits_a_dirty_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--force`` overwrites a tag; it does not admit uncommitted work."""
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9", "--force"])
    assert result.exit_code != 0
    assert "dirty_release_tree" in (result.stdout + str(result.exception or ""))
    assert _git(["tag", "--list"], tmp_path).strip() == ""


def test_release_tag_dirty_tree_waiver_records_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit waiver admits the dirty tree and carries its reason."""
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["release", "tag", "9.9.9", "--waive-dirty-tree", "hotfix cut from a live tree"],
    )
    assert result.exit_code == 0, result.stdout
    assert "hotfix cut from a live tree" in result.stdout
    assert "v9.9.9" in _git(["tag", "--list"], tmp_path)


def test_release_tag_dirty_tree_waiver_requires_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiver that states nothing records nothing, so it is refused."""
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9", "--waive-dirty-tree", "   "])
    assert result.exit_code != 0
    assert "non-empty reason" in (result.stdout + str(result.exception or ""))
    assert _git(["tag", "--list"], tmp_path).strip() == ""


def test_release_tag_dirty_tree_waiver_still_refuses_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiver admits a local tag; it never makes the sweep publishable."""
    work = _init_published_repo(tmp_path)
    (work / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    monkeypatch.chdir(work)
    result = runner.invoke(
        app,
        ["release", "tag", DEV1_VERSION, "--push", "--waive-dirty-tree", "mid-flight"],
    )
    assert result.exit_code != 0
    assert "release preflight refuses" in (result.stdout + str(result.exception or ""))
    assert _git(["tag", "--list"], work).strip() == ""


# --- the tag-push chokepoint ------------------------------------------------


def test_release_tag_push_preflight_refuses_an_unauthored_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version with no authored checkpoint has no gates, so it cannot ship."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9", "--push"])
    assert result.exit_code != 0
    assert "no release configuration authored" in (result.stdout + str(result.exception or ""))
    assert _git(["tag", "--list"], tmp_path).strip() == ""


def test_release_tag_push_preflight_names_the_first_red_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A red sweep refuses the push by name, before any tag is created."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", DEV1_VERSION, "--push"])
    output = result.stdout + str(result.exception or "")
    assert result.exit_code != 0
    assert "release preflight refuses" in output
    assert "first red signal" in output
    assert _git(["tag", "--list"], tmp_path).strip() == ""


def test_release_tag_push_preflight_refuses_a_dry_run_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` previews the push, so it meets the same chokepoint."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", DEV1_VERSION, "--push", "--dry-run"])
    assert result.exit_code != 0
    assert "release preflight refuses" in (result.stdout + str(result.exception or ""))


def test_release_preflight_verb_reports_every_signal_then_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep the workflow runs prints the whole repair list, then refuses."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "preflight", DEV1_VERSION])
    assert result.exit_code != 0
    for signal in ReleaseSignalName:
        assert signal.value in result.stdout
    assert "ready=False" in result.stdout


def test_release_preflight_verb_rejects_an_unauthored_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "preflight", "9.9.9"])
    assert result.exit_code != 0
    assert "no release configuration authored" in (result.stdout + str(result.exception or ""))


# --- the working-copy probes the sweep reads --------------------------------


def test_tag_preflight_probes_pass_on_a_publishable_checkout(tmp_path: Path) -> None:
    """The five working-copy signals go green together on a ready checkout."""
    statuses = _sweep(_ready_inputs(_init_published_repo(tmp_path)))
    for signal in (
        ReleaseSignalName.VERSION_CONSISTENCY,
        ReleaseSignalName.CHANGELOG,
        ReleaseSignalName.MIGRATION,
        ReleaseSignalName.ANCESTRY,
        ReleaseSignalName.TREE_CLEANLINESS,
    ):
        assert statuses[signal] is ReleaseSignalStatus.PASS, signal


def test_tag_preflight_leaves_producerless_signals_unavailable(tmp_path: Path) -> None:
    """A signal with no producer stays unproven rather than reading green.

    ``credentials`` is no longer in this set: it has a producer, so an
    absent handle is a FAIL it can name rather than a gap it cannot see.
    """
    statuses = _sweep(_ready_inputs(_init_published_repo(tmp_path)))
    assert statuses[ReleaseSignalName.DEPENDENCIES] is ReleaseSignalStatus.UNAVAILABLE
    assert statuses[ReleaseSignalName.ARTIFACTS] is ReleaseSignalStatus.UNAVAILABLE


def test_tag_preflight_credentials_reports_no_producer(tmp_path: Path) -> None:
    """The sunset probe leaves the row UNAVAILABLE rather than a free green.

    Every target on this train authenticates by OIDC or the ambient
    workflow token, so no shipped checkpoint holds a handle for the
    probe to look for. UNAVAILABLE says nobody can answer, which is the
    honest report; the removed probe said PASS, which was a green row
    that measured nothing.
    """
    statuses = _sweep(_ready_inputs(_init_published_repo(tmp_path)))
    assert statuses[ReleaseSignalName.CREDENTIALS] is ReleaseSignalStatus.UNAVAILABLE


def test_tag_preflight_tree_cleanliness_reds_on_one_uncommitted_path(tmp_path: Path) -> None:
    work = _init_published_repo(tmp_path)
    (work / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    statuses = _sweep(_ready_inputs(work))
    assert statuses[ReleaseSignalName.TREE_CLEANLINESS] is ReleaseSignalStatus.FAIL


def test_tag_preflight_ancestry_reds_without_the_remote_branch(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _init_repo(work)
    statuses = _sweep(_ready_inputs(work))
    assert statuses[ReleaseSignalName.ANCESTRY] is ReleaseSignalStatus.FAIL


def test_tag_preflight_ancestry_reds_on_a_commit_the_remote_lacks(tmp_path: Path) -> None:
    work = _init_published_repo(tmp_path)
    (work / "later.txt").write_text("unpublished\n", encoding="utf-8")
    _git(["add", "later.txt"], work)
    _git(["commit", "-m", "later"], work)
    statuses = _sweep(_ready_inputs(work))
    assert statuses[ReleaseSignalName.ANCESTRY] is ReleaseSignalStatus.FAIL


def test_tag_preflight_version_consistency_reds_on_a_stale_package_version(
    tmp_path: Path,
) -> None:
    work = _init_published_repo(tmp_path)
    inputs = TagPreflightInputs(
        repo_root=work,
        version=DEV1_VERSION,
        tag=f"v{DEV1_VERSION}",
        package_version="0.6.8",
        remote="origin",
    )
    statuses = _sweep(inputs)
    assert statuses[ReleaseSignalName.VERSION_CONSISTENCY] is ReleaseSignalStatus.FAIL


def test_tag_preflight_changelog_reds_on_an_entryless_section(tmp_path: Path) -> None:
    work = _init_published_repo(tmp_path)
    (work / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{DEV1_VERSION}]\n\n### Added\n\n## [0.6.8]\n\n- old\n",
        encoding="utf-8",
    )
    _git(["commit", "-am", "empty section"], work)
    statuses = _sweep(_ready_inputs(work))
    assert statuses[ReleaseSignalName.CHANGELOG] is ReleaseSignalStatus.FAIL


def test_tag_preflight_migration_reds_on_a_section_that_states_no_outcome(
    tmp_path: Path,
) -> None:
    work = _init_published_repo(tmp_path)
    (work / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{DEV1_VERSION}]\n\n- One entry that says nothing about schemas.\n",
        encoding="utf-8",
    )
    _git(["commit", "-am", "silent section"], work)
    statuses = _sweep(_ready_inputs(work))
    assert statuses[ReleaseSignalName.CHANGELOG] is ReleaseSignalStatus.PASS
    assert statuses[ReleaseSignalName.MIGRATION] is ReleaseSignalStatus.FAIL


def test_tag_preflight_changelog_reds_when_the_file_is_absent(tmp_path: Path) -> None:
    work = _init_published_repo(tmp_path)
    (work / "CHANGELOG.md").unlink()
    _git(["commit", "-am", "drop changelog"], work)
    statuses = _sweep(_ready_inputs(work))
    assert statuses[ReleaseSignalName.CHANGELOG] is ReleaseSignalStatus.FAIL
    assert statuses[ReleaseSignalName.MIGRATION] is ReleaseSignalStatus.FAIL


@pytest.mark.parametrize("blank", ["", "   "])
def test_tag_preflight_inputs_reject_a_blank_version(tmp_path: Path, blank: str) -> None:
    with pytest.raises(ValueError, match="version must not be blank"):
        TagPreflightInputs(
            repo_root=tmp_path,
            version=blank,
            tag="v0.7.0.dev1",
            package_version=DEV1_VERSION,
            remote="origin",
        )


def test_tag_preflight_inputs_reject_a_blank_remote(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="remote must not be blank"):
        TagPreflightInputs(
            repo_root=tmp_path,
            version=DEV1_VERSION,
            tag=f"v{DEV1_VERSION}",
            package_version=DEV1_VERSION,
            remote="",
        )


def test_changelog_section_returns_nothing_for_an_absent_version() -> None:
    assert _changelog_section(_READY_CHANGELOG, "9.9.9") == ()


def test_changelog_section_stops_at_the_next_release_heading() -> None:
    section = _changelog_section(_READY_CHANGELOG, DEV1_VERSION)
    assert any("tag-push chokepoint" in line for line in section)
    assert not any("older entry" in line for line in section)


def test_changelog_section_of_an_empty_changelog_is_empty() -> None:
    assert _changelog_section("", DEV1_VERSION) == ()
