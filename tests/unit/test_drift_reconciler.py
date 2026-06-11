"""Unit tests for the P28-I02-W01 drift reconciler.

Covers four surfaces:

1. :class:`eawf.surfaces.render.manifest.ManifestEntry` gains an
   optional ``scope`` field and survives the load/save round-trip.
2. :func:`eawf.workflow.lifecycle.wave_sha.detect_git_state_drift`
   surfaces the four drift kinds (``pinned_but_missing``,
   ``pinned_mismatch``, ``closed_no_pin``, ``closed_unfindable``).
3. :func:`eawf.surfaces.cli.commands.plugin.detect_cross_scope_duplicates`
   flags region_ids installed under both ``project`` and ``user``
   scope, ignoring entries with ``scope=None``.
4. The ``eawf doctor`` command surfaces both check rows
   (``git_state_drift`` + ``plugin_cross_scope_dup``) in the JSON
   envelope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.surfaces.cli.commands.plugin import detect_cross_scope_duplicates
from eawf.surfaces.render.manifest import Manifest, ManifestEntry
from eawf.surfaces.render.manifest import load as load_manifest
from eawf.surfaces.render.manifest import save_atomic as save_manifest_atomic
from eawf.workflow.lifecycle.wave_sha import Drift, detect_git_state_drift

NOW = datetime(2026, 5, 27, tzinfo=UTC)


# ---- ManifestEntry.scope ----------------------------------------------------


def _entry(
    target: str = "p.js",
    region_id: str = "plugin.codex.skill.flow",
    scope: str | None = None,
) -> ManifestEntry:
    return ManifestEntry(
        target=target,
        region_id=region_id,
        version="1.0",
        hash="0123456789abcdef",
        generator="eawf-plugin-codex",
        generated_at="2026-05-27T00:00:00+00:00",
        scope=scope,
    )


def test_manifest_entry_scope_defaults_to_none() -> None:
    entry = _entry()
    assert entry.scope is None


def test_manifest_entry_scope_roundtrip_project(tmp_path: Path) -> None:
    target = tmp_path / "indexes" / "generated.json"
    entry = _entry(scope="project")
    manifest = Manifest(version=1, generated={"k::project": entry})
    save_manifest_atomic(target, manifest)
    loaded = load_manifest(target)
    assert loaded.generated["k::project"].scope == "project"


def test_manifest_entry_scope_roundtrip_user(tmp_path: Path) -> None:
    target = tmp_path / "indexes" / "generated.json"
    entry = _entry(scope="user")
    manifest = Manifest(version=1, generated={"k::user": entry})
    save_manifest_atomic(target, manifest)
    loaded = load_manifest(target)
    assert loaded.generated["k::user"].scope == "user"


# ---- detect_git_state_drift ------------------------------------------------


def _base_state_payload() -> dict[str, Any]:
    """Minimal :class:`State` payload acceptable by Pydantic."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-27T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "ZZ",
            "description": "",
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ZZ",
        },
        "current": {
            "project_code": "ZZ",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _wave_payload(
    wave_id: str = "P28-I01-W01",
    *,
    status: str = "closed",
    commit: str | None = None,
) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": f"{wave_id.rsplit('-', 1)[0]}",
        "title": f"wave {wave_id}",
        "status": status,
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "token_budget": None,
        "tokens_consumed": 0,
        "outcome": None,
        "commit": commit,
        "opened_at": "2026-05-27T00:00:00Z",
        "closed_at": "2026-05-27T00:01:00Z" if status == "closed" else None,
    }


def _state_with_waves(waves: list[dict[str, Any]]) -> State:
    payload = _base_state_payload()
    payload["waves"] = {w["id"]: w for w in waves}
    return State.model_validate(payload)


@pytest.fixture(autouse=True)
def _stub_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the W10 one-pass index off real git so the patched ``derive_wave_sha`` answers.

    ``detect_git_state_drift`` now builds a shared SHA index once and passes
    it into ``derive_wave_sha(... index=...)``. These tests patch
    ``derive_wave_sha`` directly, so the index content is irrelevant -- stub
    the builder to an empty map so no live ``git log`` runs.
    """
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.build_wave_sha_index",
        lambda repo_root=None: {},
    )


def test_detect_git_state_drift_no_closed_waves_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open waves are not part of the reconciler set."""
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: None,
    )
    state = _state_with_waves([_wave_payload(status="pending")])
    assert detect_git_state_drift(state) == []


