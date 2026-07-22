"""Tests for the ``/theme`` swap verb + per-theme var migration (P26-I02-W02).

Covers the three deliverables of the runtime theme switch:

* **Swap** — ``/theme cb`` then ``/theme dark`` flips ``app.theme`` and
  re-resolves the migrated semantic vars (``$accent`` / ``$ok`` / ...),
  proving the global-scope → per-theme ``variables`` migration is live.
* **Persist** — the accepted choice writes through the layered-config
  writer (``_save_value_to_layer``), and a fresh launch reflects it.
* **Reject** — ``/theme bogus`` toasts a warning and leaves the theme
  unchanged.

The ``EaApp`` theme bootstrap (register + apply persisted) runs in
``__init__``, so a ``run_test`` mount already carries the dark baseline.
The persist-path tests redirect ``global_config_path`` to a tmp file and
force the in-process writer arm (``EAWF_DAEMONLESS=1``) so no real global
config is touched and no daemon is required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp, _persisted_glyphs, _persisted_theme
from eawf.surfaces.tui.palette.verbs import _handle_theme
from eawf.surfaces.tui.theme import EA_CB, EA_DARK, EA_LIGHT, resolve_theme_name

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


@pytest.fixture
def _tmp_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``global_config_path`` to a tmp file + force in-process writes.

    Patches :func:`eawf.kernel.config.layered.global_config_path` (the canonical
    definition both the ``merge_config`` reader and the writer's
    layer-label matcher resolve at call time) and sets
    ``EAWF_DAEMONLESS=1`` so the writer takes the lock-read-write arm with
    no daemon. Returns the tmp config path the global layer now resolves
    to.
    """
    cfg = tmp_path / "config" / "eawf" / "config.yaml"
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setattr("eawf.kernel.config.layered.global_config_path", lambda: cfg)
    return cfg


# --------------------------------------------------------------------------
# resolve_theme_name — logical → registered mapping + auto + unknown
# --------------------------------------------------------------------------


def test_resolve_theme_name_known_logicals_map_to_registered() -> None:
    assert resolve_theme_name("dark") == EA_DARK.name
    assert resolve_theme_name("cb") == EA_CB.name
    assert resolve_theme_name("light") == EA_LIGHT.name


def test_resolve_theme_name_auto_resolves_to_dark_baseline() -> None:
    # No synchronous terminal-background query is available, so auto is the
    # honest best-effort dark baseline.
    assert resolve_theme_name("auto") == EA_DARK.name


def test_resolve_theme_name_unknown_returns_none() -> None:
    assert resolve_theme_name("bogus") is None
    assert resolve_theme_name("") is None


# --------------------------------------------------------------------------
# Theme.variables carry the full semantic set (migration target)
# --------------------------------------------------------------------------


def test_dark_theme_ports_the_wong_palette_with_green_accent() -> None:
    """The dark theme keeps the Wong lifecycle tints; only accent/primary rotate.

    The cosmic-terminal reskin (P30-I02-W01) rotates ``accent`` / ``primary``
    teal -> green; every lifecycle ``status-*`` tint and the ok/warn/err
    bands stay at their exact pre-migration hex. ``status-claimed`` keeps the
    cool teal ``#56b6c2`` so it reads distinct from the green accent and the
    green ``status-closed``.
    """
    variables = EA_DARK.variables
    assert variables["accent"] == "#16b384"
    assert variables["primary"] == "#16b384"
    assert variables["ok"] == "#009e73"
    assert variables["warn"] == "#e69f00"
    assert variables["err"] == "#d55e00"
    assert variables["muted"] == "#6c6c6c"
    assert variables["status-pending"] == "#6c6c6c"
    assert variables["status-claimed"] == "#56b6c2"
    assert variables["status-in-progress"] == "#e69f00"
    assert variables["status-closed"] == "#009e73"
    assert variables["status-failed"] == "#d55e00"


def test_cb_theme_is_a_distinct_palette_with_the_same_var_names() -> None:
    """The cb palette is visually distinct yet covers every semantic var."""
    assert set(EA_CB.variables) == set(EA_DARK.variables)
    # Distinct accent / error / muted so a swap is observable.
    assert EA_CB.variables["accent"] != EA_DARK.variables["accent"]
    assert EA_CB.variables["err"] != EA_DARK.variables["err"]
    assert EA_CB.variables["status-in-progress"] != EA_DARK.variables["status-in-progress"]


def test_every_ea_theme_covers_the_full_semantic_var_set() -> None:
    """Each registered theme must define every var the structural CSS uses."""
    required = set(EA_DARK.variables)
    for theme in (EA_DARK, EA_CB, EA_LIGHT):
        missing = required - set(theme.variables)
        assert not missing, f"{theme.name} missing semantic vars: {sorted(missing)}"


# --------------------------------------------------------------------------
# apply_theme — the swap mechanism on the live App
# --------------------------------------------------------------------------


