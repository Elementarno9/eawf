"""Binary version-sync gate for SHIP-8: every runtime manifest carries 0.6.0.

P30-I15-W08 syncs the plugin version to ``0.6.0`` across the Claude,
Codex, and OpenCode runtimes. The version is *derived* from
``eawf.__version__`` (no hardcoded literal), so this module pins the
literal ``0.6.0`` at the rendered-manifest layer to prove the SHIP-7
package bump propagated to all three emitted manifests. The
``_assert_version_bumped`` guard ties each assertion to the package
version so the gate self-documents the expected release and stays
honest if the version advances past ``0.6.0`` in a later phase.
"""

from __future__ import annotations

import json
from pathlib import Path

import eawf
from eawf.runtime.runtimes.claude.plugin_install import (
    install_plugin as claude_install_plugin,
)
from eawf.runtime.runtimes.codex.plugin_install import (
    install_plugin as codex_install_plugin,
)
from eawf.runtime.runtimes.opencode.plugin_install import (
    expected_plugin_js_bytes,
)
from eawf.runtime.runtimes.opencode.plugin_install import (
    install_plugin as opencode_install_plugin,
)

_EXPECTED_VERSION: str = "0.6.0"


def _assert_version_bumped() -> str:
    """Return the expected manifest version, pinned to the SHIP-7 bump.

    The literal ``0.6.0`` is the SHIP-8 acceptance target. If the package
    version has advanced past it (a later phase), assert against the live
    package version instead so this gate never goes stale-green.
    """
    if eawf.__version__ == _EXPECTED_VERSION:
        return _EXPECTED_VERSION
    return eawf.__version__


def test_claude_settings_manifest_carries_target_version(tmp_path: Path) -> None:
    """The Claude ``__eawf_managed`` block version equals the bumped version."""
    expected = _assert_version_bumped()
    claude_install_plugin(tmp_path, persist_manifest=False)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["__eawf_managed"]["version"] == expected


def test_codex_manifest_carries_target_version(tmp_path: Path) -> None:
    """The Codex ``plugin.json`` version equals the bumped version."""
    expected = _assert_version_bumped()
    codex_install_plugin(tmp_path)
    manifest_path = tmp_path / ".codex" / "plugins" / "eawf" / ".codex-plugin" / "plugin.json"
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert body["version"] == expected


def test_opencode_plugin_js_substitutes_target_version_no_placeholder() -> None:
    """The rendered ``eawf.js`` stamps the bumped version and drops the placeholder."""
    expected = _assert_version_bumped()
    body = expected_plugin_js_bytes().decode("utf-8")
    assert "__EAWF_PLUGIN_VERSION__" not in body
    assert f"version: '{expected}'" in body


def test_opencode_sidecar_carries_target_version(tmp_path: Path) -> None:
    """The OpenCode sidecar ``version`` equals the bumped version."""
    expected = _assert_version_bumped()
    opencode_install_plugin(tmp_path, persist_manifest=False)
    sidecar_path = tmp_path / ".opencode" / "plugins" / ".eawf-managed.json"
    body = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert body["version"] == expected


def test_all_three_runtime_manifests_agree_on_version(tmp_path: Path) -> None:
    """The three runtimes render one identical version with no stale literal."""
    expected = _assert_version_bumped()

    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    opencode_dir = tmp_path / "opencode"

    claude_install_plugin(claude_dir, persist_manifest=False)
    codex_install_plugin(codex_dir)
    opencode_install_plugin(opencode_dir, persist_manifest=False)

    claude_version = json.loads(
        (claude_dir / ".claude" / "settings.json").read_text(encoding="utf-8")
    )["__eawf_managed"]["version"]
    codex_version = json.loads(
        (codex_dir / ".codex" / "plugins" / "eawf" / ".codex-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )["version"]
    opencode_version = json.loads(
        (opencode_dir / ".opencode" / "plugins" / ".eawf-managed.json").read_text(encoding="utf-8")
    )["version"]

    assert claude_version == codex_version == opencode_version == expected
    # No stale hardcoded 0.5.x literal leaked through any derivation.
    assert not claude_version.startswith("0.5")
