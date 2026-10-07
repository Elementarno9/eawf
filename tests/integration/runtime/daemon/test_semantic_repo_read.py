"""``repo_read`` and ``repo_search`` answer from the tree a Run reads and nowhere else.

Driven through the registered ``semantic.call`` verb, so what runs is the shipped
gateway and the shipped handlers. A Run holding a lease reads its leased worktree; a
Run holding none, such as a campaign round, reads the repository root. A refusal
raised by a handler files no receipt.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.content import CONTENT_LINE_CHARS, CONTENT_LINE_LIMIT
from eawf.kernel.runtime.semantic import SemanticCall
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.workspace.lease import workspace_path
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

#: A workspace handle the Run does not hold.
OTHER_HANDLE: Final = f"wsh-{'c' * 32}"

_ordinal = iter(range(1, 10_000))


def seal(capsule: AuthorityCapsule, tool: str, **payload: Any) -> SemanticCall:
    """Return a sealed call of *tool* with *payload*, under a fresh idempotency key."""
    ordinal = next(_ordinal)
    return SemanticCall.seal(
        {
            "call_id": f"call-{ordinal:016x}",
            "run_ref": RUN_URN,
            "contract_digest": capsule.contract_digest,
            "idempotency_key": f"read-{ordinal}",
            "tool_id": tool,
            "tool_schema_version": "1.0.0",
            "payload": {"tool_id": tool, "resource_handle": WORKSPACE_HANDLE, **payload},
            "requested_at": AT,
        }
    )


def output_of(answer: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded output of a succeeded answer."""
    result = answer["result"]
    assert result["status"] == "succeeded", result
    output: dict[str, Any] = result["bounded_output"]
    return output


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(runtime_root)


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running, task-scoped Run and no lease."""
    return make_canary(tmp_path / "repo")


@pytest.fixture
def capsule(ctx: MethodContext, canary: CanaryProvision) -> AuthorityCapsule:
    """The bound capsule of an executor granted both read tools."""
    sealed = executor_capsule(tool_grants=("repo_read", "repo_search", "submit_report"))
    bind(ctx, canary, sealed)
    return sealed


@pytest.fixture
def tree(canary: CanaryProvision, runtime_root: Path, capsule: AuthorityCapsule) -> Path:
    """The leased worktree the Run reads, once its lease is seeded."""
    seed_lease(canary, runtime_root)
    return workspace_path(root_context(canary, runtime_root), handle=WORKSPACE_HANDLE)


def read(
    ctx: MethodContext, canary: CanaryProvision, capsule: AuthorityCapsule, **payload: Any
) -> dict[str, Any]:
    """Send one ``repo_read`` call and return the daemon's answer."""
    return invoke(ctx, canary, seal(capsule, "repo_read", **payload), capsule)


def search(
    ctx: MethodContext, canary: CanaryProvision, capsule: AuthorityCapsule, **payload: Any
) -> dict[str, Any]:
    """Send one ``repo_search`` call and return the daemon's answer."""
    return invoke(ctx, canary, seal(capsule, "repo_search", **payload), capsule)


# ---- repo_read -------------------------------------------------------------------


def test_a_leased_run_reads_its_workspace_inline(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "only_here.py").write_text("y = 2\nz = 3\n", encoding="utf-8")

    output = output_of(read(ctx, canary, capsule, path="src/only_here.py"))

    assert output["text"] == "y = 2\nz = 3"
    assert output["byte_count"] == len(b"y = 2\nz = 3\n")
    assert output["truncated"] is False
    assert output["content_ref"].startswith("artifact://content/")
    assert output["content_digest"].startswith("sha256:")


