"""``domain.repository.create``: a repository row admitted at its git head.

A plan cannot be submitted until a repository row records the head it
binds, and until this verb nothing but a hand-written document row could
provide one. The row is created through the same transaction every create
uses and read back through the model the plan's head binding trusts.

The gate-fire proof is the tree with no readable ``HEAD``: the create is
refused with ``repository_head_readable`` and writes nothing, so a row can
never name a head the repository does not hold.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.epoch2.repository import Repository
from eawf.kernel.store.compaction import read_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.domain_create import REPOSITORY_CREATE_METHOD
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    firehose_path,
    method_context,
    provision,
)

pytestmark = pytest.mark.integration

REPOSITORY_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
TRACK_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.delenv("GIT_DIR", raising=False)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _committed(canary: CanaryProvision) -> str:
    """Make the canary root a git repository with one commit and return its head."""
    _git(canary.root, "init", "-q")
    (canary.root / "README").write_text("canary\n", encoding="utf-8")
    _git(canary.root, "add", "README")
    _git(
        canary.root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-q",
        "-m",
        "seed",
    )
    return _git(canary.root, "rev-parse", "HEAD")


def _create(canary: CanaryProvision, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "urn": REPOSITORY_URN,
        "expected_revision": 0,
        "idempotency_key": "req-repository",
        "actor": "OP-0001",
        "spec": {"key": "REP-EAWF"},
    }
    params.update(overrides)
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(
        methods.dispatch(REPOSITORY_CREATE_METHOD, ctx, {"repo_root": str(canary.root), **params})
    )


def _repositories(canary: CanaryProvision) -> dict[str, Any]:
    rows = read_document(document_path(canary)).get("repository", {})
    assert isinstance(rows, dict)
    return rows


def test_a_created_repository_reads_back_at_the_git_head(tmp_path: Path) -> None:
    canary = provision(tmp_path / "repo", code="REPOOK")
    head = _committed(canary)

    answer = _create(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    assert answer["result"]["event_name"] == "admission.repository.created"
    assert answer["result"]["revision_after"] == 1
    row = Repository.model_validate(_repositories(canary)["REP-EAWF"])
    assert row.head_sha == head
    assert str(row.urn) == REPOSITORY_URN
    events = [json.loads(line) for line in firehose_path(canary).read_text().splitlines()]
    assert [event["payload"]["name"] for event in events] == ["admission.repository.created"]


def test_a_tree_with_no_git_head_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    canary = provision(tmp_path / "repo", code="NOHEAD")
    before = document_path(canary).read_bytes()

    answer = _create(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "repository_head_readable"
    assert document_path(canary).read_bytes() == before
    assert not firehose_path(canary).exists()


def test_a_caller_cannot_assert_the_head(tmp_path: Path) -> None:
    canary = provision(tmp_path / "repo", code="ASSERTED")
    _committed(canary)

    answer = _create(canary, tmp_path, spec={"key": "REP-EAWF", "head_sha": "a" * 40})

    assert answer["errors"][0]["code"] == "schema_validation_failed"
    assert _repositories(canary) == {}


def test_a_document_keyed_off_its_urn_is_refused(tmp_path: Path) -> None:
    canary = provision(tmp_path / "repo", code="OFFKEY")
    _committed(canary)

    answer = _create(canary, tmp_path, spec={"key": "REP-OTHER"})

    assert answer["errors"][0]["code"] == "schema_validation_failed"
    assert _repositories(canary) == {}


def test_a_non_repository_urn_is_refused(tmp_path: Path) -> None:
    canary = provision(tmp_path / "repo", code="KIND")
    _committed(canary)

    answer = _create(canary, tmp_path, urn=TRACK_URN)

    assert answer["errors"][0]["code"] == "identity_kind_mismatch"


def test_a_taken_key_and_a_stale_cursor_are_refused(tmp_path: Path) -> None:
    canary = provision(tmp_path / "repo", code="TWICE")
    _committed(canary)
    assert _create(canary, tmp_path)["status"] == "ok"

    stale = _create(canary, tmp_path, idempotency_key="req-again")
    taken = _create(canary, tmp_path, idempotency_key="req-again-2", expected_revision=1)

    assert stale["errors"][0]["guard"] == "tree_cursor_current"
    assert taken["errors"][0]["guard"] == "record_key_free"


def test_a_retry_replays_the_original_receipt(tmp_path: Path) -> None:
    canary = provision(tmp_path / "repo", code="REPLAY")
    _committed(canary)

    first = _create(canary, tmp_path)
    second = _create(canary, tmp_path)

    assert second["status"] == "ok"
    assert second["result"] == first["result"]


def test_the_verb_is_registered_on_the_server() -> None:
    import eawf.runtime.daemon.server  # noqa: F401

    methods.ensure_all_methods_registered()
    assert REPOSITORY_CREATE_METHOD in methods.registered_methods()
    assert domain_cmd.REPOSITORY_CREATE == REPOSITORY_CREATE_METHOD


def test_the_cli_command_forwards_the_create_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((method, params))
            raise TimeoutError("no daemon in this test")

    monkeypatch.setattr(domain_cmd, "DaemonClient", _Client)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    spec = tmp_path / "repository.json"
    spec.write_bytes(orjson.dumps({"key": "REP-EAWF"}))

    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "repository",
            "create",
            REPOSITORY_URN,
            "--expected-tree-revision",
            "5",
            "--idempotency-key",
            "req-repository",
            "--actor",
            "OP-0001",
            "--from-spec",
            str(spec),
        ],
    )

    assert result.exit_code == exit_codes.DAEMON_UNREACHABLE, result.output
    method, params = calls[0]
    assert method == REPOSITORY_CREATE_METHOD
    assert params["urn"] == REPOSITORY_URN
    assert params["expected_revision"] == 5
    assert params["spec"] == {"key": "REP-EAWF"}
