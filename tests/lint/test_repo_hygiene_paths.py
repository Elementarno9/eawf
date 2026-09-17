"""Which paths the tree ignores, tracks, runs and scans.

Four hygiene facts share one failure mode: each is a path pattern
somebody has to keep in agreement with a second place, and nothing
notices when they drift apart.

* A parallel coverage run writes one ``.coverage.<host>.<pid>.<nonce>``
  shard per worker. Untracked and unignored, they make the tree read
  dirty, and the tag preflight refuses to cut a tag on a dirty tree.
* The release ledger is where a publication RPC's idempotency key lives,
  so a clone without it cannot tell a retry from a second publication.
  Being neither tracked nor ignored is the one state that helps nobody.
* ``just test ci`` is only a CI mirror while it carries the same flags as
  the workflow step it mirrors. A flag that lands in one and not the
  other turns a green local run into an unrelated claim.
* An exclusion written as a whole directory prefix stops scanning files
  nobody has written yet. A fixture directory is exactly where a
  hand-pasted credential lands, so each excluded file is named and the
  one generated class that cannot be baselined stays a narrow pattern.

The extraction and comparison helpers take text and parsed documents
rather than reading the tree themselves, so each gate is driven with a
synthetic defect as well as with the live files.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.kernel.store.commit_policy import CommitPolicy, classify_path
from eawf.kernel.store.tiers import StorageTier

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GITIGNORE = _REPO_ROOT / ".gitignore"
_JUSTFILE = _REPO_ROOT / "justfile"
_PRE_COMMIT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"

#: The ignore rule that has to cover a shard, not just the combined file.
COVERAGE_SHARD_RULE = ".coverage.*"

#: One shard name, with the host/pid/nonce suffix a real run produces.
COVERAGE_SHARD = ".coverage.host.1.2"

#: The publication ledger a clone needs to resolve an idempotency key.
RELEASE_LEDGER = ".ea/store/release.jsonl"

#: The workflow step ``just test ci`` mirrors for the render snapshots.
CI_SNAPSHOT_STEP = "Pytest (TUI render snapshots)"

#: The snapshot tree both the step and the recipe name.
SNAPSHOT_SUITE = "tests/snapshots/tui"

#: The console goldens, owned by the dedicated single-process replay job.
CONSOLE_SUITE = "tests/snapshots/tui/console"

#: The two directory prefixes the exclusion used to carry wholesale. A
#: prefix un-scans every file added under it later, which is the drift
#: this module exists to catch.
RETIRED_PREFIXES: tuple[str, ...] = (
    "^tests/fixtures/migration/",
    "^tests/golden/kernel/migration/",
)

#: Every migration fixture whose pinned commit SHAs, manifest digests or
#: recorded state slices trip an entropy plugin. Each is excluded by
#: name, so this tuple is also the list a reviewer re-derives by running
#: the scan with the exclusion removed.
EXCLUDED_MIGRATION_FIXTURES: tuple[str, ...] = (
    "tests/fixtures/migration/cutover-faults/pending-wal/locks/wal/wal-canary-0001.pending.json",
    "tests/fixtures/migration/epoch1-full/backlog_corpus.json",
    "tests/fixtures/migration/epoch2/complete-manifest.json",
    "tests/fixtures/migration/live-cutover/corpus-pin.json",
    "tests/fixtures/migration/p30-i26-history/provenance.json",
    "tests/fixtures/migration/p30-i26-history/snapshot/document.json",
    "tests/fixtures/migration/p30-i26-history/snapshot/store/audit.jsonl",
)

#: A migration fixture no exclusion names, so the scan still reads it.
UNNAMED_MIGRATION_FIXTURE = "tests/fixtures/migration/epoch1-full/snapshot/document.json"

#: The generated goldens whose digests re-roll on every regeneration.
REHEARSAL_GOLDEN_DIR = "tests/golden/kernel/migration/rehearsal"

#: Regex characters that make an alternative a family rather than a path.
_FAMILY_CHARACTERS = frozenset("[]()*+?{}")


# --- extraction and comparison ----------------------------------------------


def ignore_flags(command: str) -> tuple[str, ...]:
    """Return the ``--ignore=`` targets of one pytest command line.

    Args:
        command: A shell command line; anything that is not a pytest run
            simply carries no such flag.

    Returns:
        The ignored paths, in the order the command lists them.
    """
    return tuple(word.split("=", 1)[1] for word in command.split() if word.startswith("--ignore="))


def snapshot_ignore_violations(*, ci_command: str, just_command: str) -> list[str]:
    """Return each ignore target CI carries that the recipe drops.

    The comparison is one-directional on purpose: the recipe may add a
    local-only ignore, but dropping one CI carries means the recipe runs
    a suite CI never ran and calls the result a mirror.

    Args:
        ci_command: The workflow step's ``run`` body.
        just_command: The matching command line from the recipe.

    Returns:
        One finding per dropped target, in the CI command's order; empty
        when the recipe carries all of them.
    """
    carried = ignore_flags(just_command)
    return [
        f"just test ci drops --ignore={target}"
        for target in ignore_flags(ci_command)
        if target not in carried
    ]


def pytest_commands(text: str) -> list[str]:
    """Return every ``uv run pytest`` line in *text*, stripped."""
    return [line.strip() for line in text.splitlines() if "uv run pytest" in line]


def command_for_suite(commands: Sequence[str], suite: str) -> str:
    """Return the one command line that names *suite* as a target.

    Args:
        commands: Candidate command lines.
        suite: The path the command must pass as a bare argument, so a
            longer path carrying it as a prefix does not match.

    Returns:
        The matching command line.

    Raises:
        LookupError: No command names the suite.
    """
    for command in commands:
        if suite in command.split():
            return command
    raise LookupError(f"no command names {suite!r}")


def just_case_arm(recipe: str, mode: str) -> str:
    """Return the body of one ``case`` arm of a just recipe.

    Args:
        recipe: The recipe body.
        mode: The arm's label, without the closing parenthesis.

    Returns:
        The arm's lines, up to but excluding its ``;;`` terminator.

    Raises:
        LookupError: The recipe has no such arm.
    """
    lines = recipe.splitlines()
    opener = f"{mode})"
    for index, line in enumerate(lines):
        if line.strip() != opener:
            continue
        body: list[str] = []
        for candidate in lines[index + 1 :]:
            if candidate.strip() == ";;":
                return "\n".join(body)
            body.append(candidate)
    raise LookupError(f"recipe has no {opener} arm")


def secrets_exclude(config: Mapping[str, Any]) -> str:
    """Return the detect-secrets hook's ``exclude`` pattern.

    Args:
        config: A parsed pre-commit configuration.

    Returns:
        The pattern as written.

    Raises:
        LookupError: No detect-secrets hook declares an exclusion.
    """
    for repo in config.get("repos", ()):
        for hook in repo.get("hooks", ()):
            if hook.get("id") == "detect-secrets" and "exclude" in hook:
                return str(hook["exclude"])
    raise LookupError("no detect-secrets hook declares an exclude")


def alternatives(pattern: str) -> tuple[str, ...]:
    """Return the top-level alternatives of *pattern*.

    A plain split is only correct while no alternative wraps a group, so
    the exclusion is written without them and this stays a one-liner.

    Args:
        pattern: The exclusion regex.

    Returns:
        Each alternative, in written order; empty for an empty pattern.
    """
    return tuple(part for part in pattern.split("|") if part)


def literal_paths(pattern: str) -> tuple[str, ...]:
    """Return every anchored, single-file path *pattern* names.

    Args:
        pattern: The exclusion regex.

    Returns:
        The unescaped path of each alternative that is anchored at both
        ends and describes one file rather than a family.
    """
    paths: list[str] = []
    for alternative in alternatives(pattern):
        if not (alternative.startswith("^") and alternative.endswith("$")):
            continue
        unescaped = alternative[1:-1].replace("\\.", ".")
        if any(character in _FAMILY_CHARACTERS for character in unescaped):
            continue
        paths.append(unescaped)
    return tuple(paths)


# --- live-tree readers ------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one read-only git command against the repository."""
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_ignored(path: str) -> bool:
    """Report whether the ignore rules match *path*, index aside."""
    return _git("check-ignore", "--no-index", "-q", "--", path).returncode == 0


