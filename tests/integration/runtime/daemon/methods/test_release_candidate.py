"""``release.candidate`` freezes the manifest and pins the checkpoint.

The DRAFT-to-CANDIDATE edge had no verb, so the manifest an approval
binds was built by a throwaway script. These tests drive the verb that
script became: the receipts of a tag's publish jobs go in, a frozen
manifest and a recorded CANDIDATE come out, and the digest the record
pins is the one the returned manifest recomputes to.

The refusals carry as much weight as the walk. A receipts directory
missing a leg, or holding another version's receipts, is the failure
mode that would otherwise pin a manifest describing artifacts nobody
published -- so both are checked against the stored record afterwards,
which must still be the untouched DRAFT.
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

from eawf.kernel.spec.release import (
    Release,
    ReleaseStatus,
    release_key,
)
from eawf.kernel.spec.release_config import (
    ReleaseArtifactKind,
    ReleaseConfig,
    load_release_config,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release_candidate import candidate
from eawf.workflow.release.candidate import (
    CandidateRefusal,
    freeze_manifest,
    manifest_complete,
    pin_candidate,
    resolve_source_tree,
)
from eawf.workflow.release.lifecycle import ReleaseDenialCode, ReleaseTransitionError
from eawf.workflow.release.observation import FrozenManifest
from eawf.workflow.release.records import read_release_record, record_release
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml

pytestmark = pytest.mark.integration

DEV2 = "0.7.0.dev2"
DEV2_KEY = release_key(DEV2)
DEV1_KEY = release_key("0.7.0.dev1")

#: What the three dev2 publish jobs reported, verbatim in shape: the
#: PyPI leg lists its attestations beside the distributions, and the npm
#: leg names the checkpoint in SemVer.
RECEIPTS: dict[str, dict[str, Any]] = {
    "pypi": {
        "target_id": "pypi",
        "version": DEV2,
        "job_conclusion": "success",
        "run_id": "35119940687",
        "artifact_digests": {
            f"eawf-{DEV2}-py3-none-any.whl": f"sha256:{'1' * 64}",
            f"eawf-{DEV2}-py3-none-any.whl.publish.attestation": f"sha256:{'2' * 64}",
            f"eawf-{DEV2}.tar.gz": f"sha256:{'3' * 64}",
            f"eawf-{DEV2}.tar.gz.publish.attestation": f"sha256:{'4' * 64}",
        },
    },
    "npm": {
        "target_id": "npm",
        "version": "0.7.0-dev.2",
        "job_conclusion": "success",
        "run_id": "35119940584",
        "artifact_digests": {f"elementarno-eawf-{DEV2}.tgz": f"sha256:{'5' * 64}"},
    },
    "github": {
        "target_id": "github",
        "version": DEV2,
        "job_conclusion": "success",
        "run_id": "35119940584",
        "artifact_digests": {
            "RELEASE_NOTES.md": f"sha256:{'6' * 64}",
            "SHA256SUMS": f"sha256:{'7' * 64}",
            f"eawf-plugin-{DEV2}.tar.gz": f"sha256:{'8' * 64}",
        },
    },
}


#: The manifest digest the approved ``dev2`` record pins, bare hex. Three
#: things feed it -- the configured registry identities, the rule that
#: picks each kind's filenames, and the digest recipe itself -- and an
#: edit to any of them would leave that approval pinning a manifest
#: nothing can recompute, silently. Hence the standing reproduction.
APPROVED_DEV2_MANIFEST_SHA = (
    "3ea0e1250281a90d1840583cbd92c2751fc1c99b9d9f1824e69f740ea9c9e148"  # pragma: allowlist secret
)

#: sha256 of each file the three dev2 publish jobs really uploaded, bare
#: hex so the allowlist pragma fits the line. The ``sha256:`` prefix the
#: receipt model wants is added in :func:`write_published_receipts`.
PUBLISHED_WHEEL_SHA = (
    "d03753d6be17812e68621d2bb0ca1afef5dd07ef0148848c8ab14f239b998da5"  # pragma: allowlist secret
)
PUBLISHED_SDIST_SHA = (
    "51218682f4581294f7c0ab1a78ba846b31fee3222b73349402aa985a05a12b78"  # pragma: allowlist secret
)
PUBLISHED_TARBALL_SHA = (
    "61918412ca618f73dbae226a4777d44640110314bf9f3c2d07129a345bedbd8c"  # pragma: allowlist secret
)
PUBLISHED_NOTES_SHA = (
    "135a7b960942be9cd3018e6e199a035356d20a1e2220b8fefb348f4dc31ed305"  # pragma: allowlist secret
)
PUBLISHED_CHECKSUMS_SHA = (
    "d942badab2f0ada88cd4c0e8c31a0e0376686cdefdb0208b363aedd622e70afc"  # pragma: allowlist secret
)
PUBLISHED_BUNDLE_SHA = (
    "6d89424461e18b8fea4b23f5877722b7d40bbe0ce4d4af57e94f6e83354b99e9"  # pragma: allowlist secret
)


def dev2_config() -> ReleaseConfig:
    """Return the authored dev2 checkpoint configuration."""
    return load_release_config(checkpoint_config_yaml(DEV2), train=V07_TRAIN)


def dev2_draft() -> Release:
    """Return the dev2 DRAFT as ``release create`` now leaves it."""
    return Release(
        uid=UUID(int=77),
        key=DEV2_KEY,
        version=DEV2,
        channel="dev",  # type: ignore[arg-type]
        authority_epoch=1,
        status=ReleaseStatus.DRAFT,
        supersedes_release_ref=DEV1_KEY,
    )


def write_receipts(directory: Path, *, omit: str = "", version: str = "") -> Path:
    """Write the three receipts into *directory* and return it.

    Args:
        directory: Where the receipt files land.
        omit: Target id to leave out, for the missing-leg case.
        version: Version to stamp on every receipt, for the wrong-tag
            case. Empty keeps each receipt's own spelling.

    Returns:
        *directory*, created if needed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for target_id, body in RECEIPTS.items():
        if target_id == omit:
            continue
        payload = dict(body)
        if version:
            payload["version"] = version
        (directory / f"publication-receipt-{target_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    return directory


def write_published_receipts(directory: Path) -> Path:
    """Write the receipts the real dev2 publish jobs left, and return *directory*.

    Filenames and digests are what those jobs actually uploaded, so a
    freeze over them reproduces the manifest the approved record pins
    rather than a shape-alike of it. The two PyPI attestations keep
    placeholder digests: they are excluded from the artifact set by
    design, so a real digest there would suggest they matter.

    Args:
        directory: Where the three receipt files land.

    Returns:
        *directory*, created if needed.
    """
    published: dict[str, dict[str, Any]] = {
        "pypi": {
            "target_id": "pypi",
            "version": DEV2,
            "job_conclusion": "success",
            "run_id": "35119940687",
            "artifact_digests": {
                f"eawf-{DEV2}-py3-none-any.whl": f"sha256:{PUBLISHED_WHEEL_SHA}",
                f"eawf-{DEV2}-py3-none-any.whl.publish.attestation": f"sha256:{'2' * 64}",
                f"eawf-{DEV2}.tar.gz": f"sha256:{PUBLISHED_SDIST_SHA}",
                f"eawf-{DEV2}.tar.gz.publish.attestation": f"sha256:{'4' * 64}",
            },
        },
        "npm": {
            "target_id": "npm",
            "version": "0.7.0-dev.2",
            "job_conclusion": "success",
            "run_id": "35119940584",
            "artifact_digests": {
                "elementarno-eawf-0.7.0-dev.2.tgz": f"sha256:{PUBLISHED_TARBALL_SHA}",
            },
        },
        "github": {
            "target_id": "github",
            "version": DEV2,
            "job_conclusion": "success",
            "run_id": "35119940584",
            "artifact_digests": {
                "RELEASE_NOTES.md": f"sha256:{PUBLISHED_NOTES_SHA}",
                "SHA256SUMS": f"sha256:{PUBLISHED_CHECKSUMS_SHA}",
                f"eawf-plugin-{DEV2}.tar.gz": f"sha256:{PUBLISHED_BUNDLE_SHA}",
            },
        },
    }
    directory.mkdir(parents=True, exist_ok=True)
    for target_id, payload in published.items():
        (directory / f"publication-receipt-{target_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    return directory


def git(repo_root: Path, *args: str) -> str:
    """Run one git command in *repo_root* and return its stdout, stripped."""
    done = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Return a repository root holding one commit and an ``.ea`` directory."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    git(root, "init", "--quiet")
    git(root, "config", "user.email", "release@example.invalid")
    git(root, "config", "user.name", "Release Test")
    (root / "README.md").write_text("candidate fixture\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "--quiet", "-m", "seed")
    (root / ".ea").mkdir()
    return root


def context(repo_root: Path, *, record: Release | None) -> MethodContext:
    """Bind a method context to *repo_root*, seeding *record* when given."""
    state_path = repo_root / ".ea" / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    if record is not None:
        record_release(state_path, record, recorded_at=datetime.now(UTC), summary="seed")
    return MethodContext(
        started_at="2026-09-17T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version=DEV2,
        state_path=state_path,
    )


def params_for(repo_root: Path, **overrides: Any) -> dict[str, Any]:
    """Return well-formed candidate params over *repo_root*."""
    params: dict[str, Any] = {
        "version": DEV2,
        "receipts_dir": str(write_receipts(repo_root / "receipts")),
        "source_sha": git(repo_root, "rev-parse", "HEAD"),
    }
    params.update(overrides)
    return params


def stored(ctx: MethodContext) -> Release:
    """Return the record the collection currently holds for dev2."""
    record = read_release_record(Path(str(ctx.state_path)), DEV2_KEY)
    assert record is not None
    return record


def refused(ctx: MethodContext, params: dict[str, Any]) -> str:
    """Return the refusal a candidate call that must record nothing answers."""
    with pytest.raises(DaemonValidationError) as excinfo:
        asyncio.run(candidate(ctx, params))
    return str(excinfo.value)


# --- the pin -----------------------------------------------------------------


def test_candidate_records_the_pinned_candidate_from_the_receipts(repo: Path) -> None:
    """The verb freezes every declared leg and stores the CANDIDATE."""
    ctx = context(repo, record=dev2_draft())
    params = params_for(repo)

    result = asyncio.run(candidate(ctx, params))

    record = stored(ctx)
    assert record.status is ReleaseStatus.CANDIDATE
    assert record.revision == 1
    assert record.source_sha == params["source_sha"]
    assert record.source_tree_sha == resolve_source_tree(repo, params["source_sha"])
    assert record.manifest_digest == result["manifest_digest"]
    assert sorted(result["manifest"]["targets"]) == ["github", "npm", "pypi"]


def test_candidate_pins_a_manifest_that_recomputes_to_the_recorded_digest(repo: Path) -> None:
    """The returned document is the one the record's digest names."""
    ctx = context(repo, record=dev2_draft())

    result = asyncio.run(candidate(ctx, params_for(repo)))

    manifest = FrozenManifest.model_validate(result["manifest"])
    assert manifest.digest == result["release"]["manifest_digest"]
    assert result["manifest_ref"] == f"manifest://{DEV2_KEY}/{manifest.digest}"
    assert stored(ctx).manifest_ref == result["manifest_ref"]


def test_candidate_freezes_the_distributions_and_not_their_attestations(repo: Path) -> None:
    """A PyPI leg reports four files and exactly two are the artifact set."""
    ctx = context(repo, record=dev2_draft())

    result = asyncio.run(candidate(ctx, params_for(repo)))

    pypi = FrozenManifest.model_validate(result["manifest"]).target_for("pypi")
    assert [row.filename for row in pypi.artifacts] == [
        f"eawf-{DEV2}-py3-none-any.whl",
        f"eawf-{DEV2}.tar.gz",
    ]
    assert pypi.identity == "eawf"


def test_candidate_reads_each_identity_off_the_checkpoint_configuration(repo: Path) -> None:
    """The three registry names come from the configuration, not the verb."""
    ctx = context(repo, record=dev2_draft())

    result = asyncio.run(candidate(ctx, params_for(repo)))

    manifest = FrozenManifest.model_validate(result["manifest"])
    configured = {target.target_id: target.identity for target in dev2_config().targets}
    assert {name: row.identity for name, row in manifest.targets.items()} == configured


def test_candidate_keeps_the_supersedes_ref_the_draft_carried(repo: Path) -> None:
    """The lineage survives the pin rather than being dropped by it."""
    ctx = context(repo, record=dev2_draft())

    result = asyncio.run(candidate(ctx, params_for(repo)))

    assert result["supersedes_release_ref"] == DEV1_KEY
    assert stored(ctx).supersedes_release_ref == DEV1_KEY


# --- the refusals ------------------------------------------------------------


def test_candidate_refuses_a_directory_missing_a_declared_target(repo: Path) -> None:
    """A partial download freezes nothing and leaves the DRAFT standing."""
    ctx = context(repo, record=dev2_draft())
    directory = write_receipts(repo / "partial", omit="npm")

    message = refused(ctx, params_for(repo, receipts_dir=str(directory)))

    assert CandidateRefusal.PUBLICATION_RECEIPT_MISSING.value in message
    assert "'npm'" in message
    assert stored(ctx).status is ReleaseStatus.DRAFT
    assert stored(ctx).revision == 0


def test_candidate_refuses_receipts_for_another_version(repo: Path) -> None:
    """A directory downloaded for the wrong tag records nothing."""
    ctx = context(repo, record=dev2_draft())
    directory = write_receipts(repo / "wrong-tag", version="0.7.0.dev1")

    message = refused(ctx, params_for(repo, receipts_dir=str(directory)))

    assert CandidateRefusal.PUBLICATION_RECEIPT_VERSION_MISMATCH.value in message
    assert stored(ctx).status is ReleaseStatus.DRAFT


def test_candidate_refuses_an_empty_receipts_directory(repo: Path) -> None:
    """The empty boundary names every leg, not the first one."""
    ctx = context(repo, record=dev2_draft())
    empty = repo / "empty"
    empty.mkdir()

    message = refused(ctx, params_for(repo, receipts_dir=str(empty)))

    assert CandidateRefusal.PUBLICATION_RECEIPT_MISSING.value in message
    for target_id in ("github", "npm", "pypi"):
        assert f"'{target_id}'" in message


def test_candidate_refuses_a_source_commit_this_checkout_does_not_have(repo: Path) -> None:
    """A tree that cannot be read is a refusal, not a half pin."""
    ctx = context(repo, record=dev2_draft())

    message = refused(ctx, params_for(repo, source_sha="b" * 40))

    assert CandidateRefusal.SOURCE_TREE_UNRESOLVED.value in message
    assert stored(ctx).status is ReleaseStatus.DRAFT


def test_candidate_refuses_a_checkpoint_nobody_opened(repo: Path) -> None:
    """With no stored record the refusal names the verb that opens one."""
    ctx = context(repo, record=None)

    message = refused(ctx, params_for(repo))

    assert f"eawf release create {DEV2}" in message
    assert read_release_record(Path(str(ctx.state_path)), DEV2_KEY) is None


def test_candidate_refuses_a_second_pin_of_the_same_record(repo: Path) -> None:
    """The edge exists from DRAFT only, so pinning twice is illegal."""
    ctx = context(repo, record=dev2_draft())
    params = params_for(repo)
    asyncio.run(candidate(ctx, params))

    message = refused(ctx, params)

    assert ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION.value in message
    assert stored(ctx).revision == 1


def test_candidate_refuses_an_unauthored_checkpoint(repo: Path) -> None:
    """A rung with no configuration has no target set to freeze."""
    ctx = context(repo, record=dev2_draft())

    message = refused(ctx, params_for(repo, version="0.7.0rc1"))

    assert "no release configuration" in message


def test_candidate_refuses_an_unknown_param(repo: Path) -> None:
    """The params model forbids extras, so a typo never reaches the freeze."""
    ctx = context(repo, record=dev2_draft())

    with pytest.raises(ValueError, match="receipts"):
        asyncio.run(candidate(ctx, params_for(repo, receipts="./receipts")))


# --- the library contracts ---------------------------------------------------


def test_freeze_manifest_reproduces_the_digest_the_dev2_approval_pinned(tmp_path: Path) -> None:
    """The approved pin must stay reachable from configuration and receipts."""
    manifest = freeze_manifest(
        dev2_config(), receipts_dir=write_published_receipts(tmp_path / "published")
    )

    assert manifest.digest == f"sha256:{APPROVED_DEV2_MANIFEST_SHA}"


def test_freeze_manifest_refuses_a_target_with_no_configured_identity(tmp_path: Path) -> None:
    """An identity invented at freeze time would observe against nothing."""
    config = dev2_config()
    anonymous = tuple(
        target.model_copy(update={"identity": None}) if target.target_id == "npm" else target
        for target in config.targets
    )

    with pytest.raises(ValueError) as excinfo:
        freeze_manifest(
            config.model_copy(update={"targets": anonymous}),
            receipts_dir=write_receipts(tmp_path / "receipts"),
        )

    assert CandidateRefusal.TARGET_IDENTITY_UNDECLARED.value in str(excinfo.value)


def test_freeze_manifest_refuses_a_kind_no_rule_names(tmp_path: Path) -> None:
    """A multi-kind leg whose kinds cannot be told apart is refused."""
    config = dev2_config()
    ambiguous = tuple(
        target.model_copy(
            update={
                "artifact_kinds": (
                    ReleaseArtifactKind.CODEX_PLUGIN,
                    ReleaseArtifactKind.WHEEL,
                )
            }
        )
        if target.target_id == "npm"
        else target
        for target in config.targets
    )

    with pytest.raises(ValueError) as excinfo:
        freeze_manifest(
            config.model_copy(update={"targets": ambiguous}),
            receipts_dir=write_receipts(tmp_path / "receipts"),
        )

    assert CandidateRefusal.FROZEN_ARTIFACT_UNRESOLVED.value in str(excinfo.value)


def test_freeze_manifest_refuses_a_kind_the_receipt_never_reported(tmp_path: Path) -> None:
    """A declared source-host asset nobody uploaded is missing, not skipped."""
    directory = write_receipts(tmp_path / "receipts")
    payload = dict(RECEIPTS["github"])
    digests = dict(payload["artifact_digests"])
    digests.pop("SHA256SUMS")
    payload["artifact_digests"] = digests
    (directory / "publication-receipt-github.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(ValueError) as excinfo:
        freeze_manifest(dev2_config(), receipts_dir=directory)

    assert CandidateRefusal.FROZEN_ARTIFACT_MISSING.value in str(excinfo.value)
    assert "checksums" in str(excinfo.value)


def test_freeze_manifest_refuses_a_receipt_that_does_not_validate(tmp_path: Path) -> None:
    """A malformed receipt is a broken publisher, never an absent leg."""
    directory = write_receipts(tmp_path / "receipts")
    (directory / "publication-receipt-pypi.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="does not validate"):
        freeze_manifest(dev2_config(), receipts_dir=directory)


def test_manifest_complete_is_false_without_every_required_target(tmp_path: Path) -> None:
    """The guard predicate reads the required set, not the declared one."""
    config = dev2_config()
    full = freeze_manifest(config, receipts_dir=write_receipts(tmp_path / "receipts"))
    partial = full.model_copy(update={"targets": {"pypi": full.target_for("pypi")}})

    assert manifest_complete(config, full) is True
    assert manifest_complete(config, partial) is False


def test_manifest_complete_is_false_for_another_version(tmp_path: Path) -> None:
    """A manifest freezing a different version completes nothing here."""
    config = dev2_config()
    full = freeze_manifest(config, receipts_dir=write_receipts(tmp_path / "receipts"))

    assert manifest_complete(config, full.model_copy(update={"version": "0.7.0"})) is False


def test_pin_candidate_denies_an_incomplete_manifest(tmp_path: Path) -> None:
    """The guard is computed, so a partial manifest is denied by name."""
    config = dev2_config()
    full = freeze_manifest(config, receipts_dir=write_receipts(tmp_path / "receipts"))
    partial = full.model_copy(update={"targets": {"pypi": full.target_for("pypi")}})

    with pytest.raises(ReleaseTransitionError) as excinfo:
        pin_candidate(
            dev2_draft(),
            config,
            partial,
            source_sha="a" * 40,
            source_tree_sha="b" * 40,
            manifest_ref="manifest://partial",
        )

    assert excinfo.value.code is ReleaseDenialCode.RELEASE_MANIFEST_INCOMPLETE


def test_pin_candidate_refuses_a_manifest_frozen_for_another_release(tmp_path: Path) -> None:
    """A manifest keyed elsewhere cannot pin this record."""
    config = dev2_config()
    manifest = freeze_manifest(config, receipts_dir=write_receipts(tmp_path / "receipts"))
    other = manifest.model_copy(update={"release_key": DEV1_KEY, "version": "0.7.0.dev1"})

    with pytest.raises(ValueError, match="cannot pin release"):
        pin_candidate(
            dev2_draft(),
            config,
            other,
            source_sha="a" * 40,
            source_tree_sha="b" * 40,
            manifest_ref="manifest://other",
        )


def test_resolve_source_tree_refuses_a_directory_that_is_not_a_repository(
    tmp_path: Path,
) -> None:
    """No repository is the same answer as no commit: a named refusal."""
    with pytest.raises(ValueError) as excinfo:
        resolve_source_tree(tmp_path, "a" * 40)

    assert CandidateRefusal.SOURCE_TREE_UNRESOLVED.value in str(excinfo.value)
