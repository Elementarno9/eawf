"""Every gate the apply has to fail closed on, and the read that cannot fail.

The apply is the only epoch-2 verb that writes, so most of its surface is
refusals. Each one is asserted here against the real gate rather than
against a mock, because a gate that is exercised only through a double is
a gate nobody has run.

The outermost gate gets the most attention. An epoch-2 apply may only
write into a tree that has declared itself throwaway, and the declaration
is checked before the workspace is read, before plan mode runs and before
the first URN is minted. This repository's own ``.ea/`` carries no
declaration, and there is a test that proves it -- a read, never an apply.

The quiescence gate is itemised rather than first-past-the-post: an
operator clearing a cutover has to clear every holder, so one run names
all of them. Each holder kind has a fault fixture under
``tests/fixtures/migration/cutover-faults/`` so the gate is driven by data
an operator can read rather than by a construction buried in a test.

Last comes the verb that is the apply's counterweight: the read-only
export, which is total over the declared collections and has no write path
at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import portalocker
import pytest
from pydantic import ValidationError as PydanticValidationError
from typer.testing import CliRunner

from eawf import __version__
from eawf.kernel.migration.epoch2.apply import (
    Epoch2ApplyRequest,
    apply_cutover,
    authority_locks,
    require_unresolved_rows_accepted,
)
from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    CanaryDeclaration,
    DisposableTarget,
    read_declaration,
)
from eawf.kernel.migration.epoch2.dispositions import COLLECTION_DISPOSITION_INDEX
from eawf.kernel.migration.epoch2.errors import (
    MigrationNotQuiescentError,
    MigrationPlanDigestStaleError,
    MigrationPlanNotApplicableError,
    MigrationTargetNotDisposableError,
    MigrationWorkspaceNotRegisteredError,
)
from eawf.kernel.migration.epoch2.export import Epoch2ExportRequest, export_epoch1, export_text
from eawf.kernel.migration.epoch2.journal import CutoverStage, read_journal
from eawf.kernel.migration.epoch2.plan_mode import (
    Epoch2PlanRequest,
    MigrationPlan,
    plan_cutover,
)
from eawf.kernel.migration.epoch2.quiescence import (
    QuiescenceHolderKind,
    quiescence_findings,
    require_quiescent,
)
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state_worktree import worktree_reconcile_rpc
from eawf.runtime.lock import portalock
from eawf.runtime.worktree.reconcile import RECONCILE_COMMAND
from eawf.surfaces.cli.app import app
from tests.integration.kernel.migration._corpus_shapes import DEFAULT_TRACK_KEY

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
FULL_SNAPSHOT = FIXTURES / "epoch1-full" / "snapshot"
ALLOWLIST = FIXTURES / "allowed_legacy_symbols.txt"
FAULTS = FIXTURES / "cutover-faults"
CANARY_DECLARATION = FAULTS / "canary" / CANARY_DECLARATION_FILENAME
REGISTRY = FAULTS / "registry" / "registry.json"
REGISTRY_WITHOUT_WORKSPACE = FAULTS / "registry-without-workspace" / "registry.json"

WORKSPACE_KEY = "WSP-DEFAULT"
PROJECT_KEY = "PRJ-DEMO"
REPOSITORY_KEY = "REP-DEMO"
SEALED_BY = "apply-guards-test"
APPLIED_AT = datetime(2026, 1, 1, tzinfo=UTC)

#: A digest of the right shape that no plan over this corpus computes.
STALE_DIGEST = "f" * 64

#: A collection whose conversion no importer rule implements, and the row
#: seeded into it so the guard corpus has something unplaceable.
UNCONVERTED_COLLECTION = "hypotheses"
UNCONVERTED_ROW = "H01-01"

runner = CliRunner()


def plan_request_for(corpus: Path) -> Epoch2PlanRequest:
    """Return the plan request the guards drive the apply with."""
    return Epoch2PlanRequest(
        snapshot_root=str(corpus),
        allowlist_path=str(ALLOWLIST),
        workspace_key=WORKSPACE_KEY,
        project_key=PROJECT_KEY,
        repository_key=REPOSITORY_KEY,
        sealed_by=SEALED_BY,
        default_track_key=DEFAULT_TRACK_KEY,
    )


def declared_canary(root: Path) -> Path:
    """Create ``root`` and declare it a disposable canary."""
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CANARY_DECLARATION, root / CANARY_DECLARATION_FILENAME)
    return root


def seeded_target(root: Path, fault: str) -> Path:
    """Return a declared canary seeded with one fault fixture's tree."""
    shutil.copytree(FAULTS / fault, root, dirs_exist_ok=True)
    return declared_canary(root)


