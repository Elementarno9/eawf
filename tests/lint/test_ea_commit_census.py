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

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.store.commit_census import run_census
from eawf.kernel.store.commit_policy import (
    EA_PATH_CLASSES,
    PROMOTED_ARTIFACTS,
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
from eawf.platform.artifacts.config_defaults_benchmark import DEV1_CONFIG_DEFAULTS_BENCHMARK_PATH
from eawf.workflow.evidence.contract_reprobe import (
    CROSS_PROVIDER_REPROBE_PATH,
    DAEMON_RPC_REPROBE_PATH,
)
from eawf.workflow.evidence.provider_certification import (
    CANARY_EVIDENCE_DIRS,
    CANARY_EVIDENCE_FILENAME,
)
from eawf.workflow.release.canary_receipts import (
    CANARY_RECEIPTS_FILENAME,
    DISPATCH_SMOKE_RECEIPTS_FILENAME,
)
from tools.ea_commit_census import GIT_UNAVAILABLE_EXIT

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CENSUS_TOOL = _REPO_ROOT / "tools" / "ea_commit_census.py"

#: The most artifacts the commit policy may promote into git: the three
#: single-file readers, the one measurement the host facts cite, one canary
#: export per mapped release, and the two receipt files a release reads
#: beside its export.
_PROMOTED_CEILING = 3 + 1 + len(CANARY_EVIDENCE_DIRS) + 2


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
    (repo / ".ea" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "AGENTS.md", ".ea/config.yaml")
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


#: The epoch-1 stores the epoch-2 release train still appends to.
_KEPT_STORES = (
    "release",
    "release_record",
    "release_checkpoint_receipt",
    "release_train_advance",
    "conformance_stage",
)


def test_classify_path_v1_document_is_not_committed() -> None:
    row = classify_path(".ea/state.json")
    assert row.policy is CommitPolicy.NOT_COMMITTED
    assert row.tier is StorageTier.DOCUMENT
    assert not row.must_exist


def test_classify_path_firehose_beats_the_store_family() -> None:
    """The literal event row wins over ``.ea/store/*.jsonl`` on specificity."""
    row = classify_path(".ea/store/event.jsonl")
    assert row.policy is CommitPolicy.NOT_COMMITTED
    assert row.tier is StorageTier.FIREHOSE


@pytest.mark.parametrize("stem", ["audit", "decision", "evidence", "gold_label", "x"])
def test_classify_path_frozen_epoch1_store_is_not_committed(stem: str) -> None:
    row = classify_path(f".ea/store/{stem}.jsonl")
    assert row.pattern == ".ea/store/*.jsonl"
    assert row.policy is CommitPolicy.NOT_COMMITTED
    assert not row.must_exist


@pytest.mark.parametrize("stem", _KEPT_STORES)
def test_classify_path_release_store_is_committed_by_its_own_row(stem: str) -> None:
    row = classify_path(f".ea/store/{stem}.jsonl")
    assert row.pattern == f".ea/store/{stem}.jsonl"
    assert row.policy is CommitPolicy.COMMITTED
    assert row.tier is StorageTier.LEDGER
    # A product repository may never have released, so none is required.
    assert not row.must_exist


def test_kept_store_rows_are_exactly_the_release_and_conformance_stores() -> None:
    kept = {
        row.pattern
        for row in EA_PATH_CLASSES
        if row.pattern.startswith(".ea/store/") and row.policy is CommitPolicy.COMMITTED
    }
    assert kept == {f".ea/store/{stem}.jsonl" for stem in _KEPT_STORES}


def test_classify_path_kept_store_neighbour_stays_local() -> None:
    """A near-miss of a kept store name falls to the frozen family."""
    assert classify_path(".ea/store/xrelease.jsonl").policy is CommitPolicy.NOT_COMMITTED


@pytest.mark.parametrize("path", [".ea/store/release.jsonl.bak", ".ea/store/release.json"])
def test_classify_path_kept_store_variant_is_undeclared(path: str) -> None:
    with pytest.raises(UndeclaredPathError):
        classify_path(path)


def test_classify_path_epoch2_ledger_is_committed() -> None:
    row = classify_path(".ea/ledger/task.jsonl")
    assert row.policy is CommitPolicy.COMMITTED
    assert row.tier is StorageTier.LEDGER


def test_classify_path_derived_index_is_not_committed() -> None:
    row = classify_path(".ea/indexes/task.index.json")
    assert row.policy is CommitPolicy.NOT_COMMITTED
    assert row.tier is StorageTier.DERIVED


def test_classify_path_artifact_is_local_by_default() -> None:
    assert classify_path(".ea/artifacts/audits/x.md").policy is CommitPolicy.NOT_COMMITTED
    assert classify_path(".ea/artifacts/x.md").policy is CommitPolicy.NOT_COMMITTED
    nested = ".ea/artifacts/research/long-term/2026-05-16-c01-foundations.md"
    assert classify_path(nested).policy is CommitPolicy.NOT_COMMITTED


def test_classify_path_render_cache_stays_not_committed() -> None:
    cached = classify_path(".ea/artifacts/rendered/memory/scope.md")
    assert cached.policy is CommitPolicy.NOT_COMMITTED
    assert cached.tier is StorageTier.DERIVED


@pytest.mark.parametrize("row", PROMOTED_ARTIFACTS, ids=lambda row: row.pattern)
def test_classify_path_promoted_artifact_is_committed(row: PathClass) -> None:
    assert classify_path(row.pattern) == row
    assert row.policy is CommitPolicy.COMMITTED
    # Product repositories share the declaration, so no promoted file is required.
    assert not row.must_exist


@pytest.mark.parametrize("row", PROMOTED_ARTIFACTS, ids=lambda row: row.pattern)
def test_classify_path_promoted_artifact_sibling_stays_local(row: PathClass) -> None:
    """Promotion is per file: a neighbour in the same directory is not carried."""
    directory, _, name = row.pattern.rpartition("/")
    for sibling in (f"{directory}/x-{name}", f"{row.pattern}.bak", f"{directory}/sub/{name}"):
        assert classify_path(sibling).policy is CommitPolicy.NOT_COMMITTED, sibling


def test_promoted_artifacts_stay_within_the_ceiling() -> None:
    assert 0 < len(PROMOTED_ARTIFACTS) <= _PROMOTED_CEILING
    assert len({row.pattern for row in PROMOTED_ARTIFACTS}) == len(PROMOTED_ARTIFACTS)


@pytest.mark.parametrize("row", PROMOTED_ARTIFACTS, ids=lambda row: row.pattern)
def test_promoted_artifact_note_names_a_src_reader_of_it(row: PathClass) -> None:
    """Each promotion is justified by a source module that names the path."""
    readers = [word for word in row.note.split() if word.startswith("src/")]
    assert readers == [_READERS[row.pattern]], row.note
    assert (_REPO_ROOT / readers[0]).is_file(), readers[0]


#: The host facts file, whose certified evidence cites measurements by path and digest.
_HOST_FACTS = _REPO_ROOT / "src/eawf/platform/rules/data/host_facts.yaml"

#: Each ``(<path>, sha256:<hex>)`` citation in a host fact's evidence prose.
_CITATION = re.compile(r"(\.ea/artifacts/\S+?),\s+sha256:([0-9a-f]{64})")


def _host_fact_citations() -> dict[str, str]:
    """Return each artifact path the host facts cite, keyed to the digest cited for it."""
    facts = yaml.safe_load(_HOST_FACTS.read_text(encoding="utf-8"))
    cited: dict[str, str] = {}
    for runtime in facts.values():
        for fact in runtime.values() if isinstance(runtime, dict) else ():
            evidence = fact.get("evidence", "") if isinstance(fact, dict) else ""
            cited.update(_CITATION.findall(evidence))
    return cited


_HOST_FACT_CITATIONS = _host_fact_citations()


def test_host_facts_cite_only_committed_artifacts_whose_digest_matches() -> None:
    """A certified fact's evidence must reach a clone byte for byte, or it proves nothing."""
    assert _HOST_FACT_CITATIONS, "the claude cap cites its measurement"
    for path, digest in _HOST_FACT_CITATIONS.items():
        assert classify_path(path).policy is CommitPolicy.COMMITTED, path
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", path],
            cwd=_REPO_ROOT,
            capture_output=True,
            check=False,
            env=_isolated_git_env(),
        )
        assert tracked.returncode == 0, f"{path} is not committed"
        held = hashlib.sha256((_REPO_ROOT / path).read_bytes()).hexdigest()
        assert held == digest, path


