"""A release's merge and tag facts come from the remote, never from state.

A Milestone is accepted before its pull request merges, so acceptance
cannot wait on a merge, a tag or a publication; those are observed on the
remote afterwards. Each case drives a real repository under ``tmp_path``
whose ``origin`` is a local bare repository, so nothing reaches a
network: the bare remote starts empty or behind, is pushed to, and is
moved away to stand for a remote that does not answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.state.epoch2.transitions import (
    OBSERVED_GUARD_FACTS,
    TRANSITION_ROWS,
    LifecycleEntity,
    TransitionGuard,
)
from eawf.runtime.daemon import methods
from eawf.surfaces.cli.app import app
from eawf.workflow.release.remote_facts import (
    FACTS_CACHE_DIRNAME,
    FactsSource,
    RemoteUnreachableError,
    derive_remote_facts,
    facts_cache_path,
    show_remote_facts,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    APPROVAL_URN,
    MILESTONE_URN,
    acceptance_bundle_row,
    approval_rows,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

VERSION = "0.7.0rc2"
TAG = f"v{VERSION}"
GENERATION_FILE = ".ea/generations/gen-0001/state.json"


def _git(repo: Path, *args: str) -> str:
    """Run one git command inside *repo* and return its stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30
    ).stdout


def _commit(repo: Path, relative: str, text: str) -> str:
    """Write *relative*, commit it and return the new commit id."""
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", f"change {relative}")
    return _git(repo, "rev-parse", "HEAD").strip()


def _generation_digests(repo: Path) -> dict[str, str]:
    """Return the sha256 of every file under ``.ea/generations``."""
    root = repo / ".ea" / "generations"
    return {
        str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """Return an empty bare repository standing in for ``origin``."""
    path = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "--initial-branch", "main", str(path)],
        check=True,
        timeout=30,
    )
    return path


@pytest.fixture
def repo(tmp_path: Path, remote: Path) -> Path:
    """Return a checkout tracking committed state, with *remote* as ``origin``."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "--quiet", "--initial-branch", "main")
    _git(path, "config", "user.name", "EAWF Test")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "commit.gpgSign", "false")
    _git(path, "config", "tag.gpgSign", "false")
    _git(path, "config", "core.hooksPath", ".git/hooks")
    _git(path, "remote", "add", "origin", str(remote))
    (path / ".gitignore").write_text(".ea/local/\n", encoding="utf-8")
    _commit(path, GENERATION_FILE, '{"milestone": "COMPLETED"}\n')
    return path


# ---- acceptance never waits on a post-merge fact ---------------------------


def test_no_milestone_edge_is_guarded_by_an_observed_fact() -> None:
    """Every observed-only guard sits on a Batch edge, none on a Milestone's."""
    milestone_guards = {
        guard
        for row in TRANSITION_ROWS
        if row.entity is LifecycleEntity.MILESTONE
        for guard in row.guards
    }
    assert TransitionGuard.REQUIRED_BATCHES_COMPLETED in milestone_guards
    assert milestone_guards.isdisjoint(OBSERVED_GUARD_FACTS)


def test_a_milestone_is_accepted_with_no_remote_and_no_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance commits in a tree with no remote, tag or release record at all."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    canary = provision(tmp_path / "accept", code="ACCEPT")
    seed(
        canary,
        {"milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")}, **approval_rows()},
    )
    assert not (canary.root / ".ea" / "store" / "release_record.jsonl").exists()
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": MILESTONE_URN,
        "expected_revision": 1,
        "idempotency_key": "req-accept-0445",
        "actor": "OP-0001",
        "updates": {"accepted_binding": seed_row("milestone", "COMPLETED")["accepted_binding"]},
        "acceptance_bundle": acceptance_bundle_row(),
        "approval_receipt_ref": APPROVAL_URN,
    }

    answer = asyncio.run(
        methods.dispatch("domain.milestone.accept", method_context(tmp_path / "rt"), params)
    )

    assert answer["errors"] == []
    assert answer["result"]["event_name"] == "domain.milestone.completed"


# ---- facts derived from the remote ------------------------------------------


def test_an_empty_remote_is_untagged_and_its_merge_undecided(repo: Path) -> None:
    source = _git(repo, "rev-parse", "HEAD").strip()

    facts = derive_remote_facts(repo, version=VERSION, source_sha=source)

    assert facts.source is FactsSource.REMOTE
    assert facts.default_branch is None
    assert (facts.merged, facts.tagged, facts.tag_commit) == (None, False, None)


