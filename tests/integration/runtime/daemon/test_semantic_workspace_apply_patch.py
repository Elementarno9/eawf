"""``workspace_apply_patch`` writes a Run's files into its leased workspace and nowhere else.

Driven through the registered ``semantic.call`` verb over a canary whose seeded lease
is a real git worktree, so what runs is the shipped gateway and the shipped handler.
Each refusal is asserted with the file it would have written still absent: a refused
patch writes nothing, not its first few files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import SemanticCall, SemanticToolErrorCode
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.semantic_gateway import PreHandlerCheck
from eawf.runtime.workspace.lease import workspace_path
from eawf.runtime.worktree.tree_change import snapshot_tree
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    root_context,
)
from tests.integration.runtime.daemon.test_semantic_submit_candidate import (
    AT,
    RUN_URN,
    WORKSPACE_HANDLE,
    bind,
    executor_capsule,
    invoke,
    make_canary,
    receipt_lines,
    seed_lease,
)

pytestmark = pytest.mark.integration

#: The most files one patch may name.
MAX_FILES: Final = 64


def seal_patch(
    capsule: AuthorityCapsule, files: list[dict[str, Any]], *, ordinal: int = 1
) -> SemanticCall:
    """Return a sealed ``workspace_apply_patch`` call writing *files*."""
    return SemanticCall.seal(
        {
            "call_id": f"call-{ordinal:016x}",
            "run_ref": RUN_URN,
            "contract_digest": capsule.contract_digest,
            "idempotency_key": f"patch-{ordinal}",
            "tool_id": "workspace_apply_patch",
            "tool_schema_version": "1.0.0",
            "payload": {"tool_id": "workspace_apply_patch", "files": files},
            "requested_at": AT,
        }
    )


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(runtime_root)


@pytest.fixture
def canary(tmp_path: Path, runtime_root: Path) -> CanaryProvision:
    """A canary whose running Run holds a lease over a real worktree, writing ``src``."""
    provisioned = make_canary(tmp_path / "repo")
    seed_lease(provisioned, runtime_root)
    return provisioned


@pytest.fixture
def capsule(ctx: MethodContext, canary: CanaryProvision) -> AuthorityCapsule:
    """The bound capsule of an executor granted the patch tool."""
    sealed = executor_capsule(tool_grants=("workspace_apply_patch", "submit_report"))
    bind(ctx, canary, sealed)
    return sealed


def workspace(canary: CanaryProvision, runtime_root: Path) -> Path:
    """Return the seeded lease's worktree."""
    return workspace_path(root_context(canary, runtime_root), handle=WORKSPACE_HANDLE)


def test_a_patch_inside_the_write_claims_lands_in_the_workspace(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path, capsule: AuthorityCapsule
) -> None:
    files = [{"path": "src/hello.txt", "content": "canary\n"}]

    answer = invoke(ctx, canary, seal_patch(capsule, files), capsule)

    result = answer["result"]
    assert result["status"] == "succeeded", result
    output = result["bounded_output"]
    tree = workspace(canary, runtime_root)
    assert (tree / "src" / "hello.txt").read_text(encoding="utf-8") == "canary\n"
    assert output["changed_paths"] == ["src/hello.txt"]
    assert output["workspace_generation"] == 1
    assert output["resulting_tree_digest"] == snapshot_tree(tree).digest


