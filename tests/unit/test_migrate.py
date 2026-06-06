"""Unit tests for the ``eawf migrate`` chain runner + canonical writer.

The first migration edge (v1.0 -> v1.1) tightens every entity ``title``
to ``max_length=72`` (copying the full over-cap title into the new
``description`` first) and renames ``Decision.summary`` /
``Hypothesis.text`` to ``title``. The second edge (v1.1 -> v1.2) adds
``Iter.trigger`` and backfills every historical iter to ``"none"`` so it
drops out of the corrected planned-vs-reactive denominator. The third
edge (v1.2 -> v1.3) adds ``Wave.claimed_at`` and backfills each wave's
work-start timestamp from its ``wave_claimed`` event in the sibling event
store. The fourth edge (v1.3 -> v1.4) adds the optional
``Iter.candidate_tag`` release tag and backfills every iter to ``None``
(additive, replay-safe). The fifth edge (v1.4 -> v1.5) registers the
``ArtifactKind.MATH_EXPLAINER`` artifact kind — a purely additive enum value
no state row references — so the transform is a bare ``schema_version`` bump
with no field backfill. The sixth edge (v1.5 -> v1.6) adds the top-level
``State.dispatch_paused`` flag (the cooperative dispatch-gate marker) — a
purely additive field the model defaults to ``False`` on load, so the
transform is again a bare ``schema_version`` bump with no backfill. The
seventh edge (v1.6 -> v1.7) retypes ``Wave.success_criteria`` from
``list[str]`` to ``list[CriterionSpec]`` and backfills every legacy string
into a grandfathered ``CriterionSpec`` row — a real per-wave backfill, not a
bare version bump. The eighth edge (v1.7 -> v1.8) adds the typed
``Wave.gates`` list (the per-wave ``GateSpec`` close-gate rows) and backfills
an explicit ``gates: []`` on every wave — additive, replay-safe. The live
:class:`eawf.kernel.state.models.State` model accepts ``"1.0"``, ``"1.1"``,
``"1.2"``, ``"1.3"``, ``"1.4"``, ``"1.5"``, ``"1.6"``, ``"1.7"``, and
``"1.8"``, so a migrated state re-loads under the live model. The suite
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
* an empty / whitespace-only v1.0 title migrating to a placeholder so the
  v1.1 ``min_length=1`` floor holds and the state re-loads;
* a model-invalid *final* payload (lean ``check_post`` passes but the full
  ``State`` model rejects it) restoring the backup + raising a ``post``
  step error (MIG-F6);
* the model-supported-max guard that now permits ``1.1`` (the model
  advanced) but still refuses a target the live model cannot re-validate.

The mid-chain-failure test targets a ``1.2`` payload (a version the live
model does not yet load), so it lifts the guard ceiling via the
``lift_model_max`` fixture; the guard itself is covered by dedicated
tests that exercise the real (un-lifted) model-supported max.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migrations import (
    DEFAULT_REGISTRY,
    MigrationError,
    MigrationStepError,
    _base,
    build_migration_chain,
    guard_target_supported,
    model_supported_max_version,
    run_chain,
)
from eawf.kernel.migrations._base import backup_path_for
from eawf.kernel.migrations.v1_0_to_v1_1 import (
    _DESCRIPTION_MAX,
    _TITLE_MAX,
    MigrationV10ToV11,
    _truncate_title,
)
from eawf.kernel.migrations.v1_1_to_v1_2 import MigrationV11ToV12
from eawf.kernel.migrations.v1_2_to_v1_3 import MigrationV12ToV13, read_claim_anchors
from eawf.kernel.migrations.v1_3_to_v1_4 import MigrationV13ToV14
from eawf.kernel.migrations.v1_4_to_v1_5 import MigrationV14ToV15
from eawf.kernel.migrations.v1_5_to_v1_6 import MigrationV15ToV16
from eawf.kernel.state.enums import IterTrigger, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path


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
    """Write the minimal raw v1.0 fixture to *path* and return the dict.

    The lean fixture carries only ``schema_version`` + a couple of
    pass-through keys; it does NOT load against the full ``State`` model,
    so it is reserved for tests that exercise ``step.apply`` directly or a
    ``dry_run`` (no write-path round-trip). Write-path tests use
    :func:`_write_full_fixture` so the final-payload round-trip (MIG-F6)
    passes.
    """
    payload = _fixture_state_v1_0()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _write_full_fixture(path: Path) -> dict[str, Any]:
    """Write a full, model-valid raw v1.0 state to *path* and return the dict.

    Used by write-path machinery tests so the final-payload round-trip
    (:func:`eawf.kernel.migrations._base.run_chain` MIG-F6) loads the migrated
    candidate against the live ``State`` model without faulting.
    """
    payload = _minimal_state_v1_0()
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
    _write_full_fixture(state_path)

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
    _write_full_fixture(state_path)

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
    from eawf.kernel.migrations import _base
    from eawf.kernel.state import writer
    from eawf.runtime.lock import portalock

    state_path = tmp_path / "state.json"
    _write_full_fixture(state_path)

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


# --- Final-payload model round-trip (MIG-F6) -------------------------------


class _ModelInvalidStep:
    """A step that bumps the version but emits a model-invalid final payload.

    The lean ``check_post`` reads only ``schema_version`` so it passes,
    but the body carries an over-cap wave ``title`` the full ``State``
    model rejects — exercising the runner's final-payload round-trip.
    """

    from_version = "1.0"
    to_version = "1.1"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(state_dict)
        out["schema_version"] = "1.1"
        # A 100-char title violates the v1.1 ``max_length=72`` floor; the
        # lean ``check_post`` (schema_version only) cannot catch it.
        out["waves"]["P00-I01-W01"]["title"] = "x" * 100
        return out

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        return None

    def check_post(self, state_dict: dict[str, Any]) -> None:
        return None


def test_run_chain_model_invalid_final_payload_restores_and_raises_post(
    tmp_path: Path,
) -> None:
    """A model-invalid final payload restores the backup + raises a post error.

    The per-step ``check_post`` passes (it reads only ``schema_version``),
    so the bricking payload would land without the final round-trip. The
    runner loads the candidate against the live ``State`` model, restores
    the pre-migration v1.0 state from the backup, and re-raises as a
    ``post``-phase :class:`MigrationStepError`.
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state_v1_0_with_entities(), indent=2), encoding="utf-8")

    with pytest.raises(
        MigrationStepError, match="does not load against the live State model"
    ) as ei:
        run_chain(
            state_path,
            chain=[_ModelInvalidStep()],
            from_version="1.0",
            to_version="1.1",
        )
    assert ei.value.phase == "post"
    assert ei.value.from_version == "1.0"
    assert ei.value.to_version == "1.1"

    # The on-disk state was restored to the pre-migration v1.0 payload.
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == "1.0"
    # The pre-migration wave title survived the restore (no bricking write).
    assert on_disk["waves"]["P00-I01-W01"]["title"] == "Wave one"
    # The backup file remains for forensic recovery.
    backup = backup_path_for(state_path, from_version="1.0", to_version="1.1")
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))["schema_version"] == "1.0"


