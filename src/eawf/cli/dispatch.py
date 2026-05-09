"""Sub-Typer registration helpers shared by all wave handler modules.

Each P02 wave (``cli/commands/<area>.py``) constructs its own
:class:`typer.Typer` group and registers it with the root app via
:func:`register_subcommand`. Keeping this in one place lets later waves stay
mechanical: import the helper, call it once at module load.
"""

from __future__ import annotations

import typer


def register_subcommand(parent: typer.Typer, name: str, group: typer.Typer) -> None:
    """Mount *group* under *parent* at *name*.

    Args:
        parent: The root or intermediate Typer app.
        name: The subcommand name as users will invoke it (e.g. ``"phase"``).
        group: The Typer instance carrying the per-area handlers.
    """
    parent.add_typer(group, name=name)
