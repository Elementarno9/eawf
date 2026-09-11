"""Amend-path unit tests for ``tools/commit_prefix_lint.py``.

Two doors used to be shut on the same operator action.

The one-commit-per-wave cap greps ``HEAD`` for the wave's id, so it read an
amend of the wave commit and a genuine second commit for that wave as the same
event and rejected both -- while the cap's own diagnostic prescribes the amend.
Recognising the amend reopens the prescribed door and leaves the second commit
capped.

The claimed-proof check then refused the fold amend the single-wave-close
diagnostic instructs: by the time the close records exist to fold, the wave is
CLOSED, and the check demanded CLAIMED. A commit that stages only
state-bookkeeping paths adds no deliverable bytes, so it is now proven by a
CLOSED wave too; adding deliverable bytes under a closed wave stays rejected.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_PATH = _REPO_ROOT / "tools" / "commit_prefix_lint.py"
_TOOL_DIR = _LINT_PATH.parent

_CLAUDE_TRAILER = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
_STATE_PATHS = [".ea/state.json", ".ea/store/evidence.jsonl"]
_DELIVERABLE_PATHS = ["src/eawf/x.py"]
#: Author-date epoch of the fixture wave commit, in git's internal spelling.
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


@pytest.fixture(autouse=True)
def _no_prior_wave_commits(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every case to a wave with no commit yet on ``HEAD``.

    Without this the cap would shell out to the real repository, where the
    fixture wave ids genuinely exist on trunk. The cap's own cases re-patch it.
    """
    monkeypatch.setattr(mod, "_prior_wave_commits", lambda *_a, **_kw: [])