def test_run_chain_model_invalid_final_payload_dry_run_skips_round_trip(
    tmp_path: Path,
) -> None:
    """``--dry-run`` returns the candidate without the model round-trip firing.

    The round-trip guards the write path; a dry run takes no backup and
    writes nothing, so it must not raise on a model-invalid candidate — it
    just reports what *would* change.
    """
    state_path = tmp_path / "state.json"
    original = json.dumps(_state_v1_0_with_entities(), indent=2)
    state_path.write_text(original, encoding="utf-8")

    result = run_chain(
        state_path,
        chain=[_ModelInvalidStep()],
        from_version="1.0",
        to_version="1.1",
        dry_run=True,
    )

    assert result["schema_version"] == "1.1"
    # On-disk state is untouched and no backup ran.
    assert state_path.read_text(encoding="utf-8") == original
    backup = backup_path_for(state_path, from_version="1.0", to_version="1.1")
    assert not backup.exists()


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
    assert model_supported_max_version() == "1.8"


def test_guard_target_supported_allows_target_equal_to_max() -> None:
    """Boundary: a target equal to the model-supported max is accepted."""
    guard_target_supported(model_supported_max_version())


def test_guard_target_supported_permits_v1_1_now_model_advanced() -> None:
    """The guard permits 1.1 now the live model accepts it (W22 advanced the Literal)."""
    guard_target_supported("1.1")


def test_guard_target_supported_permits_v1_2_now_model_advanced() -> None:
    """The guard permits 1.2 now the live model accepts it (the trigger-field bump)."""
    guard_target_supported("1.2")


def test_guard_target_supported_permits_v1_3_now_model_advanced() -> None:
    """The guard permits 1.3 now the live model accepts it (the claimed_at bump)."""
    guard_target_supported("1.3")


def test_guard_target_supported_permits_v1_4_now_model_advanced() -> None:
    """The guard permits 1.4 now the live model accepts it (the candidate_tag bump)."""
    guard_target_supported("1.4")


def test_guard_target_supported_permits_v1_5_now_model_advanced() -> None:
    """The guard permits 1.5 now the live model accepts it (the MATH_EXPLAINER bump)."""
    guard_target_supported("1.5")


def test_guard_target_supported_permits_v1_6_now_model_advanced() -> None:
    """The guard permits 1.6 now the live model accepts it (the dispatch_paused bump)."""
    guard_target_supported("1.6")


def test_guard_target_supported_permits_v1_7_now_model_advanced() -> None:
    """The guard permits 1.7 now the live model accepts it (the typed-criteria bump)."""
    guard_target_supported("1.7")


def test_guard_target_supported_permits_v1_8_now_model_advanced() -> None:
    """The guard permits 1.8 now the live model accepts it (the Wave.gates bump)."""
    guard_target_supported("1.8")


def test_guard_target_supported_rejects_target_above_max() -> None:
    with pytest.raises(MigrationError, match="exceeds model-supported max"):
        guard_target_supported("1.9")


def test_run_chain_refuses_unsupported_target_with_no_write(tmp_path: Path) -> None:
    """An unsupported target (>max) raises before any write or backup touches disk."""
    state_path = tmp_path / "state.json"
    _write_fixture(state_path)
    before = state_path.read_bytes()

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    with pytest.raises(MigrationError, match="exceeds model-supported max"):
        run_chain(state_path, chain=chain, from_version="1.0", to_version="1.9")

    # The on-disk state is byte-for-byte unchanged and no backup was taken.
    assert state_path.read_bytes() == before
    backup = backup_path_for(state_path, from_version="1.0", to_version="1.9")
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

    from eawf.surfaces.cli.app import app

    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")
    before = state_path.read_bytes()

    monkeypatch.setenv("EA_STATE", str(state_path))
    result = CliRunner().invoke(app, ["migrate", "--to", "1.9"])

    assert result.exit_code != 0
    assert "exceeds model-supported max" in result.stdout
    # No write: the state file is byte-for-byte unchanged.
    assert state_path.read_bytes() == before


def test_migrate_cmd_default_target_migrates_v1_0_to_v1_8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare ``eawf migrate`` default target (1.8) walks the full chain + re-loads.

    The default target advanced to 1.8 with the Wave.gates bump, so a bare
    migrate on a v1.0 state runs 1.0 -> 1.1 -> 1.2 -> 1.3 -> 1.4 -> 1.5 -> 1.6
    -> 1.7 -> 1.8 and lands a re-loadable v1.8 state.
    """
    from typer.testing import CliRunner

    from eawf.surfaces.cli.app import app

    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")

    monkeypatch.setenv("EA_STATE", str(state_path))
    result = CliRunner().invoke(app, ["migrate"])

    assert result.exit_code == 0, result.output
    reloaded = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert reloaded.schema_version == "1.8"


def test_migrate_cmd_supported_target_noop_keeps_state_reloadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: ``--to`` equal to the current version is a re-loadable no-op."""
    from typer.testing import CliRunner

    from eawf.surfaces.cli.app import app

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


# --- W24: clause-boundary title truncation ---------------------------------
#
# A clause boundary is the latest of ``. ``, ``; ``, ``: ``, ``, ``,
# `` — ``, `` -- `` that fits the 72-char budget; the separator is dropped
# so the kept title ends on a whole clause. The fixture below is a
# synthetic-but-representative v1.0 state whose long titles mirror real
# eawf wave / backlog / phase wording (multi-clause, 100-400 chars) without
# copying any developer-local path or PII into a committed fixture.


#: Synthetic-but-representative over-cap titles, keyed by the natural break
#: each should land on. Every string is multi-clause and 100-400 chars,
#: shaped after real eawf wave / backlog / phase titles.
_REALISTIC_TITLES: dict[str, str] = {
    "colon": (
        "Hygiene preamble: state-writer EOL + version-coupling lint + "
        "phase prepare-close mutator + iter-bump hint for the active iter"
    ),
    "semicolon": (
        "Auto-truncate migrator for over-cap titles; copy the full string "
        "into description, truncate the title to a clause boundary, prove "
        "no content is lost on a realistic state fixture"
    ),
    "comma": (
        "Refactor the renderer envelope header so scope_id, wave, iter, and "
        "phase keys stay canonical across every typed payload, log line, and "
        "rendered breadcrumb in the dispatch surface"
    ),
    "double_dash": (
        "Daemon canonical mutator -- state.json, layered config YAML, "
        "registry JSON, event and audit stores, and the telemetry DB all "
        "flow through the eawfd JSON-RPC write path"
    ),
    "period": (
        "Plan the next phase. Enumerate the waves, write the per-wave "
        "success criteria, and dispatch independent waves in parallel via "
        "worktree-isolated subagents that cherry-pick back"
    ),
}


