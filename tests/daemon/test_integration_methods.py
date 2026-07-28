"""Daemon-owned Wave integration and dependency-barrier method tests."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import DependencyStage, WaveIntegrationKind
from eawf.kernel.state.models import State, wave_dependency_key
from eawf.runtime.daemon.methods.integration import adopt, set_dependency_barrier
from tests.daemon.test_close_lock_split import (
    _WAVE,
    _build_ctx,
    _state_payload,
)

_DOWNSTREAM = "P30-I23-W99"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo_with_state(tmp_path: Path) -> tuple[Path, Path, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "payload.txt").write_text("integrated\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-q", "-m", "test: integrated revision")

    state = State.model_validate(_state_payload())
    state.waves[_DOWNSTREAM].deps = [_WAVE]
    state.waves[_WAVE].blocks = [_DOWNSTREAM]
    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return repo, state_path, _build_ctx(tmp_path, state_path)


def test_adopt_persists_verified_ancestry_tree_and_reason(tmp_path: Path) -> None:
    repo, state_path, ctx = _repo_with_state(tmp_path)
    commit_sha = _git(repo, "rev-parse", "HEAD")

    result = asyncio.run(
        adopt(
            ctx,
            {
                "repo_root": str(repo),
                "wave_id": _WAVE,
                "commit": "HEAD",
                "reason": "record exact integrated revision for durable close",
            },
        )
    )

    assert result["ancestry_verified"] is True
    assert result["tree_verified"] is True
    assert result["integration"]["integrated_sha"] == commit_sha
    assert result["integration"]["kind"] == WaveIntegrationKind.ADOPT.value
    state = State.model_validate_json(state_path.read_bytes())
    integration = next(iter(state.wave_integrations.values()))
    assert integration.reason == "record exact integrated revision for durable close"
    assert integration.tree_sha == _git(repo, "rev-parse", "HEAD^{tree}")


def test_adopt_rejects_commit_not_integrated_into_head(tmp_path: Path) -> None:
    repo, state_path, ctx = _repo_with_state(tmp_path)
    tree_sha = _git(repo, "rev-parse", "HEAD^{tree}")
    orphan_sha = _git(repo, "commit-tree", tree_sha, "-m", "orphan")

    with pytest.raises(ValueError, match="not integrated into current HEAD"):
        asyncio.run(
            adopt(
                ctx,
                {
                    "repo_root": str(repo),
                    "wave_id": _WAVE,
                    "commit": orphan_sha,
                    "reason": "must be rejected because ancestry is absent",
                },
            )
        )

    state = State.model_validate_json(state_path.read_bytes())
    assert state.wave_integrations == {}


def test_set_dependency_barrier_persists_explicit_stages(tmp_path: Path) -> None:
    repo, state_path, ctx = _repo_with_state(tmp_path)

    result = asyncio.run(
        set_dependency_barrier(
            ctx,
            {
                "repo_root": str(repo),
                "wave_id": _DOWNSTREAM,
                "dep_wave_id": _WAVE,
                "start_after": "integrated",
                "land_after": "verified",
                "reason": "start on immutable code and land after proof",
            },
        )
    )

    key = wave_dependency_key(_DOWNSTREAM, _WAVE)
    assert result["barrier_key"] == key
    state = State.model_validate_json(state_path.read_bytes())
    barrier = state.wave_dependency_barriers[key]
    assert barrier.start_after is DependencyStage.INTEGRATED
    assert barrier.land_after is DependencyStage.VERIFIED


def test_set_dependency_barrier_rejects_non_dependency(tmp_path: Path) -> None:
    repo, state_path, ctx = _repo_with_state(tmp_path)
    state = State.model_validate_json(state_path.read_bytes())
    state.waves[_DOWNSTREAM].deps = []
    state.waves[_WAVE].blocks = []
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    with pytest.raises(ValueError, match="is not a dependency"):
        asyncio.run(
            set_dependency_barrier(
                ctx,
                {
                    "repo_root": str(repo),
                    "wave_id": _DOWNSTREAM,
                    "dep_wave_id": _WAVE,
                    "start_after": "closed",
                    "land_after": "closed",
                    "reason": "invalid edge",
                },
            )
        )
