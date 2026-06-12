"""``eawf wave review`` — PR-review attachment + prompt-prep verb (B041).

This module owns the wave-side review automation. It does NOT touch
the live reviewer-subagent dispatch — the caller drives the agent
themselves (e.g. via ``cavecrew-reviewer``) and either:

* passes the resulting findings document back via ``--findings``
  (state mutation path), or
* passes a ``--diff`` path to render the canonical reviewer prompt
  for the agent to consume (no state mutation, pure rendering path).

The two arguments are mutually exclusive. Either kind of invocation
emits a single envelope on stdout.

Wiring: the verb hangs off ``wave_app`` (defined in
:mod:`eawf.surfaces.cli.commands.lifecycle`). We register it here via
cross-module import — matching the W03 ``wave land`` pattern — so the
parallel-wave discipline stays intact and W05 can keep ownership of
``lifecycle.py``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.kernel.state.enums import AuditKind, AuditVerdict, StoreKind
from eawf.kernel.state.ids import is_wave_id
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import wave_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.kernel.state.models import State, Wave
    from eawf.workflow.pr_review import Finding
    from eawf.workflow.pr_review.policy import ReviewVerdict

logger = logging.getLogger(__name__)


# ---- Public verb -----------------------------------------------------------


@wave_app.command(name="review")
def wave_review_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID under review.")],
    findings: Annotated[
        Path | None,
        typer.Option(
            "--findings",
            help=(
                "Path to the caveman-reviewer Markdown findings document. "
                "Parses the file, attaches an audit (kind=review) with the "
                "derived verdict, and emits the review envelope."
            ),
        ),
    ] = None,
    diff: Annotated[
        Path | None,
        typer.Option(
            "--diff",
            help=(
                "Path to a diff to be reviewed. Renders the wave's review "
                "prompt to stdout (no state mutation). Mutually exclusive "
                "with --findings."
            ),
        ),
    ] = None,
    audit_id: Annotated[
        str | None,
        typer.Option(
            "--audit-id",
            help=(
                "Explicit audit id. When omitted, the next free "
                "A<NN>-<wave-id> id is allocated automatically."
            ),
        ),
    ] = None,
) -> None:
    """Attach review findings to a wave, or render a reviewer prompt.

    Modes:

    * ``--findings <path>``: parse the findings document, allocate an
      audit (``kind=review``) for the wave's scope, attach the
      derived verdict, and emit
      ``{"wave", "audit", "verdict", "summary", "finding_count"}``.
    * ``--diff <path>``: render the wave's review prompt (the standard
      :func:`render_wave_prompt` output plus a ``## Review prompt``
      section instructing the reviewer to emit canonical
      ``path:line: <emoji> <severity>: ...`` lines) and emit
      ``{"wave", "diff_path", "review_request"}``. No state mutation.

    Exit codes follow the canonical CLI map:
    NOT_FOUND (2) for a missing ``--findings`` file or unknown
    ``--wave-id``; INVALID_INPUT (3) when ``--findings`` and ``--diff``
    are both supplied or both omitted; VALIDATION_FAILED (4) when the
    audit transition rejects the supplied audit id.
    """
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return

    if findings is not None and diff is not None:
        cli_errors.emit_error(
            cli_errors.UserError(
                "exactly one of --findings or --diff must be provided", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    if findings is None and diff is None:
        cli_errors.emit_error(
            cli_errors.UserError(
                "exactly one of --findings or --diff must be provided", kind="InvalidInput"
            ),
            flags=flags,
        )
        return

    if diff is not None:
        _emit_review_request(ctx, flags, wave_id=wave_id, diff_path=diff)
        return

    # findings is not None — verified above.
    assert findings is not None
    _attach_findings(
        flags,
        wave_id=wave_id,
        findings_path=findings,
        audit_id_override=audit_id,
    )


# ---- Mode 1: --diff (no state mutation) ------------------------------------


def _emit_review_request(
    ctx: typer.Context,
    flags: GlobalFlags,
    *,
    wave_id: str,
    diff_path: Path,
) -> None:
    """Render the wave's reviewer prompt and emit the envelope.

    The diff path is not required to exist on disk — the caller may be
    pointing at a temp file the reviewer will materialise — but the
    caller is responsible for materialising it before handing the
    rendered prompt to the agent.
    """
    from eawf.surfaces.cli.commands.lifecycle import _load_state_readonly
    from eawf.workflow.dispatch import render_wave_prompt

    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, _flags = loaded
    if wave_id not in state.waves:
        cli_errors.emit_error(
            cli_errors.UserError(f"unknown wave: {wave_id}", kind="NotFound"),
            flags=flags,
        )
        return

    base_prompt = render_wave_prompt(state, wave_id)
    review_section = _render_review_prompt_section(diff_path=diff_path)
    review_request = base_prompt.rstrip() + "\n\n" + review_section + "\n"

    envelope: dict[str, Any] = {
        "wave": wave_id,
        "diff_path": str(diff_path),
        "review_request": review_request,
    }
    text = f"wave review {wave_id} (prompt-prep)\ndiff: {diff_path}\n\n{review_request}"
    emit_json_or_text(envelope, text, flags=flags)


def _render_review_prompt_section(*, diff_path: Path) -> str:
    """Return the trailing ``## Review prompt`` section.

    Instructs the reviewer to emit findings in the canonical
    ``path:line: <emoji> <severity>: <problem>. <fix>.`` format so the
    output can be fed straight back into ``wave review --findings``.
    """
    return (
        "## Review prompt\n"
        "\n"
        f"Review the diff at `{diff_path}` for this wave.\n"
        "\n"
        "Emit one finding per line in the canonical format:\n"
        "\n"
        "    path:line: <emoji> <severity>: <problem>. <fix>.\n"
        "\n"
        "Severity emojis:\n"
        "\n"
        "- \U0001f534 blocker: ship-stopping (security, data loss, "
        "wrong logic at API boundary).\n"
        "- \U0001f7e0 must-fix: correctness or contract violation that "
        "must land before merge.\n"
        "- \U0001f7e1 should-fix: code quality, naming, structure — "
        "address before next phase.\n"
        "- \U0001f535 nit: optional polish; defer if rushed.\n"
        "\n"
        "Lines without the `path:line:` prefix are ignored as commentary "
        "by the downstream parser. Pin every finding to a path; use "
        "`path::` (empty line number) only when the issue cannot be "
        "localised to one line."
    )


# ---- Mode 2: --findings (state mutation) -----------------------------------


def _attach_findings(
    flags: GlobalFlags,
    *,
    wave_id: str,
    findings_path: Path,
    audit_id_override: str | None,
) -> None:
    """Parse *findings_path*, attach the audit, emit the envelope."""
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import artifact as artifact_evi
    from eawf.workflow.evidence import audit as audit_evi
    from eawf.workflow.evidence._io import append_jsonl, store_contains_envelope, store_paths
    from eawf.workflow.pr_review import parse_findings, summary_line, verdict_for

    if not findings_path.exists():
        cli_errors.emit_error(
            cli_errors.UserError(f"findings file not found: {findings_path}", kind="NotFound"),
            flags=flags,
        )
        return
    raw = findings_path.read_text(encoding="utf-8")
    try:
        parsed = parse_findings(raw)
    except ValueError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"malformed findings document: {exc}", kind="InvalidInput"),
            flags=flags,
        )
        return

    review_verdict: ReviewVerdict = verdict_for(parsed)
    audit_verdict = _map_to_audit_verdict(review_verdict)
    tally = summary_line(parsed)

    state_path = _resolve_state_path(flags)
    if state_path is None:
        return

    # We hold the state lock for the entire transaction: resolve the
    # wave's scope, allocate the audit id (if absent), seed the
    # report artifact (a stable digest of the findings file), then
    # add the audit with verdict in one shot.
    final_audit_id: str = ""
    final_scope_id: str = ""
    try:
        with state_transaction(state_path) as state:
            wave = _resolve_wave_or_raise(state, wave_id)
            scope_id = _resolve_wave_scope(state, wave)
            final_scope_id = scope_id
            allocated_audit_id = audit_id_override or _allocate_audit_id(state, wave_id=wave_id)
            final_audit_id = allocated_audit_id

            artifact_id = f"ART-REVIEW-{allocated_audit_id}"
            artifact_uri = _repo_relative_findings_uri(findings_path, state_path)
            try:
                artifact_sha = _file_sha256(findings_path)
                artifact_size = findings_path.stat().st_size
            except OSError as exc:
                raise cli_errors.UserError(
                    f"findings file disappeared between existence check and read: {exc}",
                    kind="NotFound",
                ) from exc
            event_art = artifact_evi.add_artifact(
                state,
                artifact_id=artifact_id,
                kind="review_findings",
                uri=artifact_uri,
                scope_id=scope_id,
                sha256=artifact_sha,
                size_bytes=artifact_size,
                metadata={
                    "wave_id": wave_id,
                    "summary": tally,
                    "review_verdict": review_verdict,
                },
            )
            record_audit, event_audit = audit_evi.add_audit(
                state,
                audit_id=allocated_audit_id,
                scope_id=scope_id,
                kind=AuditKind.REVIEW,
                report_artifact_id=artifact_id,
                verdict=audit_verdict,
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.EVENT], event_art)
            if record_audit is not None and event_audit is not None:
                if not store_contains_envelope(paths[StoreKind.AUDIT], record_audit):
                    append_jsonl(paths[StoreKind.AUDIT], record_audit)
                append_jsonl(paths[StoreKind.EVENT], event_audit)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    envelope: dict[str, Any] = {
        "wave": wave_id,
        "scope_id": final_scope_id,
        "audit": final_audit_id,
        "verdict": review_verdict,
        "summary": tally,
        "finding_count": len(parsed),
    }
    text = _render_findings_text(
        wave_id=wave_id,
        audit_id=final_audit_id,
        review_verdict=review_verdict,
        tally=tally,
        findings=parsed,
    )
    emit_json_or_text(envelope, text, flags=flags)


# ---- Helpers ---------------------------------------------------------------


def _resolve_state_path(flags: GlobalFlags) -> Path | None:
    """Resolve the active state path or emit the canonical error envelope."""
    try:
        return resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return None


def _resolve_wave_or_raise(state: State, wave_id: str) -> Wave:
    """Look up *wave_id*; raise :class:`UserError` (``kind="NotFound"``) when missing."""
    wave = state.waves.get(wave_id)
    if wave is None:
        raise cli_errors.UserError(f"unknown wave: {wave_id}", kind="NotFound")
    return wave


def _resolve_wave_scope(state: State, wave: Wave) -> str:
    """Walk wave → iter → phase → scope_id; raise on a broken edge."""
    it = state.iters.get(wave.iter_id)
    if it is None:
        raise cli_errors.UserError(
            f"wave {wave.id!r} references unknown iter {wave.iter_id!r}", kind="NotFound"
        )
    phase = state.phases.get(it.phase_id)
    if phase is None:
        raise cli_errors.UserError(
            f"iter {it.id!r} references unknown phase {it.phase_id!r}", kind="NotFound"
        )
    return phase.scope_id


def _allocate_audit_id(state: State, *, wave_id: str) -> str:
    """Return the next free ``A<NN>-<wave-id>`` audit id.

    Strategy: scan existing audits for ids that already follow the
    ``A<NN>-<wave_id>`` pattern and pick the smallest free
    two-digit suffix. New projects start at ``A01-<wave_id>``.
    """
    suffix = f"-{wave_id}"
    pattern = re.compile(rf"^A(\d{{2}}){re.escape(suffix)}$")
    used: set[int] = set()
    for aid in state.audits or {}:
        match = pattern.match(aid)
        if match is not None:
            used.add(int(match.group(1)))
    for n in range(1, 100):
        if n not in used:
            return f"A{n:02d}{suffix}"
    raise cli_errors.ValidationError(f"audit-id allocation saturated for wave {wave_id!r}")


def _map_to_audit_verdict(review_verdict: ReviewVerdict) -> AuditVerdict:
    """Map the review-verdict literal to the storage-side AuditVerdict.

    * ``"approve"`` → :attr:`AuditVerdict.PASS`
    * ``"comment-only"`` → :attr:`AuditVerdict.MINOR`
    * ``"request-changes"`` → :attr:`AuditVerdict.MAJOR`
    """
    if review_verdict == "approve":
        return AuditVerdict.PASS
    if review_verdict == "comment-only":
        return AuditVerdict.MINOR
    return AuditVerdict.MAJOR


def _repo_relative_findings_uri(findings_path: Path, state_path: Path) -> str:
    """Return a repo-relative ``repo:`` URI for the findings file.

    Falls back to a synthetic ``repo:.ea/review/<wave>.md`` URI when
    the findings file lives outside the repo root — the artifact
    store accepts ``repo:`` URIs as opaque pointers, so we just need
    one that round-trips.
    """
    repo_root = state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent
    try:
        rel = findings_path.resolve().relative_to(repo_root.resolve())
        return f"repo:{rel.as_posix()}"
    except ValueError:
        # findings_path lies outside the repo root.
        # Hash the absolute path to keep the URI deterministic without
        # leaking machine-specific path info into the state store.
        digest = hashlib.sha256(str(findings_path).encode("utf-8")).hexdigest()[:12]
        return f"repo:.ea/review/external-{digest}.md"


def _file_sha256(path: Path) -> str:
    """Return the hex SHA-256 of *path*."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _render_findings_text(
    *,
    wave_id: str,
    audit_id: str,
    review_verdict: ReviewVerdict,
    tally: str,
    findings: list[Finding],
) -> str:
    """Render the human-readable findings table for text mode."""
    header = (
        f"wave review {wave_id} -> audit {audit_id} verdict={review_verdict}\n  summary: {tally}\n"
    )
    if not findings:
        return header + "  findings: (none)"
    lines = [header, "  findings:"]
    for f in findings:
        loc = f"{f.path}:{f.line}" if f.line is not None else f"{f.path}:?"
        lines.append(f"    [{f.severity}] {loc} — {f.message}")
    return "\n".join(lines)


__all__ = ["wave_review_cmd"]
