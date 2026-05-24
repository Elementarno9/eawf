"""Artifact command handlers.

Split out of :mod:`eawf.cli.commands.evidence` (P27-W07). The
``artifact_app`` Typer app and the shared helpers live in the parent
module; this module attaches the command bodies (add / update / show /
validate / verify) via ``@<app>.command(...)``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.commands.evidence import (
    _emit,
    _flags,
    _run_read,
    _state_path,
    artifact_app,
)
from eawf.kernel.state.enums import StoreKind

logger = logging.getLogger(__name__)


# ---- artifact --------------------------------------------------------------


@artifact_app.command("add")
def artifact_add(
    ctx: typer.Context,
    artifact_id: Annotated[str, typer.Argument(help="Artifact id")],
    kind: Annotated[str, typer.Option("--kind", help="Artifact kind, e.g. audit_report")],
    uri: Annotated[str, typer.Option("--uri", help="Artifact URI (repo:... or remote URI)")],
    sha256: Annotated[str | None, typer.Option("--sha256", help="Optional SHA-256 hash")] = None,
    size: Annotated[int | None, typer.Option("--size", help="Optional size in bytes")] = None,
    scope_id: Annotated[
        str | None,
        typer.Option(
            "--scope-id",
            help="Owning scope (defaults to project code).",
        ),
    ] = None,
) -> None:
    """Register a durable artifact."""
    from eawf.cli._mutation import state_transaction
    from eawf.evidence import artifact as artifact_evi
    from eawf.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            resolved_scope = scope_id
            if resolved_scope is None:
                if state.project is None:
                    raise cli_errors.UserError(
                        "scope_id required when state.project is unset", kind="InvalidInput"
                    )
                resolved_scope = state.project.code
            event = artifact_evi.add_artifact(
                state,
                artifact_id=artifact_id,
                kind=kind,
                uri=uri,
                scope_id=resolved_scope,
                sha256=sha256,
                size_bytes=size,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "artifact_id": artifact_id,
            "kind": kind,
            "uri": uri,
            "sha256": sha256,
            "scope_id": resolved_scope,
        },
        f"artifact {artifact_id} added kind={kind}",
        flags,
    )


@artifact_app.command("update")
def artifact_update(
    ctx: typer.Context,
    artifact_id: Annotated[str, typer.Argument(help="Artifact id to update")],
    sha256: Annotated[
        str | None,
        typer.Option("--sha256", help="New SHA-256 hash for the artifact body."),
    ] = None,
    size: Annotated[
        int | None,
        typer.Option("--size", help="New body size in bytes."),
    ] = None,
    uri: Annotated[
        str | None,
        typer.Option("--uri", help="New URI (repo:... or remote URI)."),
    ] = None,
) -> None:
    """Update mutable fields on a registered artifact.

    Re-pins ``sha256`` / ``size_bytes`` / ``uri`` when the underlying
    body content changed (typically after a pre-commit auto-fix). At
    least one of ``--sha256`` / ``--size`` / ``--uri`` must be supplied.
    Identity fields (``id``, ``kind``, ``urn``, ``scope_id``,
    ``created_at``) stay fixed.
    """
    from eawf.cli._mutation import state_transaction
    from eawf.evidence import artifact as artifact_evi
    from eawf.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            event = artifact_evi.update_artifact(
                state,
                artifact_id=artifact_id,
                sha256=sha256,
                size_bytes=size,
                uri=uri,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "artifact_id": artifact_id,
            "sha256": sha256,
            "size_bytes": size,
            "uri": uri,
        },
        f"artifact {artifact_id} updated",
        flags,
    )


@artifact_app.command("show")
def artifact_show(
    ctx: typer.Context,
    artifact_id: Annotated[str, typer.Argument(help="Artifact id")],
) -> None:
    """Show artifact metadata."""
    from eawf.evidence import artifact as artifact_evi
    from eawf.evidence._io import load_state

    flags = _flags(ctx)
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)
    artifact = _run_read(flags, artifact_evi.show_artifact, state, artifact_id)
    payload = json.loads(artifact.model_dump_json())
    _emit(
        payload,
        f"artifact {artifact.id} kind={artifact.kind} uri={artifact.uri}",
        flags,
    )


@artifact_app.command("validate")
def artifact_validate(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Markdown artifact path.")],
) -> None:
    """Validate one markdown artifact body."""
    from eawf.artifacts.validation import validate_markdown_artifact

    flags = _flags(ctx)
    text = path.read_text(encoding="utf-8")
    report = validate_markdown_artifact(text)
    payload = {"ok": report.ok, "errors": report.errors}
    if not report.ok:
        _emit(payload, "\n".join(report.errors), flags)
        raise typer.Exit(code=4)
    _emit(payload, "artifact validate: ok", flags)


# ---- artifact verify -------------------------------------------------------


def _verify_one_artifact(
    artifact: Any,
    repo_root: Path,
    *,
    refresh: bool,
) -> dict[str, Any]:
    """Verify one artifact body against its registered sha256.

    Returns a result row of the shape used by :func:`artifact_verify`. The
    ``status`` field is one of:

    - ``"ok"`` — sha256 recomputed and matched the registered value.
    - ``"no_hash"`` — no registered sha256 to compare against.
    - ``"missing_file"`` — repo-relative uri did not resolve to a file.
    - ``"skipped_remote"`` — non-``repo:`` uri with ``--refresh`` not set.
    - ``"mismatch"`` — recomputed sha256 did not match the registered value.
    """
    from eawf.artifacts.validation import sha256_file

    uri = artifact.uri
    registered = artifact.sha256
    row: dict[str, Any] = {
        "artifact_id": artifact.id,
        "uri": uri,
        "registered_sha256": registered,
        "computed_sha256": None,
        "status": "ok",
    }
    if not uri.startswith("repo:"):
        if not refresh:
            row["status"] = "skipped_remote"
            return row
        # --refresh is not implemented for remote URIs (no network in v0.3).
        row["status"] = "skipped_remote"
        row["message"] = "refresh not supported for remote uris yet"
        return row
    relpath = uri[len("repo:") :]
    if not relpath:
        row["status"] = "missing_file"
        row["message"] = "empty repo-relative path in uri"
        return row
    body_path = repo_root / relpath
    if not body_path.is_file():
        row["status"] = "missing_file"
        row["message"] = f"artifact body not found: {relpath}"
        return row
    computed = sha256_file(body_path)
    row["computed_sha256"] = computed
    if registered is None:
        row["status"] = "no_hash"
        return row
    if registered.lower() != computed.lower():
        row["status"] = "mismatch"
        row["message"] = f"sha256 mismatch: registered={registered!r} computed={computed!r}"
    return row


@artifact_app.command("verify")
def artifact_verify(
    ctx: typer.Context,
    artifact_id: Annotated[
        str | None,
        typer.Argument(help="Artifact id to verify; omit with --all."),
    ] = None,
    verify_all: Annotated[
        bool,
        typer.Option("--all", help="Verify every registered artifact."),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Re-fetch remote URIs (no-op in v0.3; remote always skipped).",
        ),
    ] = False,
) -> None:
    """Recompute artifact sha256 and compare to the registered hash.

    Exit codes:

    - ``0`` — every checked artifact matched (or had no registered hash).
    - ``2`` (``NOT_FOUND``) — single-id mode with unknown artifact id.
    - ``8`` (``INTEGRITY_VIOLATION``) — at least one artifact mismatched.
    """
    from eawf.evidence._io import load_state

    flags = _flags(ctx)
    if (artifact_id is None) == (not verify_all):
        cli_errors.emit_error(
            cli_errors.UserError(
                "exactly one of <artifact-id> or --all must be provided", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)
    repo_root = state_path.parent.parent

    if verify_all:
        artifacts_to_check = sorted(state.artifacts.values(), key=lambda a: a.id)
    else:
        assert artifact_id is not None
        if artifact_id not in state.artifacts:
            cli_errors.emit_error(
                cli_errors.UserError(f"artifact not found: {artifact_id!r}", kind="NotFound"),
                flags=flags,
            )
            return
        artifacts_to_check = [state.artifacts[artifact_id]]

    results: list[dict[str, Any]] = [
        _verify_one_artifact(art, repo_root, refresh=refresh) for art in artifacts_to_check
    ]
    mismatches = [r for r in results if r["status"] == "mismatch"]
    missing = [r for r in results if r["status"] == "missing_file"]
    payload: dict[str, Any] = {
        "checked": len(results),
        "ok": len(results) - len(mismatches) - len(missing),
        "mismatches": len(mismatches),
        "missing": len(missing),
        "results": results,
    }
    if mismatches:
        text = (
            f"artifact verify: mismatch ({len(mismatches)}/{len(results)} "
            f"artifacts failed sha256 check)"
        )
        _emit(payload, text, flags)
        raise typer.Exit(code=cli_errors.StateConflict.exit_code)
    if missing:
        text = (
            f"artifact verify: missing files ({len(missing)}/{len(results)} "
            f"artifacts had no resolvable body)"
        )
        _emit(payload, text, flags)
        raise typer.Exit(code=cli_errors.StateConflict.exit_code)
    text = f"artifact verify: ok ({len(results)} checked)"
    _emit(payload, text, flags)