def test_apply_theme_swap_changes_theme_and_resolved_variables() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # Baseline: the dark Wong palette.
            assert app.theme == EA_DARK.name
            dark_accent = app.get_css_variables()["accent"]
            # Swap to the colour-blind-safe palette.
            assert app.apply_theme("cb") is True
            await pilot.pause()
            assert app.theme == EA_CB.name
            cb_accent = app.get_css_variables()["accent"]
            assert cb_accent != dark_accent
            assert app.get_css_variables()["ok"] == EA_CB.variables["ok"]
            # Swap back to dark — the variables revert.
            assert app.apply_theme("dark") is True
            await pilot.pause()
            assert app.theme == EA_DARK.name
            assert app.get_css_variables()["accent"] == dark_accent
            assert app.get_css_variables()["ok"] == EA_DARK.variables["ok"]

    asyncio.run(body())


def test_apply_theme_unknown_name_leaves_theme_unchanged() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            before = app.theme
            assert app.apply_theme("bogus") is False
            await pilot.pause()
            assert app.theme == before

    asyncio.run(body())


# --------------------------------------------------------------------------
# /theme verb — swap + persist + reject
# --------------------------------------------------------------------------


def test_handle_theme_verb_swaps_cb_then_dark(_tmp_global_config: Path) -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            _handle_theme(app, "cb")
            await pilot.pause()
            assert app.theme == EA_CB.name
            _handle_theme(app, "dark")
            await pilot.pause()
            assert app.theme == EA_DARK.name

    asyncio.run(body())


def test_handle_theme_verb_rejects_unknown_with_warning_no_change(
    _tmp_global_config: Path,
) -> None:
    async def body() -> None:
        notices: list[tuple[str, str | None]] = []
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            before = app.theme
            app.notify = lambda message, *_a, **kw: notices.append(  # type: ignore[method-assign]
                (message, kw.get("severity"))
            )
            _handle_theme(app, "bogus")
            await pilot.pause()
            assert app.theme == before
            assert notices, "rejection must toast"
            message, severity = notices[-1]
            assert severity == "warning"
            assert "bogus" in message

    asyncio.run(body())


def test_handle_theme_verb_persists_choice_through_layer_writer(
    _tmp_global_config: Path,
) -> None:
    """``/theme cb`` writes ``ui.theme`` to the global layer; reload reflects it."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            _handle_theme(app, "cb")
            await pilot.pause()

    asyncio.run(body())
    # The on-disk global layer now carries the choice, and a fresh read
    # through the same merge path the next launch uses reflects it.
    assert _tmp_global_config.is_file()
    assert _persisted_theme() == "cb"


def test_persisted_theme_round_trips_through_save_value_to_layer(
    _tmp_global_config: Path,
) -> None:
    """Direct round-trip: ``_save_value_to_layer`` write → ``_persisted_theme`` read."""
    from eawf.surfaces.cli.commands.config import _save_value_to_layer

    # Default before any persist falls back to the dark baseline.
    assert _persisted_theme() == "dark"
    _save_value_to_layer(target_path=_tmp_global_config, key="ui.theme", value="light")
    assert _persisted_theme() == "light"


def test_persisted_theme_ignores_unrecognised_persisted_value(
    _tmp_global_config: Path,
) -> None:
    """A garbage persisted value degrades to the dark baseline, not a crash."""
    from eawf.surfaces.cli.commands.config import _save_value_to_layer

    _save_value_to_layer(target_path=_tmp_global_config, key="ui.theme", value="not-a-theme")
    assert _persisted_theme() == "dark"


def test_persisted_ui_consumers_honor_explicit_repo_anchor(
    tmp_path: Path, _tmp_global_config: Path
) -> None:
    repo = tmp_path / "repo"
    config_path = repo / ".ea" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "ui:\n  glyphs: ascii\n  theme: light\n",
        encoding="utf-8",
    )

    assert _persisted_glyphs(repo) == "ascii"
    assert _persisted_theme(repo) == "light"


# --------------------------------------------------------------------------
# ui.theme config registry rows (menu surface + leaf-key catalog)
# --------------------------------------------------------------------------


def test_ui_theme_in_config_registry_as_choice_of_four() -> None:
    """The menu-surface row is a ``ui``-tab choice over the four logical names."""
    from eawf.kernel.config.registry import registry_lookup

    entry = registry_lookup("ui.theme")
    assert entry is not None
    assert entry.tab == "ui"
    assert entry.type == "choice"
    assert entry.default == "dark"
    assert entry.choices == ("dark", "light", "cb", "auto")


def test_ui_theme_in_leaf_key_registry_like_ui_color() -> None:
    """The leaf-key row mirrors ``ui.color``'s writable layers (global/ws/repo/env)."""
    from eawf.kernel.config.registry import leaf_key_lookup

    entry = leaf_key_lookup("ui.theme")
    assert entry.domain == "ui"
    assert entry.type == "literal"
    assert entry.default == "dark"
    assert entry.writable_layers == ("global", "workspace", "repo", "env")
    assert entry.choices == ("dark", "light", "cb", "auto")