def test_facts_move_from_unmerged_and_untagged_to_merged_and_tagged(repo: Path) -> None:
    _git(repo, "push", "--quiet", "origin", "main")
    source = _commit(repo, "src/feature.py", "VALUE = 1\n")
    before = _generation_digests(repo)

    unmerged = derive_remote_facts(repo, version=VERSION, source_sha=source)
    assert unmerged.default_branch == "main"
    assert (unmerged.merged, unmerged.tagged) == (False, False)

    _git(repo, "tag", "--annotate", TAG, "--message", f"Release {TAG}", source)
    _git(repo, "push", "--quiet", "origin", "main", f"refs/tags/{TAG}")
    merged = derive_remote_facts(repo, version=VERSION, source_sha=source)

    assert (merged.merged, merged.tagged) == (True, True)
    assert merged.tag == TAG
    assert merged.tag_commit == source
    assert merged.source is FactsSource.REMOTE
    assert merged.observed_at.tzinfo is not None
    assert _generation_digests(repo) == before
    assert _git(repo, "status", "--porcelain") == ""


def test_a_tag_on_another_version_does_not_tag_this_one(repo: Path) -> None:
    _git(repo, "tag", "--annotate", "v0.7.0rc1", "--message", "rc1")
    _git(repo, "push", "--quiet", "origin", "main", "refs/tags/v0.7.0rc1")

    facts = derive_remote_facts(repo, version=VERSION, source_sha=None)

    assert (facts.tagged, facts.tag_commit) == (False, None)
    assert facts.merged is None


def test_a_lightweight_tag_counts_as_tagged(repo: Path) -> None:
    source = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "tag", TAG)
    _git(repo, "push", "--quiet", "origin", "main", f"refs/tags/{TAG}")

    facts = derive_remote_facts(repo, version=VERSION, source_sha=source)

    assert (facts.merged, facts.tagged, facts.tag_commit) == (True, True, source)


def test_a_source_commit_missing_locally_is_undecided(repo: Path) -> None:
    _git(repo, "push", "--quiet", "origin", "main")

    facts = derive_remote_facts(repo, version=VERSION, source_sha="f" * 40)

    assert facts.merged is None


# ---- the cache: fallback only, local only ------------------------------------


def test_an_unreachable_remote_answers_from_the_cache_and_says_so(repo: Path, remote: Path) -> None:
    source = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "push", "--quiet", "origin", "main")
    live = derive_remote_facts(repo, version=VERSION, source_sha=source)
    remote.rename(remote.with_name("moved.git"))

    cached = derive_remote_facts(repo, version=VERSION, source_sha=source)

    assert cached.source is FactsSource.CACHE
    assert cached.observed_at == live.observed_at
    assert (cached.merged, cached.tagged) == (True, False)
    assert facts_cache_path(repo, VERSION).is_relative_to(repo / FACTS_CACHE_DIRNAME)
    assert _git(repo, "status", "--porcelain") == ""


def test_an_unreachable_remote_with_no_cache_refuses(repo: Path, remote: Path) -> None:
    remote.rename(remote.with_name("moved.git"))

    with pytest.raises(RemoteUnreachableError, match=VERSION):
        derive_remote_facts(repo, version=VERSION, source_sha=None)

    assert not facts_cache_path(repo, VERSION).exists()


def test_a_live_answer_is_preferred_over_the_cache(repo: Path) -> None:
    _git(repo, "push", "--quiet", "origin", "main")
    derive_remote_facts(repo, version=VERSION, source_sha=None)
    _git(repo, "tag", TAG)
    _git(repo, "push", "--quiet", "origin", f"refs/tags/{TAG}")

    facts = derive_remote_facts(repo, version=VERSION, source_sha=None)

    assert (facts.source, facts.tagged) == (FactsSource.REMOTE, True)
    assert json.loads(facts_cache_path(repo, VERSION).read_text())["tagged"] is True


def test_a_malformed_cache_is_refused_at_load(repo: Path, remote: Path) -> None:
    path = facts_cache_path(repo, VERSION)
    path.parent.mkdir(parents=True)
    path.write_text('{"version": "0.7.0rc2", "unexpected": true}', encoding="utf-8")
    remote.rename(remote.with_name("moved.git"))

    with pytest.raises(ValidationError):
        derive_remote_facts(repo, version=VERSION, source_sha=None)


