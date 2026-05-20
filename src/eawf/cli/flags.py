"""Global flag definitions shared across the Typer app.

The Typer root callback in :mod:`eawf.cli.app` parses the global flags
(``--json``, ``--plain``, ``--no-input``, ``-w/--workspace``) and stashes a
:class:`GlobalFlags` instance on ``typer.Context.obj``. Every subcommand
pulls the dataclass back out via ``ctx.obj`` to drive its emission helpers.

``--scope`` is deliberately *not* a global flag: nothing in the v0.1
surface filters or anchors on it cross-cutting. Subcommands that genuinely
need a scope ID (``session start``, ``store compact``, ``memory ...``,
``estimate``, ``actual``) declare their own per-command ``--scope`` option
with semantics appropriate to that command. Hoisting it to the root would
have promised behaviour no handler implements.

**Workspace → repo_root resolution (W03 plumbing).** The
:attr:`GlobalFlags.workspace` value drives daemon JSON-RPC ``repo_root``
parameters indirectly through :mod:`eawf.cli._mutation`, which derives
``repo_root = (flags.workspace or Path.cwd()).resolve()`` per request.
Adding an explicit ``--repo-root`` global flag was rejected for v0.3:
the derivation rule is unambiguous + the daemon's
``daemon_anchor_fallback`` once-per-process warning catches stale
callers that omit the param entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class GlobalFlags:
    """Resolved global flags carried via ``typer.Context.obj``.

    Attributes:
        json_output: When True, downstream handlers emit a JSON envelope
            rather than a human-readable text body.
        plain_output: When True, Rich/colour output is disabled. Reserved
            for terminals that cannot render markup.
        no_input: When True, handlers must fail closed instead of
            prompting the user for confirmation.
        workspace: Optional explicit workspace root. Overrides
            pwd-upward scope resolution but is itself overridden by
            ``EA_STATE``. Also serves as the ``repo_root`` source for
            daemon JSON-RPC requests (see module docstring).
        daemonless: When True, the daemon-bypass carve-out is
            requested explicitly (CI / one-shot / recovery shell).
            Read-only verbs honour it by reading state directly;
            mutating verbs reject it with a ``UserError``
            (``data.kind="InvalidInput"``) per the escalation table.
            Mirrors the ``EAWF_DAEMONLESS=1`` env-var hatch.
        debug: When True, dev-mode-gated surfaces (the raw ``state rpc``
            JSON-RPC passthrough, hidden ``daemon`` control verbs)
            become reachable. Mirrors the ``EAWF_DEBUG=1`` env-var
            hatch. Off in normal operation.
    """

    json_output: bool = False
    plain_output: bool = False
    no_input: bool = False
    workspace: Path | None = None
    daemonless: bool = False
    debug: bool = False