def apply_request(
    *,
    corpus: Path,
    target_root: Path,
    plan_digest: str,
    registry: Path = REGISTRY,
    accepted: tuple[str, ...] = (),
) -> Epoch2ApplyRequest:
    """Return an apply request aimed at ``target_root``."""
    return Epoch2ApplyRequest(
        plan_request=plan_request_for(corpus),
        target_root=str(target_root),
        registry_path=str(registry),
        plan_digest=plan_digest,
        accepted_unresolved_rows=accepted,
    )


def tree_digests(root: Path) -> dict[str, str]:
    """Return a digest per file under ``root``, keyed by relative path."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def corpus_plan(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, MigrationPlan]:
    """One corpus copy and its sealed plan, shared across the guard tests.

    The copy carries one hypothesis row, a collection whose conversion is
    declared but not implemented, so the plan names an unresolved row for
    the waiver guards to refuse over.
    """
    corpus = tmp_path_factory.mktemp("guards_corpus") / "staged"
    shutil.copytree(FULL_SNAPSHOT, corpus)
    document_path = corpus / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document[UNCONVERTED_COLLECTION] = {UNCONVERTED_ROW: {"id": UNCONVERTED_ROW}}
    document_path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    return corpus, plan_cutover(plan_request_for(corpus), sealed_at=APPLIED_AT)


def test_canary_fence_refuses_a_tree_carrying_no_declaration(
    tmp_path: Path, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """A tree that cannot say it is disposable is treated as production."""
    corpus, plan = corpus_plan
    target_root = tmp_path / ".ea"
    target_root.mkdir()

    with pytest.raises(MigrationTargetNotDisposableError) as excinfo:
        apply_cutover(
            apply_request(corpus=corpus, target_root=target_root, plan_digest=plan.approval_digest),
            applied_at=APPLIED_AT,
        )

    assert excinfo.value.code == "migration_target_not_disposable"
    assert CANARY_DECLARATION_FILENAME in str(excinfo.value)
    assert tree_digests(target_root) == {}


def test_canary_fence_refuses_a_tree_that_declares_itself_not_disposable(
    tmp_path: Path, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """The declaration states the claim in words, so ``false`` still refuses."""
    corpus, plan = corpus_plan
    target_root = tmp_path / ".ea"
    target_root.mkdir()
    shutil.copyfile(
        FAULTS / "undeclared-canary" / CANARY_DECLARATION_FILENAME,
        target_root / CANARY_DECLARATION_FILENAME,
    )

    with pytest.raises(MigrationTargetNotDisposableError):
        apply_cutover(
            apply_request(corpus=corpus, target_root=target_root, plan_digest=plan.approval_digest),
            applied_at=APPLIED_AT,
        )


def test_canary_fence_refuses_a_hand_built_target(tmp_path: Path) -> None:
    """The fence lives in the type, so constructing one directly re-reads it."""
    root = tmp_path / ".ea"
    root.mkdir()
    declaration = CanaryDeclaration(
        disposable=True, declared_by="a-liar", purpose="claiming what the tree does not say"
    )

    with pytest.raises(MigrationTargetNotDisposableError):
        DisposableTarget(root=root, declaration=declaration)


def test_canary_fence_refuses_a_declaration_that_is_not_json(tmp_path: Path) -> None:
    """An unparseable declaration is no declaration, not a lenient one."""
    root = tmp_path / ".ea"
    root.mkdir()
    (root / CANARY_DECLARATION_FILENAME).write_text("not json at all", encoding="utf-8")

    with pytest.raises(MigrationTargetNotDisposableError, match="not valid JSON"):
        read_declaration(root)


def test_canary_fence_refuses_this_repository_own_state_directory() -> None:
    """This repository is not a canary, and the fence is what says so.

    A read, never an apply: the assertion is that the declaration is
    absent, which is exactly what the apply checks first.
    """
    with pytest.raises(MigrationTargetNotDisposableError) as excinfo:
        read_declaration(REPO_ROOT / ".ea")

    assert excinfo.value.code == "migration_target_not_disposable"
    assert not (REPO_ROOT / ".ea" / CANARY_DECLARATION_FILENAME).exists()


def test_canary_fence_runs_before_the_workspace_is_read(
    tmp_path: Path, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """The fence is outermost: an undeclared tree refuses on the fence.

    The request carries a registry with no workspaces at all, so both
    gates would fire. The fence wins, which is what "outermost" means.
    """
    corpus, plan = corpus_plan
    target_root = tmp_path / ".ea"
    target_root.mkdir()

    with pytest.raises(MigrationTargetNotDisposableError):
        apply_cutover(
            apply_request(
                corpus=corpus,
                target_root=target_root,
                plan_digest=plan.approval_digest,
                registry=REGISTRY_WITHOUT_WORKSPACE,
            ),
            applied_at=APPLIED_AT,
        )


def test_canary_fence_holds_while_the_export_verb_writes_nothing(
    tmp_path: Path, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """The one verb an undeclared tree may serve reads every collection.

    Two claims, because they are the same claim from two sides: the apply
    refuses an undeclared tree, and the read-only export over the same
    corpus is total over the declared collections and leaves the corpus
    and the tree byte-identical.
    """
    corpus, plan = corpus_plan
    target_root = tmp_path / ".ea"
    target_root.mkdir()
    before_corpus = tree_digests(corpus)
    before_target = tree_digests(target_root)

    with pytest.raises(MigrationTargetNotDisposableError):
        apply_cutover(
            apply_request(corpus=corpus, target_root=target_root, plan_digest=plan.approval_digest),
            applied_at=APPLIED_AT,
        )
    payload = export_epoch1(Epoch2ExportRequest(snapshot_root=str(corpus)))

    assert payload["status"] == "ok"
    assert payload["collection_count"] == len(COLLECTION_DISPOSITION_INDEX)
    assert payload["undeclared_collections"] == []
    assert {row["source_collection"] for row in payload["collections"]} == set(
        COLLECTION_DISPOSITION_INDEX
    )
    assert tree_digests(corpus) == before_corpus
    assert tree_digests(target_root) == before_target


def test_the_export_reports_an_empty_collection_rather_than_omitting_it(
    corpus_plan: tuple[Path, MigrationPlan],
) -> None:
    """Absent is not empty, so an empty collection still carries a row."""
    corpus, _plan = corpus_plan

    payload = export_epoch1(Epoch2ExportRequest(snapshot_root=str(corpus)))

    empty = [row for row in payload["collections"] if row["row_count"] == 0]
    assert empty
    assert all(row["proof_form"] is not None for row in empty)


def test_the_export_text_names_every_collection_it_emitted(
    corpus_plan: tuple[Path, MigrationPlan],
) -> None:
    """The terminal rendering is total over the same rows as the envelope."""
    corpus, _plan = corpus_plan

    payload = export_epoch1(Epoch2ExportRequest(snapshot_root=str(corpus)))
    rendered = export_text(payload)

    assert rendered.splitlines()[0].startswith("epoch1 export:")
    for row in payload["collections"]:
        assert row["source_collection"] in rendered


def test_the_export_refuses_a_snapshot_root_that_is_not_there(tmp_path: Path) -> None:
    """A corpus the exporter cannot read yields no export, not a partial one."""
    from eawf.kernel.migration.epoch2.errors import MigrationSourceUnreadableError

    with pytest.raises(MigrationSourceUnreadableError):
        export_epoch1(Epoch2ExportRequest(snapshot_root=str(tmp_path / "absent")))


def test_quiescence_refuses_an_active_session(tmp_path: Path) -> None:
    """A live session can mutate a surface the generation was derived from."""
    target = DisposableTarget.require(seeded_target(tmp_path / ".ea", "active-session"))

    findings = quiescence_findings(target)

    assert [finding.kind for finding in findings] == [QuiescenceHolderKind.ACTIVE_SESSION]
    assert findings[0].locator == "state.json:agent_sessions/SES-LIVE"
    with pytest.raises(MigrationNotQuiescentError) as excinfo:
        require_quiescent(findings)
    assert excinfo.value.code == "migration_not_quiescent"
    assert "SES-LIVE" in str(excinfo.value)


def test_quiescence_refuses_a_held_lease(tmp_path: Path) -> None:
    """A lease record present refuses without reaping somebody else's lock."""
    target = DisposableTarget.require(seeded_target(tmp_path / ".ea", "held-lease"))

    findings = quiescence_findings(target)

    assert [finding.kind for finding in findings] == [QuiescenceHolderKind.HELD_LEASE]
    assert findings[0].locator == "locks/worktrees.lock"
    with pytest.raises(MigrationNotQuiescentError, match=r"worktrees\.lock"):
        require_quiescent(findings)