#: Each promoted path, keyed to the source module whose constant names it.
_READERS = {
    DEV1_CONFIG_DEFAULTS_BENCHMARK_PATH.as_posix(): (
        "src/eawf/platform/artifacts/config_defaults_benchmark.py"
    ),
    CROSS_PROVIDER_REPROBE_PATH: "src/eawf/workflow/evidence/contract_reprobe.py",
    DAEMON_RPC_REPROBE_PATH: "src/eawf/workflow/evidence/contract_reprobe.py",
    **{
        "/".join((*directory, CANARY_EVIDENCE_FILENAME)): (
            "src/eawf/workflow/evidence/provider_certification.py"
        )
        for directory in CANARY_EVIDENCE_DIRS.values()
    },
    "/".join((*CANARY_EVIDENCE_DIRS["REL-0.7.0.dev5"], CANARY_RECEIPTS_FILENAME)): (
        "src/eawf/workflow/release/canary_receipts.py"
    ),
    "/".join((*CANARY_EVIDENCE_DIRS["REL-0.7.0rc3"], DISPATCH_SMOKE_RECEIPTS_FILENAME)): (
        "src/eawf/workflow/release/canary_receipts.py"
    ),
    **dict.fromkeys(_HOST_FACT_CITATIONS, "src/eawf/platform/rules/data/host_facts.yaml"),
}


