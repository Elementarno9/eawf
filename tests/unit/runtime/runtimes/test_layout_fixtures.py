"""One active render per runtime: pinned host layouts, the gate and injected roots.

Each runtime's detector is proven against a fixture reproducing that host's
real directory shape, the conflict gate is driven through those detectors
over the normalized runtime set, and every detector and renderer is run with
the real home made unreachable and a decoy install behind every environment
override, so a path resolved outside the injected root fails the test on the
escape itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.plugin_conflict import detect_marketplace_install
from eawf.runtime.runtimes.claude.plugin_install import install_plugin as install_claude
from eawf.runtime.runtimes.codex.plugin_conflict import (
    detect_user_install as detect_codex_install,
)
from eawf.runtime.runtimes.codex.plugin_install import install_plugin as install_codex
from eawf.runtime.runtimes.manifest import RuntimeId
from eawf.runtime.runtimes.opencode.plugin_conflict import (
    detect_user_install as detect_opencode_install,
)
from eawf.runtime.runtimes.opencode.plugin_install import install_plugin as install_opencode
from eawf.runtime.runtimes.runtime_set import (
    ALL_RUNTIMES,
    ClaimDetector,
    RuntimeSetConflictError,
    guard_runtime_set,
    normalize_runtime_set,
)

_CLAUDE_VERSION = "0.7.0"


def _claude_layout(home: Path) -> Path:
    """Pin the tree a Claude Code marketplace install writes, manifest included."""
    plugins = home / ".claude" / "plugins"
    for name in ("cache", "data", "marketplaces", "npm-cache"):
        (plugins / name).mkdir(parents=True, exist_ok=True)
    install = plugins / "cache" / "eawf" / "eawf" / _CLAUDE_VERSION
    (install / ".claude-plugin").mkdir(parents=True)
    (install / ".claude-plugin" / "plugin.json").write_text('{"name": "eawf"}\n', "utf-8")
    (plugins / "marketplaces" / "eawf").mkdir()
    manifest = {
        "version": 2,
        "plugins": {
            "eawf@eawf": [
                {"scope": "user", "installPath": str(install), "version": _CLAUDE_VERSION}
            ]
        },
    }
    (plugins / "installed_plugins.json").write_text(json.dumps(manifest), "utf-8")
    return install


def _codex_layout(home: Path) -> Path:
    """Pin the tree a user-scope Codex install writes beside its config."""
    plugin = home / ".codex" / "plugins" / "eawf"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text('{"name": "eawf"}\n', "utf-8")
    (home / ".codex" / "config.toml").write_text("[plugins.eawf]\nenabled = true\n", "utf-8")
    return plugin


def _opencode_layout(home: Path) -> Path:
    """Pin the XDG tree a user-scope OpenCode install writes."""
    plugins = home / ".config" / "opencode" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "eawf.js").write_text("export default {}\n", "utf-8")
    return plugins / "eawf.js"


def _detectors(home: Path) -> dict[RuntimeId, ClaimDetector]:
    """Bind each runtime's real detector to the injected *home*."""

    def _claude() -> Path | None:
        found = detect_marketplace_install(home=home)
        return None if found is None else found.plugin_dir

    def _codex() -> Path | None:
        found = detect_codex_install(home=home)
        return None if found is None else found.plugin_dir

    def _opencode() -> Path | None:
        found = detect_opencode_install(home=home)
        return None if found is None else found.plugin_file

    return {"claude-code": _claude, "codex": _codex, "opencode": _opencode}


@pytest.fixture
def decoy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Put a full install behind every home override and make the real home fail.

    Yields:
        The decoy root; nothing a test does may read or write beneath it.
    """
    root = tmp_path / "decoy"
    _claude_layout(root)
    _codex_layout(root)
    _opencode_layout(root)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("CODEX_HOME", str(root / ".codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / ".config"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(root / ".config" / "opencode"))

    def _no_real_home() -> Path:
        raise AssertionError("resolved the real home instead of the injected root")

    monkeypatch.setattr(Path, "home", staticmethod(_no_real_home))
    before = sorted(str(path) for path in root.rglob("*"))
    yield root
    assert sorted(str(path) for path in root.rglob("*")) == before, "wrote beneath the decoy"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


# ---- pinned layout per runtime ----------------------------------------------


def test_surf_169_claude_layout_fixture_is_detected(home: Path, decoy: Path) -> None:
    install = _claude_layout(home)
    found = detect_marketplace_install(home=home)
    assert found is not None
    assert found.plugin_dir == install


def test_surf_169_codex_layout_fixture_is_detected(home: Path, decoy: Path) -> None:
    plugin = _codex_layout(home)
    found = detect_codex_install(home=home)
    assert found is not None
    assert found.plugin_dir == plugin


def test_surf_169_opencode_layout_fixture_is_detected(home: Path, decoy: Path) -> None:
    plugin_file = _opencode_layout(home)
    found = detect_opencode_install(home=home)
    assert found is not None
    assert found.plugin_file == plugin_file


@pytest.mark.parametrize("runtime", ALL_RUNTIMES)
def test_surf_169_empty_injected_home_detects_nothing_despite_a_decoy(
    home: Path, decoy: Path, runtime: RuntimeId
) -> None:
    assert _detectors(home)[runtime]() is None


def test_surf_169_explicit_opencode_config_dir_still_wins(home: Path, decoy: Path) -> None:
    plugin_file = _opencode_layout(home)
    elsewhere = home / "elsewhere"
    assert detect_opencode_install(home=home, opencode_config_dir=str(elsewhere)) is None
    found = detect_opencode_install(home=home, opencode_config_dir=str(plugin_file.parent.parent))
    assert found is not None
    assert found.plugin_file == plugin_file


# ---- the gate over the normalized, fully expanded set ------------------------


@pytest.mark.parametrize(
    ("layout", "runtime"),
    [(_claude_layout, "claude-code"), (_codex_layout, "codex"), (_opencode_layout, "opencode")],
)
def test_surf_169_bare_invocation_fires_for_each_claimed_runtime(
    home: Path, decoy: Path, layout: Callable[[Path], Path], runtime: RuntimeId
) -> None:
    layout(home)
    with pytest.raises(RuntimeSetConflictError) as caught:
        guard_runtime_set(
            normalize_runtime_set([]), scope="project", detectors=_detectors(home), force=False
        )
    assert [claim.runtime for claim in caught.value.claims] == [runtime]


def test_surf_169_non_canonical_spelling_fires(home: Path, decoy: Path) -> None:
    install = _claude_layout(home)
    with pytest.raises(RuntimeSetConflictError) as caught:
        guard_runtime_set(
            normalize_runtime_set(["claude"]),
            scope="project",
            detectors=_detectors(home),
            force=False,
        )
    assert caught.value.claims[0].install_path == install


def test_surf_169_no_install_renders_without_a_claim(home: Path, decoy: Path) -> None:
    runtimes = normalize_runtime_set([])
    assert (
        guard_runtime_set(runtimes, scope="project", detectors=_detectors(home), force=False) == ()
    )


# ---- renderers stay inside their injected roots ------------------------------


def test_surf_169_renderers_write_only_under_the_injected_roots(
    tmp_path: Path, home: Path, decoy: Path
) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    install_claude(target)
    install_codex(target, home=home)
    install_opencode(target, home=home)
    install_opencode(target, scope="user", home=home)
    assert (home / ".config" / "opencode" / "plugins" / "eawf.js").is_file()
