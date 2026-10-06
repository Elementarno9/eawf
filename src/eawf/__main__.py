"""Console entry point for ``eawf`` and ``python -m eawf``.

Claude Code runs ``eawf cc statusline`` on every redraw, and importing the
CLI app costs several hundred milliseconds. The plain statusline invocation
is therefore answered here without importing the app: a cached line is
printed at once, and a miss is rendered and cached exactly as the
``cc statusline`` command would. Every other invocation goes to the full CLI.
"""

from __future__ import annotations

import io
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

    A cache hit loads nothing of the render stack. A miss installs the CLI's
    scrubbed log sink and renders through the same cache-aware path the
    ``cc statusline`` command takes, so output, cache entry and stderr match
    it. A TTY stdin and Windows consoles go straight to the full CLI, which
    owns the stdin hint and the console encoding setup.
    """
    matched, theme = _statusline_theme(argv)
    if not matched or sys.platform == "win32" or sys.stdin.isatty():
        return False
    from eawf.runtime.runtimes.claude.statusline import run_with_cache, serve_cached_line

    raw = sys.stdin.read()
    line = serve_cached_line(raw, theme_name=theme)
    if line is None:
        from eawf.observability.logging.scrub import configure_cli_logging

        configure_cli_logging()
        sys.stdin = io.StringIO(raw)
        line = run_with_cache(workspace=None, theme_name=theme)
    sys.stdout.write(f"{line}\n")
    return True


def main() -> None:
    """Run the ``eawf`` command line for the current process arguments."""
    if _serve_statusline(sys.argv[1:]):
        return
    from eawf.surfaces.cli.app import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
