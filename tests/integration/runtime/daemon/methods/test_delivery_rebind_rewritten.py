"""After a history rewrite in place, every ledger row naming an old commit is rebound.

A tree's history is rewritten with every tree kept, as an author rewrite does:
the integration branch's two commits, a twin of the second that the rewrite
made identical to it, and a commit on a side branch each get a new commit. The
ledgers hold what was filed against the old history; the generation document
holds a plan revision bound to an old commit. ``runtime.delivery.rebind_rewritten``
appends one superseding row per ledger line naming an old commit, records each
applied pair in ``.ea/store/rewrite_map.jsonl`` and leaves the document alone,
and refuses, writing nothing, a map naming a commit the repository lacks or
pairing two commits of different trees.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.store.compaction import compact_terminal_record, read_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    effective_records,
    latest_rows,
    read_ledger_records,
)
from eawf.kernel.store.paths import ledger_path, seed_ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_rebind import (
    DELIVERY_REBIND_REWRITTEN_METHOD,
    REWRITE_COMMIT_MISSING,
    REWRITE_TREE_MISMATCH,
)
from eawf.surfaces.cli.app import app
from eawf.workflow.integration.rebind import RewriteMap, load_rewrite_map
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    method_context,
    provision,
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_delivery_landed_loop import cli
from tests.integration.workflow.planning import test_plan_revision_apply as plan_apply

pytestmark = pytest.mark.integration

__all__ = ["cli"]

ACTOR: Final = "OP-0001"
BRANCH: Final = "feature/eawf-v0.7"
#: A commit hash no object of the repository carries, quoted in prose.
UNKNOWN: Final = "9" * 40
#: An old commit the repository no longer holds, so its tree cannot be compared.
GONE: Final = "8" * 40
COLLECTIONS: Final = tuple(Epoch2Collection)

runner = CliRunner()


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=None if env is None else {**os.environ, **env},
    ).stdout.strip()


def _write_commit(repo: Path, tree: str, parent: str, message: str, author: str) -> str:
    """Write one commit of *tree* on *parent* by *author* at a fixed instant."""
    stamp = "2026-11-01T00:00:00+00:00"
    env = {
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": "author@example.com",
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": "author@example.com",
        "GIT_COMMITTER_DATE": stamp,
    }
    return git(repo, "commit-tree", tree, "-p", parent, "-m", message, env=env)


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    (repo / path).write_text(content, encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def _tree(repo: Path, sha: str) -> str:
    return git(repo, "rev-parse", f"{sha}^{{tree}}")


@dataclass(frozen=True)
class Rewritten:
    """A canary whose history was rewritten in place.

    Attributes:
        canary: The provisioned tree.
        repo: Its repository.
        old: The old commits by role: base, first, second, twin, side.
        new: The commit each role became; ``twin`` became ``second``'s.
        map_file: The git filter-repo commit map of the rewrite.
    """

    canary: CanaryProvision
    repo: Path
    old: dict[str, str]
    new: dict[str, str]
    map_file: Path

    def rewrite_map(self) -> RewriteMap:
        return load_rewrite_map(self.map_file)


def _ledger(canary: CanaryProvision, collection: Epoch2Collection) -> Path:
    return ledger_path(document_path(canary), collection)


def _compact(canary: CanaryProvision, collection: Epoch2Collection, row: dict[str, Any]) -> None:
    """Seed *row* into the document, then compact it into its ledger as a close does."""
    seed(canary, {collection.value: {row["key"]: row}})
    compact_terminal_record(
        document_path(canary),
        record=LedgerRecord(
            collection=collection,
            record_key=row["key"],
            status=row["status"],
            recorded_at=AT,
            payload=row,
        ),
    )


def _append(canary: CanaryProvision, collection: Epoch2Collection, key: str, payload: Any) -> None:
    append_ledger_record(
        _ledger(canary, collection),
        LedgerRecord(
            collection=collection, record_key=key, status="x", recorded_at=AT, payload=payload
        ),
    )


def _write_map(path: Path, pairs: dict[str, str]) -> Path:
    path.write_text(
        "old                                      new\n"
        + "".join(f"{old} {new}\n" for old, new in pairs.items()),
        encoding="utf-8",
    )
    return path


def make_tree(tmp_path: Path) -> Rewritten:
    """Build a history, file ledgers against it, then rewrite it in place."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "ci@example.com")
    git(repo, "config", "user.name", "ci")
    git(repo, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (repo / ".gitignore").write_text(".ea/\n", encoding="utf-8")
    base = _commit(repo, "seed.txt", "seed\n", "base")
    git(repo, "checkout", "-q", "-b", BRANCH)
    first = _commit(repo, "a.txt", "a\n", "feat: a\n\nTask: EAWF-0042")
    second = _commit(repo, "b.txt", "b\n", "fix: b")
    # The same commit by another author: the author rewrite makes the two one.
    twin = _write_commit(repo, _tree(repo, second), first, "fix: b", "someone else")
    side = _write_commit(repo, _tree(repo, base), base, "side", "ci")
    old = {"base": base, "first": first, "second": second, "twin": twin, "side": side}
    canary = provision(repo, code="RWR")

    new_first = _write_commit(repo, _tree(repo, first), base, "feat: a", "rewritten")
    new_second = _write_commit(repo, _tree(repo, second), new_first, "fix: b", "rewritten")
    new_side = _write_commit(repo, _tree(repo, side), base, "side", "rewritten")
    git(repo, "update-ref", f"refs/heads/{BRANCH}", new_second)
    git(repo, "update-ref", "refs/heads/side", new_side)
    new = {
        "base": base,
        "first": new_first,
        "second": new_second,
        "twin": new_second,
        "side": new_side,
    }
    map_file = _write_map(tmp_path / "commit-map", {old[role]: new[role] for role in old})

    tree = _tree(repo, second)
    seed(
        canary,
        {
            "project": {"EAWF": {"payload": {"code": "EAWF"}}},
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")},
            # A live plan revision bound to an old commit; its own verbs rewrite it.
            "plan_revision": {
                "PRV-0001": {"key": "PRV-0001", "head_bindings": [{"head_sha": first}]}
            },
        },
    )
    batch = rekeyed(seed_row("batch", "COMPLETED"), key="BAT-0007")
    batch["current_head_binding"] |= {"head_sha": second, "tree_sha": tree}
    _compact(canary, Epoch2Collection.BATCH, batch)
    task = rekeyed(seed_row("task", "COMPLETED"), key="EAWF-0042")
    task["integrated_binding"] |= {"head_sha": second, "tree_sha": tree}
    _compact(canary, Epoch2Collection.TASK, task)
    _append(
        canary,
        Epoch2Collection.BATCH,
        "BMR-000000000001-BAT-0007",
        {"batch_ref": batch["urn"], "matched_head_sha": twin},
    )
    _append(
        canary,
        Epoch2Collection.RECEIPT,
        "PRF-0000000000000001-EAWF-0042",
        {"revision_binding": {"head_sha": second, "parent_sha": first, "tree_sha": tree}},
    )
    _append(
        canary,
        Epoch2Collection.EVIDENCE,
        "EVD-0001",
        {"summary": f"verified {first} on {side}; quoted {UNKNOWN}"},
    )
    return Rewritten(canary=canary, repo=repo, old=old, new=new, map_file=map_file)


def rebind(
    tmp_path: Path,
    tree: Rewritten,
    key: str,
    *,
    rewrite_map: RewriteMap | None = None,
    dry_run: bool = False,
) -> Any:
    """Send ``runtime.delivery.rebind_rewritten`` and return its answer."""
    call = {
        "repo_root": str(tree.canary.root),
        "actor": ACTOR,
        "idempotency_key": key,
        "rewrite_map": (rewrite_map or tree.rewrite_map()).model_dump(mode="json"),
        "dry_run": dry_run,
    }
    return asyncio.run(
        methods.dispatch(DELIVERY_REBIND_REWRITTEN_METHOD, method_context(tmp_path / "rt"), call)
    )


def _record_file(tree: Rewritten) -> Path:
    return tree.canary.root / ".ea" / "store" / "rewrite_map.jsonl"


def _snapshot(tree: Rewritten) -> dict[str, bytes]:
    """Return every file of the ``.ea`` tree but its lock files, by relative path."""
    root = tree.canary.root / ".ea"
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix != ".lock" and "locks" not in path.parts
    }


