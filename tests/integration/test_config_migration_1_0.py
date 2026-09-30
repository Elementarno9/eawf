"""Integration tests for the C08 schema_version 1.0 migration runner
(P25-W14 success criterion 4).

The migrator accepts legacy schema markers ``"1.1"`` (P14-W03 shipped
form) and ``"2"`` (interim experimental marker), upgrades them in
memory + writes back with ``schema_version: "1.0"`` plus the consumed C08
sections (telemetry, profiles.trusted), and strips the leaves no code reads
from a body on any marker. Re-running on an already-1.0 body is a no-op.

The tests exercise the migrator through both entry points:

* :func:`migrate_config_payload` — in-memory upgrade.
* :func:`migrate_config_file` — read-migrate-write with backup.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eawf.kernel.config.migration import (
    ACCEPTED_MARKERS,
    CURRENT_MARKER,
    LEGACY_MARKERS,
    migrate_config_file,
    migrate_config_payload,
)
from eawf.surfaces.cli.errors import ValidationError

# --- migrate_config_payload (in-memory) -------------------------------------


def test_migrate_payload_no_op_on_current_marker() -> None:
    """A body already at marker ``1.0`` returns ``changed=False``."""
    payload = {"schema_version": "1.0", "review": {"default_level": "medium"}}
    upgraded, changed = migrate_config_payload(payload)
    assert changed is False
    assert upgraded == payload


def test_migrate_payload_from_legacy_1_1_sets_current_marker() -> None:
    """Legacy ``1.1`` upgrades to ``1.0`` (the C08 canonical marker)."""
    payload = {"schema_version": "1.1", "review": {"default_level": "medium"}}
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert upgraded["schema_version"] == "1.0"


def test_migrate_payload_from_legacy_2_sets_current_marker() -> None:
    """Legacy ``"2"`` (interim experimental) upgrades to ``1.0``."""
    payload = {"schema_version": "2", "review": {"default_level": "high"}}
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert upgraded["schema_version"] == "1.0"
    # Operator value preserved.
    assert upgraded["review"]["default_level"] == "high"


def test_migrate_payload_introduces_telemetry_section() -> None:
    payload = {"schema_version": "1.1"}
    upgraded, _ = migrate_config_payload(payload)
    assert upgraded["telemetry"] == {"enabled": False, "db_kind": "sqlite"}


def test_migrate_payload_introduces_no_retired_section() -> None:
    """The legacy upgrade defaults only the sections a consumer reads."""
    upgraded, _ = migrate_config_payload({"schema_version": "1.1"})
    assert set(upgraded) == {"schema_version", "telemetry", "profiles"}


def test_migrate_payload_introduces_profiles_trusted() -> None:
    payload = {"schema_version": "1.1", "profiles": {"enabled": ["core"]}}
    upgraded, _ = migrate_config_payload(payload)
    assert upgraded["profiles"]["trusted"] == {}
    # Operator's existing enabled list preserved.
    assert upgraded["profiles"]["enabled"] == ["core"]


def test_migrate_payload_strips_retired_leaves_on_the_current_marker() -> None:
    """A 1.0 body loses every leaf no code reads, and each emptied section with it."""
    payload = {
        "schema_version": "1.0",
        "project": {"code": "DEMO", "default_subproject": "COLLAR"},
        "memory": {"stores": ["project", "subproject", "user"]},
        "vcs": {"auto_commit": "ask", "pr_merge_method": "merge"},
        "telemetry": {"enabled": True, "export": {"format": "prom", "endpoint": "x"}},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert upgraded == {
        "schema_version": "1.0",
        "vcs": {"pr_merge_method": "merge"},
        "telemetry": {"enabled": True},
    }


def test_migrate_payload_synthesises_runtime_preference_from_adapters() -> None:
    """Legacy v1.1 stored ``runtime.adapters`` only; C08 promotes it."""
    payload = {
        "schema_version": "1.1",
        "runtime": {"adapters": ["claude", "codex"]},
    }
    upgraded, _ = migrate_config_payload(payload)
    assert upgraded["runtime"]["preference"] == ["claude-code", "codex"]
    # Adapters list retained for deprecation-shim compatibility.
    assert upgraded["runtime"]["adapters"] == ["claude-code", "codex"]


def test_migrate_payload_preserves_existing_runtime_preference() -> None:
    """When the operator already typed ``preference``, do not overwrite."""
    payload = {
        "schema_version": "1.1",
        "runtime": {
            "adapters": ["claude"],
            "preference": ["codex", "claude"],
        },
    }
    upgraded, _ = migrate_config_payload(payload)
    assert upgraded["runtime"]["preference"] == ["codex", "claude-code"]


def test_migrate_payload_preserves_existing_telemetry_overrides() -> None:
    """An operator who set telemetry.enabled=True keeps that value."""
    payload = {
        "schema_version": "1.1",
        "telemetry": {"enabled": True},
    }
    upgraded, _ = migrate_config_payload(payload)
    assert upgraded["telemetry"]["enabled"] is True
    # Missing C08 keys still get defaulted.
    assert upgraded["telemetry"]["db_kind"] == "sqlite"


def test_migrate_payload_missing_marker_normalizes_legacy_layer() -> None:
    """An absent marker identifies a schema-less legacy layer."""
    upgraded, changed = migrate_config_payload({"review": {"default_level": "medium"}})
    assert changed is True
    assert upgraded == {
        "schema_version": "1.0",
        "review": {"default_level": "medium"},
    }


def test_migrate_payload_unknown_marker_raises_validation_failed() -> None:
    """A marker outside the accepted set raises with the value."""
    with pytest.raises(ValidationError, match="unknown config schema_version"):
        migrate_config_payload({"schema_version": "999"})


def test_migrate_payload_idempotent_double_run() -> None:
    """Running twice on a legacy body is the same as running once + once on 1.0."""
    payload = {"schema_version": "1.1", "review": {"default_level": "medium"}}
    first, _ = migrate_config_payload(payload)
    second, changed = migrate_config_payload(first)
    assert changed is False
    assert first == second


def test_migrate_payload_does_not_mutate_input() -> None:
    """Original mapping is left untouched (fresh copy returned)."""
    payload: dict = {"schema_version": "1.1", "runtime": {"adapters": ["claude"]}}
    snapshot = repr(payload)
    migrate_config_payload(payload)
    assert repr(payload) == snapshot


# --- migrate_config_file (on-disk round-trip) -------------------------------


def test_migrate_file_writes_backup_and_upgraded_yaml(tmp_path: Path) -> None:
    """Round-trip: read 1.1 body, upgrade to 1.0, write back, keep backup."""
    target = tmp_path / "config.yaml"
    target.write_text(
        yaml.safe_dump(
            {"schema_version": "1.1", "review": {"default_level": "high"}},
        ),
        encoding="utf-8",
    )
    upgraded, changed, backup_path = migrate_config_file(target)
    assert changed is True
    assert upgraded["schema_version"] == "1.0"
    assert upgraded["review"]["default_level"] == "high"
    # Backup written next to the file.
    assert backup_path is not None
    assert backup_path.exists()
    assert backup_path.name.startswith("config.yaml.bak.1.1.")
    # Original file now contains the upgraded body.
    reloaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert reloaded["schema_version"] == "1.0"
    assert reloaded["telemetry"]["enabled"] is False


def test_migrate_file_no_change_returns_no_backup(tmp_path: Path) -> None:
    """An already-1.0 body returns ``backup_path=None``."""
    target = tmp_path / "config.yaml"
    target.write_text(
        yaml.safe_dump({"schema_version": "1.0", "review": {"default_level": "medium"}}),
        encoding="utf-8",
    )
    _, changed, backup_path = migrate_config_file(target)
    assert changed is False
    assert backup_path is None


def test_migrate_file_missing_returns_empty(tmp_path: Path) -> None:
    """A missing file is the empty-layer case (no-op)."""
    target = tmp_path / "absent.yaml"
    upgraded, changed, backup_path = migrate_config_file(target)
    assert upgraded == {}
    assert changed is False
    assert backup_path is None


def test_migrate_file_corrupted_yaml_raises(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("planning:\n  - bad indent\n :::", encoding="utf-8")
    with pytest.raises(ValidationError, match="malformed YAML"):
        migrate_config_file(target)


def test_migrate_file_top_level_list_rejected(tmp_path: Path) -> None:
    """YAML whose root is a list (not mapping) is rejected."""
    target = tmp_path / "config.yaml"
    target.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="must contain a YAML mapping"):
        migrate_config_file(target)


def test_migrate_file_from_marker_2_upgrades_in_place(tmp_path: Path) -> None:
    """Legacy ``"2"`` (interim experimental) marker upgrades."""
    target = tmp_path / "config.yaml"
    target.write_text(
        yaml.safe_dump({"schema_version": "2", "review": {"default_level": "medium"}}),
        encoding="utf-8",
    )
    upgraded, changed, backup_path = migrate_config_file(target)
    assert changed is True
    assert upgraded["schema_version"] == "1.0"
    assert backup_path is not None
    assert backup_path.name.startswith("config.yaml.bak.2.")


# --- marker-set sanity checks -----------------------------------------------


def test_accepted_markers_contains_current_and_legacy() -> None:
    assert CURRENT_MARKER == "1.0"
    assert frozenset({"1.1", "2"}) == LEGACY_MARKERS
    assert LEGACY_MARKERS | {CURRENT_MARKER} == ACCEPTED_MARKERS
