"""Validate the two committed self-hosted marketplace catalog pointers.

P29-I06-W05 ships two small committed pointer manifests (NOT the rendered
plugin trees) so the eawf plugin is installable from a self-hosted catalog:

- ``.claude-plugin/marketplace.json`` — Claude Code entry, ``npm`` source.
- ``.agents/plugins/marketplace.json`` — Codex entry, ``git-subdir`` source
  pointing at the canonical repo's ``plugins-dist`` branch.

These tests assert the committed files parse as valid manifests, carry the
expected source kinds + keys, stay free of PII, and remain byte-identical to
what the packagers emit in published mode (so a re-render cannot silently
drift the committed catalog).
"""

from __future__ import annotations

import json
from pathlib import Path

from eawf.runtime.runtimes.claude.plugin_package import (
    PublishSource as ClaudePublishSource,
)
from eawf.runtime.runtimes.claude.plugin_package import (
    _read_pyproject_metadata,
)
from eawf.runtime.runtimes.claude.plugin_package import (
    _render_marketplace as _claude_render_marketplace,
)
from eawf.runtime.runtimes.codex.plugin_package import (
    PublishSource as CodexPublishSource,
)
from eawf.runtime.runtimes.codex.plugin_package import (
    _render_marketplace as _codex_render_marketplace,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLAUDE_POINTER = _REPO_ROOT / ".claude-plugin" / "marketplace.json"
_CODEX_POINTER = _REPO_ROOT / ".agents" / "plugins" / "marketplace.json"


def test_claude_pointer_exists_and_parses() -> None:
    """The committed Claude pointer is present and valid JSON."""
    assert _CLAUDE_POINTER.is_file()
    body = json.loads(_CLAUDE_POINTER.read_text(encoding="utf-8"))
    assert body["name"] == "eawf"
    assert isinstance(body["plugins"], list)


def test_codex_pointer_exists_and_parses() -> None:
    """The committed Codex pointer is present and valid JSON."""
    assert _CODEX_POINTER.is_file()
    body = json.loads(_CODEX_POINTER.read_text(encoding="utf-8"))
    assert body["name"] == "eawf"
    assert isinstance(body["plugins"], list)


def test_claude_pointer_source_kind_is_npm() -> None:
    """The committed Claude pointer declares an npm source."""
    body = json.loads(_CLAUDE_POINTER.read_text(encoding="utf-8"))
    source = body["plugins"][0]["source"]
    assert source == {"source": "npm", "package": "@elementarno/eawf"}


def test_codex_pointer_source_is_git_subdir() -> None:
    """The committed Codex pointer declares a git-subdir source with the
    expected url (from pyproject), path, and ref."""
    body = json.loads(_CODEX_POINTER.read_text(encoding="utf-8"))
    source = body["plugins"][0]["source"]
    assert source["source"] == "git-subdir"
    assert source["url"] == "https://github.com/Elementarno9/eawf"
    assert source["path"] == "./plugins/eawf"
    assert source["ref"] == "plugins-dist"
    # ref XOR sha — the declarative pointer pins a moving branch tip.
    assert "sha" not in source


def test_pointers_carry_no_pii() -> None:
    """Neither committed pointer leaks an email or machine path."""
    for pointer in (_CLAUDE_POINTER, _CODEX_POINTER):
        text = pointer.read_text(encoding="utf-8")
        # The scoped npm package name legitimately carries '@'; drop it before
        # the email-leak heuristic so a real address still trips the check.
        residual = text.replace("@elementarno/eawf", "")
        assert "@" not in residual, pointer
        assert "/Users/" not in text, pointer  # pragma: allowlist secret


def test_claude_pointer_matches_packager_published_output() -> None:
    """The committed Claude pointer is byte-identical to the packager's npm
    published-mode render — a drift here means the catalog is stale."""
    meta = _read_pyproject_metadata()
    expected = _claude_render_marketplace(
        author_name=meta["author_name"],
        publish_source=ClaudePublishSource.NPM,
    )
    assert _CLAUDE_POINTER.read_text(encoding="utf-8") == expected


def test_codex_pointer_matches_packager_published_output() -> None:
    """The committed Codex pointer is byte-identical to the packager's
    git-subdir published-mode render."""
    expected = _codex_render_marketplace(CodexPublishSource.GIT_SUBDIR).decode("utf-8")
    assert _CODEX_POINTER.read_text(encoding="utf-8") == expected
