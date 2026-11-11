"""Which paths the tree ignores, tracks, runs and scans.

Three hygiene facts share one failure mode: each is a path pattern
somebody has to keep in agreement with a second place, and nothing
notices when they drift apart.

* A parallel coverage run writes one ``.coverage.<host>.<pid>.<nonce>``
  shard per worker. Untracked and unignored, they make the tree read
  dirty, and the tag preflight refuses to cut a tag on a dirty tree.
* The release ledger is where a publication RPC's idempotency key lives,
  so a clone without it cannot tell a retry from a second publication.
  Being neither tracked nor ignored is the one state that helps nobody.
* The migration fixtures and rehearsal goldens carry digests that once
  tripped the secret scan's entropy plugins and needed an exclusion. A
  fixture directory is exactly where a hand-pasted credential lands, so
  the hook scans them like any other path and they must scan clean.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import yaml
from detect_secrets.main import main as detect_secrets_main
from detect_secrets.settings import transient_settings

from eawf.kernel.store.commit_policy import CommitPolicy, classify_path
from eawf.kernel.store.tiers import StorageTier

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GITIGNORE = _REPO_ROOT / ".gitignore"
_PRE_COMMIT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"

#: The ignore rule that has to cover a shard, not just the combined file.
COVERAGE_SHARD_RULE = ".coverage.*"

#: One shard name, with the host/pid/nonce suffix a real run produces.
COVERAGE_SHARD = ".coverage.host.1.2"

#: The publication ledger a clone needs to resolve an idempotency key.
RELEASE_LEDGER = ".ea/store/release.jsonl"

#: The fixture trees whose pinned commit SHAs, manifest digests and
#: generated rehearsal goldens once needed a secret-scan exclusion; the
#: snapshot's state document was the hook's last exempted path.
SCANNED_FIXTURE_TREES: tuple[str, ...] = (
    "tests/fixtures/migration",
    "tests/golden/kernel/migration",
    "tests/fixtures/epoch1-snapshot",
)

#: The secret types the two entropy plugins the hook disables report.
ENTROPY_SECRET_TYPES = frozenset({"Hex High Entropy String", "Base64 High Entropy String"})


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


def _hook_scan_args() -> list[str]:
    """Return the detect-secrets hook arguments minus ``--baseline <path>``."""
    config = yaml.safe_load(_PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    (hook,) = [
        hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == "detect-secrets"
    ]
    assert "exclude" not in hook, "the hook scans every path; an exclusion hides new fixtures"
    args = list(hook["args"])
    at = args.index("--baseline")
    del args[at : at + 2]
    return args


def _scan(paths: Sequence[str], args: list[str]) -> dict[str, Any]:
    """Run ``detect-secrets scan`` in-process over *paths* from the repository root.

    Fresh plugin settings per call, because the scan keeps them in a module
    global; ``--no-verify`` keeps it offline.
    """
    out = io.StringIO()
    with (
        _in_repo_root(),
        transient_settings({"plugins_used": []}),
        contextlib.redirect_stdout(out),
    ):
        detect_secrets_main(["scan", "--no-verify", *args, *paths])
    result: dict[str, Any] = json.loads(out.getvalue())
    return result


@contextlib.contextmanager
def _in_repo_root() -> Iterator[None]:
    """Run the block from the repository root; the scan resolves paths from the cwd."""
    previous = Path.cwd()
    os.chdir(_REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


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


def test_release_ledger_is_kept_like_the_other_release_stores() -> None:
    """The ledger is kept by the same kind of row as the release record store."""
    ledger = classify_path(RELEASE_LEDGER)
    record = classify_path(".ea/store/release_record.jsonl")
    assert (ledger.policy, ledger.tier) == (record.policy, record.tier)
    assert classify_path(".ea/store/audit.jsonl").policy is CommitPolicy.NOT_COMMITTED


# --- the fixture trees scan clean under the hook ----------------------------


def test_migration_fixture_trees_scan_clean_under_the_hook() -> None:
    """Every file in both trees is read by the hook's scan and none trips it."""
    for tree in SCANNED_FIXTURE_TREES:
        assert (_REPO_ROOT / tree).is_dir(), tree
    assert _scan(SCANNED_FIXTURE_TREES, _hook_scan_args())["results"] == {}


def test_fixture_trees_trip_only_the_disabled_entropy_plugins() -> None:
    """With every plugin on, both trees trip, and only on the plugins the hook turns off.

    So the clean scan above reads these files, and turning the entropy
    plugins off is the only thing keeping their digests from tripping it.
    """
    results = _scan(SCANNED_FIXTURE_TREES, [])["results"]
    for tree in SCANNED_FIXTURE_TREES:
        assert any(path.startswith(f"{tree}/") for path in results), tree
    found = {row["type"] for rows in results.values() for row in rows}
    assert found <= ENTROPY_SECRET_TYPES
