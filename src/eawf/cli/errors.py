"""Exit-code-mapped error helpers for CLI handlers.

Each non-zero canonical exit code in :mod:`eawf.cli.exit_codes` has its own
:class:`CliError` subclass; handlers raise the right subclass and rely on
:func:`emit_error` to print the canonical envelope and raise
:class:`typer.Exit`.

Envelope shape (JSON branch):

.. code-block:: json

    {
      "error": "<class name>",
      "message": "<str(err)>",
      "exit_code": 5,
      "exit_name": "LOCK_CONFLICT"
    }
"""

from __future__ import annotations

import typer

from eawf.cli import exit_codes
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text


class CliError(Exception):
    """Base class for CLI-mapped errors.

    The default :attr:`exit_code` is :data:`exit_codes.GENERIC_ERROR`. Each
    subclass overrides it with the canonical code per the v0.1 plan §5 table.
    """

    exit_code: int = exit_codes.GENERIC_ERROR


class NotFound(CliError):  # noqa: N818 — canonical CLI error name per plan §5
    """Scope, state file, artifact, or referenced ID was not found."""

    exit_code = exit_codes.NOT_FOUND


class InvalidInput(CliError):  # noqa: N818 — canonical CLI error name per plan §5
    """Bad CLI args or schema mismatch on input."""

    exit_code = exit_codes.INVALID_INPUT


class ValidationFailed(CliError):  # noqa: N818 — canonical CLI error name per plan §5
    """Strict invariant validation rejected the candidate state."""

    exit_code = exit_codes.VALIDATION_FAILED


class LockConflict(CliError):  # noqa: N818 — canonical CLI error name per plan §5
    """Sibling lock held by a live holder, or wait timed out."""

    exit_code = exit_codes.LOCK_CONFLICT


class InstrumentMissing(CliError):  # noqa: N818 — canonical CLI error name per plan §5
    """A required external tool (git, jq, ...) is absent."""

    exit_code = exit_codes.INSTRUMENT_MISSING


class UserDeclined(CliError):  # noqa: N818 — canonical CLI error name per plan §5
    """User declined at a confirmation gate (or ``--no-input`` aborted it)."""

    exit_code = exit_codes.USER_DECLINED


class IntegrityViolation(CliError):  # noqa: N818 — canonical CLI error name per plan §5
    """Hash mismatch, drift, or corrupted store."""

    exit_code = exit_codes.INTEGRITY_VIOLATION


class HookBlocked(CliError):  # noqa: N818 — canonical CLI error name per plan §5
    """Pre-/post-tool hook fail-closed."""

    exit_code = exit_codes.HOOK_BLOCKED


def emit_error(err: CliError, *, flags: GlobalFlags) -> None:
    """Print the canonical envelope for *err* and exit with its code.

    Args:
        err: The :class:`CliError` instance to surface. The class name and
            string body populate the envelope.
        flags: Resolved global flags. ``flags.json_output`` controls the
            text/JSON branch in :func:`eawf.cli.output.emit_json_or_text`.

    Raises:
        typer.Exit: Always — with ``err.exit_code``.
    """
    payload = {
        "error": err.__class__.__name__,
        "message": str(err),
        "exit_code": err.exit_code,
        "exit_name": exit_codes.name_for(err.exit_code),
    }
    emit_json_or_text(payload, f"error: {err}", flags=flags)
    raise typer.Exit(err.exit_code)