def test_promoted_artifacts_are_exactly_the_reader_constants() -> None:
    assert {row.pattern for row in PROMOTED_ARTIFACTS} == set(_READERS)


def test_classify_path_spec_render_is_not_committed() -> None:
    row = classify_path(".ea/specs/P32/P32-I01/P32-I01-W01.md")
    assert row.policy is CommitPolicy.NOT_COMMITTED
    assert not row.must_exist


def test_classify_path_folds_back_slashes() -> None:
    row = classify_path(".ea\\store\\release.jsonl")
    assert row.policy is CommitPolicy.COMMITTED
    promoted = PROMOTED_ARTIFACTS[0].pattern.replace("/", "\\")
    assert classify_path(promoted) == PROMOTED_ARTIFACTS[0]


def test_classify_path_single_star_does_not_cross_a_separator() -> None:
    """``.ea/store/*.jsonl`` must not claim a nested file."""
    with pytest.raises(UndeclaredPathError):
        classify_path(".ea/store/nested/deep.jsonl")


@pytest.mark.parametrize(
    ("path", "tier"),
    [
        (".ea/generations/selected.json", None),
        (".ea/generations/EPOCH2_ACTIVE.json", None),
        (".ea/generations/gen-0123456789abcdef/state.json", StorageTier.DOCUMENT),
        (".ea/generations/gen-0123456789abcdef/ledger/task.jsonl", StorageTier.LEDGER),
    ],
)
def test_classify_path_generation_committed_rows(path: str, tier: StorageTier | None) -> None:
    row = classify_path(path)
    assert row.policy is CommitPolicy.COMMITTED
    assert row.tier is tier
    assert not row.must_exist


@pytest.mark.parametrize(
    "path",
    [
        ".ea/generations/gen-0123456789abcdef/indexes/task.index.json",
        ".ea/generations/.staging-a/x",
        ".ea/generations/.staging-b/ledger/task.jsonl",
        ".ea/generations/.staging-a/state.json",
        ".ea/generations/restore/restore-manifest.json",
        ".ea/generations/restore/store/event.jsonl",
        ".ea/generations/restore/telemetry.db",
        ".ea/generations/journal.jsonl",
        ".ea/epoch2-disposable-canary.json",
    ],
)
def test_classify_path_generation_ignored_rows(path: str) -> None:
    assert classify_path(path).policy is CommitPolicy.NOT_COMMITTED


@pytest.mark.parametrize(
    "path",
    [
        ".ea/generations/gen-0123456789abcdef/local/status.json",
        ".ea/generations/gen-0123456789abcdef/local/ledger/task.jsonl",
        ".ea/generations/gen-0123456789abcdef/local/ledger/run.jsonl",
    ],
)
def test_classify_path_generation_status_projection_is_local(path: str) -> None:
    row = classify_path(path)
    assert row.policy is CommitPolicy.NOT_COMMITTED
    assert row.tier is StorageTier.LOCAL_STORE


def test_classify_path_generation_index_is_derived() -> None:
    row = classify_path(".ea/generations/gen-0123456789abcdef/indexes/task.index.json")
    assert row.tier is StorageTier.DERIVED


def test_classify_path_generation_ledger_star_does_not_cross_a_separator() -> None:
    """A nested ledger file is undeclared rather than silently committed."""
    with pytest.raises(UndeclaredPathError):
        classify_path(".ea/generations/gen-0123456789abcdef/ledger/nested/task.jsonl")


def test_classify_path_generation_lockfile_stays_not_committed() -> None:
    """A heartbeat beside a committed ledger is still a lockfile."""
    row = classify_path(".ea/generations/gen-0123456789abcdef/ledger/task.jsonl.lock")
    assert row.policy is CommitPolicy.NOT_COMMITTED


# --- classification: error paths -------------------------------------------


def test_classify_path_undeclared_directory_raises() -> None:
    with pytest.raises(UndeclaredPathError, match="matches no declared row"):
        classify_path(".ea/mystery/thing.bin")


