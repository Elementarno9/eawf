"""Unit tests for :mod:`eawf.cli.help_panels`.

Cover the boundary cases of the panel mapping helpers plus the import-time
guard that ties the panel set back to the metadata registry — a future tab
rename in :data:`eawf.config.registry.CONFIG_REGISTRY` must surface here.
"""

from __future__ import annotations

import importlib

import pytest

from eawf.cli import help_panels
from eawf.config.registry import tabs_sorted


def test_panel_order_matches_registry_tabs_sorted() -> None:
    """:data:`help_panels.PANEL_ORDER` mirrors :func:`tabs_sorted` exactly."""
    assert tabs_sorted() == help_panels.PANEL_ORDER


def test_panel_order_is_alphabetical() -> None:
    """Panel order is alphabetical by panel name (defensive — the registry
    accessor already sorts, but a future regression there would silently
    break the help layout)."""
    assert list(help_panels.PANEL_ORDER) == sorted(help_panels.PANEL_ORDER)


def test_every_assigned_panel_is_a_registry_tab() -> None:
    """The values of :data:`COMMAND_PANELS` are a subset of :data:`PANEL_ORDER`.

    Mirrors the assertion baked into module load — explicit test ensures
    the contract is exercised even when the module is already imported.
    """
    unknown = set(help_panels.COMMAND_PANELS.values()) - set(help_panels.PANEL_ORDER)
    assert not unknown, f"unknown panel(s) in COMMAND_PANELS: {sorted(unknown)}"


def test_panel_for_known_command_returns_panel() -> None:
    """Boundary: a registered command name resolves to its panel."""
    # ``audit`` (the command) lives in the ``audit`` panel.
    assert help_panels.panel_for("audit") == "audit"
    # ``wave`` lives in ``planning``.
    assert help_panels.panel_for("wave") == "planning"
    # ``tui`` lives in ``ui``.
    assert help_panels.panel_for("tui") == "ui"


def test_panel_for_unknown_command_returns_none() -> None:
    """Error / boundary: an unmapped command name resolves to ``None`` so
    the caller can fall through to Typer's default ``Commands`` panel."""
    assert help_panels.panel_for("nonexistent-cmd") is None
    assert help_panels.panel_for("") is None


def test_assert_panels_match_registry_rejects_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Error path: the module-load assertion fires when ``COMMAND_PANELS``
    references a panel name absent from :data:`PANEL_ORDER`.

    The test patches the module-local mapping to inject a bogus panel, then
    re-runs the guard. The real module is left untouched.
    """
    monkeypatch.setitem(help_panels.COMMAND_PANELS, "phony-cmd", "not-a-tab")
    with pytest.raises(AssertionError, match=r"unknown panel\(s\) in COMMAND_PANELS"):
        help_panels._assert_panels_match_registry()


def test_module_load_guard_passes_on_clean_import() -> None:
    """Re-importing the module under a clean monkeypatch state succeeds."""
    importlib.reload(help_panels)
    # Sanity: the public surface survives reload.
    assert "RegistryOrderedTyperGroup" in dir(help_panels)
    assert tabs_sorted() == help_panels.PANEL_ORDER


def test_registry_ordered_group_sorts_commands_by_panel_then_name() -> None:
    """The custom group orders commands by (panel, name) so Rich emits
    panel groups in alphabetical-by-panel order.

    The test builds a tiny Typer app with three commands across two panels
    and asserts the list_commands() result reproduces the expected order.
    """
    import typer

    app = typer.Typer(cls=help_panels.RegistryOrderedTyperGroup)

    # Register out of order to confirm the override re-sorts.
    @app.command(name="wave", rich_help_panel="planning")
    def _wave() -> None: ...

    @app.command(name="audit", rich_help_panel="audit")
    def _audit() -> None: ...

    @app.command(name="doctor", rich_help_panel="audit")
    def _doctor() -> None: ...

    # Click's CliRunner exposes the group instance via app's `_get_click_command`;
    # easier path: monkey-build the click group via Typer's main.get_command.
    from typer.main import get_command

    click_group = get_command(app)
    # Use a synthetic context to exercise list_commands.
    ctx = typer.Context(click_group)  # type: ignore[arg-type]
    names = click_group.list_commands(ctx)
    # Panel ``audit`` precedes panel ``planning`` alphabetically, and within
    # ``audit`` commands sort alphabetically (``audit`` before ``doctor``).
    assert names == ["audit", "doctor", "wave"]


def test_registry_ordered_group_pushes_unmapped_commands_last() -> None:
    """Commands without a panel assignment sort after every mapped panel.

    Uses the ``"~zzz"`` sentinel that compares greater than any real tab
    name to ensure the default ``Commands`` panel renders after the
    mapped panels.
    """
    import typer

    app = typer.Typer(cls=help_panels.RegistryOrderedTyperGroup)

    @app.command(name="zeta")
    def _zeta() -> None: ...

    @app.command(name="audit", rich_help_panel="audit")
    def _audit() -> None: ...

    from typer.main import get_command

    click_group = get_command(app)
    ctx = typer.Context(click_group)  # type: ignore[arg-type]
    names = click_group.list_commands(ctx)
    # ``audit`` (mapped) sorts before ``zeta`` (unmapped → sentinel).
    assert names == ["audit", "zeta"]
