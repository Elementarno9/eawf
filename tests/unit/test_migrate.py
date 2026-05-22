"""Unit tests for the ``eawf migrate`` chain runner + canonical writer.

The live :class:`eawf.state.models.State` model is pinned at
``schema_version="1.0"``, so every test drives a **fixture** state dict
(not the live ``state.json``). The fixture exercises:

* the v1.0 -> v1.1 chain with per-step pre/post Pydantic invariants;
* the canonical-writer route (``portalock`` + ``atomic_write_json_locked``)
  with an assertion that the ``atomic_write_json`` bypass is never called;
* ``--dry-run`` writing nothing;
* a mid-chain failure restoring from the gitignored backup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.migrations import (
    DEFAULT_REGISTRY,
    MigrationStepError,
    build_migration_chain,
    run_chain,
)
from eawf.migrations._base import backup_path_for
from eawf.migrations.v1_0_to_v1_1 import MigrationV10ToV11


def _fixture_state_v1_0() -> dict[str, Any]:
    """Return a minimal raw v1.0 state dict carrying a bare ``scope`` key.

    The lean invariant models read only ``schema_version`` (pre) and
    ``schema_version`` + ``principal_id`` (post); the extra keys ride
    through so the rename-``scope`` transform has something to act on.
    """
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "events": [{"scope": "P27-W22", "kind": "demo"}],
    }


def _write_fixture(path: Path) -> dict[str, Any]:
    """Write the v1.0 fixture to *path* and return the dict."""
    payload = _fixture_state_v1_0()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


# --- Chain construction ----------------------------------------------------


def test_build_migration_chain_v1_0_to_v1_1_single_step() -> None:
    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    assert len(chain) == 1
    assert chain[0].from_version == "1.0"
    assert chain[0].to_version == "1.1"


def test_build_migration_chain_same_version_is_empty() -> None:
    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.1", to_version="1.1")
    assert chain == []


def test_build_migration_chain_unknown_target_raises() -> None:
    from eawf.migrations import MigrationError

    with pytest.raises(MigrationError, match="no migration from version"):
        build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="9.9")


# --- Step transform + invariants -------------------------------------------


def test_v1_0_to_v1_1_apply_bumps_version_and_adds_principal() -> None:
    step = MigrationV10ToV11()
    out = step.apply(_fixture_state_v1_0())
    assert out["schema_version"] == "1.1"
    assert out["principal_id"] == "operator:local"


def test_v1_0_to_v1_1_apply_renames_scope_to_scope_id() -> None:
    step = MigrationV10ToV11()
    out = step.apply(_fixture_state_v1_0())
    event = out["events"][0]
    assert "scope" not in event
    assert event["scope_id"] == "P27-W22"


def test_v1_0_to_v1_1_apply_does_not_mutate_input() -> None:
    step = MigrationV10ToV11()
    src = _fixture_state_v1_0()
    step.apply(src)
    assert src["schema_version"] == "1.0"
    assert "principal_id" not in src


def test_v1_0_to_v1_1_check_pre_rejects_wrong_version() -> None:
    step = MigrationV10ToV11()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_pre({"schema_version": "1.1"})


def test_v1_0_to_v1_1_check_post_rejects_missing_principal() -> None:
    step = MigrationV10ToV11()
    with pytest.raises(Exception):  # noqa: B017 — missing principal_id
        step.check_post({"schema_version": "1.1"})


# --- Fixture migration: end-to-end run_chain -------------------------------


def test_run_chain_migrates_fixture_state_v1_0_to_v1_1(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_fixture(state_path)

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    result = run_chain(
        state_path,
        chain=chain,
        from_version="1.0",
        to_version="1.1",
    )

    assert result["schema_version"] == "1.1"
    # The on-disk file was actually rewritten through the canonical writer.
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == "1.1"
    assert on_disk["principal_id"] == "operator:local"


def test_run_chain_writes_backup_adjacent_to_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_fixture(state_path)

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.1")

    backup = backup_path_for(state_path, from_version="1.0", to_version="1.1")
    assert backup.name == "state.json.bak.v1.0.v1.1"
    assert backup.exists()
    # The backup holds the pre-migration payload.
    assert json.loads(backup.read_text(encoding="utf-8"))["schema_version"] == "1.0"


# --- Canonical-writer route assertion --------------------------------------


def test_run_chain_routes_write_through_canonical_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write MUST go through portalock + atomic_write_json_locked.

    Asserts the daemon canonical-writer primitive
    (``atomic_write_json_locked``, acquired under ``portalock``) is
    exercised and the lock-acquiring ``atomic_write_json`` bypass is
    never called (AGENTS rule 4 / D-SUP-01).
    """
    from eawf.lock import portalock
    from eawf.migrations import _base
    from eawf.state import writer

    state_path = tmp_path / "state.json"
    _write_fixture(state_path)

    locked_calls: list[Path] = []
    acquire_calls: list[Path] = []
    bypass_calls: list[Path] = []

    real_locked = writer.atomic_write_json_locked
    real_acquire = portalock.acquire

    def spy_locked(target: Path, data: Any) -> None:
        locked_calls.append(Path(target))
        real_locked(target, data)

    def spy_acquire(target: Path, **kwargs: Any) -> Any:
        acquire_calls.append(Path(target))
        return real_acquire(target, **kwargs)

    def spy_bypass(target: Path, data: Any) -> None:
        bypass_calls.append(Path(target))

    # Patch the names the migration module resolved at import time.
    monkeypatch.setattr(_base, "atomic_write_json_locked", spy_locked)
    monkeypatch.setattr(_base.portalock, "acquire", spy_acquire)
    monkeypatch.setattr(writer, "atomic_write_json", spy_bypass)

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.1")

    assert locked_calls == [state_path], "canonical atomic_write_json_locked not exercised"
    assert state_path in acquire_calls, "portalock.acquire not held during the write"
    assert bypass_calls == [], "migration must not call the atomic_write_json bypass"


