"""``eawf doctor --fix`` supersedes ledger rows that bind commits no live branch reaches.

A fixture repository's task ledger binds two commits ``origin/main`` does
not reach: one squashed (a commit on ``main`` carries its exact tree) and
one dangling (nothing on ``main`` carries its tree or patch). The preview
names one ``ledger.supersede-unreachable`` action over two rows; the apply,
served by the registered daemon method in process, only appends -- the
squashed binding rebound to its landed commit, the dangling one declared
under ``unreachable_shas`` with an archive ref that now preserves it -- and
a rerun previews nothing.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    effective_records,
    line_digest,
    read_ledger_records,
    render_ledger_line,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor.checks import CheckResult
from eawf.observability.doctor.repair import (
    ARCHIVE_REF_PREFIX,
    LEDGER_SUPERSEDE_ACTION,
    LedgerSupersession,
    build_repair_plan,
    plan_ledger_supersessions,
    superseding_record,
)
from eawf.platform.lint.ledger_history import UnreachableSha, scan_ledger_history
from eawf.runtime.daemon import methods
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.workflow.lifecycle.wave_sha import commit_identity_digest
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context

runner = CliRunner()

AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep registry, canary scratch and host probes inside this test."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "home" / ".eawf" / "registry.json"))
    for target, value in (
        ("global_config_path", lambda: tmp_path / "missing-global-config.yaml"),
        ("check_manifest_in_sync", lambda **_k: CheckResult(name="manifest_in_sync", status="ok")),
        ("_desired_sync_drift", lambda _workspace: False),
        ("check_launchd_agent", lambda: CheckResult(name="launchd_agent", status="ok")),
        ("find_stray_daemons", lambda: []),
    ):
        monkeypatch.setattr(f"eawf.observability.doctor.repair.{target}", value)


def _git(root: Path, *args: str) -> str:
    """Run git in *root* with hooks disabled and return its stripped stdout."""
    proc = subprocess.run(
        ["git", "-c", f"core.hooksPath={root / '.no-hooks'}", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _commit(root: Path, name: str, content: str, message: str) -> str:
    """Write *name* and commit it; return the new commit."""
    (root / name).write_text(content, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@dataclass(frozen=True)
class Repo:
    """A managed repository whose task ledger binds two unreachable commits."""

    root: Path
    squashed: str
    landed: str
    dangling: str

    @property
    def ledger(self) -> Path:
        """The generation's task ledger."""
        authority = require_native_authority(self.root / ".ea")
        assert authority.target is not None and authority.generation_id is not None
        document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
        return ledger_path(document, Epoch2Collection.TASK)

    def rows(self) -> dict[str, LedgerRecord]:
        """Return the effective task rows by record key."""
        return {r.record_key: r for r in effective_records(read_ledger_records(self.ledger))}


def _bind(repo_ledger: Path, key: str, commit: str, identity: str | None) -> None:
    append_ledger_record(
        repo_ledger,
        LedgerRecord(
            collection=Epoch2Collection.TASK,
            record_key=key,
            status="COMPLETED",
            recorded_at=AT,
            payload={
                "record": {"intent": f"landed {key}"},
                "legacy_refs": {
                    "commit": commit,
                    "commit_identity_digest": identity,
                    "outcome": f"landed as {commit}",
                },
            },
        ),
    )


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    init = ["--no-input", "init", "--target", str(root), "--project-code", "DEMO"]
    assert runner.invoke(app, init).exit_code == exit_codes.OK
    # The repair locks the tree's one project, as an imported tree holds.
    authority = require_native_authority(root / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    document.setdefault("project", {})["DEMO"] = {"payload": {"code": "DEMO"}}
    write_document(document_path, document)
    if not (root / ".git").exists():
        _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "ci@example.com")
    _git(root, "config", "user.name", "ci")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", "base")
    _git(root, "branch", "-M", "main")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """Build the repository: one squashed and one dangling commit bound by the ledger."""
    root = _init_repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "side")
    squashed = _commit(root, "feature.txt", "feature\n", "feat: side work")
    dangling = _commit(root, "abandoned.txt", "abandoned\n", "feat: abandoned work")
    _git(root, "checkout", "-q", "main")
    landed = _commit(root, "feature.txt", "feature\n", "feat: squashed side work")
    _git(root, "branch", "-q", "-D", "side")
    built = Repo(root=root, squashed=squashed, landed=landed, dangling=dangling)
    _bind(built.ledger, "TSK-SQUASHED", squashed, commit_identity_digest(squashed, repo_root=root))
    _bind(built.ledger, "TSK-DANGLING", dangling, None)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "state: bind the ledger")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return built


