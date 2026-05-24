"""Shared themed-App base for the widget-render harness tests.

Since the semantic palette vars (``$accent`` / ``$ok`` / ``$warn`` /
``$err`` / ``$muted`` / ``$status-*``) moved out of ``theme.tcss`` global
scope and into each :class:`~textual.theme.Theme`'s ``variables`` map
(the runtime ``/theme`` swap migration), any host App that mounts a
``tui`` widget MUST have an Eä theme active for those ``$var``\\ s to
resolve at stylesheet-parse time — exactly as :class:`eawf.surfaces.tui.app.EaApp`
does. The widget tests host their widget in a bare ``App`` subclass, so
this base wires the same theme registration + default-apply they need.

Registration and the default-theme apply happen in ``__init__`` (not
``on_mount``): Textual builds the App stylesheet from
``get_css_variables()`` before ``on_mount`` runs, and a theme's
``variables`` only enter that namespace once the theme is the active one.
"""

from __future__ import annotations

from textual.app import App

from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES


class PaletteHarnessApp(App[None]):
    """Bare host App with the Eä themes registered + the dark one active.

    Mirrors :class:`eawf.surfaces.tui.app.EaApp`'s theme bootstrap so a widget
    mounted in a test harness resolves every semantic ``$var`` its
    ``DEFAULT_CSS`` (and the shared ``theme.tcss``) references.
    """

    def __init__(self) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
