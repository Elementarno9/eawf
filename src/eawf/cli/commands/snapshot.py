"""``eawf snapshot`` Typer sub-app — golden-fixture regeneration surface.

CLI dispatch only (AGENTS rule 1): every handler parses args, resolves a
snapshot surface from the locked inventory, and routes output through
:func:`eawf.cli.output.emit_json_or_text`. The surface inventory and the
per-surface regeneration recipe live in :data:`SNAPSHOT_SURFACES`; the
handlers translate ``--kind`` into the surface's canonical refresh.

The locked surface inventory is the C09 §5.6 table — every golden tree
under ``tests/golden/<kind>/`` has exactly one ``--kind`` token that
regenerates its subset. Surfaces regenerate through the repo's canonical
``EAWF_REFRESH_GOLDEN=1`` pytest idiom: each surface names the pytest
node that, run under that env var, rewrites its committed golden bytes.

Verbs:

- ``eawf snapshot list`` — show every snapshot surface in the inventory.
- ``eawf snapshot update --kind <surface>`` — regenerate the golden
  subset for one surface (optionally into ``--out`` for verification).

Exit codes:

- ``0`` — success.
- ``1`` (``USER_ERROR``) — unknown ``--kind`` / regeneration failed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotSurface:
    """One golden surface from the C09 §5.6 inventory.

    Attributes:
        kind: The ``--kind`` token (stable wire identifier).
        golden_dir: Repo-relative directory holding the surface's
            committed golden bytes.
        regen_target: Pytest node id whose golden test rewrites the
            surface's bytes when run under ``EAWF_REFRESH_GOLDEN=1``.
        description: One-line operator summary.
    """

    kind: str
    golden_dir: str
    regen_target: str
    description: str


# Locked surface inventory — mirrors the C09 §5.6 table one-for-one. Each
# surface's golden tree lives under ``tests/golden/<kind>/`` and its bytes
# regenerate by running ``regen_target`` with ``EAWF_REFRESH_GOLDEN=1``.
SNAPSHOT_SURFACES: dict[str, SnapshotSurface] = {
    surface.kind: surface
    for surface in (
        SnapshotSurface(
            kind="state",
            golden_dir="tests/golden/state",
            regen_target="tests/golden/state",
            description="Canonical state.json sample tree per scope-pattern.",
        ),
        SnapshotSurface(
            kind="envelope",
            golden_dir="tests/golden/envelope",
            regen_target="tests/golden/test_envelope_fixtures.py",
            description="Output envelope per status (ok/failed/partial/blocked/needs_user).",
        ),
        SnapshotSurface(
            kind="dispatch",
            golden_dir="tests/golden/dispatch",
            regen_target="tests/golden/dispatch",
            description="Dispatch envelope per runtime x skill.",
        ),
        SnapshotSurface(
            kind="plan_view",
            golden_dir="tests/golden/plan_view",
            regen_target="tests/golden/plan_view/test_golden_plan_view.py",
            description="`eawf wave list` ASCII render per fixture (small/medium/large).",
        ),
        SnapshotSurface(
            kind="tui",
            golden_dir="tests/golden/tui",
            regen_target="tests/snapshots/tui",
            description="Textual screen capture (.txt) per screen x state.",
        ),
        SnapshotSurface(
            kind="spec",
            golden_dir="tests/golden/spec",
            regen_target="tests/golden/spec",
            description="PhaseSpec/IterSpec/WaveSpec render per `eawf {noun} spec render`.",
        ),
        SnapshotSurface(
            kind="agent_report",
            golden_dir="tests/golden/agent_report",
            regen_target="tests/unit/test_render_agent_report.py",
            description="Per-role typed-body envelope sample.",
        ),
        SnapshotSurface(
            kind="plugin_install",
            golden_dir="tests/golden/plugin_install",
            regen_target="tests/golden/test_plugin_install_claude.py",
            description="`build/<runtime>-plugin/` full tree per runtime.",
        ),
        SnapshotSurface(
            kind="audit_dsl",
            golden_dir="tests/golden/audit_dsl",
            regen_target="tests/unit/test_audit_dsl.py",
            description="DSL render per audit-kind.",
        ),
        SnapshotSurface(
            kind="scenarios",
            golden_dir="tests/golden/scenarios",
            regen_target="tests/golden/scenarios/test_scenarios.py",
            description="End-to-end lifecycle goldens (fresh repo, enrich, full flow).",
        ),
        SnapshotSurface(
            kind="telemetry",
            golden_dir="tests/golden/telemetry",
            regen_target="tests/golden/telemetry",
            description="DuckDB schema dump + projection output for a fixture event.jsonl.",
        ),
        SnapshotSurface(
            kind="metrics_export",
            golden_dir="tests/golden/metrics_export",
            regen_target="tests/golden/metrics_export",
            description="`eawf metrics export --format prom|json|csv` output per fixture.",
        ),
        SnapshotSurface(
            kind="agents_md",
            golden_dir="tests/golden/agents_md",
            regen_target="tests/golden/test_golden_agents_md.py",
            description="Rendered AGENTS.md after every sync.",
        ),
    )
}


snapshot_app = typer.Typer(
    name="snapshot",
    help="Golden-fixture snapshot surfaces — list and regenerate per --kind.",
    no_args_is_help=True,
    add_completion=False,
)


def resolve_surface(kind: str) -> SnapshotSurface:
    """Return the :class:`SnapshotSurface` for *kind*.

    Args:
        kind: The ``--kind`` token to resolve against the locked
            inventory.

    Returns:
        The matching :class:`SnapshotSurface`.

    Raises:
        UserError: When *kind* is not a known snapshot surface.
    """
    surface = SNAPSHOT_SURFACES.get(kind)
    if surface is None:
        known = ", ".join(sorted(SNAPSHOT_SURFACES))
        raise cli_errors.UserError(f"unknown snapshot kind: {kind!r} (known: {known})")
    return surface


def _regen_command(surface: SnapshotSurface) -> list[str]:
    """Build the ``uv run pytest`` argv that rewrites *surface*'s goldens.

    The surface's pytest golden test rewrites its committed bytes when
    invoked under ``EAWF_REFRESH_GOLDEN=1`` (set by :func:`run_regen`).
    Cache is disabled so a regeneration run is hermetic.
    """
    return ["uv", "run", "pytest", surface.regen_target, "-q", "-p", "no:cacheprovider"]


def run_regen(
    surface: SnapshotSurface,
    *,
    workspace: Path | None,
    output_dir: Path | None,
) -> subprocess.CompletedProcess[str]:
    """Run *surface*'s golden regeneration under ``EAWF_REFRESH_GOLDEN=1``.

    The regeneration drives the surface's pytest golden node — the repo's
    canonical refresh idiom. ``EAWF_REFRESH_GOLDEN=1`` switches each golden
    test from assert-mode to write-mode; ``EAWF_SNAPSHOT_OUT`` (set only
    when *output_dir* is given) redirects the rewritten bytes to a tmp
    directory so a caller can verify the regeneration without disturbing
    the committed tree.

    Args:
        surface: The resolved snapshot surface to regenerate.
        workspace: Optional workspace root the regeneration runs in (the
            ``uv run pytest`` cwd); ``None`` keeps the current directory.
        output_dir: Optional directory the regenerated subset lands in;
            ``None`` rewrites the committed golden tree in place.

    Returns:
        The completed subprocess (captured stdout/stderr, never raises on
        a non-zero exit — the caller maps that onto ``USER_ERROR``).
    """
    env = dict(os.environ)
    env["EAWF_REFRESH_GOLDEN"] = "1"
    if output_dir is not None:
        env["EAWF_SNAPSHOT_OUT"] = str(output_dir)
    cwd = str(workspace) if workspace is not None else None
    out_repr = repr(str(output_dir)) if output_dir is not None else "None"
    logger.info(f"run_regen kind={surface.kind} dir={surface.golden_dir!r} out={out_repr}")
    return subprocess.run(
        _regen_command(surface),
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@snapshot_app.command("list")
def snapshot_list(ctx: typer.Context) -> None:
    """List every snapshot surface in the locked inventory."""
    flags: GlobalFlags = ctx.obj
    payload: dict[str, object] = {
        "surfaces": [
            {
                "kind": s.kind,
                "golden_dir": s.golden_dir,
                "description": s.description,
            }
            for s in SNAPSHOT_SURFACES.values()
        ],
    }
    lines = ["snapshot surfaces:"]
    lines += [f"  {s.kind} — {s.golden_dir} — {s.description}" for s in SNAPSHOT_SURFACES.values()]
    emit_json_or_text(payload, "\n".join(lines), flags=flags)


@snapshot_app.command("update")
def snapshot_update(
    ctx: typer.Context,
    kind: Annotated[
        str,
        typer.Option("--kind", help="Snapshot surface to regenerate (see `eawf snapshot list`)."),
    ],
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Write the regenerated subset here instead of the committed golden tree.",
        ),
    ] = None,
) -> None:
    """Regenerate the golden subset for one snapshot surface.

    The surface's committed golden bytes are rewritten in place (or under
    ``--out`` for verification). On success the operator diffs the tree and
    commits as ``[P<NN>-W<NN>] test: snapshot update <kind>`` — the CI
    pairing gate refuses snapshot mutations without that exact prefix.

    Exits ``1`` (``USER_ERROR``) when ``--kind`` is unknown or the
    regeneration subprocess fails.
    """
    flags: GlobalFlags = ctx.obj
    try:
        surface = resolve_surface(kind)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    completed = run_regen(surface, workspace=flags.workspace, output_dir=output_dir)
    if completed.returncode != 0:
        # Surface the regeneration tail so the operator sees the failing
        # pytest node without re-running it manually.
        tail = (completed.stdout or completed.stderr or "").strip().splitlines()[-20:]
        sys.stderr.write("\n".join(tail) + "\n")
        cli_errors.emit_error(
            cli_errors.UserError(
                f"snapshot regeneration failed for kind {surface.kind!r} "
                f"(exit {completed.returncode}); see output above"
            ),
            flags=flags,
            data={"kind_surface": surface.kind, "exit_code": completed.returncode},
        )
        return

    target = str(output_dir) if output_dir is not None else surface.golden_dir
    payload: dict[str, object] = {
        "kind": surface.kind,
        "golden_dir": surface.golden_dir,
        "written_to": target,
    }
    text = f"snapshot update: regenerated {surface.kind} -> {target}"
    emit_json_or_text(payload, text, flags=flags)