def _live_exclude() -> str:
    """Return the exclusion the committed pre-commit config declares."""
    return secrets_exclude(yaml.safe_load(_PRE_COMMIT_CONFIG.read_text(encoding="utf-8")))


def _live_ci_snapshot_command() -> str:
    """Return the ``run`` body of the workflow's render-snapshot step.

    Returns:
        The step's command line.

    Raises:
        LookupError: The test job declares no such step.
    """
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["test"]["steps"]:
        if step.get("name") == CI_SNAPSHOT_STEP:
            return str(step["run"])
    raise LookupError(f"the test job has no {CI_SNAPSHOT_STEP!r} step")


def _live_just_snapshot_command() -> str:
    """Return the snapshot command line of the recipe's ``ci`` arm."""
    lines = _JUSTFILE.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("test "))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        body.append(line)
    arm = just_case_arm("\n".join(body), "ci")
    return command_for_suite(pytest_commands(arm), SNAPSHOT_SUITE)


# --- coverage shards are ignored --------------------------------------------


def test_coverage_shards_are_ignored() -> None:
    """A per-worker shard is ignored, not just the combined data file."""
    assert COVERAGE_SHARD_RULE in _GITIGNORE.read_text(encoding="utf-8").splitlines()
    assert _is_ignored(COVERAGE_SHARD)
    assert _is_ignored(".coverage")


