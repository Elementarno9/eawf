"""``eawf spec`` Typer sub-app — proxies through the daemon.

Five verbs:

* ``eawf spec init <scope-id> --title ... --repo-code ...``
* ``eawf spec validate <scope-id> --repo-code ...``
* ``eawf spec promote <scope-id> --to {READY,IMPLEMENTED} --repo-code ...``
* ``eawf spec archive <scope-id> --repo-code ...``
* ``eawf spec show <urn> [--from-git]`` (read-only)

The first four are mutators and route through the daemon's
``spec.{init,validate,promote,archive}`` JSON-RPC methods per
authority-map row 9-10. The dispatch matches the existing
``_persist_registry`` shape: daemon-proxy arm by default; the
daemonless carve-out (``EAWF_DAEMONLESS=1`` or daemon unreachable)
falls back to the in-process writer for the operations that do not
require a running daemon. ``archive`` always requires the daemon up because the
``git rm`` + cache atomicity is daemon-owned.

``show`` is the recovery surface: it reads the daemon-resident cache
to find ``file_path`` + ``file_sha``, then either reads the file from
disk (DRAFT / READY / IMPLEMENTED) or walks ``git log -- <path>`` to
recover the archived body (ARCHIVED + ``--from-git``).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.cli import errors as cli_errors
from eawf.cli._daemon_client import DaemonClient, DaemonRpcError
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


spec_app = typer.Typer(
    name="spec",
    help="Manage phase / iter / wave specs (init / validate / promote / archive / show).",
    no_args_is_help=True,
    add_completion=False,
)


# ---- Proxy gate -----------------------------------------------------------


def _daemon_proxy_enabled_for_spec() -> bool:
    """Return True when daemon-proxy mode is on for spec mutations.

    Mirrors :func:`eawf.cli.commands.repo._daemon_proxy_enabled_for_registry`.
    Honours ``EAWF_DAEMONLESS=1`` for the V1 carve-out.
    """
    from eawf.cli._mutation import _proxy_enabled

    if os.environ.get("EAWF_DAEMONLESS", "") == "1":
        return False
    return _proxy_enabled(None)


def _emit_result(payload: dict[str, Any], *, flags: GlobalFlags) -> None:
    """Emit the spec RPC result as JSON or terse text."""
    text = (
        f"{payload.get('operation')} ok scope_id={payload.get('scope_id')!r} "
        f"urn={payload.get('spec_urn')!r} status={payload.get('status')}"
    )
    emit_json_or_text(payload, text, flags=flags)


# ---- In-process fallback (V1 carve-out) -----------------------------------


def _inprocess_init(
    *,
    scope_id: str,
    title: str,
    repo_code: str,
    repo_root: Path,
) -> dict[str, Any]:
    """Daemonless-path fallback for ``spec init``.

    Mirrors the daemon writer body so the V1 carve-out produces an
    equivalent on-disk + cache state. ``init`` is the only mutator
    the V1 fallback supports beyond ``validate``; ``promote`` and
    ``archive`` refuse without a daemon because the lifecycle gate
    + ``git rm`` atomicity sit inside the daemon process.
    """
    from eawf.kernel.spec import cache as spec_cache
    from eawf.kernel.spec import writer as spec_writer

    spec_writer.classify_scope(scope_id)
    spec_urn = spec_writer.build_spec_urn(scope_id, repo_code=repo_code)
    file_path = spec_writer.spec_file_path(scope_id, repo_root=repo_root)
    phase_id = spec_writer.phase_of(scope_id)
    existing = spec_cache.find_cached_entry(spec_urn, phase_id=phase_id)
    if existing is not None and file_path.is_file():
        return {
            "operation": "init",
            "scope_id": scope_id,
            "spec_urn": spec_urn,
            "status": existing.status,
            "file_path": existing.file_path,
            "file_sha": existing.file_sha,
            "envelope": {},
            "idempotent_replay": True,
            "proxied": False,
        }
    body = spec_writer.scaffold_body(scope_id=scope_id, title=title, spec_urn=spec_urn)
    file_sha = spec_writer.write_spec_file(file_path, body)
    entry = spec_writer.build_entry(
        spec_urn=spec_urn,
        file_sha=file_sha,
        file_path=file_path,
        repo_root=repo_root,
        status="DRAFT",
    )
    spec_writer.write_cache_entry(phase_id=phase_id, entry=entry)
    return {
        "operation": "init",
        "scope_id": scope_id,
        "spec_urn": spec_urn,
        "status": "DRAFT",
        "file_path": entry.file_path,
        "file_sha": entry.file_sha,
        "envelope": {},
        "idempotent_replay": False,
        "proxied": False,
    }


def _inprocess_validate(
    *,
    scope_id: str,
    repo_code: str,
    repo_root: Path,
) -> dict[str, Any]:
    """Daemonless-path fallback for ``spec validate``."""
    from eawf.kernel.spec import cache as spec_cache
    from eawf.kernel.spec import writer as spec_writer

    spec_writer.classify_scope(scope_id)
    spec_urn = spec_writer.build_spec_urn(scope_id, repo_code=repo_code)
    file_path = spec_writer.spec_file_path(scope_id, repo_root=repo_root)
    if not file_path.is_file():
        raise cli_errors.UserError(
            f"spec file missing for scope_id={scope_id!r}: {file_path}", kind="NotFound"
        )
    phase_id = spec_writer.phase_of(scope_id)
    existing = spec_cache.find_cached_entry(spec_urn, phase_id=phase_id)
    if existing is None:
        raise cli_errors.UserError(
            f"scope_id={scope_id!r} not initialised; run `eawf spec init` first", kind="NotFound"
        )
    body = file_path.read_bytes()
    file_sha = spec_writer.blob_sha_for(body)
    entry = spec_writer.build_entry(
        spec_urn=spec_urn,
        file_sha=file_sha,
        file_path=file_path,
        repo_root=repo_root,
        status=existing.status,
        archived_commit=existing.archived_commit,
    )
    spec_writer.write_cache_entry(phase_id=phase_id, entry=entry)
    return {
        "operation": "validate",
        "scope_id": scope_id,
        "spec_urn": spec_urn,
        "status": existing.status,
        "file_path": entry.file_path,
        "file_sha": entry.file_sha,
        "envelope": {},
        "idempotent_replay": False,
        "proxied": False,
    }


# ---- Verbs ---------------------------------------------------------------


@spec_app.command("init")
def spec_init_cmd(
    ctx: typer.Context,
    scope_id: Annotated[
        str,
        typer.Argument(help="Scope id: P##, P##-I##, or P##-I##-W##."),
    ],
    title: Annotated[
        str,
        typer.Option("--title", help="Required title written into the scaffolded spec."),
    ],
    repo_code: Annotated[
        str,
        typer.Option("--repo-code", help="Project code symbol used as the URN owner."),
    ],
) -> None:
    """Scaffold a new spec via daemon proxy (or in-process fallback).

    Refuses to overwrite an existing spec file — re-init the same
    scope returns the cached entry verbatim (idempotent).
    """
    from pydantic import ValidationError as PydValidationError

    from eawf.cli._mutation import _daemon_reachable

    flags: GlobalFlags = ctx.obj
    repo_root = (flags.workspace or Path.cwd()).resolve()

    if _daemon_proxy_enabled_for_spec():
        if not _daemon_reachable():
            raise cli_errors.StateConflict(
                "daemon_required: daemon.proxy_enabled=true but the daemon is unreachable; "
                "run `eawf daemon start` or set EAWF_DAEMONLESS=1 for the V1 carve-out",
                kind="IntegrityViolation",
            )
        try:
            with DaemonClient() as client:
                result = client.spec_init(
                    scope_id=scope_id,
                    title=title,
                    repo_code=repo_code,
                    repo_root=str(repo_root),
                )
        except DaemonRpcError as exc:
            if exc.code == -32601:
                logger.debug("spec_init daemon method-not-found; falling back to in-process")
            elif exc.code == -32602:
                raise cli_errors.ValidationError(exc.message) from exc
            else:
                raise
        else:
            result["proxied"] = True
            _emit_result(result, flags=flags)
            return

    try:
        result = _inprocess_init(
            scope_id=scope_id,
            title=title,
            repo_code=repo_code,
            repo_root=repo_root,
        )
    except PydValidationError as exc:
        raise cli_errors.ValidationError(str(exc)) from exc
    except ValueError as exc:
        raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
    _emit_result(result, flags=flags)


@spec_app.command("validate")
def spec_validate_cmd(
    ctx: typer.Context,
    scope_id: Annotated[str, typer.Argument(help="Scope id.")],
    repo_code: Annotated[
        str,
        typer.Option("--repo-code", help="Project code symbol used as the URN owner."),
    ],
) -> None:
    """Re-hash the on-disk spec body + refresh the daemon cache row."""
    from eawf.cli._mutation import _daemon_reachable

    flags: GlobalFlags = ctx.obj
    repo_root = (flags.workspace or Path.cwd()).resolve()

    if _daemon_proxy_enabled_for_spec():
        if not _daemon_reachable():
            raise cli_errors.StateConflict(
                "daemon_required: daemon.proxy_enabled=true but the daemon is unreachable; "
                "run `eawf daemon start` or set EAWF_DAEMONLESS=1 for the V1 carve-out",
                kind="IntegrityViolation",
            )
        try:
            with DaemonClient() as client:
                result = client.spec_validate(
                    scope_id=scope_id,
                    repo_code=repo_code,
                    repo_root=str(repo_root),
                )
        except DaemonRpcError as exc:
            if exc.code == -32601:
                logger.debug("spec_validate daemon method-not-found; falling back to in-process")
            elif exc.code == -32602:
                raise cli_errors.ValidationError(exc.message) from exc
            else:
                raise
        else:
            result["proxied"] = True
            _emit_result(result, flags=flags)
            return

    try:
        result = _inprocess_validate(
            scope_id=scope_id,
            repo_code=repo_code,
            repo_root=repo_root,
        )
    except ValueError as exc:
        raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
    _emit_result(result, flags=flags)


@spec_app.command("promote")
def spec_promote_cmd(
    ctx: typer.Context,
    scope_id: Annotated[str, typer.Argument(help="Scope id.")],
    repo_code: Annotated[
        str,
        typer.Option("--repo-code", help="Project code symbol used as the URN owner."),
    ],
    to: Annotated[
        str,
        typer.Option("--to", help="Target status: READY or IMPLEMENTED."),
    ],
) -> None:
    """Forward-graduate DRAFT → READY → IMPLEMENTED through the daemon.

    Promotion is always daemon-mediated — the lifecycle gate
    (predecessor-only forward step) is enforced inside the daemon
    handler so concurrent CLIs see one consistent view of the
    cached status.
    """
    from eawf.cli._mutation import _daemon_reachable

    flags: GlobalFlags = ctx.obj
    if to not in {"READY", "IMPLEMENTED"}:
        raise cli_errors.UserError(
            f"--to must be 'READY' or 'IMPLEMENTED', got {to!r}", kind="InvalidInput"
        )
    repo_root = (flags.workspace or Path.cwd()).resolve()

    if not _daemon_proxy_enabled_for_spec() or not _daemon_reachable():
        raise cli_errors.StateConflict(
            "daemon_required: spec promote requires the daemon up; run `eawf daemon start`",
            kind="IntegrityViolation",
        )

    try:
        with DaemonClient() as client:
            result = client.spec_promote(
                scope_id=scope_id,
                repo_code=repo_code,
                target_status=to,
                repo_root=str(repo_root),
            )
    except DaemonRpcError as exc:
        if exc.code == -32602:
            raise cli_errors.ValidationError(exc.message) from exc
        raise
    result["proxied"] = True
    _emit_result(result, flags=flags)


@spec_app.command("archive")
def spec_archive_cmd(
    ctx: typer.Context,
    scope_id: Annotated[str, typer.Argument(help="Scope id.")],
    repo_code: Annotated[
        str,
        typer.Option("--repo-code", help="Project code symbol used as the URN owner."),
    ],
) -> None:
    """Atomically ``git rm`` the spec file + write the archived cache entry.

    Archive is always daemon-mediated — the ``git rm`` + cache write
    atomicity (so a recovery walk via ``git log -- <path>`` finds
    the blob) sits inside the daemon process.
    """
    from eawf.cli._mutation import _daemon_reachable

    flags: GlobalFlags = ctx.obj
    repo_root = (flags.workspace or Path.cwd()).resolve()

    if not _daemon_proxy_enabled_for_spec() or not _daemon_reachable():
        raise cli_errors.StateConflict(
            "daemon_required: spec archive requires the daemon up; run `eawf daemon start`",
            kind="IntegrityViolation",
        )

    try:
        with DaemonClient() as client:
            result = client.spec_archive(
                scope_id=scope_id,
                repo_code=repo_code,
                repo_root=str(repo_root),
            )
    except DaemonRpcError as exc:
        if exc.code == -32602:
            raise cli_errors.ValidationError(exc.message) from exc
        raise
    result["proxied"] = True
    _emit_result(result, flags=flags)


# ---- Read-only surface ----------------------------------------------------


def _parse_spec_urn(urn: str) -> tuple[str, str]:
    """Return ``(phase_id, scope_id)`` parsed from *urn*.

    Args:
        urn: ``urn:eawf:v1:spec:<repo>/<phase>[/<iter>[/<wave>]]``.

    Returns:
        Tuple of phase symbol + best-effort scope id reconstructed
        from the URN tail. Scope id uses the canonical hyphenated
        form (``P##-I##`` / ``P##-I##-W##``) so it can feed back into
        :func:`spec_writer.spec_file_path`.

    Raises:
        UserError: When *urn* does not parse as a spec URN (``kind="InvalidInput"``).
    """
    from eawf.kernel.state.urn import parse as parse_urn

    try:
        parsed = parse_urn(urn)
    except ValueError as exc:
        raise cli_errors.UserError(f"invalid spec URN: {exc}", kind="InvalidInput") from exc
    if parsed.kind != "spec":
        raise cli_errors.UserError(
            f"URN kind must be 'spec' for `spec show`, got {parsed.kind!r}", kind="InvalidInput"
        )
    if parsed.id is None:
        raise cli_errors.UserError(f"spec URN missing id: {urn!r}", kind="InvalidInput")
    parts = parsed.id.split("/")
    if not parts:
        raise cli_errors.UserError(f"spec URN id is empty: {urn!r}", kind="InvalidInput")
    phase_id = parts[0]
    # URN tail tokens carry the full hyphenated form
    # (``P25`` / ``P25-I01`` / ``P25-I01-W03``); the most-specific
    # token is the canonical scope id.
    scope_id = parts[-1]
    return phase_id, scope_id


def _git_log_recover(*, repo_root: Path, repo_relative_path: str) -> str | None:
    """Walk ``git log -- <path>`` and return the most recent blob body.

    Used by ``eawf spec show <urn> --from-git`` to recover an
    archived spec body when the file is no longer on disk. Returns
    ``None`` when git has no history for the path.
    """
    cmd_log = [
        "git",
        "log",
        "--format=%H",
        "--",
        repo_relative_path,
    ]
    try:
        completed = subprocess.run(
            cmd_log,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    shas = [line for line in completed.stdout.splitlines() if line]
    for sha in shas:
        cmd_show = ["git", "show", f"{sha}:{repo_relative_path}"]
        try:
            blob = subprocess.run(
                cmd_show,
                cwd=repo_root,
                capture_output=True,
                check=False,
            )
        except OSError:
            continue
        if blob.returncode == 0 and blob.stdout:
            return blob.stdout.decode("utf-8", errors="replace")
    return None


@spec_app.command("show")
def spec_show_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help="Spec URN to show.")],
    from_git: Annotated[
        bool,
        typer.Option("--from-git", help="Recover the body via `git log -- <path>` when archived."),
    ] = False,
) -> None:
    """Print a spec body (cache + on-disk; ``--from-git`` walks history).

    The recovery ladder per success criterion 3:

    1. Consult the daemon-resident cache for ``file_path`` +
       ``status`` + ``file_sha``.
    2. When the file exists on disk: print it.
    3. When the file is missing AND ``--from-git`` is set: walk
       ``git log -- <path>`` for the most recent blob and print
       that body.
    4. Anything else: raise :class:`UserError` (``kind="NotFound"``).
    """
    from eawf.kernel.spec import cache as spec_cache

    flags: GlobalFlags = ctx.obj
    repo_root = (flags.workspace or Path.cwd()).resolve()
    phase_id, _scope_id = _parse_spec_urn(urn)
    try:
        entry = spec_cache.find_cached_entry(urn, phase_id=phase_id)
    except spec_cache.SpecCacheReadError as exc:
        raise cli_errors.ValidationError(str(exc)) from exc
    if entry is None:
        raise cli_errors.UserError(f"spec URN not found in cache: {urn!r}", kind="NotFound")
    on_disk = repo_root / entry.file_path
    if on_disk.is_file():
        body = on_disk.read_text(encoding="utf-8")
        emit_json_or_text(
            {
                "spec_urn": urn,
                "status": entry.status,
                "file_path": entry.file_path,
                "body": body,
            },
            body,
            flags=flags,
        )
        return
    if not from_git:
        raise cli_errors.UserError(
            f"spec file missing on disk: {on_disk}; pass --from-git to walk git history",
            kind="NotFound",
        )
    recovered = _git_log_recover(
        repo_root=repo_root,
        repo_relative_path=entry.file_path,
    )
    if recovered is None:
        raise cli_errors.UserError(
            f"no git history for spec file: {entry.file_path}", kind="NotFound"
        )
    emit_json_or_text(
        {
            "spec_urn": urn,
            "status": entry.status,
            "file_path": entry.file_path,
            "body": recovered,
            "recovered_from": "git_log",
        },
        recovered,
        flags=flags,
    )


__all__ = ["spec_app"]