def test_quiescence_findings_passes_a_lease_the_lock_primitive_released(tmp_path: Path) -> None:
    """Every locked write leaves an empty lease behind, and that is not a holder.

    The lock primitive keeps the inode and empties it on release, so a
    tree that was ever written to carries these files; refusing them would
    make the cutover unreachable on any repository with a history.
    """
    target = DisposableTarget.require(seeded_target(tmp_path / ".ea", "quiescent"))
    with portalock.acquire(target.root / "locks" / "worktrees.lock", timeout=0.2):
        pass
    released = target.root / "locks" / "worktrees.lock.lock"

    findings = quiescence_findings(target)

    assert released.is_file()
    assert released.stat().st_size == 0
    assert findings == ()
    require_quiescent(findings)


def test_quiescence_findings_refuses_an_empty_lease_that_is_still_locked(tmp_path: Path) -> None:
    """A holder caught between taking the lock and writing its record still holds."""
    target = DisposableTarget.require(seeded_target(tmp_path / ".ea", "quiescent"))
    lease = target.root / "locks" / "worktrees.lock.lock"
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.touch()

    with lease.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        findings = quiescence_findings(target)
        portalocker.unlock(handle)

    assert [finding.locator for finding in findings] == ["locks/worktrees.lock.lock"]
    assert quiescence_findings(target) == ()
    with pytest.raises(MigrationNotQuiescentError, match=r"worktrees\.lock\.lock"):
        require_quiescent(findings)