def test_coverage_shard_rule_leaves_source_paths_alone() -> None:
    """The rule is a shard rule, not a ban on the word coverage."""
    assert not _is_ignored("src/eawf/workflow/propose/coverage.py")
    assert not _is_ignored("tools/coverage_gate.py")
    assert not _is_ignored("coverage.xml.keep")


# --- the release ledger is tracked ------------------------------------------


def test_release_ledger_is_tracked_and_committed() -> None:
    """Git carries the ledger and the policy table agrees that it should."""
    assert _git("ls-files", "--error-unmatch", "--", RELEASE_LEDGER).returncode == 0
    assert not _is_ignored(RELEASE_LEDGER)
    row = classify_path(RELEASE_LEDGER)
    assert row.policy is CommitPolicy.COMMITTED
    assert row.tier is StorageTier.LEDGER


def test_release_ledger_sits_beside_the_other_typed_stores() -> None:
    """The ledger is one of the store family, not a special case."""
    assert classify_path(RELEASE_LEDGER) == classify_path(".ea/store/release_record.jsonl")


# --- just test ci mirrors the workflow --------------------------------------


def test_just_test_ci_ignores_the_console_goldens() -> None:
    """The recipe skips the suite the dedicated replay job owns."""
    assert CONSOLE_SUITE in ignore_flags(_live_just_snapshot_command())


def test_just_test_ci_mirrors_the_ci_snapshot_ignores() -> None:
    """The recipe drops no ignore flag the workflow step carries."""
    violations = snapshot_ignore_violations(
        ci_command=_live_ci_snapshot_command(),
        just_command=_live_just_snapshot_command(),
    )
    assert violations == []


def test_ci_snapshot_step_still_ignores_the_console_goldens() -> None:
    """The mirror is only worth asserting while CI carries the flag."""
    assert CONSOLE_SUITE in ignore_flags(_live_ci_snapshot_command())


def test_snapshot_ignore_gate_reds_on_a_dropped_flag() -> None:
    """The defect this gate exists for: the recipe runs what CI skipped."""
    violations = snapshot_ignore_violations(
        ci_command=f"uv run pytest -n 4 {SNAPSHOT_SUITE} --ignore={CONSOLE_SUITE}",
        just_command=f"uv run pytest -n 4 {SNAPSHOT_SUITE}",
    )
    assert violations == [f"just test ci drops --ignore={CONSOLE_SUITE}"]


def test_snapshot_ignore_gate_allows_a_recipe_only_flag() -> None:
    """A local-only ignore is not drift; only a dropped one is."""
    violations = snapshot_ignore_violations(
        ci_command=f"uv run pytest {SNAPSHOT_SUITE}",
        just_command=f"uv run pytest {SNAPSHOT_SUITE} --ignore={CONSOLE_SUITE}",
    )
    assert violations == []


def test_ignore_flags_of_a_flagless_command_is_empty() -> None:
    assert ignore_flags("uv run pytest -n 4 tests") == ()
    assert ignore_flags("") == ()


