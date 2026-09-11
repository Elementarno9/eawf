"""The ``.ea/`` commit-policy census, and proof that it reds on a real defect.

Three groups. The classification group drives
:func:`~eawf.kernel.store.commit_policy.classify_path` over its boundary
and error paths. The tree group asserts the live repository agrees with
the declaration. The gate-fires group builds a throwaway repository per
case and runs ``tools/ea_commit_census.py`` against it as a subprocess,
so each failure mode is proven end to end against real git rather than
against a hand-built finding list: an undeclared path staged under
``.ea/``, a declared-committed path an ignore rule matches, a
declared-committed path nothing tracks, and a declared not-committed
path force-added into the index.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.store.commit_census import run_census
from eawf.kernel.store.commit_policy import (
    EA_PATH_CLASSES,
    CensusFindingKind,
    CommitPolicy,
    CommitPolicyError,
    PathClass,
    UndeclaredPathError,
    census_findings,
    classify_path,
    probe_paths,
)
from eawf.kernel.store.tiers import StorageTier
from tools.ea_commit_census import GIT_UNAVAILABLE_EXIT

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CENSUS_TOOL = _REPO_ROOT / "tools" / "ea_commit_census.py"


def _isolated_git_env() -> dict[str, str]:
    """Return an environment where the operator's git config cannot leak in."""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }


def _git(repo: Path, *args: str) -> None:
    """Run one git command in *repo* against an isolated config."""
    subprocess.run(["git", *args], cwd=repo, env=_isolated_git_env(), check=True)


