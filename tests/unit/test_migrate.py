"""Unit tests for the ``eawf migrate`` chain runner + canonical writer.

The live :class:`eawf.state.models.State` model is pinned at
``schema_version="1.0"``, so every test drives a **fixture** state dict
(not the live ``state.json``). The fixture exercises:

* the v1.0 -> v1.1 chain with per-step pre/post Pydantic invariants;
* the canonical-writer route (``portalock`` + ``atomic_write_json_locked``)
  with an assertion that the ``atomic_write_json`` bypass is never called;
* ``--dry-run`` writing nothing;
* a mid-chain failure restoring from the gitignored backup;
* the model-supported-max guard that refuses a target the live ``State``
  model cannot re-validate before any write.

The chain-machinery tests target ``1.1``/``1.2`` payloads (versions the
live model does not yet load), so they lift the guard ceiling via the
``lift_model_max`` fixture; the guard itself is covered by dedicated
tests that exercise the real (un-lifted) model-supported max.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eawf.migrations import (
    DEFAULT_REGISTRY,
    MigrationError,
    MigrationStepError,
    _base,
    build_migration_chain,
    guard_target_supported,
    model_supported_max_version,
    run_chain,
)
from eawf.migrations._base import backup_path_for
from eawf.migrations.v1_0_to_v1_1 import MigrationV10ToV11


@pytest.fixture
def lift_model_max(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Lift the model-supported-max ceiling so chain-machinery tests run.

    The live ``State`` model is pinned at ``"1.0"``; the chain-machinery
    tests write ``1.1``/``1.2`` fixture payloads to exercise the runner's
    write/backup/restore paths. Stubbing the max to ``"9.9"`` lets those
    tests reach :func:`run_chain`'s write path without the guard firing —
    the guard's own behaviour is covered separately against the real max.
    """
    monkeypatch.setattr(_base, "model_supported_max_version", lambda: "9.9")
    yield


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


def test_run_chain_migrates_fixture_state_v1_0_to_v1_1(
    tmp_path: Path, lift_model_max: None
) -> None:
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


def test_run_chain_writes_backup_adjacent_to_state(tmp_path: Path, lift_model_max: None) -> None:
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lift_model_max: None
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


def test_run_chain_dry_run_makes_no_write(tmp_path: Path, lift_model_max: None) -> None:
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


def test_run_chain_mid_chain_failure_restores_from_backup(
    tmp_path: Path, lift_model_max: None
) -> None:
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


# --- Model-supported-max guard ---------------------------------------------


def _minimal_state_v1_0() -> dict[str, Any]:
    """Return a full v1.0 state payload that re-loads under the live model.

    Mirrors the minimal-keys root in ``tests.unit.test_models`` so the
    guard tests can assert ``State.model_validate`` round-trips after a
    supported-target write.
    """
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant-research",
            "title": "Quant Research",
            "description": "",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "subproject_id": None,
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


class _IdentityStepV10:
    """A step whose ``run_chain`` target stays at the supported max ``1.0``.

    Keeps ``schema_version="1.0"`` while touching another field so a
    supported-target ``run_chain`` exercises the real write path and the
    result still re-loads under the live ``State`` model.
    """

    from_version = "1.0"
    to_version = "1.0"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        out = dict(state_dict)
        out["schema_version"] = "1.0"
        out["updated_at"] = "2026-05-09T00:00:00Z"
        return out

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        return None

    def check_post(self, state_dict: dict[str, Any]) -> None:
        return None


def test_model_supported_max_version_derives_from_live_model() -> None:
    """The supported max is read from the live ``State`` Literal, not hard-coded."""
    assert model_supported_max_version() == "1.0"


def test_guard_target_supported_allows_target_equal_to_max() -> None:
    """Boundary: a target equal to the model-supported max is accepted."""
    guard_target_supported(model_supported_max_version())


def test_guard_target_supported_rejects_target_above_max() -> None:
    with pytest.raises(MigrationError, match="exceeds model-supported max"):
        guard_target_supported("1.1")


def test_run_chain_refuses_unsupported_target_with_no_write(tmp_path: Path) -> None:
    """An unsupported target raises before any write or backup touches disk."""
    state_path = tmp_path / "state.json"
    _write_fixture(state_path)
    before = state_path.read_bytes()

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    with pytest.raises(MigrationError, match="exceeds model-supported max"):
        run_chain(state_path, chain=chain, from_version="1.0", to_version="1.1")

    # The on-disk state is byte-for-byte unchanged and no backup was taken.
    assert state_path.read_bytes() == before
    backup = backup_path_for(state_path, from_version="1.0", to_version="1.1")
    assert not backup.exists()


def test_run_chain_supported_target_writes_reloadable_state(tmp_path: Path) -> None:
    """A supported target (== max) writes a state the live model re-loads."""
    from eawf.state.models import State

    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")

    result = run_chain(
        state_path,
        chain=[_IdentityStepV10()],
        from_version="1.0",
        to_version="1.0",
    )

    assert result["schema_version"] == "1.0"
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    # The persisted state re-loads under the pinned live model — no brick.
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.0"
    assert on_disk["updated_at"] == "2026-05-09T00:00:00Z"


# --- CLI surface: guard wired into ``eawf migrate`` ------------------------


def test_migrate_cmd_unsupported_target_exits_nonzero_with_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``eawf migrate --to <unsupported>`` exits non-zero and writes nothing."""
    from typer.testing import CliRunner

    from eawf.cli.app import app

    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")
    before = state_path.read_bytes()

    monkeypatch.setenv("EA_STATE", str(state_path))
    result = CliRunner().invoke(app, ["migrate", "--to", "1.1"])

    assert result.exit_code != 0
    assert "exceeds model-supported max" in result.stdout
    # No write: the state file is byte-for-byte unchanged and re-loads.
    assert state_path.read_bytes() == before


def test_migrate_cmd_default_target_refused_until_model_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare ``eawf migrate`` default target (1.1) is refused, not bricking."""
    from typer.testing import CliRunner

    from eawf.cli.app import app

    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")
    before = state_path.read_bytes()

    monkeypatch.setenv("EA_STATE", str(state_path))
    result = CliRunner().invoke(app, ["migrate"])

    assert result.exit_code != 0
    assert state_path.read_bytes() == before


def test_migrate_cmd_supported_target_noop_keeps_state_reloadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: ``--to`` equal to the current version is a re-loadable no-op."""
    from typer.testing import CliRunner

    from eawf.cli.app import app
    from eawf.state.models import State

    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")

    monkeypatch.setenv("EA_STATE", str(state_path))
    result = CliRunner().invoke(app, ["migrate", "--to", "1.0"])

    assert result.exit_code == 0
    assert "no-op" in result.stdout
    # The state still re-loads under the live model after the supported-target run.
    reloaded = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert reloaded.schema_version == "1.0"