# --- Dry-run: no write -----------------------------------------------------


def test_run_chain_dry_run_makes_no_write(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    original = _write_fixture(state_path)
    original_bytes = state_path.read_bytes()

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    result = run_chain(
        state_path,
        chain=chain,
        from_version="1.0",
        to_version="1.1",
        dry_run=True,
    )

    # The computed result reflects the migration...
    assert result["schema_version"] == "1.1"
    # ...but the on-disk file is byte-for-byte unchanged and no backup ran.
    assert state_path.read_bytes() == original_bytes
    assert json.loads(state_path.read_text())["schema_version"] == original["schema_version"]
    backup = backup_path_for(state_path, from_version="1.0", to_version="1.1")
    assert not backup.exists()


# --- Mid-chain failure: restore from backup --------------------------------


class _BoomStep:
    """A second migration step that always fails its apply (mid-chain)."""

    from_version = "1.1"
    to_version = "1.2"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom in v1.1->v1.2")

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        return None

    def check_post(self, state_dict: dict[str, Any]) -> None:
        return None


def test_run_chain_mid_chain_failure_restores_from_backup(tmp_path: Path) -> None:
    """A failing step restores the pre-migration state from the backup."""
    state_path = tmp_path / "state.json"
    _write_fixture(state_path)

    chain = [MigrationV10ToV11(), _BoomStep()]
    with pytest.raises(MigrationStepError, match=r"1\.1->1\.2 failed at apply"):
        run_chain(state_path, chain=chain, from_version="1.0", to_version="1.2")

    # The on-disk state was restored to the pre-migration v1.0 payload.
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == "1.0"
    assert "principal_id" not in on_disk
    # The backup file remains for forensic recovery.
    backup = backup_path_for(state_path, from_version="1.0", to_version="1.2")
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))["schema_version"] == "1.0"
