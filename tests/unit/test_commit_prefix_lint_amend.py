"""Amend-path unit tests for ``tools/commit_prefix_lint.py``.

The one-commit-per-Task cap greps ``HEAD`` for the Task's key, so it read an
amend of the Task commit and a genuine second commit for that Task as the
same event and rejected both -- while the cap's own diagnostic prescribes the
amend. Recognising the amend reopens the prescribed door and leaves the
second commit capped.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_PATH = _REPO_ROOT / "tools" / "commit_prefix_lint.py"
_TOOL_DIR = _LINT_PATH.parent

_DELIVERABLE_PATHS = ["src/eawf/x.py"]
#: Author-date epoch of the fixture Task commit, in git's internal spelling.
_HEAD_EPOCH = "1788883700"
_HEAD_SHA = "a" * 40


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


def _model_head(
    mod: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prior: list[str],
    head: tuple[str, str] | None,
) -> None:
    """Model the Task's existing commits and the tip the probe would report."""
    monkeypatch.setattr(mod, "_prior_commits", lambda *_a, **_kw: prior)
    monkeypatch.setattr(mod, "_head_identity", lambda *_a, **_kw: head)


def _cap(mod: Any, env: dict[str, str]) -> tuple[int, str] | None:
    return mod._check_commit_cap(
        unit="task EAWF-0001",
        terms=["Task: EAWF-0001"],
        commit_type="fix",
        staged=_DELIVERABLE_PATHS,
        repo_root=None,
        env=env,
    )


# ---------------------------------------------------------------------------
# The cap distinguishes an amend from a second commit.
# ---------------------------------------------------------------------------


def test_commit_cap_accepts_amend_adding_bytes(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """An amend of the Task commit passes even when it adds deliverable bytes."""
    _model_head(mod, monkeypatch, prior=[_HEAD_SHA], head=(_HEAD_SHA, _HEAD_EPOCH))
    result = _cap(mod, {"GIT_AUTHOR_DATE": f"@{_HEAD_EPOCH} +0200"})

    assert result is None


def test_commit_cap_rejects_second_commit(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh commit stamps its own author date, so the cap still fires."""
    _model_head(mod, monkeypatch, prior=[_HEAD_SHA], head=(_HEAD_SHA, _HEAD_EPOCH))
    result = _cap(mod, {"GIT_AUTHOR_DATE": f"@{int(_HEAD_EPOCH) + 60} +0200"})

    assert result is not None
    assert result[0] == 1
    assert "second commit for task EAWF-0001" in result[1]


def test_commit_cap_rejects_second_commit_without_author_date(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: no ``GIT_AUTHOR_DATE`` in the env means no amend signal."""
    _model_head(mod, monkeypatch, prior=[_HEAD_SHA], head=(_HEAD_SHA, _HEAD_EPOCH))
    result = _cap(mod, {})

    assert result is not None
    assert result[0] == 1
    assert "second commit for task EAWF-0001" in result[1]


def test_commit_cap_rejects_amend_of_a_commit_that_is_not_head(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: only the tip can be amended, so a buried Task commit caps."""
    _model_head(mod, monkeypatch, prior=["b" * 40], head=(_HEAD_SHA, _HEAD_EPOCH))
    result = _cap(mod, {"GIT_AUTHOR_DATE": f"@{_HEAD_EPOCH} +0200"})

    assert result is not None
    assert result[0] == 1
    assert "second commit for task EAWF-0001" in result[1]


def test_commit_cap_rejects_when_the_head_probe_fails(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: an unreadable tip yields no amend signal, so the cap holds."""
    _model_head(mod, monkeypatch, prior=[_HEAD_SHA], head=None)
    result = _cap(mod, {"GIT_AUTHOR_DATE": f"@{_HEAD_EPOCH} +0200"})

    assert result is not None
    assert result[0] == 1
    assert "second commit for task EAWF-0001" in result[1]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@1788883700 +0200", "1788883700"),
        ("1788883700 +0200", "1788883700"),
        ("@1788883700", "1788883700"),
        ("  @1788883700 +0200  ", "1788883700"),
        (None, None),
        ("", None),
        ("   ", None),
        ("2026-09-08T12:00:00+02:00", None),
        ("@notanepoch +0200", None),
    ],
    ids=[
        "git-internal",
        "bare-epoch",
        "no-timezone",
        "padded",
        "absent",
        "empty",
        "blank",
        "iso",
        "non-numeric",
    ],
)
def test_author_date_epoch_boundaries(mod: Any, raw: str | None, expected: str | None) -> None:
    """Only git's internal author-date spelling yields an epoch to compare."""
    assert mod._author_date_epoch(raw) == expected


def test_head_identity_returns_sha_and_epoch(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed probe yields the tip's SHA and author-date epoch."""
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=0, stdout=f"{_HEAD_SHA} {_HEAD_EPOCH}\n", stderr=""
        ),
    )
    assert mod._head_identity(None) == (_HEAD_SHA, _HEAD_EPOCH)


@pytest.mark.parametrize(
    "stdout",
    ["", f"{_HEAD_SHA}\n", f"{_HEAD_SHA} not-an-epoch\n", f"{_HEAD_SHA} 17 extra\n"],
    ids=["empty", "one-field", "non-numeric-epoch", "three-fields"],
)
def test_head_identity_rejects_malformed_probe_output(
    mod: Any, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    """Boundary: anything but ``<sha> <epoch>`` reads as no tip at all."""
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )
    assert mod._head_identity(None) is None


def test_head_identity_skips_a_non_zero_probe(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Error path: an unborn HEAD exits non-zero and reports no tip."""
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=128, stdout="", stderr="no HEAD"),
    )
    assert mod._head_identity(None) is None


def test_head_identity_fails_open_when_git_is_missing(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: an OSError from the probe reports no tip."""

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("binary not on PATH")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    assert mod._head_identity(None) is None


def test_head_identity_fails_open_on_timeout(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Error path: a timed-out probe reports no tip."""

    def _slow(*_args: object, **_kwargs: object) -> object:
        raise mod.subprocess.TimeoutExpired(cmd="log", timeout=1.0)

    monkeypatch.setattr(mod.subprocess, "run", _slow)
    assert mod._head_identity(None) is None
