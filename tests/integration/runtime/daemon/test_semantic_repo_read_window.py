"""``repo_read`` returns its whole window, cut only at ``byte_cap`` and at whole lines.

Driven through the registered ``semantic.call`` verb, as the repo_read suite is. The
inline text is scrubbed line by line but not cut at the content store's line or
character bounds; the stored content behind ``content_ref`` stays bounded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.content import (
    CONTENT_CHAR_LIMIT,
    CONTENT_LINE_CHARS,
    CONTENT_LINE_LIMIT,
    WITHHELD_LINE,
    bound_content,
)
from eawf.kernel.runtime.semantic import REPO_READ_TEXT_CHARS, RepoReadInput
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.workspace.lease import workspace_path
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    root_context,
)
from tests.integration.runtime.daemon.test_semantic_repo_read import output_of, read
from tests.integration.runtime.daemon.test_semantic_submit_candidate import (
    WORKSPACE_HANDLE,
    bind,
    executor_capsule,
    make_canary,
    seed_lease,
)

pytestmark = pytest.mark.integration

#: A line the state-leak scan flags: it carries a home path.
LEAKY_LINE: Final = "/Users/jdoe/project/keys"  # pragma: allowlist secret


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
    """The bound capsule of an executor granted the read tool."""
    sealed = executor_capsule(tool_grants=("repo_read", "repo_search", "submit_report"))
    bind(ctx, canary, sealed)
    return sealed


@pytest.fixture
def tree(canary: CanaryProvision, runtime_root: Path, capsule: AuthorityCapsule) -> Path:
    """The leased worktree the Run reads, once its lease is seeded."""
    seed_lease(canary, runtime_root)
    return workspace_path(root_context(canary, runtime_root), handle=WORKSPACE_HANDLE)


def numbered(count: int, width: int) -> str:
    """Return *count* lines, each *width* bytes with its line break."""
    return "".join(f"{index:0{width - 1}d}\n" for index in range(count))


def read_file(
    ctx: MethodContext,
    canary: CanaryProvision,
    capsule: AuthorityCapsule,
    tree: Path,
    body: str,
    **payload: Any,
) -> dict[str, Any]:
    """Write *body* to a file of the tree and return the bounded output reading it."""
    (tree / "src" / "window.txt").write_text(body, encoding="utf-8")
    return output_of(read(ctx, canary, capsule, path="src/window.txt", **payload))


# ---- whole window ----------------------------------------------------------------


def test_a_whole_small_file_arrives_in_one_read(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    body = numbered(1_000, 40)
    assert len(body.encode("utf-8")) == 40_000

    output = read_file(ctx, canary, capsule, tree, body)

    assert output["text"].splitlines() == body.splitlines()
    assert (output["byte_count"], output["truncated"]) == (40_000, False)


def test_a_whole_small_file_passes_both_store_bounds_at_once(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    body = numbered(CONTENT_LINE_LIMIT + 1, 100)
    assert len(body) > CONTENT_CHAR_LIMIT

    output = read_file(ctx, canary, capsule, tree, body)

    assert len(output["text"].splitlines()) == CONTENT_LINE_LIMIT + 1
    assert (output["byte_count"], output["truncated"]) == (len(body), False)


def test_a_whole_small_file_of_one_line_without_a_break_reads_whole(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    output = read_file(ctx, canary, capsule, tree, "only")

    assert (output["text"], output["byte_count"], output["truncated"]) == ("only", 4, False)


# ---- byte_cap window -------------------------------------------------------------


def test_a_byte_cap_window_stops_at_the_last_whole_line_and_resumes_after_it(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    body = numbered(3_000, 40)
    cap = 65_536
    whole = cap // 40 * 40

    first = read_file(ctx, canary, capsule, tree, body, byte_cap=cap)
    second = output_of(
        read(ctx, canary, capsule, path="src/window.txt", start_byte=first["byte_count"])
    )

    assert (first["byte_count"], first["truncated"]) == (whole, True)
    assert first["text"].splitlines() == body.splitlines()[: whole // 40]
    assert second["text"].splitlines()[0] == body.splitlines()[whole // 40]
    assert first["byte_count"] + second["byte_count"] == len(body)
    assert second["truncated"] is False


def test_a_byte_cap_window_one_byte_short_of_a_line_drops_that_line(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    output = read_file(ctx, canary, capsule, tree, "aaa\nbbb\n", byte_cap=7)

    assert (output["text"], output["byte_count"], output["truncated"]) == ("aaa", 4, True)


def test_a_byte_cap_window_admits_the_largest_cap_inline() -> None:
    largest = RepoReadInput.model_fields["byte_cap"].metadata[-1].le

    assert REPO_READ_TEXT_CHARS == largest == 1_048_576


def test_a_byte_cap_window_still_cuts_a_line_over_the_line_bound(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    wide = "w" * (CONTENT_LINE_CHARS + 1)
    body = f"{numbered(CONTENT_LINE_LIMIT, 10)}{wide}\nlast\n"

    output = read_file(ctx, canary, capsule, tree, body)

    lines = output["text"].splitlines()
    assert lines[CONTENT_LINE_LIMIT] == "w" * CONTENT_LINE_CHARS
    assert lines[-1] == "last"
    assert (output["byte_count"], output["truncated"]) == (len(body), True)


def test_a_byte_cap_window_of_withheld_lines_stops_within_the_inline_bound(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    short = "/Users/a/b\n"  # pragma: allowlist secret
    count = REPO_READ_TEXT_CHARS // len(WITHHELD_LINE) + 1
    body = short * count

    output = read_file(ctx, canary, capsule, tree, body, byte_cap=REPO_READ_TEXT_CHARS)

    kept = output["text"].split("\n")
    assert set(kept) == {WITHHELD_LINE}
    assert len(output["text"]) <= REPO_READ_TEXT_CHARS
    assert output["byte_count"] == len(kept) * len(short) < len(body)
    assert output["truncated"] is True


# ---- scrub and digest ------------------------------------------------------------


@pytest.mark.parametrize("position", [0, 250, 999])
def test_a_flagged_line_is_withheld_wherever_the_window_holds_it(
    canary: CanaryProvision,
    ctx: MethodContext,
    capsule: AuthorityCapsule,
    tree: Path,
    position: int,
) -> None:
    lines = numbered(1_000, 40).splitlines()
    lines[position] = LEAKY_LINE
    body = "".join(f"{line}\n" for line in lines)

    output = read_file(ctx, canary, capsule, tree, body)

    shown = output["text"].splitlines()
    assert shown[position] == WITHHELD_LINE
    assert LEAKY_LINE not in output["text"]
    assert len(shown) == 1_000


def test_the_content_digest_covers_the_returned_text(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    body = numbered(1_000, 40)

    output = read_file(ctx, canary, capsule, tree, body)

    expected = hashlib.sha256(output["text"].encode("utf-8")).hexdigest()
    assert output["content_digest"] == f"sha256:{expected}"
    assert output["content_ref"].endswith(f"/{bound_content(body).digest}")


def test_two_windows_agreeing_on_their_first_lines_differ_in_digest(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    head = numbered(CONTENT_LINE_LIMIT, 10)

    first = read_file(ctx, canary, capsule, tree, f"{head}tail one\n")
    second = read_file(ctx, canary, capsule, tree, f"{head}tail two\n")

    assert first["content_digest"] != second["content_digest"]
