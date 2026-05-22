"""Unit tests for the ``eawf migrate`` chain runner + canonical writer.

The first migration edge (v1.0 -> v1.1) tightens every entity ``title``
to ``max_length=72`` (copying the full over-cap title into the new
``description`` first) and renames ``Decision.summary`` /
``Hypothesis.text`` to ``title``. The live
:class:`eawf.state.models.State` model accepts both ``"1.0"`` and
``"1.1"``, so a migrated state re-loads under the live model. The suite
exercises:

* the v1.0 -> v1.1 chain with per-step pre/post Pydantic invariants;
* a full v1.0 state migrating to a re-loadable v1.1 state;
* idempotency — re-running against an already-1.1 state is a no-op;
* an over-cap-title v1.0 state migrating to a capped, re-loadable v1.1
  state with the full original title preserved in ``description``;
* the canonical-writer route (``portalock`` + ``atomic_write_json_locked``)
  with an assertion that the ``atomic_write_json`` bypass is never called;
* ``--dry-run`` writing nothing;
* a mid-chain failure restoring from the gitignored backup;
* the model-supported-max guard that now permits ``1.1`` (the model
  advanced) but still refuses a target the live model cannot re-validate.

The mid-chain-failure test targets a ``1.2`` payload (a version the live
model does not yet load), so it lifts the guard ceiling via the
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
from eawf.state.models import State


@pytest.fixture
def lift_model_max(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Lift the model-supported-max ceiling so chain-machinery tests run.

    The live ``State`` model accepts up to ``"1.1"``; the mid-chain
    failure test writes a ``1.2`` fixture payload to exercise the runner's
    backup/restore path. Stubbing the max to ``"9.9"`` lets that test
    reach :func:`run_chain`'s write path without the guard firing — the
    guard's own behaviour is covered separately against the real max.
    """
    monkeypatch.setattr(_base, "model_supported_max_version", lambda: "9.9")
    yield


def _fixture_state_v1_0() -> dict[str, Any]:
    """Return a minimal raw v1.0 state dict with assorted pass-through keys.

    The lean invariant models read only ``schema_version``; the extra keys
    ride through unchanged so the identity-transform assertions have a body
    to verify is left untouched.
    """
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "events": [{"scope_id": "P27-W22", "kind": "demo"}],
    }


