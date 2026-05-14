"""``eawf status`` — current pointers, blockers, last audits, git head.

Output payload (JSON envelope, all keys always present):

.. code-block:: json

    {
      "project": {"code": "QR", "title": "Quant Research", "status": "active"} | null,
      "scope_kind": "repo" | "workspace",
      "current": {"project_code", "subproject_id", "phase_id",
                  "iter_id", "active_wave_ids", "active_session_ids"},
      "last_phase_audit": {"id", "verdict", "kind"} | null,
      "last_iter_audit":  {"id", "verdict", "kind"} | null,
      "active_waves": [<Wave>...],
      "active_sessions": [<AgentSession>...],
      "last_closed_waves": ["P01-I01-W01", ...],
      "git": {"head": "<sha>" | null,
              "branch": "<name>" | null,
              "dirty": true | false | null},
      "blockers": ["<short-text>", ...]
    }

Exit codes:

- ``0`` on success.
- ``2`` (``NOT_FOUND``) when no ``state.json`` is found via the resolver.
- ``3`` (``INVALID_INPUT``) when the resolved state file fails Pydantic
  schema validation (callers should rerun ``eawf validate`` to inspect).

The text branch prints a compact human summary; the JSON branch is the
canonical machine surface and the only one the acceptance gate inspects.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer
from pydantic import ValidationError

from eawf.cli import errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.lifecycle.wave_sha import derive_wave_sha
from eawf.state.enums import WaveStatus
from eawf.state.models import State
from eawf.state.resolve import resolve_with_reason

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS: float = 5.0


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Return ``git`` stdout (stripped) or ``None`` on any failure.

    Wraps :func:`subprocess.run` with a hard timeout so a hung git invocation
    cannot stall ``eawf status``. Any non-zero exit, missing binary, or
    timeout collapses to ``None`` — callers must treat git fields as
    optional. Successful runs return the stripped stdout (which may be the
    empty string, distinct from ``None``).
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired:
        return None
    return proc.stdout.strip()


def _find_git_root(start: Path) -> Path:
    """Walk up from *start* to the first ancestor containing ``.git``.

    Falls back to *start* (resolved) when no ``.git`` is found in the
    ancestor chain — git invocations from there will return ``None``
    via :func:`_run_git`'s safe-degrade path. Used so a custom
    ``--state-path`` outside ``<repo>/.ea/`` still locates the
    enclosing repo for the head/branch/dirty queries.
    """
    p = start.resolve()
    for ancestor in (p, *p.parents):
        if (ancestor / ".git").exists():
            return ancestor
    return p


def _git_info(cwd: Path) -> dict[str, str | bool | None]:
    """Return ``{head, branch, dirty}`` for *cwd*.

    Each field is ``None`` independently when its query fails. A clean tree
    yields ``dirty=False`` (porcelain returned empty stdout); a dirty tree
    yields ``dirty=True``; a failed porcelain query collapses ``dirty`` to
    ``None``. ``head`` is the empty-string-coerced-None when stdout is empty
    so callers can render it as ``"<unknown>"``.
    """
    head_raw = _run_git(["rev-parse", "HEAD"], cwd)
    branch_raw = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    porcelain = _run_git(["status", "--porcelain"], cwd)
    head: str | None = head_raw if head_raw else None
    branch: str | None = branch_raw if branch_raw else None
    dirty: bool | None = bool(porcelain) if porcelain is not None else None
    return {"head": head, "branch": branch, "dirty": dirty}


def _last_audit(state: State, audit_id: str | None) -> dict[str, str | None] | None:
    """Resolve *audit_id* against ``state.audits`` and return a small projection."""
    if audit_id is None or state.audits is None:
        return None
    audit = state.audits.get(audit_id)
    if audit is None:
        return None
    return {
        "id": audit.id,
        "kind": audit.kind.value,
        "verdict": audit.verdict.value if audit.verdict is not None else None,
    }


def _project_summary(state: State) -> dict[str, str] | None:
    """Return a compact ``{code, title, status}`` projection of ``state.project``.

    When ``state.project`` is ``None`` but ``state.current.project_code`` is
    populated (the v0.1 ``eawf init`` contract — project code stamped, full
    ``Project`` record deferred to ``eawf project init``), fall back to the
    current pointer with ``status="uninitialised"`` and the title pulled from
    ``state.indexes`` (empty string when missing). This stops a fresh init
    from rendering ``project: <none>`` even though the code is on disk.
    """
    if state.project is None:
        if state.current.project_code is None:
            return None
        title_raw = state.indexes.get("project_title", "")
        title = title_raw if isinstance(title_raw, str) else ""
        return {
            "code": state.current.project_code,
            "title": title,
            "status": "uninitialised",
        }
    return {
        "code": state.project.code,
        "title": state.project.title,
        "status": state.project.status.value,
    }


def _active_waves(state: State) -> list[dict[str, Any]]:
    """Return JSON-serialisable projections of every active wave.

    "Active" follows the same set used by ``INV.CURRENT.WAVE_NOT_ACTIVE``
    (``claimed`` or ``in_progress``). Waves listed in
    ``state.current.active_wave_ids`` but missing from ``state.waves`` are
    skipped silently — the validator will surface them.
    """
    out: list[dict[str, Any]] = []
    for wave_id in state.current.active_wave_ids:
        wave = state.waves.get(wave_id)
        if wave is None:
            continue
        out.append(
            {
                "id": wave.id,
                "iter_id": wave.iter_id,
                "title": wave.title,
                "status": wave.status.value,
                "claim_session_id": wave.claim_session_id,
                "commit": derive_wave_sha(wave.id),
            }
        )
    return out


def _active_sessions(state: State) -> list[dict[str, Any]]:
    """Return JSON-serialisable projections of every session in ``current.active_session_ids``."""
    out: list[dict[str, Any]] = []
    for sid in state.current.active_session_ids:
        sess = state.agent_sessions.get(sid)
        if sess is None:
            continue
        out.append(
            {
                "id": sess.id,
                "role": sess.role.value,
                "runtime": sess.runtime,
                "scope_id": sess.scope_id,
                "status": sess.status.value,
            }
        )
    return out


def _last_closed_waves(state: State, limit: int = 10) -> list[str]:
    """Return up to *limit* recently-closed wave IDs, newest first by ``closed_at``.

    Waves with ``closed_at`` unset are filtered before sorting so the comparator
    never sees ``None``. We keep ``(closed_at, id)`` pairs and index back to ids
    after sorting so the lambda's return type is concrete.
    """
    pairs: list[tuple[datetime, str]] = [
        (w.closed_at, w.id)
        for w in state.waves.values()
        if w.status == WaveStatus.CLOSED and w.closed_at is not None
    ]
    pairs.sort(key=lambda p: p[0], reverse=True)
    return [wave_id for _, wave_id in pairs[:limit]]


def _blockers(state: State) -> list[str]:
    """Surface short human-readable blockers from the loaded state.

    The list is best-effort — callers needing the canonical answer should run
    ``eawf validate``. We only report a few cheap, common cases here:

    - Active wave with no claim session.
    - Active iter with no open phase.
    - More than one active session for the same scope (suggests dual-driver).
    """
    out: list[str] = []
    for wave_id in state.current.active_wave_ids:
        wave = state.waves.get(wave_id)
        if wave is not None and wave.claim_session_id is None:
            out.append(f"wave {wave.id} active but unclaimed")
    cp = state.current
    if cp.iter_id is not None and cp.phase_id is None:
        out.append(f"iter {cp.iter_id} active but no phase pointer set")
    seen: dict[str, str] = {}
    for sid in cp.active_session_ids:
        sess = state.agent_sessions.get(sid)
        if sess is None:
            continue
        if sess.scope_id in seen:
            out.append(f"sessions {seen[sess.scope_id]} and {sess.id} share scope {sess.scope_id}")
        seen[sess.scope_id] = sess.id
    return out


def status(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "-w",
            "--workspace",
            help="Workspace root for state.json resolution (overrides pwd-upward).",
        ),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Optional scope ID (informational; not yet filtered)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a machine-readable JSON envelope (overrides root --json).",
        ),
    ] = False,
) -> None:
    """Print active pointers, blockers, and git head for the current state."""
    flags: GlobalFlags = ctx.obj
    # ``scope`` is captured for forward compatibility (per-command option) but
    # not yet consumed by the rendering path — see :mod:`eawf.cli.flags` for
    # why ``--scope`` is intentionally not a global flag.
    _ = scope
    effective_flags = GlobalFlags(
        json_output=flags.json_output or json_output,
        plain_output=flags.plain_output,
        no_input=flags.no_input,
        workspace=workspace if workspace is not None else flags.workspace,
    )

    state_path, _reason = resolve_with_reason(workspace=effective_flags.workspace)
    if not state_path.exists():
        errors.emit_error(
            errors.NotFound(f"no state.json at {state_path}"),
            flags=effective_flags,
        )
        return

    try:
        payload_dict = orjson.loads(state_path.read_bytes())
        state = State.model_validate(payload_dict)
    except ValidationError as exc:
        errors.emit_error(
            errors.InvalidInput(f"state file failed schema validation: {exc.errors()[0]['msg']}"),
            flags=effective_flags,
        )
        return

    last_phase = (
        _last_audit(state, state.phases[state.current.phase_id].audit_id)
        if state.current.phase_id is not None and state.current.phase_id in state.phases
        else None
    )
    last_iter = (
        _last_audit(state, state.iters[state.current.iter_id].audit_id)
        if state.current.iter_id is not None and state.current.iter_id in state.iters
        else None
    )

    payload: dict[str, Any] = {
        "project": _project_summary(state),
        "scope_kind": state.scope_kind.value,
        "current": state.current.model_dump(mode="json"),
        "last_phase_audit": last_phase,
        "last_iter_audit": last_iter,
        "active_waves": _active_waves(state),
        "active_sessions": _active_sessions(state),
        "last_closed_waves": _last_closed_waves(state),
        "git": _git_info(cwd=_find_git_root(state_path.parent)),
        "blockers": _blockers(state),
    }

    text = _format_text(payload)
    emit_json_or_text(payload, text, flags=effective_flags)


def _format_text(payload: dict[str, Any]) -> str:
    """Render *payload* as a compact multi-line summary for human consumption."""
    proj = payload.get("project")
    if proj:
        # Title is the human-friendly slug; when missing (uninitialised
        # fallback path with no ``--project-title`` set) fall back to the
        # status string so the operator sees ``project: REPRO (uninitialised)``
        # instead of an empty parenthesised group.
        descriptor = proj["title"] or proj["status"]
        proj_line = f"project: {proj['code']} ({descriptor})"
    else:
        proj_line = "project: <none>"
    cur = payload["current"]
    cur_line = (
        f"current: phase={cur['phase_id']} iter={cur['iter_id']} "
        f"waves={','.join(cur['active_wave_ids']) or '<none>'}"
    )
    git = payload["git"]
    branch = git["branch"] or "<unknown>"
    git_line = f"git: head={(git['head'] or '<unknown>')[:12]} branch={branch}"
    blockers = payload["blockers"]
    blockers_line = f"blockers: {', '.join(blockers) if blockers else 'none'}"
    return "\n".join([proj_line, cur_line, git_line, blockers_line])
