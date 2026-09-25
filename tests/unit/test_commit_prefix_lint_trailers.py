"""Wave trailers in ``tools/commit_prefix_lint.py``: several per commit, none dropped.

A batch commit carries one ``Eawf-Wave`` line per wave it lands, and the
commit-msg check accepts it. A rewrite of pushed history is checked by
``--check-rewrite OLD NEW`` (and the ``--pre-push`` hook built on it): every
wave the old side names by trailer must still be named on the new side, or
the post-merge repin has nothing to find that wave by.

Git is modelled here; ``tests/integration/test_wave_trailer_guard_e2e.py``
runs the same checks against a scratch repository.
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
_TOOL_DIR = _REPO_ROOT / "tools"
_CLAUDE_TRAILER = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
_OLD = "a" * 40
_NEW = "b" * 40
_BASE = "c" * 40
_OLD_SIDE = [
    "feat: a\n\nEawf-Wave: P34-I01-W01\n",
    "feat: b\n\nEawf-Wave: P34-I01-W02\nEawf-Wave: P34-I01-W03\n",
]


def _load(name: str) -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location(name, _TOOL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def lint() -> Any:
    return _load("commit_prefix_lint")


@pytest.fixture()
def guard(lint: Any) -> Any:
    return _load("wave_trailer_guard")


def _model_git(
    guard: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_ancestor: int,
    base_rc: int = 0,
    new_side: list[str] | None = None,
) -> None:
    """Answer the guard's merge-base probes and range reads without git."""

    def fake_git(args: list[str], *, repo_root: Path | None) -> SimpleNamespace:
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return SimpleNamespace(returncode=is_ancestor, stdout="", stderr="")
        return SimpleNamespace(returncode=base_rc, stdout=f"{_BASE}\n", stderr="")

    def fake_range(rev_range: str, *, repo_root: Path | None) -> list[str] | None:
        return _OLD_SIDE if rev_range.endswith(_OLD) else new_side

    monkeypatch.setattr(guard, "_git", fake_git)
    monkeypatch.setattr(guard, "_range_messages", fake_range)


# ---------------------------------------------------------------------------
# commit-msg: several trailers on one commit
# ---------------------------------------------------------------------------


def test_lint_accepts_a_commit_carrying_several_wave_trailers(
    tmp_path: Path, lint: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lint, "_prior_wave_commits", lambda *_a, **_kw: [])
    iter_id = "P34-I01"
    waves = [f"{iter_id}-W02", f"{iter_id}-W03"]
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "current": {"phase_id": "P34", "iter_id": iter_id},
                "phases": {"P34": {"id": "P34", "status": "active", "iter_ids": [iter_id]}},
                "iters": {
                    iter_id: {
                        "id": iter_id,
                        "phase_id": "P34",
                        "status": "active",
                        "wave_ids": waves,
                    }
                },
                "waves": {w: {"id": w, "iter_id": iter_id, "status": "claimed"} for w in waves},
            }
        ),
        encoding="utf-8",
    )
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(
        "feat: land a batch\n\nBody.\n\nEawf-Wave: P34-I01-W02\nEawf-Wave: P34-I01-W03"
        + _CLAUDE_TRAILER,
        encoding="utf-8",
    )

    code, diag = lint.lint(msg, ["src/eawf/x.py"], state_path=state, canonical_state_path=state)

    assert code == 0, diag


# ---------------------------------------------------------------------------
# The pure comparison
# ---------------------------------------------------------------------------


def test_dropped_wave_ids_accepts_a_batch_keeping_every_trailer(guard: Any) -> None:
    new = ["feat: ab\n\nEawf-Wave: P34-I01-W01\nEawf-Wave: P34-W02\nEawf-Wave: P34-I01-W03\n"]

    assert guard.dropped_wave_ids(_OLD_SIDE, new) == []


def test_dropped_wave_ids_names_the_missing_wave(guard: Any) -> None:
    new = ["feat: ab\n\nEawf-Wave: P34-I01-W01\nEawf-Wave: P34-I01-W02\n"]

    assert guard.dropped_wave_ids(_OLD_SIDE, new) == ["P34-I01-W03"]


def test_dropped_wave_ids_boundaries(guard: Any) -> None:
    assert guard.dropped_wave_ids([], []) == []
    assert guard.dropped_wave_ids(["chore: no trailer\n"], []) == []
    assert guard.dropped_wave_ids(["x\n\nEawf-Wave: P34-I01-W01\n"], []) == ["P34-I01-W01"]
    # A trailer quoted inside prose is not a trailer line.
    assert guard.trailer_wave_ids(["see Eawf-Wave: P34-I01-W01 above\n"]) == set()


# ---------------------------------------------------------------------------
# check_rewrite over modelled history
# ---------------------------------------------------------------------------


def test_check_rewrite_fast_forward_reads_no_messages(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=0, new_side=None)

    assert guard.check_rewrite(_OLD, _NEW) == (0, "")


def test_check_rewrite_accepts_a_squash_keeping_every_trailer(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    squash = "feat: s\n\nEawf-Wave: P34-I01-W01\nEawf-Wave: P34-I01-W02\nEawf-Wave: P34-I01-W03\n"
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=[squash])

    assert guard.check_rewrite(_OLD, _NEW) == (0, "")


def test_check_rewrite_refuses_a_squash_dropping_a_trailer(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=["feat: s\n\nEawf-Wave: P34-I01-W01\n"])

    code, diag = guard.check_rewrite(_OLD, _NEW)

    assert code == 1
    assert "drops the Eawf-Wave trailer of P34-I01-W02, P34-I01-W03" in diag


def test_check_rewrite_refuses_an_unknown_tip(guard: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _model_git(guard, monkeypatch, is_ancestor=128, base_rc=128)

    code, diag = guard.check_rewrite(_OLD, _NEW)

    assert code == 1
    assert "fetch the remote tip first" in diag


def test_check_rewrite_refuses_unreadable_ranges(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=None)

    code, diag = guard.check_rewrite(_OLD, _NEW)

    assert code == 1
    assert "cannot read the commits" in diag


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"PRE_COMMIT_FROM_REF": "0" * 40, "PRE_COMMIT_TO_REF": _NEW},
        {"PRE_COMMIT_FROM_REF": _OLD, "PRE_COMMIT_TO_REF": "0" * 40},
    ],
    ids=["no-variables", "new-ref", "deleted-ref"],
)
def test_check_pre_push_passes_without_history_to_replace(
    guard: Any, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=[])

    assert guard.check_pre_push(env) == (0, "")


def test_check_pre_push_refuses_a_dropping_rewrite(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=[])
    env = {"PRE_COMMIT_FROM_REF": _OLD, "PRE_COMMIT_TO_REF": _NEW}

    code, diag = guard.check_pre_push(env)

    assert code == 1
    assert "P34-I01-W01, P34-I01-W02, P34-I01-W03" in diag


def test_main_check_rewrite_rejects_wrong_arity(
    lint: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint.main(["lint", "--check-rewrite", _OLD]) == 1
    assert "usage: commit_prefix_lint.py --check-rewrite" in capsys.readouterr().err