def _committed(snapshot: dict[str, bytes]) -> dict[str, bytes]:
    """Return *snapshot* less the machine-local files, idempotency receipts among them."""
    return {path: data for path, data in snapshot.items() if not path.startswith("local/")}


def _effective_text(tree: Rewritten) -> str:
    return "\n".join(
        record.model_dump_json()
        for collection in COLLECTIONS
        if collection.value in {"batch", "task", "receipt", "evidence"}
        for record in effective_records(read_ledger_records(_ledger(tree.canary, collection)))
    )


def test_rebind_rewritten_supersedes_every_row_naming_an_old_commit(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    old, new = tree.old, tree.new

    answer = rebind(tmp_path, tree, "rewrite-1")

    assert answer == {
        "rebound": 5,
        "per_collection": {"batch": 2, "task": 1, "receipt": 1, "evidence": 1},
        "unmapped": 1,
        "pairs_recorded": 4,
        "dry_run": False,
        "replayed": False,
    }
    batches = latest_rows(_ledger(tree.canary, Epoch2Collection.BATCH))
    assert batches["BAT-0007"]["current_head_binding"]["head_sha"] == new["second"]
    task = latest_rows(_ledger(tree.canary, Epoch2Collection.TASK))["EAWF-0042"]
    assert task["integrated_binding"]["head_sha"] == new["second"]
    records = {
        record.record_key: record.payload
        for collection in (
            Epoch2Collection.BATCH,
            Epoch2Collection.RECEIPT,
            Epoch2Collection.EVIDENCE,
        )
        for record in effective_records(read_ledger_records(_ledger(tree.canary, collection)))
    }
    # The twin was rebound to the one commit both old commits became.
    assert records["BMR-000000000001-BAT-0007"]["matched_head_sha"] == new["second"]
    assert records["PRF-0000000000000001-EAWF-0042"]["revision_binding"] == {
        "head_sha": new["second"],
        "parent_sha": new["first"],
        "tree_sha": _tree(tree.repo, old["second"]),
    }
    # A hash in prose is rebound too; one the map leaves out stays as it is.
    assert records["EVD-0001"]["summary"] == (
        f"verified {new['first']} on {new['side']}; quoted {UNKNOWN}"
    )
    effective = _effective_text(tree)
    assert all(old[role] not in effective for role in ("first", "second", "twin", "side"))
    # The live plan revision keeps its old binding and refused nothing.
    document = read_document(document_path(tree.canary))
    assert document["plan_revision"]["PRV-0001"]["head_bindings"] == [{"head_sha": old["first"]}]
    lines = [json.loads(line) for line in _record_file(tree).read_text().splitlines()]
    assert all(sorted(line) == ["applied_at", "new", "old"] for line in lines)
    assert sorted((line["old"], line["new"]) for line in lines) == sorted(
        (old[role], new[role]) for role in ("first", "second", "twin", "side")
    )


def test_rebind_rewritten_applies_none_of_the_squash_guards(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    # The squash guards would refuse: the side commit is off the branch, no new
    # commit carries a Batch trailer, the branch head is no map head, and the
    # document's plan revision names an old commit.
    git(tree.repo, "update-ref", f"refs/heads/{BRANCH}", tree.new["first"])

    answer = rebind(tmp_path, tree, "rewrite-1")

    assert answer["rebound"] == 5


def test_a_second_run_appends_nothing_and_the_same_key_replays(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    first = rebind(tmp_path, tree, "rewrite-1")
    after = _snapshot(tree)

    again = rebind(tmp_path, tree, "rewrite-2")
    replay = rebind(tmp_path, tree, "rewrite-1")

    assert again == {**first, "rebound": 0, "per_collection": {}, "pairs_recorded": 0}
    assert replay == {**first, "replayed": True}
    # The new key files its own receipt; nothing else moves.
    assert _committed(_snapshot(tree)) == _committed(after)


def test_a_dry_run_writes_nothing_and_counts_what_the_run_writes(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    before = _snapshot(tree)

    planned = rebind(tmp_path, tree, "rewrite-1", dry_run=True)
    unchanged = _snapshot(tree)
    applied = rebind(tmp_path, tree, "rewrite-1")

    assert unchanged == before
    assert planned == {**applied, "dry_run": True}
    assert applied["rebound"] == 5


def test_an_old_commit_the_repository_no_longer_holds_is_rebound_unchecked(
    tmp_path: Path,
) -> None:
    tree = make_tree(tmp_path)
    _append(tree.canary, Epoch2Collection.EVIDENCE, "EVD-0002", {"summary": f"saw {GONE}"})

    answer = rebind(
        tmp_path, tree, "rewrite-1", rewrite_map=RewriteMap(commits={GONE: tree.new["first"]})
    )

    evidence = {
        record.record_key: record.payload
        for record in effective_records(
            read_ledger_records(_ledger(tree.canary, Epoch2Collection.EVIDENCE))
        )
    }
    assert answer["rebound"] == 1 and answer["pairs_recorded"] == 1
    assert evidence["EVD-0002"]["summary"] == f"saw {tree.new['first']}"


def test_an_empty_map_rebinds_nothing_and_counts_every_recorded_commit(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    effective = _effective_text(tree)

    answer = rebind(tmp_path, tree, "rewrite-1", rewrite_map=RewriteMap(commits={}))

    # first, second, twin and side, plus the quoted unknown hash; trees are no commits.
    assert answer["rebound"] == 0 and answer["pairs_recorded"] == 0
    assert answer["unmapped"] == 5
    assert _effective_text(tree) == effective
    assert not _record_file(tree).exists()


@pytest.mark.parametrize(
    ("case", "code"),
    [("missing-new", REWRITE_COMMIT_MISSING), ("tree-mismatch", REWRITE_TREE_MISMATCH)],
)
def test_a_refused_rewrite_writes_nothing(tmp_path: Path, case: str, code: str) -> None:
    tree = make_tree(tmp_path)
    pairs = {tree.old[role]: tree.new[role] for role in ("first", "second", "twin")}
    if case == "missing-new":
        pairs[tree.old["side"]] = "7" * 40
    else:
        pairs[tree.old["side"]] = tree.new["first"]
    before = _snapshot(tree)

    with pytest.raises(DaemonValidationError, match=code):
        rebind(tmp_path, tree, "rewrite-1", rewrite_map=RewriteMap(commits=pairs))

    assert _snapshot(tree) == before


def test_cli_rebind_rewritten_reads_the_filter_repo_map(tmp_path: Path, cli: None) -> None:
    tree = make_tree(tmp_path)
    argv = [
        "--workspace", str(tree.canary.root), "--json", "batch", "rebind-rewritten",
        "--map-file", str(tree.map_file), "--idempotency-key", "rewrite-cli-1", "--actor", ACTOR,
    ]  # fmt: skip

    planned = runner.invoke(app, [*argv, "--dry-run"])
    applied = runner.invoke(app, argv)

    assert planned.exit_code == 0, planned.output
    assert applied.exit_code == 0, applied.output
    plan, result = orjson.loads(planned.stdout)["result"], orjson.loads(applied.stdout)["result"]
    assert plan == {**result, "dry_run": True}
    assert result["rebound"] == 5 and result["pairs_recorded"] == 4


def test_cli_rebind_rewritten_refuses_an_invalid_map(tmp_path: Path, cli: None) -> None:
    tree = make_tree(tmp_path)
    broken = _write_map(tmp_path / "broken-map", {tree.old["first"]: "0" * 40})

    result = runner.invoke(
        app,
        [
            "--workspace", str(tree.canary.root), "--json", "batch", "rebind-rewritten",
            "--map-file", str(broken), "--idempotency-key", "rewrite-cli-2", "--actor", ACTOR,
        ],
    )  # fmt: skip

    assert result.exit_code != 0
    assert "rewrite_map_invalid: line 2" in result.output
    assert not _record_file(tree).exists()


def _reverted(tree: Rewritten) -> RewriteMap:
    """Return the map that undoes the rewrite: each new commit back to its old one."""
    return RewriteMap(
        commits={tree.new[role]: tree.old[role] for role in ("first", "second", "side")}
    )


def test_a_map_reverting_recorded_pairs_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    rebind(tmp_path, tree, "rewrite-1")
    before = _snapshot(tree)

    for dry_run in (True, False):
        with pytest.raises(DaemonValidationError, match="rewrite_map_invalid: line"):
            rebind(tmp_path, tree, "rewrite-2", rewrite_map=_reverted(tree), dry_run=dry_run)

    assert _snapshot(tree) == before


@pytest.mark.parametrize(
    "content", [b'{"old": "1"}\n', b'{"old": "' + b"1" * 40 + b'", "new'], ids=["bad", "torn"]
)
def test_a_malformed_record_is_refused_and_writes_nothing(tmp_path: Path, content: bytes) -> None:
    tree = make_tree(tmp_path)
    _record_file(tree).parent.mkdir(parents=True, exist_ok=True)
    _record_file(tree).write_bytes(content)
    before = _snapshot(tree)

    with pytest.raises(DaemonValidationError, match="rewrite_map_invalid: line 1"):
        rebind(tmp_path, tree, "rewrite-1")

    assert _snapshot(tree) == before


def test_a_dry_run_opens_no_session_and_leaves_the_whole_tree_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = make_tree(tmp_path)
    root = tree.canary.root / ".ea"

    def unsealed(target: Any, *, observed_at: Any) -> None:
        # A tree whose activation is not sealed yet: a closing session seals it.
        (root / "local" / "sealed-by-a-session").write_text("sealed", encoding="utf-8")

    monkeypatch.setattr("eawf.runtime.daemon.epoch2_root.seal_first_native_mutation", unsealed)
    before = {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    planned = rebind(tmp_path, tree, "rewrite-1", dry_run=True)

    assert {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()} == before
    assert planned["rebound"] == 5 and planned["dry_run"] is True


def test_a_dry_run_counts_the_rows_of_an_unseeded_local_run_ledger(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    local = _ledger(tree.canary, Epoch2Collection.RUN)
    committed = seed_ledger_path(document_path(tree.canary), Epoch2Collection.RUN)
    append_ledger_record(
        committed,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key="RUN-00000001",
            status="x",
            recorded_at=AT,
            payload={"head_sha": tree.old["first"]},
        ),
    )
    local.unlink(missing_ok=True)

    planned = rebind(tmp_path, tree, "rewrite-1", dry_run=True)
    assert not local.exists()
    applied = rebind(tmp_path, tree, "rewrite-1")

    assert planned == {**applied, "dry_run": True}
    assert applied["per_collection"]["run"] == 1


def test_a_plan_apply_refuses_a_cyclic_record_of_rewrite_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    canary = plan_apply.approved(tmp_path, code="RWRAPPLY")
    record = canary.root / ".ea" / "store" / "rewrite_map.jsonl"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        "".join(
            f'{{"old": "{old}", "new": "{new}", "applied_at": "2026-11-01T00:00:00Z"}}\n'
            for old, new in (("1" * 40, "2" * 40), ("2" * 40, "1" * 40))
        ),
        encoding="utf-8",
    )

    answer = plan_apply.apply_plan(canary, tmp_path)

    (error,) = answer["errors"]
    assert (error["code"], error["guard"]) == ("schema_validation_failed", "rewrite_map_invalid")
    assert "line 1" in error["message"]
    assert plan_apply.collection(canary, "plan_revision")[plan_apply.REVISION_KEY]["status"] == (
        "APPROVED"
    )


def _capture(tree: Rewritten) -> Path:
    """File a content-addressed capture of tool output quoting an old commit; return its ledger."""
    text = f"HEAD is now at {tree.old['first']} feat: a"
    digest = hashlib.sha256(text.encode()).hexdigest()
    ref = f"artifact://content/run-00000077/{digest}"
    ledger = _ledger(tree.canary, Epoch2Collection.ARTIFACT)
    append_ledger_record(
        ledger,
        LedgerRecord(
            collection=Epoch2Collection.ARTIFACT,
            record_key=ref,
            status="content_artifact",
            recorded_at=AT,
            payload={
                "payload_kind": "content_artifact",
                "artifact_ref": ref,
                "run_ref": "eawf://RWR/run/RUN-00000077",
                "lines": [text],
                "total_lines": 1,
                "withheld_lines": 0,
                "recorded_at": "2026-11-01T00:00:00Z",
            },
        ),
    )
    return ledger


def test_a_content_addressed_capture_is_left_as_it_is_and_not_counted(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    ledger = _capture(tree)
    captured = ledger.read_bytes()

    planned = rebind(tmp_path, tree, "rewrite-1", dry_run=True)
    first = rebind(tmp_path, tree, "rewrite-1")
    after = _snapshot(tree)
    second = rebind(tmp_path, tree, "rewrite-2")

    assert planned == {**first, "dry_run": True}
    assert first["rebound"] == 5 and "artifact" not in first["per_collection"]
    assert ledger.read_bytes() == captured
    assert not (ledger.parent.parent / "local" / "ledger" / ledger.name).exists()
    assert second["rebound"] == 0 and second["per_collection"] == {}
    assert _committed(_snapshot(tree)) == _committed(after)
