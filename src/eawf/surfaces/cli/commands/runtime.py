"""``eawf runtime certify``: certify the installed version of a runtime harness.

The verb sends ``runtime.certification.certify``. The daemon probes the binary
installed now, the same conformance probe it runs on its own when a Run first
reports a version nothing certifies, and answers with the row it recorded. A
certified version prints its expiry and exits 0; a quarantined one prints what
the probe did not find and exits non-zero. The probe runs no model, so it costs
no tokens.
"""

from __future__ import annotations

from typing import Annotated, Final

import typer

from eawf.surfaces.cli.commands.domain_integration import _answer, _send

#: The dotted JSON-RPC name the verb forwards to, spelled here so the Typer tree
#: builds without the daemon method registry on the path.
RUNTIME_CERTIFY: Final = "runtime.certification.certify"

runtime_app = typer.Typer(
    name="runtime",
    help="Certify the runtime harnesses installed on this machine.",
    no_args_is_help=True,
)


@runtime_app.command("certify")
def runtime_certify_cmd(
    ctx: typer.Context,
    runtime: Annotated[
        str, typer.Argument(help="The runtime to probe: claude-code, codex or opencode.")
    ],
) -> None:
    """Probe the installed runtime and record its certification or quarantine."""
    subject = f"runtime://{runtime}"
    answer = _send(
        ctx,
        RUNTIME_CERTIFY,
        {"runtime": runtime},
        urn=subject,
        verb_text="runtime certify",
        key=None,
    )
    if answer is not None:
        failed = "findings" if answer["outcome"] == "quarantined" else None
        _answer(
            ctx, answer, operation=RUNTIME_CERTIFY, urn=subject, revision=None, failed_guard=failed
        )


__all__ = ["RUNTIME_CERTIFY", "runtime_app", "runtime_certify_cmd"]