# ---- boundary and error inputs ------------------------------------------------


@pytest.mark.parametrize("version", ["", "0.7", "v0.7.0", "0.7.0-rc2", "../0.7.0"])
def test_an_unknown_version_shape_is_refused_before_any_read(repo: Path, version: str) -> None:
    with pytest.raises(ValueError, match="not a train version"):
        derive_remote_facts(repo, version=version, source_sha=None)

    assert not (repo / FACTS_CACHE_DIRNAME).exists()


def test_a_version_never_released_is_untagged(repo: Path) -> None:
    _git(repo, "push", "--quiet", "origin", "main")

    facts = derive_remote_facts(repo, version="9.9.9", source_sha=None)

    assert (facts.version, facts.tag, facts.tagged) == ("9.9.9", "v9.9.9", False)


def test_a_malformed_source_commit_is_refused(repo: Path) -> None:
    with pytest.raises(ValidationError):
        derive_remote_facts(repo, version=VERSION, source_sha="not-a-sha")


# ---- what ``release show`` reports, and from which tree -----------------------


def _show_answer(**record: Any) -> dict[str, Any]:
    """Return a ``release.show`` answer for :data:`VERSION`'s rung."""
    return {
        "checkpoint": {"version": VERSION, "release_key": f"REL-{VERSION}"},
        "record": record or None,
    }


class _ShowDaemon:
    """A ``DaemonClient`` stand-in that answers ``release.show`` for the rung."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    def __enter__(self) -> _ShowDaemon:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Answer the show of :data:`VERSION`'s unopened rung."""
        assert method == "release.show"
        return _show_answer()


def test_show_reads_the_record_source_commit(repo: Path) -> None:
    source = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "push", "--quiet", "origin", "main")

    facts = show_remote_facts(repo, _show_answer(source_sha=source))

    assert facts is not None
    assert (facts["version"], facts["source_sha"], facts["merged"]) == (VERSION, source, True)


def test_show_of_an_unopened_rung_has_no_source_commit(repo: Path) -> None:
    facts = show_remote_facts(repo, _show_answer())

    assert facts is not None
    assert (facts["source_sha"], facts["merged"], facts["source"]) == (None, None, "remote")


@pytest.mark.parametrize(
    "answer",
    [{}, {"checkpoint": None}, {"checkpoint": {}}, {"checkpoint": {"release_key": 7}}],
    ids=["no-checkpoint", "null-checkpoint", "empty-checkpoint", "non-string-key"],
)
def test_show_with_no_checkpoint_reports_null_and_reads_nothing(
    repo: Path, answer: dict[str, Any]
) -> None:
    assert show_remote_facts(repo, answer) is None
    assert not (repo / FACTS_CACHE_DIRNAME).exists()


def test_show_with_an_unreachable_remote_and_no_cache_says_unavailable(
    repo: Path, remote: Path
) -> None:
    remote.rename(remote.with_name("moved.git"))

    facts = show_remote_facts(repo, _show_answer())

    assert facts is not None
    assert facts["source"] == "unavailable"
    assert VERSION in str(facts["detail"])


def test_show_with_a_non_train_version_is_refused(repo: Path) -> None:
    with pytest.raises(ValueError, match="not a train version"):
        show_remote_facts(repo, {"checkpoint": {"release_key": "REL-0.7"}, "record": None})


def test_release_show_reads_and_caches_in_the_named_workspace(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--workspace`` names the tree whose ``origin`` is read and where the cache lands."""
    _git(repo, "push", "--quiet", "origin", "main")
    _git(repo, "tag", TAG)
    _git(repo, "push", "--quiet", "origin", f"refs/tags/{TAG}")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "--quiet", "--initial-branch", "main")
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _ShowDaemon)

    result = CliRunner().invoke(
        app, ["--json", "--workspace", str(repo), "release", "show", VERSION]
    )

    assert result.exit_code == 0, result.output
    assert '"tagged":true' in result.output.replace(" ", "")
    cached = json.loads(facts_cache_path(repo, VERSION).read_text())
    assert (cached["tagged"], cached["tag_commit"]) == (True, _git(repo, "rev-parse", TAG).strip())
    assert not (elsewhere / FACTS_CACHE_DIRNAME).exists()
    assert _git(repo, "status", "--porcelain") == ""