def test_a_run_without_a_lease_reads_the_repository_root(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    output = output_of(read(ctx, canary, capsule, path="src/module.py"))

    assert output["text"] == "x = 1"
    assert output["byte_count"] == len(b"x = 1\n")


def test_an_empty_file_reads_as_empty_text(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "empty.txt").write_text("", encoding="utf-8")

    output = output_of(read(ctx, canary, capsule, path="src/empty.txt"))

    assert (output["text"], output["byte_count"], output["truncated"]) == ("", 0, False)


def test_a_read_cut_at_the_byte_cap_stops_at_a_whole_line_and_resumes_there(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "lines.txt").write_text("aaaa\nbbbb\ncccc\n", encoding="utf-8")

    first = output_of(read(ctx, canary, capsule, path="src/lines.txt", byte_cap=12))
    second = output_of(
        read(ctx, canary, capsule, path="src/lines.txt", start_byte=first["byte_count"])
    )

    assert (first["text"], first["byte_count"], first["truncated"]) == ("aaaa\nbbbb", 10, True)
    assert (second["text"], second["truncated"]) == ("cccc", False)


def test_a_window_exactly_the_file_size_is_not_truncated(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "exact.txt").write_text("abc\n", encoding="utf-8")

    output = output_of(read(ctx, canary, capsule, path="src/exact.txt", byte_cap=4))

    assert (output["text"], output["byte_count"], output["truncated"]) == ("abc", 4, False)


def test_a_read_past_the_end_of_the_file_returns_nothing(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "short.txt").write_text("abc\n", encoding="utf-8")

    output = output_of(read(ctx, canary, capsule, path="src/short.txt", start_byte=100))

    assert (output["text"], output["byte_count"], output["truncated"]) == ("", 0, False)


def test_a_read_longer_than_the_store_keeps_is_cut_at_its_line_limit(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    body = "".join(f"{index}\n" for index in range(CONTENT_LINE_LIMIT + 1))
    (tree / "src" / "long.txt").write_text(body, encoding="utf-8")

    output = output_of(read(ctx, canary, capsule, path="src/long.txt"))

    assert len(output["text"].splitlines()) == CONTENT_LINE_LIMIT
    assert output["truncated"] is True
    assert output["byte_count"] == len(body) - len(f"{CONTENT_LINE_LIMIT}\n")


def test_a_line_longer_than_the_store_keeps_is_cut_and_flagged(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "wide.txt").write_text("w" * (CONTENT_LINE_CHARS + 1), encoding="utf-8")

    output = output_of(read(ctx, canary, capsule, path="src/wide.txt"))

    assert output["text"] == "w" * CONTENT_LINE_CHARS
    assert output["truncated"] is True


@pytest.mark.parametrize("path", [".ea/state.json", ".git/HEAD", "src/.git/config"])
def test_a_read_under_ea_or_git_is_refused_with_no_receipt(
    canary: CanaryProvision,
    ctx: MethodContext,
    runtime_root: Path,
    capsule: AuthorityCapsule,
    path: str,
) -> None:
    before = receipt_lines(canary, runtime_root)

    with pytest.raises(DaemonValidationError, match="repo_path_refused"):
        read(ctx, canary, capsule, path=path)

    assert receipt_lines(canary, runtime_root) == before


def test_a_link_into_ea_is_refused(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    os.symlink(canary.root / ".ea", canary.root / "src" / "state")

    with pytest.raises(DaemonValidationError, match="repo_path_refused"):
        read(ctx, canary, capsule, path="src/state/state.json")


def test_a_link_out_of_the_workspace_is_refused(
    canary: CanaryProvision,
    ctx: MethodContext,
    capsule: AuthorityCapsule,
    tree: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours\n", encoding="utf-8")
    os.symlink(outside, tree / "src" / "link")

    with pytest.raises(DaemonValidationError, match="repo_path_escapes_root"):
        read(ctx, canary, capsule, path="src/link/secret.txt")


@pytest.mark.parametrize("path", ["../outside.txt", "src/../../outside.txt", "/etc/hosts"])
def test_a_path_walking_out_or_absolute_is_refused_at_the_boundary(
    capsule: AuthorityCapsule, path: str
) -> None:
    with pytest.raises(ValidationError):
        seal(capsule, "repo_read", path=path)


@pytest.mark.parametrize("path", ["src/missing.py", "src"])
def test_a_path_naming_no_file_is_refused(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, path: str
) -> None:
    with pytest.raises(DaemonValidationError, match="repo_path_not_found"):
        read(ctx, canary, capsule, path=path)


def test_a_binary_file_is_refused(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "blob.bin").write_bytes(b"\x00\x01\x02")

    with pytest.raises(DaemonValidationError, match="repo_path_binary"):
        read(ctx, canary, capsule, path="src/blob.bin")


def test_a_read_naming_another_workspace_is_refused(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    with pytest.raises(DaemonValidationError, match="read_handle_mismatch"):
        read(ctx, canary, capsule, path="src/module.py", resource_handle=OTHER_HANDLE)


@pytest.mark.parametrize("byte_cap", [0, 1_048_577, "64"])
def test_a_byte_cap_out_of_range_or_of_the_wrong_type_is_refused(
    capsule: AuthorityCapsule, byte_cap: object
) -> None:
    with pytest.raises(ValidationError):
        seal(capsule, "repo_read", path="src/module.py", byte_cap=byte_cap)


# ---- repo_search -----------------------------------------------------------------


def test_a_literal_search_returns_each_matching_line(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "a.py").write_text("one\nneedle here\nthree\n", encoding="utf-8")
    (tree / "src" / "b.py").write_text("needle.*\n", encoding="utf-8")

    output = output_of(search(ctx, canary, capsule, query="needle"))

    assert output["matches"] == [
        {"path": "src/a.py", "line": 2, "text": "needle here"},
        {"path": "src/b.py", "line": 1, "text": "needle.*"},
    ]
    assert (output["match_count"], output["truncated"]) == (2, False)
    assert output["match_ref"].startswith("artifact://content/")


def test_a_literal_search_does_not_read_its_query_as_an_expression(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "a.py").write_text("needle here\nneedle.*\n", encoding="utf-8")

    output = output_of(search(ctx, canary, capsule, query="needle.*"))

    assert [item["line"] for item in output["matches"]] == [2]


def test_a_regex_search_matches_an_expression(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "a.py").write_text("def alpha():\nx = 1\ndef beta():\n", encoding="utf-8")

    output = output_of(search(ctx, canary, capsule, query=r"^def \w+", regex=True))

    assert [item["line"] for item in output["matches"]] == [1, 3]


def test_a_search_with_no_match_returns_none(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    output = output_of(search(ctx, canary, capsule, query="absent-from-every-file"))

    assert (output["matches"], output["match_count"], output["truncated"]) == ([], 0, False)


def test_a_search_is_cut_at_its_result_cap(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "a.py").write_text("hit\nhit\nhit\n", encoding="utf-8")

    exact = output_of(search(ctx, canary, capsule, query="hit", result_cap=3))
    cut = output_of(search(ctx, canary, capsule, query="hit", result_cap=2))

    assert (exact["match_count"], exact["truncated"]) == (3, False)
    assert (cut["match_count"], cut["truncated"]) == (2, True)


def test_a_search_is_cut_at_the_store_line_limit(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "a.py").write_text("hit\n" * (CONTENT_LINE_LIMIT + 1), encoding="utf-8")

    output = output_of(search(ctx, canary, capsule, query="hit", result_cap=1000))

    assert (output["match_count"], output["truncated"]) == (CONTENT_LINE_LIMIT, True)


def test_a_search_skips_binary_files_links_and_the_refused_trees(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tmp_path: Path
) -> None:
    root = canary.root
    (root / "src" / "blob.bin").write_bytes(b"needle\x00")
    (root / ".ea" / "needle.txt").write_text("needle\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("needle\n", encoding="utf-8")
    os.symlink(outside, root / "src" / "link.txt")
    (root / "src" / "kept.py").write_text("needle\n", encoding="utf-8")

    output = output_of(search(ctx, canary, capsule, query="needle"))

    assert [item["path"] for item in output["matches"]] == ["src/kept.py"]


def test_a_search_under_a_prefix_reads_only_that_tree(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src" / "a.py").write_text("hit\n", encoding="utf-8")
    (tree / "docs").mkdir()
    (tree / "docs" / "b.md").write_text("hit\n", encoding="utf-8")

    output = output_of(search(ctx, canary, capsule, query="hit", path_prefix="docs"))

    assert [item["path"] for item in output["matches"]] == ["docs/b.md"]


def test_a_search_prefix_under_ea_is_refused(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    with pytest.raises(DaemonValidationError, match="repo_path_refused"):
        search(ctx, canary, capsule, query="x", path_prefix=".ea")


def test_an_invalid_expression_is_refused(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    with pytest.raises(DaemonValidationError, match="search_pattern_invalid"):
        search(ctx, canary, capsule, query="(unclosed", regex=True)


@pytest.mark.parametrize("query", ["", "   "])
def test_an_empty_search_pattern_is_refused_at_the_boundary(
    capsule: AuthorityCapsule, query: str
) -> None:
    with pytest.raises(ValidationError):
        seal(capsule, "repo_search", query=query)


@pytest.mark.parametrize("result_cap", [0, 1001])
def test_a_result_cap_out_of_range_is_refused(capsule: AuthorityCapsule, result_cap: int) -> None:
    with pytest.raises(ValidationError):
        seal(capsule, "repo_search", query="x", result_cap=result_cap)


def test_a_search_naming_another_workspace_is_refused(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    with pytest.raises(DaemonValidationError, match="read_handle_mismatch"):
        search(ctx, canary, capsule, query="x", resource_handle=OTHER_HANDLE)