def _minimal_state_v1_0() -> dict[str, Any]:
    """Return a full v1.0 state payload that re-loads under the live model.

    Mirrors the minimal-keys root in ``tests.unit.test_models`` so the
    migration + guard tests can assert ``State.model_validate`` round-trips
    after a supported-target write.
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


def _state_v1_0_with_entities() -> dict[str, Any]:
    """Return a full v1.0 state carrying a wave, a decision, and a hypothesis.

    The decision row carries the pre-rename ``summary`` key and the
    hypothesis row the pre-rename ``text`` key so the migration's rename +
    title-cap transform has a body to exercise. The phase/iter/wave chain
    is referentially complete so the migrated state re-loads under the
    live model.
    """
    ts = "2026-05-08T00:00:00Z"
    payload = _minimal_state_v1_0()
    payload["phases"] = {
        "P00": {
            "id": "P00",
            "scope_id": "QR",
            "subproject_id": None,
            "title": "Phase zero",
            "status": "active",
            "iter_ids": ["P00-I01"],
            "outcome_ids": [],
            "depends_on": [],
            "source_brief_ids": [],
            "opened_at": ts,
            "closed_at": None,
            "audit_id": None,
        }
    }
    payload["iters"] = {
        "P00-I01": {
            "id": "P00-I01",
            "phase_id": "P00",
            "title": "Iter one",
            "status": "active",
            "wave_ids": ["P00-I01-W01"],
            "estimate_id": None,
            "audit_id": None,
            "opened_at": ts,
            "closed_at": None,
        }
    }
    payload["waves"] = {
        "P00-I01-W01": {
            "id": "P00-I01-W01",
            "iter_id": "P00-I01",
            "title": "Wave one",
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "opened_at": ts,
            "closed_at": None,
        }
    }
    payload["decisions"] = {
        "D01": {
            "id": "D01",
            "scope_id": "QR",
            "summary": "Pick rebase merges",
            "rationale": "Squash destroys the wave-prefix history.",
            "alternatives": [],
            "consequences": [],
            "status": "active",
            "created_at": ts,
            "superseded_by": None,
        }
    }
    payload["hypotheses"] = {
        "H01-01": {
            "id": "H01-01",
            "scope_id": "QR",
            "text": "Render is idempotent",
            "metric": "drift",
            "confirm": "drift == 0",
            "reject": "drift > 0",
            "status": "pending",
            "verdict": None,
            "audit_id": None,
            "source_artifact_id": None,
        }
    }
    return payload


def _write_fixture(path: Path) -> dict[str, Any]:
    """Write the minimal raw v1.0 fixture to *path* and return the dict."""
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
    with pytest.raises(MigrationError, match="no migration from version"):
        build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="9.9")


# --- Step transform + invariants -------------------------------------------


def test_v1_0_to_v1_1_apply_bumps_version() -> None:
    step = MigrationV10ToV11()
    out = step.apply(_fixture_state_v1_0())
    assert out["schema_version"] == "1.1"


def test_v1_0_to_v1_1_apply_is_identity_apart_from_version() -> None:
    """The transform touches only ``schema_version`` — every other key rides through."""
    step = MigrationV10ToV11()
    src = _fixture_state_v1_0()
    out = step.apply(src)
    expected = dict(src)
    expected["schema_version"] = "1.1"
    assert out == expected


def test_v1_0_to_v1_1_apply_does_not_mutate_input() -> None:
    step = MigrationV10ToV11()
    src = _fixture_state_v1_0()
    step.apply(src)
    assert src["schema_version"] == "1.0"


def test_v1_0_to_v1_1_apply_deep_copies_nested_nodes() -> None:
    """The deep copy means mutating the output never bleeds into the input."""
    step = MigrationV10ToV11()
    src = _fixture_state_v1_0()
    out = step.apply(src)
    out["events"][0]["kind"] = "tampered"
    assert src["events"][0]["kind"] == "demo"


def test_v1_0_to_v1_1_apply_renames_decision_summary_to_title() -> None:
    step = MigrationV10ToV11()
    out = step.apply(_state_v1_0_with_entities())
    row = out["decisions"]["D01"]
    assert "summary" not in row
    assert row["title"] == "Pick rebase merges"


def test_v1_0_to_v1_1_apply_renames_hypothesis_text_to_title() -> None:
    step = MigrationV10ToV11()
    out = step.apply(_state_v1_0_with_entities())
    row = out["hypotheses"]["H01-01"]
    assert "text" not in row
    assert row["title"] == "Render is idempotent"


def test_v1_0_to_v1_1_apply_caps_over_72_title_into_description() -> None:
    step = MigrationV10ToV11()
    src = _state_v1_0_with_entities()
    long_title = "Wave " + "x" * 174  # 179 chars
    src["waves"]["P00-I01-W01"]["title"] = long_title
    out = step.apply(src)
    row = out["waves"]["P00-I01-W01"]
    assert len(row["title"]) <= 72
    # The full original title is preserved with no loss.
    assert row["description"] == long_title


def test_v1_0_to_v1_1_apply_truncates_at_word_boundary() -> None:
    """The truncated title cuts on a whole-word boundary at or before 72."""
    step = MigrationV10ToV11()
    src = _state_v1_0_with_entities()
    # 8-char words separated by spaces; the boundary at/<=72 lands cleanly.
    words = " ".join(["abcdefgh"] * 12)  # 12*8 + 11 spaces = 107 chars
    src["waves"]["P00-I01-W01"]["title"] = words
    out = step.apply(src)
    title = out["waves"]["P00-I01-W01"]["title"]
    assert len(title) <= 72
    # No partial trailing word — the cut lands on a space boundary.
    assert not title.endswith("abcdefg")
    assert title.split() == ["abcdefgh"] * len(title.split())


def test_v1_0_to_v1_1_apply_leaves_at_cap_title_untouched() -> None:
    """Boundary: a title of exactly 72 chars is neither capped nor copied."""
    step = MigrationV10ToV11()
    src = _state_v1_0_with_entities()
    exact = "y" * 72
    src["waves"]["P00-I01-W01"]["title"] = exact
    out = step.apply(src)
    row = out["waves"]["P00-I01-W01"]
    assert row["title"] == exact
    assert row.get("description") is None


def test_v1_0_to_v1_1_apply_minimal_state_is_identity_apart_from_version() -> None:
    """A state with no over-cap titles + no renamed fields is pure version bump."""
    step = MigrationV10ToV11()
    src = _minimal_state_v1_0()
    out = step.apply(src)
    expected = dict(src)
    expected["schema_version"] = "1.1"
    assert out == expected


def test_v1_0_to_v1_1_check_pre_rejects_wrong_version() -> None:
    step = MigrationV10ToV11()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_pre({"schema_version": "1.1"})


def test_v1_0_to_v1_1_check_post_rejects_unbumped_version() -> None:
    step = MigrationV10ToV11()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_post({"schema_version": "1.0"})


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


def test_run_chain_migrates_full_state_to_reloadable_v1_1(tmp_path: Path) -> None:
    """A full v1.0 state migrates to a v1.1 state that the live model re-loads."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.1")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.1"


