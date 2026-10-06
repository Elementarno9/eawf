"""The commit lint exempts a ``state:`` checkpoint commit from the wave proof.

A batched close lands under one ``state:`` bookkeeping commit that advances no
single wave, so it cannot carry a per-wave CLAIMED proof;
``tools/commit_prefix_lint.py`` exempts ``type == "state"`` from the proof
branch for exactly that reason. These tests pin both halves: the exemption
holds for a checkpoint commit, and the proof branch itself is unchanged for
wave-scoped commits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _load_commit_prefix_lint() -> Any:
    """Import ``tools/commit_prefix_lint.py`` by path (it ships outside the package)."""
    import importlib.util
    import sys

    lint_path = Path(__file__).resolve().parents[2] / "tools" / "commit_prefix_lint.py"
    tool_dir = str(lint_path.parent)
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)
    spec = importlib.util.spec_from_file_location("commit_prefix_lint", lint_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = module
    spec.loader.exec_module(module)
    return module


def _write_checkpoint_hierarchy_state(tmp_path: Path, *, wave_status: str) -> Path:
    """Write a bidirectionally linked P28/P28-I01/P28-I01-W02 lint fixture."""
    import json

    payload = {
        "current": {"phase_id": "P28", "iter_id": "P28-I01"},
        "phases": {"P28": {"id": "P28", "status": "active", "iter_ids": ["P28-I01"]}},
        "iters": {
            "P28-I01": {
                "id": "P28-I01",
                "phase_id": "P28",
                "status": "active",
                "wave_ids": ["P28-I01-W02"],
            }
        },
        "waves": {
            "P28-I01-W02": {"id": "P28-I01-W02", "iter_id": "P28-I01", "status": wave_status}
        },
    }
    path = tmp_path / "checkpoint-state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_commit_message(tmp_path: Path, subject: str) -> Path:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(
        f"{subject}\n\nbody\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
        encoding="utf-8",
    )
    return path


def test_commit_prefix_lint_accepts_state_checkpoint_without_wave_proof(tmp_path: Path) -> None:
    """A ``state:`` checkpoint commit is accepted with no CLAIMED wave proof."""
    lint = _load_commit_prefix_lint()
    state_path = _write_checkpoint_hierarchy_state(tmp_path, wave_status="pending")
    message = _write_commit_message(tmp_path, "[P28-I01] state: close iter + phase (audit=AUD-1)")

    code, diag = lint.lint(
        message,
        [".ea/state.json"],
        state_path=state_path,
        canonical_state_path=tmp_path / "no-canonical-state.json",
    )

    assert code == 0, diag


def test_commit_prefix_lint_wave_proof_branch_unchanged(tmp_path: Path) -> None:
    """The wave-proof branch still rejects a wave commit whose canonical wave is PENDING."""
    lint = _load_commit_prefix_lint()
    state_path = _write_checkpoint_hierarchy_state(tmp_path, wave_status="pending")
    message = _write_commit_message(tmp_path, "[P28-I01-W02] feat: wave deliverable")

    code, diag = lint.lint(
        message,
        ["src/eawf/x.py"],
        state_path=state_path,
        canonical_state_path=state_path,
    )

    assert code == 1
    assert "canonical status 'pending'" in diag


def test_commit_prefix_lint_wave_proof_branch_accepts_claimed_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wave-proof branch still accepts a wave commit with a CLAIMED canonical wave."""
    lint = _load_commit_prefix_lint()
    # ``[P28-I01-W02]`` is a real subject on this repo's trunk, so the D54
    # one-commit-per-wave cap would fire on the live history and mask the
    # wave-proof verdict this case is about.
    monkeypatch.setattr(lint, "_prior_wave_commits", lambda *_a, **_kw: [])
    state_path = _write_checkpoint_hierarchy_state(tmp_path, wave_status="claimed")
    message = _write_commit_message(tmp_path, "[P28-I01-W02] feat: wave deliverable")

    code, diag = lint.lint(
        message,
        ["src/eawf/x.py"],
        state_path=state_path,
        canonical_state_path=state_path,
    )

    assert code == 0, diag
