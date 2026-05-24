"""Unit tests for the ``settings.json`` ``__eawf_managed`` namespace patcher.

Covers:

- ``__eawf_managed`` is the only key Eä writes; user-owned keys
  (``$schema``, ``permissions``, ``additionalDirectories``,
  ``skillOverrides``) round-trip verbatim.
- ``__eawf_managed.hash`` is a deterministic 16-hex digest of the body.
- Two installs with the same registry produce a byte-identical
  ``__eawf_managed`` dict.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from eawf.runtime.runtimes.claude.plugin_install import install_plugin

_USER_KEYS_FIXTURE: dict[str, object] = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "permissions": {
        "allow": [
            "Bash(uv *)",
            "Bash(git *)",
        ]
    },
    "enableAllProjectMcpServers": False,
    "additionalDirectories": ["~/projects/example"],
    "skillOverrides": {"peon-ping-config": "off"},
}


def test_settings_managed_namespace_is_present(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    parsed = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "__eawf_managed" in parsed
    managed = parsed["__eawf_managed"]
    assert managed["version"] == "1.0"
    assert isinstance(managed["skills"], list)
    assert isinstance(managed["agents"], list)
    assert isinstance(managed["hooks"], list)


def test_settings_user_keys_preserved(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(_USER_KEYS_FIXTURE, indent=2), encoding="utf-8")
    install_plugin(tmp_path)
    parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    for k, v in _USER_KEYS_FIXTURE.items():
        assert parsed[k] == v, f"user-owned key {k} was modified"
    assert "__eawf_managed" in parsed


def test_settings_managed_hash_is_16_hex(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    parsed = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    digest = parsed["__eawf_managed"]["hash"]
    assert isinstance(digest, str)
    assert len(digest) == 16
    int(digest, 16)  # raises if not hex


def test_settings_managed_hash_is_deterministic(tmp_path: Path) -> None:
    """Two fresh installs produce identical hash strings."""
    install_plugin(tmp_path / "a")
    install_plugin(tmp_path / "b")
    parsed_a = json.loads(
        (tmp_path / "a" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    parsed_b = json.loads(
        (tmp_path / "b" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert parsed_a["__eawf_managed"]["hash"] == parsed_b["__eawf_managed"]["hash"]


def test_settings_re_render_byte_stable(tmp_path: Path) -> None:
    """Acceptance §2: re-running install yields a byte-identical settings.json."""
    install_plugin(tmp_path)
    snapshot = (tmp_path / ".claude" / "settings.json").read_bytes()
    install_plugin(tmp_path)
    assert (tmp_path / ".claude" / "settings.json").read_bytes() == snapshot


def test_settings_managed_skill_listing_matches_registry(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    parsed = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    from eawf.render.skills import SKILL_REGISTRY

    listed = {s["name"] for s in parsed["__eawf_managed"]["skills"]}
    expected = {s.skill_name for s in SKILL_REGISTRY}
    assert listed == expected


def test_settings_managed_agent_listing_matches_registry(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    parsed = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    from eawf.render.agents import AGENT_REGISTRY

    listed = {a["name"] for a in parsed["__eawf_managed"]["agents"]}
    expected = {a.role for a in AGENT_REGISTRY}
    assert listed == expected


def test_settings_managed_hook_listing_matches_registry(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    parsed = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    from eawf.render.hooks import HOOK_REGISTRY

    listed = {h["event_type"] for h in parsed["__eawf_managed"]["hooks"]}
    expected = {h.event_type.value for h in HOOK_REGISTRY}
    assert listed == expected


def test_settings_managed_hash_changes_with_payload(tmp_path: Path) -> None:
    """A modified body should change the hash (defence in depth)."""
    install_plugin(tmp_path)
    parsed = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    body = dict(parsed["__eawf_managed"])
    original_hash = body.pop("hash")
    body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
    expected_hash = hashlib.blake2b(body_json.encode("utf-8"), digest_size=8).hexdigest()
    assert original_hash == expected_hash
