"""Config-modal writable-layers lock snapshot + affordance parity.

The config modal surfaces every operator-tunable
:data:`~eawf.kernel.config.registry.CONFIG_REGISTRY` key, but a key is
writable only on the layers its leaf-catalog row declares. The active catalog
retains ``runtime.default`` but excludes the deprecated
``runtime.adapter_catalog.*.enabled`` leaves. It also includes
global/workspace/repo-writable budget keys. This module pins both the catalog
removal and the per-layer read-only lock.

This module pins both criteria of the wave:

* **CR-01 (snapshot)** -- the runtime tab contains only ``runtime.default``;
  deprecated adapter keys never reappear. Runtime and budget keys show a
  trailing ``(read-only)`` marker on layers their writable lock forbids.
* **CR-02 (affordance parity)** -- the advertised ``c`` config key resolves
  to a live :class:`~textual.binding.Binding` that opens the config modal
  (driven through the real key->Binding probe + a Pilot keypress).

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_config_modal_lock.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.config_modal import ConfigModal
from eawf.surfaces.tui.screens.overlays.config_modal_logic import is_editable_on_layer
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.snapshot.behaviour_probe import ProbeStatus, record_keypress_transcript
from eawf.surfaces.tui.widgets.git_pane import GitFields

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"
_COMMIT = "config-modal-lock-test"
_CONFIG_KEY = "c"

#: Deprecated adapter keys must stay absent from the operator registry.
_DEPRECATED_ADAPTER_KEYS: tuple[str, ...] = (
    "runtime.adapter_catalog.claude.enabled",
    "runtime.adapter_catalog.codex.enabled",
    "runtime.adapter_catalog.opencode.enabled",
)
_RUNTIME_DEFAULT = "runtime.default"
_BUDGET_KEYS: tuple[str, ...] = (
    "flow.budget.enforce",
    "flow.budget.multiplier",
)


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every git probe so the rendered chrome is deterministic."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )
    monkeypatch.setattr("eawf.surfaces.tui.widgets.git_pane._git_run", lambda *a, **k: None)


def _goto_tab(modal: ConfigModal, tab: str) -> None:
    """Activate *tab* and clear focus the way the modal's own switch does."""
    from textual.widgets import TabbedContent

    modal.query_one("#config-tabs", TabbedContent).active = modal._tab_pane_id(tab)
    modal.set_focus(None)
    modal.field_index = 0


# --------------------------------------------------------------------------
# Pure helper: is_editable_on_layer is the per-layer lock the modal reads
# --------------------------------------------------------------------------


def test_deprecated_adapter_keys_absent_from_operator_registry() -> None:
    from eawf.kernel.config.registry import LEAF_KEY_REGISTRY, registry_lookup

    for key in _DEPRECATED_ADAPTER_KEYS:
        assert registry_lookup(key) is None
        leaf = LEAF_KEY_REGISTRY[key]
        assert leaf.reserved is True
        assert leaf.consumer_kind == "deprecated"


def test_budget_keys_locked_off_durable_non_gwr_layer() -> None:
    # The budget keys are writable global/workspace/repo, so they are read-only
    # on the local layer (which is durable but outside their writable set).
    from eawf.kernel.config.registry import registry_lookup

    for key in _BUDGET_KEYS:
        entry = registry_lookup(key)
        assert entry is not None
        assert is_editable_on_layer(entry, "global")
        assert is_editable_on_layer(entry, "repo")
        assert not is_editable_on_layer(entry, "local")


def test_is_editable_on_layer_missing_leaf_is_editable() -> None:
    # error-path: a curated key with no leaf-catalog row is treated as
    # editable on every layer (the leaf gate is the lock authority; a missing
    # row is a registry-consistency bug, not a lock).
    from eawf.kernel.config.registry import ConfigKey

    orphan = ConfigKey(tab="t", key="t.no_leaf_row", label="x", type="bool", default=False)
    assert is_editable_on_layer(orphan, "global")
    assert is_editable_on_layer(orphan, "repo")


# --------------------------------------------------------------------------
# CR-01: the config modal shows the new keys read-only on locked layers
# --------------------------------------------------------------------------


