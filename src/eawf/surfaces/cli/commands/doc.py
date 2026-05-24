"""``eawf doc`` Typer sub-app.

Houses the doc-drift linter (``eawf doc verify``). Sibling of ``eawf doctor``;
shares ``GlobalFlags`` + canonical JSON-envelope shape.

Exit codes:

- ``0`` — no drift, no cross-check violation, no autogen drift (or drift
  detected without ``--strict``).
- ``2`` (``NOT_FOUND``) — no ``state.json`` resolvable.
- ``4`` (``VALIDATION_FAILED``) — drift, cross-check violation, autogen
  drift, or a failed mkdocs strict build detected and ``--strict`` was set.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import orjson
import typer

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


doc_app = typer.Typer(
    name="doc",
    help="Read-only documentation drift + state-vs-doc cross-checks.",
    no_args_is_help=True,
    add_completion=False,
)


def _load_state(state_path: Path) -> State:
    from eawf.kernel.validate.strict import validate_state

    if not state_path.exists():
        raise cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationError(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


def _mkdocs_build(repo_root: Path) -> tuple[str, str]:
    """Run ``mkdocs build --strict`` when mkdocs is installed.

    Returns:
        A ``(status, detail)`` pair. ``status`` is ``"ok"`` on a clean
        strict build, ``"failed"`` when the build returned non-zero, and
        ``"skipped"`` when mkdocs is not importable (the optional
        ``eawf[docs]`` extra is not installed) or ``mkdocs.yml`` is absent.
        ``detail`` carries the tail of the build output / skip reason.
    """
    import importlib.util
    import subprocess

    if importlib.util.find_spec("mkdocs") is None:
        return "skipped", "mkdocs not installed (install the eawf[docs] extra)"
    config = repo_root / "mkdocs.yml"
    if not config.is_file():
        return "skipped", f"mkdocs.yml not found under {repo_root}"
    completed = subprocess.run(
        ["mkdocs", "build", "--strict", "--quiet", "-f", str(config)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return "ok", "mkdocs build --strict passed"
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-20:]
    return "failed", "\n".join(tail)


@doc_app.command("verify")
def doc_verify(
    ctx: typer.Context,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help=(
                "Exit 4 on any drift / cross-check / autogen drift, and run "
                "the mkdocs strict build when the docs extra is installed."
            ),
        ),
    ] = False,
) -> None:
    """Verify that rendered docs match state.json + manifest hashes.

    With ``--strict`` the command additionally (a) regenerates the
    introspection-driven reference pages and asserts no diff against the
    committed ``docs/reference/autogen/`` tree, and (b) runs
    ``mkdocs build --strict`` when the optional ``eawf[docs]`` extra is
    installed (degrading cleanly to a skip otherwise). The drift diff
    always runs under ``--strict``; only the mkdocs build is gated on
    availability.
    """
    from eawf.docs.autogen import diff_against_disk
    from eawf.observability.doctor.doc_verify import verify_docs

    flags: GlobalFlags = ctx.obj
    try:
        state_path, _reason = resolve_with_reason(flags.workspace)
        state = _load_state(state_path)
        repo_root = state_path.parent.parent
        report = verify_docs(state, repo_root)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    autogen_drift = [
        {"target": d.relpath, "reason": d.reason} for d in diff_against_disk(repo_root)
    ]
    mkdocs_status, mkdocs_detail = ("not_run", "")
    if strict:
        mkdocs_status, mkdocs_detail = _mkdocs_build(repo_root)

    payload = {
        "status": report.status,
        "manifest_targets": report.manifest_targets,
        "manifest_entries": report.manifest_entries,
        "drift": [
            {
                "target": str(r.target),
                "id": r.id,
                "kind": r.kind,
                "on_disk_hash": r.on_disk_hash,
                "manifest_hash": r.manifest_hash,
            }
            for r in report.drift_reports
            if r.kind != "ok"
        ],
        "cross_check_violations": [
            {"code": v.code, "target": v.target, "message": v.message}
            for v in report.cross_check_violations
        ],
        "autogen_drift": autogen_drift,
        "autogen_drift_count": len(autogen_drift),
        "mkdocs_build": mkdocs_status,
        "mkdocs_detail": mkdocs_detail,
        "drift_count": report.extras.get("drift_count", 0),
        "cross_check_count": report.extras.get("cross_check_count", 0),
    }
    region_ok = report.status == "ok" and not autogen_drift and mkdocs_status != "failed"
    if region_ok:
        text = (
            f"doc verify: ok ({report.manifest_targets} targets, "
            f"{report.manifest_entries} regions, mkdocs={mkdocs_status})"
        )
    else:
        text = (
            f"doc verify: drift detected "
            f"(drift={payload['drift_count']}, cross_check={payload['cross_check_count']}, "
            f"autogen={payload['autogen_drift_count']}, mkdocs={mkdocs_status})"
        )
    emit_json_or_text(payload, text, flags=flags)
    if strict and not region_ok:
        raise typer.Exit(code=exit_codes.VALIDATION_FAILED)
