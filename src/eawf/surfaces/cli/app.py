"""Typer dispatcher for ``eawf`` with global flags and version banner.

The root callback parses the global flags (``--json``, ``--plain``,
``--no-input``, ``-w/--workspace``) and stores a
:class:`eawf.surfaces.cli.flags.GlobalFlags` on ``ctx.obj``. Every subcommand pulls the
dataclass back out via :attr:`typer.Context.obj` to drive its emission and
output choices. ``--scope`` is intentionally *not* hoisted to the root — see
:mod:`eawf.surfaces.cli.flags` for the rationale.

The bare invocation (``eawf`` with no subcommand) prints the version banner —
this matches the Phase 1 behaviour and avoids breaking the validate test
suite. ``--version`` short-circuits via the eager callback and exits.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from eawf.surfaces.cli._version_display import compose_display_version
from eawf.surfaces.cli.flag_day_gate import FlagDayTyperGroup
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.help_panels import panel_for
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.registry import register_commands

if TYPE_CHECKING:
    from eawf.surfaces.tui.console.operations import Operator

app = typer.Typer(
    name="eawf",
    help="Eä Workflow — agent-driven development framework.",
    no_args_is_help=False,
    add_completion=False,
    cls=FlagDayTyperGroup,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(compose_display_version())
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON output."),
    ] = False,
    plain_output: Annotated[
        bool,
        typer.Option("--plain", help="Disable colour and Rich markup."),
    ] = False,
    no_input: Annotated[
        bool,
        typer.Option("--no-input", help="Fail closed instead of prompting the user."),
    ] = False,
    workspace: Annotated[
        Path | None,
        typer.Option("-w", "--workspace", help="Workspace root used to locate .ea/state.json."),
    ] = None,
    daemonless: Annotated[
        bool,
        typer.Option(
            "--daemonless",
            help=(
                "Bypass the daemon (V1 carve-out: CI / one-shot / recovery). "
                "Read-only verbs read state directly; mutating verbs reject it."
            ),
        ),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Enable dev-mode surfaces (raw `state rpc`, hidden daemon verbs).",
        ),
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the eawf version and exit.",
        ),
    ] = False,
) -> None:
    """Populate ``ctx.obj`` with resolved global flags and emit the banner."""
    ctx.obj = GlobalFlags(
        json_output=json_output,
        plain_output=plain_output,
        no_input=no_input,
        workspace=workspace,
        daemonless=daemonless,
        debug=debug,
    )
    # Record the --daemonless flag process-wide so the shared
    # state_transaction chokepoint can reject mutating verbs without
    # each command threading the flag through. Always set (incl. False)
    # so the record reflects only this invocation.
    from eawf.surfaces.cli._mutation import set_daemonless_flag

    set_daemonless_flag(daemonless)
    if ctx.invoked_subcommand is None:
        # Bare ``eawf`` on a TTY opens the console; plain / no-input /
        # non-TTY writes the console's plain frame so headless callers
        # stay script-stable.
        rc = _dispatch_tui(workspace=workspace, no_input=no_input, plain=plain_output)
        raise typer.Exit(code=rc)


def _dispatch_tui(
    *,
    workspace: Path | None,
    no_input: bool,
    plain: bool,
    verbose: bool = False,
    operator: Operator | None = None,
) -> int:
    """Resolve the launch scope and open the console.

    On an interactive TTY this resolves the tree's authority epoch
    (:func:`eawf.kernel.state.epoch2.authority.resolve_authority`) and opens the
    epoch-2 console: over a live projection seam for a tree it attaches to, else on
    the pre-session entry state the attach path landed in (an epoch-1 tree lands in
    migration-required), so the operator reads the exact next command. When
    ``--plain`` / ``--no-input`` is set or stdout is not a TTY it writes the
    console's own frame in plain mode instead, except a terminal entry state, which
    exits ``4`` (SURF-085) with its commands on stderr. See
    :mod:`eawf.surfaces.tui.launch` for the full decision.

    Args:
        workspace: Optional workspace root from ``-w/--workspace``.
        no_input: Fail-closed flag — writes the plain frame instead of the app.
        plain: Plain-output flag — writes the plain frame instead of the app.
        verbose: Whether the console's key-trace row is shown (SURF-173).
        operator: Who the console's writes are attributed to; ``None``
            leaves every writing verb refused with that reason.

    Returns:
        Process exit code (``0`` on a clean quit).
    """
    from eawf.surfaces.tui.launch import launch_tui

    return launch_tui(
        workspace=workspace, no_input=no_input, plain=plain, verbose=verbose, operator=operator
    )


@app.command(name="version", rich_help_panel=panel_for("version"))
def version_cmd(ctx: typer.Context) -> None:
    """Show the eawf version (text or JSON envelope)."""
    flags: GlobalFlags = ctx.obj
    display = compose_display_version()
    emit_json_or_text(
        {"version": display},
        f"eawf {display}",
        flags=flags,
    )


@app.command(name="scope-debug", hidden=True)
def scope_debug(ctx: typer.Context) -> None:
    """Print resolved global flags. Test/internal use only."""
    flags: GlobalFlags = ctx.obj
    text = (
        f"workspace={flags.workspace}"
        f"\njson={flags.json_output}"
        f"\nplain={flags.plain_output}"
        f"\nno_input={flags.no_input}"
        f"\ndaemonless={flags.daemonless}"
        f"\ndebug={flags.debug}"
    )
    typer.echo(text)


# --- ui command (inline: wraps the shared _dispatch_tui resolver) ---
@app.command(
    name="ui",
    help="Open the Eä console (or its plain frame off-TTY).",
    rich_help_panel=panel_for("ui"),
)
def _ui_cmd(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Show the console's key-trace row (SURF-173).",
        ),
    ] = False,
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            envvar="EAWF_ACTOR",
            help="Principal key the native console's writes are attributed to. "
            "Without it the console acts as the operator.principal claimed in the user "
            "config, else as nobody.",
        ),
    ] = None,
    receipt_ref: Annotated[
        str | None,
        typer.Option(
            "--receipt-ref",
            envvar="EAWF_RECEIPT_REF",
            help="Qualified evidence URN the console's answers are recorded under. "
            "Without it answers are refused; Run controls still reach the daemon.",
        ),
    ] = None,
) -> None:
    from eawf.surfaces.tui.launch import resolve_operator

    flags: GlobalFlags = ctx.obj
    try:
        operator = resolve_operator(actor=actor, receipt_ref=receipt_ref)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    rc = _dispatch_tui(
        workspace=flags.workspace,
        no_input=flags.no_input,
        plain=flags.plain_output,
        verbose=verbose,
        operator=operator,
    )
    raise typer.Exit(code=rc)


# Every other command — the sub-Typer groups, direct commands, and the
# wave-attached side-effect verbs — is mounted from the declarative table
# in eawf.surfaces.cli.registry (see register_commands for the mount logic).
register_commands(app)


def _configure_logging() -> None:
    """Install a scrubbed stderr log sink for the CLI process.

    Attaches a :class:`~eawf.observability.logging.scrub.SensitiveScrubber` to the
    root handler so any library log line the CLI emits (error details,
    resolved state paths) is redacted before it reaches the terminal —
    the CLI is the operator-facing surface and would otherwise print raw
    machine paths and secret-shaped tokens. Skips installation when the
    root logger already has handlers so a caller (test harness, embedding
    process) that configured logging first is not clobbered.
    """
    import logging
    import sys

    from eawf.observability.logging.scrub import SensitiveScrubber

    root = logging.getLogger()  # noqa: EAWF003 (root-logger handler config, not library acquisition)
    if root.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    # Lead with the timestamp (matching the daemon file log) so arm / drive
    # latency is measurable from the inline console, not only from eawfd.log.
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SensitiveScrubber())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _ensure_utf8_console() -> None:
    """Make stdout/stderr UTF-8-safe so the Eä brand never crashes on Windows.

    The brand wordmark (``Eä``) and the rendered help carry non-ASCII
    glyphs. On a Windows console whose active code page is cp1251 / a CJK
    page (not UTF-8), the default stdout encoding cannot represent ``ä``
    and ``typer.echo`` / ``print`` raise :class:`UnicodeEncodeError`,
    crashing even ``eawf --help``. This reconfigures both streams to UTF-8
    with ``errors="replace"`` so an unrepresentable glyph degrades to a
    placeholder instead of crashing. POSIX terminals already default to
    UTF-8, so this only fires on Windows and is a no-op when the stream is
    not reconfigurable (e.g. a captured non-``TextIOWrapper`` stream under
    test).
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    _ensure_utf8_console()
    _configure_logging()
    app()
