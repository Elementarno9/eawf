"""``eawf migrate urn-rekey``: the re-key planned, applied and re-applied on a fixture tree.

An initialised tree is seeded with records spelled under three-slot URNs,
a ledger line carrying them, and a registry row filing the repository
under another code. The plan reports and writes nothing; the apply extends every ledger,
rewrites the document and moves the registry row; the tree then still
reads and still takes a native verb addressed by the re-keyed URN; and a
second apply has nothing left to write.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.urn_rekey import rekey_urn
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    effective_records,
    read_ledger_records,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.registry import read_registry
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.urn_rekey import URN_REKEY_METHOD
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from eawf.surfaces.cli.commands import migrate as migrate_cmd
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    BATCH_URN,
    TASK_URN,
    method_context,
    rekeyed,
    seed_row,
)

pytestmark = pytest.mark.integration

runner = CliRunner()

KEY = "EAWF"
NEW_TASK_URN = f"eawf://{KEY}/task/EAWF-0042"
NEW_BATCH_URN = f"eawf://{KEY}/batch/BAT-0007"
ACTOR = "OP-0001"
AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def registry_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every registry read and write at a file under this test's tmp dir."""
    path = tmp_path / "home" / ".eawf" / "registry.json"
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(path))
    return path


def _register(path: Path, repos: dict[str, Path]) -> None:
    """Write a registry filing each repository under its code, in one workspace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    codes = sorted(repos)
    path.write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "repos": {code: {"code": code, "path": str(root)} for code, root in repos.items()},
                "workspaces": {
                    "WSP-MAIN": {
                        "key": "WSP-MAIN",
                        "member_project_codes": codes,
                        "home_project_code": codes[0],
                    }
                },
            }
        )
    )


@dataclass(frozen=True)
class Tree:
    """An initialised repository whose generation holds three-slot URNs."""

    root: Path

    @property
    def document(self) -> Path:
        """The selected generation's document."""
        authority = require_native_authority(self.root / ".ea")
        assert authority.target is not None
        assert authority.generation_id is not None
        return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


@pytest.fixture
def canary(tmp_path: Path, registry_path: Path) -> Tree:
    """An initialised tree holding a PLANNED Task in an ACTIVE Batch and an old-URN ledger line."""
    root = tmp_path / "repo"
    init = ["--no-input", "init", "--target", str(root), "--project-code", "DEMO"]
    assert runner.invoke(app, init).exit_code == exit_codes.OK
    tree = Tree(root=root)
    batch = rekeyed(seed_row("batch", "ACTIVE"), key="BAT-0007")
    batch["task_refs"] = [TASK_URN]
    document = read_document(tree.document)
    document.setdefault("task", {})["EAWF-0042"] = seed_row("task", "PLANNED")
    document.setdefault("batch", {})["BAT-0007"] = batch
    write_document(tree.document, document)
    append_ledger_record(
        ledger_path(tree.document, Epoch2Collection.TASK),
        LedgerRecord(
            collection=Epoch2Collection.TASK,
            record_key="EAWF-0041",
            status="COMPLETED",
            recorded_at=AT,
            payload={"key": "EAWF-0041", "batch_ref": BATCH_URN, "title": "landed earlier"},
        ),
    )
    _register(registry_path, {"CANARY": root})
    return tree


