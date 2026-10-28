"""CR-0442-2: after a per-Batch squash, every ledger row naming a source commit is rebound.

A tree's branch carries two Batches' Task commits. Its ledgers hold what closing
them on landing filed against the source history: each COMPLETED Batch's pinned
head, the reconciliation row that matched it, the COMPLETED Task's integrated
binding and a proof receipt's revision. ``eawf batch squash`` writes the branch
again as one commit per Batch; once the branch points at the squashed tip,
``runtime.delivery.rebind_squashed`` appends one superseding row per line,
naming the commit that now carries the row's Batch -- and refuses, appending
nothing, a branch that does not point at that tip, a commit lacking its Batch
trailer, or a live document row still naming a source commit.
"""

from __future__ import annotations

import asyncio
import copy
import re
import subprocess
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.delivery.acceptance import MilestoneAcceptanceBundle
from eawf.kernel.store.compaction import compact_terminal_record, read_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    effective_records,
    latest_rows,
    line_digest,
    read_ledger_records,
    render_ledger_line,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_acceptance import DELIVERY_RECORD_EVIDENCE_METHOD
from eawf.runtime.daemon.methods.delivery_rebind import (
    DELIVERY_REBIND_SQUASHED_METHOD,
    REBIND_HEAD_MOVED,
    REBIND_LIVE_ROW,
    REBIND_TRAILER_MISSING,
)
from eawf.runtime.daemon.methods.projection import MILESTONE_ACCEPTANCE_METHOD
from eawf.surfaces.cli.app import app
from eawf.workflow.integration.squash import SquashMap, squash
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    acceptance_bundle_row,
    document_path,
    method_context,
    provision,
    rekeyed,
    sealed_approval_row,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_delivery_landed_loop import cli
from tests.integration.workflow.planning._v07_plans import seal_plan_approval

pytestmark = pytest.mark.integration

__all__ = ["cli"]

ACTOR: Final = "OP-0001"
BRANCH: Final = "feature/eawf-v0.7"
TASKS: Final = {"EAWF-0042": "BAT-0007", "EAWF-0043": "BAT-0008"}
SHA_RE: Final = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")