def test_quiescence_refuses_a_pending_write_ahead_record(tmp_path: Path) -> None:
    """A mutation mid-flight is a mutation the cutover would lose."""
    target = DisposableTarget.require(seeded_target(tmp_path / ".ea", "pending-wal"))

    findings = quiescence_findings(target)

    assert [finding.kind for finding in findings] == [QuiescenceHolderKind.PENDING_WAL_RECORD]
    assert findings[0].locator.endswith("wal-canary-0001.pending.json")
    with pytest.raises(MigrationNotQuiescentError, match="pending"):
        require_quiescent(findings)


def test_quiescence_refuses_a_managed_worktree(tmp_path: Path) -> None:
    """A live worktree is an agent still writing into the tree's branch."""
    target = DisposableTarget.require(seeded_target(tmp_path / ".ea", "managed-worktree"))

    findings = quiescence_findings(target)

    assert [finding.kind for finding in findings] == [QuiescenceHolderKind.MANAGED_WORKTREE]
    assert findings[0].locator == "state.json:worktrees/WT-LIVE"
    with pytest.raises(MigrationNotQuiescentError, match="WT-LIVE"):
        require_quiescent(findings)


def test_quiescence_itemises_every_holder_in_one_run(tmp_path: Path) -> None:
    """An operator clearing a cutover sees all four holders at once."""
    root = tmp_path / ".ea"
    for fault in ("active-session", "held-lease", "pending-wal"):
        shutil.copytree(FAULTS / fault, root, dirs_exist_ok=True)
    document = json.loads((root / "state.json").read_text(encoding="utf-8"))
    document["worktrees"] = {"WT-LIVE": {"id": "WT-LIVE", "status": "active"}}
    (root / "state.json").write_text(json.dumps(document), encoding="utf-8")
    target = DisposableTarget.require(declared_canary(root))

    findings = quiescence_findings(target)

    assert [finding.kind for finding in findings] == [
        QuiescenceHolderKind.ACTIVE_SESSION,
        QuiescenceHolderKind.HELD_LEASE,
        QuiescenceHolderKind.PENDING_WAL_RECORD,
        QuiescenceHolderKind.MANAGED_WORKTREE,
    ]
    with pytest.raises(MigrationNotQuiescentError) as excinfo:
        require_quiescent(findings)
    assert "4 holders" in str(excinfo.value)


def test_quiescence_passes_a_tree_whose_holders_have_all_finished(tmp_path: Path) -> None:
    """Closed sessions and abandoned worktrees hold nothing, so nothing fires."""
    target = DisposableTarget.require(seeded_target(tmp_path / ".ea", "quiescent"))

    assert quiescence_findings(target) == ()
    require_quiescent(())


def test_quiescence_passes_an_empty_tree(tmp_path: Path) -> None:
    """The boundary case: a tree with no document holds no holder."""
    target = DisposableTarget.require(declared_canary(tmp_path / ".ea"))

    assert quiescence_findings(target) == ()


def test_an_unregistered_workspace_refuses_before_the_first_urn_is_minted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """Plan mode mints URNs, so the workspace check has to precede it."""
    corpus, plan = corpus_plan
    target_root = declared_canary(tmp_path / ".ea")
    minted: list[str] = []

    def _record_and_fail(*_args: object, **_kwargs: object) -> None:
        minted.append("plan_cutover")
        raise AssertionError("plan mode ran before the workspace was confirmed")

    monkeypatch.setattr("eawf.kernel.migration.epoch2.apply.plan_cutover", _record_and_fail)

    with pytest.raises(MigrationWorkspaceNotRegisteredError) as excinfo:
        apply_cutover(
            apply_request(
                corpus=corpus,
                target_root=target_root,
                plan_digest=plan.approval_digest,
                registry=REGISTRY_WITHOUT_WORKSPACE,
            ),
            applied_at=APPLIED_AT,
        )

    assert excinfo.value.code == "workspace_not_registered"
    assert minted == []
    assert not target_root.joinpath("generations").exists()


