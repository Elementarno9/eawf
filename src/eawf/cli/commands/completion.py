"""``eawf completion`` — shell-completion script generation + install.

Two opt-in verbs (explicit verb, never auto-edit dotfiles):

* ``eawf completion show <shell>`` — render the completion script to stdout.
  The operator pipes it wherever they like.
* ``eawf completion install <shell>`` — write the script to the shell's
  canonical completion directory and print the path. Best-effort: on a
  permission failure the script is written to stdout instead with a hint
  carrying the explicit operator-side ``mv`` command.

Supported shells: ``bash`` / ``zsh`` / ``fish``. The underlying generator is
Typer's ``get_completion_script``; ``add_completion=False`` on the root app
(:mod:`eawf.cli.app`) stays — this verb is the only completion entry point so
``eawf --help`` never pollutes a dotfile on first invocation.

The install verb never edits shell-rc files. It writes a single completion
file; the operator sources it (bash/fish) or ensures the target directory is
on ``$fpath`` (zsh) per the printed hint.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from typer._completion_shared import get_completion_script

from eawf.cli.errors import UserError, emit_error
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)

#: Program name baked into the generated completion script.
_PROG_NAME = "eawf"

#: Typer/Click completion env-var convention: ``_<PROG_UPPER>_COMPLETE``.
_COMPLETE_VAR = "_EAWF_COMPLETE"


class Shell(StrEnum):
    """Shell flavours the completion verbs support."""

    BASH = "bash"
    ZSH = "zsh"
    FISH = "fish"


completion_app = typer.Typer(
    name="completion",
    help="Generate or install shell completion scripts (bash/zsh/fish).",
    no_args_is_help=True,
    add_completion=False,
)


def _xdg_data_home() -> Path:
    """Return ``$XDG_DATA_HOME`` or its ``~/.local/share`` default.

    Returns:
        The resolved XDG data-home directory (not created).
    """
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share"


def _install_path(shell: Shell) -> Path:
    """Return the canonical completion-script install path for *shell*.

    Args:
        shell: Target shell flavour.

    Returns:
        The absolute path the completion file is written to.
        Directories are not created here — :func:`_install` does that.
    """
    data_home = _xdg_data_home()
    if shell is Shell.BASH:
        return data_home / "bash-completion" / "completions" / _PROG_NAME
    if shell is Shell.ZSH:
        # zsh loads completion functions named ``_<cmd>`` from any dir on
        # ``$fpath``. ``$fpath`` is only meaningful inside a live zsh, so we
        # target a conventional XDG site-functions dir and hint the operator
        # to keep it on fpath rather than guessing their live ``$fpath[1]``.
        return data_home / "zsh" / "site-functions" / f"_{_PROG_NAME}"
    return data_home / "fish" / "completions" / f"{_PROG_NAME}.fish"


def _source_hint(shell: Shell, path: Path) -> str:
    """Return the operator-side activation hint for *shell* + *path*.

    Args:
        shell: Target shell flavour.
        path: Where the completion script was (or should be) written.

    Returns:
        A one-line hint telling the operator how to activate completion.
    """
    if shell is Shell.ZSH:
        return f"ensure {path.parent} is on your $fpath, then restart zsh"
    if shell is Shell.FISH:
        return f"fish auto-loads {path} on next launch"
    return f"add `source {path}` to your ~/.bashrc, or restart bash"


def _render_script(shell: Shell) -> str:
    """Generate the completion script for *shell* via Typer's generator.

    Args:
        shell: Target shell flavour.

    Returns:
        The completion script body (no trailing newline guarantee).
    """
    return get_completion_script(
        prog_name=_PROG_NAME,
        complete_var=_COMPLETE_VAR,
        shell=shell.value,
    )


@completion_app.command(name="show")
def show_cmd(
    ctx: typer.Context,
    shell: Annotated[
        Shell,
        typer.Argument(help="Shell flavour to generate the completion script for."),
    ],
) -> None:
    """Print the completion script for *shell* to stdout (no file written).

    The operator redirects it themselves, e.g.
    ``eawf completion show zsh > "$fpath[1]/_eawf"``.
    """
    flags: GlobalFlags = ctx.obj
    script = _render_script(shell)
    emit_json_or_text(
        {"shell": shell.value, "script": script},
        script,
        flags=flags,
    )


@completion_app.command(name="install")
def install_cmd(
    ctx: typer.Context,
    shell: Annotated[
        Shell,
        typer.Argument(help="Shell flavour to install completion for."),
    ],
) -> None:
    """Write the completion script to *shell*'s canonical directory.

    Best-effort: on a permission / OS error the script is written
    to stdout instead and a :class:`UserError` envelope carries the explicit
    operator-side ``mv`` command. Never edits shell-rc files.

    Raises:
        typer.Exit: Via :func:`emit_error` with exit 1 USER_ERROR when the
            target path is not writable (the script still reaches stdout).
    """
    flags: GlobalFlags = ctx.obj
    script = _render_script(shell)
    target = _install_path(shell)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(script + "\n", encoding="utf-8")
    except OSError as exc:
        # Fall back to stdout so the operator can still capture the script,
        # then surface the explicit recovery command.
        typer.echo(script)
        logger.warning(f"install_cmd write-failed shell={shell.value!r} target={str(target)!r}")
        emit_error(
            UserError(f"could not write completion script to {target}: {exc}"),
            flags=flags,
            data={
                "kind": "InvalidInput",
                "target": str(target),
                "recovery": f"eawf completion show {shell.value} > {target}",
            },
        )
        return
    emit_json_or_text(
        {"shell": shell.value, "path": str(target), "hint": _source_hint(shell, target)},
        f"installed {shell.value} completion to {target}\nhint: {_source_hint(shell, target)}",
        flags=flags,
    )


__all__ = ["Shell", "completion_app"]