def _seed_repo(tmp_path: Path) -> Path:
    """Build a minimal repository that satisfies the declaration.

    Args:
        tmp_path: The pytest-provided scratch directory.

    Returns:
        The repository root, with the real ``.gitignore`` copied in and
        every ``must_exist`` row present and staged.
    """
    repo = tmp_path / "tree"
    (repo / ".ea").mkdir(parents=True)
    _git(repo, "init", "--quiet")
    (repo / ".gitignore").write_text(
        (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "AGENTS.md").write_text("# contract\n", encoding="utf-8")
    (repo / ".ea" / "state.json").write_text("{}\n", encoding="utf-8")
    (repo / ".ea" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "AGENTS.md", ".ea/state.json", ".ea/config.yaml")
    return repo


def _run_tool(repo: Path) -> subprocess.CompletedProcess[str]:
    """Run the census script against *repo* and capture its output."""
    return subprocess.run(
        [sys.executable, str(_CENSUS_TOOL), "--repo-root", str(repo)],
        cwd=_REPO_ROOT,
        env=_isolated_git_env(),
        capture_output=True,
        text=True,
        check=False,
    )


# --- classification: pass paths --------------------------------------------


def test_classify_path_document_is_committed() -> None:
    row = classify_path(".ea/state.json")
    assert row.policy is CommitPolicy.COMMITTED
    assert row.tier is StorageTier.DOCUMENT
    assert row.must_exist


def test_classify_path_firehose_beats_the_store_family() -> None:
    """The literal event row wins over ``.ea/store/*.jsonl`` on specificity."""
    assert classify_path(".ea/store/event.jsonl").policy is CommitPolicy.NOT_COMMITTED
    assert classify_path(".ea/store/audit.jsonl").policy is CommitPolicy.COMMITTED


def test_classify_path_epoch2_ledger_is_committed() -> None:
    row = classify_path(".ea/ledger/task.jsonl")
    assert row.policy is CommitPolicy.COMMITTED
    assert row.tier is StorageTier.LEDGER


def test_classify_path_derived_index_is_not_committed() -> None:
    row = classify_path(".ea/indexes/task.index.json")
    assert row.policy is CommitPolicy.NOT_COMMITTED
    assert row.tier is StorageTier.DERIVED


def test_classify_path_render_cache_beats_the_artifact_family() -> None:
    authored = classify_path(".ea/artifacts/audits/2026-09-10-a.md")
    cached = classify_path(".ea/artifacts/rendered/memory/scope.md")
    assert authored.policy is CommitPolicy.COMMITTED
    assert cached.policy is CommitPolicy.NOT_COMMITTED


def test_classify_path_folds_back_slashes() -> None:
    row = classify_path(".ea\\artifacts\\audits\\2026-09-10-a.md")
    assert row.policy is CommitPolicy.COMMITTED


def test_classify_path_single_star_does_not_cross_a_separator() -> None:
    """``.ea/store/*.jsonl`` must not claim a nested file."""
    with pytest.raises(UndeclaredPathError):
        classify_path(".ea/store/nested/deep.jsonl")


# --- classification: error paths -------------------------------------------


def test_classify_path_undeclared_directory_raises() -> None:
    with pytest.raises(UndeclaredPathError, match="matches no declared row"):
        classify_path(".ea/mystery/thing.bin")


def test_classify_path_outside_the_surface_raises() -> None:
    with pytest.raises(UndeclaredPathError):
        classify_path("src/eawf/kernel/store/commit_policy.py")


def test_classify_path_empty_string_raises() -> None:
    with pytest.raises(UndeclaredPathError):
        classify_path("")


def test_classify_path_ambiguous_rows_raise() -> None:
    """Two equally specific rows that disagree are a declaration bug."""
    left = PathClass(pattern=".ea/x/*.json", policy=CommitPolicy.COMMITTED, note="left")
    right = PathClass(pattern=".ea/*/a.json", policy=CommitPolicy.NOT_COMMITTED, note="right")
    assert left.specificity == right.specificity
    with pytest.raises(CommitPolicyError, match="disagreeing rows"):
        classify_path(".ea/x/a.json", classes=(left, right))


def test_path_class_rejects_an_absolute_pattern() -> None:
    with pytest.raises(ValidationError, match="repo-relative"):
        PathClass(pattern="/etc/passwd", policy=CommitPolicy.COMMITTED, note="n")


def test_path_class_rejects_a_directory_pattern() -> None:
    with pytest.raises(ValidationError, match="not a dir"):
        PathClass(pattern=".ea/locks/", policy=CommitPolicy.NOT_COMMITTED, note="n")


def test_path_class_rejects_an_upward_walk() -> None:
    with pytest.raises(ValidationError, match="walk upward"):
        PathClass(pattern=".ea/../secrets", policy=CommitPolicy.NOT_COMMITTED, note="n")


def test_path_class_rejects_must_exist_on_a_family() -> None:
    with pytest.raises(ValidationError, match="cannot be must_exist"):
        PathClass(
            pattern=".ea/store/*.jsonl",
            policy=CommitPolicy.COMMITTED,
            must_exist=True,
            note="n",
        )


def test_path_class_rejects_an_empty_note() -> None:
    with pytest.raises(ValidationError):
        PathClass(pattern=".ea/x.json", policy=CommitPolicy.COMMITTED, note="")


def test_path_class_forbids_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        PathClass(
            pattern=".ea/x.json",
            policy=CommitPolicy.COMMITTED,
            note="n",
            surprise=True,  # type: ignore[call-arg]
        )


# --- the declaration is total and self-consistent --------------------------


def test_every_row_probe_classifies_back_to_its_own_row() -> None:
    """Totality: each family's representative resolves to the family."""
    for row in EA_PATH_CLASSES:
        assert classify_path(row.probe) == row, row.pattern


def test_probe_paths_are_unique() -> None:
    probes = probe_paths()
    assert len(probes) == len(EA_PATH_CLASSES)
    assert len(set(probes)) == len(probes)


def test_census_findings_over_an_empty_declaration_is_empty() -> None:
    assert census_findings(tracked=(), ignored_probes=(), classes=()) == ()


def test_census_findings_ignores_paths_outside_the_surface() -> None:
    row = PathClass(pattern=".ea/x.json", policy=CommitPolicy.NOT_COMMITTED, note="n")
    findings = census_findings(
        tracked=("src/eawf/main.py", "README.md"),
        ignored_probes=(".ea/x.json",),
        classes=(row,),
    )
    assert findings == ()


# --- the live tree agrees with the declaration -----------------------------


def test_repo_tree_agrees_with_the_declaration() -> None:
    """The census passes on the tree at this commit."""
    findings = run_census(_REPO_ROOT)
    assert findings == (), "\n".join(finding.render() for finding in findings)


def test_census_tool_exits_zero_on_the_repo_tree() -> None:
    result = _run_tool(_REPO_ROOT)
    assert result.returncode == 0, result.stderr


# --- the gate fires on a real defect ---------------------------------------


def test_seeded_fixture_repo_passes_the_census(tmp_path: Path) -> None:
    """The fixture is only a proof when its clean form is green."""
    result = _run_tool(_seed_repo(tmp_path))
    assert result.returncode == 0, result.stderr


def test_census_reds_on_an_undeclared_staged_path(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    (repo / ".ea" / "mystery").mkdir()
    (repo / ".ea" / "mystery" / "firehose.bin").write_bytes(b"\x00")
    _git(repo, "add", ".ea/mystery/firehose.bin")

    result = _run_tool(repo)
    assert result.returncode == 1
    assert CensusFindingKind.UNDECLARED.value in result.stderr
    assert ".ea/mystery/firehose.bin" in result.stderr


def test_census_reds_when_a_declared_committed_path_is_gitignored(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    with (repo / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("\n.ea/config.yaml\n")

    result = _run_tool(repo)
    assert result.returncode == 1
    assert CensusFindingKind.COMMITTED_BUT_IGNORED.value in result.stderr
    assert ".ea/config.yaml" in result.stderr


def test_census_reds_when_a_required_path_is_absent(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    _git(repo, "rm", "--quiet", "--cached", ".ea/state.json")

    result = _run_tool(repo)
    assert result.returncode == 1
    assert CensusFindingKind.DECLARED_BUT_ABSENT.value in result.stderr
    assert ".ea/state.json" in result.stderr


def test_census_reds_when_the_firehose_is_force_added(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    (repo / ".ea" / "store").mkdir()
    (repo / ".ea" / "store" / "event.jsonl").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "--force", ".ea/store/event.jsonl")

    result = _run_tool(repo)
    assert result.returncode == 1
    assert CensusFindingKind.TRACKED_BUT_NOT_COMMITTED.value in result.stderr


def test_census_tool_exits_two_outside_a_repository(tmp_path: Path) -> None:
    """git cannot be consulted, which is distinct from a policy failure."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    result = _run_tool(outside)
    assert result.returncode == GIT_UNAVAILABLE_EXIT
    assert "ea-commit-census:" in result.stderr
