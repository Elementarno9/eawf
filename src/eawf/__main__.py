"""Console entry point for ``eawf`` and ``python -m eawf``.

Claude Code runs ``eawf cc statusline`` on every redraw, and importing the
CLI app costs several hundred milliseconds. The plain statusline invocation
is therefore answered here without importing the app, rendered and cached
exactly as the ``cc statusline`` command would. A bare ``--version`` is
answered here too. Every other invocation goes to the full CLI.
"""

from __future__ import annotations

import os
import sys


def _statusline_theme(argv: list[str]) -> tuple[bool, str | None]:
    """Match the plain statusline invocation in *argv*.

    Returns:
        ``(True, theme)`` for ``cc statusline`` with at most a ``--theme``
        option (``theme`` is ``None`` without it), ``(False, None)`` for
        anything else, which the full CLI parses.
    """
    if argv[:2] != ["cc", "statusline"]:
        return False, None
    match argv[2:]:
        case []:
            return True, None
        case ["--theme", theme]:
            return True, theme
        case [option] if option.startswith("--theme="):
            return True, option.removeprefix("--theme=")
        case _:
            return False, None


def _serve_statusline(argv: list[str]) -> bool:
    """Print the statusline for *argv* and return ``True`` when it was served.

    It installs the CLI's scrubbed log sink and renders through the same
    budgeted path the ``cc statusline`` command takes, so output, cache entry
    and stderr match it. When that path printed the cached line because the
    render overran, the process exits at once: the host waits for it to end,
    and the abandoned render is a daemon thread that may be mid-import, which
    interpreter shutdown should not wait on. A TTY stdin and Windows consoles
    go straight to the full CLI, which owns the stdin hint and the console
    encoding setup.
    """
    matched, theme = _statusline_theme(argv)
    if not matched or sys.platform == "win32" or sys.stdin.isatty():
        return False
    from eawf.observability.logging.scrub import configure_cli_logging
    from eawf.runtime.runtimes.claude.statusline import read_payload, serve_line

    configure_cli_logging()
    served = serve_line(read_payload(), workspace=None, theme_name=theme)
    sys.stdout.write(f"{served.text}\n")
    if served.render_running:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return True


def _serve_version(argv: list[str]) -> bool:
    """Print the version for a bare ``eawf --version`` and return ``True`` when served.

    The CLI app's import pulls Typer, the error models and Pydantic, which a
    version probe never needs. Any other argument alongside the flag goes to
    the full CLI, whose eager option prints the same line.
    """
    if argv != ["--version"]:
        return False
    from eawf.surfaces.cli._version_display import compose_display_version

    sys.stdout.write(f"{compose_display_version()}\n")
    return True


def main() -> None:
    """Run the ``eawf`` command line for the current process arguments."""
    if _serve_version(sys.argv[1:]) or _serve_statusline(sys.argv[1:]):
        return
    from eawf.surfaces.cli.app import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