def _eawf(root: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    """Run one ``eawf --json`` command against *root*; return its exit code and answer."""
    result = runner.invoke(app, ["--workspace", str(root), "--json", *argv])
    return result.exit_code, json.loads(result.stdout) if result.stdout.strip() else {}


def _rekey(root: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    """Run ``eawf migrate urn-rekey``; return its exit code and its result, or its refusal."""
    code, envelope = _eawf(root, "--no-input", "migrate", "urn-rekey", *argv)
    return code, envelope["result"] if code == exit_codes.OK else envelope


def _ledgers(canary: Tree) -> dict[Path, bytes]:
    """Return the bytes of every ledger file the tree holds."""
    return {
        path: path.read_bytes() for path in sorted((canary.root / ".ea").rglob("ledger/*.jsonl"))
    }


def _holds_old_urn(value: Any) -> bool:
    if isinstance(value, str):
        return rekey_urn(value, KEY) is not None
    if isinstance(value, dict):
        return any(_holds_old_urn(item) for item in value.values())
    if isinstance(value, list):
        return any(_holds_old_urn(item) for item in value)
    return False


def test_plan_reports_the_rekey_and_writes_nothing(canary: Tree) -> None:
    before = _ledgers(canary)
    document = canary.document.read_bytes()

    code, answer = _rekey(canary.root, "--project-key", KEY)

    assert code == exit_codes.OK, answer
    assert answer["applied"] is False
    assert answer["rows"] == 1
    assert answer["ledgers"] == {f"generations/{canary.document.parent.name}/ledger/task.jsonl": 1}
    assert answer["document"] is True
    assert answer["registry"] == {"from": "CANARY", "to": KEY}
    assert answer["pending"] == 3
    assert _ledgers(canary) == before
    assert canary.document.read_bytes() == document


def test_apply_rekeys_append_only_and_the_tree_keeps_working(
    canary: Tree, registry_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _ledgers(canary)

    code, applied = _rekey(canary.root, "--project-key", KEY, "--apply")

    assert code == exit_codes.OK, applied
    assert (applied["applied"], applied["pending"], applied["rows"]) == (True, 0, 1)
    after = _ledgers(canary)
    for path, content in before.items():
        assert after[path].startswith(content), path
    assert after != before
    for path in after:
        for record in effective_records(read_ledger_records(path)):
            assert not _holds_old_urn(record.payload), path
    stored = read_document(canary.document)
    assert not _holds_old_urn(stored)
    assert stored["task"]["EAWF-0042"]["urn"] == NEW_TASK_URN
    assert stored["batch"]["BAT-0007"]["task_refs"] == [NEW_TASK_URN]
    landed = ledger_path(canary.document, Epoch2Collection.TASK)
    (row,) = [
        r for r in effective_records(read_ledger_records(landed)) if r.record_key == "EAWF-0041"
    ]
    assert row.payload["batch_ref"] == NEW_BATCH_URN
    registry = read_registry(registry_path)
    assert set(registry.repos) == {KEY}
    assert registry.repos[KEY].path == str(canary.root)
    assert registry.workspaces["WSP-MAIN"].member_project_codes == frozenset({KEY})

    code, again = _rekey(canary.root, "--project-key", KEY, "--apply")
    assert code == exit_codes.OK, again
    assert (again["rows"], again["pending"], again["document"], again["registry"]) == (
        0,
        0,
        False,
        None,
    )
    assert _ledgers(canary) == after

    code, status = _eawf(canary.root, "status")
    assert code == exit_codes.OK, status.get("message")

    # The tree takes a native verb addressed by the re-keyed URN, served by
    # the registered daemon methods in process.
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr(domain_cmd, "DaemonClient", _InProcessClient)
    _InProcessClient.context = method_context(tmp_path / "runtime")
    spec = tmp_path / "demote.json"
    spec.write_bytes(orjson.dumps({"reason_code": "back-to-backlog"}))
    code, demoted = _eawf(
        canary.root, "--no-input", "task", "demote", NEW_TASK_URN,
        "--expected-task-revision", "1", "--idempotency-key", "demote-1",
        "--actor", ACTOR, "--from-spec", str(spec), "--yes",
    )  # fmt: skip
    assert code == exit_codes.OK, demoted
    stored = read_document(canary.document)
    assert stored["task"]["EAWF-0042"]["status"] == "DRAFT"
    assert stored["batch"]["BAT-0007"]["task_refs"] == []
    # The verb rewrote two records; neither went back to the three-slot form.
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    code, replanned = _rekey(canary.root, "--project-key", KEY)
    assert code == exit_codes.OK, replanned
    assert replanned["pending"] == 0


class _InProcessClient:
    """The CLI's daemon client, answering from the registered methods in process."""

    context: methods.MethodContext | None = None

    def __init__(self, *_a: object, **_k: object) -> None:
        return None

    def __enter__(self) -> _InProcessClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from eawf.surfaces.cli._daemon_client import DaemonRpcError

        context = type(self).context
        assert context is not None
        try:
            answer = asyncio.run(methods.dispatch(method, context, params))
        except methods.DaemonValidationError as error:
            raise DaemonRpcError(-32002, str(error)) from error
        assert isinstance(answer, dict)
        return answer


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Route the migrate verb's daemon arm into the registered methods."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(migrate_cmd, "_epoch2_daemon_enabled", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _InProcessClient)
    _InProcessClient.context = method_context(tmp_path / "runtime")
    methods.ensure_all_methods_registered()
    yield
    _InProcessClient.context = None


def test_the_daemon_serves_the_apply(canary: Tree, daemon: None) -> None:
    code, applied = _rekey(canary.root, "--project-key", KEY, "--apply")

    assert code == exit_codes.OK, applied
    assert (applied["applied"], applied["rows"], applied["registry"]) == (
        True,
        1,
        {"from": "CANARY", "to": KEY},
    )
    assert read_document(canary.document)["task"]["EAWF-0042"]["urn"] == NEW_TASK_URN


def test_a_key_held_by_another_repository_is_refused_before_any_write(
    canary: Tree, registry_path: Path, tmp_path: Path, daemon: None
) -> None:
    _register(registry_path, {"CANARY": canary.root, KEY: tmp_path / "elsewhere"})
    before = _ledgers(canary)
    document = canary.document.read_bytes()
    registry = registry_path.read_bytes()

    code, answer = _rekey(canary.root, "--project-key", KEY, "--apply")

    assert code == exit_codes.VALIDATION_ERROR, answer
    assert "urn_rekey_duplicate_project_key" in json.dumps(answer)
    assert _ledgers(canary) == before
    assert canary.document.read_bytes() == document
    assert registry_path.read_bytes() == registry


def test_an_unregistered_repository_is_refused(canary: Tree, registry_path: Path) -> None:
    registry_path.unlink()

    code, answer = _rekey(canary.root, "--project-key", KEY, "--apply")

    assert code == exit_codes.VALIDATION_ERROR, answer
    assert "urn_rekey_repository_not_registered" in json.dumps(answer)
    assert _holds_old_urn(read_document(canary.document))


@pytest.mark.parametrize("key", ["", "eawf", "E", "A" * 17, "EA/WF"])
def test_a_malformed_project_key_is_refused(canary: Tree, key: str) -> None:
    code, answer = _rekey(canary.root, "--project-key", key)

    assert code == exit_codes.VALIDATION_ERROR, answer


def test_the_daemon_refuses_a_malformed_request(canary: Tree, tmp_path: Path) -> None:
    params = {"repo_root": str(canary.root), "project_key": KEY, "unknown": 1}

    with pytest.raises(methods.DaemonValidationError, match="schema_validation_failed"):
        asyncio.run(methods.dispatch(URN_REKEY_METHOD, method_context(tmp_path / "rt"), params))


def test_the_daemon_refuses_a_malformed_key(canary: Tree, tmp_path: Path) -> None:
    params = {"repo_root": str(canary.root), "project_key": "lower"}

    with pytest.raises(methods.DaemonValidationError, match="validation_failed"):
        asyncio.run(methods.dispatch(URN_REKEY_METHOD, method_context(tmp_path / "rt"), params))