def test_a_patch_overwrites_a_file_and_creates_missing_directories(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path, capsule: AuthorityCapsule
) -> None:
    files = [
        {"path": "src/module.py", "content": "x = 2\n"},
        {"path": "src/deep/new/file.txt", "content": ""},
    ]

    answer = invoke(ctx, canary, seal_patch(capsule, files), capsule)

    assert answer["result"]["status"] == "succeeded", answer["result"]
    tree = workspace(canary, runtime_root)
    assert (tree / "src" / "module.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (tree / "src" / "deep" / "new" / "file.txt").read_text(encoding="utf-8") == ""


def test_a_patch_of_the_most_files_one_call_may_name_lands_whole(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path, capsule: AuthorityCapsule
) -> None:
    files = [
        {"path": f"src/many/{index}.txt", "content": f"{index}\n"} for index in range(MAX_FILES)
    ]

    answer = invoke(ctx, canary, seal_patch(capsule, files), capsule)

    assert answer["result"]["status"] == "succeeded", answer["result"]
    assert len(answer["result"]["bounded_output"]["changed_paths"]) == MAX_FILES
    assert len(list((workspace(canary, runtime_root) / "src" / "many").iterdir())) == MAX_FILES


def test_a_path_outside_the_write_claims_is_denied_with_a_receipt(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path, capsule: AuthorityCapsule
) -> None:
    before = receipt_lines(canary, runtime_root)
    files = [
        {"path": "src/inside.txt", "content": "fine\n"},
        {"path": "README.md", "content": "outside\n"},
    ]

    answer = invoke(ctx, canary, seal_patch(capsule, files), capsule)

    result = answer["result"]
    assert result["status"] == "denied"
    assert result["error"]["code"] == SemanticToolErrorCode.SCOPE_DENIED.value
    assert result["error"]["field_path"] == "/files"
    assert answer["refused_check"] == PreHandlerCheck.SCOPE.value
    assert receipt_lines(canary, runtime_root) == before + 1
    tree = workspace(canary, runtime_root)
    assert not (tree / "README.md").exists()
    assert not (tree / "src" / "inside.txt").exists()


def test_a_path_through_a_link_out_of_the_workspace_is_refused_and_writes_nothing(
    canary: CanaryProvision,
    ctx: MethodContext,
    runtime_root: Path,
    capsule: AuthorityCapsule,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    tree = workspace(canary, runtime_root)
    os.symlink(outside, tree / "src" / "link")
    before = receipt_lines(canary, runtime_root)
    files = [
        {"path": "src/first.txt", "content": "would be first\n"},
        {"path": "src/link/escaped.txt", "content": "escaped\n"},
    ]

    with pytest.raises(DaemonValidationError, match="patch_path_escapes_workspace"):
        invoke(ctx, canary, seal_patch(capsule, files), capsule)

    assert list(outside.iterdir()) == []
    assert not (tree / "src" / "first.txt").exists()
    assert receipt_lines(canary, runtime_root) == before


def test_a_run_not_granted_the_tool_is_denied_and_writes_nothing(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    sealed = executor_capsule(tool_grants=("submit_report",))
    bind(ctx, canary, sealed)

    answer = invoke(
        ctx, canary, seal_patch(sealed, [{"path": "src/a.txt", "content": "a"}]), sealed
    )

    assert answer["result"]["status"] == "denied"
    assert answer["result"]["error"]["code"] == SemanticToolErrorCode.CAPABILITY_DENIED.value
    assert not (workspace(canary, runtime_root) / "src" / "a.txt").exists()


def test_a_run_holding_no_lease_is_denied_at_the_lease_check(
    tmp_path: Path, ctx: MethodContext
) -> None:
    unleased = make_canary(tmp_path / "unleased")
    sealed = executor_capsule(tool_grants=("workspace_apply_patch",))
    bind(ctx, unleased, sealed)

    answer = invoke(
        ctx, unleased, seal_patch(sealed, [{"path": "src/a.txt", "content": "a"}]), sealed
    )

    assert answer["result"]["status"] == "denied"
    assert answer["refused_check"] == PreHandlerCheck.LEASE.value
    assert answer["result"]["error"]["code"] == SemanticToolErrorCode.LEASE_NOT_ACTIVE.value
    assert not (unleased.root / "src" / "a.txt").exists()


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ([], "empty"),
        (
            [{"path": f"src/{index}.txt", "content": ""} for index in range(MAX_FILES + 1)],
            "too-many",
        ),
        ([{"path": "src/../escape.txt", "content": "x"}], "dot-dot"),
        ([{"path": "/etc/passwd", "content": "x"}], "absolute"),
        ([{"path": "src/a.txt", "content": "a"}, {"path": "src/a.txt", "content": "b"}], "repeat"),
        ([{"path": "src/a.txt"}], "missing-content"),
        ([{"path": "src/a.txt", "content": 7}], "wrong-type"),
        ([{"path": "src/a.txt", "content": "a", "mode": "0755"}], "extra-field"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_malformed_patch_is_refused_before_it_is_sent(
    files: list[dict[str, Any]], reason: str
) -> None:
    capsule = executor_capsule(tool_grants=("workspace_apply_patch",))
    with pytest.raises(ValidationError):
        seal_patch(capsule, files)