def _realistic_state_v1_0() -> dict[str, Any]:
    """Return a full v1.0 state whose entity titles mirror real eawf wording.

    Extends :func:`_minimal_state_v1_0` with a referentially complete
    phase/iter/wave chain plus a backlog item, decision, hypothesis, and
    incident. Several rows carry multi-clause over-cap titles (100-400
    chars); one wave keeps a short in-cap title so the "untouched" path is
    exercised in the same migration. The chain re-loads under the live
    model after migration.
    """
    ts = "2026-05-08T00:00:00Z"
    payload = _minimal_state_v1_0()
    payload["phases"] = {
        "P00": {
            "id": "P00",
            "scope_id": "QR",
            "subproject_id": None,
            "title": _REALISTIC_TITLES["period"],
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
            "title": "Iter one",  # already in-cap: stays untouched
            "status": "active",
            "wave_ids": ["P00-I01-W01", "P00-I01-W02"],
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
            "title": _REALISTIC_TITLES["colon"],
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "opened_at": ts,
            "closed_at": None,
        },
        "P00-I01-W02": {
            "id": "P00-I01-W02",
            "iter_id": "P00-I01",
            "title": "Wave two",  # already in-cap: stays untouched
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "opened_at": ts,
            "closed_at": None,
        },
    }
    payload["backlog"] = {
        "B01": {
            "id": "B01",
            "scope_id": "QR",
            "title": _REALISTIC_TITLES["double_dash"],
            "priority": "P1",
            "status": "open",
            "created_at": ts,
        }
    }
    payload["incidents"] = {
        "INC01": {
            "id": "INC01",
            "scope_id": "QR",
            "severity": "medium",
            "title": _REALISTIC_TITLES["comma"],
            "status": "open",
            "opened_at": ts,
        }
    }
    payload["decisions"] = {
        "D01": {
            "id": "D01",
            "scope_id": "QR",
            "summary": _REALISTIC_TITLES["semicolon"],
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
            "text": "Render is idempotent",  # already in-cap: stays untouched
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


@pytest.mark.parametrize("key", sorted(_REALISTIC_TITLES))
def test_truncate_title_breaks_on_clause_boundary(key: str) -> None:
    """Each realistic over-cap title truncates on its natural clause break.

    The kept title must fit the budget, must not be empty, and must not end
    on a clause separator or a trailing space (the separator is dropped).
    """
    original = _REALISTIC_TITLES[key]
    assert len(original) > _TITLE_MAX  # the fixture is genuinely over-cap
    out = _truncate_title(original)
    assert 0 < len(out) <= _TITLE_MAX
    assert out == out.rstrip()
    assert not out.endswith((",", ";", ":", ".", "-", "—"))
    # The kept title is a clause-aligned prefix of the original.
    assert original.startswith(out)


def test_truncate_title_prefers_clause_over_word_boundary() -> None:
    """A clause break beats a later word break within the same budget."""
    # A word boundary sits at index 70 ("...word"), but a clause break (": ")
    # sits earlier at index 16 — the clause break is preferred even though a
    # later word boundary would keep more characters.
    title = "Clause break here: " + "word " * 14  # >72, word-boundary-rich
    out = _truncate_title(title)
    assert out == "Clause break here"


def test_truncate_title_picks_latest_in_budget_clause() -> None:
    """Among multiple in-budget separators the latest one wins."""
    title = (
        "First clause, second clause, third clause that pushes the whole "
        "title comfortably past the seventy-two-character truncation budget"
    )
    out = _truncate_title(title)
    assert out == "First clause, second clause"
    assert len(out) <= _TITLE_MAX


def test_truncate_title_falls_back_to_word_boundary_without_clauses() -> None:
    """A title with spaces but no clause separator cuts on a word boundary."""
    title = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november"
    assert len(title) > _TITLE_MAX
    out = _truncate_title(title)
    assert len(out) <= _TITLE_MAX
    # No partial trailing word: every kept token is a whole input word.
    assert set(out.split()).issubset(set(title.split()))


def test_truncate_title_hard_cuts_unbreakable_token() -> None:
    """Boundary: no clause and no space falls back to a hard 72-char cut."""
    title = "x" * 130
    out = _truncate_title(title)
    assert out == "x" * _TITLE_MAX


def test_truncate_title_leaves_exactly_72_untouched() -> None:
    """Boundary: a title of exactly 72 chars is returned verbatim."""
    title = "y" * _TITLE_MAX
    assert _truncate_title(title) == title


def test_truncate_title_ignores_separator_only_in_overflow_tail() -> None:
    """A clause separator entirely past the budget never moves the cut.

    The only separator sits at/beyond char 72, so it cannot anchor an
    in-budget break; the truncator falls back to the last word boundary.
    """
    title = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima, mike"
    assert len(title) > _TITLE_MAX
    out = _truncate_title(title)
    assert len(out) <= _TITLE_MAX
    assert not out.endswith(",")


def test_apply_realistic_fixture_caps_all_titles_with_no_loss() -> None:
    """Every over-cap row caps title to <=72 and preserves the full text.

    Asserts the four W24 success criteria on a realistic fixture: each
    over-cap title is <=72, ends on a clause/word boundary (no mid-word
    cut), its full original string is preserved in ``description``, and an
    already-in-cap row is left untouched with no ``description`` added.
    """
    step = MigrationV10ToV11()
    src = _realistic_state_v1_0()

    # Capture the originals BEFORE the rename so the no-loss check compares
    # against the true source string for every title-bearing row.
    originals: dict[tuple[str, str], str] = {
        ("phases", "P00"): src["phases"]["P00"]["title"],
        ("iters", "P00-I01"): src["iters"]["P00-I01"]["title"],
        ("waves", "P00-I01-W01"): src["waves"]["P00-I01-W01"]["title"],
        ("waves", "P00-I01-W02"): src["waves"]["P00-I01-W02"]["title"],
        ("backlog", "B01"): src["backlog"]["B01"]["title"],
        ("incidents", "INC01"): src["incidents"]["INC01"]["title"],
        ("decisions", "D01"): src["decisions"]["D01"]["summary"],
        ("hypotheses", "H01-01"): src["hypotheses"]["H01-01"]["text"],
    }

    out = step.apply(src)

    for (section, row_id), original in originals.items():
        row = out[section][row_id]
        title = row["title"]
        assert len(title) <= _TITLE_MAX, f"{section}/{row_id} title over cap"
        if len(original) <= _TITLE_MAX:
            # In-cap rows are untouched and gain no description.
            assert title == original
            assert row.get("description") is None
        else:
            # Over-cap rows: full text preserved, title clause-aligned.
            assert row["description"] == original, f"{section}/{row_id} lost content"
            assert original.startswith(title)
            assert title == title.rstrip()
            assert not title.endswith((",", ";", ":", ".", "-", "—"))


def test_apply_realistic_fixture_reloads_under_live_model(tmp_path: Path) -> None:
    """The migrated realistic fixture re-loads under the live State model."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_realistic_state_v1_0(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.1")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.1"
    # Spot-check the clause-aligned cap + no-loss survived the full round-trip.
    wave = reloaded.waves["P00-I01-W01"]
    assert wave.title == "Hygiene preamble"
    assert wave.description == _REALISTIC_TITLES["colon"]


def test_apply_realistic_fixture_is_idempotent() -> None:
    """Re-applying the step to the already-migrated 1.1 result is a no-op.

    The migrator's pre-check binds to ``schema_version == "1.0"``, so a true
    re-run goes through an empty chain; here we assert the transform itself
    is stable by feeding its own output (with the version reset to 1.0)
    back through ``apply`` and getting a byte-identical row set.
    """
    step = MigrationV10ToV11()
    once = step.apply(_realistic_state_v1_0())

    # Reset only the version marker so the idempotent transform re-runs over
    # already-capped titles + already-renamed fields without re-truncating.
    replay = copy.deepcopy(once)
    replay["schema_version"] = "1.0"
    twice = step.apply(replay)

    assert twice == once


def test_apply_over_500_title_caps_description_to_stay_model_valid() -> None:
    """Boundary: a title longer than the description cap is bounded to 500.

    No live row reaches 500 chars, but the migrated state must still
    re-load; the copied ``description`` is capped at :data:`_DESCRIPTION_MAX`
    so a pathological title cannot brick model validation.
    """
    step = MigrationV10ToV11()
    src = _realistic_state_v1_0()
    huge = "word " * 200  # 1000 chars, well past the 500 description cap
    src["waves"]["P00-I01-W01"]["title"] = huge
    out = step.apply(src)
    row = out["waves"]["P00-I01-W01"]
    assert len(row["title"]) <= _TITLE_MAX
    assert len(row["description"]) == _DESCRIPTION_MAX
    assert row["description"] == huge[:_DESCRIPTION_MAX]


def test_apply_preexisting_description_is_not_overwritten() -> None:
    """An over-cap row that already carries a description keeps it intact."""
    step = MigrationV10ToV11()
    src = _realistic_state_v1_0()
    src["waves"]["P00-I01-W01"]["description"] = "hand-authored note"
    out = step.apply(src)
    row = out["waves"]["P00-I01-W01"]
    assert len(row["title"]) <= _TITLE_MAX
    # The pre-existing description is preserved; the title is still truncated.
    assert row["description"] == "hand-authored note"


def test_apply_empty_string_title_gets_id_placeholder() -> None:
    """Boundary: an empty title is replaced with the row ``id`` placeholder.

    The v1.1 model floors ``title`` at ``min_length=1``, so an empty v1.0
    title cannot pass through untouched. The migrator substitutes the row's
    own ``id`` (already non-empty) and adds no ``description``.
    """
    step = MigrationV10ToV11()
    src = _realistic_state_v1_0()
    src["waves"]["P00-I01-W01"]["title"] = ""
    out = step.apply(src)
    row = out["waves"]["P00-I01-W01"]
    assert row["title"] == "P00-I01-W01"
    assert row.get("description") is None


def test_apply_whitespace_only_title_gets_id_placeholder() -> None:
    """A whitespace-only title is treated as empty and gets the id placeholder."""
    step = MigrationV10ToV11()
    src = _realistic_state_v1_0()
    src["waves"]["P00-I01-W01"]["title"] = "   \t  "
    out = step.apply(src)
    row = out["waves"]["P00-I01-W01"]
    assert row["title"] == "P00-I01-W01"
    assert row.get("description") is None


def test_apply_empty_title_without_id_falls_back_to_untitled() -> None:
    """A row with an empty title and no usable ``id`` gets ``"(untitled)"``."""
    step = MigrationV10ToV11()
    src = _realistic_state_v1_0()
    # Drop the id so the placeholder must fall back to the literal.
    del src["waves"]["P00-I01-W01"]["id"]
    src["waves"]["P00-I01-W01"]["title"] = ""
    out = step.apply(src)
    row = out["waves"]["P00-I01-W01"]
    assert row["title"] == "(untitled)"
    assert row.get("description") is None


def test_run_chain_empty_title_migrates_to_model_valid_v1_1(tmp_path: Path) -> None:
    """End-to-end: an empty-title v1.0 row migrates to a State-valid v1.1 payload.

    The empty wave title is replaced with the row ``id`` so the migrated
    state satisfies the v1.1 ``min_length=1`` floor and re-loads under the
    live ``State`` model — without the empty title bricking the next read.
    """
    state_path = tmp_path / "state.json"
    payload = _state_v1_0_with_entities()
    payload["waves"]["P00-I01-W01"]["title"] = ""
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.1")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.1")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.1"
    wave = reloaded.waves["P00-I01-W01"]
    assert wave.title == "P00-I01-W01"


# --- v1.1 -> v1.2: Iter.trigger backfill -----------------------------------
#
# The v1.2 edge adds ``Iter.trigger`` and backfills every historical iter to
# ``trigger="none"`` (excluding it from the corrected planned-vs-reactive
# denominator) without clobbering a trigger a later writer already set. The
# fixtures below build a v1.1 state whose iters exercise the absent-key
# backfill path and the preserve-existing path in one migration.


def _minimal_state_v1_1() -> dict[str, Any]:
    """Return a full v1.1 state payload that re-loads under the live model.

    The v1.0 -> v1.1 edge only bumps the version marker for a state with no
    over-cap titles, so a v1.1 minimal state is the v1.0 minimal state with
    ``schema_version`` advanced — every other key is identical.
    """
    payload = _minimal_state_v1_0()
    payload["schema_version"] = "1.1"
    return payload


def _state_v1_1_with_iters() -> dict[str, Any]:
    """Return a full v1.1 state carrying three iters for the trigger backfill.

    ``P00-I01`` and ``P00-I02`` carry no ``trigger`` key (the pre-v1.2
    shape) so the migration backfills them to ``"none"``; ``P00-I03``
    already carries ``trigger="reactive"`` so the no-clobber path is
    exercised in the same run. The phase references all three iters and the
    chain re-loads under the live model after migration.
    """
    ts = "2026-05-08T00:00:00Z"
    payload = _minimal_state_v1_1()
    payload["phases"] = {
        "P00": {
            "id": "P00",
            "scope_id": "QR",
            "subproject_id": None,
            "title": "Phase zero",
            "status": "active",
            "iter_ids": ["P00-I01", "P00-I02", "P00-I03"],
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
            "status": "closed",
            "wave_ids": [],
            "estimate_id": None,
            "audit_id": None,
            "opened_at": ts,
            "closed_at": None,
        },
        "P00-I02": {
            "id": "P00-I02",
            "phase_id": "P00",
            "title": "Iter two",
            "status": "closed",
            "wave_ids": [],
            "estimate_id": None,
            "audit_id": None,
            "opened_at": ts,
            "closed_at": None,
        },
        "P00-I03": {
            "id": "P00-I03",
            "phase_id": "P00",
            "title": "Iter three",
            "status": "active",
            "trigger": "reactive",  # already set: must survive the backfill
            "wave_ids": [],
            "estimate_id": None,
            "audit_id": None,
            "opened_at": ts,
            "closed_at": None,
        },
    }
    return payload


def test_build_migration_chain_full_v1_0_to_v1_2_two_steps() -> None:
    """The registry walks v1.0 -> v1.1 -> v1.2 as two ordered steps."""
    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.2")
    assert [(s.from_version, s.to_version) for s in chain] == [("1.0", "1.1"), ("1.1", "1.2")]


def test_build_migration_chain_v1_1_to_v1_2_single_step() -> None:
    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.1", to_version="1.2")
    assert len(chain) == 1
    assert chain[0].from_version == "1.1"
    assert chain[0].to_version == "1.2"


def test_v1_1_to_v1_2_apply_bumps_version() -> None:
    step = MigrationV11ToV12()
    out = step.apply(_minimal_state_v1_1())
    assert out["schema_version"] == "1.2"


def test_v1_1_to_v1_2_apply_does_not_mutate_input() -> None:
    step = MigrationV11ToV12()
    src = _minimal_state_v1_1()
    step.apply(src)
    assert src["schema_version"] == "1.1"


def test_v1_1_to_v1_2_apply_empty_iters_is_pure_version_bump() -> None:
    """Boundary: a state with no iters migrates to a bare version bump."""
    step = MigrationV11ToV12()
    src = _minimal_state_v1_1()  # iters == {}
    out = step.apply(src)
    expected = dict(src)
    expected["schema_version"] = "1.2"
    assert out == expected


def test_v1_1_to_v1_2_apply_backfills_absent_trigger_to_none() -> None:
    """Iters lacking ``trigger`` are backfilled to ``"none"`` (excluded)."""
    step = MigrationV11ToV12()
    out = step.apply(_state_v1_1_with_iters())
    assert out["iters"]["P00-I01"]["trigger"] == IterTrigger.NONE.value
    assert out["iters"]["P00-I02"]["trigger"] == IterTrigger.NONE.value


def test_v1_1_to_v1_2_apply_preserves_preexisting_trigger() -> None:
    """An iter that already carries a ``trigger`` is never clobbered."""
    step = MigrationV11ToV12()
    out = step.apply(_state_v1_1_with_iters())
    assert out["iters"]["P00-I03"]["trigger"] == IterTrigger.REACTIVE.value


def test_v1_1_to_v1_2_apply_is_idempotent() -> None:
    """Re-applying to the already-migrated 1.2 result (version reset) is stable.

    The pre-check binds to ``schema_version == "1.1"``, so a true re-run goes
    through an empty chain; resetting only the version marker proves the
    transform itself never re-touches a row whose ``trigger`` is set.
    """
    step = MigrationV11ToV12()
    once = step.apply(_state_v1_1_with_iters())
    replay = copy.deepcopy(once)
    replay["schema_version"] = "1.1"
    twice = step.apply(replay)
    assert twice == once


def test_v1_1_to_v1_2_check_pre_rejects_wrong_version() -> None:
    step = MigrationV11ToV12()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_pre({"schema_version": "1.0"})


def test_v1_1_to_v1_2_check_post_rejects_unbumped_version() -> None:
    step = MigrationV11ToV12()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_post({"schema_version": "1.1"})


def test_run_chain_v1_1_to_v1_2_reloads_with_backfilled_triggers(tmp_path: Path) -> None:
    """End-to-end: a v1.1 state migrates to a re-loadable v1.2 state.

    The backfilled iters carry ``trigger == none`` (excluded from the
    metric denominator) and the pre-set iter keeps its ``reactive`` value
    after the full canonical-writer round-trip.
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state_v1_1_with_iters(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.1", to_version="1.2")
    run_chain(state_path, chain=chain, from_version="1.1", to_version="1.2")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.2"
    assert reloaded.iters["P00-I01"].trigger is IterTrigger.NONE
    assert reloaded.iters["P00-I02"].trigger is IterTrigger.NONE
    assert reloaded.iters["P00-I03"].trigger is IterTrigger.REACTIVE


def test_run_chain_full_v1_0_to_v1_2_reloads(tmp_path: Path) -> None:
    """A v1.0 state walks the full 1.0 -> 1.1 -> 1.2 chain to a re-loadable v1.2.

    The chained run bumps the version twice and defaults the (absent) iter
    triggers; the minimal fixture carries no iters, so the post-state simply
    re-loads at 1.2 under the live model.
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.2")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.2")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.2"


# --- v1.2 -> v1.3: Wave.claimed_at backfill --------------------------------
#
# The v1.3 edge adds ``Wave.claimed_at`` and backfills each wave's
# work-start timestamp from its ``wave_claimed`` event in the sibling event
# store. A wave with no claim event keeps ``claimed_at`` unset, and the
# backfill is idempotent (never overwrites an existing value). The fixtures
# below build a v1.2 state with two waves -- one with a claim event, one
# without -- and write a real event-store envelope so the end-to-end run
# exercises the event-anchored read path.


_CLAIM_TS = "2026-05-20T08:15:00+00:00"


def _minimal_state_v1_2() -> dict[str, Any]:
    """Return a full v1.2 state payload that re-loads under the live model."""
    payload = _minimal_state_v1_0()
    payload["schema_version"] = "1.2"
    return payload


def _state_v1_2_with_waves() -> dict[str, Any]:
    """Return a full v1.2 state carrying two waves for the claimed_at backfill.

    ``P00-I01-W01`` has a matching ``wave_claimed`` event (backfilled);
    ``P00-I01-W02`` has none (stays unset). Neither row carries the
    pre-v1.3 ``claimed_at`` key. The phase / iter chain is referentially
    complete so the migrated state re-loads under the live model.
    """
    ts = "2026-05-08T00:00:00Z"
    payload = _minimal_state_v1_2()
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
            "trigger": "none",
            "wave_ids": ["P00-I01-W01", "P00-I01-W02"],
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
            "status": "claimed",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "opened_at": ts,
            "closed_at": None,
        },
        "P00-I01-W02": {
            "id": "P00-I01-W02",
            "iter_id": "P00-I01",
            "title": "Wave two",
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "opened_at": ts,
            "closed_at": None,
        },
    }
    return payload


def _write_wave_claimed_event(events_path: Path, *, wave_id: str, timestamp: str) -> None:
    """Append a valid ``wave_claimed`` envelope JSONL row to *events_path*."""
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.event import EventPayload

    payload = EventPayload(
        timestamp=timestamp,  # type: ignore[arg-type]
        event_type="state.mutate.wave_claim",
        event_kind="wave_claimed",
        actor="daemon",
        command="state.mutate",
        args_hash="deadbeef",
        status="ok",
        message="claimed",
    )
    envelope = Envelope(
        id=f"evt-{wave_id}",
        kind=StoreKind.EVENT,
        scope_id=wave_id,
        created_at=timestamp,  # type: ignore[arg-type]
        summary="wave claimed",
        payload=payload.model_dump(mode="json"),
    )
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(envelope.model_dump(mode="json")) + "\n")


def test_build_migration_chain_v1_2_to_v1_3_single_step() -> None:
    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.2", to_version="1.3")
    assert len(chain) == 1
    assert chain[0].from_version == "1.2"
    assert chain[0].to_version == "1.3"


def test_v1_2_to_v1_3_apply_bumps_version() -> None:
    step = MigrationV12ToV13()
    out = step.apply(_minimal_state_v1_2())
    assert out["schema_version"] == "1.3"


def test_v1_2_to_v1_3_apply_does_not_mutate_input() -> None:
    step = MigrationV12ToV13()
    src = _minimal_state_v1_2()
    step.apply(src)
    assert src["schema_version"] == "1.2"


def test_v1_2_to_v1_3_apply_without_events_leaves_claimed_at_unset() -> None:
    """With no bound event store, every wave keeps ``claimed_at`` unset."""
    step = MigrationV12ToV13()  # no bind_events_path call
    out = step.apply(_state_v1_2_with_waves())
    assert "claimed_at" not in out["waves"]["P00-I01-W01"]
    assert "claimed_at" not in out["waves"]["P00-I01-W02"]


def test_v1_2_to_v1_3_apply_backfills_claimed_at_from_event(tmp_path: Path) -> None:
    """A wave with a ``wave_claimed`` event gets its ``claimed_at`` backfilled."""
    events_path = tmp_path / "store" / "event.jsonl"
    _write_wave_claimed_event(events_path, wave_id="P00-I01-W01", timestamp=_CLAIM_TS)
    step = MigrationV12ToV13()
    step.bind_events_path(events_path)

    out = step.apply(_state_v1_2_with_waves())

    assert out["waves"]["P00-I01-W01"]["claimed_at"] == _CLAIM_TS


def test_v1_2_to_v1_3_apply_wave_without_event_stays_unset(tmp_path: Path) -> None:
    """A wave with no claim event keeps ``claimed_at`` unset after backfill."""
    events_path = tmp_path / "store" / "event.jsonl"
    _write_wave_claimed_event(events_path, wave_id="P00-I01-W01", timestamp=_CLAIM_TS)
    step = MigrationV12ToV13()
    step.bind_events_path(events_path)

    out = step.apply(_state_v1_2_with_waves())

    assert "claimed_at" not in out["waves"]["P00-I01-W02"]


def test_v1_2_to_v1_3_apply_is_idempotent_on_preexisting_claimed_at(tmp_path: Path) -> None:
    """An existing ``claimed_at`` is never overwritten by the backfill."""
    events_path = tmp_path / "store" / "event.jsonl"
    _write_wave_claimed_event(events_path, wave_id="P00-I01-W01", timestamp=_CLAIM_TS)
    step = MigrationV12ToV13()
    step.bind_events_path(events_path)

    src = _state_v1_2_with_waves()
    preset = "2026-01-01T00:00:00+00:00"
    src["waves"]["P00-I01-W01"]["claimed_at"] = preset

    out = step.apply(src)

    # setdefault leaves the operator-set value intact (not the event ts).
    assert out["waves"]["P00-I01-W01"]["claimed_at"] == preset


def test_v1_2_to_v1_3_check_pre_rejects_wrong_version() -> None:
    step = MigrationV12ToV13()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_pre({"schema_version": "1.1"})


def test_v1_2_to_v1_3_check_post_rejects_unbumped_version() -> None:
    step = MigrationV12ToV13()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_post({"schema_version": "1.2"})


def test_read_claim_anchors_absent_file_is_empty(tmp_path: Path) -> None:
    """A missing event store yields no anchors (the backfill then no-ops)."""
    assert read_claim_anchors(tmp_path / "store" / "event.jsonl") == {}


def test_read_claim_anchors_latest_timestamp_wins(tmp_path: Path) -> None:
    """Two claim events for one wave keep the latest timestamp (last write)."""
    events_path = tmp_path / "store" / "event.jsonl"
    _write_wave_claimed_event(events_path, wave_id="P00-I01-W01", timestamp=_CLAIM_TS)
    later = "2026-05-21T09:00:00+00:00"
    _write_wave_claimed_event(events_path, wave_id="P00-I01-W01", timestamp=later)

    anchors = read_claim_anchors(events_path)

    assert anchors["P00-I01-W01"].isoformat() == later


def test_run_chain_v1_2_to_v1_3_reloads_with_backfilled_claimed_at(tmp_path: Path) -> None:
    """End-to-end: a v1.2 state migrates to a re-loadable v1.3 state.

    The wave with a ``wave_claimed`` event in the sibling store carries
    the backfilled ``claimed_at`` after the full canonical-writer
    round-trip; the wave with no claim event stays unset (None).
    ``run_chain`` resolves the sibling event store from the state path
    (no explicit ``events_path`` override needed).
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state_v1_2_with_waves(), indent=2), encoding="utf-8")
    events_path = store_path(state_path, StoreKind.EVENT)
    _write_wave_claimed_event(events_path, wave_id="P00-I01-W01", timestamp=_CLAIM_TS)

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.2", to_version="1.3")
    run_chain(state_path, chain=chain, from_version="1.2", to_version="1.3")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.3"
    assert reloaded.waves["P00-I01-W01"].claimed_at is not None
    assert reloaded.waves["P00-I01-W01"].claimed_at.isoformat() == _CLAIM_TS
    assert reloaded.waves["P00-I01-W02"].claimed_at is None


def test_run_chain_v1_2_to_v1_3_old_state_without_claimed_at_loads(tmp_path: Path) -> None:
    """A v1.2 state lacking ``claimed_at`` keys migrates + re-loads at v1.3.

    No event store is present, so every wave stays unset; the migrated
    state still re-loads under the live model (additive field).
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state_v1_2_with_waves(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.2", to_version="1.3")
    run_chain(state_path, chain=chain, from_version="1.2", to_version="1.3")

    reloaded = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert reloaded.schema_version == "1.3"
    assert reloaded.waves["P00-I01-W01"].claimed_at is None
    assert reloaded.waves["P00-I01-W02"].claimed_at is None


def test_run_chain_full_v1_0_to_v1_3_reloads(tmp_path: Path) -> None:
    """A v1.0 state walks the full 1.0 -> 1.3 chain to a re-loadable v1.3."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.3")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.3")

    reloaded = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert reloaded.schema_version == "1.3"


