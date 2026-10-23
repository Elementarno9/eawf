"""The golden-only exemption from the one-commit cap in ``tools/commit_prefix_lint.py``.

Every ``test`` commit used to escape the one-commit cap, although only the
managed golden refresh the snapshot-pairing gate forces is a legitimate
second commit. Now a ``test`` commit escapes it only when every path it
stages is a managed snapshot golden.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from eawf.surfaces.cli.commands.snapshot import SNAPSHOT_SURFACES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_PATH = _REPO_ROOT / "tools" / "commit_prefix_lint.py"
_TOOL_DIR = _LINT_PATH.parent

_STATE_PATHS = [".ea/state.json", ".ea/store/evidence.jsonl"]
_PRIOR_SHA = "a" * 40
_UNIT = "task EAWF-0001"
_TERMS = ["Task: EAWF-0001", "[EAWF-0001]"]


def _load_module() -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("commit_prefix_lint", _LINT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod() -> Any:
    return _load_module()


def _cap(mod: Any, commit_type: str, staged: list[str]) -> tuple[int, str] | None:
    return mod._check_commit_cap(
        unit=_UNIT, terms=_TERMS, commit_type=commit_type, staged=staged, repo_root=None, env={}
    )


def _model_prior_commit(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a Task that already has a commit and a fresh (non-amend) commit."""
    monkeypatch.setattr(mod, "_prior_commits", lambda *_a, **_kw: [_PRIOR_SHA])
    monkeypatch.setattr(mod, "_head_identity", lambda *_a, **_kw: None)


@pytest.mark.parametrize(
    "staged",
    [
        ["tests/golden/envelope/ok.json"],
        ["tests/snapshots/svg/golden/modes_feed.png", "tests/golden/agents_md/core_only.md"],
        ["tests/golden/surfaces/cli/scenarios/wave_close.txt"],
    ],
    ids=["one-golden", "two-surfaces", "nested-surface"],
)
def test_golden_only_test_commit_skips_the_cap(
    mod: Any, monkeypatch: pytest.MonkeyPatch, staged: list[str]
) -> None:
    """The paired golden refresh is the one second commit a Task may take."""
    _model_prior_commit(mod, monkeypatch)

    assert _cap(mod, "test", staged) is None


@pytest.mark.parametrize(
    "staged",
    [
        ["tests/unit/test_x.py"],
        ["tests/golden/envelope/ok.json", "tests/unit/test_x.py"],
        ["tests/golden/cli/help.txt"],
        ["tests/golden/surfaces/cli/scenarios/conftest.py"],
        ["tests/golden/envelope_extra/ok.json"],
        ["tests/golden/envelope/ok.json", ".ea/state.json", "src/eawf/x.py"],
    ],
    ids=[
        "unit-test",
        "golden-plus-unit-test",
        "unmanaged-golden-tree",
        "golden-generator-python",
        "sibling-prefix",
        "golden-plus-source",
    ],
)
def test_non_golden_test_commit_is_capped(
    mod: Any, monkeypatch: pytest.MonkeyPatch, staged: list[str]
) -> None:
    """A test commit that stages anything but managed goldens is a second commit."""
    _model_prior_commit(mod, monkeypatch)

    code, diag = _cap(mod, "test", staged)

    assert code == 1
    assert f"second commit for {_UNIT}" in diag
    assert "managed snapshot golden" in diag


def test_non_test_commit_cap_diagnostic_omits_the_golden_note(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The golden exemption note is only actionable for a test commit."""
    _model_prior_commit(mod, monkeypatch)

    code, diag = _cap(mod, "fix", ["src/eawf/x.py"])

    assert code == 1
    assert f"second commit for {_UNIT}" in diag
    assert "managed snapshot golden" not in diag


def test_state_commit_still_skips_the_cap(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the state type keeps its exemption."""
    _model_prior_commit(mod, monkeypatch)

    assert _cap(mod, "state", _STATE_PATHS) is None


def test_managed_golden_dirs_match_the_snapshot_inventory(mod: Any) -> None:
    """The hook's hand-kept mirror equals the inventory the pairing gate watches."""
    inventory = tuple(sorted(f"{surface.golden_dir}/" for surface in SNAPSHOT_SURFACES.values()))
    assert inventory == mod._MANAGED_GOLDEN_DIRS


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("", False),
        ("tests/golden/envelope/", True),
        ("tests/golden/envelope/x.json", True),
        ("tests/golden/envelope/test_x.py", False),
        ("tests/golden/envelope", False),
        ("tests/golden/cli/x.txt", False),
        ("tests/snapshots/svg/golden/x.svg", True),
        ("tests/snapshots/statusline/x.txt", False),
    ],
    ids=[
        "empty",
        "dir-itself",
        "golden-file",
        "python",
        "no-trailing-slash",
        "unmanaged-tree",
        "snapshots-tree",
        "unmanaged-snapshot",
    ],
)
def test_is_managed_golden_boundaries(mod: Any, path: str, expected: bool) -> None:
    """Boundary sweep of the golden-only predicate."""
    assert mod._is_managed_golden(path) is expected


def test_the_first_commit_for_a_task_is_not_capped(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: no prior commit for the Task, so the cap does not fire."""
    monkeypatch.setattr(mod, "_prior_commits", lambda *_a, **_kw: [])

    assert _cap(mod, "fix", ["src/eawf/x.py"]) is None


def test_module_docstring_states_the_golden_only_rule(mod: Any) -> None:
    """The lint's own docstring carries the rule a reader needs."""
    assert "golden-only" in mod.__doc__
    assert "managed snapshot golden" in mod.__doc__