def _write_msg(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(body.rstrip() + _CLAUDE_TRAILER, encoding="utf-8")
    return path


def _model_head(
    mod: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prior: list[str],
    head: tuple[str, str] | None,
) -> None:
    """Model the wave's existing commits and the tip the probe would report."""
    monkeypatch.setattr(mod, "_prior_wave_commits", lambda *_a, **_kw: prior)
    monkeypatch.setattr(mod, "_head_identity", lambda *_a, **_kw: head)


def _write_wave_state(tmp_path: Path, *, wave_status: str) -> Path:
    """Write one bidirectionally linked phase/iter/wave state fixture."""
    phase_id = "P32"
    iter_id = f"{phase_id}-I01"
    wave_id = f"{iter_id}-W33"
    payload = {
        "current": {"phase_id": phase_id, "iter_id": iter_id},
        "phases": {phase_id: {"id": phase_id, "status": "active", "iter_ids": [iter_id]}},
        "iters": {
            iter_id: {
                "id": iter_id,
                "phase_id": phase_id,
                "status": "active",
                "wave_ids": [wave_id],
            }
        },
        "waves": {wave_id: {"id": wave_id, "iter_id": iter_id, "status": wave_status}},
    }
    path = tmp_path / f"state-{wave_status}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The cap distinguishes an amend from a second commit.
# ---------------------------------------------------------------------------


def test_commit_cap_accepts_amend_adding_bytes(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An amend of the wave commit passes even when it adds deliverable bytes."""
    _model_head(mod, monkeypatch, prior=[_HEAD_SHA], head=(_HEAD_SHA, _HEAD_EPOCH))
    msg = _write_msg(tmp_path, "[P31-W27] fix: the wave deliverable, enlarged\n")

    code, diag = mod.lint(
        msg,
        _DELIVERABLE_PATHS,
        env={"GIT_AUTHOR_DATE": f"@{_HEAD_EPOCH} +0200"},
    )

    assert code == 0, diag


def test_commit_cap_rejects_second_commit(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh commit stamps its own author date, so the cap still fires."""
    _model_head(mod, monkeypatch, prior=[_HEAD_SHA], head=(_HEAD_SHA, _HEAD_EPOCH))
    msg = _write_msg(tmp_path, "[P31-W27] fix: a second bite at the same wave\n")

    code, diag = mod.lint(
        msg,
        _DELIVERABLE_PATHS,
        env={"GIT_AUTHOR_DATE": f"@{int(_HEAD_EPOCH) + 60} +0200"},
    )

    assert code == 1
    assert "second commit for wave P31-I01-W27" in diag


def test_commit_cap_rejects_second_commit_without_author_date(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: no ``GIT_AUTHOR_DATE`` in the env means no amend signal."""
    _model_head(mod, monkeypatch, prior=[_HEAD_SHA], head=(_HEAD_SHA, _HEAD_EPOCH))
    msg = _write_msg(tmp_path, "[P31-W27] fix: no author date to compare\n")

    code, diag = mod.lint(msg, _DELIVERABLE_PATHS, env={})

    assert code == 1
    assert "second commit for wave P31-I01-W27" in diag


def test_commit_cap_rejects_amend_of_a_commit_that_is_not_head(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: only the tip can be amended, so a buried wave commit caps."""
    _model_head(mod, monkeypatch, prior=["b" * 40], head=(_HEAD_SHA, _HEAD_EPOCH))
    msg = _write_msg(tmp_path, "[P31-W27] fix: the wave commit is buried\n")

    code, diag = mod.lint(
        msg,
        _DELIVERABLE_PATHS,
        env={"GIT_AUTHOR_DATE": f"@{_HEAD_EPOCH} +0200"},
    )

    assert code == 1
    assert "second commit for wave P31-I01-W27" in diag


def test_commit_cap_rejects_when_the_head_probe_fails(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: an unreadable tip yields no amend signal, so the cap holds."""
    _model_head(mod, monkeypatch, prior=[_HEAD_SHA], head=None)
    msg = _write_msg(tmp_path, "[P31-W27] fix: unreadable tip\n")

    code, diag = mod.lint(
        msg,
        _DELIVERABLE_PATHS,
        env={"GIT_AUTHOR_DATE": f"@{_HEAD_EPOCH} +0200"},
    )

    assert code == 1
    assert "second commit for wave P31-I01-W27" in diag


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


# ---------------------------------------------------------------------------
# The fold amend the single-wave-close diagnostic prescribes.
# ---------------------------------------------------------------------------


def test_fold_amend_accepts_state_only_paths_under_a_closed_wave(tmp_path: Path, mod: Any) -> None:
    """The close records fold into the wave commit after the wave closed."""
    state = _write_wave_state(tmp_path, wave_status="closed")
    msg = _write_msg(tmp_path, "[P32-I01-W33] fix: the wave deliverable\n")

    code, diag = mod.lint(
        msg,
        _STATE_PATHS,
        state_path=state,
        canonical_state_path=state,
    )

    assert code == 0, diag


def test_fold_amend_accepts_an_empty_stage_under_a_closed_wave(tmp_path: Path, mod: Any) -> None:
    """Boundary (empty): a reword amend stages nothing and adds no bytes."""
    state = _write_wave_state(tmp_path, wave_status="closed")
    msg = _write_msg(tmp_path, "[P32-I01-W33] fix: reworded after the close\n")

    code, diag = mod.lint(msg, [], state_path=state, canonical_state_path=state)

    assert code == 0, diag


def test_fold_amend_rejects_deliverable_bytes_under_a_closed_wave(tmp_path: Path, mod: Any) -> None:
    """A closed wave proves bookkeeping only -- new bytes need a live wave."""
    state = _write_wave_state(tmp_path, wave_status="closed")
    msg = _write_msg(tmp_path, "[P32-I01-W33] fix: late deliverable bytes\n")

    code, diag = mod.lint(
        msg,
        [*_STATE_PATHS, "src/eawf/x.py"],
        state_path=state,
        canonical_state_path=state,
    )

    assert code == 1
    assert "canonical status 'closed' is not CLAIMED or IN_PROGRESS" in diag


def test_fold_amend_rejects_a_pending_wave(tmp_path: Path, mod: Any) -> None:
    """Error path: the widened set adds CLOSED only, never PENDING."""
    state = _write_wave_state(tmp_path, wave_status="pending")
    msg = _write_msg(tmp_path, "[P32-I01-W33] fix: never claimed\n")

    code, diag = mod.lint(
        msg,
        _STATE_PATHS,
        state_path=state,
        canonical_state_path=state,
    )

    assert code == 1
    assert "canonical status 'pending' is not CLAIMED, IN_PROGRESS or CLOSED" in diag


def test_fold_amend_keeps_a_claimed_wave_source_commit_green(tmp_path: Path, mod: Any) -> None:
    """Regression: the CLAIMED source-commit path is untouched by the widening."""
    state = _write_wave_state(tmp_path, wave_status="claimed")
    msg = _write_msg(tmp_path, "[P32-I01-W33] fix: the wave deliverable\n")

    code, diag = mod.lint(
        msg,
        _DELIVERABLE_PATHS,
        state_path=state,
        canonical_state_path=state,
    )

    assert code == 0, diag
