"""P26-W02: legacy on-marker-1.0 config bodies still need a cleanup pass.

The C08 schema bump (P25-W14) landed ``schema_version: "1.0"`` but the
v0.1 wizard wrote three pre-C08 keys that the new ``_ConfigSchema``
rejects + the ``_normalise_runtime_adapters`` shim warns about on every
CLI invocation:

* top-level ``lifecycle`` block — superseded by per-iter depth tracking;
* top-level ``plugins`` block — superseded by canonical runtime selectors;
* ``runtime.kind`` scalar — superseded by ``runtime.adapters`` (list)
  plus ``runtime.preference`` (ordered fallback ladder per D14).

The migrator's "no-op on 1.0" fast path now scans for any of those three
signals and runs the canonical cleanup, so an upgrade-in-place rewrites
the body without bumping the marker. The cleanup is idempotent — a body
already in canonical shape stays untouched.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from eawf.kernel.config.migration import migrate_config_file, migrate_config_payload

# --- in-memory cleanup ------------------------------------------------------


def test_cleanup_drops_top_level_lifecycle_on_1_0() -> None:
    """A ``1.0`` body still carrying ``lifecycle:`` is rewritten cleanly."""
    payload = {
        "schema_version": "1.0",
        "lifecycle": {"depth": "phase"},
        "runtime": {"adapters": ["claude-code"], "preference": ["claude-code"]},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert "lifecycle" not in upgraded
    assert upgraded["schema_version"] == "1.0"


def test_cleanup_drops_top_level_plugins_on_1_0() -> None:
    """A ``1.0`` body still carrying ``plugins:`` is rewritten cleanly."""
    payload = {
        "schema_version": "1.0",
        "plugins": {"enabled": []},
        "runtime": {"adapters": ["claude-code"], "preference": ["claude-code"]},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert "plugins" not in upgraded


def test_cleanup_rewrites_runtime_kind_into_adapters_and_preference() -> None:
    """A ``1.0`` body with bare ``runtime.kind`` lands on the C08 keys."""
    payload = {
        "schema_version": "1.0",
        "runtime": {"kind": "claude"},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert "kind" not in upgraded["runtime"]
    assert upgraded["runtime"]["adapters"] == ["claude-code"]
    assert upgraded["runtime"]["preference"] == ["claude-code"]


def test_cleanup_preserves_existing_adapters_when_dropping_kind() -> None:
    """When both ``kind`` and ``adapters`` exist, ``adapters`` is authoritative."""
    payload = {
        "schema_version": "1.0",
        "runtime": {
            "kind": "claude-code",
            "adapters": ["codex"],
            "preference": ["codex"],
        },
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert "kind" not in upgraded["runtime"]
    assert upgraded["runtime"]["adapters"] == ["codex"]
    assert upgraded["runtime"]["preference"] == ["codex"]


def test_cleanup_synthesises_preference_when_only_adapters_set() -> None:
    """Dropping ``kind`` still leaves ``preference`` populated."""
    payload = {
        "schema_version": "1.0",
        "runtime": {
            "kind": "opencode",
            "adapters": ["opencode"],
        },
    }
    upgraded, _ = migrate_config_payload(payload)
    assert upgraded["runtime"]["preference"] == ["opencode"]


def test_cleanup_promotes_adapter_catalog_with_canonical_runtime_ids() -> None:
    """Enabled legacy adapter rows become canonical selectors."""
    payload = {
        "schema_version": "1.0",
        "runtime": {
            "adapter_catalog": {
                "claude": {"enabled": True},
                "codex": {"enabled": True},
                "opencode": {"enabled": False},
            }
        },
    }

    upgraded, changed = migrate_config_payload(payload)

    assert changed is True
    assert "adapter_catalog" not in upgraded["runtime"]
    assert upgraded["runtime"]["adapters"] == ["claude-code", "codex"]
    assert upgraded["runtime"]["preference"] == ["claude-code", "codex"]


def test_cleanup_renames_default_subproject_to_default_track() -> None:
    """A ``1.0`` body with the legacy project key lands on ``default_track``."""
    payload = {
        "schema_version": "1.0",
        "project": {"default_subproject": "COLLAR"},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert "default_subproject" not in upgraded["project"]
    assert upgraded["project"]["default_track"] == "COLLAR"


def test_cleanup_preserves_existing_default_track_when_dropping_legacy_key() -> None:
    """When both names exist, ``default_track`` is authoritative."""
    payload = {
        "schema_version": "1.0",
        "project": {"default_subproject": "OLD", "default_track": "NEW"},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert "default_subproject" not in upgraded["project"]
    assert upgraded["project"]["default_track"] == "NEW"


def test_cleanup_renames_memory_subproject_store_to_track() -> None:
    """Legacy memory store names are rewritten to Track vocabulary."""
    payload = {
        "schema_version": "1.0",
        "memory": {"stores": ["project", "subproject", "track", "agent"]},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert upgraded["memory"]["stores"] == ["project", "track", "agent"]


def test_cleanup_all_three_signals_in_one_pass() -> None:
    """A v0.1 wizard-shaped body collapses to the canonical form."""
    payload = {
        "schema_version": "1.0",
        "acceptance": {"lint": True, "tests": True, "typecheck": True},
        "lifecycle": {"depth": "phase"},
        "plugins": {"enabled": []},
        "project": {"default_subproject": "COLLAR"},
        "memory": {"stores": ["project", "subproject", "user"]},
        "profiles": {"enabled": ["core", "python", "research"]},
        "runtime": {"kind": "claude-code"},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert "lifecycle" not in upgraded
    assert "plugins" not in upgraded
    assert "kind" not in upgraded["runtime"]
    assert "default_subproject" not in upgraded["project"]
    assert upgraded["project"]["default_track"] == "COLLAR"
    assert upgraded["memory"]["stores"] == ["project", "track", "user"]
    assert upgraded["runtime"]["adapters"] == ["claude-code"]
    assert upgraded["runtime"]["preference"] == ["claude-code"]
    # Operator-curated sections survive.
    assert upgraded["profiles"]["enabled"] == ["core", "python", "research"]
    assert upgraded["acceptance"] == {"lint": True, "tests": True, "typecheck": True}


def test_cleanup_no_op_on_already_canonical_body() -> None:
    """A body free of legacy signals returns ``changed=False``."""
    payload = {
        "schema_version": "1.0",
        "runtime": {"adapters": ["claude-code"], "preference": ["claude-code"]},
        "profiles": {"enabled": ["core"]},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is False
    assert upgraded == payload


def test_cleanup_idempotent_double_run() -> None:
    """Re-running the cleanup on its own output is a no-op."""
    payload = {
        "schema_version": "1.0",
        "lifecycle": {"depth": "phase"},
        "plugins": {"enabled": []},
        "runtime": {"kind": "claude-code"},
    }
    first, first_changed = migrate_config_payload(payload)
    second, second_changed = migrate_config_payload(first)
    assert first_changed is True
    assert second_changed is False
    assert first == second


def test_cleanup_preserves_unrelated_operator_keys() -> None:
    """Operator values survive while deprecated flow paths are renamed."""
    payload = {
        "schema_version": "1.0",
        "flow": {"auto_accept": {"research": True}},
        "lifecycle": {"depth": "phase"},
        "polish": {"auto_apply_safe": True},
        "runtime": {"kind": "claude-code"},
    }
    upgraded, _ = migrate_config_payload(payload)
    assert "lifecycle" not in upgraded
    assert upgraded["flow"] == {"advance_after": {"research": True}}
    assert "polish" not in upgraded


def test_cleanup_does_not_mutate_input() -> None:
    """Cleanup mode still returns a fresh copy — input mapping is untouched."""
    payload: dict = {
        "schema_version": "1.0",
        "lifecycle": {"depth": "phase"},
        "runtime": {"kind": "claude-code"},
    }
    snapshot = repr(payload)
    migrate_config_payload(payload)
    assert repr(payload) == snapshot


# --- on-disk round-trip -----------------------------------------------------


def test_migrate_file_cleanup_on_disk_keeps_marker_writes_backup(
    tmp_path: Path,
) -> None:
    """On-disk cleanup writes a ``1.0`` backup before rewriting the body."""
    target = tmp_path / "config.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "lifecycle": {"depth": "phase"},
                "plugins": {"enabled": []},
                "runtime": {"kind": "claude-code"},
            },
        ),
        encoding="utf-8",
    )
    upgraded, changed, backup_path = migrate_config_file(target)
    assert changed is True
    assert upgraded["schema_version"] == "1.0"
    assert "lifecycle" not in upgraded
    assert "plugins" not in upgraded
    assert "kind" not in upgraded["runtime"]
    assert upgraded["runtime"]["adapters"] == ["claude-code"]
    assert upgraded["runtime"]["preference"] == ["claude-code"]
    # Backup file uses the original marker.
    assert backup_path is not None
    assert backup_path.exists()
    assert backup_path.name.startswith("config.yaml.bak.1.0.")
    # Re-running on the rewritten file is a no-op (idempotent on disk).
    _, second_changed, second_backup = migrate_config_file(target)
    assert second_changed is False
    assert second_backup is None


def test_migrate_file_canonical_1_0_body_no_backup(tmp_path: Path) -> None:
    """A body already in canonical shape writes no backup."""
    target = tmp_path / "config.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "runtime": {
                    "adapters": ["claude-code"],
                    "preference": ["claude-code"],
                },
            },
        ),
        encoding="utf-8",
    )
    _, changed, backup_path = migrate_config_file(target)
    assert changed is False
    assert backup_path is None
