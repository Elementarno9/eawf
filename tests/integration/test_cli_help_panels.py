"""Unit tests for :mod:`eawf.surfaces.cli.help_panels`.

Cover the boundary cases of the panel mapping helpers and the derived
alphabetical ordering. :data:`PANEL_ORDER` is resolved lazily through the
module's PEP 562 ``__getattr__``.
"""

from __future__ import annotations

import importlib

import pytest

from eawf.surfaces.cli import help_panels


def test_panel_order_matches_assigned_command_panels() -> None:
    """:data:`help_panels.PANEL_ORDER` covers every assigned command panel."""
    assert tuple(sorted(set(help_panels.COMMAND_PANELS.values()))) == help_panels.PANEL_ORDER


def test_panel_order_is_alphabetical() -> None:
    """Panel order is alphabetical by panel name (defensive — the registry
    accessor already sorts, but a future regression there would silently
    break the help layout)."""
    assert list(help_panels.PANEL_ORDER) == sorted(help_panels.PANEL_ORDER)


def test_command_panels_are_nonempty_labels() -> None:
    """Every command-panel assignment carries a renderable label."""
    assert all(panel.strip() for panel in help_panels.COMMAND_PANELS.values())


def test_module_getattr_rejects_unknown_attribute() -> None:
    """Error path: the PEP 562 ``__getattr__`` only resolves ``PANEL_ORDER``;
    any other missing attribute raises ``AttributeError`` so typos fail
    loud rather than silently returning ``None``."""
    with pytest.raises(AttributeError, match="no attribute 'does_not_exist'"):
        help_panels.does_not_exist  # type: ignore[attr-defined]  # noqa: B018


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


def test_command_panels_subset_check_catches_injected_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newly assigned panel enters the derived order deterministically."""
    monkeypatch.setitem(help_panels.COMMAND_PANELS, "phony-cmd", "not-a-tab")
    help_panels._panel_order.cache_clear()
    assert "not-a-tab" in help_panels.PANEL_ORDER


def test_panel_order_resolves_lazily_and_survives_reload() -> None:
    """``PANEL_ORDER`` resolves through PEP 562 ``__getattr__`` and survives
    a module reload — the public surface stays intact after the eager
    module-level binding was replaced by a lazy accessor."""
    importlib.reload(help_panels)
    # Sanity: the public surface survives reload.
    assert "RegistryOrderedTyperGroup" in dir(help_panels)
    assert tuple(sorted(set(help_panels.COMMAND_PANELS.values()))) == help_panels.PANEL_ORDER


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