runner = CliRunner()


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    (repo / path).write_text(content, encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


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


def _batch(key: str, head: str, tree: str) -> dict[str, Any]:
    row = rekeyed(seed_row("batch", "COMPLETED"), key=key)
    row["current_head_binding"]["head_sha"] = head
    row["current_head_binding"]["tree_sha"] = tree
    return row


def _task(key: str, batch: str, head: str, tree: str) -> dict[str, Any]:
    row = rekeyed(seed_row("task", "COMPLETED"), key=key)
    row["batch_ref"] = f"{row['batch_ref'].rsplit('/', 1)[0]}/{batch}"
    row["integrated_binding"]["head_sha"] = head
    row["integrated_binding"]["tree_sha"] = tree
    return row


def make_tree(tmp_path: Path) -> tuple[CanaryProvision, Path, str, list[str]]:
    """A branch of two Batches' interleaved Task commits, closed against the source history."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "ci@example.com")
    git(repo, "config", "user.name", "ci")
    git(repo, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (repo / ".gitignore").write_text(".ea/\n", encoding="utf-8")
    base = _commit(repo, "seed.txt", "seed\n", "base")
    git(repo, "checkout", "-q", "-b", BRANCH)
    shas = [
        _commit(repo, "a.txt", "a\n", "feat: a\n\nTask: EAWF-0042"),
        _commit(repo, "b.txt", "b\n", "fix: b\n\nTask: EAWF-0043"),
        _commit(repo, "a.txt", "a2\n", "feat: a again\n\nTask: EAWF-0042"),
    ]
    canary = provision(repo, code="RBD")
    head_a, head_b = shas[2], shas[1]
    tree_a, tree_b = (git(repo, "rev-parse", f"{sha}^{{tree}}") for sha in (head_a, head_b))
    seed(
        canary,
        {
            # A tree-wide append locks the tree's one project.
            "project": {"EAWF": {"payload": {"code": "EAWF"}}},
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")},
        },
    )
    _compact(canary, Epoch2Collection.BATCH, _batch("BAT-0007", head_a, tree_a))
    _compact(canary, Epoch2Collection.BATCH, _batch("BAT-0008", head_b, tree_b))
    _compact(canary, Epoch2Collection.TASK, _task("EAWF-0042", "BAT-0007", head_a, tree_a))
    _compact(canary, Epoch2Collection.TASK, _task("EAWF-0043", "BAT-0008", head_b, tree_b))
    batch_urn = seed_row("batch", "COMPLETED")["urn"]
    append_ledger_record(
        _ledger(canary, Epoch2Collection.BATCH),
        LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key="BMR-000000000001-BAT-0007",
            status="landed",
            recorded_at=AT,
            payload={
                "batch_ref": batch_urn,
                "matched_head_sha": head_a,
                "reason": f"branch {BRANCH} was read at {head_a} and carries the pinned head",
            },
        ),
    )
    append_ledger_record(
        _ledger(canary, Epoch2Collection.RECEIPT),
        LedgerRecord(
            collection=Epoch2Collection.RECEIPT,
            record_key="PRF-0000000000000001-EAWF-0043",
            status="pass",
            recorded_at=AT,
            payload={
                "task_ref": f"{batch_urn.rsplit('/', 2)[0]}/task/EAWF-0043",
                "receipt": {
                    "freshness": {
                        "revision_binding": {
                            "head_sha": head_b,
                            "parent_sha": shas[0],
                            "tree_sha": tree_b,
                        }
                    }
                },
            },
        ),
    )
    return canary, repo, base, shas


def _squash_and_point(repo: Path, base: str) -> SquashMap:
    """Squash the branch into a scratch branch and point the branch at it, as an operator does."""
    result = squash(
        repo,
        base=base,
        source=BRANCH,
        branch=BRANCH,
        task_batches=TASKS,
        into="scratch/squashed",
        on_conflict="merge",
    )
    git(repo, "update-ref", f"refs/heads/{BRANCH}", "scratch/squashed")
    return result.map


def rebind(tmp_path: Path, canary: CanaryProvision, squash_map: SquashMap, key: str) -> Any:
    """Send ``runtime.delivery.rebind_squashed`` and return its answer."""
    call = {
        "repo_root": str(canary.root),
        "actor": ACTOR,
        "idempotency_key": key,
        "squash_map": squash_map.model_dump(mode="json"),
    }
    return asyncio.run(
        methods.dispatch(DELIVERY_REBIND_SQUASHED_METHOD, method_context(tmp_path / "rt"), call)
    )


def _effective_shas(canary: CanaryProvision) -> set[str]:
    """Return every 40-hex string an effective line of any touched ledger carries."""
    found: set[str] = set()
    for collection in (Epoch2Collection.BATCH, Epoch2Collection.TASK, Epoch2Collection.RECEIPT):
        for record in effective_records(read_ledger_records(_ledger(canary, collection))):
            found |= set(SHA_RE.findall(record.model_dump_json()))
    return found


def _line_count(canary: CanaryProvision) -> int:
    return sum(
        len(read_ledger_records(_ledger(canary, collection)))
        for collection in (Epoch2Collection.BATCH, Epoch2Collection.TASK, Epoch2Collection.RECEIPT)
    )


def test_rebind_supersedes_every_row_naming_a_source_commit(tmp_path: Path) -> None:
    canary, repo, base, shas = make_tree(tmp_path)
    squash_map = _squash_and_point(repo, base)
    commit_a, commit_b = squash_map.batches["BAT-0007"], squash_map.batches["BAT-0008"]

    answer = rebind(tmp_path, canary, squash_map, "rebind-1")

    assert answer["rows"] == 6
    assert answer["ledgers"] == {"batch": 3, "task": 2, "receipt": 1}
    assert answer["rebound"] == {
        shas[0]: [commit_b],
        shas[1]: [commit_b],
        shas[2]: [commit_a],
    }
    batches = latest_rows(_ledger(canary, Epoch2Collection.BATCH))
    binding = batches["BAT-0007"]["current_head_binding"]
    assert binding["head_sha"] == commit_a
    assert binding["tree_sha"] == git(repo, "rev-parse", f"{commit_a}^{{tree}}")
    assert batches["BAT-0008"]["current_head_binding"]["head_sha"] == commit_b
    task = latest_rows(_ledger(canary, Epoch2Collection.TASK))["EAWF-0042"]
    assert task["integrated_binding"]["head_sha"] == commit_a
    records = effective_records(read_ledger_records(_ledger(canary, Epoch2Collection.BATCH)))
    matched = next(r for r in records if r.record_key == "BMR-000000000001-BAT-0007")
    assert matched.payload["matched_head_sha"] == commit_a
    assert commit_a in matched.payload["reason"]
    receipt = effective_records(read_ledger_records(_ledger(canary, Epoch2Collection.RECEIPT)))[0]
    revision = receipt.payload["receipt"]["freshness"]["revision_binding"]
    # The receipt keeps the tree its gate ran on; only the commit identity moves.
    assert revision == {
        "head_sha": commit_b,
        "parent_sha": git(repo, "rev-parse", f"{commit_b}^"),
        "tree_sha": git(repo, "rev-parse", f"{shas[1]}^{{tree}}"),
    }
    assert not _effective_shas(canary) & set(shas)


def test_rebound_evidence_can_be_recorded_and_used_to_seal_approval(tmp_path: Path) -> None:
    canary, repo, base, shas = make_tree(tmp_path)
    ledger = _ledger(canary, Epoch2Collection.EVIDENCE)
    append_ledger_record(
        ledger,
        LedgerRecord(
            collection=Epoch2Collection.EVIDENCE,
            record_key="EVD-0001",
            status="recorded",
            recorded_at=AT,
            payload={
                "id": "EVD-0001",
                "kind": "decision",
                "summary": f"verified source commit {shas[2]}",
                "recorded_at": AT.isoformat(),
            },
        ),
    )
    squash_map = _squash_and_point(repo, base)
    rebind(tmp_path, canary, squash_map, "rebind-evidence")
    history = read_ledger_records(ledger)
    summary = f"verified source commit {squash_map.batches['BAT-0007']}"
    assert len(history) == 2
    assert history[0].payload["summary"] == f"verified source commit {shas[2]}"
    assert history[1].payload["summary"] == summary
    assert history[1].supersedes == line_digest(render_ledger_line(history[0]))
    milestone = seed_row("milestone", "ACTIVE")["urn"]

    recorded = asyncio.run(
        methods.dispatch(
            DELIVERY_RECORD_EVIDENCE_METHOD,
            method_context(tmp_path / "rt"),
            {
                "repo_root": str(canary.root),
                "urn": milestone,
                "actor": ACTOR,
                "idempotency_key": "record-rebound-evidence",
                "kind": "decision",
                "summary": summary,
            },
        )
    )

    assert recorded == {
        "evidence_ref": f"{milestone.rsplit('/', 2)[0]}/evidence/EVD-0001",
        "created": False,
    }
    action_ref = seal_plan_approval(
        canary.root, tmp_path / "rt", subject=milestone, tag="rebound-evidence"
    )
    action = read_document(document_path(canary))["pending_action"][action_ref.rsplit("/", 1)[1]]
    assert action["status"] == "SEALED"


def _accept(canary: CanaryProvision, head: str, tree: str) -> str:
    """File MLS-0030 as accepted at *head* on bundle revision one; return the bundle digest."""
    payload = acceptance_bundle_row()
    payload["accepted_binding"] = {
        **payload["accepted_binding"],
        "head_sha": head,
        "tree_sha": tree,
    }
    bundle = MilestoneAcceptanceBundle.model_validate(payload)
    append_ledger_record(
        _ledger(canary, Epoch2Collection.MILESTONE),
        LedgerRecord(
            collection=Epoch2Collection.MILESTONE,
            record_key="MAB-0001-MLS-0030",
            status="revision-1",
            recorded_at=AT,
            payload=payload,
        ),
    )
    milestone = seed_row("milestone", "COMPLETED")
    milestone["accepted_binding"] = payload["accepted_binding"]
    milestone["acceptance_bundle_revision"] = 1
    _compact(canary, Epoch2Collection.MILESTONE, milestone)
    approval = {**sealed_approval_row(), "bundle_digest": bundle.digest()}
    seed(canary, {Epoch2Collection.PENDING_ACTION.value: {"ACT-0001": approval}})
    return bundle.digest()


def _acceptance(tmp_path: Path, canary: CanaryProvision) -> Any:
    """Send ``projection.milestone.acceptance`` for MLS-0030 and return its answer."""
    call = {"repo_root": str(canary.root), "milestone_key": "MLS-0030"}
    return asyncio.run(
        methods.dispatch(MILESTONE_ACCEPTANCE_METHOD, method_context(tmp_path / "rt"), call)
    )


def test_a_rebound_accepted_milestone_reads_back_as_one_chain_with_its_approval(
    tmp_path: Path,
) -> None:
    canary, repo, base, shas = make_tree(tmp_path)
    approved = _accept(canary, shas[2], git(repo, "rev-parse", f"{shas[2]}^{{tree}}"))
    assert _acceptance(tmp_path, canary)["approval"]["approved_digest"] == approved
    squash_map = _squash_and_point(repo, base)
    commit_a = squash_map.batches["BAT-0007"]

    rebind(tmp_path, canary, squash_map, "rebind-1")
    answer = _acceptance(tmp_path, canary)

    bundle = MilestoneAcceptanceBundle.model_validate(answer["bundle"])
    assert bundle.revision == 1
    assert bundle.accepted_binding.head_sha == commit_a
    assert bundle.digest() != approved
    # The approval was given to the bytes before the rebind and still binds the bundle.
    assert answer["approval"]["approved_digest"] == bundle.digest()
    assert answer["accepted_binding"]["head_sha"] == commit_a


def test_a_bundle_changed_beyond_its_commits_loses_the_earlier_approval(tmp_path: Path) -> None:
    canary, repo, _, shas = make_tree(tmp_path)
    _accept(canary, shas[2], git(repo, "rev-parse", f"{shas[2]}^{{tree}}"))
    ledger = _ledger(canary, Epoch2Collection.MILESTONE)
    first = next(r for r in read_ledger_records(ledger) if r.record_key == "MAB-0001-MLS-0030")
    changed = copy.deepcopy(first.payload)
    changed["steps"][0]["observation"] = "a different observation"
    append_ledger_record(
        ledger,
        first.model_copy(
            update={"payload": changed, "supersedes": line_digest(render_ledger_line(first))}
        ),
    )

    answer = _acceptance(tmp_path, canary)

    assert answer["bundle"]["steps"][0]["observation"] == "a different observation"
    assert answer["approval"] is None


def test_a_second_rebind_appends_nothing_and_a_retry_replays(tmp_path: Path) -> None:
    canary, repo, base, _ = make_tree(tmp_path)
    squash_map = _squash_and_point(repo, base)
    first = rebind(tmp_path, canary, squash_map, "rebind-1")
    lines = _line_count(canary)

    again = rebind(tmp_path, canary, squash_map, "rebind-2")
    replay = rebind(tmp_path, canary, squash_map, "rebind-1")

    assert again["rows"] == 0
    assert replay == {**first, "replayed": True}
    assert _line_count(canary) == lines


def test_rebind_refuses_a_branch_not_at_the_squashed_tip(tmp_path: Path) -> None:
    canary, repo, base, _ = make_tree(tmp_path)
    squash_map = _squash_and_point(repo, base)
    git(repo, "update-ref", f"refs/heads/{BRANCH}", f"{squash_map.head}^")
    lines = _line_count(canary)

    with pytest.raises(DaemonValidationError, match=REBIND_HEAD_MOVED):
        rebind(tmp_path, canary, squash_map, "rebind-1")

    assert _line_count(canary) == lines


def test_rebind_refuses_a_batch_commit_without_its_trailer(tmp_path: Path) -> None:
    canary, repo, base, _ = make_tree(tmp_path)
    squash_map = _squash_and_point(repo, base)
    wrong = squash_map.model_copy(
        update={"batches": {**squash_map.batches, "BAT-0007": squash_map.batches["BAT-0008"]}}
    )
    lines = _line_count(canary)

    with pytest.raises(DaemonValidationError, match=REBIND_TRAILER_MISSING):
        rebind(tmp_path, canary, wrong, "rebind-1")

    assert _line_count(canary) == lines


def test_rebind_refuses_a_live_row_naming_a_source_commit(tmp_path: Path) -> None:
    canary, repo, base, shas = make_tree(tmp_path)
    squash_map = _squash_and_point(repo, base)
    live = copy.deepcopy(_batch("BAT-0009", shas[0], git(repo, "rev-parse", f"{shas[0]}^{{tree}}")))
    seed(canary, {"batch": {"BAT-0009": live}})
    lines = _line_count(canary)

    with pytest.raises(DaemonValidationError, match=REBIND_LIVE_ROW):
        rebind(tmp_path, canary, squash_map, "rebind-1")

    assert _line_count(canary) == lines


def test_an_evidence_only_task_rebinds_to_its_batch_commit(tmp_path: Path) -> None:
    canary, repo, base, shas = make_tree(tmp_path)
    tree_a = git(repo, "rev-parse", f"{shas[2]}^{{tree}}")
    # Landed on evidence alone: no commit anywhere names EAWF-0044.
    _compact(canary, Epoch2Collection.TASK, _task("EAWF-0044", "BAT-0007", shas[2], tree_a))
    squash_map = _squash_and_point(repo, base)

    answer = rebind(tmp_path, canary, squash_map, "rebind-1")

    task = latest_rows(_ledger(canary, Epoch2Collection.TASK))["EAWF-0044"]
    assert answer["ledgers"]["task"] == 3
    assert task["integrated_binding"]["head_sha"] == squash_map.batches["BAT-0007"]


def test_a_task_its_source_commits_name_but_the_squash_dropped_is_refused(
    tmp_path: Path,
) -> None:
    canary, repo, base, _ = make_tree(tmp_path)
    landed = _commit(repo, "c.txt", "c\n", "feat: c\n\nTask: EAWF-0045")
    _compact(
        canary,
        Epoch2Collection.TASK,
        _task("EAWF-0045", "BAT-0007", landed, git(repo, "rev-parse", f"{landed}^{{tree}}")),
    )
    # The squash was planned without EAWF-0045's placement, so no Task trailer names it.
    squash_map = _squash_and_point(repo, base)
    lines = _line_count(canary)

    with pytest.raises(DaemonValidationError, match="Task EAWF-0045 beside Batch BAT-0007"):
        rebind(tmp_path, canary, squash_map, "rebind-1")

    assert _line_count(canary) == lines


def _receipt(canary: CanaryProvision, key: str, task: str, head: str, parent: str) -> None:
    batch_urn = seed_row("batch", "COMPLETED")["urn"]
    append_ledger_record(
        _ledger(canary, Epoch2Collection.RECEIPT),
        LedgerRecord(
            collection=Epoch2Collection.RECEIPT,
            record_key=key,
            status="pass",
            recorded_at=AT,
            payload={
                "task_ref": f"{batch_urn.rsplit('/', 2)[0]}/task/{task}",
                "receipt": {
                    "freshness": {
                        "revision_binding": {
                            "head_sha": head,
                            "parent_sha": parent,
                            "tree_sha": "f" * 40,
                        }
                    }
                },
            },
        ),
    )


def test_a_second_squash_keeps_each_binding_parent_the_head_parent(tmp_path: Path) -> None:
    canary, repo, base, shas = make_tree(tmp_path)
    _receipt(canary, "PRF-0000000000000002-EAWF-0042", "EAWF-0042", shas[2], shas[1])
    first = _squash_and_point(repo, base)
    rebind(tmp_path, canary, first, "rebind-1")
    # A later commit of a third Batch: the second squash writes the first two
    # Batch commits again byte for byte, so it maps them onto themselves.
    _commit(repo, "d.txt", "d\n", "feat: d\n\nTask: EAWF-0046")
    second = squash(
        repo,
        base=base,
        source=BRANCH,
        branch=BRANCH,
        task_batches={**TASKS, "EAWF-0046": "BAT-0009"},
        into="scratch/squashed-2",
        on_conflict="merge",
    ).map
    git(repo, "update-ref", f"refs/heads/{BRANCH}", "scratch/squashed-2")
    commit_a = second.batches["BAT-0007"]
    assert commit_a == first.batches["BAT-0007"]

    rebind(tmp_path, canary, second, "rebind-2")

    receipts = effective_records(read_ledger_records(_ledger(canary, Epoch2Collection.RECEIPT)))
    revision = next(r for r in receipts if r.record_key.endswith("EAWF-0042")).payload["receipt"][
        "freshness"
    ]["revision_binding"]
    assert revision["head_sha"] == commit_a
    assert revision["parent_sha"] == git(repo, "rev-parse", f"{commit_a}^")


def _orphan(repo: Path, content: str) -> str:
    """Write a commit no branch reaches, as a rewritten-away history leaves one."""
    tree = git(repo, "write-tree")
    return subprocess.run(
        ["git", "commit-tree", tree, "-m", content],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _publish(repo: Path, base: str) -> str:
    """Publish a commit on top of *base* that the branch does not carry, and return it."""
    tree = git(repo, "rev-parse", f"{base}^{{tree}}")
    published = subprocess.run(
        ["git", "commit-tree", tree, "-p", base, "-m", "published"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git(repo, "update-ref", "refs/remotes/origin/main", published)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return published


def _orphan_map(repo: Path, commits: dict[str, str | None], dropped: dict[str, str]) -> SquashMap:
    head = git(repo, "rev-parse", BRANCH)
    return SquashMap(
        branch=BRANCH,
        base=head,
        source_head=head,
        head=head,
        commits=commits,
        dropped=dropped,
        batches={},
    )


def test_an_orphan_rebinds_to_its_published_equivalent_or_is_dropped(tmp_path: Path) -> None:
    canary, repo, base, _ = make_tree(tmp_path)
    published = _publish(repo, base)
    bound, quoted = _orphan(repo, "bound"), _orphan(repo, "quoted")
    _receipt(canary, "PRF-0000000000000003-EAWF-0042", "EAWF-0042", bound, base)
    append_ledger_record(
        _ledger(canary, Epoch2Collection.RECEIPT),
        LedgerRecord(
            collection=Epoch2Collection.RECEIPT,
            record_key="call-0000000000000001",
            status="succeeded",
            recorded_at=AT,
            payload={"result": {"bounded_output": {"text": f"one\nfixture {quoted}\ntwo"}}},
        ),
    )
    orphan_map = _orphan_map(
        repo, {bound: published, quoted: None}, {quoted: "quoted fixture text"}
    )

    answer = rebind(tmp_path, canary, orphan_map, "orphan-1")

    assert answer["rows"] == 2
    receipts = {
        r.record_key: r.payload
        for r in effective_records(read_ledger_records(_ledger(canary, Epoch2Collection.RECEIPT)))
    }
    binding = receipts["PRF-0000000000000003-EAWF-0042"]["receipt"]["freshness"]["revision_binding"]
    assert binding["head_sha"] == published
    assert binding["parent_sha"] == base
    text = receipts["call-0000000000000001"]["result"]["bounded_output"]["text"]
    assert quoted not in text and "quoted fixture text" in text
    assert not _effective_shas(canary) & {bound, quoted}


@pytest.mark.parametrize("case", ["unpublished-equivalent", "branch-commit-off-branch"])
def test_an_orphan_map_refuses_an_equivalent_off_main_or_a_non_orphan(
    tmp_path: Path, case: str
) -> None:
    canary, repo, base, shas = make_tree(tmp_path)
    published = _publish(repo, base)
    orphan = _orphan(repo, "orphan")
    commits = (
        {orphan: _orphan(repo, "elsewhere")} if case.startswith("unpub") else {shas[0]: published}
    )
    lines = _line_count(canary)

    with pytest.raises(DaemonValidationError, match="rebind_commit_off_branch"):
        rebind(tmp_path, canary, _orphan_map(repo, commits, {}), "orphan-1")

    assert _line_count(canary) == lines


def test_cli_rebind_sends_the_map_file(tmp_path: Path, cli: None) -> None:
    canary, repo, base, _ = make_tree(tmp_path)
    squash_map = _squash_and_point(repo, base)
    map_file = tmp_path / "map.json"
    map_file.write_bytes(orjson.dumps(squash_map.model_dump(mode="json")))

    result = runner.invoke(
        app,
        [
            "--workspace", str(canary.root), "--json", "batch", "rebind", "--map-file",
            str(map_file), "--idempotency-key", "rebind-cli-1", "--actor", ACTOR,
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert orjson.loads(result.stdout)["result"]["rows"] == 6
