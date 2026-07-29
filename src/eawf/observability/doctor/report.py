"""``eawf doctor`` formatters — JSON envelope + Rich text body.

Both renderers consume the :class:`list[CheckResult]` returned by
:func:`eawf.observability.doctor.checks.run_all`. The JSON branch produces a stable shape
(``{"ok": bool, "checks": [...]}``) that golden tests can pin; the text
branch builds a Rich :class:`~rich.table.Table` for the TTY case (with a
plain fallback when ``flags.plain_output`` is set).
"""

from __future__ import annotations

import io
import logging
from typing import Any

from rich.console import Console
from rich.table import Table

from eawf.observability.doctor.models import CheckResult

logger = logging.getLogger(__name__)


_STATUS_RANK: dict[str, int] = {"ok": 0, "warn": 1, "fail": 2}


def overall_status(results: list[CheckResult]) -> str:
    """Return the highest-severity status across *results*.

    An empty list yields ``"ok"`` so an empty doctor run does not falsely
    flag a problem.
    """
    if not results:
        return "ok"
    return max(results, key=lambda r: _STATUS_RANK.get(r.status, 0)).status


def to_payload(results: list[CheckResult]) -> dict[str, Any]:
    """Return the canonical JSON envelope for *results*."""
    return {
        "ok": overall_status(results) == "ok",
        "status": overall_status(results),
        "checks": [r.model_dump(mode="json") for r in results],
    }


def to_text(results: list[CheckResult], *, plain: bool = False) -> str:
    """Return a human-readable body for *results*.

    When *plain* is True, emits ``"<status>  <name>  <detail>"`` lines so
    terminals without ANSI support stay readable. Otherwise renders a Rich
    :class:`Table` into a string buffer.
    """
    if plain:
        lines: list[str] = []
        for r in results:
            detail = r.detail or ""
            lines.append(f"{r.status.upper():<4}  {r.name:<24}  {detail}")
        lines.append(f"overall: {overall_status(results)}")
        return "\n".join(lines)

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100, record=False)
    table = Table(title="eawf doctor", show_lines=False)
    table.add_column("status", justify="left", style="bold")
    table.add_column("check", style="cyan")
    table.add_column("detail")
    for r in results:
        style = {"ok": "green", "warn": "yellow", "fail": "red"}.get(r.status, "white")
        table.add_row(f"[{style}]{r.status}[/{style}]", r.name, r.detail or "")
    console.print(table)
    console.print(f"overall: {overall_status(results)}")
    return buf.getvalue().rstrip()