def test_run_chain_migrate_is_idempotent_on_already_v1_1(tmp_path: Path) -> None:
    """Migrating an already-1.1 state is an empty-chain no-op — content unchanged.

    An empty chain (source == target) applies no step, so the persisted
    dict is value-identical to the input: re-running the migrate on an
    already-1.1 state neither errors nor mutates the state.
    """
    state_path = tmp_path / "state.json"
    payload = _minimal_state_v1_0()
    payload["schema_version"] = "1.1"
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.1", to_version="1.1")
    assert chain == []
    result = run_chain(state_path, chain=chain, from_version="1.1", to_version="1.1")

    assert result["schema_version"] == "1.1"
    # The persisted dict is value-identical to the input — a true no-op.
    assert json.loads(state_path.read_text(encoding="utf-8")) == payload
    # It still re-loads under the live model.
    reloaded = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert reloaded.schema_version == "1.1"


def test_run_chain_over_cap_title_state_caps_and_reloads(tmp_path: Path) -> None:
    """Boundary: a v1.0 state with a 179-char wave title caps + re-loads.

    The full original title is preserved in ``description`` and the
    on-disk ``title`` is truncated to <= 72 so the migrated state passes
    the tightened model. The decision ``summary`` and hypothesis ``text``
    rename to ``title`` in the same step.
    """
    state_path = tmp_path / "state.json"
    payload = _state_v1_0_with_entities()
    long_title = "Wave " + "x" * 174  # 179 chars
    assert len(long_title) == 179
    payload["waves"]["P00-I01-W01"]["title"] = long_title
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.1")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.1"

    wave = reloaded.waves["P00-I01-W01"]
    assert len(wave.title) <= 72
    # The complete original title is preserved with no loss.
    assert wave.description == long_title

    # The decision/hypothesis field renames carried their value into title.
    assert reloaded.decisions["D01"].title == "Pick rebase merges"
    assert reloaded.hypotheses is not None
    assert reloaded.hypotheses["H01-01"].title == "Render is idempotent"


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
    # The backup file remains for forensic recovery.
    backup = backup_path_for(state_path, from_version="1.0", to_version="1.2")
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))["schema_version"] == "1.0"


# --- Model-supported-max guard ---------------------------------------------


class _IdentityStepV10:
    """A step whose ``run_chain`` target stays at ``1.0`` (a supported version).

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
    assert model_supported_max_version() == "1.1"


def test_guard_target_supported_allows_target_equal_to_max() -> None:
    """Boundary: a target equal to the model-supported max is accepted."""
    guard_target_supported(model_supported_max_version())


def test_guard_target_supported_permits_v1_1_now_model_advanced() -> None:
    """The guard permits 1.1 now the live model accepts it (W22 advanced the Literal)."""
    guard_target_supported("1.1")


def test_guard_target_supported_rejects_target_above_max() -> None:
    with pytest.raises(MigrationError, match="exceeds model-supported max"):
        guard_target_supported("1.2")


def test_run_chain_refuses_unsupported_target_with_no_write(tmp_path: Path) -> None:
    """An unsupported target (>max) raises before any write or backup touches disk."""
    state_path = tmp_path / "state.json"
    _write_fixture(state_path)
    before = state_path.read_bytes()

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    with pytest.raises(MigrationError, match="exceeds model-supported max"):
        run_chain(state_path, chain=chain, from_version="1.0", to_version="1.2")

    # The on-disk state is byte-for-byte unchanged and no backup was taken.
    assert state_path.read_bytes() == before
    backup = backup_path_for(state_path, from_version="1.0", to_version="1.2")
    assert not backup.exists()


def test_run_chain_supported_target_writes_reloadable_state(tmp_path: Path) -> None:
    """A supported target (== 1.0) writes a state the live model re-loads."""
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
    # The persisted state re-loads under the live model — no brick.
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
    result = CliRunner().invoke(app, ["migrate", "--to", "1.2"])

    assert result.exit_code != 0
    assert "exceeds model-supported max" in result.stdout
    # No write: the state file is byte-for-byte unchanged.
    assert state_path.read_bytes() == before


def test_migrate_cmd_default_target_migrates_v1_0_to_v1_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare ``eawf migrate`` default target (1.1) now succeeds + re-loads.

    Previously refused (the model was pinned at 1.0); W22 advanced the
    Literal so the default target lands a re-loadable v1.1 state.
    """
    from typer.testing import CliRunner

    from eawf.cli.app import app

    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")

    monkeypatch.setenv("EA_STATE", str(state_path))
    result = CliRunner().invoke(app, ["migrate"])

    assert result.exit_code == 0, result.output
    reloaded = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert reloaded.schema_version == "1.1"


def test_migrate_cmd_supported_target_noop_keeps_state_reloadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: ``--to`` equal to the current version is a re-loadable no-op."""
    from typer.testing import CliRunner

    from eawf.cli.app import app

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