def _mount_and_capture(tab: str, layer: str, tmp_repo: Path) -> str:
    """Mount the config modal, select *layer* + *tab*, return the frame."""

    async def body() -> str:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = ConfigModal(workspace=None, repo=tmp_repo)
            app.push_screen(modal)
            await settle_screen(pilot)
            modal._view.layer = layer
            _goto_tab(modal, tab)
            modal._repaint_fields()
            await settle_screen(pilot)
            return normalize_snapshot(capture_screen_text(app))

    return asyncio.run(body())


def test_config_modal_runtime_tab_lock_snapshot(tmp_path: Path) -> None:
    # CR-01 golden: the runtime tab on the local layer contains only the
    # active runtime.default row, rendered read-only.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = ConfigModal(workspace=None, repo=tmp_path)
            app.push_screen(modal)
            await settle_screen(pilot)
            modal._view.layer = "local"
            _goto_tab(modal, "runtime")
            modal._repaint_fields()
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert _RUNTIME_DEFAULT in frame
            assert "<claude-code>" in frame
            for key in _DEPRECATED_ADAPTER_KEYS:
                assert key not in frame
            assert "(read-only)" in frame
            assert_screen_snapshot(app, _GOLDEN / "config_modal_runtime_lock.txt")

    asyncio.run(body())


def test_deprecated_adapter_keys_never_render(tmp_path: Path) -> None:
    for layer in ("global", "repo", "local"):
        frame = _mount_and_capture("runtime", layer, tmp_path)
        assert _RUNTIME_DEFAULT in frame
        for key in _DEPRECATED_ADAPTER_KEYS:
            assert key not in frame


def test_budget_keys_render_read_only_off_writable_layers(tmp_path: Path) -> None:
    # The budget keys (global/workspace/repo-writable) render read-only on the
    # local layer and editable on global + repo -- pinned per cell.
    local_frame = _mount_and_capture("flow", "local", tmp_path)
    for key in _BUDGET_KEYS:
        row = next(line for line in local_frame.splitlines() if key in line)
        assert "(read-only)" in row, f"{key} not read-only on local: {row!r}"

    for writable_layer in ("global", "repo"):
        frame = _mount_and_capture("flow", writable_layer, tmp_path)
        for key in _BUDGET_KEYS:
            row = next(line for line in frame.splitlines() if key in line)
            assert "(read-only)" not in row, (
                f"{key} unexpectedly locked on {writable_layer}: {row!r}"
            )


def test_locked_key_edit_is_no_op(tmp_path: Path) -> None:
    # error-path: pressing Enter on a read-only key (locked on the active
    # layer) stages NO edit -- the lock blocks the mutator, not just the
    # rendering.
    async def body() -> bool:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = ConfigModal(workspace=None, repo=tmp_path)
            app.push_screen(modal)
            await settle_screen(pilot)
            modal._view.layer = "local"
            _goto_tab(modal, "runtime")
            modal._repaint_fields()
            await settle_screen(pilot)
            modal.field_index = next(
                index
                for index, entry in enumerate(modal._active_fields())
                if entry.key == _RUNTIME_DEFAULT
            )
            modal.action_edit()
            await settle_screen(pilot)
            return bool(modal._view.dirty)

    assert asyncio.run(body()) is False


# --------------------------------------------------------------------------
# CR-02: the advertised `c` config key resolves + opens the config modal
# --------------------------------------------------------------------------


def test_config_key_resolves_in_home_mode() -> None:
    # The advertised ``c`` config key resolves to a live binding in the home
    # mode (NOT unresolved) -- the affordance the footer promises is live.
    async def body() -> ProbeStatus:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await app.switch_mode("home")
            await settle_screen(pilot)
            transcript = await record_keypress_transcript(
                pilot, [_CONFIG_KEY], source_commit=_COMMIT
            )
            return transcript.outcomes[0].status

    status = asyncio.run(body())
    assert status is ProbeStatus.OBSERVABLE
    assert status is not ProbeStatus.UNRESOLVED


def test_config_key_opens_config_modal() -> None:
    # Pressing ``c`` in the home mode opens the ConfigModal (the advertised
    # affordance does what the footer promises).
    async def body() -> bool:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await app.switch_mode("home")
            await settle_screen(pilot)
            depth_before = app.modal_depth()
            await pilot.press(_CONFIG_KEY)
            await settle_screen(pilot)
            return app.modal_depth() == depth_before + 1 and isinstance(app.screen, ConfigModal)

    assert asyncio.run(body()) is True
