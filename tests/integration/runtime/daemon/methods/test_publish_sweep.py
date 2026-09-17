"""``release.publish`` sweeps the approval's pinned source, unpatched.

Nothing here patches a probe. The daemon handlers run the shared
chokepoint sweep over a real checkout: a git repository under
``tmp_path`` with a version module, a changelog, the committed cutover
rehearsal records, the three CI receipts and an ``origin`` remote whose
``main`` already carries the pinned commit. The state root lives inside
that checkout, which is how the handlers find it.

Three claims are pinned.

**Publish opens on the sweep alone.** An approved ``0.7.0.dev2`` record
whose pinned commit is clean and published moves to PUBLISHING with every
leg queued, even while the process sits in an unrelated directory.

**A version bump on HEAD does not stale the approval.** The sweep reads
the version module and the changelog out of the pinned commit and
proves ancestry for that commit, so a later bump keeps it ready -- and
the same sweep taken at HEAD reds, which is what shows the pin is doing
the work.

**The ledger the publish wrote does not red the next sweep.** The
release store the verb appends to is untracked, and the tree check sets
it aside.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from eawf.kernel.spec.release import Release, ReleaseChannel, ReleaseStatus
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import compute_readiness_method, publish
from eawf.workflow.evidence.migration_rehearsal import REHEARSAL_EVIDENCE_DIR, evidence_dir
from eawf.workflow.release.ledger import ledger_path
from eawf.workflow.release.train import V07_TRAIN
from eawf.workflow.verify.release_probes import CHANGELOG_FILENAME, VERSION_MODULE_PATH
from tests._release_helpers import stage_passing_receipts

pytestmark = pytest.mark.integration

VERSION = "0.7.0.dev2"
BUMPED_VERSION = "0.7.0.dev3"
RELEASE_KEY = f"REL-{VERSION}"
MANIFEST_DIGEST = f"sha256:{'d' * 64}"
PROOF_DIGEST = f"sha256:{'1' * 64}"
APPROVED_REVISION = 2

#: This repository, which the checkout copies its cutover records from.
_REPO_ROOT = Path(__file__).resolve().parents[5]

#: Receipts and the locks the stores take are never committed, exactly
#: as in the real repository.
GITIGNORE_TEXT = "dist/\n.ea/locks/\n.ea/**/*.lock\n"

#: The seven rows the dev2 configuration requires, every one of them
#: computed from the checkout rather than from the checkpoint alone.
REQUIRED_DEV2_SIGNALS = (
    "version_consistency",
    "changelog",
    "ancestry",
    "tree_cleanliness",
    "migration",
    "artifacts",
    "dependencies",
)


def _changelog(*versions: str) -> str:
    """Return a changelog with one entry-bearing section per version."""
    sections = "".join(
        f"## [{version}]\n\n### Added\n- The {version} checkpoint.\n\n"
        f"### Migration\n- No persisted schema is migrated by {version}.\n\n"
        for version in versions
    )
    return f"# Changelog\n\n{sections}"


def _git(repo: Path, *args: str) -> str:
    """Run one git command inside *repo* and return its stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def _write(repo: Path, relative: str, text: str) -> None:
    """Write *text* to *relative* under *repo*, creating its parents."""
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_and_push(repo: Path, message: str) -> str:
    """Commit everything, push ``main`` to ``origin`` and return HEAD."""
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", message)
    _git(repo, "push", "--quiet", "origin", "main")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Return a checkout whose HEAD is the published ``0.7.0.dev2`` cut."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch", "main")
    _git(repo, "config", "user.name", "EAWF Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "commit.gpgSign", "false")
    _git(repo, "config", "core.hooksPath", ".git/hooks")
    _write(repo, ".gitignore", GITIGNORE_TEXT)
    _write(repo, VERSION_MODULE_PATH, f'__version__ = "{VERSION}"\n')
    _write(repo, CHANGELOG_FILENAME, _changelog(VERSION))
    _write(repo, ".ea/state.json", json.dumps({}))
    shutil.copytree(evidence_dir(_REPO_ROOT), repo.joinpath(*REHEARSAL_EVIDENCE_DIR))
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    head = _commit_and_push(repo, "feat: cut the dev2 checkpoint")
    stage_passing_receipts(repo, version=VERSION, source_sha=head)
    return repo


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move the process into a directory that holds no checkout at all."""
    path = tmp_path / "elsewhere"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


def context_for(repo: Path) -> MethodContext:
    """Return a method context whose state root lives inside *repo*."""
    return MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=4242,
        protocol_version=PROTOCOL_VERSION,
        version="test",
        state_path=repo / ".ea" / "state.json",
    )


def call(
    handler: Callable[[MethodContext, dict[str, Any]], Awaitable[dict[str, Any]]],
    ctx: MethodContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Drive one handler to completion and return its result."""

    async def run() -> dict[str, Any]:
        return await handler(ctx, params)

    return asyncio.run(run())


def record(repo: Path, *, status: ReleaseStatus = ReleaseStatus.APPROVED) -> dict[str, Any]:
    """Return the serialized dev2 record pinned to *repo*'s HEAD."""
    rung = V07_TRAIN.checkpoint_for_version(VERSION)
    release = Release(
        uid=UUID(int=2002),
        key=RELEASE_KEY,
        version=VERSION,
        channel=ReleaseChannel.DEV,
        authority_epoch=rung.authority_epoch,
        status=status,
        approval_ref=None if status is ReleaseStatus.CANDIDATE else "receipt://approval/dev2",
        source_sha=_git(repo, "rev-parse", "HEAD"),
        source_tree_sha=_git(repo, "rev-parse", "HEAD^{tree}"),
        manifest_ref="artifact://release/manifest/0.7.0.dev2",
        manifest_digest=MANIFEST_DIGEST,
        revision=APPROVED_REVISION,
    )
    return release.model_dump(mode="json")


def publish_params(payload: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return well-formed ``release.publish`` params for *payload*."""
    return {
        "release": payload,
        "expected_revision": payload["revision"],
        "idempotency_key": "publish-0.7.0.dev2-sweep",
        "approved_manifest_digest": MANIFEST_DIGEST,
        "proof_digest": PROOF_DIGEST,
        **overrides,
    }


def bump_head(repo: Path) -> str:
    """Commit and publish the next version bump; return the new HEAD."""
    _write(repo, VERSION_MODULE_PATH, f'__version__ = "{BUMPED_VERSION}"\n')
    _write(repo, CHANGELOG_FILENAME, _changelog(BUMPED_VERSION, VERSION))
    return _commit_and_push(repo, "chore: open the dev3 checkpoint")


def statuses(result: dict[str, Any]) -> dict[str, str]:
    """Return each readiness row's status by signal name."""
    return {row["signal"]: row["status"] for row in result["readiness"]["signals"]}


# --- publish opens on the sweep alone ------------------------------------


@pytest.mark.usefixtures("elsewhere")
def test_publish_opens_on_an_unpatched_sweep_of_the_pinned_source(checkout: Path) -> None:
    """The approved record moves to PUBLISHING with every leg queued."""
    result = call(publish, context_for(checkout), publish_params(record(checkout)))

    assert result["replayed"] is False
    assert result["release"]["status"] == ReleaseStatus.PUBLISHING.value
    assert set(result["release"]["target_statuses"].values()) == {"queued"}
    assert len(result["release"]["target_statuses"]) == 3
    assert ledger_path(checkout / ".ea" / "state.json").is_file()


@pytest.mark.usefixtures("elsewhere")
def test_compute_readiness_passes_every_required_row_at_the_pin(checkout: Path) -> None:
    """Each working-copy and receipt row is computed, not left unavailable."""
    pinned = record(checkout, status=ReleaseStatus.CANDIDATE)
    result = call(
        compute_readiness_method,
        context_for(checkout),
        {"version": VERSION, "release": pinned},
    )

    rows = statuses(result)
    for signal in REQUIRED_DEV2_SIGNALS:
        assert rows[signal] == "pass", (signal, result["readiness"])
    assert result["readiness"]["ready"] is True
    assert result["next_status"] == ReleaseStatus.CANDIDATE.value
    observed = {row["observed_revision"] for row in result["readiness"]["signals"]}
    assert observed == {pinned["source_sha"]}


# --- a version bump on HEAD does not stale the approval -------------------


def test_publish_opens_after_a_version_bump_on_head(checkout: Path) -> None:
    """The pinned commit still carries dev2, so the approval still binds."""
    approved = record(checkout)
    bump_head(checkout)

    result = call(publish, context_for(checkout), publish_params(approved))

    assert result["release"]["status"] == ReleaseStatus.PUBLISHING.value


def test_compute_readiness_stays_ready_at_the_pin_after_a_head_bump(checkout: Path) -> None:
    """Asking about the pinned commit after the bump answers ready."""
    pinned = record(checkout)["source_sha"]
    bump_head(checkout)

    result = call(
        compute_readiness_method,
        context_for(checkout),
        {"version": VERSION, "observed_revision": pinned},
    )

    assert result["readiness"]["ready"] is True


def test_compute_readiness_reds_version_consistency_at_a_bumped_head(checkout: Path) -> None:
    """With no pin the sweep reads HEAD, whose version module moved on."""
    bump_head(checkout)

    result = call(compute_readiness_method, context_for(checkout), {"version": VERSION})

    assert statuses(result)["version_consistency"] == "fail"
    assert result["readiness"]["ready"] is False
    assert result["first_red"] == "version_consistency"


# --- the ledger the publish wrote -----------------------------------------


def test_publish_ledger_does_not_red_the_next_sweep(checkout: Path) -> None:
    """The untracked release store is set aside by the tree check."""
    approved = record(checkout)
    call(publish, context_for(checkout), publish_params(approved))
    assert "release.jsonl" in _git(checkout, "status", "--porcelain", "--untracked-files=all")

    result = call(
        compute_readiness_method,
        context_for(checkout),
        {"version": VERSION, "observed_revision": approved["source_sha"]},
    )

    assert statuses(result)["tree_cleanliness"] == "pass"
    assert result["readiness"]["ready"] is True


# --- refusals ---------------------------------------------------------------


def test_publish_refuses_an_observed_revision_other_than_the_pin(checkout: Path) -> None:
    """A sweep of another commit cannot clear an approval of this one."""
    approved = record(checkout)
    head = bump_head(checkout)

    with pytest.raises(DaemonValidationError, match="is not the pinned source"):
        call(
            publish,
            context_for(checkout),
            publish_params(approved, observed_revision=head),
        )


def test_publish_refuses_a_pin_the_checkout_does_not_carry(checkout: Path) -> None:
    """A pinned commit git cannot show reds the sweep and stales the approval."""
    approved = {**record(checkout), "source_sha": "f" * 40}

    with pytest.raises(DaemonValidationError, match="approval_stale"):
        call(publish, context_for(checkout), publish_params(approved))


def test_publish_refuses_when_a_receipt_is_missing(checkout: Path) -> None:
    """The receipts are read from the checkout, so removing one reds publish."""
    (checkout / "dist" / "release-receipts" / "reproducible-build-receipt.json").unlink()

    with pytest.raises(DaemonValidationError, match="approval_stale"):
        call(publish, context_for(checkout), publish_params(record(checkout)))


def test_compute_readiness_refuses_a_blank_observed_revision(checkout: Path) -> None:
    """A blank revision names no commit, so the sweep is refused outright."""
    with pytest.raises(DaemonValidationError, match="source_sha must not be blank"):
        call(
            compute_readiness_method,
            context_for(checkout),
            {"version": VERSION, "observed_revision": "   "},
        )


def test_compute_readiness_requires_a_state_root() -> None:
    """Without a state root there is no checkout to sweep."""
    ctx = MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=4242,
        protocol_version=PROTOCOL_VERSION,
        version="test",
        state_path=None,
    )

    with pytest.raises(DaemonValidationError, match="on-disk state root"):
        call(compute_readiness_method, ctx, {"version": VERSION})
