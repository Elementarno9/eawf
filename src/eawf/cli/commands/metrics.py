"""``eawf metrics`` — rolling workflow metrics view.

Surfaces four wave-level metrics from the current state (no mutation, no
event emission, read-only):

- **EU variance** — calibration drift between estimate and actual EU.
- **Audit pass rate** — share of decided audits with verdict PASS.
- **Wave elapsed (min)** — wall-clock latency roll-up for CLOSED waves.
- **Planned vs reactive** — split of waves by iter (I01 vs I02+).

The handler is thin dispatch: load state, run pure
:func:`~eawf.estimation.metrics.compute_metrics`, then route either a
JSON envelope (``--json``, ``schema_version=1``) or a Rich table (default)
through :func:`~eawf.cli.output.emit_json_or_text`.

The shared renderer lives in :mod:`eawf.render.metrics_view` so the same
table feeds the CLI today, the TUI overlay (P20 W04), and the release-
notes section (P20 W13) without bytes drifting between consumers.
"""

from __future__ import annotations

import logging
from typing import Any

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path

logger = logging.getLogger(__name__)


def metrics_cmd(ctx: typer.Context) -> None:
    """Print the eawf metrics roll-up for the active workspace.

    Read-only — does not acquire a lock, append events, or mutate
    ``state.json``. Failures map to the canonical CLI exit codes:

    - :class:`~eawf.cli.errors.NotFound` (``exit=4``) when no
      ``.ea/state.json`` is locatable from the cwd / ``-w`` / ``EA_STATE``
      precedence chain.
    - :class:`~eawf.cli.errors.ValidationFailed` (``exit=6``) when the
      on-disk payload fails strict schema validation.
    """
    from eawf.estimation.metrics import compute_metrics
    from eawf.evidence._io import load_state
    from eawf.render.metrics_view import render_metrics_plain, render_metrics_table

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return

    try:
        state = load_state(state_path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    summary = compute_metrics(state)
    # ``model_dump(mode="json")`` emits the schema_version literal as the
    # integer ``1`` (Pydantic v2 normalises the ``Literal[1]`` to its
    # underlying value), which matches the wire contract documented in
    # :data:`eawf.estimation.metrics.METRICS_SCHEMA_VERSION`.
    payload: dict[str, Any] = summary.model_dump(mode="json")
    text = render_metrics_plain(summary) if flags.plain_output else render_metrics_table(summary)
    emit_json_or_text(payload, text, flags=flags)


__all__ = ["metrics_cmd"]