def test_an_unreadable_registry_refuses_as_an_unregistered_workspace(
    tmp_path: Path, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """A registry nobody can read cannot confirm the addressing key either."""
    corpus, plan = corpus_plan
    target_root = declared_canary(tmp_path / ".ea")

    with pytest.raises(MigrationWorkspaceNotRegisteredError):
        apply_cutover(
            apply_request(
                corpus=corpus,
                target_root=target_root,
                plan_digest=plan.approval_digest,
                registry=tmp_path / "no-registry.json",
            ),
            applied_at=APPLIED_AT,
        )


def machine_home(root: Path, monkeypatch: pytest.MonkeyPatch, *, registry: Path | None) -> Path:
    """Point ``HOME`` at ``root``, optionally seeding its machine registry.

    The CLI resolves ``~/.eawf/registry.json`` from ``HOME``, so the
    registration and the apply below both land in the tmp tree and never
    in the operator's real registry. The daemon is opted out so neither
    verb reaches a daemon serving somebody else's home.
    """
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    if registry is not None:
        target = root / ".eawf" / "registry.json"
        target.parent.mkdir(parents=True)
        shutil.copyfile(registry, target)
    return root


def cli_apply(
    *, corpus: Path, target_root: Path, plan: MigrationPlan, extra: tuple[str, ...] = ()
) -> list[str]:
    """Return the ``migrate epoch2 --apply`` argv, naming no registry file."""
    accepted: list[str] = []
    for row in plan.manifest.unresolved_rows:
        accepted += ["--accept-unresolved", row.address]
    return [
        "--json",
        "migrate",
        "epoch2",
        "--apply",
        "--snapshot-root",
        str(corpus),
        "--allowlist",
        str(ALLOWLIST),
        "--workspace-key",
        WORKSPACE_KEY,
        "--project-key",
        PROJECT_KEY,
        "--repository-key",
        REPOSITORY_KEY,
        "--sealed-by",
        SEALED_BY,
        "--default-track-key",
        DEFAULT_TRACK_KEY,
        "--target-root",
        str(target_root),
        "--plan-digest",
        plan.approval_digest,
        *accepted,
        *extra,
    ]


def test_epoch2_apply_resolves_the_workspace_registered_through_the_workspace_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """The cutover input the machine registry lacks today, end to end.

    Against a machine registry whose workspaces are empty the apply
    refuses before it writes; once ``eawf workspace add`` has filed the
    record, the same command resolves it and journals ``WORKSPACE_RESOLVED``.
    """
    corpus, plan = corpus_plan
    home = machine_home(tmp_path / "home", monkeypatch, registry=REGISTRY_WITHOUT_WORKSPACE)
    target_root = declared_canary(tmp_path / ".ea")
    argv = cli_apply(corpus=corpus, target_root=target_root, plan=plan)

    refused = runner.invoke(app, argv)

    assert refused.exit_code != 0
    assert "workspace_not_registered" in refused.output
    assert not target_root.joinpath("generations").exists()
    assert read_journal(DisposableTarget.require(target_root).journal_path) == ()

    registered = runner.invoke(app, ["workspace", "add", WORKSPACE_KEY, "--home", PROJECT_KEY])
    assert registered.exit_code == 0, registered.output
    machine_registry = json.loads((home / ".eawf" / "registry.json").read_text())
    assert WORKSPACE_KEY in machine_registry["workspaces"]

    applied = runner.invoke(app, argv)

    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["status"] == "applied"
    rows = read_journal(DisposableTarget.require(target_root).journal_path)
    resolved = [row for row in rows if row.stage is CutoverStage.WORKSPACE_RESOLVED]
    assert len(resolved) == 1
    assert WORKSPACE_KEY in resolved[0].detail


def test_epoch2_apply_refuses_when_the_machine_registry_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """A machine that never registered anything cannot address the corpus."""
    corpus, plan = corpus_plan
    machine_home(tmp_path / "home", monkeypatch, registry=None)
    target_root = declared_canary(tmp_path / ".ea")

    result = runner.invoke(app, cli_apply(corpus=corpus, target_root=target_root, plan=plan))

    assert result.exit_code != 0
    assert "workspace_not_registered" in result.output
    assert not target_root.joinpath("generations").exists()


def test_epoch2_apply_prefers_an_explicit_registry_over_the_machine_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """``--registry-path`` still wins, so a rehearsal can pin its own registry."""
    corpus, plan = corpus_plan
    machine_home(tmp_path / "home", monkeypatch, registry=REGISTRY)
    target_root = declared_canary(tmp_path / ".ea")
    argv = cli_apply(
        corpus=corpus,
        target_root=target_root,
        plan=plan,
        extra=("--registry-path", str(REGISTRY_WITHOUT_WORKSPACE)),
    )

    result = runner.invoke(app, argv)

    assert result.exit_code != 0
    assert "workspace_not_registered" in result.output
    assert not target_root.joinpath("generations").exists()


def test_a_stale_plan_digest_refuses_and_writes_nothing(
    tmp_path: Path, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """An approval that is not the plan this corpus now computes is refused."""
    corpus, _plan = corpus_plan
    target_root = declared_canary(tmp_path / ".ea")

    with pytest.raises(MigrationPlanDigestStaleError) as excinfo:
        apply_cutover(
            apply_request(corpus=corpus, target_root=target_root, plan_digest=STALE_DIGEST),
            applied_at=APPLIED_AT,
        )

    assert excinfo.value.code == "migration_plan_digest_stale"
    assert STALE_DIGEST in str(excinfo.value)
    assert read_journal(DisposableTarget.require(target_root).journal_path) == ()
    assert not target_root.joinpath("generations").exists()


def test_an_unresolved_row_the_apply_does_not_accept_refuses(
    tmp_path: Path, corpus_plan: tuple[Path, MigrationPlan]
) -> None:
    """The staged write tolerates an unplaceable row; the apply does not."""
    corpus, plan = corpus_plan
    target_root = declared_canary(tmp_path / ".ea")
    assert plan.manifest.unresolved_rows

    with pytest.raises(MigrationPlanNotApplicableError) as excinfo:
        apply_cutover(
            apply_request(corpus=corpus, target_root=target_root, plan_digest=plan.approval_digest),
            applied_at=APPLIED_AT,
        )

    assert excinfo.value.code == "migration_plan_not_applicable"
    for row in plan.manifest.unresolved_rows:
        assert row.address in str(excinfo.value)
    assert not target_root.joinpath("generations").exists()


def test_a_partial_acknowledgement_of_unresolved_rows_refuses(
    corpus_plan: tuple[Path, MigrationPlan],
) -> None:
    """Accepting some rows and not the rest still leaves rows unaccounted for."""
    _corpus, plan = corpus_plan
    addresses = tuple(row.address for row in plan.manifest.unresolved_rows)

    with pytest.raises(MigrationPlanNotApplicableError, match=addresses[-1]):
        require_unresolved_rows_accepted(plan, accepted=addresses[:-1])


def test_an_acknowledgement_the_plan_does_not_name_refuses(
    corpus_plan: tuple[Path, MigrationPlan],
) -> None:
    """A waiver written against another plan is not a waiver for this one."""
    _corpus, plan = corpus_plan
    addresses = tuple(row.address for row in plan.manifest.unresolved_rows)

    with pytest.raises(MigrationPlanNotApplicableError, match="different plan"):
        require_unresolved_rows_accepted(plan, accepted=(*addresses, "decisions/NOT-A-ROW"))


def test_the_full_acknowledgement_of_unresolved_rows_passes(
    corpus_plan: tuple[Path, MigrationPlan],
) -> None:
    """Naming every unplaceable row is the one way past the refusal."""
    _corpus, plan = corpus_plan

    require_unresolved_rows_accepted(
        plan, accepted=tuple(row.address for row in plan.manifest.unresolved_rows)
    )


def test_the_authority_locks_shut_the_fallback_writer_out(tmp_path: Path) -> None:
    """A fallback writer takes the same sibling lock, so it cannot proceed.

    This is the mechanism behind "fallback writers disabled": the direct
    ``portalocker`` path a daemonless mutation falls back to acquires the
    lock this context manager is holding.
    """
    target = DisposableTarget.require(declared_canary(tmp_path / ".ea"))

    with authority_locks(target) as locators:
        assert "state.json" in locators
        with (
            pytest.raises(portalock.LockTimeout),
            portalock.acquire(target.root / "state.json", timeout=0.2),
        ):
            pass

    with portalock.acquire(target.root / "state.json", timeout=0.2):
        pass


def test_a_generation_id_cannot_address_a_tree_outside_the_fence(tmp_path: Path) -> None:
    """Every generation path is a plain directory name under the target."""
    target = DisposableTarget.require(declared_canary(tmp_path / ".ea"))

    for bad in ("", "../escape", "nested/child", ".staging-a"):
        with pytest.raises(ValueError, match="plain directory name"):
            target.generation_path(bad)


def test_the_export_verb_through_the_cli_writes_nothing(
    corpus_plan: tuple[Path, MigrationPlan],
) -> None:
    """The operator surface for the export is read-only end to end."""
    corpus, _plan = corpus_plan
    before = tree_digests(corpus)

    result = runner.invoke(
        app, ["--json", "migrate", "epoch2", "--export", "--snapshot-root", str(corpus)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["collection_count"] == len(COLLECTION_DISPOSITION_INDEX)
    assert tree_digests(corpus) == before


def test_the_export_verb_needs_no_addressing_slots(
    corpus_plan: tuple[Path, MigrationPlan],
) -> None:
    """A read that mints nothing asks for no workspace, project or repository."""
    corpus, _plan = corpus_plan

    result = runner.invoke(app, ["migrate", "epoch2", "--export", "--snapshot-root", str(corpus)])

    assert result.exit_code == 0, result.output
    assert "epoch1 export:" in result.output


# ---- stale holder reconcile ------------------------------------------------

BOOTED_AT = datetime(2026, 6, 1, tzinfo=UTC)


def _git(repo: Path, *args: str) -> None:
    """Run one git command inside ``repo`` with a throwaway identity."""
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
    )


def _repo_with_document(root: Path, document: dict[str, Any]) -> Path:
    """Return a git repository whose ``.ea`` holds ``document`` as its state."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    (repo / ".ea" / "store").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / ".ea" / "config.yaml", repo / ".ea" / "config.yaml")
    (repo / ".ea" / "state.json").write_text(json.dumps(document), encoding="utf-8")
    return repo


def _daemon_context(
    repo: Path, tmp_path: Path, *, booted_at: datetime = BOOTED_AT
) -> MethodContext:
    """Return a daemon context that booted at ``booted_at``."""
    return MethodContext(
        started_at=booted_at.isoformat(),
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        bus=EventBus(),
        state_path=repo / ".ea" / "state.json",
        wal_dir=tmp_path / "wal",
    )


def _reconcile(ctx: MethodContext, repo: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Drive the daemon verb once."""
    return asyncio.run(worktree_reconcile_rpc(ctx, {"repo_root": str(repo), "dry_run": dry_run}))


def _staged_findings(repo: Path, destination: Path) -> tuple[Any, ...]:
    """Stage ``repo``'s ``.ea`` document as a declared canary target and probe it."""
    staged = declared_canary(destination)
    shutil.copyfile(repo / ".ea" / "state.json", staged / "state.json")
    return quiescence_findings(DisposableTarget.require(staged))


def _synthetic_document() -> dict[str, Any]:
    """Return the live document with hand-built worktree and session rows."""
    document = json.loads((REPO_ROOT / ".ea" / "state.json").read_text(encoding="utf-8"))
    template_wt = next(iter(document["worktrees"].values()))
    template_ses = next(
        row for row in document["agent_sessions"].values() if row["status"] != "active"
    )

    def worktree(key: str, path: str, status: str) -> dict[str, Any]:
        return {**template_wt, "id": key, "path": path, "status": status}

    def session(key: str, started: str, worktree_ids: list[str]) -> dict[str, Any]:
        return {
            **template_ses,
            "id": key,
            "status": "active",
            "ended_at": None,
            "summary": None,
            "started_at": started,
            "worktree_ids": worktree_ids,
        }

    after_boot = "2026-07-01T00:00:00Z"
    document["worktrees"] = {
        "WT-KEEP": worktree("WT-KEEP", ".ea/worktrees/keep", "active"),
        "WT-GONE": worktree("WT-GONE", ".ea/worktrees/gone", "active"),
        "WT-DONE": worktree("WT-DONE", ".ea/worktrees/done", "merged"),
    }
    document["agent_sessions"] = {
        "SES-ON-GONE": session("SES-ON-GONE", after_boot, ["WT-GONE"]),
        "SES-ON-KEEP": session("SES-ON-KEEP", after_boot, ["WT-KEEP"]),
        "SES-OLD": session("SES-OLD", "2026-05-01T00:00:00Z", []),
        "SES-NEW": session("SES-NEW", after_boot, []),
    }
    document["current"]["active_session_ids"] = sorted(document["agent_sessions"])
    return document


def test_worktree_reconcile_rpc_empties_quiescence_over_the_staged_live_corpus(
    tmp_path: Path,
) -> None:
    """Every stale active row in the committed corpus is retired with its reason."""
    live = json.loads((REPO_ROOT / ".ea" / "state.json").read_text(encoding="utf-8"))
    repo = _repo_with_document(tmp_path, live)
    before = _staged_findings(repo, tmp_path / "before")
    assert before, "the committed corpus should carry stale holders to reconcile"

    # The daemon serving the reconcile booted after the corpus was committed.
    ctx = _daemon_context(repo, tmp_path, booted_at=datetime.now(UTC))
    result = _reconcile(ctx, repo)

    assert result["written"] is True
    retired = {(row["kind"], row["row_id"]) for row in result["retired"]}
    assert all(row["reason"] for row in result["retired"])
    expected = {
        ("worktree", key) for key, row in live["worktrees"].items() if row["status"] == "active"
    }
    assert expected <= retired
    assert len(before) == len(retired)
    assert _staged_findings(repo, tmp_path / "after") == ()


def test_worktree_reconcile_rpc_keeps_a_worktree_git_still_lists(tmp_path: Path) -> None:
    """Only rows whose holder is gone are retired, each naming why."""
    repo = _repo_with_document(tmp_path, _synthetic_document())
    _git(repo, "worktree", "add", "-q", "-b", "keep", str(repo / ".ea" / "worktrees" / "keep"))
    ctx = _daemon_context(repo, tmp_path)
    state_file = repo / ".ea" / "state.json"
    original = state_file.read_bytes()

    dry = _reconcile(ctx, repo, dry_run=True)
    assert state_file.read_bytes() == original
    assert dry["dry_run"] is True and dry["written"] is False

    result = _reconcile(ctx, repo)

    assert (
        result["retired"]
        == dry["retired"]
        == [
            {"kind": "worktree", "row_id": "WT-GONE", "reason": "no_git_worktree"},
            {"kind": "session", "row_id": "SES-OLD", "reason": "predates_daemon_boot"},
            {"kind": "session", "row_id": "SES-ON-GONE", "reason": "bound_worktrees_gone"},
        ]
    )
    document = json.loads(state_file.read_text(encoding="utf-8"))
    assert document["worktrees"]["WT-KEEP"]["status"] == "active"
    assert document["worktrees"]["WT-GONE"]["status"] == "abandoned"
    assert document["agent_sessions"]["SES-OLD"]["status"] == "stale"
    assert document["agent_sessions"]["SES-NEW"]["status"] == "active"
    assert RECONCILE_COMMAND in store_path(state_file, StoreKind.EVENT).read_text(encoding="utf-8")


def test_worktree_reconcile_rpc_second_run_writes_nothing(tmp_path: Path) -> None:
    """The reconcile is idempotent: a clean document is left byte-identical."""
    repo = _repo_with_document(tmp_path, _synthetic_document())
    ctx = _daemon_context(repo, tmp_path)
    _reconcile(ctx, repo)
    state_file = repo / ".ea" / "state.json"
    settled = state_file.read_bytes()

    again = _reconcile(ctx, repo)

    assert again == {"retired": [], "dry_run": False, "written": False}
    assert state_file.read_bytes() == settled


def test_worktree_reconcile_rpc_refuses_when_git_cannot_list(tmp_path: Path) -> None:
    """A git failure is not "no worktrees"; it refuses and writes nothing."""
    repo = _repo_with_document(tmp_path, _synthetic_document())
    shutil.rmtree(repo / ".git")
    state_file = repo / ".ea" / "state.json"
    original = state_file.read_bytes()

    with pytest.raises(DaemonValidationError, match="git worktree list failed"):
        _reconcile(_daemon_context(repo, tmp_path), repo)
    assert state_file.read_bytes() == original


def test_worktree_reconcile_rpc_rejects_unknown_params(tmp_path: Path) -> None:
    """The wire params are strict."""
    ctx = _daemon_context(tmp_path, tmp_path)
    with pytest.raises(PydanticValidationError):
        asyncio.run(worktree_reconcile_rpc(ctx, {"repo_root": str(tmp_path), "force": True}))
    with pytest.raises(PydanticValidationError):
        asyncio.run(worktree_reconcile_rpc(ctx, {"repo_root": ""}))


def test_worktree_reconcile_cli_dry_run_reports_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI dry run itemises the rows and leaves the document untouched."""
    repo = _repo_with_document(tmp_path, _synthetic_document())
    state_file = repo / ".ea" / "state.json"
    original = state_file.read_bytes()
    monkeypatch.chdir(repo)
    # A gate runner exports EA_STATE at its sandbox copy, and EA_STATE outranks
    # the cwd walk this test relies on to reach the synthetic repo.
    monkeypatch.delenv("EA_STATE", raising=False)

    result = runner.invoke(app, ["--daemonless", "--json", "worktree", "reconcile", "--dry-run"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert {row["row_id"] for row in payload["retired"]} == {
        "WT-GONE",
        "WT-KEEP",
        "SES-ON-GONE",
        "SES-ON-KEEP",
    }
    assert state_file.read_bytes() == original