class _InProcessClient:
    """The CLI's daemon client, answering from the registered methods in process."""

    context: methods.MethodContext | None = None

    def __init__(self, *_a: object, **_k: object) -> None:
        return None

    def __enter__(self) -> _InProcessClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any], **_k: object) -> dict[str, Any]:
        context = type(self).context
        assert context is not None
        answer = asyncio.run(methods.dispatch(method, context, params))
        assert isinstance(answer, dict)
        return answer


def _doctor(root: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["--workspace", str(root), *argv, "doctor", "--fix"])
    payload = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}
    return result.exit_code, payload.get("result", payload)


def _ledger_actions(answer: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in answer.get("actions", []) if a["action_id"] == LEDGER_SUPERSEDE_ACTION]


def test_fix_previews_applies_by_appending_and_reruns_clean(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = repo.ledger.read_bytes()

    code, preview = _doctor(repo.root, "--json")

    assert code == exit_codes.OK, preview
    (action,) = _ledger_actions(preview)
    assert action["record_count"] == 2
    assert action["mutation_class"] == "committed_store"
    assert repo.ledger.read_bytes() == before

    monkeypatch.setattr("eawf.surfaces.doctor_repair.DaemonClient", _InProcessClient)
    _InProcessClient.context = method_context(tmp_path / "runtime")
    methods.ensure_all_methods_registered()
    argv = ["--workspace", str(repo.root), "--no-input", "doctor", "--fix", "--yes"]
    result = runner.invoke(app, argv)
    assert result.exit_code == exit_codes.OK, result.stdout

    after = repo.ledger.read_bytes()
    assert after.startswith(before)
    assert after[len(before) :].count(b"\n") == 2
    rows = repo.rows()
    rebound = rows["TSK-SQUASHED"].payload["legacy_refs"]
    assert rebound["commit"] == repo.landed
    assert rebound["outcome"] == f"landed as {repo.landed}"
    assert rebound["commit_identity_digest"] == commit_identity_digest(
        repo.landed, repo_root=repo.root
    )
    declared = rows["TSK-DANGLING"].payload["unreachable_shas"]
    archive = f"{ARCHIVE_REF_PREFIX}{repo.dangling}"
    assert declared == [
        {
            "sha": repo.dangling,
            "archive_ref": archive,
            "reason": "no commit on origin/main shares its tree or patch id",
        }
    ]
    assert _git(repo.root, "rev-parse", archive) == repo.dangling

    code, rerun = _doctor(repo.root, "--json")
    assert code == exit_codes.OK, rerun
    assert _ledger_actions(rerun) == []
    assert scan_ledger_history(repo.root, "origin/main").unreachable == ()


def test_apply_refuses_a_preview_the_ledger_moved_past(repo: Repo, tmp_path: Path) -> None:
    (action,) = [
        a for a in build_repair_plan(repo.root).actions if a.action_id == LEDGER_SUPERSEDE_ACTION
    ]
    _bind(repo.ledger, "TSK-LATER", repo.dangling, None)
    before = repo.ledger.read_bytes()
    params = {
        "repo_root": str(repo.root),
        "preview_digest": "sha256:" + "0" * 64,
        "selected_preview_digest": "sha256:" + "0" * 64,
        "action_ids": [LEDGER_SUPERSEDE_ACTION],
        "action_previews": {LEDGER_SUPERSEDE_ACTION: action.preview_digest},
    }

    with pytest.raises(methods.DaemonValidationError):
        asyncio.run(
            methods.dispatch("doctor.apply_repair", method_context(tmp_path / "rt"), params)
        )

    assert repo.ledger.read_bytes() == before


def test_apply_refuses_a_stale_action_digest(repo: Repo, tmp_path: Path) -> None:
    from eawf.runtime.daemon.methods.doctor import _apply_ledger_supersede

    (action,) = [
        a for a in build_repair_plan(repo.root).actions if a.action_id == LEDGER_SUPERSEDE_ACTION
    ]
    _bind(repo.ledger, "TSK-LATER", repo.dangling, None)
    before = repo.ledger.read_bytes()

    with pytest.raises(methods.DaemonValidationError, match="preview changed"):
        _apply_ledger_supersede(method_context(tmp_path / "rt"), repo.root, action)

    assert repo.ledger.read_bytes() == before


def test_apply_refuses_a_tree_without_one_project(repo: Repo, tmp_path: Path) -> None:
    from eawf.runtime.daemon.methods.doctor import _apply_ledger_supersede

    (action,) = [
        a for a in build_repair_plan(repo.root).actions if a.action_id == LEDGER_SUPERSEDE_ACTION
    ]
    authority = require_native_authority(repo.root / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    document["project"]["OTHER"] = {"payload": {"code": "OTHER"}}
    write_document(document_path, document)
    before = repo.ledger.read_bytes()

    with pytest.raises(methods.DaemonValidationError, match="holds 2"):
        _apply_ledger_supersede(method_context(tmp_path / "rt"), repo.root, action)

    assert repo.ledger.read_bytes() == before


def test_no_base_branch_plans_nothing(repo: Repo) -> None:
    _git(repo.root, "update-ref", "-d", "refs/remotes/origin/main")

    actions = [a.action_id for a in build_repair_plan(repo.root).actions]

    assert LEDGER_SUPERSEDE_ACTION not in actions


def test_a_clean_ledger_plans_no_row(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")

    assert plan_ledger_supersessions(root) == ((), 0)


def test_a_cherry_pick_rebinds_by_patch_id(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "side")
    picked = _commit(root, "fix.txt", "fix\n", "fix: on the side")
    _git(root, "checkout", "-q", "main")
    _commit(root, "other.txt", "other\n", "feat: main moved on")
    _git(root, "cherry-pick", picked)
    landed = _git(root, "rev-parse", "HEAD")
    _git(root, "branch", "-q", "-D", "side")
    repo = Repo(root=root, squashed=picked, landed=landed, dangling=picked)
    _bind(repo.ledger, "TSK-PICKED", picked, None)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "state: bind")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")

    (row,), unrepairable = plan_ledger_supersessions(root)

    assert unrepairable == 0
    assert row.rebinds == {picked: landed}
    assert row.unreachable == ()


def test_two_commits_sharing_the_tree_are_declared_not_guessed(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "side")
    stray = _commit(root, "same.txt", "same\n", "feat: on the side")
    _git(root, "checkout", "-q", "main")
    _commit(root, "same.txt", "same\n", "feat: landed once")
    _git(root, "revert", "--no-edit", "HEAD")
    _commit(root, "same.txt", "same\n", "feat: landed twice")
    _git(root, "tag", "archive/side", stray)
    _git(root, "branch", "-q", "-D", "side")
    repo = Repo(root=root, squashed=stray, landed=stray, dangling=stray)
    _bind(repo.ledger, "TSK-AMBIGUOUS", stray, None)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "state: bind")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")

    (row,), _ = plan_ledger_supersessions(root)

    assert row.rebinds == {}
    (entry,) = row.unreachable
    assert entry.archive_ref == "refs/tags/archive/side"
    assert entry.reason == "2 commits on origin/main share its tree"


def test_superseding_record_extends_an_existing_declaration(repo: Repo) -> None:
    kept = UnreachableSha(sha="a" * 40, archive_ref="refs/tags/kept", reason="kept on purpose")
    append_ledger_record(
        repo.ledger,
        LedgerRecord(
            collection=Epoch2Collection.TASK,
            record_key="TSK-DECLARED",
            status="COMPLETED",
            recorded_at=AT,
            payload={"unreachable_shas": [kept.model_dump(mode="json")], "commit": repo.dangling},
        ),
    )
    line = repo.ledger.read_bytes().decode().splitlines()[-1]
    added = UnreachableSha(sha=repo.dangling, archive_ref="refs/tags/x", reason="dangling")
    row = LedgerSupersession(
        path=repo.ledger.relative_to(repo.root).as_posix(),
        line=len(repo.ledger.read_bytes().decode().splitlines()),
        supersedes=line_digest(line),
        unreachable=(added,),
    )

    record = superseding_record(repo.root, row, recorded_at=AT)

    assert record.supersedes == line_digest(line)
    assert record.payload["unreachable_shas"] == [
        kept.model_dump(mode="json"),
        added.model_dump(mode="json"),
    ]
    assert render_ledger_line(record) != line


@pytest.mark.parametrize(
    ("field", "value"),
    [("line", 0), ("supersedes", "sha256:short"), ("path", ""), ("extra", 1)],
)
def test_a_malformed_supersession_is_refused(field: str, value: object) -> None:
    fields: dict[str, object] = {
        "path": "x/ledger/task.jsonl",
        "line": 1,
        "supersedes": "sha256:" + "0" * 64,
    }
    fields[field] = value

    with pytest.raises(ValueError):
        LedgerSupersession.model_validate(fields)