# --- v1.3 -> v1.4: Iter.candidate_tag backfill -----------------------------
#
# The v1.4 edge adds the optional ``Iter.candidate_tag`` release tag and
# backfills every historical iter to ``None`` (the model default). The
# transform is purely additive -- there is no historical fact to recover --
# and is idempotent (an iter a writer already tagged is never clobbered).
# The fixtures below build a v1.3 state whose iters exercise both the
# absent-key backfill path and the preserve-existing path in one run.


def _minimal_state_v1_3() -> dict[str, Any]:
    """Return a full v1.3 state payload that re-loads under the live model."""
    payload = _minimal_state_v1_0()
    payload["schema_version"] = "1.3"
    return payload


def _state_v1_3_with_iters() -> dict[str, Any]:
    """Return a full v1.3 state carrying two iters for the candidate_tag backfill.

    ``P00-I01`` carries no ``candidate_tag`` key (the pre-v1.4 shape) so the
    migration backfills it to ``None``; ``P00-I02`` already carries an
    operator-set ``candidate_tag`` so the no-clobber path is exercised in the
    same run. The phase references both iters and the chain re-loads under
    the live model after migration.
    """
    ts = "2026-05-08T00:00:00Z"
    payload = _minimal_state_v1_3()
    payload["phases"] = {
        "P00": {
            "id": "P00",
            "scope_id": "QR",
            "subproject_id": None,
            "title": "Phase zero",
            "status": "active",
            "iter_ids": ["P00-I01", "P00-I02"],
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
            "trigger": "none",
            "wave_ids": [],
            "estimate_id": None,
            "audit_id": None,
            "opened_at": ts,
            "closed_at": None,
        },
        "P00-I02": {
            "id": "P00-I02",
            "phase_id": "P00",
            "title": "Iter two",
            "status": "planned",
            "trigger": "none",
            "candidate_tag": "v0.5.0",  # already set: must survive the backfill
            "wave_ids": [],
            "estimate_id": None,
            "audit_id": None,
            "opened_at": ts,
            "closed_at": None,
        },
    }
    return payload


