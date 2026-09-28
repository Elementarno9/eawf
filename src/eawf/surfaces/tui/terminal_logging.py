"""Keep log records off the terminal while a Textual app owns the screen."""

from __future__ import annotations

import logging


def swap_root_logging_to_textual() -> list[logging.Handler]:
    """Detach terminal-bound root handlers, install a :class:`TextualHandler`.

    The CLI installs a :class:`logging.StreamHandler` on the root logger
    (:func:`eawf.surfaces.cli.app._configure_logging`) so non-TUI commands get
    scrubbed stderr logs. That handler keeps writing to the terminal after
    Textual owns the screen, corrupting the live TUI. This swaps the root
    logger for the duration of the Textual run: every root handler whose
    ``stream`` is :data:`sys.stderr` / :data:`sys.stdout` is removed, and a
    :class:`textual.logging.TextualHandler` (which routes to Textual's
    devtools console, never the screen) is installed in its place. The
    :class:`~eawf.observability.logging.scrub.SensitiveScrubber` is not needed on this
    path because the TextualHandler never reaches a terminal.

    Returns:
        The root logger's handler list as it was before the swap, so
        :func:`restore_root_logging` can reinstate it on app exit.
    """
    import sys

    from textual.logging import TextualHandler

    root = logging.getLogger()  # noqa: EAWF003 (root-logger handler config, not library acquisition)
    saved = list(root.handlers)
    terminal_streams = (sys.stderr, sys.stdout)
    for handler in saved:
        if isinstance(handler, logging.StreamHandler) and handler.stream in terminal_streams:
            root.removeHandler(handler)
    textual_handler = TextualHandler()
    # Timestamp the TUI-routed console log too (matching the CLI + daemon log
    # shape) so a line captured in the dev console carries its own clock.
    log_format = "%(asctime)s %(levelname)s %(name)s %(message)s"
    textual_handler.setFormatter(logging.Formatter(log_format))
    root.addHandler(textual_handler)
    return saved


def restore_root_logging(saved: list[logging.Handler]) -> None:
    """Restore the root logger handler list captured before the TUI swap.

    Removes every handler currently on the root logger (the
    :class:`TextualHandler` installed by :func:`swap_root_logging_to_textual`
    plus any survivors) and reinstates *saved* so the non-TUI CLI path keeps
    its scrubbed stderr sink once the Textual app has exited.

    Args:
        saved: The handler list returned by
            :func:`swap_root_logging_to_textual`.
    """
    root = logging.getLogger()  # noqa: EAWF003 (root-logger handler config, not library acquisition)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved:
        root.addHandler(handler)
