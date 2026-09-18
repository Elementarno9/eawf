"""The published-branch adapter, and the git plumbing that feeds it.

Under test: the ``git_ref`` leg of the read-back, which is the one
publication target that is not an HTTP registry. The Codex plugin tree
is pushed to a branch, so the thing to read back is a ref and the tree
retained under it, and the same four verdicts have to come out --
match, missing, mismatch, unknown -- off recorded answers rather than a
live remote.

Two seams are exercised separately because they fail differently. The
adapter is a pure function of a recorded answer, so every verdict is
driven from a committed fixture. The reader is the part that shells out
to git, so it is driven through an injected runner that hands back
canned replies: the point there is which commands run, in which order,
and what each non-zero exit maps onto.

The totality check rides along at the end. An adapter enum member with
no implementation or no reader would leave a declared target
unobservable, which is exactly the hole this leg was added to close.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.spec.release_config import (
    ObservationAdapter,
    ReleaseArtifactKind,
    parse_release_config,
)
from eawf.workflow.release.adapters import (
    DEFAULT_REGISTRY_READERS,
    OBSERVATION_ADAPTERS,
    collect_observation,
    observe_git_ref,
)
from eawf.workflow.release.observation import (
    CODE_RESULTS,
    FrozenArtifact,
    FrozenManifest,
    FrozenTarget,
    ObservationCode,
    ObservationRequest,
    ObservationResult,
    PublicationObservation,
    RecordedResponse,
    evidence_reference,
    observation_request,
)
from eawf.workflow.release.registry_readers import (
    GIT_REF_FIELD,
    GIT_TIP_FIELD,
    GIT_TREE_DIGESTS_FIELD,
    PLUGINS_DIST_REF,
    SOURCE_HOST_REPOSITORY_FIELD,
    GitRefReader,
    GitReply,
    plugins_dist_path,
    source_host_clone_url,
    tree_listing_digest,
)
from eawf.workflow.release.train import DEV3_RELEASE_CONFIG_YAML

pytestmark = pytest.mark.unit

#: The leg under test, and the first rung that declares it.
TARGET_ID = "plugins-dist"
VERSION = "0.7.0.dev3"

#: The ``owner/repo`` the publication branch lives on.
IDENTITY = "Elementarno9/eawf"

#: The retained path the version's tree is published at.
TREE_PATH = f"versions/{VERSION}/plugins/eawf"

#: The tree digest the manifest freezes, and the one a mismatching
#: branch exposes instead. Both are the fixtures' literals.
FROZEN_TREE_DIGEST = "sha256:" + (
    "8888888888888888888888888888888888888888888888888888888888888888"  # pragma: allowlist secret
)
REPUBLISHED_TREE_DIGEST = "sha256:" + (
    "9999999999999999999999999999999999999999999999999999999999999999"  # pragma: allowlist secret
)

#: Instant every judgement in this module is stamped with.
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

#: Committed registry answers, recorded per verdict.
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "release" / "observations"

#: The result each recorded case is expected to reach.
CASE_RESULTS = {
    "match": ObservationResult.MATCH,
    "missing": ObservationResult.MISSING,
    "mismatch": ObservationResult.MISMATCH,
    "unknown": ObservationResult.UNKNOWN,
}

#: The commit ``ls-remote`` resolves the branch to in the fake remote.
TIP = "1111111111111111111111111111111111111111"

#: One ``ls-tree -r`` listing of a published plugin tree.
TREE_LISTING = (
    b"100644 blob 1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a\t"
    b"versions/0.7.0.dev3/plugins/eawf/plugin.json\n"
    b"100755 blob 2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b\t"
    b"versions/0.7.0.dev3/plugins/eawf/hooks/pre-turn\n"
)


def frozen_manifest() -> FrozenManifest:
    """Return a manifest freezing the plugin tree at its published path."""
    return FrozenManifest(
        release_key=f"REL-{VERSION}",
        version=VERSION,
        targets={
            TARGET_ID: FrozenTarget(
                identity=IDENTITY,
                artifacts=(
                    FrozenArtifact(
                        kind=ReleaseArtifactKind.CODEX_PLUGIN,
                        filename=TREE_PATH,
                        digest=FROZEN_TREE_DIGEST,
                    ),
                ),
            )
        },
    )


def read_back_request() -> ObservationRequest:
    """Return the request the ``plugins-dist`` leg of ``dev3`` is judged on."""
    config = parse_release_config(DEV3_RELEASE_CONFIG_YAML)
    return observation_request(config, frozen_manifest(), target_id=TARGET_ID)


def recorded(case: str) -> RecordedResponse:
    """Return the committed branch answer for *case*."""
    body = json.loads((FIXTURES / f"git_ref-{case}.json").read_text(encoding="utf-8"))
    return RecordedResponse.model_validate(body)


def answered(payload: Mapping[str, object] | None) -> RecordedResponse:
    """Return a ``200`` answer carrying *payload*."""
    return RecordedResponse(status=200, payload=payload)


def observe(response: RecordedResponse) -> PublicationObservation:
    """Return the observation *response* supports for the leg under test."""
    return observe_git_ref(read_back_request(), response, observed_at=NOW)


@dataclass
class FakeGit:
    """A :class:`GitRunner` that answers by subcommand.

    Attributes:
        replies: Reply per git subcommand; an unlisted subcommand
            succeeds with no output.
        calls: Every argv the reader ran, in order.
    """

    replies: Mapping[str, GitReply]
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str]) -> GitReply:
        """Record *argv* and return the reply its subcommand declares."""
        self.calls.append(list(argv))
        return self.replies.get(argv[0], GitReply(exit_code=0))


def published_remote(**overrides: GitReply) -> FakeGit:
    """Return a runner standing in for a remote carrying the tree."""
    replies = {
        "ls-remote": GitReply(exit_code=0, stdout=f"{TIP}\t{PLUGINS_DIST_REF}\n".encode()),
        "fetch": GitReply(exit_code=0),
        "ls-tree": GitReply(exit_code=0, stdout=TREE_LISTING),
    }
    replies.update(overrides)
    return FakeGit(replies=replies)


# --- the four recorded verdicts ------------------------------------------


@pytest.mark.parametrize("case", sorted(CASE_RESULTS))
def test_observe_git_ref_records_every_verdict(case: str) -> None:
    observation = observe(recorded(case))
    assert observation.result is CASE_RESULTS[case]
    assert CODE_RESULTS[observation.code] is observation.result
    assert observation.detail.strip()
    assert observation.adapter is ObservationAdapter.GIT_REF


def test_observe_git_ref_reports_the_tree_digest_it_read_back() -> None:
    response = recorded("match")
    observation = observe(response)
    assert observation.code is ObservationCode.MATCHED
    assert observation.observed_digests == {TREE_PATH: FROZEN_TREE_DIGEST}
    assert observation.queried_identity == IDENTITY
    assert observation.version == VERSION
    assert observation.adapter_digest == response.digest
    assert observation.evidence_ref == evidence_reference(
        adapter=ObservationAdapter.GIT_REF,
        target_id=TARGET_ID,
        identity=IDENTITY,
        version=VERSION,
        adapter_digest=response.digest,
    )


def test_observe_git_ref_names_the_version_path_the_branch_does_not_carry() -> None:
    observation = observe(recorded("missing"))
    assert observation.code is ObservationCode.VERSION_ABSENT
    assert observation.observed_digests == {}
    assert TREE_PATH in observation.detail


def test_observe_git_ref_names_the_digest_that_contradicts_the_manifest() -> None:
    observation = observe(recorded("mismatch"))
    assert observation.code is ObservationCode.DIGEST_MISMATCH
    assert REPUBLISHED_TREE_DIGEST in observation.detail
    assert FROZEN_TREE_DIGEST in observation.detail


def test_observe_git_ref_settles_nothing_on_an_unanswered_read() -> None:
    observation = observe(recorded("unknown"))
    assert observation.code is ObservationCode.REGISTRY_UNREACHABLE
    assert observation.result is ObservationResult.UNKNOWN


# --- the verdicts a recorded shape cannot carry ---------------------------


def test_observe_git_ref_refuses_an_answer_about_another_repository() -> None:
    payload = dict(recorded("match").payload or {})
    payload[SOURCE_HOST_REPOSITORY_FIELD] = "someone-else/eawf"
    observation = observe(answered(payload))
    assert observation.code is ObservationCode.IDENTITY_MISMATCH
    assert "someone-else/eawf" in observation.detail


def test_observe_git_ref_refuses_a_tree_listed_off_another_ref() -> None:
    """A tree on some other ref is a tree no Codex install resolves."""
    payload = dict(recorded("match").payload or {})
    payload[GIT_REF_FIELD] = "refs/heads/main"
    observation = observe(answered(payload))
    assert observation.code is ObservationCode.IDENTITY_MISMATCH
    assert PLUGINS_DIST_REF in observation.detail


def test_observe_git_ref_reports_an_absent_branch_as_a_missing_version() -> None:
    observation = observe(RecordedResponse(status=404))
    assert observation.code is ObservationCode.VERSION_ABSENT
    assert observation.result is ObservationResult.MISSING


def test_observe_git_ref_reports_an_answer_with_no_body_as_unreadable() -> None:
    observation = observe(RecordedResponse(status=200))
    assert observation.code is ObservationCode.RESPONSE_UNREADABLE
    assert observation.result is ObservationResult.UNKNOWN


def test_observe_git_ref_ignores_a_tree_row_that_is_not_a_digest() -> None:
    """A row the reader could not have written is skipped, not trusted."""
    payload = dict(recorded("match").payload or {})
    payload[GIT_TREE_DIGESTS_FIELD] = {TREE_PATH: 17}
    observation = observe(answered(payload))
    assert observation.code is ObservationCode.VERSION_ABSENT


def test_collect_observation_routes_the_leg_to_the_git_ref_adapter() -> None:
    observation = collect_observation(
        read_back_request(), response=recorded("match"), observed_at=NOW
    )
    assert observation.adapter is ObservationAdapter.GIT_REF
    assert observation.code is ObservationCode.MATCHED


# --- the reader behind it -------------------------------------------------


def test_git_ref_reader_records_the_ref_tip_and_tree_digest() -> None:
    runner = published_remote()
    response = GitRefReader(runner=runner)(read_back_request())
    assert response.status == 200
    assert response.payload == {
        SOURCE_HOST_REPOSITORY_FIELD: IDENTITY,
        GIT_REF_FIELD: PLUGINS_DIST_REF,
        GIT_TIP_FIELD: TIP,
        GIT_TREE_DIGESTS_FIELD: {TREE_PATH: tree_listing_digest(TREE_LISTING)},
    }
    assert [call[0] for call in runner.calls] == ["ls-remote", "fetch", "ls-tree"]
    assert runner.calls[-1][-1] == TREE_PATH


def test_git_ref_reader_reports_an_absent_branch_as_not_found() -> None:
    runner = published_remote(**{"ls-remote": GitReply(exit_code=0)})
    response = GitRefReader(runner=runner)(read_back_request())
    assert response.status == 404
    assert response.payload is None
    assert [call[0] for call in runner.calls] == ["ls-remote"]


def test_git_ref_reader_reports_an_unreachable_remote_as_unanswered() -> None:
    runner = published_remote(**{"ls-remote": GitReply(exit_code=128)})
    assert GitRefReader(runner=runner)(read_back_request()).status == 0


def test_git_ref_reader_reports_a_failed_fetch_as_unanswered() -> None:
    runner = published_remote(fetch=GitReply(exit_code=128))
    assert GitRefReader(runner=runner)(read_back_request()).status == 0


def test_git_ref_reader_reports_a_failed_listing_as_unanswered() -> None:
    runner = published_remote(**{"ls-tree": GitReply(exit_code=128)})
    assert GitRefReader(runner=runner)(read_back_request()).status == 0


def test_git_ref_reader_reports_an_absent_version_path_as_an_empty_tree() -> None:
    """The branch is there and this version is not on it."""
    runner = published_remote(**{"ls-tree": GitReply(exit_code=0, stdout=b"")})
    response = GitRefReader(runner=runner)(read_back_request())
    assert response.status == 200
    assert (response.payload or {})[GIT_TREE_DIGESTS_FIELD] == {}


def test_git_ref_reader_ignores_a_listing_row_that_names_no_object() -> None:
    noise = GitReply(exit_code=0, stdout=b"warning: redirecting\n")
    runner = published_remote(**{"ls-remote": noise})
    assert GitRefReader(runner=runner)(read_back_request()).status == 404


def test_git_ref_reader_refuses_an_identity_that_is_not_a_repository_pair() -> None:
    request = read_back_request()
    bare = ObservationRequest(
        target=request.target,
        version=request.version,
        identity="eawf",
        artifacts=request.artifacts,
        manifest_digest=request.manifest_digest,
    )
    with pytest.raises(ValueError, match="not an 'owner/repo' pair"):
        GitRefReader(runner=published_remote())(bare)


# --- the derived strings both sides of the comparison share ---------------


def test_plugins_dist_path_names_the_retained_version_tree() -> None:
    assert plugins_dist_path(VERSION) == TREE_PATH


def test_source_host_clone_url_builds_the_https_remote() -> None:
    assert source_host_clone_url(IDENTITY) == f"https://github.com/{IDENTITY}.git"


@pytest.mark.parametrize("identity", ["", "eawf", "owner/..", "owner/repo/extra"])
def test_source_host_clone_url_refuses_anything_but_a_repository_pair(identity: str) -> None:
    with pytest.raises(ValueError, match="not an 'owner/repo' pair"):
        source_host_clone_url(identity)


def test_tree_listing_digest_hashes_the_listing_bytes() -> None:
    assert tree_listing_digest(TREE_LISTING).startswith("sha256:")
    assert tree_listing_digest(TREE_LISTING) != tree_listing_digest(TREE_LISTING + b"\n")
    assert tree_listing_digest(b"") == tree_listing_digest(b"")


# --- totality -------------------------------------------------------------


def test_the_adapter_tables_stay_total_with_the_git_ref_leg() -> None:
    assert set(OBSERVATION_ADAPTERS) == set(ObservationAdapter)
    assert set(DEFAULT_REGISTRY_READERS) == set(ObservationAdapter)
    assert OBSERVATION_ADAPTERS[ObservationAdapter.GIT_REF] is observe_git_ref
    assert isinstance(DEFAULT_REGISTRY_READERS[ObservationAdapter.GIT_REF], GitRefReader)