def test_build_migration_chain_v1_3_to_v1_4_single_step() -> None:
    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.3", to_version="1.4")
    assert len(chain) == 1
    assert chain[0].from_version == "1.3"
    assert chain[0].to_version == "1.4"


def test_v1_3_to_v1_4_apply_bumps_version() -> None:
    step = MigrationV13ToV14()
    out = step.apply(_minimal_state_v1_3())
    assert out["schema_version"] == "1.4"


def test_v1_3_to_v1_4_apply_does_not_mutate_input() -> None:
    step = MigrationV13ToV14()
    src = _minimal_state_v1_3()
    step.apply(src)
    assert src["schema_version"] == "1.3"


def test_v1_3_to_v1_4_apply_empty_iters_is_pure_version_bump() -> None:
    """Boundary: a state with no iters migrates to a bare version bump."""
    step = MigrationV13ToV14()
    src = _minimal_state_v1_3()  # iters == {}
    out = step.apply(src)
    expected = dict(src)
    expected["schema_version"] = "1.4"
    assert out == expected


def test_v1_3_to_v1_4_apply_backfills_absent_candidate_tag_to_none() -> None:
    """Iters lacking ``candidate_tag`` are backfilled to ``None``."""
    step = MigrationV13ToV14()
    out = step.apply(_state_v1_3_with_iters())
    assert out["iters"]["P00-I01"]["candidate_tag"] is None