def test_ignore_flags_keeps_written_order() -> None:
    assert ignore_flags("uv run pytest --ignore=b --ignore=a") == ("b", "a")


def test_command_for_suite_ignores_a_longer_path() -> None:
    """A prefix match would pick the console run for the snapshot suite."""
    commands = [f"uv run pytest {CONSOLE_SUITE}", f"uv run pytest {SNAPSHOT_SUITE}"]
    assert command_for_suite(commands, SNAPSHOT_SUITE) == commands[1]


def test_command_for_suite_raises_when_nothing_names_it() -> None:
    with pytest.raises(LookupError, match="no command names"):
        command_for_suite(["uv run pytest tests/unit"], SNAPSHOT_SUITE)


def test_just_case_arm_raises_on_an_absent_mode() -> None:
    with pytest.raises(LookupError, match=re.escape("no fast) arm")):
        just_case_arm("    ci)\n      run\n      ;;", "fast")


# --- the secrets exclusion names its files ----------------------------------


def test_secrets_exclude_retires_the_whole_migration_prefixes() -> None:
    """Neither directory prefix survives as an alternative."""
    declared = alternatives(_live_exclude())
    assert [prefix for prefix in RETIRED_PREFIXES if prefix in declared] == []


def test_secrets_exclude_scans_an_unnamed_migration_fixture() -> None:
    """A fixture nobody named is read by the scan, as a new one will be."""
    assert (_REPO_ROOT / UNNAMED_MIGRATION_FIXTURE).is_file()
    assert re.search(_live_exclude(), UNNAMED_MIGRATION_FIXTURE) is None


def test_secrets_exclude_names_every_entropy_tripping_fixture() -> None:
    """Each fixture the narrowed exclusion has to carry is still carried."""
    pattern = _live_exclude()
    missed = [path for path in EXCLUDED_MIGRATION_FIXTURES if re.search(pattern, path) is None]
    assert missed == []


def test_secrets_exclude_named_files_all_exist() -> None:
    """Every path the exclusion names is a real file, so none is stale."""
    absent = [path for path in literal_paths(_live_exclude()) if not (_REPO_ROOT / path).is_file()]
    assert absent == []


def test_secrets_exclude_named_files_cover_the_migration_fixtures() -> None:
    """The fixtures are named one by one rather than matched by a family."""
    named = literal_paths(_live_exclude())
    assert all(path in named for path in EXCLUDED_MIGRATION_FIXTURES)


def test_secrets_exclude_covers_every_rehearsal_golden() -> None:
    """The class pattern holds for the whole generated directory."""
    pattern = _live_exclude()
    goldens = sorted((_REPO_ROOT / REHEARSAL_GOLDEN_DIR).glob("*.json"))
    assert goldens, "the rehearsal goldens moved, so the pattern guards nothing"
    for golden in goldens:
        relative = golden.relative_to(_REPO_ROOT).as_posix()
        assert re.search(pattern, relative) is not None, relative


def test_secrets_exclude_stops_at_the_rehearsal_directory() -> None:
    """A sibling golden outside the generated class is still scanned."""
    assert re.search(_live_exclude(), "tests/golden/kernel/migration/plan.json") is None


def test_secrets_exclude_raises_without_the_hook() -> None:
    with pytest.raises(LookupError, match="no detect-secrets hook"):
        secrets_exclude({"repos": [{"hooks": [{"id": "ruff"}]}]})


def test_secrets_exclude_raises_on_an_empty_config() -> None:
    with pytest.raises(LookupError, match="no detect-secrets hook"):
        secrets_exclude({})


def test_alternatives_of_an_empty_pattern_is_empty() -> None:
    assert alternatives("") == ()


def test_alternatives_splits_a_single_alternative() -> None:
    assert alternatives("^a\\.json$") == ("^a\\.json$",)


def test_literal_paths_skips_a_family_alternative() -> None:
    assert literal_paths("^a/b\\.json$|^c/[a-z]+\\.json$") == ("a/b.json",)


def test_literal_paths_skips_an_unanchored_alternative() -> None:
    assert literal_paths("uv\\.lock|^a/b\\.json$") == ("a/b.json",)


def test_literal_paths_of_an_empty_pattern_is_empty() -> None:
    assert literal_paths("") == ()
