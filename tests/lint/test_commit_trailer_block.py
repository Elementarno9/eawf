"""The pushed-commit lint reads Batch and Task trailers from git's trailer block.

Git takes trailers only from a message's last paragraph, so a ``Batch`` or
``Task`` line a blank line separates from a closing ``Co-Authored-By`` block
is invisible to git and to the integration while a reader still sees it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_TOOL_DIR = Path(__file__).resolve().parents[2] / "tools"

pytestmark = pytest.mark.unit

# The real message tail of a882842cd323, which landed with its Batch and Task
# trailers in a paragraph of their own above the co-author trailer.
_DETACHED_MESSAGE = (
    "test: forward the added runner arguments\n\n"
    "Include the two added runner arguments in the exhaustive forwarding\n"
    "fixture so the compiler and its coverage stay aligned.\n\n"
    "Batch: BAT-0364\n"
    "Task: EAWF-0607\n\n"
    "Co-Authored-By: Codex <noreply@openai.com>\n"
)
_JOINED_MESSAGE = _DETACHED_MESSAGE.replace("Task: EAWF-0607\n\n", "Task: EAWF-0607\n")
_BARE_MESSAGE = _DETACHED_MESSAGE.split("Co-Authored-By", 1)[0].rstrip("\n") + "\n"


@pytest.fixture()
def lint() -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location(
        "commit_prefix_lint", _TOOL_DIR / "commit_prefix_lint.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def records(lint: Any) -> Any:
    return lint.NativeRecords(
        batch_keys=frozenset({"BAT-0364"}),
        task_batches={"EAWF-0607": "BAT-0364", "EAWF-0608": "BAT-0364"},
    )


def test_trailers_split_from_coauthor_by_blank_line_rejected(lint: Any, records: Any) -> None:
    error = lint.pushed_commit_error(_DETACHED_MESSAGE, ["tests/x.py"], records)

    assert error is not None
    assert "'Batch: BAT-0364' is not in the final trailer block git parses" in error


def test_single_task_trailer_split_from_coauthor_rejected(lint: Any, records: Any) -> None:
    message = (
        "fix: a thing\n\nWhy.\n\nTask: EAWF-0607\n\nCo-Authored-By: Codex <noreply@openai.com>\n"
    )

    error = lint.pushed_commit_error(message, ["a.py"], records)

    assert error is not None
    assert "'Task: EAWF-0607' is not in the final trailer block" in error


def test_task_trailer_followed_by_prose_paragraph_rejected(lint: Any, records: Any) -> None:
    message = "fix: a thing\n\nTask: EAWF-0607\n\nA closing paragraph of prose.\n"

    error = lint.pushed_commit_error(message, ["a.py"], records)

    assert error is not None
    assert "'Task: EAWF-0607'" in error


def test_second_task_detached_from_final_block_rejected(lint: Any, records: Any) -> None:
    message = (
        "feat: two Tasks\n\nTask: EAWF-0608\n\nBatch: BAT-0364\nTask: EAWF-0607\n"
        "Co-Authored-By: Codex <noreply@openai.com>\n"
    )

    error = lint.pushed_commit_error(message, ["a.py"], records)

    assert error is not None
    assert "'Task: EAWF-0608'" in error


def test_trailers_joined_with_coauthor_admitted(lint: Any, records: Any) -> None:
    assert lint.pushed_commit_error(_JOINED_MESSAGE, ["tests/x.py"], records) is None


def test_trailers_in_final_block_without_coauthor_admitted(lint: Any, records: Any) -> None:
    assert lint.pushed_commit_error(_BARE_MESSAGE, ["tests/x.py"], records) is None


def test_only_final_block_trailer_admitted(lint: Any, records: Any) -> None:
    message = "fix: a thing\n\nTask: EAWF-0607\n"

    assert lint.pushed_commit_error(message, ["a.py"], records) is None


def test_message_without_trailers_still_names_missing_task(lint: Any, records: Any) -> None:
    error = lint.pushed_commit_error("fix: a thing\n\nNo trailers here.\n", ["a.py"], records)

    assert error is not None
    assert "carries no Task trailers" in error


def test_state_commit_skips_trailer_block_read(lint: Any, records: Any) -> None:
    message = "state: record\n\nTask: EAWF-0607\n\nCo-Authored-By: Codex <noreply@openai.com>\n"

    assert lint.pushed_commit_error(message, [".ea/generations/g/state.json"], records) is None