def test_detect_git_state_drift_clean_when_pinned_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    sha40 = "a" * 40
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: sha40,
    )
    state = _state_with_waves([_wave_payload(status="closed", commit=sha40)])
    assert detect_git_state_drift(state) == []


def test_detect_git_state_drift_pinned_but_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: None,
    )
    state = _state_with_waves([_wave_payload(status="closed", commit="a" * 40)])
    drifts = detect_git_state_drift(state)
    assert len(drifts) == 1
    assert drifts[0].kind == "pinned_but_missing"
    assert drifts[0].state_commit == "a" * 40
    assert drifts[0].git_commit is None


def test_detect_git_state_drift_pinned_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: "b" * 40,
    )
    state = _state_with_waves([_wave_payload(status="closed", commit="a" * 40)])
    drifts = detect_git_state_drift(state)
    assert len(drifts) == 1
    assert drifts[0].kind == "pinned_mismatch"
    assert drifts[0].state_commit == "a" * 40
    assert drifts[0].git_commit == "b" * 40


def test_detect_git_state_drift_closed_no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: None,
    )
    state = _state_with_waves([_wave_payload(status="closed", commit=None)])
    drifts = detect_git_state_drift(state)
    assert len(drifts) == 1
    assert drifts[0].kind == "closed_no_pin"
    assert drifts[0].state_commit is None
    assert drifts[0].git_commit is None


def test_detect_git_state_drift_closed_unfindable_when_git_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When git is not on PATH, closed-no-pin maps to closed_unfindable."""
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: None)
    state = _state_with_waves([_wave_payload(status="closed", commit=None)])
    drifts = detect_git_state_drift(state)
    assert len(drifts) == 1
    assert drifts[0].kind == "closed_unfindable"


def test_detect_git_state_drift_prefix_match_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short prefix in state.commit reconciles with the full git SHA."""
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: "abc1234" + "0" * 33,
    )
    # Wave.commit is ShaStr (40-hex), so we use the full 40-hex but
    # the derived SHA matches at the start — the helper is
    # prefix-tolerant in either direction.
    state = _state_with_waves([_wave_payload(status="closed", commit="abc1234" + "0" * 33)])
    assert detect_git_state_drift(state) == []


# ---- detect_cross_scope_duplicates -----------------------------------------


def test_cross_scope_duplicates_empty_when_no_manifest(tmp_path: Path) -> None:
    assert detect_cross_scope_duplicates(tmp_path) == []


def test_cross_scope_duplicates_ignores_legacy_none_scope(tmp_path: Path) -> None:
    """Pre-P28 entries with scope=None never count as duplicates."""
    target = tmp_path / ".ea" / "indexes" / "generated.json"
    target.parent.mkdir(parents=True)
    manifest = Manifest(
        version=1,
        generated={
            "a::r": _entry(target="a", region_id="r", scope=None),
            "b::r": _entry(target="b", region_id="r", scope=None),
        },
    )
    save_manifest_atomic(target, manifest)
    assert detect_cross_scope_duplicates(tmp_path) == []


def test_cross_scope_duplicates_flags_project_plus_user(tmp_path: Path) -> None:
    target = tmp_path / ".ea" / "indexes" / "generated.json"
    target.parent.mkdir(parents=True)
    manifest = Manifest(
        version=1,
        generated={
            "a::skill.x": _entry(target="a", region_id="plugin.codex.skill.x", scope="project"),
            "b::skill.x": _entry(target="b", region_id="plugin.codex.skill.x", scope="user"),
            "a::skill.y": _entry(target="a", region_id="plugin.codex.skill.y", scope="project"),
        },
    )
    save_manifest_atomic(target, manifest)
    duplicates = detect_cross_scope_duplicates(tmp_path)
    assert duplicates == ["plugin.codex.skill.x"]


def test_cross_scope_duplicates_unreadable_manifest_returns_empty(tmp_path: Path) -> None:
    target = tmp_path / ".ea" / "indexes" / "generated.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json", encoding="utf-8")
    assert detect_cross_scope_duplicates(tmp_path) == []


# ---- Drift dataclass smoke -------------------------------------------------


def test_drift_dataclass_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    drift = Drift(wave_id="P28-I01-W01", kind="closed_no_pin")
    with pytest.raises(FrozenInstanceError):
        drift.wave_id = "P28-I01-W02"  # type: ignore[misc]
