"""AUTH-028: Task identity in version control is a trailer by default.

The trailer is the default and a subject prefix is the configured
alternative. Wherever it is rendered, the Task trailer is the last
trailer before the agent co-author trailers -- the daemon's own delivery
commits included, so the commit-msg hook's position grammar reads them
the same way it reads an agent's commit. The two remaining clauses, the
prefix form's warning and the rejection of a Task absent from state, are
proved against a real generation by ``test_lint_031_*`` and
``test_lint_008_*`` in ``tests/contract/platform/lint/test_commit_task_existence.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.config.defaults import built_in_defaults
from eawf.runtime.integration.commit_policy import (
    PROVENANCE_TRAILER_KEY,
    TASK_TRAILER_KEY,
    render_delivery_commits,
)
from eawf.runtime.vcs.coauthor import VcsConfig
from tests.unit.runtime.integration.test_commit_policy import bundle, manifest_of

_TOOL_DIR = Path(__file__).resolve().parents[4] / "tools"
_COAUTHOR = "Co-Authored-By: Claude <noreply@anthropic.com>"


@pytest.fixture()
def lint() -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location(
        "commit_prefix_lint", _TOOL_DIR / "commit_prefix_lint.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = module
    spec.loader.exec_module(module)
    return module


def test_auth_028_the_trailer_is_the_default_task_reference() -> None:
    assert built_in_defaults()["vcs"]["task_reference"] == "trailer"
    assert VcsConfig.model_fields["task_reference"].default == "trailer"


def test_auth_028_the_daemon_and_the_hook_name_the_task_trailer_alike(lint: Any) -> None:
    assert lint._TASK_TRAILER_NAME == TASK_TRAILER_KEY


def test_auth_028_a_per_task_delivery_commit_ends_with_its_task_trailer() -> None:
    manifest = manifest_of(bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b"))
    commits = render_delivery_commits(
        manifest, unit="task", task_reference="trailer", batch_subject="unused"
    )
    assert [commit.trailers[-1] for commit in commits] == [
        f"{TASK_TRAILER_KEY}: EAWF-0001",
        f"{TASK_TRAILER_KEY}: EAWF-0002",
    ]
    assert all(commit.trailers[0].startswith(f"{PROVENANCE_TRAILER_KEY}: ") for commit in commits)


def test_auth_028_a_delivery_commit_passes_the_hook_position_grammar(lint: Any) -> None:
    manifest = manifest_of(bundle(task="EAWF-0001"))
    (commit,) = render_delivery_commits(
        manifest, unit="task", task_reference="trailer", batch_subject="unused"
    )
    assert lint.task_trailer_position_error(f"{commit.message}\n{_COAUTHOR}\n") is None


def test_auth_028_a_squashed_batch_keeps_every_task_trailer_last() -> None:
    manifest = manifest_of(
        bundle(task="EAWF-0001"),
        bundle(task="EAWF-0002", digest="b"),
        bundle(task="EAWF-0003", digest="c"),
    )
    (commit,) = render_delivery_commits(
        manifest, unit="batch", task_reference="trailer", batch_subject="deliver the batch"
    )
    tail = commit.message.splitlines()[-3:]
    assert tail == [f"{TASK_TRAILER_KEY}: EAWF-000{index}" for index in (1, 2, 3)]


def test_auth_028_the_prefix_configuration_moves_identity_into_the_subject() -> None:
    manifest = manifest_of(bundle(task="EAWF-0001"))
    (commit,) = render_delivery_commits(
        manifest, unit="task", task_reference="subject", batch_subject="unused"
    )
    assert commit.subject.startswith("[EAWF-0001] ")
    assert not any(line.startswith(f"{TASK_TRAILER_KEY}: ") for line in commit.trailers)


@pytest.mark.parametrize(
    "text",
    [
        f"feat: a\n\nTask: EAWF-0001\nEawf-Provenance: manifest://x\n{_COAUTHOR}\n",
        f"feat: a\n\n{_COAUTHOR}\nTask: EAWF-0001\n",
        "feat: a\n\nTask: EAWF-0001\nTask: EAWF-0002\n",
    ],
)
def test_auth_028_a_task_trailer_out_of_place_is_named(lint: Any, text: str) -> None:
    assert lint.task_trailer_position_error(text) is not None