def test_v1_3_to_v1_4_apply_preserves_preexisting_candidate_tag() -> None:
    """An iter that already carries a ``candidate_tag`` is never clobbered."""
    step = MigrationV13ToV14()
    out = step.apply(_state_v1_3_with_iters())
    assert out["iters"]["P00-I02"]["candidate_tag"] == "v0.5.0"


def test_v1_3_to_v1_4_apply_is_idempotent() -> None:
    """Re-applying to the already-migrated 1.4 result (version reset) is stable.

    The pre-check binds to ``schema_version == "1.3"``, so a true re-run goes
    through an empty chain; resetting only the version marker proves the
    transform itself never re-touches a row whose ``candidate_tag`` is set.
    """
    step = MigrationV13ToV14()
    once = step.apply(_state_v1_3_with_iters())
    replay = copy.deepcopy(once)
    replay["schema_version"] = "1.3"
    twice = step.apply(replay)
    assert twice == once


def test_v1_3_to_v1_4_check_pre_rejects_wrong_version() -> None:
    step = MigrationV13ToV14()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_pre({"schema_version": "1.2"})


def test_v1_3_to_v1_4_check_post_rejects_unbumped_version() -> None:
    step = MigrationV13ToV14()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_post({"schema_version": "1.3"})


def test_run_chain_v1_3_to_v1_4_reloads_with_backfilled_candidate_tag(tmp_path: Path) -> None:
    """End-to-end: a v1.3 state migrates to a re-loadable v1.4 state.

    The backfilled iter carries ``candidate_tag is None`` and the pre-set
    iter keeps its ``v0.5.0`` value after the full canonical-writer
    round-trip.
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state_v1_3_with_iters(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.3", to_version="1.4")
    run_chain(state_path, chain=chain, from_version="1.3", to_version="1.4")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.4"
    assert reloaded.iters["P00-I01"].candidate_tag is None
    assert reloaded.iters["P00-I02"].candidate_tag == "v0.5.0"


def test_run_chain_v1_3_to_v1_4_old_state_without_candidate_tag_loads(tmp_path: Path) -> None:
    """A v1.3 state lacking ``candidate_tag`` keys migrates + re-loads at v1.4.

    Every iter stays unset; the migrated state still re-loads under the live
    model (additive field).
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state_v1_3_with_iters(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.3", to_version="1.4")
    run_chain(state_path, chain=chain, from_version="1.3", to_version="1.4")

    reloaded = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert reloaded.schema_version == "1.4"
    assert reloaded.iters["P00-I01"].candidate_tag is None


def test_run_chain_full_v1_0_to_v1_4_reloads(tmp_path: Path) -> None:
    """A v1.0 state walks the full 1.0 -> 1.4 chain to a re-loadable v1.4."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.4")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.4")

    reloaded = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert reloaded.schema_version == "1.4"


# --- v1.4 -> v1.5: ArtifactKind.MATH_EXPLAINER registration ----------------
#
# The v1.5 edge registers the ``MATH_EXPLAINER`` artifact kind -- a purely
# additive enum value. No existing state row references it (``Artifact.kind``
# is free-form and no historical artifact carries ``"math_explainer"``), so
# the transform is a bare ``schema_version`` bump with no field backfill: the
# round-trip below asserts every pre-existing row survives byte-for-byte.


def _minimal_state_v1_4() -> dict[str, Any]:
    """Return a full v1.4 state payload that re-loads under the live model."""
    payload = _minimal_state_v1_0()
    payload["schema_version"] = "1.4"
    return payload


def _state_v1_4_with_artifact() -> dict[str, Any]:
    """Return a v1.4 state carrying one pre-existing artifact row.

    The artifact carries the pre-existing ``research_brief`` kind so the
    no-row-change invariant of the additive ``MATH_EXPLAINER`` enum bump is
    exercised: the migration must leave the artifact untouched and re-load it
    after the canonical-writer round-trip.
    """
    payload = _minimal_state_v1_4()
    payload["artifacts"] = {
        "ART-research-demo": {
            "id": "ART-research-demo",
            "kind": "research_brief",
            "uri": ".ea/artifacts/research-demo.md",
            "urn": "urn:eawf:v1:artifact:QR/ART-research-demo",
            "created_at": "2026-05-08T00:00:00Z",
        }
    }
    return payload


def test_build_migration_chain_v1_4_to_v1_5_single_step() -> None:
    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.4", to_version="1.5")
    assert len(chain) == 1
    assert chain[0].from_version == "1.4"
    assert chain[0].to_version == "1.5"


def test_v1_4_to_v1_5_apply_bumps_version() -> None:
    step = MigrationV14ToV15()
    out = step.apply(_minimal_state_v1_4())
    assert out["schema_version"] == "1.5"


def test_v1_4_to_v1_5_apply_does_not_mutate_input() -> None:
    step = MigrationV14ToV15()
    src = _minimal_state_v1_4()
    step.apply(src)
    assert src["schema_version"] == "1.4"


def test_v1_4_to_v1_5_apply_is_pure_version_bump() -> None:
    """Boundary: the only delta is the version marker -- every row is preserved."""
    step = MigrationV14ToV15()
    src = _state_v1_4_with_artifact()
    out = step.apply(src)
    expected = copy.deepcopy(src)
    expected["schema_version"] = "1.5"
    assert out == expected


def test_v1_4_to_v1_5_apply_preserves_existing_artifact_rows() -> None:
    """The additive enum bump leaves every pre-existing artifact row unchanged."""
    step = MigrationV14ToV15()
    out = step.apply(_state_v1_4_with_artifact())
    assert out["artifacts"]["ART-research-demo"]["kind"] == "research_brief"


def test_v1_4_to_v1_5_apply_is_idempotent() -> None:
    """Re-applying to the already-migrated 1.5 result (version reset) is stable."""
    step = MigrationV14ToV15()
    once = step.apply(_state_v1_4_with_artifact())
    replay = copy.deepcopy(once)
    replay["schema_version"] = "1.4"
    twice = step.apply(replay)
    assert twice == once


def test_v1_4_to_v1_5_check_pre_rejects_wrong_version() -> None:
    step = MigrationV14ToV15()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_pre({"schema_version": "1.3"})


def test_v1_4_to_v1_5_check_post_rejects_unbumped_version() -> None:
    step = MigrationV14ToV15()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_post({"schema_version": "1.4"})


def test_run_chain_v1_4_to_v1_5_reloads_with_no_row_loss(tmp_path: Path) -> None:
    """End-to-end: a v1.4 state migrates to a re-loadable v1.5 with no row loss.

    The pre-existing artifact row survives the full canonical-writer round-trip
    and the migrated state re-loads under the live ``State`` model.
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state_v1_4_with_artifact(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.4", to_version="1.5")
    run_chain(state_path, chain=chain, from_version="1.4", to_version="1.5")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.5"
    assert reloaded.artifacts["ART-research-demo"].kind == "research_brief"
    assert reloaded.artifacts["ART-research-demo"].uri == ".ea/artifacts/research-demo.md"


def test_run_chain_full_v1_0_to_v1_5_reloads(tmp_path: Path) -> None:
    """A v1.0 state walks the full 1.0 -> 1.5 chain to a re-loadable v1.5."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.5")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.5")

    reloaded = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert reloaded.schema_version == "1.5"


# --- v1.5 -> v1.6: State.dispatch_paused flag -----------------------------
#
# The v1.6 edge adds the top-level ``dispatch_paused`` flag -- a purely
# additive field. No existing state row carries it (the field is brand new),
# and the live ``State`` model supplies the ``False`` default on load, so the
# transform is a bare ``schema_version`` bump with no field backfill: the
# round-trip below asserts the migrated state re-loads with the flag defaulted
# to ``False`` and every pre-existing row preserved.


def _minimal_state_v1_5() -> dict[str, Any]:
    """Return a full v1.5 state payload that re-loads under the live model."""
    payload = _minimal_state_v1_0()
    payload["schema_version"] = "1.5"
    return payload


def test_build_migration_chain_v1_5_to_v1_6_single_step() -> None:
    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.5", to_version="1.6")
    assert len(chain) == 1
    assert chain[0].from_version == "1.5"
    assert chain[0].to_version == "1.6"


def test_v1_5_to_v1_6_apply_bumps_version() -> None:
    step = MigrationV15ToV16()
    out = step.apply(_minimal_state_v1_5())
    assert out["schema_version"] == "1.6"


def test_v1_5_to_v1_6_apply_does_not_mutate_input() -> None:
    step = MigrationV15ToV16()
    src = _minimal_state_v1_5()
    step.apply(src)
    assert src["schema_version"] == "1.5"


def test_v1_5_to_v1_6_apply_is_pure_version_bump() -> None:
    """Boundary: the only delta is the version marker -- every row is preserved."""
    step = MigrationV15ToV16()
    src = _minimal_state_v1_5()
    out = step.apply(src)
    expected = copy.deepcopy(src)
    expected["schema_version"] = "1.6"
    assert out == expected


def test_v1_5_to_v1_6_apply_is_idempotent() -> None:
    """Re-applying to the already-migrated 1.6 result (version reset) is stable."""
    step = MigrationV15ToV16()
    once = step.apply(_minimal_state_v1_5())
    replay = copy.deepcopy(once)
    replay["schema_version"] = "1.5"
    twice = step.apply(replay)
    assert twice == once


def test_v1_5_to_v1_6_check_pre_rejects_wrong_version() -> None:
    step = MigrationV15ToV16()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_pre({"schema_version": "1.4"})


def test_v1_5_to_v1_6_check_post_rejects_unbumped_version() -> None:
    step = MigrationV15ToV16()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        step.check_post({"schema_version": "1.5"})


def test_run_chain_v1_5_to_v1_6_reloads_with_dispatch_paused_false(tmp_path: Path) -> None:
    """End-to-end: a v1.5 state migrates to a re-loadable v1.6 with ``dispatch_paused=False``.

    The additive flag defaults to ``False`` on load (the migration writes no
    backfill), and the migrated state re-loads under the live ``State`` model.
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_minimal_state_v1_5(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.5", to_version="1.6")
    run_chain(state_path, chain=chain, from_version="1.5", to_version="1.6")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    reloaded = State.model_validate(on_disk)
    assert reloaded.schema_version == "1.6"
    assert reloaded.dispatch_paused is False


def test_run_chain_full_v1_0_to_v1_6_reloads(tmp_path: Path) -> None:
    """A v1.0 state walks the full 1.0 -> 1.6 chain to a re-loadable v1.6."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_minimal_state_v1_0(), indent=2), encoding="utf-8")

    chain = build_migration_chain(DEFAULT_REGISTRY, from_version="1.0", to_version="1.6")
    run_chain(state_path, chain=chain, from_version="1.0", to_version="1.6")

    reloaded = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert reloaded.schema_version == "1.6"
    assert reloaded.dispatch_paused is False
