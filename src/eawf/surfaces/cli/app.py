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

from pathlib import Path
from typing import Annotated

import orjson
import typer

from eawf import __version__
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.help_panels import RegistryOrderedTyperGroup, panel_for
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.registry import register_commands

app = typer.Typer(
    name="eawf",
    help="Eä Workflow — agent-driven development framework.",
    no_args_is_help=False,
    add_completion=False,
    cls=RegistryOrderedTyperGroup,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
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
        # Bare ``eawf`` on a TTY routes to the Textual TUI
        # (config.ui.bare_command default: "tui") via the scope-dispatch
        # ladder; plain / no-input / non-TTY falls back to the
        # deterministic status emission so headless callers stay
        # script-stable.
        rc = _dispatch_tui(workspace=workspace, no_input=no_input, plain=plain_output)
        raise typer.Exit(code=rc)


def _dispatch_tui(
    *,
    workspace: Path | None,
    no_input: bool,
    plain: bool,
) -> int:
    """Resolve the launch scope and open the TUI.

    On an interactive TTY this resolves the scope via the cwd-upward
    ladder (``-w/--workspace`` flag wins, else the nearest ``state.json``
    determines ``repo`` vs ``workspace``) and launches the Textual
    :class:`~eawf.surfaces.tui.app.EaApp`. When ``--plain`` / ``--no-input`` is
    set or stdout is not a TTY it falls back to the deterministic
    single-frame status emission (:func:`eawf.surfaces.tui.offline.emit_status`)
    so headless callers stay script-stable.

    ``tui`` is the only TUI surface, so both the interactive launch and
    the non-TTY fallback route through it.

    Args:
        workspace: Optional workspace root from ``-w/--workspace``.
        no_input: Fail-closed flag — forces the deterministic fallback.
        plain: Plain-output flag — forces the deterministic fallback.

    Returns:
        Process exit code (``0`` on a clean quit).
    """
    import sys

    from eawf.surfaces.tui.offline import emit_status

    if no_input or plain or not sys.stdout.isatty():
        return emit_status(workspace=workspace, no_input=no_input, plain=plain)

    from eawf.kernel.state.enums import ScopeKind
    from eawf.kernel.state.resolve import resolve_with_reason
    from eawf.surfaces.tui.app import resolve_scope, run_app

    state_path, _reason = resolve_with_reason(workspace=workspace)
    if state_path.is_file():
        try:
            payload = orjson.loads(state_path.read_bytes())
            scope_kind = ScopeKind(payload["scope_kind"])
        except orjson.JSONDecodeError, OSError, KeyError, ValueError:
            return run_app("repo", state_path)
        return run_app(resolve_scope(scope_kind), state_path)
    return run_app("user", None)


@app.command(name="version", rich_help_panel=panel_for("version"))
def version_cmd(ctx: typer.Context) -> None:
    """Show the eawf version (text or JSON envelope)."""
    flags: GlobalFlags = ctx.obj
    emit_json_or_text(
        {"version": __version__},
        f"eawf {__version__}",
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


# --- TUI command (inline: wraps the shared _dispatch_tui resolver) ---
@app.command(
    name="tui",
    help="Open the Eä Textual TUI (or deterministic status fallback off-TTY).",
    rich_help_panel=panel_for("tui"),
)
def _tui_cmd(ctx: typer.Context) -> None:
    flags: GlobalFlags = ctx.obj
    rc = _dispatch_tui(workspace=flags.workspace, no_input=flags.no_input, plain=flags.plain_output)
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
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    handler.addFilter(SensitiveScrubber())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main() -> None:
    _configure_logging()
    app()
