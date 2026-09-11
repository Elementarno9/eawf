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

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

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
from eawf.kernel.migration.epoch2.journal import read_journal
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
from eawf.runtime.lock import portalock
from eawf.surfaces.cli.app import app

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
    """One corpus copy and its sealed plan, shared across the guard tests."""
    corpus = tmp_path_factory.mktemp("guards_corpus") / "staged"
    shutil.copytree(FULL_SNAPSHOT, corpus)
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