def test_classify_path_generation_undeclared_file_raises() -> None:
    """The generation tree is declared file family by file family, not as a whole."""
    with pytest.raises(UndeclaredPathError, match="matches no declared row"):
        classify_path(".ea/generations/unexpected.json")


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
    _git(repo, "rm", "--quiet", "--cached", ".ea/config.yaml")

    result = _run_tool(repo)
    assert result.returncode == 1
    assert CensusFindingKind.DECLARED_BUT_ABSENT.value in result.stderr
    assert ".ea/config.yaml" in result.stderr


def test_census_reds_when_the_firehose_is_force_added(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    (repo / ".ea" / "store").mkdir()
    (repo / ".ea" / "store" / "event.jsonl").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "--force", ".ea/store/event.jsonl")

    result = _run_tool(repo)
    assert result.returncode == 1
    assert CensusFindingKind.TRACKED_BUT_NOT_COMMITTED.value in result.stderr


@pytest.mark.parametrize("path", [".ea/state.json", ".ea/store/audit.jsonl"])
def test_tracked_but_not_committed_v1_file_names_the_untrack_command(
    tmp_path: Path, path: str
) -> None:
    """An adopter's still-tracked v1 file reds with the command that untracks it."""
    repo = _seed_repo(tmp_path)
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "--force", path)

    result = _run_tool(repo)
    assert result.returncode == 1
    assert f"{CensusFindingKind.TRACKED_BUT_NOT_COMMITTED.value} {path}" in result.stderr
    assert f"git rm --cached -- {path}" in result.stderr


def test_tracked_but_not_committed_kept_store_stays_clean(tmp_path: Path) -> None:
    """The release and conformance stores are added without force and pass."""
    repo = _seed_repo(tmp_path)
    (repo / ".ea" / "store").mkdir()
    for stem in _KEPT_STORES:
        (repo / ".ea" / "store" / f"{stem}.jsonl").write_text("{}\n", encoding="utf-8")
    (repo / ".ea" / "store" / "audit.jsonl").write_text("{}\n", encoding="utf-8")
    (repo / ".ea" / "state.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "--all")

    result = _run_tool(repo)
    assert result.returncode == 0, result.stderr


def test_tracked_but_not_committed_finding_fits_the_longest_row() -> None:
    """The untrack reason quotes pattern, note and path without overflowing."""
    longest = max(EA_PATH_CLASSES, key=lambda row: len(row.pattern) + len(row.note))
    path = f".ea/{'x' * 230}"
    row = PathClass(pattern=".ea/*", policy=CommitPolicy.NOT_COMMITTED, note=longest.note)
    (finding,) = census_findings(tracked=(path,), ignored_probes=(row.probe,), classes=(row,))
    assert finding.kind is CensusFindingKind.TRACKED_BUT_NOT_COMMITTED
    assert finding.reason.endswith(f"git rm --cached -- {path}")


def test_census_ignores_a_local_artifact(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    (repo / ".ea" / "artifacts" / "audits").mkdir(parents=True)
    (repo / ".ea" / "artifacts" / "audits" / "x.md").write_text("# a\n", encoding="utf-8")
    _git(repo, "add", "--all")

    result = _run_tool(repo)
    assert result.returncode == 0, result.stderr


def test_census_reds_when_a_local_artifact_is_force_added(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    (repo / ".ea" / "artifacts" / "audits").mkdir(parents=True)
    (repo / ".ea" / "artifacts" / "audits" / "x.md").write_text("# a\n", encoding="utf-8")
    _git(repo, "add", "--force", ".ea/artifacts/audits/x.md")

    result = _run_tool(repo)
    assert result.returncode == 1
    assert CensusFindingKind.TRACKED_BUT_NOT_COMMITTED.value in result.stderr
    assert ".ea/artifacts/audits/x.md" in result.stderr


def test_census_passes_a_promoted_artifact_added_without_force(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    for row in PROMOTED_ARTIFACTS:
        promoted = repo / row.pattern
        promoted.parent.mkdir(parents=True, exist_ok=True)
        promoted.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "--all")
    listed = subprocess.run(
        ["git", "ls-files", ".ea/artifacts"],
        cwd=repo,
        env=_isolated_git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(listed) == sorted(row.pattern for row in PROMOTED_ARTIFACTS)

    result = _run_tool(repo)
    assert result.returncode == 0, result.stderr


def test_census_tool_exits_two_outside_a_repository(tmp_path: Path) -> None:
    """git cannot be consulted, which is distinct from a policy failure."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    result = _run_tool(outside)
    assert result.returncode == GIT_UNAVAILABLE_EXIT
    assert "ea-commit-census:" in result.stderr
